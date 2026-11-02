import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Property
from django.contrib.auth.models import Group, Permission, User
from apps.billing.models import Folio, LedgerEntry
from apps.guests.models import Guest
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.inventory.models import Room
from apps.reservations.models import Reservation, ReservationRoom, Stay
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


    def test_public_reserve_page_renders_template(self):
        response = self.client.get("/reserve/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "public/booking.html")
        self.assertContains(response, "查询可订房型")
        self.assertContains(response, "/api/v1/availability")


class FrontDeskReadApiTests(TestCase):
    """前台工作台只读 API：物业范围、RBAC、脱敏与账单摘要。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.user = User.objects.create_user(
            username="frontdesk-api-user",
            password="TestPass-2026!",
        )
        self.profile = StaffProfile.objects.create(
            user=self.user,
            full_name="前台测试员",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        self.membership = PropertyStaffMembership.objects.create(
            staff_profile=self.profile,
            property=self.property,
            employee_no="FD-API-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        self.role = Group.objects.get(name="前台")
        self.permission = Permission.objects.get(
            content_type__app_label="hotelapp",
            codename="view_accommodation",
        )
        self.role.permissions.add(self.permission)
        self.assignment = StaffRoleAssignment.objects.create(
            membership=self.membership,
            group=self.role,
            starts_at=timezone.now() - timedelta(minutes=1),
        )

        self.check_in = timezone.localdate() + timedelta(days=1)
        self.check_out = self.check_in + timedelta(days=2)
        self.room_type = RoomType.objects.create(
            property=self.property,
            code="FD-API-KING",
            name="前台 API 大床房",
            capacity=2,
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="FD-API-BAR",
            name="前台 API 门市价",
            valid_from=self.check_in - timedelta(days=1),
        )
        self.guest = Guest.objects.create(
            property=self.property,
            full_name="前台测试住客",
            phone="13812345678",
            phone_normalized="13812345678",
        )
        self.reservation = Reservation.objects.create(
            property=self.property,
            booking_no="RFDTEST0001",
            guest=self.guest,
            source="WEBSITE",
            status=Reservation.Status.CONFIRMED,
            check_in=self.check_in,
            check_out=self.check_out,
            room_count=1,
            adult_count=2,
            child_count=0,
            total_amount=Decimal("600.00"),
            currency="CNY",
        )
        self.room_line = ReservationRoom.objects.create(
            property=self.property,
            reservation=self.reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=self.check_in,
            check_out=self.check_out,
            nightly_rates=[
                {"date": self.check_in.isoformat(), "price": "300.00", "currency": "CNY"},
                {"date": (self.check_in + timedelta(days=1)).isoformat(), "price": "300.00", "currency": "CNY"},
            ],
            amount=Decimal("600.00"),
        )
        self.folio = Folio.objects.create(
            property=self.property,
            reservation=self.reservation,
            guest=self.guest,
            currency="CNY",
        )
        LedgerEntry.objects.create(
            property=self.property,
            folio=self.folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=Decimal("600.00"),
            currency="CNY",
            business_date=timezone.localdate(),
            reference="FD-TEST-CHARGE-001",
            description="测试房费",
        )
        self.room = Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="FD101",
            floor="1",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )

    def test_front_desk_api_requires_employee_authentication(self):
        response = self.client.get("/api/v1/front-desk/overview")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_front_desk_api_requires_view_permission(self):
        self.role.permissions.remove(self.permission)
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/front-desk/overview")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_front_desk_arrivals_are_property_scoped_and_phone_masked(self):
        self.client.force_login(self.user)
        response = self.client.get(
            "/api/v1/front-desk/arrivals",
            {"date": self.check_in.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["booking_no"], self.reservation.booking_no)
        self.assertEqual(data[0]["guest"]["phone_masked"], "138****5678")
        self.assertNotIn("phone", data[0]["guest"])
        self.assertNotIn("identity_number", data[0]["guest"])

    def test_front_desk_overview_and_room_readiness(self):
        self.client.force_login(self.user)
        overview = self.client.get(
            "/api/v1/front-desk/overview", {"date": self.check_in.isoformat()}
        )
        rooms = self.client.get("/api/v1/front-desk/rooms?readiness=READY")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(rooms.status_code, 200)
        self.assertEqual(overview.json()["data"]["unassigned_count"], 1)
        self.assertTrue(any(item["number"] == "FD101" for item in rooms.json()["data"]))

    def test_front_desk_folio_detail_uses_signed_ledger_balance(self):
        self.client.force_login(self.user)
        response = self.client.get(
            f"/api/v1/front-desk/folios/{self.reservation.booking_no}"
        )
        self.assertEqual(response.status_code, 200)
        summary = response.json()["data"]["summary"]
        self.assertEqual(summary["charges"], "600.00")
        self.assertEqual(summary["balance"], "600.00")


    def test_front_desk_overview_rejects_invalid_business_date(self):
        self.client.force_login(self.user)
        response = self.client.get(
            "/api/v1/front-desk/overview", {"date": "not-a-date"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
