"""TASK-05 通用 Outbox 测试：入队/回滚/认领/退避/死信/恢复/幂等/并发互斥。

覆盖面：
- enqueue 后事件 PENDING 且 payload 可解析；
- 业务事务回滚时事件随之回滚（事务性发件箱核心保证）；
- worker 单轮：PENDING→SUCCEEDED、payload 传给消费者、消费者异常→RETRY
  （attempt_count=1 且 next_attempt_at 为未来）、超限→DEAD；
- recover_stale_events：locked_at 过期的 PROCESSING→PENDING；
- 幂等：SUCCEEDED 事件不再被认领；
- 并发不重复认领：TransactionTestCase + 双线程 select_for_update
  (skip_locked=True) 认领互斥（MySQL 专用，SQLite 整类跳过；
  权威验证由主会话在 MySQL 侧串行执行）。
"""

import threading
import unittest
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.core.management.commands.outbox_worker import run_worker_cycle
from apps.core.models import OutboxEvent
from apps.core.outbox import (
    OUTBOX_CONSUMERS,
    OUTBOX_NOP,
    OUTBOX_NOTIFICATION_DELIVER,
    complete_outbox_event,
    default_lease_owner,
    enqueue_outbox_event,
    lease_outbox_events,
    recover_stale_events,
)


class EnqueueOutboxEventTests(TestCase):
    """入队契约：PENDING 初始态、payload 可解析、字段裁剪。"""

    def test_enqueue_creates_pending_event_with_parsable_payload(self):
        event = enqueue_outbox_event(
            event_type="reservation.status_changed",
            aggregate_type="Reservation",
            aggregate_id="42",
            payload={"from": "PENDING", "to": "CONFIRMED"},
        )
        self.assertEqual(event.status, OutboxEvent.Status.PENDING)
        self.assertEqual(event.event_type, "reservation.status_changed")
        self.assertEqual(event.aggregate_type, "Reservation")
        self.assertEqual(event.aggregate_id, "42")
        # payload 落库后可原样解析回 dict（JSON 往返）
        event.refresh_from_db()
        self.assertEqual(event.payload, {"from": "PENDING", "to": "CONFIRMED"})
        self.assertEqual(event.attempt_count, 0)
        self.assertIsNone(event.processed_at)

    def test_enqueue_rejects_missing_identity_fields(self):
        with self.assertRaises(ValueError):
            enqueue_outbox_event(
                event_type="  ", aggregate_type="Reservation", aggregate_id="1", payload={}
            )
        with self.assertRaises(ValueError):
            enqueue_outbox_event(event_type="x", aggregate_type="", aggregate_id="1", payload={})
        with self.assertRaises(ValueError):
            enqueue_outbox_event(
                event_type="x", aggregate_type="Reservation", aggregate_id="  ", payload={}
            )

    def test_enqueue_none_payload_becomes_empty_dict(self):
        event = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="X", aggregate_id="1", payload=None
        )
        self.assertEqual(event.payload, {})

    def test_enqueue_inside_business_transaction_rolls_back_with_it(self):
        """事务性发件箱核心保证：业务回滚则事件回滚，不留半截事件。"""
        with self.assertRaises(RuntimeError), transaction.atomic():
            enqueue_outbox_event(
                event_type=OUTBOX_NOP,
                aggregate_type="Reservation",
                aggregate_id="7",
                payload={"reason": "should vanish"},
            )
            # 模拟业务事务中途失败：手动 raise 触发整体回滚
            raise RuntimeError("业务失败，事务必须整体回滚")
        self.assertFalse(OutboxEvent.objects.filter(aggregate_id="7").exists())

    def test_enqueue_commits_outside_atomic_block(self):
        enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="Reservation", aggregate_id="8", payload={}
        )
        self.assertEqual(OutboxEvent.objects.count(), 1)


