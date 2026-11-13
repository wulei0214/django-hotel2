"""V1.2 §15.3 ApprovalRecord（Q-05/Q-06/Q-14 P0）+ §15.4 前台操作规则服务。

ApprovalRecord 是审批事实的持久化载体；同一审批只能执行一次。
BillingService 统一收款/退款/冲销/转账写入口（V1.1 §6.3）。
"""
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.billing.models import ApprovalRecord
from apps.core.models import AuditEvent


class BillingInputError(ValueError):
    """账务服务的入参校验失败（金额格式/精度、支付方式、参考号/幂等键格式）。

    RSV-10 只应把「客户端传参错误」映射为 400 VALIDATION_ERROR；业务状态
    冲突（关账守卫、预订状态不允许、累计押金超额、班次未开、应收余额不足、
    幂等键复用于不同事实等）必须继续以普通 ValueError 抛出，由视图层映射为
    409 BUSINESS_CONFLICT。继承 ValueError 保证既有 `except ValueError`
    调用方（含服务层与测试）行为不变。
    """


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
        scope = dict(
            property=property_obj, action_code=action_code,
            entity_type=entity_type, entity_id=str(entity_id),
            requested_by=requested_by)
        existing = ApprovalRecord.objects.filter(idempotency_key=idem_key).first()
        prior_count = 0
        if existing:
            if existing.status in (ApprovalRecord.Status.PENDING,
                                   ApprovalRecord.Status.APPROVED):
                return existing, False
            # 终态审批（已驳回/已过期/已撤销）是历史事实，不再阻挡同一申请人
            # 重新发起；否则营业日重开、红字冲销等高危闭环会被一次驳回永久卡死。
            # 若此前重试已产出待审批申请，先幂等返回它，避免重复堆叠待审批记录。
            pending = ApprovalRecord.objects.filter(
                status=ApprovalRecord.Status.PENDING, **scope).first()
            if pending is not None:
                return pending, False
            # 新申请用“原幂等键 + 重试序号”派生新键；待审批重放契约不受影响。
            prior_count = ApprovalRecord.objects.filter(**scope).count()
        # WB-03：并发重复申请可能同时通过预检查询，由 idempotency_key 唯一约束
        # 兜底仲裁；撞键方必须幂等返回既有记录而不是抛 IntegrityError。
        for attempt in range(8):
            if prior_count == 0 and attempt == 0:
                candidate_key = idem_key
            else:
                candidate_key = hashlib.sha256(
                    f"{raw_key}:retry:{prior_count + attempt}".encode()
                ).hexdigest()
            try:
                with transaction.atomic():
                    record = ApprovalRecord.objects.create(
                        property=property_obj, action_code=action_code,
                        entity_type=entity_type, entity_id=str(entity_id),
                        requested_by=requested_by, reason=reason,
                        payload_snapshot=payload, idempotency_key=candidate_key,
                        expires_at=now_plus(hours=expires_hours))
            except IntegrityError:
                raced = ApprovalRecord.objects.filter(
                    idempotency_key=candidate_key).first()
                if raced is not None:
                    # 审批幂等键由物业、动作、目标对象和申请人共同确定；
                    # 不得在审批服务中读取收银班次字段。唯一约束冲突时：
                    # 待审批/已批准记录幂等返回；终态撞键则换下一个重试序号。
                    if raced.status in (ApprovalRecord.Status.PENDING,
                                        ApprovalRecord.Status.APPROVED):
                        return raced, False
                    prior_count += 1
                    continue
                raise
            AuditEvent.record(
                action="approval.request", entity_type=entity_type,
                entity_id=str(entity_id), actor=requested_by,
                property_obj=property_obj,
                after={"action_code": action_code, "status": "PENDING"},
                reason=reason[:240], request=getattr(requested_by, "_request", None))
            return record, True
        raise ValueError("审批申请并发冲突，请稍后重试。")

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
    import logging

    from django.core.cache import cache

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
    def _validate_refund_approval_link(refund, property_obj, expected_status=None):
        """校验审批事实与退款申请一一对应，防止错链审批被复用。"""
        from apps.billing.models import ApprovalRecord

        approval = refund.approval_record
        if approval is None:
            raise ValueError("退款申请缺少审批记录，禁止继续。")
        if (
                approval.property_id != property_obj.pk
                or approval.action_code != "billing.refund"
                or approval.entity_type != "Refund"
                or approval.entity_id != str(refund.pk)
                or approval.requested_by_id != refund.requested_by_id):
            raise ValueError("退款审批记录与当前退款申请不匹配，禁止继续。")

        payload = approval.payload_snapshot or {}
        if not isinstance(payload, dict):
            raise ValueError("退款审批参数快照格式无效，禁止继续。")
        try:
            payload_amount = Decimal(str(payload.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            payload_amount = None
        if (
                str(payload.get("payment_id")) != str(refund.payment_id)
                or payload_amount != refund.amount
                or payload.get("currency") != refund.currency):
            raise ValueError("退款审批参数快照与当前退款金额/支付流水不一致，禁止继续。")

        if expected_status is not None and approval.status != expected_status:
            raise ValueError("退款关联审批状态与当前退款流程不一致，禁止继续。")
        if approval.status == ApprovalRecord.Status.APPROVED:
            if not approval.approved_by_id or approval.approved_by_id != refund.approved_by_id:
                raise ValueError("退款审批人与关联审批事实不一致，禁止执行。")
        return approval

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
    def _validate_channel_payment_exemption(channel_payment, *, property_obj,
                                            folio, amount, currency,
                                            reference):
        """WB-111：关账日渠道支付豁免的支付事实校验（全部通过才豁免）。

        豁免只授予"真实渠道资金事实实时到达"的入账：调用方必须显式传入
        channel_payment，且该 Payment 与凭证的归属/状态/币种/金额/引用
        完全一致，任一不符直接拒绝（中文报错），绝不凭"没传操作人"放行。
        """
        from apps.billing.models import Payment
        if not isinstance(channel_payment, Payment):
            raise ValueError(
                "channel_payment 必须是真实的支付流水（Payment）实例，"
                "关账日渠道支付豁免拒绝。")
        if (channel_payment.property_id != property_obj.pk
                or channel_payment.folio_id != folio.pk):
            raise ValueError(
                "channel_payment 与凭证的物业/账单归属不一致，"
                "关账日渠道支付豁免拒绝。")
        if channel_payment.status != Payment.Status.SUCCEEDED:
            raise ValueError(
                "channel_payment 不是 SUCCEEDED 状态的收款事实，"
                "关账日渠道支付豁免拒绝。")
        if currency != channel_payment.currency:
            raise ValueError(
                "channel_payment 币种与凭证币种不一致，关账日渠道支付豁免拒绝。")
        if abs(amount) != channel_payment.amount:
            raise ValueError(
                "channel_payment 金额与凭证金额不一致，关账日渠道支付豁免拒绝。")
        expected_reference = f"payment:{channel_payment.provider_ref}"
        if reference != expected_reference:
            raise ValueError(
                "凭证引用与 channel_payment 渠道流水号不一致"
                f"（应为 {expected_reference}），关账日渠道支付豁免拒绝。")

    @staticmethod
    @transaction.atomic
    def post_entry(*, property_obj, folio, entry_type, amount, reference,
                   description="", posted_by=None, business_date=None,
                   currency=None, stay=None, void_of=None, void_reason="",
                   channel_payment=None):
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
        # WB-079：营业日关账守卫。目标 (property, business_date) 的 BusinessDate
        # 存在且状态为 CLOSED 时，禁止新增账务凭证；补记/冲账必须走
        # 营业日重开审批（ApprovalService / Break-glass）授权流程。
        # 守卫放在幂等重放判定之后：同事实重放不产生任何新写入，不属于
        # "新增凭证"，保持强幂等契约（关账后重试不误报）。
        # 夜审 run() 的过租凭证写入发生在同一事务内 bd 置 CLOSED 之前
        # （见 night_audit.run 注释），因此本守卫不阻断合法夜审链路。
        # 豁免（合法链路，WB-111 收紧口径）：渠道支付回调（posted_by=None 且
        # PAYMENT）代表外部资金事实的实时到达——回调可能在夜审关账后才到达
        # （深夜支付），"Payment 是不可逆的资金事实"不可拒绝也不可延迟；
        # 其真实经济时点由 Payment.paid_at 保留。员工发起的入账（posted_by
        # 非空）一律拦截。CLOSED 营业日下豁免还必须同时满足：调用方显式传入
        # channel_payment=<Payment 实例>，且该 Payment 通过归属/状态/币种/
        # 金额/引用五重校验——不再凭"没传操作人"就放行。OPEN 营业日（或无
        # 营业日记录）行为不变，不强制 channel_payment。
        from apps.core.models import BusinessDate
        closed_day = BusinessDate.objects.filter(
            property=property_obj, business_date=business_date,
            status=BusinessDate.Status.CLOSED,
        ).only("pk").first()
        if closed_day is not None:
            is_channel_payment_fact = False
            if (entry_type == LedgerEntry.EntryType.PAYMENT
                    and posted_by is None):
                if channel_payment is None:
                    raise ValueError(
                        f"营业日 {business_date.isoformat()} 已关账，未显式绑定"
                        "渠道支付事实（channel_payment）的收款凭证禁止入账；"
                        "补记须走授权流程。")
                BillingService._validate_channel_payment_exemption(
                    channel_payment, property_obj=property_obj, folio=folio,
                    amount=amount, currency=currency, reference=reference)
                is_channel_payment_fact = True
            if not is_channel_payment_fact:
                raise ValueError(
                    f"营业日 {business_date.isoformat()} 已关账，禁止新增账务凭证；"
                    "补记/冲账须走授权流程。")
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
            raise BillingInputError(
                f"支付方式必须是 {'、'.join(BillingService.VALID_PAYMENT_METHODS)}")
        try:
            amount = BillingService._money(amount)
        except ValueError as exc:
            raise BillingInputError(str(exc)) from exc
        if amount <= 0:
            raise BillingInputError("收款金额必须大于 0。")
        if reservation.property_id != property_obj.pk:
            raise ValueError("预订不属于当前物业。")
        provider_ref = str(reference or "").strip() or f"FD-{uuid.uuid4().hex.upper()}"
        if len(provider_ref) > 80:
            raise BillingInputError("收款参考号不能超过 80 个字符。")
        # 先处理幂等重放，再检查账单是否仍开放；成功收款后账单可能已结账，
        # 但同一参考号重试仍必须返回原支付，不能因后续状态变化制造“失败”假象。
        existing = Payment.objects.filter(provider_ref=provider_ref).first()
        if existing:
            if (existing.property_id != property_obj.pk
                    or existing.reservation_id != reservation.pk
                    or existing.amount != amount or existing.method != method
                    or existing.currency != reservation.currency
                    or existing.provider != "frontdesk_manual"
                    or existing.status not in (
                        Payment.Status.SUCCEEDED,
                        Payment.Status.REFUND_PENDING,
                        Payment.Status.PARTIALLY_REFUNDED,
                        Payment.Status.REFUNDED)):
                raise ValueError("该收款参考号已用于不同支付。")
            return existing
        folio = Folio.objects.select_for_update().filter(
            property=property_obj, reservation=reservation,
            folio_type="MASTER", status="OPEN").first()
        if folio is None:
            raise ValueError("主账单不存在或已关闭。")
        from apps.reservations.models import ReservationRoom
        # 幂等预检的普通读已建立 MySQL RR 快照；锁定账单后汇总账本条目必须用
        # 当前读，否则会漏掉等待期间其他收款提交的负向凭证，导致超额收款。
        entries = list(
            folio.entries.select_for_update().order_by("pk").values_list(
                "amount", flat=True))
        balance = sum(entries, start=Decimal("0.00"))
        expected = sum(
            ReservationRoom.objects.filter(
                property=property_obj, reservation=reservation
            ).values_list("amount", flat=True),
            start=Decimal("0.00"))
        posted_room = sum(
            folio.entries.select_for_update().filter(
                entry_type=LedgerEntry.EntryType.ROOM_CHARGE
            ).order_by("pk").values_list("amount", flat=True),
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
        except IntegrityError as exc:
            raced = Payment.objects.filter(provider_ref=provider_ref).first()
            if raced is not None and (
                raced.property_id == property_obj.pk
                and raced.reservation_id == reservation.pk
                and raced.amount == amount and raced.method == method
                and raced.provider == "frontdesk_manual"
                and raced.status == Payment.Status.SUCCEEDED
            ):
                return raced
            # RSV-08：不同预订复用同一 Idempotency-Key 时业务事实不一致，
            # 这是可预期的客户端冲突，必须以 400/409 返回，不能让
            # IntegrityError 冒泡成 500。
            raise ValueError(
                "该 Idempotency-Key 已被其他收款请求使用且业务事实不一致；"
                "请更换幂等键后重试。") from exc
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
                       idempotency_key, received_by=None,
                       client_display_amount=None):
        """统一登记实际收到的押金；Payment 与 DEPOSIT 凭证原子提交。

        DEC-DEP-01（用户已确认）：押金金额由服务端按 DepositPolicy 计算
        （默认 30% of Reservation.total_amount）；客户端传入的 amount 仅
        用于展示核对，不得覆盖服务端计算结果。若房型无 DepositPolicy
        或 deposit_type=NONE，押金金额退回调用方传入值（兼容 Walk-in
        和未配置政策的 RatePlan）。
        """
        from apps.billing.models import Folio, LedgerEntry, Payment
        from apps.reservations.models import Reservation
        method = str(method or "").strip().upper()
        if method == "BANK_TRANSFER":
            method = "TRANSFER"
        if method not in BillingService.VALID_PAYMENT_METHODS:
            raise BillingInputError("押金方式只支持现金、刷卡或转账。")
        try:
            amount = BillingService._money(amount)
        except ValueError as exc:
            raise BillingInputError(str(exc)) from exc
        if amount <= 0:
            raise BillingInputError("押金金额必须大于零。")
        if reservation.property_id != property_obj.pk:
            raise ValueError("预订不属于当前物业。")

        # WB-030 修复：押金 30% 为强制策略——无论 DepositPolicy 是否配置，
        # 服务端统一按 Reservation.total_amount × 30% 计算（DEC-DEP-01 正式规则）。
        # 客户端传入金额仅用于一致性核对（记日志），不得覆盖服务端结果。
        import logging
        from decimal import ROUND_HALF_UP
        # FIN-04：账务域统一 ROUND_HALF_UP（与 dec02._money 同口径）。
        # 此前 quantize 未指定 rounding，取 Decimal 上下文默认 HALF_EVEN，
        # 特定金额（如总额 6.75）会与调整/退款负债差 1 分。
        server_amount = (reservation.total_amount * Decimal("30") / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        if client_display_amount is not None:
            try:
                display = BillingService._money(client_display_amount)
            except ValueError as exc:
                raise BillingInputError(str(exc)) from exc
            if display != server_amount:
                logging.getLogger(__name__).warning(
                    "DEC-DEP-01 押金金额不一致：前端展示 %s，服务端计算 %s（以服务端为准）",
                    display, server_amount)
        amount = server_amount
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 64:
            raise BillingInputError("押金登记必须提供 1 至 64 个字符的幂等键。")
        # WB-05a：押金上限与累计押金都写回预订行；必须在行锁下读取最新值，
        # 否则并发押金（或脏快照实例）会绕过“累计押金 ≤ 房费总额”上限。
        reservation = Reservation.objects.select_for_update().get(
            pk=reservation.pk)
        provider_ref = f"FDDEP-{property_obj.pk}-{key}"
        existing = Payment.objects.filter(provider_ref=provider_ref).first()
        if existing is not None:
            if (existing.property_id != property_obj.pk
                    or existing.reservation_id != reservation.pk
                    or existing.amount != amount or existing.method != method
                    or existing.currency != reservation.currency
                    or existing.provider != "frontdesk_deposit"
                    or existing.status not in (
                        Payment.Status.SUCCEEDED,
                        Payment.Status.PARTIALLY_REFUNDED,
                        Payment.Status.REFUNDED)):
                raise ValueError("该幂等键已用于其他押金登记。")
            return existing, False
        if reservation.status not in ("PENDING", "CONFIRMED", "CHECKED_IN"):
            raise ValueError("只有待支付/已确认或在住预订可以收取押金。")
        folio = Folio.objects.select_for_update().filter(
            property=property_obj, reservation=reservation,
            folio_type=Folio.FolioType.MASTER,
            status=Folio.Status.OPEN).first()
        if folio is None:
            raise ValueError("有效主账单不存在。")
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
        # 业务闭环缺口修复（用户全链路串测发现）：PENDING 公开订单在前台
        # 收押金即确认——先确认库存占用，再迁移状态，与在线支付回调同口径。
        if reservation.status == "PENDING":
            from apps.inventory.services import (
                InventoryUnavailable,
                confirm_inventory_for_reservation,
            )
            try:
                confirm_inventory_for_reservation(reservation)
            except InventoryUnavailable as exc:
                raise ValueError(str(exc)) from exc
            reservation.status = "CONFIRMED"
            reservation.save(update_fields=("status", "updated_at"))
            # WB-098：前台收押金确认同样是正式确认路径，必须写政策快照。
            from apps.reservations.front_desk_services import snapshot_early_departure_policy
            snapshot_early_departure_policy(reservation)
            from apps.billing.services import cancel_open_payment_intents_for_reservation
            cancel_open_payment_intents_for_reservation(reservation)
            AuditEvent.record(
                action="reservation.confirmed_at_frontdesk",
                entity_type="Reservation",
                entity_id=reservation.booking_no,
                actor=received_by,
                property_obj=property_obj,
                reason=f"前台押金确认（{method} {amount}，凭证 {payment.provider_ref}）",
            )
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
                       requested_by, idempotency_key="", request=None,
                       refund_liability_id=None):
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
                    or existing.amount != amount or existing.reason != reason
                    or existing.refund_liability_id != refund_liability_id):
                raise ValueError("该幂等键已用于不同退款申请。")
            return existing, False
        liability = None
        if refund_liability_id is not None:
            from apps.billing.models import RefundLiability
            liability = RefundLiability.objects.select_for_update().filter(
                pk=refund_liability_id, property=property_obj,
                reservation_id=payment.reservation_id, folio_id=payment.folio_id,
            ).first()
            if liability is None:
                raise ValueError("待退款负债不存在，或与当前支付不属于同一物业、预订和账单。")
            if not liability.source_payments.filter(pk=payment.pk).exists():
                raise ValueError("所选原支付流水不属于该待退款负债的候选支付集合。")
            liability_reserved = Refund.objects.filter(
                refund_liability=liability,
                status__in=(Refund.Status.PENDING, Refund.Status.APPROVED),
            ).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")
            if liability_reserved + amount > liability.remaining_amount:
                raise ValueError("该待退款负债的未处理余额不足。")
        if payment.status not in (
                Payment.Status.SUCCEEDED, Payment.Status.PARTIALLY_REFUNDED,
                Payment.Status.REFUND_PENDING):
            raise ValueError("只有成功、部分退款或退款处理中的支付可申请退款。")
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
                    property=property_obj, payment=payment,
                    refund_liability=liability, amount=amount,
                    currency=payment.currency, reason=reason,
                    status=Refund.Status.PENDING, requested_by=requested_by,
                    provider_ref=f"RFD-{uuid.uuid4().hex.upper()[:16]}",
                    idempotency_key=key)
        except IntegrityError:
            raced = Refund.objects.filter(
                property=property_obj, idempotency_key=key).first()
            if raced is not None:
                if (raced.payment_id != payment.pk
                        or raced.amount != amount or raced.reason != reason
                        or raced.refund_liability_id != (liability.pk if liability else None)):
                    raise ValueError("该幂等键已用于不同退款申请。")
                return raced, False
            raise
        approval, _ = ApprovalService.request_approval(
            property_obj=property_obj, action_code="billing.refund",
            entity_type="Refund", entity_id=refund.pk,
            requested_by=requested_by, reason=reason,
            payload={"payment_id": payment.pk, "amount": str(amount),
                     "currency": payment.currency,
                     "refund_liability_id": liability.pk if liability else None})
        refund.approval_record = approval
        refund.save(update_fields=("approval_record",))
        # 支付流水状态反映存在待处理退款；多笔部分退款可并行排队申请，
        # 其可退余额由 payment 行锁 + pending/approved/succeeded 累计金额控制。
        if payment.status != Payment.Status.REFUND_PENDING:
            payment.status = Payment.Status.REFUND_PENDING
            payment.save(update_fields=("status", "updated_at"))
        AuditEvent.record(
            action="billing.refund_request", entity_type="Refund",
            entity_id=refund.pk, actor=requested_by, property_obj=property_obj,
            after={"amount": str(amount), "payment_id": payment.pk,
                   "refund_liability_id": liability.pk if liability else None},
            reason=reason, request=request)
        return refund, True

    @staticmethod
    @transaction.atomic
    def approve_refund(*, property_obj, refund_id, approved_by):
        """仅审批退款申请，不执行退款、不产生账本凭证。"""
        from apps.billing.models import Payment, Refund
        probe = Refund.objects.filter(
            pk=refund_id, property=property_obj
        ).values("payment_id").first()
        if probe is None:
            raise ValueError("退款申请不存在或不属于当前物业。")
        # 与申请/驳回/执行保持 Payment -> Refund 的锁顺序。
        payment = Payment.objects.select_for_update().filter(
            pk=probe["payment_id"], property=property_obj).first()
        if payment is None:
            raise ValueError("退款对应支付流水不存在或不属于当前物业。")
        refund = Refund.objects.select_for_update().filter(
            pk=refund_id, property=property_obj).first()
        if refund is None or refund.payment_id != payment.pk:
            raise ValueError("退款申请不存在或支付归属已变化。")
        if refund.status == Refund.Status.APPROVED:
            if refund.approved_by_id == approved_by.pk:
                return refund, False
            raise ValueError("该退款已由其他人员审批。")
        if refund.status != Refund.Status.PENDING:
            raise ValueError("只有待审批退款可以审批。")
        if refund.requested_by_id == approved_by.pk:
            raise ValueError("退款申请人与审批人不能是同一账号。")
        approval = BillingService._validate_refund_approval_link(
            refund, property_obj, expected_status=ApprovalRecord.Status.PENDING)
        approval = ApprovalService.approve(
            approval.pk, approved_by=approved_by, property_obj=property_obj)
        if approval.status == ApprovalRecord.Status.EXPIRED:
            # 审批超时是明确终态，不得回滚成永远占用原支付余额的 PENDING。
            refund.status = Refund.Status.EXPIRED
            refund.processed_at = approval.approved_at
            refund.save(update_fields=("status", "processed_at"))
            pending_refunds = Refund.objects.select_for_update().filter(
                payment_id=payment.pk,
                status__in=(Refund.Status.PENDING, Refund.Status.APPROVED),
            ).order_by("pk")
            if not pending_refunds.exists():
                refunded_amounts = Refund.objects.select_for_update().filter(
                    payment_id=payment.pk,
                    status=Refund.Status.SUCCEEDED,
                ).order_by("pk").values_list("amount", flat=True)
                refunded_total = sum(refunded_amounts, Decimal("0.00"))
                if refunded_total >= payment.amount:
                    payment.status = Payment.Status.REFUNDED
                elif refunded_total > 0:
                    payment.status = Payment.Status.PARTIALLY_REFUNDED
                else:
                    payment.status = Payment.Status.SUCCEEDED
                payment.save(update_fields=("status", "updated_at"))
            AuditEvent.record(
                action="billing.refund_expired", entity_type="Refund",
                entity_id=refund.pk, actor=approved_by,
                property_obj=property_obj,
                after={"status": refund.status,
                       "approval_record_id": approval.pk,
                       "processed_at": refund.processed_at.isoformat()},
                reason="退款审批超过有效期，释放未执行退款占用")
            return refund, True
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
    def expire_due_refunds(*, property_obj=None, limit=100, now=None):
        """定时关闭审批超时退款；每笔独立事务，锁顺序 Payment -> Refund -> ApprovalRecord。"""
        from apps.billing.models import ApprovalRecord, Payment, Refund
        from apps.core.models import AuditEvent

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError("过期退款处理条数必须是整数。")
        if limit < 1 or limit > 500:
            raise ValueError("单次过期退款处理条数必须在 1 至 500 之间。")
        now = now or timezone.now()
        candidates = Refund.objects.filter(
            status=Refund.Status.PENDING,
            legacy_workflow=False,
            approval_record__status=ApprovalRecord.Status.PENDING,
            approval_record__expires_at__lte=now,
        )
        if property_obj is not None:
            candidates = candidates.filter(property=property_obj)
        candidate_rows = list(
            candidates.order_by("approval_record__expires_at", "pk")
            .values_list("pk", "payment_id")[:limit]
        )
        expired_ids = []
        for refund_id, payment_id in candidate_rows:
            with transaction.atomic():
                payment = Payment.objects.select_for_update().filter(
                    pk=payment_id,
                    **({"property": property_obj} if property_obj is not None else {}),
                ).first()
                if payment is None:
                    continue
                refund = Refund.objects.select_for_update().filter(
                    pk=refund_id, payment=payment,
                    status=Refund.Status.PENDING, legacy_workflow=False,
                ).first()
                if refund is None or not refund.approval_record_id:
                    continue
                approval = ApprovalRecord.objects.select_for_update().filter(
                    pk=refund.approval_record_id,
                    property=payment.property,
                    status=ApprovalRecord.Status.PENDING,
                    expires_at__lte=now,
                ).first()
                if approval is None:
                    continue
                BillingService._validate_refund_approval_link(
                    refund, payment.property,
                    expected_status=ApprovalRecord.Status.PENDING)

                approval.status = ApprovalRecord.Status.EXPIRED
                approval.approved_at = now
                approval.save(update_fields=("status", "approved_at"))
                refund.status = Refund.Status.EXPIRED
                refund.processed_at = now
                refund.save(update_fields=("status", "processed_at"))

                pending_refunds = Refund.objects.select_for_update().filter(
                    payment_id=payment.pk,
                    status__in=(Refund.Status.PENDING, Refund.Status.APPROVED),
                ).order_by("pk")
                if not pending_refunds.exists():
                    refunded_amounts = Refund.objects.select_for_update().filter(
                        payment_id=payment.pk,
                        status=Refund.Status.SUCCEEDED,
                    ).order_by("pk").values_list("amount", flat=True)
                    refunded_total = sum(refunded_amounts, Decimal("0.00"))
                    if refunded_total >= payment.amount:
                        payment.status = Payment.Status.REFUNDED
                    elif refunded_total > 0:
                        payment.status = Payment.Status.PARTIALLY_REFUNDED
                    else:
                        payment.status = Payment.Status.SUCCEEDED
                    payment.save(update_fields=("status", "updated_at"))

                AuditEvent.record(
                    action="billing.refund_expired",
                    entity_type="Refund", entity_id=refund.pk,
                    actor=None, property_obj=payment.property,
                    after={"status": refund.status,
                           "approval_record_id": approval.pk,
                           "processed_at": now.isoformat()},
                    reason="系统定时任务：退款审批超过有效期，释放原支付可退额度")
                expired_ids.append(refund.pk)
        return expired_ids

    @staticmethod
    @transaction.atomic
    def reject_refund(*, property_obj, refund_id, rejected_by, reason):
        """审批驳回只改变申请状态，不产生退款账本凭证。"""
        from apps.billing.models import Payment, Refund
        # 退款写路径统一先锁 Payment，再锁 Refund；申请、驳回和执行同序，
        # 避免与关班/退款执行形成反向锁等待。
        probe = Refund.objects.filter(
            pk=refund_id, property=property_obj
        ).values("payment_id").first()
        if probe is None:
            raise ValueError("退款申请不存在或不属于当前物业。")
        payment = Payment.objects.select_for_update().filter(
            pk=probe["payment_id"], property=property_obj).first()
        if payment is None:
            raise ValueError("退款对应支付流水不存在或不属于当前物业。")
        refund = Refund.objects.select_for_update().filter(
            pk=refund_id, property=property_obj).first()
        if refund is None or refund.payment_id != payment.pk:
            raise ValueError("退款申请不存在或支付归属已变化。")
        if refund.status != Refund.Status.PENDING:
            raise ValueError("只有待审批退款可以驳回。")
        if refund.requested_by_id == rejected_by.pk:
            raise ValueError("退款申请人不能驳回自己的退款。")
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise ValueError("驳回原因必须填写且不能超过 240 个字符。")
        BillingService._validate_refund_approval_link(
            refund, property_obj, expected_status=ApprovalRecord.Status.PENDING)
        ApprovalService.reject(
            refund.approval_record_id, approved_by=rejected_by,
            property_obj=property_obj, reason=reason)
        refund.status = Refund.Status.REJECTED
        refund.approved_by = rejected_by
        refund.processed_at = timezone.now()
        refund.save(update_fields=("status", "approved_by", "processed_at"))
        # probe 在等待 Payment 锁前可能已建立 MySQL RR 快照；状态汇总必须用当前读，
        # 避免漏掉等待期间刚提交的退款申请/执行，错误地把 Payment 回退为 SUCCEEDED。
        pending_refunds = Refund.objects.select_for_update().filter(
            payment_id=refund.payment_id,
            status__in=(Refund.Status.PENDING, Refund.Status.APPROVED),
        ).order_by("pk")
        if not pending_refunds.exists():
            refunded_amounts = Refund.objects.select_for_update().filter(
                payment_id=payment.pk, status=Refund.Status.SUCCEEDED
            ).order_by("pk").values_list("amount", flat=True)
            refunded_total = sum(refunded_amounts, Decimal("0.00"))
            if refunded_total >= payment.amount:
                payment.status = Payment.Status.REFUNDED
            elif refunded_total > 0:
                payment.status = Payment.Status.PARTIALLY_REFUNDED
            else:
                payment.status = Payment.Status.SUCCEEDED
            payment.save(update_fields=("status", "updated_at"))
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
        from apps.billing.models import CashierShift, LedgerEntry, Payment, Refund
        if confirm_refund_paid is not True:
            raise ValueError("必须确认退款已经实际支付给客人。")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 96:
            raise ValueError("退款执行必须提供 1 至 96 个字符的幂等键。")
        execution_reference = str(execution_reference or "").strip()
        if len(execution_reference) > 80:
            raise ValueError("退款执行参考号不能超过 80 个字符。")
        # 先用非锁定读取确定支付方式；现金退款与关班统一按 Shift -> Payment -> Refund
        # 加锁，避免 MySQL InnoDB 因反向锁顺序产生死锁。
        probe = Refund.objects.filter(
            pk=refund_id, property=property_obj
        ).values("payment_id", "status", "execution_idempotency_key",
                 "payment__method").first()
        if probe is None:
            raise ValueError("退款申请不存在或不属于当前物业。")
        if probe["status"] == Refund.Status.SUCCEEDED:
            completed = Refund.objects.select_for_update().get(
                pk=refund_id, property=property_obj)
            if completed.status == Refund.Status.SUCCEEDED:
                if completed.execution_idempotency_key == key:
                    return completed, False
                raise ValueError("该退款已经执行，不能使用其他幂等键重复执行。")

        execution_shift = None
        if probe["payment__method"] == "CASH":
            execution_shift = CashierShift.objects.select_for_update().filter(
                property=property_obj, cashier=executed_by,
                status=CashierShift.ShiftStatus.OPEN).first()
            if execution_shift is None:
                # 可能另一请求已完成执行并关闭班次；先复核幂等终态，避免把成功重放
                # 错误报告为“未开班”。
                completed = Refund.objects.select_for_update().filter(
                    pk=refund_id, property=property_obj).first()
                if (completed is not None
                        and completed.status == Refund.Status.SUCCEEDED
                        and completed.execution_idempotency_key == key):
                    return completed, False
                raise ValueError("现金退款执行前必须先开立执行人本人收银班次。")
        elif not execution_reference:
            raise ValueError("银行卡/转账退款必须记录渠道退款参考号。")

        # 全局锁顺序：现金班次（现金退款）-> Payment -> Refund。
        # 关班也是 Shift -> Payment -> Refund；退款申请/驳回均先锁 Payment。
        payment = Payment.objects.select_for_update().filter(
            pk=probe["payment_id"], property=property_obj).first()
        if payment is None:
            raise ValueError("退款对应支付流水不存在或不属于当前物业。")
        refund = Refund.objects.select_for_update().filter(
            pk=refund_id, property=property_obj).first()
        if refund is None or refund.payment_id != payment.pk:
            raise ValueError("退款申请不存在或支付归属已变化。")
        key_owner = Refund.objects.filter(
            execution_idempotency_key=key).exclude(pk=refund.pk).first()
        if key_owner is not None:
            raise ValueError("该退款执行幂等键已被其他退款使用。")
        if refund.status == Refund.Status.SUCCEEDED:
            if refund.execution_idempotency_key == key:
                return refund, False
            raise ValueError("该退款已经执行，不能使用其他幂等键重复执行。")
        if refund.status != Refund.Status.APPROVED:
            raise ValueError("只有已审批退款可以执行。")
        # WB-091-旧：非现金退款的渠道执行参考号在物业内唯一（数据库约束
        # uq_refund_property_execution_reference 兜底）；重复参考号说明同一
        # 笔渠道退款凭据被登记到两笔退款事实，必须明确拒绝而不是静默通过。
        # 幂等重放（同一退款同一 key）已在上方提前返回，不会走到这里。
        if execution_reference and Refund.objects.filter(
                property=property_obj,
                execution_reference=execution_reference,
        ).exclude(pk=refund.pk).exists():
            raise ValueError(
                "该退款执行参考号已被本物业其他退款使用，禁止重复登记。")
        if not refund.approved_by_id:
            raise ValueError("退款缺少审批人，禁止执行。")
        BillingService._validate_refund_approval_link(
            refund, property_obj, expected_status=ApprovalRecord.Status.APPROVED)
        if refund.requested_by_id == executed_by.pk:
            raise ValueError("退款申请人不能执行自己的退款。")
        if refund.approved_by_id == executed_by.pk:
            raise ValueError("退款审批人与执行人不能是同一账号。")
        if payment.method == "CASH" and execution_shift is None:
            raise ValueError("现金退款执行前必须先开立执行人本人收银班次。")

        # Payment 锁串行化同一支付的申请/驳回/执行；select_for_update 是 MySQL
        # 当前读，确保前面的 probe 建立 RR 快照后也不会漏掉刚提交的退款申请。
        reserved_amounts = Refund.objects.select_for_update().filter(
            payment=payment,
            status__in=(Refund.Status.PENDING, Refund.Status.APPROVED,
                        Refund.Status.SUCCEEDED),
        ).exclude(pk=refund.pk).order_by("pk").values_list("amount", flat=True)
        reserved = sum(reserved_amounts, Decimal("0.00"))
        if reserved + refund.amount > payment.amount:
            raise ValueError("原支付可退款余额不足。")
        folio = payment.folio
        if folio.property_id != property_obj.pk or folio.status == "VOIDED":
            raise ValueError("原支付对应账单不存在或已作废。")
        if execution_shift is not None:
            available_cash = BillingService.cash_shift_expected_cash(execution_shift, lock_facts=True)
            if refund.amount > available_cash:
                raise ValueError(
                    f"执行班次现金余额不足：当前应有 {available_cash}，退款金额 {refund.amount}。"
                    "请先通过现金补入记录实际领取备用金。")
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
        try:
            # execution_idempotency_key 是全局唯一键；不同退款并发复用同一 key
            # 时由数据库仲裁。嵌套 atomic 提供 savepoint，冲突后可安全读取占用方，
            # 并让外层事务回滚本次尚未提交的账本凭证。
            with transaction.atomic():
                refund.save(update_fields=(
                    "status", "executed_by", "executed_at", "execution_reference",
                    "execution_idempotency_key", "execution_shift", "processed_at"))
        except IntegrityError:
            key_owner = Refund.objects.filter(
                execution_idempotency_key=key).exclude(pk=refund.pk).first()
            if key_owner is not None:
                raise ValueError("该退款执行幂等键已被其他退款使用。")
            raise
        liability = None
        if refund.refund_liability_id:
            from apps.billing.models import RefundLiability
            liability = RefundLiability.objects.select_for_update().filter(
                pk=refund.refund_liability_id, property=property_obj,
                reservation_id=payment.reservation_id, folio_id=payment.folio_id,
            ).first()
            if liability is None or not liability.source_payments.filter(pk=payment.pk).exists():
                raise ValueError("退款关联的待退款负债或原支付候选关系不存在。")
            if liability.remaining_amount < refund.amount:
                raise ValueError("待退款负债余额不足，不能完成该笔退款。")
            liability.remaining_amount -= refund.amount
            if liability.remaining_amount == 0:
                liability.status = RefundLiability.Status.SETTLED
                liability.settled_at = timezone.now()
            else:
                liability.status = RefundLiability.Status.PARTIALLY_SETTLED
            liability.save(update_fields=("remaining_amount", "status", "settled_at"))

        # 与申请/驳回一致：状态汇总必须用当前读。probe 在等待 Payment 锁前
        # 已建立 MySQL RR 快照，普通读会漏掉等待期间提交的退款事实，
        # 把 REFUND_PENDING/已退款终态误写成 PARTIALLY_REFUNDED。
        refunded_amounts = Refund.objects.select_for_update().filter(
            payment=payment, status=Refund.Status.SUCCEEDED
        ).order_by("pk").values_list("amount", flat=True)
        refunded_total = sum(refunded_amounts, Decimal("0.00"))
        has_pending_refund = Refund.objects.select_for_update().filter(
            payment=payment,
            status__in=(Refund.Status.PENDING, Refund.Status.APPROVED),
        ).exclude(pk=refund.pk).order_by("pk").exists()
        if has_pending_refund:
            payment.status = Payment.Status.REFUND_PENDING
        elif refunded_total >= payment.amount:
            payment.status = Payment.Status.REFUNDED
        else:
            payment.status = Payment.Status.PARTIALLY_REFUNDED
        payment.save(update_fields=("status", "updated_at"))
        if payment.provider == "frontdesk_deposit":
            # 押金累计写在预订行上；回减前必须当前读锁定该行，否则与并发的
            # record_deposit（先锁预订行再累加）互相覆盖，造成押金账实不符。
            from apps.reservations.models import Reservation
            reservation = Reservation.objects.select_for_update().get(
                pk=payment.reservation_id)
            reservation.deposit_amount = max(
                Decimal("0.00"), reservation.deposit_amount - refund.amount)
            reservation.save(update_fields=("deposit_amount", "updated_at"))
        AuditEvent.record(
            action="billing.refund_executed", entity_type="Refund",
            entity_id=refund.pk, actor=executed_by, property_obj=property_obj,
            after={"amount": str(refund.amount), "status": refund.status,
                   "approved_by_id": refund.approved_by_id,
                   "executed_by_id": executed_by.pk,
                   "execution_shift_id": refund.execution_shift_id,
                   "refund_liability_id": liability.pk if liability else None,
                   "liability_remaining_amount": (
                       str(liability.remaining_amount) if liability else None)},
            reason=refund.reason)
        return refund, True

    @staticmethod
    @transaction.atomic
    def reverse_entry(*, property_obj, entry_id, approval_id, reason,
                      reversed_by, request=None):
        """红字冲销：财务执行前必须存在值班经理/店长批准的审批事实。"""
        from apps.billing.models import ApprovalRecord, LedgerEntry
        from apps.identity.authorization import AuthorizationService
        if not AuthorizationService.has_permission(
                reversed_by, property_obj, "billing.reverse_entry"):
            raise ValueError("当前员工没有财务红字冲销执行权限。")
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("冲销必须填写原因。")
        original = LedgerEntry.objects.select_for_update().get(
            pk=entry_id, property=property_obj)
        if original.entry_type == LedgerEntry.EntryType.REVERSAL:
            raise ValueError("不能冲销一条红字凭证。")
        approval = ApprovalRecord.objects.select_for_update().filter(
            pk=approval_id, property=property_obj,
            action_code="billing.reversal", entity_type="LedgerEntry",
            entity_id=str(original.pk),
            status=ApprovalRecord.Status.APPROVED,
        ).first()
        if approval is None:
            raise ValueError("冲销必须先取得当前物业对该原凭证的有效审批。")
        if not AuthorizationService.has_permission(
                approval.requested_by, property_obj, "billing.reverse_entry"):
            raise ValueError("冲销申请人不具备财务冲销申请权限。")
        if not AuthorizationService.has_permission(
                approval.approved_by, property_obj, "billing.reversal_approve"):
            raise ValueError("冲销审批人不具备值班经理/店长审批权限。")
        if approval.approved_by_id == reversed_by.pk:
            raise ValueError("冲销审批人与实际执行人必须是不同账号。")
        if approval.reason != reason:
            raise ValueError("冲销执行原因必须与已批准的申请原因完全一致。")
        # WB-079：红字冲销此前是唯一绕过 post_entry 的生产活跃账本直写，
        # 因此在 CLOSED 营业日也能写入。关账后调整必须先经授权重开营业日
        # （night_audit.reopen / break-glass），审批链不能替代营业日守卫。
        from apps.core.models import BusinessDate
        business_date = BillingService._business_date(original.property)
        closed_day = BusinessDate.objects.filter(
            property=property_obj, business_date=business_date,
            status=BusinessDate.Status.CLOSED).exists()
        if closed_day:
            raise ValueError(
                f"营业日 {business_date.isoformat()} 已关账；红字冲销须先经授权"
                "重开营业日后再执行，不得绕过关账守卫（WB-079）。")
        existing_ref = f"reversal:{original.reference}"
        dup = LedgerEntry.objects.filter(reference=existing_ref).first()
        if dup:
            if dup.approval_record_id == approval.pk:
                return dup, False
            if dup.approval_record_id is None:
                raise ValueError("该凭证已由历史流程冲销，不能通过新流程重复执行。")
            raise ValueError("该凭证已关联其他冲销审批，不得重复执行。")
        reversal = LedgerEntry.objects.create(
            property=original.property, folio=original.folio,
            stay=original.stay,
            entry_type=LedgerEntry.EntryType.REVERSAL,
            amount=-original.amount, currency=original.currency,
            business_date=business_date,
            reference=existing_ref,
            description=f"冲销：{original.description}"[:240],
            void_of=original, void_reason=reason[:240],
            approval_record=approval, posted_by=reversed_by)
        AuditEvent.record(
            action="billing.reverse_entry", entity_type="LedgerEntry",
            entity_id=original.pk, actor=reversed_by,
            property_obj=original.property, reason=reason[:240],
            before={"amount": str(original.amount), "reference": original.reference},
            after={"reversal_id": reversal.pk, "amount": str(reversal.amount),
                   "approval_id": approval.pk,
                   "requested_by_id": approval.requested_by_id,
                   "approved_by_id": approval.approved_by_id,
                   "executed_by_id": reversed_by.pk},
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
            if open_shift.opening_cash != opening_cash:
                raise ValueError("当前已有 OPEN 班次，重复开班的备用金金额与原记录不一致。")
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
                if raced.opening_cash != opening_cash:
                    raise ValueError("并发开班已使用不同备用金金额创建班次，请核对原班次。")
                return raced, False
            raise
        AuditEvent.record(
            action="billing.shift_open", entity_type="CashierShift",
            entity_id=shift.pk, actor=cashier, property_obj=property_obj,
            after={"opening_cash": str(opening_cash)})
        return shift, True

    @staticmethod
    def cash_shift_breakdown(shift, lock_facts=False):
        """返回班次现金核算明细；写路径先锁班次，再用当前读锁定现金事实。"""
        from apps.billing.models import CashierShiftMovement, Payment, Refund
        payment_qs = Payment.objects.filter(
            cashier_shift=shift, method="CASH",
            status__in=(Payment.Status.SUCCEEDED,
                        Payment.Status.REFUND_PENDING,
                        Payment.Status.PARTIALLY_REFUNDED,
                        Payment.Status.REFUNDED))
        refund_qs = Refund.objects.filter(
            execution_shift=shift, payment__method="CASH",
            status=Refund.Status.SUCCEEDED)
        movement_qs = CashierShiftMovement.objects.filter(shift=shift)
        if lock_facts:
            # SELECT ... FOR UPDATE 是 MySQL 当前读，避免事务在锁班次前已建立
            # REPEATABLE READ 快照时，现金支出/退款被旧快照漏算。
            payment_amounts = payment_qs.select_for_update().order_by("pk").values_list(
                "amount", flat=True)
            refund_amounts = refund_qs.select_for_update().order_by("pk").values_list(
                "amount", flat=True)
            movement_rows = movement_qs.select_for_update().order_by("pk").values_list(
                "direction", "amount")
            cash_income = sum(payment_amounts, Decimal("0.00"))
            cash_refunds = sum(refund_amounts, Decimal("0.00"))
            movement_rows = list(movement_rows)
            cash_in = sum((amount for direction, amount in movement_rows
                           if direction == CashierShiftMovement.Direction.IN),
                          Decimal("0.00"))
            cash_out = sum((amount for direction, amount in movement_rows
                            if direction == CashierShiftMovement.Direction.OUT),
                           Decimal("0.00"))
        else:
            cash_income = payment_qs.aggregate(
                total=models.Sum("amount"))["total"] or Decimal("0.00")
            cash_refunds = refund_qs.aggregate(
                total=models.Sum("amount"))["total"] or Decimal("0.00")
            cash_in = movement_qs.filter(
                direction=CashierShiftMovement.Direction.IN).aggregate(
                    total=models.Sum("amount"))["total"] or Decimal("0.00")
            cash_out = movement_qs.filter(
                direction=CashierShiftMovement.Direction.OUT).aggregate(
                    total=models.Sum("amount"))["total"] or Decimal("0.00")
        expected = shift.opening_cash + cash_income - cash_refunds + cash_in - cash_out
        return {
            "cash_income": cash_income,
            "cash_refunds": cash_refunds,
            "cash_in": cash_in,
            "cash_out": cash_out,
            "expected": expected,
        }

    @staticmethod
    def cash_shift_expected_cash(shift, lock_facts=False):
        """按班次现金事实计算应有现金；写路径应启用 lock_facts 当前读。"""
        return BillingService.cash_shift_breakdown(
            shift, lock_facts=lock_facts)["expected"]

    @staticmethod
    @transaction.atomic
    def close_shift(*, property_obj, shift_id, actual_cash, closed_by,
                    variance_reason="", allow_other_cashier=False):
        """关班（CashierShift）：物业范围内锁定班次，按现金事实计算差异。"""
        from apps.billing.models import CashierShift
        shift = CashierShift.objects.select_for_update().get(
            pk=shift_id, property=property_obj)
        if shift.status != CashierShift.ShiftStatus.OPEN:
            raise ValueError("班次已关闭。")
        if shift.cashier_id != closed_by.pk and not allow_other_cashier:
            raise ValueError("只能关闭本人班次；代关其他员工班次需要财务/管理权限。")
        actual = BillingService._money(actual_cash)
        variance_reason = str(variance_reason or "").strip()
        if actual < 0:
            raise ValueError("实盘现金不能为负数。")
        cash_facts = BillingService.cash_shift_breakdown(shift, lock_facts=True)
        expected = cash_facts["expected"]
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
                   "cash_income": str(cash_facts["cash_income"]),
                   "cash_refunds": str(cash_facts["cash_refunds"]),
                   "cash_in": str(cash_facts["cash_in"]),
                   "cash_out": str(cash_facts["cash_out"]),
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
            pk=shift_id, cashier=created_by).first()
        if shift is None:
            raise ValueError("班次不存在或不属于当前操作人。")
        existing = CashierShiftMovement.objects.filter(
            shift=shift, idempotency_key=key).first()
        if existing:
            if (existing.direction != direction or existing.amount != amount
                    or existing.reason != reason):
                raise ValueError("该幂等键已用于不同现金变动。")
            return existing, False
        if shift.status != CashierShift.ShiftStatus.OPEN:
            raise ValueError("已关闭班次不能登记新的现金变动。")
        # 幂等重放必须先返回原事实，再检查新支出余额；否则重放会被首次支出
        # 已经扣减的余额误拒绝，破坏“同 key 同事实返回既有记录”的契约。
        if direction == CashierShiftMovement.Direction.OUT:
            available_cash = BillingService.cash_shift_expected_cash(shift, lock_facts=True)
            if amount > available_cash:
                raise ValueError(
                    f"班次现金余额不足：当前应有 {available_cash}，本次支出 {amount}。")
        try:
            with transaction.atomic():
                movement = CashierShiftMovement.objects.create(
                    shift=shift, direction=direction, amount=amount,
                    reason=reason, idempotency_key=key, created_by=created_by)
        except IntegrityError:
            raced = CashierShiftMovement.objects.filter(
                shift=shift, idempotency_key=key).first()
            if raced is not None:
                if (raced.direction == direction and raced.amount == amount
                        and raced.reason == reason):
                    return raced, False
                raise ValueError("该幂等键已用于不同现金变动。")
            raise
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
        # created_at 是带时区时间戳；必须先按物业时区换算自然日边界，
        # 再用半开区间过滤，避免 UTC/数据库会话时区造成跨日错位。
        from datetime import datetime
        from datetime import time as datetime_time
        from zoneinfo import ZoneInfo

        from django.utils import timezone
        try:
            property_tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
        except Exception:
            property_tz = ZoneInfo("Asia/Shanghai")
        start_at = timezone.make_aware(
            datetime.combine(start_date, datetime_time.min), property_tz)
        end_at = timezone.make_aware(
            datetime.combine(end_date + timedelta(days=1), datetime_time.min),
            property_tz)
        # FIN-03：报表只统计真实到账/已动用资金。此前零状态过滤，
        # FAILED/CANCELLED/PENDING 支付与 PENDING/REJECTED 退款全部计入，
        # 报表金额虚增。口径与对账服务保持一致（同一常量、同一退款终态）。
        from apps.billing.reconciliation import PAYMENT_ENTRY_CHECK_STATUSES
        payments = Payment.objects.filter(
            property=property_obj, created_at__gte=start_at, created_at__lt=end_at,
            status__in=PAYMENT_ENTRY_CHECK_STATUSES)
        refunds = Refund.objects.filter(
            property=property_obj, created_at__gte=start_at, created_at__lt=end_at,
            status=Refund.Status.SUCCEEDED)
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
