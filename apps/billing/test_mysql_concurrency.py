"""收尾专项：真实 MySQL 并发测试（billing 侧，收尾任务一 场景 1/5/6/7/8）。

全部用例 TransactionTestCase + 双（多）线程各自独立数据库连接，
skipUnless(connection.vendor == "mysql")（SQLite 下 select_for_update 为
no-op，无法构造真实行锁竞争，测试环境跳过；SQLite 回归由主会话执行）。
断言一律针对最终态（不是线程内瞬时值）。

场景索引（对应收尾专项任务一）：
  * 场景 1：补偿任务双 worker——lease_compensation_task 同一任务恰一个成功；
    未租到者 complete 被拒（WB-067）；同一租约重复 complete 只生效一次
    （终态不得被复活）；
  * 场景 5：现金退款 vs 关班——统一 Shift → Payment → Refund 锁序下，
    终态现金非负且业务一致（退款成功+关班 或 关班成功+退款被拒）；
  * 场景 6：夜审 vs 退房多房订单——两间房一间退一间在住，并发夜审，
    每间按实际入住晚数只收一次房费（WB-109 多房分支）；
  * 场景 7：PendingBookingGate——同手机号 5 并发创建、上限 2，恰 2 笔
    PENDING（WB-106，补充既有 2 并发上限 1 用例的多线程口径）；
  * 场景 8：同一退款并发执行——两线程 execute_refund 同一已批准退款，
    恰一笔 SUCCEEDED、一张 REFUND 凭证、负债只回减一次（WB-091-旧）。
"""
import threading
import traceback
import unittest
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.billing.liabilities import register_refund_liability
from apps.billing.models import (
    CashierShift,
    Folio,
    LedgerEntry,
    Payment,
    PaymentCompensationTask,
    Refund,
    RefundLiability,
)
from apps.billing.night_audit import NightAuditService, property_date
from apps.billing.payment_intents import (
    complete_compensation_task,
    create_compensation_task,
    lease_compensation_task,
)
from apps.billing.services_v2 import BillingService
from apps.core.models import BusinessDate, NightAuditRun, Property
from apps.guests.models import Guest
from apps.guests.services import phone_hash
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory
from apps.reservations.front_desk_services import check_out_stay
from apps.reservations.models import PendingBookingGate, Reservation, ReservationRoom, Stay
from apps.reservations.services import ReservationConflict, create_reservation


def _join(test, threads, timeout=90):
    for thread in threads:
        thread.join(timeout=timeout)
        test.assertFalse(thread.is_alive(), "工作线程超时未结束（疑似死锁）")


class BillingConcurrencyBase(TransactionTestCase):
    """公共夹具：自建物业/员工账号，避免依赖 DEFAULT 物业存量数据。"""

    def setUp(self):
        self.property = Property.objects.create(
            code="MWBC", name="MySQL 并发专项酒店", timezone="Asia/Shanghai")
        self.today = property_date(self.property)
        self.operator = User.objects.create_user(username="mwbc-operator")
        self.finance = User.objects.create_user(username="mwbc-finance")
        self.duty = User.objects.create_user(username="mwbc-duty")
        self.executor = User.objects.create_user(username="mwbc-executor")
        self.cashier = User.objects.create_user(username="mwbc-cashier")
        self.guest = Guest.objects.create(
            property=self.property, full_name="并发专项客人")

    def _make_reservation(self, booking_no, *, check_in=None, nights=2,
                          price="300.00", room_count=1, status=None):
        """创建预订 + 房型/房价/明细（不建房/Stay），返回 (reservation, lines)。"""
        check_in = check_in if check_in is not None else self.today
        check_out = check_in + timedelta(days=nights)
        room_type = RoomType.objects.create(
            property=self.property, code=f"MWBC-RT-{booking_no[-6:]}",
            name="并发专项房型", capacity=2)
        rate_plan = RatePlan.objects.create(
            property=self.property, code=f"MWBC-BAR-{booking_no[-6:]}",
            name="并发专项门市价", valid_from=check_in)
        reservation = Reservation.objects.create(
            property=self.property, booking_no=booking_no, guest=self.guest,
            status=status or Reservation.Status.CHECKED_IN,
            check_in=check_in, check_out=check_out,
            room_count=room_count, adult_count=1,
            total_amount=Decimal(price) * nights * room_count,
            currency="CNY")
        lines = []
        for index in range(room_count):
            nightly = [
                {"date": (check_in + timedelta(days=i)).isoformat(),
                 "price": price, "currency": "CNY"}
                for i in range(nights)
            ]
            lines.append(ReservationRoom.objects.create(
                property=self.property, reservation=reservation,
                room_type=room_type, rate_plan=rate_plan,
                check_in=check_in, check_out=check_out,
                nightly_rates=nightly,
                amount=Decimal(price) * nights))
        return reservation, lines

    def _make_folio(self, reservation):
        return Folio.objects.create(
            property=self.property, reservation=reservation,
            guest=self.guest, currency="CNY")