class HighRiskEventGuardTests(TestCase):
    """任务书 V1.1 §十二.10：Worker 不得自动重跑账务/退款/支付/夜审动作。

    该约束必须在「入队」阶段硬拒绝，而不是只靠注释或 Worker 侧退避——
    否则任何调用方传入 billing.refund.execute 都会先落一条事件事实。
    """

    FORBIDDEN_TYPES = (
        "billing.post_entry",
        "billing.refund.execute",
        "billing.payment.capture",
        "billing.reversal",
        "night_audit.run",
        "night_audit.close",
        "business_date.reopen",
        "reservation.checkout",
    )

    def test_high_risk_event_types_are_rejected_at_enqueue(self):
        for event_type in self.FORBIDDEN_TYPES:
            with self.subTest(event_type=event_type), self.assertRaises(ValueError):
                enqueue_outbox_event(
                    event_type=event_type,
                    aggregate_type="Billing",
                    aggregate_id="1",
                    payload={},
                )

    def test_rejected_event_leaves_no_persisted_fact(self):
        with self.assertRaises(ValueError):
            enqueue_outbox_event(
                event_type="billing.refund.execute",
                aggregate_type="Refund",
                aggregate_id="9",
                payload={"amount": "1.00"},
            )
        self.assertFalse(OutboxEvent.objects.exists())

    def test_low_risk_event_types_still_allowed(self):
        event = enqueue_outbox_event(
            event_type=OUTBOX_NOTIFICATION_DELIVER,
            aggregate_type="Notification",
            aggregate_id="5",
            payload={"k": "v"},
        )
        self.assertEqual(event.status, OutboxEvent.Status.PENDING)


class LeaseOutboxEventsTests(TestCase):
    """认领契约：只认领 PENDING/到期 RETRY，置 PROCESSING + 租约。"""

    def test_lease_claims_pending_and_marks_processing(self):
        event = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        owner = default_lease_owner()
        leased = lease_outbox_events(limit=10, lease_owner=owner, lease_seconds=60)
        self.assertEqual([e.pk for e in leased], [event.pk])
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.Status.PROCESSING)
        self.assertEqual(event.lease_owner, owner)
        self.assertIsNotNone(event.locked_at)

    def test_lease_claims_due_retry_but_not_future_retry(self):
        due = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        future = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="2", payload={}
        )
        future.status = OutboxEvent.Status.RETRY
        future.next_attempt_at = timezone.now() + timedelta(hours=1)
        future.save(update_fields=("status", "next_attempt_at"))
        leased = lease_outbox_events(limit=10, lease_owner="w1")
        self.assertEqual([e.pk for e in leased], [due.pk])

    def test_lease_never_claims_terminal_states(self):
        for aggregate_id in ("1", "2", "3"):
            enqueue_outbox_event(
                event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id=aggregate_id, payload={}
            )
        OutboxEvent.objects.update(status=OutboxEvent.Status.SUCCEEDED)
        OutboxEvent.objects.filter(aggregate_id="2").update(status=OutboxEvent.Status.PROCESSING)
        OutboxEvent.objects.filter(aggregate_id="3").update(status=OutboxEvent.Status.DEAD)
        self.assertEqual(lease_outbox_events(limit=10, lease_owner="w1"), [])

    def test_lease_respects_limit(self):
        for i in range(5):
            enqueue_outbox_event(
                event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id=str(i), payload={}
            )
        leased = lease_outbox_events(limit=3, lease_owner="w1")
        self.assertEqual(len(leased), 3)


