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
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry, LegacyMigrationBatch
from apps.core.models import LegacyRecordLink, Property
from apps.reservations.models import Reservation

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


LEGACY_REFERENCE_NAMES = {"Folio": "folio", "FinanceRecord": "finance"}


def legacy_reference(source_model: str, source_pk: str | int) -> str:
    """生成迁移凭证唯一 reference；稳定使用业务名而非 Django 模型类名。"""
    name = LEGACY_REFERENCE_NAMES.get(source_model)
    if name is None:
        raise LegacyMigrationError(f"不支持生成 {source_model} 的历史凭证 reference")
    return f"legacy:{name}:{source_pk}"


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
    blocks_batch: bool = False

    @property
    def reference(self) -> str:
        return legacy_reference(self.source_model, self.source_pk)


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
          "reversal_pairs": {"<旧Folio pk>": {"void_reference": "legacy:folio:12",
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
    mapping: Mapping[str, Any],
    order_ref: Any,
    *,
    batch: LegacyMigrationBatch,
    source_model: str,
    source_pk: str,
) -> tuple[Folio | None, str]:
    """旧订单 → 当前有效关系集合 → 逐凭证选定预订 → MASTER 账单。

    1:1/N:1 可使用唯一当前关系；1:N/N:M 必须通过 record_reservations
    按来源模型+主键逐条选择关系行，禁止按日期、金额或列表顺序自动选边。
    """
    key = "" if order_ref is None else str(order_ref).strip()
    if not key:
        return None, "旧凭证无订单关联（孤儿凭证）"
    if not key.isdigit():
        return None, f"订单号非数字：{order_ref!r}"

    from apps.billing.models import LegacyOrderReservationMap

    foreign_property_mapping = LegacyOrderReservationMap.objects.filter(
        legacy_order_id=int(key), superseded_by__isnull=True
    ).exclude(property_id=batch.scope_property_id)
    if foreign_property_mapping.exists():
        return (
            None,
            f"订单 {key} 当前有效关系存在其他物业映射，物业与批次授权物业不一致",
        )

    raw_relations = (mapping.get("orders") or {}).get(key)
    if isinstance(raw_relations, dict):
        relations = [raw_relations]
    elif isinstance(raw_relations, list):
        relations = raw_relations
    else:
        relations = []
    if not relations:
        return None, f"订单 {key} 未建立订单-预订关系映射（禁止判定 C1）"
    if not all(isinstance(item, dict) for item in relations):
        return None, f"订单 {key} 的关系映射结构非法"

    assignment = (mapping.get("record_reservations") or {}).get(
        f"{source_model}:{source_pk}"
    )
    assignment_data = assignment if isinstance(assignment, dict) else {}
    if assignment is not None and not isinstance(assignment, dict):
        return None, f"{source_model}#{source_pk} 的逐条归属结构非法"
    selected: dict[str, Any]
    requires_record_assignment = len(relations) > 1 or str(
        relations[0].get("relation_type") or ""
    ).upper() in {"SPLIT_SOURCE", "OTHER"}
    if requires_record_assignment:
        if not isinstance(assignment, dict):
            return (
                None,
                f"订单 {key} 属于拆分/多目标关系，{source_model}#{source_pk} "
                "必须逐条指定 relation_id/reservation",
            )
        if not str(assignment.get("basis") or "").strip() or not str(
            assignment.get("confirmed_by") or ""
        ).strip():
            return None, f"{source_model}#{source_pk} 的逐条归属缺少人工确认依据/确认人"
        relation_id = assignment.get("relation_id")
        reservation_id = assignment.get("reservation")
        if reservation_id in (None, ""):
            return None, f"{source_model}#{source_pk} 的逐条归属缺少 reservation"
        matches = [
            item
            for item in relations
            if str(item.get("reservation")) == str(reservation_id)
            and (
                relation_id in (None, "")
                or str(item.get("relation_id")) == str(relation_id)
            )
        ]
        if len(matches) != 1:
            return None, f"{source_model}#{source_pk} 的逐条归属未指向唯一当前关系行"
        selected = matches[0]
        if (
            selected.get("source") == "legacy_order_reservation_map"
            and relation_id in (None, "")
        ):
            return None, f"{source_model}#{source_pk} 必须指定关系表 relation_id"
    else:
        selected = relations[0]
        if isinstance(assignment, dict):
            if not str(assignment.get("basis") or "").strip() or not str(
                assignment.get("confirmed_by") or ""
            ).strip():
                return None, f"{source_model}#{source_pk} 的逐条归属缺少人工确认依据/确认人"
            if (
                assignment.get("reservation") not in (None, "")
                and str(assignment.get("reservation")) != str(selected.get("reservation"))
            ):
                return None, f"{source_model}#{source_pk} 的逐条归属与当前关系冲突"
            if (
                assignment.get("relation_id") not in (None, "")
                and str(assignment.get("relation_id")) != str(selected.get("relation_id"))
            ):
                return None, f"{source_model}#{source_pk} 指向的关系版本已失效或不匹配"

    try:
        reservation_id = int(selected["reservation"])
    except (KeyError, TypeError, ValueError):
        return None, f"订单 {key} 映射的 reservation 非法"
    relation_property_id = selected.get("property_id")
    if relation_property_id not in (None, ""):
        try:
            relation_property_id = int(relation_property_id)
        except (TypeError, ValueError):
            return None, f"订单 {key} 关系物业标识非法"
        if relation_property_id != batch.scope_property_id:
            return None, f"订单 {key} 关系物业与批次授权物业不一致"
    reservation_property_id = Reservation.objects.filter(pk=reservation_id).values_list(
        "property_id", flat=True
    ).first()
    if reservation_property_id is None:
        return None, f"订单 {key} 映射的预订 {reservation_id} 不存在"
    if reservation_property_id != batch.scope_property_id:
        return None, f"预订 {reservation_id} 物业与批次授权物业不一致"
    folio = Folio.objects.filter(
        reservation_id=reservation_id, folio_type=Folio.FolioType.MASTER
    ).first()
    if folio is None:
        return None, f"预订 {reservation_id} 无 MASTER 账单"
    if folio.property_id != batch.scope_property_id:
        return None, f"预订 {reservation_id} 账单物业与批次授权物业不一致"

    relation_id = selected.get("relation_id")
    if relation_id not in (None, "") and selected.get("source") != "legacy_order_reservation_map":
        relation_row = LegacyOrderReservationMap.objects.filter(
            pk=relation_id,
            legacy_order_id=int(key),
            property_id=batch.scope_property_id,
            superseded_by__isnull=True,
        ).first()
        if relation_row is None:
            return None, f"订单 {key} 指定的关系版本不存在、已失效或不属于本物业"
    basis = str(assignment_data.get("basis") or selected.get("basis") or "").strip()
    confirmer = str(
        assignment_data.get("confirmed_by") or selected.get("confirmed_by") or ""
    ).strip()
    if selected.get("source") != "legacy_order_reservation_map" and (
        not basis or not confirmer
    ):
        return None, f"订单 {key} 的文件回退映射缺少人工确认依据/确认人"
    reason = (
        f"rel#{relation_id or 'FILE'}->res#{reservation_id}; "
        f"by={confirmer[:20]}; basis={basis[:50]}"
    )
    return folio, reason


def _candidate_decision(
    mapping: Mapping[str, Any],
    *,
    source_model: str,
    source_pk: str,
    batch: LegacyMigrationBatch,
    expected_folio_id: int,
    relation_reason: str,
) -> Decision | None:
    """人工显式声明的 C2/C3 候选，且必须落在逐条关系选定的账单上。"""
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
    candidate = (
        LedgerEntry.objects.filter(pk=candidate_entry_id).select_related("folio").first()
    )
    if candidate is None:
        raise LegacyMigrationError(
            f"候选声明 {source_model}:{source_pk} 指向的凭证 {candidate_entry_id} 不存在"
        )
    if (
        candidate.property_id != batch.scope_property_id
        or candidate.folio.property_id != batch.scope_property_id
        or candidate.folio_id != expected_folio_id
    ):
        raise LegacyMigrationError(
            f"候选声明 {source_model}:{source_pk} 与逐条关系选定的账单不一致，拒绝映射"
        )
    note = str(entry.get("note") or "").strip()
    confirmer = str(entry.get("confirmed_by") or "").strip()
    if not note or not confirmer:
        raise LegacyMigrationError(
            f"候选声明 {source_model}:{source_pk} 必须包含人工确认人和匹配证据"
        )
    return Decision(
        source_model=source_model,
        source_pk=str(source_pk),
        category=category,
        reason=f"{category} by={confirmer[:20]}; {note[:50]}；{relation_reason}",
        candidate_entry_id=candidate_entry_id,
    )


def classify_folio_row(
    row: dict[str, Any], mapping: Mapping[str, Any], *, batch: LegacyMigrationBatch
) -> Decision:
    source_pk = str(row["id"])
    legacy_type = str(row.get("类型") or "").strip()
    mapped = FOLIO_TYPE_MAP.get(legacy_type)
    if mapped is None:
        return Decision(
            source_model="Folio",
            source_pk=source_pk,
            category="C4",
            reason=f"未知 Folio 类型：{legacy_type or '空'}",
        )
    entry_type, sign = mapped
    if entry_type is None:
        return Decision(
            source_model="Folio",
            source_pk=source_pk,
            category="C5",
            reason=f"旧类型 {legacy_type} 按裁定仅归档，不迁入新账本",
        )
    try:
        amount = parse_amount(row.get("金额"))
        business_date = parse_business_date(row.get("日期"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        return Decision(source_model="Folio", source_pk=source_pk, category="C4", reason=str(exc))

    folio, relation_reason = _resolve_target_folio(
        mapping,
        row.get("order_id"),
        batch=batch,
        source_model="Folio",
        source_pk=source_pk,
    )
    if folio is None:
        return Decision(
            source_model="Folio",
            source_pk=source_pk,
            category="C4",
            reason=relation_reason,
            blocks_batch="物业与批次授权物业不一致" in relation_reason,
        )

    if entry_type == LedgerEntry.EntryType.REVERSAL:
        pair = (mapping.get("reversal_pairs") or {}).get(source_pk)
        if not isinstance(pair, dict):
            return Decision(
                source_model="Folio",
                source_pk=source_pk,
                category="C4",
                reason=f"冲账 {source_pk} 未提供原凭证配对及人工确认，禁止自动迁移；{relation_reason}",
            )
        void_reference = str(pair.get("void_reference") or "")
        void_of = LedgerEntry.objects.filter(reference=void_reference).first()
        if not void_reference or void_of is None:
            return Decision(
                source_model="Folio",
                source_pk=source_pk,
                category="C4",
                reason=f"冲账 {source_pk} 指向的原凭证不存在；{relation_reason}",
            )
        if void_of.folio_id != folio.pk:
            return Decision(
                source_model="Folio",
                source_pk=source_pk,
                category="C4",
                reason=f"冲账原凭证账单与逐条关系选定账单不一致；{relation_reason}",
            )
        confirmation = {
            "confirmed_by": str(pair.get("confirmed_by") or ""),
            "basis": str(pair.get("basis") or ""),
        }
        if not confirmation["confirmed_by"] or not confirmation["basis"]:
            return Decision(
                source_model="Folio",
                source_pk=source_pk,
                category="C4",
                reason=f"冲账 {source_pk} 缺少人工确认人或证据依据；{relation_reason}",
            )
        return Decision(
            source_model="Folio",
            source_pk=source_pk,
            category="C1",
            entry_type=entry_type,
            amount=amount * sign,
            business_date=business_date,
            target_folio_id=folio.pk,
            void_of_pk=void_of.pk,
            reversal_confirmation=confirmation,
            reason=(
                f"冲销 {void_reference[:24]}; by={confirmation['confirmed_by'][:20]}; "
                f"basis={confirmation['basis'][:50]}；{relation_reason}"
            ),
        )

    candidate = _candidate_decision(
        mapping,
        source_model="Folio",
        source_pk=source_pk,
        batch=batch,
        expected_folio_id=folio.pk,
        relation_reason=relation_reason,
    )
    if candidate:
        return candidate
    return Decision(
        source_model="Folio",
        source_pk=source_pk,
        category="C1",
        entry_type=entry_type,
        amount=amount * sign,
        business_date=business_date,
        target_folio_id=folio.pk,
        reason=f"订单-预订关系已确认，可迁移；{relation_reason}",
    )


def classify_finance_row(
    row: dict[str, Any], mapping: Mapping[str, Any], *, batch: LegacyMigrationBatch
) -> Decision:
    source_pk = str(row["id"])
    legacy_type = str(row.get("类型") or "").strip()
    mapped = FINANCE_TYPE_MAP.get(legacy_type)
    if mapped is None:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C4",
            reason=f"未知 FinanceRecord 类型：{legacy_type or '空'}",
        )
    entry_type, sign = mapped
    if entry_type is None:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C5",
            reason=f"旧类型 {legacy_type} 按裁定仅归档，不迁入新账本",
        )
    related_order = row.get("关联订单")
    if related_order in (None, ""):
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C5",
            reason="无订单关联的独立收支按裁定仅归档",
        )
    try:
        amount = parse_amount(row.get("金额"))
        business_date = parse_business_date(row.get("日期"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C4",
            reason=str(exc),
        )

    folio, relation_reason = _resolve_target_folio(
        mapping,
        related_order,
        batch=batch,
        source_model="FinanceRecord",
        source_pk=source_pk,
    )
    if folio is None:
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C4",
            reason=relation_reason,
            blocks_batch="物业与批次授权物业不一致" in relation_reason,
        )

    candidate = _candidate_decision(
        mapping,
        source_model="FinanceRecord",
        source_pk=source_pk,
        batch=batch,
        expected_folio_id=folio.pk,
        relation_reason=relation_reason,
    )
    if candidate:
        return candidate

    nature = (mapping.get("finance_natures") or {}).get(f"FinanceRecord:{source_pk}")
    if (
        not isinstance(nature, dict)
        or str(nature.get("entry_type") or "").upper() != "PAYMENT"
        or not str(nature.get("basis") or "").strip()
        or not str(nature.get("confirmed_by") or "").strip()
    ):
        return Decision(
            source_model="FinanceRecord",
            source_pk=source_pk,
            category="C4",
            reason=(
                "FinanceRecord 仅有收入标签，缺少逐笔资金性质、人工确认人或证据依据；"
                f"{relation_reason}"
            ),
        )
    nature_reason = (
        f"funds=PAYMENT by={str(nature['confirmed_by'])[:20]}; "
        f"{str(nature['basis'])[:50]}"
    )
    return Decision(
        source_model="FinanceRecord",
        source_pk=source_pk,
        category="C1",
        entry_type=entry_type,
        amount=amount * sign,
        business_date=business_date,
        target_folio_id=folio.pk,
        reason=f"资金性质人工确认，可迁移；{relation_reason}；{nature_reason}",
    )


