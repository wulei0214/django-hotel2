# -*- coding: utf-8 -*-
"""白盒服务层单元测试（子智能体 1：billing/services_v2.py）。

覆盖 45-whitebox-services.md 登记缺陷的针对性回归：
  * WB-01 金额 quantize 对超大有限小数抛未捕获 InvalidOperation；
  * WB-02 reverse_entry 营业日不按物业时区；
  * WB-03 request_approval 并发同幂等键 IntegrityError 未转幂等返回；
  * WB-04 post_entry 并发同凭证号 IntegrityError 未转幂等返回；
  * WB-05 record_deposit 押金上限读到脏数据/未限定主账单；
  * WB-08 post_entry 未校验凭证币种与账单币种一致；
  * WB-11 open_shift 并发开班 IntegrityError 未转幂等返回；
  * WB-13 request_refund 并发同幂等键 IntegrityError 未转幂等返回；
  * WB-14 collect_payment 并发同参考号 IntegrityError 未转幂等返回。

并发竞态以“预检查询首查扑空 + 数据库唯一约束兜底”的方式确定性模拟。
"""
import hashlib
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.db.models import Model as DjangoModel
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import (ApprovalRecord, CashierShift, CashierShiftMovement, Folio, LedgerEntry, Payment, Refund)
from apps.billing.services_v2 import ApprovalService, BillingService
from apps.core.models import Property
from apps.guests.models import Guest
from apps.reservations.models import Reservation, ReservationRoom


def _flaky_first_filter(manager, model_none_factory):
    """让 manager.filter 的第一次调用返回空集（模拟并发下预检扑空）。"""
    real_filter = manager.filter
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return model_none_factory()
        return real_filter(*args, **kwargs)

    return _flaky


class WhiteboxBillingBase(TestCase):
    """最小可复用数据：物业 + 已确认预订 + 主账单。"""

    def setUp(self):
        self.today = timezone.localdate()
        self.property = Property.objects.create(code="WBB", name="白盒账务酒店")
        from apps.inventory.models import RatePlan, RoomType
        self.room_type = RoomType.objects.create(
            property=self.property, code="WBB-STD", name="白盒标准间")
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="WBB-BAR", name="白盒门市价",
            valid_from=self.today)
        self.guest = Guest.objects.create(
            property=self.property, full_name="白盒客人",
            phone="13800007701", phone_normalized="13800007701")
        self.operator = User.objects.create_user(username="wbb-operator")

    def _make_reservation(self, booking_no, total="300.00", nights=1):
        reservation = Reservation.objects.create(
            property=self.property, booking_no=booking_no,
            guest=self.guest, status=Reservation.Status.CONFIRMED,
            check_in=self.today, check_out=self.today + timedelta(days=nights),
            room_count=1, adult_count=1, total_amount=Decimal(total))
        ReservationRoom.objects.create(
            property=self.property, reservation=reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=self.today, check_out=self.today + timedelta(days=nights),
            nightly_rates=[{"date": self.today.isoformat(), "price": total,
                            "currency": "CNY"}],
            amount=Decimal(total))
        self.folio = Folio.objects.create(
            property=self.property, reservation=reservation, guest=self.guest,
            currency="CNY")
        return reservation


