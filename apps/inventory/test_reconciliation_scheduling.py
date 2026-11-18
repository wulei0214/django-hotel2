"""M4-3/D4-3+D4-4：库存日历滚动自动化 + 对账扫描调度化 + MC-007 WorkItem 衔接。

覆盖（派发单 M4-3 验收口径）：
  * 日历覆盖告警触发口径：MAX(stay_date) < 今天+7（物业时区）触发告警并派生
    待办；覆盖达标不触发；未建立日历（无库存行）视为覆盖不足；滚动同步补齐
    后待办随源事实消失自动销项（SOURCE_RESOLVED，不新增通知表）；
  * 对账差异 → WorkItem：调度命令 run_inventory_reconciliation 产出 OPEN 差异
    并经 task_registry 任务源 inventory.discrepancy_open 派生待办（权限=
    inventory.maintenance_manage 既有码，不新增权限码）；
  * 重复扫描幂等：同一（物业, 房型, 住宿日）只一条 OPEN 差异报告与一条待办；
    重算值变化原位更新，不重复建单；
  * 待办完成=调源领域服务：review_discrepancy 按唯一重算口径复核，差异已消除
    才经 dismiss_discrepancy 关闭；差异仍在拒绝闭环（不改状态字段、不改库存
    权威计数、不触碰排房明细——排房冲突只报告、不自动改派）；
  * 滚动同步调度口径（门禁项①）：--all-room-types 与单房型共用同一
    _sync_room_type_calendar 容量校验（复用而非复制）；无启用房间/容量校验
    未过的房型在调度滚动中跳过告警、不中断其他房型，单房型模式保持显式失败；
  * 覆盖阈值边界：MAX(stay_date) == 今天+7 达标不告警（含边界）。
"""

import io
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.management import CommandError, call_command
from django.test import override_settings

from apps.api.test_notification_center import NotificationCenterTestBase
from apps.core.models import AuditEvent, Notification, WorkItem
from apps.core.services_notifications import refresh_operational_todos
from apps.core.services_work_items import complete_work_item
from apps.core.task_registry import get_spec
from apps.guests.models import Guest
from apps.identity.models import StaffProfile
from apps.inventory.models import (
    InventoryDiscrepancy,
    InventoryReconciliationRun,
    Room,
    RoomType,
    RoomTypeInventory,
)
from apps.inventory.reconciliation import dismiss_discrepancy
from apps.inventory.services import calendar_coverage_gaps
from apps.reservations.models import InventoryHold, Reservation, ReservationRoom


