import json
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

# 测试专用 webhook 密钥：运行时随机生成，源码不落凭据字面量（Mimosa L3 门禁）。
from django.utils.crypto import get_random_string  # noqa: E402

from apps.billing.models import (
    FolioReconciliationRun,
    LedgerEntry,
    Payment,
    PaymentCompensationTask,
    PaymentIntent,
    ProviderTransaction,
)
from apps.billing.payment_intents import (
    create_compensation_task,
    replay_dead_letter_task,
)
from apps.billing.services import create_mock_payment_intent, sign_mock_callback
from apps.core.models import Property
from apps.inventory.models import DailyRate, RatePlan, RoomType, RoomTypeInventory
from apps.inventory.services import release_inventory_hold
from apps.reservations.models import Reservation
from apps.reservations.services import create_reservation

UNIT_TEST_WEBHOOK_SECRET = get_random_string(32)


@override_settings(
    DEBUG=True,
    DJANGO_ENV="test",
    PAYMENT_WEBHOOK_SECRET=UNIT_TEST_WEBHOOK_SECRET,
)
class MockPaymentFixtureMixin(TestCase):
    """Mock 支付链路公共夹具：库存日历 + 预订 + 本地支付单 + 签名回调助手。"""

    def setUp(self):
        # 日期动态取今天，避免硬编码日期随时间过期（入住校验要求不早于今天）
        self.today = date.today()
        self.stay_days = (self.today, self.today + timedelta(days=1))
        self.property = Property.objects.create(code="PAY", name="支付测试酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="TWIN", name="标准双床房"
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="BAR",
            name="门市价",
            valid_from=self.today,
        )
        for day in self.stay_days:
            DailyRate.objects.create(
                property=self.property,
                rate_plan=self.rate_plan,
                room_type=self.room_type,
                stay_date=day,
                price=Decimal("400.00"),
            )
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=self.room_type,
                stay_date=day,
                total_rooms=2,
            )
        payload = {
            "full_name": "支付测试住客",
            "phone": "13800000001",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": self.today,
            "check_out": self.today + timedelta(days=2),
            "rooms": 1,
            "adults": 2,
            "children": 0,
        }
        self.reservation, _, self.access_token = create_reservation(
            property_obj=self.property,
            payload=payload,
            idempotency_key="payment-test-reservation",
        )
        self.payment = create_mock_payment_intent(self.reservation)

    def _callback(self, payment=None):
        payment = payment or self.payment
        payload = {
            "provider_ref": payment.provider_ref,
            "amount": str(payment.amount),
            "status": "SUCCESS",
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        signature = sign_mock_callback(raw)
        return self.client.post(
            "/api/v1/payments/callback",
            data=raw,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
        )


class MockPaymentTests(MockPaymentFixtureMixin):
    """渠道回调契约回归：既有断言保持不变，另补 WB-087/088 用例。"""

    def test_inventory_confirmation_is_idempotent_after_reservation_confirmed(self):
        from apps.inventory.services import confirm_inventory_for_reservation

        self.assertTrue(confirm_inventory_for_reservation(self.reservation))
        Reservation.objects.filter(pk=self.reservation.pk).update(
            status=Reservation.Status.CONFIRMED
        )
        self.assertFalse(confirm_inventory_for_reservation(self.reservation))
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", "sold")),
            [(0, 1), (0, 1)],
        )

    def test_signed_success_callback_confirms_reservation_and_posts_once(self):
        response = self._callback()
        self.assertEqual(response.status_code, 200)
        self.reservation.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(self.payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(LedgerEntry.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.get().amount, Decimal("-800.00"))
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", "sold")),
            [(0, 1), (0, 1)],
        )

    def test_duplicate_callback_is_idempotent(self):
        first = self._callback()
        second = self._callback()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertTrue(second.json()["reservation_confirmed"])
        self.assertFalse(second.json()["compensation_required"])
        self.assertEqual(LedgerEntry.objects.count(), 1)

    def test_invalid_signature_is_rejected(self):
        raw = b'{"amount":"800.00","provider_ref":"invalid","status":"SUCCESS"}'
        response = self.client.post(
            "/api/v1/payments/callback",
            data=raw,
            content_type="application/json",
            HTTP_X_SIGNATURE="not-a-valid-signature",
        )
        self.assertEqual(response.status_code, 401)

    def test_late_success_at_expiry_boundary_preserves_payment_and_reuses_held_inventory(self):
        Reservation.objects.filter(pk=self.reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self._callback()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["expired"])
        self.assertTrue(response.json()["reservation_confirmed"])
        self.assertFalse(response.json()["compensation_required"])
        self.reservation.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(LedgerEntry.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.get().amount, Decimal("-800.00"))
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", "sold")),
            [(0, 1), (0, 1)],
        )

    def test_late_success_after_hold_release_preserves_payment_and_marks_compensation(self):
        from apps.inventory.services import release_inventory_hold

        Reservation.objects.filter(pk=self.reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        release_inventory_hold(self.reservation)
        response = self._callback()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["expired"])
        self.assertFalse(response.json()["reservation_confirmed"])
        self.assertTrue(response.json()["compensation_required"])
        self.reservation.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(self.reservation.status, Reservation.Status.EXPIRED)
        self.assertEqual(LedgerEntry.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.get().amount, Decimal("-800.00"))
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", "sold")),
            [(0, 0), (0, 0)],
        )

    def test_frontdesk_deposit_confirmation_cancels_older_pending_payment_intents(self):
        from apps.billing.services_v2 import BillingService

        deposit, created = BillingService.record_deposit(
            property_obj=self.property,
            reservation=self.reservation,
            amount=Decimal("200.00"),
            method="CARD",
            idempotency_key="deposit-close-old-intent",
            received_by=None,
        )
        self.assertTrue(created)
        self.assertEqual(deposit.status, Payment.Status.SUCCEEDED)
        self.payment.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CANCELLED)
        self.assertEqual(self.reservation.status, Reservation.Status.CONFIRMED)

    def test_late_success_after_local_cancellation_preserves_channel_payment_fact(self):
        from apps.reservations.services import cancel_pending_reservation

        cancel_pending_reservation(
            property_obj=self.property,
            booking_no=self.reservation.booking_no,
            reservation_pk=self.reservation.pk,
        )
        self.reservation.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CANCELLED)

        response = self._callback()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["compensation_required"])
        self.payment.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(self.reservation.status, Reservation.Status.CANCELLED)
        self.assertEqual(LedgerEntry.objects.count(), 1)
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", "sold")),
            [(0, 0), (0, 0)],
        )
        replay = self._callback()
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["duplicate"])
        self.assertTrue(replay.json()["compensation_required"])
        self.assertFalse(replay.json()["reservation_confirmed"])

    def _create_public_reservation(self, idempotency_key):
        """再下一单公开预订（消耗库存第 2 个单位），返回 (reservation, token)。"""
        reservation, _, token = create_reservation(
            property_obj=self.property,
            payload={
                "full_name": "公开支付测试住客",
                "phone": "13800000002",
                "room_type_id": self.room_type.pk,
                "rate_plan_id": self.rate_plan.pk,
                "check_in": self.today,
                "check_out": self.today + timedelta(days=2),
                "rooms": 1,
                "adults": 2,
                "children": 0,
            },
            idempotency_key=idempotency_key,
        )
        return reservation, token

    def test_late_success_creates_persistent_compensation_task_idempotently(self):
        """WB-087：迟到成功除审计外，还须同事务落持久化补偿任务，重放不重复。"""
        from apps.core.models import AuditEvent

        Reservation.objects.filter(pk=self.reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        release_inventory_hold(self.reservation)
        response = self._callback()
        self.assertEqual(response.status_code, 200)
        # 既有审计与响应契约保持不变
        self.assertTrue(response.json()["compensation_required"])
        self.assertTrue(
            AuditEvent.objects.filter(
                action="billing.payment_late_success_requires_reconciliation",
                entity_type="Payment",
                entity_id=str(self.payment.pk),
            ).exists()
        )
        # 同事务持久化补偿任务：HOLD_COORDINATION，聚合到本笔 Payment
        task = PaymentCompensationTask.objects.get(
            idempotency_key=f"late-success:payment:{self.payment.pk}"
        )
        self.assertEqual(task.task_type, PaymentCompensationTask.TaskType.HOLD_COORDINATION)
        self.assertEqual(task.aggregate_type, "Payment")
        self.assertEqual(task.aggregate_id, self.payment.pk)
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.last_error_code, "LATE_SUCCESS_REQUIRES_RECONCILIATION")
        self.assertIn("迟到成功需库存/预订协调", task.last_error_summary)
        # 渠道重放回调：不得重复建任务
        replay = self._callback()
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(
            PaymentCompensationTask.objects.filter(
                idempotency_key=f"late-success:payment:{self.payment.pk}"
            ).count(),
            1,
        )

    @override_settings(DEFAULT_PROPERTY_CODE="PAY")
    def test_public_intent_endpoint_creates_intent_and_channel_facts(self):
        """WB-088：公开支付入口同事务产生 PaymentIntent，回调推进状态机。"""
        reservation, token = self._create_public_reservation("wb088-endpoint-resv")
        response = self.client.post(
            "/api/v1/payments/intents",
            data=json.dumps({"booking_no": reservation.booking_no, "token": token}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        # 旧响应契约字段不变
        self.assertEqual(body["provider"], "mock")
        self.assertEqual(body["status"], Payment.Status.PENDING)
        self.assertEqual(body["amount"], str(reservation.total_amount))
        self.assertEqual(body["currency"], reservation.currency)
        payment = Payment.objects.get(provider_ref=body["provider_ref"])
        intent = PaymentIntent.objects.get(idempotency_key=body["provider_ref"])
        # 意图与 Payment 的物业/预订/账单/金额/币种完全一致（WB-072 前提）
        self.assertEqual(intent.reservation_id, reservation.pk)
        self.assertEqual(intent.folio_id, payment.folio_id)
        self.assertEqual(intent.amount, payment.amount)
        self.assertEqual(intent.currency, payment.currency)
        self.assertEqual(intent.provider_code, "mock")
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
        self.assertIsNone(intent.confirmed_payment_id)
        # 渠道回调：同一事务记录渠道事实并把意图推进到 SUCCEEDED
        callback = self._callback(payment)
        self.assertEqual(callback.status_code, 200)
        self.assertTrue(callback.json()["reservation_confirmed"])
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.SUCCEEDED)
        self.assertEqual(intent.confirmed_payment_id, payment.pk)
        provider_txn = ProviderTransaction.objects.get(
            provider_code="mock", provider_transaction_id=payment.provider_ref
        )
        self.assertEqual(provider_txn.channel_status, "SUCCESS")
        self.assertTrue(provider_txn.signature_verified)
        self.assertEqual(provider_txn.intent_id, intent.pk)
        self.assertEqual(provider_txn.event_id, f"mock-callback:{payment.provider_ref}")

    @override_settings(DEFAULT_PROPERTY_CODE="PAY")
    def test_public_intent_endpoint_replay_is_idempotent(self):
        """WB-088：重复 POST 返回同一笔支付单，不重复创建意图。"""
        payload = json.dumps(
            {"booking_no": self.reservation.booking_no, "token": self.access_token}
        )
        first = self.client.post(
            "/api/v1/payments/intents", data=payload, content_type="application/json"
        )
        second = self.client.post(
            "/api/v1/payments/intents", data=payload, content_type="application/json"
        )
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()["provider_ref"], first.json()["provider_ref"])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(PaymentIntent.objects.count(), 1)

    @override_settings(DEFAULT_PROPERTY_CODE="PAY")
    def test_public_intent_endpoint_honors_request_idempotency_key(self):
        """WB-088：带请求幂等键时意图幂等键取请求值，重放复用同一意图。"""
        reservation, token = self._create_public_reservation("wb088-header-resv")
        payload = json.dumps({"booking_no": reservation.booking_no, "token": token})
        first = self.client.post(
            "/api/v1/payments/intents",
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="wb088-key-1",
        )
        self.assertEqual(first.status_code, 201, first.content)
        intent = PaymentIntent.objects.get(idempotency_key="wb088-key-1")
        self.assertEqual(intent.reservation_id, reservation.pk)
        self.assertEqual(intent.provider_code, "mock")
        second = self.client.post(
            "/api/v1/payments/intents",
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="wb088-key-1",
        )
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()["provider_ref"], first.json()["provider_ref"])
        self.assertEqual(PaymentIntent.objects.filter(idempotency_key="wb088-key-1").count(), 1)
        # 请求幂等键创建的意图同样会在回调时被推进
        callback = self._callback(Payment.objects.get(provider_ref=first.json()["provider_ref"]))
        self.assertEqual(callback.status_code, 200)
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.SUCCEEDED)

    def test_mock_callback_is_not_available_in_production(self):
        with override_settings(DEBUG=False, DJANGO_ENV="prod"):
            raw = b"{}"
            response = self.client.post(
                "/api/v1/payments/callback",
                data=raw,
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 404)