class PostEntryMoneyGuardTests(WhiteboxBillingBase):
    """WB-01 / WB-04 / WB-08：post_entry 金额精度、幂等竞态与币种守卫。"""

    def setUp(self):
        super().setUp()
        self.reservation = self._make_reservation("WBBPOST0001")

    def test_post_entry_rejects_unquantizable_huge_amount(self):
        """WB-01：超大有限金额必须得到干净的 ValueError，而不是 InvalidOperation 500。"""
        with self.assertRaises(ValueError):
            BillingService.post_entry(
                property_obj=self.property, folio=self.folio,
                entry_type=LedgerEntry.EntryType.ADJUSTMENT,
                amount="1e30", reference="WBB-HUGE-001",
                posted_by=self.operator)

    def test_post_entry_duplicate_reference_race_is_idempotent(self):
        """WB-04：预检扑空 + 唯一约束仲裁时，同事实应幂等返回既有凭证。"""
        kwargs = dict(
            property_obj=self.property, folio=self.folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("10.00"), reference="WBB-RACE-001",
            posted_by=self.operator)
        entry, created = BillingService.post_entry(**kwargs)
        self.assertTrue(created)

        flaky = _flaky_first_filter(
            LedgerEntry.objects, LedgerEntry.objects.none)
        with mock.patch.object(LedgerEntry.objects, "filter", side_effect=flaky):
            raced, raced_created = BillingService.post_entry(**kwargs)
        self.assertFalse(raced_created)
        self.assertEqual(raced.pk, entry.pk)
        self.assertEqual(
            LedgerEntry.objects.filter(reference="WBB-RACE-001").count(), 1)

    def test_post_entry_rejects_foreign_currency(self):
        """WB-08：账本凭证币种必须与账单币种一致，禁止混币入账。"""
        with self.assertRaises(ValueError):
            BillingService.post_entry(
                property_obj=self.property, folio=self.folio,
                entry_type=LedgerEntry.EntryType.ADJUSTMENT,
                amount=Decimal("10.00"), currency="USD",
                reference="WBB-USD-001", posted_by=self.operator)
        self.assertFalse(
            LedgerEntry.objects.filter(reference="WBB-USD-001").exists())


class ReverseEntryBusinessDateTests(WhiteboxBillingBase):
    """WB-02：红字冲销凭证的营业日必须按物业时区计算。"""

    def test_reverse_entry_uses_property_business_date(self):
        self.property.timezone = "Pacific/Kiritimati"  # UTC+14
        self.property.save(update_fields=("timezone",))
        reservation = self._make_reservation("WBBREV0001")
        original, _ = BillingService.post_entry(
            property_obj=self.property, folio=self.folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("10.00"), reference="WBB-REV-SRC-001",
            business_date=date(2026, 1, 10), posted_by=self.operator)

        # 固定“现在”：物业当地已是 2026-01-16，而默认时区（Asia/Shanghai）仍是 01-15
        fixed_now = datetime(2026, 1, 15, 15, 59, 0, tzinfo=dt_timezone.utc)
        with mock.patch.object(timezone, "now", return_value=fixed_now):
            reversal, created = BillingService.reverse_entry(
                property_obj=self.property, entry_id=original.pk,
                reason="错账冲销白盒测试", reversed_by=self.operator)
        self.assertTrue(created)
        self.assertEqual(reversal.business_date, date(2026, 1, 16))
        other_property = Property.objects.create(code="WBBREVOTHER", name="冲销跨物业测试")
        with self.assertRaises(LedgerEntry.DoesNotExist):
            BillingService.reverse_entry(
                property_obj=other_property, entry_id=original.pk,
                reason="跨物业冲销必须拒绝", reversed_by=self.operator)


