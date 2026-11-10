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
    Payment,
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
        # WB-011：确认成功必须关联真实 Payment（不得出现意图成功但无收款事实）
        payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="MOCK-TXN-001", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        confirmed, changed = confirm_intent_success(
            property_obj=self.prop, intent_id=intent.pk,
            provider_transaction_id="MOCK-TXN-001",
            confirmed_payment=payment, actor=self.owner)
        self.assertTrue(changed)
        self.assertEqual(confirmed.status, PaymentIntent.Status.SUCCEEDED)
        self.assertEqual(confirmed.confirmed_payment_id, payment.pk)
        # 重复确认幂等
        confirmed2, changed2 = confirm_intent_success(
            property_obj=self.prop, intent_id=intent.pk,
            provider_transaction_id="MOCK-TXN-001",
            confirmed_payment=payment, actor=self.owner)
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

    def test_create_intent_replay_rejects_currency_purpose_provider(self):
        """WB-069：幂等重放比较补齐 currency/purpose/provider_code。"""
        intent, created = self._create("pi-wb069")
        self.assertTrue(created)
        base = dict(
            property_obj=self.prop, reservation=self.reservation,
            folio=self.folio, amount="200.00", created_by=self.owner)
        # purpose 不一致 → 拒绝
        with self.assertRaises(ValueError):
            create_intent(**base, idempotency_key="pi-wb069", purpose="DEPOSIT")
        # provider_code 不一致 → 拒绝
        with self.assertRaises(ValueError):
            create_intent(**base, idempotency_key="pi-wb069",
                          provider_code="alipay")
        # currency 不一致 → 拒绝（直接落一条异币种历史意图绕过 WB-071 前置校验）
        PaymentIntent.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            intent_no="PI-WB069-USD", idempotency_key="pi-wb069-usd",
            amount=Decimal("200.00"), currency="USD",
            purpose=PaymentIntent.Purpose.ROOM_FEE, provider_code="mock",
            status=PaymentIntent.Status.PENDING)
        with self.assertRaises(ValueError):
            create_intent(**base, idempotency_key="pi-wb069-usd")
        # 完全一致的重放仍命中同一意图
        replayed, replay_created = self._create("pi-wb069")
        self.assertFalse(replay_created)
        self.assertEqual(replayed.pk, intent.pk)

    def test_create_intent_currency_must_match_folio_and_reservation(self):
        """WB-071：传入币种必须与账单及预订币种一致。"""
        with self.assertRaises(ValueError):
            create_intent(
                property_obj=self.prop, reservation=self.reservation,
                folio=self.folio, amount="200.00", currency="USD",
                created_by=self.owner, idempotency_key="pi-wb071")
        self.assertFalse(
            PaymentIntent.objects.filter(idempotency_key="pi-wb071").exists())

    def test_confirm_success_payment_ownership_validated(self):
        """WB-072：确认前对收款事实做完整归属/金额/币种/状态/占用校验。"""
        intent, _ = self._create("pi-wb072")
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=("status",))

        def _confirm(payment):
            return confirm_intent_success(
                property_obj=self.prop, intent_id=intent.pk,
                provider_transaction_id="MOCK-TXN-WB072",
                confirmed_payment=payment, actor=self.owner)

        # 非 SUCCEEDED 的收款事实不可确认
        pending_payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="MOCK-WB072-PENDING", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.PENDING)
        with self.assertRaises(ValueError):
            _confirm(pending_payment)
        # 金额与意图不一致 → 拒绝
        short_payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="MOCK-WB072-SHORT", method="MOCK",
            amount=Decimal("100.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        with self.assertRaises(ValueError):
            _confirm(short_payment)
        # 币种与意图不一致 → 拒绝
        usd_payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="MOCK-WB072-USD", method="MOCK",
            amount=Decimal("200.00"), currency="USD",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        with self.assertRaises(ValueError):
            _confirm(usd_payment)
        # 已被其他意图关联的收款事实不可复用
        claimed_payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="MOCK-WB072-DUP", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        intent_b, _ = self._create("pi-wb072-b")
        intent_b.status = PaymentIntent.Status.PROCESSING
        intent_b.save(update_fields=("status",))
        confirmed_b, changed_b = confirm_intent_success(
            property_obj=self.prop, intent_id=intent_b.pk,
            provider_transaction_id="MOCK-TXN-WB072-B",
            confirmed_payment=claimed_payment, actor=self.owner)
        self.assertTrue(changed_b)
        self.assertEqual(confirmed_b.status, PaymentIntent.Status.SUCCEEDED)
        with self.assertRaises(ValueError):
            _confirm(claimed_payment)
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, PaymentIntent.Status.SUCCEEDED)
        self.assertIsNone(intent.confirmed_payment)

    def test_confirm_inventory_failure_creates_compensation_task(self):
        """WB-073：库存确认失败时创建持久化 HOLD_COORDINATION 补偿任务。"""
        intent, _ = self._create("pi-wb073")
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=("status",))
        payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="MOCK-WB073", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        # 预订回退 PENDING 且无库存 Hold 行 → confirm_inventory 抛 InventoryUnavailable
        Reservation.objects.filter(pk=self.reservation.pk).update(
            status=Reservation.Status.PENDING)
        confirmed, changed = confirm_intent_success(
            property_obj=self.prop, intent_id=intent.pk,
            provider_transaction_id="MOCK-TXN-WB073",
            confirmed_payment=payment, actor=self.owner)
        # 资金事实保留：意图仍成功并关联收款
        self.assertTrue(changed)
        self.assertEqual(confirmed.status, PaymentIntent.Status.SUCCEEDED)
        self.assertEqual(confirmed.confirmed_payment_id, payment.pk)
        # 持久化补偿任务已创建（幂等键绑定意图）
        task = PaymentCompensationTask.objects.get(
            idempotency_key=f"intent-inventory-confirm:{intent.pk}")
        self.assertEqual(
            task.task_type, PaymentCompensationTask.TaskType.HOLD_COORDINATION)
        self.assertEqual(task.aggregate_type, "PaymentIntent")
        self.assertEqual(task.aggregate_id, intent.pk)
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.last_error_code, "INVENTORY_CONFIRM_UNAVAILABLE")
        self.assertIn("库存确认失败", task.last_error_summary)
        self.assertIn(str(self.reservation.pk), task.last_error_summary)
        self.assertIn(intent.intent_no, task.last_error_summary)
        # 保留审计：补偿触发事件已落库
        self.assertTrue(AuditEvent.objects.filter(
            action="billing.intent_inventory_compensation_created",
            entity_id=str(task.pk)).exists())

    def test_record_provider_event_validates_first_write(self):
        """WB-076/089：首次写入校验渠道/金额/币种/状态取值/验签。"""
        intent, _ = self._create("pi-wb076")
        self.assertEqual(intent.provider_code, "mock")
        base = dict(
            property_obj=self.prop, intent_id=intent.pk,
            provider_transaction_id="MOCK-WB076")
        # 渠道代码与意图不一致 → 拒绝
        with self.assertRaises(ValueError):
            record_provider_event(**base, provider_code="alipay",
                                  amount="200.00", currency="CNY",
                                  channel_status="PENDING",
                                  event_id="evt-wb076-1")
        # 金额与意图不一致 → 拒绝
        with self.assertRaises(ValueError):
            record_provider_event(**base, provider_code="mock",
                                  amount="100.00", currency="CNY",
                                  channel_status="PENDING",
                                  event_id="evt-wb076-2")
        # 币种与意图不一致 → 拒绝
        with self.assertRaises(ValueError):
            record_provider_event(**base, provider_code="mock",
                                  amount="200.00", currency="USD",
                                  channel_status="PENDING",
                                  event_id="evt-wb076-3")
        # 渠道状态不在允许取值内 → 拒绝
        with self.assertRaises(ValueError):
            record_provider_event(**base, provider_code="mock",
                                  amount="200.00", currency="CNY",
                                  channel_status="WHATEVER",
                                  event_id="evt-wb076-4")
        # SUCCESS 但未验签 → 拒绝
        with self.assertRaises(ValueError):
            record_provider_event(**base, provider_code="mock",
                                  amount="200.00", currency="CNY",
                                  channel_status="SUCCESS",
                                  event_id="evt-wb076-5",
                                  signature_verified=False)
        # 合法成功事实可写入
        pt, created = record_provider_event(
            **base, provider_code="mock", amount="200.00", currency="CNY",
            channel_status="SUCCESS",
            event_id="evt-wb076-6", signature_verified=True)
        self.assertTrue(created)
        self.assertEqual(pt.channel_status, "SUCCESS")
        self.assertTrue(pt.signature_verified)

    def test_expire_intent_requires_window_elapsed(self):
        """WB-077/090：支付窗口未到期（或缺失）的意图不可过期。"""
        intent, _ = self._create("pi-wb077")  # 初始窗口 15 分钟，尚未到期
        with self.assertRaises(ValueError):
            expire_intent(property_obj=self.prop, intent_id=intent.pk)
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
        # expires_at 缺失同样不可过期
        intent.expires_at = None
        intent.save(update_fields=("expires_at",))
        with self.assertRaises(ValueError):
            expire_intent(property_obj=self.prop, intent_id=intent.pk)
        # 真实到期后方可过期
        intent.expires_at = timezone.now() - timedelta(seconds=1)
        intent.save(update_fields=("expires_at",))
        expired = expire_intent(property_obj=self.prop, intent_id=intent.pk)
        self.assertEqual(expired.status, PaymentIntent.Status.EXPIRED)

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
        # WB-067：完成任务必须声明与租约一致的 lease_owner（旧测试同步新约束）
        complete_compensation_task(task.pk, success=False,
                                   error_code="CHANNEL_TIMEOUT",
                                   error_summary="渠道查询超时",
                                   lease_owner="worker-1")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.attempt_count, 1)
        # 第二次也失败 → 死信
        lease_compensation_task(task.pk, "worker-1")
        complete_compensation_task(task.pk, success=False,
                                   error_code="CHANNEL_TIMEOUT",
                                   error_summary="渠道查询超时",
                                   lease_owner="worker-1")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.DEAD_LETTER)

    def test_replay_dead_letter_resets(self):
        task, _ = self._create_task(key="task-003", max_attempts=1)
        lease_compensation_task(task.pk, "worker-1")
        complete_compensation_task(task.pk, success=False,
                                   error_code="E", error_summary="E",
                                   lease_owner="worker-1")
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
        complete_compensation_task(task.pk, success=True, lease_owner="worker-1")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertIsNotNone(task.completed_at)

    def test_complete_requires_matching_unexpired_lease(self):
        """WB-067：完成任务必须持有有效租约，空值不得绕过比对。"""
        task, _ = self._create_task(key="task-wb067")
        # 未领取（无租约持有者）的任务不可完成
        with self.assertRaises(ValueError):
            complete_compensation_task(task.pk, success=True,
                                       lease_owner="worker-1")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        # 领取后：调用方必须声明身份，且不得与他人租约冲突
        lease_compensation_task(task.pk, "worker-1", lease_seconds=60)
        with self.assertRaises(ValueError):
            complete_compensation_task(task.pk, success=True, lease_owner="")
        with self.assertRaises(ValueError):
            complete_compensation_task(task.pk, success=True,
                                       lease_owner="worker-2")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.LEASED)
        # 租约过期视为失效，原持有者也不得提交
        task.lease_expires_at = timezone.now() - timedelta(seconds=1)
        task.save(update_fields=("lease_expires_at",))
        with self.assertRaises(ValueError):
            complete_compensation_task(task.pk, success=True,
                                       lease_owner="worker-1")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.LEASED)
        # 有效租约持有者可正常完成
        task.lease_expires_at = timezone.now() + timedelta(seconds=60)
        task.save(update_fields=("lease_expires_at",))
        done = complete_compensation_task(task.pk, success=True,
                                          lease_owner="worker-1")
        self.assertEqual(done.status, PaymentCompensationTask.Status.SUCCEEDED)
