"""前台接待工作台数据流转回归（2026-11-23 修复批次）。

覆盖四个此前会导致"页面看起来正常、业务事实错误"的缺口：

1. 多房订单部分入住后，未到店房间不得从「今日抵店」与 KPI 消失；
2. 已登记 No-Show 的明细不得继续计入「今日抵店 / 待排房」；
3. 账务摘要必须同时给出「合同待收」与「已入账余额」两种口径——夜宿房费
   在夜审/退房前不入账，只按已入账口径会把"未入账"显示成"已结清"；
4. 在住/离店列表的手机号检索必须走 phone_hash（WB-103 后明文列停写），
   房间视图必须支持房号/房型检索并按 CLEANING 口径筛选。
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.billing.models import Folio, LedgerEntry
from apps.core.models import Property
from apps.guests.models import Guest
from apps.guests.services import GuestService
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.inventory.models import RatePlan, Room, RoomType, RoomTypeInventory
from apps.reservations.models import Reservation, ReservationRoom, Stay

_TEST_PW = get_random_string(24)


class FrontDeskDataFlowTests(TestCase):
    """前台七个页签共用的取数口径回归。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.today = timezone.localdate()
        self.user = self._staff(["view_accommodation", "change_accommodation", "no_show_handle"])
        self.client.force_login(self.user)
        self.room_type = RoomType.objects.create(
            property=self.property,
            code=f"FDDF-{uuid.uuid4().hex[:6].upper()}",
            name="数据流转测试大床房",
            capacity=2,
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code=f"FDDF-{uuid.uuid4().hex[:6].upper()}",
            name="数据流转测试门市价",
            valid_from=self.today - timedelta(days=1),
        )
        self.room_a = self._room("DF101")
        self.room_b = self._room("DF102")

    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------
    def _staff(self, codenames):
        group = Group.objects.create(name=f"FDDF-{uuid.uuid4().hex[:8]}")
        group.permissions.add(
            *Permission.objects.filter(content_type__app_label="hotelapp", codename__in=codenames)
        )
        user = User.objects.create_user(username=f"fddf-{uuid.uuid4().hex[:8]}", password=_TEST_PW)
        profile = StaffProfile.objects.create(
            user=user,
            full_name="数据流转前台",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property,
            employee_no=f"FDDF-{user.pk:06d}",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=group,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        return user

    def _room(self, number):
        return Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number=number,
            floor="1",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )

    def _reservation(self, *, room_count=1, status=Reservation.Status.CONFIRMED, guest=None):
        guest = guest or Guest.objects.create(
            property=self.property, full_name=f"数据流转住客{uuid.uuid4().hex[:4]}"
        )
        total = Decimal("600.00") * room_count
        reservation = Reservation.objects.create(
            property=self.property,
            guest=guest,
            booking_no=f"FDDF{uuid.uuid4().hex[:8].upper()}",
            status=status,
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            room_count=room_count,
            adult_count=1,
            child_count=0,
            total_amount=total,
            currency="CNY",
        )
        lines = [
            ReservationRoom.objects.create(
                property=self.property,
                reservation=reservation,
                room_type=self.room_type,
                rate_plan=self.rate_plan,
                check_in=self.today,
                check_out=self.today + timedelta(days=2),
                amount=Decimal("600.00"),
            )
            for _ in range(room_count)
        ]
        return reservation, lines

    def _folio(self, reservation):
        return Folio.objects.create(
            property=self.property,
            reservation=reservation,
            guest=reservation.guest,
            folio_type=Folio.FolioType.MASTER,
            status=Folio.Status.OPEN,
            currency="CNY",
        )

    def _assign(self, line, room):
        line.assigned_room = room
        line.save(update_fields=("assigned_room",))

    def _post(self, path, payload=None):
        return self.client.post(
            path,
            data={} if payload is None else payload,
            content_type="application/json",
        )

    # ------------------------------------------------------------------
    # 1. 多房订单部分入住
    # ------------------------------------------------------------------
    def test_partial_check_in_keeps_remaining_rooms_visible(self):
        reservation, lines = self._reservation(room_count=2)
        self._assign(lines[0], self.room_a)
        self._assign(lines[1], self.room_b)
        self._folio(reservation)

        response = self._post(f"/api/v1/front-desk/reservation-rooms/{lines[0].pk}/check-in", {})
        self.assertEqual(response.status_code, 201, response.content)

        arrivals = self.client.get("/api/v1/front-desk/arrivals", {"date": self.today.isoformat()})
        self.assertEqual(arrivals.status_code, 200)
        rows = arrivals.json()["data"]
        self.assertEqual(len(rows), 2, "部分入住后同订单其余房间不得从今日抵店消失")
        by_line = {row["reservation_room_id"]: row for row in rows}
        self.assertEqual(by_line[lines[0].pk]["stay"]["status"], "IN_HOUSE")
        self.assertIsNone(by_line[lines[1].pk]["stay"])
        self.assertEqual(by_line[lines[1].pk]["reservation_status"], "CHECKED_IN")

        # 工作台是待办清单：只保留尚未办理入住的明细。
        pending = self.client.get(
            "/api/v1/front-desk/arrivals",
            {"date": self.today.isoformat(), "stage": "pending"},
        )
        self.assertEqual(
            [row["reservation_room_id"] for row in pending.json()["data"]], [lines[1].pk]
        )

        overview = self.client.get(
            "/api/v1/front-desk/overview", {"date": self.today.isoformat()}
        ).json()["data"]
        self.assertEqual(overview["arrivals_count"], 2)
        self.assertEqual(overview["arrivals_checked_in_count"], 1)
        # 两间都已排房，待排房为 0（此前会把已入住的整单算成未排房）。
        self.assertEqual(overview["unassigned_count"], 0)

    def test_expired_pending_line_is_not_counted_as_arrival(self):
        """保留期已过的 PENDING 单库存已释放，不得继续算作今日应到。"""
        _, expired_lines = self._reservation(room_count=1, status=Reservation.Status.PENDING)
        expired_lines[0].reservation.expires_at = timezone.now() - timedelta(minutes=5)
        expired_lines[0].reservation.save(update_fields=("expires_at",))
        _, live_lines = self._reservation(room_count=1, status=Reservation.Status.PENDING)
        live_lines[0].reservation.expires_at = timezone.now() + timedelta(minutes=30)
        live_lines[0].reservation.save(update_fields=("expires_at",))

        arrivals = self.client.get("/api/v1/front-desk/arrivals", {"date": self.today.isoformat()})
        self.assertEqual(
            [row["reservation_room_id"] for row in arrivals.json()["data"]],
            [live_lines[0].pk],
            "过期 PENDING 应排除，保留期内 PENDING 仍应出现在今日抵店",
        )
        overview = self.client.get(
            "/api/v1/front-desk/overview", {"date": self.today.isoformat()}
        ).json()["data"]
        self.assertEqual(overview["arrivals_count"], 1)

    def test_arrivals_rejects_unknown_stage(self):
        response = self.client.get(
            "/api/v1/front-desk/arrivals",
            {"date": self.today.isoformat(), "stage": "checked-in"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    # ------------------------------------------------------------------
    # 2. No-Show 不再占用抵店清单与待排房计数
    # ------------------------------------------------------------------
    def test_no_show_line_is_excluded_from_arrivals_and_kpi(self):
        reservation, lines = self._reservation(room_count=1)
        self._assign(lines[0], self.room_a)
        self._folio(reservation)
        # No-Show 按 fail-closed 释放已售库存：日历缺行会整体拒绝，先补两晚。
        for offset in (0, 1):
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=self.room_type,
                stay_date=self.today + timedelta(days=offset),
                total_rooms=5,
                sold=1,
            )

        response = self._post(
            f"/api/v1/front-desk/reservation-rooms/{lines[0].pk}/no-show",
            {"reason": "客人未到店，电话确认取消"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        lines[0].refresh_from_db()
        self.assertIsNone(lines[0].assigned_room_id, "No-Show 必须释放已排房号")

        arrivals = self.client.get("/api/v1/front-desk/arrivals", {"date": self.today.isoformat()})
        self.assertEqual(arrivals.json()["data"], [])
        overview = self.client.get(
            "/api/v1/front-desk/overview", {"date": self.today.isoformat()}
        ).json()["data"]
        self.assertEqual(overview["arrivals_count"], 0)
        self.assertEqual(overview["unassigned_count"], 0, "No-Show 不得再计入待排房")

    # ------------------------------------------------------------------
    # 3. 合同应收 vs 已入账余额
    # ------------------------------------------------------------------
    def test_folio_summary_exposes_contract_receivable_before_room_charge(self):
        reservation, lines = self._reservation(room_count=1)
        self._assign(lines[0], self.room_a)
        folio = self._folio(reservation)
        # 只登记 30% 押金（贷方），房费尚未由夜审过租。
        LedgerEntry.objects.create(
            property=self.property,
            folio=folio,
            entry_type=LedgerEntry.EntryType.DEPOSIT,
            amount=Decimal("-180.00"),
            currency="CNY",
            business_date=self.today,
            reference=f"FDDF-DEP-{uuid.uuid4().hex[:6]}",
            description="前台登记押金",
            posted_by=self.user,
        )

        response = self.client.get("/api/v1/front-desk/arrivals", {"date": self.today.isoformat()})
        summary = response.json()["data"][0]["folio_summary"]
        self.assertEqual(summary["contract_total"], "600.00")
        self.assertEqual(summary["collected_amount"], "180.00")
        self.assertEqual(
            summary["contract_balance"],
            "420.00",
            "合同待收=合同房费−已实收，不能因房费未入账而归零",
        )
        self.assertEqual(summary["charges"], "0.00", "已入账口径保持不变")
        self.assertEqual(summary["balance"], "-180.00")

    def test_contract_fields_present_without_folio(self):
        reservation, _ = self._reservation(room_count=1)
        response = self.client.get(f"/api/v1/front-desk/reservations/{reservation.booking_no}")
        summary = response.json()["data"]["folio_summary"]
        self.assertEqual(summary["contract_total"], "600.00")
        self.assertEqual(summary["collected_amount"], "0.00")
        self.assertEqual(summary["contract_balance"], "600.00")

    # ------------------------------------------------------------------
    # 4. 手机号检索（在住 / 离店）
    # ------------------------------------------------------------------
    def test_in_house_and_departures_search_by_phone_hash(self):
        guest = GuestService.create_guest(
            property_obj=self.property, full_name="哈希检索住客", phone="13800001111"
        )
        reservation, lines = self._reservation(room_count=1, guest=guest)
        self._assign(lines[0], self.room_a)
        self._folio(reservation)
        Stay.objects.create(
            property=self.property,
            reservation_room=lines[0],
            room=self.room_a,
            actual_check_in=timezone.now() - timedelta(hours=3),
            status=Stay.Status.IN_HOUSE,
        )
        self.room_a.occupancy_status = Room.OccupancyStatus.OCCUPIED
        self.room_a.save(update_fields=("occupancy_status",))

        in_house = self.client.get("/api/v1/front-desk/in-house", {"q": "13800001111"})
        self.assertEqual(in_house.status_code, 200)
        self.assertEqual(
            [row["booking_no"] for row in in_house.json()["data"]],
            [reservation.booking_no],
            "在住列表必须能用手机号命中（明文列已停写，需走 phone_hash）",
        )
        departures = self.client.get(
            "/api/v1/front-desk/departures",
            {"date": (self.today + timedelta(days=2)).isoformat(), "q": "13800001111"},
        )
        self.assertEqual(
            [row["booking_no"] for row in departures.json()["data"]],
            [reservation.booking_no],
        )

    # ------------------------------------------------------------------
    # 5. 房间视图检索与清洁口径
    # ------------------------------------------------------------------
    def test_rooms_support_search_and_cleaning_filter(self):
        self.room_b.housekeeping_status = Room.HousekeepingStatus.DIRTY
        self.room_b.save(update_fields=("housekeeping_status",))

        by_number = self.client.get("/api/v1/front-desk/rooms", {"q": "DF102"})
        self.assertEqual([row["number"] for row in by_number.json()["data"]], ["DF102"])

        by_type = self.client.get("/api/v1/front-desk/rooms", {"q": "数据流转"})
        self.assertEqual(len(by_type.json()["data"]), 2, "房型名称也应可检索")

        cleaning = self.client.get("/api/v1/front-desk/rooms", {"readiness": "CLEANING"})
        self.assertEqual([row["number"] for row in cleaning.json()["data"]], ["DF102"])

        overview = self.client.get(
            "/api/v1/front-desk/overview", {"date": self.today.isoformat()}
        ).json()["data"]
        self.assertEqual(overview["cleaning_count"], 1)

    # ------------------------------------------------------------------
    # 6. 逾期未离
    # ------------------------------------------------------------------
    def test_overdue_departures_are_separated_from_due_today(self):
        reservation, lines = self._reservation(room_count=1)
        self._assign(lines[0], self.room_a)
        self._folio(reservation)
        lines[0].check_in = self.today - timedelta(days=3)
        lines[0].check_out = self.today - timedelta(days=1)
        lines[0].save(update_fields=("check_in", "check_out"))
        Stay.objects.create(
            property=self.property,
            reservation_room=lines[0],
            room=self.room_a,
            actual_check_in=timezone.now() - timedelta(days=3),
            status=Stay.Status.IN_HOUSE,
        )

        due_today = self.client.get(
            "/api/v1/front-desk/departures", {"date": self.today.isoformat()}
        )
        self.assertEqual(due_today.json()["data"], [])

        overdue = self.client.get(
            "/api/v1/front-desk/departures",
            {"date": self.today.isoformat(), "overdue": "1"},
        )
        self.assertEqual(
            [row["booking_no"] for row in overdue.json()["data"]], [reservation.booking_no]
        )
        overview = self.client.get(
            "/api/v1/front-desk/overview", {"date": self.today.isoformat()}
        ).json()["data"]
        self.assertEqual(overview["overdue_departures_count"], 1)
        self.assertEqual(overview["exception_counts"]["overdue_departures"], 1)
