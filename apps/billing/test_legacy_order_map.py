"""旧订单-新预订关系映射的 TDD（迁移方案 §1.5.2 / §1.5.3）。

覆盖三层守卫与版本链：
* 确认事务：物业一致性复核、跨物业拒绝、重复配对拒绝、匹配依据/确认人必填；
* 不可变：save 守卫 + ``ImmutableMapQuerySet`` 批量路径全拒；
* 版本链：追加 supersedes 新行、旧行原样保留、当前有效为派生口径、禁止分叉；
* 映射来源：关系表与 ``--mapping-file`` 合并、冲突拒绝、``build_plan`` 从表取关系；
* 数据库守卫命令在 SQLite 下必须明确拒绝（不得伪造验收）；
* 真实 MySQL 并发确认（SQLite 的 ``select_for_update`` 是 no-op，不能替代）。
"""

import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from threading import Barrier
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.billing.legacy_migration import build_plan
from django.db import transaction

from apps.billing.legacy_order_map import (
    LegacyOrderMapError,
    confirm_pair,
    pair_position_key,
    current_map_for_order,
    current_rows,
    merge_order_mappings,
    orders_mapping,
    superseded_chain,
)
from apps.billing.models import Folio, LegacyMigrationBatch, LegacyOrderReservationMap
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.reservations.models import Reservation


def _make_legacy_order(pk, *, room_no=9401):
    from hotelapp.models import Accommodation, Client, Room, Staff

    staff = Staff.objects.create(姓名=f"关系员工{pk}", 职务="服务员", 月薪="3000", 工龄="1")
    room = Room.objects.create(
        房间号=room_no, 客房类型="标准间", 价格=300, 负责人编号=staff, 房间状态="空闲"
    )
    client = Client.objects.create(姓名=f"关系顾客{pk}", 身份证号=f"1101011990010{pk:04d}")
    return Accommodation.objects.create(
        id=pk, 顾客id=client, 客房编号=room, 状态="已退房", 入住时间="2026-01-02"
    )


