"""通用 Outbox 服务层（TASK-05 / Q3 批准方向：MySQL Outbox + Django Worker）。

职责（Model/Service 优先，业务规则服务端执行）：
- enqueue_outbox_event：供业务代码在自身事务内调用——同事务写入
  OutboxEvent，业务回滚则事件随之回滚（事务性发件箱核心保证）；
- lease_outbox_events：transaction.atomic + select_for_update(skip_locked=True)
  批量认领 PENDING/到期 RETRY 事件，置 PROCESSING + locked_at + lease_owner
  （MySQL 行锁互斥保证两个 worker 不会重复认领；SQLite 下 select_for_update
  为 no-op，测试不依赖锁语义，并发权威验证由主会话在 MySQL 侧执行）；
- complete_outbox_event：成功 → SUCCEEDED；失败 → attempt_count+1，未超
  max_attempts → RETRY + 指数退避（基数 30s，2^n），超限 → DEAD + last_error；
- recover_stale_events：locked_at 过期的 PROCESSING 事件重置回 PENDING
  （Worker 崩溃后的租约恢复）。

消费者注册表 OUTBOX_CONSUMERS：dict[event_type, callable(event)]。治理约束
（NS-E-05）：新增消费者不得引入新权限码；第一批只落结构化日志，不接任何
高风险资金/夜审业务动作。预留三个业务事件类型常量，handler 先落日志。
"""

import logging
import os
import socket
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import OutboxEvent

logger = logging.getLogger("hotel.core.outbox")

# ── 事件类型常量（第一批只预留 + 一个低风险内置消费者）──────────────────
# 低风险内置消费者：无业务副作用，仅结构化日志（治理：不引入新权限码）。
OUTBOX_NOP = "outbox.nop"
# 预留的业务事件类型（handler 先落结构化日志，不接资金/夜审等高风险动作）：
OUTBOX_RESERVATION_STATUS_CHANGED = "reservation.status_changed"
OUTBOX_MEMBERSHIP_CHANGED = "membership.changed"
OUTBOX_NOTIFICATION_DELIVER = "notification.deliver"

# 指数退避基数（秒）：第 n 次失败后退避 30 * 2^n 秒。
OUTBOX_BACKOFF_BASE_SECONDS = 30

# 任务书 V1.1 §十二.10 强制约束：Worker 不得自动重跑已完成的账务、退款、
# 支付或夜审操作。该边界必须在入队阶段硬拒绝（注释不构成运行时保护），
# 否则任何调用方都能先把事件写成持久化事实、再由 Worker 重放业务动作。
HIGH_RISK_EVENT_PREFIXES = (
    "billing.",
    "night_audit.",
    "business_date.reopen",
    "reservation.checkout",
)

# 事件风险级别登记表（OWNER-09 裁定交付物，PMS2-FINAL-DECISION-EXEC-002 批次 C）。
# 唯一事实源：入队端与 Worker 消费端共用同一登记表判定风险，杜绝两端口径漂移。
# 级别语义：
#   LOW         普通领域通知/状态同步，Worker 可自动消费；
#   FORBIDDEN   高危动作（账务/支付/退款/夜审/营业日关闭/退房）——入队硬拒绝、
#               消费端再校验拒绝，此类操作只能走领域服务 + ApprovalService 审批链，
#               不得经通用自动处理路径（含直接写库绕过入队门禁）。
# 新增事件类型必须先在此登记并注明风险依据，未登记类型按 FORBIDDEN 处置
# （入队拒绝、消费退避），防止「先写库后补登记」绕过门禁。
OUTBOX_EVENT_RISK_REGISTRY = {
    OUTBOX_NOP: "LOW",
    OUTBOX_RESERVATION_STATUS_CHANGED: "LOW",
    OUTBOX_MEMBERSHIP_CHANGED: "LOW",
    OUTBOX_NOTIFICATION_DELIVER: "LOW",
}


def _is_high_risk_event(event_type):
    """事件类型是否属于禁止 Worker 自动重放的高风险动作。

    判定顺序：先查登记表（已登记 → 按登记级别），未登记 → 按高危前缀
    兜底（历史高危类型即使未登记也拒绝）。"""
    normalized = str(event_type or "").strip().lower()
    level = OUTBOX_EVENT_RISK_REGISTRY.get(normalized)
    if level is not None:
        return level == "FORBIDDEN"
    return any(normalized.startswith(prefix) for prefix in HIGH_RISK_EVENT_PREFIXES)


