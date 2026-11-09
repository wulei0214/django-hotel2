"""事务化前台排房、入住、收款与退房服务。"""
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry, Payment
from apps.inventory.models import Room
from apps.inventory.services import _property_today
from apps.core.models import AuditEvent
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


def _money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FrontDeskOperationError("金额格式无效。") from exc
    if not amount.is_finite() or amount <= 0:
        raise FrontDeskOperationError("金额必须是大于 0 的有效数字。")
    try:
        exact_to_cent = amount == amount.quantize(CENT)
    except InvalidOperation as exc:
        # WB-01：超出 Decimal 上下文精度的超大值统一拒绝为业务校验错误。
        raise FrontDeskOperationError("金额最多保留两位小数。") from exc
    if not exact_to_cent:
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
    AuditEvent.record(
        action="frontdesk.room_assigned", entity_type="ReservationRoom",
        entity_id=str(line.pk), actor=assigned_by, property_obj=property_obj,
        after={"booking_no": reservation.booking_no, "room": room.number},
        reason="前台排房")
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
    # §4.2 营业日结后的业务写入限制（P0-C 缺口收口）：当日营业日已完成夜审关账
    # 后，拒绝再生成新的在住 Stay（新入住的房费过租会归属已关账营业日）。
    # 仅阻挡入住；排房/换房不产生营业日归属账务，允许为次日到店提前排房。
    from apps.core.models import BusinessDate
    if BusinessDate.objects.filter(
        property=property_obj,
        business_date=today,
        status=BusinessDate.Status.CLOSED,
    ).exists():
        raise FrontDeskOperationError(
            f"当前营业日 {today.isoformat()} 已完成夜审关账，不能办理新入住；"
            "请等待下一营业日开启，或走营业日重开审批/break-glass 流程。"
        )
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
    AuditEvent.record(
        action="frontdesk.checked_in", entity_type="Stay",
        entity_id=str(stay.pk), actor=checked_in_by, property_obj=property_obj,
        after={"booking_no": reservation.booking_no, "room": room.number,
               "status": stay.status},
        reason="前台办理入住")
    return stay, True


