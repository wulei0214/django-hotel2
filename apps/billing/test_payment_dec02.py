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

    # ------------------------------------------------------------------
    # WB-108：create_intent 预订侧前置校验（状态/窗口/Hold/金额）
    # ------------------------------------------------------------------
    def test_create_intent_rejects_terminal_or_inhouse_status(self):
        """WB-108②：取消/过期/已入住预订不得再创建支付意图。"""
        for status in (Reservation.Status.CANCELLED,
                       Reservation.Status.EXPIRED,
                       Reservation.Status.CHECKED_IN):
            Reservation.objects.filter(pk=self.reservation.pk).update(
                status=status)
            with self.assertRaises(ValueError):
                self._create("pi-wb108-status")
        self.assertFalse(PaymentIntent.objects.filter(
            idempotency_key="pi-wb108-status").exists())

    def test_create_intent_rejects_missing_or_expired_payment_window(self):
        """WB-108③：PENDING 缺支付截止时间或已过期 → 拒绝。"""
        Reservation.objects.filter(pk=self.reservation.pk).update(
            status=Reservation.Status.PENDING)
        Reservation.objects.filter(pk=self.reservation.pk).update(
            expires_at=None)
        with self.assertRaises(ValueError):
            self._create("pi-wb108-window-1")
        Reservation.objects.filter(pk=self.reservation.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            self._create("pi-wb108-window-2")
        self.assertFalse(PaymentIntent.objects.filter(
            idempotency_key__startswith="pi-wb108-window").exists())

    def test_create_intent_requires_live_hold_for_pending(self):
        """WB-108④：PENDING 无有效 Hold（库存计数口径）→ 拒绝；补足后可创建。"""
        Reservation.objects.filter(pk=self.reservation.pk).update(
            status=Reservation.Status.PENDING,
            expires_at=timezone.now() + timedelta(minutes=10))
        with self.assertRaises(ValueError):
            self._create("pi-wb108-hold-none")
        self.assertFalse(PaymentIntent.objects.filter(
            idempotency_key="pi-wb108-hold-none").exists())
        # 补足有效 Hold 计数（与 confirm_inventory_for_reservation 同口径：
        # 每个住宿日库存行 held ≥ 预订需求量）后可正常创建
        from apps.inventory.models import RoomTypeInventory
        RoomTypeInventory.objects.create(
            property=self.prop, room_type=self.rt,
            stay_date=timezone.localdate(), total_rooms=5, held=1)
        intent, created = self._create("pi-wb108-hold-ok")
        self.assertTrue(created)
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)

    def test_create_intent_amount_must_equal_reservation_total(self):
        """WB-108⑤：意图金额必须等于订单应付总额（公开链路全款口径）。"""
        with self.assertRaises(ValueError) as ctx:
            create_intent(
                property_obj=self.prop, reservation=self.reservation,
                folio=self.folio, amount="150.00",
                created_by=self.owner, idempotency_key="pi-wb108-amt")
        self.assertIn("应付总额", str(ctx.exception))
        self.assertFalse(PaymentIntent.objects.filter(
            idempotency_key="pi-wb108-amt").exists())

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
            # WB-107：真实渠道链路中 Payment.provider_ref 即渠道交易号，
            # 夹具必须与本次确认使用的 provider_transaction_id 一致。
            provider="mock", provider_ref="MOCK-TXN-WB072-B", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        intent_b, _ = self._create("pi-wb072-b")
        intent_b.status = PaymentIntent.Status.PROCESSING
        intent_b.save(update_fields=("status",))
        # WB-107：确认前必须存在本意图名下已验签的 SUCCESS 渠道交易
        record_provider_event(
            property_obj=self.prop, intent_id=intent_b.pk,
            provider_code="mock", provider_transaction_id="MOCK-TXN-WB072-B",
            amount="200.00", currency="CNY", channel_status="SUCCESS",
            event_id="evt-wb072-b", signature_verified=True)
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

    # ------------------------------------------------------------------
    # WB-107：确认成功前核验渠道交易事实
    # ------------------------------------------------------------------
    def _processing_intent(self, key):
        intent, _ = self._create(key)
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=("status",))
        return intent

    def _succeeded_payment(self, provider_ref):
        return Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref=provider_ref, method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())

    def test_confirm_success_happy_path_with_channel_transaction(self):
        """WB-107：已验签 SUCCESS 渠道交易 + 合法 Payment → 正常确认成功。"""
        intent = self._processing_intent("pi-wb107-ok")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk,
            provider_code="mock", provider_transaction_id="WB107-TXN-OK",
            amount="200.00", currency="CNY", channel_status="SUCCESS",
            event_id="evt-wb107-ok", signature_verified=True)
        payment = self._succeeded_payment("WB107-TXN-OK")
        confirmed, changed = confirm_intent_success(
            property_obj=self.prop, intent_id=intent.pk,
            provider_transaction_id="WB107-TXN-OK",
            confirmed_payment=payment, actor=self.owner)
        self.assertTrue(changed)
        self.assertEqual(confirmed.status, PaymentIntent.Status.SUCCEEDED)
        self.assertEqual(confirmed.confirmed_payment_id, payment.pk)

    def test_confirm_success_rejects_missing_provider_transaction(self):
        """WB-107：本意图名下不存在该渠道交易号 → 拒绝确认。"""
        intent = self._processing_intent("pi-wb107-missing")
        payment = self._succeeded_payment("WB107-TXN-MISSING")
        with self.assertRaises(ValueError) as ctx:
            confirm_intent_success(
                property_obj=self.prop, intent_id=intent.pk,
                provider_transaction_id="WB107-TXN-MISSING",
                confirmed_payment=payment, actor=self.owner)
        self.assertIn("渠道交易不存在或不属于该意图", str(ctx.exception))
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PROCESSING)
        self.assertIsNone(intent.confirmed_payment)

    def test_confirm_success_rejects_foreign_intent_transaction(self):
        """WB-107：渠道交易属于其他意图（或物业）→ 拒绝确认。"""
        intent = self._processing_intent("pi-wb107-foreign")
        intent_b = self._processing_intent("pi-wb107-foreign-b")
        record_provider_event(
            property_obj=self.prop, intent_id=intent_b.pk,
            provider_code="mock", provider_transaction_id="WB107-TXN-B",
            amount="200.00", currency="CNY", channel_status="SUCCESS",
            event_id="evt-wb107-b", signature_verified=True)
        payment = self._succeeded_payment("WB107-TXN-B")
        with self.assertRaises(ValueError) as ctx:
            confirm_intent_success(
                property_obj=self.prop, intent_id=intent.pk,
                provider_transaction_id="WB107-TXN-B",
                confirmed_payment=payment, actor=self.owner)
        self.assertIn("渠道交易不存在或不属于该意图", str(ctx.exception))
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, PaymentIntent.Status.SUCCEEDED)

    def test_confirm_success_rejects_pending_channel_status(self):
        """WB-107：渠道交易状态非 SUCCESS（如 PENDING）→ 拒绝确认。"""
        intent = self._processing_intent("pi-wb107-pending")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk,
            provider_code="mock", provider_transaction_id="WB107-TXN-PENDING",
            amount="200.00", currency="CNY", channel_status="PENDING")
        payment = self._succeeded_payment("WB107-TXN-PENDING")
        with self.assertRaises(ValueError) as ctx:
            confirm_intent_success(
                property_obj=self.prop, intent_id=intent.pk,
                provider_transaction_id="WB107-TXN-PENDING",
                confirmed_payment=payment, actor=self.owner)
        self.assertIn("SUCCESS", str(ctx.exception))
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PROCESSING)

    def test_confirm_success_rejects_unsigned_success_transaction(self):
        """WB-107：SUCCESS 渠道交易未通过验签 → 拒绝确认（防御直写脏数据）。"""
        intent = self._processing_intent("pi-wb107-unsigned")
        # record_provider_event 会拒绝未验签 SUCCESS 首写，这里直写模拟历史脏数据
        ProviderTransaction.objects.create(
            property=self.prop, intent=intent, provider_code="mock",
            provider_transaction_id="WB107-TXN-UNSIGNED",
            amount=Decimal("200.00"), currency="CNY",
            channel_status="SUCCESS", signature_verified=False)
        payment = self._succeeded_payment("WB107-TXN-UNSIGNED")
        with self.assertRaises(ValueError) as ctx:
            confirm_intent_success(
                property_obj=self.prop, intent_id=intent.pk,
                provider_transaction_id="WB107-TXN-UNSIGNED",
                confirmed_payment=payment, actor=self.owner)
        self.assertIn("未通过验签", str(ctx.exception))
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PROCESSING)

    def test_confirm_success_rejects_amount_mismatch_transaction(self):
        """WB-107：渠道交易金额/币种与意图不一致 → 拒绝确认（防御直写脏数据）。"""
        intent = self._processing_intent("pi-wb107-amount")
        ProviderTransaction.objects.create(
            property=self.prop, intent=intent, provider_code="mock",
            provider_transaction_id="WB107-TXN-SHORT",
            amount=Decimal("100.00"), currency="CNY",
            channel_status="SUCCESS", signature_verified=True)
        payment = self._succeeded_payment("WB107-TXN-SHORT")
        with self.assertRaises(ValueError) as ctx:
            confirm_intent_success(
                property_obj=self.prop, intent_id=intent.pk,
                provider_transaction_id="WB107-TXN-SHORT",
                confirmed_payment=payment, actor=self.owner)
        self.assertIn("金额或币种", str(ctx.exception))
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PROCESSING)

    def test_confirm_inventory_failure_creates_compensation_task(self):
        """WB-073：库存确认失败时创建持久化 HOLD_COORDINATION 补偿任务。"""
        intent, _ = self._create("pi-wb073")
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=("status",))
        payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            # WB-107：Payment.provider_ref 与渠道交易号同源（真实回调链路）。
            provider="mock", provider_ref="MOCK-TXN-WB073", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        # WB-107：先落本意图名下已验签的 SUCCESS 渠道交易事实
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk,
            provider_code="mock", provider_transaction_id="MOCK-TXN-WB073",
            amount="200.00", currency="CNY", channel_status="SUCCESS",
            event_id="evt-wb073", signature_verified=True)
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


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ProviderEventReplayFactTests(TestCase):
    """WB-115：渠道事件幂等重放必须核对完整不可变事实。

    验收矩阵（审查报告 WB-115）：
      * SUCCESS/已验签 重放 SUCCESS/已验签 → 幂等返回；
      * SUCCESS/已验签 重放 FAILED/已验签 → 拒绝 + 安全审计；
      * SUCCESS/已验签 重放 SUCCESS/未验签 → 拒绝 + 安全审计；
      * PENDING/未验签 重放 SUCCESS/已验签（同事件 ID）→ 拒绝 + 安全审计；
      * 同事件 ID 不同金额 / 不同意图 → 拒绝（WB-012 既有语义 + 审计）。
    渠道状态变化必须使用新的渠道事件 ID，不得复用旧事件 ID 翻转事实。
    """

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.owner = User.objects.create_user(username="pi_wb115", password="x")
        from apps.guests.models import Guest
        cls.guest = Guest.objects.create(property=cls.prop, full_name="WB115 客")
        from apps.reservations.models import ReservationRoom
        from apps.inventory.models import RoomType, RatePlan
        cls.rt = RoomType.objects.create(
            property=cls.prop, code="PI-WB115", name="WB115 房型", capacity=2)
        cls.rate = RatePlan.objects.create(
            property=cls.prop, code="PI-WB115-BAR", name="WB115 门市价",
            valid_from=timezone.localdate())
        cls.reservation = Reservation.objects.create(
            property=cls.prop, guest=cls.guest, booking_no="PI-WB115-001",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("200.00"), currency="CNY")
        ReservationRoom.objects.create(
            property=cls.prop, reservation=cls.reservation,
            room_type=cls.rt, rate_plan=cls.rate,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal("200.00"))
        cls.folio = Folio.objects.create(
            property=cls.prop, reservation=cls.reservation,
            guest=cls.guest, currency="CNY")

    def _intent(self, key, status=PaymentIntent.Status.PROCESSING):
        intent, _ = create_intent(
            property_obj=self.prop, reservation=self.reservation,
            folio=self.folio, amount="200.00",
            created_by=self.owner, idempotency_key=key)
        intent.status = status
        intent.save(update_fields=("status",))
        return intent

    @staticmethod
    def _conflict_count(event_id):
        return AuditEvent.objects.filter(
            action="billing.provider_event_replay_conflict").count()

    def test_identical_replay_is_idempotent_without_conflict_audit(self):
        intent = self._intent("pi-wb115-ok")
        base = dict(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB115-TXN-OK", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb115-ok",
            signature_verified=True)
        pt, created = record_provider_event(**base)
        self.assertTrue(created)
        before = self._conflict_count("evt-wb115-ok")
        pt2, created2 = record_provider_event(**base)
        self.assertFalse(created2)
        self.assertEqual(pt.pk, pt2.pk)
        self.assertEqual(self._conflict_count("evt-wb115-ok"), before)

    def test_replay_with_failed_status_is_rejected(self):
        intent = self._intent("pi-wb115-fail")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB115-TXN-F", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb115-f",
            signature_verified=True)
        before = self._conflict_count("evt-wb115-f")
        with self.assertRaises(ValueError) as ctx:
            record_provider_event(
                property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
                provider_transaction_id="WB115-TXN-F", amount="200.00",
                currency="CNY", channel_status="FAILED", event_id="evt-wb115-f",
                signature_verified=True)
        self.assertIn("渠道状态", str(ctx.exception))
        self.assertEqual(self._conflict_count("evt-wb115-f"), before + 1)
        # 首次记录不被第二次请求覆盖
        pt = ProviderTransaction.objects.get(event_id="evt-wb115-f")
        self.assertEqual(pt.channel_status, "SUCCESS")
        self.assertTrue(pt.signature_verified)

    def test_replay_downgrading_signature_is_rejected(self):
        intent = self._intent("pi-wb115-sig")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB115-TXN-S", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb115-s",
            signature_verified=True)
        before = self._conflict_count("evt-wb115-s")
        with self.assertRaises(ValueError):
            record_provider_event(
                property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
                provider_transaction_id="WB115-TXN-S", amount="200.00",
                currency="CNY", channel_status="SUCCESS", event_id="evt-wb115-s",
                signature_verified=False)
        self.assertEqual(self._conflict_count("evt-wb115-s"), before + 1)
        pt = ProviderTransaction.objects.get(event_id="evt-wb115-s")
        self.assertTrue(pt.signature_verified)

    def test_replay_pending_to_success_same_event_id_is_rejected(self):
        intent = self._intent("pi-wb115-p2s")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB115-TXN-P", amount="200.00",
            currency="CNY", channel_status="PENDING", event_id="evt-wb115-p",
            signature_verified=False)
        before = self._conflict_count("evt-wb115-p")
        with self.assertRaises(ValueError):
            record_provider_event(
                property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
                provider_transaction_id="WB115-TXN-P", amount="200.00",
                currency="CNY", channel_status="SUCCESS", event_id="evt-wb115-p",
                signature_verified=True)
        self.assertEqual(self._conflict_count("evt-wb115-p"), before + 1)
        pt = ProviderTransaction.objects.get(event_id="evt-wb115-p")
        self.assertEqual(pt.channel_status, "PENDING")
        self.assertFalse(pt.signature_verified)

    def test_replay_with_different_amount_or_intent_is_rejected(self):
        intent = self._intent("pi-wb115-amt")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB115-TXN-A", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb115-a",
            signature_verified=True)
        before = self._conflict_count("evt-wb115-a")
        # 同事件 ID 不同金额
        with self.assertRaises(ValueError):
            record_provider_event(
                property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
                provider_transaction_id="WB115-TXN-A", amount="100.00",
                currency="CNY", channel_status="SUCCESS", event_id="evt-wb115-a",
                signature_verified=True)
        # 同事件 ID 不同意图
        other = self._intent("pi-wb115-amt-b")
        with self.assertRaises(ValueError):
            record_provider_event(
                property_obj=self.prop, intent_id=other.pk, provider_code="mock",
                provider_transaction_id="WB115-TXN-A", amount="200.00",
                currency="CNY", channel_status="SUCCESS", event_id="evt-wb115-a",
                signature_verified=True)
        self.assertEqual(self._conflict_count("evt-wb115-a"), before + 2)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ProviderEventProgressionTests(TestCase):
    """WB-119/125：同交易号新事件 ID 推进状态 + 主记录不可变。

    - 同交易号新事件（PENDING→SUCCESS）不得撞 uq_pt_provider_txn（WB-119），
      主记录按受控状态机推进，事件历史保留每次不可变事实；
    - SUCCESS 是终态，不得被后续 FAILED/PENDING/UNKNOWN 事件倒退；
    - 实例 save() 直接改写主记录被模型禁止（WB-125），QuerySet.update 亦然。
    """

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.owner = User.objects.create_user(username="pi_wb119", password="x")
        from apps.guests.models import Guest
        cls.guest = Guest.objects.create(property=cls.prop, full_name="WB119 客")
        from apps.reservations.models import ReservationRoom
        from apps.inventory.models import RoomType, RatePlan
        cls.rt = RoomType.objects.create(
            property=cls.prop, code="PI-WB119", name="WB119 房型", capacity=2)
        cls.rate = RatePlan.objects.create(
            property=cls.prop, code="PI-WB119-BAR", name="WB119 门市价",
            valid_from=timezone.localdate())
        cls.reservation = Reservation.objects.create(
            property=cls.prop, guest=cls.guest, booking_no="PI-WB119-001",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("200.00"), currency="CNY")
        ReservationRoom.objects.create(
            property=cls.prop, reservation=cls.reservation,
            room_type=cls.rt, rate_plan=cls.rate,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal("200.00"))
        cls.folio = Folio.objects.create(
            property=cls.prop, reservation=cls.reservation,
            guest=cls.guest, currency="CNY")

    def _intent(self, key):
        intent, _ = create_intent(
            property_obj=self.prop, reservation=self.reservation,
            folio=self.folio, amount="200.00",
            created_by=self.owner, idempotency_key=key)
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=("status",))
        return intent

    def test_new_event_id_advances_pending_to_success_on_same_txn(self):
        from apps.billing.models import ProviderTransactionEvent
        intent = self._intent("pi-wb119-p2s")
        main, created = record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-1", amount="200.00",
            currency="CNY", channel_status="PENDING", event_id="evt-wb119-1")
        self.assertTrue(created)
        self.assertEqual(main.channel_status, "PENDING")
        # 同交易号、新事件 ID：状态推进不再被交易号唯一约束阻断（WB-119）
        main2, created2 = record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-1", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb119-2",
            signature_verified=True)
        self.assertTrue(created2)  # 新事件已追加
        self.assertEqual(main2.pk, main.pk)
        main2.refresh_from_db()
        self.assertEqual(main2.channel_status, "SUCCESS")
        self.assertTrue(main2.signature_verified)
        self.assertIsNotNone(main2.provider_confirmed_at)
        # 事件历史保留两次不可变事实
        events = list(ProviderTransactionEvent.objects.filter(
            transaction=main2).order_by("pk"))
        self.assertEqual([e.channel_status for e in events],
                         ["PENDING", "SUCCESS"])
        self.assertEqual([e.event_id for e in events],
                         ["evt-wb119-1", "evt-wb119-2"])

    def test_success_is_terminal_and_cannot_regress(self):
        from apps.billing.models import ProviderTransactionEvent
        intent = self._intent("pi-wb119-terminal")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-2", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb119-t1",
            signature_verified=True)
        with self.assertRaises(ValueError) as ctx:
            record_provider_event(
                property_obj=self.prop, intent_id=intent.pk,
                provider_code="mock",
                provider_transaction_id="WB119-TXN-2", amount="200.00",
                currency="CNY", channel_status="FAILED",
                event_id="evt-wb119-t2", signature_verified=True)
        self.assertIn("终态", str(ctx.exception))
        main = ProviderTransaction.objects.get(
            provider_code="mock", provider_transaction_id="WB119-TXN-2")
        self.assertEqual(main.channel_status, "SUCCESS")
        self.assertEqual(
            ProviderTransactionEvent.objects.filter(transaction=main).count(), 1)

    def test_late_success_after_failure_notification(self):
        from apps.billing.models import ProviderTransactionEvent
        intent = self._intent("pi-wb119-late")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-3", amount="200.00",
            currency="CNY", channel_status="PENDING", event_id="evt-wb119-l1")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-3", amount="200.00",
            currency="CNY", channel_status="FAILED", event_id="evt-wb119-l2")
        main, created = record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-3", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb119-l3",
            signature_verified=True)
        self.assertTrue(created)
        main.refresh_from_db()
        self.assertEqual(main.channel_status, "SUCCESS")
        self.assertEqual(
            ProviderTransactionEvent.objects.filter(transaction=main).count(), 3)
        # 渠道成功确认闭环：合法 Payment 可绑定
        payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="WB119-TXN-3", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        confirmed, changed = confirm_intent_success(
            property_obj=self.prop, intent_id=intent.pk,
            provider_transaction_id="WB119-TXN-3",
            confirmed_payment=payment, actor=self.owner)
        self.assertTrue(changed)
        self.assertEqual(confirmed.status, PaymentIntent.Status.SUCCEEDED)

    def test_unknown_then_success_unblocks_expiry(self):
        """UNKNOWN 后收到 SUCCESS：主记录不再是 UNKNOWN，意图可正常过期。"""
        intent = self._intent("pi-wb119-unk")
        # 过期守卫只对 PENDING/CREATED 意图生效，先降回待支付状态
        intent.status = PaymentIntent.Status.PENDING
        intent.save(update_fields=("status",))
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-4", amount="200.00",
            currency="CNY", channel_status="UNKNOWN", event_id="evt-wb119-u1")
        intent.expires_at = timezone.now() - timedelta(seconds=1)
        intent.save(update_fields=("expires_at",))
        with self.assertRaises(ValueError) as ctx:
            expire_intent(property_obj=self.prop, intent_id=intent.pk)
        self.assertIn("渠道状态未知", str(ctx.exception))
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
        # 渠道迟到成功：主记录推进为 SUCCESS，UNKNOWN 守卫不再拦截
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-4", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb119-u2",
            signature_verified=True)
        expired = expire_intent(property_obj=self.prop, intent_id=intent.pk)
        self.assertEqual(expired.status, PaymentIntent.Status.EXPIRED)

    def test_instance_save_and_queryset_update_are_blocked(self):
        """WB-125：实例级 save 不得绕过不可变保护；QuerySet.update 亦被禁。"""
        from django.core.exceptions import ValidationError
        intent = self._intent("pi-wb125")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB125-TXN-1", amount="200.00",
            currency="CNY", channel_status="SUCCESS", event_id="evt-wb125",
            signature_verified=True)
        fetched = ProviderTransaction.objects.get(
            provider_code="mock", provider_transaction_id="WB125-TXN-1")
        fetched.channel_status = "FAILED"
        fetched.signature_verified = False
        with self.assertRaises(ValueError):
            fetched.save()
        fetched.refresh_from_db()
        self.assertEqual(fetched.channel_status, "SUCCESS")
        self.assertTrue(fetched.signature_verified)
        with self.assertRaises(ValidationError):
            ProviderTransaction.objects.filter(
                provider_transaction_id="WB125-TXN-1").update(
                channel_status="FAILED")

    def test_event_history_instance_save_and_delete_are_blocked(self):
        """WB-119（第 31 轮）：事件历史只追加——实例 save 修改与 delete
        删除都必须被拒绝，QuerySet 批量旁路同样被拦截。"""
        from django.core.exceptions import ValidationError
        from apps.billing.models import ProviderTransactionEvent
        intent = self._intent("pi-wb119-evt-immutable")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB119-TXN-5", amount="200.00",
            currency="CNY", channel_status="PENDING", event_id="evt-wb119-i1")
        event = ProviderTransactionEvent.objects.get(
            provider_code="mock", event_id="evt-wb119-i1")
        # 实例级改写：渠道状态/金额/验签结果均不可修改
        event.channel_status = "FAILED"
        event.amount = Decimal("999.00")
        event.signature_verified = True
        with self.assertRaises(ValueError):
            event.save()
        event.refresh_from_db()
        self.assertEqual(event.channel_status, "PENDING")
        self.assertEqual(event.amount, Decimal("200.00"))
        self.assertFalse(event.signature_verified)
        # 实例级物理删除：拒绝且记录仍在
        with self.assertRaises(RuntimeError):
            event.delete()
        self.assertTrue(
            ProviderTransactionEvent.objects.filter(pk=event.pk).exists())
        # QuerySet 批量旁路：ImmutableFinancialFactQuerySet 拦截
        with self.assertRaises(ValidationError):
            ProviderTransactionEvent.objects.filter(pk=event.pk).update(
                channel_status="FAILED")
        with self.assertRaises(ValidationError):
            ProviderTransactionEvent.objects.filter(pk=event.pk).delete()

    def test_forged_transition_flag_cannot_bypass_immutable_guard(self):
        """WB-125（第 31 轮）：_allow_controlled_transition 实例属性放行
        已废除——伪造该属性不再能绕过不可变保护；状态/验签/确认时间仅
        允许经 authorized_provider_transition 受控上下文推进，且不可变
        字段（金额/交易号等）即使持有上下文也不得修改。"""
        from apps.billing.models import authorized_provider_transition
        intent = self._intent("pi-wb125-forged")
        record_provider_event(
            property_obj=self.prop, intent_id=intent.pk, provider_code="mock",
            provider_transaction_id="WB125-TXN-2", amount="200.00",
            currency="CNY", channel_status="PENDING", event_id="evt-wb125-f1")
        fetched = ProviderTransaction.objects.get(
            provider_code="mock", provider_transaction_id="WB125-TXN-2")
        self.assertEqual(fetched.channel_status, "PENDING")
        # 伪造旧版放行属性（审查复现路径）：必须被拒绝
        fetched._allow_controlled_transition = True
        fetched.channel_status = "FAILED"
        with self.assertRaises(ValueError) as ctx:
            fetched.save(update_fields=("channel_status",))
        self.assertIn("受控状态机", str(ctx.exception))
        fetched.refresh_from_db()
        self.assertEqual(fetched.channel_status, "PENDING")
        # 持有受控上下文也改不了不可变事实字段（金额）
        fetched2 = ProviderTransaction.objects.get(
            provider_code="mock", provider_transaction_id="WB125-TXN-2")
        fetched2.amount = Decimal("999.00")
        with authorized_provider_transition():
            with self.assertRaises(ValueError) as ctx2:
                fetched2.save(update_fields=("amount",))
        self.assertIn("不可变", str(ctx2.exception))
        fetched2.refresh_from_db()
        self.assertEqual(fetched2.amount, Decimal("200.00"))