def default_lease_owner():
    """租约身份：worker:{hostname}:{pid}，用于租约校验与事故排查。"""
    return f"worker:{socket.gethostname()}:{os.getpid()}"


def enqueue_outbox_event(*, event_type, aggregate_type, aggregate_id, payload, property_obj=None):
    """在调用方事务内追加一条 PENDING 事件（事务性发件箱）。

    必须在业务事务内调用：本函数自身用 transaction.atomic 包裹——外层已有
    事务时并入（savepoint），无外层事务时独立提交；业务事务回滚时事件随之
    回滚，保证「业务事实」与「事件事实」同生同灭（单一权威事实源）。
    property_obj 为预留参数：当前 OutboxEvent 未设物业外键，多物业过滤
    需求落地时再升级（保留签名稳定，避免调用方将来改动）。
    """
    if not event_type or not str(event_type).strip():
        raise ValueError("Outbox 事件必须提供 event_type。")
    if not aggregate_type or not str(aggregate_type).strip():
        raise ValueError("Outbox 事件必须提供 aggregate_type。")
    if not str(aggregate_id).strip():
        raise ValueError("Outbox 事件必须提供 aggregate_id。")
    if _is_high_risk_event(event_type):
        raise ValueError(
            f"高风险事件 {event_type} 禁止进入 Outbox：Worker 不得自动重放"
            "账务/退款/支付/夜审等已完成动作，此类操作必须走其领域服务与审批链。"
        )
    if payload is None:
        payload = {}
    with transaction.atomic():
        return OutboxEvent.objects.create(
            event_type=str(event_type)[:64],
            aggregate_type=str(aggregate_type)[:64],
            aggregate_id=str(aggregate_id)[:64],
            payload=payload,
            status=OutboxEvent.Status.PENDING,
        )


def lease_outbox_events(limit, lease_owner, lease_seconds=60):
    """批量认领 PENDING / 到期 RETRY 事件，置 PROCESSING 并记录租约。

    MySQL 下 transaction.atomic + select_for_update(skip_locked=True) 保证
    并发 worker 认领互斥（已被锁定的行直接跳过，不阻塞、不重复认领）；
    SQLite 下 select_for_update 为 no-op，单线程测试语义不变。

    返回已认领的事件列表（每条带 locked_at/lease_owner）。
    """
    now = timezone.now()
    with transaction.atomic():
        candidates = (
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(status__in=(OutboxEvent.Status.PENDING, OutboxEvent.Status.RETRY))
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("id")[: max(1, int(limit))]
        )
        leased = list(candidates)
        for event in leased:
            event.status = OutboxEvent.Status.PROCESSING
            event.locked_at = now
            event.lease_owner = lease_owner
        if leased:
            OutboxEvent.objects.bulk_update(leased, ("status", "locked_at", "lease_owner"))
    return leased


def complete_outbox_event(event_id, success, error="", lease_owner=""):
    """回写事件执行结果（租约持有者校验，旧 worker 不得覆盖新 worker）。

    - success=True → SUCCEEDED + processed_at；
    - success=False 且 attempt_count 已达 max_attempts → DEAD + last_error；
    - 否则 → attempt_count+1、RETRY、指数退避 next_attempt_at
      （基数 30s，2^n；第 1 次失败退避 60s，第 2 次 120s，依此类推）。

    租约不匹配 / 状态已流转时抛 ValueError，调用方按边界处理。
    """
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().filter(pk=event_id).first()
        if event is None:
            raise ValueError("Outbox 事件不存在。")
        if not event.lease_owner:
            raise ValueError("事件尚未被认领（无租约持有者），不可提交结果。")
        if not lease_owner:
            raise ValueError("提交结果必须声明 lease_owner。")
        if event.lease_owner != lease_owner:
            raise ValueError(
                f"租约持有者不匹配：当前 {event.lease_owner}，调用方 {lease_owner}。"
                "旧 Worker 不得覆盖新 Worker 状态。"
            )
        if event.status != OutboxEvent.Status.PROCESSING:
            raise ValueError("事件不在 PROCESSING 状态（已终态或已退回），拒绝重复提交。")
        if success:
            event.status = OutboxEvent.Status.SUCCEEDED
            event.processed_at = timezone.now()
            event.last_error = ""
            event.save(update_fields=("status", "processed_at", "last_error"))
            return event
        # 失败：先计一次尝试，再判断是否超限
        event.attempt_count += 1
        if event.attempt_count >= event.max_attempts:
            event.status = OutboxEvent.Status.DEAD
            event.last_error = str(error)[:500]
            event.processed_at = timezone.now()
            event.save(update_fields=("status", "attempt_count", "last_error", "processed_at"))
            return event
        backoff = OUTBOX_BACKOFF_BASE_SECONDS * (2**event.attempt_count)
        event.status = OutboxEvent.Status.RETRY
        event.last_error = str(error)[:500]
        event.next_attempt_at = timezone.now() + timedelta(seconds=backoff)
        event.save(update_fields=("status", "attempt_count", "last_error", "next_attempt_at"))
        return event


