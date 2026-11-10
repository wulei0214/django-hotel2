# -*- coding: utf-8 -*-
"""支付意图领域服务：创建、状态迁移、渠道窗口策略（D2/D6）。

依据 docs/PMS2支付中心P0P1专项整改需求_2026-11-09.md PAY-P0-01~05：
- 意图与渠道交易分离；
- UNKNOWN 不得直接释放库存，须先查询渠道；
- 渠道窗口由服务端策略控制（客户端不得提交/修改窗口/延长上限）；
- 支付成功与 Hold 过期竞争时以条件迁移保证单一结果。
"""
import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.billing.models import (
    Payment,
    PaymentIntent,
    ProviderTransaction,
    PaymentCompensationTask,
)
from apps.core.models import AuditEvent

# D2：渠道支付窗口策略（服务端配置；客户端不得提交或修改）
CHANNEL_WINDOW_CONFIG = {
    "mock": {
        "initial_wait_seconds": 15 * 60,       # 初始等待 15 分钟
        "max_total_wait_seconds": 30 * 60,     # 最大累计等待 30 分钟
        "max_extensions": 1,                    # 最大延长次数
        "extension_seconds": 5 * 60,           # 每次延长 5 分钟
    },
}
DEFAULT_WINDOW = CHANNEL_WINDOW_CONFIG["mock"]

# WB-076：ProviderTransaction.channel_status 允许取值（渠道事实状态，
# 与意图状态机独立；模型未定义 choices，由此处服务层统一约束）
PROVIDER_CHANNEL_STATUSES = frozenset({
    "PENDING",      # 渠道侧待支付
    "PROCESSING",   # 渠道侧处理中（查询返回）
    "SUCCESS",      # 渠道扣款成功（必须验签）
    "FAILED",       # 渠道扣款失败
    "CLOSED",       # 渠道订单关闭（超时未支付）
    "UNKNOWN",      # 渠道状态未知（须先查询渠道再推进，PAY-P0-02）
})


def get_channel_window(provider_code):
    """D2：读取渠道支付窗口策略（服务端配置，不接受前端传入）。"""
    return CHANNEL_WINDOW_CONFIG.get(provider_code, DEFAULT_WINDOW)


@transaction.atomic
def create_intent(*, property_obj, reservation, folio, amount, currency="CNY",
                  purpose="ROOM_FEE", provider_code="mock", created_by=None,
                  idempotency_key=""):
    """创建支付意图（幂等）。创建时关联有效 Hold 并设置初始窗口。"""
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 96:
        raise ValueError("支付意图必须提供 1 至 96 个字符的幂等键。")
    intent_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if intent_amount <= 0:
        raise ValueError("支付意图金额必须大于零。")

    # WB-008 修复：跨表业务归属一致性校验
    if reservation.property_id != property_obj.pk:
        raise ValueError("预订不属于当前物业。")
    if folio.property_id != property_obj.pk:
        raise ValueError("账单不属于当前物业。")
    if folio.reservation_id != reservation.pk:
        raise ValueError("账单与预订不一致。")
    if folio.currency != reservation.currency:
        raise ValueError("账单币种与预订币种不一致。")
    # WB-071 修复：传入币种必须与账单、预订币种一致（原先只校验账单 vs 预订）
    if currency != folio.currency:
        raise ValueError("支付意图币种与账单币种不一致。")
    if currency != reservation.currency:
        raise ValueError("支付意图币种与预订币种不一致。")
    existing = PaymentIntent.objects.filter(idempotency_key=key).first()
    if existing is not None:
        # WB-069 修复：幂等重放比较补齐 currency/purpose/provider_code
        if (existing.property_id != property_obj.pk
                or existing.reservation_id != reservation.pk
                or existing.amount != intent_amount
                or existing.folio_id != folio.pk
                or existing.currency != currency
                or existing.purpose != purpose
                or existing.provider_code != provider_code):
            raise ValueError("该幂等键已用于不同支付意图。")
        return existing, False

    window = get_channel_window(provider_code)
    now = timezone.now()
    intent = PaymentIntent.objects.create(
        property=property_obj, reservation=reservation, folio=folio,
        intent_no=f"PI-{uuid.uuid4().hex.upper()[:20]}",
        idempotency_key=key,
        amount=intent_amount, currency=currency,
        purpose=PaymentIntent.Purpose.purpose if hasattr(PaymentIntent.Purpose, 'purpose') else purpose,
        provider_code=provider_code,
        provider_config_version="v1",
        status=PaymentIntent.Status.PENDING,
        expires_at=now + timezone.timedelta(seconds=window["initial_wait_seconds"]),
        max_expires_at=now + timezone.timedelta(seconds=window["max_total_wait_seconds"]),
        created_by=created_by)
    AuditEvent.record(
        action="billing.payment_intent_created", entity_type="PaymentIntent",
        entity_id=intent.pk, actor=created_by, property_obj=property_obj,
        after={"intent_no": intent.intent_no, "amount": str(intent_amount),
               "provider_code": provider_code, "expires_at": intent.expires_at.isoformat()},
        reason="创建支付意图")
    return intent, True


