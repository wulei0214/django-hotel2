"""历史财务迁移分类与执行（迁移方案 §1 分类守恒 + §4 执行蓝图）。

设计约束（不得自行放宽）：
1. **分类只由证据决定**：C1 只在其订单-预订关系已被人工确认时成立；C2/C3
   （新旧重复事实）**禁止自动判定**——必须由人工在映射文件中显式声明候选凭证，
   命令行不做"同金额同日期"式猜测（§2 R6-01 解读：多候选禁止自动判定）。
2. **C1 才产生新凭证**，且只能经 :meth:`BillingService.post_legacy_migration_entry`
   （签名凭证 + scope + 人工确认）；C2/C3 只建映射，C4/C5 **零新凭证**
   （分类泄漏 = 阻断级缺陷）。
3. **裁定口径**：旧 Folio「消费」→ C5 原样归档（裁定 1）；旧 FinanceRecord「支出」
   → C5 归档，不扩展 LedgerEntry 类型（裁定 2）。
4. 失败不静默：无法解析/无法配对/无关系映射的记录一律 C4 并留 CONFLICT 映射，
   附原因码，不做"猜测式迁移"。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry, LegacyMigrationBatch
from apps.core.models import LegacyRecordLink

AMOUNT_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 旧 Folio 类型 → 新凭证类型 + 金额符号（新口径：房费为正、押金/收款为负、冲账原符号）。
FOLIO_TYPE_MAP: dict[str, tuple[str, int]] = {
    "押金": (LedgerEntry.EntryType.DEPOSIT, -1),
    "房费": (LedgerEntry.EntryType.ROOM_CHARGE, 1),
    "收款": (LedgerEntry.EntryType.PAYMENT, -1),
    "冲账": (LedgerEntry.EntryType.REVERSAL, 1),
}
# 裁定 1：「消费」不映射任何新凭证类型——C5 原样归档。
FOLIO_ARCHIVE_ONLY_TYPES = frozenset({"消费"})
# 裁定 2：FinanceRecord 只迁移「收入」（→ PAYMENT），「支出」无对应凭证类型 → C5。
FINANCE_TYPE_MAP: dict[str, tuple[str, int]] = {
    "收入": (LedgerEntry.EntryType.PAYMENT, -1),
}
FINANCE_ARCHIVE_ONLY_TYPES = frozenset({"支出"})

# 映射方式标记：对账按 (source_model, match_method) 分组核验分类守恒。
METHOD_C1 = "legacy_migration"
METHOD_C2 = "legacy_migration_covered"
METHOD_C3 = "legacy_migration_duplicate"
METHOD_C4 = "legacy_migration_conflict"
METHOD_C5 = "legacy_migration_archive_only"
METHOD_BY_CATEGORY = {
    "C1": METHOD_C1,
    "C2": METHOD_C2,
    "C3": METHOD_C3,
    "C4": METHOD_C4,
    "C5": METHOD_C5,
}


class LegacyMigrationError(Exception):
    """迁移计划/映射文件不合法（结构性错误，不是单条记录的业务分类）。"""


@dataclass(frozen=True)
class Decision:
    """单条旧记录的分类结论（C1~C5 之一 + 依据）。"""

    source_model: str
    source_pk: str
    category: str
    reason: str
    entry_type: str = ""
    amount: Decimal | None = None
    business_date: date | None = None
    target_folio_id: int | None = None
    candidate_entry_id: int | None = None
    void_of_pk: int | None = None
    reversal_confirmation: dict[str, Any] | None = None
    description: str = ""

    @property
    def reference(self) -> str:
        return f"legacy:{self.source_model}:{self.source_pk}"


@dataclass
class Plan:
    """一个批次的迁移计划：分类结论 + 汇总。"""

    decisions: list[Decision] = field(default_factory=list)

    def by_category(self) -> dict[str, int]:
        counts = {f"C{i}": 0 for i in range(1, 6)}
        for decision in self.decisions:
            counts[decision.category] = counts.get(decision.category, 0) + 1
        return counts

    def category(self, name: str) -> list[Decision]:
        return [d for d in self.decisions if d.category == name]


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_amount(raw: Any) -> Decimal:
    text = "" if raw is None else str(raw).strip()
    if not AMOUNT_RE.match(text):
        raise LegacyMigrationError(f"金额格式非法：{raw!r}")
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - 正则已拦住绝大多数
        raise LegacyMigrationError(f"金额无法解析：{raw!r}") from exc


def parse_business_date(raw: Any) -> date:
    """严格解析 YYYY-MM-DD：格式 + 真实存在 + roundtrip 三重校验（§2 R6-02）。"""
    text = "" if raw is None else str(raw).strip()
    if not DATE_RE.match(text):
        raise LegacyMigrationError(f"日期格式非法：{raw!r}")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise LegacyMigrationError(f"日期不存在：{raw!r}") from exc
    if parsed.isoformat() != text:  # pragma: no cover - fromisoformat 已规范化
        raise LegacyMigrationError(f"日期 roundtrip 不一致：{raw!r}")
    return parsed


# --------------------------------------------------------------------------- #
# 映射文件
# --------------------------------------------------------------------------- #
def load_mapping(path: str | Path) -> dict[str, Any]:
    """读取人工确认映射文件（JSON）。

    结构::

        {
          "orders": {"<旧订单id>": {"reservation": 201, "basis": "...", "confirmed_by": "..."}},
          "candidates": {"<source_model>:<pk>": {"category": "C2",
                                                 "entry": 5001, "note": "..."}},
          "reversal_pairs": {"<旧Folio pk>": {"void_reference": "legacy:Folio:12",
                                              "confirmed_by": "...", "basis": "..."}}
        }
    """
    raw = Path(path)
    if not raw.exists():
        raise LegacyMigrationError(f"映射文件不存在：{raw}")
    try:
        payload = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LegacyMigrationError(f"映射文件不可解析：{exc}") from exc
    if not isinstance(payload, dict):
        raise LegacyMigrationError("映射文件根节点必须是对象")
    for key in ("orders", "candidates", "reversal_pairs"):
        value = payload.setdefault(key, {})
        if not isinstance(value, dict):
            raise LegacyMigrationError(f"映射文件 {key} 必须是对象")
    return payload


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def _resolve_target_folio(
    mapping: Mapping[str, Any], order_ref: Any, *, batch: LegacyMigrationBatch
) -> tuple[Folio | None, str]:
    """订单 → 人工确认的新预订 → MASTER 账单；任一环节缺失都返回原因码。"""
    key = "" if order_ref is None else str(order_ref).strip()
    if not key:
        return None, "旧凭证无订单关联（孤儿凭证）"
    if not key.isdigit():
        return None, f"订单号非数字：{order_ref!r}"
    entry = (mapping.get("orders") or {}).get(key)
    if not isinstance(entry, dict) or entry.get("reservation") in (None, ""):
        return None, f"订单 {key} 未建立订单-预订关系映射（禁止判定 C1）"
    try:
        reservation_id = int(entry["reservation"])
    except (TypeError, ValueError):
        return None, f"订单 {key} 映射的 reservation 非法"
    folio = Folio.objects.filter(
        reservation_id=reservation_id, folio_type=Folio.FolioType.MASTER
    ).first()
    if folio is None:
        return None, f"预订 {reservation_id} 无 MASTER 账单"
    if folio.property_id != batch.scope_property_id:
        return None, f"预订 {reservation_id} 账单物业与批次授权物业不一致"
    return folio, ""


def _candidate_decision(
    mapping: Mapping[str, Any], *, source_model: str, source_pk: str
) -> Decision | None:
    """人工显式声明的 C2/C3 候选（禁止自动判定，故只认文件里的声明）。"""
    entry = (mapping.get("candidates") or {}).get(f"{source_model}:{source_pk}")
    if not isinstance(entry, dict):
        return None
    category = str(entry.get("category") or "").upper()
    if category not in {"C2", "C3"}:
        raise LegacyMigrationError(
            f"候选声明 {source_model}:{source_pk} 的 category 必须是 C2 或 C3"
        )
    try:
        candidate_entry_id = int(entry["entry"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LegacyMigrationError(f"候选声明 {source_model}:{source_pk} 缺少合法 entry") from exc
    if not LedgerEntry.objects.filter(pk=candidate_entry_id).exists():
        raise LegacyMigrationError(
            f"候选声明 {source_model}:{source_pk} 指向的凭证 {candidate_entry_id} 不存在"
        )
    return Decision(
        source_model=source_model,
        source_pk=str(source_pk),
        category=category,
        reason=str(entry.get("note") or f"人工确认 {category}（新旧重复事实）"),
        candidate_entry_id=candidate_entry_id,
    )


def classify_folio_row(
    row: Mapping[str, Any], mapping: Mapping[str, Any], *, batch: LegacyMigrationBatch
) -> Decision:
    """旧 Folio 单行分类（C1~C5，逐条给原因码）。"""
    source_pk = str(row["id"])
    legacy_type = str(row.get("类型") or "").strip()
    if legacy_type in FOLIO_ARCHIVE_ONLY_TYPES:
        return Decision(
            source_model="Folio",
            source_pk=source_pk,
            category="C5",
            reason=f"旧凭证类型「{legacy_type}」按裁定 1 原样归档，不建新凭证",
        )
    if legacy_type not in FOLIO_TYPE_MAP:
        return Decision(
            source_model="Folio",
            source_pk=source_pk,
            category="C4",
            reason=f"未知旧凭证类型：{legacy_type!r}",
        )
    try:
        amount = parse_amount(row.get("金额"))
        business_date = parse_business_date(row.get("日期"))
    except LegacyMigrationError as exc:
        return Decision(source_model="Folio", source_pk=source_pk, category="C4", reason=str(exc))

    entry_type, sign = FOLIO_TYPE_MAP[legacy_type]
    signed = amount * sign
    if entry_type == LedgerEntry.EntryType.REVERSAL:
        pair = (mapping.get("reversal_pairs") or {}).get(source_pk)
        if not isinstance(pair, dict) or not str(pair.get("confirmed_by") or "").strip():
            return Decision(
                source_model="Folio",
                source_pk=source_pk,
                category="C4",
                reason="旧冲账未配对或缺少人工确认（禁止伪造 void_of 链）",
            )
        void_reference = str(pair.get("void_reference") or "").strip()
        void_of = LedgerEntry.objects.filter(reference=void_reference).first()
        if void_of is None:
            return Decision(
                source_model="Folio",
                source_pk=source_pk,
                category="C4",
                reason=f"冲销原凭证尚未迁移：{void_reference or '(未提供)'}",
            )
        return Decision(
            source_model="Folio",
            source_pk=source_pk,
            category="C1",
            reason=f"人工确认冲销配对（{pair.get('basis') or '人工确认'}）",
            entry_type=entry_type,
            amount=signed,
            business_date=business_date,
            target_folio_id=void_of.folio_id,
            void_of_pk=void_of.pk,
            reversal_confirmation={
                "confirmed_by": str(pair["confirmed_by"]),
                "basis": str(pair.get("basis") or ""),
            },
            description=str(row.get("事由") or "")[:240],
        )

    candidate = _candidate_decision(mapping, source_model="Folio", source_pk=source_pk)
    if candidate is not None:
        return candidate
    folio, reason = _resolve_target_folio(mapping, row.get("order_id"), batch=batch)
    if folio is None:
        return Decision(source_model="Folio", source_pk=source_pk, category="C4", reason=reason)
    return Decision(
        source_model="Folio",
        source_pk=source_pk,
        category="C1",
        reason="订单-预订关系已人工确认，可迁移",
        entry_type=entry_type,
        amount=signed,
        business_date=business_date,
        target_folio_id=folio.pk,
        description=str(row.get("事由") or "")[:240],
    )


def classify_finance_row(
    row: Mapping[str, Any], mapping: Mapping[str, Any], *, batch: LegacyMigrationBatch
) -> Decision:
    """旧 FinanceRecord 单行分类（C1~C5）。"""
    source_pk = str(row["id"])
    legacy_type = str(row.get("类型") or "").strip()
    if legacy_type in FINANCE_ARCHIVE_ONLY_TYPES:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C5",
            reason=f"旧流水类型「{legacy_type}」按裁定 2 归档，不扩展账本凭证类型",
        )
    if legacy_type not in FINANCE_TYPE_MAP:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C4",
            reason=f"未知旧流水类型：{legacy_type!r}",
        )
    related = str(row.get("关联订单") or "").strip()
    if not related:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C5",
            reason="无关联订单，资金性质待人工判定（仅归档）",
        )
    try:
        amount = parse_amount(row.get("金额"))
        business_date = parse_business_date(row.get("日期"))
    except LegacyMigrationError as exc:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C4",
            reason=str(exc),
        )
    candidate = _candidate_decision(mapping, source_model="FinanceRecord", source_pk=source_pk)
    if candidate is not None:
        return candidate
    folio, reason = _resolve_target_folio(mapping, related, batch=batch)
    if folio is None:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C4",
            reason=reason,
        )
    entry_type, sign = FINANCE_TYPE_MAP[legacy_type]
    return Decision(
        source_model="FinanceRecord",
        source_pk=source_pk,
        category="C1",
        reason="订单-预订关系已人工确认，可迁移",
        entry_type=entry_type,
        amount=amount * sign,
        business_date=business_date,
        target_folio_id=folio.pk,
        description=str(row.get("事由") or "")[:240],
    )


def build_plan(*, batch: LegacyMigrationBatch, mapping: Mapping[str, Any]) -> Plan:
    """按批次 scope 读取旧表并逐条分类（只读，不写任何表）。"""
    from hotelapp import models as legacy_models

    plan = Plan()
    scope_models = list(batch.scope_source_models or [])
    price_range = batch.scope_record_range or {}
    pk_min = price_range.get("pk_min")
    pk_max = price_range.get("pk_max")
    allowed_pks = price_range.get("pks")

    def _in_range(pk: int) -> bool:
        if isinstance(allowed_pks, list):
            return str(pk) in {str(item) for item in allowed_pks}
        if pk_min is None or pk_max is None:
            raise LegacyMigrationError("批次 scope_record_range 必须提供 pk_min/pk_max 或 pks 清单")
        return int(pk_min) <= pk <= int(pk_max)

    if "Folio" in scope_models:
        rows = (
            legacy_models.Folio.objects.all()
            .values("id", "order_id", "类型", "金额", "事由", "日期", "经手人")
            .order_by("id")
        )
        for row in rows:
            if not _in_range(int(row["id"])):
                continue
            plan.decisions.append(classify_folio_row(row, mapping, batch=batch))
    if "FinanceRecord" in scope_models:
        finance_rows = (
            legacy_models.FinanceRecord.objects.all()
            .values("id", "类型", "金额", "事由", "关联订单", "日期", "经手人")
            .order_by("id")
        )
        for row in finance_rows:
            if not _in_range(int(row["id"])):
                continue
            plan.decisions.append(classify_finance_row(row, mapping, batch=batch))
    return plan


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def _link(
    *,
    batch: LegacyMigrationBatch,
    decision: Decision,
    executor: Any,
    target_pk: str,
    method: str,
    note: str,
) -> LegacyRecordLink:
    return LegacyRecordLink.objects.create(
        property=batch.scope_property,
        source_system="hotelapp",
        source_model=decision.source_model,
        source_pk=decision.source_pk,
        target_model="LedgerEntry" if decision.category in {"C1", "C2", "C3"} else "",
        target_pk=str(target_pk or ""),
        mapping_status=(
            LegacyRecordLink.MappingStatus.LINKED
            if decision.category in {"C1", "C2", "C3"}
            else (
                LegacyRecordLink.MappingStatus.CONFLICT
                if decision.category == "C4"
                else LegacyRecordLink.MappingStatus.IGNORED
            )
        ),
        match_method=method,
        reviewed_by=executor,
        reviewed_at=timezone.now(),
        review_note=note[:240],
        migration_batch=batch.batch_uid,
    )


def execute_plan(
    *, batch: LegacyMigrationBatch, plan: Plan, credential_factory: Any, executor: Any
) -> dict[str, Any]:
    """按计划落库：C1 经受控入口建凭证，C2/C3/C4/C5 只写映射。

    C1 的凭证创建**逐条独立事务**：一条失败只回滚该条（不残留映射/审计），
    不把整批拖入"部分成功但无法定位"的状态。失败逐条收集并在返回值中返回。

    ``credential_factory`` 每次调用都必须铸造**新凭证**：执行凭证是 nonce 单次
    消费制（§5.2），同一条凭证第二次使用会被判重放——长批次按需重新铸造。
    """
    from apps.billing.legacy_credentials import LegacyMigrationDenied
    from apps.billing.services_v2 import BillingService

    counts = {"C1": 0, "C2": 0, "C3": 0, "C4": 0, "C5": 0, "failed": 0}
    failures: list[str] = []
    for decision in plan.decisions:
        method = METHOD_BY_CATEGORY[decision.category]
        try:
            if decision.category == "C1":
                folio = Folio.objects.get(pk=decision.target_folio_id)
                BillingService.post_legacy_migration_entry(
                    credential_token=credential_factory(),
                    batch=batch,
                    property_obj=batch.scope_property,
                    folio=folio,
                    entry_type=decision.entry_type,
                    amount=decision.amount,
                    reference=decision.reference,
                    source_model=decision.source_model,
                    source_pk=decision.source_pk,
                    description=decision.description,
                    business_date=decision.business_date,
                    void_of=(
                        LedgerEntry.objects.get(pk=decision.void_of_pk)
                        if decision.void_of_pk
                        else None
                    ),
                    void_reason=decision.reason if decision.void_of_pk else "",
                    reversal_confirmation=decision.reversal_confirmation,
                    review_note=f"batch={batch.batch_uid} {decision.reason}",
                )
            elif decision.category in {"C2", "C3"}:
                _link(
                    batch=batch,
                    decision=decision,
                    executor=executor,
                    target_pk=str(decision.candidate_entry_id),
                    method=method,
                    note=f"batch={batch.batch_uid} {decision.reason}",
                )
            elif decision.category == "C4":
                _link(
                    batch=batch,
                    decision=decision,
                    executor=executor,
                    target_pk="",
                    method=method,
                    note=f"batch={batch.batch_uid} {decision.reason}",
                )
            else:  # C5
                _link(
                    batch=batch,
                    decision=decision,
                    executor=executor,
                    target_pk="",
                    method=method,
                    note=f"batch={batch.batch_uid} {decision.reason}",
                )
            counts[decision.category] += 1
        except (LegacyMigrationDenied, ValueError, LegacyMigrationError) as exc:
            counts["failed"] += 1
            failures.append(f"{decision.source_model}#{decision.source_pk}: {exc}")
    return {"counts": counts, "failures": failures}


# --------------------------------------------------------------------------- #
# 对账（§1 分类守恒 + §1.2 金额守恒，按 来源模型 × 分类 × 批次 分组）
# --------------------------------------------------------------------------- #
def reconcile_batch(batch: LegacyMigrationBatch) -> dict[str, Any]:
    """按分类守恒与金额守恒核验一个批次，返回可打印的对账报告。

    守恒口径（§1.1）：
      * C1：源记录数 = 新建凭证数 = LINKED 映射数（本实现以"链接指向的凭证存在"核验）；
      * C2/C3：源记录数 = 已核验映射数，**新建凭证必须为 0**；
      * C4：源记录数 = CONFLICT 映射数；
      * C5：源记录数 = 归档（IGNORED）映射数。
    金额守恒（§1.2）：按 来源模型 × 凭证类型 × 币种 分组，Σ新凭证 = Σ旧记录（换算符号后）。
    """
    report: dict[str, Any] = {
        "batch_uid": batch.batch_uid,
        "categories": {},
        "amounts": [],
        "diffs": [],
        "archive": [],
    }
    links = LegacyRecordLink.objects.filter(
        source_system="hotelapp", migration_batch=batch.batch_uid
    )
    for source_model in ("Folio", "FinanceRecord"):
        rows: dict[str, Any] = {}
        for category, method in METHOD_BY_CATEGORY.items():
            count = links.filter(source_model=source_model, match_method=method).count()
            rows[category] = count
        c1_links = links.filter(source_model=source_model, match_method=METHOD_C1)
        missing_entries = []
        for link in c1_links.only("target_pk", "source_pk"):
            if not LedgerEntry.objects.filter(pk=link.target_pk).exists():
                missing_entries.append(link.source_pk)
        rows["C1_missing_entries"] = missing_entries
        # C2/C3 分类泄漏检查：这两类不得产生新凭证。
        leaked = [
            link.source_pk
            for link in links.filter(
                source_model=source_model, match_method__in=[METHOD_C2, METHOD_C3]
            ).only("source_pk", "target_pk")
            if LedgerEntry.objects.filter(
                reference=f"legacy:{source_model}:{link.source_pk}"
            ).exists()
        ]
        rows["leaked_new_entries"] = leaked
        report["categories"][source_model] = rows
        if missing_entries:
            report["diffs"].append(
                f"{source_model}: {len(missing_entries)} 条 C1 映射指向的凭证不存在"
            )
        if leaked:
            report["diffs"].append(
                f"{source_model}: {len(leaked)} 条 C2/C3 记录泄漏出新建凭证（阻断级）"
            )

    # 金额守恒：逐条 C1 映射比对"新凭证金额"与"旧记录（按映射符号换算）"。
    for link in links.filter(match_method=METHOD_C1).select_related("property"):
        entry = LedgerEntry.objects.filter(pk=link.target_pk).first()
        if entry is None:
            continue
        expected = _expected_signed_amount(link.source_model, link.source_pk)
        if expected is None:
            continue
        if entry.amount != expected:
            report["diffs"].append(
                f"{link.source_model}#{link.source_pk}: 新凭证 {entry.amount} ≠ 旧记录 {expected}"
            )
        report["amounts"].append(
            {
                "source_model": link.source_model,
                "source_pk": link.source_pk,
                "entry_type": entry.entry_type,
                "currency": entry.currency,
                "expected": str(expected),
                "actual": str(entry.amount),
            }
        )

    archive_rows = []
    from apps.billing.models import LegacyArchiveRun

    for run in LegacyArchiveRun.objects.filter(batch_uid=batch.batch_uid).order_by("source_table"):
        archive_rows.append(
            {
                "source_table": run.source_table,
                "row_count": run.row_count,
                "table_digest": run.table_digest,
            }
        )
    report["archive"] = archive_rows
    return report


def _expected_signed_amount(source_model: str, source_pk: str) -> Decimal | None:
    """旧记录 → 新账本符号的期望金额（读旧表只读；无法解析返回 None）。"""
    from hotelapp import models as legacy_models

    if source_model == "Folio":
        row = legacy_models.Folio.objects.filter(pk=source_pk).values("类型", "金额").first()
        if row is None:
            return None
        mapping = FOLIO_TYPE_MAP.get(str(row["类型"]))
        if mapping is None:
            return None
        try:
            return parse_amount(row["金额"]) * mapping[1]
        except LegacyMigrationError:
            return None
    if source_model == "FinanceRecord":
        row = (
            legacy_models.FinanceRecord.objects.filter(pk=source_pk).values("类型", "金额").first()
        )
        if row is None:
            return None
        mapping = FINANCE_TYPE_MAP.get(str(row["类型"]))
        if mapping is None:
            return None
        try:
            return parse_amount(row["金额"]) * mapping[1]
        except LegacyMigrationError:
            return None
    return None


def record_failures(*, batch: LegacyMigrationBatch, failures: Iterable[str]) -> None:
    """把逐条失败写入审计（独立事务，不随迁移事务回滚丢失）。"""
    from apps.core.models import AuditEvent

    for failure in failures:
        AuditEvent.record(
            action="billing.legacy_migration_failed",
            entity_type="LegacyMigrationBatch",
            entity_id=str(batch.pk),
            property_obj=batch.scope_property,
            after={"batch_uid": batch.batch_uid, "detail": failure[:500]},
            reason=failure[:240],
        )


def batch_links(batch: LegacyMigrationBatch) -> Any:
    return LegacyRecordLink.objects.filter(
        source_system="hotelapp", migration_batch=batch.batch_uid
    )


def plan_summary(plan: Plan) -> dict[str, Any]:
    counts = plan.by_category()
    counts["total"] = len(plan.decisions)
    return counts


def write_plan_json(plan: Plan, path: str | Path) -> None:
    payload = [
        {
            "source_model": d.source_model,
            "source_pk": d.source_pk,
            "category": d.category,
            "reason": d.reason,
            "entry_type": d.entry_type,
            "amount": str(d.amount) if d.amount is not None else None,
            "business_date": d.business_date.isoformat() if d.business_date else None,
            "target_folio_id": d.target_folio_id,
            "candidate_entry_id": d.candidate_entry_id,
        }
        for d in plan.decisions
    ]
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