class ApprovalRaceTests(WhiteboxBillingBase):
    """WB-03：request_approval 并发同幂等键必须幂等返回既有记录。"""

    def test_request_approval_race_falls_back_to_existing_record(self):
        first, created = ApprovalService.request_approval(
            property_obj=self.property, action_code="billing.refund",
            entity_type="Refund", entity_id="41001",
            requested_by=self.operator, reason="白盒审批竞态测试")
        self.assertTrue(created)

        flaky = _flaky_first_filter(
            ApprovalRecord.objects, ApprovalRecord.objects.none)
        with mock.patch.object(ApprovalRecord.objects, "filter", side_effect=flaky):
            raced, raced_created = ApprovalService.request_approval(
                property_obj=self.property, action_code="billing.refund",
                entity_type="Refund", entity_id="41001",
                requested_by=self.operator, reason="白盒审批竞态测试")
        self.assertFalse(raced_created)
        self.assertEqual(raced.pk, first.pk)
        self.assertEqual(ApprovalRecord.objects.count(), 1)

    def test_request_approval_idempotency_key_is_property_scoped(self):
        """补充：同参数不同物业不得共享幂等键（授权边界回归）。"""
        other_property = Property.objects.create(code="WBB2", name="白盒第二物业")
        first, _ = ApprovalService.request_approval(
            property_obj=self.property, action_code="billing.refund",
            entity_type="Refund", entity_id="41002",
            requested_by=self.operator, reason="白盒审批物业隔离")
        second, _ = ApprovalService.request_approval(
            property_obj=other_property, action_code="billing.refund",
            entity_type="Refund", entity_id="41002",
            requested_by=self.operator, reason="白盒审批物业隔离")
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        expected = hashlib.sha256(
            f"{self.property.pk}:billing.refund:Refund:41002:{self.operator.pk}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(first.idempotency_key, expected)


class DepositGuardTests(WhiteboxBillingBase):
    """WB-05：record_deposit 押金上限脏读与主账单归属。"""

    def setUp(self):
        super().setUp()
        self.reservation = self._make_reservation("WBBDEP0001", total="100.00")

    def test_record_deposit_rechecks_cap_on_fresh_lock(self):
        """WB-05a：押金上限校验必须读取数据库最新值，不能信任传入实例的脏快照。"""
        # 模拟并发窗口：数据库里其他会话已登记 60.00 押金，传入实例仍是旧值 0
        Reservation.objects.filter(pk=self.reservation.pk).update(
            deposit_amount=Decimal("60.00"))
        with self.assertRaises(ValueError):
            BillingService.record_deposit(
                property_obj=self.property, reservation=self.reservation,
                amount="50.00", method="CASH", idempotency_key="wbb-dep-001",
                received_by=self.operator)
        self.assertFalse(Payment.objects.filter(
            provider="frontdesk_deposit").exists())
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.deposit_amount, Decimal("60.00"))

    def test_record_deposit_targets_master_folio(self):
        """WB-05b：押金必须落在主账单上，即使存在更新的非主账单。"""
        guest_folio = Folio.objects.create(
            property=self.property, reservation=self.reservation,
            guest=self.guest, currency="CNY",
            folio_type=Folio.FolioType.GUEST)
        # 保证分账单 opened_at 严格晚于主账单（排除同刻并列导致排序不稳定）
        Folio.objects.filter(pk=guest_folio.pk).update(
            opened_at=timezone.now() + timedelta(seconds=10))
        BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash=Decimal("0.00"))
        payment, created = BillingService.record_deposit(
            property_obj=self.property, reservation=self.reservation,
            amount="50.00", method="CASH", idempotency_key="wbb-dep-002",
            received_by=self.operator)
        self.assertTrue(created)
        self.assertEqual(payment.folio_id, self.folio.pk)
        self.assertNotEqual(payment.folio_id, guest_folio.pk)
        entry = LedgerEntry.objects.get(reference=f"deposit:{payment.provider_ref}")
        self.assertEqual(entry.folio_id, self.folio.pk)

    def test_deposit_replay_survives_checkout_and_folio_close(self):
        payment, created = BillingService.record_deposit(
            property_obj=self.property, reservation=self.reservation,
            amount="30.00", method="CARD",
            idempotency_key="wbb-deposit-replay-001",
            received_by=self.operator)
        self.assertTrue(created)
        self.reservation.status = "CHECKED_OUT"
        self.reservation.save(update_fields=("status", "updated_at"))
        self.folio.status = Folio.Status.CLOSED
        self.folio.save(update_fields=("status", "updated_at"))

        replay, replay_created = BillingService.record_deposit(
            property_obj=self.property, reservation=self.reservation,
            amount="30.00", method="CARD",
            idempotency_key="wbb-deposit-replay-001",
            received_by=self.operator)
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, payment.pk)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.deposit_amount, Decimal("30.00"))
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=f"deposit:{payment.provider_ref}").count(), 1)


class CollectPaymentRaceTests(WhiteboxBillingBase):
    """WB-14：collect_payment 并发同参考号必须幂等返回既有支付。"""

    def test_collect_payment_race_returns_existing_payment(self):
        reservation = self._make_reservation("WBBPAY0001", total="600.00", nights=2)
        BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash=Decimal("0.00"))
        first = BillingService.collect_payment(
            property_obj=self.property, reservation=reservation,
            amount="100.00", method="CASH", collected_by=self.operator,
            reference="FDWBB-RACE-001")

        flaky = _flaky_first_filter(Payment.objects, Payment.objects.none)
        with mock.patch.object(Payment.objects, "filter", side_effect=flaky):
            raced = BillingService.collect_payment(
                property_obj=self.property, reservation=reservation,
                amount="100.00", method="CASH", collected_by=self.operator,
                reference="FDWBB-RACE-001")
        self.assertEqual(raced.pk, first.pk)
        self.assertEqual(
            Payment.objects.filter(provider_ref="FDWBB-RACE-001").count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference="payment:FDWBB-RACE-001").count(), 1)




