"""DEC-02-A1/B2 业务流服务（依据 docs/PMS2_DEC-02业务决策基线_2026-11-09.md）。

A1：原负债冻结；账务调整独立记录，财务发起、值班经理审批、执行生成独立
    凭证并同步负债余额（抵扣减少应退净额 / 冲销·新增应退增加净额）。
B2：无可靠原支付来源或候选余额不足时，经前台申请、值班经理审批后异常放行，
    建立"待财务核查"记录（不是退款负债）；财务核查证据补齐后经受控流程
    转登记正式 RefundLiability 或按审批关闭。

SoD：申请人不得审批自己的调整/放行；重复审批、重复执行、并发执行由
行锁 + 状态机 + 唯一幂等键约束防护；全部状态迁移落 AuditEvent。
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import (
    BillingAdjustment,
    CheckoutExceptionRelease,
    Folio,
    LedgerEntry,
    RefundLiability,
)
from apps.core.models import AuditEvent

ADJUSTMENT_REFERENCE_PREFIX = "adjustment:"


def _money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _release_blocked_amount(folio):
    """账单当前负余额的绝对值（WB-049 金额绑定基准）。

    与 front_desk_services._folio_balance 同口径：余额=账本凭证求和；
    负余额=客人多付/预付超过已入账房费，正是异常放行针对的金额。
    """
    return abs(_money(folio.entries.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")))


def _sync_liability_status(liability, now):
    """按 DEC-02-A1 语义同步负债状态（SETTLED/OPEN/PARTIALLY_SETTLED）。

    WB-093：模型校验（ck_rfl_status 与 save 一致性检查）要求 OPEN/
    PARTIALLY_SETTLED 必须没有 settled_at；余额经批准回增重新 >0 时
    必须清空结清时间，否则保存被拒。原结清时间不丢失——由调用方
    （execute_adjustment）在审计 after.prior_settled_at 中保留。
    """
    if liability.remaining_amount == 0:
        liability.status = RefundLiability.Status.SETTLED
        if liability.settled_at is None:
            liability.settled_at = now
    elif liability.remaining_amount != liability.amount:
        liability.status = RefundLiability.Status.PARTIALLY_SETTLED
        liability.settled_at = None
    else:
        liability.status = RefundLiability.Status.OPEN
        liability.settled_at = None


# ---------------------------------------------------------------------------
# DEC-02-A1：账务调整 / 抵扣
# ---------------------------------------------------------------------------


@transaction.atomic
def request_adjustment(
    *,
    property_obj,
    folio_id,
    adjustment_type,
    amount,
    reason,
    requested_by,
    evidence_note="",
    idempotency_key="",
):
    """财务发起账务调整；仅适用于已关闭账单（退房后调账）。"""
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 96:
        raise ValueError("调整申请必须提供 1 至 96 个字符的幂等键。")
    if adjustment_type not in BillingAdjustment.AdjustmentType.values:
        raise ValueError("调整类型不合法。")
    if reason is None or not str(reason).strip():
        raise ValueError("调整原因必须填写。")
    adj_amount = _money(amount)
    if adj_amount <= 0:
        raise ValueError("调整金额必须大于零。")

    existing = BillingAdjustment.objects.filter(idempotency_key=key).first()
    if existing is not None:
        if (
            existing.property_id != property_obj.pk
            or existing.folio_id != folio_id
            or existing.adjustment_type != adjustment_type
            or existing.amount != adj_amount
        ):
            raise ValueError("该幂等键已用于不同调整申请。")
        return existing, False

    folio = Folio.objects.select_for_update().get(pk=folio_id, property=property_obj)
    reservation = folio.reservation
    if folio.status != Folio.Status.CLOSED:
        raise ValueError("账务调整仅适用于已关闭账单（退房后调账）；未关闭账单请走正常账务流程。")
    if folio.currency != reservation.currency:
        raise ValueError("调整币种必须与预订和账单一致。")
    liability = RefundLiability.objects.filter(folio=folio).first()
    if (
        adjustment_type
        in (
            BillingAdjustment.AdjustmentType.CHARGE_ADD,
            BillingAdjustment.AdjustmentType.REFUND_INCREASE,
        )
        and liability is None
    ):
        raise ValueError("该账单没有待退款负债，不能发起抵扣或增加应退调整。")
    evidence_note = str(evidence_note or "").strip()
    if len(evidence_note) > 500:
        raise ValueError("证据/备注最多 500 字符。")

    adjustment = BillingAdjustment.objects.create(
        property=property_obj,
        reservation=reservation,
        folio=folio,
        refund_liability=liability,
        adjustment_type=adjustment_type,
        amount=adj_amount,
        currency=folio.currency,
        reason=str(reason).strip(),
        evidence_note=evidence_note,
        status=BillingAdjustment.Status.REQUESTED,
        requested_by=requested_by,
        idempotency_key=key,
    )
    AuditEvent.record(
        action="billing.adjustment_requested",
        entity_type="BillingAdjustment",
        entity_id=adjustment.pk,
        actor=requested_by,
        property_obj=property_obj,
        after={
            "adjustment_type": adjustment_type,
            "amount": str(adj_amount),
            "folio_id": folio.pk,
            "refund_liability_id": liability.pk if liability else None,
        },
        reason=str(reason).strip()[:240],
    )
    return adjustment, True


@transaction.atomic
def approve_adjustment(*, property_obj, adjustment_id, approved_by, comment=""):
    """值班经理审批；申请人不得自批。"""
    adjustment = (
        BillingAdjustment.objects.select_for_update()
        .filter(pk=adjustment_id, property=property_obj)
        .first()
    )
    if adjustment is None:
        raise ValueError("账务调整不存在或不属于当前物业。")
    if adjustment.status != BillingAdjustment.Status.REQUESTED:
        raise ValueError("只有待审批的调整可以审批。")
    if requested_by_id(adjustment) == approved_by.pk:
        raise ValueError("申请人不得审批自己的账务调整（职责分离）。")
    adjustment.status = BillingAdjustment.Status.APPROVED
    adjustment.approved_by = approved_by
    adjustment.approved_at = timezone.now()
    adjustment.save(update_fields=("status", "approved_by", "approved_at", "updated_at"))
    AuditEvent.record(
        action="billing.adjustment_approved",
        entity_type="BillingAdjustment",
        entity_id=adjustment.pk,
        actor=approved_by,
        property_obj=property_obj,
        after={"status": adjustment.status, "comment": str(comment)[:200]},
        reason="值班经理审批账务调整",
    )
    return adjustment


@transaction.atomic
def reject_adjustment(*, property_obj, adjustment_id, rejected_by, reason=""):
    """值班经理驳回；驳回不改负债与净额。"""
    adjustment = (
        BillingAdjustment.objects.select_for_update()
        .filter(pk=adjustment_id, property=property_obj)
        .first()
    )
    if adjustment is None:
        raise ValueError("账务调整不存在或不属于当前物业。")
    if adjustment.status != BillingAdjustment.Status.REQUESTED:
        raise ValueError("只有待审批的调整可以驳回。")
    if requested_by_id(adjustment) == rejected_by.pk:
        raise ValueError("申请人不得驳回自己的账务调整（职责分离）。")
    adjustment.status = BillingAdjustment.Status.REJECTED
    adjustment.approved_by = rejected_by
    adjustment.approved_at = timezone.now()
    adjustment.save(update_fields=("status", "approved_by", "approved_at", "updated_at"))
    AuditEvent.record(
        action="billing.adjustment_rejected",
        entity_type="BillingAdjustment",
        entity_id=adjustment.pk,
        actor=rejected_by,
        property_obj=property_obj,
        after={"status": adjustment.status},
        reason=str(reason or "")[:240],
    )
    return adjustment


@transaction.atomic
def execute_adjustment(*, property_obj, adjustment_id, executed_by):
    """执行已批准调整：生成独立凭证 + 同事务更新负债余额（DEC-02-A1）。

    抵扣（CHARGE_ADD）与退款执行都通过负债行锁串行化，不会重复占用余额。
    回增（CHARGE_REVERSE/REFUND_INCREASE）走 sanctioned_increase 受控通道。
    """
    adjustment = (
        BillingAdjustment.objects.select_for_update()
        .filter(pk=adjustment_id, property=property_obj)
        .first()
    )
    if adjustment is None:
        raise ValueError("账务调整不存在或不属于当前物业。")
    if adjustment.status != BillingAdjustment.Status.APPROVED:
        raise ValueError("只有已批准的调整可以执行。")

    liability = None
    if adjustment.refund_liability_id is not None:
        liability = RefundLiability.objects.select_for_update().get(
            pk=adjustment.refund_liability_id
        )

    now = timezone.now()
    # WB-093：原结清时间在余额被批准回增重新打开时会被清空（模型校验要求
    # OPEN/PARTIALLY_SETTLED 无 settled_at）；结清事实经审计 after 保留。
    prior_settled_at = liability.settled_at if liability is not None else None
    ledger_entry = None
    increase = False
    if adjustment.adjustment_type == BillingAdjustment.AdjustmentType.CHARGE_ADD:
        if liability is None:
            raise ValueError("抵扣调整必须关联待退款负债。")
        if liability.remaining_amount < adjustment.amount:
            raise ValueError(
                f"剩余可退款净额 {liability.remaining_amount} 不足以抵扣 " f"{adjustment.amount}。"
            )
        ledger_entry, _ = _post_adjustment_entry(
            property_obj=property_obj,
            folio=adjustment.folio,
            adjustment=adjustment,
            amount=adjustment.amount,
            posted_by=executed_by,
        )
        liability.remaining_amount -= adjustment.amount
        _sync_liability_status(liability, now)
        liability.save(update_fields=("remaining_amount", "status", "settled_at"))
    elif adjustment.adjustment_type in (
        BillingAdjustment.AdjustmentType.CHARGE_REVERSE,
        BillingAdjustment.AdjustmentType.REFUND_INCREASE,
    ):
        # WB-091：回增（费用冲销/新增应退）与抵扣同构——执行即生成独立
        # 负向调整凭证（语义=补偿/增加应退），reference=adjustment:{幂等键}，
        # 并回填 adjustment.ledger_entry；否则负债净额与账本脱钩，对账
        # 无法重建（FIN-P0/WB-092）。sanctioned_increase 受控通道与
        # remaining_amount 递增语义保持不变。
        if (
            adjustment.adjustment_type == BillingAdjustment.AdjustmentType.REFUND_INCREASE
            and liability is None
        ):
            raise ValueError("增加应退调整必须关联待退款负债。")
        ledger_entry, _ = _post_adjustment_entry(
            property_obj=property_obj,
            folio=adjustment.folio,
            adjustment=adjustment,
            amount=-adjustment.amount,
            posted_by=executed_by,
        )
        if liability is not None:
            liability.remaining_amount += adjustment.amount
            increase = True
            _sync_liability_status(liability, now)
            liability.save(
                update_fields=("remaining_amount", "status", "settled_at"),
                sanctioned_increase=increase,
            )

    adjustment.status = BillingAdjustment.Status.EXECUTED
    adjustment.executed_by = executed_by
    adjustment.executed_at = now
    adjustment.ledger_entry = ledger_entry
    adjustment.save(
        update_fields=("status", "executed_by", "executed_at", "ledger_entry", "updated_at")
    )
    AuditEvent.record(
        action="billing.adjustment_executed",
        entity_type="BillingAdjustment",
        entity_id=adjustment.pk,
        actor=executed_by,
        property_obj=property_obj,
        after={
            "adjustment_type": adjustment.adjustment_type,
            "amount": str(adjustment.amount),
            "ledger_entry_id": ledger_entry.pk if ledger_entry else None,
            "liability_remaining": (str(liability.remaining_amount) if liability else None),
            "prior_settled_at": (prior_settled_at.isoformat() if prior_settled_at else None),
        },
        reason="执行账务调整（DEC-02-A1）",
    )
    return adjustment


def _post_adjustment_entry(*, property_obj, folio, adjustment, amount, posted_by):
    """调整凭证：ADJUSTMENT 类型，允许在已关闭账单上追加（受控唯一写路径）。

    凭证号幂等（adjustment:{idempotency_key}），同一调整重复执行不会重复入账。
    """
    from apps.billing.services_v2 import BillingService

    return BillingService.post_entry(
        property_obj=property_obj,
        folio=folio,
        entry_type=LedgerEntry.EntryType.ADJUSTMENT,
        amount=amount,
        currency=folio.currency,
        reference=f"{ADJUSTMENT_REFERENCE_PREFIX}{adjustment.idempotency_key}",
        description=f"账务调整（{adjustment.get_adjustment_type_display()}）：{adjustment.reason}",
        posted_by=posted_by,
    )


def requested_by_id(adjustment):
    return adjustment.requested_by_id


def liability_net_snapshot(liability):
    """DEC-02-A1：净额视图 = 原始负债 − 已执行抵扣 + 已批准增加 − 已成功退款
    通过账本与调整记录完整重建；未审批调整不影响净额。"""
    from apps.billing.models import Refund

    executed = liability.billing_adjustments.filter(status=BillingAdjustment.Status.EXECUTED)
    deducted = sum(
        (
            a.amount
            for a in executed
            if a.adjustment_type == BillingAdjustment.AdjustmentType.CHARGE_ADD
        ),
        start=Decimal("0.00"),
    )
    increased = sum(
        (
            a.amount
            for a in executed
            if a.adjustment_type
            in (
                BillingAdjustment.AdjustmentType.CHARGE_REVERSE,
                BillingAdjustment.AdjustmentType.REFUND_INCREASE,
            )
        ),
        start=Decimal("0.00"),
    )
    refunded = Refund.objects.filter(
        refund_liability=liability, status=Refund.Status.SUCCEEDED
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    pending_refunds = Refund.objects.filter(
        refund_liability=liability, status__in=(Refund.Status.PENDING, Refund.Status.APPROVED)
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    net = liability.amount - deducted + increased - refunded
    return {
        "original_amount": liability.amount,
        "refunded_amount": refunded,
        "pending_refund_amount": pending_refunds,
        "deducted_amount": deducted,
        "increased_amount": increased,
        "net_refundable": net,
    }


# ---------------------------------------------------------------------------
# DEC-02-B2：异常放行与待财务核查
# ---------------------------------------------------------------------------

ACTIVE_RELEASE_STATUSES = (
    CheckoutExceptionRelease.Status.REQUESTED,
    CheckoutExceptionRelease.Status.APPROVED,
    CheckoutExceptionRelease.Status.RELEASED,
)


@transaction.atomic
def request_exception_release(
    *,
    property_obj,
    reservation,
    folio,
    amount,
    reason_code,
    reason_text,
    requested_by,
    idempotency_key="",
):
    """前台发起异常放行申请（结构化原因 + 说明）；同一账单仅一笔在途。"""
    # WB-112：进入事务后第一步锁定账单行——同一 Folio 的并发申请在行锁上
    # 串行化，后续的对象关联校验、幂等键重放、在途申请检查、创建与审计
    # 全部基于锁后重取的 folio 真值执行，杜绝"双在途"竞态。
    try:
        folio = Folio.objects.select_for_update().get(pk=folio.pk, property=property_obj)
    except Folio.DoesNotExist as exc:
        raise ValueError("账单不属于当前物业或不存在，不能发起异常放行。") from exc
    # WB-094：异常放行对象关联核验——账单/预订/物业必须同主且币种一致，
    # 杜绝跨预订 folio、跨物业对象或异币种账单混入放行流程。
    if reservation.property_id != property_obj.pk:
        raise ValueError("预订不属于当前物业，不能发起异常放行。")
    if folio.property_id != property_obj.pk:
        raise ValueError("账单不属于当前物业，不能发起异常放行。")
    if folio.reservation_id != reservation.pk:
        raise ValueError("账单与预订不一致，不能发起异常放行。")
    if folio.currency != reservation.currency:
        raise ValueError("账单币种与预订币种不一致，不能发起异常放行。")
    if reason_code not in CheckoutExceptionRelease.ReasonCode.values:
        raise ValueError("异常原因代码不合法。")
    reason_text = str(reason_text or "").strip()
    if not reason_text:
        raise ValueError("异常放行必须填写结构化异常说明。")
    if len(reason_text) > 500:
        raise ValueError("异常说明最多 500 字符。")
    release_amount = _money(amount)
    if release_amount <= 0:
        raise ValueError("异常放行涉及金额必须大于零。")
    # WB-049：申请金额与账单负余额绑定——放行只能覆盖当前已形成的
    # 负余额（退房时后续房费入账只会使余额更接近 0，不会超过申请时的
    # 负余额绝对值），杜绝"申请 1 元放行走 1 万"的超额放行。
    blocked_amount = _release_blocked_amount(folio)
    if blocked_amount <= 0:
        raise ValueError("当前账单没有负余额（客人未多付），无需异常放行。")
    if release_amount > blocked_amount:
        raise ValueError(
            f"异常放行金额 {release_amount} 超过当前账单负余额 {blocked_amount}，"
            "请按实际负余额发起申请。"
        )
    key = str(idempotency_key or "").strip() or f"checkout-exception:folio:{folio.pk}"

    existing = CheckoutExceptionRelease.objects.filter(idempotency_key=key).first()
    if existing is not None:
        if existing.status in ACTIVE_RELEASE_STATUSES:
            return existing, False
        # 历史申请已终态：允许重新发起，换用新幂等键
        key = f"{key}:{timezone.now().strftime('%Y%m%d%H%M%S')}"

    active = (
        CheckoutExceptionRelease.objects.filter(
            folio=folio,
            status__in=ACTIVE_RELEASE_STATUSES,
        )
        .exclude(idempotency_key=key)
        .first()
    )
    if active is not None:
        raise ValueError("该账单已存在在途异常放行申请，不能重复发起。")

    release = CheckoutExceptionRelease.objects.create(
        property=property_obj,
        reservation=reservation,
        folio=folio,
        amount=release_amount,
        currency=folio.currency,
        reason_code=reason_code,
        reason_text=reason_text,
        status=CheckoutExceptionRelease.Status.REQUESTED,
        requested_by=requested_by,
        idempotency_key=key,
    )
    AuditEvent.record(
        action="billing.checkout_exception_requested",
        entity_type="CheckoutExceptionRelease",
        entity_id=release.pk,
        actor=requested_by,
        property_obj=property_obj,
        after={"reason_code": reason_code, "amount": str(release_amount), "folio_id": folio.pk},
        reason=reason_text[:240],
    )
    return release, True


@transaction.atomic
def approve_exception_release(*, property_obj, release_id, approved_by, comment=""):
    """值班经理审批；申请人不得自批；并发放行由行锁+状态机防护。"""
    release = (
        CheckoutExceptionRelease.objects.select_for_update()
        .filter(pk=release_id, property=property_obj)
        .first()
    )
    if release is None:
        raise ValueError("异常放行申请不存在或不属于当前物业。")
    if release.status != CheckoutExceptionRelease.Status.REQUESTED:
        raise ValueError("只有待审批的异常放行申请可以审批。")
    if release.requested_by_id == approved_by.pk:
        raise ValueError("申请人不得批准自己的异常放行（职责分离）。")
    release.status = CheckoutExceptionRelease.Status.APPROVED
    release.approved_by = approved_by
    release.approved_at = timezone.now()
    release.save(update_fields=("status", "approved_by", "approved_at", "updated_at"))
    # WB-050：审批记录绑定放行金额与审批时点负余额快照——审批事实
    # 必须可追溯到"批准放行多少钱"，而非只记录状态翻转。
    AuditEvent.record(
        action="billing.checkout_exception_approved",
        entity_type="CheckoutExceptionRelease",
        entity_id=release.pk,
        actor=approved_by,
        property_obj=property_obj,
        after={
            "status": release.status,
            "comment": str(comment)[:200],
            "amount": str(release.amount),
            "currency": release.currency,
            "blocked_amount_at_approval": str(_release_blocked_amount(release.folio)),
        },
        reason="值班经理审批异常放行",
    )
    return release


@transaction.atomic
def reject_exception_release(*, property_obj, release_id, rejected_by, reason=""):
    """值班经理驳回异常放行申请。"""
    release = (
        CheckoutExceptionRelease.objects.select_for_update()
        .filter(pk=release_id, property=property_obj)
        .first()
    )
    if release is None:
        raise ValueError("异常放行申请不存在或不属于当前物业。")
    if release.status != CheckoutExceptionRelease.Status.REQUESTED:
        raise ValueError("只有待审批的异常放行申请可以驳回。")
    if release.requested_by_id == rejected_by.pk:
        raise ValueError("申请人不得驳回自己的异常放行（职责分离）。")
    release.status = CheckoutExceptionRelease.Status.REJECTED
    release.approved_by = rejected_by
    release.approved_at = timezone.now()
    release.save(update_fields=("status", "approved_by", "approved_at", "updated_at"))
    AuditEvent.record(
        action="billing.checkout_exception_rejected",
        entity_type="CheckoutExceptionRelease",
        entity_id=release.pk,
        actor=rejected_by,
        property_obj=property_obj,
        after={"status": release.status},
        reason=str(reason or "")[:240],
    )
    return release


@transaction.atomic
def consume_approved_exception_release(
    *, property_obj, folio, stay, amount, actor, trigger_reason=""
):
    """退房被正常负债登记阻断时，消费一笔已批准的异常放行（同事务）。

    返回 release（并置 RELEASED）或 None（无已批准放行 → 调用方维持阻断）。
    放行不创建 RefundLiability，不伪造任何支付/凭证（DEC-02-B2 禁止项）。
    """
    release = (
        CheckoutExceptionRelease.objects.select_for_update()
        .filter(folio=folio, property=property_obj, status=CheckoutExceptionRelease.Status.APPROVED)
        .first()
    )
    if release is None:
        return None
    # WB-094：锁内复核放行记录与当前账单/预订/物业归属一致——查询条件已按
    # folio+property 圈定，这里显式复核三重归属与币种，历史脏数据直接拒绝
    # （不得把 A 单的放行记录消费到 B 单的退房上）。
    if (
        release.property_id != property_obj.pk
        or release.folio_id != folio.pk
        or release.reservation_id != folio.reservation_id
        or release.currency != folio.currency
    ):
        raise ValueError("异常放行记录与当前账单/预订/物业归属不一致，禁止放行。")
    # WB-051：放行金额绑定——退房时实际受阻金额不得超过已批准金额；
    # 超出部分未经审批，保持阻断让前台重新申请（不得以实际金额覆盖
    # 已批金额，否则审批事实被静默篡改）。
    if amount is not None:
        blocked_now = _money(amount)
        if blocked_now > release.amount:
            return None
    release.status = CheckoutExceptionRelease.Status.RELEASED
    release.stay = stay
    release.released_at = timezone.now()
    release.save(update_fields=("status", "stay", "released_at", "updated_at"))
    AuditEvent.record(
        action="billing.checkout_exception_released",
        entity_type="CheckoutExceptionRelease",
        entity_id=release.pk,
        actor=actor,
        property_obj=property_obj,
        after={"status": release.status, "stay_id": stay.pk, "amount": str(release.amount)},
        reason=f"异常放行完成离店；正常负债登记受阻原因：{trigger_reason}"[:240],
    )
    return release


@transaction.atomic
def settle_exception_release(*, property_obj, release_id, settled_by, outcome, verify_note=""):
    """财务核查结案：转正式负债（受控重验）或按审批关闭；记录不可删除。"""
    if outcome not in ("CONVERT_LIABILITY", "CLOSE_NO_REFUND"):
        raise ValueError("核查结案方式不合法。")
    release = (
        CheckoutExceptionRelease.objects.select_for_update()
        .filter(pk=release_id, property=property_obj)
        .first()
    )
    if release is None:
        raise ValueError("异常放行记录不存在或不属于当前物业。")
    if release.status != CheckoutExceptionRelease.Status.RELEASED:
        raise ValueError("只有已放行待核查的记录可以结案。")
    # WB-094：锁内复核放行记录与账单/预订/物业归属一致（register_refund_liability
    # 已有同型校验，这里补齐服务层前置，CLOSE_NO_REFUND 路径同样受保护）。
    release_folio = release.folio
    if (
        release.property_id != property_obj.pk
        or release.reservation_id is None
        or release_folio is None
        or release_folio.property_id != property_obj.pk
        or release_folio.reservation_id != release.reservation_id
        or release_folio.currency != release.reservation.currency
    ):
        raise ValueError("异常放行记录与账单/预订/物业归属或币种不一致，禁止结案。")
    verify_note = str(verify_note or "").strip()
    if not verify_note:
        raise ValueError("结案必须填写财务核查结论。")

    from apps.billing.liabilities import register_refund_liability

    if outcome == "CONVERT_LIABILITY":
        # WB-051：转负债金额绑定实际负余额（核查时点真值），而非照抄
        # 放行申请金额——余额已变化（补录凭证/房费入账）时按实际金额
        # 建负债；实际金额超过已批放行额说明超出审批授权，拒绝转负债。
        actual_blocked = _release_blocked_amount(release.folio)
        if actual_blocked <= 0:
            raise ValueError("当前账单已无负余额，无需转正式负债；请按核查结论关闭。")
        if actual_blocked > release.amount:
            raise ValueError(
                f"当前账单负余额 {actual_blocked} 超过已批准放行金额 "
                f"{release.amount}，超出部分未经审批；请驳回后重新申请放行。"
            )
        # 受控转正式负债：重新验证原支付、账本凭证、金额、币种与物业归属；
        # 不能仅凭人工点击把异常记录变成退款负债（DEC-02-B2 控制要求）。
        liability, created = register_refund_liability(
            property_obj=property_obj,
            reservation=release.reservation,
            folio=release.folio,
            amount=actual_blocked,
            created_by=settled_by,
        )
        release.status = CheckoutExceptionRelease.Status.CONVERTED_LIABILITY
        release.refund_liability = liability
        note_suffix = f"转正式负债 {liability.pk}（新建={created}）"
    else:
        release.status = CheckoutExceptionRelease.Status.CLOSED_NO_REFUND
        note_suffix = "核查无需退款，按审批关闭"
    release.verified_by = settled_by
    release.verified_at = timezone.now()
    release.verify_note = verify_note[:500]
    release.save(
        update_fields=(
            "status",
            "refund_liability",
            "verified_by",
            "verified_at",
            "verify_note",
            "updated_at",
        )
    )
    AuditEvent.record(
        action="billing.checkout_exception_settled",
        entity_type="CheckoutExceptionRelease",
        entity_id=release.pk,
        actor=settled_by,
        property_obj=property_obj,
        after={"status": release.status, "refund_liability_id": release.refund_liability_id},
        reason=f"{verify_note[:200]}；{note_suffix}",
    )
    return release
