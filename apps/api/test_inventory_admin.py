"""MC-004 房态库存受控写工作台回归（复用既有库存/对账服务）。"""

import json
from datetime import timedelta

from django.test import override_settings

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.core.models import AuditEvent
from apps.inventory.models import (
    DailyRate,
    InventoryDiscrepancy,
    InventoryReconciliationRun,
    Room,
    RoomType,
    RoomTypeInventory,
)
from apps.inventory.services import get_room_type_availability


@override_settings(DEBUG=True, DJANGO_ENV="test")
class InventoryAdminTests(E2EFlowTestBase):
    def setUp(self):
        super().setUp()
        self.manager = self._make_staff("inv_admin_manager", "店长")
        self.duty = self._make_staff("inv_admin_duty", "值班经理")
        self.client.force_login(self.manager)

    def _post(self, url, payload=None, user=None):
        if user:
            self.client.force_login(user)
        return self.client.post(
            url, data=json.dumps(payload or {}, ensure_ascii=False), content_type="application/json"
        )

    def test_close_rate_and_stop_sell_are_derived_not_direct_available_write(self):
        rate = DailyRate.objects.filter(
            property=self.property, room_type=self.room_type, stay_date=self.today
        ).first()
        assert rate is not None
        response = self._post(
            f"/api/v1/inventory/daily-rates/{rate.pk}/close",
            {"closed": True, "reason": "房型维护临时关房"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        rate.refresh_from_db()
        self.assertTrue(rate.closed)

        stock = RoomTypeInventory.objects.filter(
            property=self.property, room_type=self.room_type, stay_date=self.today
        ).first()
        assert stock is not None
        response = self._post(
            f"/api/v1/inventory/stock/{stock.pk}/stop-sell",
            {"stop_sell": True, "reason": "维修影响今日可售"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        stock.refresh_from_db()
        self.assertTrue(stock.stop_sell)
        availability = get_room_type_availability(
            self.property, self.room_type, self.today, self.today + timedelta(days=1), rooms=1
        )
        self.assertEqual(availability["available_count"], 0)
        self.assertTrue(
            AuditEvent.objects.filter(
                property=self.property, action="inventory.stop_sell_changed"
            ).exists()
        )

    def test_room_and_room_type_master_data_actions(self):
        response = self._post(
            "/api/v1/inventory/room-types/create",
            {"code": "MC004T", "name": "MC-004 测试房型", "capacity": 3},
        )
        self.assertEqual(response.status_code, 201, response.content)
        room_type_id = response.json()["data"]["id"]
        response = self._post(
            f"/api/v1/inventory/room-types/{room_type_id}/update",
            {"name": "MC-004 更新房型", "capacity": 4, "reason": "核定人数更正"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(RoomType.objects.get(pk=room_type_id).capacity, 4)

        response = self._post(
            "/api/v1/inventory/rooms/create",
            {"room_type_id": room_type_id, "number": "MC-004-01", "floor": "9"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        room_id = response.json()["data"]["id"]
        room = Room.objects.get(pk=room_id)
        self.assertEqual(room.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        response = self._post(
            f"/api/v1/inventory/rooms/{room_id}/update", {"floor": "10", "reason": "楼层纠正"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        self.assertEqual(room.floor, "10")

    def test_inventory_fix_requires_separation_of_duties(self):
        run = InventoryReconciliationRun.objects.create(  # type: ignore[has-type]
            property=self.property
        )
        disc = InventoryDiscrepancy.objects.create(  # type: ignore[has-type]
            run=run,
            property=self.property,
            room_type=self.room_type,
            stay_date=self.today,
            current_total=3,
            recomputed_total=2,
            diff=1,
        )
        response = self._post(
            f"/api/v1/inventory/discrepancies/{disc.pk}/fix-request",
            {"fix_reason": "房间停用后库存未同步", "fix_evidence": "巡检记录"},
            user=self.duty,
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = self._post(
            f"/api/v1/inventory/discrepancies/{disc.pk}/approve", {}, user=self.duty
        )
        self.assertEqual(response.status_code, 409)
        response = self._post(
            f"/api/v1/inventory/discrepancies/{disc.pk}/approve", {}, user=self.manager
        )
        self.assertEqual(response.status_code, 200, response.content)
        disc.refresh_from_db()
        self.assertEqual(disc.status, InventoryDiscrepancy.Status.FIX_APPROVED)

    # ------------------------------------------------------------------
    # MC11-INV-03/04（11C 盘点整改）：主数据写契约与库存日历即时联动
    # ------------------------------------------------------------------
    def test_room_type_create_invalid_capacity_returns_400(self):
        """MC11-INV-03：capacity 非整数此前穿透 500；契约口径为 400。"""
        response = self._post(
            "/api/v1/inventory/room-types/create",
            {"code": "INV03A", "name": "非法容量房型", "capacity": "abc"},
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertFalse(RoomType.objects.filter(code="INV03A").exists())

    def test_room_type_create_duplicate_code_returns_409(self):
        """MC11-INV-03：房型 code 物业内唯一约束违反此前穿透 500；口径为 409。"""
        first = self._post(
            "/api/v1/inventory/room-types/create",
            {"code": "INV03B", "name": "首建房型", "capacity": 2},
        )
        self.assertEqual(first.status_code, 201, first.content)
        duplicate = self._post(
            "/api/v1/inventory/room-types/create",
            {"code": "INV03B", "name": "重复房型", "capacity": 2},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.content)
        self.assertEqual(duplicate.json()["error"]["code"], "BUSINESS_CONFLICT")
        self.assertEqual(RoomType.objects.filter(code="INV03B").count(), 1)

    def test_room_create_syncs_inventory_calendar_immediately(self):
        """MC11-INV-04：新增房间即时进入库存日历（此前最迟次日 02:30 才生效）。"""
        response = self._post(
            "/api/v1/inventory/rooms/create",
            {"room_type_id": self.room_type.pk, "number": "MC11-INV04", "floor": "3"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        room_id = response.json()["data"]["id"]
        from apps.inventory.models import RoomTypeInventory

        row = RoomTypeInventory.objects.filter(
            property=self.property, room_type=self.room_type, stay_date=self.today
        ).first()
        assert row is not None, "主数据写后今日库存行必须存在"
        self.assertGreaterEqual(row.total_rooms, 1)
        total_after_create = row.total_rooms

        # 停用该房间同样即时反映（total_rooms 相对回落 1）
        deactivate = self._post(
            f"/api/v1/inventory/rooms/{room_id}/update", {"is_active": False, "reason": "MC11 回归"}
        )
        self.assertEqual(deactivate.status_code, 200, deactivate.content)
        row.refresh_from_db()
        self.assertEqual(
            row.total_rooms,
            total_after_create - 1,
            "停用后当日 total_rooms 应相对回落 1（即时联动）",
        )
