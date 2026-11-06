# -*- coding: utf-8 -*-
"""V1.2 §15.3 ApprovalRecord（Q-05/Q-06/Q-14 P0）+ §15.4 前台操作规则服务。

ApprovalRecord 是审批事实的持久化载体；同一审批只能执行一次。
BillingService 统一收款/退款/冲销/转账写入口（V1.1 §6.3）。
"""
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.core.models import AuditEvent, Property


from apps.billing.models import ApprovalRecord

class ApprovalService:
    """审批流程唯一写入口（Q-05/Q-06/Q-14 SoD 强制；P0-02 白名单双层校验）。"""

    @staticmethod
    @transaction.atomic
    def request_approval(*, property_obj, action_code, entity_type, entity_id,
                         requested_by, reason, payload=None, expires_hours=48):
        """发起审批申请；幂等（同 key 返回已有记录）。

        P0-02：action_code 必须已注册、entity_type 必须与登记项一致、
        payload 必须通过字段白名单/长度/敏感值校验；任一不满足直接拒绝，
        不写 ApprovalRecord、不写 AuditEvent。
        """
        import hashlib
        from apps.billing.approval_registry import ApprovalActionRegistry
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise ValueError("高危操作必须填写 1 至 240 个字符的原因。")
        if not action_code or not entity_type or entity_id in (None, ""):
            raise ValueError("审批动作和目标对象不能为空。")
        if expires_hours <= 0 or expires_hours > 168:
            raise ValueError("审批有效期必须在 1 至 168 小时之间。")
        action_code = str(action_code).strip()
        entity_type = str(entity_type).strip()
        spec = ApprovalActionRegistry.get(action_code)
        if spec is None:
            raise ValueError(f"未知审批动作代码：{action_code}。")
        ApprovalActionRegistry.validate_entity_type(spec, entity_type)
        payload = ApprovalActionRegistry.validate_payload(spec, payload)
        if len(str(entity_id)) > 64:
            raise ValueError("目标对象 ID 不能超过 64 个字符。")
        raw_key = f"{property_obj.pk}:{action_code}:{entity_type}:{entity_id}:{requested_by.pk}"
        idem_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        existing = ApprovalRecord.objects.filter(idempotency_key=idem_key).first()
        if existing:
            return existing, False
        # WB-03：并发重复申请可能同时通过预检查询，由 idempotency_key 唯一约束
        # 兜底仲裁；撞键方必须幂等返回既有记录而不是抛 IntegrityError。
        try:
            with transaction.atomic():
                record = ApprovalRecord.objects.create(
                    property=property_obj, action_code=action_code,
                    entity_type=entity_type, entity_id=str(entity_id),
                    requested_by=requested_by, reason=reason,
                    payload_snapshot=payload, idempotency_key=idem_key,
                    expires_at=now_plus(hours=expires_hours))
        except IntegrityError:
            raced = ApprovalRecord.objects.filter(idempotency_key=idem_key).first()
            if raced is not None:
                return raced, False
            raise
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


