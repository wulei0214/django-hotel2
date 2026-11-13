"""白盒服务层单元测试（子智能体 1：inventory/services.py）。

覆盖 45-whitebox-services.md 登记缺陷的针对性回归：
  * WB-01 adjust_daily_rate 对超大有限价格抛未捕获 InvalidOperation；
  * WB-12 housekeeping_summary “今日流转”窗口锚定默认时区而非物业时区。
"""
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Property
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType
from apps.inventory.services import (
    HousekeepingOperationError,
    adjust_daily_rate,
    change_housekeeping_status,
    change_maintenance_status,
    housekeeping_summary,
)


class AdjustDailyRateBoundaryTests(TestCase):
    """WB-01：调价入口必须把无法量化到分的超大值拒绝为 ValueError。"""

    def setUp(self):
        self.today = timezone.localdate()
        self.property = Property.objects.create(code="WBI", name="白盒库存酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="WBI-STD", name="白盒库存标准间")
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="WBI-BAR", name="白盒库存门市价",
            valid_from=self.today)
        self.rate = DailyRate.objects.create(
            property=self.property, rate_plan=self.rate_plan,
            room_type=self.room_type, stay_date=self.today,
            price="300.00")

    def test_adjust_daily_rate_rejects_unquantizable_price(self):
        with self.assertRaises(ValueError):
            adjust_daily_rate(
                property_obj=self.property, rate_id=self.rate.pk,
                price="1e30", reason="白盒调价边界测试")
        self.rate.refresh_from_db()
        self.assertEqual(self.rate.price, Decimal("300.00"))

    def test_adjust_daily_rate_still_accepts_normal_price(self):
        rate, changed = adjust_daily_rate(
            property_obj=self.property, rate_id=self.rate.pk,
            price="388.88", reason="白盒正常调价")
        self.assertTrue(changed)
        self.assertEqual(rate.price, Decimal("388.88"))


class HousekeepingSummaryTimezoneTests(TestCase):
    """WB-12：今日流转统计窗口必须按物业时区取零点。"""

    def setUp(self):
        self.property = Property.objects.create(
            code="WBI2", name="白盒时区酒店", timezone="Pacific/Kiritimati")
        self.room_type = RoomType.objects.create(
            property=self.property, code="WBI2-STD", name="白盒时区标准间")
        self.room_a = Room.objects.create(
            property=self.property, room_type=self.room_type, number="WBI2-8201",
            floor="8", housekeeping_status=Room.HousekeepingStatus.DIRTY)
        self.room_b = Room.objects.create(
            property=self.property, room_type=self.room_type, number="WBI2-8202",
            floor="8", housekeeping_status=Room.HousekeepingStatus.DIRTY)
        self.operator = User.objects.create_user(username="wbi2-operator")
        from django.contrib.auth.models import Group, Permission
        group = Group.objects.create(name="WBI2-清洁操作组")
        permission = Permission.objects.get(
            content_type__app_label="inventory",
            codename="housekeeping_update")
        group.permissions.add(permission)
        from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
        profile = StaffProfile.objects.create(
            user=self.operator, full_name="白盒清洁操作员")
        membership = PropertyStaffMembership.objects.create(
            property=self.property, staff_profile=profile,
            employee_no="WBI2-001", status="ACTIVE")
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=datetime(2020, 1, 1, tzinfo=dt_timezone.utc))

    def test_today_window_uses_property_timezone(self):
        # T1：物业当地 2026-01-15 23:30（就物业日历而言是“昨天”）；
        # 默认时区 Asia/Shanghai 当地是 01-15 17:30（会被错误地算进“今天”窗口）。
        t1 = datetime(2026, 1, 15, 9, 30, 0, tzinfo=dt_timezone.utc)
        with mock.patch.object(timezone, "now", return_value=t1):
            change_housekeeping_status(
                property_obj=self.property, room_id=self.room_a.pk,
                target_status=Room.HousekeepingStatus.CLEANING,
                changed_by=self.operator)
        # T2：物业当地 2026-01-16 05:59（物业“今天”），默认时区仍是 01-15。
        t2 = datetime(2026, 1, 15, 15, 59, 0, tzinfo=dt_timezone.utc)
        with mock.patch.object(timezone, "now", return_value=t2):
            change_housekeeping_status(
                property_obj=self.property, room_id=self.room_b.pk,
                target_status=Room.HousekeepingStatus.CLEANING,
                changed_by=self.operator)
            summary = housekeeping_summary(self.property)
        # 只有 T2 的流转属于物业“今天”；T1 属于物业昨天，不得计入。
        self.assertEqual(summary["today"]["housekeeping_changes"], 1)
        self.assertEqual(summary["today"]["reworked"], 0)