class CollectPaymentClosedFolioReplayTests(WhiteboxBillingBase):
    """收款成功后账单结账，原参考号重放仍返回既有支付，不重复入账。"""

    def test_successful_payment_replay_survives_folio_close(self):
        reservation = self._make_reservation("WBBPAYCLOSED001")
        payment = BillingService.collect_payment(
            property_obj=self.property, reservation=reservation,
            amount="80.00", method="CARD", collected_by=self.operator,
            reference="WBB-PAY-CLOSED-001")
        folio = payment.folio
        folio.status = Folio.Status.CLOSED
        folio.save(update_fields=("status", "updated_at"))

        replay = BillingService.collect_payment(
            property_obj=self.property, reservation=reservation,
            amount="80.00", method="CARD", collected_by=self.operator,
            reference="WBB-PAY-CLOSED-001")
        self.assertEqual(replay.pk, payment.pk)
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference="payment:WBB-PAY-CLOSED-001").count(), 1)

class RefundRaceTests(WhiteboxBillingBase):
    """WB-13：request_refund 并发同幂等键必须幂等返回既有申请。"""

    def _make_successful_payment(self, reservation, amount="300.00"):
        return Payment.objects.create(
            property=self.property, reservation=reservation, folio=self.folio,
            provider="frontdesk_manual", provider_ref="FDWBB-PAY-SRC-001",
            method="CASH", amount=Decimal(amount), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())

    def test_request_refund_race_returns_existing_refund(self):
        reservation = self._make_reservation("WBBRFD0001")
        payment = self._make_successful_payment(reservation)
        first, created = BillingService.request_refund(
            property_obj=self.property, payment_id=payment.pk,
            amount="100.00", reason="白盒退款竞态测试",
            requested_by=self.operator, idempotency_key="wbb-rfd-001")
        self.assertTrue(created)

        flaky = _flaky_first_filter(Refund.objects, Refund.objects.none)
        with mock.patch.object(Refund.objects, "filter", side_effect=flaky):
            raced, raced_created = BillingService.request_refund(
                property_obj=self.property, payment_id=payment.pk,
                amount="100.00", reason="白盒退款竞态测试",
                requested_by=self.operator, idempotency_key="wbb-rfd-001")
        self.assertFalse(raced_created)
        self.assertEqual(raced.pk, first.pk)
        self.assertEqual(Refund.objects.count(), 1)


class OpenShiftRaceTests(WhiteboxBillingBase):
    """WB-11：open_shift 并发开班撞唯一键必须幂等返回既有班次。"""

    def test_open_shift_race_returns_existing_shift(self):
        shift, created = BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash=Decimal("100.00"))
        self.assertTrue(created)

        flaky = _flaky_first_filter(
            CashierShift.objects, CashierShift.objects.none)
        with mock.patch.object(CashierShift.objects, "filter", side_effect=flaky):
            raced, raced_created = BillingService.open_shift(
                property_obj=self.property, cashier=self.operator,
                opening_cash=Decimal("100.00"))
        self.assertFalse(raced_created)
        self.assertEqual(raced.pk, shift.pk)
        self.assertEqual(
            CashierShift.objects.filter(
                property=self.property, cashier=self.operator).count(), 1)


class MoneyBoundaryTests(WhiteboxBillingBase):
    """WB-01 补充：_money 对 NaN/Inf/超大值的统一拒绝口径。"""

    def test_money_rejects_non_finite_and_oversized_values(self):
        for bad in ("NaN", "Infinity", "-Infinity", "1e30", "1" + "0" * 40):
            with self.assertRaises(ValueError, msg=bad):
                BillingService._money(bad)
        with self.assertRaises(ValueError):
            BillingService._money("12.345")  # 超过两位小数
        self.assertEqual(BillingService._money("12.34"), Decimal("12.34"))

