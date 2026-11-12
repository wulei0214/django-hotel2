# -*- coding: utf-8 -*-
"""白盒回归：收银班次守卫 + FIN-P0 账务对账扫描（WB-092/WB-095/WB-096）。

覆盖：
  * WB-092/WB-096 scan_folio_reconciliation 扩展核对：
      - 原始收款凭证覆盖 REFUND_PENDING/PARTIALLY_REFUNDED/REFUNDED 支付
        与前台押金（缺失报 PAYMENT_NO_ENTRY / DEPOSIT_NO_ENTRY）；
      - 已关闭 Folio 余额重建（账本合计 vs -应退净额；B2 放行核销跳过）；
      - RefundLiability 净额重建（复用 liability_net_snapshot 口径）；
      - 已执行调整（含 REFUND_INCREASE）缺 ADJUSTMENT 凭证报
        ADJUSTMENT_NO_ENTRY。
  * WB-092-旧 CashierShiftMovement.save() 模型层 OUT 余额守卫
    （口径=cash_shift_breakdown）；服务层 record_shift_movement 原路径可用。
  * WB-095 CashierShift RECONCILED 不可达状态已移除。
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import (
    ApprovalRecord,
    BillingAdjustment,
    CashierShift,
    CashierShiftMovement,
    CheckoutExceptionRelease,
    Folio,
    FolioReconciliationDiff,
    LedgerEntry,
    Payment,
    Refund,
    RefundLiability,
)
from apps.billing.reconciliation import scan_folio_reconciliation
from apps.billing.services_v2 import BillingService
from apps.core.models import Property
from apps.guests.models import Guest
from apps.reservations.models import Reservation


def _money(v):
    return Decimal(str(v))


@override_settings(DEBUG=True, DJANGO_ENV="test")
class CashierReconcileTestBase(TestCase):
    """最小夹具：物业 + 客人 + 预订/账单构造器。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.create(
            code="WBRC", name="班次对账白盒酒店", timezone="Asia/Shanghai")
        from apps.inventory.models import RatePlan, RoomType
        cls.room_type = RoomType.objects.create(
            property=cls.prop, code="WBRC-STD", name="班次对账标准间",
            capacity=2)
        cls.rate_plan = RatePlan.objects.create(
            property=cls.prop, code="WBRC-BAR", name="班次对账门市价",
            valid_from=timezone.localdate())
        cls.finance = User.objects.create_user(username="wbrc-finance")
        cls.approver = User.objects.create_user(username="wbrc-approver")
        cls.executor = User.objects.create_user(username="wbrc-executor")
        cls.cashier = User.objects.create_user(username="wbrc-cashier")
        cls.guest = Guest.objects.create(property=cls.prop, full_name="班次对账客人")

    def _make_folio(self, booking_no, total="600.00"):
        reservation = Reservation.objects.create(
            property=self.prop, guest=self.guest, booking_no=booking_no,
            status=Reservation.Status.CHECKED_IN,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, adult_count=1, total_amount=_money(total),
            currency="CNY")
        from apps.reservations.models import ReservationRoom
        ReservationRoom.objects.create(
            property=self.prop, reservation=reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            nightly_rates=[{"date": timezone.localdate().isoformat(),
                            "price": total, "currency": "CNY"}],
            amount=_money(total))
        folio = Folio.objects.create(
            property=self.prop, reservation=reservation, guest=self.guest,
            currency="CNY")
        return reservation, folio

    def _add_payment(self, folio, provider_ref, amount,
                     status=Payment.Status.SUCCEEDED,
                     provider="frontdesk_manual", method="CARD",
                     with_entry=True):
        payment = Payment.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            provider=provider, provider_ref=provider_ref, method=method,
            amount=_money(amount), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        if status != Payment.Status.SUCCEEDED:
            # 支付不得以退款相关状态直接创建；用原生 QuerySet 构造异常场景。
            QuerySet(Payment).filter(pk=payment.pk).update(status=status)
            payment.refresh_from_db()
        if with_entry:
            LedgerEntry.objects.create(
                property=self.prop, folio=folio,
                entry_type=LedgerEntry.EntryType.PAYMENT,
                amount=-_money(amount), currency="CNY",
                business_date=timezone.localdate(),
                reference=f"payment:{provider_ref}", description="预付")
        return payment

    def _add_payment_with_entry(self, folio, provider_ref, amount,
                                provider="frontdesk_manual", method="CARD"):
        return self._add_payment(folio, provider_ref, amount,
                                 provider=provider, method=method,
                                 with_entry=True)

    def _make_liability(self, folio, amount, remaining=None, status=None):
        remaining = _money(remaining if remaining is not None else amount)
        if status is None:
            status = (RefundLiability.Status.OPEN
                      if remaining == _money(amount)
                      else RefundLiability.Status.PARTIALLY_SETTLED)
        liability = RefundLiability.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            amount=_money(amount), remaining_amount=remaining,
            currency="CNY", status=status,
            idempotency_key=f"wbrc-liability-{booking_key(folio)}-{amount}")
        return liability

    def _scan_diffs(self, folio):
        run = scan_folio_reconciliation(property_obj=self.prop,
                                        checked_by=self.finance)
        return list(FolioReconciliationDiff.objects.filter(run=run, folio=folio))


