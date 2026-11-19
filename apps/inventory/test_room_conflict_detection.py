"""M4-4/D4-2：排房冲突日检测回归（只报告、不自动改派；MC-007 衔接）。

覆盖：
  * 单房同日两条活跃排房 → 检出冲突并派生 inventory.room_conflict_open 待办；
  * 已取消预订的排房不计数；不同日期/不同房间不误报；
  * 重复扫描幂等：同（物业, 物理房, 住宿日）只一条 OPEN 报告，不重复建单；
  * 复核语义：冲突仍在拒绝闭环；人工修复（摘除多余排房）后复核关闭；
  * 只报告不自动改派：扫描/复核从不改写排房明细与预订状态。
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import override_settings

from apps.api.test_notification_center import NotificationCenterTestBase
from apps.core.models import AuditEvent, WorkItem
from apps.core.services_work_items import complete_work_item
from apps.guests.models import Guest
from apps.identity.models import StaffProfile
from apps.inventory.models import (
    Room,
    RoomAssignmentConflict,
)
from apps.inventory.reconciliation import (
    dismiss_room_conflict,
    review_room_conflict,
    scan_inventory_discrepancies,
)
from apps.reservations.models import Reservation, ReservationRoom


@override_settings(DEBUG=True, DJANGO_ENV="test")
class RoomConflictDetectionTests(NotificationCenterTestBase):
    """D4-2：排房冲突日检测（只报告、不自动改派）+ MC-007 待办衔接。"""

    def setUp(self):
        super().setUp()
        self.conflict_room = Room.objects.create(
            property=self.property,
            number="M44-01",
            room_type=self.room_type,
            floor=1,
        )
        self.cleaner_profile = StaffProfile.objects.get(user=self.housekeeper)

    def _confirmed_reservation(self, *, booking_no=None, check_in=None, nights=2):
        check_in = check_in or self.today
        guest = Guest.objects.create(property=self.property, full_name="冲突测试住客")
        reservation = Reservation.objects.create(
            property=self.property,
            guest=guest,
            booking_no=booking_no or f"M44-{uuid.uuid4().hex[:10].upper()}",
            status=Reservation.Status.CONFIRMED,
            check_in=check_in,
            check_out=check_in + timedelta(days=nights),
            room_count=1,
            total_amount=Decimal("300.00"),
            currency="CNY",
        )
        return reservation

    def _assigned_line(self, reservation, room, *, check_in=None, nights=2):
        check_in = check_in or reservation.check_in
        return ReservationRoom.objects.create(
            property=self.property,
            reservation=reservation,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            check_in=check_in,
            check_out=check_in + timedelta(days=nights),
            amount=Decimal("300.00"),
            assigned_room=room,
        )

    def _scan(self):
        scan_inventory_discrepancies(property_obj=self.property, days_back=30, checked_by=self.dm)
        from apps.core.services_notifications import refresh_operational_todos

        refresh_operational_todos(self.property)

    def _conflict(self):
        return RoomAssignmentConflict.objects.filter(
            property=self.property, status=RoomAssignmentConflict.Status.OPEN
        ).first()

    def _open_todo(self, conflict):
        return WorkItem.objects.get(
            property=self.property,
            task_code="inventory.room_conflict_open",
            source_id=str(conflict.pk),
        )

    def test_double_assigned_same_room_same_day_detected(self):
        """同物理房同住宿日两条活跃排房 → 检出冲突 + MC-007 待办派生。"""
        r1 = self._confirmed_reservation(booking_no="M44-A")
        r2 = self._confirmed_reservation(booking_no="M44-B")
        self._assigned_line(r1, self.conflict_room)
        self._assigned_line(r2, self.conflict_room)
        self._scan()
        conflict = self._conflict()
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.room, self.conflict_room)
        self.assertEqual(conflict.conflict_count, 2)
        self.assertEqual(len(conflict.details), 2)
        todo = self._open_todo(conflict)
        self.assertEqual(todo.status, WorkItem.Status.PENDING)
        self.assertIsNotNone(todo.assigned_group)
        run = conflict.run
        self.assertGreaterEqual(run.room_conflict_count, 1)

    def test_cancelled_reservation_does_not_count(self):
        """已取消预订的排房明细不计入占用（活跃状态口径）。"""
        r_active = self._confirmed_reservation(booking_no="M44-C1")
        r_cancelled = self._confirmed_reservation(booking_no="M44-C2")
        r_cancelled.status = Reservation.Status.CANCELLED
        r_cancelled.save()
        self._assigned_line(r_active, self.conflict_room)
        self._assigned_line(r_cancelled, self.conflict_room)
        self._scan()
        self.assertIsNone(self._conflict(), "取消预订的排房不构成冲突")

    def test_non_overlapping_dates_not_flagged(self):
        """不同住宿日的排房不误报。"""
        r1 = self._confirmed_reservation(booking_no="M44-D1", check_in=self.today, nights=1)
        r2 = self._confirmed_reservation(
            booking_no="M44-D2", check_in=self.today + timedelta(days=5), nights=1
        )
        self._assigned_line(r1, self.conflict_room, check_in=self.today, nights=1)
        self._assigned_line(
            r2, self.conflict_room, check_in=self.today + timedelta(days=5), nights=1
        )
        self._scan()
        self.assertIsNone(self._conflict())

    def test_rescan_idempotent_no_duplicate_conflicts(self):
        """重复扫描幂等：同（物业, 物理房, 住宿日）只一条 OPEN 报告。"""
        r1 = self._confirmed_reservation(booking_no="M44-E1", nights=1)
        r2 = self._confirmed_reservation(booking_no="M44-E2", nights=1)
        self._assigned_line(r1, self.conflict_room, nights=1)
        self._assigned_line(r2, self.conflict_room, nights=1)
        self._scan()
        self._scan()
        self.assertEqual(
            RoomAssignmentConflict.objects.filter(
                property=self.property, status=RoomAssignmentConflict.Status.OPEN
            ).count(),
            1,
            "重复调度不得为同一冲突堆积重复报告行",
        )
        self.assertEqual(
            RoomAssignmentConflict.objects.filter(property=self.property).count(),
            1,
            "重复扫描不得新建第二行（原位更新而非重复建行）",
        )
        conflict = self._conflict()
        todo = self._open_todo(conflict)
        self.assertEqual(todo.status, WorkItem.Status.PENDING)

    def test_review_rejects_while_conflict_persists(self):
        """复核语义：冲突仍在（两条活跃明细）→ 拒绝闭环，任务保持待处理。"""
        r1 = self._confirmed_reservation(booking_no="M44-F1")
        r2 = self._confirmed_reservation(booking_no="M44-F2")
        line1 = self._assigned_line(r1, self.conflict_room)
        self._assigned_line(r2, self.conflict_room)
        self._scan()
        conflict = self._conflict()
        with self.assertRaises(ValueError):
            review_room_conflict(
                property_obj=self.property, conflict_id=conflict.pk, reviewed_by=self.dm
            )
        todo = self._open_todo(conflict)
        todo.refresh_from_db()
        self.assertEqual(todo.status, WorkItem.Status.PENDING)
        # 只报告不自动改派：两条排房明细均未被系统触碰
        line1.refresh_from_db()
        self.assertIsNotNone(line1.assigned_room_id)
        # MC-007 唯一完成入口同样拒绝：源服务失败 → 待办保留 + 错误留痕
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

    def test_review_closes_after_manual_fix(self):
        """人工修复（摘除多余排房）后经 MC-007 复核 → 冲突关闭 + 任务完成。"""
        r1 = self._confirmed_reservation(booking_no="M44-G1", nights=1)
        r2 = self._confirmed_reservation(booking_no="M44-G2", nights=1)
        line1 = self._assigned_line(r1, self.conflict_room, nights=1)
        line2 = self._assigned_line(r2, self.conflict_room, nights=1)
        self._scan()
        conflict = self._conflict()
        todo = self._open_todo(conflict)
        # 人工修复：前台换房——摘除多余排房（系统外的唯一修复工作流）
        line2.assigned_room = None
        line2.save(update_fields=("assigned_room",))
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
        conflict.refresh_from_db()
        self.assertEqual(conflict.status, RoomAssignmentConflict.Status.DISMISSED)
        todo.refresh_from_db()
        self.assertEqual(todo.status, WorkItem.Status.DONE)
        self.assertEqual(todo.resolution, WorkItem.Resolution.COMPLETED)
        self.assertEqual(todo.completed_by, profile)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="inventory.room_conflict_reviewed", entity_id=conflict.pk
            ).exists()
        )
        # 只报告不自动改派：存留明细未被系统触碰
        line1.refresh_from_db()
        self.assertEqual(line1.assigned_room, self.conflict_room)
        # 源冲突关闭后刷新：无残留活跃待办
        self._scan()
        self.assertEqual(
            WorkItem.objects.filter(
                property=self.property,
                task_code="inventory.room_conflict_open",
                status__in=(WorkItem.Status.PENDING, WorkItem.Status.CLAIMED),
            ).count(),
            0,
        )

    def test_dismiss_after_conflict_resolved_elsewhere(self):
        """冲突在他处消除后：dismiss 走终态，重复扫描按判定式不再报。"""
        r1 = self._confirmed_reservation(booking_no="M44-H1", nights=1)
        r2 = self._confirmed_reservation(booking_no="M44-H2", nights=1)
        line1 = self._assigned_line(r1, self.conflict_room, nights=1)
        line2 = self._assigned_line(r2, self.conflict_room, nights=1)
        self._scan()
        conflict = self._conflict()
        line2.assigned_room = None
        line2.save(update_fields=("assigned_room",))
        dismiss_room_conflict(
            property_obj=self.property,
            conflict_id=conflict.pk,
            dismissed_by=self.dm,
            reason="人工换房后冲突消除",
        )
        self._scan()
        self.assertEqual(
            RoomAssignmentConflict.objects.filter(
                property=self.property, status=RoomAssignmentConflict.Status.OPEN
            ).count(),
            0,
            "冲突消除后重复扫描不得再报同一冲突",
        )
        self.assertEqual(line1.assigned_room, self.conflict_room)