@override_settings(DEBUG=True, DJANGO_ENV="test")
class CalendarCoverageAlertTests(NotificationCenterTestBase):
    """D4-3：日历覆盖告警触发口径、滚动同步补齐后的自动销项。"""

    def _trim_calendar(self, last_offset):
        """把夹具房型日历截到 今天+offset（删除更远的库存行）。"""
        RoomTypeInventory.objects.filter(
            property=self.property,
            room_type=self.room_type,
            stay_date__gt=self.today + timedelta(days=last_offset),
        ).delete()

    def _coverage_todo(self):
        return WorkItem.objects.get(
            property=self.property,
            task_code="inventory.calendar_coverage",
            source_id=str(self.room_type.pk),
        )

    def test_coverage_below_horizon_triggers_alert_and_todo(self):
        self._trim_calendar(3)  # 只覆盖到 今天+3 < 今天+7
        out = io.StringIO()
        call_command("check_inventory_calendar_coverage", stdout=out)
        self.assertIn("覆盖不足", out.getvalue())
        self.assertIn(self.room_type.code, out.getvalue())
        todo = self._coverage_todo()
        self.assertEqual(todo.status, WorkItem.Status.PENDING)
        self.assertEqual(todo.assigned_group.name, "值班经理")
        self.assertEqual(todo.link, "/manage/rooms/")
        # 通知投递给 值班经理 + 店长（inventory.maintenance_manage 持有者口径）
        audiences = set(
            Notification.objects.filter(
                property=self.property,
                dedupe_key__startswith="inventory.calendar_coverage|",
                role_group__isnull=False,
            ).values_list("role_group__name", flat=True)
        )
        self.assertEqual(audiences, {"值班经理", "店长"})

    def test_coverage_meeting_horizon_is_silent(self):
        # 夹具日历已滚动到 今天+30 ≥ 今天+7：不告警、不生成待办
        out = io.StringIO()
        call_command("check_inventory_calendar_coverage", stdout=out)
        self.assertNotIn("覆盖不足", out.getvalue())
        self.assertFalse(
            WorkItem.objects.filter(
                property=self.property, task_code="inventory.calendar_coverage"
            ).exists()
        )
        self.assertFalse(
            Notification.objects.filter(
                property=self.property,
                dedupe_key__startswith="inventory.calendar_coverage|",
            ).exists()
        )

    def test_missing_calendar_counts_as_gap(self):
        RoomTypeInventory.objects.filter(property=self.property, room_type=self.room_type).delete()
        out = io.StringIO()
        call_command("check_inventory_calendar_coverage", stdout=out)
        self.assertIn("覆盖不足", out.getvalue())
        self.assertIn("未建立", out.getvalue())
        self.assertTrue(
            WorkItem.objects.filter(
                property=self.property, task_code="inventory.calendar_coverage"
            ).exists()
        )

    def test_alert_resolves_after_rolling_sync_extends_calendar(self):
        self._trim_calendar(3)
        call_command("check_inventory_calendar_coverage")
        todo = self._coverage_todo()
        self.assertEqual(todo.status, WorkItem.Status.PENDING)

        # 滚动同步补齐（复用同一 sync 命令的 --all-room-types 调度口径）
        call_command(
            "sync_inventory_calendar",
            property_code="DEFAULT",
            all_room_types=True,
            start_date=self.today.isoformat(),
            end_date=(self.today + timedelta(days=30)).isoformat(),
        )
        out = io.StringIO()
        call_command("check_inventory_calendar_coverage", stdout=out)
        self.assertNotIn("覆盖不足", out.getvalue())
        todo.refresh_from_db()
        self.assertEqual(todo.status, WorkItem.Status.DONE)
        self.assertEqual(todo.resolution, WorkItem.Resolution.SOURCE_RESOLVED)

    def test_task_specs_use_existing_inventory_permission(self):
        """任务源登记约束：复用既有权限码；覆盖待办为提示型（无执行器）。"""
        spec = get_spec("inventory.discrepancy_open")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.permission, "inventory.maintenance_manage")
        self.assertIsNotNone(spec.executor)
        coverage = get_spec("inventory.calendar_coverage")
        self.assertEqual(coverage.permission, "inventory.maintenance_manage")
        self.assertIsNone(
            coverage.executor,
            "覆盖补齐只能走唯一同步命令，不得从待办层直写库存",
        )

    def test_coverage_horizon_boundary_is_inclusive(self):
        """阈值边界：MAX(stay_date)==今天+7 视为达标；今天+6 才是缺口。"""
        RoomTypeInventory.objects.filter(
            property=self.property,
            room_type=self.room_type,
            stay_date__gt=self.today + timedelta(days=7),
        ).delete()
        self.assertEqual(calendar_coverage_gaps(self.property, today=self.today), [])
        RoomTypeInventory.objects.filter(
            property=self.property,
            room_type=self.room_type,
            stay_date__gt=self.today + timedelta(days=6),
        ).delete()
        gaps = calendar_coverage_gaps(self.property, today=self.today)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["max_stay"], self.today + timedelta(days=6))
        self.assertEqual(gaps[0]["horizon"], self.today + timedelta(days=7))

    def test_coverage_command_rejects_invalid_horizon(self):
        with self.assertRaises(CommandError):
            call_command("check_inventory_calendar_coverage", horizon_days=0)
        with self.assertRaises(CommandError):
            call_command("check_inventory_calendar_coverage", horizon_days=366)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ReconciliationSchedulingTests(NotificationCenterTestBase):
    """D4-4：对账扫描调度化 + 差异 WorkItem 衔接（MC-007 机制复用）。"""

    def _today_row(self):
        return RoomTypeInventory.objects.get(
            property=self.property, room_type=self.room_type, stay_date=self.today
        )

    def _dirty_hold(self, row, booking_no=None):
        """制造差异：账面 held=0，但存在 HELD 冻结明细行（重算真值 +1）。

        预订保持 PENDING（待支付冻结口径）：明细行不计 sold，只产生
        held 不一致，便于按 held_count 精确断言。
        """
        guest = Guest.objects.create(property=self.property, full_name="调度测试住客")
        reservation = Reservation.objects.create(
            property=self.property,
            guest=guest,
            booking_no=booking_no or f"M43-{uuid.uuid4().hex[:10].upper()}",
            status=Reservation.Status.PENDING,
            check_in=row.stay_date,
            check_out=row.stay_date + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("200.00"),
            currency="CNY",
        )
        room_line = ReservationRoom.objects.create(
            property=self.property,
            reservation=reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=row.stay_date,
            check_out=row.stay_date + timedelta(days=1),
            amount=Decimal("200.00"),
        )
        InventoryHold.objects.create(
            property=self.property,
            room_type=self.room_type,
            stay_date=row.stay_date,
            reservation_room=room_line,
            status=InventoryHold.HoldStatus.HELD,
        )
        return reservation

    def _scan(self):
        out = io.StringIO()
        call_command("run_inventory_reconciliation", stdout=out)
        return out.getvalue()

    def _open_discrepancy(self):
        return InventoryDiscrepancy.objects.get(
            property=self.property, status=InventoryDiscrepancy.Status.OPEN
        )

    def test_scan_command_creates_discrepancy_and_work_item(self):
        row = self._today_row()
        self._dirty_hold(row)
        output = self._scan()
        self.assertIn("待办 inventory.discrepancy_open", output)
        run = InventoryReconciliationRun.objects.order_by("-pk").first()
        self.assertEqual(run.diff_count, 1)
        disc = self._open_discrepancy()
        self.assertEqual(disc.room_type, self.room_type)
        self.assertEqual(disc.stay_date, self.today)
        todo = WorkItem.objects.get(
            property=self.property,
            task_code="inventory.discrepancy_open",
            source_id=str(disc.pk),
        )
        self.assertEqual(todo.status, WorkItem.Status.PENDING)
        self.assertEqual(todo.link, "/manage/rooms/")
        # 扫描只报告：权威计数不被扫描覆盖
        row.refresh_from_db()
        self.assertEqual(row.held, 0)

    def test_repeated_scan_is_idempotent_no_duplicate_rows_or_todos(self):
        row = self._today_row()
        self._dirty_hold(row)
        self._scan()
        self._scan()
        self.assertEqual(
            InventoryDiscrepancy.objects.filter(
                property=self.property,
                status=InventoryDiscrepancy.Status.OPEN,
            ).count(),
            1,
            "重复调度不得为同一差异堆积重复报告行",
        )
        self.assertEqual(
            WorkItem.objects.filter(
                property=self.property, task_code="inventory.discrepancy_open"
            ).count(),
            1,
            "重复调度不得重复建单（MC-007 dedupe_key 一个源事实一条）",
        )
        second_run = InventoryReconciliationRun.objects.order_by("-pk").first()
        self.assertEqual(second_run.diff_count, 1, "差异仍在时仍应计入当批扫描")

    def test_repeated_scan_updates_existing_report_in_place(self):
        row = self._today_row()
        self._dirty_hold(row)
        self._scan()
        first = self._open_discrepancy()
        self.assertEqual(first.held_count, 1)
        # 差异值变化：同住宿日再冻结一间 → 重算真值 2，原报告应原位更新
        self._dirty_hold(row)
        self._scan()
        self.assertEqual(
            InventoryDiscrepancy.objects.filter(
                property=self.property,
                status=InventoryDiscrepancy.Status.OPEN,
            ).count(),
            1,
        )
        first.refresh_from_db()
        self.assertEqual(first.pk, self._open_discrepancy().pk)
        self.assertEqual(first.held_count, 2)
        self.assertEqual(first.sold_count, 0)

    def test_workitem_completion_closes_discrepancy_via_review_service(self):
        row = self._today_row()
        self._dirty_hold(row)
        self._scan()
        disc = self._open_discrepancy()
        todo = WorkItem.objects.get(
            property=self.property,
            task_code="inventory.discrepancy_open",
            source_id=str(disc.pk),
        )
        # 差异已消除：权威计数与重算真值一致（人工经其他入口修复后复核场景）
        RoomTypeInventory.objects.filter(pk=row.pk).update(held=1)
        profile = StaffProfile.objects.get(user=self.dm)
        _, changed = complete_work_item(
            property_obj=self.property,
            work_item_id=todo.pk,
            profile=profile,
            actor=self.dm,
            payload={},
            request=None,
        )
        self.assertTrue(changed)
        disc.refresh_from_db()
        self.assertEqual(disc.status, InventoryDiscrepancy.Status.DISMISSED)
        todo.refresh_from_db()
        self.assertEqual(todo.status, WorkItem.Status.DONE)
        self.assertEqual(todo.resolution, WorkItem.Resolution.COMPLETED)
        self.assertEqual(todo.completed_by, profile)
        self.assertTrue(
            AuditEvent.objects.filter(action="inventory.disc_reviewed", entity_id=disc.pk).exists()
        )
        # 源差异关闭后刷新：无残留活跃待办/通知
        refresh_operational_todos(self.property)
        self.assertEqual(
            WorkItem.objects.filter(
                property=self.property,
                task_code="inventory.discrepancy_open",
                status__in=(WorkItem.Status.PENDING, WorkItem.Status.CLAIMED),
            ).count(),
            0,
        )

    def test_workitem_completion_rejected_while_divergence_persists(self):
        row = self._today_row()
        reservation = self._dirty_hold(row)
        self._scan()
        disc = self._open_discrepancy()
        todo = WorkItem.objects.get(
            property=self.property,
            task_code="inventory.discrepancy_open",
            source_id=str(disc.pk),
        )
        profile = StaffProfile.objects.get(user=self.dm)
        with self.assertRaises(ValueError):
            complete_work_item(
                property_obj=self.property,
                work_item_id=todo.pk,
                profile=profile,
                actor=self.dm,
                payload={},
                request=None,
            )
        todo.refresh_from_db()
        self.assertEqual(todo.status, WorkItem.Status.PENDING)
        self.assertEqual(todo.last_error_code, "SOURCE_SERVICE_FAILED")
        disc.refresh_from_db()
        self.assertEqual(disc.status, InventoryDiscrepancy.Status.OPEN)
        # 只报告不修复：库存权威计数与排房明细均不被待办完成动作改写
        row.refresh_from_db()
        self.assertEqual(row.held, 0)
        self.assertEqual(row.sold, 0)
        self.assertEqual(ReservationRoom.objects.filter(reservation=reservation).count(), 1)
        room_line = ReservationRoom.objects.get(reservation=reservation)
        self.assertIsNone(
            room_line.assigned_room_id,
            "排房冲突只报告、不自动改派：待办复核不得触碰排房明细",
        )
        self.assertEqual(
            Reservation.objects.get(pk=reservation.pk).status,
            Reservation.Status.PENDING,
            "待办复核不改源预订状态（改派/确认走唯一前台履约工作流）",
        )

    def test_source_resolved_when_discrepancy_closed_elsewhere(self):
        row = self._today_row()
        self._dirty_hold(row)
        self._scan()
        disc = self._open_discrepancy()
        todo = WorkItem.objects.get(
            property=self.property,
            task_code="inventory.discrepancy_open",
            source_id=str(disc.pk),
        )
        dismiss_discrepancy(
            property_obj=self.property,
            discrepancy_id=disc.pk,
            dismissed_by=self.dm,
            reason="误报关闭",
        )
        refresh_operational_todos(self.property)
        todo.refresh_from_db()
        self.assertEqual(todo.status, WorkItem.Status.DONE)
        self.assertEqual(todo.resolution, WorkItem.Resolution.SOURCE_RESOLVED)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class SyncCalendarRollingTests(NotificationCenterTestBase):
    """D4-3：sync_inventory_calendar 滚动调度口径（门禁项①同口径复用）。

    --all-room-types（调度每日 02:30）与 --room-type-code（人工/修复流）共用
    ``_sync_room_type_calendar``：同一容量校验（total ≥ Hold/Sold/OOS 合计）、
    同一 get_or_create 幂等语义；调度滚动不因单一房型失败中断，单房型模式
    保持既有显式失败语义。
    """

    def _window(self):
        return self.today.isoformat(), (self.today + timedelta(days=30)).isoformat()

    def _make_twin_room_type(self, *, with_rooms=True):
        twin = RoomType.objects.create(
            property=self.property, code="E2E-TWIN", name="E2E 测试双床房", capacity=2
        )
        for index in range(2 if with_rooms else 0):
            Room.objects.create(
                property=self.property,
                room_type=twin,
                number=f"E2E-TWN-{index + 1}",
                floor="8",
                occupancy_status=Room.OccupancyStatus.VACANT,
                housekeeping_status=Room.HousekeepingStatus.CLEAN,
                maintenance_status=Room.MaintenanceStatus.NORMAL,
            )
        return twin

    def test_mode_arguments_are_mutually_exclusive(self):
        start, end = self._window()
        with self.assertRaises(CommandError):
            call_command(
                "sync_inventory_calendar",
                property_code="DEFAULT",
                room_type_code="E2E-KING",
                all_room_types=True,
                start_date=start,
                end_date=end,
            )
        with self.assertRaises(CommandError):
            call_command(
                "sync_inventory_calendar",
                property_code="DEFAULT",
                start_date=start,
                end_date=end,
            )

    def test_all_room_types_skips_roomless_type_and_keeps_rolling_others(self):
        roomless = self._make_twin_room_type(with_rooms=False)
        start, end = self._window()
        out = io.StringIO()
        call_command(
            "sync_inventory_calendar",
            property_code="DEFAULT",
            all_room_types=True,
            start_date=start,
            end_date=end,
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn("[跳过]", text)
        self.assertIn(roomless.code, text)
        self.assertIn("未完成房型", text)
        # 其他启用房型不受单一房型失败影响，窗口仍然滚满
        self.assertTrue(
            RoomTypeInventory.objects.filter(
                property=self.property,
                room_type=self.room_type,
                stay_date=self.today + timedelta(days=30),
            ).exists()
        )

    def test_roomless_type_fails_loudly_in_single_mode(self):
        roomless = self._make_twin_room_type(with_rooms=False)
        start, end = self._window()
        with self.assertRaises(CommandError):
            call_command(
                "sync_inventory_calendar",
                property_code="DEFAULT",
                room_type_code=roomless.code,
                start_date=start,
                end_date=end,
            )

    def test_capacity_conflict_skips_type_in_all_mode_and_raises_in_single_mode(self):
        """容量校验同一口径：滚动目标总量 < 已占用合计 → 跳过/显式失败，不改占用。"""
        twin = self._make_twin_room_type(with_rooms=True)
        # 只留 1 间启用房，并预置 sold=2 的权威行 → total(1) < held+sold(2)
        Room.objects.filter(property=self.property, room_type=twin).exclude(
            number="E2E-TWN-1"
        ).update(is_active=False)
        far_day = self.today + timedelta(days=30)
        RoomTypeInventory.objects.create(
            property=self.property,
            room_type=twin,
            stay_date=far_day,
            total_rooms=5,
            sold=2,
        )
        start, end = self._window()

        # 调度滚动：该房型跳过告警，其他房型正常滚动，命令不失败
        out = io.StringIO()
        call_command(
            "sync_inventory_calendar",
            property_code="DEFAULT",
            all_room_types=True,
            start_date=start,
            end_date=end,
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn("[跳过]", text)
        self.assertIn(twin.code, text)
        row = RoomTypeInventory.objects.get(
            property=self.property, room_type=twin, stay_date=far_day
        )
        self.assertEqual(row.sold, 2, "滚动同步不得改写既有占用权威计数")

        # 单房型人工模式：同一校验显式失败（既有语义）
        with self.assertRaises(CommandError):
            call_command(
                "sync_inventory_calendar",
                property_code="DEFAULT",
                room_type_code=twin.code,
                start_date=start,
                end_date=end,
            )

    def test_repeat_roll_is_idempotent_and_bumps_version(self):
        """调度幂等：重复滚动不重复建行；已有行按容量校验原位更新并推进版本。"""
        start, end = self._window()
        call_command(
            "sync_inventory_calendar",
            property_code="DEFAULT",
            all_room_types=True,
            start_date=start,
            end_date=end,
        )
        row = RoomTypeInventory.objects.get(
            property=self.property, room_type=self.room_type, stay_date=self.today
        )
        version_first = row.version
        call_command(
            "sync_inventory_calendar",
            property_code="DEFAULT",
            all_room_types=True,
            start_date=start,
            end_date=end,
        )
        self.assertEqual(
            RoomTypeInventory.objects.filter(
                property=self.property, room_type=self.room_type
            ).count(),
            31,
            "重复滚动不得为同一窗口重复建行",
        )
        row.refresh_from_db()
        self.assertEqual(row.version, version_first + 1)
        # 总量按物理房间主数据重推（E2E 夹具 3 间启用房），不保留夹具旧值
        self.assertEqual(row.total_rooms, 3)
