"""WB-085（第 32 轮）：续住与排房/退房的真实 MySQL 并发验收。

TransactionTestCase + 双线程各自独立数据库连接 + Barrier 同步起跑，
skipUnless(connection.vendor == "mysql")（SQLite 下 select_for_update 为
no-op，无法构造真实行锁竞争；顺序语义由既有 SQLite 用例覆盖）。
断言一律针对最终态。

场景 1：续住 vs 排房——在住订单 A 续住延长到与预订 B 已确认区间重叠，
  前台同时把同一物理房间分配给 B。两个事务对同一 Room 行都做了
  select_for_update 且锁序一致（明细 → 预订 → 房间），无论哪个先提交，
  最多只有一个成功，绝不产生重叠的有效房间占用。房间"可售"状态取
  VACANT+CLEAN，模拟房态更新与在住操作之间的真实并发窗口（两个事务
  分别判断房间可用正是本场景要验证的竞争条件）。
场景 2：续住 vs 退房——同一 Stay 并发续住/退房，锁序与业务校验保证
  终态一致（先退房则续住被拒；先续住则退房因应收余额被拒），无死锁。
"""

import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.billing.liabilities import register_refund_liability  # noqa: F401
from apps.billing.models import Folio, LedgerEntry
from apps.core.models import Property
from apps.guests.models import Guest
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory
from apps.reservations.front_desk_services import assign_room, check_out_stay
from apps.reservations.lifecycle_services import extend_stay
from apps.reservations.models import Reservation, ReservationRoom, Stay


def _join(test, threads, timeout=90):
    for thread in threads:
        thread.join(timeout=timeout)
        test.assertFalse(thread.is_alive(), "工作线程超时未结束（疑似死锁）")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ReservationConcurrencyBase(TransactionTestCase):
    """公共夹具：自建物业，避免依赖 DEFAULT 物业存量数据。"""

    def setUp(self):
        self.property = Property.objects.create(
            code="WBRA", name="预订并发专项酒店", timezone="Asia/Shanghai"
        )
        self.today = timezone.localdate()
        self.operator = User.objects.create_user(username="wbra-operator")
        self.frontdesk = User.objects.create_user(username="wbra-frontdesk")
        self.guest = Guest.objects.create(property=self.property, full_name="WB85 并发客人")
        self.room_type = RoomType.objects.create(
            property=self.property, code="WBRA-STD", name="WB85 标准间", capacity=2
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="WBRA-BAR",
            name="WB85 门市价",
            valid_from=self.today - timedelta(days=2),
        )
        # 覆盖续住区间的每日房价（today ~ today+2，均不关房）
        for offset in range(-1, 3):
            DailyRate.objects.create(
                property=self.property,
                rate_plan=self.rate_plan,
                room_type=self.room_type,
                stay_date=self.today + timedelta(days=offset),
                price=Decimal("300.00"),
                currency="CNY",
            )
        # 覆盖续住区间的库存日历（可售充足）
        for offset in (1, 2):
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=self.room_type,
                stay_date=self.today + timedelta(days=offset),
                total_rooms=5,
            )

    def _make_inhouse(self, booking_no, *, check_in, nights):
        """在住订单：CHECKED_IN 预订 + 明细 + 主账单 + OCCUPIED 房 + Stay。"""
        check_out = check_in + timedelta(days=nights)
        reservation = Reservation.objects.create(
            property=self.property,
            booking_no=booking_no,
            guest=self.guest,
            status=Reservation.Status.CHECKED_IN,
            check_in=check_in,
            check_out=check_out,
            room_count=1,
            adult_count=1,
            total_amount=Decimal("300.00") * nights,
            currency="CNY",
        )
        nightly = [
            {
                "date": (check_in + timedelta(days=i)).isoformat(),
                "price": "300.00",
                "currency": "CNY",
            }
            for i in range(nights)
        ]
        line = ReservationRoom.objects.create(
            property=self.property,
            reservation=reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=check_in,
            check_out=check_out,
            nightly_rates=nightly,
            amount=Decimal("300.00") * nights,
        )
        folio = Folio.objects.create(
            property=self.property, reservation=reservation, guest=self.guest, currency="CNY"
        )
        room = Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number=f"WBRA-{booking_no[-4:]}",
            occupancy_status=Room.OccupancyStatus.OCCUPIED,
        )
        # 在住明细必须已排房（真实入住流程前置），冲突检查按 assigned_room 判定
        line.assigned_room = room
        line.save(update_fields=("assigned_room",))
        stay = Stay.objects.create(
            property=self.property, reservation_room=line, room=room, status=Stay.Status.IN_HOUSE
        )
        return reservation, line, folio, room, stay

    def _post(self, folio, stay, entry_type, amount, reference):
        LedgerEntry.objects.create(
            property=self.property,
            folio=folio,
            stay=stay,
            entry_type=entry_type,
            amount=Decimal(amount),
            currency="CNY",
            business_date=self.today,
            reference=reference,
        )