@unittest.skipUnless(
    connection.vendor == "mysql",
    "补偿任务租约并发依赖 MySQL 行锁（select_for_update 在 SQLite 下为 "
    "no-op），SQLite 测试环境跳过。")
class CompensationTaskLeaseConcurrencyTests(BillingConcurrencyBase):
    """场景 1：补偿任务双 worker 租约仲裁 + WB-067 重复收尾守卫。"""

    def test_two_workers_lease_same_task_exactly_one_wins(self):
        task, _created = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            aggregate_type="Payment", aggregate_id=self.property.pk,
            idempotency_key="mwbc-comp-task-001", max_attempts=5)

        barrier = Barrier(2)
        results = {"winner": None, "loser": None, "errors": []}

        def lease_worker(owner):
            close_old_connections()
            try:
                property_pk = self.property.pk
                task_pk = task.pk
                barrier.wait(timeout=15)
                try:
                    lease_compensation_task(task_pk, owner, lease_seconds=60)
                    results["winner"] = owner
                except ValueError as exc:
                    results["loser"] = owner
                    results["loser_message"] = str(exc)
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=lease_worker, args=(owner,))
            for owner in ("worker-A", "worker-B")
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        self.assertEqual(results["errors"], [])
        self.assertIsNotNone(results["winner"], "必须有恰一个 worker 抢占成功")
        self.assertIsNotNone(results["loser"], "另一个 worker 必须被拒")
        self.assertIn("抢占", results.get("loser_message", ""))

        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.LEASED)
        self.assertEqual(task.lease_owner, results["winner"])
        self.assertEqual(task.attempt_count, 1)

        # WB-067：未租到者（含伪造空租约）不得提交执行结果
        with self.assertRaises(ValueError) as ctx:
            complete_compensation_task(
                task.pk, success=True, lease_owner=results["loser"])
        self.assertIn("租约持有者不匹配", str(ctx.exception))
        with self.assertRaises(ValueError):
            complete_compensation_task(task.pk, success=True, lease_owner="")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.LEASED)

        # 胜者正常收尾 → SUCCEEDED
        completed = complete_compensation_task(
            task.pk, success=True, lease_owner=results["winner"])
        self.assertEqual(completed.status,
                         PaymentCompensationTask.Status.SUCCEEDED)
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        completed_at = task.completed_at
        self.assertIsNotNone(completed_at)

        # 同一租约下重复 complete 只生效一次：成功重放被拒、失败回执更不得
        # 把终态复活成 PENDING（attempt_count 不再增长）
        with self.assertRaises(ValueError) as ctx:
            complete_compensation_task(
                task.pk, success=True, lease_owner=results["winner"])
        self.assertIn("重复提交", str(ctx.exception))
        with self.assertRaises(ValueError):
            complete_compensation_task(
                task.pk, success=False, error_code="LATE_REPLY",
                error_summary="迟到重复收尾", lease_owner=results["winner"])
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED)
        self.assertEqual(task.attempt_count, 1)
        self.assertEqual(task.completed_at, completed_at)
        self.assertEqual(
            PaymentCompensationTask.objects.filter(
                idempotency_key="mwbc-comp-task-001").count(), 1)


@unittest.skipUnless(
    connection.vendor == "mysql",
    "现金退款与关班并发依赖 MySQL 行锁（Shift → Payment → Refund 锁序），"
    "SQLite 测试环境跳过。")