def build_plan(*, batch: LegacyMigrationBatch, mapping: Mapping[str, Any] | None = None) -> Plan:
    """按批次 scope 读取旧表并逐条分类（只读，不写任何表）。

    订单-预订关系来源以 ``legacy_order_reservation_map`` 的全部当前有效行为权威；
    ``--mapping-file`` 的 ``orders`` 仅作关系表铺满前的兼容回退，双来源必须表达
    同一关系集合。1:N/SPLIT_SOURCE 必须用 ``record_reservations`` 逐凭证选边。
    """
    from hotelapp import models as legacy_models

    from apps.billing.legacy_order_map import (
        LegacyOrderMapError,
        merge_order_mappings,
        orders_mapping,
    )

    effective: dict[str, Any] = dict(mapping or {})
    for section in ("record_reservations", "finance_natures", "candidates", "reversal_pairs"):
        value = effective.get(section)
        if value is None:
            effective[section] = {}
        elif not isinstance(value, dict):
            raise LegacyMigrationError(f"映射文件 {section} 必须是 JSON 对象")
    raw_orders = effective.get("orders") or {}
    if not isinstance(raw_orders, dict):
        raise LegacyMigrationError("映射文件 orders 必须是 JSON 对象")
    try:
        effective["orders"] = merge_order_mappings(
            table_map=orders_mapping(batch.scope_property),
            file_map=raw_orders,
        )
    except LegacyOrderMapError as exc:
        raise LegacyMigrationError(str(exc)) from exc

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
            plan.decisions.append(classify_folio_row(row, effective, batch=batch))
    if "FinanceRecord" in scope_models:
        finance_rows = (
            legacy_models.FinanceRecord.objects.all()
            .values("id", "类型", "金额", "事由", "关联订单", "日期", "经手人")
            .order_by("id")
        )
        for row in finance_rows:
            if not _in_range(int(row["id"])):
                continue
            plan.decisions.append(classify_finance_row(row, effective, batch=batch))
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


