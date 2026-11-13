"""A8 历史快照回填：Dry-run / 幂等 / 审计 / 可回滚 专项测试。"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.inventory.models import RatePlan, RoomType
from apps.reservations.backfill_services import (
    backfill_early_departure_snapshots,
    clear_backfilled_early_departure_snapshots,
)
from apps.reservations.models import (
    EarlyDeparturePolicy,
    Reservation,
    ReservationRoom,
)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class EarlyDepartureSnapshotBackfillTests(TestCase):
    def setUp(self):
        self.prop = Property.objects.create(
            code=f"EDB-{uuid.uuid4().hex[:8]}", name="回填测试物业")
        self.rt = RoomType.objects.create(
            property=self.prop, code="ED-RT", name="ED房型", capacity=2)

    def _make_legacy_booking(self, rate_plan, policy_type=None):
        """创建预订+明细，early_departure_snapshot 保持 NULL（模拟迁移前 legacy 行）。"""
        guest = Guest.objects.create(property=self.prop, full_name="ED回填客")
        res = Reservation.objects.create(
            property=self.prop, guest=guest,
            booking_no=f"ED-{uuid.uuid4().hex[:12]}",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("600.00"), currency="CNY")
        room = ReservationRoom.objects.create(
            property=self.prop, reservation=res, room_type=self.rt,
            rate_plan=rate_plan,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal("600.00"))
        if policy_type is not None:
            EarlyDeparturePolicy.objects.create(
                rate_plan=rate_plan, policy_type=policy_type, is_active=True)
        return res, room

    def test_dry_run_does_not_write(self):
        rate = RatePlan.objects.create(
            property=self.prop, code="ED-R1", name="ED1",
            valid_from=timezone.localdate())
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate, policy_type="FIRST_NIGHT", is_active=True)
        _, room = self._make_legacy_booking(rate)
        summary = backfill_early_departure_snapshots(
            dry_run=True, property_obj=self.prop)
        room.refresh_from_db()
        self.assertIsNone(room.early_departure_snapshot)
        self.assertEqual(summary["would_backfill"], 1)
        self.assertEqual(summary["applied"], False)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="reservation.early_departure_policy_backfilled").count(), 0)

    def test_apply_backfills_with_marker(self):
        rate = RatePlan.objects.create(
            property=self.prop, code="ED-R2", name="ED2",
            valid_from=timezone.localdate())
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate, policy_type="FIRST_NIGHT", is_active=True)
        _, room = self._make_legacy_booking(rate)
        summary = backfill_early_departure_snapshots(
            dry_run=False, property_obj=self.prop)
        room.refresh_from_db()
        self.assertEqual(summary["backfilled"], 1)
        self.assertEqual(room.early_departure_snapshot["policy_type"], "FIRST_NIGHT")
        self.assertTrue(room.early_departure_snapshot.get("backfilled"))
        self.assertEqual(room.early_departure_snapshot["rate_plan_id"], rate.pk)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="reservation.early_departure_policy_backfilled").count(), 1)
        # 确认路径审计动作不被回填污染
        self.assertEqual(
            AuditEvent.objects.filter(
                action="reservation.early_departure_policy_snapshotted").count(), 0)

    def test_idempotent_rerun_noop(self):
        rate = RatePlan.objects.create(
            property=self.prop, code="ED-R3", name="ED3",
            valid_from=timezone.localdate())
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate, policy_type="FIXED", is_active=True)
        self._make_legacy_booking(rate)
        first = backfill_early_departure_snapshots(
            dry_run=False, property_obj=self.prop)
        second = backfill_early_departure_snapshots(
            dry_run=False, property_obj=self.prop)
        self.assertEqual(first["backfilled"], 1)
        self.assertEqual(second["backfilled"], 0)
        self.assertEqual(second["would_backfill"], 0)

    def test_none_fallback_when_no_policy(self):
        rate = RatePlan.objects.create(
            property=self.prop, code="ED-R4", name="ED4",
            valid_from=timezone.localdate())
        # 不创建任何 EarlyDeparturePolicy：应按 NONE 兜底
        _, room = self._make_legacy_booking(rate, policy_type=None)
        backfill_early_departure_snapshots(dry_run=False, property_obj=self.prop)
        room.refresh_from_db()
        self.assertEqual(room.early_departure_snapshot["policy_type"], "NONE")
        self.assertTrue(room.early_departure_snapshot.get("backfilled"))

    def test_property_filter(self):
        prop2 = Property.objects.create(
            code=f"EDB2-{uuid.uuid4().hex[:8]}", name="回填测试物业2")
        rt2 = RoomType.objects.create(
            property=prop2, code="ED-RT2", name="ED房型2", capacity=2)
        rate1 = RatePlan.objects.create(
            property=self.prop, code="ED-R5", name="ED5",
            valid_from=timezone.localdate())
        rate2 = RatePlan.objects.create(
            property=prop2, code="ED-R6", name="ED6",
            valid_from=timezone.localdate())
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate1, policy_type="FIRST_NIGHT", is_active=True)
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate2, policy_type="FIRST_NIGHT", is_active=True)
        _, room1 = self._make_legacy_booking(rate1)
        # prop2 的预订
        guest2 = Guest.objects.create(property=prop2, full_name="ED客2")
        res2 = Reservation.objects.create(
            property=prop2, guest=guest2,
            booking_no=f"ED-{uuid.uuid4().hex[:12]}",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("600.00"), currency="CNY")
        room2 = ReservationRoom.objects.create(
            property=prop2, reservation=res2, room_type=rt2, rate_plan=rate2,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal("600.00"))
        backfill_early_departure_snapshots(dry_run=False, property_obj=self.prop)
        room1.refresh_from_db()
        room2.refresh_from_db()
        self.assertIsNotNone(room1.early_departure_snapshot)
        self.assertIsNone(room2.early_departure_snapshot)

    def test_rate_plan_filter(self):
        rate_a = RatePlan.objects.create(
            property=self.prop, code="ED-R7", name="ED7",
            valid_from=timezone.localdate())
        rate_b = RatePlan.objects.create(
            property=self.prop, code="ED-R8", name="ED8",
            valid_from=timezone.localdate())
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate_a, policy_type="FIRST_NIGHT", is_active=True)
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate_b, policy_type="FIXED", is_active=True)
        _, room_a = self._make_legacy_booking(rate_a)
        _, room_b = self._make_legacy_booking(rate_b)
        backfill_early_departure_snapshots(
            dry_run=False, property_obj=self.prop, rate_plan_id=rate_a.pk)
        room_a.refresh_from_db()
        room_b.refresh_from_db()
        self.assertIsNotNone(room_a.early_departure_snapshot)
        self.assertIsNone(room_b.early_departure_snapshot)

    def test_clear_rollback(self):
        rate = RatePlan.objects.create(
            property=self.prop, code="ED-R9", name="ED9",
            valid_from=timezone.localdate())
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate, policy_type="FIRST_NIGHT", is_active=True)
        _, room = self._make_legacy_booking(rate)
        backfill_early_departure_snapshots(dry_run=False, property_obj=self.prop)
        room.refresh_from_db()
        self.assertIsNotNone(room.early_departure_snapshot)
        clear = clear_backfilled_early_departure_snapshots(dry_run=False)
        self.assertEqual(clear["cleared"], 1)
        room.refresh_from_db()
        self.assertIsNone(room.early_departure_snapshot)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="reservation.early_departure_policy_backfill_cleared").count(), 1)

    def test_clear_dry_run_no_write(self):
        rate = RatePlan.objects.create(
            property=self.prop, code="ED-R10", name="ED10",
            valid_from=timezone.localdate())
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate, policy_type="FIRST_NIGHT", is_active=True)
        _, room = self._make_legacy_booking(rate)
        backfill_early_departure_snapshots(dry_run=False, property_obj=self.prop)
        room.refresh_from_db()
        self.assertIsNotNone(room.early_departure_snapshot)
        clear = clear_backfilled_early_departure_snapshots(dry_run=True)
        self.assertEqual(clear["cleared"], 0)
        room.refresh_from_db()
        self.assertIsNotNone(room.early_departure_snapshot)

    def test_batch_size_guard(self):
        with self.assertRaises(ValueError):
            backfill_early_departure_snapshots(dry_run=True, batch_size=0)
        with self.assertRaises(ValueError):
            clear_backfilled_early_departure_snapshots(dry_run=True, batch_size=0)