@transaction.atomic
def collect_manual_payment(
    *, property_obj, booking_no, amount, method, idempotency_key, received_by=None
):
    """前台人工收款：委托 BillingService 统一写入 Payment、LedgerEntry 与审计。"""
    method = str(method or "").strip().upper()
    method = "TRANSFER" if method == "BANK_TRANSFER" else method
    if method not in {"CASH", "CARD", "TRANSFER"}:
        raise FrontDeskOperationError("收款方式只支持现金、刷卡或转账。")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 64:
        raise FrontDeskOperationError("请提供 1 至 64 个字符的 Idempotency-Key。")
    reservation = Reservation.objects.select_for_update().filter(
        property=property_obj, booking_no=booking_no).first()
    if reservation is None:
        raise FrontDeskOperationError("未找到该物业下的预订。")
    if reservation.status not in (
        Reservation.Status.PENDING, Reservation.Status.CONFIRMED,
        Reservation.Status.CHECKED_IN,
    ):
        raise FrontDeskOperationError("当前预订状态不允许前台收款。")
    provider_ref = f"FD{property_obj.pk}-{key}"
    existing = Payment.objects.filter(provider_ref=provider_ref).first()
    if existing is not None and (
        existing.property_id != property_obj.pk
        or existing.reservation_id != reservation.pk
        or existing.amount != _money(amount)
        or existing.method != method
        or existing.provider != "frontdesk_manual"
        or existing.status != Payment.Status.SUCCEEDED
    ):
        raise FrontDeskOperationError(
            "该 Idempotency-Key 已用于其他收款请求，不能重复使用。")
    from apps.billing.services_v2 import BillingService
    try:
        payment = BillingService.collect_payment(
            property_obj=property_obj, reservation=reservation,
            amount=amount, method=method, collected_by=received_by,
            reference=provider_ref)
    except ValueError as exc:
        raise FrontDeskOperationError(str(exc)) from exc

    # 业务闭环缺口修复（用户全链路串测发现）：未接入真实支付渠道时，
    # PENDING 公开订单没有任何确认路径（收款只收已确认单、入住又要求
    # 已确认）→ 永远卡在 PENDING。现定：前台收款成功即确认，状态迁移
    # 与在线支付回调同口径（先确认库存占用，再置 CONFIRMED），并落审计。
    if reservation.status == Reservation.Status.PENDING:
        from apps.inventory.services import confirm_inventory_for_reservation
        from apps.inventory.services import InventoryUnavailable
        try:
            confirm_inventory_for_reservation(reservation)
        except InventoryUnavailable as exc:
            raise FrontDeskOperationError(str(exc)) from exc
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=("status", "updated_at"))
        from apps.billing.services import cancel_open_payment_intents_for_reservation
        cancel_open_payment_intents_for_reservation(reservation)
        AuditEvent.record(
            action="reservation.confirmed_at_frontdesk",
            entity_type="Reservation", entity_id=reservation.booking_no,
            property_obj=property_obj, actor=received_by,
            reason=f"前台收款确认（{method} {amount}，凭证 {payment.provider_ref}）")

    return payment, existing is None


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
        property=property_obj, reservation=reservation,
        folio_type=Folio.FolioType.MASTER,
    ).first()
    if folio is None or folio.status != Folio.Status.OPEN:
        raise FrontDeskOperationError("账单不存在或已关闭，不能办理退房。")

    # 退房房费按净额入账（用户全链路串测发现双记风险）：夜审已按晚过账
    # 的部分（night-audit:*:stay:{pk} 凭证）不得在退房时以整单金额重复入账。
    # 归集口径=本入住单（多房预订共用一本 Folio，不能把兄弟房间的房费算进来）。
    # 待补房费 = 本间合同房费 − 本间已入账房费；为 0 则不再生成凭证。
    charge_reference = f"stay-room-charge:{stay.pk}"
    posted_for_stay = (
        folio.entries.filter(
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            reference=charge_reference,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    ) + (
        folio.entries.filter(
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            reference__endswith=f":stay:{stay.pk}",
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    remaining_room_charge = (line.amount or Decimal("0.00")) - posted_for_stay
    if remaining_room_charge > 0 and not folio.entries.filter(
        reference=charge_reference
    ).exists():
        from apps.billing.services_v2 import BillingService
        BillingService.post_entry(
            property_obj=property_obj, folio=folio, stay=stay,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=remaining_room_charge, currency=folio.currency,
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
    no_show_lines = NoShowRecord.objects.filter(
        property=property_obj, reservation_room__reservation=reservation
    ).count()
    final_room_checkout = total_lines > 0 and checked_out_before + no_show_lines + 1 >= total_lines

    balance = _folio_balance(folio)
    if balance > 0:
        raise FrontDeskOperationError(
            f"账单尚有应收余额 {balance:.2f} {folio.currency}，请先完成收款。"
        )
    if balance < 0 and final_room_checkout:
        # DEC-02：允许客人先离店，但必须在同一事务内登记待退款负债。
        # 只记录候选原支付流水，不自动分配退款金额，也不自动创建 Refund。
        # DEC-02-B2：无可靠原支付来源/候选余额不足时，若存在已批准的异常
        # 放行申请，则在同一事务内消费放行并建立"待财务核查"记录（不创建
        # 正式负债）；无已批准放行则维持阻断并给出申请指引。
        try:
            from apps.billing.liabilities import register_refund_liability
            register_refund_liability(
                property_obj=property_obj,
                reservation=reservation,
                folio=folio,
                amount=abs(balance),
                created_by=checked_out_by,
            )
        except ValueError as exc:
            from apps.billing.dec02 import consume_approved_exception_release
            release = consume_approved_exception_release(
                property_obj=property_obj, folio=folio, stay=stay,
                amount=abs(balance), actor=checked_out_by,
                trigger_reason=str(exc))
            if release is None:
                raise FrontDeskOperationError(
                    f"{exc} 如需让客人先离店，请发起异常放行申请并由值班经理审批。") from exc

    now = timezone.now()
    StayRoomSegment.objects.filter(stay=stay, ended_at__isnull=True).update(
        ended_at=now, active_stay_key=None)
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
    AuditEvent.record(
        action="frontdesk.checked_out", entity_type="Stay",
        entity_id=str(stay.pk), actor=checked_out_by, property_obj=property_obj,
        after={"booking_no": reservation.booking_no, "room": room.number,
               "status": stay.status},
        reason="前台办理退房")
    return stay, True
