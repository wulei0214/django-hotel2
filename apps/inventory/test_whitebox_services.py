# -*- coding: utf-8 -*-
"""白盒服务层单元测试（子智能体 1：inventory/services.py）。

覆盖 45-whitebox-services.md 登记缺陷的针对性回归：
  * WB-01 adjust_daily_rate 对超大有限价格抛未捕获 InvalidOperation；
  * WB-12 housekeeping_summary “今日流转”窗口锚定默认时区而非物业时区。
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType
from apps.inventory.services import (
    adjust_daily_rate,
    change_housekeeping_status,
    housekeeping_summary,
)
from datetime import timezone as dt_timezone


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
        StaffRoleAssignment.objects.create(membership=membership, group=group)

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