def recover_stale_events(lease_seconds=60):
    """把 locked_at 过期（租约超时）的 PROCESSING 事件重置回 PENDING。

    Worker 崩溃恢复路径：认领后 worker 进程死亡 → 事件滞留 PROCESSING，
    租约到期后由下一轮 recover_stale_events 重置，可被重新认领。
    返回重置的事件数量。
    """
    cutoff = timezone.now() - timedelta(seconds=max(1, int(lease_seconds)))
    with transaction.atomic():
        stale = list(
            OutboxEvent.objects.select_for_update(skip_locked=True).filter(
                status=OutboxEvent.Status.PROCESSING, locked_at__lte=cutoff
            )
        )
        for event in stale:
            event.status = OutboxEvent.Status.PENDING
            event.locked_at = None
            event.lease_owner = ""
        if stale:
            OutboxEvent.objects.bulk_update(stale, ("status", "locked_at", "lease_owner"))
    return len(stale)


# ── 消费者注册表 ────────────────────────────────────────────────────────
def _nop_consumer(event):
    """内置低风险消费者：结构化日志，无任何业务副作用。"""
    logger.info(
        "outbox 事件 #%s 已消费（type=%s aggregate=%s/%s）payload=%s",
        event.pk,
        event.event_type,
        event.aggregate_type,
        event.aggregate_id,
        event.payload,
    )


def _log_only_consumer(event):
    """预留业务事件的先导消费者：只落结构化日志（治理 NS-E-05：
    不引入新权限码、不执行任何业务动作；真实 handler 后续批次再接入）。"""
    logger.info(
        "outbox 预留事件 #%s（type=%s aggregate=%s/%s）待真实消费者接入；"
        "当前仅记录日志，不执行业务动作。payload=%s",
        event.pk,
        event.event_type,
        event.aggregate_type,
        event.aggregate_id,
        event.payload,
    )


# 注册表：dict[event_type, callable(event)]。未注册类型按失败退避处理。
OUTBOX_CONSUMERS = {
    OUTBOX_NOP: _nop_consumer,
    OUTBOX_RESERVATION_STATUS_CHANGED: _log_only_consumer,
    OUTBOX_MEMBERSHIP_CHANGED: _log_only_consumer,
    OUTBOX_NOTIFICATION_DELIVER: _log_only_consumer,
}


def enforce_consumer_risk_gate(event):
    """Worker 消费端二次校验（OWNER-09）：不信任入队端检查结果。

    高危/未登记事件即使绕过 enqueue_outbox_event（直接写库、改名重试、
    历史遗留数据）进入 PROCESSING，也在此拒绝消费。拒绝不消费、不产生
    业务副作用，事件转入 RETRY 记录拒绝原因，等待人工处置。
    返回 None 表示放行；返回错误字符串表示拒绝（调用方按失败处理）。
    """
    normalized = str(event.event_type or "").strip().lower()
    level = OUTBOX_EVENT_RISK_REGISTRY.get(normalized)
    if level is None:
        # 未登记事件：先按高危前缀兜底判定（历史高危类型即使未登记也按高危拒绝），
        # 其余未登记类型一律 fail-closed 拒绝，不因「注册表里没消费者」放行。
        if any(normalized.startswith(prefix) for prefix in HIGH_RISK_EVENT_PREFIXES):
            return (
                f"事件类型 {event.event_type} 命中高危前缀且未登记为低风险，"
                "禁止 Worker 自动重放（OWNER-09），必须走领域服务与审批链。"
            )
        return (
            f"事件类型 {event.event_type} 未登记于风险登记表，"
            "消费端默认拒绝（OWNER-09 fail-closed）。"
        )
    if level == "FORBIDDEN":
        return (
            f"事件类型 {event.event_type} 登记为高危动作，禁止 Worker 自动重放"
            "（OWNER-09），必须走领域服务与审批链。"
        )
    consumer = OUTBOX_CONSUMERS.get(event.event_type)
    if consumer is None:
        return f"已登记低风险事件 {event.event_type} 无消费者，按失败退避。"
    return None
