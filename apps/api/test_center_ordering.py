"""管理中心列表排序契约（服务端白名单 + 前端列声明一致性）。

背景：把 pms-ux 的 opt-in 能力（筛选 / 排序 / 分页同步 URL）接到真实管理中心页面时，
排序必须在**服务端**执行 —— 浏览器内排序只作用于当前页，在服务端分页下会让用户误以为
「整表已排序」。因此排序键必须同时满足：

1. 前端可排序列（``apps/api/pages.py`` 的 ``columns[*].sort``）与服务端白名单
   （``apps/api/center_ordering.CENTER_ORDERING``）一一对应，避免「前端能点、后端不认」；
2. 白名单外的 ``order_by`` 一律忽略（不 400、不改变默认排序）；
3. ``asc`` / ``desc`` 两个方向都真实生效。
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from django.contrib.auth.models import Group, Permission, User
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.core.api_contract import apply_ordering
from apps.core.models import Property
from apps.guests.models import Guest, GuestMembership
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.inventory.models import Room, RoomType
from apps.reservations.models import Reservation

from .center_ordering import CENTER_ORDERING
from .pages import MANAGEMENT_CENTERS

# 使用通用管理中心模板（templates/pms/management_center.html）的中心
GENERIC_CENTER_KEYS = ("guests", "rooms", "memberships", "reservations")


class CenterOrderingRegistryTests(SimpleTestCase):
    """前端列声明与服务端白名单的一致性（纯静态，无需数据库）。"""

    def test_every_sortable_column_is_registered(self):
        for center in MANAGEMENT_CENTERS:
            config = cast(dict[str, Any], MANAGEMENT_CENTERS[center])
            allowed = CENTER_ORDERING.get(center, {})
            for column in config.get("columns", []):
                key = column.get("sort")
                if not key:
                    continue
                self.assertIn(
                    key,
                    allowed,
                    f"中心 {center} 的列「{column.get('label')}」声明了 sort={key}，"
                    f"但未登记到 apps/api/center_ordering.py",
                )

    def test_every_registered_key_is_used_by_a_column(self):
        for center, allowed in CENTER_ORDERING.items():
            config = cast(dict[str, Any], MANAGEMENT_CENTERS.get(center) or {})
            used = {c.get("sort") for c in config.get("columns", []) if c.get("sort")}
            for key in allowed:
                self.assertIn(
                    key,
                    used,
                    f"中心 {center} 的排序键 {key} 已登记但没有任何列使用（前后端会漂移）",
                )

    def test_generic_centers_all_registered(self):
        for center in GENERIC_CENTER_KEYS:
            self.assertIn(
                center,
                CENTER_ORDERING,
                f"通用管理中心模板服务 {center}，必须登记排序白名单",
            )

    def test_registered_keys_are_safe_identifiers(self):
        for center, allowed in CENTER_ORDERING.items():
            for key, field in allowed.items():
                self.assertRegex(key, r"^[a-z][a-z0-9_]*$", f"{center}.{key} 命名不合规")
                self.assertRegex(
                    field,
                    r"^[a-z][a-z0-9_]*(__[a-z][a-z0-9_]*)*$",
                    f"{center}.{key} 的 ORM 字段路径不合规：{field}",
                )


class ApplyOrderingUnitTests(SimpleTestCase):
    """apply_ordering 的白名单与方向语义。"""

    def setUp(self):
        self.factory = RequestFactory()

    def _ordering(self, query_string, allowed=None):
        request = self.factory.get("/api/v1/x/" + query_string)
        return apply_ordering(
            Guest.objects.none(),
            request,
            allowed if allowed is not None else {"full_name": "full_name"},
        )

    def test_unknown_key_keeps_default_ordering(self):
        qs = self._ordering("?order_by=password")
        self.assertEqual(list(qs.query.order_by), [])

    def test_asc_is_default_direction(self):
        qs = self._ordering("?order_by=full_name")
        self.assertEqual(list(qs.query.order_by), ["full_name", "id"])

    def test_desc_direction_prefixes_minus(self):
        qs = self._ordering("?order_by=full_name&direction=desc")
        self.assertEqual(list(qs.query.order_by), ["-full_name", "id"])

    def test_invalid_direction_falls_back_to_asc(self):
        qs = self._ordering("?order_by=full_name&direction=drop%20table")
        self.assertEqual(list(qs.query.order_by), ["full_name", "id"])

    def test_empty_order_by_keeps_default_ordering(self):
        qs = self._ordering("")
        self.assertEqual(list(qs.query.order_by), [])


class _CenterOrderingApiTestBase(TestCase):
    """按物业 + 最小权限组造数，供各中心排序端点做 HTTP 级验证。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.client = Client()

    def _make_viewer(self, username, permission_codes):
        user = User.objects.create_user(username=username, password=get_random_string(16))
        profile = StaffProfile.objects.create(
            user=user,
            full_name=username,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property,
            employee_no=username,
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        group = Group.objects.create(name=f"{username}-group")
        for code in permission_codes:
            app_label, codename = code.split(".", 1)
            group.permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=group,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        self.client.force_login(user)
        return user

    def _make_room_type(self, code):
        return RoomType.objects.create(
            property=self.property,
            code=code,
            name=f"排序测试房型 {code}",
            capacity=2,
        )

    def _make_guest(self, full_name, phone):
        return Guest.objects.create(
            property=self.property,
            full_name=full_name,
            phone=phone,
        )


class RoomCenterOrderingApiTests(_CenterOrderingApiTestBase):
    """房态与库存中心：/api/v1/rooms/status 的排序。"""

    def setUp(self):
        super().setUp()
        self._make_viewer("order_rooms_viewer", ["hotelapp.view_room"])
        room_type = self._make_room_type("ORD-RT")
        for number in ("8303", "8301", "8302"):
            Room.objects.create(
                property=self.property,
                room_type=room_type,
                number=number,
                floor="8",
            )

    def _numbers(self, query_string):
        response = self.client.get("/api/v1/rooms/status" + query_string)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["number"] for row in response.json()["data"]]

    def test_ascending_by_number(self):
        self.assertEqual(self._numbers("?order_by=number&direction=asc"), ["8301", "8302", "8303"])

    def test_descending_by_number(self):
        self.assertEqual(self._numbers("?order_by=number&direction=desc"), ["8303", "8302", "8301"])

    def test_unknown_order_key_is_ignored_not_rejected(self):
        response = self.client.get("/api/v1/rooms/status?order_by=secret_field")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["data"]), 3)


