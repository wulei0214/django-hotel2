import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Property
from apps.inventory.models import DailyRate, RatePlan, RoomType, RoomTypeInventory


@override_settings(DEBUG=True, DJANGO_ENV="test", PAYMENT_WEBHOOK_SECRET="api-test-webhook-secret")
class PublicBookingApiTests(TestCase):
    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.check_in = timezone.localdate() + timedelta(days=3)
        self.check_out = self.check_in + timedelta(days=2)
        self.room_type = RoomType.objects.create(
            property=self.property,
            code="API-KING",
            name="API 测试大床房",
            capacity=2,
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="API-BAR",
            name="API 测试门市价",
            valid_from=self.check_in - timedelta(days=1),
        )
        for day in (self.check_in, self.check_in + timedelta(days=1)):
            DailyRate.objects.create(
                property=self.property,
                rate_plan=self.rate_plan,
                room_type=self.room_type,
                stay_date=day,
                price=Decimal("520.00"),
            )
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=self.room_type,
                stay_date=day,
                total_rooms=2,
            )

    def _reservation_payload(self):
        return {
            "full_name": "API 测试客人",
            "phone": "13800000002",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "rooms": 1,
            "adults": 2,
            "children": 0,
        }

    def test_availability_api_returns_each_night_and_total(self):
        response = self.client.get(
            "/api/v1/availability",
            {
                "check_in": self.check_in.isoformat(),
                "check_out": self.check_out.isoformat(),
                "rooms": 1,
                "room_type_id": self.room_type.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["total_amount"], "1040.00")
        self.assertEqual(len(response.json()["data"][0]["nightly_details"]), 2)

    def test_booking_api_creates_and_replays_idempotently(self):
        payload = self._reservation_payload()
        first = self.client.post(
            "/api/v1/reservations",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-test-key-001",
        )
        second = self.client.post(
            "/api/v1/reservations",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-test-key-001",
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.json()["created"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(first.json()["booking_no"], second.json()["booking_no"])
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", flat=True)),
            [1, 1],
        )

    def test_public_reservation_lookup_requires_matching_phone(self):
        payload = self._reservation_payload()
        created = self.client.post(
            "/api/v1/reservations",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-test-key-002",
        )
        booking_no = created.json()["booking_no"]
        wrong_phone = self.client.get(
            f"/api/v1/reservations/{booking_no}", {"phone": "13800000099"}
        )
        correct_phone = self.client.get(
            f"/api/v1/reservations/{booking_no}", {"phone": "13800000002"}
        )
        self.assertEqual(wrong_phone.status_code, 404)
        self.assertEqual(correct_phone.status_code, 200)

    def test_pending_reservation_can_be_cancelled_and_releases_stock(self):
        payload = self._reservation_payload()
        created = self.client.post(
            "/api/v1/reservations",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-test-key-003",
        )
        booking_no = created.json()["booking_no"]
        cancelled = self.client.post(
            f"/api/v1/reservations/{booking_no}/cancel",
            data=json.dumps({"phone": "13800000002"}),
            content_type="application/json",
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "CANCELLED")
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", flat=True)),
            [0, 0],
        )

    def test_mock_payment_intent_exposes_only_dev_callback_example(self):
        payload = self._reservation_payload()
        created = self.client.post(
            "/api/v1/reservations",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-test-key-004",
        )
        booking_no = created.json()["booking_no"]
        response = self.client.post(
            "/api/v1/payments/intents",
            data=json.dumps({"booking_no": booking_no, "phone": "13800000002"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["provider"], "mock")
        self.assertIn("mock_callback_example", response.json())