class CashRefundVersusCloseShiftConcurrencyTests(BillingConcurrencyBase):
    """场景 5：现金退款 100 vs 关班并发——终态现金非负且业务一致。"""

    def test_cash_refund_and_close_shift_serialize_consistently(self):
        reservation, lines = self._make_reservation(
            "MWBCRF001", check_in=self.today - timedelta(days=1), nights=1,
            price="100.00")
        folio = self._make_folio(reservation)
        # 开班备用金 100，收一笔现金 100 → 关班前班次现金事实 = 200
        shift, created = BillingService.open_shift(
            property_obj=self.property, cashier=self.cashier,
            opening_cash=Decimal("100.00"))
        self.assertTrue(created)
        payment = BillingService.collect_payment(
            property_obj=self.property, reservation=reservation,
            amount="100.00", method="CASH", collected_by=self.cashier,
            reference="MWBC-CASH-PAY-001")
        self.assertEqual(payment.cashier_shift_id, shift.pk)
        # 退款三权分立：finance 申请 / duty 审批 / cashier（班次持有人）执行
        refund, created = BillingService.request_refund(
            property_obj=self.property, payment_id=payment.pk,
            amount="100.00", reason="并发专项：现金退款与关班竞争",
            requested_by=self.finance, idempotency_key="mwbc-refund-req-001")
        self.assertTrue(created)
        BillingService.approve_refund(
            property_obj=self.property, refund_id=refund.pk,
            approved_by=self.duty)

        barrier = Barrier(2)
        results = {"errors": [], "refund_error": None, "shift_error": None,
                   "refund_done": False, "shift_done": False}

        def refund_worker():
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                try:
                    done, _changed = BillingService.execute_refund(
                        property_obj=self.property, refund_id=refund.pk,
                        executed_by=self.cashier, confirm_refund_paid=True,
                        idempotency_key="mwbc-refund-exec-001")
                    results["refund_done"] = bool(done)
                except ValueError as exc:
                    results["refund_error"] = str(exc)
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        def close_worker():
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                try:
                    # actual=100：退款先提交时班次应有现金 100（差额 0）；
                    # 关班先提交时应有现金 200（差异 -100，需差异说明）。
                    BillingService.close_shift(
                        property_obj=self.property, shift_id=shift.pk,
                        actual_cash=Decimal("100.00"),
                        closed_by=self.cashier,
                        variance_reason="并发专项：退款与关班竞争差异说明")
                    results["shift_done"] = True
                except ValueError as exc:
                    results["shift_error"] = str(exc)
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=refund_worker),
            threading.Thread(target=close_worker),
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        self.assertEqual(results["errors"], [],
                         f"并发执行出现意外异常: {results['errors']}")
        self.assertIsNone(results["shift_error"],
                          f"关班不应失败: {results['shift_error']}")

        shift.refresh_from_db()
        refund.refresh_from_db()
        payment.refresh_from_db()
        # 终态不变量①：班次必然关账成功，且现金事实非负、口径自洽
        self.assertEqual(shift.status, CashierShift.ShiftStatus.CLOSED)
        self.assertIsNotNone(shift.expected_cash)
        self.assertIsNotNone(shift.actual_cash)
        self.assertGreaterEqual(shift.expected_cash, Decimal("0.00"))
        self.assertGreaterEqual(shift.actual_cash, Decimal("0.00"))
        self.assertEqual(shift.variance, shift.actual_cash - shift.expected_cash)
        # 关班时点锁定的事实与最终账务事实一致（此后再无写入）
        from django.db import transaction as db_transaction
        with db_transaction.atomic():
            self.assertEqual(
                shift.expected_cash,
                BillingService.cash_shift_expected_cash(shift,
                                                        lock_facts=True))

        refund_succeeded = refund.status == Refund.Status.SUCCEEDED
        if refund_succeeded:
            # 终态不变量②A：退款成功 + 关班成功——现金扣减进班次事实
            self.assertTrue(results["refund_done"])
            self.assertIsNone(results["refund_error"])
            self.assertEqual(
                LedgerEntry.objects.filter(
                    entry_type=LedgerEntry.EntryType.REFUND,
                    reference=f"refund:{refund.provider_ref}").count(), 1)
            self.assertIn(payment.status,
                          (Payment.Status.PARTIALLY_REFUNDED,
                           Payment.Status.REFUNDED))
            self.assertLessEqual(shift.expected_cash, Decimal("200.00"))
        else:
            # 终态不变量②B：关班成功 + 退款被拒（班次已关，无执行人 OPEN 班次）
            self.assertIsNotNone(results["refund_error"])
            self.assertIn("班次", results["refund_error"])
            self.assertEqual(refund.status, Refund.Status.APPROVED)
            self.assertFalse(LedgerEntry.objects.filter(
                entry_type=LedgerEntry.EntryType.REFUND,
                reference=f"refund:{refund.provider_ref}").exists())
            self.assertEqual(payment.status, Payment.Status.REFUND_PENDING)
            # 退款被拒时班次现金事实必须包含全额收款
            self.assertEqual(shift.expected_cash, Decimal("200.00"))


