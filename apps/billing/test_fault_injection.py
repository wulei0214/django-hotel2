"""收尾专项：故障注入 FI-01~08（收尾任务二）。

在账务执行事务的中途注入异常（mock 抛出），验证领域服务的原子性：
凭证/负债/调整单/审计要么全部提交、要么全部回滚，不留半截账务事实；
并确认状态机拒绝与对账只读核对语义（FI-06/07/08 与既有覆盖互为引用）。

运行后端：SQLite 即可（mock 注入与后端无关；SQLite 全量回归由主会话执行，
MySQL 下同样应通过）。既有覆盖引用：
  * FI-06 ← apps/billing/test_dec02.py::Dec02A1AdjustmentTests
            ::test_a1_execute_twice_rejected_by_status_machine
  * FI-07 ← apps/billing/test_dec02.py::Dec02A1AdjustmentTests
            ::test_a1_charge_reverse_reopens_settled_liability_and_keeps_prior_settled_at
  * FI-08 ← apps/billing/test_cashier_reconcile.py
            ::ReconciliationLiabilityAndAdjustmentTests
            ::test_tampered_liability_remaining_reported
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.dec02 import (
    approve_adjustment,
    execute_adjustment,
    liability_net_snapshot,
    request_adjustment,
)
from apps.billing.liabilities import register_refund_liability
from apps.billing.models import (
    BillingAdjustment,
    Folio,
    FolioReconciliationDiff,
    FolioReconciliationRun,
    LedgerEntry,
    Payment,
    Refund,
    RefundLiability,
)
from apps.billing.reconciliation import scan_folio_reconciliation
from apps.billing.services_v2 import BillingService
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.reservations.models import Reservation, ReservationRoom


def _money(value):
    return Decimal(str(value))


def _boom_save(self, *args, **kwargs):
    raise OSError("故障注入：模型保存失败（FI 注入点）")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class FaultInjectionTestBase(TestCase):
    """公共夹具：物业 + 账单/支付/负债/调整申请构造器。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.create(
            code="MWFJ", name="故障注入专项酒店", timezone="Asia/Shanghai"
        )
        from apps.inventory.models import RatePlan, RoomType

        cls.room_type = RoomType.objects.create(
            property=cls.prop, code="MWFJ-STD", name="故障注入标准间", capacity=2
        )
        cls.rate_plan = RatePlan.objects.create(
            property=cls.prop,
            code="MWFJ-BAR",
            name="故障注入门市价",
            valid_from=timezone.localdate(),
        )
        cls.front = User.objects.create_user(username="fj-front")
        cls.duty = User.objects.create_user(username="fj-duty")
        cls.finance = User.objects.create_user(username="fj-finance")
        cls.approver = User.objects.create_user(username="fj-approver")
        cls.executor = User.objects.create_user(username="fj-executor")
        cls.guest = Guest.objects.create(property=cls.prop, full_name="故障注入客人")

    def _make_folio(self, booking_no, total="600.00", close=False):
        reservation = Reservation.objects.create(
            property=self.prop,
            guest=self.guest,
            booking_no=booking_no,
            status=Reservation.Status.CHECKED_IN,
            check_in=timezone.localdate() - timedelta(days=1),
            check_out=timezone.localdate(),
            room_count=1,
            adult_count=1,
            total_amount=_money(total),
            currency="CNY",
        )
        ReservationRoom.objects.create(
            property=self.prop,
            reservation=reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=timezone.localdate() - timedelta(days=1),
            check_out=timezone.localdate(),
            nightly_rates=[
                {
                    "date": str(timezone.localdate() - timedelta(days=1)),
                    "price": total,
                    "currency": "CNY",
                }
            ],
            amount=_money(total),
        )
        folio = Folio.objects.create(
            property=self.prop, reservation=reservation, guest=self.guest, currency="CNY"
        )
        if close:
            folio.status = Folio.Status.CLOSED
            folio.save(update_fields=("status",))
        return reservation, folio

    def _add_card_payment(self, folio, provider_ref, amount="600.00"):
        payment = Payment.objects.create(
            property=self.prop,
            reservation=folio.reservation,
            folio=folio,
            provider="frontdesk_manual",
            provider_ref=provider_ref,
            method="CARD",
            amount=_money(amount),
            currency="CNY",
            status=Payment.Status.SUCCEEDED,
            paid_at=timezone.now(),
        )
        LedgerEntry.objects.create(
            property=self.prop,
            folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=-_money(amount),
            currency="CNY",
            business_date=timezone.localdate(),
            reference=f"payment:{provider_ref}",
            description="故障注入预付",
        )
        return payment

    def _register_liability(
        self, reservation, folio, amount="600.00", remaining=None, status=None, settled_at=None
    ):
        if remaining is None and status is None:
            liability, _created = register_refund_liability(
                property_obj=self.prop,
                reservation=reservation,
                folio=folio,
                amount=amount,
                created_by=self.finance,
            )
            return liability
        # 直接构造特定状态负债（如 SETTLED，用于 FI-07 回增重开）
        remaining = _money(remaining if remaining is not None else amount)
        return RefundLiability.objects.create(
            property=self.prop,
            reservation=reservation,
            folio=folio,
            amount=_money(amount),
            remaining_amount=remaining,
            currency="CNY",
            status=status or RefundLiability.Status.OPEN,
            settled_at=settled_at,
            idempotency_key=f"fj-liability-{folio.pk}-{timezone.now().timestamp()}",
            created_by=self.finance,
        )

    def _request_and_approve_adjustment(self, folio, liability, adjustment_type, amount, key):
        adjustment, created = request_adjustment(
            property_obj=self.prop,
            folio_id=folio.pk,
            adjustment_type=adjustment_type,
            amount=amount,
            reason="故障注入专项调整",
            requested_by=self.finance,
            idempotency_key=key,
        )
        self.assertTrue(created)
        approve_adjustment(
            property_obj=self.prop, adjustment_id=adjustment.pk, approved_by=self.duty
        )
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, BillingAdjustment.Status.APPROVED)
        return adjustment

    def _request_and_approve_refund(self, payment, amount, key, liability=None, requested_by=None):
        refund, created = BillingService.request_refund(
            property_obj=self.prop,
            payment_id=payment.pk,
            amount=amount,
            reason="故障注入专项退款",
            requested_by=requested_by or self.front,
            idempotency_key=key,
            refund_liability_id=liability.pk if liability else None,
        )
        self.assertTrue(created)
        BillingService.approve_refund(
            property_obj=self.prop, refund_id=refund.pk, approved_by=self.approver
        )
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        return refund

    def _voucher_count(self, reference):
        return LedgerEntry.objects.filter(reference=reference).count()

    def _assert_all_rolled_back(
        self, *, adjustment, liability, folio, entry_reference, payment=None, refund=None
    ):
        """凭证/负债/调整单/审计全回滚（数量前后一致）。"""
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, BillingAdjustment.Status.APPROVED)
        self.assertIsNone(adjustment.executed_at)
        self.assertIsNone(adjustment.executed_by_id)
        self.assertIsNone(adjustment.ledger_entry_id)
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, _money("600.00"))
        self.assertEqual(liability.status, RefundLiability.Status.OPEN)
        self.assertIsNone(liability.settled_at)
        self.assertEqual(self._voucher_count(entry_reference), 0, "调整凭证不得残留")
        if payment is not None:
            payment.refresh_from_db()
        if refund is not None:
            refund.refresh_from_db()

    def _audit_count(self):
        return AuditEvent.objects.count()


