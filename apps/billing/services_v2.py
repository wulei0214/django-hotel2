# -*- coding: utf-8 -*-
"""V1.2 §15.3 ApprovalRecord（Q-05/Q-06/Q-14 P0）+ §15.4 前台操作规则服务。

ApprovalRecord 是审批事实的持久化载体；同一审批只能执行一次。
BillingService 统一收款/退款/冲销/转账写入口（V1.1 §6.3）。
"""
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.core.models import AuditEvent, Property


from apps.billing.models import ApprovalRecord

class ApprovalService:
    """审批流程唯一写入口（Q-05/Q-06/Q-14 SoD 强制）。"""

    @staticmethod
    @transaction.atomic
    def request_approval(*, property_obj, action_code, entity_type, entity_id,
                         requested_by, reason, payload=None, expires_hours=48):
        """发起审批申请；幂等（同 key 返回已有记录）。"""
        import hashlib
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise ValueError("高危操作必须填写 1 至 240 个字符的原因。")
        if not action_code or not entity_type or entity_id in (None, ""):
            raise ValueError("审批动作和目标对象不能为空。")
        if expires_hours <= 0 or expires_hours > 168:
            raise ValueError("审批有效期必须在 1 至 168 小时之间。")
        raw_key = f"{property_obj.pk}:{action_code}:{entity_type}:{entity_id}:{requested_by.pk}"
        idem_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        existing = ApprovalRecord.objects.filter(idempotency_key=idem_key).first()
        if existing:
            return existing, False
        record = ApprovalRecord.objects.create(
            property=property_obj, action_code=action_code,
            entity_type=entity_type, entity_id=str(entity_id),
            requested_by=requested_by, reason=reason,
            payload_snapshot=payload, idempotency_key=idem_key,
            expires_at=now_plus(hours=expires_hours))
        AuditEvent.record(
            action="approval.request", entity_type=entity_type,
            entity_id=str(entity_id), actor=requested_by,
            property_obj=property_obj,
            after={"action_code": action_code, "status": "PENDING"},
            reason=reason[:240], request=getattr(requested_by, "_request", None))
        return record, True

    @staticmethod
    @transaction.atomic
    def approve(record_id, *, approved_by, property_obj):
        """审批通过：申请人≠审批人（SoD Q-05）；写审计。"""
        record = ApprovalRecord.objects.select_for_update().filter(
            pk=record_id, property=property_obj).first()
        if record is None:
            raise ValueError("审批记录不存在或不属于当前物业。")
        if record.requested_by_id == approved_by.pk:
            raise ValueError("申请人不能审批自己的申请（Q-05 SoD）。")
        if record.status != ApprovalRecord.Status.PENDING:
            raise ValueError(f"审批状态为 {record.status}，不可审批。")
        if record.expires_at and record.expires_at < timezone.now():
            record.status = ApprovalRecord.Status.EXPIRED
            record.approved_at = timezone.now()
            record.save(update_fields=("status", "approved_at"))
            AuditEvent.record(
                action="approval.expire", entity_type=record.entity_type,
                entity_id=record.entity_id, actor=approved_by,
                property_obj=property_obj,
                after={"action_code": record.action_code, "status": "EXPIRED"},
                reason=record.reason)
            return record
        record.status = ApprovalRecord.Status.APPROVED
        record.approved_by = approved_by
        record.approved_at = timezone.now()
        record.save(update_fields=("status", "approved_by", "approved_at"))
        AuditEvent.record(
            action="approval.approve", entity_type=record.entity_type,
            entity_id=record.entity_id, actor=approved_by,
            property_obj=property_obj,
            after={"action_code": record.action_code, "status": "APPROVED"},
            reason=record.reason)
        return record

    @staticmethod
    @transaction.atomic
    def reject(record_id, *, approved_by, property_obj, reason=""):
        """审批驳回。"""
        record = ApprovalRecord.objects.select_for_update().filter(
            pk=record_id, property=property_obj).first()
        if record is None:
            raise ValueError("审批记录不存在或不属于当前物业。")
        if record.requested_by_id == approved_by.pk:
            raise ValueError("申请人不能审批自己的申请。")
        if record.status != ApprovalRecord.Status.PENDING:
            raise ValueError(f"审批状态为 {record.status}，不可驳回。")
        record.status = ApprovalRecord.Status.REJECTED
        record.approved_by = approved_by
        record.approved_at = timezone.now()
        record.save(update_fields=("status", "approved_by", "approved_at"))
        AuditEvent.record(
            action="approval.reject", entity_type=record.entity_type,
            entity_id=record.entity_id, actor=approved_by,
            property_obj=property_obj, reason=reason or "驳回",
            after={"action_code": record.action_code, "status": "REJECTED"})
        return record


def now_plus(**kwargs):
    from datetime import timedelta
    return timezone.now() + timedelta(**kwargs)


