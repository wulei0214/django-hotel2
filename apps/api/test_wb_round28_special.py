# -*- coding: utf-8 -*-
"""第 28 轮整改专项验收：接口 E2E + 真实 MySQL 并发 + 故障注入。

本套件不是「普通单元测试」的替代品，而是针对第二份审查报告里那些
SQLite 全绿会掩盖的生产问题：

  * FIN-01：`select_for_update()` 在 autocommit 下求值，MySQL 会抛
    TransactionManagementError → 接口 500；SQLite 静默忽略，测不出来。
  * WB-05a：累计押金上限必须靠 MySQL 行锁做当前读，并发两笔不能击穿。
  * RSV-01/02/03：InventoryHold 明细、扫描/执行同一重算口径、提前离店
    释放区间，三者必须能从明细独立重建 held/sold，禁止用空明细清零真实 held。
  * RSV-06：幂等契约对「视图正常返回的非 2xx」也必须落 FAILED 终态。
  * RSV-10：入参格式错误 400 与业务状态冲突 409 必须分流，不能一刀切。
  * WB-090 / MAINT-02：渠道 UNKNOWN 故障注入后补偿任务必须在独立事务
    存活；stale RUNNING 夜审必须可恢复并由生产命令调度。

真实并发用例一律 `TransactionTestCase` + 多线程独立连接 +
`@skipUnless(connection.vendor == "mysql")`，断言只针对最终态。
"""
import json
import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import close_old_connections, connection, connections
from django.db.transaction import TransactionManagementError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.billing.models import (
    CheckoutExceptionRelease, Folio, LedgerEntry, Payment,
    PaymentCompensationTask, PaymentIntent, ProviderTransaction,
)
from apps.billing.night_audit import recover_stale_night_audit_runs
from apps.core.models import (
    AuditEvent, BusinessDate, IdempotencyRecord, NightAuditRun, Property,
)
from apps.guests.models import Guest
from apps.identity.models import (
    PropertyStaffMembership, StaffProfile, StaffRoleAssignment,
)
from apps.inventory.models import (
    InventoryDiscrepancy, RatePlan, RoomType, RoomTypeInventory,
)
from apps.inventory.reconciliation import (
    _recompute_inventory_day, approve_inventory_fix, execute_inventory_fix,
    request_inventory_fix, scan_inventory_discrepancies,
)
from apps.inventory.services import (
    confirm_inventory_for_reservation, hold_inventory_for_reservation,
)
from apps.reservations.front_desk_services import (
    _release_sold_inventory_for_early_departure,
)
from apps.reservations.models import (
    InventoryHold, NoShowRecord, Reservation, ReservationRoom,
)