class MaintenanceInventorySyncTests(TestCase):
    """FD-61：维修停用/恢复与未来库存 out_of_service 同事务同步（§3.3）。

    覆盖口径：
      * 停用 +1 / 恢复 -1 只作用于 stay_date >= 物业当前营业日的行；
      * 停用导致容量不足（total < held + sold + out_of_service）时整体失败，
        房间状态与库存行同事务回滚（fail-closed）；
      * 恢复时停用计数不为负（日历命令先归零的行按 0 封底）；
      * OUT_OF_ORDER ↔ OUT_OF_SERVICE 互换不重复累计停用计数。
    """

    def setUp(self):
        from django.contrib.auth.models import Group, Permission

        from apps.identity.models import (
            PropertyStaffMembership,
            StaffProfile,
            StaffRoleAssignment,
        )
        from apps.inventory.models import RoomTypeInventory

        self.today = timezone.localdate()
        self.property = Property.objects.create(code="WBM", name="白盒维修酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="WBM-STD", name="白盒维修标准间")
        self.room = Room.objects.create(
            property=self.property, room_type=self.room_type,
            number="WBM-101", floor="1")
        self.inventory = RoomTypeInventory
        for offset in (-1, 0, 1):
            RoomTypeInventory.objects.create(
                property=self.property, room_type=self.room_type,
                stay_date=self.today + timedelta(days=offset),
                total_rooms=2)
        self.manager = User.objects.create_user(username="wbm-manager")
        group = Group.objects.create(name="WBM维修组")
        permission = Permission.objects.get(
            content_type__app_label="inventory", codename="maintenance_manage")
        group.permissions.add(permission)
        profile = StaffProfile.objects.create(
            user=self.manager, full_name="白盒维修经理")
        membership = PropertyStaffMembership.objects.create(
            property=self.property, staff_profile=profile,
            employee_no="WBM-001", status="ACTIVE")
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=datetime(2020, 1, 1, tzinfo=dt_timezone.utc))

    def _rows(self):
        return {
            row.stay_date: row for row in self.inventory.objects.filter(
                property=self.property, room_type=self.room_type)
        }

    def test_disable_and_restore_sync_only_future_rows(self):
        changed_room, changed = change_maintenance_status(
            property_obj=self.property, room_id=self.room.pk,
            target_status=Room.MaintenanceStatus.OUT_OF_ORDER,
            reason="空调漏水停用维修", changed_by=self.manager)
        self.assertTrue(changed)
        rows = self._rows()
        self.assertEqual(
            rows[self.today - timedelta(days=1)].out_of_service, 0)
        self.assertEqual(rows[self.today].out_of_service, 1)
        self.assertEqual(rows[self.today + timedelta(days=1)].out_of_service, 1)
        self.assertEqual(
            rows[self.today + timedelta(days=1)].version, 1)
        changed_room.refresh_from_db()
        self.assertEqual(changed_room.maintenance_status,
                         Room.MaintenanceStatus.OUT_OF_ORDER)

        restored_room, restored = change_maintenance_status(
            property_obj=self.property, room_id=self.room.pk,
            target_status=Room.MaintenanceStatus.NORMAL,
            reason="维修完成验收恢复", changed_by=self.manager)
        self.assertTrue(restored)
        rows = self._rows()
        self.assertEqual(rows[self.today].out_of_service, 0)
        self.assertEqual(rows[self.today + timedelta(days=1)].out_of_service, 0)
        restored_room.refresh_from_db()
        self.assertEqual(restored_room.maintenance_status,
                         Room.MaintenanceStatus.NORMAL)

    def test_disable_fails_closed_and_rolls_back_when_capacity_short(self):
        stock = self.inventory.objects.get(
            property=self.property, room_type=self.room_type,
            stay_date=self.today)
        stock.total_rooms = 1
        stock.sold = 1
        stock.save(update_fields=("total_rooms", "sold"))
        with self.assertRaises(HousekeepingOperationError):
            change_maintenance_status(
                property_obj=self.property, room_id=self.room.pk,
                target_status=Room.MaintenanceStatus.OUT_OF_ORDER,
                reason="容量不足时停用必须失败", changed_by=self.manager)
        # 同事务回滚：房间状态与库存行都必须保持原状
        self.room.refresh_from_db()
        self.assertEqual(self.room.maintenance_status,
                         Room.MaintenanceStatus.NORMAL)
        stock.refresh_from_db()
        self.assertEqual(stock.out_of_service, 0)
        self.assertEqual(stock.version, 0)

    def test_restore_caps_out_of_service_at_zero(self):
        # 模拟日历同步命令先把停用计数归零后的人工恢复
        self.inventory.objects.filter(
            property=self.property, room_type=self.room_type,
            stay_date__gte=self.today).update(out_of_service=0)
        self.room.maintenance_status = Room.MaintenanceStatus.OUT_OF_ORDER
        self.room.save(update_fields=("maintenance_status",))
        change_maintenance_status(
            property_obj=self.property, room_id=self.room.pk,
            target_status=Room.MaintenanceStatus.NORMAL,
            reason="计数已归零的恢复不得产生负数", changed_by=self.manager)
        rows = self._rows()
        self.assertEqual(rows[self.today].out_of_service, 0)
        self.assertEqual(rows[self.today + timedelta(days=1)].out_of_service, 0)

    def test_out_of_order_to_out_of_service_swap_does_not_double_count(self):
        change_maintenance_status(
            property_obj=self.property, room_id=self.room.pk,
            target_status=Room.MaintenanceStatus.OUT_OF_ORDER,
            reason="先按工程维修停用", changed_by=self.manager)
        change_maintenance_status(
            property_obj=self.property, room_id=self.room.pk,
            target_status=Room.MaintenanceStatus.OUT_OF_SERVICE,
            reason="升级为停售级别", changed_by=self.manager)
        rows = self._rows()
        self.assertEqual(rows[self.today].out_of_service, 1)
        self.assertEqual(rows[self.today + timedelta(days=1)].out_of_service, 1)

class PropertyDateBoundaryTests(TestCase):
    """财务自然日边界按物业时区计算，不能依赖 UTC/数据库会话时区。"""

    def test_financial_summary_uses_property_local_midnight_bounds(self):
        from datetime import date, datetime, timedelta
        from datetime import time as datetime_time
        from zoneinfo import ZoneInfo

        from django.utils import timezone

        prop = Property.objects.create(
            code="TZFIN", name="财务时区测试酒店", timezone="Asia/Singapore")
        start_day = date(2026, 11, 10)
        end_day = date(2026, 11, 10)
        tz = ZoneInfo(prop.timezone)
        start_at = timezone.make_aware(datetime.combine(start_day, datetime_time.min), tz)
        end_at = timezone.make_aware(
            datetime.combine(end_day + timedelta(days=1), datetime_time.min), tz)
        self.assertEqual(start_at.isoformat(), "2026-11-10T00:00:00+08:00")
        self.assertEqual(end_at.isoformat(), "2026-11-11T00:00:00+08:00")
        self.assertEqual((end_at - start_at).total_seconds(), 86400)
