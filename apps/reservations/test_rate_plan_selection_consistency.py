"""房价计划与房型非 1:1 时的选价口径一致性回归（P0 公开预订链路）。

背景：公开可售查询（apps.inventory.services.get_room_type_availability）与
公开预订下单（apps.reservations.services._get_rate_plan）原先各自选价计划：

  * 查询侧：按"该房型在住宿区间实际有价格行"的计划选（房型非 1:1 时才能
    取到正确房价与可售量）；
  * 下单侧：按"物业下第一个有效计划"选。

房价计划与房型不是 1:1（每房型各挂一个计划是常见配置）时，下单侧取到不含
该房型价格行的计划 → 该房型每日价格行缺失 → 可售量被算成 0 → 误报
"所选日期房量不足"（409）。而访客在公开查询里明明看到有房价、有可售量。
OpenAPI 契约已写明"rate_plan_id 不传由服务按有价计划自动选择"，运行时行为
与契约相悖。

本模块锁定：
  1. 查询侧与下单侧选出的计划必须是同一个（口径同源）。
  2. 计划与房型非 1:1 时，公开预订可正常创建，金额取自该房型自己的计划。
  3. 显式指定 rate_plan_id 时仍以调用方为准。
  4. 该房型在区间内确实无价格 → 报"暂不可预订"，不得误报"房量不足"。
  5. 确有价格但房量售罄 → 报"房量不足"。
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Property
from apps.inventory.models import DailyRate, RatePlan, RoomType, RoomTypeInventory
from apps.inventory.services import (
    InventoryUnavailable,
    get_room_type_availability,
    select_rate_plan_for_room_type,
)
from apps.reservations.services import _get_rate_plan, create_reservation


class RatePlanSelectionConsistencyTests(TestCase):
    """每房型各挂一个房价计划的物业（计划与房型非 1:1）。"""

    def setUp(self):
        self.today = date.today()
        self.check_in = self.today
        self.check_out = self.today + timedelta(days=2)
        self.property = Property.objects.create(code="RPS", name="选价口径测试酒店")

        # 两个房型各挂一个计划；plan_std 先建 → 全局第一个计划是 plan_std。
        self.plan_std = RatePlan.objects.create(
            property=self.property,
            code="BAR_STD",
            name="标准间门市价",
            valid_from=self.today,
        )
        self.plan_sup = RatePlan.objects.create(
            property=self.property,
            code="BAR_SUP",
            name="套房门市价",
            valid_from=self.today,
        )
        self.room_type_std = RoomType.objects.create(
            property=self.property, code="STD", name="标准间"
        )
        self.room_type_sup = RoomType.objects.create(
            property=self.property, code="SUP", name="套房"
        )
        # 房价计划与房型非 1:1：每个房型的价格只挂在自己的计划下。
        for room_type, plan, price in (
            (self.room_type_std, self.plan_std, "300.00"),
            (self.room_type_sup, self.plan_sup, "800.00"),
        ):
            for offset in range(2):
                day = self.today + timedelta(days=offset)
                DailyRate.objects.create(
                    property=self.property,
                    rate_plan=plan,
                    room_type=room_type,
                    stay_date=day,
                    price=Decimal(price),
                )
                RoomTypeInventory.objects.create(
                    property=self.property,
                    room_type=room_type,
                    stay_date=day,
                    total_rooms=3,
                )

    def _payload(self, room_type, phone, rate_plan_id=None):
        payload = {
            "full_name": "口径测试住客",
            "phone": phone,
            "room_type_id": room_type.pk,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        if rate_plan_id is not None:
            payload["rate_plan_id"] = rate_plan_id
        return payload

    def test_query_and_creation_select_the_same_rate_plan(self):
        """查询侧与下单侧必须落到同一个计划（口径同源）。"""
        query_side = get_room_type_availability(
            self.property, self.room_type_sup, self.check_in, self.check_out, rooms=1
        )
        order_side = _get_rate_plan(
            self.property, self.room_type_sup, self.check_in, self.check_out
        )
        self.assertEqual(query_side["rate_plan_id"], order_side.pk)
        self.assertEqual(order_side.pk, self.plan_sup.pk)

    def test_shared_selector_prefers_plan_that_has_price_for_room_type(self):
        """共用选择器：不指定计划时取该房型有价的计划。"""
        selected = select_rate_plan_for_room_type(
            self.property, self.room_type_sup, self.check_in, self.check_out
        )
        self.assertEqual(selected.pk, self.plan_sup.pk)

    def test_creation_succeeds_for_room_type_bound_to_its_own_plan(self):
        """计划与房型非 1:1 时，公开预订必须能创建成功且金额取自本房型计划。"""
        reservation, created, _token = create_reservation(
            property_obj=self.property,
            payload=self._payload(self.room_type_sup, "13900000001"),
            idempotency_key="rate-plan-consistency-001",
        )
        self.assertTrue(created)
        self.assertEqual(reservation.total_amount, Decimal("1600.00"))
        line = reservation.reservation_rooms.get()
        self.assertEqual(line.rate_plan_id, self.plan_sup.pk)

    def test_explicit_rate_plan_id_is_still_respected(self):
        """显式指定计划时以调用方为准，不被自动选择覆盖。"""
        selected = select_rate_plan_for_room_type(
            self.property,
            self.room_type_sup,
            self.check_in,
            self.check_out,
            self.plan_std.pk,
        )
        self.assertEqual(selected.pk, self.plan_std.pk)

    def test_missing_price_reports_not_bookable_instead_of_out_of_stock(self):
        """该房型在区间内无价格 → "暂不可预订"，不得误报"房量不足"。"""
        room_type_without_price = RoomType.objects.create(
            property=self.property, code="NOP", name="无价房型"
        )
        for offset in range(2):
            day = self.today + timedelta(days=offset)
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=room_type_without_price,
                stay_date=day,
                total_rooms=3,
            )
        with self.assertRaises(InventoryUnavailable) as ctx:
            create_reservation(
                property_obj=self.property,
                payload=self._payload(room_type_without_price, "13900000002"),
                idempotency_key="rate-plan-consistency-002",
            )
        message = str(ctx.exception)
        self.assertIn("暂不可预订", message)
        self.assertNotIn("房量不足", message)

    def test_sold_out_inventory_reports_out_of_stock(self):
        """确有价格但房量售罄 → "房量不足"。"""
        room_type_sold_out = RoomType.objects.create(
            property=self.property, code="SOLD", name="售罄房型"
        )
        for offset in range(2):
            day = self.today + timedelta(days=offset)
            DailyRate.objects.create(
                property=self.property,
                rate_plan=self.plan_sup,
                room_type=room_type_sold_out,
                stay_date=day,
                price=Decimal("500.00"),
            )
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=room_type_sold_out,
                stay_date=day,
                total_rooms=0,
            )
        with self.assertRaises(InventoryUnavailable) as ctx:
            create_reservation(
                property_obj=self.property,
                payload=self._payload(room_type_sold_out, "13900000003"),
                idempotency_key="rate-plan-consistency-003",
            )
        message = str(ctx.exception)
        self.assertIn("房量不足", message)
        self.assertNotIn("暂不可预订", message)
