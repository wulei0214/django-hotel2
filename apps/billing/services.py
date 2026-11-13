import hashlib
import hmac
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.inventory.services import InventoryUnavailable, confirm_inventory_for_reservation
from apps.reservations.models import Reservation

from .models import LedgerEntry, Payment, PaymentCompensationTask, PaymentIntent
from .payment_intents import (
    confirm_intent_success,
    create_compensation_task,
    create_intent,
    record_provider_event,
)


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


def cancel_open_payment_intents_for_reservation(reservation):
    """订单已确认/取消/过期时关闭尚未成功的本地支付意图。

    调用方必须先持有 Reservation 行锁，以保持 Reservation -> Payment 锁顺序。
    渠道迟到 SUCCESS 仍由回调处理为成功资金事实并进入补偿流程。
    """
    cancelled = 0
    payments = Payment.objects.select_for_update().filter(
        reservation=reservation,
        status__in=(Payment.Status.CREATED, Payment.Status.PENDING),
    ).order_by("pk")
    for payment in payments:
        payment.status = Payment.Status.CANCELLED
        payment.save(update_fields=("status", "updated_at"))
        cancelled += 1
    return cancelled


def _ensure_payment_intent_for_payment(payment, *, idempotency_key=""):
    """WB-088：为 mock Payment 同事务创建/复用配套 PaymentIntent。

    幂等键优先取调用方幂等键（请求 Idempotency-Key），缺省回落渠道流水号
    provider_ref。意图的物业/预订/账单/金额/币种必须与 Payment 完全一致，
    以满足 confirm_intent_success 的 WB-072 归属校验。
    """
    key = str(idempotency_key or "").strip() or str(payment.provider_ref)
    intent, _created = create_intent(
        property_obj=payment.property,
        reservation=payment.reservation,
        folio=payment.folio,
        amount=payment.amount,
        currency=payment.currency,
        purpose=PaymentIntent.Purpose.ROOM_FEE,
        provider_code=payment.provider or "mock",
        created_by=None,
        idempotency_key=key,
    )
    return intent


def _promote_payment_intent_on_channel_success(payment, *, signature_verified):
    """WB-088：渠道 SUCCESS 回调在同一事务内推进 PaymentIntent 状态机。

    - record_provider_event：把 mock 渠道 SUCCESS 记成渠道交易事实
      （event_id 幂等，绑定渠道流水号）；
    - confirm_intent_success：意图 → SUCCEEDED 并绑定 confirmed_payment=
      本笔收款；其内部会再做库存确认，Hold 已失效时由该服务创建
      HOLD_COORDINATION 补偿任务（WB-073 路径）。
    - 未验签的 SUCCESS 不推进（WB-076：成功事实必须验签，正常由视图
      门禁先行拦截，这里是服务层防御）；意图缺失（历史 Payment/服务
      直连创建）时跳过，不阻断回调主流程。
    """
    from apps.core.models import AuditEvent

    intent = PaymentIntent.objects.filter(
        reservation_id=payment.reservation_id,
        folio_id=payment.folio_id,
        amount=payment.amount,
        currency=payment.currency,
        provider_code=payment.provider or "mock",
        status__in=(PaymentIntent.Status.PENDING, PaymentIntent.Status.PROCESSING),
    ).order_by("pk").first()
    if intent is None:
        return
    if not signature_verified:
        AuditEvent.record(
            action="billing.mock_callback_intent_skipped_unsigned",
            entity_type="Payment", entity_id=str(payment.pk),
            property_obj=payment.property,
            after={"intent_id": intent.pk, "provider_ref": payment.provider_ref},
            reason="渠道回调未验签，跳过支付意图推进（成功事实必须验签）")
        return
    record_provider_event(
        property_obj=payment.property,
        intent_id=intent.pk,
        provider_code=intent.provider_code,
        provider_transaction_id=payment.provider_ref,
        amount=payment.amount,
        currency=payment.currency,
        channel_status="SUCCESS",
        event_id=f"mock-callback:{payment.provider_ref}",
        signature_verified=bool(signature_verified),
    )
    confirm_intent_success(
        property_obj=payment.property,
        intent_id=intent.pk,
        provider_transaction_id=payment.provider_ref,
        confirmed_payment=payment,
        actor=None,
    )