@transaction.atomic
def extend_intent_window(*, intent_id, property_obj, extended_by=None):
    """D2：有限延长支付窗口（仅 PENDING/PROCESSING；有上限）。"""
    intent = PaymentIntent.objects.select_for_update().filter(
        pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ValueError("支付意图不存在或不属于当前物业。")
    if intent.status not in (PaymentIntent.Status.PENDING,
                             PaymentIntent.Status.PROCESSING):
        raise ValueError("只有待支付/处理中的意图可以延长窗口。")
    window = get_channel_window(intent.provider_code)
    if intent.extension_count >= window["max_extensions"]:
        raise ValueError("已达到最大延长次数，转入补偿核查。")
    if intent.max_expires_at and intent.expires_at and (
            intent.expires_at + timezone.timedelta(seconds=window["extension_seconds"])
            > intent.max_expires_at):
        raise ValueError("已达到最大累计等待时长，转入补偿核查。")
    intent.extension_count += 1
    intent.expires_at = (intent.expires_at or timezone.now()) + timezone.timedelta(
        seconds=window["extension_seconds"])
    intent.save(update_fields=("extension_count", "expires_at", "updated_at"))
    AuditEvent.record(
        action="billing.intent_window_extended", entity_type="PaymentIntent",
        entity_id=intent.pk, actor=extended_by, property_obj=property_obj,
        after={"extension_count": intent.extension_count,
               "expires_at": intent.expires_at.isoformat()},
        reason="D2 窗口延长")
    return intent


@transaction.atomic
def record_provider_event(*, property_obj, intent_id, provider_code,
                          provider_transaction_id, amount, currency,
                          channel_status, event_id="", signature_verified=False):
    """记录渠道交易事实（event_id 幂等；重复/乱序通知先记录再判断）。"""
    from apps.core.models import AuditEvent as AE
    intent = PaymentIntent.objects.filter(pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ValueError("支付意图不存在或不属于当前物业。")
    existing = ProviderTransaction.objects.filter(
        provider_code=provider_code, event_id=event_id).first() if event_id else None
    if existing is not None:
        # WB-012 修复：命中已有事件时校验物业/意图/交易号/金额/币种一致
        if (existing.property_id != property_obj.pk
                or existing.intent_id != intent.pk
                or existing.provider_transaction_id != provider_transaction_id
                or existing.amount != Decimal(str(amount)).quantize(Decimal("0.01"))
                or existing.currency != currency):
            raise ValueError(
                "事件 ID 已用于不同的物业/意图/交易号/金额/币种，"
                "不能复用已有渠道交易记录。")
        return existing, False
    # WB-076/089 修复：首次写入渠道事实前做完整一致性校验
    pt_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if provider_code != intent.provider_code:
        raise ValueError("渠道代码与支付意图的渠道不一致，不能记录渠道交易事实。")
    if pt_amount != intent.amount:
        raise ValueError("渠道交易金额与支付意图金额不一致。")
    if currency != intent.currency:
        raise ValueError("渠道交易币种与支付意图币种不一致。")
    if channel_status not in PROVIDER_CHANNEL_STATUSES:
        raise ValueError(
            f"渠道状态 {channel_status} 不在允许取值内"
            f"（{sorted(PROVIDER_CHANNEL_STATUSES)}）。")
    if channel_status == "SUCCESS" and not signature_verified:
        raise ValueError(
            "渠道状态为 SUCCESS 时必须提供验签通过结果"
            "（signature_verified=True），拒绝未验签的成功事实。")
    pt = ProviderTransaction.objects.create(
        property=property_obj, intent=intent,
        provider_code=provider_code,
        provider_transaction_id=provider_transaction_id,
        event_id=event_id, amount=pt_amount,
        currency=currency, channel_status=channel_status,
        signature_verified=signature_verified)
    AE.record(
        action="billing.provider_event_recorded", entity_type="ProviderTransaction",
        entity_id=pt.pk, property_obj=property_obj,
        after={"provider_transaction_id": provider_transaction_id,
               "channel_status": channel_status, "event_id": event_id},
        reason="记录渠道交易事实")
    return pt, True


@transaction.atomic
def confirm_intent_success(*, property_obj, intent_id, provider_transaction_id,
                           confirmed_payment=None, actor=None):
    """渠道确认成功 → 意图 SUCCEEDED + 关联 Payment + Hold 确认（同事务）。

    竞态仲裁：本方法与 Hold 过期释放共用数据库行锁，保证渠道成功后
    Hold 不被过期任务释放（PAY-P0-04）。
    """
    from apps.inventory.services import confirm_inventory_for_reservation
    from apps.inventory.services import InventoryUnavailable
    intent = PaymentIntent.objects.select_for_update().filter(
        pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ValueError("支付意图不存在或不属于当前物业。")
    if intent.status == PaymentIntent.Status.SUCCEEDED:
        return intent, False
    if intent.status in (PaymentIntent.Status.FAILED,
                         PaymentIntent.Status.EXPIRED,
                         PaymentIntent.Status.CANCELLED):
        raise ValueError("已终态意图不可确认成功。")
    if confirmed_payment is None:
        raise ValueError(
            "WB-011：确认成功必须关联真实 Payment；"
            "不得产生意图显示成功但没有对应收款事实的状态。")
    # WB-072 修复：确认前对收款事实做完整归属/金额/币种/状态/占用校验
    if confirmed_payment.status != Payment.Status.SUCCEEDED:
        raise ValueError("确认成功必须关联 SUCCEEDED 状态的收款事实。")
    if (confirmed_payment.property_id != intent.property_id
            or confirmed_payment.reservation_id != intent.reservation_id
            or confirmed_payment.folio_id != intent.folio_id):
        raise ValueError("收款事实与支付意图的物业/预订/账单归属不一致。")
    if (confirmed_payment.amount != intent.amount
            or confirmed_payment.currency != intent.currency):
        raise ValueError("收款事实金额或币种与支付意图不一致。")
    if PaymentIntent.objects.filter(confirmed_payment=confirmed_payment).exclude(
            pk=intent.pk).exists():
        raise ValueError("该收款事实已被其他支付意图关联，不可重复确认。")
    intent.confirmed_payment = confirmed_payment
    intent.status = PaymentIntent.Status.SUCCEEDED
    intent.save(update_fields=("status", "confirmed_payment", "updated_at"))
    try:
        confirm_inventory_for_reservation(intent.reservation)
    except InventoryUnavailable:
        # WB-073 修复：Hold 已失效不再静默吞掉——创建持久化补偿任务，
        # 资金事实保留，转入高优先级异常处理（D3 P0）
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.HOLD_COORDINATION,
            aggregate_type="PaymentIntent", aggregate_id=intent.pk,
            idempotency_key=f"intent-inventory-confirm:{intent.pk}")
        # 模型无独立 payload 字段：预订/意图标识与触发原因写入错误摘要，
        # 并落审计事件（create_compensation_task 自身亦审计）
        task.last_error_code = "INVENTORY_CONFIRM_UNAVAILABLE"
        task.last_error_summary = (
            f"库存确认失败：reservation={intent.reservation_id} "
            f"intent={intent.intent_no} 的 Hold 已失效/释放，需高优先级协调")
        task.save(update_fields=("last_error_code", "last_error_summary"))
        AuditEvent.record(
            action="billing.intent_inventory_compensation_created",
            entity_type="PaymentCompensationTask", entity_id=task.pk,
            actor=actor, property_obj=property_obj,
            after={"task_id": task.pk, "intent_no": intent.intent_no,
                   "reservation_id": intent.reservation_id},
            reason="渠道确认成功但库存 Hold 失效，创建 HOLD_COORDINATION 补偿任务")
    AuditEvent.record(
        action="billing.intent_confirmed", entity_type="PaymentIntent",
        entity_id=intent.pk, actor=actor, property_obj=property_obj,
        after={"status": "SUCCEEDED",
               "provider_transaction_id": provider_transaction_id},
        reason="渠道确认成功（PAY-P0-04 竞态仲裁）")
    return intent, True


@transaction.atomic
def expire_intent(*, property_obj, intent_id, actor=None):
    """意图过期（窗口到期且渠道未确认成功方可过期；UNKNOWN 不走此路径）。"""
    intent = PaymentIntent.objects.select_for_update().filter(
        pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ValueError("支付意图不存在或不属于当前物业。")
    if intent.status not in (PaymentIntent.Status.PENDING,
                             PaymentIntent.Status.CREATED):
        raise ValueError("只有待支付状态的意图可以过期。")
    # WB-077/090 修复：必须真实到期才允许过期，防止提前作废未到期意图
    if intent.expires_at is None or intent.expires_at > timezone.now():
        raise ValueError("支付窗口尚未到期，不可将意图标记为过期。")
    intent.status = PaymentIntent.Status.EXPIRED
    intent.save(update_fields=("status", "updated_at"))
    AuditEvent.record(
        action="billing.intent_expired", entity_type="PaymentIntent",
        entity_id=intent.pk, actor=actor, property_obj=property_obj,
        after={"status": "EXPIRED"}, reason="窗口到期未支付")
    return intent


@transaction.atomic
def create_compensation_task(*, task_type, aggregate_type, aggregate_id,
                             idempotency_key, max_attempts=5):
    """创建持久化补偿任务（幂等）。"""
    existing = PaymentCompensationTask.objects.filter(
        idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, False
    task = PaymentCompensationTask.objects.create(
        task_type=task_type, aggregate_type=aggregate_type,
        aggregate_id=aggregate_id, idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        next_attempt_at=timezone.now())
    AuditEvent.record(
        action="billing.compensation_task_created",
        entity_type="PaymentCompensationTask", entity_id=task.pk,
        after={"task_type": task_type, "aggregate_id": aggregate_id},
        reason="创建支付补偿任务（D3 P0）")
    return task, True


@transaction.atomic
def lease_compensation_task(task_id, lease_owner, lease_seconds=60):
    """数据库租约抢占：两个 worker 同时抢占，最多一个有效执行者。"""
    task = PaymentCompensationTask.objects.select_for_update().filter(
        pk=task_id).first()
    if task is None:
        raise ValueError("补偿任务不存在。")
    now = timezone.now()
    if task.status == PaymentCompensationTask.Status.LEASED and (
            task.lease_expires_at and task.lease_expires_at > now):
        raise ValueError("任务已被其他 worker 抢占且租约未到期。")
    if task.status not in (PaymentCompensationTask.Status.PENDING,
                           PaymentCompensationTask.Status.LEASED):
        raise ValueError("只有待执行/已抢占的任务可以租约执行。")
    task.status = PaymentCompensationTask.Status.LEASED
    task.lease_owner = lease_owner
    task.lease_expires_at = now + timezone.timedelta(seconds=lease_seconds)
    task.attempt_count += 1
    task.started_at = now
    task.save(update_fields=("status", "lease_owner", "lease_expires_at",
                             "attempt_count", "started_at"))
    return task


@transaction.atomic
def complete_compensation_task(task_id, success=True, error_code="",
                               error_summary="", lease_owner=""):
    """完成任务：成功或超限进死信。WB-006/WB-067 修复：强制校验租约持有者。"""
    task = PaymentCompensationTask.objects.select_for_update().filter(
        pk=task_id).first()
    if task is None:
        raise ValueError("补偿任务不存在。")
    # WB-067 修复：强制租约校验，空值不得绕过比对——
    # 任务必须已被领取、调用方必须声明身份、两者必须一致
    if not task.lease_owner:
        raise ValueError("任务尚未被领取（无租约持有者），不可提交执行结果。")
    if not lease_owner:
        raise ValueError("完成任务必须声明租约持有者（lease_owner 必填）。")
    if task.lease_owner != lease_owner:
        raise ValueError(
            f"租约持有者不匹配：当前 {task.lease_owner}，调用方 {lease_owner}。"
            "旧 Worker 不得覆盖新 Worker 的执行状态。")
    # WB-067：租约未过期才可提交；过期视为租约失效，拒绝提交。
    # 任务已不在 LEASED 状态的合法路径（退避重试/重复收尾）无活跃租约可校验
    if task.status == PaymentCompensationTask.Status.LEASED and (
            task.lease_expires_at is None
            or task.lease_expires_at <= timezone.now()):
        raise ValueError("租约已过期，视为租约失效，拒绝提交执行结果。")
    if success:
        task.status = PaymentCompensationTask.Status.SUCCEEDED
        task.completed_at = timezone.now()
        task.save(update_fields=("status", "completed_at"))
        return task
    if task.attempt_count >= task.max_attempts:
        task.status = PaymentCompensationTask.Status.DEAD_LETTER
        task.completed_at = timezone.now()
    else:
        # 指数退避
        backoff = min(2 ** task.attempt_count * 30, 300)
        task.status = PaymentCompensationTask.Status.PENDING
        task.next_attempt_at = timezone.now() + timezone.timedelta(seconds=backoff)
    task.last_error_code = error_code[:64]
    task.last_error_summary = error_summary[:500]
    task.save(update_fields=("status", "completed_at", "last_error_code",
                             "last_error_summary", "next_attempt_at"))
    return task


@transaction.atomic
def replay_dead_letter_task(task_id, operator, reason=""):
    """人工重放死信任务：复用原意图幂等键，不新建第二笔支付事实。"""
    task = PaymentCompensationTask.objects.select_for_update().filter(
        pk=task_id).first()
    if task is None:
        raise ValueError("补偿任务不存在。")
    if task.status != PaymentCompensationTask.Status.DEAD_LETTER:
        raise ValueError("只有死信任务可以人工重放。")
    task.status = PaymentCompensationTask.Status.PENDING
    task.attempt_count = 0
    task.next_attempt_at = timezone.now()
    task.save(update_fields=("status", "attempt_count", "next_attempt_at"))
    AuditEvent.record(
        action="billing.compensation_task_replayed",
        entity_type="PaymentCompensationTask", entity_id=task.pk,
        after={"status": "PENDING"},
        reason=str(reason or "人工重放")[:240])
    return task
