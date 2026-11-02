"""事务化前台排房、入住、收款与退房服务。"""
import logging
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry, Payment
from apps.inventory.models import Room
from .models import NoShowRecord, Reservation, ReservationRoom, Stay, StayRoomSegment


audit = logging.getLogger("hotel.audit")
CENT = Decimal("0.01")
ACTIVE_RESERVATION_STATUSES = (
    Reservation.Status.PENDING,
    Reservation.Status.CONFIRMED,
    Reservation.Status.CHECKED_IN,
)
MANUAL_PAYMENT_METHODS = {"CASH", "CARD", "BANK_TRANSFER", "OTHER"}


class FrontDeskOperationError(Exception):
    """前台操作不满足业务状态或账务约束。"""


def _property_today(property_obj):
    try:
        tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return timezone.localdate(timezone.now(), tz)


def _money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FrontDeskOperationError("金额格式无效。") from exc
    if not amount.is_finite() or amount <= 0:
        raise FrontDeskOperationError("金额必须是大于 0 的有效数字。")
    if amount != amount.quantize(CENT):
        raise FrontDeskOperationError("金额最多保留两位小数。")
    return amount


def _folio_balance(folio):
    return sum(
        folio.entries.values_list("amount", flat=True),
        start=Decimal("0.00"),
    )


def _check_room_assignment_conflict(line, room):
    conflict = ReservationRoom.objects.filter(
        property=line.property,
        assigned_room=room,
        check_in__lt=line.check_out,
        check_out__gt=line.check_in,
        reservation__status__in=ACTIVE_RESERVATION_STATUSES,
    ).exclude(pk=line.pk).exists()
    if conflict:
        raise FrontDeskOperationError("该房间在预订住宿日期内已有其他有效预订占用。")


@transaction.atomic
def assign_room(*, property_obj, reservation_room_id, room_id, assigned_by=None):
    """将已确认预订的一间房分配到同物业、同房型且可售的物理客房。"""
    line = (
        ReservationRoom.objects.select_for_update()
        .filter(
            pk=reservation_room_id,
            property=property_obj,
            reservation__property=property_obj,
        )
        .first()
    )
    if line is None:
        raise FrontDeskOperationError("未找到该物业下的预订房间明细。")

    reservation = Reservation.objects.select_for_update().get(pk=line.reservation_id)
    if NoShowRecord.objects.filter(reservation_room=line).exists():
        raise FrontDeskOperationError("该房间明细已处理为 No-Show，不能再次排房。")
    if reservation.status not in (
        Reservation.Status.CONFIRMED,
        Reservation.Status.CHECKED_IN,
    ):
        raise FrontDeskOperationError("只有已确认或部分办理入住的预订可以排房。")
    if line.check_out <= _property_today(property_obj):
        raise FrontDeskOperationError("该预订的离店日期已过，不能再排房。")
    if Stay.objects.filter(reservation_room=line).exists():
        raise FrontDeskOperationError("该房间明细已生成实际入住记录，不能重新排房。")

    if line.assigned_room_id and line.assigned_room_id != room_id:
        raise FrontDeskOperationError("该预订明细已分配其他房间；换房请走独立换房流程。")
    room = (
        Room.objects.select_for_update()
        .filter(pk=room_id, property=property_obj, room_type_id=line.room_type_id)
        .first()
    )
    if room is None:
        raise FrontDeskOperationError("房间不存在、物业不匹配或房型不匹配。")
    if not room.is_sellable_now:
        raise FrontDeskOperationError("该房间当前不是可分配净房。")
    _check_room_assignment_conflict(line, room)

    if line.assigned_room_id == room.pk:
        return line, False
    line.assigned_room = room
    line.save(update_fields=("assigned_room",))
    audit.info(
        "前台排房 user=%s booking=%s line=%s room=%s",
        getattr(assigned_by, "username", "system"),
        reservation.booking_no,
        line.pk,
        room.number,
    )
    return line, True


