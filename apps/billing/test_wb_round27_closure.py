"""第 27 轮收尾专项：对新增/加固修复做可复验的行为断言。

覆盖：
  * WB-107 收款事实与渠道交易事实的双向绑定（provider / provider_ref）；
  * WB-090 渠道 UNKNOWN 事实不得直接过期，且必须创建可重试补偿任务；
  * WB-098 在线支付回调 / 前台押金确认两条确认路径写入政策快照；
  * WB-102 证件登记与审计同事务，审计失败整笔回滚；
  * WB-EMP-01 可空唯一键在状态切换时被正确维护，get_membership 优先 ACTIVE；
  * WB-079 旧冲销入口已退役，不得绕过统一账务守卫。

这些用例针对「修复是否真正改变行为」，不以测试名称或提交说明作为结论。
MySQL 真实并发另见 apps/{billing,guests,identity}/test_mysql_concurrency.py。
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import (
    Folio,
    Payment,
    PaymentCompensationTask,
    PaymentIntent,
)
from apps.billing.payment_intents import (
    confirm_intent_success,
    create_intent,
    expire_intent,
    record_provider_event,
)
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.identity.models import PropertyStaffMembership, StaffProfile
from apps.inventory.models import RatePlan, RoomType
from apps.reservations.models import (
    EarlyDeparturePolicy,
    Reservation,
    ReservationRoom,
)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class Round27ClosureTests(TestCase):
    """独立物业夹具，避免依赖 DEFAULT 物业的存量数据。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.create(
            code="WB27CL", name="第27轮闭环专项酒店",
            timezone="Asia/Shanghai")
        cls.owner = User.objects.create_user(username="wb27-owner")
        cls.guest = Guest.objects.create(
            property=cls.prop, full_name="第27轮专项客人")
        cls.rt = RoomType.objects.create(
            property=cls.prop, code="WB27-STD", name="闭环专项标准间",
            capacity=2)
        cls.rate = RatePlan.objects.create(
            property=cls.prop, code="WB27-BAR", name="闭环专项门市价",
            valid_from=timezone.localdate())
        today = timezone.localdate()
        cls.reservation = Reservation.objects.create(
            property=cls.prop, guest=cls.guest, booking_no="WB27-0001",
            status=Reservation.Status.CONFIRMED,
            check_in=today, check_out=today + timedelta(days=1),
            room_count=1, adult_count=1,
            total_amount=Decimal("200.00"), currency="CNY",
            expires_at=timezone.now() + timedelta(hours=1))
        cls.line = ReservationRoom.objects.create(
            property=cls.prop, reservation=cls.reservation,
            room_type=cls.rt, rate_plan=cls.rate,
            check_in=today, check_out=today + timedelta(days=1),
            nightly_rates=[{"date": str(today), "price": "200.00",
                            "currency": "CNY"}],
            amount=Decimal("200.00"))
        cls.folio = Folio.objects.create(
            property=cls.prop, reservation=cls.reservation,
            guest=cls.guest, currency="CNY")

    # ---------------- WB-107 ----------------
    def _processing_intent(self, key):
        intent, _ = create_intent(
            property_obj=self.prop, reservation=self.reservation,
            folio=self.folio, amount="200.00", created_by=self.owner,
            idempotency_key=key)
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=("status",))
        return intent

    def _succeeded_payment(self, provider, provider_ref):
        return Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider=provider, provider_ref=provider_ref, method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())

    def test_wb107_rejects_payment_with_foreign_channel_txn_ref(self):
        """WB-107：属性全部一致、但渠道流水号不同的 Payment 不得配对本意图。"""
        intent = self._processing_intent("wb27-bind-ref")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk,
            provider_code="mock", provider_transaction_id="WB27-TXN-REAL",
            amount="200.00", currency="CNY", channel_status="SUCCESS",
            event_id="evt-wb27-bind-ref", signature_verified=True)
        payment = self._succeeded_payment("mock", "WB27-TXN-FOREIGN")
        with self.assertRaises(ValueError) as ctx:
            confirm_intent_success(
                property_obj=self.prop, intent_id=intent.pk,
                provider_transaction_id="WB27-TXN-REAL",
                confirmed_payment=payment, actor=self.owner)
        self.assertIn("渠道流水号", str(ctx.exception))
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, PaymentIntent.Status.SUCCEEDED)
        self.assertIsNone(intent.confirmed_payment_id)

    def test_wb107_rejects_payment_with_foreign_provider(self):
        """WB-107：收款事实渠道与渠道交易渠道不一致时拒绝确认。"""
        intent = self._processing_intent("wb27-bind-provider")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk,
            provider_code="mock", provider_transaction_id="WB27-TXN-PROV",
            amount="200.00", currency="CNY", channel_status="SUCCESS",
            event_id="evt-wb27-bind-provider", signature_verified=True)
        payment = self._succeeded_payment("alipay", "WB27-TXN-PROV")
        with self.assertRaises(ValueError) as ctx:
            confirm_intent_success(
                property_obj=self.prop, intent_id=intent.pk,
                provider_transaction_id="WB27-TXN-PROV",
                confirmed_payment=payment, actor=self.owner)
        self.assertIn("支付渠道", str(ctx.exception))

    # ---------------- WB-090 ----------------
    def test_wb090_unknown_channel_fact_blocks_expiry_and_creates_task(self):
        """WB-090：渠道 UNKNOWN 时不可释放 Hold/过期，并落可重试查询任务。"""
        intent = self._processing_intent("wb27-expire-unknown")
        intent.status = PaymentIntent.Status.PENDING
        intent.save(update_fields=("status",))
        intent.expires_at = timezone.now() - timedelta(seconds=5)
        intent.save(update_fields=("expires_at",))
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk,
            provider_code="mock", provider_transaction_id="WB27-TXN-UNKNOWN",
            amount="200.00", currency="CNY", channel_status="UNKNOWN",
            event_id="evt-wb27-unknown")
        with self.assertRaises(ValueError) as ctx:
            expire_intent(property_obj=self.prop, intent_id=intent.pk,
                          actor=self.owner)
        self.assertIn("渠道状态未知", str(ctx.exception))
        self.assertTrue(PaymentCompensationTask.objects.filter(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            aggregate_type="PaymentIntent",
            aggregate_id=intent.pk).exists(),
            "UNKNOWN 必须有持久化渠道查询补偿任务（生产可达）")
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, PaymentIntent.Status.EXPIRED)

    # ---------------- WB-098 ----------------
    def _pending_with_policy(self):
        self.reservation.status = Reservation.Status.PENDING
        self.reservation.save(update_fields=("status",))
        EarlyDeparturePolicy.objects.create(
            rate_plan=self.rate,
            policy_type=EarlyDeparturePolicy.PolicyType.FIRST_NIGHT,
            is_active=True)
        self.line.refresh_from_db()
        self.assertFalse(self.line.early_departure_snapshot)

    def test_wb098_online_payment_callback_writes_confirmation_snapshot(self):
        """WB-098：在线支付成功确认必须写政策快照，且不受后续政策变更影响。"""
        from apps.billing.services import process_mock_payment_callback
        self._pending_with_policy()
        payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="WB27-CALLBACK-1", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.PENDING)
        # 隔离库存子系统，专注断言「确认事实落库同事务内写快照」。
        with mock.patch(
                "apps.billing.services.confirm_inventory_for_reservation"):
            result = process_mock_payment_callback(
                provider_ref=payment.provider_ref, amount="200.00",
                status="SUCCESS", signature_verified=True)
        self.assertTrue(result["reservation_confirmed"])
        self.line.refresh_from_db()
        self.assertEqual(
            self.line.early_departure_snapshot["policy_type"], "FIRST_NIGHT")
        # 确认后新增/启用另一政策，历史订单快照不得被改写
        EarlyDeparturePolicy.objects.create(
            rate_plan=self.rate,
            policy_type=EarlyDeparturePolicy.PolicyType.REMAINING_NIGHTS,
            is_active=True)
        self.line.refresh_from_db()
        self.assertEqual(
            self.line.early_departure_snapshot["policy_type"], "FIRST_NIGHT")

    def test_wb098_front_desk_deposit_writes_confirmation_snapshot(self):
        """WB-098：前台押金确认路径同样必须写政策快照。"""
        from apps.billing.services_v2 import BillingService
        self._pending_with_policy()
        with mock.patch(
                "apps.inventory.services.confirm_inventory_for_reservation"):
            BillingService.record_deposit(
                property_obj=self.prop, reservation=self.reservation,
                amount="60.00", method="CARD",
                idempotency_key="wb27-deposit-1", received_by=self.owner)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)
        self.line.refresh_from_db()
        self.assertEqual(
            self.line.early_departure_snapshot["policy_type"], "FIRST_NIGHT")

    # ---------------- WB-102 ----------------
    def test_wb102_audit_failure_rolls_back_document_and_index(self):
        """WB-102：审计落库失败 → 证件与物业级占用索引一并回滚。"""
        from apps.guests.models import GuestDocument, GuestDocumentIndex
        from apps.guests.services import register_document

        def boom(*args, **kwargs):
            raise OSError("故障注入：证件审计落库失败")

        with mock.patch.object(AuditEvent, "record", staticmethod(boom)):
            with self.assertRaises(OSError):
                register_document(
                    guest=self.guest, doc_type="ID_CARD",
                    doc_no="WB27DOC0001")
        self.assertFalse(GuestDocument.objects.filter(
            guest=self.guest).exists(), "证件不得在审计失败后残留")
        self.assertFalse(GuestDocumentIndex.objects.filter(
            property=self.prop).exists(), "索引不得在审计失败后残留")

    # ---------------- WB-EMP-01 / WB-041 ----------------
    def test_wb_emp01_active_key_released_on_termination_and_reused(self):
        """WB-EMP-01：唯一键随状态切换维护；WB-041：查询优先返回新 ACTIVE 任职。"""
        from apps.identity.services import EmployeeLifecycleService
        user = User.objects.create_user(username="wb27-rehire-user")
        profile = StaffProfile.objects.create(user=user, full_name="再入职员工")
        old = PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=profile, employee_no="FO-0001",
            status=PropertyStaffMembership.Status.ACTIVE)
        self.assertEqual(
            old.active_membership_key, f"{self.prop.pk}:{profile.pk}")
        old.status = PropertyStaffMembership.Status.TERMINATED
        old.save(update_fields=("status",))
        self.assertIsNone(old.active_membership_key,
                          "离职后必须释放唯一键，否则重新入职无法建档")
        new = PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=profile, employee_no="FO-0002",
            status=PropertyStaffMembership.Status.ACTIVE)
        self.assertEqual(
            new.active_membership_key, f"{self.prop.pk}:{profile.pk}")
        self.assertEqual(
            EmployeeLifecycleService.get_membership(profile, self.prop).pk,
            new.pk, "重新入职后不得再取到最旧的 TERMINATED 任职")

    # ---------------- WB-079 ----------------
    def test_wb079_legacy_reversal_entry_is_retired(self):
        """WB-079：旧冲销入口不得再直接写 LedgerEntry。"""
        from apps.billing.services import PaymentConflict, reverse_ledger_entry
        with self.assertRaises(PaymentConflict) as ctx:
            reverse_ledger_entry(entry_id=1, reason="回归测试")
        self.assertIn("退役", str(ctx.exception))
        self.assertIn("BillingService.reverse_entry", str(ctx.exception))