class AdjustmentExecuteFaultInjectionTests(FaultInjectionTestBase):
    """FI-01/FI-02/FI-06：账务调整执行的事务原子性与状态机。"""

    def _prepared_adjustment(self, key):
        reservation, folio = self._make_folio(f"MWFJ-ADJ-{key[-6:]}", total="600.00", close=True)
        self._add_card_payment(folio, f"MWFJ-ADJ-PAY-{key[-6:]}")
        liability = self._register_liability(reservation, folio, "600.00")
        adjustment = self._request_and_approve_adjustment(
            folio, liability, BillingAdjustment.AdjustmentType.CHARGE_ADD, "100.00", key
        )
        return folio, liability, adjustment

    def test_fi_01_liability_save_failure_rolls_back_everything(self):
        """FI-01：mock RefundLiability.save 抛异常 → 凭证/负债/调整单/审计全回滚。"""
        key = "fj-adj-fi01"
        folio, liability, adjustment = self._prepared_adjustment(key)
        entries_before = LedgerEntry.objects.filter(folio=folio).count()
        audit_before = self._audit_count()
        reference = f"adjustment:{key}"

        with mock.patch.object(RefundLiability, "save", _boom_save):
            with self.assertRaises(OSError):
                execute_adjustment(
                    property_obj=self.prop, adjustment_id=adjustment.pk, executed_by=self.executor
                )

        self._assert_all_rolled_back(
            adjustment=adjustment, liability=liability, folio=folio, entry_reference=reference
        )
        self.assertEqual(
            LedgerEntry.objects.filter(folio=folio).count(),
            entries_before,
            "账本凭证数量必须与执行前一致（注入异常整体回滚）",
        )
        self.assertEqual(self._audit_count(), audit_before, "审计事件数量必须与执行前一致")
        net = liability_net_snapshot(liability)
        self.assertEqual(net["deducted_amount"], _money("0.00"))
        self.assertEqual(net["net_refundable"], _money("600.00"))

    def test_fi_02_adjustment_save_failure_after_liability_rolls_back(self):
        """FI-02：mock BillingAdjustment.save 抛异常（负债保存后阶段）→ 全回滚。"""
        key = "fj-adj-fi02"
        folio, liability, adjustment = self._prepared_adjustment(key)
        entries_before = LedgerEntry.objects.filter(folio=folio).count()
        audit_before = self._audit_count()
        reference = f"adjustment:{key}"

        with mock.patch.object(BillingAdjustment, "save", _boom_save):
            with self.assertRaises(OSError):
                execute_adjustment(
                    property_obj=self.prop, adjustment_id=adjustment.pk, executed_by=self.executor
                )

        self._assert_all_rolled_back(
            adjustment=adjustment, liability=liability, folio=folio, entry_reference=reference
        )
        self.assertEqual(LedgerEntry.objects.filter(folio=folio).count(), entries_before)
        self.assertEqual(self._audit_count(), audit_before)
        # 负债余额在回滚后保持原值（负债保存本身已发生在注入点之前）
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, _money("600.00"))

    def test_fi_06_duplicate_execute_rejected_with_single_voucher(self):
        """FI-06：同一调整重复执行 → 状态机拒绝、只一笔凭证。

        （既有覆盖：apps/billing/test_dec02.py
        ::test_a1_execute_twice_rejected_by_status_machine，此处收尾引用复验。）
        """
        key = "fj-adj-fi06"
        folio, liability, adjustment = self._prepared_adjustment(key)
        executed = execute_adjustment(
            property_obj=self.prop, adjustment_id=adjustment.pk, executed_by=self.executor
        )
        self.assertEqual(executed.status, BillingAdjustment.Status.EXECUTED)
        self.assertEqual(self._voucher_count(f"adjustment:{key}"), 1)
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, _money("500.00"))

        entries_before = LedgerEntry.objects.filter(folio=folio).count()
        audit_before = self._audit_count()
        with self.assertRaises(ValueError) as ctx:
            execute_adjustment(
                property_obj=self.prop, adjustment_id=adjustment.pk, executed_by=self.executor
            )
        self.assertIn("只有已批准", str(ctx.exception))
        self.assertEqual(self._voucher_count(f"adjustment:{key}"), 1, "重复执行不得产生第二笔凭证")
        self.assertEqual(LedgerEntry.objects.filter(folio=folio).count(), entries_before)
        self.assertEqual(self._audit_count(), audit_before)
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, _money("500.00"), "负债只回减一次")