# ---------------------------------------------------------------------------
# A/B：接口 E2E —— 400/409 分流 + 幂等非 2xx 终态
# ---------------------------------------------------------------------------
@override_settings(DEBUG=True, DJANGO_ENV="test")
class Round28HttpContractTests(E2EFlowTestBase):
    """RSV-10 分类回归：格式错误 400、业务冲突 409，两条写入链路都要覆盖。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("r28_http_fd", "前台")

    def test_deposit_input_errors_are_400_but_cap_conflict_is_409(self):
        booking_no, _, _, _ = self._booking_checked_in()
        self.client.force_login(self.fd)
        opened = self._open_cashier_shift("r28-shift-deposit")
        self.assertIn(opened.status_code, (200, 201), opened.content)
        url = f"/api/v1/front-desk/reservations/{booking_no}/deposits"

        bad_amount = self._post_json(
            url, {"amount": "not-a-number", "method": "CASH"},
            idempotency_key="r28-dep-bad-amount")
        self.assertEqual(bad_amount.status_code, 400, bad_amount.content)
        self.assertEqual(bad_amount.json()["error"]["code"], "VALIDATION_ERROR")

        bad_method = self._post_json(
            url, {"amount": "180.00", "method": "WECHAT"},
            idempotency_key="r28-dep-bad-method")
        self.assertEqual(bad_method.status_code, 400, bad_method.content)
        self.assertEqual(bad_method.json()["error"]["code"], "VALIDATION_ERROR")

        reservation = Reservation.objects.get(booking_no=booking_no)
        self.assertEqual(reservation.deposit_amount, Decimal("0.00"))

        # 600 × 30% = 180/笔；第 4 笔 540+180=720 > 600 属于业务状态冲突。
        for index in range(1, 4):
            resp = self._post_json(
                url, {"amount": "180.00", "method": "CASH"},
                idempotency_key=f"r28-dep-{index}")
            self.assertEqual(resp.status_code, 201, resp.content)
        over = self._post_json(
            url, {"amount": "180.00", "method": "CASH"},
            idempotency_key="r28-dep-4")
        self.assertEqual(over.status_code, 409, over.content)
        self.assertEqual(over.json()["error"]["code"], "BUSINESS_CONFLICT")
        self.assertIn("累计押金", over.json()["error"]["message"])

        reservation.refresh_from_db()
        self.assertEqual(reservation.deposit_amount, Decimal("540.00"))
        self.assertEqual(Payment.objects.filter(
            reservation=reservation, provider="frontdesk_deposit").count(), 3)

    def test_collect_payment_input_error_is_400_but_overcharge_conflict_is_409(self):
        booking_no, _, _, _ = self._booking_with_outstanding()
        self.client.force_login(self.fd)
        opened = self._open_cashier_shift("r28-shift-payment")
        self.assertIn(opened.status_code, (200, 201), opened.content)
        url = f"/api/v1/front-desk/reservations/{booking_no}/payments"

        bad = self._post_json(
            url, {"amount": "12.345", "method": "CASH"},
            idempotency_key="r28-pay-bad")
        self.assertEqual(bad.status_code, 400, bad.content)
        self.assertEqual(bad.json()["error"]["code"], "VALIDATION_ERROR")

        over = self._post_json(
            url, {"amount": "99999.00", "method": "CASH"},
            idempotency_key="r28-pay-over")
        self.assertEqual(over.status_code, 409, over.content)
        self.assertEqual(over.json()["error"]["code"], "BUSINESS_CONFLICT")
        self.assertIn("应收余额", over.json()["error"]["message"])


@override_settings(DEBUG=True, DJANGO_ENV="test")
class Round28IdempotencyTerminalTests(TestCase):
    """RSV-06：视图正常返回的 4xx 必须落 FAILED 终态，重试不得被 425 掩盖。"""

    def test_non_2xx_response_lands_failed_terminal_and_retry_is_not_425(self):
        url = "/api/v1/front-desk/refunds/1/reject"
        body = json.dumps({"reason": "未提供员工身份（专项断言）"})
        key = "r28-idem-401"

        first = self.client.post(
            url, data=body, content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(first.status_code, 401, first.content)

        record = IdempotencyRecord.objects.get(key=key)
        self.assertEqual(record.status, IdempotencyRecord.RecordStatus.FAILED)
        self.assertEqual(record.response_status, 401)
        self.assertIsInstance(record.response_body, dict)

        second = self.client.post(
            url, data=body, content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=key)
        self.assertNotEqual(second.status_code, 425, second.content)
        self.assertEqual(second.status_code, 401, second.content)
        record.refresh_from_db()
        self.assertEqual(record.status, IdempotencyRecord.RecordStatus.FAILED)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class Round28ExportEndpointSmokeTests(TestCase):
    """MAINT-01：导出端点必须真能返回 CSV，不能因被注释吞掉的 import 报 500。"""

    def test_export_module_returns_csv_and_records_audit(self):
        from apps.identity.test_module_gates import _grant, _make_user

        owner = _make_user("r28-export-owner", "店长")
        _grant("店长", "export_pii", "view_room")
        self.client.force_login(owner)
        response = self.client.get("/export/room/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertIn("text/csv", response["Content-Type"])
        self.assertTrue(AuditEvent.objects.filter(
            action="export.module_csv", entity_id="room").exists(),
            "函数名前段的 AuditEvent 引用必须可解析（旧代码必 UnboundLocalError）")
        self.assertTrue(AuditEvent.objects.filter(
            action="legacy.export", entity_id="room").exists())


# ---------------------------------------------------------------------------
# E：RSV-01/02/03 库存明细与对账口径（含故障注入）
# ---------------------------------------------------------------------------
@override_settings(DEBUG=True, DJANGO_ENV="test")
class Round28InventoryClosureTests(TestCase):
    """持仓明细 ↔ 权威计数 ↔ 对账重算必须三者一致，且禁止空明细清零。"""

    def setUp(self):
        self.prop = Property.objects.create(
            code="R28INV", name="第28轮库存专项酒店", timezone="Asia/Shanghai")
        self.rt = RoomType.objects.create(
            property=self.prop, code="R28-RT", name="专项房型", capacity=2)
        self.rate_plan = RatePlan.objects.create(
            property=self.prop, code="R28-BAR", name="专项门市价",
            valid_from=timezone.localdate() - timedelta(days=1))
        self.guest = Guest.objects.create(
            property=self.prop, full_name="库存专项住客")
        self.days = [timezone.localdate() + timedelta(days=i) for i in range(3)]
        for day in self.days:
            RoomTypeInventory.objects.create(
                property=self.prop, room_type=self.rt, stay_date=day,
                total_rooms=5)
        self.front = User.objects.create_user(username="r28-inv-front")
        self.duty = User.objects.create_user(username="r28-inv-duty")

    def _make_reservation(self, *, room_count=2, status=Reservation.Status.PENDING,
                          booking_no="R28INV-001"):
        reservation = Reservation.objects.create(
            property=self.prop, guest=self.guest, booking_no=booking_no,
            status=status, check_in=self.days[0],
            check_out=self.days[0] + timedelta(days=3),
            room_count=room_count, adult_count=1,
            total_amount=Decimal("300.00") * 3 * room_count, currency="CNY")
        lines = []
        for _ in range(room_count):
            lines.append(ReservationRoom.objects.create(
                property=self.prop, reservation=reservation,
                room_type=self.rt, rate_plan=self.rate_plan,
                check_in=self.days[0],
                check_out=self.days[0] + timedelta(days=3),
                amount=Decimal("900.00")))
        return reservation, lines

    def _row(self, day):
        return RoomTypeInventory.objects.get(
            property=self.prop, room_type=self.rt, stay_date=day)

    def test_hold_confirm_writes_details_and_recompute_matches_counters(self):
        reservation, lines = self._make_reservation()
        self.assertEqual(len(lines), 2)

        hold_inventory_for_reservation(reservation)
        self.assertEqual(InventoryHold.objects.filter(
            property=self.prop, status=InventoryHold.HoldStatus.HELD).count(), 6)
        for day in self.days:
            row = self._row(day)
            self.assertEqual(row.held, 2)
            self.assertEqual(row.sold, 0)
            # PENDING 不计入 sold；held 真值来自逐日逐单明细。
            self.assertEqual(
                _recompute_inventory_day(
                    property_obj=self.prop, room_type=self.rt, stay_date=day),
                (2, 0))

        self.assertTrue(confirm_inventory_for_reservation(reservation))
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=("status", "updated_at"))

        self.assertEqual(InventoryHold.objects.filter(
            property=self.prop,
            status=InventoryHold.HoldStatus.CONFIRMED).count(), 6)
        for day in self.days:
            row = self._row(day)
            self.assertEqual(row.held, 0)
            self.assertEqual(row.sold, 2)
            self.assertEqual(
                _recompute_inventory_day(
                    property_obj=self.prop, room_type=self.rt, stay_date=day),
                (0, 2))

    def test_early_departure_release_is_durable_and_excluded_from_recompute(self):
        reservation, lines = self._make_reservation()
        hold_inventory_for_reservation(reservation)
        confirm_inventory_for_reservation(reservation)
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=("status", "updated_at"))

        line = lines[0]
        released = _release_sold_inventory_for_early_departure(
            line, from_date=self.days[1])
        self.assertEqual(released, 2)
        line.refresh_from_db()
        self.assertEqual(line.inventory_released_from, self.days[1])
        # 合同 check_out 保持原值，释放靠明细级事实表达（RSV-03）。
        self.assertEqual(line.check_out, self.days[0] + timedelta(days=3))

        self.assertEqual(
            _recompute_inventory_day(
                property_obj=self.prop, room_type=self.rt,
                stay_date=self.days[0]), (0, 2))
        for day in self.days[1:]:
            self.assertEqual(
                _recompute_inventory_day(
                    property_obj=self.prop, room_type=self.rt, stay_date=day),
                (0, 1))
            self.assertEqual(self._row(day).sold, 1)
        self.assertEqual(InventoryHold.objects.filter(
            property=self.prop, reservation_room=line,
            status=InventoryHold.HoldStatus.RELEASED).count(), 2)
        # 扫描端与执行端共用同一函数：两者的真值必须完全一致。
        run = scan_inventory_discrepancies(
            property_obj=self.prop, days_back=1, checked_by=self.duty)
        self.assertEqual(run.diff_count, 0,
                         "释放后扫描不得把已释放夜晚重新计回 sold")

    def test_no_show_and_released_lines_are_both_excluded_from_sold(self):
        reservation, lines = self._make_reservation(
            status=Reservation.Status.CONFIRMED)
        NoShowRecord.objects.create(
            property=self.prop, reservation_room=lines[0],
            reason="未到（专项）", processed_by=self.duty)
        lines[1].inventory_released_from = self.days[0]
        lines[1].save(update_fields=("inventory_released_from",))
        for day in self.days:
            self.assertEqual(
                _recompute_inventory_day(
                    property_obj=self.prop, room_type=self.rt, stay_date=day),
                (0, 0))

    def test_fault_injection_missing_hold_details_refuses_zeroing_real_held(self):
        """RSV-01/02：无明细时执行修复必须拒绝，而不是把权威 held 清零。"""
        row = self._row(self.days[0])
        row.held = 2
        row.save(update_fields=("held",))
        run = scan_inventory_discrepancies(
            property_obj=self.prop, days_back=1, checked_by=self.duty)
        disc = InventoryDiscrepancy.objects.filter(
            run=run, status=InventoryDiscrepancy.Status.OPEN).first()
        self.assertIsNotNone(disc, "held=2 但无明细，扫描应报差异")
        self.assertEqual(disc.held_count, 0)

        request_inventory_fix(
            property_obj=self.prop, discrepancy_id=disc.pk,
            fix_reason="注入故障：库存服务有 held 但明细表为空",
            fix_evidence="专项故障注入", requested_by=self.front)
        approve_inventory_fix(
            property_obj=self.prop, discrepancy_id=disc.pk, approved_by=self.duty)
        with self.assertRaises(ValueError) as ctx:
            execute_inventory_fix(
                property_obj=self.prop, discrepancy_id=disc.pk,
                executed_by=self.duty)
        self.assertIn("拒绝自动修复", str(ctx.exception))

        row.refresh_from_db()
        self.assertEqual(row.held, 2, "真实冻结不得被空明细清零")
        disc.refresh_from_db()
        self.assertEqual(disc.status, InventoryDiscrepancy.Status.FIX_APPROVED)
        self.assertTrue(AuditEvent.objects.filter(
            action="inventory.fix_refused_missing_hold_details").exists())


@override_settings(DEBUG=True, DJANGO_ENV="test")
class Round28CompensationWorkerTests(TestCase):
    """WB-110a~e：五类补偿任务都必须有真实执行器，且走完租约→终态。"""

    def setUp(self):
        self.prop = Property.objects.create(
            code="R28WORKER", name="第28轮补偿专项酒店",
            timezone="Asia/Shanghai")
        guest = Guest.objects.create(
            property=self.prop, full_name="补偿专项住客")
        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            property=self.prop, guest=guest, booking_no="R28WORKER-001",
            status=Reservation.Status.CONFIRMED,
            check_in=today, check_out=today + timedelta(days=1),
            room_count=1, adult_count=1,
            total_amount=Decimal("600.00"), currency="CNY")
        self.folio = Folio.objects.create(
            property=self.prop, reservation=self.reservation,
            guest=guest, currency="CNY")
        self.payment = Payment.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            provider="mock", provider_ref="R28WORKER-PAY", method="MOCK",
            amount=Decimal("600.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        LedgerEntry.objects.create(
            property=self.prop, folio=self.folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-600.00"), currency="CNY",
            business_date=today, reference="payment:R28WORKER-PAY",
            description="预付（补偿专项）")
        self.intent = PaymentIntent.objects.create(
            property=self.prop, reservation=self.reservation, folio=self.folio,
            intent_no="R28WORKER-INTENT", idempotency_key="R28WORKER-INTENT-KEY",
            amount=Decimal("600.00"), currency="CNY",
            status=PaymentIntent.Status.CREATED,
            expires_at=timezone.now() + timedelta(hours=1))

    def test_all_five_executor_types_are_dispatched_and_reach_terminal(self):
        from apps.billing.payment_intents import create_compensation_task

        TaskType = PaymentCompensationTask.TaskType
        specs = (
            (TaskType.HOLD_COORDINATION, "PaymentIntent", self.intent.pk),
            (TaskType.CHANNEL_QUERY, "PaymentIntent", self.intent.pk),
            (TaskType.CALLBACK_CONFIRM_RETRY, "PaymentIntent", self.intent.pk),
            (TaskType.PAYMENT_LEDGER_REPAIR, "Payment", self.payment.pk),
            (TaskType.RECONCILIATION_DIFF, "Property", self.prop.pk),
        )
        pks = {}
        for kind, aggregate_type, aggregate_id in specs:
            task, created = create_compensation_task(
                task_type=kind, aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                idempotency_key=f"r28-worker-{kind}")
            self.assertTrue(created)
            pks[kind] = task.pk

        call_command("payment_compensation_worker", "--once", "--limit", "20")

        succeeded = 0
        for kind, pk in pks.items():
            task = PaymentCompensationTask.objects.get(pk=pk)
            self.assertGreaterEqual(task.attempt_count, 1, kind)
            self.assertNotIn(task.last_error_code,
                             ("UNSUPPORTED_TASK_TYPE", "WORKER_EXCEPTION"),
                             f"{kind}: {task.last_error_summary}")
            self.assertIn(task.status, (
                PaymentCompensationTask.Status.SUCCEEDED,
                PaymentCompensationTask.Status.PENDING,
                PaymentCompensationTask.Status.DEAD_LETTER), kind)
            if task.status == PaymentCompensationTask.Status.SUCCEEDED:
                succeeded += 1
        self.assertGreaterEqual(succeeded, 3,
                                "Hold 协调/账本修复/对账差异应能幂等成功")


# ---------------------------------------------------------------------------
# F/G：故障注入与生产调度（MAINT-02 / WB-090）
# ---------------------------------------------------------------------------
@override_settings(DEBUG=True, DJANGO_ENV="test")
class Round28MaintenanceTests(TestCase):
    """恢复命令与统一调度入口必须真实可达，且失败路径不丢补偿事实。"""

    def setUp(self):
        self.prop = Property.objects.create(
            code="R28MAINT", name="第28轮维护专项酒店", timezone="Asia/Shanghai")
        self.guest = Guest.objects.create(
            property=self.prop, full_name="维护专项住客")
        self.today = timezone.localdate()

    def _make_folio(self, booking_no):
        reservation = Reservation.objects.create(
            property=self.prop, guest=self.guest, booking_no=booking_no,
            status=Reservation.Status.PENDING, check_in=self.today,
            check_out=self.today + timedelta(days=1), room_count=1,
            adult_count=1, total_amount=Decimal("600.00"), currency="CNY")
        folio = Folio.objects.create(
            property=self.prop, reservation=reservation, guest=self.guest,
            currency="CNY")
        return reservation, folio

    def _make_intent(self, folio, suffix, *, unknown=False):
        intent = PaymentIntent.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            intent_no=f"R28-INTENT-{suffix}",
            idempotency_key=f"R28-INTENT-KEY-{suffix}",
            amount=Decimal("600.00"), currency="CNY",
            status=PaymentIntent.Status.CREATED,
            expires_at=timezone.now() - timedelta(minutes=1))
        if unknown:
            ProviderTransaction.objects.create(
                property=self.prop, intent=intent, provider_code="mock",
                provider_transaction_id=f"R28-PT-{suffix}",
                amount=Decimal("600.00"), currency="CNY",
                channel_status="UNKNOWN", signature_verified=False)
        return intent

    def test_stale_night_audit_run_recovered_but_active_run_untouched(self):
        bd = BusinessDate.objects.create(
            property=self.prop, business_date=self.today,
            status=BusinessDate.Status.OPEN)
        stale = NightAuditRun.objects.create(
            business_date=bd, status=NightAuditRun.RunStatus.RUNNING)
        active = NightAuditRun.objects.create(
            business_date=bd, status=NightAuditRun.RunStatus.RUNNING)
        NightAuditRun.objects.filter(pk=stale.pk).update(
            started_at=timezone.now() - timedelta(minutes=30))

        count = recover_stale_night_audit_runs(property_obj=self.prop,
                                               timeout_minutes=15)
        self.assertEqual(count, 1)
        stale.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(stale.status, NightAuditRun.RunStatus.FAILED)
        self.assertEqual(active.status, NightAuditRun.RunStatus.RUNNING)
        self.assertIsNotNone(stale.finished_at)
        self.assertTrue(AuditEvent.objects.filter(
            action="night_audit.run_recovered").exists())
        # 生产命令可重复执行且幂等
        call_command("recover_stale_night_audits")
        self.assertFalse(NightAuditRun.objects.filter(
            pk=stale.pk, status=NightAuditRun.RunStatus.RUNNING).exists())

    def test_maintenance_expires_due_intent_and_keeps_unknown_compensation_task(self):
        """WB-090 故障注入：UNKNOWN 分支原子块回滚，补偿任务在独立事务存活。"""
        _, folio = self._make_folio("R28MAINT-001")
        plain = self._make_intent(folio, "PLAIN")
        unknown = self._make_intent(folio, "UNKNOWN", unknown=True)

        call_command("run_billing_maintenance", "--property", self.prop.code,
                     "--only", "expire")
        plain.refresh_from_db()
        unknown.refresh_from_db()
        self.assertEqual(plain.status, PaymentIntent.Status.EXPIRED)
        self.assertEqual(unknown.status, PaymentIntent.Status.CREATED,
                         "渠道 UNKNOWN 时意图不得被直接过期")

        task = PaymentCompensationTask.objects.get(
            idempotency_key=f"intent-expire-channel-query:{unknown.pk}")
        self.assertEqual(task.task_type,
                         PaymentCompensationTask.TaskType.CHANNEL_QUERY)
        self.assertTrue(AuditEvent.objects.filter(
            action="billing.intent_expired").exists())

        # 重复调度不得刷屏：幂等键唯一，任务数保持 1。
        call_command("run_billing_maintenance", "--property", self.prop.code,
                     "--only", "expire")
        self.assertEqual(PaymentCompensationTask.objects.filter(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            idempotency_key=f"intent-expire-channel-query:{unknown.pk}").count(), 1)

    def test_maintenance_creates_stuck_callback_retry_task_once(self):
        """MAINT：渠道已成功验签但意图未达终态 → 生产调度创建重试补偿任务。"""
        _, folio = self._make_folio("R28MAINT-002")
        intent = self._make_intent(folio, "STUCK")
        ProviderTransaction.objects.create(
            property=self.prop, intent=intent, provider_code="mock",
            provider_transaction_id="R28-PT-STUCK",
            amount=Decimal("600.00"), currency="CNY",
            channel_status="SUCCESS", signature_verified=True)

        call_command("run_billing_maintenance", "--property", self.prop.code,
                     "--only", "stuck")
        key = f"callback-confirm-retry:{intent.pk}"
        self.assertEqual(PaymentCompensationTask.objects.filter(
            idempotency_key=key,
            task_type=PaymentCompensationTask.TaskType.CALLBACK_CONFIRM_RETRY
        ).count(), 1)
        call_command("run_billing_maintenance", "--property", self.prop.code,
                     "--only", "stuck")
        self.assertEqual(PaymentCompensationTask.objects.filter(
            idempotency_key=key).count(), 1, "重复调度不得产生重复补偿任务")


# ---------------------------------------------------------------------------
# C/D：真实 MySQL 并发（HTTP 级），SQLite 一律跳过
# ---------------------------------------------------------------------------
@override_settings(DEBUG=True, DJANGO_ENV="test")
@unittest.skipUnless(
    connection.vendor == "mysql",
    "本类依赖 MySQL 真实行锁与 autocommit 下的 select_for_update 报错语义；"
    "SQLite 静默忽略 select_for_update，不能替代生产验证。")
class Round28MySQLConcurrencyHttpTests(TransactionTestCase):
    """FIN-01 / WB-05a：通过真实 HTTP 路由在多线程下验证 MySQL 事务与锁。"""

    def setUp(self):
        # TransactionTestCase 每个用例后 flush 会清掉迁移播种的 DEFAULT 物业，
        # 不能假设它仍然存在；但物业上下文中间件只认 DEFAULT_PROPERTY_CODE。
        self.property, _ = Property.objects.get_or_create(
            code="DEFAULT",
            defaults={"name": "并发专项默认物业", "timezone": "Asia/Shanghai"})
        self.fd = self._make_staff("r28_mysql_fd", "前台")
        self.guest = Guest.objects.create(
            property=self.property, full_name="第28轮并发住客")

    def _make_staff(self, username, group_name):
        user = User.objects.create_user(username=username, password="r28-pass")
        profile = StaffProfile.objects.create(
            user=user, full_name=username,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile, property=self.property,
            employee_no=f"R28-{username}",
            status=PropertyStaffMembership.Status.ACTIVE)
        group, _ = Group.objects.get_or_create(name=group_name)
        # TransactionTestCase flush 会清掉迁移播种的分组及其权限关联；
        # 专项夹具自行补齐所需权限，避免把「夹具缺权限」误判成业务 403/500。
        for app_label, codename in (
            ("hotelapp", "view_accommodation"),
            ("hotelapp", "change_accommodation"),
            ("billing", "refund_request"),
        ):
            perm = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename).first()
            if perm is None:
                ctype = ContentType.objects.filter(app_label=app_label).first()
                perm = Permission.objects.create(
                    content_type=ctype, codename=codename, name=codename)
            group.permissions.add(perm)
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=timezone.now() - timedelta(minutes=1))
        return user

    def _make_reservation(self, booking_no, *, amount=Decimal("600.00")):
        today = timezone.localdate()
        reservation = Reservation.objects.create(
            property=self.property, guest=self.guest, booking_no=booking_no,
            status=Reservation.Status.CONFIRMED,
            check_in=today, check_out=today + timedelta(days=2),
            room_count=1, adult_count=1, total_amount=amount, currency="CNY")
        folio = Folio.objects.create(
            property=self.property, reservation=reservation,
            guest=self.guest, currency="CNY")
        return reservation, folio

    @staticmethod
    def _join(test, threads, timeout=90):
        for thread in threads:
            thread.join(timeout=timeout)
            test.assertFalse(thread.is_alive(), "工作线程超时未结束（疑似死锁）")

    def _http_post(self, results, name, url, payload, key):
        def _worker():
            try:
                close_old_connections()
                client = Client()
                client.force_login(self.fd)
                response = client.post(
                    url, data=json.dumps(payload),
                    content_type="application/json",
                    HTTP_IDEMPOTENCY_KEY=key)
                results[name] = response.status_code
            except Exception as exc:  # 线程内异常必须回传主线程断言
                results[name] = f"exception:{type(exc).__name__}:{exc}"
            finally:
                connections["default"].close()
        return _worker

    def test_mysql_rejects_select_for_update_in_autocommit(self):
        """负向对照：证明 SQLite 全绿确实无法覆盖 FIN-01。

        MySQL 后端 has_select_for_update=True，autocommit 下求值会抛
        TransactionManagementError；SQLite 该特性为 False，静默忽略，
        因此「SQLite 全绿」绝不能当作生产已修复的证据。
        """
        self.assertEqual(connection.vendor, "mysql")
        self.assertTrue(connection.features.has_select_for_update)
        self.assertFalse(connection.in_atomic_block)
        with self.assertRaises(TransactionManagementError):
            Reservation.objects.select_for_update().filter(pk=-1).first()

    def test_exception_release_endpoint_two_threads_never_500(self):
        """FIN-01：select_for_update 必须在事务内；否则 MySQL 接口必 500。"""
        reservation, folio = self._make_reservation("R28FIN01")
        Payment.objects.create(
            property=self.property, reservation=reservation, folio=folio,
            provider="mock", provider_ref="R28FIN01-PREPAID", method="MOCK",
            amount=Decimal("1200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        LedgerEntry.objects.create(
            property=self.property, folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-1200.00"), currency="CNY",
            business_date=timezone.localdate(),
            reference="payment:R28FIN01-PREPAID", description="预付（并发专项）")

        url = (f"/api/v1/front-desk/folios/{reservation.booking_no}"
               "/exception-release-request")
        payload = {"amount": "600.00",
                   "reason_code": CheckoutExceptionRelease.ReasonCode.OTHER,
                   "reason_text": "第28轮 FIN-01 并发专项"}
        barrier = Barrier(2)
        results = {}

        def _run(name, key):
            try:
                close_old_connections()
                barrier.wait(timeout=30)
                client = Client()
                client.force_login(self.fd)
                response = client.post(
                    url, data=json.dumps(payload),
                    content_type="application/json",
                    HTTP_IDEMPOTENCY_KEY=key)
                results[name] = response.status_code
            except Exception as exc:
                results[name] = f"exception:{type(exc).__name__}:{exc}"
            finally:
                connections["default"].close()

        threads = [
            threading.Thread(target=_run, args=("t1", "r28-fin01-key-1")),
            threading.Thread(target=_run, args=("t2", "r28-fin01-key-2")),
        ]
        for thread in threads:
            thread.start()
        self._join(self, threads)

        self.assertEqual(len(results), 2, results)
        for value in results.values():
            self.assertIsInstance(value, int, results)
            self.assertNotEqual(value, 500,
                                "MySQL autocommit 下 select_for_update 会触发"
                                " TransactionManagementError → 接口 500（FIN-01）")
            self.assertIn(value, (200, 201, 400, 409), results)
        self.assertEqual(CheckoutExceptionRelease.objects.filter(
            folio=folio,
            status=CheckoutExceptionRelease.Status.REQUESTED).count(), 1,
            "同一账单最多一笔在途异常放行")

    def test_deposit_cap_is_enforced_under_real_mysql_row_lock(self):
        """WB-05a：并发两笔押金在 540 上限附近，只能成功一笔。"""
        reservation, _folio = self._make_reservation("R28CAP01")
        url = f"/api/v1/front-desk/reservations/{reservation.booking_no}/deposits"
        client = Client()
        client.force_login(self.fd)
        for index in (1, 2):
            resp = client.post(
                url, data=json.dumps({"amount": "180.00", "method": "CARD"}),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY=f"r28-cap-init-{index}")
            self.assertEqual(resp.status_code, 201, resp.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.deposit_amount, Decimal("360.00"))

        barrier = Barrier(2)
        results = {}
        payload = {"amount": "180.00", "method": "CARD"}

        def _run(name, key):
            try:
                close_old_connections()
                barrier.wait(timeout=30)
                worker = Client()
                worker.force_login(self.fd)
                response = worker.post(
                    url, data=json.dumps(payload),
                    content_type="application/json",
                    HTTP_IDEMPOTENCY_KEY=key)
                results[name] = response.status_code
            except Exception as exc:
                results[name] = f"exception:{type(exc).__name__}:{exc}"
            finally:
                connections["default"].close()

        threads = [
            threading.Thread(target=_run, args=("t1", "r28-cap-race-1")),
            threading.Thread(target=_run, args=("t2", "r28-cap-race-2")),
        ]
        for thread in threads:
            thread.start()
        self._join(self, threads)

        self.assertEqual(len(results), 2, results)
        statuses = sorted(v for v in results.values())
        self.assertEqual(statuses, [201, 409], results)
        reservation.refresh_from_db()
        self.assertEqual(reservation.deposit_amount, Decimal("540.00"))
        self.assertEqual(Payment.objects.filter(
            reservation=reservation, provider="frontdesk_deposit").count(), 3)
        self.assertEqual(LedgerEntry.objects.filter(
            folio__reservation=reservation,
            entry_type=LedgerEntry.EntryType.DEPOSIT).count(), 3)


# ---------------------------------------------------------------------------
# 第 29 轮：WB-120/121 —— 维护命令创建的任务必须能被 Worker 按同一聚合契约消费
# ---------------------------------------------------------------------------
@override_settings(DEBUG=True, DJANGO_ENV="test")
class Round29MaintenanceWorkerContractTests(TestCase):
    """WB-120：创建方（run_billing_maintenance）与执行方（Worker）聚合契约
    必须一致——PAYMENT_LEDGER_REPAIR 用 Payment 聚合、RECONCILIATION_DIFF
    用 FolioReconciliationRun 聚合；从真实对账差异出发走完
    「维护命令 → 任务 → Worker → 终态/账务事实 → 重跑无重复副作用」。
    WB-121：UNKNOWN 与状态冲突在维护统计中分开计数。
    """

    def setUp(self):
        from apps.core.models import Property
        self.prop = Property.objects.create(
            code="R29MAINT", name="第29轮维护契约专项酒店",
            timezone="Asia/Shanghai")
        self.guest = Guest.objects.create(
            property=self.prop, full_name="第29轮契约住客")
        self.today = timezone.localdate()

    def _make_folio(self, booking_no):
        reservation = Reservation.objects.create(
            property=self.prop, guest=self.guest, booking_no=booking_no,
            status=Reservation.Status.CONFIRMED, check_in=self.today,
            check_out=self.today + timedelta(days=1), room_count=1,
            adult_count=1, total_amount=Decimal("600.00"), currency="CNY")
        folio = Folio.objects.create(
            property=self.prop, reservation=reservation, guest=self.guest,
            currency="CNY")
        return reservation, folio

    def test_payment_no_entry_uses_payment_aggregate_and_worker_repairs(self):
        """PAYMENT_NO_ENTRY 差异：任务聚合=Payment，Worker 成功补凭证。"""
        _, folio = self._make_folio("R29-REPAIR-001")
        payment = Payment.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            provider="mock", provider_ref="R29-PAY-REPAIR", method="MOCK",
            amount=Decimal("600.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        # 故意不建 LedgerEntry —— 构造真实对账差异
        call_command("run_billing_maintenance", "--property", "R29MAINT",
                     "--only", "recon")

        task = PaymentCompensationTask.objects.get(
            task_type=PaymentCompensationTask.TaskType.PAYMENT_LEDGER_REPAIR,
            idempotency_key__startswith="PAYMENT_NO_ENTRY:")
        self.assertEqual(task.aggregate_type, "Payment",
                         "WB-120：不得再用 Folio ID 冒充 Payment 聚合")
        self.assertEqual(task.aggregate_id, payment.pk)

        call_command("payment_compensation_worker", "--once", "--limit", "10")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED,
                         task.last_error_summary)
        self.assertTrue(LedgerEntry.objects.filter(
            folio=folio, reference="payment:R29-PAY-REPAIR").exists())

        # 重跑维护 + Worker：差异不再产生，凭证不重复
        call_command("run_billing_maintenance", "--property", "R29MAINT",
                     "--only", "recon")
        call_command("payment_compensation_worker", "--once", "--limit", "10")
        self.assertEqual(PaymentCompensationTask.objects.filter(
            task_type=PaymentCompensationTask.TaskType.PAYMENT_LEDGER_REPAIR,
            idempotency_key__startswith="PAYMENT_NO_ENTRY:",
            aggregate_type="Payment").count(), 1)
        self.assertEqual(LedgerEntry.objects.filter(
            folio=folio, reference="payment:R29-PAY-REPAIR").count(), 1)

    def test_non_payment_diff_uses_run_aggregate_and_worker_rescans_readonly(self):
        """BALANCE_MISMATCH（已关闭账单余额重建不符）差异：任务聚合=对账运行，
        Worker 解析物业做只读重扫成功，不改任何账务事实。"""
        _, folio = self._make_folio("R29-RECON-001")
        folio.status = Folio.Status.CLOSED
        folio.closed_at = timezone.now()
        folio.save(update_fields=("status", "closed_at"))
        LedgerEntry.objects.create(
            property=self.prop, folio=folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=Decimal("600.00"), currency="CNY",
            business_date=self.today, reference="charge:R29-RECON-ROOM",
            description="房费（已关闭账单，余额重建不符 → 构造对账差异）")
        call_command("run_billing_maintenance", "--property", "R29MAINT",
                     "--only", "recon")

        task = PaymentCompensationTask.objects.get(
            task_type=PaymentCompensationTask.TaskType.RECONCILIATION_DIFF,
            idempotency_key__startswith="BALANCE_MISMATCH:")
        self.assertEqual(task.aggregate_type, "FolioReconciliationRun")

        call_command("payment_compensation_worker", "--once", "--limit", "10")
        task.refresh_from_db()
        self.assertEqual(task.status, PaymentCompensationTask.Status.SUCCEEDED,
                         task.last_error_summary)
        # 只读核对：不改账本（差异事实保持原样，等待授权财务处理）
        self.assertEqual(LedgerEntry.objects.filter(folio=folio).count(), 1)

    def test_expire_statistics_separate_unknown_from_conflicts(self):
        """WB-121（第 31 轮）：真实调用链验证——UNKNOWN 意图计入 unknown
        且不 mock expire_intent；竞态冲突单独计 conflicts。"""
        from io import StringIO
        from unittest.mock import patch
        from apps.billing.management.commands import run_billing_maintenance as maint

        r1, f1 = self._make_folio("R29-EXP-U")
        r2, f2 = self._make_folio("R29-EXP-C")
        intent_u = PaymentIntent.objects.create(
            property=self.prop, reservation=r1, folio=f1,
            intent_no="R29-INTENT-U", idempotency_key="R29-INTENT-KEY-U",
            amount=Decimal("600.00"), currency="CNY",
            status=PaymentIntent.Status.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1))
        intent_c = PaymentIntent.objects.create(
            property=self.prop, reservation=r2, folio=f2,
            intent_no="R29-INTENT-C", idempotency_key="R29-INTENT-KEY-C",
            amount=Decimal("600.00"), currency="CNY",
            status=PaymentIntent.Status.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1))
        # 真实 UNKNOWN 场景：渠道交易事实 UNKNOWN → expire_intent 走
        # 真实服务链抛 ChannelStateUnknownError（不再包装成 ValueError）
        ProviderTransaction.objects.create(
            property=self.prop, intent=intent_u, provider_code="mock",
            provider_transaction_id="R29-PT-UNKNOWN-U",
            amount=Decimal("600.00"), currency="CNY",
            channel_status="UNKNOWN", signature_verified=False)

        real_expire = maint.expire_intent

        def expire_with_race_conflict(*, property_obj, intent_id, **kwargs):
            if intent_id == intent_c.pk:
                # 模拟并发竞态：意图在命令快照后已被迁移出待支付状态
                raise ValueError("意图状态已被并发修改")
            return real_expire(property_obj=property_obj,
                               intent_id=intent_id, **kwargs)

        with patch.object(maint, "expire_intent",
                          side_effect=expire_with_race_conflict):
            out = StringIO()
            call_command("run_billing_maintenance", "--property", "R29MAINT",
                         "--only", "expire", stdout=out)
        output = out.getvalue()
        self.assertIn("1 个渠道状态未知", output)
        self.assertIn("1 个状态冲突/业务拒绝", output)
        # 三者一致：意图保持原状态 + CHANNEL_QUERY 任务已落 + 统计正确
        intent_u.refresh_from_db()
        self.assertEqual(intent_u.status, PaymentIntent.Status.PENDING)
        self.assertTrue(PaymentCompensationTask.objects.filter(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            idempotency_key=f"intent-expire-channel-query:{intent_u.pk}",
        ).exists())

    def test_unknown_intent_via_maintenance_creates_channel_query_task(self):
        """真实 UNKNOWN 路径：维护命令不过期意图，落 CHANNEL_QUERY 任务；
        Worker 渠道查询无 SUCCESS 事实时不伪造成功。"""
        _, folio = self._make_folio("R29-EXP-REAL")
        intent = PaymentIntent.objects.create(
            property=self.prop, reservation=folio.reservation, folio=folio,
            intent_no="R29-INTENT-REAL", idempotency_key="R29-INTENT-KEY-REAL",
            amount=Decimal("600.00"), currency="CNY",
            status=PaymentIntent.Status.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1))
        ProviderTransaction.objects.create(
            property=self.prop, intent=intent, provider_code="mock",
            provider_transaction_id="R29-PT-UNKNOWN",
            amount=Decimal("600.00"), currency="CNY",
            channel_status="UNKNOWN", signature_verified=False)

        call_command("run_billing_maintenance", "--property", "R29MAINT",
                     "--only", "expire")
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING,
                         "UNKNOWN 意图不得直接过期/释放库存")
        task = PaymentCompensationTask.objects.get(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            idempotency_key=f"intent-expire-channel-query:{intent.pk}")
        self.assertEqual(task.aggregate_type, "PaymentIntent")

        call_command("payment_compensation_worker", "--once", "--limit", "10")
        task.refresh_from_db()
        self.assertEqual(task.last_error_code, "CHANNEL_QUERY_NO_SUCCESS_FACT")
        self.assertIn(task.status, (
            PaymentCompensationTask.Status.PENDING,
            PaymentCompensationTask.Status.LEASED,
            PaymentCompensationTask.Status.DEAD_LETTER))
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)