def booking_key(folio):
    return f"folio-{folio.pk}"


class ReconciliationPaymentEntryTests(CashierReconcileTestBase):
    """WB-092：原始收款凭证核对覆盖退款相关支付状态与押金。"""

    def test_partially_refunded_payment_without_entry_reported(self):
        """① PARTIALLY_REFUNDED 支付缺原始凭证被报出。"""
        _, folio = self._make_folio("WBRC-PRF-001")
        self._add_payment(
            folio, "WBRC-PRF-PAY-1", "200.00",
            status=Payment.Status.PARTIALLY_REFUNDED, with_entry=False)
        diffs = self._scan_diffs(folio)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].diff_type,
                         FolioReconciliationDiff.DiffType.PAYMENT_NO_ENTRY)
        self.assertEqual(diffs[0].entity_type, "Payment")
        self.assertIn("payment:WBRC-PRF-PAY-1", diffs[0].detail)

    def test_refunded_payment_without_entry_reported(self):
        """② REFUNDED 支付缺原始凭证被报出。"""
        _, folio = self._make_folio("WBRC-RFD-002")
        self._add_payment(
            folio, "WBRC-RFD-PAY-2", "200.00",
            status=Payment.Status.REFUNDED, with_entry=False)
        diffs = self._scan_diffs(folio)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].diff_type,
                         FolioReconciliationDiff.DiffType.PAYMENT_NO_ENTRY)

    def test_deposit_payment_without_entry_reported(self):
        """③ 押金（frontdesk_deposit）缺 DEPOSIT 凭证被报出。"""
        _, folio = self._make_folio("WBRC-DEP-003")
        Payment.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            provider="frontdesk_deposit",
            provider_ref=f"FDDEP-{self.prop.pk}-WBRC-DEP-003",
            method="CARD", amount=_money("100.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        diffs = self._scan_diffs(folio)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].diff_type,
                         FolioReconciliationDiff.DiffType.DEPOSIT_NO_ENTRY)

    def test_refund_pending_payment_with_entry_not_reported(self):
        """REFUND_PENDING 但凭证齐全 → 不报差异（避免误报回归）。"""
        _, folio = self._make_folio("WBRC-RFP-004")
        self._add_payment(
            folio, "WBRC-RFP-PAY-4", "200.00",
            status=Payment.Status.REFUND_PENDING, with_entry=True)
        diffs = self._scan_diffs(folio)
        self.assertEqual(len(diffs), 0)


