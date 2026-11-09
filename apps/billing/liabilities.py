"""待退款负债登记服务。只登记负债，不创建退款申请或执行退款。"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum

from apps.billing.models import LedgerEntry, Payment, Refund, RefundLiability
from apps.core.models import AuditEvent


@transaction.atomic
def register_refund_liability(*, property_obj, reservation, folio, amount,
                              created_by=None):
    """将最后退房时的负余额登记为待退款负债，并关联候选原支付流水。

    source_payments 仅是财务核对候选集合，不分配退款金额。具体一笔/多笔
    Payment 的金额分配须遵循后续 DEC-02 参数，不能在此处按顺序自动分配。
    """
    try:
        liability_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception as exc:
        raise ValueError("待退款负债金额无效。") from exc
    if not liability_amount.is_finite() or liability_amount <= 0:
        raise ValueError("待退款负债金额必须大于零。")
    # 先锁 Reservation，再锁 Folio，最后锁 Payment；退房登记与财务退款
    # 使用一致的父记录优先顺序，MySQL 下同一账单的重复/并发登记会串行化。
    from apps.reservations.models import Reservation
    from apps.billing.models import Folio
    reservation = Reservation.objects.select_for_update().select_related(
        "property").get(pk=reservation.pk)
    folio = Folio.objects.select_for_update().get(pk=folio.pk)
    if (reservation.property_id != property_obj.pk
            or folio.property_id != property_obj.pk
            or folio.reservation_id != reservation.pk):
        raise ValueError("待退款负债、预订与账单必须属于当前物业和同一预订。")
    if folio.currency != reservation.currency:
        raise ValueError("待退款负债币种必须与预订和账单一致。")

    payment_candidates = Payment.objects.select_for_update().filter(
        property=property_obj, reservation=reservation, folio=folio,
        status__in=(
            Payment.Status.SUCCEEDED,
            Payment.Status.REFUND_PENDING,
            Payment.Status.PARTIALLY_REFUNDED,
        ),
    ).order_by("pk")
    # 只有同时存在对应收款账本凭证的支付，才能成为负债的原支付候选。
    # 普通收款使用 PAYMENT/payment:；前台押金使用 DEPOSIT/deposit:。
    # 两类都属于真实资金来源，不能因凭证类型不同而漏掉押金退款。
    candidates = []
    for payment in payment_candidates:
        if payment.provider == "frontdesk_deposit":
            entry_type = LedgerEntry.EntryType.DEPOSIT
            reference = f"deposit:{payment.provider_ref}"
        else:
            entry_type = LedgerEntry.EntryType.PAYMENT
            reference = f"payment:{payment.provider_ref}"
        if LedgerEntry.objects.filter(
            property=property_obj, folio=folio,
            entry_type=entry_type,
            reference=reference,
        ).exists():
            candidates.append(payment)
    if not candidates:
        raise ValueError("存在负余额，但未找到同时具有支付流水和对应收款账本凭证的原支付，不能登记待退款负债。")

    available_total = Decimal("0.00")
    for payment in candidates:
        reserved = Refund.objects.filter(
            payment=payment,
            status__in=(Refund.Status.PENDING, Refund.Status.APPROVED,
                        Refund.Status.SUCCEEDED),
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        available_total += max(payment.amount - reserved, Decimal("0.00"))
    if liability_amount > available_total:
        raise ValueError("待退款负债超过候选原支付的可退款余额，需先核对账务来源。")

    key = f"refund-liability:folio:{folio.pk}"
    existing = RefundLiability.objects.filter(folio=folio).first()
    if existing:
        if (existing.amount != liability_amount
                or existing.reservation_id != reservation.pk
                or existing.property_id != property_obj.pk):
            raise ValueError("该账单已登记不同金额的待退款负债，禁止覆盖原始负债事实。")
        existing.source_payments.add(*candidates)
        return existing, False

    try:
        liability = RefundLiability.objects.create(
            property=property_obj,
            reservation=reservation,
            folio=folio,
            amount=liability_amount,
            remaining_amount=liability_amount,
            currency=folio.currency,
            status=RefundLiability.Status.OPEN,
            idempotency_key=key,
            created_by=created_by,
        )
    except IntegrityError:
        liability = RefundLiability.objects.select_for_update().get(folio=folio)
        if liability.amount != liability_amount:
            raise ValueError("并发请求已登记不同金额的待退款负债。")
        liability.source_payments.add(*candidates)
        return liability, False

    liability.source_payments.set(candidates)
    AuditEvent.record(
        action="billing.refund_liability_registered",
        entity_type="RefundLiability",
        entity_id=liability.pk,
        actor=created_by,
        property_obj=property_obj,
        after={
            "reservation_id": reservation.pk,
            "folio_id": folio.pk,
            "amount": str(liability.amount),
            "currency": liability.currency,
            "candidate_payment_ids": [payment.pk for payment in candidates],
        },
        reason="最后一间房退房时登记待退款负债；财务后续主动发起退款",
    )
    return liability, True