class CompleteOutboxEventTests(TestCase):
    """回写契约：成功/退避/死信三态迁移与租约校验。"""

    def _leased_event(self, owner="w1"):
        event = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        lease_outbox_events(limit=10, lease_owner=owner, lease_seconds=60)
        event.refresh_from_db()
        return event, owner

    def test_success_marks_succeeded_with_processed_at(self):
        event, owner = self._leased_event()
        done = complete_outbox_event(event.pk, success=True, lease_owner=owner)
        self.assertEqual(done.status, OutboxEvent.Status.SUCCEEDED)
        self.assertIsNotNone(done.processed_at)
        self.assertEqual(done.last_error, "")

    def test_failure_goes_retry_with_exponential_backoff(self):
        event, owner = self._leased_event()
        before = timezone.now()
        done = complete_outbox_event(event.pk, success=False, error="boom", lease_owner=owner)
        self.assertEqual(done.status, OutboxEvent.Status.RETRY)
        self.assertEqual(done.attempt_count, 1)
        self.assertEqual(done.last_error, "boom")
        # 第一次失败退避 30*2^1=60s，必须在未来且约等于 before+60s（留时钟误差）
        self.assertIsNotNone(done.next_attempt_at)
        expected = before + timedelta(seconds=60)
        self.assertGreater(done.next_attempt_at, timezone.now())
        self.assertLessEqual(done.next_attempt_at, expected + timedelta(seconds=2))
        self.assertGreaterEqual(done.next_attempt_at, expected - timedelta(seconds=2))

    def test_failure_beyond_max_attempts_goes_dead(self):
        event, owner = self._leased_event()
        event.max_attempts = 2
        event.save(update_fields=("max_attempts",))
        complete_outbox_event(event.pk, success=False, error="e1", lease_owner=owner)
        # 第 2 次失败达到 max_attempts → DEAD + last_error
        # 手动把事件放回 PROCESSING 模拟第二次认领后的回写
        event.status = OutboxEvent.Status.PROCESSING
        event.lease_owner = owner
        event.save(update_fields=("status", "lease_owner"))
        done = complete_outbox_event(event.pk, success=False, error="e2", lease_owner=owner)
        self.assertEqual(done.status, OutboxEvent.Status.DEAD)
        self.assertEqual(done.last_error, "e2")
        self.assertIsNotNone(done.processed_at)

    def test_complete_rejects_wrong_or_missing_lease_owner(self):
        event, owner = self._leased_event()
        with self.assertRaises(ValueError):
            complete_outbox_event(event.pk, success=True, lease_owner="")
        with self.assertRaises(ValueError):
            complete_outbox_event(event.pk, success=True, lease_owner="w2")
        # 正确持有者仍可提交
        done = complete_outbox_event(event.pk, success=True, lease_owner=owner)
        self.assertEqual(done.status, OutboxEvent.Status.SUCCEEDED)

    def test_complete_rejects_unleased_or_terminal_events(self):
        # 未认领事件不可提交结果
        event = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        with self.assertRaises(ValueError):
            complete_outbox_event(event.pk, success=True, lease_owner="w1")
        # 终态事件不可复活
        event.status = OutboxEvent.Status.SUCCEEDED
        event.save(update_fields=("status",))
        with self.assertRaises(ValueError):
            complete_outbox_event(event.pk, success=True, lease_owner="w1")


class RecoverStaleEventsTests(TestCase):
    """崩溃恢复：locked_at 过期的 PROCESSING → PENDING。"""

    def test_stale_processing_resets_to_pending(self):
        event = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        # 模拟 worker 崩溃：认领后 locked_at 停在过去
        lease_outbox_events(limit=10, lease_owner="dead-worker")
        OutboxEvent.objects.update(locked_at=timezone.now() - timedelta(seconds=120))
        recovered = recover_stale_events(lease_seconds=60)
        self.assertEqual(recovered, 1)
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.Status.PENDING)
        self.assertIsNone(event.locked_at)
        self.assertEqual(event.lease_owner, "")

    def test_fresh_lease_is_not_recovered(self):
        enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        lease_outbox_events(limit=10, lease_owner="alive-worker")
        # 租约 60s，locked_at 刚刚写入：不应被恢复
        self.assertEqual(recover_stale_events(lease_seconds=60), 0)
        first = OutboxEvent.objects.first()
        assert first is not None
        self.assertEqual(first.status, OutboxEvent.Status.PROCESSING)

    def test_terminal_states_never_recovered(self):
        enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        lease_outbox_events(limit=10, lease_owner="w1")
        OutboxEvent.objects.update(
            status=OutboxEvent.Status.SUCCEEDED, locked_at=timezone.now() - timedelta(seconds=120)
        )
        self.assertEqual(recover_stale_events(lease_seconds=60), 0)


