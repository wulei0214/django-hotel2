# -*- coding: utf-8 -*-
"""D3 P0 / WB-070/078：支付补偿任务 Worker。

循环扫描到期的 PaymentCompensationTask（PENDING，或租约已过期的 LEASED），
以数据库租约抢占后按任务类型分派执行：
- HOLD_COORDINATION（库存确认类）：按聚合标识解析出预订，调用
  apps.inventory.services.confirm_inventory_for_reservation 做幂等库存确认；
- 其余任务类型（CHANNEL_QUERY / CALLBACK_CONFIRM_RETRY 等）暂无自动执行器：
  先记日志，再按失败处理进入服务层指数退避，超过 max_attempts 由
  complete_compensation_task 转入 DEAD_LETTER，人工可通过
  replay_dead_letter_task 重放。

用法：
  python manage.py payment_compensation_worker --once          # 单轮扫描后退出
  python manage.py payment_compensation_worker --interval 30   # 循环轮询（默认 30s）

空转（无到期任务）时保持安静，不产生输出。
"""
import logging
import os
import socket
import time

from django.core.management.base import BaseCommand
from django.db import models
from django.utils import timezone

from apps.billing.models import (
    Payment,
    PaymentCompensationTask,
    PaymentIntent,
)
from apps.billing.payment_intents import (
    complete_compensation_task,
    lease_compensation_task,
)
from apps.inventory.services import (
    InventoryUnavailable,
    confirm_inventory_for_reservation,
)

logger = logging.getLogger("hotel.billing.compensation")

ERROR_UNSUPPORTED_TASK_TYPE = "UNSUPPORTED_TASK_TYPE"
ERROR_INVENTORY_CONFIRM_UNAVAILABLE = "INVENTORY_CONFIRM_UNAVAILABLE"
ERROR_AGGREGATE_MISSING = "AGGREGATE_MISSING"
ERROR_WORKER_EXCEPTION = "WORKER_EXCEPTION"


def default_lease_owner():
    """租约身份：worker:{hostname}:{pid}，用于租约强校验与事故排查。"""
    return f"worker:{socket.gethostname()}:{os.getpid()}"


def resolve_hold_coordination_reservation(task):
    """HOLD_COORDINATION：由聚合标识解析需协调库存的预订。

    PaymentCompensationTask 没有独立 payload 字段，定位信息由
    aggregate_type/aggregate_id 承载：
    - aggregate_type="Payment"       → 迟到成功补偿（WB-087）
    - aggregate_type="PaymentIntent" → 意图库存确认失败补偿（WB-073）
    """
    if task.aggregate_type == "Payment":
        payment = Payment.objects.filter(pk=task.aggregate_id).first()
        if payment is None:
            raise LookupError(f"聚合实体不存在：Payment {task.aggregate_id}。")
        return payment.reservation
    if task.aggregate_type == "PaymentIntent":
        intent = PaymentIntent.objects.filter(pk=task.aggregate_id).first()
        if intent is None:
            raise LookupError(f"聚合实体不存在：PaymentIntent {task.aggregate_id}。")
        return intent.reservation
    raise LookupError(f"不支持的聚合类型：{task.aggregate_type}。")


def execute_compensation_task(task):
    """执行单个已租约任务，返回 (success, error_code, error_summary)。

    任何单任务异常都不得拖垮整轮扫描；InventoryUnavailable 等失败统一
    交给 complete_compensation_task 做退避/死信迁移。
    """
    try:
        if task.task_type == PaymentCompensationTask.TaskType.HOLD_COORDINATION:
            reservation = resolve_hold_coordination_reservation(task)
            # True=Hold 已消耗并确认；False=预订早已确认（幂等空跑）——都算成功
            confirm_inventory_for_reservation(reservation)
            return True, "", ""
        # 其他任务类型目前没有自动执行器：先记日志，再按失败处理进入退避，
        # 超过 max_attempts 转死信，等待人工排查后 replay_dead_letter_task 重放。
        logger.warning(
            "补偿任务 %s 类型 %s 暂无自动执行器，按失败退避。",
            task.pk, task.task_type)
        return (False, ERROR_UNSUPPORTED_TASK_TYPE,
                f"任务类型 {task.task_type} 暂无自动执行器，需人工处理。")
    except InventoryUnavailable as exc:
        return False, ERROR_INVENTORY_CONFIRM_UNAVAILABLE, str(exc)
    except LookupError as exc:
        return False, ERROR_AGGREGATE_MISSING, str(exc)
    except Exception as exc:  # 兜底防御：单任务异常不影响后续任务
        logger.exception("补偿任务 %s 执行异常。", task.pk)
        return False, ERROR_WORKER_EXCEPTION, str(exc)[:200]