class ReconciliationLiabilityAndAdjustmentTests(CashierReconcileTestBase):
    """WB-096 负债净额重建 + WB-092 已执行调整凭证核对。"""

    def test_tampered_liability_remaining_reported(self):
        """④ 负债 300/退款 100/人为改 remaining=250 被报出。"""
        _, folio = self._make_folio("WBRC-LIA-005")
        payment = self._add_payment_with_entry(folio, "WBRC-LIA-PAY-5", "300.00")
        liability = self._make_liability(folio, "300.00")
        liability.source_payments.add(payment)
        refund, created = BillingService.request_refund(
            property_obj=self.prop, payment_id=payment.pk,
            amount="100.00", reason="负债部分退款",
            requested_by=self.finance, idempotency_key="wbrc-lia-rfd-1",
            refund_liability_id=liability.pk)
        self.assertTrue(created)
        BillingService.approve_refund(
            property_obj=self.prop, refund_id=refund.pk,
            approved_by=self.approver)
        BillingService.execute_refund(
            property_obj=self.prop, refund_id=refund.pk,
            executed_by=self.executor, confirm_refund_paid=True,
            execution_reference="BANK-WBRC-LIA-1",
            idempotency_key="wbrc-lia-exec-1")
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, _money("200.00"))
        # 人为篡改 remaining（原生 QuerySet 绕过不可变管理器构造场景）
        QuerySet(RefundLiability).filter(pk=liability.pk).update(
            remaining_amount=_money("250.00"),
            status=RefundLiability.Status.PARTIALLY_SETTLED)
        diffs = self._scan_diffs(folio)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(
            diffs[0].diff_type,
            FolioReconciliationDiff.DiffType.LIABILITY_NET_MISMATCH)
        self.assertEqual(diffs[0].entity_type, "RefundLiability")
        self.assertIn("250.00", diffs[0].detail)
        self.assertIn("200.00", diffs[0].detail)

    def test_refund_increase_adjustment_without_voucher_reported(self):
        """⑤ 已执行 REFUND_INCREASE 缺 ADJUSTMENT 凭证被报出。"""
        _, folio = self._make_folio("WBRC-ADJ-006")
        self._add_payment_with_entry(folio, "WBRC-ADJ-PAY-6", "300.00")
        # 负债净额与"已执行增加 150"一致（remaining=750），
        # 使本测试聚焦凭证缺失这一类差异。
        liability = self._make_liability(folio, "600.00", remaining="750.00")
        adjustment = BillingAdjustment.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            refund_liability=liability,
            adjustment_type=BillingAdjustment.AdjustmentType.REFUND_INCREASE,
            amount=_money("150.00"), currency="CNY",
            reason="投诉补偿（历史执行未落凭证）",
            status=BillingAdjustment.Status.EXECUTED,
            requested_by=self.finance, approved_by=self.approver,
            executed_by=self.executor, executed_at=timezone.now(),
            idempotency_key="wbrc-adj-missing-001")
        diffs = self._scan_diffs(folio)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(
            diffs[0].diff_type,
            FolioReconciliationDiff.DiffType.ADJUSTMENT_NO_ENTRY)
        self.assertEqual(diffs[0].entity_type, "BillingAdjustment")
        self.assertEqual(diffs[0].entity_id, adjustment.pk)
        self.assertIn("adjustment:wbrc-adj-missing-001", diffs[0].detail)

    def test_executed_adjustment_with_voucher_not_reported(self):
        """已执行调整凭证齐全（方向/金额一致）→ 不报差异。"""
        _, folio = self._make_folio("WBRC-ADJ-007")
        self._add_payment_with_entry(folio, "WBRC-ADJ-PAY-7", "300.00")
        liability = self._make_liability(folio, "300.00", remaining="450.00")
        LedgerEntry.objects.create(
            property=self.prop, folio=folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=_money("-150.00"), currency="CNY",
            business_date=timezone.localdate(),
            reference="adjustment:wbrc-adj-ok-001",
            description="补偿增加应退")
        BillingAdjustment.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            refund_liability=liability,
            adjustment_type=BillingAdjustment.AdjustmentType.REFUND_INCREASE,
            amount=_money("150.00"), currency="CNY",
            reason="投诉补偿（凭证齐全）",
            status=BillingAdjustment.Status.EXECUTED,
            requested_by=self.finance, approved_by=self.approver,
            executed_by=self.executor, executed_at=timezone.now(),
            idempotency_key="wbrc-adj-ok-001", ledger_entry=None)
        diffs = self._scan_diffs(folio)
        self.assertEqual(
            [d.diff_type for d in diffs], [],
            f"凭证齐全不应报差异，实际：{[d.detail for d in diffs]}")