class PaymentRefundModelSaveRegressionTests(WhiteboxBillingBase):
    """回归：支付状态可正常更新；退款不可变字段守卫必须挂在 Refund 而非 Payment。"""

    def test_payment_status_update_does_not_query_refund_only_fields(self):
        reservation = self._make_reservation("WBBMODEL001")
        payment = Payment.objects.create(
            property=self.property, reservation=reservation, folio=self.folio,
            provider="mock", provider_ref="WBB-MODEL-PAY-001",
            method="CARD", amount=Decimal("100.00"), currency="CNY",
            status=Payment.Status.PENDING)
        payment.status = Payment.Status.SUCCEEDED
        payment.paid_at = timezone.now()
        payment.save(update_fields=("status", "paid_at", "updated_at"))
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)

        payment.amount = Decimal("101.00")
        with self.assertRaises(ValidationError):
            payment.save()

    def test_refund_save_allows_status_progression_but_protects_request_facts(self):
        reservation = self._make_reservation("WBBMODEL002")
        payment = Payment.objects.create(
            property=self.property, reservation=reservation, folio=self.folio,
            provider="frontdesk_manual", provider_ref="WBB-MODEL-PAY-002",
            method="CARD", amount=Decimal("100.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        refund = Refund.objects.create(
            property=self.property, payment=payment, amount=Decimal("20.00"),
            currency="CNY", reason="模型保存回归", idempotency_key="wbb-model-refund",
            provider_ref="WBB-MODEL-RFD-002", requested_by=self.operator)
        approver = User.objects.create_user(username="wbb-model-approver")
        refund.status = Refund.Status.APPROVED
        refund.approved_by = approver
        refund.save(update_fields=("status", "approved_by"))
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        self.assertEqual(refund.approved_by_id, approver.pk)

        refund.amount = Decimal("21.00")
        with self.assertRaises(ValidationError):
            refund.save()


class RefundThreePartyExecutionTests(WhiteboxBillingBase):
    """申请、审批、执行必须是三个不同账号，账本仅在执行后落一笔。"""

    def test_refund_three_party_flow_posts_once_and_is_idempotent(self):
        reservation = self._make_reservation("WBBRFD3P001")
        payment = Payment.objects.create(
            property=self.property, reservation=reservation, folio=self.folio,
            provider="frontdesk_manual", provider_ref="WBB-3P-PAY-001",
            method="CARD", amount=Decimal("120.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        approver = User.objects.create_user(username="wbb-refund-approver")
        executor = User.objects.create_user(username="wbb-refund-executor")

        refund, created = BillingService.request_refund(
            property_obj=self.property, payment_id=payment.pk,
            amount="45.00", reason="三方退款闭环测试",
            requested_by=self.operator, idempotency_key="wbb-refund-3p-001")
        self.assertTrue(created)
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"refund:{refund.provider_ref}").exists())

        approved, changed = BillingService.approve_refund(
            property_obj=self.property, refund_id=refund.pk, approved_by=approver)
        self.assertTrue(changed)
        self.assertEqual(approved.status, Refund.Status.APPROVED)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"refund:{refund.provider_ref}").exists())

        with self.assertRaises(ValueError):
            BillingService.execute_refund(
                property_obj=self.property, refund_id=refund.pk,
                executed_by=self.operator, confirm_refund_paid=True,
                idempotency_key="wbb-refund-exec-bad-1")
        with self.assertRaises(ValueError):
            BillingService.execute_refund(
                property_obj=self.property, refund_id=refund.pk,
                executed_by=approver, confirm_refund_paid=True,
                idempotency_key="wbb-refund-exec-bad-2")

        executed, created = BillingService.execute_refund(
            property_obj=self.property, refund_id=refund.pk,
            executed_by=executor, confirm_refund_paid=True,
            execution_reference="BANK-REF-3P-001",
            idempotency_key="wbb-refund-exec-3p-001")
        self.assertTrue(created)
        self.assertEqual(executed.status, Refund.Status.SUCCEEDED)
        self.assertEqual(executed.requested_by_id, self.operator.pk)
        self.assertEqual(executed.approved_by_id, approver.pk)
        self.assertEqual(executed.executed_by_id, executor.pk)
        self.assertEqual(executed.execution_reference, "BANK-REF-3P-001")
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=f"refund:{refund.provider_ref}").count(), 1)

        replay, replay_created = BillingService.execute_refund(
            property_obj=self.property, refund_id=refund.pk,
            executed_by=executor, confirm_refund_paid=True,
            execution_reference="BANK-REF-3P-001",
            idempotency_key="wbb-refund-exec-3p-001")
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, executed.pk)
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=f"refund:{refund.provider_ref}").count(), 1)


