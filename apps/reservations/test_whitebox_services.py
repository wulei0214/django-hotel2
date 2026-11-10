# -*- coding: utf-8 -*-
"""白盒服务层单元测试（子智能体 1：reservations 前台/生命周期服务）。

覆盖 45-whitebox-services.md 登记缺陷的针对性回归：
  * WB-06 check_out_stay 账单选择未限定主账单（可能结错账单）；
  * WB-07 lifecycle._folio_for_update 未限定主账单（续住/换房门禁失真）；
  * WB-01 前台/生命周期 _money 对超大有限金额抛未捕获 InvalidOperation；
  * WB-074 Walk-in 占位号确定性/幂等重放/跨物业房间隔离；
  * WB-075 退房缺少逐晚房价快照时阻断自动结算。
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory
from apps.reservations.front_desk_services import (
    FrontDeskOperationError,
    assign_room,
    check_in_stay,
    check_out_stay,
)
from apps.reservations.lifecycle_services import (
    LifecycleOperationError,
    _money as lifecycle_money,
    extend_stay,
)
from apps.reservations.models import Reservation, ReservationRoom, Stay
from apps.reservations.walkin import create_walk_in


class FrontDeskFlowBase(TestCase):
    """最小前台链路数据：物业/房型/房价/库存/物理房。"""

    def setUp(self):
        self.today = timezone.localdate()
        self.property = Property.objects.create(code="WBR", name="白盒前台酒店")
        self.room_type = RoomType.objects.create(
            property=self.property, code="WBR-KING", name="白盒大床房", capacity=2)
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="WBR-BAR", name="白盒门市价",
            valid_from=self.today - timedelta(days=1))
        self.guest = Guest.objects.create(
            property=self.property, full_name="白盒住客",
            phone="13800006601", phone_normalized="13800006601")
        self.operator = User.objects.create_user(username="wbr-operator")
        self.room = Room.objects.create(
            property=self.property, room_type=self.room_type, number="WBR-8101",
            floor="8", occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL)

    def _make_confirmed_reservation(self, booking_no, amount="0.00", nights=1):
        reservation = Reservation.objects.create(
            property=self.property, booking_no=booking_no, guest=self.guest,
            status=Reservation.Status.CONFIRMED,
            check_in=self.today, check_out=self.today + timedelta(days=nights),
            room_count=1, adult_count=1, total_amount=Decimal(amount))
        line = ReservationRoom.objects.create(
            property=self.property, reservation=reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=self.today, check_out=self.today + timedelta(days=nights),
            nightly_rates=[{"date": self.today.isoformat(), "price": amount,
                            "currency": "CNY"}],
            amount=Decimal(amount))
        folio = Folio.objects.create(
            property=self.property, reservation=reservation, guest=self.guest,
            currency="CNY")
        return reservation, line, folio


class CheckOutFolioScopeTests(FrontDeskFlowBase):
    """WB-06：退房必须结算主账单，不能被更晚开立的非主账单劫持。"""

    def test_check_out_settles_master_folio(self):
        reservation, line, master_folio = self._make_confirmed_reservation(
            "WBRCO0001", amount="0.00")
        # 更晚开立的住客分账单（Meta ordering=-opened_at，会被 .first() 优先选中）
        guest_folio = Folio.objects.create(
            property=self.property, reservation=reservation, guest=self.guest,
            currency="CNY", folio_type=Folio.FolioType.GUEST)
        Folio.objects.filter(pk=guest_folio.pk).update(
            opened_at=timezone.now() + timedelta(seconds=10))

        assign_room(property_obj=self.property, reservation_room_id=line.pk,
                    room_id=self.room.pk, assigned_by=self.operator)
        stay, created = check_in_stay(
            property_obj=self.property, reservation_room_id=line.pk,
            checked_in_by=self.operator)
        self.assertTrue(created)

        stay, checkout_created = check_out_stay(
            property_obj=self.property, stay_id=stay.pk,
            checked_out_by=self.operator)
        self.assertTrue(checkout_created)

        master_folio.refresh_from_db()
        guest_folio.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(master_folio.status, Folio.Status.CLOSED)
        self.assertEqual(guest_folio.status, Folio.Status.OPEN)
        self.assertEqual(reservation.status, Reservation.Status.CHECKED_OUT)


class LifecycleFolioScopeTests(FrontDeskFlowBase):
    """WB-07：续住门禁必须校验主账单状态。"""

    def _make_in_house(self, booking_no, master_closed=False):
        reservation, line, master_folio = self._make_confirmed_reservation(
            booking_no, amount="300.00", nights=1)
        if master_closed:
            Folio.objects.filter(pk=master_folio.pk).update(
                status=Folio.Status.CLOSED, closed_at=timezone.now())
        # 更晚开立的住客分账单保持 OPEN（会被 .first() 优先选中）
        guest_folio = Folio.objects.create(
            property=self.property, reservation=reservation, guest=self.guest,
            currency="CNY", folio_type=Folio.FolioType.GUEST)
        Folio.objects.filter(pk=guest_folio.pk).update(
            opened_at=timezone.now() + timedelta(seconds=10))
        stay = Stay.objects.create(
            property=self.property, reservation_room=line, room=self.room,
            actual_check_in=timezone.now(), status=Stay.Status.IN_HOUSE)
        reservation.status = Reservation.Status.CHECKED_IN
        reservation.save(update_fields=("status", "updated_at"))
        return reservation, line, stay, master_folio

    def test_extend_stay_requires_open_master_folio(self):
        """WB-07：主账单已关闭时续住必须拒绝，即使存在 OPEN 的分账单。"""
        reservation, line, stay, master_folio = self._make_in_house(
            "WBREX0001", master_closed=True)
        new_check_out = line.check_out + timedelta(days=1)
        # 续住新增住宿日 = [line.check_out, new_check_out)，房价/库存须落在该日
        DailyRate.objects.create(
            property=self.property, rate_plan=self.rate_plan,
            room_type=self.room_type, stay_date=line.check_out,
            price=Decimal("300.00"))
        RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type,
            stay_date=line.check_out, total_rooms=5)
        with self.assertRaises(LifecycleOperationError):
            extend_stay(
                property_obj=self.property, stay_id=stay.pk,
                new_check_out=new_check_out, reason="白盒续住门禁测试",
                extended_by=self.operator)


class MoneyBoundaryTests(FrontDeskFlowBase):
    """WB-01：前台与生命周期金额工具对超大有限金额的统一拒绝。"""

    def test_lifecycle_money_rejects_unquantizable_huge_amount(self):
        with self.assertRaises(LifecycleOperationError):
            lifecycle_money("1e30")
        with self.assertRaises(LifecycleOperationError):
            lifecycle_money("NaN")
        self.assertEqual(lifecycle_money("12.34"), Decimal("12.34"))

    def test_collect_manual_payment_rejects_unquantizable_huge_amount(self):
        reservation, line, folio = self._make_confirmed_reservation(
            "WBRCM0001", amount="300.00")
        with self.assertRaises(FrontDeskOperationError):
            from apps.reservations.front_desk_services import collect_manual_payment
            collect_manual_payment(
                property_obj=self.property, booking_no=reservation.booking_no,
                amount="1e30", method="CASH", idempotency_key="wbr-cm-001",
                received_by=self.operator)
        self.assertFalse(LedgerEntry.objects.filter(folio=folio).exists())


class ExtendStayBoundaryTests(FrontDeskFlowBase):
    """边界补充：续住日期参数与原离店日期的边界行为。"""

    def test_extend_stay_same_date_is_noop(self):
        reservation, line, master_folio = self._make_confirmed_reservation(
            "WBREX0002", amount="300.00")
        stay = Stay.objects.create(
            property=self.property, reservation_room=line, room=self.room,
            actual_check_in=timezone.now(), status=Stay.Status.IN_HOUSE)
        reservation.status = Reservation.Status.CHECKED_IN
        reservation.save(update_fields=("status", "updated_at"))
        _, returned_line, extension = extend_stay(
            property_obj=self.property, stay_id=stay.pk,
            new_check_out=line.check_out, reason="同日续住空操作",
            extended_by=self.operator)
        self.assertEqual(extension, Decimal("0.00"))
        self.assertEqual(returned_line.pk, line.pk)

    def test_extend_stay_rejects_earlier_date(self):
        reservation, line, master_folio = self._make_confirmed_reservation(
            "WBREX0003", amount="300.00")
        stay = Stay.objects.create(
            property=self.property, reservation_room=line, room=self.room,
            actual_check_in=timezone.now(), status=Stay.Status.IN_HOUSE)
        reservation.status = Reservation.Status.CHECKED_IN
        reservation.save(update_fields=("status", "updated_at"))
        with self.assertRaises(LifecycleOperationError):
            extend_stay(
                property_obj=self.property, stay_id=stay.pk,
                new_check_out=line.check_out - timedelta(days=1),
                reason="白盒提前离店拒绝", extended_by=self.operator)


class WalkInDeterminismTests(FrontDeskFlowBase):
    """WB-074：Walk-in 占位号确定性、幂等重放与跨物业房间隔离。"""

    def _walkin_inventory(self):
        DailyRate.objects.create(
            property=self.property, rate_plan=self.rate_plan,
            room_type=self.room_type, stay_date=self.today,
            price=Decimal("300.00"))
        RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type,
            stay_date=self.today, total_rooms=3)

    def _walk_in(self, key, full_name="白盒散客", phone=""):
        return create_walk_in(
            property_obj=self.property, full_name=full_name, phone=phone,
            room_type_id=self.room_type.pk, rate_plan_id=self.rate_plan.pk,
            nights=1, created_by=self.operator, idempotency_key=key)

    def test_same_idempotency_key_replays_without_duplicate_records(self):
        self._walkin_inventory()
        key = "walkin-wb074-replay-001"
        first = self._walk_in(key)
        second = self._walk_in(key)

        self.assertEqual(second["reservation"].pk, first["reservation"].pk)
        self.assertEqual(second["stay"].pk, first["stay"].pk)
        self.assertEqual(second["room"].pk, first["room"].pk)
        self.assertEqual(second["booking_no"], first["booking_no"])
        # 幂等重放：不重复建 Guest/Reservation/Folio/Stay
        self.assertEqual(Guest.objects.filter(full_name="白盒散客").count(), 1)
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(Folio.objects.count(), 1)
        self.assertEqual(Stay.objects.count(), 1)
        # 占位号确定性：由物业 + 幂等键经 uuid5 派生，两次调用一致
        expected = "0000" + uuid.uuid5(
            uuid.NAMESPACE_URL, f"walkin:{self.property.pk}:{key}").hex[:8]
        guest = first["reservation"].guest
        self.assertEqual(guest.phone, expected)
        self.assertEqual(
            guest.phone_normalized,
            "".join(char for char in expected if char.isdigit()))
        # 重放无新增事实：walk_in_created 审计只落一次
        self.assertEqual(AuditEvent.objects.filter(
            action="reservation.walk_in_created",
            entity_id=str(first["reservation"].pk)).count(), 1)

    def test_walkin_room_search_is_scoped_to_current_property(self):
        # 另一物业存在被错误挂到本物业房型下的净房（数据完整性被破坏时
        # 的纵深防御场景），房号排序在主物业房间之前：
        # 未限定 property 的房间查询会跨物业占用他店客房。
        other = Property.objects.create(code="WBRO", name="白盒他物业酒店")
        other_room = Room.objects.create(
            property=other, room_type=self.room_type, number="AAA-0001",
            floor="1", occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL)
        self._walkin_inventory()

        result = self._walk_in("walkin-wb074-prop-001", phone="13900000001")

        self.assertEqual(result["room"].pk, self.room.pk)
        self.assertEqual(result["room"].property_id, self.property.pk)
        self.assertEqual(result["stay"].room_id, self.room.pk)
        other_room.refresh_from_db()
        self.assertEqual(other_room.occupancy_status,
                         Room.OccupancyStatus.VACANT)
        self.assertFalse(Stay.objects.filter(room=other_room).exists())


class CheckOutNightlyRatesGuardTests(FrontDeskFlowBase):
    """WB-075：退房缺少逐晚房价快照时阻断自动结算（缺数据分支）。

    语义边界：NONE 政策=合同全额的基线行为不变；本守卫只拦截
    nightly_rates 为空的"缺数据"分支（与夜审 NIGHTLY_RATE_MISSING
    critical 阻断同口径）。
    """

    def test_checkout_without_nightly_rates_snapshot_is_blocked(self):
        reservation, line, folio = self._make_confirmed_reservation(
            "WBRCO0002", amount="300.00")
        ReservationRoom.objects.filter(pk=line.pk).update(nightly_rates=[])
        line.refresh_from_db()
        assign_room(property_obj=self.property, reservation_room_id=line.pk,
                    room_id=self.room.pk, assigned_by=self.operator)
        stay, created = check_in_stay(
            property_obj=self.property, reservation_room_id=line.pk,
            checked_in_by=self.operator)
        self.assertTrue(created)

        with self.assertRaises(FrontDeskOperationError) as ctx:
            check_out_stay(property_obj=self.property, stay_id=stay.pk,
                           checked_out_by=self.operator)
        self.assertIn("缺少逐晚房价快照", str(ctx.exception))

        # 阻断不得有部分写入：stay 仍在住、无房费凭证、账单未关闭
        stay.refresh_from_db()
        self.assertEqual(stay.status, Stay.Status.IN_HOUSE)
        self.assertIsNone(stay.actual_check_out)
        self.assertFalse(LedgerEntry.objects.filter(folio=folio).exists())
        folio.refresh_from_db()
        self.assertEqual(folio.status, Folio.Status.OPEN)

    def test_checkout_with_nightly_rates_snapshot_still_settles_full_amount(self):
        """基线语义回归：有快照 + NONE 政策 → 仍按合同全额结算。"""
        reservation, line, folio = self._make_confirmed_reservation(
            "WBRCO0003", amount="300.00")
        assign_room(property_obj=self.property, reservation_room_id=line.pk,
                    room_id=self.room.pk, assigned_by=self.operator)
        stay, _ = check_in_stay(
            property_obj=self.property, reservation_room_id=line.pk,
            checked_in_by=self.operator)
        LedgerEntry.objects.create(
            property=self.property, folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-300.00"), currency="CNY",
            business_date=self.today, reference="WBR-PREPAID-075",
            description="预付房费")
        checked_out, changed = check_out_stay(
            property_obj=self.property, stay_id=stay.pk,
            checked_out_by=self.operator)
        self.assertTrue(changed)
        charge = LedgerEntry.objects.get(reference=f"stay-room-charge:{stay.pk}")
        self.assertEqual(charge.amount, Decimal("300.00"))