class ReconciliationClosedFolioBalanceTests(CashierReconcileTestBase):
    """WB-096：已关闭 Folio 余额重建（账本合计 vs -应退净额）。"""

    def _close_folio(self, folio):
        folio.status = Folio.Status.CLOSED
        folio.save(update_fields=("status",))

    def test_consistent_closed_folio_with_liability_no_diff(self):
        _, folio = self._make_folio("WBRC-BAL-001")
        self._add_payment_with_entry(folio, "WBRC-BAL-PAY-1", "300.00")
        self._make_liability(folio, "300.00")
        self._close_folio(folio)
        diffs = self._scan_diffs(folio)
        self.assertEqual(
            [d.diff_type for d in diffs], [],
            f"一致的已关闭账单不应报差异，实际：{[d.detail for d in diffs]}")

    def test_stray_entry_on_closed_folio_reported(self):
        """已关闭账单出现多余凭证（无负债可解释）→ BALANCE_MISMATCH。"""
        _, folio = self._make_folio("WBRC-BAL-002")
        self._add_payment_with_entry(folio, "WBRC-BAL-PAY-2", "300.00")
        self._make_liability(folio, "300.00")
        self._close_folio(folio)
        LedgerEntry.objects.create(
            property=self.prop, folio=folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=_money("50.00"), currency="CNY",
            business_date=timezone.localdate(),
            reference="stay-room-charge:wbrc-stray",
            description="结账后凭空补记（异常场景）")
        diffs = self._scan_diffs(folio)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].diff_type,
                         FolioReconciliationDiff.DiffType.BALANCE_MISMATCH)
        self.assertEqual(diffs[0].entity_type, "Folio")

    def test_closed_folio_without_liability_reported(self):
        """无负债的已关闭负余额账单（且无放行核销）→ BALANCE_MISMATCH。"""
        _, folio = self._make_folio("WBRC-BAL-003")
        self._add_payment_with_entry(folio, "WBRC-BAL-PAY-3", "600.00")
        self._close_folio(folio)
        diffs = self._scan_diffs(folio)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].diff_type,
                         FolioReconciliationDiff.DiffType.BALANCE_MISMATCH)

    def test_written_off_folio_via_release_is_skipped(self):
        """B2 异常放行核销（RELEASED）的负余额账外事实 → 跳过余额核对。"""
        _, folio = self._make_folio("WBRC-BAL-004")
        self._add_payment_with_entry(folio, "WBRC-BAL-PAY-4", "600.00")
        self._close_folio(folio)
        # 放行核销前：报 BALANCE_MISMATCH
        diffs = self._scan_diffs(folio)
        self.assertEqual([d.diff_type for d in diffs],
                         [FolioReconciliationDiff.DiffType.BALANCE_MISMATCH])
        CheckoutExceptionRelease.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            amount=_money("600.00"), currency="CNY",
            reason_code=CheckoutExceptionRelease.ReasonCode.NO_PAYMENT_SOURCE,
            reason_text="预付款来源无法追溯，经批准异常放行（测试）",
            status=CheckoutExceptionRelease.Status.RELEASED,
            requested_by=self.cashier, approved_by=self.approver,
            released_at=timezone.now(),
            idempotency_key="wbrc-release-004")
        diffs = self._scan_diffs(folio)
        self.assertEqual([d.diff_type for d in diffs], [])


