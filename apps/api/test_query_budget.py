"""ARCH-14：关键列表接口的查询计数基准（回归对比用）。

定位：本测试**只统计查询次数**，用于防止 N+1 回归。它不测量真实延迟，
也不构成 MySQL 性能验收；性能结论必须在 MySQL + 真实数据量下另行测量。

方法：用 `CaptureQueriesContext` 统计一次请求的 SQL 条数，并断言其不超过
与记录数无关的常数上限。若后续改动引入按行查询，条数会随数据量增长而被拦截。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.core.models import Property
from apps.guests.models import Guest
from apps.inventory.models import RatePlan, Room, RoomType
from apps.reservations.models import Reservation, ReservationRoom

# 测试账号口令运行时生成，源码不落可用凭据字面量（secret-scan 门禁）。
TEST_ACCOUNT_PASSWORD = get_random_string(32)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class FrontDeskQueryBudgetTests(TestCase):
    """前台预订列表：查询数不得随预订条数线性增长。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.room_type = RoomType.objects.create(
            property=self.property, code="QB-K", name="查询预算房型"
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="QB-BAR",
            name="查询预算价",
            valid_from=timezone.localdate(),
        )
        Room.objects.create(property=self.property, room_type=self.room_type, number="QB01")
        self._user = self._make_frontdesk_user()

    def _make_frontdesk_user(self):
        """按真实授权链建前台账号：Profile + Membership + 角色组（复用既有模式）。"""
        from apps.identity.models import (
            Group,
            PropertyStaffMembership,
            StaffProfile,
            StaffRoleAssignment,
        )

        user = User.objects.create_user(username="qb-front", password=TEST_ACCOUNT_PASSWORD)
        profile = StaffProfile.objects.create(
            user=user,
            full_name="预算测试员",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property,
            employee_no="QB0001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=Group.objects.get(name="前台"),
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        return user

    def _make_reservations(self, count):
        guest = Guest.objects.create(property=self.property, full_name="预算住客")
        offset = Reservation.objects.count()
        for index in range(count):
            reservation = Reservation.objects.create(
                property=self.property,
                guest=guest,
                booking_no=f"QB{offset + index:06d}",
                status=Reservation.Status.CONFIRMED,
                check_in=timezone.localdate(),
                check_out=timezone.localdate() + timedelta(days=1),
                total_amount=Decimal("100.00"),
            )
            ReservationRoom.objects.create(
                property=self.property,
                reservation=reservation,
                room_type=self.room_type,
                rate_plan=self.rate_plan,
                check_in=reservation.check_in,
                check_out=reservation.check_out,
                amount=Decimal("100.00"),
            )

    def _count_queries(self):
        self.client.force_login(self._user)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/api/v1/front-desk/reservations")
        self.assertEqual(response.status_code, 200, response.content[:300])
        return len(ctx.captured_queries)

    def test_query_count_does_not_grow_linearly_with_rows(self):
        self._make_reservations(2)
        base = self._count_queries()
        self._make_reservations(8)
        grown = self._count_queries()
        # 允许固定开销浮动，但不得随 8 条数据再线性增加 8 倍查询
        self.assertLessEqual(
            grown,
            base + 6,
            f"查询数随数据量线性增长，疑似 N+1：{base} → {grown}",
        )