@unittest.skipUnless(
    connection.vendor == "mysql",
    "夜审与退房并发仲裁依赖 MySQL 行锁（select_for_update），SQLite 跳过"
    "（顺序语义已有 SQLite 用例覆盖）。")
class NightAuditMultiRoomCheckoutConcurrencyTests(BillingConcurrencyBase):
    """场景 6：夜审 vs 退房多房订单（WB-109 多房分支）。

    两间房共用一本主账单：一间在夜审并发窗口内退房、另一间保持在住。
    无论夜审/退房哪个先提交，串行化结果必须满足：
      * 每间按实际入住晚数（1 晚快照 300）只收一次房费；
      * 夜审只对锁定后仍 IN_HOUSE 的 Stay 过租；
      * 已退房间的"夜审过租凭证"与"退房补差凭证"互斥（差额口径）；
      * 账单最终两讫（预收 600 = 两间各 300）。
    """

    def _make_two_room_inhouse(self, booking_no):
        reservation, lines = self._make_reservation(
            booking_no, check_in=self.today - timedelta(days=1), nights=2,
            price="300.00", room_count=2)
        folio = self._make_folio(reservation)
        stays = []
        for index, line in enumerate(lines):
            room = Room.objects.create(
                property=self.property, room_type=line.room_type,
                number=f"MWBC-{booking_no[-4:]}-{index}",
                occupancy_status=Room.OccupancyStatus.OCCUPIED)
            stays.append(Stay.objects.create(
                property=self.property, reservation_room=line, room=room,
                status=Stay.Status.IN_HOUSE))
        # 预收 600（两间各 1 晚实际房费），全部链路结束时账单应两讫
        BillingService.post_entry(
            property_obj=self.property, folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-600.00"), currency="CNY",
            business_date=self.today, reference=f"{booking_no}-PREPAY",
            posted_by=self.operator)
        return reservation, lines, folio, stays

    def _charges_for_stay(self, folio, stay):
        """归集"本间已入账房费"（stay-room-charge 与 night-audit 同口径）。"""
        entries = LedgerEntry.objects.filter(
            folio=folio, entry_type=LedgerEntry.EntryType.ROOM_CHARGE)
        return sum(
            (entry.amount for entry in entries
             if entry.reference == f"stay-room-charge:{stay.pk}"
             or entry.reference.endswith(f":stay:{stay.pk}")),
            start=Decimal("0.00"))

    def test_multi_room_night_audit_only_posts_in_house_stay(self):
        reservation, lines, folio, stays = self._make_two_room_inhouse(
            "MWBCNA001")
        stay_out, stay_in = stays[0], stays[1]

        barrier = Barrier(2)
        results = {"errors": [], "run": None, "checkout": None}

        def audit_worker():
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                run, created = NightAuditService.run(
                    property_obj=self.property,
                    executed_by=self.operator,
                    business_date=self.today)
                results["run"] = (str(run.status), created)
            except Exception:
                results["errors"].append(
                    f"audit-worker: {traceback.format_exc()}")
            finally:
                close_old_connections()

        def checkout_worker():
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                checked_out, changed = check_out_stay(
                    property_obj=self.property, stay_id=stay_out.pk,
                    checked_out_by=self.operator)
                results["checkout"] = (str(checked_out.status), changed)
            except Exception:
                results["errors"].append(
                    f"checkout-worker: {traceback.format_exc()}")
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=audit_worker),
            threading.Thread(target=checkout_worker),
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        self.assertEqual(results["errors"], [],
                         f"并发执行出现意外异常: {results['errors']}")
        self.assertEqual(results.get("checkout"), ("CHECKED_OUT", True),
                         "退房必须成功（差额口径下不因关账守卫被阻断）")
        self.assertIsNotNone(results.get("run"))
        run_status, run_created = results["run"]
        self.assertEqual(run_status, str(NightAuditRun.RunStatus.SUCCESS))
        self.assertTrue(run_created)

        stay_out.refresh_from_db()
        stay_in.refresh_from_db()
        self.assertEqual(stay_out.status, Stay.Status.CHECKED_OUT)
        self.assertEqual(stay_in.status, Stay.Status.IN_HOUSE)
        bd = BusinessDate.objects.get(
            property=self.property, business_date=self.today)
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

        # 不变量①：夜审只对在住间过租——在住间恰好一张夜审凭证 300，
        # 且永不产生退房补差凭证（它没有退房）
        night_in = LedgerEntry.objects.filter(
            folio=folio,
            reference=(f"night-audit:{self.property.pk}:"
                       f"{self.today.isoformat()}:stay:{stay_in.pk}"))
        self.assertEqual(night_in.count(), 1)
        self.assertEqual(night_in.get().amount, Decimal("300.00"))
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"stay-room-charge:{stay_in.pk}").exists())
        # 不变量②：已退房间只收一次 300——夜审过租与退房补差互斥（差额口径）
        night_out = LedgerEntry.objects.filter(
            reference=(f"night-audit:{self.property.pk}:"
                       f"{self.today.isoformat()}:stay:{stay_out.pk}"))
        checkout_out = LedgerEntry.objects.filter(
            reference=f"stay-room-charge:{stay_out.pk}")
        self.assertEqual(night_out.count() + checkout_out.count(), 1,
                         "已退房间必须在夜审过租与退房补差二选一，各 300")
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"checkout-adjust:{stay_out.pk}").exists())
        # 不变量③：账实两讫——预收 600 = 两间各 300 已入账房费
        total_room_charge = sum(
            (entry.amount for entry in folio.entries.filter(
                entry_type=LedgerEntry.EntryType.ROOM_CHARGE)),
            start=Decimal("0.00"))
        self.assertEqual(total_room_charge, Decimal("600.00"))
        self.assertEqual(self._charges_for_stay(folio, stay_out),
                         Decimal("300.00"))
        self.assertEqual(self._charges_for_stay(folio, stay_in),
                         Decimal("300.00"))
        balance = sum(
            (entry.amount for entry in folio.entries.all()),
            Decimal("0.00"))
        self.assertEqual(balance, Decimal("0.00"))