class CashShiftMovementModelGuardTests(CashierReconcileTestBase):
    """WB-092-旧：CashierShiftMovement.save() 模型层 OUT 余额守卫。"""

    def test_direct_out_movement_exceeding_opening_cash_rejected(self):
        """备用金 100 直建 OUT 150 → ValidationError。"""
        shift, _ = BillingService.open_shift(
            property_obj=self.prop, cashier=self.cashier,
            opening_cash=_money("100.00"))
        with self.assertRaises(ValidationError) as ctx:
            CashierShiftMovement.objects.create(
                shift=shift, direction=CashierShiftMovement.Direction.OUT,
                amount=_money("150.00"), reason="超额现金支出（直写绕过）",
                idempotency_key="wbrc-move-over-001", created_by=self.cashier)
        self.assertIn("班次现金余额不足", str(ctx.exception))
        self.assertFalse(CashierShiftMovement.objects.filter(
            shift=shift, idempotency_key="wbrc-move-over-001").exists())

    def test_out_movement_up_to_available_cash_allowed(self):
        """IN 补入后可用现金增加；恰好等于可用现金的 OUT 允许。"""
        shift, _ = BillingService.open_shift(
            property_obj=self.prop, cashier=self.cashier,
            opening_cash=_money("100.00"))
        CashierShiftMovement.objects.create(
            shift=shift, direction=CashierShiftMovement.Direction.IN,
            amount=_money("50.00"), reason="补入备用金",
            idempotency_key="wbrc-move-in-001", created_by=self.cashier)
        movement = CashierShiftMovement.objects.create(
            shift=shift, direction=CashierShiftMovement.Direction.OUT,
            amount=_money("150.00"), reason="全额取出（等于可用现金）",
            idempotency_key="wbrc-move-out-002", created_by=self.cashier)
        self.assertEqual(movement.amount, _money("150.00"))
        # 取空后再支出 0.01 也必须被拒
        with self.assertRaises(ValidationError):
            CashierShiftMovement.objects.create(
                shift=shift, direction=CashierShiftMovement.Direction.OUT,
                amount=_money("0.01"), reason="无余额支出",
                idempotency_key="wbrc-move-out-003", created_by=self.cashier)

    def test_cash_refund_reduces_available_cash_for_model_guard(self):
        """现金退款事实减少可用现金（与 cash_shift_breakdown 口径一致）。"""
        reservation, folio = self._make_folio("WBRC-MOVE-005")
        shift, _ = BillingService.open_shift(
            property_obj=self.prop, cashier=self.cashier,
            opening_cash=_money("0.00"))
        payment = BillingService.collect_payment(
            property_obj=self.prop, reservation=reservation,
            amount="80.00", method="CASH", collected_by=self.cashier,
            reference="WBRC-CASH-PAY-5")
        self.assertEqual(payment.cashier_shift_id, shift.pk)
        refund, created = BillingService.request_refund(
            property_obj=self.prop, payment_id=payment.pk,
            amount="30.00", reason="现金退款占用班次现金",
            requested_by=self.finance, idempotency_key="wbrc-move-rfd-5")
        self.assertTrue(created)
        BillingService.approve_refund(
            property_obj=self.prop, refund_id=refund.pk,
            approved_by=self.approver)
        # 现金退款必须由执行人本人 OPEN 班次执行；由收款同班次收银员执行
        #（申请/审批/执行三方分离仍然成立：finance/approver/cashier）。
        BillingService.execute_refund(
            property_obj=self.prop, refund_id=refund.pk,
            executed_by=self.cashier, confirm_refund_paid=True,
            idempotency_key="wbrc-move-exec-5")
        # 可用现金 = 0 + 80 − 30 = 50；OUT 50.01 被模型层拒绝
        with self.assertRaises(ValidationError):
            CashierShiftMovement.objects.create(
                shift=shift, direction=CashierShiftMovement.Direction.OUT,
                amount=_money("50.01"), reason="退款后超额支出",
                idempotency_key="wbrc-move-out-006", created_by=self.cashier)

    def test_service_layer_record_shift_movement_path_still_works(self):
        """服务层原路径：可用余额内创建成功；超余额返回 ValueError。"""
        shift, _ = BillingService.open_shift(
            property_obj=self.prop, cashier=self.cashier,
            opening_cash=_money("100.00"))
        movement, created = BillingService.record_shift_movement(
            shift_id=shift.pk, direction="OUT", amount="60.00",
            reason="经批准现金支出", created_by=self.cashier,
            idempotency_key="wbrc-svc-out-001")
        self.assertTrue(created)
        self.assertEqual(movement.amount, _money("60.00"))
        with self.assertRaises(ValueError):
            BillingService.record_shift_movement(
                shift_id=shift.pk, direction="OUT", amount="41.00",
                reason="超出剩余可用现金", created_by=self.cashier,
                idempotency_key="wbrc-svc-out-002")