class ApprovalRecordTests(TestCase):
    def setUp(self):
        from apps.billing.services_v2 import ApprovalService

        self.ApprovalService = ApprovalService
        self.property = Property.objects.create(code="APR", name="审批测试酒店")
        self.other_property = Property.objects.create(code="APR2", name="其他审批酒店")
        self.requester = User.objects.create_user(username="approval-requester")
        self.approver = User.objects.create_user(username="approval-approver")

    def test_approval_request_is_property_scoped_and_idempotent(self):
        first, created = self.ApprovalService.request_approval(
            property_obj=self.property,
            action_code="billing.refund",
            entity_type="Refund",
            entity_id=100,
            requested_by=self.requester,
            reason="客人重复扣款申请退款",
            payload={"amount": "100.00"},
        )
        replay, replay_created = self.ApprovalService.request_approval(
            property_obj=self.property,
            action_code="billing.refund",
            entity_type="Refund",
            entity_id=100,
            requested_by=self.requester,
            reason="客人重复扣款申请退款",
            payload={"amount": "100.00"},
        )
        other, other_created = self.ApprovalService.request_approval(
            property_obj=self.other_property,
            action_code="billing.refund",
            entity_type="Refund",
            entity_id=100,
            requested_by=self.requester,
            reason="另一物业同号退款申请",
            payload={"amount": "100.00"},
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertTrue(other_created)
        self.assertEqual(first.pk, replay.pk)
        self.assertNotEqual(first.pk, other.pk)

    def test_approval_cannot_be_self_approved_or_approved_cross_property(self):
        record, _ = self.ApprovalService.request_approval(
            property_obj=self.property,
            action_code="night_audit.reopen",
            entity_type="BusinessDate",
            entity_id=1,
            requested_by=self.requester,
            reason="核对营业日错账后申请重开",
        )
        with self.assertRaises(ValueError):
            self.ApprovalService.approve(
                record.pk, approved_by=self.requester, property_obj=self.property
            )
        with self.assertRaises(ValueError):
            self.ApprovalService.approve(
                record.pk, approved_by=self.approver, property_obj=self.other_property
            )
        approved = self.ApprovalService.approve(
            record.pk, approved_by=self.approver, property_obj=self.property
        )
        self.assertEqual(approved.status, "APPROVED")
        self.assertEqual(approved.approved_by_id, self.approver.pk)

    def test_approval_reason_is_required_and_expiry_is_persisted(self):
        with self.assertRaises(ValueError):
            self.ApprovalService.request_approval(
                property_obj=self.property,
                action_code="billing.refund",
                entity_type="Refund",
                entity_id=1,
                requested_by=self.requester,
                reason=" ",
            )
        record, _ = self.ApprovalService.request_approval(
            property_obj=self.property,
            action_code="billing.refund",
            entity_type="Refund",
            entity_id=2,
            requested_by=self.requester,
            reason="审批超时流程测试",
        )
        from datetime import timedelta

        from django.utils import timezone

        type(record)._base_manager.filter(pk=record.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        expired = self.ApprovalService.approve(
            record.pk, approved_by=self.approver, property_obj=self.property
        )
        self.assertEqual(expired.status, "EXPIRED")


class NightAuditServiceTests(TestCase):
    def setUp(self):
        self.today = date.today()
        self.property = Property.objects.create(code="NA", name="夜审测试酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="NA-STD", name="夜审标准间"
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="NA-BAR", name="夜审门市价", valid_from=self.today
        )
        self.guest = __import__("apps.guests.models", fromlist=["Guest"]).Guest.objects.create(
            property=self.property,
            full_name="夜审客人",
            phone="13800008881",
            phone_normalized="13800008881",
        )
        from decimal import Decimal

        from django.contrib.auth.models import User

        from apps.billing.night_audit import NightAuditService
        from apps.inventory.models import Room
        from apps.reservations.models import Reservation, ReservationRoom, Stay

        self.user = User.objects.create_user(username="night-audit-operator")
        self.reservation = Reservation.objects.create(
            property=self.property,
            booking_no="NIGHTAUDIT0001",
            guest=self.guest,
            status=Reservation.Status.CHECKED_IN,
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            room_count=1,
            adult_count=1,
            total_amount=Decimal("800.00"),
        )
        line = ReservationRoom.objects.create(
            property=self.property,
            reservation=self.reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            nightly_rates=[
                {"date": self.today.isoformat(), "price": "400.00", "currency": "CNY"},
                {
                    "date": (self.today + timedelta(days=1)).isoformat(),
                    "price": "400.00",
                    "currency": "CNY",
                },
            ],
            amount=Decimal("800.00"),
        )
        room = Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="NA101",
            occupancy_status=Room.OccupancyStatus.OCCUPIED,
        )
        self.stay = Stay.objects.create(
            property=self.property, reservation_room=line, room=room, status=Stay.Status.IN_HOUSE
        )
        from apps.billing.models import Folio

        Folio.objects.create(
            property=self.property, reservation=self.reservation, guest=self.guest, currency="CNY"
        )
        self.NightAuditService = NightAuditService

    def test_night_audit_posts_daily_room_charge_once_and_closes_business_date(self):
        from apps.billing.models import LedgerEntry
        from apps.core.models import BusinessDate

        run, created = self.NightAuditService.run(
            property_obj=self.property, executed_by=self.user, business_date=self.today
        )
        self.assertTrue(created)
        self.assertEqual(run.status, "SUCCESS")
        self.assertEqual(run.summary["charge_count"], 1)
        self.assertEqual(
            LedgerEntry.objects.filter(
                property=self.property,
                business_date=self.today,
                entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            ).count(),
            1,
        )
        self.assertEqual(
            BusinessDate.objects.get(property=self.property, business_date=self.today).status,
            BusinessDate.Status.CLOSED,
        )
        replay, replay_created = self.NightAuditService.run(
            property_obj=self.property, executed_by=self.user, business_date=self.today
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, run.pk)
        self.assertEqual(
            LedgerEntry.objects.filter(property=self.property, business_date=self.today).count(), 1
        )

    def test_night_audit_requires_second_manager_to_reopen(self):
        from django.contrib.auth.models import User

        from apps.billing.models import LedgerEntry
        from apps.core.models import BusinessDate

        run, _ = self.NightAuditService.run(
            property_obj=self.property, executed_by=self.user, business_date=self.today
        )
        bd = BusinessDate.objects.get(property=self.property, business_date=self.today)
        requester = User.objects.create_user(username="night-audit-requester")
        approver = User.objects.create_user(username="night-audit-approver")
        approval, created = self.NightAuditService.request_reopen(
            property_obj=self.property,
            business_date_id=bd.pk,
            requested_by=requester,
            reason="营业日账务复核后需要重开",
        )
        self.assertTrue(created)
        with self.assertRaises(Exception):
            self.NightAuditService.approve_reopen(
                property_obj=self.property,
                business_date_id=bd.pk,
                approval_id=approval.pk,
                approved_by=requester,
            )
        reopened = self.NightAuditService.approve_reopen(
            property_obj=self.property,
            business_date_id=bd.pk,
            approval_id=approval.pk,
            approved_by=approver,
        )
        self.assertEqual(reopened.status, BusinessDate.Status.REOPENED)
        rerun, rerun_created = self.NightAuditService.run(
            property_obj=self.property, executed_by=approver, business_date=self.today
        )
        self.assertTrue(rerun_created)
        self.assertEqual(rerun.status, "SUCCESS")
        self.assertEqual(
            LedgerEntry.objects.filter(
                property=self.property,
                business_date=self.today,
                entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            ).count(),
            1,
        )


class BillingServiceManualCollectionTests(TestCase):
    def setUp(self):
        today = date.today()
        self.property = Property.objects.create(code="BILL", name="统一账务测试酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="BILL-STD", name="统一账务标准间"
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="BILL-BAR", name="统一账务门市价", valid_from=today
        )
        from apps.billing.models import Folio
        from apps.guests.models import Guest
        from apps.reservations.models import Reservation, ReservationRoom

        self.guest = Guest.objects.create(
            property=self.property,
            full_name="统一账务客人",
            phone="13800008882",
            phone_normalized="13800008882",
        )
        self.reservation = Reservation.objects.create(
            property=self.property,
            booking_no="BILLINGCOLLECT0001",
            guest=self.guest,
            status=Reservation.Status.CONFIRMED,
            check_in=today,
            check_out=today + timedelta(days=1),
            room_count=1,
            adult_count=1,
            total_amount=Decimal("300.00"),
        )
        ReservationRoom.objects.create(
            property=self.property,
            reservation=self.reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=today,
            check_out=today + timedelta(days=1),
            nightly_rates=[{"date": today.isoformat(), "price": "300.00", "currency": "CNY"}],
            amount=Decimal("300.00"),
        )
        self.folio = Folio.objects.create(
            property=self.property, reservation=self.reservation, guest=self.guest, currency="CNY"
        )
        self.operator = User.objects.create_user(username="bill-operator")
        from apps.billing.services_v2 import BillingService

        BillingService.open_shift(
            property_obj=self.property, cashier=self.operator, opening_cash=Decimal("0.00")
        )

    def test_manual_collection_posts_successful_payment_and_is_idempotent(self):
        from apps.billing.services_v2 import BillingService

        first = BillingService.collect_payment(
            property_obj=self.property,
            reservation=self.reservation,
            amount="300.00",
            method="CASH",
            collected_by=self.operator,
            reference="POS-CASH-REF-001",
        )
        replay = BillingService.collect_payment(
            property_obj=self.property,
            reservation=self.reservation,
            amount="300.00",
            method="CASH",
            collected_by=self.operator,
            reference="POS-CASH-REF-001",
        )
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(first.status, Payment.Status.SUCCEEDED)
        self.assertEqual(
            LedgerEntry.objects.filter(
                property=self.property, folio=self.folio, entry_type=LedgerEntry.EntryType.PAYMENT
            ).count(),
            1,
        )
        self.assertEqual(
            LedgerEntry.objects.get(property=self.property, folio=self.folio).amount,
            Decimal("-300.00"),
        )


class PaymentCompensationWorkerTests(MockPaymentFixtureMixin):
    """WB-070/078：补偿 Worker 的租约消费、退避与死信重放路径。"""

    def _run_worker(self):
        out = StringIO()
        call_command("payment_compensation_worker", "--once", stdout=out)
        return out.getvalue()

    def test_worker_consumes_due_hold_task_and_completes_it(self):
        """空转安静；到期 HOLD_COORDINATION 任务被租约消费并成功收尾。"""
        # 空转：无到期任务时保持安静
        self.assertEqual(self._run_worker(), "")
        task, created = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.HOLD_COORDINATION,
            aggregate_type="Payment",
            aggregate_id=self.payment.pk,
            idempotency_key="worker-hold-coordination-ok",
        )
        self.assertTrue(created)
        output = self._run_worker()
        self.assertIn("扫描 1", output)
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.attempt_count, 1)
        self.assertTrue(task.lease_owner.startswith("worker:"))
        # 已成功任务不再被扫描（继续空转安静）
        self.assertEqual(self._run_worker(), "")

    def test_worker_backs_off_when_inventory_hold_cannot_confirm(self):
        """迟到成功补偿任务在 Hold 失效时执行失败：退避计数增加、不再立即重试。

        WB-110a：预订保持 PENDING（未终态）时，Hold 失效仍按现有语义
        退避重试（INVENTORY_CONFIRM_UNAVAILABLE）。
        """
        # 预订保持 PENDING 且未过期，仅释放 Hold → 迟到成功后进入补偿
        Reservation.objects.filter(pk=self.reservation.pk).update(
            expires_at=timezone.now() + timedelta(minutes=10)
        )
        release_inventory_hold(self.reservation)
        response = self._callback()
        self.assertTrue(response.json()["compensation_required"])
        task = PaymentCompensationTask.objects.get(
            idempotency_key=f"late-success:payment:{self.payment.pk}"
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.attempt_count, 1)
        self.assertEqual(task.last_error_code, "INVENTORY_CONFIRM_UNAVAILABLE")
        self.assertIsNotNone(task.next_attempt_at)
        self.assertGreater(task.next_attempt_at, timezone.now())
        # 退避期内不得被再次抢占执行
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.attempt_count, 1)

    def test_worker_dead_letters_terminal_reservation_hold_task(self):
        """WB-110a：终态预订的 Hold 协调任务不得无限重试——首轮即转死信。"""
        # 渠道迟到成功但订单已取消（终态）→ 补偿任务创建
        Reservation.objects.filter(pk=self.reservation.pk).update(
            status=Reservation.Status.CANCELLED
        )
        response = self._callback()
        self.assertTrue(response.json()["compensation_required"])
        task = PaymentCompensationTask.objects.get(
            idempotency_key=f"late-success:payment:{self.payment.pk}"
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.DEAD_LETTER)
        self.assertEqual(task.last_error_code, "REQUIRES_MANUAL_COORDINATION")
        self.assertIn("终态", task.last_error_summary)
        self.assertIn(str(self.reservation.pk), task.last_error_summary)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.attempt_count, 1)
        # 意图侧 Hold 协调任务（WB-073 路径）同样不得无限重试
        intent = PaymentIntent.objects.filter(reservation=self.reservation).order_by("pk").first()
        intent_task = PaymentCompensationTask.objects.get(
            idempotency_key=f"intent-inventory-confirm:{intent.pk}"
        )
        self.assertEqual(intent_task.status, PaymentCompensationTask.Status.DEAD_LETTER)
        self.assertEqual(intent_task.last_error_code, "REQUIRES_MANUAL_COORDINATION")
        # 支付资金事实保留：不得篡改成功支付
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCEEDED)
        # 死信任务不再被扫描
        self.assertEqual(self._run_worker(), "")

    def test_worker_hold_coordination_confirmed_reservation_is_idempotent(self):
        """WB-110a：已确认预订的 Hold 协调任务幂等成功，不重复消耗库存。"""
        Reservation.objects.filter(pk=self.reservation.pk).update(
            status=Reservation.Status.CONFIRMED
        )
        held_before = list(
            RoomTypeInventory.objects.order_by("stay_date").values_list("held", "sold")
        )
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.HOLD_COORDINATION,
            aggregate_type="Payment",
            aggregate_id=self.payment.pk,
            idempotency_key="worker-hold-confirmed-idempotent",
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", "sold")),
            held_before,
        )

    def test_worker_channel_query_reports_persisted_success(self):
        """WB-110b：mock 渠道查询读到 SUCCESS 渠道事实 → 任务成功且不改状态。"""
        intent = PaymentIntent.objects.filter(reservation=self.reservation).order_by("pk").first()
        ProviderTransaction.objects.create(
            property=self.property,
            intent=intent,
            provider_code="mock",
            provider_transaction_id="CQ-TXN-001",
            amount=intent.amount,
            currency="CNY",
            channel_status="SUCCESS",
            signature_verified=True,
        )
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            aggregate_type="PaymentIntent",
            aggregate_id=intent.pk,
            idempotency_key="worker-channel-query-success",
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertIn("mock 渠道查询", task.last_error_summary)
        # 只汇报渠道事实，不修改意图/支付状态
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
        self.assertIsNone(intent.confirmed_payment)

    def test_worker_channel_query_fails_without_success_fact(self):
        """WB-110b：无渠道交易或非 SUCCESS → 失败退避，不得伪造渠道成功。"""
        intent = PaymentIntent.objects.filter(reservation=self.reservation).order_by("pk").first()
        # 最新渠道事实为 PENDING（非 SUCCESS）
        ProviderTransaction.objects.create(
            property=self.property,
            intent=intent,
            provider_code="mock",
            provider_transaction_id="CQ-TXN-002",
            amount=intent.amount,
            currency="CNY",
            channel_status="PENDING",
        )
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            aggregate_type="PaymentIntent",
            aggregate_id=intent.pk,
            idempotency_key="worker-channel-query-fail",
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.last_error_code, "CHANNEL_QUERY_NO_SUCCESS_FACT")
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
        # 无任何渠道交易 → 同样失败退避
        task2, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            aggregate_type="Payment",
            aggregate_id=self.payment.pk,
            idempotency_key="worker-channel-query-no-txn",
        )
        task.next_attempt_at = timezone.now() - timedelta(seconds=1)
        task.save(update_fields=("next_attempt_at",))
        self._run_worker()
        task2.refresh_from_db()
        self.assertEqual(task2.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task2.last_error_code, "CHANNEL_QUERY_NO_SUCCESS_FACT")

    def test_worker_callback_confirm_retry_advances_intent(self):
        """WB-110c：存在已验签 SUCCESS 渠道交易 + 成功 Payment → 推进意图。"""
        self.payment.status = Payment.Status.SUCCEEDED
        self.payment.paid_at = timezone.now()
        self.payment.save(update_fields=("status", "paid_at"))
        intent = PaymentIntent.objects.filter(reservation=self.reservation).order_by("pk").first()
        intent.status = PaymentIntent.Status.PROCESSING
        intent.save(update_fields=("status",))
        ProviderTransaction.objects.create(
            property=self.property,
            intent=intent,
            provider_code="mock",
            provider_transaction_id=self.payment.provider_ref,
            amount=self.payment.amount,
            currency=self.payment.currency,
            channel_status="SUCCESS",
            signature_verified=True,
        )
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CALLBACK_CONFIRM_RETRY,
            aggregate_type="PaymentIntent",
            aggregate_id=intent.pk,
            idempotency_key="worker-callback-confirm-ok",
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.SUCCEEDED)
        self.assertEqual(intent.confirmed_payment_id, self.payment.pk)
        # 重复执行幂等：重建任务再跑，确认返回原状态、不产生新事实
        task2, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CALLBACK_CONFIRM_RETRY,
            aggregate_type="PaymentIntent",
            aggregate_id=intent.pk,
            idempotency_key="worker-callback-confirm-replay",
        )
        self._run_worker()
        task2.refresh_from_db()
        self.assertEqual(task2.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertEqual(PaymentIntent.objects.filter(pk=intent.pk).count(), 1)

    def test_worker_callback_confirm_retry_fails_without_payment_fact(self):
        """WB-110c：找不到对应成功 Payment → 失败重试，不得凭空造成功。"""
        intent = PaymentIntent.objects.filter(reservation=self.reservation).order_by("pk").first()
        ProviderTransaction.objects.create(
            property=self.property,
            intent=intent,
            provider_code="mock",
            provider_transaction_id="CQ-NO-PAYMENT-TXN",
            amount=intent.amount,
            currency="CNY",
            channel_status="SUCCESS",
            signature_verified=True,
        )
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CALLBACK_CONFIRM_RETRY,
            aggregate_type="PaymentIntent",
            aggregate_id=intent.pk,
            idempotency_key="worker-callback-confirm-missing",
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.last_error_code, "CALLBACK_CONFIRM_NO_PAYMENT_FACT")
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)

    def test_worker_ledger_repair_posts_missing_voucher(self):
        """WB-110d：成功支付缺凭证 → 按 WB-111 新口径补凭证；重复执行幂等。"""
        self._callback()  # 渠道成功回调入账：payment:{ref} 凭证已存在
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.PAYMENT_LEDGER_REPAIR,
            aggregate_type="Payment",
            aggregate_id=self.payment.pk,
            idempotency_key="worker-ledger-repair-exists",
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertEqual(
            LedgerEntry.objects.filter(reference=f"payment:{self.payment.provider_ref}").count(), 1
        )

        # 缺凭证场景：同账单上另一笔已成功但从未入账的支付
        payment2 = Payment.objects.create(
            property=self.property,
            reservation=self.reservation,
            folio=self.reservation.get_master_folio(),
            provider="mock",
            provider_ref="REPAIR-REF-001",
            method="MOCK",
            amount=Decimal("100.00"),
            currency="CNY",
            status=Payment.Status.SUCCEEDED,
            paid_at=timezone.now(),
        )
        task2, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.PAYMENT_LEDGER_REPAIR,
            aggregate_type="Payment",
            aggregate_id=payment2.pk,
            idempotency_key="worker-ledger-repair-missing",
        )
        self._run_worker()
        task2.refresh_from_db()
        self.assertEqual(task2.status, PaymentCompensationTask.Status.SUCCEEDED)
        entry = LedgerEntry.objects.get(reference="payment:REPAIR-REF-001")
        self.assertEqual(entry.amount, Decimal("-100.00"))
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.PAYMENT)
        self.assertIsNone(entry.posted_by)
        # 幂等重放：同一支付再来一张修复任务 → 幂等成功，不重复入账
        task3, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.PAYMENT_LEDGER_REPAIR,
            aggregate_type="Payment",
            aggregate_id=payment2.pk,
            idempotency_key="worker-ledger-repair-replay",
        )
        self._run_worker()
        task3.refresh_from_db()
        self.assertEqual(task3.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertEqual(LedgerEntry.objects.filter(reference="payment:REPAIR-REF-001").count(), 1)

    def test_worker_ledger_repair_dead_letters_unsafe_payment(self):
        """WB-110d：Payment 非 SUCCEEDED → 直接死信，注明需人工/对账。"""
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.PAYMENT_LEDGER_REPAIR,
            aggregate_type="Payment",
            aggregate_id=self.payment.pk,  # 未回调：PENDING
            idempotency_key="worker-ledger-repair-pending",
        )
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.DEAD_LETTER)
        self.assertEqual(task.last_error_code, "REQUIRES_MANUAL_COORDINATION")
        self.assertIn("人工", task.last_error_summary)
        self.assertFalse(
            LedgerEntry.objects.filter(reference=f"payment:{self.payment.provider_ref}").exists()
        )

    def test_worker_reconciliation_diff_runs_read_only_scan(self):
        """WB-110e：对账差异任务调用既有扫描 → 成功且不自动改账。"""
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.RECONCILIATION_DIFF,
            aggregate_type="Property",
            aggregate_id=self.property.pk,
            idempotency_key="worker-reconciliation-diff",
        )
        entries_before = LedgerEntry.objects.count()
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertIn("不自动改账", task.last_error_summary)
        # 扫描生成对账运行批次；账本不被改动（只读核对）
        self.assertTrue(FolioReconciliationRun.objects.filter(property=self.property).exists())
        self.assertEqual(LedgerEntry.objects.count(), entries_before)

    def test_worker_reconciliation_diff_failure_retries(self):
        """WB-110e：扫描异常 → 失败退避重试。"""
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.RECONCILIATION_DIFF,
            aggregate_type="Property",
            aggregate_id=self.property.pk,
            idempotency_key="worker-reconciliation-diff-fail",
        )
        with mock.patch(
            "apps.billing.reconciliation.scan_folio_reconciliation",
            side_effect=RuntimeError("db unavailable"),
        ):
            self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.last_error_code, "WORKER_EXCEPTION")

    def test_dead_letter_task_can_be_replayed_manually(self):
        """超过尝试上限转死信；replay_dead_letter_task 重放后可再次消费。"""
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            aggregate_type="Payment",
            aggregate_id=self.payment.pk,
            idempotency_key="worker-dead-letter-replay",
            max_attempts=2,
        )
        # 第 1 次执行：mock 渠道查询无 SUCCESS 事实 → 失败退避
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.attempt_count, 1)
        self.assertEqual(task.last_error_code, "CHANNEL_QUERY_NO_SUCCESS_FACT")
        # 人为把退避时间拨回，模拟到期后的第 2 次执行 → 达到上限转死信
        task.next_attempt_at = timezone.now() - timedelta(seconds=1)
        task.save(update_fields=("next_attempt_at",))
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.DEAD_LETTER)
        self.assertIsNotNone(task.completed_at)
        # 人工重放：回到 PENDING、清零尝试，可再次被 worker 租约消费
        replayed = replay_dead_letter_task(task.pk, operator=None, reason="人工核查渠道后重放")
        self.assertEqual(replayed.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(replayed.attempt_count, 0)
        self._run_worker()
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.PENDING)
        self.assertEqual(task.attempt_count, 1)
        self.assertEqual(task.last_error_code, "CHANNEL_QUERY_NO_SUCCESS_FACT")