@override_settings(DEBUG=True, DJANGO_ENV="test")
class LegacyOrderMapBaseTests(TestCase):
    """关系映射确认、不可变与版本链（服务层，SQLite 亦可）。"""

    prop: Property
    other_prop: Property
    confirmer: User
    guest: Guest
    reservation: Reservation
    other_reservation: Reservation
    # 旧表模型不参与 mypy 路径（pyproject exclude hotelapp），故按 Any 标注。
    legacy_order: Any

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.other_prop = Property.objects.create(code="MAP-OTHER", name="关系映射他物业")
        cls.confirmer = User.objects.create_user(
            username="map-confirmer", password="x", is_staff=True
        )
        cls.guest = Guest.objects.create(property=cls.prop, full_name="关系映射客人")
        today = timezone.localdate()
        cls.reservation = Reservation.objects.create(
            property=cls.prop,
            guest=cls.guest,
            booking_no="MAP-RES-1",
            status=Reservation.Status.CONFIRMED,
            check_in=today,
            check_out=today + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("688.00"),
            currency="CNY",
        )
        cls.other_reservation = Reservation.objects.create(
            property=cls.prop,
            guest=cls.guest,
            booking_no="MAP-RES-2",
            status=Reservation.Status.CONFIRMED,
            check_in=today,
            check_out=today + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("688.00"),
            currency="CNY",
        )
        cls.legacy_order = _make_legacy_order(8501)

    def _confirm(self, **overrides):
        payload = {
            "legacy_order_id": self.legacy_order.pk,
            "reservation_id": self.reservation.pk,
            "property_obj": self.prop,
            "confirmed_by": self.confirmer,
            "match_basis": "客人姓名+入住日期+房型一致",
        }
        payload.update(overrides)
        return confirm_pair(**payload)

    # --- 确认事务 ---------------------------------------------------------
    def test_confirm_creates_immutable_fact_with_audit(self):
        row = self._confirm()
        self.assertEqual(row.legacy_order_id, self.legacy_order.pk)
        self.assertEqual(row.reservation_id, self.reservation.pk)
        self.assertEqual(row.relation_type, LegacyOrderReservationMap.RelationType.ONE_TO_ONE)
        self.assertIsNone(row.supersedes_id)
        self.assertTrue(
            AuditEvent.objects.filter(action="billing.legacy_order_map_confirm").exists()
        )

    def test_missing_basis_rejected(self):
        with self.assertRaises(LegacyOrderMapError):
            self._confirm(match_basis="   ")

    def test_missing_confirmer_rejected(self):
        with self.assertRaises(LegacyOrderMapError):
            self._confirm(confirmed_by=None)

    def test_unknown_legacy_order_rejected(self):
        with self.assertRaises(LegacyOrderMapError):
            self._confirm(legacy_order_id=999999)

    def test_unknown_reservation_rejected(self):
        with self.assertRaises(LegacyOrderMapError):
            self._confirm(reservation_id=999999)

    def test_cross_property_rejected(self):
        """关系行物业 ≠ 新预订物业 → 拒绝（跨物业关系禁止）。"""
        with self.assertRaises(LegacyOrderMapError):
            self._confirm(property_obj=self.other_prop)

    def test_duplicate_pair_rejected(self):
        self._confirm()
        with self.assertRaises(LegacyOrderMapError):
            self._confirm()
        self.assertEqual(LegacyOrderReservationMap.objects.count(), 1)

    def test_invalid_relation_type_rejected(self):
        with self.assertRaises(LegacyOrderMapError):
            self._confirm(relation_type="NOT_A_TYPE")

    # --- 不可变 -----------------------------------------------------------
    def test_save_guard_rejects_mutation(self):
        row = self._confirm()
        row.match_basis = "试图事后改写确认依据"
        with self.assertRaises(ValidationError):
            row.save()

    def test_queryset_guards_reject_bulk_ops(self):
        row = self._confirm()
        queryset = LegacyOrderReservationMap.objects.all()
        with self.assertRaises(ValidationError):
            queryset.update(match_basis="批量改写")
        with self.assertRaises(ValidationError):
            queryset.delete()
        with self.assertRaises(ValidationError):
            LegacyOrderReservationMap.objects.bulk_create([row])
        with self.assertRaises(ValidationError):
            LegacyOrderReservationMap.objects.bulk_update([row], ["match_basis"])

    def test_delete_guard_rejects_instance_delete(self):
        row = self._confirm()
        with self.assertRaises(ValidationError):
            row.delete()

    # --- 版本链 -----------------------------------------------------------
    def test_supersede_appends_and_keeps_old_row(self):
        old = self._confirm()
        new = self._confirm(reservation_id=self.other_reservation.pk, supersedes=old)
        self.assertEqual(new.supersedes_id, old.pk)
        old.refresh_from_db()
        self.assertEqual(old.match_basis, "客人姓名+入住日期+房型一致")
        self.assertEqual(current_rows(property_obj=self.prop).count(), 1)
        self.assertEqual(current_map_for_order(self.legacy_order.pk).pk, new.pk)

    def test_supersede_requires_same_legacy_order(self):
        other_order = _make_legacy_order(8502, room_no=9402)
        old = self._confirm()
        with self.assertRaises(LegacyOrderMapError):
            self._confirm(
                legacy_order_id=other_order.pk,
                reservation_id=self.other_reservation.pk,
                supersedes=old,
            )

    def test_supersede_chain_cannot_fork(self):
        first = self._confirm()
        second = self._confirm(reservation_id=self.other_reservation.pk, supersedes=first)
        with self.assertRaises(LegacyOrderMapError):
            self._confirm(
                legacy_order_id=self.legacy_order.pk,
                reservation_id=self.reservation.pk,
                supersedes=first,
            )
        self.assertEqual(LegacyOrderReservationMap.objects.count(), 2)
        self.assertEqual(second.supersedes_id, first.pk)

    def test_pair_revert_via_supersede_allowed(self):
        """FIN-01（2026-11-16 裁定）：改映射后又改回原预订——经版本链回指允许。

        组合唯一含 supersedes 后，同配对在不同版本位置可再次成立；
        当前有效关系为派生口径（最后落库的未取代行）。"""
        first = self._confirm()
        second = self._confirm(reservation_id=self.other_reservation.pk, supersedes=first)
        reverted = self._confirm(
            legacy_order_id=self.legacy_order.pk,
            reservation_id=self.reservation.pk,
            supersedes=second,
        )
        self.assertEqual(reverted.reservation_id, self.reservation.pk)
        self.assertEqual(reverted.supersedes_id, second.pk)
        self.assertEqual(LegacyOrderReservationMap.objects.count(), 3)
        current = current_map_for_order(self.legacy_order.pk)
        self.assertEqual(current.pk, reverted.pk)
        self.assertEqual(current.reservation_id, self.reservation.pk)
        chain = superseded_chain(self.legacy_order.pk)
        self.assertEqual([row.pk for row in chain], [first.pk, second.pk, reverted.pk])

    def test_same_pair_same_position_integrity_error(self):
        """并发防护保持：绕过服务层直插同（配对, 版本位置）第二行 → DB 唯一约束拒绝。

        直插必须自给 position_key（可空列 NULL 相异，不设键不会触发约束）——
        这正是服务层统一经 pair_position_key 物化的原因。"""
        from django.db import IntegrityError, transaction

        first = self._confirm()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LegacyOrderReservationMap.objects.create(
                legacy_order=self.legacy_order,
                reservation=self.reservation,
                property=self.prop,
                relation_type=LegacyOrderReservationMap.RelationType.ONE_TO_ONE,
                match_basis="并发第二行（无人工确认通道）",
                confirmed_by=self.confirmer,
                supersedes=None,
                position_key=pair_position_key(
                    self.legacy_order.pk, self.reservation.pk, None
                ),
            )
        self.assertEqual(LegacyOrderReservationMap.objects.count(), 1)
        self.assertIsNotNone(first.pk)

    def test_supersede_chain_returns_full_history(self):
        first = self._confirm()
        second = self._confirm(reservation_id=self.other_reservation.pk, supersedes=first)
        chain = superseded_chain(self.legacy_order.pk)
        self.assertEqual([row.pk for row in chain], [first.pk, second.pk])

    # --- 映射来源合并 -----------------------------------------------------
    def test_orders_mapping_shape(self):
        self._confirm()
        mapping = orders_mapping(self.prop)
        entry = mapping[str(self.legacy_order.pk)]
        self.assertEqual(entry["reservation"], self.reservation.pk)
        self.assertEqual(entry["source"], "legacy_order_reservation_map")
        self.assertEqual(entry["confirmed_by"], "map-confirmer")

    def test_merge_detects_conflict(self):
        table_map = {"8501": {"reservation": 201}}
        file_map = {"8501": {"reservation": 202}}
        with self.assertRaises(LegacyOrderMapError):
            merge_order_mappings(table_map=table_map, file_map=file_map)

    def test_merge_keeps_both_sources_when_consistent(self):
        table_map = {"8501": {"reservation": 201}}
        file_map = {"8501": {"reservation": 201}, "8502": {"reservation": 202}}
        merged = merge_order_mappings(table_map=table_map, file_map=file_map)
        self.assertEqual(merged["8501"]["reservation"], 201)
        self.assertEqual(merged["8502"]["reservation"], 202)

    # --- 与迁移分类的接缝 -------------------------------------------------
    def _make_batch(self):
        executor = User.objects.create_user(username="map-ops", password="x", is_staff=True)
        return LegacyMigrationBatch.objects.create(
            batch_uid="BATCH-MAP-1",
            scope_property=self.prop,
            scope_source_models=["Folio"],
            scope_record_range={"pk_min": 1, "pk_max": 999999},
            executed_by=executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )

    def _make_legacy_folio(self, pk=9101):
        from hotelapp.models import Folio as LegacyFolio

        return LegacyFolio.objects.create(
            id=pk,
            order=self.legacy_order,
            类型="房费",
            金额="688.00",
            事由="旧房费",
            日期="2026-01-02",
        )

    def test_build_plan_uses_relation_table(self):
        """关系表已有确认关系 → 旧凭证判 C1（不依赖映射文件）。"""
        master = Folio.objects.create(
            property=self.prop, reservation=self.reservation, guest=self.guest, currency="CNY"
        )
        self._make_legacy_folio()
        self._confirm()
        plan = build_plan(batch=self._make_batch(), mapping=None)
        folio_decisions = [d for d in plan.decisions if d.source_model == "Folio"]
        self.assertEqual(len(folio_decisions), 1)
        self.assertEqual(folio_decisions[0].category, "C1")
        self.assertEqual(folio_decisions[0].target_folio_id, master.pk)

    def test_build_plan_rejects_conflicting_mapping_file(self):
        self._make_legacy_folio()
        self._confirm()
        conflicting = {"orders": {str(self.legacy_order.pk): {"reservation": 999999}}}
        from apps.billing.legacy_migration import LegacyMigrationError

        with self.assertRaises(LegacyMigrationError):
            build_plan(batch=self._make_batch(), mapping=conflicting)

    # --- 命令 -------------------------------------------------------------
    def test_command_confirm_list_and_chain(self):
        out = StringIO()
        call_command(
            "confirm_legacy_order_map",
            "--order",
            str(self.legacy_order.pk),
            "--reservation",
            str(self.reservation.pk),
            "--property",
            self.prop.code,
            "--confirmed-by",
            "map-confirmer",
            "--basis",
            "客人+日期核对",
            stdout=out,
        )
        self.assertIn("已确认关系", out.getvalue())

        out = StringIO()
        call_command("confirm_legacy_order_map", "--list", "--property", self.prop.code, stdout=out)
        self.assertIn(f"旧订单 {self.legacy_order.pk}", out.getvalue())

        out = StringIO()
        call_command("confirm_legacy_order_map", "--chain", str(self.legacy_order.pk), stdout=out)
        self.assertIn("当前有效", out.getvalue())

    def test_command_requires_confirmer_and_basis(self):
        with self.assertRaises(CommandError):
            call_command(
                "confirm_legacy_order_map",
                "--order",
                str(self.legacy_order.pk),
                "--reservation",
                str(self.reservation.pk),
                "--property",
                self.prop.code,
                stdout=StringIO(),
            )

    @unittest.skipIf(connection.vendor != "sqlite", "仅验证 SQLite 下命令 fail-closed")
    def test_map_guard_commands_refuse_sqlite(self):
        with self.assertRaises(CommandError):
            call_command("install_legacy_map_guards", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("check_legacy_map_guards", stdout=StringIO())


@unittest.skipUnless(connection.vendor == "mysql", "SQLite 的 select_for_update 为 no-op")
@override_settings(DEBUG=True, DJANGO_ENV="test")
class LegacyOrderMapConcurrencyTests(TransactionTestCase):
    """真实 MySQL：两线程同时确认同一配对，恰一行落库。"""

    def setUp(self):
        self.prop = Property.objects.create(code="MAP-CONC", name="关系并发物业")
        self.operator = User.objects.create_user(username="map-conc-1", password="x")
        self.operator2 = User.objects.create_user(username="map-conc-2", password="x")
        self.guest = Guest.objects.create(property=self.prop, full_name="关系并发客人")
        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            property=self.prop,
            guest=self.guest,
            booking_no="MAP-CONC-1",
            status=Reservation.Status.CONFIRMED,
            check_in=today,
            check_out=today + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("688.00"),
            currency="CNY",
        )
        self.legacy_order = _make_legacy_order(8601, room_no=9501)

    def test_concurrent_confirm_creates_exactly_one_row(self):
        barrier = Barrier(2)
        results = []
        lock = threading.Lock()

        def worker(user):
            close_old_connections()
            try:
                barrier.wait(timeout=30)
                row = confirm_pair(
                    legacy_order_id=self.legacy_order.pk,
                    reservation_id=self.reservation.pk,
                    property_obj=self.prop,
                    confirmed_by=user,
                    match_basis="并发确认",
                )
                with lock:
                    results.append(("ok", row.pk))
            except LegacyOrderMapError as exc:
                with lock:
                    results.append(("rejected", str(exc)))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=worker, args=(self.operator,)),
            threading.Thread(target=worker, args=(self.operator2,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
            self.assertFalse(thread.is_alive(), "工作线程超时未结束（疑似死锁）")

        self.assertEqual(len([item for item in results if item[0] == "ok"]), 1)
        self.assertEqual(LegacyOrderReservationMap.objects.count(), 1)
