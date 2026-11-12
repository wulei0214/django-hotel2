# -*- coding: utf-8 -*-
"""支付/账务维护调度入口（生产可达性收尾）。

此前以下能力虽已实现，但没有任何生产调用方：
  * ``expire_intent()``：支付意图窗口到期后永不进入 EXPIRED；
  * ``scan_folio_reconciliation()``：对账扫描仅被测试调用；
  * ``CALLBACK_CONFIRM_RETRY`` / ``PAYMENT_LEDGER_REPAIR`` /
    ``RECONCILIATION_DIFF`` 三类补偿任务：有执行器但无创建方。

本命令串起「到期意图过期 → 卡住的渠道成功回调重试 → 账务对账差异转
可租约补偿任务」，补偿任务由 ``payment_compensation_worker`` 消费。
所有创建均通过幂等键去重，重复调度不会产生重复任务。

用法：
  python manage.py run_billing_maintenance
  python manage.py run_billing_maintenance --property DEFAULT --only recon
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing.models import (
    FolioReconciliationDiff,
    PaymentCompensationTask,
    PaymentIntent,
    ProviderTransaction,
)
from apps.billing.payment_intents import (
    create_compensation_task,
    expire_intent,
)
from apps.billing.reconciliation import scan_folio_reconciliation
from apps.core.models import Property


LEDGER_REPAIR_DIFF_TYPES = (
    FolioReconciliationDiff.DiffType.PAYMENT_NO_ENTRY,
    FolioReconciliationDiff.DiffType.DEPOSIT_NO_ENTRY,
    FolioReconciliationDiff.DiffType.REFUND_NO_ENTRY,
    FolioReconciliationDiff.DiffType.ADJUSTMENT_NO_ENTRY,
)


class Command(BaseCommand):
    help = "支付意图过期 / 回调确认重试 / 账务对账差异转补偿任务"

    def add_arguments(self, parser):
        parser.add_argument("--property", dest="property_code", default=None,
                            help="只处理指定物业 code；缺省处理全部物业。")
        parser.add_argument("--only", default="all",
                            choices=("all", "expire", "stuck", "recon"),
                            help="只执行某一类维护动作。")

    def handle(self, *args, **options):
        only = options["only"]
        props = Property.objects.all().order_by("pk")
        if options["property_code"]:
            props = props.filter(code=options["property_code"])
        totals = {"expired": 0, "unknown": 0, "callback": 0,
                  "ledger_repair": 0, "recon_diff": 0, "errors": 0}
        for prop in props:
            if only in ("all", "expire"):
                self._expire_intents(prop, totals)
            if only in ("all", "stuck"):
                self._stuck_callbacks(prop, totals)
            if only in ("all", "recon"):
                self._reconcile(prop, totals)
        self.stdout.write(self.style.SUCCESS(
            "支付/账务维护完成：%(expired)s 个意图过期，%(unknown)s 个渠道状态未知"
            "（已转 CHANNEL_QUERY），%(callback)s 个回调确认重试，"
            "%(ledger_repair)s 个账本修复，%(recon_diff)s 个对账差异任务，"
            "%(errors)s 个错误。" % totals))

    def _expire_intents(self, prop, totals):
        due = list(PaymentIntent.objects.filter(
            property=prop,
            status__in=(PaymentIntent.Status.CREATED,
                        PaymentIntent.Status.PENDING),
            expires_at__lte=timezone.now(),
        ).order_by("pk").values_list("pk", flat=True))
        for intent_id in due:
            try:
                expire_intent(property_obj=prop, intent_id=intent_id)
                totals["expired"] += 1
            except ValueError:
                # WB-090：渠道 UNKNOWN 路径由 expire_intent 在独立事务中
                # 落 CHANNEL_QUERY 任务后抛出，属于预期分支。
                totals["unknown"] += 1
            except Exception as exc:  # 单个意图失败不阻断其它物业/意图
                totals["errors"] += 1
                self.stderr.write(f"意图 {intent_id} 过期失败：{exc}")

    def _stuck_callbacks(self, prop, totals):
        """渠道已成功验签但意图未达 SUCCEEDED → 回调确认重试任务。"""
        stuck = (ProviderTransaction.objects.filter(
            property=prop, channel_status="SUCCESS", signature_verified=True,
            intent__isnull=False)
            .exclude(intent__status=PaymentIntent.Status.SUCCEEDED)
            .values_list("intent_id", flat=True).distinct())
        for intent_id in stuck:
            _, created = create_compensation_task(
                task_type=PaymentCompensationTask.TaskType.CALLBACK_CONFIRM_RETRY,
                aggregate_type="PaymentIntent", aggregate_id=intent_id,
                idempotency_key=f"callback-confirm-retry:{intent_id}")
            if created:
                totals["callback"] += 1

    def _reconcile(self, prop, totals):
        try:
            run = scan_folio_reconciliation(property_obj=prop)
        except Exception as exc:
            totals["errors"] += 1
            self.stderr.write(f"物业 {prop.code} 对账扫描失败：{exc}")
            return
        for diff in run.diffs.all().order_by("pk"):
            if diff.diff_type in LEDGER_REPAIR_DIFF_TYPES:
                task_type = PaymentCompensationTask.TaskType.PAYMENT_LEDGER_REPAIR
                counter = "ledger_repair"
                aggregate_type = "Folio"
                aggregate_id = diff.folio_id
            else:
                task_type = PaymentCompensationTask.TaskType.RECONCILIATION_DIFF
                counter = "recon_diff"
                aggregate_type = "FolioReconciliationRun"
                aggregate_id = run.pk
            _, created = create_compensation_task(
                task_type=task_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                # 稳定键：同一实体的同一类差异只建一次任务，重复调度不刷屏；
                # 修复后新扫描不再产生该 diff，任务完成后由人工归档/重放。
                idempotency_key=(f"{diff.diff_type}:{diff.folio_id}:"
                                 f"{diff.entity_type}:{diff.entity_id}"))
            if created:
                totals[counter] += 1