class RefundExecuteFaultInjectionTests(FaultInjectionTestBase):
    """FI-03/FI-04/FI-05：退款执行的事务原子性。"""

    def _prepared_open_refund(self, key):
        """未关联负债的已批准退款（FI-03：凭证写入后注入）。"""
        reservation, folio = self._make_folio(f"MWFJ-RFD-{key[-6:]}", total="600.00")
        payment = self._add_card_payment(folio, f"MWFJ-RFD-PAY-{key[-6:]}")
        refund = self._request_and_approve_refund(payment, "200.00", key)
        return folio, payment, refund

    def _prepared_liability_refund(self, key):
        """已关闭账单 + 负债 + 已批准退款（FI-04/FI-05：负债更新阶段注入）。"""
        reservation, folio = self._make_folio(f"MWFJ-RFB-{key[-6:]}", total="600.00", close=True)
        payment = self._add_card_payment(folio, f"MWFJ-RFB-PAY-{key[-6:]}")
        liability = self._register_liability(reservation, folio, "600.00")
        refund = self._request_and_approve_refund(payment, "200.00", key, liability=liability)
        return folio, payment, refund, liability

    def _assert_refund_rolled_back(self, *, payment, refund, liability=None):
        refund.refresh_from_db()
        self.assertEqual(
            refund.status, Refund.Status.APPROVED, "退款状态必须保持已批准（不得半途推进）"
        )
        self.assertIsNone(refund.executed_at)
        self.assertIsNone(refund.executed_by_id)
        self.assertIsNone(refund.execution_idempotency_key)
        self.assertEqual(
            self._voucher_count(f"refund:{refund.provider_ref}"), 0, "REFUND 凭证不得残留"
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REFUND_PENDING)
        if liability is not None:
            liability.refresh_from_db()
            self.assertEqual(liability.remaining_amount, _money("600.00"))
            self.assertEqual(liability.status, RefundLiability.Status.OPEN)
            self.assertIsNone(liability.settled_at)

    def test_fi_03_refund_save_failure_removes_voucher_keeps_status(self):
        """FI-03：mock Refund.save 抛异常（退款凭证已写）→ 凭证不残留、状态不变。"""
        folio, payment, refund = self._prepared_open_refund("fj-rfd-fi03")
        entries_before = LedgerEntry.objects.filter(folio=folio).count()
        audit_before = self._audit_count()

        with mock.patch.object(Refund, "save", _boom_save):
            with self.assertRaises(OSError):
                BillingService.execute_refund(
                    property_obj=self.prop,
                    refund_id=refund.pk,
                    executed_by=self.executor,
                    confirm_refund_paid=True,
                    execution_reference="BANK-FI-03",
                    idempotency_key="fj-exec-fi03",
                )

        self._assert_refund_rolled_back(payment=payment, refund=refund)
        self.assertEqual(LedgerEntry.objects.filter(folio=folio).count(), entries_before)
        self.assertEqual(self._audit_count(), audit_before)

    def test_fi_04_liability_save_failure_rolls_back_refund_and_voucher(self):
        """FI-04：Refund 保存后 mock RefundLiability.save 抛异常 → Refund+凭证回滚。"""
        folio, payment, refund, liability = self._prepared_liability_refund("fj-rfd-fi04")
        entries_before = LedgerEntry.objects.filter(folio=folio).count()
        audit_before = self._audit_count()

        with mock.patch.object(RefundLiability, "save", _boom_save):
            with self.assertRaises(OSError):
                BillingService.execute_refund(
                    property_obj=self.prop,
                    refund_id=refund.pk,
                    executed_by=self.executor,
                    confirm_refund_paid=True,
                    execution_reference="BANK-FI-04",
                    idempotency_key="fj-exec-fi04",
                )

        self._assert_refund_rolled_back(payment=payment, refund=refund, liability=liability)
        self.assertEqual(LedgerEntry.objects.filter(folio=folio).count(), entries_before)
        self.assertEqual(self._audit_count(), audit_before)

    def test_fi_05_audit_failure_after_liability_rolls_back_all(self):
        """FI-05：负债更新后 mock AuditEvent.record 抛异常 → 整体回滚。"""
        folio, payment, refund, liability = self._prepared_liability_refund("fj-rfd-fi05")
        entries_before = LedgerEntry.objects.filter(folio=folio).count()
        audit_before = self._audit_count()
        real_record = AuditEvent.record

        def hooked_record(*args, **kwargs):
            if kwargs.get("action") == "billing.refund_executed":
                raise OSError("故障注入：审计落库失败（FI-05 注入点）")
            return real_record(*args, **kwargs)

        with mock.patch.object(AuditEvent, "record", staticmethod(hooked_record)):
            with self.assertRaises(OSError):
                BillingService.execute_refund(
                    property_obj=self.prop,
                    refund_id=refund.pk,
                    executed_by=self.executor,
                    confirm_refund_paid=True,
                    execution_reference="BANK-FI-05",
                    idempotency_key="fj-exec-fi05",
                )

        self._assert_refund_rolled_back(payment=payment, refund=refund, liability=liability)
        self.assertEqual(LedgerEntry.objects.filter(folio=folio).count(), entries_before)
        self.assertEqual(self._audit_count(), audit_before, "注入点前写入的凭证/审计必须一并回滚")
        self.assertFalse(AuditEvent.objects.filter(action="billing.refund_executed").exists())