def _existing_link_matches_decision(
    *, batch: LegacyMigrationBatch, decision: Decision, link: LegacyRecordLink
) -> bool:
    """同批次同分类的已完成映射视为幂等成功；任何不一致都拒绝覆盖。"""
    method = METHOD_BY_CATEGORY[decision.category]
    if (
        link.property.pk != batch.scope_property_id
        or link.migration_batch != batch.batch_uid
        or link.match_method != method
    ):
        return False

    if decision.category == "C1":
        if (
            link.mapping_status != LegacyRecordLink.MappingStatus.LINKED
            or link.target_model != "LedgerEntry"
            or not link.target_pk
        ):
            return False
        entry = LedgerEntry.objects.filter(pk=link.target_pk).first()
        return bool(
            entry
            and entry.reference == decision.reference
            and entry.property_id == batch.scope_property_id
            and entry.folio_id == decision.target_folio_id
            and entry.currency == entry.folio.currency
            and entry.entry_type == decision.entry_type
            and entry.amount == decision.amount
            and entry.business_date == decision.business_date
        )

    if decision.category in {"C2", "C3"}:
        if (
            link.mapping_status != LegacyRecordLink.MappingStatus.LINKED
            or link.target_model != "LedgerEntry"
            or str(link.target_pk) != str(decision.candidate_entry_id)
        ):
            return False
        candidate = LedgerEntry.objects.filter(pk=link.target_pk).first()
        return bool(candidate and candidate.property_id == batch.scope_property_id)

    expected_status = (
        LegacyRecordLink.MappingStatus.CONFLICT
        if decision.category == "C4"
        else LegacyRecordLink.MappingStatus.IGNORED
    )
    return link.mapping_status == expected_status and not link.target_model and not link.target_pk


