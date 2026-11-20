"""V12-01 日租（day-use）房租收入闭环测试。

覆盖负责人要求的关键口径：
  * 当日入住当日退的合法计费；
  * 价格唯一来源 DayUsePolicy，缺失政策 fail-closed；
  * 入住即确认收入、重复入住/退房幂等、不重复计费；
  * 夜审不过租、不误判缺快照；
  * 超时费独立凭证；
  * 多收款只形成退款负债，不冲减房租收入；
  * 日租占用当日房量，防止同日超卖。
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from threading import Barrier, Thread
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.billing.models import DayUsePolicy, Folio, LedgerEntry, RefundLiability
from apps.billing.night_audit import NightAuditService, property_date
from apps.core.models import AuditEvent, BusinessDate
from apps.inventory.models import (
    DailyRate,
    RatePlan,
    Room,
    RoomType,
    RoomTypeInventory,
)
from apps.inventory.services import InventoryUnavailable
from apps.reservations.day_use import (
    DayUseError,
    convert_day_use_to_overnight,
    post_day_use_room_charge,
    post_overtime_charge,
    resolve_day_use_policy,
)
from apps.reservations.front_desk_services import (
    FrontDeskOperationError,
    check_in_stay,
    check_out_stay,
    collect_manual_payment,
)
from apps.reservations.models import Reservation, Stay
from apps.reservations.services import create_reservation
from apps.reservations.walkin import create_walk_in


class DayUseFixtureMixin:
    """物业 + 房型 + 可售日历 + 日租政策 + 净房 + 操作员。"""

    def setUp(self):
        from apps.core.models import Property

        self.property = Property.objects.create(code="DU", name="日租测试酒店")
        self.today = property_date(self.property)
        # GAP-DU-TIME 修复：超时计费以真实时钟对比政策结束时间（18:00+宽限），
        # 未冻结时钟时 19:00 后运行本套件会额外产生超时费导致 3 个退房类
        # 断言失败（时间依赖 flaky）。统一冻结在当日 12:00（物业时区，处于
        # 9:00-18:00 使用时段内），使套件任意时刻运行结果一致。
        frozen = timezone.make_aware(
            datetime.combine(self.today, time(12, 0)),
            ZoneInfo(self.property.timezone or "Asia/Shanghai"),
        )
        clock = mock.patch.object(timezone, "now", return_value=frozen)
        clock.start()
        self.addCleanup(clock.stop)  # type: ignore[attr-defined]  # 混入类与 TestCase 同用
        self.operator = User.objects.create_user(username="du-operator")
        # 现金押金/收款要求操作员先开立本人收银班次（BillingService 强制）。
        from apps.billing.services_v2 import BillingService

        BillingService.open_shift(
            property_obj=self.property,
            cashier=self.operator,
            opening_cash=Decimal("2000.00"),
        )
        self.room_type = RoomType.objects.create(
            property=self.property, code="DU-STD", name="日租标准房"
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="DU-BAR",
            name="日租门市价",
            valid_from=self.today - timedelta(days=30),
        )
        DailyRate.objects.create(
            property=self.property,
            rate_plan=self.rate_plan,
            room_type=self.room_type,
            stay_date=self.today,
            price=Decimal("400.00"),
        )
        RoomTypeInventory.objects.create(
            property=self.property,
            room_type=self.room_type,
            stay_date=self.today,
            total_rooms=2,
        )
        self.policy = DayUsePolicy.objects.create(
            property=self.property,
            room_type=self.room_type,
            day_use_price=Decimal("200.00"),
            start_time=time(9, 0),
            end_time=time(18, 0),
            grace_minutes=60,
            overtime_hourly_rate=Decimal("50.00"),
            valid_from=self.today - timedelta(days=30),
            valid_to=self.today + timedelta(days=30),
        )
        self.room = Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="DU-101",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )

    def _walk_in(self, key="du-1", phone="13900000001"):
        return create_walk_in(
            property_obj=self.property,
            full_name="日租客人",
            phone=phone,
            room_type_id=self.room_type.pk,
            rate_plan_id=self.rate_plan.pk,
            nights=0,
            adults=1,
            created_by=self.operator,
            idempotency_key=key,
            billing_type=Reservation.BillingType.DAY_USE,
        )

    def _room_charge_entries(self, folio):
        return LedgerEntry.objects.filter(folio=folio, entry_type=LedgerEntry.EntryType.ROOM_CHARGE)

    def _pay(self, reservation, amount, key):
        return collect_manual_payment(
            property_obj=self.property,
            booking_no=reservation.booking_no,
            amount=Decimal(amount),
            method="CASH",
            idempotency_key=key,
            received_by=self.operator,
        )


class DayUseCreationTests(DayUseFixtureMixin, TestCase):
    def test_day_use_creation_snapshots_policy_price(self):
        result = self._walk_in()
        reservation = result["reservation"]
        line = reservation.reservation_rooms.get()

        self.assertEqual(reservation.billing_type, Reservation.BillingType.DAY_USE)
        self.assertEqual(reservation.check_in, self.today)
        self.assertEqual(reservation.check_out, self.today)
        self.assertEqual(line.nightly_rates, [])
        self.assertEqual(line.day_use_price, Decimal("200.00"))
        self.assertEqual(line.amount, Decimal("200.00"))
        self.assertEqual(reservation.total_amount, Decimal("200.00"))

    def test_night_stay_with_equal_dates_is_still_rejected(self):
        payload = {
            "full_name": "过夜客人",
            "phone": "13900000002",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": self.today,
            "check_out": self.today,
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        with self.assertRaisesMessage(ValueError, "离店日期必须晚于入住日期"):
            create_reservation(
                property_obj=self.property,
                payload=payload,
                idempotency_key="night-equal-dates",
            )

    def test_day_use_with_unequal_dates_is_rejected(self):
        payload = {
            "full_name": "日租客人",
            "phone": "13900000003",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": self.today,
            "check_out": self.today + timedelta(days=1),
            "billing_type": Reservation.BillingType.DAY_USE,
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        with self.assertRaisesMessage(ValueError, "离店日期必须等于入住日期"):
            create_reservation(
                property_obj=self.property,
                payload=payload,
                idempotency_key="dayuse-unequal",
            )

    def test_missing_policy_fails_closed_instead_of_zero_price(self):
        self.policy.is_active = False
        self.policy.save(update_fields=("is_active",))
        with self.assertRaises(DayUseError):
            resolve_day_use_policy(
                property_obj=self.property,
                room_type=self.room_type,
                day=self.today,
            )

    def test_day_use_occupies_same_day_inventory(self):
        self._walk_in(key="du-inv", phone="13900000004")
        row = RoomTypeInventory.objects.get(
            property=self.property, room_type=self.room_type, stay_date=self.today
        )
        # 冻结→确认：held 归零、sold+1；否则同日可被重复售卖。
        self.assertEqual(row.held, 0)
        self.assertEqual(row.sold, 1)

    def test_stop_sell_blocks_day_use(self):
        RoomTypeInventory.objects.filter(
            property=self.property, room_type=self.room_type, stay_date=self.today
        ).update(stop_sell=True)
        with self.assertRaises((InventoryUnavailable, ValueError)):
            self._walk_in(key="du-stop", phone="13900000005")


class DayUseRevenueTests(DayUseFixtureMixin, TestCase):
    def test_room_charge_recognized_once_at_check_in(self):
        result = self._walk_in()
        stay = result["stay"]
        folio = Folio.objects.get(reservation=result["reservation"])
        entries = self._room_charge_entries(folio)

        self.assertEqual(entries.count(), 1)
        entry = entries.get()
        self.assertEqual(entry.reference, f"day-use:{stay.pk}")
        self.assertEqual(entry.amount, Decimal("200.00"))
        self.assertEqual(entry.business_date, self.today)
        self.assertEqual(entry.stay_id, stay.pk)

    def test_repeat_check_in_does_not_double_post(self):
        result = self._walk_in()
        line = result["reservation"].reservation_rooms.get()
        stay, created = check_in_stay(
            property_obj=self.property,
            reservation_room_id=line.pk,
            checked_in_by=self.operator,
        )
        self.assertFalse(created)
        self.assertEqual(stay.pk, result["stay"].pk)
        folio = Folio.objects.get(reservation=result["reservation"])
        self.assertEqual(self._room_charge_entries(folio).count(), 1)

    def test_check_out_requires_settlement_and_adds_no_second_room_charge(self):
        result = self._walk_in()
        reservation = result["reservation"]
        stay = result["stay"]
        folio = Folio.objects.get(reservation=reservation)

        with self.assertRaisesMessage(FrontDeskOperationError, "应收余额"):
            check_out_stay(
                property_obj=self.property,
                stay_id=stay.pk,
                checked_out_by=self.operator,
            )

        # 押金 30%（60 元）已收，补收 140 元后余额为 0。
        self._pay(reservation, "140.00", "du-pay-1")
        stay, changed = check_out_stay(
            property_obj=self.property, stay_id=stay.pk, checked_out_by=self.operator
        )
        self.assertTrue(changed)
        self.assertEqual(stay.status, Stay.Status.CHECKED_OUT)
        total = sum(
            (entry.amount for entry in self._room_charge_entries(folio)),
            Decimal("0.00"),
        )
        self.assertEqual(total, Decimal("200.00"))

    def test_repeat_check_out_is_idempotent(self):
        result = self._walk_in()
        reservation = result["reservation"]
        stay = result["stay"]
        self._pay(reservation, "140.00", "du-pay-2")
        check_out_stay(property_obj=self.property, stay_id=stay.pk, checked_out_by=self.operator)
        folio = Folio.objects.get(reservation=reservation)
        before = LedgerEntry.objects.filter(folio=folio).count()
        stay_again, changed = check_out_stay(
            property_obj=self.property, stay_id=stay.pk, checked_out_by=self.operator
        )
        self.assertFalse(changed)
        self.assertEqual(stay_again.status, Stay.Status.CHECKED_OUT)
        self.assertEqual(LedgerEntry.objects.filter(folio=folio).count(), before)

    def test_overpayment_becomes_liability_not_revenue_reversal(self):
        result = self._walk_in()
        reservation = result["reservation"]
        stay = result["stay"]
        folio = Folio.objects.get(reservation=reservation)

        # 收款接口按"不得超过当前应收"硬约束，多收款无法发生；负余额来自
        # 账务调减（补偿/减免）——结清后再调减 60 元，形成 -60 余额。
        self._pay(reservation, "140.00", "du-pay-3")
        from apps.billing.services_v2 import BillingService

        BillingService.post_entry(
            property_obj=self.property,
            folio=folio,
            stay=stay,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("-60.00"),
            currency=folio.currency,
            business_date=self.today,
            reference=f"du-comp:{stay.pk}",
            description="房价调减（测试）",
            posted_by=self.operator,
        )
        check_out_stay(property_obj=self.property, stay_id=stay.pk, checked_out_by=self.operator)

        liability = RefundLiability.objects.get(reservation=reservation)
        self.assertEqual(liability.amount, Decimal("60.00"))
        total = sum(
            (entry.amount for entry in self._room_charge_entries(folio)),
            Decimal("0.00"),
        )
        self.assertEqual(total, Decimal("200.00"))


class DayUseNightAuditTests(DayUseFixtureMixin, TestCase):
    def test_night_audit_preview_excludes_day_use_without_blockers(self):
        self._walk_in()
        preview = NightAuditService.preview(property_obj=self.property)
        codes = {item["code"] for item in preview["blockers"]}
        self.assertNotIn("NIGHTLY_RATE_MISSING", codes)
        self.assertEqual(preview["stay_count"], 0)
        self.assertEqual(preview["stays"], [])

    def test_night_audit_posts_no_extra_room_charge_for_day_use(self):
        result = self._walk_in()
        folio = Folio.objects.get(reservation=result["reservation"])
        before = self._room_charge_entries(folio).count()

        NightAuditService.run(property_obj=self.property, executed_by=self.operator)
        self.assertEqual(self._room_charge_entries(folio).count(), before)


class DayUseOvertimeTests(DayUseFixtureMixin, TestCase):
    def test_overtime_charge_posted_once_as_separate_adjustment(self):
        # 约定结束时间 00:30、无宽限：当前时间必然已超时。
        self.policy.start_time = time(0, 0)
        self.policy.end_time = time(0, 30)
        self.policy.grace_minutes = 0
        self.policy.save(update_fields=("start_time", "end_time", "grace_minutes"))
        result = self._walk_in(key="du-ot", phone="13900000006")
        stay = result["stay"]
        folio = Folio.objects.get(reservation=result["reservation"])

        from apps.reservations.day_use import (
            overtime_charge,
            post_overtime_charge,
            resolve_window_for_line,
        )

        line = stay.reservation_room
        policy, _start, planned_end = resolve_window_for_line(line=line, property_obj=self.property)
        now = timezone.now()
        _hours, expected = overtime_charge(policy=policy, planned_end=planned_end, actual_out=now)
        self.assertGreater(expected, Decimal("0.00"))

        post_overtime_charge(
            property_obj=self.property,
            stay=stay,
            folio=folio,
            actor=self.operator,
            business_date=self.today,
            actual_out=now,
        )
        # 重复结算：凭证号唯一 + 金额一致 → 幂等返回，不重复计费。
        post_overtime_charge(
            property_obj=self.property,
            stay=stay,
            folio=folio,
            actor=self.operator,
            business_date=self.today,
            actual_out=now,
        )

        overtime = LedgerEntry.objects.filter(
            folio=folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            reference=f"day-use-overtime:{stay.pk}",
        )
        self.assertEqual(overtime.count(), 1)
        self.assertEqual(overtime.get().amount, expected)
        # 原房费不被改写。
        self.assertEqual(self._room_charge_entries(folio).get().amount, Decimal("200.00"))


class DayUseConcurrencyTests(DayUseFixtureMixin, TransactionTestCase):
    """MySQL 权威并发：两个线程同时确认同一日租房费，只能落一条凭证。"""

    def test_concurrent_room_charge_confirmation_posts_once(self):
        if connection.vendor != "mysql":
            self.skipTest("并发凭证幂等以 MySQL 为权威环境（SQLite 无行锁语义）")

        from apps.inventory.services import confirm_inventory_for_reservation

        payload = {
            "full_name": "并发日租客人",
            "phone": "13900000007",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": self.today,
            "check_out": self.today,
            "billing_type": Reservation.BillingType.DAY_USE,
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        reservation, _created, _token = create_reservation(
            property_obj=self.property,
            payload=payload,
            idempotency_key="du-conc",
        )
        confirm_inventory_for_reservation(reservation)
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=("status", "updated_at"))
        line = reservation.reservation_rooms.get()
        stay = Stay.objects.create(
            property=self.property,
            reservation_room=line,
            room=self.room,
            status=Stay.Status.IN_HOUSE,
        )
        folio = Folio.objects.get(reservation=reservation)

        barrier = Barrier(2)
        errors = []

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                post_day_use_room_charge(
                    property_obj=self.property,
                    reservation=reservation,
                    stay=stay,
                    folio=folio,
                    actor=self.operator,
                    business_date=self.today,
                )
            except Exception as exc:  # pragma: no cover - 失败时输出真实原因
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(
            LedgerEntry.objects.filter(
                folio=folio,
                entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
                reference=f"day-use:{stay.pk}",
            ).count(),
            1,
        )


class DayUseWindowTests(DayUseFixtureMixin, TestCase):
    def test_policy_window_and_end_override(self):
        from apps.reservations.day_use import day_use_window

        start, end = day_use_window(policy=self.policy, day=self.today, property_obj=self.property)
        self.assertEqual(start.hour, 9)
        self.assertEqual(end.hour, 18)

        override = timezone.make_aware(
            datetime.combine(self.today, time(20, 0)),
            timezone.get_current_timezone(),
        )
        _start, end2 = day_use_window(
            policy=self.policy,
            day=self.today,
            property_obj=self.property,
            end_override=override,
        )
        self.assertEqual(timezone.localtime(end2).hour, 20)


class DayUseConvertTests(DayUseFixtureMixin, TestCase):
    """GAP-DU-11：day-use 转过夜住宿（负责人裁定 2.5 正式业务操作）。"""

    def _checked_in_day_use(self, key="du-conv", phone="13900000901"):
        result = self._walk_in(key=key, phone=phone)
        return result

    def test_convert_to_next_night_updates_dates_rates_type_and_audits(self):
        result = self._checked_in_day_use()
        reservation = result["reservation"]
        line = reservation.reservation_rooms.get()

        new_out = self.today + timedelta(days=1)
        line2, reservation2 = convert_day_use_to_overnight(
            property_obj=self.property,
            reservation_room_id=line.pk,
            new_check_out=new_out,
            actor=self.operator,
            reason="客人申请续住一夜（DU-11）",
        )
        reservation.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(reservation.billing_type, Reservation.BillingType.NIGHT_STAY)
        self.assertEqual(line.check_out, new_out)
        self.assertEqual(
            line.nightly_rates,
            [{"date": self.today.isoformat(), "price": "400.00"}],
        )
        self.assertEqual(line.amount, Decimal("400.00"))
        self.assertEqual(reservation.total_amount, Decimal("600.00"))
        # 账单不动：日租房费已入账，新增夜晚由夜审过租生成
        folio = Folio.objects.get(reservation=reservation)
        self.assertEqual(self._room_charge_entries(folio).count(), 1)
        self.assertEqual(self._room_charge_entries(folio).get().amount, Decimal("200.00"))
        self.assertIsNone(line.day_use_end_override)
        # 审计留痕
        self.assertTrue(
            AuditEvent.objects.filter(
                action="reservations.day_use_converted_to_overnight",
                entity_id=str(line.pk),
            ).exists()
        )

    def test_convert_beyond_one_night_acquires_added_inventory(self):
        tomorrow = self.today + timedelta(days=1)
        RoomTypeInventory.objects.create(
            property=self.property,
            room_type=self.room_type,
            stay_date=tomorrow,
            total_rooms=2,
        )
        DailyRate.objects.create(
            property=self.property,
            rate_plan=self.rate_plan,
            room_type=self.room_type,
            stay_date=tomorrow,
            price=Decimal("420.00"),
        )
        result = self._checked_in_day_use(key="du-conv2", phone="13900000902")
        line = result["reservation"].reservation_rooms.get()

        convert_day_use_to_overnight(
            property_obj=self.property,
            reservation_room_id=line.pk,
            new_check_out=self.today + timedelta(days=2),
            actor=self.operator,
            reason="连住两晚",
        )
        inv_tomorrow = RoomTypeInventory.objects.get(
            property=self.property, room_type=self.room_type, stay_date=tomorrow
        )
        self.assertEqual(inv_tomorrow.sold, 1)
        # 新占用日的 CONFIRMED 冻结明细已建（RSV-01）
        self.assertTrue(
            __import__("apps.reservations.models", fromlist=["InventoryHold"])
            .InventoryHold.objects.filter(
                property=self.property,
                room_type=self.room_type,
                stay_date=tomorrow,
                reservation_room=line,
                status="CONFIRMED",
            )
            .exists()
        )

    def test_convert_fails_closed_on_missing_nightly_rate(self):
        result = self._checked_in_day_use(key="du-conv3", phone="13900000903")
        line = result["reservation"].reservation_rooms.get()
        DailyRate.objects.all().delete()

        with self.assertRaisesMessage(DayUseError, "缺少"):
            convert_day_use_to_overnight(
                property_obj=self.property,
                reservation_room_id=line.pk,
                new_check_out=self.today + timedelta(days=1),
                actor=self.operator,
                reason="缺价格快照",
            )
        line.refresh_from_db()
        result["reservation"].refresh_from_db()
        self.assertEqual(result["reservation"].billing_type, Reservation.BillingType.DAY_USE)
        self.assertEqual(line.check_out, self.today)

    def test_convert_rejects_overnight_orders_blank_reason_and_past_dates(self):
        # 过夜订单不可转（裁定 2.4 反向禁止）
        payload = {
            "full_name": "过夜客人",
            "phone": "13900000904",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": self.today,
            "check_out": self.today + timedelta(days=1),
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        reservation, _created, _token = create_reservation(
            property_obj=self.property, payload=payload, idempotency_key="du-ovn-1"
        )
        line = reservation.reservation_rooms.get()
        with self.assertRaisesMessage(DayUseError, "仅 day-use"):
            convert_day_use_to_overnight(
                property_obj=self.property,
                reservation_room_id=line.pk,
                new_check_out=self.today + timedelta(days=2),
                actor=self.operator,
                reason="过夜订单",
            )
        # day-use 订单：理由必填 / 新离店日必须晚于今天
        result = self._checked_in_day_use(key="du-conv5", phone="13900000905")
        line5 = result["reservation"].reservation_rooms.get()
        with self.assertRaisesMessage(DayUseError, "原因必填"):
            convert_day_use_to_overnight(
                property_obj=self.property,
                reservation_room_id=line5.pk,
                new_check_out=self.today + timedelta(days=1),
                actor=self.operator,
                reason="  ",
            )
        with self.assertRaisesMessage(DayUseError, "晚于今天"):
            convert_day_use_to_overnight(
                property_obj=self.property,
                reservation_room_id=line5.pk,
                new_check_out=self.today,
                actor=self.operator,
                reason="同日",
            )

    def test_convert_after_partial_payment_and_full_settlement(self):
        """V13-06 §8.3：已部分收款/已结清状态下的转换——账务状态保持一致。"""
        # 部分收款（押金 60 之外再收 40）：转换后 Folio 保持 OPEN、凭证不变
        result = self._checked_in_day_use(key="du-conv6", phone="13900000907")
        reservation = result["reservation"]
        collect_manual_payment(
            property_obj=self.property,
            booking_no=reservation.booking_no,
            amount=Decimal("40.00"),
            method="CASH",
            idempotency_key="du-conv6-pay1",
            received_by=self.operator,
        )
        folio = Folio.objects.get(reservation=reservation)
        entries_before = LedgerEntry.objects.filter(folio=folio).count()
        line = reservation.reservation_rooms.get()
        convert_day_use_to_overnight(
            property_obj=self.property,
            reservation_room_id=line.pk,
            new_check_out=self.today + timedelta(days=1),
            actor=self.operator,
            reason="部分收款后转为过夜",
        )
        folio.refresh_from_db()
        self.assertEqual(folio.status, Folio.Status.OPEN)
        self.assertEqual(
            LedgerEntry.objects.filter(folio=folio).count(), entries_before
        )

        # 已结清（补齐余额至 0）后转换：同样保持账务事实不变
        Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="DU-102",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )
        result2 = self._checked_in_day_use(key="du-conv7", phone="13900000908")
        reservation2 = result2["reservation"]
        folio2 = Folio.objects.get(reservation=reservation2)
        balance = sum(
            (e.amount for e in LedgerEntry.objects.filter(folio=folio2)),
            Decimal("0.00"),
        )
        self.assertGreater(balance, 0, "结清前应存在正的应收余额")
        collect_manual_payment(
            property_obj=self.property,
            booking_no=reservation2.booking_no,
            amount=balance,
            method="CASH",
            idempotency_key="du-conv7-pay-full",
            received_by=self.operator,
        )
        line2 = reservation2.reservation_rooms.get()
        convert_day_use_to_overnight(
            property_obj=self.property,
            reservation_room_id=line2.pk,
            new_check_out=self.today + timedelta(days=1),
            actor=self.operator,
            reason="结清后申请续住",
        )
        folio2.refresh_from_db()
        self.assertEqual(folio2.status, Folio.Status.OPEN)
        self.assertEqual(
            sum((e.amount for e in LedgerEntry.objects.filter(folio=folio2)), Decimal("0.00")),
            Decimal("0.00"),
        )

    def test_convert_repeat_submission_fails_safe(self):
        """V13-06 §8.3：重复提交——第二次按类型守卫拒绝，原状态保持一致。"""
        result = self._checked_in_day_use(key="du-conv8", phone="13900000909")
        line = result["reservation"].reservation_rooms.get()
        first = convert_day_use_to_overnight(
            property_obj=self.property,
            reservation_room_id=line.pk,
            new_check_out=self.today + timedelta(days=1),
            actor=self.operator,
            reason="首次转换",
        )
        with self.assertRaisesMessage(DayUseError, "仅 day-use"):
            convert_day_use_to_overnight(
                property_obj=self.property,
                reservation_room_id=line.pk,
                new_check_out=self.today + timedelta(days=2),
                actor=self.operator,
                reason="重复提交",
            )
        line.refresh_from_db()
        self.assertEqual(line.check_out, self.today + timedelta(days=1))
        self.assertEqual(
            line.nightly_rates,
            [{"date": self.today.isoformat(), "price": "400.00"}],
        )
        self.assertEqual(first[1].total_amount, Decimal("600.00"))


class DayUseClosedDayGuardTests(DayUseFixtureMixin, TestCase):
    """GAP-DU-14：营业日关账守卫对 day-use 超时凭证同样生效。"""

    def test_day_use_overtime_rejected_on_closed_business_day(self):
        result = self._walk_in(key="du-closed", phone="13900000906")
        stay = result["stay"]
        folio = Folio.objects.get(reservation=result["reservation"])
        BusinessDate.objects.create(
            property=self.property,
            business_date=self.today,
            status=BusinessDate.Status.CLOSED,
        )
        late_out = timezone.make_aware(
            datetime.combine(self.today, time(21, 0)), timezone.get_current_timezone()
        )
        with self.assertRaisesMessage(ValueError, "已关账"):
            post_overtime_charge(
                property_obj=self.property,
                stay=stay,
                folio=folio,
                actual_out=late_out,
            )
        # 超时费是 ADJUSTMENT 凭证；关账后不得产生（押金/房费为既有事实）
        self.assertEqual(
            LedgerEntry.objects.filter(
                folio=folio, entry_type=LedgerEntry.EntryType.ADJUSTMENT
            ).count(),
            0,
        )