class CashShiftReconciliationTests(WhiteboxBillingBase):
    """现金班次对账：现金支付、现金退款和现金变动按事实归属到执行班次。"""

    def test_cash_shift_expected_balance_includes_income_refunds_and_movements(self):
        reservation = self._make_reservation("WBBCASHSHIFT001")
        cashier_shift, created = BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash="50.00")
        self.assertTrue(created)

        payment = BillingService.collect_payment(
            property_obj=self.property, reservation=reservation,
            amount="100.00", method="CASH", collected_by=self.operator,
            reference="WBB-CASH-PAY-001")
        self.assertEqual(payment.cashier_shift_id, cashier_shift.pk)

        approver = User.objects.create_user(username="wbb-cash-approver")
        executor = User.objects.create_user(username="wbb-cash-executor")
        execution_shift, created = BillingService.open_shift(
            property_obj=self.property, cashier=executor,
            opening_cash="50.00")
        self.assertTrue(created)

        refund, created = BillingService.request_refund(
            property_obj=self.property, payment_id=payment.pk,
            amount="20.00", reason="现金退款班次对账",
            requested_by=self.operator, idempotency_key="wbb-cash-refund-001")
        self.assertTrue(created)
        BillingService.approve_refund(
            property_obj=self.property, refund_id=refund.pk,
            approved_by=approver)
        executed, created = BillingService.execute_refund(
            property_obj=self.property, refund_id=refund.pk,
            executed_by=executor, confirm_refund_paid=True,
            execution_reference="CASH-VOUCHER-001",
            idempotency_key="wbb-cash-refund-exec-001")
        self.assertTrue(created)
        self.assertEqual(executed.execution_shift_id, execution_shift.pk)

        BillingService.record_shift_movement(
            shift_id=execution_shift.pk, direction="IN", amount="10.00",
            reason="补充备用金", created_by=executor,
            idempotency_key="wbb-cash-in-001")
        out_movement, out_created = BillingService.record_shift_movement(
            shift_id=execution_shift.pk, direction="OUT", amount="35.00",
            reason="经批准现金支出", created_by=executor,
            idempotency_key="wbb-cash-out-001")
        self.assertTrue(out_created)
        replay, replay_created = BillingService.record_shift_movement(
            shift_id=execution_shift.pk, direction="OUT", amount="35.00",
            reason="经批准现金支出", created_by=executor,
            idempotency_key="wbb-cash-out-001")
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, out_movement.pk)

        closed = BillingService.close_shift(
            property_obj=self.property, shift_id=execution_shift.pk,
            actual_cash="5.00", closed_by=executor)
        self.assertEqual(closed.expected_cash, Decimal("5.00"))
        self.assertEqual(closed.variance, Decimal("0.00"))
        self.assertEqual(closed.status, CashierShift.ShiftStatus.CLOSED)
        replay_after_close, replay_created = BillingService.record_shift_movement(
            shift_id=execution_shift.pk, direction="OUT", amount="35.00",
            reason="经批准现金支出", created_by=executor,
            idempotency_key="wbb-cash-out-001")
        self.assertFalse(replay_created)
        self.assertEqual(replay_after_close.pk, out_movement.pk)

    def test_non_cash_payment_is_not_counted_in_cash_shift(self):
        reservation = self._make_reservation("WBBCASHSHIFT002")
        shift, _ = BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash="25.00")
        payment = BillingService.collect_payment(
            property_obj=self.property, reservation=reservation,
            amount="80.00", method="CARD", collected_by=self.operator,
            reference="WBB-CARD-PAY-001")
        self.assertIsNone(payment.cashier_shift_id)

        closed = BillingService.close_shift(
            property_obj=self.property, shift_id=shift.pk,
            actual_cash="25.00", closed_by=self.operator)
        self.assertEqual(closed.expected_cash, Decimal("25.00"))

    def test_closed_shift_rejects_new_cash_movement(self):
        shift, _ = BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash="10.00")
        BillingService.close_shift(
            property_obj=self.property, shift_id=shift.pk,
            actual_cash="10.00", closed_by=self.operator)
        with self.assertRaises(ValueError):
            BillingService.record_shift_movement(
                shift_id=shift.pk, direction="OUT", amount="1.00",
                reason="关班后不得补录", created_by=self.operator,
                idempotency_key="wbb-cash-closed-001")