@transaction.atomic
def check_in_stay(*, property_obj, reservation_room_id, checked_in_by=None):
    """把一条已排房预订明细转为实际在住 Stay。"""
    line = (
        ReservationRoom.objects.select_for_update()
        .filter(
            pk=reservation_room_id,
            property=property_obj,
            reservation__property=property_obj,
        )
        .first()
    )
    if line is None:
        raise FrontDeskOperationError("未找到该物业下的预订房间明细。")

    reservation = Reservation.objects.select_for_update().get(pk=line.reservation_id)
    if NoShowRecord.objects.filter(reservation_room=line).exists():
        raise FrontDeskOperationError("该房间明细已处理为 No-Show，不能办理入住。")
    if reservation.status not in (
        Reservation.Status.CONFIRMED,
        Reservation.Status.CHECKED_IN,
    ):
        raise FrontDeskOperationError("预订尚未确认，不能办理入住。")
    today = _property_today(property_obj)
    if line.check_in > today:
        raise FrontDeskOperationError("尚未到预订入住日期，不能提前办理入住。")
    if line.check_out <= today:
        raise FrontDeskOperationError("预订离店日期已过，不能办理入住。")
    if not line.assigned_room_id:
        raise FrontDeskOperationError("请先为该预订明细分配房间。")

    existing = Stay.objects.select_for_update().filter(
        reservation_room=line
    ).first()
    if existing is not None:
        if existing.status == Stay.Status.IN_HOUSE:
            return existing, False
        raise FrontDeskOperationError("该预订明细已有已退房记录，不能重复入住。")

    room = Room.objects.select_for_update().filter(
        pk=line.assigned_room_id,
        property=property_obj,
        room_type_id=line.room_type_id,
    ).first()
    if room is None or not room.is_sellable_now:
        raise FrontDeskOperationError("已分配房间不再满足入住条件，请重新排房。")
    if Stay.objects.filter(
        property=property_obj,
        room=room,
        status=Stay.Status.IN_HOUSE,
    ).exists():
        raise FrontDeskOperationError("该房间已有其他在住记录。")
    if not Folio.objects.filter(
        property=property_obj, reservation=reservation
    ).exists():
        raise FrontDeskOperationError("预订账单尚未建立，不能办理入住。")

    now = timezone.now()
    stay = Stay.objects.create(
        property=property_obj,
        reservation_room=line,
        room=room,
        actual_check_in=now,
        status=Stay.Status.IN_HOUSE,
    )
    room.occupancy_status = Room.OccupancyStatus.OCCUPIED
    room.save(update_fields=("occupancy_status", "updated_at"))
    StayRoomSegment.objects.create(
        property=property_obj, stay=stay, room=room, started_at=now,
        move_reason="初始入住房间", moved_by=checked_in_by,
    )

    reservation.status = Reservation.Status.CHECKED_IN
    reservation.save(update_fields=("status", "updated_at"))
    audit.info(
        "前台办理入住 user=%s booking=%s stay=%s room=%s",
        getattr(checked_in_by, "username", "system"),
        reservation.booking_no,
        stay.pk,
        room.number,
    )
    return stay, True


@transaction.atomic
def collect_manual_payment(
    *,
    property_obj,
    booking_no,
    amount,
    method,
    idempotency_key,
    received_by=None,
):
    """登记前台已实际收到的现金/刷卡/转账；不调用任何真实支付渠道。"""
    amount = _money(amount)
    method = str(method or "").strip().upper()
    if method not in MANUAL_PAYMENT_METHODS:
        raise FrontDeskOperationError(
            "收款方式只支持 CASH、CARD、BANK_TRANSFER 或 OTHER。"
        )
    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key or len(idempotency_key) > 64:
        raise FrontDeskOperationError("请提供 1 至 64 个字符的 Idempotency-Key。")

    reservation = (
        Reservation.objects.select_for_update()
        .filter(property=property_obj, booking_no=booking_no)
        .first()
    )
    if reservation is None:
        raise FrontDeskOperationError("未找到该物业下的预订。")
    if reservation.status not in (
        Reservation.Status.CONFIRMED,
        Reservation.Status.CHECKED_IN,
    ):
        raise FrontDeskOperationError("当前预订状态不允许前台收款。")

    folio = Folio.objects.select_for_update().filter(
        property=property_obj, reservation=reservation
    ).first()
    if folio is None or folio.status != Folio.Status.OPEN:
        raise FrontDeskOperationError("账单不存在或已关闭，不能收款。")

    provider_ref = f"FD{property_obj.pk}-{idempotency_key}"
    existing = Payment.objects.filter(provider_ref=provider_ref).first()
    if existing is not None:
        if (
            existing.reservation_id != reservation.pk
            or existing.folio_id != folio.pk
            or existing.amount != amount
            or existing.method != method
            or existing.provider != "frontdesk_manual"
            or existing.status != Payment.Status.SUCCEEDED
        ):
            raise FrontDeskOperationError(
                "该 Idempotency-Key 已用于其他收款请求，不能重复使用。"
            )
        return existing, False

    expected_room_charges = sum(
        ReservationRoom.objects.filter(
            property=property_obj, reservation=reservation
        ).values_list("amount", flat=True),
        start=Decimal("0.00"),
    )
    entries = list(folio.entries.all())
    current_balance = sum(
        (entry.amount for entry in entries), start=Decimal("0.00")
    )
    posted_room_charges = sum(
        (
            entry.amount
            for entry in entries
            if entry.entry_type == LedgerEntry.EntryType.ROOM_CHARGE
        ),
        start=Decimal("0.00"),
    )
    outstanding = current_balance + expected_room_charges - posted_room_charges
    if outstanding <= 0:
        raise FrontDeskOperationError("当前账单没有待收余额，不需要重复收款。")
    if amount > outstanding:
        raise FrontDeskOperationError(
            f"收款金额超过当前应收余额 {outstanding:.2f} {folio.currency}。"
        )

    now = timezone.now()
    payment = Payment.objects.create(
        property=property_obj,
        reservation=reservation,
        folio=folio,
        provider="frontdesk_manual",
        provider_ref=provider_ref,
        method=method,
        amount=amount,
        currency=folio.currency,
        status=Payment.Status.SUCCEEDED,
        paid_at=now,
    )
    LedgerEntry.objects.create(
        property=property_obj,
        folio=folio,
        entry_type=LedgerEntry.EntryType.PAYMENT,
        amount=-amount,
        currency=folio.currency,
        business_date=_property_today(property_obj),
        reference=f"payment:{provider_ref}",
        description=f"前台登记收款（{method}）",
        posted_by=received_by,
    )
    audit.info(
        "前台收款 user=%s booking=%s payment=%s method=%s amount=%s currency=%s",
        getattr(received_by, "username", "system"),
        reservation.booking_no,
        payment.provider_ref,
        method,
        amount,
        folio.currency,
    )
    return payment, True


