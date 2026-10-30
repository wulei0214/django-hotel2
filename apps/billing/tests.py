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
        self.property = Property.objects.create(code="PAY", name="支付测试酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="TWIN", name="标准双床房"
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="BAR",
            name="门市价",
            valid_from=date(2026, 11, 1),
        )
        for day in (date(2026, 11, 1), date(2026, 11, 2)):
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
            "check_in": date(2026, 11, 1),
            "check_out": date(2026, 11, 3),
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