class RefundDatabaseInvariantTests(WhiteboxBillingBase):
    """退款 SoD 与执行状态不只依赖 service；数据库约束必须阻止 QuerySet.update 绕过。"""

    def _pending_refund(self):
        reservation = self._make_reservation("WBBDBREFUND01")
        payment = Payment.objects.create(
            property=self.property, reservation=reservation, folio=self.folio,
            provider="frontdesk_manual", provider_ref="WBB-DB-PAY-001",
            method="CARD", amount=Decimal("100.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        refund, created = BillingService.request_refund(
            property_obj=self.property, payment_id=payment.pk,
            amount="25.00", reason="数据库 SoD 约束测试",
            requested_by=self.operator, idempotency_key="wbb-db-refund-001")
        self.assertTrue(created)
        self.assertFalse(refund.legacy_workflow)
        return refund

    def test_database_rejects_requester_as_approver_even_when_model_save_is_bypassed(self):
        refund = self._pending_refund()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QuerySet.update(
                    Refund.objects.filter(pk=refund.pk),
                    status=Refund.Status.APPROVED,
                    approved_by_id=self.operator.pk)

    def test_database_rejects_approver_as_executor_even_when_model_save_is_bypassed(self):
        refund = self._pending_refund()
        approver = User.objects.create_user(username="wbb-db-approver")
        BillingService.approve_refund(
            property_obj=self.property, refund_id=refund.pk,
            approved_by=approver)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QuerySet.update(
                    Refund.objects.filter(pk=refund.pk),
                    status=Refund.Status.SUCCEEDED,
                    executed_by_id=approver.pk,
                    executed_at=timezone.now(),
                    execution_idempotency_key="wbb-db-bypass-exec-001")

    def test_model_rejects_skipping_approval_and_execution_steps(self):
        refund = self._pending_refund()
        refund.status = Refund.Status.SUCCEEDED
        refund.executed_by = User.objects.create_user(username="wbb-db-executor")
        refund.executed_at = timezone.now()
        refund.execution_idempotency_key = "wbb-db-skip-exec-001"
        with self.assertRaises(ValidationError):
            refund.save()




    def test_cash_refund_cannot_overdraw_execution_shift(self):
        reservation = self._make_reservation("WBBCASHSHIFT003")
        applicant_shift, _ = BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash="0.00")
        payment = BillingService.collect_payment(
            property_obj=self.property, reservation=reservation,
            amount="100.00", method="CASH", collected_by=self.operator,
            reference="WBB-CASH-PAY-002")
        self.assertEqual(payment.cashier_shift_id, applicant_shift.pk)

        approver = User.objects.create_user(username="wbb-cash-approver-2")
        executor = User.objects.create_user(username="wbb-cash-executor-2")
        execution_shift, _ = BillingService.open_shift(
            property_obj=self.property, cashier=executor,
            opening_cash="0.00")
        refund, _ = BillingService.request_refund(
            property_obj=self.property, payment_id=payment.pk,
            amount="20.00", reason="现金退款余额不足测试",
            requested_by=self.operator, idempotency_key="wbb-cash-refund-002")
        BillingService.approve_refund(
            property_obj=self.property, refund_id=refund.pk,
            approved_by=approver)

        with self.assertRaises(ValueError):
            BillingService.execute_refund(
                property_obj=self.property, refund_id=refund.pk,
                executed_by=executor, confirm_refund_paid=True,
                idempotency_key="wbb-cash-refund-exec-002")
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"refund:{refund.provider_ref}").exists())

        BillingService.record_shift_movement(
            shift_id=execution_shift.pk, direction="IN", amount="20.00",
            reason="实际领取现金备用金", created_by=executor,
            idempotency_key="wbb-cash-fund-002")
        executed, created = BillingService.execute_refund(
            property_obj=self.property, refund_id=refund.pk,
            executed_by=executor, confirm_refund_paid=True,
            idempotency_key="wbb-cash-refund-exec-002")
        self.assertTrue(created)
        self.assertEqual(executed.status, Refund.Status.SUCCEEDED)
        self.assertEqual(executed.execution_shift_id, execution_shift.pk)
        self.assertEqual(
            BillingService.cash_shift_expected_cash(execution_shift),
            Decimal("0.00"))

