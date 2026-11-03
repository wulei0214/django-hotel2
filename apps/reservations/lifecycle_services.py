"""PMS 2.0 前台入住生命周期：续住、换房、No-Show、押金和退款审批。"""
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import ApprovalRecord, Folio, LedgerEntry, Payment, Refund
from apps.billing.services_v2 import ApprovalRecord as RegisteredApprovalRecord, ApprovalService
from apps.inventory.models import DailyRate, Room, RoomTypeInventory
from apps.inventory.services import InventoryUnavailable, release_sold_inventory_for_room_line
from .models import NoShowRecord, Reservation, ReservationRoom, Stay, StayRoomSegment


CENT = Decimal("0.01")


class LifecycleOperationError(Exception):
    """前台入住生命周期操作冲突。"""


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
        raise LifecycleOperationError("金额格式无效。") from exc
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
        raise LifecycleOperationError("金额必须大于 0 且最多保留两位小数。")
    return amount


def _folio_for_update(property_obj, reservation):
    folio = Folio.objects.select_for_update().filter(
        property=property_obj, reservation=reservation
    ).first()
    if folio is None or folio.status != Folio.Status.OPEN:
        raise LifecycleOperationError("账单不存在或已关闭，不能执行该操作。")
    return folio


@transaction.atomic
def extend_stay(*, property_obj, stay_id, new_check_out, reason, extended_by=None):
    """按新增住宿日的实时日价与库存续住，并更新订单金额快照。"""
    reason = str(reason or "").strip()
    if not reason or len(reason) > 240:
        raise LifecycleOperationError("续住必须填写 1 至 240 个字符的原因。")
    stay = Stay.objects.select_for_update().filter(
        pk=stay_id, property=property_obj, status=Stay.Status.IN_HOUSE
    ).first()
    if stay is None:
        raise LifecycleOperationError("未找到该物业下的在住记录。")
    line = ReservationRoom.objects.select_for_update().get(
        pk=stay.reservation_room_id, property=property_obj
    )
    reservation = Reservation.objects.select_for_update().get(
        pk=line.reservation_id, property=property_obj
    )
    if reservation.status != Reservation.Status.CHECKED_IN:
        raise LifecycleOperationError("只有部分或全部已办理入住的预订可以续住。")
    _folio_for_update(property_obj, reservation)
    old_check_out = line.check_out
    if new_check_out == old_check_out:
        return stay, line, Decimal("0.00")
    if new_check_out < old_check_out:
        raise LifecycleOperationError("新的离店日期不能早于当前离店日期。")
    if old_check_out < _property_today(property_obj):
        raise LifecycleOperationError("原离店日期已过，不能直接续住，请先走异常入住处理。")
    room_conflict = ReservationRoom.objects.filter(
        property=property_obj,
        assigned_room_id=stay.room_id,
        check_in__lt=new_check_out,
        check_out__gt=old_check_out,
        reservation__status__in=(
            Reservation.Status.PENDING,
            Reservation.Status.CONFIRMED,
            Reservation.Status.CHECKED_IN,
        ),
    ).exclude(pk=line.pk).exists()
    if room_conflict:
        raise LifecycleOperationError("当前实际房间在续住区间已被其他有效预订占用，不能续住。")

    plan = line.rate_plan
    if (
        not plan.is_active
        or plan.property_id != property_obj.pk
        or plan.valid_from > old_check_out
        or (plan.valid_to and plan.valid_to < new_check_out - timedelta(days=1))
    ):
        raise LifecycleOperationError("原房价计划未覆盖续住日期，不能自动续住。")

    dates = []
    day = old_check_out
    while day < new_check_out:
        dates.append(day)
        day += timedelta(days=1)

    rates = list(
        DailyRate.objects.select_for_update()
        .filter(
            property=property_obj,
            rate_plan=plan,
            room_type_id=line.room_type_id,
            stay_date__in=dates,
        )
        .order_by("stay_date")
    )
    if len(rates) != len(dates) or any(item.closed for item in rates):
        raise LifecycleOperationError("续住日期存在缺失、关房或不可售房价。")
    full_nights = (new_check_out - line.check_in).days
    if any(
        full_nights < item.min_stay or
        (item.max_stay is not None and full_nights > item.max_stay)
        for item in rates
    ):
        raise LifecycleOperationError("续住后的总连住晚数不符合房价计划限制。")
    currencies = {item.currency for item in rates}
    if currencies != {reservation.currency}:
        raise LifecycleOperationError("续住价格币种与原预订不一致，需人工处理。")

    stocks = list(
        RoomTypeInventory.objects.select_for_update()
        .filter(property=property_obj, room_type_id=line.room_type_id, stay_date__in=dates)
        .order_by("stay_date")
    )
    if len(stocks) != len(dates):
        raise LifecycleOperationError("续住日期库存日历不完整。")
    if any(row.stop_sell or row.available_count < 1 for row in stocks):
        raise LifecycleOperationError("续住日期房量不足或已停售。")

    extension_amount = sum((item.price for item in rates), Decimal("0.00"))
    if extension_amount <= 0:
        raise LifecycleOperationError("续住金额必须大于 0。")
    nightly_rates = list(line.nightly_rates or [])
    nightly_rates.extend({
        "date": item.stay_date.isoformat(),
        "price": str(item.price),
        "currency": item.currency,
    } for item in rates)

    for row in stocks:
        row.sold += 1
        row.version += 1
        row.save(update_fields=("sold", "version", "updated_at"))

    line.check_out = new_check_out
    line.amount += extension_amount
    line.nightly_rates = nightly_rates
    line.save(update_fields=("check_out", "amount", "nightly_rates"))

    line_summaries = list(
        ReservationRoom.objects.filter(property=property_obj, reservation=reservation)
        .values_list("check_out", "amount")
    )
    reservation.check_out = max(item[0] for item in line_summaries)
    reservation.total_amount = sum((item[1] for item in line_summaries), Decimal("0.00"))
    reservation.save(update_fields=("check_out", "total_amount", "updated_at"))

    return stay, line, extension_amount