class BillingService:
    """统一账务服务（V1.1 §6.3）：收款/退款/冲销唯一写入口。

    规则（Q-07/Q-08/V1.2 §15.4）：
      * 收款记录支付方式（现金/刷卡/转账）；
      * 退款须 ApprovalRecord APPROVED + 申请人≠审批人；
      * 冲销只追加红字，不修改原凭证；
      * 金额 Decimal，不接受 float。
    """
    VALID_PAYMENT_METHODS = ("CASH", "CARD", "TRANSFER")
    CENT = Decimal("0.01")

    @staticmethod
    def _money(value):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("金额格式无效。") from exc
        if not amount.is_finite() or amount != amount.quantize(BillingService.CENT):
            raise ValueError("金额必须精确到分。")
        return amount

    @staticmethod
    @transaction.atomic
    def collect_payment(*, property_obj, reservation, amount, method,
                        collected_by, reference=""):
        """统一收款（Q-07）：记录支付方式，创建 Payment + LedgerEntry。"""
        from apps.billing.models import Folio, LedgerEntry, Payment
        if method not in BillingService.VALID_PAYMENT_METHODS:
            raise ValueError(f"支付方式必须是 {'、'.join(BillingService.VALID_PAYMENT_METHODS)}")
        amount = BillingService._money(amount)
        if amount <= 0:
            raise ValueError("收款金额必须大于 0。")
        if reservation.property_id != property_obj.pk:
            raise ValueError("预订不属于当前物业。")
        folio = Folio.objects.select_for_update().filter(
            property=property_obj, reservation=reservation,
            folio_type="MASTER", status="OPEN").first()
        if folio is None:
            raise ValueError("主账单不存在或已关闭。")
        provider_ref = str(reference or "").strip() or f"FD-{uuid.uuid4().hex.upper()}"
        if len(provider_ref) > 80:
            raise ValueError("收款参考号不能超过 80 个字符。")
        existing = Payment.objects.filter(provider_ref=provider_ref).first()
        if existing:
            if (existing.property_id != property_obj.pk
                    or existing.reservation_id != reservation.pk
                    or existing.amount != amount or existing.method != method):
                raise ValueError("该收款参考号已用于不同支付。")
            return existing
        from apps.billing.models import ReservationRoom
        entries = list(folio.entries.values_list("amount", flat=True))
        balance = sum(entries, start=Decimal("0.00"))
        expected = sum(
            ReservationRoom.objects.filter(
                property=property_obj, reservation=reservation
            ).values_list("amount", flat=True),
            start=Decimal("0.00"))
        posted_room = sum(
            (entry.amount for entry in folio.entries.filter(
                entry_type=LedgerEntry.EntryType.ROOM_CHARGE)),
            start=Decimal("0.00"))
        outstanding = balance + expected - posted_room
        if amount > outstanding:
            raise ValueError(f"收款金额超过当前应收余额 {outstanding:.2f}。")
        now = timezone.now()
        payment = Payment.objects.create(
            property=property_obj, reservation=reservation, folio=folio,
            provider="frontdesk_manual", provider_ref=provider_ref,
            method=method, amount=amount, currency=reservation.currency,
            status=Payment.Status.SUCCEEDED, paid_at=now,
        )
        LedgerEntry.objects.create(
            property=property_obj, folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=-amount, currency=reservation.currency,
            business_date=timezone.localdate(),
            reference=f"pay:{payment.pk}",
            description=f"前台收款（{method}）",
            posted_by=collected_by)
        AuditEvent.record(
            action="billing.collect", entity_type="Payment",
            entity_id=payment.pk, actor=collected_by,
            property_obj=property_obj,
            after={"method": method, "amount": str(amount)},
            request=getattr(collected_by, "_request", None))
        return payment

    @staticmethod
    @transaction.atomic
    def request_refund(*, property_obj, payment_id, amount, reason,
                       requested_by, request=None):
        """退款申请（Q-05）：创建 ApprovalRecord + Refund PENDING。"""
        from apps.billing.models import LedgerEntry, Payment, Refund
        amount = BillingService._money(amount)
        payment = Payment.objects.select_for_update().filter(
            pk=payment_id, property=property_obj).first()
        if payment is None:
            raise ValueError("未找到支付流水。")
        if payment.status not in (
                Payment.Status.SUCCEEDED, Payment.Status.PARTIALLY_REFUNDED):
            raise ValueError("只有成功或部分退款的支付可申请退款。")
        refunded = Refund.objects.filter(
            payment=payment, status=Refund.Status.SUCCEEDED).aggregate(
            total=models.Sum("amount"))["total"] or Decimal("0.00")
        pending = Refund.objects.filter(
            payment=payment, status=Refund.Status.PENDING).aggregate(
            total=models.Sum("amount"))["total"] or Decimal("0.00")
        if refunded + pending + amount > payment.amount:
            raise ValueError("累计退款不能超过原支付金额。")
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise ValueError("退款申请原因必须填写，且不能超过 240 个字符。")
        refund = Refund.objects.create(
            property=property_obj, payment=payment, amount=amount,
            currency=payment.currency, reason=reason,
            status=Refund.Status.PENDING, requested_by=requested_by,
            provider_ref=f"RFD-{uuid.uuid4().hex.upper()[:16]}",
            idempotency_key=f"refund:{payment.pk}:{uuid.uuid4().hex[:12]}")
        approval, _ = ApprovalService.request_approval(
            property_obj=property_obj, action_code="billing.refund",
            entity_type="Refund", entity_id=refund.pk,
            requested_by=requested_by, reason=reason,
            payload={"payment_id": payment.pk, "amount": str(amount),
                     "currency": payment.currency})
        refund.approval_record = approval
        refund.save(update_fields=("approval_record",))
        AuditEvent.record(
            action="billing.refund_request", entity_type="Refund",
            entity_id=refund.pk, actor=requested_by, property_obj=property_obj,
            after={"amount": str(amount), "payment_id": payment.pk},
            reason=reason[:240], request=request)
        return refund
