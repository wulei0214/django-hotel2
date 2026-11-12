# -*- coding: utf-8 -*-
"""DEC-WALKIN-01 Walk-in 散客原子化创建服务 + DEC-ED-01 提前离店政策计算。

Walk-in：Guest→Reservation(source=WALK_IN)→ReservationRoom→Folio→Stay 同事务。
提前离店：EarlyDeparturePolicy 按 RatePlan 关联，5 种政策类型。

WB-002 修复：补齐 assign_room/check_in_stay 导入。
WB-003 修复：不再单独创建 Guest（create_reservation 内部统一创建）；
           无手机号时使用确定性占位号（基于 idempotency_key 而非 hash()）。
WB-004/065 修复：calculate_early_departure 按日期顺序取 nightly_rates（不排序金额）。
WB-074 修复：占位号升级为 uuid5（幂等键派生，同键重放恒定）；房间查询限定
           当前物业；同幂等键重放不再重复排房/入住（走幂等返回既有 Stay）。
"""
import uuid
from decimal import Decimal

from django.conf import settings
from datetime import timedelta

from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.reservations.models import (
    EarlyDeparturePolicy,
    Reservation,
    ReservationRoom,
    Stay,
)


# ---------------------------------------------------------------------------
# DEC-ED-01 提前离店政策计算
# ---------------------------------------------------------------------------

