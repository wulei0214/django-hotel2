"""D3 P0 / WB-070/078 + WB-110：支付补偿任务 Worker。

循环扫描到期的 PaymentCompensationTask（PENDING，或租约已过期的 LEASED），
以数据库租约抢占后按任务类型分派执行：
- HOLD_COORDINATION（库存确认类）：按聚合标识解析出预订；PENDING 调用
  apps.inventory.services.confirm_inventory_for_reservation 做幂等库存确认；
  已确认/入住/退房为幂等成功；取消/过期/未到店等终态不再重试，直接进死信
  等人工协调（WB-110a）；
- CHANNEL_QUERY：mock 渠道查询适配器——只读取该意图/支付已持久化的
  ProviderTransaction 渠道事实；最新渠道状态 SUCCESS → 任务成功（不改任何
  状态，只汇报）；无交易或非 SUCCESS → 按失败退避重试，绝不伪造渠道成功
  （真实渠道适配器未接入，WB-110b）；
- CALLBACK_CONFIRM_RETRY：找意图的 SUCCESS+已验签 ProviderTransaction，并
  找到按渠道流水号关联的 SUCCEEDED Payment 后调 confirm_intent_success 推进
  意图状态机；找不到支付事实则失败重试，不凭空造成功（WB-110c）；
- PAYMENT_LEDGER_REPAIR：核对 Payment 与账本凭证（PAYMENT payment:{ref} 或
  DEPOSIT deposit:{ref}）；缺失且可安全重建时经 post_entry（WB-111 新口径，
  显式传 channel_payment）补凭证；凭证已存在幂等成功；Payment 非 SUCCEEDED、
  账单已关闭等不可安全重建场景直接进死信注明需人工/对账差异，不得篡改
  （WB-110d）；
- RECONCILIATION_DIFF：调用既有 scan_folio_reconciliation 生成差异报告与
  审计；只读核对，绝不自动改账（WB-110e）；
- 其余未知任务类型：先记日志，再按失败处理进入服务层指数退避，超过
  max_attempts 由 complete_compensation_task 转入 DEAD_LETTER，人工可通过
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
    Folio,
    FolioReconciliationRun,
    LedgerEntry,
    Payment,
    PaymentCompensationTask,
    PaymentIntent,
    ProviderTransaction,
)
from apps.billing.payment_intents import (
    complete_compensation_task,
    confirm_intent_success,
    lease_compensation_task,
)
from apps.core.models import BusinessDate, Property
from apps.inventory.services import (
    InventoryUnavailable,
    confirm_inventory_for_reservation,
)
from apps.reservations.models import Reservation

logger = logging.getLogger("hotel.billing.compensation")

ERROR_UNSUPPORTED_TASK_TYPE = "UNSUPPORTED_TASK_TYPE"
ERROR_INVENTORY_CONFIRM_UNAVAILABLE = "INVENTORY_CONFIRM_UNAVAILABLE"
ERROR_AGGREGATE_MISSING = "AGGREGATE_MISSING"
ERROR_WORKER_EXCEPTION = "WORKER_EXCEPTION"
# WB-110：不可自动恢复的失败——complete_compensation_task 对该错误码
# 跳过退避直接转 DEAD_LETTER（人工协调/对账差异），原因写入错误摘要。
ERROR_REQUIRES_MANUAL_COORDINATION = "REQUIRES_MANUAL_COORDINATION"
ERROR_CHANNEL_QUERY_NO_SUCCESS_FACT = "CHANNEL_QUERY_NO_SUCCESS_FACT"
ERROR_CALLBACK_CONFIRM_NO_PAYMENT_FACT = "CALLBACK_CONFIRM_NO_PAYMENT_FACT"

# WB-110a：Hold 协调视角下"永远无法确认"的预订终态——重试无意义，
# 必须直接进入死信/人工协调，不得无限重试。
HOLD_TERMINAL_RESERVATION_STATUSES = (
    Reservation.Status.CANCELLED,
    Reservation.Status.EXPIRED,
    Reservation.Status.NO_SHOW,
)
# 已成交/在住/离店：库存已占用或无需再消耗 Hold，幂等成功。
HOLD_SETTLED_RESERVATION_STATUSES = (
    Reservation.Status.CONFIRMED,
    Reservation.Status.CHECKED_IN,
    Reservation.Status.CHECKED_OUT,
)


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


def execute_hold_coordination(task):
    """WB-110a：Hold 协调执行器。

    - PENDING：confirm_inventory_for_reservation（成功/失败按现有语义，
      Hold 失效 → INVENTORY_CONFIRM_UNAVAILABLE 退避重试）；
    - CONFIRMED/CHECKED_IN/CHECKED_OUT：库存已成交/占用，幂等成功；
    - CANCELLED/EXPIRED/NO_SHOW 等终态：确认永远不可能成功，不得无限
      重试——返回 REQUIRES_MANUAL_COORDINATION，由服务层直接转死信，
      原因写入摘要，支付资金事实保留。
    """
    reservation = resolve_hold_coordination_reservation(task)
    status = reservation.status
    if status in HOLD_TERMINAL_RESERVATION_STATUSES:
        return (
            False,
            ERROR_REQUIRES_MANUAL_COORDINATION,
            f"预订 {reservation.pk} 已是终态 {status}，库存 Hold 无法再确认，"
            f"不得无限重试；支付资金事实保留，需人工协调（退款/对账）。",
        )
    if status in HOLD_SETTLED_RESERVATION_STATUSES:
        # 幂等成功：库存确认已达成或无需再做（不重复消耗 Hold）
        return (True, "", "")
    # PENDING（及其余可确认状态）：按现有语义执行幂等库存确认。
    # WB-087（第 32 轮）：库存确认成功后必须在同一业务动作内完成预订
    # 状态迁移（PENDING→CONFIRMED）与 WB-098 政策快照——与支付回调
    # 正常确认路径完全一致。只确认库存不迁移状态会让任务"成功"而
    # 订单永远停留 PENDING，随后被维护命令过期、库存/资金事实脱节。
    confirm_inventory_for_reservation(reservation)
    reservation.refresh_from_db()
    if reservation.status == Reservation.Status.PENDING:
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=("status", "updated_at"))
        from apps.reservations.front_desk_services import snapshot_early_departure_policy

        snapshot_early_departure_policy(reservation)
    return (True, "", "")


def _find_intent_for_payment(payment):
    """为 Payment 定位关联 PaymentIntent（优先已确认绑定，其次同事实匹配）。"""
    intent = PaymentIntent.objects.filter(confirmed_payment=payment).order_by("pk").first()
    if intent is not None:
        return intent
    return (
        PaymentIntent.objects.filter(
            reservation_id=payment.reservation_id,
            folio_id=payment.folio_id,
            amount=payment.amount,
            currency=payment.currency,
            provider_code=payment.provider or "mock",
        )
        .order_by("-pk")
        .first()
    )


def _latest_provider_transaction(intent):
    if intent is None:
        return None
    return ProviderTransaction.objects.filter(intent=intent).order_by("-received_at", "-pk").first()


def execute_channel_query(task):
    """WB-110b：渠道查询执行器（mock 适配器）。

    真实渠道适配器未接入：本执行器只读取已持久化的 ProviderTransaction
    渠道事实（PMS 侧记录的回调/查询结果），不发起任何外部请求。
    - 最新渠道状态 SUCCESS → 任务成功；不改意图/支付状态，只汇报渠道事实；
    - 无交易或非 SUCCESS → 按失败退避重试，绝不伪造渠道成功。
    """
    intent = None
    if task.aggregate_type == "PaymentIntent":
        intent = PaymentIntent.objects.filter(pk=task.aggregate_id).first()
        if intent is None:
            raise LookupError(f"聚合实体不存在：PaymentIntent {task.aggregate_id}。")
    elif task.aggregate_type == "Payment":
        payment = Payment.objects.filter(pk=task.aggregate_id).first()
        if payment is None:
            raise LookupError(f"聚合实体不存在：Payment {task.aggregate_id}。")
        intent = _find_intent_for_payment(payment)
    else:
        raise LookupError(f"不支持的聚合类型：{task.aggregate_type}。")
    pt = _latest_provider_transaction(intent)
    if pt is None:
        return (
            False,
            ERROR_CHANNEL_QUERY_NO_SUCCESS_FACT,
            "mock 渠道查询：意图/支付名下无任何已持久化的渠道交易事实，"
            "不伪造渠道成功；按失败退避重试。",
        )
    if pt.channel_status != "SUCCESS":
        return (
            False,
            ERROR_CHANNEL_QUERY_NO_SUCCESS_FACT,
            f"mock 渠道查询：最新渠道交易 {pt.provider_transaction_id} "
            f"状态为 {pt.channel_status}（非 SUCCESS），不伪造渠道成功；"
            "按失败退避重试。",
        )
    # SUCCESS：任务成功。仅汇报渠道事实，不修改意图/支付任何状态。
    return (
        True,
        "",
        f"mock 渠道查询：渠道交易 {pt.provider_transaction_id} "
        f"channel_status=SUCCESS（验签={pt.signature_verified}）；"
        "仅汇报渠道事实，不修改任何状态。",
    )


def execute_callback_confirm_retry(task):
    """WB-110c：回调确认重试执行器。

    找意图的 SUCCESS+已验签 ProviderTransaction；存在按渠道流水号关联的
    SUCCEEDED Payment → confirm_intent_success 推进状态机（含幂等重放）；
    找不到支付事实（或支付非 SUCCEEDED）→ 失败重试，不得凭空造成功。
    """
    if task.aggregate_type != "PaymentIntent":
        raise LookupError(f"不支持的聚合类型：{task.aggregate_type}。")
    intent = PaymentIntent.objects.filter(pk=task.aggregate_id).first()
    if intent is None:
        raise LookupError(f"聚合实体不存在：PaymentIntent {task.aggregate_id}。")
    pt = (
        ProviderTransaction.objects.filter(
            intent=intent,
            channel_status="SUCCESS",
            signature_verified=True,
        )
        .order_by("-received_at", "-pk")
        .first()
    )
    if pt is None:
        return (
            False,
            ERROR_CALLBACK_CONFIRM_NO_PAYMENT_FACT,
            "意图名下没有已验签的 SUCCESS 渠道交易，不能确认成功；" "按失败退避重试。",
        )
    payment = Payment.objects.filter(provider_ref=pt.provider_transaction_id).first()
    if payment is None or payment.status != Payment.Status.SUCCEEDED:
        return (
            False,
            ERROR_CALLBACK_CONFIRM_NO_PAYMENT_FACT,
            f"渠道交易 {pt.provider_transaction_id} 没有对应的 SUCCEEDED "
            "收款事实（Payment），不能凭空造成功；按失败退避重试。",
        )
    confirm_intent_success(
        property_obj=intent.property,
        intent_id=intent.pk,
        provider_transaction_id=pt.provider_transaction_id,
        confirmed_payment=payment,
        actor=None,
    )
    return (True, "", "")


def execute_payment_ledger_repair(task):
    """WB-110d：支付账本修复执行器。

    - 核对 Payment 与账本凭证（PAYMENT payment:{ref} 或 DEPOSIT
      deposit:{ref}）；凭证已存在 → 幂等成功；
    - 缺失且 Payment SUCCEEDED、账单未结 → 经 post_entry 补凭证
      （WB-111 新口径：显式传 channel_payment；营业日取当前物业开放营业日，
      无开放营业日回退自然营业日）；
    - Payment 非 SUCCEEDED、账单已关闭/作废、补写被拒等不可安全重建场景 →
      REQUIRES_MANUAL_COORDINATION 直接进死信，注明需人工/对账差异，
      绝不篡改任何账务事实。
    """
    if task.aggregate_type != "Payment":
        raise LookupError(f"不支持的聚合类型：{task.aggregate_type}。")
    payment = Payment.objects.select_related("folio").filter(pk=task.aggregate_id).first()
    if payment is None:
        raise LookupError(f"聚合实体不存在：Payment {task.aggregate_id}。")
    payment_ref = payment.provider_ref
    candidate_refs = [f"payment:{payment_ref}", f"deposit:{payment_ref}"]
    if LedgerEntry.objects.filter(reference__in=candidate_refs).exists():
        # 幂等成功：凭证已存在，不重复入账
        return (True, "", "")
    if payment.status != Payment.Status.SUCCEEDED:
        return (
            False,
            ERROR_REQUIRES_MANUAL_COORDINATION,
            f"Payment {payment_ref} 状态为 {payment.status}（非 SUCCEEDED），"
            "无法安全重建账本凭证；需人工/对账差异处理，不得篡改。",
        )
    folio = payment.folio
    if folio is None or folio.property_id != payment.property_id:
        return (
            False,
            ERROR_REQUIRES_MANUAL_COORDINATION,
            f"Payment {payment_ref} 对应账单缺失或归属不一致，"
            "无法安全重建账本凭证；需人工/对账差异处理，不得篡改。",
        )
    if folio.status != Folio.Status.OPEN:
        return (
            False,
            ERROR_REQUIRES_MANUAL_COORDINATION,
            f"Payment {payment_ref} 对应账单 {folio.pk} 状态为 "
            f"{folio.status}（非未结），补凭证会影响已结账余额；"
            "需人工/对账差异处理，不得篡改。",
        )
    # 押金登记走 DEPOSIT deposit:{ref}，其余收款走 PAYMENT payment:{ref}
    entry_type = (
        LedgerEntry.EntryType.DEPOSIT
        if payment.provider == "frontdesk_deposit"
        else LedgerEntry.EntryType.PAYMENT
    )
    reference = f"{entry_type.lower()}:{payment_ref}"
    from apps.billing.services_v2 import BillingService

    open_bd = (
        BusinessDate.objects.filter(
            property=payment.property,
            status=BusinessDate.Status.OPEN,
        )
        .order_by("-business_date")
        .first()
    )
    business_date = (
        open_bd.business_date
        if open_bd is not None
        else BillingService._business_date(payment.property)
    )
    try:
        BillingService.post_entry(
            property_obj=payment.property,
            folio=folio,
            entry_type=entry_type,
            amount=-payment.amount,
            currency=payment.currency,
            business_date=business_date,
            reference=reference,
            description=f"补偿修复：支付 {payment_ref} 补入账本凭证",
            posted_by=None,
            # WB-111 新口径：显式绑定渠道支付事实（关账日豁免必需）
            channel_payment=payment,
        )
    except Exception as exc:  # 补写被拒（关账守卫/归属校验等）→ 人工处理
        return (
            False,
            ERROR_REQUIRES_MANUAL_COORDINATION,
            f"补凭证失败（{reference}）：{exc}；需人工/对账差异处理，" "不得篡改。",
        )
    return (True, "", "")


def resolve_reconciliation_property(task):
    """RECONCILIATION_DIFF：由聚合标识解析对账范围的物业。

    WB-120：维护命令创建的对账差异任务统一以 FolioReconciliationRun 为
    聚合（run 自带 property）；此处补齐对应解析分支，创建方与执行方契约
    保持一致，避免任务因聚合类型不被解析而退避进死信。
    """
    if task.aggregate_type == "FolioReconciliationRun":
        run = FolioReconciliationRun.objects.filter(pk=task.aggregate_id).first()
        if run is None:
            raise LookupError(f"聚合实体不存在：FolioReconciliationRun {task.aggregate_id}。")
        return run.property
    if task.aggregate_type == "Property":
        prop = Property.objects.filter(pk=task.aggregate_id).first()
        if prop is None:
            raise LookupError(f"聚合实体不存在：Property {task.aggregate_id}。")
        return prop
    if task.aggregate_type == "Payment":
        payment = Payment.objects.filter(pk=task.aggregate_id).first()
        if payment is None:
            raise LookupError(f"聚合实体不存在：Payment {task.aggregate_id}。")
        return payment.property
    if task.aggregate_type == "PaymentIntent":
        intent = PaymentIntent.objects.filter(pk=task.aggregate_id).first()
        if intent is None:
            raise LookupError(f"聚合实体不存在：PaymentIntent {task.aggregate_id}。")
        return intent.property
    if task.aggregate_type == "Folio":
        folio = Folio.objects.filter(pk=task.aggregate_id).first()
        if folio is None:
            raise LookupError(f"聚合实体不存在：Folio {task.aggregate_id}。")
        return folio.property
    raise LookupError(f"不支持的聚合类型：{task.aggregate_type}。")


def execute_reconciliation_diff(task):
    """WB-110e：对账差异处理执行器——只读核对，绝不自动改账。

    调用既有 scan_folio_reconciliation 生成差异报告与审计事件；扫描异常
    按失败退避重试。
    """
    from apps.billing.reconciliation import scan_folio_reconciliation

    property_obj = resolve_reconciliation_property(task)
    run = scan_folio_reconciliation(property_obj=property_obj)
    return (
        True,
        "",
        f"对账扫描完成：run={run.pk}，扫描账单 {run.scanned_folios}，"
        f"差异 {run.diff_count} 项；差异已生成报告与审计，"
        "等待授权财务处理，绝不自动改账。",
    )


def execute_compensation_task(task):
    """执行单个已租约任务，返回 (success, error_code, error_summary)。

    任何单任务异常都不得拖垮整轮扫描；InventoryUnavailable 等失败统一
    交给 complete_compensation_task 做退避/死信迁移。
    """
    try:
        if task.task_type == PaymentCompensationTask.TaskType.HOLD_COORDINATION:
            return execute_hold_coordination(task)
        if task.task_type == PaymentCompensationTask.TaskType.CHANNEL_QUERY:
            return execute_channel_query(task)
        if task.task_type == PaymentCompensationTask.TaskType.CALLBACK_CONFIRM_RETRY:
            return execute_callback_confirm_retry(task)
        if task.task_type == PaymentCompensationTask.TaskType.PAYMENT_LEDGER_REPAIR:
            return execute_payment_ledger_repair(task)
        if task.task_type == PaymentCompensationTask.TaskType.RECONCILIATION_DIFF:
            return execute_reconciliation_diff(task)
        # 未知任务类型：先记日志，再按失败处理进入退避，超过 max_attempts
        # 转死信，等待人工排查后 replay_dead_letter_task 重放。
        logger.warning("补偿任务 %s 类型 %s 暂无自动执行器，按失败退避。", task.pk, task.task_type)
        return (
            False,
            ERROR_UNSUPPORTED_TASK_TYPE,
            f"任务类型 {task.task_type} 暂无自动执行器，需人工处理。",
        )
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
    candidates = (
        PaymentCompensationTask.objects.filter(
            status__in=(
                PaymentCompensationTask.Status.PENDING,
                PaymentCompensationTask.Status.LEASED,
            ),
        )
        .filter(models.Q(next_attempt_at__isnull=True) | models.Q(next_attempt_at__lte=now))
        .order_by("next_attempt_at", "created_at")[:limit]
    )
    summary = {"scanned": 0, "succeeded": 0, "retried": 0, "skipped": 0}
    for candidate in candidates:
        summary["scanned"] += 1
        try:
            leased = lease_compensation_task(candidate.pk, lease_owner, lease_seconds=lease_seconds)
        except ValueError:
            # 已被其他 worker 抢占（租约未到期）或状态不允许租约：安静跳过
            summary["skipped"] += 1
            continue
        success, error_code, error_summary = execute_compensation_task(leased)
        try:
            complete_compensation_task(
                leased.pk,
                success=success,
                error_code=error_code,
                error_summary=error_summary,
                lease_owner=lease_owner,
            )
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
    help = (
        "支付补偿任务 Worker（D3 P0 / WB-070/078 + WB-110）：租约抢占到期"
        "补偿任务并按类型分派执行（Hold 协调/渠道查询/回调确认重试/账本"
        "修复/对账差异）；失败走指数退避（不可自动恢复直接死信），超过上限"
        "转死信等待人工重放。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="只执行一轮扫描后退出（调试/测试用；默认循环运行）。",
        )
        parser.add_argument(
            "--interval", type=int, default=30, help="循环轮询间隔秒数（默认 30，最小 1）。"
        )
        parser.add_argument(
            "--limit", type=int, default=50, help="单轮最多处理任务数（默认 50，最小 1）。"
        )
        parser.add_argument(
            "--lease-seconds", type=int, default=60, help="租约时长秒数（默认 60，最小 1）。"
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
                # 空转安静：仅在有任务被扫描时输出一轮摘要
                if summary["scanned"]:
                    self.stdout.write(
                        self.style.SUCCESS(
                            "补偿任务本轮：扫描 {scanned}，成功 {succeeded}，"
                            "退避/失败 {retried}，跳过 {skipped}。".format(**summary)
                        )
                    )
                if options["once"]:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:  # 运维中断：干净退出
            self.stdout.write("收到中断信号，补偿 Worker 退出。")
