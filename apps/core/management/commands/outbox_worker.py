"""通用 Outbox Worker（TASK-05 / Q3 批准方向：MySQL Outbox + Django Worker）。

每轮循环：
1. recover_stale_events：先把租约过期的 PROCESSING 事件重置回 PENDING
   （Worker 崩溃恢复，不丢弃任何事件）；
2. lease_outbox_events：以 select_for_update(skip_locked=True) 批量认领
   PENDING/到期 RETRY 事件（MySQL 行锁互斥；SQLite 下为 no-op）；
3. 逐条分派给 OUTBOX_CONSUMERS 注册表中的消费者执行，按结果回写
   SUCCEEDED / RETRY（指数退避）/ DEAD（超限死信）。

消费者治理（NS-E-05）：注册表中的消费者不得引入新权限码；第一批只落
结构化日志，不执行任何业务动作（真实业务消费者后续批次接入）。

用法：
  python manage.py outbox_worker --once            # 单轮处理后退出
  python manage.py outbox_worker --interval 30     # 循环轮询（默认 30s）

空转（无到期事件）时保持安静，不产生输出（对齐 payment_compensation_worker）。
"""

import logging
import time

from django.core.management.base import BaseCommand

from apps.core.outbox import (
    OUTBOX_CONSUMERS,
    complete_outbox_event,
    default_lease_owner,
    lease_outbox_events,
    recover_stale_events,
)

logger = logging.getLogger("hotel.core.outbox")


def dispatch_outbox_event(event):
    """执行单个已认领事件：查注册表分派消费者，返回 (success, error)。

    任何单事件异常都不得拖垮整轮处理：异常统一按失败回写，交给
    complete_outbox_event 做退避/死信迁移。
    """
    consumer = OUTBOX_CONSUMERS.get(event.event_type)
    if consumer is None:
        return False, f"未注册的事件类型 {event.event_type}（无消费者），按失败退避。"
    try:
        consumer(event)
        return True, ""
    except Exception as exc:  # 兜底防御：单事件异常不影响后续事件
        logger.exception("outbox 事件 %s 消费异常。", event.pk)
        return False, str(exc)[:200]


def run_worker_cycle(*, lease_owner, lease_seconds=60, limit=50):
    """单轮处理：恢复过期租约 → 认领 → 分派执行 → 回写结果。

    返回摘要 dict；空转（无认领事件）返回全 0，调用方据此保持安静。
    """
    recovered = recover_stale_events(lease_seconds=lease_seconds)
    events = lease_outbox_events(limit=limit, lease_owner=lease_owner, lease_seconds=lease_seconds)
    summary = {"recovered": recovered, "claimed": 0, "succeeded": 0, "retried": 0, "dead": 0}
    for event in events:
        summary["claimed"] += 1
        success, error = dispatch_outbox_event(event)
        try:
            done = complete_outbox_event(
                event.pk, success=success, error=error, lease_owner=lease_owner
            )
        except ValueError as exc:
            # 租约在执行期间过期等边界：本轮结果丢弃，事件等租约到期后恢复
            logger.warning("outbox 事件 %s 回写结果失败：%s", event.pk, exc)
            summary["retried"] += 1
            continue
        if done.status == "SUCCEEDED":
            summary["succeeded"] += 1
        elif done.status == "DEAD":
            summary["dead"] += 1
        else:
            summary["retried"] += 1
    return summary


class Command(BaseCommand):
    help = (
        "通用 Outbox Worker（TASK-05）：恢复过期租约后批量认领 PENDING/到期 "
        "RETRY 事件，分派给 OUTBOX_CONSUMERS 注册表执行；失败走指数退避"
        "（基数 30s，2^n），超过 max_attempts 转死信等待人工排查。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="只执行一轮处理后退出（调试/测试用；默认循环运行）。",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="循环轮询间隔秒数（默认 30，最小 1）。",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="单轮最多处理事件数（默认 50，最小 1）。",
        )
        parser.add_argument(
            "--lease-seconds",
            type=int,
            default=60,
            help="租约时长秒数（默认 60，最小 1）。",
        )

    def handle(self, *args, **options):
        interval = max(1, int(options["interval"]))
        lease_owner = default_lease_owner()
        try:
            while True:
                summary = run_worker_cycle(
                    lease_owner=lease_owner,
                    lease_seconds=max(1, int(options["lease_seconds"])),
                    limit=max(1, int(options["limit"])),
                )
                # 空转安静：仅在有事件被恢复/认领时输出一轮摘要
                if summary["recovered"] or summary["claimed"]:
                    self.stdout.write(
                        self.style.SUCCESS(
                            "Outbox 本轮：恢复过期租约 {recovered}，认领 "
                            "{claimed}，成功 {succeeded}，退避 {retried}，"
                            "死信 {dead}。".format(**summary)
                        )
                    )
                if options["once"]:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:  # 运维中断：干净退出
            self.stdout.write("收到中断信号，Outbox Worker 退出。")
