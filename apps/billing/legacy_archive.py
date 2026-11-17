"""历史财务四表归档服务（归档门禁 §5 结构 + §4.2 checksum-v2 的落地实现）。

职责边界：
1. **只读旧表**（hotelapp 的 folio / finance_record / night_audit / accommodation），
   不写不改旧表——旧表处置属 D5/D6，本模块不越权；
2. **只写归档表**（legacy_*_archive）与批次元数据（LegacyArchiveRun）；
3. 归档行摘要与整表摘要全部由 :mod:`apps.billing.legacy_checksum` 在 Python 侧计算，
   不依赖任何 DB 方言函数（SQLite/MySQL 同数据必须同摘要）。

归档不可变：``LegacyArchiveRecord.save()`` 拒绝二次保存、QuerySet 拒绝批量改删。
同一批次重复归档同一张表一律拒绝（摘要登记后不可修改，重跑会污染对位关系）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.billing import models as billing_models
from apps.billing.legacy_checksum import build_metadata, row_digest, table_digest
from apps.core.models import Property


class LegacyArchiveError(Exception):
    """归档受阻（批次重复、源表不可读、摘要不一致）。"""


@dataclass(frozen=True)
class ArchiveSpec:
    """一张旧表的归档契约：源模型 → 归档模型 + 列映射（列序即摘要顺序）。"""

    source_model: str
    source_table: str
    archive_table: str
    # 归档字段名 → 旧表字段名；元组顺序 = 归档表列序 = checksum-v2 编码顺序。
    column_map: tuple[tuple[str, str], ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(archive_field for archive_field, _ in self.column_map)

    @property
    def archive_model(self) -> Any:
        return {
            "legacy_folio_archive": billing_models.LegacyFolioArchive,
            "legacy_finance_record_archive": billing_models.LegacyFinanceRecordArchive,
            "legacy_night_audit_archive": billing_models.LegacyNightAuditArchive,
            "legacy_accommodation_archive": billing_models.LegacyAccommodationArchive,
        }[self.archive_table]


ARCHIVE_SPECS: tuple[ArchiveSpec, ...] = (
    ArchiveSpec(
        source_model="Folio",
        source_table="folio",
        archive_table="legacy_folio_archive",
        column_map=(
            ("id", "id"),
            ("order_id", "order_id"),
            ("entry_type", "类型"),
            ("amount_text", "金额"),
            ("subject", "事由"),
            ("date_text", "日期"),
            ("handler", "经手人"),
        ),
    ),
    ArchiveSpec(
        source_model="FinanceRecord",
        source_table="finance_record",
        archive_table="legacy_finance_record_archive",
        column_map=(
            ("id", "id"),
            ("record_type", "类型"),
            ("amount_text", "金额"),
            ("subject", "事由"),
            ("related_order", "关联订单"),
            ("date_text", "日期"),
            ("handler", "经手人"),
        ),
    ),
    ArchiveSpec(
        source_model="NightAudit",
        source_table="night_audit",
        archive_table="legacy_night_audit_archive",
        column_map=(
            ("id", "id"),
            ("business_date_text", "营业日期"),
            ("in_house_count", "在住单数"),
            ("rollover_total_text", "过租总额"),
            ("no_show_count", "未到处理数"),
            ("operator", "操作人"),
            ("executed_at_text", "执行时间"),
        ),
    ),
    ArchiveSpec(
        source_model="Accommodation",
        source_table="accommodation",
        archive_table="legacy_accommodation_archive",
        column_map=(
            ("id", "id"),
            ("client_id", "顾客id"),
            ("room_id", "客房编号"),
            ("check_in_text", "入住时间"),
            ("status", "状态"),
            ("contact_phone", "联系电话"),
            ("received_amount_text", "收款金额"),
            ("deposit_text", "押金"),
            ("check_out_text", "退房时间"),
            ("reversed_by", "冲账人"),
            ("reversed_at_text", "冲账时间"),
        ),
    ),
)

_BY_MODEL = {spec.source_model: spec for spec in ARCHIVE_SPECS}


def get_spec(source_model: str) -> ArchiveSpec:
    try:
        return _BY_MODEL[source_model]
    except KeyError as exc:
        raise LegacyArchiveError(
            f"未知归档来源模型 {source_model!r}；可选：{', '.join(sorted(_BY_MODEL))}"
        ) from exc


def _source_queryset(spec: ArchiveSpec) -> Any:
    """延迟导入旧表模型：避免 apps 在 app registry 就绪前耦合 hotelapp。"""
    from hotelapp import models as legacy_models

    model = {
        "Folio": legacy_models.Folio,
        "FinanceRecord": legacy_models.FinanceRecord,
        "NightAudit": legacy_models.NightAudit,
        "Accommodation": legacy_models.Accommodation,
    }[spec.source_model]
    return model.objects.all()


def unknown_source_primary_keys(
    spec: ArchiveSpec, source_pks: Iterable[str], chunk_size: int = 500
) -> list[str]:
    """只查询映射文件涉及的主键，分块校验，避免为物业映射重复读取整张旧表。"""
    requested = sorted({str(source_pk) for source_pk in source_pks})
    if not requested:
        return []

    source_queryset = _source_queryset(spec)
    pk_field = source_queryset.model._meta.pk
    converted: list[tuple[str, Any]] = []
    unknown: set[str] = set()
    for raw_pk in requested:
        try:
            converted_pk = pk_field.to_python(raw_pk)
        except (TypeError, ValueError, ValidationError):
            unknown.add(raw_pk)
            continue
        if str(converted_pk) != raw_pk:
            unknown.add(raw_pk)
            continue
        converted.append((raw_pk, converted_pk))

    converted_values = list({value for _, value in converted})
    existing: set[str] = set()
    for offset in range(0, len(converted_values), chunk_size):
        chunk = converted_values[offset : offset + chunk_size]
        existing.update(
            str(pk) for pk in source_queryset.filter(pk__in=chunk).values_list("pk", flat=True)
        )

    unknown.update(raw_pk for raw_pk, value in converted if str(value) not in existing)
    return sorted(unknown)


def read_source_rows(spec: ArchiveSpec) -> list[dict[str, Any]]:
    """按归档列序读取旧表（只读）；返回的键为**归档字段名**。"""
    source_fields = [source_field for _, source_field in spec.column_map]
    rows: list[dict[str, Any]] = []
    for raw in _source_queryset(spec).values(*source_fields).order_by("id"):
        rows.append(
            {archive_field: raw[source_field] for archive_field, source_field in spec.column_map}
        )
    return rows


def summarize_source(spec: ArchiveSpec) -> dict[str, Any]:
    """只读盘点（dry-run 用）：行数 + min/max 主键，不写任何表。"""
    rows = read_source_rows(spec)
    return {
        "source_model": spec.source_model,
        "source_table": spec.source_table,
        "archive_table": spec.archive_table,
        "row_count": len(rows),
        "min_pk": min((row["id"] for row in rows), default=None),
        "max_pk": max((row["id"] for row in rows), default=None),
    }


def archive_table(
    *,
    spec: ArchiveSpec,
    batch_uid: str,
    operator: str,
    property_assignments: Mapping[str, Property] | None = None,
) -> Any:
    """归档一张旧表：逐行写归档行（含行摘要）+ 登记整表摘要元数据。

    幂等口径：同一 (batch_uid, source_table) 只允许归档一次——重跑直接拒绝，
    不静默覆盖（归档事实不可变，覆盖会破坏"归档行=源表快照"的对位关系）。
    """
    batch_uid = str(batch_uid or "").strip()
    operator = str(operator or "").strip()
    if not batch_uid or len(batch_uid) > 64:
        raise LegacyArchiveError("归档批次号必须为 1 至 64 个字符。")
    if not operator:
        raise LegacyArchiveError("归档执行人不能为空（元数据须登记执行人）。")
    if billing_models.LegacyArchiveRun.objects.filter(
        batch_uid=batch_uid, source_table=spec.source_table
    ).exists():
        raise LegacyArchiveError(
            f"批次 {batch_uid} 已归档 {spec.source_table}；归档事实不可变，禁止重跑覆盖。"
        )

    columns = spec.columns
    rows = read_source_rows(spec)
    assignments = {
        str(source_pk): property_obj
        for source_pk, property_obj in (property_assignments or {}).items()
    }
    source_pks = {str(row["id"]) for row in rows}
    unknown_pks = sorted(set(assignments) - source_pks)
    if unknown_pks:
        raise LegacyArchiveError(
            "物业归属映射包含当前源表不存在的主键：" + ", ".join(unknown_pks[:20])
        )
    invalid_assignments = [
        source_pk
        for source_pk, property_obj in assignments.items()
        if not isinstance(property_obj, Property) or property_obj.pk is None
    ]
    if invalid_assignments:
        raise LegacyArchiveError(
            "物业归属必须引用已保存的 Property：" + ", ".join(invalid_assignments[:20])
        )
    property_ids = {property_obj.pk for property_obj in assignments.values()}
    existing_property_ids = set(
        Property.objects.filter(pk__in=property_ids).values_list("pk", flat=True)
    )
    missing_property_assignments = [
        source_pk
        for source_pk, property_obj in assignments.items()
        if property_obj.pk not in existing_property_ids
    ]
    if missing_property_assignments:
        raise LegacyArchiveError(
            "物业归属引用了已不存在的 Property：" + ", ".join(missing_property_assignments[:20])
        )

    batch = billing_models.LegacyMigrationBatch.objects.filter(batch_uid=batch_uid).first()
    if batch is not None:
        mismatched = sorted(
            source_pk
            for source_pk, property_obj in assignments.items()
            if property_obj.pk != batch.scope_property_id
        )
        if mismatched:
            raise LegacyArchiveError(
                "物业快照与迁移批次 scope_property 不一致：" + ", ".join(mismatched[:20])
            )

    digests = [row_digest(row, columns).hex() for row in rows]
    summary = table_digest(rows, columns, "id")
    meta = build_metadata(
        source_table=spec.archive_table,
        rows=rows,
        pk_field="id",
        table_summary=summary,
        computed_at=datetime.now(timezone.utc),
        operator=operator,
    )
    archive_model = spec.archive_model

    with transaction.atomic():
        for row, digest in zip(rows, digests):
            archive_model(
                **row,
                archive_batch=batch_uid,
                verified_checksum=digest,
                property=assignments.get(str(row["id"])),
            ).save()
        run = billing_models.LegacyArchiveRun.objects.create(
            batch_uid=batch_uid,
            source_table=spec.source_table,
            archive_table=spec.archive_table,
            algorithm_version=meta["algorithm_version"],
            row_count=meta["row_count"],
            min_pk=meta["min_pk"],
            max_pk=meta["max_pk"],
            table_digest=meta["table_digest"],
            computed_at=datetime.strptime(meta["computed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ),
            operator=operator,
        )
    return run


def verify_archive_run(run: Any) -> dict[str, Any]:
    """D4 恢复演练/收口核验：从归档表重算摘要与行数，与登记元数据比对。"""
    spec = get_spec_by_table(run.source_table)
    columns = spec.columns
    rows = list(
        spec.archive_model.objects.filter(archive_batch=run.batch_uid)
        .values(*columns)
        .order_by("id")
    )
    # 空表也要算：table_digest(()) = SHA-256(空串)，与归档时同一算法，缺省成 None 会误报不一致。
    digest = table_digest(rows, columns, "id")
    ok = digest == run.table_digest and len(rows) == run.row_count
    return {
        "batch_uid": run.batch_uid,
        "source_table": run.source_table,
        "expected_rows": run.row_count,
        "actual_rows": len(rows),
        "expected_digest": run.table_digest,
        "actual_digest": digest,
        "ok": ok,
    }


def get_spec_by_table(source_table: str) -> ArchiveSpec:
    for spec in ARCHIVE_SPECS:
        if spec.source_table == source_table:
            return spec
    raise LegacyArchiveError(f"未知源表 {source_table!r}")


def archive_manifest(batch_uid: str) -> Sequence[Any]:
    return list(
        billing_models.LegacyArchiveRun.objects.filter(batch_uid=batch_uid).order_by("source_table")
    )
