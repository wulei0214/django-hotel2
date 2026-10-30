"""预订与支付确认服务。"""
import datetime
import secrets
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import Client, LedgerEntry, Payment, Reservation
from . import inventory, ledger

HOLD_MINUTES = 15


class ReservationError(Exception):
    pass


class PaymentError(Exception):
    pass


def _new_booking_no():
    prefix = "R" + timezone.localdate().strftime("%Y%m%d")
    for _ in range(10):
        value = prefix + secrets.token_hex(3).upper()
        if not Reservation.objects.filter(booking_no=value).exists():
            return value
    return prefix + secrets.token_hex(6).upper()


def get_or_create_guest(name, phone, idcard=None):
    name = (name or "").strip()
    phone = (phone or "").strip()
    if not name:
        raise ReservationError("姓名不能为空")
    if not phone:
        raise ReservationError("手机号不能为空")
    guest = Client.objects.filter(手机号=phone).first()
    if guest:
        changed = []
        if name and guest.姓名 != name:
            guest.姓名 = name
            changed.append("姓名")
        if idcard and guest.身份证号 != idcard:
            guest.身份证号 = idcard
            changed.append("身份证号")
        if changed:
            guest.save(update_fields=changed)
        return guest, False
    try:
        return Client.objects.create(姓名=name, 手机号=phone, 身份证号=idcard or None), True
    except IntegrityError:
        return Client.objects.get(手机号=phone), False


def create_reservation(*, name, phone, room_type, check_in, check_out, rooms=1,
                       idempotency_key=None, source="web", idcard=None):
    if rooms < 1 or rooms > 9:
        raise ReservationError("房间数必须在 1-9 之间")
    if check_in < timezone.localdate():
        raise ReservationError("入住日期不能早于今天")
    if check_out <= check_in:
        raise ReservationError("离店日期必须晚于入住日期")

    if idempotency_key:
        existing = Reservation.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing, False

    guest, _ = get_or_create_guest(name, phone, idcard)
    price = inventory.room_type_price(room_type)
    nights = (check_out - check_in).days
    total = ledger.money(price * nights * rooms)

    inventory.expire_stale_holds()
    try:
        with transaction.atomic():
            reservation = Reservation.objects.create(
                booking_no=_new_booking_no(),
                guest=guest,
                room_type=room_type,
                check_in=check_in,
                check_out=check_out,
                rooms=rooms,
                total_amount=total,
                expires_at=timezone.now() + datetime.timedelta(minutes=HOLD_MINUTES),
                idempotency_key=idempotency_key or None,
                source=source,
            )
            inventory.hold_inventory(reservation)
    except IntegrityError:
        if idempotency_key:
            existing = Reservation.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                return existing, False
        raise ReservationError("预订创建失败，请重试")
    return reservation, True


def confirm_reservation(reservation):
    """前台排房确认：把待支付预订的 hold 转为 sold（不走支付）。"""
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
        if reservation.status == Reservation.Status.CONFIRMED:
            return reservation
        if reservation.status != Reservation.Status.PENDING:
            raise ReservationError("当前状态不能确认预订")
        inventory.convert_hold_to_sold(reservation)
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=["status", "updated_at"])
    return reservation


def confirm_payment(*, booking_no, provider_ref, amount, provider="mock", raw_payload=None):
    """支付回调：幂等确认预订，hold 转 sold，并写统一账本。"""
    amount = ledger.money(amount)
    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(provider_ref=provider_ref).first()
        if payment and payment.status == Payment.Status.SUCCESS:
            return payment.reservation, payment, False
        if payment and payment.reservation.booking_no != booking_no:
            raise PaymentError("支付流水号已绑定其他预订")

        reservation = Reservation.objects.select_for_update().filter(
            booking_no=booking_no).first()
        if not reservation:
            raise PaymentError("预订不存在")
        if reservation.status == Reservation.Status.CONFIRMED:
            if payment:
                payment.status = Payment.Status.SUCCESS
                payment.paid_at = payment.paid_at or timezone.now()
                payment.save(update_fields=["status", "paid_at"])
            return reservation, payment, False
        if reservation.status != Reservation.Status.PENDING:
            raise PaymentError(f"预订状态为 {reservation.get_status_display()}，无法支付确认")
        if reservation.expires_at < timezone.now():
            raise PaymentError("预订已超过支付时限，请重新下单")
        if amount != reservation.total_amount:
            raise PaymentError("支付金额与预订金额不一致")

        if payment is None:
            payment = Payment.objects.create(
                reservation=reservation,
                provider=provider,
                provider_ref=provider_ref,
                amount=amount,
                status=Payment.Status.SUCCESS,
                raw_payload=raw_payload or {},
                paid_at=timezone.now(),
            )
        else:
            payment.status = Payment.Status.SUCCESS
            payment.amount = amount
            payment.raw_payload = raw_payload or payment.raw_payload
            payment.paid_at = timezone.now()
            payment.save(update_fields=["status", "amount", "raw_payload", "paid_at"])

        inventory.convert_hold_to_sold(reservation)
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=["status", "updated_at"])
        ledger.post_entry(
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=amount,
            description=f"预订 {reservation.booking_no} 支付",
            business_date=timezone.localdate(),
            operator=provider,
            reservation=reservation,
            source_type="payment",
            source_id=provider_ref,
            reference=f"payment:{provider_ref}",
        )
    return reservation, payment, True


def cancel_reservation(reservation, *, reason="取消预订"):
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
        if reservation.status == Reservation.Status.PENDING:
            inventory.release_hold(reservation)
        elif reservation.status == Reservation.Status.CONFIRMED:
            # 已确认订单取消：sold 释放回可售
            for day in inventory.stay_dates(reservation.check_in, reservation.check_out):
                from ..models import RoomTypeInventory
                row = RoomTypeInventory.objects.select_for_update().get(
                    room_type=reservation.room_type, stay_date=day)
                row.sold = max(0, row.sold - reservation.rooms)
                row.save(update_fields=["sold", "updated_at"])
        else:
            raise ReservationError("当前状态不能取消")
        reservation.status = Reservation.Status.CANCELLED
        reservation.save(update_fields=["status", "updated_at"])
    return reservation