@unittest.skipUnless(
    connection.vendor == "mysql",
    "同手机号并发下单上限依赖 PendingBookingGate 行锁（MySQL），SQLite 跳过。")
@override_settings(PUBLIC_PENDING_RESERVATION_CAP=2)
class PendingGateFiveConcurrentCapTwoTests(BillingConcurrencyBase):
    """场景 7（WB-106 补充）：同手机号 5 并发创建、上限 2 → 恰 2 笔 PENDING。

    既有 PendingCapConcurrentCreationTests（apps/reservations/tests.py）
    已覆盖 2 并发 / 上限 1；本用例补充"5 并发 / 上限 2"多线程口径。
    """

    def test_five_concurrent_same_phone_creations_cap_two(self):
        room_type = RoomType.objects.create(
            property=self.property, code="MWBC-GATE", name="并发上限房型",
            capacity=2)
        rate_plan = RatePlan.objects.create(
            property=self.property, code="MWBC-GATE-BAR", name="并发上限门市价",
            valid_from=self.today)
        for offset in (0, 1):
            day = self.today + timedelta(days=offset)
            DailyRate.objects.create(
                property=self.property, rate_plan=rate_plan,
                room_type=room_type, stay_date=day, price=Decimal("300.00"))
            RoomTypeInventory.objects.create(
                property=self.property, room_type=room_type,
                stay_date=day, total_rooms=5)
        phone = "13877776666"
        payload = {
            "full_name": "并发上限五线程住客",
            "phone": phone,
            "room_type_id": room_type.pk,
            "rate_plan_id": rate_plan.pk,
            "check_in": self.today,
            "check_out": self.today + timedelta(days=2),
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        property_pk = self.property.pk
        worker_count = 5
        barrier = Barrier(worker_count)
        results = {"created": [], "conflicts": [], "errors": []}

        def create_worker(index):
            close_old_connections()
            try:
                from apps.core.models import Property as P
                property_obj = P.objects.get(pk=property_pk)
                barrier.wait(timeout=20)
                try:
                    create_reservation(
                        property_obj=property_obj, payload=payload,
                        idempotency_key=f"mwbc-gate-five-{index}")
                    results["created"].append(index)
                except ReservationConflict:
                    results["conflicts"].append(index)
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=create_worker, args=(i,))
            for i in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        self.assertEqual(results["errors"], [],
                         f"并发创建出现意外异常: {results['errors']}")
        self.assertEqual(len(results["created"]), 2,
                         "恰 2 笔通过上限创建成功")
        self.assertEqual(len(results["conflicts"]), 3,
                         "其余 3 笔必须收到 ReservationConflict")
        pending = Reservation.objects.filter(
            property_id=property_pk,
            guest__phone_hash=phone_hash(phone),
            status=Reservation.Status.PENDING)
        self.assertEqual(pending.count(), 2, "最终恰 2 笔 PENDING")
        self.assertEqual(
            PendingBookingGate.objects.filter(
                property_id=property_pk, gate_key=phone_hash(phone)).count(),
            1, "同（物业, phone_hash）只有一条锁行")


