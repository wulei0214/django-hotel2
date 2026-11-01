from datetime import date, timedelta

from django.test import TestCase

from apps.core.models import Property
from apps.guests.models import Guest
from apps.inventory.models import DailyRate, RatePlan, RoomType, RoomTypeInventory
from apps.inventory.services import InventoryUnavailable
from .models import Reservation
from .services import ReservationConflict, create_reservation


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
        reservation, created = create_reservation(
            property_obj=self.property,
            payload=self.payload,
            idempotency_key="reservation-test-001",
        )
        self.assertTrue(created)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(reservation.total_amount, 1000)
        self.assertEqual(reservation.reservation_rooms.count(), 1)
        self.assertTrue(hasattr(reservation, "folio"))
        self.assertEqual(
            list(RoomTypeInventory.objects.order_by("stay_date").values_list("held", flat=True)),
            [1, 1],
        )

    def test_same_idempotency_key_returns_same_reservation(self):
        first, created = create_reservation(
            property_obj=self.property,
            payload=self.payload,
            idempotency_key="reservation-test-002",
        )
        second, created_again = create_reservation(
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