def create_mock_payment_intent(reservation, idempotency_key=""):
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
            payment = pending_payment
        else:
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
        # WB-088：保留旧 Payment 创建的同时，同一事务内创建/复用支付意图，
        # 使公开支付入口真正产生 PaymentIntent/ProviderTransaction 事实。
        _ensure_payment_intent_for_payment(payment, idempotency_key=idempotency_key)
        return payment


@transaction.atomic
def process_mock_payment_callback(*, provider_ref, amount, status,
                                  signature_verified=False):
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaymentConflict("支付金额格式不正确。") from exc
    if status not in {"SUCCESS", "FAILED"}:
        raise PaymentConflict("不支持的模拟支付结果。")
    # WB-076/089（第 32 轮）：服务层自身必须把「已验签」作为 SUCCESS 资金
    # 事实的必要前置条件——不能依赖 HTTP 入口的验签，也不能只靠事后
    # _promote_payment_intent_on_channel_success 拦截意图推进（彼时
    # Payment/库存/账本已写入）。任何未验签的 SUCCESS 在写入任何资金
    # 事实前一律拒绝。
    if status == "SUCCESS" and not signature_verified:
        raise PaymentConflict(
            "SUCCESS 回调必须提供验签通过的渠道事实"
            "（signature_verified=True），拒绝未验签的支付成功。")

    # 全局锁顺序必须为 Reservation -> Payment -> Inventory Hold。
    # 先做无锁关联读取取得 reservation_id，再锁预订，最后锁支付行并复核关联；
    # 不得先锁 Payment 再锁 Reservation，否则会与取消/过期路径形成反向死锁。
    payment_probe = Payment.objects.filter(
        provider_ref=provider_ref
    ).values("reservation_id").first()
    if payment_probe is None:
        raise PaymentConflict("支付流水不存在。")
    try:
        reservation = Reservation.objects.select_for_update().get(
            pk=payment_probe["reservation_id"]
        )
    except Reservation.DoesNotExist as exc:
        raise PaymentConflict("支付流水关联的预订不存在。") from exc
    try:
        payment = (
            Payment.objects.select_for_update()
            .select_related("folio", "property")
            .get(provider_ref=provider_ref, reservation=reservation)
        )
    except Payment.DoesNotExist as exc:
        # 关联在无锁探测与加锁之间发生变化时，拒绝继续处理。
        raise PaymentConflict("支付流水不存在或关联预订已变化。") from exc

    if amount != payment.amount:
        raise PaymentConflict("回调金额与支付流水金额不一致。")
    if payment.status == Payment.Status.SUCCEEDED:
        # 幂等重放必须返回与首次处理一致的业务结果，不能把此前已进入补偿
        # 的迟到成功错误报告成“无需补偿”。
        from apps.core.models import AuditEvent
        compensation_required = AuditEvent.objects.filter(
            action="billing.payment_late_success_requires_reconciliation",
            entity_type="Payment",
            entity_id=str(payment.pk),
        ).exists()
        reservation_confirmed = reservation.status in (
            Reservation.Status.CONFIRMED,
            Reservation.Status.CHECKED_IN,
            Reservation.Status.CHECKED_OUT,
        )
        return {
            "duplicate": True,
            "expired": bool(
                reservation.expires_at and reservation.expires_at <= timezone.now()
            ),
            "compensation_required": compensation_required,
            "reservation_confirmed": reservation_confirmed,
            "payment": payment,
            "reservation": reservation,
        }
    if payment.status != Payment.Status.PENDING and not (
            status == "SUCCESS" and payment.status in (
                Payment.Status.FAILED, Payment.Status.CANCELLED)):
        raise PaymentConflict("当前支付流水状态不接受该回调结果。")

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
            # WB-098：在线支付回调确认是 PENDING→CONFIRMED 的正式确认路径，
            # 必须在确认事实落库的同一事务内保存提前离店政策快照，否则该
            # 渠道订单恒走 legacy NONE 分支，RatePlan 已配置的政策不生效。
            from apps.reservations.front_desk_services import snapshot_early_departure_policy
            snapshot_early_departure_policy(reservation)
        except InventoryUnavailable:
            if expired:
                reservation.status = Reservation.Status.EXPIRED
                reservation.save(update_fields=("status", "updated_at"))
            # 未过期但 Hold 已丢失同样不能回滚渠道 SUCCESS；
            # 保留订单原状态并标记补偿，避免伪造支付失败。
            compensation_required = True
    else:
        # 取消/过期等终态订单不再恢复库存，成功支付进入补偿处理。
        compensation_required = True

    if compensation_required:
        from apps.core.models import AuditEvent
        AuditEvent.record(
            action="billing.payment_late_success_requires_reconciliation",
            entity_type="Payment",
            entity_id=str(payment.pk),
            property_obj=payment.property,
            after={
                "payment_id": payment.pk,
                "reservation_id": reservation.pk,
                "payment_status": payment.status,
                "reservation_status": reservation.status,
                "provider_ref": payment.provider_ref,
                "amount": str(payment.amount),
                "currency": payment.currency,
            },
            reason="渠道已确认支付成功，但原预订已终态或库存 Hold 无法确认；必须人工/自动补偿核对。",
        )
        # WB-087 修复：迟到成功不能只写审计——必须在同一事务内落一条持久化
        # 补偿任务，由 payment_compensation_worker 消费做库存/预订协调；
        # 幂等键绑定 Payment，渠道重放回调不会重复建任务。
        task, task_created = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.HOLD_COORDINATION,
            aggregate_type="Payment",
            aggregate_id=payment.pk,
            idempotency_key=f"late-success:payment:{payment.pk}",
        )
        if task_created:
            # 模型无独立 payload 字段：触发原因与定位信息写入错误摘要，
            # 由 worker 按聚合标识（Payment）解析预订后协调库存。
            task.last_error_code = "LATE_SUCCESS_REQUIRES_RECONCILIATION"
            task.last_error_summary = (
                "迟到成功需库存/预订协调：渠道已确认支付成功，但原预订已终态"
                f"或库存 Hold 无法确认；payment={payment.provider_ref} "
                f"reservation={reservation.pk} "
                f"reservation_status={reservation.status}。"
            )
            task.save(update_fields=("last_error_code", "last_error_summary"))

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
        # WB-111：关账日豁免必须显式绑定渠道支付事实（回调路径为 mock
        # 回调/迟到成功），由 post_entry 校验归属/状态/币种/金额/引用。
        channel_payment=payment,
    )
    # WB-088：渠道成功事实同步推进 PaymentIntent 状态机（同一事务），
    # 保证公开入口创建的意图与 Payment/渠道交易事实不再脱节。
    _promote_payment_intent_on_channel_success(
        payment, signature_verified=signature_verified)
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
    """WB-079 收尾：旧冲销入口已退役，不得再直接写 LedgerEntry。

    该遗留实现绕过 BillingService.post_entry() 的营业日关账守卫（WB-111）
    与红字冲销审批链，且不写 approval_record，在 MySQL 上必然被
    ck_ledger_reversal_approval_link 约束拒绝。仓库内无生产调用点，
    保留函数签名仅为导入兼容，调用一律明确引导到受控入口。
    """
    raise PaymentConflict(
        "该旧冲销入口已退役：请调用 BillingService.reverse_entry()，"
        "它要求当前物业已批准的冲销审批记录、执行权限与申请/审批/执行"
        "职责分离，并是关账日调整的唯一合法账务写入链路。")
