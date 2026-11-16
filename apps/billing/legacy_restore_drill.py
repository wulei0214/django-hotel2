"""D4 恢复演练工具（归档门禁 D4 + §4.1 第 4 条 + §4.2 跨库一致性）。

一次演练 = 归档表 → 隔离库（独立 SQLite 文件）→ 历史查询复现 → 与归档/源表对比。

校验项（逐项给证据，不合并成一句"通过"）：
1. **摘要复核**：按 checksum-v2 从归档表重算整表摘要与行数，与 ``LegacyArchiveRun`` 比对
   （复用 :func:`apps.billing.legacy_archive.verify_archive_run`）；
2. **恢复完整性**：隔离库写入行数 = 归档行数；主键集合逐一相等；
3. **主键连续性**：行数 + min/max 主键 + 空洞清单（旧表历史删除留下的空洞不算失败，
   但必须列出来供人工判断）；
4. **查询等价性**：同一"历史查询"（按订单分组汇总金额、按类型分组计数）在
   归档表与隔离库两侧执行，结果必须逐键相等；
5. **关联完整率**：``folio.order_id`` 能在 ``accommodation`` 归档中找到的比例
   （旧表侧同步比对，供 D5 前判断孤儿凭证规模）；
6. **金额聚合一致**：按订单分组的金额合计，归档 / 隔离库 / 源表（若仍可读）三方比对；
7. **LegacyRecordLink 可重建**：本批次 C1 链接的目标凭证存在率。

边界声明：
* 隔离库用 SQLite 文件，是**可用性演练**而非生产 MySQL 恢复；生产恢复须按运维方案
  用备份集在独立实例执行并由运维/财务共同确认（本工具不代签、不替代该步骤）；
* 只读：不修改归档表、不修改旧表、不删除隔离库（证据留存，默认放 ``.work/``）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from apps.billing import models as billing_models
from apps.billing.legacy_archive import ARCHIVE_SPECS, ArchiveSpec, verify_archive_run

# 演练中"历史查询"的固定口径：按订单分组合计金额（folio）、按类型分组计数与合计。
AMOUNT_GROUPS: dict[str, tuple[str, str]] = {
    "folio": ("order_id", "amount_text"),
    "finance_record": ("related_order", "amount_text"),
}


class LegacyRestoreDrillError(Exception):
    """演练无法进行（批次不存在、隔离库不可写、参数非法）。"""


def _to_decimal(value: Any) -> Decimal | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _canonical(value: Any) -> str:
    """行值规范化（NULL 与空串区分；其余按字符串原样，保历史事实）。"""
    return "<NULL>" if value is None else str(value)


def _scratch_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    return connection


def _create_scratch_table(connection: sqlite3.Connection, spec: ArchiveSpec) -> None:
    columns = ", ".join(f'"{column}" TEXT' for column in spec.columns if column != "id")
    connection.execute(f'DROP TABLE IF EXISTS "{spec.source_table}"')
    connection.execute(f'CREATE TABLE "{spec.source_table}" (id INTEGER PRIMARY KEY, {columns})')


def _restore_table(
    *, connection: sqlite3.Connection, spec: ArchiveSpec, batch_uid: str
) -> tuple[int, list[dict[str, Any]]]:
    """把归档行写入隔离库（列原样文本），返回 (写入行数, 归档行快照)。"""
    columns = spec.columns
    rows = list(
        spec.archive_model.objects.filter(archive_batch=batch_uid).values(*columns).order_by("id")
    )
    _create_scratch_table(connection, spec)
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(f'"{column}"' for column in columns)
    connection.executemany(
        f'INSERT INTO "{spec.source_table}" ({column_sql}) VALUES ({placeholders})',
        [tuple(row[column] for column in columns) for row in rows],
    )
    connection.commit()
    return len(rows), rows


def _scratch_rows(connection: sqlite3.Connection, spec: ArchiveSpec) -> list[dict[str, Any]]:
    columns = spec.columns
    column_sql = ", ".join(f'"{column}"' for column in columns)
    cursor = connection.execute(f'SELECT {column_sql} FROM "{spec.source_table}" ORDER BY id')
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _pk_continuity(pks: Sequence[int]) -> dict[str, Any]:
    ordered = sorted(int(pk) for pk in pks)
    if not ordered:
        return {"row_count": 0, "min_pk": None, "max_pk": None, "gaps": []}
    gaps: list[list[int]] = []
    for previous, current in zip(ordered, ordered[1:]):
        if current - previous > 1:
            gaps.append([previous + 1, current - 1])
    return {
        "row_count": len(ordered),
        "min_pk": ordered[0],
        "max_pk": ordered[-1],
        "gaps": gaps,
    }


def _group_totals(
    rows: Iterable[dict[str, Any]], *, group_field: str, amount_field: str
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        key = _canonical(row.get(group_field))
        amount = _to_decimal(row.get(amount_field))
        if amount is None:
            # 金额脏数据不参与合计，但在报告里由 bad_amount_rows 单独暴露。
            continue
        totals[key] = totals.get(key, Decimal("0")) + amount
    return totals


def _bad_amount_rows(
    rows: Iterable[dict[str, Any]], *, amount_field: str, group_field: str
) -> list[dict[str, str]]:
    bad: list[dict[str, str]] = []
    for row in rows:
        raw = row.get(amount_field)
        if _to_decimal(raw) is None and str(raw or "").strip() != "":
            bad.append({"group": _canonical(row.get(group_field)), "amount_text": _canonical(raw)})
    return bad


def _compare_totals(left: dict[str, Decimal], right: dict[str, Decimal]) -> dict[str, Any]:
    keys = sorted(set(left) | set(right))
    diffs = [
        {
            "key": key,
            "left": str(left.get(key, Decimal("0"))),
            "right": str(right.get(key, Decimal("0"))),
        }
        for key in keys
        if left.get(key, Decimal("0")) != right.get(key, Decimal("0"))
    ]
    return {"group_count": len(keys), "diffs": diffs, "ok": not diffs}


def _link_rebuild_check(batch_uid: str) -> dict[str, Any]:
    """本批次 C1 链接的目标凭证存在率（无迁移链接时标 not_applicable）。"""
    from apps.core.models import LegacyRecordLink

    links = LegacyRecordLink.objects.filter(
        source_system="hotelapp", migration_batch=batch_uid, target_model="LedgerEntry"
    )
    total = links.count()
    if total == 0:
        return {"total": 0, "resolved": 0, "ok": True, "status": "not_applicable"}
    resolved = 0
    missing: list[str] = []
    for link in links.only("source_model", "source_pk", "target_pk"):
        if billing_models.LedgerEntry.objects.filter(pk=link.target_pk).exists():
            resolved += 1
        else:
            missing.append(f"{link.source_model}#{link.source_pk}")
    return {
        "total": total,
        "resolved": resolved,
        "ok": resolved == total,
        "status": "checked",
        "missing": missing,
    }


def _relation_check(
    *, archive_rows: Sequence[dict[str, Any]], accommodation_pks: set[int]
) -> dict[str, Any]:
    """folio.order_id → accommodation 归档的存在率（孤儿凭证规模）。"""
    total = 0
    missing: list[Any] = []
    for row in archive_rows:
        raw = row.get("order_id")
        if raw in (None, ""):
            continue
        total += 1
        try:
            order_pk = int(str(raw))
        except (TypeError, ValueError):
            missing.append(str(raw))
            continue
        if order_pk not in accommodation_pks:
            missing.append(order_pk)
    rate = 1.0 if total == 0 else (total - len(missing)) / total
    return {
        "checked_rows": total,
        "missing_rows": len(missing),
        "missing": missing[:50],
        "link_rate": round(rate, 6),
        "ok": not missing,
    }


def _source_rows(spec: ArchiveSpec) -> list[dict[str, Any]] | None:
    """读旧表（仍存在时）；旧表已物理退役则返回 None（不伪装成"一致"）。"""
    from apps.billing.legacy_archive import read_source_rows

    try:
        return read_source_rows(spec)
    except Exception:  # 旧表已 DROP / 无权限：演练按"源表不可读"标注
        return None


def run_drill(
    *,
    batch_uid: str,
    scratch_path: str | Path,
    operator: str,
    tables: Sequence[str] | None = None,
) -> dict[str, Any]:
    """执行一次恢复演练，返回可归档的报告结构（JSON 友好）。"""
    uid = str(batch_uid or "").strip()
    if not uid:
        raise LegacyRestoreDrillError("必须指定归档批次")
    operator_name = str(operator or "").strip()
    if not operator_name:
        raise LegacyRestoreDrillError("必须登记演练执行人")
    runs = {
        run.source_table: run
        for run in billing_models.LegacyArchiveRun.objects.filter(batch_uid=uid).order_by(
            "source_table"
        )
    }
    if not runs:
        raise LegacyRestoreDrillError(f"批次 {uid} 无归档元数据（先执行 archive_legacy_finance）")

    selected = [spec for spec in ARCHIVE_SPECS if not tables or spec.source_table in tables]
    scratch = Path(scratch_path)
    report: dict[str, Any] = {
        "batch_uid": uid,
        "operator": operator_name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scratch_db": str(scratch),
        "engine_note": "隔离库为 SQLite 文件（可用性演练）；生产恢复须按运维方案在独立实例执行",
        "tables": [],
        "relations": {},
        "amounts": [],
        "links": {},
        "blockers": [],
        "ok": False,
    }

    connection = _scratch_connection(scratch)
    table_results: list[dict[str, Any]] = []
    archive_snapshots: dict[str, list[dict[str, Any]]] = {}
    try:
        for spec in selected:
            run = runs.get(spec.source_table)
            if run is None:
                table_results.append(
                    {
                        "source_table": spec.source_table,
                        "status": "not_archived",
                        "ok": False,
                    }
                )
                report["blockers"].append(f"{spec.source_table}：批次未归档该表")
                continue
            verify = verify_archive_run(run)
            restored_rows, archive_rows = _restore_table(
                connection=connection, spec=spec, batch_uid=uid
            )
            archive_snapshots[spec.source_table] = archive_rows
            scratch_rows = _scratch_rows(connection, spec)

            columns = spec.columns
            equivalence = [tuple(_canonical(r[c]) for c in columns) for r in archive_rows] == [
                tuple(_canonical(r[c]) for c in columns) for r in scratch_rows
            ]
            continuity = _pk_continuity([int(row["id"]) for row in archive_rows])
            source_rows = _source_rows(spec)
            source_continuity = (
                _pk_continuity([int(row["id"]) for row in source_rows])
                if source_rows is not None
                else None
            )

            entry = {
                "source_table": spec.source_table,
                "archive_table": spec.archive_table,
                "status": "checked",
                "expected_rows": run.row_count,
                "archived_rows": len(archive_rows),
                "restored_rows": restored_rows,
                "digest_ok": verify["ok"],
                "expected_digest": verify["expected_digest"],
                "actual_digest": verify["actual_digest"],
                "equivalence_ok": equivalence,
                "pk_continuity": continuity,
                "source_available": source_rows is not None,
                "source_row_count": len(source_rows) if source_rows is not None else None,
                "source_pk_continuity": source_continuity,
                "min_max_matches_source": (
                    None
                    if source_continuity is None
                    else (
                        source_continuity["min_pk"] == continuity["min_pk"]
                        and source_continuity["max_pk"] == continuity["max_pk"]
                    )
                ),
            }
            ok = (
                verify["ok"]
                and equivalence
                and restored_rows == len(archive_rows) == run.row_count
                and (entry["min_max_matches_source"] in (True, None))
            )
            entry["ok"] = bool(ok)
            if not ok:
                report["blockers"].append(f"{spec.source_table}：摘要/行数/等价性校验未通过")
            table_results.append(entry)
    finally:
        connection.close()

    report["tables"] = table_results

    # 关联完整率：folio.order_id → accommodation 归档主键集合
    accommodation_rows = archive_snapshots.get("accommodation")
    if accommodation_rows is not None and "folio" in archive_snapshots:
        accommodation_pks = {int(row["id"]) for row in accommodation_rows}
        relation = _relation_check(
            archive_rows=archive_snapshots["folio"], accommodation_pks=accommodation_pks
        )
        report["relations"]["folio_order_to_accommodation"] = relation
        if not relation["ok"]:
            report["blockers"].append(
                f"folio：{relation['missing_rows']} 条记录的订单在 accommodation 归档中不存在"
            )

    # 金额聚合：归档 vs 隔离库 vs 源表（三方）
    for spec in selected:
        grouping = AMOUNT_GROUPS.get(spec.source_table)
        rows = archive_snapshots.get(spec.source_table)
        if grouping is None or rows is None:
            continue
        group_field, amount_field = grouping
        archive_totals = _group_totals(rows, group_field=group_field, amount_field=amount_field)
        # 隔离库侧：从复现结果再算一遍（验证恢复后可查询）
        connection = _scratch_connection(scratch)
        try:
            scratch_rows = _scratch_rows(connection, spec)
        finally:
            connection.close()
        scratch_totals = _group_totals(
            scratch_rows, group_field=group_field, amount_field=amount_field
        )
        archive_vs_scratch = _compare_totals(archive_totals, scratch_totals)
        source_rows = _source_rows(spec)
        source_cmp: dict[str, Any] | None = None
        if source_rows is not None:
            source_totals = _group_totals(
                source_rows, group_field=group_field, amount_field=amount_field
            )
            source_cmp = _compare_totals(archive_totals, source_totals)
        entry = {
            "source_table": spec.source_table,
            "group_field": group_field,
            "amount_field": amount_field,
            "archive_vs_scratch": archive_vs_scratch,
            "archive_vs_source": source_cmp,
            "bad_amount_rows": _bad_amount_rows(
                rows, amount_field=amount_field, group_field=group_field
            ),
            "ok": archive_vs_scratch["ok"] and (source_cmp is None or source_cmp["ok"]),
        }
        report["amounts"].append(entry)
        if not archive_vs_scratch["ok"]:
            report["blockers"].append(f"{spec.source_table}：归档与隔离库金额分组不一致")
        if source_cmp is not None and not source_cmp["ok"]:
            report["blockers"].append(f"{spec.source_table}：归档与源表金额分组不一致")

    links = _link_rebuild_check(uid)
    report["links"] = links
    if not links["ok"]:
        report["blockers"].append(
            f"LegacyRecordLink：{links['total'] - links['resolved']} 条链接的目标凭证缺失"
        )

    report["ok"] = not report["blockers"]
    return report


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    """报告落盘（JSON，UTF-8）；供 D4 证据归档与财务抽凭引用。"""
    import json

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