class GuestCenterOrderingApiTests(_CenterOrderingApiTestBase):
    """Guest 360：/api/v1/guests 的排序。"""

    def setUp(self):
        super().setUp()
        self._make_viewer("order_guests_viewer", ["hotelapp.view_client"])
        for index, name in enumerate(("王排序", "李排序", "陈排序")):
            self._make_guest(name, f"1390000{index:04d}")

    def _names(self, query_string):
        response = self.client.get("/api/v1/guests" + query_string)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["full_name"] for row in response.json()["data"]]

    def test_ascending_by_full_name(self):
        names = self._names("?order_by=full_name&direction=asc")
        self.assertEqual(names, sorted(names))

    def test_descending_by_full_name(self):
        names = self._names("?order_by=full_name&direction=desc")
        self.assertEqual(names, sorted(names, reverse=True))


class MembershipCenterOrderingApiTests(_CenterOrderingApiTestBase):
    """会员中心：/api/v1/memberships 的排序。"""

    def setUp(self):
        super().setUp()
        self._make_viewer("order_vip_viewer", ["hotelapp.view_vip"])
        for index, number in enumerate(("MB-003", "MB-001", "MB-002")):
            guest = self._make_guest(f"会员客{index}", f"1380000{index:04d}")
            GuestMembership.objects.create(
                property=self.property,
                guest=guest,
                member_no=number,
                level_code="STANDARD",
                status=GuestMembership.Status.ACTIVE,
            )

    def _member_numbers(self, query_string):
        response = self.client.get("/api/v1/memberships" + query_string)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["member_no"] for row in response.json()["data"]]

    def test_ascending_by_member_no(self):
        self.assertEqual(
            self._member_numbers("?order_by=member_no&direction=asc"),
            ["MB-001", "MB-002", "MB-003"],
        )

    def test_descending_by_member_no(self):
        self.assertEqual(
            self._member_numbers("?order_by=member_no&direction=desc"),
            ["MB-003", "MB-002", "MB-001"],
        )


class ReservationCenterOrderingApiTests(_CenterOrderingApiTestBase):
    """预订与入住中心：/api/v1/front-desk/reservations 的排序。"""

    def setUp(self):
        super().setUp()
        self._make_viewer("order_res_viewer", ["hotelapp.view_accommodation"])
        today = timezone.localdate()
        for index, booking_no in enumerate(("BK-003", "BK-001", "BK-002")):
            guest = self._make_guest(f"预订客{index}", f"1370000{index:04d}")
            Reservation.objects.create(
                property=self.property,
                guest=guest,
                booking_no=booking_no,
                status=Reservation.Status.PENDING,
                check_in=today,
                check_out=today + timedelta(days=1),
                room_count=1,
                adult_count=1,
                child_count=0,
                total_amount=Decimal("100.00"),
            )

    def _booking_numbers(self, query_string):
        response = self.client.get("/api/v1/front-desk/reservations" + query_string)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["booking_no"] for row in response.json()["data"]]

    def test_ascending_by_booking_no(self):
        self.assertEqual(
            self._booking_numbers("?order_by=booking_no&direction=asc"),
            ["BK-001", "BK-002", "BK-003"],
        )

    def test_descending_by_booking_no(self):
        self.assertEqual(
            self._booking_numbers("?order_by=booking_no&direction=desc"),
            ["BK-003", "BK-002", "BK-001"],
        )

    def test_unknown_order_key_is_ignored_not_rejected(self):
        response = self.client.get("/api/v1/front-desk/reservations?order_by=secret_field")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["data"]), 3)