@unittest.skipUnless(
    connection.vendor == "mysql",
    "同一退款并发执行依赖 MySQL 行锁（Payment → Refund 锁序），SQLite 跳过。")
class SameRefundConcurrentExecutionTests(BillingConcurrencyBase):
    """场景 8：两线程 execute_refund 同一已批准退款 → 恰一笔成功。"""

    def test_same_refund_executed_concurrently_single_success(self):
        reservation, lines = self._make_reservation(
            "MWBCRD001", check_in=self.today, nights=1, price="600.00")
        folio = self._make_folio(reservation)
        payment = Payment.objects.create(
            property=self.property, reservation=reservation, folio=folio,
            provider="frontdesk_manual", provider_ref="MWBC-CARD-PAY-001",
            method="CARD", amount=Decimal("600.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        LedgerEntry.objects.create(
            property=self.property, folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-600.00"), currency="CNY",
            business_date=self.today, reference="payment:MWBC-CARD-PAY-001",
            description="并发专项预付")
        # 退房结账 + 负余额登记负债（卡支付 600，无房费 → 负余额 600）
        folio.status = Folio.Status.CLOSED
        folio.save(update_fields=("status",))
        liability, _liability_created = register_refund_liability(
            property_obj=self.property, reservation=reservation,
            folio=folio, amount="600.00", created_by=self.finance)
        self.assertEqual(liability.remaining_amount, Decimal("600.00"))

        refund, created = BillingService.request_refund(
            property_obj=self.property, payment_id=payment.pk,
            amount="200.00", reason="并发专项：同一退款并发执行",
            requested_by=self.finance, idempotency_key="mwbc-rfd-req-001",
            refund_liability_id=liability.pk)
        self.assertTrue(created)
        BillingService.approve_refund(
            property_obj=self.property, refund_id=refund.pk,
            approved_by=self.duty)

        worker_count = 2
        barrier = Barrier(worker_count)
        results = {"winners": [], "errors": [], "value_errors": []}

        def execute_worker(index):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                try:
                    done, _changed = BillingService.execute_refund(
                        property_obj=self.property, refund_id=refund.pk,
                        executed_by=self.executor, confirm_refund_paid=True,
                        execution_reference=f"BANK-MWBC-{index}",
                        idempotency_key=f"mwbc-rfd-exec-{index}")
                    results["winners"].append(index)
                except ValueError as exc:
                    results["value_errors"].append(str(exc))
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=execute_worker, args=(i,))
            for i in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        self.assertEqual(results["errors"], [],
                         f"并发执行出现意外异常: {results['errors']}")
        self.assertEqual(len(results["winners"]), 1,
                         "恰一个执行者成功")
        self.assertEqual(len(results["value_errors"]), 1,
                         "另一个执行者必须被幂等键/终态拒绝")
        self.assertIn("已经执行", results["value_errors"][0])

        refund.refresh_from_db()
        payment.refresh_from_db()
        liability.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.SUCCEEDED)
        self.assertEqual(refund.executed_by_id, self.executor.pk)
        self.assertEqual(
            LedgerEntry.objects.filter(
                entry_type=LedgerEntry.EntryType.REFUND,
                reference=f"refund:{refund.provider_ref}").count(), 1,
            "只允许一张 REFUND 凭证")
        # 负债只回减一次：600 − 200 = 400
        self.assertEqual(liability.remaining_amount, Decimal("400.00"))
        self.assertEqual(liability.status,
                         RefundLiability.Status.PARTIALLY_SETTLED)
        self.assertIsNone(liability.settled_at)
        # 支付状态按退款事实收敛（200/600 → PARTIALLY_REFUNDED）
        self.assertEqual(payment.status, Payment.Status.PARTIALLY_REFUNDED)
