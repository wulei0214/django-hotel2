"""V12-07：RoomAssignmentConflict 并发扫描防护（MySQL 权威环境）。

问题：旧实现是 Python 层"先查 OPEN 行、没有就建"。两个扫描运行同时判定
同一（物理房, 住宿日）无 OPEN 行时，会各插一条 OPEN 差异——同一真实冲突
被重复报告，人工处置时出现两份待办。

现由数据库可空唯一键 `open_key` 仲裁（MySQL 8 不支持条件唯一约束，沿用本
仓库"可空唯一键 + save() 维护"口径）：只有一条能插入成功，另一条撞唯一键后
回落到"原位更新该行"。本模块验证：

1. 两个运行并发扫描 → 只产生 1 条 OPEN 差异；
2. 顺序重复扫描 → 不新增（既有行为回归）；
3. 人工关闭后冲突仍在 → 重新扫描会再次建 OPEN（键位已在 DISMISSED 时释放）。

SQLite 无行锁与真正的并发插入语义，故并发用例仅在 MySQL 执行（任务书
§2.3：SQLite 不能替代 MySQL 权威验证）。
"""

import threading
from datetime import timedelta
from decimal import Decimal

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.core.models import Property
from apps.guests.models import Guest
from apps.inventory.models import (
    InventoryReconciliationRun,
    RatePlan,
    Room,
    RoomAssignmentConflict,
    RoomType,
)
from apps.inventory.reconciliation import (
    dismiss_room_conflict,
    scan_inventory_discrepancies,
)
from apps.reservations.models import Reservation, ReservationRoom


class _ConflictFixture:
    """最小夹具：物业 + 房型 + 房价计划 + 物理房 + 两条同日重复排房。"""

    def build(self):
        self.property = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "默认物业"}
        )[0]
        self.room_type = RoomType.objects.create(
            property=self.property, code="V1207-KING", name="并发测试大床房", capacity=2
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="V1207-BAR",
            name="并发测试门市价",
            valid_from=timezone.localdate() - timedelta(days=1),
        )
        self.room = Room.objects.create(
            property=self.property, number="V1207-01", room_type=self.room_type, floor=1
        )
        self.today = timezone.localdate()
        self._assign(self._reservation("V1207-A"))
        self._assign(self._reservation("V1207-B"))

    def _reservation(self, booking_no):
        guest = Guest.objects.create(property=self.property, full_name="并发测试住客")
        return Reservation.objects.create(
            property=self.property,
            guest=guest,
            booking_no=booking_no,
            status=Reservation.Status.CONFIRMED,
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            room_count=1,
            total_amount=Decimal("300.00"),
            currency="CNY",
        )

    def _assign(self, reservation):
        ReservationRoom.objects.create(
            property=self.property,
            reservation=reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            amount=Decimal("300.00"),
            assigned_room=self.room,
        )


