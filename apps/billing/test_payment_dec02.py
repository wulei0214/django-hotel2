# -*- coding: utf-8 -*-
"""支付意图/渠道交易/补偿任务测试（PAY-P0-01~05 + D2/D3/D6）。"""
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import (
    Folio,
    LedgerEntry,
    PaymentIntent,
    PaymentCompensationTask,
    ProviderTransaction,
)
from apps.billing.payment_intents import (
    complete_compensation_task,
    confirm_intent_success,
    create_compensation_task,
    create_intent,
    expire_intent,
    extend_intent_window,
    get_channel_window,
    lease_compensation_task,
    record_provider_event,
    replay_dead_letter_task,
)
from apps.core.models import AuditEvent, Property
from apps.reservations.models import Reservation


@override_settings(DEBUG=True, DJANGO_ENV="test")
class PaymentIntentTests(TestCase):
    """PAY-P0-01: 支付意图创建/状态迁移/窗口策略。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.owner = User.objects.create_user(username="pi_owner", password="x")
        from apps.guests.models import Guest
        cls.guest = Guest.objects.create(property=cls.prop, full_name="意图测试客")
        from apps.reservations.models import ReservationRoom
        from apps.inventory.models import Room, RoomType, RatePlan
        cls.rt = RoomType.objects.create(
            property=cls.prop, code="PI-STD", name="意图测试房型", capacity=2)
        cls.rate = RatePlan.objects.create(
            property=cls.prop, code="PI-BAR", name="意图测试门市价",
            valid_from=timezone.localdate())
        cls.reservation = Reservation.objects.create(
            property=cls.prop, guest=cls.guest, booking_no="PI-TEST-001",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(), check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("200.00"), currency="CNY")
        ReservationRoom.objects.create(
            property=cls.prop, reservation=cls.reservation,
            room_type=cls.rt, rate_plan=cls.rate,
            check_in=timezone.localdate(), check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal("200.00"))
        cls.folio = Folio.objects.create(
            property=cls.prop, reservation=cls.reservation,
            guest=cls.guest, currency="CNY")

    def _create(self, key="pi-001"):
        return create_intent(
            property_obj=self.prop, reservation=self.reservation,
            folio=self.folio, amount="200.00",
            created_by=self.owner, idempotency_key=key)

    def test_create_intent_and_idempotent_replay(self):
        intent, created = self._create()
        self.assertTrue(created)
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
        self.assertTrue(intent.expires_at is not None)
        intent2, created2 = self._create()
        self.assertFalse(created2)
        self.assertEqual(intent.pk, intent2.pk)
        with self.assertRaises(ValueError):
            create_intent(
                property_obj=self.prop, reservation=self.reservation,
                folio=self.folio, amount="999.00",
                created_by=self.owner, idempotency_key="pi-001")

    def test_extend_window_with_limits(self):
        intent, _ = self._create("pi-002")
        extended = extend_intent_window(
            intent_id=intent.pk, property_obj=self.prop, extended_by=self.owner)
        self.assertEqual(extended.extension_count, 1)
        window = get_channel_window("mock")
        if window["max_extensions"] <= 1:
            with self.assertRaises(ValueError):
                extend_intent_window(
                    intent_id=intent.pk, property_obj=self.prop,
                    extended_by=self.owner)

    def test_confirm_success_idempotent(self):
        intent, _ = self._create("pi-003")
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=("status",))
        pt, _ = record_provider_event(
            property_obj=self.prop, intent_id=intent.pk,
            provider_code="mock", provider_transaction_id="MOCK-TXN-001",
            amount="200.00", currency="CNY", channel_status="SUCCESS",
            event_id="evt-001", signature_verified=True)
        confirmed, changed = confirm_intent_success(
            property_obj=self.prop, intent_id=intent.pk,
            provider_transaction_id="MOCK-TXN-001", actor=self.owner)
        self.assertTrue(changed)
        self.assertEqual(confirmed.status, PaymentIntent.Status.SUCCEEDED)
        # 重复确认幂等
        confirmed2, changed2 = confirm_intent_success(
            property_obj=self.prop, intent_id=intent.pk,
            provider_transaction_id="MOCK-TXN-001", actor=self.owner)
        self.assertFalse(changed2)
        # 重复事件幂等
        pt2, pt2_created = record_provider_event(
            property_obj=self.prop, intent_id=intent.pk,
            provider_code="mock", provider_transaction_id="MOCK-TXN-001",
            amount="200.00", currency="CNY", channel_status="SUCCESS",
            event_id="evt-001", signature_verified=True)
        self.assertFalse(pt2_created)

    def test_expire_intent_unknown_not_expired(self):
        intent, _ = self._create("pi-004")
        intent.status = PaymentIntent.Status.UNKNOWN
        intent.save(update_fields=("status",))
        with self.assertRaises(ValueError):
            expire_intent(property_obj=self.prop, intent_id=intent.pk)

    def test_provider_transaction_unique_constraint(self):
        intent, _ = self._create("pi-005")
        ProviderTransaction.objects.create(
            property=self.prop, intent=intent, provider_code="mock",
            provider_transaction_id="TXN-U1", amount=Decimal("200.00"),
            currency="CNY", channel_status="SUCCESS")
        with self.assertRaises(Exception):
            ProviderTransaction.objects.create(
                property=self.prop, intent=intent, provider_code="mock",
                provider_transaction_id="TXN-U1", amount=Decimal("200.00"),
                currency="CNY", channel_status="SUCCESS")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class CompensationTaskTests(TestCase):
    """PAY-P0-03: 持久化补偿队列（租约/退避/死信/重放）。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")

    def _create_task(self, key="task-001", max_attempts=2):
        return create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            aggregate_type="PaymentIntent", aggregate_id=999,
            idempotency_key=key, max_attempts=max_attempts)

    def test_create_and_lease(self):
        task, created = self._create_task()
        self.assertTrue(created)
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        leased = lease_compensation_task(task.pk, "worker-1", lease_seconds=60)
        self.assertEqual(leased.status, PaymentCompensationTask.Status.LEASED)
        self.assertEqual(leased.lease_owner, "worker-1")
        # 第二个 worker 抢占被拒
        with self.assertRaises(ValueError):
            lease_compensation_task(task.pk, "worker-2", lease_seconds=60)

    def test_retry_then_dead_letter(self):
        task, _ = self._create_task(key="task-002", max_attempts=2)
        lease_compensation_task(task.pk, "worker-1")
        complete_compensation_task(task.pk, success=False,
                                   error_code="CHANNEL_TIMEOUT",
                                   error_summary="渠道查询超时")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.attempt_count, 1)
        # 第二次也失败 → 死信
        lease_compensation_task(task.pk, "worker-1")
        complete_compensation_task(task.pk, success=False,
                                   error_code="CHANNEL_TIMEOUT",
                                   error_summary="渠道查询超时")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.DEAD_LETTER)

    def test_replay_dead_letter_resets(self):
        task, _ = self._create_task(key="task-003", max_attempts=1)
        lease_compensation_task(task.pk, "worker-1")
        complete_compensation_task(task.pk, success=False,
                                   error_code="E", error_summary="E")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.DEAD_LETTER)
        replayed = replay_dead_letter_task(
            task.pk, operator="admin_user", reason="人工核查后重放")
        self.assertEqual(replayed.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(replayed.attempt_count, 0)

    def test_idempotent_task_creation(self):
        task1, c1 = self._create_task(key="task-004")
        task2, c2 = self._create_task(key="task-004")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(task1.pk, task2.pk)

    def test_successful_completion(self):
        task, _ = self._create_task(key="task-005")
        lease_compensation_task(task.pk, "worker-1")
        complete_compensation_task(task.pk, success=True)
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertIsNotNone(task.completed_at)