def calculate_early_departure(*, line, actual_check_out):
    """按提前离店政策计算实际应收房费和违约金（纯函数，可测试）。

    WB-004/065 修复：nightly_rates 按日期顺序取值（不按金额排序），
    确保不同日期不同价格时实际住宿晚数和罚金正确。

    WB-097（DEC-ED-01 §2.1 正式语义，docs/PMS2六项业务规则正式决策
    与实施基线_2026-11-10.md 第 36/38 行）：
      * 实际住宿房费 = 预订时保存的逐晚房价快照 × 实际入住晚数
        （actual_nights = (actual_check_out.date() - line.check_in).days，
        截取到 [0, 总晚数]）；未住晚不作为实际住宿房费入账；
      * NONE/未配置政策 = 未住晚不收违约金、未住晚不计房费——不再回退
        "合同全额 line.amount"（旧实现已按正式语义推翻）；
      * WB-098：优先读取预订确认时保存的 line.early_departure_snapshot
        （政策版本快照，第 38 行口径）；无快照的 legacy 行按 NONE 兜底并
        落一条审计说明回退原因（§2.1/§11.3-A 第 77 行：快照缺失不得
        生成非零罚金），不再无条件实时读表；
      * nightly_rates 缺失时无结算依据：抛 ValueError fail-closed。
        退房链路的 WB-075 前置守卫（FrontDeskOperationError）保持不变。

    返回 {"actual_room_charge": Decimal, "penalty": Decimal, "policy_type": str}。
    """
    nightly = [
        Decimal(str(n["price"]))
        for n in sorted(
            (line.nightly_rates or []),
            key=lambda n: n.get("date", ""),
        )
    ]
    if not nightly:
        # WB-097 正式语义：没有逐晚房价快照就没有结算依据，不得回退
        # 合同全额。退房入口已按 WB-075 先行阻断；此处对直接调用兜底。
        raise ValueError(
            "缺少逐晚房价快照，无法按实际入住晚数结算退房房费（DEC-ED-01）。")
    total_nights = len(nightly)
    # FIN-02 / DEC-ED-01 §2.3：actual_nights 必须按**物业时区**业务日期计算。
    # 此前用 timezon.localdate()（部署 TIME_ZONE）或 aware datetime 的 .date()
    # （UTC 日期）：Asia/Shanghai 凌晨 00:00–07:59 退房时 UTC 是前一天，
    # 少算一晚房费且该晚库存不释放也不计费。与夜审 property_date /
    # _property_today / 提前离店释放起点统一到同一物业时区口径。
    from datetime import date as date_cls, datetime as dt_datetime
    from zoneinfo import ZoneInfo
    try:
        property_tz = ZoneInfo(line.property.timezone or "Asia/Shanghai")
    except Exception:
        property_tz = ZoneInfo("Asia/Shanghai")
    if isinstance(actual_check_out, dt_datetime):
        if timezone.is_aware(actual_check_out):
            actual_check_out_day = timezone.localdate(actual_check_out, property_tz)
        else:
            # naive 视为物业本地时间（测试夹具常用），直接取其日期部分。
            actual_check_out_day = actual_check_out.date()
    elif isinstance(actual_check_out, date_cls):
        actual_check_out_day = actual_check_out
    else:
        raise ValueError("actual_check_out 必须是日期或日期时间。")
    actual_nights = max((actual_check_out_day - line.check_in).days, 0)
    actual_nights = min(actual_nights, total_nights)
    actual_room_charge = sum(nightly[:actual_nights], Decimal("0.00"))

    none_result = {"actual_room_charge": actual_room_charge,
                   "penalty": Decimal("0.00"),
                   "policy_type": EarlyDeparturePolicy.PolicyType.NONE}

    # WB-098：优先使用预订确认时保存的政策版本快照。
    snapshot = line.early_departure_snapshot or None
    if snapshot is not None:
        pt = str(snapshot.get("policy_type") or "").strip() or \
            EarlyDeparturePolicy.PolicyType.NONE
        if pt not in dict(EarlyDeparturePolicy.PolicyType.choices):
            # 快照内容异常（如手工篡改/脏数据）按 NONE 兜底，不得生成罚金。
            return none_result
        pv_raw = snapshot.get("policy_value")
        pv = Decimal(str(pv_raw)) if pv_raw not in (None, "") else Decimal("0.00")
    else:
        # legacy 行无预订时快照：按 NONE 处理（DEC-ED-01 §2.1："没有有效
        # 政策配置或政策快照时……按 NONE 处理"；§11.3-A 第 77 行："快照
        # 缺失时按 NONE，不得生成非零罚金"），并审计一条 history 说明
        # 回退原因。不回退当前活跃政策——确认后新增/修改的政策不得追溯
        # 施加到从未确认过该政策的订单上。
        current_policy = EarlyDeparturePolicy.objects.filter(
            rate_plan_id=line.rate_plan_id, is_active=True).order_by(
            "-created_at", "-pk").first()
        if line.pk and line.property_id:
            AuditEvent.record(
                action="reservations.early_departure_snapshot_missing",
                entity_type="ReservationRoom", entity_id=str(line.pk),
                property_obj=line.property,
                after={"rate_plan_id": line.rate_plan_id,
                       "current_active_policy": (
                           current_policy.policy_type
                           if current_policy else None),
                       "applied_policy_type": "NONE"},
                reason="该明细无预订时提前离店政策快照（legacy 数据），"
                       "按 NONE 处理、不生成罚金（DEC-ED-01 §2.1/§11.3-A）")
        return none_result

    if pt == EarlyDeparturePolicy.PolicyType.NONE:
        return none_result
    if pt == EarlyDeparturePolicy.PolicyType.FIRST_NIGHT:
        penalty = nightly[0]
    elif pt == EarlyDeparturePolicy.PolicyType.FIXED:
        penalty = pv
    elif pt == EarlyDeparturePolicy.PolicyType.PERCENT_UNUSED:
        unused_amount = sum(nightly[actual_nights:], Decimal("0.00"))
        penalty = (unused_amount * pv / Decimal("100")).quantize(Decimal("0.01"))
    elif pt == EarlyDeparturePolicy.PolicyType.REMAINING_NIGHTS:
        penalty = sum(nightly[actual_nights:], Decimal("0.00"))
    else:
        penalty = Decimal("0.00")
    return {"actual_room_charge": actual_room_charge,
            "penalty": penalty.quantize(Decimal("0.01")), "policy_type": pt}