@transaction.atomic
def change_stay_room(*, property_obj, stay_id, new_room_id, reason, moved_by=None):
    """在住换房：保留房段历史，Stay.room 只作为当前房间指针。"""
    reason = str(reason or "").strip()
    if not reason or len(reason) > 240:
        raise LifecycleOperationError("换房必须填写 1 至 240 个字符的原因。")
    stay = Stay.objects.select_for_update().filter(
        pk=stay_id, property=property_obj, status=Stay.Status.IN_HOUSE
    ).first()
    if stay is None:
        raise LifecycleOperationError("未找到该物业下的在住记录。")
    line = ReservationRoom.objects.select_for_update().get(
        pk=stay.reservation_room_id, property=property_obj
    )
    reservation = Reservation.objects.select_for_update().get(
        pk=line.reservation_id, property=property_obj
    )
    if int(new_room_id) == stay.room_id:
        return stay, False

    new_room = Room.objects.select_for_update().filter(
        pk=new_room_id, property=property_obj, is_active=True
    ).first()
    if new_room is None:
        raise LifecycleOperationError("目标房间不存在或不属于当前酒店。")
    if new_room.room_type_id != line.room_type_id:
        raise LifecycleOperationError("暂只支持同房型换房；跨房型差价需要独立价格审批规则。")
    if not new_room.is_sellable_now:
        raise LifecycleOperationError("目标房间不是空闲净房或处于维修停用状态。")
    if Stay.objects.filter(
        property=property_obj, room=new_room, status=Stay.Status.IN_HOUSE
    ).exclude(pk=stay.pk).exists():
        raise LifecycleOperationError("目标房间已有其他客人在住。")

    conflict = ReservationRoom.objects.filter(
        property=property_obj,
        assigned_room=new_room,
        check_in__lt=line.check_out,
        check_out__gt=_property_today(property_obj),
        reservation__status__in=(
            Reservation.Status.PENDING, Reservation.Status.CONFIRMED,
            Reservation.Status.CHECKED_IN,
        ),
    ).exclude(pk=line.pk).exists()
    if conflict:
        raise LifecycleOperationError("目标房间在剩余住宿日期已有其他有效预订。")

    old_room = Room.objects.select_for_update().get(pk=stay.room_id, property=property_obj)
    now = timezone.now()
    current_segment = StayRoomSegment.objects.select_for_update().filter(
        stay=stay, ended_at__isnull=True
    ).first()
    if current_segment is None:
        current_segment = StayRoomSegment.objects.create(
            property=property_obj,
            stay=stay,
            room=old_room,
            started_at=stay.actual_check_in or stay.created_at,
            move_reason="初始入住房间（系统补建）",
        )
    current_segment.ended_at = now
    current_segment.move_reason = reason
    current_segment.moved_by = moved_by
    current_segment.save(update_fields=("ended_at", "move_reason", "moved_by"))

    StayRoomSegment.objects.create(
        property=property_obj, stay=stay, room=new_room,
        started_at=now, moved_by=moved_by, move_reason=reason,
    )
    old_room.occupancy_status = Room.OccupancyStatus.VACANT
    old_room.housekeeping_status = Room.HousekeepingStatus.DIRTY
    old_room.save(update_fields=("occupancy_status", "housekeeping_status", "updated_at"))
    new_room.occupancy_status = Room.OccupancyStatus.OCCUPIED
    new_room.save(update_fields=("occupancy_status", "updated_at"))
    stay.room = new_room
    stay.save(update_fields=("room", "updated_at"))
    line.assigned_room = new_room
    line.save(update_fields=("assigned_room",))
    return stay, True


