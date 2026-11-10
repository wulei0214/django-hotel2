from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Property
from apps.guests.models import Guest
from apps.billing.models import Folio, LedgerEntry, Payment, Refund, RefundLiability
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory
from apps.inventory.services import InventoryUnavailable
from .models import NoShowRecord, Reservation, ReservationRoom, Stay, StayRoomSegment
from .services import ReservationConflict, create_reservation
from .front_desk_services import (
    FrontDeskOperationError, assign_room, check_in_stay,
    collect_manual_payment, check_out_stay,
)
from .lifecycle_services import (
    LifecycleOperationError,
    approve_refund,
    change_stay_room,
    extend_stay,
    mark_reservation_room_no_show,
    receive_manual_deposit,
    request_refund,
)


class ReservationCreationTests(TestCase):
    def setUp(self):
        # 日期动态取今天，避免硬编码日期随时间过期（入住校验要求不早于今天）
        self.today = date.today()
        self.stay_days = (self.today, self.today + timedelta(days=1))
        self.property = Property.objects.create(code="RES", name="预订测试酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="QUEEN", name="高级大床房"
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
                price="500.00",
            )
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=self.room_type,
                stay_date=day,
                total_rooms=2,
            )
        self.payload = {
            "full_name": "测试住客",
            "phone": "13800000000",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": self.today,
            "check_out": self.today + timedelta(days=2),
            "rooms": 1,
            "adults": 2,
            "children": 0,
        }

    def test_create_reservation_holds_inventory_and_creates_folio(self):
        reservation, created, _tk = create_reservation(
            property_obj=self.property,
            payload=self.payload,
            idempotency_key="reservation-test-001",
        )
        self.assertTrue(created)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(reservation.total_amount, 1000)
        self.assertEqual(reservation.reservation_rooms.count(), 1)
        self.assertIsNotNone(reservation.get_master_folio(), "M-04：创建预订生成 MASTER 账单")
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", flat=True)),
            [1, 1],
        )

    def test_same_idempotency_key_returns_same_reservation(self):
        first, created, _tk = create_reservation(
            property_obj=self.property,
            payload=self.payload,
            idempotency_key="reservation-test-002",
        )
        second, created_again, _tk = create_reservation(
            property_obj=self.property,
            payload=self.payload,
            idempotency_key="reservation-test-002",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Guest.objects.count(), 1)
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", flat=True)),
            [1, 1],
        )

    def test_same_idempotency_key_with_different_request_conflicts(self):
        create_reservation(
            property_obj=self.property,
            payload=self.payload,
            idempotency_key="reservation-test-003",
        )
        changed = dict(self.payload, rooms=2)
        with self.assertRaises(ReservationConflict):
            create_reservation(
                property_obj=self.property,
                payload=changed,
                idempotency_key="reservation-test-003",
            )

    def test_insufficient_stock_does_not_leave_partial_reservation(self):
        changed = dict(self.payload, rooms=3)
        with self.assertRaises(InventoryUnavailable):
            create_reservation(
                property_obj=self.property,
                payload=changed,
                idempotency_key="reservation-test-004",
            )
        self.assertEqual(Reservation.objects.count(), 0)
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", flat=True)),
            [0, 0],
        )


    def test_guest_capacity_is_enforced(self):
        changed = dict(self.payload, adults=3)
        self.room_type.capacity = 2
        self.room_type.save(update_fields=("capacity",))
        with self.assertRaises(ValueError):
            create_reservation(
                property_obj=self.property,
                payload=changed,
                idempotency_key="reservation-test-capacity",
            )
        self.assertEqual(Reservation.objects.count(), 0)