import threading
import unittest
from threading import Barrier
from django.db import close_old_connections, connection
from django.test import TransactionTestCase


@unittest.skipUnless(connection.vendor == "mysql",
                     "真实 MySQL 并发专用（SQLite 无法复现唯一键竞争）")
@override_settings(DEBUG=True, DJANGO_ENV="test")
class CompensationTaskCreateConcurrencyTests(TransactionTestCase):
    """WB-126：两个调度进程并发创建同幂等键任务，必须幂等且不 500。"""

    def test_concurrent_same_key_create_is_idempotent(self):
        from apps.billing.payment_intents import create_compensation_task

        results = []
        barrier = Barrier(2)

        def worker(tag):
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                task, created = create_compensation_task(
                    task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
                    aggregate_type="PaymentIntent", aggregate_id=1,
                    idempotency_key="wb126-race-key")
                results.append((tag, task.pk, created))
            except Exception as exc:  # noqa: BLE001 —— 测试收集器
                results.append((tag, None, repr(exc)))
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(results), 2, results)
        pks = {r[1] for r in results}
        self.assertEqual(len(pks), 1,
                         f"并发创建必须收敛到同一任务且无异常：{results}")
        created_flags = sorted(r[2] for r in results)
        self.assertEqual(created_flags, [False, True], results)
        self.assertEqual(
            PaymentCompensationTask.objects.filter(
                idempotency_key="wb126-race-key").count(), 1)