@transaction.atomic
def mark_reservation_room_no_show(*, property_obj, reservation_room_id, reason, processed_by):
    """对多房订单中的单间明细执行 No-Show；已入住明细禁止标未到。"""
    reason = str(reason or "").strip()
    if not reason or len(reason) > 240:
        raise LifecycleOperationError("No-Show 必须填写 1 至 240 个字符的原因。")
    line = ReservationRoom.objects.select_for_update().filter(
        pk=reservation_room_id, property=property_obj
    ).first()
    if line is None:
        raise LifecycleOperationError("未找到该物业下的预订房间明细。")
    reservation = Reservation.objects.select_for_update().get(
        pk=line.reservation_id, property=property_obj
    )
    existing = NoShowRecord.objects.filter(reservation_room=line).first()
    if existing:
        return existing, False
    if reservation.status not in (Reservation.Status.CONFIRMED, Reservation.Status.CHECKED_IN):
        raise LifecycleOperationError("只有已确认或部分入住的预订可以处理 No-Show。")
    today = _property_today(property_obj)
    if line.check_in > today:
        raise LifecycleOperationError("预订入住日期尚未到达，不能标记 No-Show。")
    if line.check_out <= today:
        raise LifecycleOperationError("预订住宿日期已结束，不能补做 No-Show。")
    if Stay.objects.filter(reservation_room=line).exists():
        raise LifecycleOperationError("该房间明细已有实际入住记录，不能标记 No-Show。")

    try:
        released_nights = release_sold_inventory_for_room_line(line, from_date=today)
    except InventoryUnavailable as exc:
        raise LifecycleOperationError(str(exc)) from exc

    record = NoShowRecord.objects.create(
        property=property_obj,
        reservation_room=line,
        reason=reason,
        processed_by=processed_by,
        inventory_released_from=today,
        released_nights=released_nights,
    )
    if line.assigned_room_id:
        line.assigned_room = None
        line.save(update_fields=("assigned_room",))

    lines = list(
        ReservationRoom.objects.filter(property=property_obj, reservation=reservation)
        .values_list("id", flat=True)
    )
    no_show_count = NoShowRecord.objects.filter(reservation_room_id__in=lines).count()
    has_any_stay = Stay.objects.filter(
        property=property_obj, reservation_room_id__in=lines
    ).exists()
    if lines:
        completed_stays = Stay.objects.filter(
            property=property_obj, reservation_room_id__in=lines,
            status=Stay.Status.CHECKED_OUT,
        ).count()
        active_stays = Stay.objects.filter(
            property=property_obj, reservation_room_id__in=lines,
            status=Stay.Status.IN_HOUSE,
        ).exists()
        if not active_stays and no_show_count + completed_stays == len(lines):
            reservation.status = (
                Reservation.Status.NO_SHOW if no_show_count == len(lines)
                else Reservation.Status.CHECKED_OUT
            )
            reservation.save(update_fields=("status", "updated_at"))
    return record, True