class ShiftStatusReconciledRemovalTests(CashierReconcileTestBase):
    """WB-095：RECONCILED 未使用状态已从 choices 移除且不可达。"""

    def test_reconciled_status_removed_from_choices(self):
        self.assertNotIn("RECONCILED", CashierShift.ShiftStatus.values)
        self.assertEqual(set(CashierShift.ShiftStatus.values),
                         {"OPEN", "CLOSED"})

    def test_shift_lifecycle_only_open_to_closed(self):
        shift, created = BillingService.open_shift(
            property_obj=self.prop, cashier=self.cashier,
            opening_cash=_money("20.00"))
        self.assertTrue(created)
        self.assertEqual(shift.status, CashierShift.ShiftStatus.OPEN)
        # 直接以已删除状态创建班次被模型状态机拒绝（不可达性回归）
        with self.assertRaises(ValidationError):
            CashierShift.objects.create(
                property=self.prop, cashier=self.cashier,
                opening_cash=_money("20.00"), status="RECONCILED")
        closed = BillingService.close_shift(
            property_obj=self.prop, shift_id=shift.pk,
            actual_cash=_money("20.00"), closed_by=self.cashier)
        self.assertEqual(closed.status, CashierShift.ShiftStatus.CLOSED)


class ReconciliationStrictMatchTests(CashierReconcileTestBase):
    """WB-092（第 32 轮）：凭证核对必须校验类型/金额/币种/引用精确匹配，
    并补齐 ChargeLine 科目行合计核对——不能只按引用存在性判断。"""

    def _refund(self, folio, payment, provider_ref, amount="50.00"):
        # Refund.save() 有状态机守卫（必须从 PENDING 起步）；对账夹具先建
        # 合法 PENDING 申请，再按项目约定用 QuerySet.update 直置终态。
        refund = Refund.objects.create(
            property=self.prop, payment=payment,
            provider_ref=provider_ref, amount=_money(amount), currency="CNY",
            reason="WB092", idempotency_key=f"wbrc-sm-refund-{provider_ref}",
            requested_by=self.finance)
        approval = ApprovalRecord.objects.create(
            property=self.prop, action_code="REFUND_EXECUTE",
            entity_type="Refund", entity_id=str(refund.pk),
            requested_by=self.finance, reason="WB092 对账夹具")
        QuerySet(ApprovalRecord).filter(pk=approval.pk).update(
            status=ApprovalRecord.Status.APPROVED, approved_by=self.approver)
        QuerySet(Refund).filter(pk=refund.pk).update(
            status=Refund.Status.SUCCEEDED, approval_record=approval,
            approved_by=self.approver, executed_by=self.executor,
            executed_at=timezone.now(),
            processed_at=timezone.now(),
            execution_idempotency_key=f"wbrc-sm-exec-{provider_ref}")
        refund.refresh_from_db()
        return refund

    def _entry(self, folio, entry_type, amount, reference, currency="CNY"):
        return LedgerEntry.objects.create(
            property=self.prop, folio=folio, entry_type=entry_type,
            amount=_money(amount), currency=currency,
            business_date=timezone.localdate(), reference=reference)

    def _diff_types(self, folio):
        return [d.diff_type for d in self._scan_diffs(folio)]

    def test_payment_entry_amount_mismatch_reported(self):
        """复现路径：引用存在但金额不符 → 必须报差异（旧实现漏报）。"""
        _, folio = self._make_folio("WBRC-SM-001")
        self._add_payment(folio, "WBRC-SM-PAY-1", "100.00", with_entry=False)
        self._entry(folio, LedgerEntry.EntryType.PAYMENT, "-1.00",
                    "payment:WBRC-SM-PAY-1")
        diffs = [d for d in self._scan_diffs(folio)
                 if d.entity_type == "Payment"]
        self.assertEqual(len(diffs), 1)
        self.assertIn("金额", diffs[0].detail)

    def test_payment_entry_currency_mismatch_reported(self):
        """复现路径：引用/金额正确但币种 USD → 必须报差异。"""
        _, folio = self._make_folio("WBRC-SM-002")
        self._add_payment(folio, "WBRC-SM-PAY-2", "100.00", with_entry=False)
        self._entry(folio, LedgerEntry.EntryType.PAYMENT, "-100.00",
                    "payment:WBRC-SM-PAY-2", currency="USD")
        diffs = [d for d in self._scan_diffs(folio)
                 if d.entity_type == "Payment"]
        self.assertEqual(len(diffs), 1)
        self.assertIn("币种", diffs[0].detail)

    def test_payment_entry_wrong_type_reported(self):
        """复现路径：引用正确但凭证类型是 ADJUSTMENT → 必须报差异。"""
        _, folio = self._make_folio("WBRC-SM-003")
        self._add_payment(folio, "WBRC-SM-PAY-3", "100.00", with_entry=False)
        self._entry(folio, LedgerEntry.EntryType.ADJUSTMENT, "-100.00",
                    "payment:WBRC-SM-PAY-3")
        diffs = [d for d in self._scan_diffs(folio)
                 if d.entity_type == "Payment"]
        self.assertEqual(len(diffs), 1)
        self.assertIn("凭证类型", diffs[0].detail)

    def test_refund_exact_reference_not_prefix_match(self):
        """复现路径：退款 R-10 缺凭证，账本只有 refund:R-100 →
        无边界前缀匹配误判存在；精确匹配必须报 REFUND_NO_ENTRY。"""
        _, folio = self._make_folio("WBRC-SM-004")
        payment = self._add_payment(
            folio, "WBRC-SM-PAY-4", "200.00", with_entry=True)
        refund = self._refund(folio, payment, "R-10")
        # 相似引用凭证（另一笔退款 R-100 的凭证）
        self._entry(folio, LedgerEntry.EntryType.REFUND, "50.00",
                    "refund:R-100")
        diffs = [d for d in self._scan_diffs(folio)
                 if d.entity_type == "Refund"]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].diff_type,
                         FolioReconciliationDiff.DiffType.REFUND_NO_ENTRY)
        self.assertEqual(diffs[0].entity_id, refund.pk)

    def test_refund_entry_content_mismatch_reported(self):
        """退款凭证存在但金额/币种不符 → 必须报差异。"""
        _, folio = self._make_folio("WBRC-SM-005")
        payment = self._add_payment(
            folio, "WBRC-SM-PAY-5", "200.00", with_entry=True)
        self._refund(folio, payment, "R-20", amount="50.00")
        self._entry(folio, LedgerEntry.EntryType.REFUND, "60.00",
                    "refund:R-20", currency="USD")
        diffs = [d for d in self._scan_diffs(folio)
                 if d.entity_type == "Refund"]
        self.assertEqual(len(diffs), 1)
        self.assertIn("与业务事实不符", diffs[0].detail)

    def test_charge_lines_sum_mismatch_reported(self):
        """科目行合计 != 凭证金额 → 新差异类型 CHARGE_LINES_MISMATCH。"""
        _, folio = self._make_folio("WBRC-SM-006")
        from apps.billing.models import ChargeLine
        entry = self._entry(folio, LedgerEntry.EntryType.ROOM_CHARGE,
                            "300.00", "WBRC-SM-CHARGE-1")
        ChargeLine.objects.create(
            ledger_entry=entry, charge_type=ChargeLine.ChargeType.ROOM,
            amount=_money("250.00"))
        diffs = [d for d in self._scan_diffs(folio)
                 if d.entity_type == "LedgerEntry"]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(
            diffs[0].diff_type,
            FolioReconciliationDiff.DiffType.CHARGE_LINES_MISMATCH)
        self.assertEqual(diffs[0].entity_id, entry.pk)

    def test_consistent_entries_still_not_reported(self):
        """正向回归：类型/金额方向/币种/引用全部一致时不误报。"""
        _, folio = self._make_folio("WBRC-SM-007")
        self._add_payment(folio, "WBRC-SM-PAY-7", "100.00", with_entry=True)
        self._entry(folio, LedgerEntry.EntryType.PAYMENT, "-100.00",
                    "payment:WBRC-SM-PAY-7-dup-ref-not-exist")  # 干扰项
        payment = Payment.objects.get(
            provider_ref="WBRC-SM-PAY-7", folio=folio)
        self._refund(folio, payment, "R-30", amount="40.00")
        self._entry(folio, LedgerEntry.EntryType.REFUND, "40.00",
                    "refund:R-30")
        diffs = [d for d in self._scan_diffs(folio)
                 if d.entity_type in ("Payment", "Refund")]
        self.assertEqual(len(diffs), 0)
