from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Property
from .models import DailyRate, RatePlan, RoomType, RoomTypeInventory
from .services import get_room_type_availability, stay_dates


class InventoryAvailabilityTests(TestCase):
    def setUp(self):
        self.property = Property.objects.create(code="INV", name="库存测试酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="KING", name="豪华大床房"
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
                price=Decimal("688.00"),
            )
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=self.room_type,
                stay_date=day,
                total_rooms=3,
            )

    def test_availability_aggregates_each_night(self):
        result = get_room_type_availability(
            self.property,
            self.room_type,
            date(2026, 11, 1),
            date(2026, 11, 3),
            rooms=2,
        )
        self.assertEqual(result["available_count"], 3)
        self.assertEqual(result["total_amount"], "2752.00")
        self.assertEqual(len(result["nightly_details"]), 2)

    def test_inventory_uses_minimum_availability_across_nights(self):
        RoomTypeInventory.objects.filter(stay_date=date(2026, 11, 2)).update(sold=2)
        result = get_room_type_availability(
            self.property,
            self.room_type,
            date(2026, 11, 1),
            date(2026, 11, 3),
            rooms=1,
        )
        self.assertEqual(result["available_count"], 1)

    def test_checkout_date_is_exclusive(self):
        self.assertEqual(
            list(stay_dates(date(2026, 11, 1), date(2026, 11, 3))),
            [date(2026, 11, 1), date(2026, 11, 2)],
        )

    def test_invalid_date_range_is_rejected(self):
        with self.assertRaises(ValueError):
            list(stay_dates(date(2026, 11, 2), date(2026, 11, 1)))
