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
    def _business_date(property_obj):
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        return timezone.localdate(timezone.now(), tz)

    @staticmethod
    @transaction.atomic
    def post_entry(*, property_obj, folio, entry_type, amount, reference,
                   description="", posted_by=None, business_date=None,
                   currency=None, stay=None, void_of=None, void_reason=""):
        """账本唯一写入口：追加、强幂等；冲销只能追加等额反向凭证。"""
        from apps.billing.models import LedgerEntry
        amount = BillingService._money(amount)
        reference = str(reference or "").strip()
        if not reference or len(reference) > 160:
            raise ValueError("账本凭证号必须为 1 至 160 个字符。")
        if amount == 0:
            raise ValueError("账本凭证金额不能为零。")
        if folio.property_id != property_obj.pk:
            raise ValueError("账单不属于当前物业。")
        if stay is not None and stay.property_id != property_obj.pk:
            raise ValueError("入住记录不属于当前物业。")
        currency = currency or folio.currency
        business_date = business_date or BillingService._business_date(property_obj)
        existing = LedgerEntry.objects.filter(reference=reference).first()
        if existing is not None:
            same = (
                existing.property_id == property_obj.pk
                and existing.folio_id == folio.pk
                and existing.stay_id == (stay.pk if stay else None)
                and existing.entry_type == entry_type
                and existing.amount == amount
                and existing.currency == currency
                and existing.business_date == business_date
                and existing.void_of_id == (void_of.pk if void_of else None)
                and existing.void_reason == str(void_reason or "").strip()
            )
            if not same:
                raise ValueError("账本凭证号已用于不同账务事实，拒绝覆盖。")
            return existing, False
        if entry_type == LedgerEntry.EntryType.REVERSAL:
            reason = str(void_reason or "").strip()
            if void_of is None or not reason:
                raise ValueError("红字冲销必须关联原凭证并填写原因。")
            if void_of.property_id != property_obj.pk or void_of.folio_id != folio.pk:
                raise ValueError("冲销原凭证必须属于同一物业和账单。")
            if amount != -void_of.amount:
                raise ValueError("红字冲销金额必须与原凭证等额反向。")
            if void_of.reversals.exists():
                raise ValueError("原凭证已经冲销，不能重复冲销。")
        elif void_of is not None or str(void_reason or "").strip():
            raise ValueError("只有红字冲销凭证可以关联 void_of 或冲销原因。")
        entry = LedgerEntry.objects.create(
            property=property_obj, folio=folio, stay=stay,
            entry_type=entry_type, amount=amount, currency=currency,
            business_date=business_date, reference=reference,
            description=str(description or "")[:240],
            posted_by=posted_by, void_of=void_of,
            void_reason=str(void_reason or "").strip())
        AuditEvent.record(
            action="billing.ledger_post", entity_type="LedgerEntry",
            entity_id=entry.pk, actor=posted_by, property_obj=property_obj,
            after={"reference": reference, "entry_type": entry_type,
                   "amount": str(amount), "folio_id": folio.pk},
            reason=void_reason or "")
        return entry, True

    @staticmethod
    @transaction.atomic
    def collect_payment(*, property_obj, reservation, amount, method,
                        collected_by, reference=""):
        """统一收款（Q-07）：记录支付方式，创建 Payment + LedgerEntry。"""
        from apps.billing.models import Folio, LedgerEntry, Payment
        method = "TRANSFER" if method == "BANK_TRANSFER" else method
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
                    or existing.amount != amount or existing.method != method
                    or existing.provider != "frontdesk_manual"
                    or existing.status != Payment.Status.SUCCEEDED):
                raise ValueError("该收款参考号已用于不同支付。")
            return existing
        from apps.reservations.models import ReservationRoom
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
        BillingService.post_entry(
            property_obj=property_obj, folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=-amount, currency=reservation.currency,
            business_date=BillingService._business_date(property_obj),
            reference=f"payment:{provider_ref}",
            description=f"前台登记收款（{method}）",
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
    def record_deposit(*, property_obj, reservation, amount, method,
                       idempotency_key, received_by=None):
        """统一登记实际收到的押金；Payment 与 DEPOSIT 凭证原子提交。"""
        from apps.billing.models import Folio, Payment, LedgerEntry
        method = str(method or "").strip().upper()
        if method == "BANK_TRANSFER":
            method = "TRANSFER"
        if method not in BillingService.VALID_PAYMENT_METHODS:
            raise ValueError("押金方式只支持现金、刷卡或转账。")
        amount = BillingService._money(amount)
        if amount <= 0:
            raise ValueError("押金金额必须大于零。")
        if reservation.property_id != property_obj.pk:
            raise ValueError("预订不属于当前物业。")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 64:
            raise ValueError("押金登记必须提供 1 至 64 个字符的幂等键。")
        if reservation.status not in ("CONFIRMED", "CHECKED_IN"):
            raise ValueError("只有已确认或在住预订可以收取押金。")
        folio = Folio.objects.select_for_update().filter(
            property=property_obj, reservation=reservation,
            status=Folio.Status.OPEN).first()
        if folio is None:
            raise ValueError("有效主账单不存在。")
        provider_ref = f"FDDEP-{property_obj.pk}-{key}"
        existing = Payment.objects.filter(provider_ref=provider_ref).first()
        if existing is not None:
            if (existing.property_id != property_obj.pk
                    or existing.reservation_id != reservation.pk
                    or existing.amount != amount or existing.method != method
                    or existing.provider != "frontdesk_deposit"
                    or existing.status != Payment.Status.SUCCEEDED):
                raise ValueError("该幂等键已用于其他押金登记。")
            return existing, False
        if reservation.deposit_amount + amount > reservation.total_amount:
            raise ValueError("累计押金不能超过当前预订房费总额。")
        payment = Payment.objects.create(
            property=property_obj, reservation=reservation, folio=folio,
            provider="frontdesk_deposit", provider_ref=provider_ref,
            method=method, amount=amount, currency=folio.currency,
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        BillingService.post_entry(
            property_obj=property_obj, folio=folio,
            entry_type=LedgerEntry.EntryType.DEPOSIT, amount=-amount,
            currency=folio.currency,
            business_date=BillingService._business_date(property_obj),
            reference=f"deposit:{provider_ref}",
            description=f"前台登记押金（{method}）", posted_by=received_by)
        reservation.deposit_amount += amount
        reservation.save(update_fields=("deposit_amount", "updated_at"))
        AuditEvent.record(
            action="billing.deposit_collect", entity_type="Payment",
            entity_id=payment.pk, actor=received_by, property_obj=property_obj,
            after={"method": method, "amount": str(amount)},
            reason="前台实际收取押金")
        return payment, True

    @staticmethod
    @transaction.atomic
    def request_refund(*, property_obj, payment_id, amount, reason,
                       requested_by, idempotency_key="", request=None):
        """退款申请唯一入口：校验金额、幂等并建立双人审批记录。"""
        from apps.billing.models import Payment, Refund
        amount = BillingService._money(amount)
        if amount <= 0:
            raise ValueError("退款金额必须大于零。")
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise ValueError("退款申请原因必须填写，且不能超过 240 个字符。")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 64:
            raise ValueError("退款申请必须提供 1 至 64 个字符的幂等键。")
        payment = Payment.objects.select_for_update().filter(
            pk=payment_id, property=property_obj).first()
        if payment is None:
            raise ValueError("未找到支付流水。")
        existing = Refund.objects.filter(
            property=property_obj, idempotency_key=key).first()
        if existing is not None:
            if (existing.payment_id != payment.pk
                    or existing.amount != amount or existing.reason != reason):
                raise ValueError("该幂等键已用于不同退款申请。")
            return existing, False
        if payment.status not in (
                Payment.Status.SUCCEEDED, Payment.Status.PARTIALLY_REFUNDED):
            raise ValueError("只有成功或部分退款的支付可申请退款。")
        reserved = Refund.objects.filter(
            payment=payment, status__in=(Refund.Status.PENDING, Refund.Status.SUCCEEDED)
        ).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")
        if reserved + amount > payment.amount:
            raise ValueError("累计退款申请及已完成退款不能超过原支付金额。")
        refund = Refund.objects.create(
            property=property_obj, payment=payment, amount=amount,
            currency=payment.currency, reason=reason,
            status=Refund.Status.PENDING, requested_by=requested_by,
            provider_ref=f"RFD-{uuid.uuid4().hex.upper()[:16]}",
            idempotency_key=key)
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
            reason=reason, request=request)
        return refund, True

    @staticmethod
    @transaction.atomic
    def complete_refund(*, property_obj, refund_id, approved_by,
                        confirm_refund_paid=False):
        """第二人确认线下退款已实际支付后，追加 REFUND 凭证并更新支付状态。"""
        from apps.billing.models import Payment, Refund, LedgerEntry
        if confirm_refund_paid is not True:
            raise ValueError("必须确认退款已经实际支付给客人。")
        refund = Refund.objects.select_for_update().filter(
            pk=refund_id, property=property_obj).first()
        if refund is None:
            raise ValueError("退款申请不存在或不属于当前物业。")
        if refund.status == Refund.Status.SUCCEEDED:
            return refund, False
        if refund.status != Refund.Status.PENDING:
            raise ValueError("只有待审批退款申请可以执行。")
        if refund.requested_by_id == approved_by.pk:
            raise ValueError("退款申请人与审批人不能是同一账号。")
        approval = refund.approval_record
        if approval is None:
            approval, _ = ApprovalService.request_approval(
                property_obj=property_obj, action_code="billing.refund",
                entity_type="Refund", entity_id=refund.pk,
                requested_by=refund.requested_by, reason=refund.reason,
                payload={"payment_id": refund.payment_id,
                         "amount": str(refund.amount),
                         "currency": refund.currency})
            refund.approval_record = approval
            refund.save(update_fields=("approval_record",))
        if approval.status == ApprovalRecord.Status.PENDING:
            approval = ApprovalService.approve(
                approval.pk, approved_by=approved_by, property_obj=property_obj)
        if (approval.status != ApprovalRecord.Status.APPROVED
                or approval.approved_by_id != approved_by.pk):
            raise ValueError("退款申请没有由当前账号完成有效审批。")
        payment = Payment.objects.select_for_update().get(
            pk=refund.payment_id, property=property_obj)
        successful_total = Refund.objects.filter(
            payment=payment, status=Refund.Status.SUCCEEDED
        ).exclude(pk=refund.pk).aggregate(
            total=models.Sum("amount"))["total"] or Decimal("0.00")
        if successful_total + refund.amount > payment.amount:
            raise ValueError("原支付可退款余额不足。")
        folio = refund.payment.folio
        if folio.property_id != property_obj.pk or folio.status == "VOIDED":
            raise ValueError("原支付对应账单不存在或已作废。")
        BillingService.post_entry(
            property_obj=property_obj, folio=folio,
            entry_type=LedgerEntry.EntryType.REFUND,
            amount=refund.amount, currency=refund.currency,
            business_date=BillingService._business_date(property_obj),
            reference=f"refund:{refund.provider_ref}",
            description=f"人工退款完成：{refund.reason}",
            posted_by=approved_by)
        refund.status = Refund.Status.SUCCEEDED
        refund.approved_by = approved_by
        refund.processed_at = timezone.now()
        refund.save(update_fields=("status", "approved_by", "processed_at"))
        refunded_total = Refund.objects.filter(
            payment=payment, status=Refund.Status.SUCCEEDED
        ).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")
        payment.status = (Payment.Status.REFUNDED if refunded_total >= payment.amount
                          else Payment.Status.PARTIALLY_REFUNDED)
        payment.save(update_fields=("status", "updated_at"))
        if payment.provider == "frontdesk_deposit":
            reservation = payment.reservation
            reservation.deposit_amount = max(
                Decimal("0.00"), reservation.deposit_amount - refund.amount)
            reservation.save(update_fields=("deposit_amount", "updated_at"))
        AuditEvent.record(
            action="billing.refund_completed", entity_type="Refund",
            entity_id=refund.pk, actor=approved_by, property_obj=property_obj,
            after={"amount": str(refund.amount), "status": refund.status},
            reason=refund.reason)
        return refund, True