@unittest.skipUnless(
    connection.vendor == "mysql",
    "续住与排房/退房并发仲裁依赖 MySQL 行锁（select_for_update），SQLite 跳过。",
)
class ExtendStayVsAssignRoomConcurrencyTests(ReservationConcurrencyBase):
    """场景 1：续住 vs 排房（同一物理房间，区间重叠）。"""

    def test_extend_and_assign_race_admit_exactly_one_winner(self):
        # 订单 A：昨天入住、明天离店（原区间 [today-1, today+1)），
        # 续住到 today+3 将与订单 B 的 [today+1, today+3) 重叠。
        reservation_a, line_a, folio_a, room, stay = self._make_inhouse(
            "WBRA-A001", check_in=self.today - timedelta(days=1), nights=2
        )
        # 房态置 VACANT+CLEAN：模拟房态更新与在住操作之间的并发窗口，
        # 使排房事务的 is_sellable_now 与续住事务的重叠检查真正同时生效。
        room.occupancy_status = Room.OccupancyStatus.VACANT
        room.housekeeping_status = Room.HousekeepingStatus.CLEAN
        room.save(update_fields=("occupancy_status", "housekeeping_status"))
        # 订单 B：已确认，区间 [today+1, today+3)，未排房
        reservation_b = Reservation.objects.create(
            property=self.property,
            booking_no="WBRA-B002",
            guest=self.guest,
            status=Reservation.Status.CONFIRMED,
            check_in=self.today + timedelta(days=1),
            check_out=self.today + timedelta(days=3),
            room_count=1,
            adult_count=1,
            total_amount=Decimal("600.00"),
            currency="CNY",
        )
        line_b = ReservationRoom.objects.create(
            property=self.property,
            reservation=reservation_b,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=self.today + timedelta(days=1),
            check_out=self.today + timedelta(days=3),
            nightly_rates=[
                {
                    "date": (self.today + timedelta(days=i)).isoformat(),
                    "price": "300.00",
                    "currency": "CNY",
                }
                for i in (1, 2)
            ],
            amount=Decimal("600.00"),
        )

        barrier = Barrier(2)
        results = {
            "extend_ok": False,
            "assign_ok": False,
            "errors": [],
            "extend_error": "",
            "assign_error": "",
        }

        def extend_worker():
            close_old_connections()
            try:
                prop_pk = self.property.pk
                stay_pk = stay.pk
                new_out = self.today + timedelta(days=3)
                barrier.wait(timeout=15)
                try:
                    extend_stay(
                        property_obj=Property.objects.get(pk=prop_pk),
                        stay_id=stay_pk,
                        new_check_out=new_out,
                        reason="WB85 并发续住",
                        extended_by=self.operator,
                    )
                    results["extend_ok"] = True
                except Exception as exc:
                    results["extend_error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        def assign_worker():
            close_old_connections()
            try:
                prop_pk = self.property.pk
                line_pk = line_b.pk
                room_pk = room.pk
                barrier.wait(timeout=15)
                try:
                    assign_room(
                        property_obj=Property.objects.get(pk=prop_pk),
                        reservation_room_id=line_pk,
                        room_id=room_pk,
                        assigned_by=self.frontdesk,
                    )
                    results["assign_ok"] = True
                except Exception as exc:
                    results["assign_error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=extend_worker),
            threading.Thread(target=assign_worker),
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        # 无意外异常、无死锁（join 超时已断言）
        self.assertEqual(results["errors"], [], f"并发出现未处理异常：{results['errors']}")
        # 恰有一个事务成功：续住成功则排房必须被拒，反之亦然
        self.assertTrue(
            results["extend_ok"] ^ results["assign_ok"], f"必须恰有一个事务成功：{results}"
        )
        line_a.refresh_from_db()
        line_b.refresh_from_db()
        stay.refresh_from_db()
        if results["extend_ok"]:
            self.assertIn("占用", results["assign_error"])
            self.assertEqual(line_a.check_out, self.today + timedelta(days=3))
            self.assertIsNone(line_b.assigned_room, "续住成功后排房必须被拒，B 不得占用同一房间")
        else:
            self.assertIn("占用", results["extend_error"])
            self.assertEqual(line_b.assigned_room_id, room.pk)
            self.assertEqual(
                line_a.check_out,
                self.today + timedelta(days=1),
                "排房成功后续住必须被拒，A 不得延入 B 的区间",
            )
        # 无重叠占用：两笔有效明细在同一房间上的区间不相交
        if line_b.assigned_room_id == room.pk:
            self.assertLessEqual(line_a.check_out, line_b.check_in)


@unittest.skipUnless(
    connection.vendor == "mysql",
    "续住与退房并发仲裁依赖 MySQL 行锁（select_for_update），SQLite 跳过。",
)
class ExtendStayVsCheckoutConcurrencyTests(ReservationConcurrencyBase):
    """场景 2：续住 vs 退房（同一 Stay）。

    两种合法串行化结果：
    * 退房先提交 → Stay 已非 IN_HOUSE，续住被拒，终态=按原区间退房；
    * 续住先提交 → 退房走「提前离店」结算（WB-086）：冲回未住晚的
      续住房费、释放剩余住宿日已售库存、余额两讫后退房。
    锁序统一（stay → 明细 → 预订 → 账单 → 房间），绝不死锁；断言
    最终态一致：账单无未结清应收、库存不残留已售占位、Stay 终态正确。
    """

    def test_extend_and_checkout_race_reaches_consistent_terminal_state(self):
        reservation_a, line_a, folio_a, room, stay = self._make_inhouse(
            "WBRA-C003", check_in=self.today - timedelta(days=1), nights=2
        )
        # 已住 1 晚房费 300 + 预收 300 → 余额 0，退房可两讫结清
        self._post(
            folio_a,
            stay,
            LedgerEntry.EntryType.ROOM_CHARGE,
            "300.00",
            f"stay-room-charge:{stay.pk}",
        )
        self._post(folio_a, stay, LedgerEntry.EntryType.PAYMENT, "-300.00", "WBRA-C003-PREPAY")

        barrier = Barrier(2)
        results = {
            "extend_ok": False,
            "checkout_ok": False,
            "errors": [],
            "extend_error": "",
            "checkout_error": "",
        }

        def extend_worker():
            close_old_connections()
            try:
                prop_pk = self.property.pk
                stay_pk = stay.pk
                new_out = self.today + timedelta(days=3)
                barrier.wait(timeout=15)
                try:
                    extend_stay(
                        property_obj=Property.objects.get(pk=prop_pk),
                        stay_id=stay_pk,
                        new_check_out=new_out,
                        reason="WB85 并发续住vs退房",
                        extended_by=self.operator,
                    )
                    results["extend_ok"] = True
                except Exception as exc:
                    results["extend_error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        def checkout_worker():
            close_old_connections()
            try:
                prop_pk = self.property.pk
                stay_pk = stay.pk
                barrier.wait(timeout=15)
                try:
                    check_out_stay(
                        property_obj=Property.objects.get(pk=prop_pk),
                        stay_id=stay_pk,
                        checked_out_by=self.frontdesk,
                    )
                    results["checkout_ok"] = True
                except Exception as exc:
                    results["checkout_error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=extend_worker),
            threading.Thread(target=checkout_worker),
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        self.assertEqual(results["errors"], [], f"并发出现未处理异常：{results['errors']}")
        stay.refresh_from_db()
        line_a.refresh_from_db()
        reservation_a.refresh_from_db()
        if results["checkout_ok"] and not results["extend_ok"]:
            # 先退房：续住必须被拒（Stay 已非 IN_HOUSE），终态=已退房
            self.assertIn("在住", results["extend_error"])
            self.assertEqual(stay.status, Stay.Status.CHECKED_OUT)
            self.assertEqual(line_a.check_out, self.today + timedelta(days=1))
        elif results["extend_ok"] and results["checkout_ok"]:
            # 先续住后离店：合法业务链路——退房走「提前离店」结算，
            # 冲回未住晚的续住房费、释放剩余库存，余额两讫后退房。
            # 绝不允许的坏终态是：续住成功且账单留有未结清应收。
            self.assertEqual(stay.status, Stay.Status.CHECKED_OUT)
            self.assertEqual(reservation_a.status, Reservation.Status.CHECKED_OUT)
            self.assertEqual(
                line_a.check_out,
                self.today + timedelta(days=3),
                "续住延长的事实必须保留（冲回不删事实）",
            )
            balance = sum(
                LedgerEntry.objects.filter(folio=folio_a).values_list("amount", flat=True),
                start=Decimal("0.00"),
            )
            self.assertLessEqual(balance, Decimal("0.00"), "退房后账单不得留有未结清应收")
            # 剩余住宿日库存已释放（续住 +sold 后被早离店回冲）
            from apps.inventory.models import RoomTypeInventory

            for offset in (1, 2):
                row = RoomTypeInventory.objects.get(
                    property=self.property,
                    room_type=self.room_type,
                    stay_date=self.today + timedelta(days=offset),
                )
                self.assertEqual(row.sold, 0, f"{row.stay_date} 已售库存必须随早离店释放")
        else:
            self.fail(f"出现意外的互斥失败组合：{results}")
        # 不存在的坏终态：续住成功但 Stay 仍在住（退房被余额卡住且未恢复）
        if results["extend_ok"]:
            self.assertIn(stay.status, (Stay.Status.CHECKED_OUT, Stay.Status.IN_HOUSE))