def _assert_batch_executor(batch: LegacyMigrationBatch, executor: Any) -> None:
    """C2-C5 不经过凭证入口，必须在写映射事务内复核同等批次授权。"""
    from apps.billing.legacy_credentials import LegacyMigrationDenied
    if batch.status != LegacyMigrationBatch.Status.ACTIVE:
        raise LegacyMigrationDenied(
            f"批次状态为 {batch.status}，仅 ACTIVE 批次可执行迁移"
        )
    if batch.approved_by_id is None:
        raise LegacyMigrationDenied("批次缺少正式批准人记录，禁止执行迁移")
    if batch.executed_by_id is None or getattr(executor, "pk", None) != batch.executed_by_id:
        raise LegacyMigrationDenied("当前执行人与批次登记的 executed_by 不一致")
    if batch.approved_by_id == batch.executed_by_id:
        raise LegacyMigrationDenied("批准人与执行人必须分离，禁止同一账号执行迁移")
    if not getattr(executor, "is_active", False) or not getattr(executor, "is_staff", False):
        raise LegacyMigrationDenied("执行账号未启用或不具备平台身份")


def execute_plan(
    *, batch: LegacyMigrationBatch, plan: Plan, credential_factory: Any, executor: Any
) -> dict[str, Any]:
    """按计划落库；同批次已完成的逐条结果可安全跳过，支持失败后断点重执行。

    C1 的凭证、映射和审计由受控入口在自身事务内完成；不能再套一层事务，
    否则受控入口的独立拒绝审计会被外层回滚。C2-C5 在写映射事务内锁定并
    复核 ACTIVE 批次、批准人/执行人分离与执行账号，防止绕过受控入口。
    """
    from apps.billing.legacy_credentials import LegacyMigrationDenied, record_denial
    from apps.billing.services_v2 import BillingService

    batch.refresh_from_db()
    try:
        _assert_batch_executor(batch, executor)
    except LegacyMigrationDenied as exc:
        record_denial(batch=batch, reason=str(exc), actor_id=getattr(executor, "pk", None))
        raise

    counts = {"C1": 0, "C2": 0, "C3": 0, "C4": 0, "C5": 0, "failed": 0}
    failures: list[str] = []
    blocking_decisions = [decision for decision in plan.decisions if decision.blocks_batch]
    if blocking_decisions:
        try:
            with transaction.atomic():
                locked_batch = LegacyMigrationBatch.objects.select_for_update().get(pk=batch.pk)
                _assert_batch_executor(locked_batch, executor)
                for decision in blocking_decisions:
                    existing = list(
                        LegacyRecordLink.objects.select_for_update().filter(
                            source_system="hotelapp",
                            source_model=decision.source_model,
                            source_pk=decision.source_pk,
                        )[:2]
                    )
                    if len(existing) > 1:
                        raise LegacyMigrationDenied(
                            f"来源记录 {decision.source_model}#{decision.source_pk} 存在多条映射"
                        )
                    if existing:
                        if not _existing_link_matches_decision(
                            batch=locked_batch, decision=decision, link=existing[0]
                        ):
                            raise LegacyMigrationDenied(
                                f"来源记录 {decision.source_model}#{decision.source_pk} 已有其他映射，禁止覆盖"
                            )
                        continue
                    _link(
                        batch=locked_batch,
                        decision=decision,
                        executor=executor,
                        target_pk="",
                        method=METHOD_C4,
                        note=f"整批阻断：{decision.reason}",
                    )
            counts["C4"] = len(blocking_decisions)
            counts["failed"] = 1
            failures.append(
                "检测到跨物业订单-预订映射，整批迁移已阻断；冲突记录已标记 C4/CONFLICT"
            )
        except LegacyMigrationDenied as exc:
            record_denial(batch=batch, reason=str(exc), actor_id=getattr(executor, "pk", None))
            counts["failed"] = 1
            failures.append(str(exc))
        except IntegrityError as exc:
            record_denial(batch=batch, reason=str(exc), actor_id=getattr(executor, "pk", None))
            counts["failed"] = 1
            failures.append(f"跨物业冲突映射写入失败：{exc}")
        return {"counts": counts, "failures": failures}
    for decision in plan.decisions:
        method = METHOD_BY_CATEGORY[decision.category]
        try:
            existing_links = list(
                LegacyRecordLink.objects.filter(
                    source_system="hotelapp",
                    source_model=decision.source_model,
                    source_pk=decision.source_pk,
                )[:2]
            )
            if len(existing_links) > 1:
                raise LegacyMigrationDenied(
                    f"来源记录 {decision.source_model}#{decision.source_pk} 存在多条映射，需人工处置"
                )
            if existing_links:
                if _existing_link_matches_decision(
                    batch=batch, decision=decision, link=existing_links[0]
                ):
                    counts[decision.category] += 1
                    continue
                raise LegacyMigrationDenied(
                    f"来源记录 {decision.source_model}#{decision.source_pk} 已有不一致或其他批次映射，禁止覆盖"
                )

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
            else:
                with transaction.atomic():
                    locked_batch = LegacyMigrationBatch.objects.select_for_update().get(
                        pk=batch.pk
                    )
                    _assert_batch_executor(locked_batch, executor)
                    # C2-C5 没有凭证入口；事务内 current read 复查并发写入。
                    raced_links = list(
                        LegacyRecordLink.objects.select_for_update().filter(
                            source_system="hotelapp",
                            source_model=decision.source_model,
                            source_pk=decision.source_pk,
                        )[:2]
                    )
                    if raced_links:
                        if len(raced_links) == 1 and _existing_link_matches_decision(
                            batch=locked_batch, decision=decision, link=raced_links[0]
                        ):
                            counts[decision.category] += 1
                            continue
                        raise LegacyMigrationDenied(
                            f"来源记录 {decision.source_model}#{decision.source_pk} 并发产生冲突映射"
                        )
                    _link(
                        batch=locked_batch,
                        decision=decision,
                        executor=executor,
                        target_pk=(
                            str(decision.candidate_entry_id)
                            if decision.category in {"C2", "C3"}
                            else ""
                        ),
                        method=method,
                        note=f"batch={batch.batch_uid} {decision.reason}",
                    )
            counts[decision.category] += 1
        except LegacyMigrationDenied as exc:
            if decision.category != "C1":
                record_denial(
                    batch=batch, reason=str(exc), actor_id=getattr(executor, "pk", None)
                )
            counts["failed"] += 1
            failures.append(f"{decision.source_model}#{decision.source_pk}: {exc}")
        except (ValueError, LegacyMigrationError, IntegrityError) as exc:
            counts["failed"] += 1
            failures.append(f"{decision.source_model}#{decision.source_pk}: {exc}")
    return {"counts": counts, "failures": failures}


