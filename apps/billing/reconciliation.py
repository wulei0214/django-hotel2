# -*- coding: utf-8 -*-
"""FIN-P0 账本与 Folio/负债一致性对账服务（只读核对，不自动改写）。

依据 docs/PMS2财务账务与营业日P0P1专项整改需求_2026-11-09.md：
- 核对 Payment ↔ PAYMENT 凭证、Deposit ↔ DEPOSIT 凭证、Refund ↔ REFUND 凭证；
- 核对已执行 BillingAdjustment ↔ ADJUSTMENT 凭证（WB-092）；
- 已关闭 Folio 余额重建：账本凭证合计 vs 期望余额（WB-096）；
- RefundLiability 净额重建：remaining_amount vs amount − 已成功退款
  − 已执行调整净效应（复用 dec02.liability_net_snapshot 口径，WB-096）；
- 差异不能自动改写账本或 Folio，生成差异报告供授权财务处理。
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import (
    BillingAdjustment,
    ChargeLine,
    CheckoutExceptionRelease,
    Folio,
    FolioReconciliationDiff,
    FolioReconciliationRun,
    LedgerEntry,
    Payment,
    Refund,
    RefundLiability,
)
from apps.core.models import AuditEvent

# WB-092：原始收款凭证核对覆盖全部"资金已到账/已动用"状态——不只
# SUCCEEDED；退款相关终态（REFUND_PENDING/PARTIALLY_REFUNDED/REFUNDED）
# 同样必须能追溯到原始收款凭证，否则退款来源不可审计。
PAYMENT_ENTRY_CHECK_STATUSES = (
    Payment.Status.SUCCEEDED,
    Payment.Status.REFUND_PENDING,
    Payment.Status.PARTIALLY_REFUNDED,
    Payment.Status.REFUNDED,
)


def scan_folio_reconciliation(*, property_obj, checked_by=None, batch_size=200):
    """FIN-P0：全量扫描账务一致性差异（只读核对，不自动改写）。

    核对项：
    1. 每笔成功/已进入退款流程的 Payment/Deposit 必须有对应
       PAYMENT/DEPOSIT 凭证（引用精确匹配，且核对凭证类型、金额方向、
       币种与物业归属；缺失或内容不符报差异，WB-092）。
    2. 每笔成功 Refund 必须有对应 REFUND 凭证（引用精确匹配——不用
       无边界前缀匹配，防止 refund:R-10 误配 refund:R-100；同时核对
       类型/金额/币种，WB-092）。
    3. 每笔已执行 BillingAdjustment 必须有对应 ADJUSTMENT 凭证
       （reference=adjustment:{幂等键}，金额与调整方向一致）。
    4. 凭证科目行核对：LedgerEntry 上挂了 ChargeLine 的，科目行合计
       必须等于凭证金额（模型层规则 sum(ChargeLine.amount) =
       LedgerEntry.amount，WB-092 第 32 轮补齐）。
    5. 已关闭 Folio 余额重建：账本凭证合计 == −(应退给客人的净额)。
       应退净额 = 负债净额（liability_net_snapshot 口径）+ 无负债关联的
       已执行冲销调整。经审批异常放行（B2 离店核销）造成的负余额属于
       账外事实，跳过余额核对，不视为差异。
    6. RefundLiability 净额重建：remaining_amount == amount − 已成功退款
       − 已执行抵扣 + 已执行增加（未执行/未批准调整不计入）。
    """
    from apps.billing.dec02 import liability_net_snapshot
    run = FolioReconciliationRun.objects.create(
        property=property_obj, checked_by=checked_by)
    diff_count = 0
    folios = list(Folio.objects.filter(
        property=property_obj).order_by("pk").values_list("pk", flat=True))
    scanned = 0

    for chunk_start in range(0, len(folios), batch_size):
        chunk = folios[chunk_start:chunk_start + batch_size]
        for folio_id in chunk:
            folio = Folio.objects.filter(pk=folio_id).first()
            if folio is None:
                continue
            scanned += 1
            # WB-092（第 32 轮）：凭证核对需要类型/金额/币种/物业全量字段，
            # 不能只按引用判断存在性；引用建立精确索引（不做前缀匹配）。
            entries = list(LedgerEntry.objects.filter(folio=folio).values(
                "pk", "entry_type", "amount", "reference", "currency",
                "property_id"))
            entries_by_ref = {}
            for e in entries:
                entries_by_ref.setdefault(e["reference"], []).append(e)
            ledger_total = sum(
                (Decimal(str(e["amount"])) for e in entries),
                start=Decimal("0.00"))
            payments = list(Payment.objects.filter(
                folio=folio, status__in=PAYMENT_ENTRY_CHECK_STATUSES))
            for p in payments:
                if p.provider == "frontdesk_deposit":
                    expected_ref = f"deposit:{p.provider_ref}"
                    expected_type = LedgerEntry.EntryType.DEPOSIT
                    diff_type = FolioReconciliationDiff.DiffType.DEPOSIT_NO_ENTRY
                else:
                    expected_ref = f"payment:{p.provider_ref}"
                    expected_type = LedgerEntry.EntryType.PAYMENT
                    diff_type = FolioReconciliationDiff.DiffType.PAYMENT_NO_ENTRY
                # 收款/押金凭证为负向入账（金额方向：-payment.amount）。
                expected_amount = -p.amount
                candidates = entries_by_ref.get(expected_ref, [])
                if not candidates:
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio, diff_type=diff_type,
                        entity_type="Payment", entity_id=p.pk,
                        detail=f"Payment {p.pk} ({p.provider_ref}) 缺对应凭证 {expected_ref}")
                    diff_count += 1
                    continue
                # WB-092：凭证存在≠事实正确——逐项核对类型/金额/币种/物业。
                entry = candidates[0]
                problems = []
                if entry["entry_type"] != expected_type:
                    problems.append(
                        f"凭证类型 {entry['entry_type']}，应为 {expected_type}")
                if Decimal(str(entry["amount"])) != expected_amount:
                    problems.append(
                        f"金额 {entry['amount']}，按方向应为 {expected_amount}")
                if entry["currency"] != p.currency:
                    problems.append(
                        f"币种 {entry['currency']}，应为 {p.currency}")
                if entry["property_id"] != p.property_id:
                    problems.append("物业归属不一致")
                if problems:
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio, diff_type=diff_type,
                        entity_type="Payment", entity_id=p.pk,
                        detail=(f"Payment {p.pk} ({p.provider_ref}) 凭证 "
                                f"{expected_ref} 与业务事实不符："
                                + "；".join(problems)))
                    diff_count += 1
            refunds = list(Refund.objects.filter(
                payment__folio=folio, status=Refund.Status.SUCCEEDED))
            for rf in refunds:
                # WB-092：退款凭证引用精确匹配——无边界前缀匹配会把
                # refund:R-100 误配给 refund:R-10，漏报缺凭证差异。
                expected_ref = f"refund:{rf.provider_ref}"
                candidates = entries_by_ref.get(expected_ref, [])
                if not candidates:
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio,
                        diff_type=FolioReconciliationDiff.DiffType.REFUND_NO_ENTRY,
                        entity_type="Refund", entity_id=rf.pk,
                        detail=f"Refund {rf.pk} ({rf.provider_ref}) 缺对应 REFUND 凭证")
                    diff_count += 1
                    continue
                entry = candidates[0]
                problems = []
                # 退款凭证为正向出账（金额方向：+refund.amount）。
                if entry["entry_type"] != LedgerEntry.EntryType.REFUND:
                    problems.append(
                        f"凭证类型 {entry['entry_type']}，应为 REFUND")
                if Decimal(str(entry["amount"])) != rf.amount:
                    problems.append(
                        f"金额 {entry['amount']}，应为 {rf.amount}")
                if entry["currency"] != rf.currency:
                    problems.append(
                        f"币种 {entry['currency']}，应为 {rf.currency}")
                if problems:
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio,
                        diff_type=FolioReconciliationDiff.DiffType.REFUND_NO_ENTRY,
                        entity_type="Refund", entity_id=rf.pk,
                        detail=(f"Refund {rf.pk} ({rf.provider_ref}) 凭证 "
                                f"{expected_ref} 与业务事实不符："
                                + "；".join(problems)))
                    diff_count += 1

            # WB-092（第 32 轮）：凭证科目行核对——挂了 ChargeLine 的凭证，
            # 科目行合计必须等于凭证金额（模型层规则）。
            line_sums = {}
            for row in ChargeLine.objects.filter(
                    ledger_entry__folio=folio).values(
                    "ledger_entry_id").annotate(total=Sum("amount")):
                line_sums[row["ledger_entry_id"]] = row["total"]
            for e in entries:
                if e["pk"] not in line_sums:
                    continue  # 未拆科目的凭证（如收款/退款）不参与本核对
                line_total = Decimal(str(line_sums[e["pk"]]))
                if line_total != Decimal(str(e["amount"])):
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio,
                        diff_type=FolioReconciliationDiff.DiffType.CHARGE_LINES_MISMATCH,
                        entity_type="LedgerEntry", entity_id=e["pk"],
                        detail=(f"凭证 {e['pk']}（{e['entry_type']}）科目行合计 "
                                f"{line_total} 与凭证金额 {e['amount']} 不一致"))
                    diff_count += 1

            # WB-092：已执行调整 ↔ ADJUSTMENT 凭证（抵扣为正、回增为负）。
            executed_adjustments = list(BillingAdjustment.objects.filter(
                folio=folio, status=BillingAdjustment.Status.EXECUTED))
            for adj in executed_adjustments:
                expected_ref = f"adjustment:{adj.idempotency_key}"
                expected_amount = adj.amount
                if adj.adjustment_type != BillingAdjustment.AdjustmentType.CHARGE_ADD:
                    expected_amount = -adj.amount
                matched = next(
                    (e for e in entries if e["reference"] == expected_ref), None)
                if matched is None:
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio,
                        diff_type=FolioReconciliationDiff.DiffType.ADJUSTMENT_NO_ENTRY,
                        entity_type="BillingAdjustment", entity_id=adj.pk,
                        detail=(f"已执行调整 {adj.pk}（{adj.adjustment_type}）"
                                f"缺对应 ADJUSTMENT 凭证 {expected_ref}"))
                    diff_count += 1
                elif Decimal(str(matched["amount"])) != expected_amount:
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio,
                        diff_type=FolioReconciliationDiff.DiffType.ADJUSTMENT_NO_ENTRY,
                        entity_type="BillingAdjustment", entity_id=adj.pk,
                        detail=(f"已执行调整 {adj.pk} 凭证 {expected_ref} 金额不符："
                                f"凭证 {matched['amount']}，按调整方向应为 {expected_amount}"))
                    diff_count += 1

            liability = RefundLiability.objects.filter(folio=folio).first()

            # WB-096：已关闭 Folio 余额重建（账本合计 vs 期望余额）。
            if folio.status == Folio.Status.CLOSED:
                # B2 异常放行（RELEASED/CLOSED_NO_REFUND）是经审批的账外
                # 核销：关班负余额没有对应账本事实，余额不可重建，跳过。
                written_off = CheckoutExceptionRelease.objects.filter(
                    folio=folio,
                    status__in=(CheckoutExceptionRelease.Status.RELEASED,
                                CheckoutExceptionRelease.Status.CLOSED_NO_REFUND),
                ).exists()
                if not written_off:
                    owed = Decimal("0.00")
                    if liability is not None:
                        owed += liability_net_snapshot(liability)["net_refundable"]
                    # 无负债关联的已执行冲销（CHARGE_REVERSE）同样增加应退，
                    # 其负向凭证体现在账本中，但不在任何负债净额内。
                    owed += sum(
                        (adj.amount for adj in executed_adjustments
                         if adj.refund_liability_id is None
                         and adj.adjustment_type
                         == BillingAdjustment.AdjustmentType.CHARGE_REVERSE),
                        start=Decimal("0.00"))
                    expected_balance = -owed
                    if ledger_total != expected_balance:
                        FolioReconciliationDiff.objects.create(
                            run=run, folio=folio,
                            diff_type=FolioReconciliationDiff.DiffType.BALANCE_MISMATCH,
                            entity_type="Folio", entity_id=folio.pk,
                            detail=(f"已关闭账单余额重建不符：账本凭证合计 "
                                    f"{ledger_total}，期望 {expected_balance}"
                                    f"（=-应退净额 {owed}）"))
                        diff_count += 1

            # WB-096：RefundLiability 净额重建（复用 liability_net_snapshot 口径；
            # 未执行/未批准调整不计入）。
            if liability is not None:
                rebuilt_net = liability_net_snapshot(liability)["net_refundable"]
                if liability.remaining_amount != rebuilt_net:
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio,
                        diff_type=FolioReconciliationDiff.DiffType.LIABILITY_NET_MISMATCH,
                        entity_type="RefundLiability", entity_id=liability.pk,
                        detail=(f"负债 {liability.pk} 剩余额 {liability.remaining_amount}"
                                f" 与重建净额 {rebuilt_net} 不符"
                                f"（原始 {liability.amount} − 已成功退款 − 已执行抵扣"
                                " + 已执行增加）"))
                    diff_count += 1

    run.scanned_folios = scanned
    run.diff_count = diff_count
    run.save(update_fields=("scanned_folios", "diff_count"))
    AuditEvent.record(
        action="billing.folio_reconciliation_scanned",
        entity_type="FolioReconciliationRun", entity_id=run.pk,
        actor=checked_by, property_obj=property_obj,
        after={"scanned_folios": scanned, "diff_count": diff_count},
        reason="FIN-P0 账务对账扫描")
    return run
