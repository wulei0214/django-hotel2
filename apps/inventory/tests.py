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


class DailyRateAdjustmentTests(TestCase):
    def setUp(self):
        self.today = date.today()
        self.property = Property.objects.create(code="RATE", name="房价调整测试酒店")
        self.other_property = Property.objects.create(code="RATE2", name="其他物业")
        self.room_type = RoomType.objects.create(
            property=self.property, code="RATE-STD", name="房价标准间")
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="RATE-BAR", name="房价测试计划",
            valid_from=self.today)
        self.rate = DailyRate.objects.create(
            property=self.property, rate_plan=self.rate_plan,
            room_type=self.room_type, stay_date=self.today + timedelta(days=1),
            price=Decimal("300.00"))
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username="rate-manager")

    def test_adjustment_updates_price_and_records_audit(self):
        from apps.core.models import AuditEvent
        from apps.inventory.services import adjust_daily_rate
        rate, changed = adjust_daily_rate(
            property_obj=self.property, rate_id=self.rate.pk,
            price="368.00", reason="根据节假日需求调整未来住宿日价格",
            updated_by=self.user)
        self.assertTrue(changed)
        self.assertEqual(rate.price, Decimal("368.00"))
        self.assertEqual(AuditEvent.objects.filter(
            action="inventory.daily_rate_adjust", entity_id=str(rate.pk)).count(), 1)

    def test_adjustment_rejects_cross_property_rate(self):
        from apps.inventory.services import adjust_daily_rate
        with self.assertRaises(ValueError):
            adjust_daily_rate(
                property_obj=self.other_property, rate_id=self.rate.pk,
                price="368.00", reason="越权跨物业调价测试",
                updated_by=self.user)
