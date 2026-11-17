"""旧订单 ↔ 新预订关系映射的确认与版本演进（迁移方案 §1.5.2）。

设计口径（不得自行放宽）：
1. **确认事实不可变**：关系行创建即 CONFIRMED，没有状态字段、没有状态流转；
   改映射只能追加新行并以 ``supersedes`` 指向被取代的旧行，旧行原样保留。
2. **当前有效关系是派生口径**：未被任何行 ``supersedes`` 引用
   （``superseded_by IS NULL``），不做存储态翻转。
3. **物业一致性**：关系行 property 必须等于新 Reservation.property；
   确认事务内 ``select_for_update`` 锁预订行后复核，跨物业关系一律拒绝。
4. **不猜测**：候选驳回不成为关系行；映射表只收录人工确认过的关系。

兼容层：D3 之前``--mapping-file``仍可用（阶段 D 关系表尚未铺满时的人工确认
输入）。表与文件同时给出且指向不同预订 → **拒绝**，不静默选一边。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import LegacyOrderReservationMap
from apps.core.models import AuditEvent


class LegacyOrderMapError(Exception):
    """关系映射确认被拒绝（配对非法、跨物业、版本链冲突、已存在）。"""


def _normalize_order_id(value: Any) -> int:
    text = "" if value is None else str(value).strip()
    if not text:
        raise LegacyOrderMapError("旧订单 id 不能为空")
    try:
        return int(text)
    except (TypeError, ValueError) as exc:
        raise LegacyOrderMapError(f"旧订单 id 必须是整数：{value!r}") from exc


def _require_reservation(reservation_id: Any) -> Any:
    from apps.reservations.models import Reservation

    try:
        return Reservation.objects.select_for_update().get(pk=int(reservation_id))
    except (TypeError, ValueError) as exc:
        raise LegacyOrderMapError(f"新预订 id 必须是整数：{reservation_id!r}") from exc
    except Reservation.DoesNotExist as exc:
        raise LegacyOrderMapError(f"新预订 #{reservation_id} 不存在") from exc


def _require_legacy_order(legacy_order_id: int) -> None:
    from hotelapp import models as legacy_models

    if not legacy_models.Accommodation.objects.filter(pk=legacy_order_id).exists():
        raise LegacyOrderMapError(f"旧订单 #{legacy_order_id} 不存在")


def pair_position_key(legacy_order_id: int, reservation_id: int, supersedes_id: Any) -> str:
    """配对版本位置键（FIN-01）：'order:reservation:root' 或 'order:reservation:sup:<id>'。

    唯一约束 uq_legacy_map_position_key 的防重载体；同键 = 同配对同版本位置，
    并发第二行被拒；回退（经 supersedes 链回指）产生新键，自然放行。"""
    tail = "root" if supersedes_id is None else f"sup:{int(supersedes_id)}"
    return f"{int(legacy_order_id)}:{int(reservation_id)}:{tail}"


def _validate_basis(match_basis: str) -> str:
    text = str(match_basis or "").strip()
    if not text:
        raise LegacyOrderMapError("匹配依据必填：须写明人工核对依据（客人/日期/房型等）")
    return text[:240]


def confirm_pair(
    *,
    legacy_order_id: Any,
    reservation_id: Any,
    property_obj: Any,
    confirmed_by: Any,
    match_basis: str,
    relation_type: str = LegacyOrderReservationMap.RelationType.ONE_TO_ONE,
    supersedes: Any = None,
) -> LegacyOrderReservationMap:
    """单事务确认一对（旧订单, 新预订）关系；``supersedes`` 非空即版本演进。

    事务内顺序（§1.5.2 确认事务）：
    锁定新预订行 → 复核物业一致性 → 校验被取代行（同旧订单、同物业、当前有效）
    → 创建关系行。并发确认同一（配对, 版本位置）由唯一约束拒绝第二行；同配对经
    版本链回指（FIN-01，2026-11-16 裁定）允许，当前有效关系仍为派生口径。
    """
    order_pk = _normalize_order_id(legacy_order_id)
    if confirmed_by is None:
        raise LegacyOrderMapError("必须记录确认人（人工确认留痕）")
    if property_obj is None:
        raise LegacyOrderMapError("必须提供物业（关系记录显式登记物业）")
    if relation_type not in LegacyOrderReservationMap.RelationType.values:
        raise LegacyOrderMapError(
            f"未知关系角色 {relation_type!r}；可选："
            + ", ".join(LegacyOrderReservationMap.RelationType.values)
        )
    basis = _validate_basis(match_basis)

    try:
        with transaction.atomic():
            reservation = _require_reservation(reservation_id)
            if reservation.property_id != property_obj.pk:
                raise LegacyOrderMapError(
                    f"跨物业关系禁止：预订 #{reservation.pk} 属于物业 "
                    f"{reservation.property_id}，关系记录登记物业 {property_obj.pk}"
                )
            _require_legacy_order(order_pk)
            if supersedes is not None:
                previous = type(supersedes).objects.select_for_update().get(pk=supersedes.pk)
                if previous.property_id != property_obj.pk:
                    raise LegacyOrderMapError("被取代的关系行不属于当前物业")
                if previous.legacy_order_id != order_pk:
                    raise LegacyOrderMapError("被取代的关系行属于另一旧订单，版本链禁止跨订单")
                if type(previous).objects.filter(supersedes_id=previous.pk).exists():
                    raise LegacyOrderMapError(
                        f"关系行 #{previous.pk} 已被取代，版本链只能延伸不能分叉"
                    )
                supersedes = previous
            row = LegacyOrderReservationMap.objects.create(
                legacy_order_id=order_pk,
                reservation=reservation,
                property=property_obj,
                relation_type=relation_type,
                match_basis=basis,
                confirmed_by=confirmed_by,
                confirmed_at=timezone.now(),
                supersedes=supersedes,
                position_key=pair_position_key(order_pk, reservation.pk, supersedes_id=None if supersedes is None else supersedes.pk),
            )
    except IntegrityError as exc:
        raise LegacyOrderMapError(
            f"旧订单 #{order_pk} 与预订 #{reservation_id} 的关系在同一版本位置已确认"
            "（唯一约束 uq_legacy_order_reservation_pair），禁止重复落库；"
            "改映射/回退请走 supersedes 版本链"
        ) from exc

    AuditEvent.record(
        action="billing.legacy_order_map_confirm",
        entity_type="LegacyOrderReservationMap",
        entity_id=str(row.pk),
        actor=confirmed_by,
        property_obj=property_obj,
        after={
            "legacy_order_id": order_pk,
            "reservation_id": row.reservation_id,
            "relation_type": relation_type,
            "supersedes_id": row.supersedes_id,
            "match_basis": basis,
        },
        reason=basis,
    )
    return row


def current_rows(*, property_obj: Any = None) -> Any:
    """当前有效关系（未被取代）；按物业过滤时传 property_obj。"""
    queryset = LegacyOrderReservationMap.objects.filter(superseded_by__isnull=True)
    if property_obj is not None:
        queryset = queryset.filter(property=property_obj)
    return queryset


def current_map_for_order(legacy_order_id: Any, *, property_obj: Any = None) -> Any:
    """某旧订单的当前有效关系（派生口径，不做存储态翻转）。"""
    return (
        current_rows(property_obj=property_obj)
        .filter(legacy_order_id=_normalize_order_id(legacy_order_id))
        .first()
    )


def orders_mapping(property_obj: Any) -> dict[str, list[dict[str, Any]]]:
    """返回旧订单的全部当前有效关系；不得把 1:N 静默压成最后一条。"""
    result: dict[str, list[dict[str, Any]]] = {}
    rows = current_rows(property_obj=property_obj).select_related("confirmed_by")
    for row in rows.order_by("legacy_order_id", "id"):
        result.setdefault(str(row.legacy_order_id), []).append(
            {
                "reservation": row.reservation_id,
                "relation_id": row.pk,
                "property_id": row.property_id,
                "basis": row.match_basis,
                "confirmed_by": getattr(row.confirmed_by, "username", "")
                or str(row.confirmed_by_id),
                "relation_type": row.relation_type,
                "source": "legacy_order_reservation_map",
            }
        )
    return result


def _normalize_order_entries(order_id: str, value: Any) -> list[dict[str, Any]]:
    """兼容旧单行输入与新版 relations 列表，统一为关系行列表。"""
    if isinstance(value, dict):
        raw_entries = value.get("relations") if "relations" in value else [value]
    elif isinstance(value, list):
        raw_entries = value
    else:
        raise LegacyOrderMapError(f"旧订单 {order_id} 的映射必须是对象或 relations 列表")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise LegacyOrderMapError(f"旧订单 {order_id} 的 relations 必须是非空列表")

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or raw.get("reservation") in (None, ""):
            raise LegacyOrderMapError(f"旧订单 {order_id} 的关系行缺少 reservation")
        try:
            reservation_id = int(raw["reservation"])
            relation_id = (
                int(raw["relation_id"])
                if raw.get("relation_id") not in (None, "")
                else None
            )
        except (TypeError, ValueError) as exc:
            raise LegacyOrderMapError(f"旧订单 {order_id} 的关系主键非法") from exc
        identity = (str(reservation_id), str(relation_id or ""))
        if identity in seen:
            raise LegacyOrderMapError(f"旧订单 {order_id} 存在重复关系行 {identity}")
        seen.add(identity)
        item = dict(raw)
        item["reservation"] = reservation_id
        if relation_id is not None:
            item["relation_id"] = relation_id
        normalized.append(item)
    return sorted(
        normalized,
        key=lambda item: (
            int(item.get("relation_id") or 0),
            int(item["reservation"]),
        ),
    )


def merge_order_mappings(
    *, table_map: Mapping[str, Any], file_map: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """关系表优先；文件仅作兼容回退，双来源必须表达同一组关系。

    单个旧订单可对应多条当前有效关系。若文件只给其中一条而关系表已有多条，
    视为冲突，不静默选择。具体旧凭证归属由 record_reservations 逐条指定。
    """
    merged: dict[str, list[dict[str, Any]]] = {
        str(key): _normalize_order_entries(str(key), value)
        for key, value in (file_map or {}).items()
    }
    conflicts: list[str] = []
    for key, value in (table_map or {}).items():
        table_entries = _normalize_order_entries(str(key), value)
        existing = merged.get(str(key))
        if existing is not None:
            file_has_relation_ids = any(
                entry.get("relation_id") not in (None, "") for entry in existing
            )
            if file_has_relation_ids:
                table_signature = sorted(
                    (str(entry["reservation"]), str(entry.get("relation_id") or ""))
                    for entry in table_entries
                )
                file_signature = sorted(
                    (str(entry["reservation"]), str(entry.get("relation_id") or ""))
                    for entry in existing
                )
            else:
                table_signature = sorted(str(entry["reservation"]) for entry in table_entries)
                file_signature = sorted(str(entry["reservation"]) for entry in existing)
            if table_signature != file_signature:
                conflicts.append(
                    f"旧订单 {key}：关系表关系集合 {table_signature}，"
                    f"映射文件关系集合 {file_signature}"
                )
                continue
        # 关系表记录含 relation_id、property_id 和确认人证据，优先作为权威值。
        merged[str(key)] = table_entries
    if conflicts:
        raise LegacyOrderMapError(
            "关系表与映射文件不一致，禁止迁移（须人工消除冲突后重跑）：" + "；".join(conflicts)
        )
    return merged


def superseded_chain(legacy_order_id: Any, *, property_obj: Any = None) -> list[Any]:
    """某旧订单的全部关系行（含已被取代的历史行），按版本链从旧到新排列。"""
    order_pk = _normalize_order_id(legacy_order_id)
    queryset = LegacyOrderReservationMap.objects.filter(legacy_order_id=order_pk)
    if property_obj is not None:
        queryset = queryset.filter(property=property_obj)
    rows = list(queryset.order_by("id"))
    if not rows:
        return []
    # supersedes 指向"被本行取代的旧行"：链头 = 未取代任何行的行（supersedes 为空），
    # 后继 = 以该行为 supersedes 目标的行。
    successors = {row.supersedes_id: row for row in rows if row.supersedes_id is not None}
    heads = [row for row in rows if row.supersedes_id is None]
    chain: list[Any] = []
    for head in heads:
        cursor = head
        while cursor is not None:
            chain.append(cursor)
            cursor = successors.get(cursor.pk)
    return chain