class LiabilityReopenAndReconcileFaultTests(FaultInjectionTestBase):
    """FI-07/FI-08：结清负债回增重开 + 对账只读核对。

    （既有覆盖引用：apps/billing/test_dec02.py
    ::test_a1_charge_reverse_reopens_settled_liability_and_keeps_prior_settled_at
    与 apps/billing/test_cashier_reconcile.py
    ::test_tampered_liability_remaining_reported；此处收尾引用复验。）
    """

    def test_fi_07_settled_liability_reopened_with_settled_at_cleared(self):
        """FI-07：结清负债经批准回增 → 正确重开、settled_at 清空且保留审计。"""
        reservation, folio = self._make_folio("MWFJ-FI07", total="600.00", close=True)
        self._add_card_payment(folio, "MWFJ-FI07-PAY")
        settled_at = timezone.now() - timedelta(hours=2)
        liability = self._register_liability(
            reservation,
            folio,
            amount="600.00",
            remaining="0.00",
            status=RefundLiability.Status.SETTLED,
            settled_at=settled_at,
        )
        self.assertEqual(liability.status, RefundLiability.Status.SETTLED)

        adjustment = self._request_and_approve_adjustment(
            folio,
            liability,
            BillingAdjustment.AdjustmentType.CHARGE_REVERSE,
            "80.00",
            "fj-adj-fi07",
        )
        execute_adjustment(
            property_obj=self.prop, adjustment_id=adjustment.pk, executed_by=self.executor
        )

        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, _money("80.00"))
        self.assertEqual(liability.status, RefundLiability.Status.PARTIALLY_SETTLED)
        self.assertIsNone(liability.settled_at, "重新打开的负债不得残留旧结清时间")
        self.assertEqual(self._voucher_count("adjustment:fj-adj-fi07"), 1)
        event = AuditEvent.objects.filter(
            action="billing.adjustment_executed", entity_id=adjustment.pk
        ).latest("pk")
        self.assertEqual(
            event.after_json["prior_settled_at"],
            settled_at.isoformat(),
            "原结清时间经审计 after.prior_settled_at 保留",
        )
        # 反向守卫：普通（未批准通道）回增仍被模型拒绝
        fresh = RefundLiability.objects.get(pk=liability.pk)
        fresh.remaining_amount = _money("600.00")
        fresh.status = RefundLiability.Status.OPEN
        with self.assertRaises(Exception):
            fresh.save(update_fields=("remaining_amount", "status", "settled_at"))

    def test_fi_08_reconciliation_reports_diff_without_fixing_data(self):
        """FI-08：人为制造负债/账本差异 → scan 报差异且不改数。"""
        reservation, folio = self._make_folio("MWFJ-FI08", total="600.00", close=True)
        payment = self._add_card_payment(folio, "MWFJ-FI08-PAY")
        liability = self._register_liability(reservation, folio, "600.00")
        refund = self._request_and_approve_refund(
            payment, "100.00", "fj-rfd-fi08", liability=liability, requested_by=self.finance
        )
        BillingService.execute_refund(
            property_obj=self.prop,
            refund_id=refund.pk,
            executed_by=self.executor,
            confirm_refund_paid=True,
            execution_reference="BANK-FI-08",
            idempotency_key="fj-exec-fi08",
        )
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, _money("500.00"))

        # 人为制造负债/账本差异（原生 QuerySet 绕过不可变管理器构造场景）
        QuerySet(RefundLiability).filter(pk=liability.pk).update(
            remaining_amount=_money("450.00"), status=RefundLiability.Status.PARTIALLY_SETTLED
        )
        entries_before = LedgerEntry.objects.filter(folio=folio).count()
        refunds_before = Refund.objects.filter(payment=payment).count()

        run = scan_folio_reconciliation(property_obj=self.prop, checked_by=self.finance)
        diffs = list(FolioReconciliationDiff.objects.filter(run=run, folio=folio))
        self.assertEqual(run.diff_count, len(diffs))
        self.assertEqual(
            [d.diff_type for d in diffs],
            [FolioReconciliationDiff.DiffType.LIABILITY_NET_MISMATCH],
            f"必须报出负债净额差异，实际：{[d.detail for d in diffs]}",
        )
        self.assertIn("450.00", diffs[0].detail)
        self.assertIn("500.00", diffs[0].detail)
        # 只读核对：不改账本、不改退款、不"修复"负债
        self.assertEqual(LedgerEntry.objects.filter(folio=folio).count(), entries_before)
        self.assertEqual(Refund.objects.filter(payment=payment).count(), refunds_before)
        liability.refresh_from_db()
        self.assertEqual(
            liability.remaining_amount, _money("450.00"), "对账扫描不得自动改写负债余额"
        )
        self.assertEqual(liability.status, RefundLiability.Status.PARTIALLY_SETTLED)
        self.assertEqual(FolioReconciliationRun.objects.filter(property=self.prop).count(), 1)
