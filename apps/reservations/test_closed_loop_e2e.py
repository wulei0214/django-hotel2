"""ARCH-12/13：单条预订「创建 → 入住 → 退房」全链路闭环测试。

与既有 test_e2e_flows.py 的差别：后者按能力分组（支付/退款/夜审各测一段），
本文件验证**同一预订对象**贯穿三阶段时的跨模型一致性——Reservation、
ReservationRoom、Stay、Room、Folio、LedgerEntry、HousekeepingTask 在同一
业务事实链上不脱节。任务书 ARCH-12 要求七条流程「可验证的状态机闭环」，
本测试固定其中最核心的「预约→入住→退房」主干。

边界：不覆盖支付渠道（mock 走既有用例）、不改变任何业务规则。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.billing.models import Folio, LedgerEntry
from apps.core.models import Property
from apps.guests.models import Guest
from apps.identity.models import (
    Group,
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.inventory.models import HousekeepingTask, RatePlan, Room, RoomType
from apps.reservations.front_desk_services import (
    assign_room,
    check_in_stay,
    check_out_stay,
    collect_manual_payment,
)
from apps.reservations.models import Reservation, ReservationRoom, Stay

# 测试账号口令运行时生成，源码不落可用凭据字面量（secret-scan 门禁）。
TEST_ACCOUNT_PASSWORD = get_random_string(32)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ReservationToCheckoutClosedLoopTests(TestCase):
    """一条预订从建单到退房，跨模型状态必须逐步一致。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.today = timezone.localdate()
        self.room_type = RoomType.objects.create(
            property=self.property, code="LOOP-K", name="闭环测试大床房"
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="LOOP-BAR",
            name="闭环测试价",
            valid_from=self.today - timedelta(days=2),
        )
        self.guest = Guest.objects.create(property=self.property, full_name="闭环测试住客")
        self.room = Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="LOOP01",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
        )
        self.operator = self._make_frontdesk_user()

    def _make_frontdesk_user(self):
        user = User.objects.create_user(username="loop-front", password=TEST_ACCOUNT_PASSWORD)
        profile = StaffProfile.objects.create(
            user=user,
            full_name="闭环前台",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property,
            employee_no="LOOP0001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=Group.objects.get(name="前台"),
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        return user

    def _make_reservation(self):
        reservation = Reservation.objects.create(
            property=self.property,
            guest=self.guest,
            booking_no="LOOP-2026-001",
            status=Reservation.Status.CONFIRMED,
            check_in=self.today - timedelta(days=1),
            check_out=self.today + timedelta(days=1),
            room_count=1,
            adult_count=1,
            total_amount=Decimal("600.00"),
            currency="CNY",
        )
        line = ReservationRoom.objects.create(
            property=self.property,
            reservation=reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=reservation.check_in,
            check_out=reservation.check_out,
            amount=Decimal("600.00"),
            nightly_rates=[
                {
                    "date": (reservation.check_in + timedelta(days=index)).isoformat(),
                    "price": "300.00",
                    "currency": "CNY",
                }
                for index in range(2)
            ],
        )
        Folio.objects.create(
            property=self.property,
            reservation=reservation,
            guest=self.guest,
            currency="CNY",
        )
        return reservation, line

    def test_full_loop_keeps_reservation_room_stay_folio_consistent(self):
        reservation, line = self._make_reservation()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertFalse(Stay.objects.filter(reservation_room=line).exists())

        # ---- 阶段一：排房 ----
        assign_room(
            property_obj=self.property,
            reservation_room_id=line.pk,
            room_id=self.room.pk,
            assigned_by=self.operator,
        )
        line.refresh_from_db()
        self.assertEqual(line.assigned_room_id, self.room.pk)

        # ---- 阶段二：入住 ----
        stay, created = check_in_stay(
            property_obj=self.property,
            reservation_room_id=line.pk,
            checked_in_by=self.operator,
        )
        self.assertTrue(created)
        reservation.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CHECKED_IN)
        self.assertEqual(stay.status, Stay.Status.IN_HOUSE)
        self.assertEqual(self.room.occupancy_status, Room.OccupancyStatus.OCCUPIED)

        # 重复入住被拒绝，不产生第二条 Stay
        _, created_again = check_in_stay(
            property_obj=self.property,
            reservation_room_id=line.pk,
            checked_in_by=self.operator,
        )
        self.assertFalse(created_again)
        self.assertEqual(Stay.objects.filter(reservation_room=line).count(), 1)

        # ---- 阶段三：付款 ----
        collect_manual_payment(
            property_obj=self.property,
            booking_no=reservation.booking_no,
            amount="600.00",
            method="CARD",
            idempotency_key="loop-payment-001",
            received_by=self.operator,
        )
        paid_entries = LedgerEntry.objects.filter(
            folio__reservation=reservation,
            entry_type=LedgerEntry.EntryType.PAYMENT,
        )
        self.assertEqual(paid_entries.count(), 1, "收款应只生成一条 PAYMENT 凭证")

        # ---- 阶段四：退房 ----
        checked_out, changed = check_out_stay(
            property_obj=self.property,
            stay_id=stay.pk,
            checked_out_by=self.operator,
        )
        self.assertTrue(changed)
        checked_out.refresh_from_db()
        reservation.refresh_from_db()
        self.room.refresh_from_db()
        folio = Folio.objects.get(reservation=reservation, folio_type=Folio.FolioType.MASTER)
        folio.refresh_from_db()

        self.assertEqual(checked_out.status, Stay.Status.CHECKED_OUT)
        self.assertEqual(reservation.status, Reservation.Status.CHECKED_OUT)
        self.assertEqual(self.room.occupancy_status, Room.OccupancyStatus.VACANT)
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        self.assertEqual(folio.status, Folio.Status.CLOSED)

        # 退房自动生成且只生成一条清洁任务
        tasks = HousekeepingTask.objects.filter(
            property=self.property,
            room=self.room,
            task_type=HousekeepingTask.TaskType.DEPARTURE,
        )
        self.assertEqual(tasks.count(), 1)
        departure_task = tasks.first()
        if departure_task is None:
            self.fail("退房后应生成一条 DEPARTURE 清洁任务")
        self.assertEqual(departure_task.status, HousekeepingTask.TaskStatus.PENDING)

        # ---- 阶段五：重复退房幂等，不重复结算 ----
        room_charge_before = LedgerEntry.objects.filter(
            folio=folio, entry_type=LedgerEntry.EntryType.ROOM_CHARGE
        ).count()
        _, changed_again = check_out_stay(
            property_obj=self.property,
            stay_id=stay.pk,
            checked_out_by=self.operator,
        )
        self.assertFalse(changed_again)
        self.assertEqual(
            LedgerEntry.objects.filter(
                folio=folio, entry_type=LedgerEntry.EntryType.ROOM_CHARGE
            ).count(),
            room_charge_before,
            "重复退房不得再生成房费凭证",
        )
        self.assertEqual(
            HousekeepingTask.objects.filter(
                property=self.property,
                room=self.room,
                task_type=HousekeepingTask.TaskType.DEPARTURE,
            ).count(),
            1,
            "重复退房不得重复生成清洁任务",
        )
