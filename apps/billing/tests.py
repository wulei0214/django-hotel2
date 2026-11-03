import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.billing.models import LedgerEntry, Payment
from apps.billing.services import create_mock_payment_intent, sign_mock_callback
from apps.core.models import Property
from apps.inventory.models import DailyRate, RatePlan, RoomType, RoomTypeInventory
from apps.reservations.models import Reservation
from apps.reservations.services import create_reservation


@override_settings(
    DEBUG=True,
    DJANGO_ENV="test",
    PAYMENT_WEBHOOK_SECRET="unit-test-webhook-secret",
)
class MockPaymentTests(TestCase):
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
        self.reservation, _ = create_reservation(
            property_obj=self.property,
            payload=payload,
            idempotency_key="payment-test-reservation",
        )
        self.payment = create_mock_payment_intent(self.reservation)

    def _callback(self):
        payload = {
            "provider_ref": self.payment.provider_ref,
            "amount": str(self.payment.amount),
            "status": "SUCCESS",
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        signature = sign_mock_callback(raw)
        return self.client.post(
            "/api/v1/payments/callback",
            data=raw,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
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

    def test_expired_reservation_releases_held_stock(self):
        Reservation.objects.filter(pk=self.reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self._callback()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["expired"])
        self.assertEqual(LedgerEntry.objects.count(), 0)
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", "sold")),
            [(0, 0), (0, 0)],
        )

    def test_mock_callback_is_not_available_in_production(self):
        with override_settings(DEBUG=False, DJANGO_ENV="prod"):
            raw = b'{}'
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
            property_obj=self.property, action_code="billing.refund",
            entity_type="Refund", entity_id=100,
            requested_by=self.requester, reason="客人重复扣款申请退款",
            payload={"amount": "100.00"})
        replay, replay_created = self.ApprovalService.request_approval(
            property_obj=self.property, action_code="billing.refund",
            entity_type="Refund", entity_id=100,
            requested_by=self.requester, reason="客人重复扣款申请退款",
            payload={"amount": "100.00"})
        other, other_created = self.ApprovalService.request_approval(
            property_obj=self.other_property, action_code="billing.refund",
            entity_type="Refund", entity_id=100,
            requested_by=self.requester, reason="另一物业同号退款申请",
            payload={"amount": "100.00"})
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertTrue(other_created)
        self.assertEqual(first.pk, replay.pk)
        self.assertNotEqual(first.pk, other.pk)

    def test_approval_cannot_be_self_approved_or_approved_cross_property(self):
        record, _ = self.ApprovalService.request_approval(
            property_obj=self.property, action_code="night_audit.reopen",
            entity_type="BusinessDate", entity_id=1,
            requested_by=self.requester, reason="核对营业日错账后申请重开")
        with self.assertRaises(ValueError):
            self.ApprovalService.approve(
                record.pk, approved_by=self.requester, property_obj=self.property)
        with self.assertRaises(ValueError):
            self.ApprovalService.approve(
                record.pk, approved_by=self.approver, property_obj=self.other_property)
        approved = self.ApprovalService.approve(
            record.pk, approved_by=self.approver, property_obj=self.property)
        self.assertEqual(approved.status, "APPROVED")
        self.assertEqual(approved.approved_by_id, self.approver.pk)

    def test_approval_reason_is_required_and_expiry_is_persisted(self):
        with self.assertRaises(ValueError):
            self.ApprovalService.request_approval(
                property_obj=self.property, action_code="billing.refund",
                entity_type="Refund", entity_id=1,
                requested_by=self.requester, reason=" ")
        record, _ = self.ApprovalService.request_approval(
            property_obj=self.property, action_code="billing.refund",
            entity_type="Refund", entity_id=2,
            requested_by=self.requester, reason="审批超时流程测试")
        from datetime import timedelta
        from django.utils import timezone
        type(record).objects.filter(pk=record.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1))
        expired = self.ApprovalService.approve(
            record.pk, approved_by=self.approver, property_obj=self.property)
        self.assertEqual(expired.status, "EXPIRED")