class RoomConflictConcurrencyTests(TransactionTestCase):
    """并发扫描只允许一条有效差异（MySQL 权威）。"""

    reset_sequences = True

    def setUp(self):
        self.fixture = _ConflictFixture()
        self.fixture.build()

    def _open_conflicts(self):
        # 夹具住了两晚 → 冲突日有 today / today+1 两天，按住宿日收窄到 today。
        return RoomAssignmentConflict.objects.filter(
            property=self.fixture.property,
            room=self.fixture.room,
            stay_date=self.fixture.today,
            status=RoomAssignmentConflict.Status.OPEN,
        )

    def test_concurrent_scans_create_single_open_conflict(self):
        if connection.vendor != "mysql":
            self.skipTest("并发唯一性以 MySQL 为权威环境（SQLite 无并发插入语义）")

        runs = 2
        barrier = threading.Barrier(runs)
        errors = []

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                scan_inventory_discrepancies(
                    property_obj=self.fixture.property, days_back=30, checked_by=None
                )
            except Exception as exc:  # pragma: no cover - 失败时输出真实原因
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(runs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(self._open_conflicts().count(), 1, "同一（房, 住宿日）只允许一条有效差异")
        conflict = self._open_conflicts().get()
        self.assertEqual(conflict.conflict_count, 2)
        self.assertEqual(
            conflict.open_key, f"{self.fixture.room.pk}:{self.fixture.today.isoformat()}"
        )

    def test_sequential_rescan_does_not_duplicate(self):
        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        first = self._open_conflicts().get()
        first_run_id = first.run_id
        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        self.assertEqual(self._open_conflicts().count(), 1)
        # 首报运行归属不变（原位更新，不迁移到新运行）
        self.assertEqual(self._open_conflicts().get().run_id, first_run_id)

    def test_dismissed_conflict_reopens_when_still_present(self):
        """关闭后冲突仍存在 → 下次扫描重新建 OPEN（键位在 DISMISSED 已释放）。"""
        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        conflict = self._open_conflicts().get()
        dismiss_room_conflict(
            property_obj=self.fixture.property,
            conflict_id=conflict.pk,
            dismissed_by=None,
            reason="人工确认误报（测试）",
        )
        conflict.refresh_from_db()
        self.assertEqual(conflict.status, RoomAssignmentConflict.Status.DISMISSED)
        self.assertIsNone(conflict.open_key, "DISMISSED 必须释放唯一键位")

        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        self.assertEqual(self._open_conflicts().count(), 1)
        self.assertNotEqual(self._open_conflicts().get().pk, conflict.pk)

    def test_scan_does_not_modify_assignment_rows(self):
        """扫描只读：排房明细/预订状态/物理房归属不得被改写。"""
        before = list(
            ReservationRoom.objects.filter(property=self.fixture.property).values(
                "pk", "assigned_room_id", "check_in", "check_out", "reservation__status"
            )
        )
        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        after = list(
            ReservationRoom.objects.filter(property=self.fixture.property).values(
                "pk", "assigned_room_id", "check_in", "check_out", "reservation__status"
            )
        )
        self.assertEqual(before, after)


class RoomConflictScanRecoveryTests(_ConflictFixture, TransactionTestCase):
    """G-M4-4-CONC-01A：扫描中断恢复（V1.3 任务书 §9.2-A）。

    扫描全程 @transaction.atomic——中断即整体回滚，不残留部分差异；
    重启扫描不产生重复 OPEN、不丢失真实冲突、人工处理记录不被覆盖。
    """

    def setUp(self):
        self.fixture = _ConflictFixture()
        self.fixture.build()

    def _conflict_rows(self):
        return RoomAssignmentConflict.objects.filter(property=self.fixture.property)

    def _open_conflicts(self):
        return RoomAssignmentConflict.objects.filter(
            property=self.fixture.property,
            room=self.fixture.room,
            stay_date=self.fixture.today,
            status=RoomAssignmentConflict.Status.OPEN,
        )

    def test_interruption_rolls_back_and_restart_recovers(self):
        from unittest import mock

        from apps.inventory.reconciliation import _conflict_details_snapshot

        # 夹具住了两晚 → 检测到两个冲突日；让第二个冲突日在快照阶段抛出，
        # 模拟扫描进行到一半被异常中断。
        real = _conflict_details_snapshot
        calls = {"n": 0}

        def interrupted(line_ids):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("扫描中断（模拟进程被杀/异常）")
            return real(line_ids)

        with (
            mock.patch(
                "apps.inventory.reconciliation._conflict_details_snapshot",
                side_effect=interrupted,
            ),
            self.assertRaises(RuntimeError),
        ):
            scan_inventory_discrepancies(
                property_obj=self.fixture.property, days_back=30, checked_by=None
            )
        # 中断 → 原子回滚：不残留部分差异行、也不残留运行记录
        self.assertEqual(self._conflict_rows().count(), 0)
        # 该模型 objects 管理器存在 mypy has-type 循环推断（无 django-stubs），
        # 运行时属性恒存在——窄化 ignore 并注明原因
        self.assertEqual(
            InventoryReconciliationRun.objects.count(), 0  # type: ignore[has-type]
        )

        # 重启扫描：真实冲突不丢失，正常建行且无重复
        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        self.assertGreaterEqual(self._conflict_rows().count(), 1)
        self.assertEqual(
            self._conflict_rows().filter(status=RoomAssignmentConflict.Status.OPEN).count(),
            self._conflict_rows().count(),
        )
        # 幂等：重启后的第二次扫描不新增
        before = self._conflict_rows().count()
        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        self.assertEqual(self._conflict_rows().count(), before)

    def test_restart_preserves_dismissed_history_records(self):
        """人工关闭（DISMISSED）的历史记录在重启扫描后不被覆盖或删除；
        冲突仍真实存在时按既有口径另建新 OPEN 行（键位已释放）。"""
        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        dismissed = self._open_conflicts().get()
        dismiss_room_conflict(
            property_obj=self.fixture.property,
            conflict_id=dismissed.pk,
            dismissed_by=None,
            reason="人工核对为同屋换房过渡（测试留痕）",
        )
        dismissed.refresh_from_db()
        dismissed_pk = dismissed.pk
        dismissed_status = dismissed.status
        dismissed_details = dismissed.details

        scan_inventory_discrepancies(
            property_obj=self.fixture.property, days_back=30, checked_by=None
        )
        dismissed.refresh_from_db()
        self.assertEqual(dismissed.pk, dismissed_pk)
        self.assertEqual(dismissed.status, dismissed_status)
        self.assertEqual(dismissed.details, dismissed_details)
        # 冲突仍存在 → 另建新 OPEN（既有口径，重开不覆盖历史）
        self.assertEqual(self._open_conflicts().count(), 1)
        self.assertNotEqual(self._open_conflicts().get().pk, dismissed_pk)


class RoomConflictScanVsManualFixTests(_ConflictFixture, TransactionTestCase):
    """G-M4-4-CONC-01B：扫描与人工修复并发（确定性交错模拟）。

    扫描在自身事务内对占用计数取快照；人工修复（换房消除冲突）在快照
    之后、扫描提交之前发生。验证三件事：
      1. 本次扫描按快照如实报告（不读到提交后的新世界）；
      2. 扫描不自动关闭任何 OPEN 差异（含已被人工修复消除的）；
      3. 下一轮全量扫描收敛：冲突已消除则不再新建，人工结果不被修改。
    """

    def setUp(self):
        self.fixture = _ConflictFixture()
        self.fixture.build()

    def test_stale_snapshot_scan_then_manual_fix_converges(self):
        from unittest import mock

        from apps.inventory import reconciliation

        prop = self.fixture.property
        room = self.fixture.room
        today = self.fixture.today

        # 1) 人工修复发生在"扫描快照之后"：先取快照，再换房消除冲突
        stale_snapshot = reconciliation._room_occupancy_counts(
            property_obj=prop, start=today, end=today + timedelta(days=1)
        )
        self.assertTrue(
            any(bucket["count"] >= 2 for bucket in stale_snapshot.values()),
            "快照必须捕获到修复前的真实冲突",
        )
        # 人工修复：把其中一条排房换到另一间空房（换房 = 正式业务操作）
        spare_room = Room.objects.create(
            property=prop,
            number="V1207-02",
            room_type=self.fixture.room_type,
            floor=1,
        )
        second_line = (
            ReservationRoom.objects.filter(property=prop, assigned_room_id=room.pk)
            .order_by("-pk")
            .first()
        )
        assert second_line is not None
        second_line.assigned_room = spare_room
        second_line.save(update_fields=("assigned_room",))

        # 2) 扫描带着修复前的旧快照运行（模拟并发交错：扫描读到旧世界）
        with mock.patch.object(
            reconciliation,
            "_room_occupancy_counts",
            return_value=stale_snapshot,
        ):
            scan_inventory_discrepancies(property_obj=prop, days_back=30, checked_by=None)
        # 扫描按快照如实报告——OPEN 差异存在（包含陈旧数据）
        self.assertEqual(
            RoomAssignmentConflict.objects.filter(
                property=prop,
                room=room,
                stay_date=today,
                status=RoomAssignmentConflict.Status.OPEN,
            ).count(),
            1,
        )
        # 扫描未改写任何排房明细（只读口径）
        self.assertEqual(
            ReservationRoom.objects.filter(pk=second_line.pk, assigned_room=spare_room).count(),
            1,
        )

        # 3) 下一轮全量扫描（新快照）：冲突已消除 → 不新建、不自动关闭
        #    旧 OPEN（关闭须人工确认，§9.3 禁止自动关闭）
        scan_inventory_discrepancies(property_obj=prop, days_back=30, checked_by=None)
        self.assertEqual(
            RoomAssignmentConflict.objects.filter(
                property=prop,
                room=room,
                stay_date=today,
                status=RoomAssignmentConflict.Status.OPEN,
            ).count(),
            1,
            "扫描不得自动关闭 OPEN 差异（须人工确认消除）",
        )
        # 人工确认关闭后，第三轮扫描收敛：不再新建
        stale = RoomAssignmentConflict.objects.get(
            property=prop,
            room=room,
            stay_date=today,
            status=RoomAssignmentConflict.Status.OPEN,
        )
        dismiss_room_conflict(
            property_obj=prop,
            conflict_id=stale.pk,
            dismissed_by=None,
            reason="人工确认换房已消除冲突",
        )
        scan_inventory_discrepancies(property_obj=prop, days_back=30, checked_by=None)
        self.assertEqual(
            RoomAssignmentConflict.objects.filter(
                property=prop,
                room=room,
                stay_date=today,
                status=RoomAssignmentConflict.Status.OPEN,
            ).count(),
            0,
            "冲突真实消除后扫描收敛：不再新建 OPEN",
        )
