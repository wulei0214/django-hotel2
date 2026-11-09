# -*- coding: utf-8 -*-
"""DEC-WALKIN-01 Walk-in 散客原子化创建服务 + DEC-ED-01 提前离店政策模型。

Walk-in：Guest→Reservation(source=WALK_IN)→ReservationRoom→Folio→Stay 同事务。
提前离店：EarlyDeparturePolicy 按 RatePlan 关联，5 种政策类型。
"""
import uuid
from decimal import Decimal

from django.conf import settings
from datetime import timedelta

from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.inventory.services import InventoryUnavailable, hold_inventory_for_reservation
from apps.reservations.models import (
    EarlyDeparturePolicy,
    Reservation,
    ReservationRoom,
    Stay,
)


# ---------------------------------------------------------------------------
# DEC-ED-01 提前离店政策模型
# ---------------------------------------------------------------------------

def calculate_early_departure(*, line, actual_check_out):
    """按提前离店政策计算实际应收房费和违约金（纯函数，可测试）。

    返回 {"actual_room_charge": Decimal, "penalty": Decimal, "policy_type": str}。
    """
    nightly = sorted(
        (Decimal(str(n["price"])) for n in (line.nightly_rates or [])),
    )
    if not nightly:
        return {"actual_room_charge": line.amount, "penalty": Decimal("0.00"),
                "policy_type": "NONE"}
    total_nights = len(nightly)
    actual_nights = max(
        (actual_check_out.date() - line.check_in).days, 0)
    actual_nights = min(actual_nights, total_nights)
    actual_room_charge = sum(nightly[:actual_nights], Decimal("0.00"))
    unused_nights = total_nights - actual_nights

    policy = EarlyDeparturePolicy.objects.filter(
        rate_plan_id=line.rate_plan_id, is_active=True).order_by("-created_at").first()
    if policy is None or policy.policy_type == EarlyDeparturePolicy.PolicyType.NONE:
        return {"actual_room_charge": actual_room_charge,
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
    """DEC-WALKIN-01：Walk-in 散客原子化创建（同一事务内全链路）。

    Guest → Reservation(source=WALK_IN) → ReservationRoom → Folio
    → Hold → 入住确认 → Stay；失败整体回滚不留孤儿记录。
    """
    from apps.guests.models import Guest, GuestPhone
    from apps.billing.models import Folio
    from apps.billing.services_v2 import BillingService
    from apps.reservations.services import create_reservation

    if not full_name or not full_name.strip():
        raise ValueError("Walk-in 住客姓名必填。")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise ValueError("Walk-in 必须提供 1 至 128 字符的幂等键。")

    # Guest 复用或创建
    phone_normalized = str(phone or "").strip()
    guest = None
    if phone_normalized:
        guest = Guest.objects.filter(
            property=property_obj,
            phones__phone_normalized=phone_normalized).first()
    if guest is None:
        guest = Guest.objects.create(property=property_obj, full_name=full_name)
    if phone_normalized:
        GuestPhone.objects.get_or_create(
            guest=guest, phone_normalized=phone_normalized,
            defaults={"phone": phone_normalized, "source": "WALK_IN"})

    # Reservation + ReservationRoom + Folio + Hold（走 create_reservation 服务）
    payload = {
        "full_name": full_name.strip(),
        "phone": phone_normalized or f"000000{abs(hash(key)) % 100000000:08d}",
        "room_type_id": room_type_id, "rate_plan_id": rate_plan_id,
        "check_in": timezone.localdate(), "check_out": timezone.localdate() + timedelta(days=nights),
        "rooms": 1, "adults": adults, "children": children,
    }
    reservation, created, token = create_reservation(
        property_obj=property_obj, payload=payload, idempotency_key=key)
    reservation.source = Reservation.Source.WALK_IN
    reservation.save(update_fields=("source", "updated_at"))

    line = reservation.reservation_rooms.first()

    # 排房 + 入住（同事务）
    from apps.inventory.models import Room
    target_room = Room.objects.filter(
        room_type_id=room_type_id, occupancy_status=Room.OccupancyStatus.VACANT,
        housekeeping_status=Room.HousekeepingStatus.CLEAN,
        maintenance_status=Room.MaintenanceStatus.NORMAL).first()
    if target_room is None:
        raise ValueError("当前无可入住净房，Walk-in 无法完成。")
    assign_room(property_obj=property_obj, reservation_room_id=line.pk,
                room_id=target_room.pk, assigned_by=created_by)
    stay, _ = check_in_stay(property_obj=property_obj,
                            reservation_room_id=line.pk, checked_in_by=created_by)

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
