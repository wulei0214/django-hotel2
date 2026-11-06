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
from apps.core.models import AuditEvent
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
    if not amount.is_finite() or amount <= 0:
        raise LifecycleOperationError("金额必须大于 0 且最多保留两位小数。")
    try:
        exact_to_cent = amount == amount.quantize(CENT)
    except InvalidOperation as exc:
        # WB-01：超出 Decimal 上下文精度的超大值统一拒绝为业务校验错误。
        raise LifecycleOperationError(
            "金额必须大于 0 且最多保留两位小数。") from exc
    if not exact_to_cent:
        raise LifecycleOperationError("金额必须大于 0 且最多保留两位小数。")
    return amount


def _folio_for_update(property_obj, reservation):
    # WB-07：续住/换房门禁只看主账单；不限定类型时，Meta ordering=-opened_at
    # 会让更晚开立的非主账单掩盖已关闭的主账单。
    folio = Folio.objects.select_for_update().filter(
        property=property_obj, reservation=reservation,
        folio_type=Folio.FolioType.MASTER,
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

    AuditEvent.record(
        action="frontdesk.stay_extended", entity_type="Stay",
        entity_id=str(stay.pk), actor=extended_by, property_obj=property_obj,
        after={"new_check_out": str(stay.reservation_room.check_out),
               "extension_amount": str(extension_amount)},
        reason=reason[:240])
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

    # FD-61-03：源房间与目标房间必须按主键升序一次性行锁；
    # 分两次加锁时，双向换房并发（A→B 与 B→A）会形成 ABBA 死锁窗口。
    room_ids = sorted({int(new_room_id), stay.room_id})
    locked_rooms = Room.objects.select_for_update().filter(
        pk__in=room_ids, property=property_obj
    ).in_bulk()
    new_room = locked_rooms.get(int(new_room_id))
    if new_room is None or not new_room.is_active:
        raise LifecycleOperationError("目标房间不存在或不属于当前酒店。")
    old_room = locked_rooms.get(stay.room_id)
    if old_room is None:
        raise LifecycleOperationError("当前入住房间不存在或不属于当前酒店。")
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

    old_room = locked_rooms[stay.room_id]
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
    AuditEvent.record(
        action="frontdesk.stay_room_changed", entity_type="Stay",
        entity_id=str(stay.pk), actor=moved_by, property_obj=property_obj,
        after={"new_room": new_room.number, "reason": reason[:240]},
        reason="在住换房")
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
    AuditEvent.record(
        action="frontdesk.no_show_processed", entity_type="ReservationRoom",
        entity_id=str(record.reservation_room_id), actor=processed_by,
        property_obj=property_obj,
        after={"released_nights": record.released_nights,
               "reason": reason[:240]},
        reason="No-Show 处理")
    return record, True


@transaction.atomic
def receive_manual_deposit(
    *, property_obj, booking_no, amount, method, idempotency_key, received_by=None
):
    """押金登记统一委托 BillingService，避免绕开账务唯一写入口。"""
    reservation = Reservation.objects.select_for_update().filter(
        property=property_obj, booking_no=booking_no,
        status__in=(Reservation.Status.CONFIRMED, Reservation.Status.CHECKED_IN),
    ).first()
    if reservation is None:
        raise LifecycleOperationError("未找到可收取押金的已确认/在住预订。")
    from apps.billing.services_v2 import BillingService
    try:
        return BillingService.record_deposit(
            property_obj=property_obj, reservation=reservation, amount=amount,
            method=method, idempotency_key=idempotency_key, received_by=received_by)
    except ValueError as exc:
        raise LifecycleOperationError(str(exc)) from exc


@transaction.atomic
def request_refund(*, property_obj, payment_id, amount, reason, idempotency_key, requested_by):
    """退款申请统一委托 BillingService；审批事实和业务申请同事务生成。"""
    from apps.billing.services_v2 import BillingService
    try:
        return BillingService.request_refund(
            property_obj=property_obj, payment_id=payment_id,
            amount=amount, reason=reason, idempotency_key=idempotency_key,
            requested_by=requested_by)
    except ValueError as exc:
        raise LifecycleOperationError(str(exc)) from exc


@transaction.atomic
def approve_refund(*, property_obj, refund_id, approved_by):
    """仅审批退款；不确认实际付款、不执行退款、不写 REFUND 账本凭证。"""
    from apps.billing.services_v2 import BillingService
    try:
        return BillingService.approve_refund(
            property_obj=property_obj, refund_id=refund_id,
            approved_by=approved_by)
    except ValueError as exc:
        raise LifecycleOperationError(str(exc)) from exc


@transaction.atomic
def reject_refund(*, property_obj, refund_id, rejected_by, reason):
    """退款驳回统一委托 BillingService，保持支付状态、审批与审计同事务。"""
    from apps.billing.services_v2 import BillingService
    try:
        return BillingService.reject_refund(
            property_obj=property_obj, refund_id=refund_id,
            rejected_by=rejected_by, reason=reason)
    except ValueError as exc:
        raise LifecycleOperationError(str(exc)) from exc
