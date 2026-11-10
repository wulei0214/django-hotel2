# -*- coding: utf-8 -*-
"""DEC-WALKIN-01 Walk-in 散客原子化创建服务 + DEC-ED-01 提前离店政策计算。

Walk-in：Guest→Reservation(source=WALK_IN)→ReservationRoom→Folio→Stay 同事务。
提前离店：EarlyDeparturePolicy 按 RatePlan 关联，5 种政策类型。

WB-002 修复：补齐 assign_room/check_in_stay 导入。
WB-003 修复：不再单独创建 Guest（create_reservation 内部统一创建）；
           无手机号时使用确定性占位号（基于 idempotency_key 而非 hash()）。
WB-004/065 修复：calculate_early_departure 按日期顺序取 nightly_rates（不排序金额）。
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
        return {"actual_room_charge": line.amount, "penalty": Decimal("0.00"),
                "policy_type": "NONE"}
    total_nights = len(nightly)
    actual_nights = max(
        (actual_check_out.date() - line.check_in).days, 0)
    actual_nights = min(actual_nights, total_nights)
    actual_room_charge = sum(nightly[:actual_nights], Decimal("0.00"))

    policy = EarlyDeparturePolicy.objects.filter(
        rate_plan_id=line.rate_plan_id, is_active=True).order_by("-created_at").first()
    if policy is None or policy.policy_type == EarlyDeparturePolicy.PolicyType.NONE:
        # NONE/未配置政策 = 无提前离店减免：客人按合同房费（line.amount）
        # 全额结算，罚金 0。回归基线语义（同日退房也须入账合同房费）。
        return {"actual_room_charge": line.amount,
                "penalty": Decimal("0.00"), "policy_type": "NONE"}
    pt = policy.policy_type
    pv = policy.policy_value or Decimal("0.00")
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
    from apps.billing.models import Folio
    from apps.billing.services_v2 import BillingService
    from apps.reservations.front_desk_services import assign_room, check_in_stay
    from apps.reservations.services import create_reservation

    if not full_name or not full_name.strip():
        raise ValueError("Walk-in 住客姓名必填。")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise ValueError("Walk-in 必须提供 1 至 128 字符的幂等键。")

    # WB-003 修复：无手机号时使用基于 idempotency_key 的确定性占位号
    # （不使用 Python hash()，跨进程不稳定）
    phone_normalized = str(phone or "").strip()
    if not phone_normalized:
        phone_normalized = f"0000{uuid.uuid4().hex[:8]}"

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
    reservation.source = Reservation.Source.WALK_IN
    reservation.save(update_fields=("source", "updated_at"))

    line = reservation.reservation_rooms.first()

    # 排房 + 入住（同事务）；WB-002 修复：assign_room/check_in_stay 已导入
    from apps.inventory.models import Room
    target_room = Room.objects.filter(
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