@transaction.atomic
def receive_manual_deposit(
    *, property_obj, booking_no, amount, method, idempotency_key, received_by=None
):
    """登记已经实际收到的押金；账本以负数 DEPOSIT 凭证记入住客贷方。"""
    amount = _money(amount)
    method = str(method or "").strip().upper()
    if method not in {"CASH", "CARD", "BANK_TRANSFER", "OTHER"}:
        raise LifecycleOperationError("押金方式只支持 CASH、CARD、BANK_TRANSFER 或 OTHER。")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 64:
        raise LifecycleOperationError("押金登记必须提供 1 至 64 个字符的 Idempotency-Key。")
    reservation = Reservation.objects.select_for_update().filter(
        property=property_obj, booking_no=booking_no,
        status__in=(Reservation.Status.CONFIRMED, Reservation.Status.CHECKED_IN),
    ).first()
    if reservation is None:
        raise LifecycleOperationError("未找到可收取押金的已确认/在住预订。")
    folio = _folio_for_update(property_obj, reservation)
    provider_ref = f"FDDEP-{property_obj.pk}-{key}"
    existing = Payment.objects.filter(provider_ref=provider_ref).first()
    if existing:
        if (
            existing.reservation_id != reservation.pk or existing.amount != amount
            or existing.method != method or existing.provider != "frontdesk_deposit"
        ):
            raise LifecycleOperationError("该 Idempotency-Key 已用于其他押金登记。")
        return existing, False
    already_deposited = reservation.deposit_amount
    if already_deposited + amount > reservation.total_amount:
        raise LifecycleOperationError("累计押金不能超过当前预订房费总额。")
    now = timezone.now()
    payment = Payment.objects.create(
        property=property_obj, reservation=reservation, folio=folio,
        provider="frontdesk_deposit", provider_ref=provider_ref,
        method=method, amount=amount, currency=folio.currency,
        status=Payment.Status.SUCCEEDED, paid_at=now,
    )
    LedgerEntry.objects.create(
        property=property_obj, folio=folio, entry_type=LedgerEntry.EntryType.DEPOSIT,
        amount=-amount, currency=folio.currency, business_date=_property_today(property_obj),
        reference=f"deposit:{provider_ref}", description=f"前台登记押金（{method}）",
        posted_by=received_by,
    )
    reservation.deposit_amount += amount
    reservation.save(update_fields=("deposit_amount", "updated_at"))
    return payment, True