class OutboxIdempotencyTests(TestCase):
    """幂等：同一事件消费成功后不再被认领（单次消费语义）。"""

    def test_succeeded_event_never_reclaimed(self):
        event = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={"k": "v"}
        )
        owner = default_lease_owner()
        leased = lease_outbox_events(limit=10, lease_owner=owner)
        self.assertEqual(len(leased), 1)
        complete_outbox_event(event.pk, success=True, lease_owner=owner)
        # 成功后再认领：不返回任何事件
        self.assertEqual(lease_outbox_events(limit=10, lease_owner=owner), [])
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.Status.SUCCEEDED)

    def test_retry_event_not_claimable_before_next_attempt_at(self):
        event = enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        owner = "w1"
        lease_outbox_events(limit=10, lease_owner=owner)
        done = complete_outbox_event(event.pk, success=False, error="e", lease_owner=owner)
        self.assertEqual(done.status, OutboxEvent.Status.RETRY)
        # 退避期内不可被认领
        self.assertEqual(lease_outbox_events(limit=10, lease_owner=owner), [])


class OutboxConsumerRegistryTests(TestCase):
    """注册表契约：预留类型已注册，未注册类型按失败退避。"""

    def test_reserved_event_types_are_registered(self):
        for event_type in (
            "reservation.status_changed",
            "membership.changed",
            "notification.deliver",
            OUTBOX_NOP,
        ):
            self.assertIn(event_type, OUTBOX_CONSUMERS)

    def test_worker_single_cycle_succeeds_and_passes_payload(self):
        """worker 单轮：PENDING→SUCCEEDED，payload 传给消费者。"""
        seen = {}
        original = OUTBOX_CONSUMERS[OUTBOX_NOP]

        def recording_consumer(event):
            seen["payload"] = event.payload
            seen["aggregate_id"] = event.aggregate_id

        OUTBOX_CONSUMERS[OUTBOX_NOP] = recording_consumer
        try:
            enqueue_outbox_event(
                event_type=OUTBOX_NOP,
                aggregate_type="Reservation",
                aggregate_id="9",
                payload={"hello": "world"},
            )
            summary = run_worker_cycle(lease_owner="w-test", lease_seconds=60)
        finally:
            OUTBOX_CONSUMERS[OUTBOX_NOP] = original
        self.assertEqual(summary["claimed"], 1)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(seen["payload"], {"hello": "world"})
        self.assertEqual(seen["aggregate_id"], "9")
        event = OutboxEvent.objects.get(aggregate_id="9")
        self.assertEqual(event.status, OutboxEvent.Status.SUCCEEDED)

    def test_consumer_exception_goes_retry_with_backoff(self):
        """消费者抛异常 → RETRY、attempt_count=1、next_attempt_at 为未来。"""

        def exploding_consumer(event):
            raise RuntimeError("consumer exploded")

        original = OUTBOX_CONSUMERS[OUTBOX_NOP]
        OUTBOX_CONSUMERS[OUTBOX_NOP] = exploding_consumer
        try:
            enqueue_outbox_event(
                event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
            )
            summary = run_worker_cycle(lease_owner="w-test", lease_seconds=60)
        finally:
            OUTBOX_CONSUMERS[OUTBOX_NOP] = original
        self.assertEqual(summary["retried"], 1)
        event = OutboxEvent.objects.get(aggregate_id="1")
        self.assertEqual(event.status, OutboxEvent.Status.RETRY)
        self.assertEqual(event.attempt_count, 1)
        self.assertIn("consumer exploded", event.last_error)
        self.assertIsNotNone(event.next_attempt_at)
        self.assertGreater(event.next_attempt_at, timezone.now())

    def test_unregistered_event_type_retries(self):
        enqueue_outbox_event(
            event_type="no.such.type", aggregate_type="R", aggregate_id="1", payload={}
        )
        summary = run_worker_cycle(lease_owner="w-test", lease_seconds=60)
        self.assertEqual(summary["retried"], 1)
        event = OutboxEvent.objects.get(aggregate_id="1")
        self.assertEqual(event.status, OutboxEvent.Status.RETRY)
        self.assertIn("未登记", event.last_error)