@transaction.atomic
def check_out_stay(*, property_obj, stay_id, checked_out_by=None):
    """结清账单后退房；账本只追加，重复请求幂等。"""
    stay = (
        Stay.objects.select_for_update()
        .filter(pk=stay_id, property=property_obj)
        .first()
    )
    if stay is None:
        raise FrontDeskOperationError("未找到该物业下的入住记录。")
    if stay.status == Stay.Status.CHECKED_OUT:
        return stay, False
    if stay.status != Stay.Status.IN_HOUSE:
        raise FrontDeskOperationError("当前入住记录不允许退房。")

    line = ReservationRoom.objects.select_for_update().get(
        pk=stay.reservation_room_id,
        property=property_obj,
    )
    reservation = Reservation.objects.select_for_update().get(
        pk=line.reservation_id, property=property_obj
    )
    folio = Folio.objects.select_for_update().filter(
        property=property_obj, reservation=reservation
    ).first()
    if folio is None or folio.status != Folio.Status.OPEN:
        raise FrontDeskOperationError("账单不存在或已关闭，不能办理退房。")

    charge_reference = f"stay-room-charge:{stay.pk}"
    if line.amount > 0 and not folio.entries.filter(
        reference=charge_reference
    ).exists():
        LedgerEntry.objects.create(
            property=property_obj,
            folio=folio,
            stay=stay,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=line.amount,
            currency=folio.currency,
            business_date=_property_today(property_obj),
            reference=charge_reference,
            description=f"房费：{line.check_in.isoformat()} 至 {line.check_out.isoformat()}",
            posted_by=checked_out_by,
        )

    total_lines = ReservationRoom.objects.filter(
        property=property_obj, reservation=reservation
    ).count()
    checked_out_before = Stay.objects.filter(
        property=property_obj,
        reservation_room__reservation=reservation,
        status=Stay.Status.CHECKED_OUT,
    ).count()
    final_room_checkout = total_lines > 0 and checked_out_before + 1 >= total_lines

    balance = _folio_balance(folio)
    if balance > 0:
        raise FrontDeskOperationError(
            f"账单尚有应收余额 {balance:.2f} {folio.currency}，请先完成收款。"
        )
    if balance < 0 and final_room_checkout:
        raise FrontDeskOperationError(
            f"最后一间房退房后仍有客人余额 {abs(balance):.2f} {folio.currency}，需先处理退款或余额结转。"
        )

    now = timezone.now()
    StayRoomSegment.objects.filter(stay=stay, ended_at__isnull=True).update(ended_at=now)
    stay.status = Stay.Status.CHECKED_OUT
    stay.actual_check_out = now
    stay.save(update_fields=("status", "actual_check_out", "updated_at"))

    room = Room.objects.select_for_update().get(
        pk=stay.room_id, property=property_obj
    )
    room.occupancy_status = Room.OccupancyStatus.VACANT
    room.housekeeping_status = Room.HousekeepingStatus.DIRTY
    room.save(update_fields=(
        "occupancy_status", "housekeeping_status", "updated_at"
    ))

    if final_room_checkout:
        reservation.status = Reservation.Status.CHECKED_OUT
        folio.status = Folio.Status.CLOSED
        folio.closed_at = now
        folio.save(update_fields=("status", "closed_at"))
    else:
        reservation.status = Reservation.Status.CHECKED_IN
    reservation.save(update_fields=("status", "updated_at"))

    audit.info(
        "前台办理退房 user=%s booking=%s stay=%s room=%s",
        getattr(checked_out_by, "username", "system"),
        reservation.booking_no,
        stay.pk,
        room.number,
    )
    return stay, True