def _invalidate_open_summary_cache(property_id):
    """账本/账单状态提交后失效物业未结余额摘要，避免 60 秒旧余额窗口。"""
    from django.core.cache import cache
    import logging

    key = "billing:open-summary:%s" % property_id

    def invalidate():
        try:
            cache.delete(key)
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to invalidate billing summary cache for property %s",
                property_id)

    transaction.on_commit(invalidate)


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
        if not amount.is_finite():
            raise ValueError("金额必须精确到分。")
        try:
            exact_to_cent = amount == amount.quantize(BillingService.CENT)
        except InvalidOperation as exc:
            # 超出 Decimal 上下文精度的超大值（如 1e30）quantize 会抛
            # InvalidOperation；必须统一转成面向调用方的 ValueError（WB-01）。
            raise ValueError("金额必须精确到分。") from exc
        if not exact_to_cent:
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
    def _entry_facts_match(entry, *, property_obj, folio, stay, entry_type,
                           amount, currency, business_date, void_of,
                           void_reason):
        """判断既有凭证与本请求是否为完全相同的账务事实（幂等重放判定）。"""
        return (
            entry.property_id == property_obj.pk
            and entry.folio_id == folio.pk
            and entry.stay_id == (stay.pk if stay else None)
            and entry.entry_type == entry_type
            and entry.amount == amount
            and entry.currency == currency
            and entry.business_date == business_date
            and entry.void_of_id == (void_of.pk if void_of else None)
            and entry.void_reason == str(void_reason or "").strip()
        )

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
        # WB-08：账单余额/应收按条目直接求和，混币入账会让不同币种金额相加；
        # 凭证币种必须与所属账单币种一致。
        if currency != folio.currency:
            raise ValueError("凭证币种必须与账单币种一致，不能混币记账。")
        business_date = business_date or BillingService._business_date(property_obj)
        facts = dict(
            property_obj=property_obj, folio=folio, stay=stay,
            entry_type=entry_type, amount=amount, currency=currency,
            business_date=business_date, void_of=void_of,
            void_reason=void_reason)
        existing = LedgerEntry.objects.filter(reference=reference).first()
        if existing is not None:
            if not BillingService._entry_facts_match(existing, **facts):
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
        # WB-04：并发同凭证号可能同时通过预检查询，由 reference 唯一约束兜底；
        # 撞键方按幂等口径返回既有凭证。
        try:
            with transaction.atomic():
                entry = LedgerEntry.objects.create(
                    property=property_obj, folio=folio, stay=stay,
                    entry_type=entry_type, amount=amount, currency=currency,
                    business_date=business_date, reference=reference,
                    description=str(description or "")[:240],
                    posted_by=posted_by, void_of=void_of,
                    void_reason=str(void_reason or "").strip())
        except IntegrityError:
            raced = LedgerEntry.objects.filter(reference=reference).first()
            if raced is not None and BillingService._entry_facts_match(raced, **facts):
                return raced, False
            raise
        AuditEvent.record(
            action="billing.ledger_post", entity_type="LedgerEntry",
            entity_id=entry.pk, actor=posted_by, property_obj=property_obj,
            after={"reference": reference, "entry_type": entry_type,
                   "amount": str(amount), "folio_id": folio.pk},
            reason=void_reason or "")
        _invalidate_open_summary_cache(property_obj.pk)
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
        cashier_shift = None
        if method == "CASH":
            from apps.billing.models import CashierShift
            cashier_shift = CashierShift.objects.select_for_update().filter(
                property=property_obj, cashier=collected_by,
                status=CashierShift.ShiftStatus.OPEN).first()
            if cashier_shift is None:
                raise ValueError("现金收款前必须先开立本人收银班次。")
        now = timezone.now()
        # WB-14：并发同参考号可能同时通过预检查询，由 provider_ref 唯一约束
        # 兜底；撞键方幂等返回既有支付，不得重复入账。
        try:
            with transaction.atomic():
                payment = Payment.objects.create(
                    property=property_obj, reservation=reservation, folio=folio,
                    provider="frontdesk_manual", provider_ref=provider_ref,
                    method=method, amount=amount, currency=reservation.currency,
                    cashier_shift=cashier_shift,
                    status=Payment.Status.SUCCEEDED, paid_at=now,
                )
        except IntegrityError:
            raced = Payment.objects.filter(provider_ref=provider_ref).first()
            if raced is not None and (
                raced.property_id == property_obj.pk
                and raced.reservation_id == reservation.pk
                and raced.amount == amount and raced.method == method
                and raced.provider == "frontdesk_manual"
                and raced.status == Payment.Status.SUCCEEDED
            ):
                return raced
            raise
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
        from apps.reservations.models import Reservation
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
        # WB-05a：押金上限与累计押金都写回预订行；必须在行锁下读取最新值，
        # 否则并发押金（或脏快照实例）会绕过“累计押金 ≤ 房费总额”上限。
        reservation = Reservation.objects.select_for_update().get(
            pk=reservation.pk)
        if reservation.status not in ("CONFIRMED", "CHECKED_IN"):
            raise ValueError("只有已确认或在住预订可以收取押金。")
        folio = Folio.objects.select_for_update().filter(
            property=property_obj, reservation=reservation,
            folio_type=Folio.FolioType.MASTER,
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
        cashier_shift = None
        if method == "CASH":
            from apps.billing.models import CashierShift
            cashier_shift = CashierShift.objects.select_for_update().filter(
                property=property_obj, cashier=received_by,
                status=CashierShift.ShiftStatus.OPEN).first()
            if cashier_shift is None:
                raise ValueError("现金押金登记前必须先开立本人收银班次。")
        payment = Payment.objects.create(
            property=property_obj, reservation=reservation, folio=folio,
            provider="frontdesk_deposit", provider_ref=provider_ref,
            method=method, amount=amount, currency=folio.currency,
            cashier_shift=cashier_shift,
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
            payment=payment, status__in=(Refund.Status.PENDING, Refund.Status.APPROVED,
                        Refund.Status.SUCCEEDED)
        ).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")
        if reserved + amount > payment.amount:
            raise ValueError("累计退款申请及已完成退款不能超过原支付金额。")
        # WB-13：并发同幂等键由 (property, idempotency_key) 唯一约束兜底；
        # 撞键方幂等返回既有申请。
        try:
            with transaction.atomic():
                refund = Refund.objects.create(
                    property=property_obj, payment=payment, amount=amount,
                    currency=payment.currency, reason=reason,
                    status=Refund.Status.PENDING, requested_by=requested_by,
                    provider_ref=f"RFD-{uuid.uuid4().hex.upper()[:16]}",
                    idempotency_key=key)
        except IntegrityError:
            raced = Refund.objects.filter(
                property=property_obj, idempotency_key=key).first()
            if raced is not None:
                if (raced.payment_id != payment.pk
                        or raced.amount != amount or raced.reason != reason):
                    raise ValueError("该幂等键已用于不同退款申请。")
                return raced, False
            raise
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
    def approve_refund(*, property_obj, refund_id, approved_by):
        """仅审批退款申请，不执行退款、不产生账本凭证。"""
        from apps.billing.models import Refund
        refund = Refund.objects.select_for_update().filter(
            pk=refund_id, property=property_obj).first()
        if refund is None:
            raise ValueError("退款申请不存在或不属于当前物业。")
        if refund.status == Refund.Status.APPROVED:
            if refund.approved_by_id == approved_by.pk:
                return refund, False
            raise ValueError("该退款已由其他人员审批。")
        if refund.status != Refund.Status.PENDING:
            raise ValueError("只有待审批退款可以审批。")
        if refund.requested_by_id == approved_by.pk:
            raise ValueError("退款申请人与审批人不能是同一账号。")
        approval = refund.approval_record
        if approval is None:
            raise ValueError("退款申请缺少审批记录，禁止绕过审批。")
        approval = ApprovalService.approve(
            approval.pk, approved_by=approved_by, property_obj=property_obj)
        if approval.status != ApprovalRecord.Status.APPROVED:
            raise ValueError("退款审批未成功。")
        refund.status = Refund.Status.APPROVED
        refund.approved_by = approved_by
        refund.save(update_fields=("status", "approved_by"))
        AuditEvent.record(
            action="billing.refund_approved", entity_type="Refund",
            entity_id=refund.pk, actor=approved_by, property_obj=property_obj,
            after={"status": refund.status, "approval_record_id": approval.pk},
            reason=refund.reason)
        return refund, True

    @staticmethod
    @transaction.atomic
    def reject_refund(*, property_obj, refund_id, rejected_by, reason):
        """审批驳回只改变申请状态，不产生退款账本凭证。"""
        from apps.billing.models import Refund
        refund = Refund.objects.select_for_update().filter(
            pk=refund_id, property=property_obj).first()
        if refund is None:
            raise ValueError("退款申请不存在或不属于当前物业。")
        if refund.status != Refund.Status.PENDING:
            raise ValueError("只有待审批退款可以驳回。")
        if refund.requested_by_id == rejected_by.pk:
            raise ValueError("退款申请人不能驳回自己的申请。")
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise ValueError("驳回原因必须填写且不能超过 240 个字符。")
        if refund.approval_record_id:
            ApprovalService.reject(
                refund.approval_record_id, approved_by=rejected_by,
                property_obj=property_obj, reason=reason)
        refund.status = Refund.Status.REJECTED
        refund.approved_by = rejected_by
        refund.processed_at = timezone.now()
        refund.save(update_fields=("status", "approved_by", "processed_at"))
        AuditEvent.record(
            action="billing.refund_rejected", entity_type="Refund",
            entity_id=refund.pk, actor=rejected_by, property_obj=property_obj,
            after={"status": refund.status}, reason=reason)
        return refund, True

    @staticmethod
    @transaction.atomic
    def execute_refund(*, property_obj, refund_id, executed_by,
                       confirm_refund_paid=False, execution_reference="",
                       idempotency_key=""):
        """执行已批准退款；申请、审批、执行必须由三个不同账号完成。"""
        from apps.billing.models import CashierShift, Payment, Refund, LedgerEntry
        if confirm_refund_paid is not True:
            raise ValueError("必须确认退款已经实际支付给客人。")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 96:
            raise ValueError("退款执行必须提供 1 至 96 个字符的幂等键。")
        execution_reference = str(execution_reference or "").strip()
        if len(execution_reference) > 80:
            raise ValueError("退款执行参考号不能超过 80 个字符。")
        refund = Refund.objects.select_for_update().filter(
            pk=refund_id, property=property_obj).first()
        if refund is None:
            raise ValueError("退款申请不存在或不属于当前物业。")
        if refund.status == Refund.Status.SUCCEEDED:
            if refund.execution_idempotency_key == key:
                return refund, False
            raise ValueError("该退款已经执行，不能使用其他幂等键重复执行。")
        if refund.status != Refund.Status.APPROVED:
            raise ValueError("只有已审批退款可以执行。")
        if not refund.approved_by_id:
            raise ValueError("退款缺少审批人，禁止执行。")
        if refund.requested_by_id == executed_by.pk:
            raise ValueError("退款申请人不能执行自己的退款。")
        if refund.approved_by_id == executed_by.pk:
            raise ValueError("退款审批人与执行人不能是同一账号。")
        payment = Payment.objects.select_for_update().get(
            pk=refund.payment_id, property=property_obj)
        reserved = Refund.objects.filter(
            payment=payment,
            status__in=(Refund.Status.PENDING, Refund.Status.APPROVED,
                        Refund.Status.SUCCEEDED),
        ).exclude(pk=refund.pk).aggregate(
            total=models.Sum("amount"))["total"] or Decimal("0.00")
        if reserved + refund.amount > payment.amount:
            raise ValueError("原支付可退款余额不足。")
        folio = payment.folio
        if folio.property_id != property_obj.pk or folio.status == "VOIDED":
            raise ValueError("原支付对应账单不存在或已作废。")
        execution_shift = None
        if payment.method == "CASH":
            execution_shift = CashierShift.objects.select_for_update().filter(
                property=property_obj, cashier=executed_by,
                status=CashierShift.ShiftStatus.OPEN).first()
            if execution_shift is None:
                raise ValueError("现金退款执行前必须先开立执行人本人收银班次。")
            available_cash = BillingService.cash_shift_expected_cash(execution_shift)
            if refund.amount > available_cash:
                raise ValueError(
                    f"执行班次现金余额不足：当前应有 {available_cash}，退款金额 {refund.amount}。"
                    "请先通过现金补入记录实际领取备用金。")
        elif not execution_reference:
            raise ValueError("银行卡/转账退款必须记录渠道退款参考号。")
        BillingService.post_entry(
            property_obj=property_obj, folio=folio,
            entry_type=LedgerEntry.EntryType.REFUND,
            amount=refund.amount, currency=refund.currency,
            business_date=BillingService._business_date(property_obj),
            reference=f"refund:{refund.provider_ref}",
            description=f"退款执行完成：{refund.reason}",
            posted_by=executed_by)
        refund.status = Refund.Status.SUCCEEDED
        refund.executed_by = executed_by
        refund.executed_at = timezone.now()
        refund.execution_reference = execution_reference
        refund.execution_idempotency_key = key
        refund.execution_shift = execution_shift
        refund.processed_at = refund.executed_at
        refund.save(update_fields=(
            "status", "executed_by", "executed_at", "execution_reference",
            "execution_idempotency_key", "execution_shift", "processed_at"))
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
            action="billing.refund_executed", entity_type="Refund",
            entity_id=refund.pk, actor=executed_by, property_obj=property_obj,
            after={"amount": str(refund.amount), "status": refund.status,
                   "approved_by_id": refund.approved_by_id,
                   "executed_by_id": executed_by.pk,
                   "execution_shift_id": refund.execution_shift_id},
            reason=refund.reason)
        return refund, True

    @staticmethod
    @transaction.atomic
    def reverse_entry(*, entry_id, reason, reversed_by, request=None):
        """红字冲销（V1.1 P1-08/Q-05）：追加反向凭证，不修改原凭证。

        规则：不可冲销红字凭证；同 reference 幂等；须填写原因。
        """
        from apps.billing.models import LedgerEntry
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("冲销必须填写原因。")
        original = LedgerEntry.objects.select_for_update().get(pk=entry_id)
        existing_ref = f"reversal:{original.reference}"
        dup = LedgerEntry.objects.filter(reference=existing_ref).first()
        if dup:
            return dup, False
        if original.entry_type == LedgerEntry.EntryType.REVERSAL:
            raise ValueError("不能冲销一条红字凭证。")
        reversal = LedgerEntry.objects.create(
            property=original.property, folio=original.folio,
            stay=original.stay,
            entry_type=LedgerEntry.EntryType.REVERSAL,
            amount=-original.amount, currency=original.currency,
            # WB-02：营业日必须与其它账务写入口一致按物业时区取值，
            # 否则默认时区下跨零点的冲销会记到错误的营业日。
            business_date=BillingService._business_date(original.property),
            reference=existing_ref,
            description=f"冲销：{original.description}"[:240],
            void_of=original, void_reason=reason[:240],  # F-08 口径统一：与 services.reverse_ledger_entry 一致，硬关联原凭证
            posted_by=reversed_by)
        AuditEvent.record(
            action="billing.reverse_entry", entity_type="LedgerEntry",
            entity_id=original.pk, actor=reversed_by,
            property_obj=original.property, reason=reason[:240],
            before={"amount": str(original.amount), "reference": original.reference},
            after={"reversal_id": reversal.pk, "amount": str(reversal.amount)},
            request=request)
        return reversal, True

    @staticmethod
    @transaction.atomic
    def open_shift(*, property_obj, cashier, opening_cash=Decimal("0.00")):
        """开班（CashierShift）。"""
        from apps.billing.models import CashierShift
        opening_cash = BillingService._money(opening_cash)
        if opening_cash < 0:
            raise ValueError("开班备用金不能为负数。")
        open_shift = CashierShift.objects.filter(
            property=property_obj, cashier=cashier,
            status=CashierShift.ShiftStatus.OPEN).first()
        if open_shift:
            return open_shift, False
        # WB-11：并发开班可能同时通过预检查询，由 open_shift_key 唯一约束
        # 兜底；撞键方幂等返回既有班次。
        try:
            with transaction.atomic():
                shift = CashierShift.objects.create(
                    property=property_obj, cashier=cashier,
                    opening_cash=opening_cash)
        except IntegrityError:
            raced = CashierShift.objects.filter(
                property=property_obj, cashier=cashier,
                status=CashierShift.ShiftStatus.OPEN).first()
            if raced is not None:
                return raced, False
            raise
        AuditEvent.record(
            action="billing.shift_open", entity_type="CashierShift",
            entity_id=shift.pk, actor=cashier, property_obj=property_obj,
            after={"opening_cash": str(opening_cash)})
        return shift, True

    @staticmethod
    def cash_shift_expected_cash(shift):
        """按已锁定班次的现金事实计算抽屉应有现金；调用方必须先锁定班次行。"""
        from apps.billing.models import Payment, Refund, CashierShiftMovement
        cash_income = Payment.objects.filter(
            cashier_shift=shift, method="CASH",
            status__in=(Payment.Status.SUCCEEDED,
                        Payment.Status.PARTIALLY_REFUNDED,
                        Payment.Status.REFUNDED)).aggregate(
                total=models.Sum("amount"))["total"] or Decimal("0.00")
        cash_refunds = Refund.objects.filter(
            execution_shift=shift, payment__method="CASH",
            status=Refund.Status.SUCCEEDED).aggregate(
                total=models.Sum("amount"))["total"] or Decimal("0.00")
        cash_in = CashierShiftMovement.objects.filter(
            shift=shift, direction=CashierShiftMovement.Direction.IN).aggregate(
                total=models.Sum("amount"))["total"] or Decimal("0.00")
        cash_out = CashierShiftMovement.objects.filter(
            shift=shift, direction=CashierShiftMovement.Direction.OUT).aggregate(
                total=models.Sum("amount"))["total"] or Decimal("0.00")
        return shift.opening_cash + cash_income - cash_refunds + cash_in - cash_out

    @staticmethod
    @transaction.atomic
    def close_shift(*, property_obj, shift_id, actual_cash, closed_by, variance_reason=""):
        """关班（CashierShift）：物业范围内锁定班次，按现金事实计算差异。"""
        from apps.billing.models import CashierShift
        shift = CashierShift.objects.select_for_update().get(
            pk=shift_id, property=property_obj)
        if shift.status != CashierShift.ShiftStatus.OPEN:
            raise ValueError("班次已关闭。")
        actual = BillingService._money(actual_cash)
        if actual < 0:
            raise ValueError("实盘现金不能为负数。")
        expected = BillingService.cash_shift_expected_cash(shift)
        if expected < 0:
            raise ValueError("班次现金事实计算为负数，必须先核对现金退款/支出记录，禁止直接关班。")
        shift.expected_cash = expected
        shift.actual_cash = actual
        variance = actual - expected
        shift.variance = variance
        if variance != 0 and not variance_reason:
            raise ValueError(f"差异 {variance} 不为零，必须填写差异说明。")
        shift.variance_reason = variance_reason[:240]
        shift.status = CashierShift.ShiftStatus.CLOSED
        shift.closed_at = timezone.now()
        shift.closed_by = closed_by
        shift.save()
        AuditEvent.record(
            action="billing.shift_close", entity_type="CashierShift",
            entity_id=shift.pk, actor=closed_by, property_obj=shift.property,
            after={"opening_cash": str(shift.opening_cash),
                   "cash_income": str(cash_income), "cash_refunds": str(cash_refunds),
                   "cash_in": str(cash_in), "cash_out": str(cash_out),
                   "expected": str(expected), "actual": str(actual),
                   "variance": str(variance), "reason": variance_reason})
        return shift

    @staticmethod
    @transaction.atomic
    def record_shift_movement(*, shift_id, direction, amount, reason,
                              created_by, idempotency_key):
        """登记班次现金补入/支出；只追加、幂等且仅开放班次可写。"""
        from apps.billing.models import CashierShift, CashierShiftMovement
        direction = str(direction or "").strip().upper()
        if direction not in CashierShiftMovement.Direction.values:
            raise ValueError("现金方向必须是 IN 或 OUT。")
        amount = BillingService._money(amount)
        if amount <= 0:
            raise ValueError("现金变动金额必须大于零。")
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise ValueError("现金变动原因必须填写且不能超过 240 个字符。")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 64:
            raise ValueError("现金变动必须提供 1 至 64 个字符的幂等键。")
        shift = CashierShift.objects.select_for_update().filter(
            pk=shift_id, cashier=created_by,
            status=CashierShift.ShiftStatus.OPEN).first()
        if shift is None:
            raise ValueError("班次不存在、已关闭或不属于当前操作人。")
        if direction == CashierShiftMovement.Direction.OUT:
            available_cash = BillingService.cash_shift_expected_cash(shift)
            if amount > available_cash:
                raise ValueError(
                    f"班次现金余额不足：当前应有 {available_cash}，本次支出 {amount}。")
        existing = CashierShiftMovement.objects.filter(
            shift=shift, idempotency_key=key).first()
        if existing:
            if (existing.direction != direction or existing.amount != amount
                    or existing.reason != reason):
                raise ValueError("该幂等键已用于不同现金变动。")
            return existing, False
        movement = CashierShiftMovement.objects.create(
            shift=shift, direction=direction, amount=amount,
            reason=reason, idempotency_key=key, created_by=created_by)
        AuditEvent.record(
            action="billing.shift_cash_movement", entity_type="CashierShiftMovement",
            entity_id=movement.pk, actor=created_by, property_obj=shift.property,
            after={"shift_id": shift.pk, "direction": direction,
                   "amount": str(amount)}, reason=reason)
        return movement, True

    @staticmethod
    def financial_summary(*, property_obj, start_date, end_date):
        """财务汇总（P1-08/Q-13）：PMS2 LedgerEntry/Payment/Refund 聚合。"""
        from apps.billing.models import LedgerEntry, Payment, Refund
        entries = LedgerEntry.objects.filter(
            property=property_obj,
            business_date__gte=start_date,
            business_date__lte=end_date)
        payments = Payment.objects.filter(
            property=property_obj,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date)
        refunds = Refund.objects.filter(
            property=property_obj,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date)
        total_revenue = sum(
            e.amount for e in entries if e.amount > 0) or Decimal("0.00")
        total_expense = sum(-e.amount for e in entries if e.amount < 0) or Decimal("0.00")
        return {
            "total_revenue": str(total_revenue),
            "total_expense": str(total_expense),
            "net": str(total_revenue - total_expense),
            "payment_count": payments.count(),
            "payment_total": str(sum(p.amount for p in payments) or Decimal("0.00")),
            "refund_count": refunds.count(),
            "refund_total": str(sum(r.amount for r in refunds) or Decimal("0.00")),
            "entry_count": entries.count(),
        }