# --------------------------------------------------------------------------- #
# 对账（§1 分类守恒 + §1.2 金额守恒，按 来源模型 × 分类 × 批次 分组）
# --------------------------------------------------------------------------- #
def reconcile_batch(batch: LegacyMigrationBatch) -> dict[str, Any]:
    """只读核验分类守恒、凭证关联、金额分组守恒和归档元数据，不修复任何数据。"""
    from hotelapp import models as legacy_models

    report: dict[str, Any] = {
        "batch_uid": batch.batch_uid,
        "categories": {},
        "amounts": [],
        "amount_groups": [],
        "diffs": [],
        "archive": [],
    }
    links = LegacyRecordLink.objects.filter(
        source_system="hotelapp", migration_batch=batch.batch_uid
    )
    source_model_classes = {
        "Folio": legacy_models.Folio,
        "FinanceRecord": legacy_models.FinanceRecord,
    }
    expected_status = {
        "C1": LegacyRecordLink.MappingStatus.LINKED,
        "C2": LegacyRecordLink.MappingStatus.LINKED,
        "C3": LegacyRecordLink.MappingStatus.LINKED,
        "C4": LegacyRecordLink.MappingStatus.CONFLICT,
        "C5": LegacyRecordLink.MappingStatus.IGNORED,
    }
    expected_groups: dict[tuple[str, int, str, str], Decimal] = defaultdict(lambda: Decimal("0.00"))
    actual_groups: dict[tuple[str, int, str, str], Decimal] = defaultdict(lambda: Decimal("0.00"))
    scope_models = list(batch.scope_source_models or [])
    unscoped_links = links.exclude(source_model__in=scope_models)
    if unscoped_links.exists():
        report["diffs"].append("批次映射包含未授权来源模型，必须人工核查")
    all_expected_references: set[str] = set()
    c1_target_ids: list[str] = []

    for source_model in scope_models:
        model = source_model_classes.get(source_model)
        if model is None:
            report["diffs"].append(f"批次授权了不支持的财务来源模型：{source_model}")
            continue

        record_range = batch.scope_record_range or {}
        source_qs = model.objects.all()
        allowed_pks = record_range.get("pks")
        if isinstance(allowed_pks, list):
            source_qs = source_qs.filter(pk__in=allowed_pks)
        else:
            pk_min = record_range.get("pk_min")
            pk_max = record_range.get("pk_max")
            if pk_min is None or pk_max is None:
                report["diffs"].append(
                    f"{source_model}: 批次授权范围缺少 pk_min/pk_max 或 pks 清单"
                )
                continue
            source_qs = source_qs.filter(pk__gte=pk_min, pk__lte=pk_max)
        source_pks = {str(pk) for pk in source_qs.values_list("pk", flat=True)}
        all_expected_references.update(
            legacy_reference(source_model, source_pk) for source_pk in source_pks
        )

        model_links = list(
            links.filter(source_model=source_model).select_related("property").order_by("source_pk")
        )
        link_pks = [str(link.source_pk) for link in model_links]
        link_pk_set = set(link_pks)
        missing_links = sorted(source_pks - link_pk_set)
        out_of_scope_links = sorted(link_pk_set - source_pks)
        duplicate_source_links = list(
            LegacyRecordLink.objects.filter(
                source_system="hotelapp",
                source_model=source_model,
                source_pk__in=source_pks,
            )
            .values("source_pk")
            .annotate(total=Count("id"))
            .filter(total__gt=1)
            .values_list("source_pk", flat=True)
        )

        rows: dict[str, Any] = {
            "source_count": len(source_pks),
            "link_count": len(model_links),
            "missing_source_links": missing_links,
            "out_of_scope_links": out_of_scope_links,
            "duplicate_source_links": sorted(str(pk) for pk in duplicate_source_links),
        }
        for category, method in METHOD_BY_CATEGORY.items():
            rows[category] = sum(1 for link in model_links if link.match_method == method)

        if len(source_pks) != len(model_links) or missing_links or out_of_scope_links:
            report["diffs"].append(
                f"{source_model}: 来源记录 {len(source_pks)} 条、映射 {len(model_links)} 条，分类守恒不成立"
            )
        if duplicate_source_links:
            report["diffs"].append(
                f"{source_model}: {len(duplicate_source_links)} 条来源记录存在跨物业重复映射（阻断级）"
            )

        unknown_methods: list[str] = []
        status_mismatches = []
        missing_entries = []
        invalid_targets = []
        leaked_new_entries = []
        for link in model_links:
            if link.property.pk != batch.scope_property_id:
                invalid_targets.append(link.source_pk)
            matched_category = {
                method: name for name, method in METHOD_BY_CATEGORY.items()
            }.get(link.match_method)
            if matched_category is None:
                unknown_methods.append(link.source_pk)
                continue
            if link.mapping_status != expected_status[matched_category]:
                status_mismatches.append(link.source_pk)

            reference = legacy_reference(source_model, link.source_pk)
            reference_entry = LedgerEntry.objects.filter(reference=reference).first()
            if matched_category == "C1":
                c1_target_ids.append(str(link.target_pk))
                entry = (
                    LedgerEntry.objects.filter(pk=link.target_pk)
                    .select_related("folio", "property")
                    .first()
                )
                if (
                    entry is None
                    or link.target_model != "LedgerEntry"
                    or not link.target_pk
                    or entry.reference != reference
                ):
                    missing_entries.append(link.source_pk)
                    continue
                if (
                    link.property.pk != batch.scope_property_id
                    or entry.property_id != link.property.pk
                    or entry.folio.property_id != link.property.pk
                ):
                    invalid_targets.append(link.source_pk)
                expected_amount = _expected_signed_amount(source_model, link.source_pk)
                expected_type = _expected_entry_type(source_model, link.source_pk)
                if expected_amount is None or expected_type is None:
                    report["diffs"].append(
                        f"{source_model}#{link.source_pk}: C1 来源金额或凭证类型无法核验"
                    )
                    continue
                expected_currency = entry.folio.currency
                expected_key = ("C1", link.property.pk, expected_currency, expected_type)
                actual_key = ("C1", entry.property_id, entry.currency, entry.entry_type)
                expected_groups[expected_key] += expected_amount
                actual_groups[actual_key] += entry.amount
                if entry.currency != expected_currency:
                    invalid_targets.append(link.source_pk)
                if entry.amount != expected_amount:
                    report["diffs"].append(
                        f"{source_model}#{link.source_pk}: 新凭证 {entry.amount} ≠ 旧记录 {expected_amount}"
                    )
                if entry.entry_type != expected_type:
                    invalid_targets.append(link.source_pk)
                report["amounts"].append(
                    {
                        "category": "C1",
                        "source_model": source_model,
                        "source_pk": link.source_pk,
                        "entry_type": entry.entry_type,
                        "currency": entry.currency,
                        "expected": str(expected_amount),
                        "actual": str(entry.amount),
                    }
                )
            elif matched_category in {"C2", "C3"}:
                candidate = (
                    LedgerEntry.objects.filter(pk=link.target_pk)
                    .select_related("folio", "property")
                    .first()
                )
                if (
                    link.target_model != "LedgerEntry"
                    or not link.target_pk
                    or candidate is None
                    or candidate.property_id != link.property.pk
                    or candidate.folio.property_id != link.property.pk
                ):
                    invalid_targets.append(link.source_pk)
                else:
                    expected_amount = _expected_signed_amount(source_model, link.source_pk)
                    expected_type = _expected_entry_type(source_model, link.source_pk)
                    if expected_amount is None or expected_type is None:
                        report["diffs"].append(
                            f"{source_model}#{link.source_pk}: {matched_category} 来源金额或凭证类型无法核验"
                        )
                    else:
                        expected_currency = candidate.folio.currency
                        expected_key = (
                            matched_category,
                            link.property.pk,
                            expected_currency,
                            expected_type,
                        )
                        actual_key = (
                            matched_category,
                            candidate.property_id,
                            candidate.currency,
                            candidate.entry_type,
                        )
                        expected_groups[expected_key] += expected_amount
                        actual_groups[actual_key] += candidate.amount
                        if candidate.amount != expected_amount:
                            report["diffs"].append(
                                f"{source_model}#{link.source_pk}: {matched_category} 候选凭证 "
                                f"{candidate.amount} ≠ 旧记录 {expected_amount}"
                            )
                        if (
                            candidate.entry_type != expected_type
                            or candidate.currency != expected_currency
                        ):
                            invalid_targets.append(link.source_pk)
                        report["amounts"].append(
                            {
                                "category": matched_category,
                                "source_model": source_model,
                                "source_pk": link.source_pk,
                                "entry_type": candidate.entry_type,
                                "currency": candidate.currency,
                                "expected": str(expected_amount),
                                "actual": str(candidate.amount),
                            }
                        )
                if reference_entry is not None:
                    leaked_new_entries.append(link.source_pk)
            else:
                if link.target_model or link.target_pk:
                    invalid_targets.append(link.source_pk)
                if reference_entry is not None:
                    leaked_new_entries.append(link.source_pk)

        rows["unknown_methods"] = unknown_methods
        rows["status_mismatches"] = status_mismatches
        rows["C1_missing_entries"] = missing_entries
        rows["invalid_targets"] = invalid_targets
        rows["leaked_new_entries"] = leaked_new_entries
        report["categories"][source_model] = rows

        if unknown_methods:
            report["diffs"].append(f"{source_model}: {len(unknown_methods)} 条映射使用未知分类")
        if status_mismatches:
            report["diffs"].append(
                f"{source_model}: {len(status_mismatches)} 条映射状态与分类不一致"
            )
        if missing_entries:
            report["diffs"].append(
                f"{source_model}: {len(missing_entries)} 条 C1 映射指向的凭证不存在或 reference 不一致"
            )
        if invalid_targets:
            report["diffs"].append(
                f"{source_model}: {len(invalid_targets)} 条映射目标无效或物业不一致"
            )
        if leaked_new_entries:
            report["diffs"].append(
                f"{source_model}: {len(leaked_new_entries)} 条 C2/C3/C4/C5 记录泄漏出新建凭证（阻断级）"
            )

    duplicate_targets = [
        target_pk for target_pk, count in Counter(c1_target_ids).items() if count > 1
    ]
    if duplicate_targets:
        report["diffs"].append(
            f"C1 多条旧记录指向同一新凭证（阻断级）：{', '.join(sorted(duplicate_targets))}"
        )

    # 兜底检查：范围内存在 legacy reference，但没有对应 C1 链接，属于孤儿新凭证。
    linked_c1_refs = {
        (LEGACY_REFERENCE_NAMES.get(source_model, source_model), str(source_pk))
        for source_model, source_pk in links.filter(match_method=METHOD_C1).values_list(
            "source_model", "source_pk"
        )
    }
    source_ref_entries = LedgerEntry.objects.filter(reference__in=all_expected_references)
    orphan_references = [
        entry.reference
        for entry in source_ref_entries
        if tuple(entry.reference.split(":", 2)[1:]) not in linked_c1_refs
    ]
    if orphan_references:
        report["diffs"].append(
            f"存在未被 C1 映射认领的历史凭证 reference（阻断级）：{', '.join(sorted(orphan_references))}"
        )

    for key in sorted(set(expected_groups) | set(actual_groups)):
        expected_total = expected_groups.get(key, Decimal("0.00"))
        actual_total = actual_groups.get(key, Decimal("0.00"))
        category, property_id, currency, entry_type = key
        property_code = (
            Property.objects.filter(pk=property_id).values_list("code", flat=True).first()
        )
        report["amount_groups"].append(
            {
                "category": category,
                "property_id": property_id,
                "property_code": property_code,
                "currency": currency,
                "entry_type": entry_type,
                "expected": str(expected_total),
                "actual": str(actual_total),
            }
        )
        if expected_total != actual_total:
            report["diffs"].append(
                f"金额分组不守恒：category={category} property={property_code or property_id} "
                f"currency={currency} entry_type={entry_type} "
                f"期望 {expected_total}，实测 {actual_total}"
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


def _expected_entry_type(source_model: str, source_pk: str) -> str | None:
    """旧记录映射的期望凭证类型；未知类型不猜测。"""
    from hotelapp import models as legacy_models

    if source_model == "Folio":
        row = legacy_models.Folio.objects.filter(pk=source_pk).values("类型").first()
        mapping = FOLIO_TYPE_MAP
    elif source_model == "FinanceRecord":
        row = legacy_models.FinanceRecord.objects.filter(pk=source_pk).values("类型").first()
        mapping = FINANCE_TYPE_MAP
    else:
        return None
    if row is None:
        return None
    mapped = mapping.get(str(row["类型"] or "").strip())
    return mapped[0] if mapped else None


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
    counts["blocking"] = sum(1 for decision in plan.decisions if decision.blocks_batch)
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
            "blocks_batch": d.blocks_batch,
        }
        for d in plan.decisions
    ]
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