class ImmutableFinancialFactManagerTests(WhiteboxBillingBase):
    """防止 ORM bulk update/delete 绕过财务模型的实例级不可变保护。"""

    def test_payment_refund_and_cash_movement_reject_bulk_mutation(self):
        reservation = self._make_reservation("WBBIMMUTABLE001")
        payment = Payment.objects.create(
            property=self.property, reservation=reservation, folio=self.folio,
            provider="frontdesk_manual", provider_ref="WBB-IMM-PAY-001",
            method="CARD", amount=Decimal("100.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        with self.assertRaises(ValidationError):
            Payment.objects.filter(pk=payment.pk).update(amount=Decimal("99.00"))
        with self.assertRaises(ValidationError):
            Payment.objects.filter(pk=payment.pk).delete()
        with self.assertRaises(ValidationError):
            Payment.objects.bulk_update([payment], ["status"])
        with self.assertRaises(ValidationError):
            Payment.objects.bulk_create([Payment(
                property=self.property, reservation=reservation, folio=self.folio,
                provider="frontdesk_manual", provider_ref="WBB-IMM-PAY-002",
                method="CARD", amount=Decimal("10.00"), currency="CNY")])

        refund = Refund.objects.create(
            property=self.property, payment=payment, amount=Decimal("20.00"),
            currency="CNY", reason="不可变事实测试",
            idempotency_key="wbb-immutable-refund-001",
            provider_ref="WBB-IMM-RFD-001", requested_by=self.operator)
        with self.assertRaises(ValidationError):
            Refund.objects.filter(pk=refund.pk).update(amount=Decimal("19.00"))
        with self.assertRaises(ValidationError):
            Refund.objects.filter(pk=refund.pk).delete()

        refund.legacy_workflow = True
        with self.assertRaises(ValidationError):
            refund.save()
        with self.assertRaises(ValidationError):
            Refund.objects.create(
                property=self.property, payment=payment,
                amount=Decimal("10.00"), currency="CNY",
                reason="不得伪造历史", idempotency_key="wbb-legacy-forgery",
                provider_ref="WBB-IMM-RFD-002", requested_by=self.operator,
                legacy_workflow=True)

        shift, _ = BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash="10.00")
        movement, _ = BillingService.record_shift_movement(
            shift_id=shift.pk, direction="IN", amount="1.00",
            reason="不可变现金变动测试", created_by=self.operator,
            idempotency_key="wbb-immutable-movement-001")
        with self.assertRaises(ValidationError):
            CashierShiftMovement.objects.filter(pk=movement.pk).update(
                amount=Decimal("2.00"))
        with self.assertRaises(ValidationError):
            CashierShiftMovement.objects.filter(pk=movement.pk).delete()
        with self.assertRaises(ValidationError):
            CashierShift.objects.filter(pk=shift.pk).update(status=CashierShift.ShiftStatus.CLOSED)
        with self.assertRaises(ValidationError):
            CashierShift.objects.filter(pk=shift.pk).delete()

    def test_cash_movement_direction_is_database_constrained(self):
        shift, _ = BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash="10.00")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CashierShiftMovement.objects.create(
                    shift=shift, direction="SIDEWAYS", amount=Decimal("1.00"),
                    reason="非法方向", idempotency_key="wbb-invalid-direction",
                    created_by=self.operator)

    def test_cashier_shift_nonnegative_cash_is_database_constrained(self):
        invalid = CashierShift(
            property=self.property, cashier=self.operator,
            opening_cash=Decimal("-1.00"))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DjangoModel.save(invalid)

class ApprovalRecordImmutabilityTests(WhiteboxBillingBase):
    """审批申请参数固定；审批状态只能经 ApprovalService 单向推进。"""

    def test_approval_request_facts_cannot_be_changed_through_save_or_queryset(self):
        record, created = ApprovalService.request_approval(
            property_obj=self.property, action_code="billing.refund",
            entity_type="Refund", entity_id="approval-immutable-001",
            requested_by=self.operator, reason="不可变审批申请测试")
        self.assertTrue(created)
        record.reason = "篡改申请原因"
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(ValidationError):
            ApprovalRecord.objects.filter(pk=record.pk).update(
                reason="QuerySet 绕过尝试")
        record.refresh_from_db()
        self.assertEqual(record.reason, "不可变审批申请测试")
        self.assertEqual(record.status, ApprovalRecord.Status.PENDING)