@transaction.atomic
def request_refund(*, property_obj, payment_id, amount, reason, idempotency_key, requested_by):
    """登记退款申请；待独立审批前不改变账本余额。"""
    amount = _money(amount)
    reason = str(reason or "").strip()
    key = str(idempotency_key or "").strip()
    if not reason or len(reason) > 240:
        raise LifecycleOperationError("退款原因必须填写，且不能超过 240 个字符。")
    if not key or len(key) > 64:
        raise LifecycleOperationError("退款申请必须提供 1 至 64 个字符的 Idempotency-Key。")
    payment = Payment.objects.select_for_update().filter(
        pk=payment_id, property=property_obj
    ).first()
    if payment is None:
        raise LifecycleOperationError("未找到该物业下的支付流水。")
    existing = Refund.objects.filter(property=property_obj, idempotency_key=key).first()
    if existing:
        if existing.payment_id != payment.pk or existing.amount != amount or existing.reason != reason:
            raise LifecycleOperationError("该 Idempotency-Key 已用于其他退款申请。")
        return existing, False
    if payment.status not in (Payment.Status.SUCCEEDED, Payment.Status.PARTIALLY_REFUNDED):
        raise LifecycleOperationError("只有成功或部分退款的支付流水可以申请退款。")
    reserved = Refund.objects.filter(
        payment=payment, status__in=(Refund.Status.PENDING, Refund.Status.SUCCEEDED)
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    if reserved + amount > payment.amount:
        raise LifecycleOperationError(
            f"累计申请及已完成退款不能超过原支付金额 {payment.amount:.2f}。"
        )
    refund = Refund.objects.create(
        property=property_obj, payment=payment, amount=amount, currency=payment.currency,
        reason=reason, idempotency_key=key, provider_ref="RFD-" + uuid.uuid4().hex.upper(),
        status=Refund.Status.PENDING, requested_by=requested_by,
    )
    approval, _ = ApprovalService.request_approval(
        property_obj=property_obj, action_code="billing.refund",
        entity_type="Refund", entity_id=refund.pk,
        requested_by=requested_by, reason=reason,
        payload={"payment_id": payment.pk, "amount": str(amount),
                 "currency": payment.currency})
    refund.approval_record = approval
    refund.save(update_fields=("approval_record",))
    return refund, True


@transaction.atomic
def approve_refund(*, property_obj, refund_id, approved_by):
    """由第二人确认人工退款已实际完成，追加不可变 REFUND 凭证。"""
    refund = Refund.objects.select_for_update().filter(
        pk=refund_id, property=property_obj
    ).first()
    if refund is None:
        raise LifecycleOperationError("退款申请不存在。")
    if refund.status == Refund.Status.SUCCEEDED:
        return refund, False
    if refund.status != Refund.Status.PENDING:
        raise LifecycleOperationError("只有待审批退款申请可以审批。")
    if refund.requested_by_id == approved_by.pk:
        raise LifecycleOperationError("退款申请人不能审批自己的退款申请。")
    approval = refund.approval_record
    if approval is None:
        # 兼容迁移前尚未关联审批记录的待处理退款：补建后仍由第二人审批。
        approval, _ = ApprovalService.request_approval(
            property_obj=property_obj, action_code="billing.refund",
            entity_type="Refund", entity_id=refund.pk,
            requested_by=refund.requested_by, reason=refund.reason,
            payload={"payment_id": refund.payment_id,
                     "amount": str(refund.amount), "currency": refund.currency})
        refund.approval_record = approval
        refund.save(update_fields=("approval_record",))
    if approval.status == ApprovalRecord.Status.PENDING:
        approval = ApprovalService.approve(
            approval.pk, approved_by=approved_by, property_obj=property_obj)
    if approval.status != ApprovalRecord.Status.APPROVED or approval.approved_by_id != approved_by.pk:
        raise LifecycleOperationError("退款审批记录未由当前审批人批准。")
    payment = Payment.objects.select_for_update().get(pk=refund.payment_id, property=property_obj)
    successful_total = Refund.objects.filter(
        payment=payment, status=Refund.Status.SUCCEEDED
    ).exclude(pk=refund.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    if successful_total + refund.amount > payment.amount:
        raise LifecycleOperationError("原支付可退款余额不足，请核对其他退款申请。")
    folio = Folio.objects.select_for_update().filter(
        property=property_obj, reservation=payment.reservation
    ).first()
    if folio is None or folio.status == Folio.Status.VOIDED:
        raise LifecycleOperationError("原支付对应账单不存在或已作废，不能登记退款。")
    LedgerEntry.objects.create(
        property=property_obj, folio=folio, entry_type=LedgerEntry.EntryType.REFUND,
        amount=refund.amount, currency=refund.currency,
        business_date=_property_today(property_obj),
        reference=f"refund:{refund.provider_ref}",
        description=f"人工退款完成：{refund.reason}",
        posted_by=approved_by,
    )
    refund.status = Refund.Status.SUCCEEDED
    refund.approved_by = approved_by
    refund.processed_at = timezone.now()
    refund.save(update_fields=("status", "approved_by", "processed_at"))
    from apps.core.models import AuditEvent
    AuditEvent.record(
        action="billing.refund_completed", entity_type="Refund",
        entity_id=refund.pk, actor=approved_by, property_obj=property_obj,
        after={"amount": str(refund.amount), "status": refund.status},
        reason=refund.reason)

    refunded_total = Refund.objects.filter(
        payment=payment, status=Refund.Status.SUCCEEDED
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    payment.status = (
        Payment.Status.REFUNDED if refunded_total >= payment.amount
        else Payment.Status.PARTIALLY_REFUNDED
    )
    payment.save(update_fields=("status", "updated_at"))
    if payment.provider == "frontdesk_deposit":
        reservation = Reservation.objects.select_for_update().get(pk=payment.reservation_id)
        reservation.deposit_amount = max(Decimal("0.00"), reservation.deposit_amount - refund.amount)
        reservation.save(update_fields=("deposit_amount", "updated_at"))
    return refund, True