def run_worker_cycle(*, lease_owner, lease_seconds=60, limit=50):
    """单轮扫描：到期任务 → 租约抢占 → 分派执行 → 回写结果。

    返回摘要 dict；空转（无到期任务）返回全 0，调用方据此保持安静。
    """
    now = timezone.now()
    candidates = PaymentCompensationTask.objects.filter(
        status__in=(PaymentCompensationTask.Status.PENDING,
                    PaymentCompensationTask.Status.LEASED),
    ).filter(
        models.Q(next_attempt_at__isnull=True)
        | models.Q(next_attempt_at__lte=now)
    ).order_by("next_attempt_at", "created_at")[:limit]
    summary = {"scanned": 0, "succeeded": 0, "retried": 0, "skipped": 0}
    for candidate in candidates:
        summary["scanned"] += 1
        try:
            leased = lease_compensation_task(
                candidate.pk, lease_owner, lease_seconds=lease_seconds)
        except ValueError:
            # 已被其他 worker 抢占（租约未到期）或状态不允许租约：安静跳过
            summary["skipped"] += 1
            continue
        success, error_code, error_summary = execute_compensation_task(leased)
        try:
            complete_compensation_task(
                leased.pk, success=success, error_code=error_code,
                error_summary=error_summary, lease_owner=lease_owner)
        except ValueError as exc:
            # 租约在执行期间过期等边界：本轮结果丢弃，任务等租约到期后被重新抢占
            logger.warning("补偿任务 %s 回写结果失败：%s", leased.pk, exc)
            summary["skipped"] += 1
            continue
        if success:
            summary["succeeded"] += 1
        else:
            summary["retried"] += 1
    return summary


class Command(BaseCommand):
    help = ("支付补偿任务 Worker（D3 P0 / WB-070/078）：租约抢占到期补偿任务并"
            "按类型分派执行；失败走指数退避，超过上限转死信等待人工重放。")

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true",
            help="只执行一轮扫描后退出（调试/测试用；默认循环运行）。")
        parser.add_argument(
            "--interval", type=int, default=30,
            help="循环轮询间隔秒数（默认 30，最小 1）。")
        parser.add_argument(
            "--limit", type=int, default=50,
            help="单轮最多处理任务数（默认 50，最小 1）。")
        parser.add_argument(
            "--lease-seconds", type=int, default=60,
            help="租约时长秒数（默认 60，最小 1）。")

    def handle(self, *args, **options):
        interval = max(1, int(options["interval"]))
        lease_owner = default_lease_owner()
        try:
            while True:
                summary = run_worker_cycle(
                    lease_owner=lease_owner,
                    lease_seconds=max(1, int(options["lease_seconds"])),
                    limit=max(1, int(options["limit"])))
                # 空转安静：仅在有任务被扫描时输出一轮摘要
                if summary["scanned"]:
                    self.stdout.write(self.style.SUCCESS(
                        "补偿任务本轮：扫描 {scanned}，成功 {succeeded}，"
                        "退避/失败 {retried}，跳过 {skipped}。".format(**summary)))
                if options["once"]:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:  # 运维中断：干净退出
            self.stdout.write("收到中断信号，补偿 Worker 退出。")
