import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
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
        self.reservation, _, _tk = create_reservation(
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
        type(record)._base_manager.filter(pk=record.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1))
        expired = self.ApprovalService.approve(
            record.pk, approved_by=self.approver, property_obj=self.property)
        self.assertEqual(expired.status, "EXPIRED")


class NightAuditServiceTests(TestCase):
    def setUp(self):
        self.today = date.today()
        self.property = Property.objects.create(code="NA", name="夜审测试酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="NA-STD", name="夜审标准间")
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="NA-BAR", name="夜审门市价",
            valid_from=self.today)
        self.guest = __import__("apps.guests.models", fromlist=["Guest"]).Guest.objects.create(
            property=self.property, full_name="夜审客人",
            phone="13800008881", phone_normalized="13800008881")
        from apps.reservations.models import Reservation, ReservationRoom, Stay
        from apps.inventory.models import Room
        from apps.billing.night_audit import NightAuditService
        from decimal import Decimal
        from django.contrib.auth.models import User

        self.user = User.objects.create_user(username="night-audit-operator")
        self.reservation = Reservation.objects.create(
            property=self.property, booking_no="NIGHTAUDIT0001",
            guest=self.guest, status=Reservation.Status.CHECKED_IN,
            check_in=self.today, check_out=self.today + timedelta(days=2),
            room_count=1, adult_count=1, total_amount=Decimal("800.00"))
        line = ReservationRoom.objects.create(
            property=self.property, reservation=self.reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=self.today, check_out=self.today + timedelta(days=2),
            nightly_rates=[
                {"date": self.today.isoformat(), "price": "400.00", "currency": "CNY"},
                {"date": (self.today + timedelta(days=1)).isoformat(), "price": "400.00", "currency": "CNY"},
            ], amount=Decimal("800.00"))
        room = Room.objects.create(
            property=self.property, room_type=self.room_type, number="NA101",
            occupancy_status=Room.OccupancyStatus.OCCUPIED)
        self.stay = Stay.objects.create(
            property=self.property, reservation_room=line, room=room,
            status=Stay.Status.IN_HOUSE)
        from apps.billing.models import Folio
        Folio.objects.create(
            property=self.property, reservation=self.reservation,
            guest=self.guest, currency="CNY")
        self.NightAuditService = NightAuditService

    def test_night_audit_posts_daily_room_charge_once_and_closes_business_date(self):
        from apps.billing.models import LedgerEntry
        from apps.core.models import BusinessDate
        run, created = self.NightAuditService.run(
            property_obj=self.property, executed_by=self.user,
            business_date=self.today)
        self.assertTrue(created)
        self.assertEqual(run.status, "SUCCESS")
        self.assertEqual(run.summary["charge_count"], 1)
        self.assertEqual(LedgerEntry.objects.filter(
            property=self.property, business_date=self.today,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE).count(), 1)
        self.assertEqual(BusinessDate.objects.get(
            property=self.property, business_date=self.today).status,
            BusinessDate.Status.CLOSED)
        replay, replay_created = self.NightAuditService.run(
            property_obj=self.property, executed_by=self.user,
            business_date=self.today)
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, run.pk)
        self.assertEqual(LedgerEntry.objects.filter(
            property=self.property, business_date=self.today).count(), 1)

    def test_night_audit_requires_second_manager_to_reopen(self):
        from apps.billing.models import LedgerEntry
        from apps.core.models import BusinessDate
        from django.contrib.auth.models import User
        run, _ = self.NightAuditService.run(
            property_obj=self.property, executed_by=self.user,
            business_date=self.today)
        bd = BusinessDate.objects.get(
            property=self.property, business_date=self.today)
        requester = User.objects.create_user(username="night-audit-requester")
        approver = User.objects.create_user(username="night-audit-approver")
        approval, created = self.NightAuditService.request_reopen(
            property_obj=self.property, business_date_id=bd.pk,
            requested_by=requester, reason="营业日账务复核后需要重开")
        self.assertTrue(created)
        with self.assertRaises(Exception):
            self.NightAuditService.approve_reopen(
                property_obj=self.property, business_date_id=bd.pk,
                approval_id=approval.pk, approved_by=requester)
        reopened = self.NightAuditService.approve_reopen(
            property_obj=self.property, business_date_id=bd.pk,
            approval_id=approval.pk, approved_by=approver)
        self.assertEqual(reopened.status, BusinessDate.Status.REOPENED)
        rerun, rerun_created = self.NightAuditService.run(
            property_obj=self.property, executed_by=approver,
            business_date=self.today)
        self.assertTrue(rerun_created)
        self.assertEqual(rerun.status, "SUCCESS")
        self.assertEqual(LedgerEntry.objects.filter(
            property=self.property, business_date=self.today,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE).count(), 1)


class BillingServiceManualCollectionTests(TestCase):
    def setUp(self):
        today = date.today()
        self.property = Property.objects.create(code="BILL", name="统一账务测试酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="BILL-STD", name="统一账务标准间")
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="BILL-BAR", name="统一账务门市价",
            valid_from=today)
        from apps.guests.models import Guest
        from apps.reservations.models import Reservation, ReservationRoom
        from apps.billing.models import Folio
        self.guest = Guest.objects.create(
            property=self.property, full_name="统一账务客人",
            phone="13800008882", phone_normalized="13800008882")
        self.reservation = Reservation.objects.create(
            property=self.property, booking_no="BILLINGCOLLECT0001",
            guest=self.guest, status=Reservation.Status.CONFIRMED,
            check_in=today, check_out=today + timedelta(days=1),
            room_count=1, adult_count=1, total_amount=Decimal("300.00"))
        ReservationRoom.objects.create(
            property=self.property, reservation=self.reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=today, check_out=today + timedelta(days=1),
            nightly_rates=[{"date": today.isoformat(), "price": "300.00", "currency": "CNY"}],
            amount=Decimal("300.00"))
        self.folio = Folio.objects.create(
            property=self.property, reservation=self.reservation,
            guest=self.guest, currency="CNY")
        self.operator = User.objects.create_user(username="bill-operator")
        from apps.billing.services_v2 import BillingService
        BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash=Decimal("0.00"))

    def test_manual_collection_posts_successful_payment_and_is_idempotent(self):
        from apps.billing.services_v2 import BillingService
        first = BillingService.collect_payment(
            property_obj=self.property, reservation=self.reservation,
            amount="300.00", method="CASH", collected_by=self.operator,
            reference="POS-CASH-REF-001")
        replay = BillingService.collect_payment(
            property_obj=self.property, reservation=self.reservation,
            amount="300.00", method="CASH", collected_by=self.operator,
            reference="POS-CASH-REF-001")
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(first.status, Payment.Status.SUCCEEDED)
        self.assertEqual(LedgerEntry.objects.filter(
            property=self.property, folio=self.folio,
            entry_type=LedgerEntry.EntryType.PAYMENT).count(), 1)
        self.assertEqual(LedgerEntry.objects.get(
            property=self.property, folio=self.folio).amount, Decimal("-300.00"))
