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
from apps.reservations.models import Reservation, ReservationRoom
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

    def test_public_reservation_lookup_requires_access_token(self):
        """P0-08/09：查询凭 Booking Access Token；错误 token 拒绝；响应白名单脱敏"""
        payload = self._reservation_payload()
        created = self.client.post(
            "/api/v1/reservations",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-test-key-002",
        )
        booking_no = created.json()["booking_no"]
        access_token = created.json()["access_token"]
        self.assertTrue(access_token, "创建成功必须一次性返回访问令牌")
        wrong_token = self.client.get(
            f"/api/v1/reservations/{booking_no}",
            HTTP_X_BOOKING_TOKEN="wrong-token-value")
        self.assertEqual(wrong_token.status_code, 404)
        correct = self.client.get(
            f"/api/v1/reservations/{booking_no}",
            HTTP_X_BOOKING_TOKEN=access_token)
        self.assertEqual(correct.status_code, 200)
        body = correct.json()
        self.assertIn("*", body["guest_name"], "姓名必须脱敏")
        self.assertNotIn("provider_ref", body, "不得暴露支付流水标识")
        self.assertNotIn("13800000002", correct.content.decode(),
                         "不得返回完整手机号")

    def test_pending_reservation_can_be_cancelled_and_releases_stock(self):
        payload = self._reservation_payload()
        created = self.client.post(
            "/api/v1/reservations",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-test-key-003",
        )
        booking_no = created.json()["booking_no"]
        access_token = created.json()["access_token"]
        cancelled = self.client.post(
            f"/api/v1/reservations/{booking_no}/cancel",
            data=json.dumps({"token": access_token}),
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
        access_token = created.json()["access_token"]
        response = self.client.post(
            "/api/v1/payments/intents",
            data=json.dumps({"booking_no": booking_no, "token": access_token}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["provider"], "mock")
        self.assertIn("mock_callback_example", response.json())


    def test_public_reserve_page_renders_template(self):
        response = self.client.get("/reserve/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "public/booking.html")
        # 32f952b 页面重做后的现行文案
        self.assertContains(response, "在线预订")
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



class FrontDeskWriteApiTests(TestCase):
    """前台写 API 必须具备当前物业的入住查看与修改权限。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.today = timezone.localdate()
        self.tomorrow = self.today + timedelta(days=1)
        self.user = User.objects.create_user(
            username="frontdesk-write-user",
            password="TestPass-Write-2026!",
        )
        self.profile = StaffProfile.objects.create(
            user=self.user,
            full_name="写操作前台测试员",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        self.membership = PropertyStaffMembership.objects.create(
            staff_profile=self.profile,
            property=self.property,
            employee_no="FD-WRITE-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        self.role = Group.objects.get(name="前台")
        self.view_permission = Permission.objects.get(
            content_type__app_label="hotelapp", codename="view_accommodation"
        )
        self.change_permission = Permission.objects.get(
            content_type__app_label="hotelapp", codename="change_accommodation"
        )
        self.role.permissions.add(self.view_permission, self.change_permission)
        StaffRoleAssignment.objects.create(
            membership=self.membership,
            group=self.role,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        self.room_type = RoomType.objects.create(
            property=self.property,
            code="FD-WRITE-KING",
            name="写操作测试大床房",
            capacity=2,
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="FD-WRITE-BAR",
            name="写操作测试房价",
            valid_from=self.today,
        )
        self.guest = Guest.objects.create(
            property=self.property,
            full_name="写操作测试住客",
            phone="13800007777",
            phone_normalized="13800007777",
        )
        self.reservation = Reservation.objects.create(
            property=self.property,
            booking_no="RFDWRITE0001",
            guest=self.guest,
            status=Reservation.Status.CONFIRMED,
            check_in=self.today,
            check_out=self.tomorrow,
            room_count=1,
            adult_count=2,
            total_amount=Decimal("500.00"),
            currency="CNY",
        )
        self.line = ReservationRoom.objects.create(
            property=self.property,
            reservation=self.reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=self.today,
            check_out=self.tomorrow,
            nightly_rates=[{
                "date": self.today.isoformat(),
                "price": "500.00",
                "currency": "CNY",
            }],
            amount=Decimal("500.00"),
        )
        self.folio = Folio.objects.create(
            property=self.property,
            reservation=self.reservation,
            guest=self.guest,
            currency="CNY",
        )
        self.room = Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="FD-W801",
            floor="8",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )

    def test_front_desk_write_api_denies_missing_change_permission(self):
        self.role.permissions.remove(self.change_permission)
        self.client.force_login(self.user)
        response = self.client.post(
            f"/api/v1/front-desk/reservation-rooms/{self.line.pk}/assign",
            data=json.dumps({"room_id": self.room.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_assign_checkin_collect_and_checkout_api_flow(self):
        self.client.force_login(self.user)
        assigned = self.client.post(
            f"/api/v1/front-desk/reservation-rooms/{self.line.pk}/assign",
            data=json.dumps({"room_id": self.room.pk}),
            content_type="application/json",
        )
        self.assertEqual(assigned.status_code, 201, assigned.content)
        self.assertEqual(assigned.json()["data"]["assigned_room"]["number"], "FD-W801")

        checked_in = self.client.post(
            f"/api/v1/front-desk/reservation-rooms/{self.line.pk}/check-in",
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(checked_in.status_code, 201, checked_in.content)
        stay_id = checked_in.json()["data"]["stay_id"]

        opened_shift = self.client.post(
            "/api/v1/billing/shifts/open",
            data=json.dumps({"opening_cash": "0.00"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="front-desk-write-shift-001",
        )
        self.assertEqual(opened_shift.status_code, 201, opened_shift.content)

        payment = self.client.post(
            f"/api/v1/front-desk/reservations/{self.reservation.booking_no}/payments",
            data=json.dumps({"amount": "500.00", "method": "CASH"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="front-desk-write-payment-001",
        )
        self.assertEqual(payment.status_code, 201, payment.content)

        checked_out = self.client.post(
            f"/api/v1/front-desk/stays/{stay_id}/check-out",
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(checked_out.status_code, 200, checked_out.content)
        self.room.refresh_from_db()
        self.folio.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(self.room.occupancy_status, Room.OccupancyStatus.VACANT)
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        self.assertEqual(self.folio.status, Folio.Status.CLOSED)
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)


    def test_front_desk_can_request_but_cannot_no_show_or_approve_refunds(self):
        self.client.force_login(self.user)
        no_show = self.client.post(
            f"/api/v1/front-desk/reservation-rooms/{self.line.pk}/no-show",
            data=json.dumps({"reason": "未到店"}),
            content_type="application/json",
        )
        refund_request = self.client.post(
            "/api/v1/front-desk/payments/999999/refunds",
            data=json.dumps({"amount": "10.00", "reason": "退款"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-refund-denied-001",
        )
        refund_approve = self.client.post(
            "/api/v1/front-desk/refunds/999999/approve",
            data=json.dumps({"confirm_refund_paid": True}),
            content_type="application/json",
        )
        self.assertEqual(no_show.status_code, 403)
        self.assertEqual(no_show.json()["error"]["code"], "PERMISSION_DENIED")
        # 退款申请权按业务矩阵授予前台；不存在的支付流水应通过业务校验返回 409。
        self.assertEqual(refund_request.status_code, 409)
        self.assertEqual(refund_request.json()["error"]["code"], "BUSINESS_CONFLICT")
        self.assertEqual(refund_approve.status_code, 403)
        self.assertEqual(refund_approve.json()["error"]["code"], "PERMISSION_DENIED")

    def test_legacy_reservation_list_is_exposed_as_separate_read_only_source(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/front-desk/legacy-reservations?page_size=1")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["meta"]["source_system"], "LEGACY")
        self.assertTrue(response.json()["meta"]["read_only"])


class FrontDeskWorkspacePageTests(TestCase):
    """前台工作台只按当前岗位权限显示写操作能力，服务端仍独立授权。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.user = User.objects.create_user(
            username="frontdesk-page-user",
            password="TestPass-Page-2026!",
        )
        self.profile = StaffProfile.objects.create(
            user=self.user,
            full_name="前台页面测试员",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        self.membership = PropertyStaffMembership.objects.create(
            staff_profile=self.profile,
            property=self.property,
            employee_no="FD-PAGE-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        self.role = Group.objects.get(name="前台")
        self.view_permission = Permission.objects.get(
            content_type__app_label="hotelapp", codename="view_accommodation"
        )
        self.change_permission = Permission.objects.get(
            content_type__app_label="hotelapp", codename="change_accommodation"
        )
        self.role.permissions.add(self.view_permission)
        self.role.permissions.remove(self.change_permission)
        StaffRoleAssignment.objects.create(
            membership=self.membership,
            group=self.role,
            starts_at=timezone.now() - timedelta(minutes=1),
        )

    def test_frontdesk_page_shows_read_only_mode_without_change_permission(self):
        self.client.force_login(self.user)
        response = self.client.get("/checkin/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-can-operate="false"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_frontdesk_page_exposes_operations_for_authorized_role(self):
        self.role.permissions.add(self.change_permission)
        self.client.force_login(self.user)
        response = self.client.get("/checkin/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-can-operate="true"')
        self.assertContains(response, 'id="detail-operations"')
