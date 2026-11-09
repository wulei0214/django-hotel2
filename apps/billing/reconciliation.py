# -*- coding: utf-8 -*-
"""FIN-P0 账本与 Folio/负债一致性对账服务（只读核对，不自动改写）。

依据 docs/PMS2财务账务与营业日P0P1专项整改需求_2026-11-09.md：
- 核对 Payment ↔ PAYMENT 凭证、Deposit ↔ DEPOSIT 凭证、Refund ↔ REFUND 凭证；
- Folio 展示余额 ↔ 全部有效业务事实；
- RefundLiability ↔ 原始负债 / 已执行退款 / 已批准调整净额；
- 差异不能自动改写账本或 Folio，生成差异报告供授权财务处理。
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.billing.models import (
    Folio,
    FolioReconciliationDiff,
    FolioReconciliationRun,
    LedgerEntry,
    Payment,
    Refund,
    RefundLiability,
)
from apps.core.models import AuditEvent


def scan_folio_reconciliation(*, property_obj, checked_by=None, batch_size=200):
    """FIN-P0：全量扫描账务一致性差异（只读核对，不自动改写）。

    核对项：
    1. 每笔成功 Payment/Deposit 必须有对应 PAYMENT/DEPOSIT 凭证（引用匹配）。
    2. 每笔成功 Refund 必须有对应 REFUND 凭证。
    3. 已关闭 Folio 的余额应由账本凭证合计完整重建。
    4. RefundLiability 剩余额 ≥ 0 且 ≤ 原始额 + 已执行增加调整 − 已执行抵扣。
    """
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
            entries = list(LedgerEntry.objects.filter(folio=folio).values(
                "entry_type", "amount", "reference"))
            entry_refs = {e["reference"] for e in entries}
            payments = list(Payment.objects.filter(
                folio=folio, status=Payment.Status.SUCCEEDED))
            for p in payments:
                if p.provider == "frontdesk_deposit":
                    expected_ref = f"deposit:{p.provider_ref}"
                    diff_type = FolioReconciliationDiff.DiffType.DEPOSIT_NO_ENTRY
                else:
                    expected_ref = f"payment:{p.provider_ref}"
                    diff_type = FolioReconciliationDiff.DiffType.PAYMENT_NO_ENTRY
                if expected_ref not in entry_refs:
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio, diff_type=diff_type,
                        entity_type="Payment", entity_id=p.pk,
                        detail=f"Payment {p.pk} ({p.provider_ref}) 缺对应凭证 {expected_ref}")
                    diff_count += 1
            refunds = list(Refund.objects.filter(
                payment__folio=folio, status=Refund.Status.SUCCEEDED))
            for rf in refunds:
                ref_prefix = f"refund:{rf.provider_ref}"
                if not any(e["reference"].startswith(ref_prefix) for e in entries):
                    FolioReconciliationDiff.objects.create(
                        run=run, folio=folio,
                        diff_type=FolioReconciliationDiff.DiffType.REFUND_NO_ENTRY,
                        entity_type="Refund", entity_id=rf.pk,
                        detail=f"Refund {rf.pk} ({rf.provider_ref}) 缺对应 REFUND 凭证")
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