class OutboxCommandOnceTests(TestCase):
    """管理命令 --once：单轮处理后输出摘要并退出。"""

    def test_command_once_processes_event(self):
        enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        out = StringIO()
        call_command("outbox_worker", "--once", stdout=out)
        self.assertIn("认领 1", out.getvalue())
        event = OutboxEvent.objects.get(aggregate_id="1")
        self.assertEqual(event.status, OutboxEvent.Status.SUCCEEDED)

    def test_command_once_quiet_when_idle(self):
        out = StringIO()
        call_command("outbox_worker", "--once", stdout=out)
        self.assertEqual(out.getvalue().strip(), "")


class OutboxMysqlLeaseMutualExclusionTests(TransactionTestCase):
    """MySQL 专用：双线程并发认领互斥（select_for_update(skip_locked=True)）。

    SQLite 下 select_for_update 为 no-op，锁语义不成立，整类跳过；
    权威验证由主会话在 MySQL 侧串行执行（测试库错峰纪律照旧）。
    """

    @unittest.skipUnless(
        connection.vendor == "mysql",
        "SQLite 下 select_for_update 为 no-op，锁语义用例仅 MySQL 执行",
    )
    def test_two_threads_never_claim_same_event(self):
        enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        results: dict = {"w1": [], "w2": []}
        barrier = threading.Barrier(parties=2, timeout=10)

        def worker(name):
            barrier.wait()  # 两线程同时对齐，最大化锁竞争窗口
            leased = lease_outbox_events(limit=10, lease_owner=name, lease_seconds=60)
            results[name] = [e.pk for e in leased]

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("w1", "w2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        # 互斥核心断言：同一事件至多被一个线程认领，两线程认领集合不相交
        w1, w2 = results["w1"], results["w2"]
        self.assertEqual(len(w1) + len(w2), 1)
        self.assertEqual(set(w1) & set(w2), set())


class OutboxConsumerRiskGateTests(TestCase):
    """OWNER-09 消费端硬拒绝（PMS2-FINAL-DECISION-EXEC-002 批次 C）。

    入队端门禁（enqueue ValueError）之外，Worker 分派前二次校验：
    不信任入队端标记；直接写库绕过入队 / 改名重试 / 历史遗留事件均
    在消费端 fail-closed。覆盖任务书 §2.5 测试矩阵 1~6 项（第 7 项
    双引擎由全量回归与 MySQL 专项覆盖）。
    """

    def _persist_raw_event(self, event_type, aggregate_id="1"):
        """直接写库插入事件（模拟绕过 enqueue 门禁的路径）。"""
        return OutboxEvent.objects.create(
            event_type=event_type,
            aggregate_type="R",
            aggregate_id=aggregate_id,
            payload={},
            status=OutboxEvent.Status.PENDING,
        )

    def test_whitelist_event_consumed_normally(self):
        """1. 白名单事件正常处理。"""
        enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="1", payload={}
        )
        summary = run_worker_cycle(lease_owner="w-gate", lease_seconds=60)
        self.assertEqual(summary["succeeded"], 1)
        event = OutboxEvent.objects.get(aggregate_id="1")
        self.assertEqual(event.status, OutboxEvent.Status.SUCCEEDED)

    def test_high_risk_event_rejected_at_worker_even_if_persisted(self):
        """2+4. 高危事件即使绕过入队直接写库，Worker 消费也拒绝。"""
        raw = self._persist_raw_event("billing.payment_settled")
        summary = run_worker_cycle(lease_owner="w-gate", lease_seconds=60)
        self.assertEqual(summary["succeeded"], 0)
        event = OutboxEvent.objects.get(pk=raw.pk)
        self.assertEqual(event.status, OutboxEvent.Status.RETRY)
        self.assertIn("高危", event.last_error)

    def test_unknown_event_type_rejected_by_worker(self):
        """3. 未知（未登记）事件消费端默认拒绝（fail-closed）。"""
        raw = self._persist_raw_event("totally.unknown.event")
        summary = run_worker_cycle(lease_owner="w-gate", lease_seconds=60)
        self.assertEqual(summary["succeeded"], 0)
        event = OutboxEvent.objects.get(pk=raw.pk)
        self.assertEqual(event.status, OutboxEvent.Status.RETRY)
        self.assertIn("未登记", event.last_error)

    def test_direct_db_insert_of_checkout_event_never_executes_side_effect(self):
        """4. 直接调用 Worker / 直接写库不得绕过入队门禁产生副作用。"""
        executed = {"called": False}
        original = OUTBOX_CONSUMERS.get("reservation.checkout")

        def would_settle(event):
            executed["called"] = True

        # 即使有人把高危消费者塞进注册表，门禁也必须先拦下
        OUTBOX_CONSUMERS["reservation.checkout"] = would_settle
        try:
            self._persist_raw_event("reservation.checkout", aggregate_id="9")
            summary = run_worker_cycle(lease_owner="w-gate", lease_seconds=60)
        finally:
            if original is None:
                OUTBOX_CONSUMERS.pop("reservation.checkout", None)
            else:
                OUTBOX_CONSUMERS["reservation.checkout"] = original
        self.assertFalse(executed["called"])
        self.assertEqual(summary["succeeded"], 0)

    def test_case_or_space_rename_cannot_bypass_gate(self):
        """5. 修改事件名称（大小写/空白/前缀变体）不能绕过门禁。"""
        for ev in (
            "Billing.Payment",
            " billing.refund ",
            "NIGHT_AUDIT.CLOSE",
            "Business_Date.Reopen",
            "reservation.checkout ".rstrip().upper(),
        ):
            with self.subTest(event_type=ev):
                raw = self._persist_raw_event(ev, aggregate_id="42")
                summary = run_worker_cycle(lease_owner="w-gate", lease_seconds=60)
                self.assertEqual(summary["succeeded"], 0)
                event = OutboxEvent.objects.get(pk=raw.pk)
                self.assertNotEqual(event.status, OutboxEvent.Status.SUCCEEDED)
                event.delete()

    def test_redelivered_event_no_duplicate_side_effect(self):
        """6. 重复投递不产生重复业务副作用：SUCCEEDED 终态不再被认领。"""
        enqueue_outbox_event(
            event_type=OUTBOX_NOP, aggregate_type="R", aggregate_id="7", payload={}
        )
        first = run_worker_cycle(lease_owner="w-gate", lease_seconds=60)
        second = run_worker_cycle(lease_owner="w-gate", lease_seconds=60)
        self.assertEqual(first["succeeded"], 1)
        self.assertEqual(second["claimed"], 0)
        self.assertEqual(OutboxEvent.objects.filter(aggregate_id="7").count(), 1)

    def test_registry_levels_match_risk_registry(self):
        """登记表一致性：预留类型全部登记为 LOW；高危前缀不可登记为 LOW。"""
        from apps.core.outbox import HIGH_RISK_EVENT_PREFIXES, OUTBOX_EVENT_RISK_REGISTRY

        for event_type in OUTBOX_CONSUMERS:
            self.assertEqual(OUTBOX_EVENT_RISK_REGISTRY.get(event_type), "LOW")
        for event_type, level in OUTBOX_EVENT_RISK_REGISTRY.items():
            if level == "LOW":
                normalized = event_type.strip().lower()
                for prefix in HIGH_RISK_EVENT_PREFIXES:
                    self.assertFalse(
                        normalized.startswith(prefix),
                        f"登记表冲突：{event_type} 既是 LOW 又命中高危前缀 {prefix}",
                    )