# ---------------------------------------------------------------------------
# DEC-WALKIN-01 Walk-in 原子化创建
# ---------------------------------------------------------------------------
@transaction.atomic
def create_walk_in(*, property_obj, full_name, phone, room_type_id,
                   rate_plan_id, nights=1, adults=1, children=0,
                   deposit_amount=None, created_by=None,
                   idempotency_key=""):
    """Walk-in 散客原子化创建（同一事务内全链路）。

    Guest → Reservation(source=WALK_IN) → ReservationRoom → Folio
    → Hold → 入住确认 → Stay；失败整体回滚不留孤儿记录。

    WB-003 修复：不再单独创建 Guest；create_reservation 内部统一创建，
    避免重复住客档案。无手机号时使用确定性占位号。
    """
    from apps.billing.services_v2 import BillingService
    from apps.inventory.services import (
        InventoryUnavailable,
        confirm_inventory_for_reservation,
    )
    from apps.reservations.front_desk_services import assign_room, check_in_stay
    from apps.reservations.services import create_reservation

    if not full_name or not full_name.strip():
        raise ValueError("Walk-in 住客姓名必填。")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise ValueError("Walk-in 必须提供 1 至 128 字符的幂等键。")

    # WB-074 修复：无手机号时占位号由幂等键确定性派生（uuid5，同一物业同一
    # 幂等键恒得到同一占位号）。占位号进入 create_reservation 的
    # request_fingerprint，确定性是同键重放能命中幂等路径的前提；
    # 不使用 hash()/uuid4 等非确定性来源。
    phone_normalized = str(phone or "").strip()
    if not phone_normalized:
        phone_normalized = f"0000{uuid.uuid5(uuid.NAMESPACE_URL, f'walkin:{property_obj.pk}:{key}').hex[:8]}"

    # create_reservation 统一创建 Guest + GuestPhone + Reservation + Folio + Hold
    payload = {
        "full_name": full_name.strip(),
        "phone": phone_normalized,
        "room_type_id": room_type_id, "rate_plan_id": rate_plan_id,
        "check_in": timezone.localdate(),
        "check_out": timezone.localdate() + timedelta(days=nights),
        "rooms": 1, "adults": adults, "children": children,
    }
    reservation, created, token = create_reservation(
        property_obj=property_obj, payload=payload, idempotency_key=key)
    # Reservation.source 是普通 CharField（无枚举类），WALK_IN 取值见字段 help_text。
    if reservation.source != "WALK_IN":
        reservation.source = "WALK_IN"
        reservation.save(update_fields=("source", "updated_at"))

    if not created:
        # WB-074 修复：同幂等键重放 → create_reservation 幂等返回既有订单，
        # 不重复创建 Guest/Reservation/Stay，也不重复排房/入住/收款；
        # 直接返回首次请求已落库的入住事实（与预订幂等重放同口径，无新增写入）。
        line = reservation.reservation_rooms.first()
        stay = Stay.objects.filter(
            reservation_room=line, property=property_obj).first()
        if stay is None:
            raise ValueError(
                "Walk-in 幂等重放异常：首次请求未完成入住，请联系前台核对。")
        return {"reservation": reservation, "stay": stay, "room": stay.room,
                "booking_no": reservation.booking_no}

    line = reservation.reservation_rooms.first()

    # Walk-in 客人已到店：与前台收款确认同口径——先确认库存（Hold→Sold）
    # 再迁移 PENDING → CONFIRMED，否则排房/入住会被预订状态门禁拒绝。
    try:
        confirm_inventory_for_reservation(reservation)
    except InventoryUnavailable as exc:
        raise ValueError(str(exc)) from exc
    reservation.status = Reservation.Status.CONFIRMED
    reservation.save(update_fields=("status", "updated_at"))
    # WB-098：确认事实落库的同一事务内保存提前离店政策快照
    # （DEC-ED-01：预订确认时保存政策版本快照）。
    from apps.reservations.front_desk_services import (
        snapshot_early_departure_policy,
    )
    snapshot_early_departure_policy(reservation)

    # 排房 + 入住（同事务）；WB-002 修复：assign_room/check_in_stay 已导入。
    # WB-074 修复：房间查询限定当前物业（room_type 已由 create_reservation
    # 校验属于本物业），杜绝多物业同房型时跨物业占用他店客房。
    from apps.inventory.models import Room
    target_room = Room.objects.filter(
        property=property_obj,
        room_type_id=room_type_id,
        occupancy_status=Room.OccupancyStatus.VACANT,
        housekeeping_status=Room.HousekeepingStatus.CLEAN,
        maintenance_status=Room.MaintenanceStatus.NORMAL).first()
    if target_room is None:
        raise ValueError("当前无可入住净房，Walk-in 无法完成。")
    assign_room(property_obj=property_obj, reservation_room_id=line.pk,
                room_id=target_room.pk, assigned_by=created_by)
    stay, _ = check_in_stay(property_obj=property_obj,
                            reservation_room_id=line.pk,
                            checked_in_by=created_by)

    # 押金（DEC-DEP-01 政策服务端计算）
    if deposit_amount:
        BillingService.record_deposit(
            property_obj=property_obj, reservation=reservation,
            amount=deposit_amount, method="CASH",
            idempotency_key=f"{key}-dep", received_by=created_by)

    AuditEvent.record(
        action="reservation.walk_in_created", entity_type="Reservation",
        entity_id=reservation.pk, actor=created_by, property_obj=property_obj,
        after={"booking_no": reservation.booking_no, "stay_id": stay.pk,
               "room": target_room.number, "source": "WALK_IN"},
        reason="Walk-in 散客原子化入住（DEC-WALKIN-01）")
    return {"reservation": reservation, "stay": stay, "room": target_room,
            "booking_no": reservation.booking_no}
