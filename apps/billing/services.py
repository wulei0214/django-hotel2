import hashlib
import hmac
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.inventory.services import InventoryUnavailable, confirm_inventory_for_reservation, release_inventory_hold
from apps.reservations.models import Reservation
from .models import LedgerEntry, Payment


class PaymentConflict(Exception):
    pass


def sign_mock_callback(raw_body):
    secret = getattr(settings, "PAYMENT_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("未配置 PAYMENT_WEBHOOK_SECRET。")
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_mock_callback(raw_body, signature):
    if not signature:
        return False
    try:
        expected = sign_mock_callback(raw_body)
    except RuntimeError:
        return False
    return hmac.compare_digest(expected, signature)


def create_mock_payment_intent(reservation):
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().select_related(
            "property"
        ).get(pk=reservation.pk)
        if reservation.status != Reservation.Status.PENDING:
            raise PaymentConflict("只有待支付预订可以创建支付模拟单。")
        if reservation.expires_at and reservation.expires_at <= timezone.now():
            raise PaymentConflict("预订已超时，请重新查询房态。")
        if reservation.total_amount <= Decimal("0"):
            raise PaymentConflict("当前预订金额必须大于 0 才能模拟支付。")

        pending_payment = (
            Payment.objects.filter(
                reservation=reservation,
                status=Payment.Status.PENDING,
            )
            .order_by("-created_at")
            .first()
        )
        if pending_payment is not None:
            return pending_payment

        payment = Payment.objects.create(
            property=reservation.property,
            reservation=reservation,
            folio=reservation.get_master_folio(),
            provider="mock",
            provider_ref="MOCK-" + uuid.uuid4().hex.upper(),
            method="MOCK",
            amount=reservation.total_amount,
            currency=reservation.currency,
            status=Payment.Status.PENDING,
        )
        return payment


@transaction.atomic
def process_mock_payment_callback(*, provider_ref, amount, status):
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaymentConflict("支付金额格式不正确。") from exc
    if status not in {"SUCCESS", "FAILED"}:
        raise PaymentConflict("不支持的模拟支付结果。")

    try:
        payment = (
            Payment.objects.select_for_update()
            .select_related("reservation", "folio", "property")
            .get(provider_ref=provider_ref)
        )
    except Payment.DoesNotExist as exc:
        raise PaymentConflict("支付流水不存在。") from exc

    if amount != payment.amount:
        raise PaymentConflict("回调金额与支付流水金额不一致。")
    if payment.status == Payment.Status.SUCCEEDED:
        return {"duplicate": True, "payment": payment, "reservation": payment.reservation}
    if payment.status != Payment.Status.PENDING:
        raise PaymentConflict("当前支付流水不处于待支付状态。")

    reservation = Reservation.objects.select_for_update().get(
        pk=payment.reservation_id
    )
    if status == "FAILED":
        payment.status = Payment.Status.FAILED
        payment.save(update_fields=("status", "updated_at"))
        return {"duplicate": False, "payment": payment, "reservation": reservation}

    now = timezone.now()

    # 外部渠道已确认 SUCCESS 后，Payment 是不可逆的资金事实。
    # 即使订单已过期/取消，也不得把成功支付改写为 FAILED。
    payment.status = Payment.Status.SUCCEEDED
    payment.paid_at = now
    payment.save(update_fields=("status", "paid_at", "updated_at"))

    expired = bool(reservation.expires_at and reservation.expires_at <= now)
    reservation_confirmed = False
    compensation_required = False

    if reservation.status == Reservation.Status.PENDING:
        try:
            # 过期边界先尝试使用仍有效的 Hold 完成确认；若 Hold 已释放，
            # 不得抢占其他订单库存，保留成功支付事实并标记待补偿。
            confirm_inventory_for_reservation(reservation)
            reservation.status = Reservation.Status.CONFIRMED
            reservation.save(update_fields=("status", "updated_at"))
            reservation_confirmed = True
        except InventoryUnavailable:
            if not expired:
                raise PaymentConflict("支付成功但库存 Hold 已失效，需进入支付补偿处理。")
            reservation.status = Reservation.Status.EXPIRED
            reservation.save(update_fields=("status", "updated_at"))
            compensation_required = True
    else:
        # 取消/过期等终态订单不再恢复库存，成功支付进入补偿处理。
        compensation_required = True

    from apps.billing.services_v2 import BillingService
    BillingService.post_entry(
        property_obj=payment.property,
        folio=payment.folio,
        entry_type=LedgerEntry.EntryType.PAYMENT,
        amount=-payment.amount,
        currency=payment.currency,
        business_date=BillingService._business_date(payment.property),
        reference=f"payment:{payment.provider_ref}",
        description="渠道支付成功回调入账",
        posted_by=None,
    )
    return {
        "duplicate": False,
        "expired": expired,
        "compensation_required": compensation_required,
        "reservation_confirmed": reservation_confirmed,
        "payment": payment,
        "reservation": reservation,
    }



@transaction.atomic
def reverse_ledger_entry(*, entry_id, reason, posted_by=None):
    reason = str(reason or "").strip()
    if not reason:
        raise PaymentConflict("红字冲销必须填写原因。")
    if len(reason) > 240:
        raise PaymentConflict("冲销原因不能超过 240 个字符。")

    original = LedgerEntry.objects.select_for_update().get(pk=entry_id)
    existing = LedgerEntry.objects.filter(
        reference=f"reversal:{original.reference}"
    ).first()
    if existing:
        return existing, False
    if original.entry_type == LedgerEntry.EntryType.REVERSAL:
        raise PaymentConflict("不能直接冲销一条红字凭证。")

    reversal = LedgerEntry.objects.create(
        property=original.property,
        folio=original.folio,
        stay=original.stay,
        entry_type=LedgerEntry.EntryType.REVERSAL,
        amount=-original.amount,
        currency=original.currency,
        business_date=timezone.localdate(),
        reference=f"reversal:{original.reference}",
        description=f"冲销：{original.description}"[:240],
        posted_by=posted_by,
        void_of=original,
        void_reason=reason,
    )
    return reversal, True