class FrontDeskStayLifecycleServiceTests(TestCase):
    """新版入住写操作：物业边界、排房、实际入住、收款、结账与房态。"""

    def setUp(self):
        self.property = Property.objects.create(code="STAY", name="入住测试酒店")
        self.today = timezone.localdate(
            timezone.now(), ZoneInfo(self.property.timezone or "Asia/Shanghai")
        )
        self.tomorrow = self.today + timedelta(days=1)
        self.room_type = RoomType.objects.create(
            property=self.property, code="STAY-KING", name="入住测试大床房", capacity=2
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="STAY-BAR", name="测试房价", valid_from=self.today
        )
        self.guest = Guest.objects.create(
            property=self.property,
            full_name="入住测试住客",
            phone="13800009999",
            phone_normalized="13800009999",
        )
        self.reservation = Reservation.objects.create(
            property=self.property,
            booking_no="RSTAY000001",
            guest=self.guest,
            source="WEBSITE",
            status=Reservation.Status.CONFIRMED,
            check_in=self.today,
            check_out=self.tomorrow,
            room_count=1,
            adult_count=2,
            child_count=0,
            total_amount=Decimal("600.00"),
            currency="CNY",
        )
        self.line = ReservationRoom.objects.create(
            property=self.property,
            reservation=self.reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=self.today,
            check_out=self.tomorrow,
            nightly_rates=[
                {"date": self.today.isoformat(), "price": "600.00", "currency": "CNY"}
            ],
            amount=Decimal("600.00"),
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
            number="ST101",
            floor="1",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )
        self.operator = User.objects.create_user(
            username="stay-operator", password="stay-test-password"
        )
        from apps.billing.services_v2 import BillingService
        BillingService.open_shift(
            property_obj=self.property, cashier=self.operator,
            opening_cash=Decimal("0.00"))

    def test_assign_room_is_property_and_room_type_scoped(self):
        line, changed = assign_room(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            room_id=self.room.pk,
            assigned_by=self.operator,
        )
        self.assertTrue(changed)
        self.assertEqual(line.assigned_room_id, self.room.pk)
        _, changed_again = assign_room(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            room_id=self.room.pk,
            assigned_by=self.operator,
        )
        self.assertFalse(changed_again)

    def test_assign_room_rejects_overlapping_reservation(self):
        other_guest = Guest.objects.create(
            property=self.property,
            full_name="另一位住客",
            phone="13800008888",
            phone_normalized="13800008888",
        )
        other_reservation = Reservation.objects.create(
            property=self.property,
            booking_no="RSTAY000002",
            guest=other_guest,
            status=Reservation.Status.CONFIRMED,
            check_in=self.today,
            check_out=self.tomorrow,
            room_count=1,
            adult_count=1,
            total_amount=Decimal("600.00"),
        )
        ReservationRoom.objects.create(
            property=self.property,
            reservation=other_reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            assigned_room=self.room,
            check_in=self.today,
            check_out=self.tomorrow,
            nightly_rates=[],
            amount=Decimal("600.00"),
        )
        with self.assertRaises(FrontDeskOperationError):
            assign_room(
                property_obj=self.property,
                reservation_room_id=self.line.pk,
                room_id=self.room.pk,
                assigned_by=self.operator,
            )

    def test_check_in_requires_assignment_and_creates_stay(self):
        with self.assertRaises(FrontDeskOperationError):
            check_in_stay(
                property_obj=self.property,
                reservation_room_id=self.line.pk,
                checked_in_by=self.operator,
            )
        assign_room(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            room_id=self.room.pk,
            assigned_by=self.operator,
        )
        stay, created = check_in_stay(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        self.assertTrue(created)
        self.assertEqual(stay.status, Stay.Status.IN_HOUSE)
        self.room.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(self.room.occupancy_status, Room.OccupancyStatus.OCCUPIED)
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_IN)
        duplicate, created_again = check_in_stay(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        self.assertFalse(created_again)
        self.assertEqual(duplicate.pk, stay.pk)

    def test_manual_payment_is_idempotent_and_cannot_overcharge(self):
        payment, created = collect_manual_payment(
            property_obj=self.property,
            booking_no=self.reservation.booking_no,
            amount="200.00",
            method="CASH",
            idempotency_key="stay-payment-001",
            received_by=self.operator,
        )
        self.assertTrue(created)
        duplicate, created_again = collect_manual_payment(
            property_obj=self.property,
            booking_no=self.reservation.booking_no,
            amount="200.00",
            method="CASH",
            idempotency_key="stay-payment-001",
            received_by=self.operator,
        )
        self.assertFalse(created_again)
        self.assertEqual(duplicate.pk, payment.pk)
        self.assertEqual(
            LedgerEntry.objects.get(reference=f"payment:{payment.provider_ref}").amount,
            Decimal("-200.00"),
        )
        with self.assertRaises(FrontDeskOperationError):
            collect_manual_payment(
                property_obj=self.property,
                booking_no=self.reservation.booking_no,
                amount="401.00",
                method="CASH",
                idempotency_key="stay-payment-002",
                received_by=self.operator,
            )

    def test_checkout_requires_zero_balance_then_posts_charge_and_cleans_room(self):
        assign_room(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            room_id=self.room.pk,
            assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        with self.assertRaises(FrontDeskOperationError):
            check_out_stay(
                property_obj=self.property,
                stay_id=stay.pk,
                checked_out_by=self.operator,
            )
        self.assertFalse(
            LedgerEntry.objects.filter(
                reference=f"stay-room-charge:{stay.pk}"
            ).exists()
        )
        collect_manual_payment(
            property_obj=self.property,
            booking_no=self.reservation.booking_no,
            amount="600.00",
            method="CARD",
            idempotency_key="stay-payment-003",
            received_by=self.operator,
        )
        checked_out, changed = check_out_stay(
            property_obj=self.property,
            stay_id=stay.pk,
            checked_out_by=self.operator,
        )
        self.assertTrue(changed)
        self.assertEqual(checked_out.status, Stay.Status.CHECKED_OUT)
        self.assertEqual(
            LedgerEntry.objects.get(reference=f"stay-room-charge:{stay.pk}").amount,
            Decimal("600.00"),
        )
        self.room.refresh_from_db()
        self.folio.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(self.room.occupancy_status, Room.OccupancyStatus.VACANT)
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        self.assertEqual(self.folio.status, Folio.Status.CLOSED)
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)
        _, changed_again = check_out_stay(
            property_obj=self.property,
            stay_id=stay.pk,
            checked_out_by=self.operator,
        )
        self.assertFalse(changed_again)



    def test_final_checkout_registers_refund_liability_without_creating_refund(self):
        assign_room(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            room_id=self.room.pk,
            assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        payment = Payment.objects.create(
            property=self.property,
            reservation=self.reservation,
            folio=self.folio,
            provider="test",
            provider_ref="REFUND-LIABILITY-SOURCE-001",
            method="CARD",
            amount=Decimal("700.00"),
            currency="CNY",
            status=Payment.Status.SUCCEEDED,
            paid_at=timezone.now(),
        )
        LedgerEntry.objects.create(
            property=self.property,
            folio=self.folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-700.00"),
            currency="CNY",
            business_date=self.today,
            reference=f"payment:{payment.provider_ref}",
            description="超额预收款，退房后形成待退款负债",
            posted_by=self.operator,
        )

        checked_out, changed = check_out_stay(
            property_obj=self.property,
            stay_id=stay.pk,
            checked_out_by=self.operator,
        )

        self.assertTrue(changed)
        self.assertEqual(checked_out.status, Stay.Status.CHECKED_OUT)
        liability = RefundLiability.objects.get(folio=self.folio)
        self.assertEqual(liability.amount, Decimal("100.00"))
        self.assertEqual(liability.remaining_amount, Decimal("100.00"))
        self.assertEqual(liability.status, RefundLiability.Status.OPEN)
        self.assertEqual(
            list(liability.source_payments.order_by("pk")),
            [payment],
        )
        self.assertFalse(Refund.objects.filter(
            payment=payment, refund_liability=liability).exists())
        self.folio.refresh_from_db()
        self.assertEqual(self.folio.status, Folio.Status.CLOSED)

        replay, changed_again = check_out_stay(
            property_obj=self.property,
            stay_id=stay.pk,
            checked_out_by=self.operator,
        )
        self.assertFalse(changed_again)
        self.assertEqual(replay.pk, stay.pk)
        self.assertEqual(RefundLiability.objects.filter(folio=self.folio).count(), 1)

    def test_final_checkout_registers_deposit_refund_liability(self):
        """真实退房时，前台押金凭证也必须支持登记待退款负债。"""
        assign_room(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            room_id=self.room.pk,
            assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        payment = Payment.objects.create(
            property=self.property, reservation=self.reservation,
            folio=self.folio, provider="frontdesk_deposit",
            provider_ref="REFUND-LIABILITY-DEPOSIT-001", method="CARD",
            amount=Decimal("700.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now(),
        )
        LedgerEntry.objects.create(
            property=self.property, folio=self.folio,
            entry_type=LedgerEntry.EntryType.DEPOSIT,
            amount=Decimal("-700.00"), currency="CNY",
            business_date=self.today,
            reference=f"deposit:{payment.provider_ref}",
            description="前台押金，退房后形成待退款负债",
            posted_by=self.operator,
        )

        checked_out, changed = check_out_stay(
            property_obj=self.property, stay_id=stay.pk,
            checked_out_by=self.operator,
        )

        self.assertTrue(changed)
        self.assertEqual(checked_out.status, Stay.Status.CHECKED_OUT)
        liability = RefundLiability.objects.get(folio=self.folio)
        self.assertEqual(liability.amount, Decimal("100.00"))
        self.assertEqual(liability.remaining_amount, Decimal("100.00"))
        self.assertEqual(
            list(liability.source_payments.order_by("pk").values_list("pk", flat=True)),
            [payment.pk],
        )
        self.assertFalse(Refund.objects.filter(
            payment=payment, refund_liability=liability).exists())
        self.folio.refresh_from_db()
        self.assertEqual(self.folio.status, Folio.Status.CLOSED)

    def test_partial_checkout_keeps_prepaid_credit_for_other_room(self):
        self.reservation.room_count = 2
        self.reservation.total_amount = Decimal("1200.00")
        self.reservation.save(update_fields=("room_count", "total_amount", "updated_at"))
        second_line = ReservationRoom.objects.create(
            property=self.property,
            reservation=self.reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=self.today,
            check_out=self.tomorrow,
            nightly_rates=[
                {"date": self.today.isoformat(), "price": "600.00", "currency": "CNY"}
            ],
            amount=Decimal("600.00"),
        )
        second_room = Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="ST102",
            floor="1",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )
        assign_room(
            property_obj=self.property, reservation_room_id=self.line.pk,
            room_id=self.room.pk, assigned_by=self.operator,
        )
        assign_room(
            property_obj=self.property, reservation_room_id=second_line.pk,
            room_id=second_room.pk, assigned_by=self.operator,
        )
        first_stay, _ = check_in_stay(
            property_obj=self.property, reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        second_stay, _ = check_in_stay(
            property_obj=self.property, reservation_room_id=second_line.pk,
            checked_in_by=self.operator,
        )
        LedgerEntry.objects.create(
            property=self.property, folio=self.folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-1200.00"), currency="CNY",
            business_date=self.today, reference="STAY-PREPAID-MULTI-001",
            description="多房预付测试",
        )

        first_out, changed = check_out_stay(
            property_obj=self.property, stay_id=first_stay.pk,
            checked_out_by=self.operator,
        )
        self.assertTrue(changed)
        self.folio.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(first_out.status, Stay.Status.CHECKED_OUT)
        self.assertEqual(self.folio.status, Folio.Status.OPEN)
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_IN)
        interim_balance = sum(
            self.folio.entries.values_list("amount", flat=True),
            start=Decimal("0.00"),
        )
        self.assertEqual(interim_balance, Decimal("-600.00"))

        second_out, changed = check_out_stay(
            property_obj=self.property, stay_id=second_stay.pk,
            checked_out_by=self.operator,
        )
        self.assertTrue(changed)
        self.folio.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(second_out.status, Stay.Status.CHECKED_OUT)
        self.assertEqual(self.folio.status, Folio.Status.CLOSED)
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)
        final_balance = sum(
            self.folio.entries.values_list("amount", flat=True),
            start=Decimal("0.00"),
        )
        self.assertEqual(final_balance, Decimal("0.00"))

    def test_prepaid_reservation_can_checkout_without_second_collection(self):
        assign_room(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            room_id=self.room.pk,
            assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property,
            reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        LedgerEntry.objects.create(
            property=self.property,
            folio=self.folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-600.00"),
            currency="CNY",
            business_date=self.today,
            reference="STAY-PREPAID-001",
            description="预付测试",
        )
        checked_out, changed = check_out_stay(
            property_obj=self.property,
            stay_id=stay.pk,
            checked_out_by=self.operator,
        )
        self.assertTrue(changed)
        self.assertEqual(checked_out.status, Stay.Status.CHECKED_OUT)
        balance = sum(
            self.folio.entries.values_list("amount", flat=True),
            start=Decimal("0.00"),
        )
        self.assertEqual(balance, Decimal("0.00"))


    def test_extension_updates_new_night_price_inventory_and_total_snapshot(self):
        extension_date = self.tomorrow
        new_check_out = extension_date + timedelta(days=1)
        DailyRate.objects.create(
            property=self.property,
            rate_plan=self.rate_plan,
            room_type=self.room_type,
            stay_date=extension_date,
            price=Decimal("650.00"),
        )
        inventory = RoomTypeInventory.objects.create(
            property=self.property,
            room_type=self.room_type,
            stay_date=extension_date,
            total_rooms=2,
        )
        assign_room(
            property_obj=self.property, reservation_room_id=self.line.pk,
            room_id=self.room.pk, assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property, reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )

        _, line, extension_amount = extend_stay(
            property_obj=self.property, stay_id=stay.pk,
            new_check_out=new_check_out, reason="客人申请延住一晚",
            extended_by=self.operator,
        )

        self.reservation.refresh_from_db()
        inventory.refresh_from_db()
        self.assertEqual(extension_amount, Decimal("650.00"))
        self.assertEqual(line.check_out, new_check_out)
        self.assertEqual(line.amount, Decimal("1250.00"))
        self.assertEqual(self.reservation.check_out, new_check_out)
        self.assertEqual(self.reservation.total_amount, Decimal("1250.00"))
        self.assertEqual(inventory.sold, 1)

    def test_extension_rejects_conflicting_future_assignment_for_current_room(self):
        extension_date = self.tomorrow
        new_check_out = extension_date + timedelta(days=1)
        DailyRate.objects.create(
            property=self.property, rate_plan=self.rate_plan,
            room_type=self.room_type, stay_date=extension_date,
            price=Decimal("650.00"),
        )
        RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type,
            stay_date=extension_date, total_rooms=2,
        )
        other_guest = Guest.objects.create(
            property=self.property, full_name="后续预订住客",
            phone="13800001111", phone_normalized="13800001111",
        )
        other_reservation = Reservation.objects.create(
            property=self.property, booking_no="RSTAY-FUTURE-ROOM",
            guest=other_guest, status=Reservation.Status.CONFIRMED,
            check_in=extension_date, check_out=new_check_out,
            room_count=1, adult_count=1, total_amount=Decimal("650.00"),
        )
        ReservationRoom.objects.create(
            property=self.property, reservation=other_reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            assigned_room=self.room, check_in=extension_date,
            check_out=new_check_out, nightly_rates=[],
            amount=Decimal("650.00"),
        )
        assign_room(
            property_obj=self.property, reservation_room_id=self.line.pk,
            room_id=self.room.pk, assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property, reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        with self.assertRaises(LifecycleOperationError):
            extend_stay(
                property_obj=self.property, stay_id=stay.pk,
                new_check_out=new_check_out, reason="延住申请",
                extended_by=self.operator,
            )

    def test_in_house_room_change_preserves_room_segment_history(self):
        assign_room(
            property_obj=self.property, reservation_room_id=self.line.pk,
            room_id=self.room.pk, assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property, reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        target = Room.objects.create(
            property=self.property, room_type=self.room_type,
            number="ST103", floor="1",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )

        moved, changed = change_stay_room(
            property_obj=self.property, stay_id=stay.pk,
            new_room_id=target.pk, reason="空调故障换房",
            moved_by=self.operator,
        )

        self.assertTrue(changed)
        self.assertEqual(moved.room_id, target.pk)
        segments = list(StayRoomSegment.objects.filter(stay=stay).order_by("started_at"))
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].room_id, self.room.pk)
        self.assertIsNotNone(segments[0].ended_at)
        self.assertEqual(segments[1].room_id, target.pk)
        self.assertIsNone(segments[1].ended_at)
        self.room.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(self.room.occupancy_status, Room.OccupancyStatus.VACANT)
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        self.assertEqual(target.occupancy_status, Room.OccupancyStatus.OCCUPIED)

    def test_no_show_releases_remaining_sold_inventory_and_blocks_later_checkin(self):
        stock = RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type,
            stay_date=self.today, total_rooms=2, sold=1,
        )
        record, created = mark_reservation_room_no_show(
            property_obj=self.property, reservation_room_id=self.line.pk,
            reason="客人未在规定时间到店", processed_by=self.operator,
        )
        self.assertTrue(created)
        self.assertEqual(record.released_nights, 1)
        stock.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(stock.sold, 0)
        self.assertEqual(self.reservation.status, Reservation.Status.NO_SHOW)
        self.assertTrue(NoShowRecord.objects.filter(reservation_room=self.line).exists())
        with self.assertRaises(FrontDeskOperationError):
            assign_room(
                property_obj=self.property, reservation_room_id=self.line.pk,
                room_id=self.room.pk, assigned_by=self.operator,
            )
        duplicate, changed_again = mark_reservation_room_no_show(
            property_obj=self.property, reservation_room_id=self.line.pk,
            reason="重复处理", processed_by=self.operator,
        )
        self.assertFalse(changed_again)
        self.assertEqual(duplicate.pk, record.pk)

    def test_deposit_refund_requires_separate_approver_and_posts_immutable_entries(self):
        deposit, created = receive_manual_deposit(
            property_obj=self.property, booking_no=self.reservation.booking_no,
            amount="200.00", method="CASH", idempotency_key="deposit-001",
            received_by=self.operator,
        )
        self.assertTrue(created)
        self.assertEqual(deposit.status, Payment.Status.SUCCEEDED)

        refund, created = request_refund(
            property_obj=self.property, payment_id=deposit.pk,
            amount="100.00", reason="客人提前离店退还部分押金",
            idempotency_key="refund-001", requested_by=self.operator,
        )
        self.assertTrue(created)
        with self.assertRaises(LifecycleOperationError):
            approve_refund(
                property_obj=self.property, refund_id=refund.pk,
                approved_by=self.operator,
            )

        supervisor = User.objects.create_user(
            username="stay-refund-supervisor", password="refund-test-password"
        )
        approved, changed = approve_refund(
            property_obj=self.property, refund_id=refund.pk,
            approved_by=supervisor,
        )
        self.assertTrue(changed)
        self.assertEqual(approved.status, Refund.Status.APPROVED)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"refund:{refund.provider_ref}").exists())

        executor = User.objects.create_user(
            username="stay-refund-executor", password="refund-exec-password")
        from apps.billing.services_v2 import BillingService
        # 现金退款必须来自真实可用现金；本测试为执行班次准备 100 元备用现金。
        BillingService.open_shift(
            property_obj=self.property, cashier=executor,
            opening_cash=Decimal("100.00"))
        executed, did_execute = BillingService.execute_refund(
            property_obj=self.property, refund_id=refund.pk,
            executed_by=executor, confirm_refund_paid=True,
            execution_reference="STAY-MANUAL-REFUND-001",
            idempotency_key="stay-refund-exec-001")
        self.assertTrue(did_execute)
        self.assertEqual(executed.status, Refund.Status.SUCCEEDED)
        self.assertEqual(executed.approved_by_id, supervisor.pk)
        self.assertEqual(executed.executed_by_id, executor.pk)
        deposit.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(deposit.status, Payment.Status.PARTIALLY_REFUNDED)
        # WB-030/DEC-DEP-01：押金由服务端按房费 30% 强制计算（600×30%=180），
        # 调用方传入的 200 仅作展示核对；退 100 后累计押金余 80。
        self.assertEqual(self.reservation.deposit_amount, Decimal("80.00"))
        self.assertEqual(
            LedgerEntry.objects.get(reference=f"deposit:{deposit.provider_ref}").amount,
            Decimal("-180.00"),
        )
        self.assertEqual(
            LedgerEntry.objects.get(reference=f"refund:{refund.provider_ref}").amount,
            Decimal("100.00"),
        )


    def test_partial_no_show_then_last_stay_checkout_closes_multi_room_folio(self):
        self.reservation.room_count = 2
        self.reservation.total_amount = Decimal("1200.00")
        self.reservation.save(update_fields=("room_count", "total_amount", "updated_at"))
        second_line = ReservationRoom.objects.create(
            property=self.property, reservation=self.reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=self.today, check_out=self.tomorrow,
            nightly_rates=[{
                "date": self.today.isoformat(), "price": "600.00", "currency": "CNY"
            }],
            amount=Decimal("600.00"),
        )
        RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type,
            stay_date=self.today, total_rooms=2, sold=1,
        )
        assign_room(
            property_obj=self.property, reservation_room_id=self.line.pk,
            room_id=self.room.pk, assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property, reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        mark_reservation_room_no_show(
            property_obj=self.property, reservation_room_id=second_line.pk,
            reason="第二间房住客未到", processed_by=self.operator,
        )
        LedgerEntry.objects.create(
            property=self.property, folio=self.folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-600.00"), currency="CNY",
            business_date=self.today, reference="STAY-PARTIAL-NOSHOW-001",
            description="已入住客房预付款",
        )

        checked_out, changed = check_out_stay(
            property_obj=self.property, stay_id=stay.pk,
            checked_out_by=self.operator,
        )
        self.assertTrue(changed)
        self.assertEqual(checked_out.status, Stay.Status.CHECKED_OUT)
        self.folio.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(self.folio.status, Folio.Status.CLOSED)
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)


    def test_repeating_the_same_extension_target_is_idempotent(self):
        extension_date = self.tomorrow
        new_check_out = extension_date + timedelta(days=1)
        DailyRate.objects.create(
            property=self.property, rate_plan=self.rate_plan,
            room_type=self.room_type, stay_date=extension_date,
            price=Decimal("650.00"),
        )
        inventory = RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type,
            stay_date=extension_date, total_rooms=2,
        )
        assign_room(
            property_obj=self.property, reservation_room_id=self.line.pk,
            room_id=self.room.pk, assigned_by=self.operator,
        )
        stay, _ = check_in_stay(
            property_obj=self.property, reservation_room_id=self.line.pk,
            checked_in_by=self.operator,
        )
        extend_stay(
            property_obj=self.property, stay_id=stay.pk,
            new_check_out=new_check_out, reason="延住",
            extended_by=self.operator,
        )
        _, line, replay_amount = extend_stay(
            property_obj=self.property, stay_id=stay.pk,
            new_check_out=new_check_out, reason="延住",
            extended_by=self.operator,
        )
        inventory.refresh_from_db()
        self.assertEqual(replay_amount, Decimal("0.00"))
        self.assertEqual(line.amount, Decimal("1250.00"))
        self.assertEqual(inventory.sold, 1)
