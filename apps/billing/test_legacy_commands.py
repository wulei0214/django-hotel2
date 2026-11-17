"""批次授权/迁移/对账命令与服务的 TDD（批次 3）。

覆盖：
* 批次闭环 §5.2：登记 PENDING → 批准 ACTIVE（SoD）→ 关闭 CLOSED（终态）；
* 迁移命令 §4：dry-run 只分类、执行经受控入口、认证失败拒绝并审计；
* 对账 §1：分类守恒/金额守恒/分类泄漏（C2/C3 不得产新凭证）；
* 数据库守卫命令在 SQLite 下必须明确拒绝（不得伪造验收）。
"""

import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock, skipIf

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection, IntegrityError, models as django_models, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.legacy_batches import (
    LegacyBatchError,
    approve_batch,
    close_batch,
    register_batch,
    reject_batch,
)
from apps.billing.legacy_credentials import LegacyMigrationDenied
from apps.billing.legacy_migration import reconcile_batch
from apps.billing.models import Folio, LedgerEntry, LegacyArchiveRun, LegacyMigrationBatch
from apps.core.models import AuditEvent, LegacyRecordLink, Property
from apps.guests.models import Guest
from apps.reservations.models import Reservation


def _legacy_rows():
    """旧表夹具：C1 可迁移 / C5 归档 / C4 无法确认 各若干条。"""
    from hotelapp.models import Accommodation, Client, FinanceRecord, Folio, Room, Staff

    staff = Staff.objects.create(姓名="迁移员工", 职务="服务员", 月薪="3000", 工龄="1")
    room = Room.objects.create(
        房间号=9301, 客房类型="标准间", 价格=300, 负责人编号=staff, 房间状态="空闲"
    )
    client = Client.objects.create(姓名="迁移顾客", 身份证号="110101199001018888")
    acc = Accommodation.objects.create(
        id=8101, 顾客id=client, 客房编号=room, 状态="已退房", 入住时间="2026-01-02"
    )
    Folio.objects.create(
        id=9001, order=acc, 类型="收款", 金额="688.00", 事由="旧收款", 日期="2026-01-02"
    )
    Folio.objects.create(
        id=9002, order=acc, 类型="消费", 金额="100.00", 事由="旧消费", 日期="2026-01-02"
    )
    Folio.objects.create(
        id=9003, order=acc, 类型="押金", 金额="abc", 事由="金额脏数据", 日期="2026-01-02"
    )
    FinanceRecord.objects.create(
        id=7001,
        类型="收入",
        金额="688.00",
        事由="旧收入",
        关联订单="8101",
        日期="2026-01-02",
    )
    FinanceRecord.objects.create(
        id=7002,
        类型="支出",
        金额="50.00",
        事由="旧支出",
        关联订单="8101",
        日期="2026-01-02",
    )
    return acc


@override_settings(DEBUG=True, DJANGO_ENV="test")
class LegacyBatchLifecycleTests(TestCase):
    """批次授权闭环（服务层）。"""

    prop: Property
    approver: User
    executor: User

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.approver = User.objects.create_user(username="batch-owner", password="x", is_staff=True)
        cls.executor = User.objects.create_user(username="batch-ops", password="x", is_staff=True)

    def _register(self, uid="BATCH-LIFE-1", **overrides):
        payload = {
            "batch_uid": uid,
            "property_obj": self.prop,
            "source_models": ["Folio", "FinanceRecord"],
            "record_range": {"pk_min": 1, "pk_max": 100},
            "executed_by": self.executor,
            "approval_basis": "迁移计划 v1",
        }
        payload.update(overrides)
        return register_batch(**payload)

    def test_register_creates_pending_with_audit(self):
        batch = self._register()
        self.assertEqual(batch.status, LegacyMigrationBatch.Status.PENDING)
        self.assertEqual(batch.scope_source_models, ["Folio", "FinanceRecord"])
        self.assertTrue(AuditEvent.objects.filter(action="billing.legacy_batch_register").exists())

    def test_duplicate_batch_uid_rejected(self):
        self._register()
        with self.assertRaises(LegacyBatchError):
            self._register()

    def test_unknown_source_model_rejected(self):
        with self.assertRaises(LegacyBatchError):
            self._register(uid="BATCH-BAD-MODEL", source_models=["Order"])

    def test_invalid_range_rejected(self):
        with self.assertRaises(LegacyBatchError):
            self._register(uid="BATCH-BAD-RANGE", record_range={"pk_min": 10, "pk_max": 1})
        with self.assertRaises(LegacyBatchError):
            self._register(uid="BATCH-EMPTY-RANGE", record_range={})

    def test_approve_enforces_sod(self):
        batch = self._register()
        with self.assertRaises(LegacyBatchError):
            approve_batch(batch=batch, approved_by=self.executor)
        approve_batch(batch=batch, approved_by=self.approver)
        self.assertEqual(batch.status, LegacyMigrationBatch.Status.ACTIVE)
        self.assertEqual(batch.approved_by_id, self.approver.pk)
        self.assertIsNotNone(batch.approved_at)

    def test_approve_requires_pending(self):
        batch = self._register()
        approve_batch(batch=batch, approved_by=self.approver)
        with self.assertRaises(LegacyBatchError):
            approve_batch(batch=batch, approved_by=self.approver)

    def test_reject_is_terminal(self):
        batch = self._register(uid="BATCH-REJECT")
        reject_batch(batch=batch, approved_by=self.approver, reason="范围未确认")
        self.assertEqual(batch.status, LegacyMigrationBatch.Status.REJECTED)
        with self.assertRaises(LegacyBatchError):
            approve_batch(batch=batch, approved_by=self.approver)
        with self.assertRaises(LegacyBatchError):
            close_batch(batch=batch, actor=self.approver)

    def test_close_then_cannot_reopen(self):
        batch = self._register(uid="BATCH-CLOSE")
        approve_batch(batch=batch, approved_by=self.approver)
        close_batch(batch=batch, actor=self.approver)
        self.assertEqual(batch.status, LegacyMigrationBatch.Status.CLOSED)
        self.assertIsNotNone(batch.closed_at)
        with self.assertRaises(LegacyBatchError):
            close_batch(batch=batch, actor=self.approver)

    def test_command_register_approve_close(self):
        out = StringIO()
        call_command(
            "manage_legacy_migration_batch",
            "--action",
            "register",
            "--batch-uid",
            "BATCH-CMD-1",
            "--property",
            "DEFAULT",
            "--source-models",
            "Folio",
            "--pk-min",
            "1",
            "--pk-max",
            "50",
            "--executed-by",
            "batch-ops",
            "--basis",
            "迁移计划 v1",
            stdout=out,
        )
        self.assertIn("BATCH-CMD-1", out.getvalue())
        call_command(
            "manage_legacy_migration_batch",
            "--action",
            "approve",
            "--batch-uid",
            "BATCH-CMD-1",
            "--approved-by",
            "batch-owner",
            stdout=StringIO(),
        )
        call_command(
            "manage_legacy_migration_batch",
            "--action",
            "close",
            "--batch-uid",
            "BATCH-CMD-1",
            "--approved-by",
            "batch-owner",
            stdout=StringIO(),
        )
        batch = LegacyMigrationBatch.objects.get(batch_uid="BATCH-CMD-1")
        self.assertEqual(batch.status, LegacyMigrationBatch.Status.CLOSED)

    def test_command_list(self):
        self._register(uid="BATCH-LIST-1")
        out = StringIO()
        call_command("manage_legacy_migration_batch", "--action", "list", stdout=out)
        self.assertIn("BATCH-LIST-1", out.getvalue())


@override_settings(DEBUG=True, DJANGO_ENV="test")
class LegacyMigrationCommandTests(TestCase):
    """迁移命令：dry-run 分类、认证门槛、执行与对账。"""

    prop: Property
    approver: User
    executor: User
    intruder: User
    reservation: Reservation
    folio: Folio

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.approver = User.objects.create_user(
            username="mig-owner", password="pw-owner", is_staff=True
        )
        cls.executor = User.objects.create_user(
            username="mig-ops", password="pw-ops", is_staff=True
        )
        cls.intruder = User.objects.create_user(
            username="mig-intruder", password="pw-intruder", is_staff=True
        )
        guest = Guest.objects.create(property=cls.prop, full_name="迁移命令测试客")
        cls.reservation = Reservation.objects.create(
            property=cls.prop,
            guest=guest,
            booking_no="LEGACY-MIG-1",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("688.00"),
            currency="CNY",
        )
        cls.folio = Folio.objects.create(
            property=cls.prop, reservation=cls.reservation, guest=guest, currency="CNY"
        )

    def setUp(self):
        _legacy_rows()
        self.batch = LegacyMigrationBatch.objects.create(
            batch_uid="BATCH-MIG-1",
            approved_by=self.approver,
            approved_at=timezone.now(),
            approval_basis="GOV-03 准入通过（测试）",
            scope_property=self.prop,
            scope_source_models=["Folio", "FinanceRecord"],
            scope_record_range={"pk_min": 1, "pk_max": 99999},
            executed_by=self.executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )
        self.tmpdir = Path(tempfile.mkdtemp(prefix="legacy-mig-"))
        self.mapping_path = self.tmpdir / "mapping.json"
        self.mapping_path.write_text(
            json.dumps(
                {
                    "orders": {
                        "8101": {
                            "reservation": self.reservation.pk,
                            "basis": "客人+日期+房型人工确认",
                            "confirmed_by": "finance-lead",
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _call(self, *extra, password="pw-ops"):
        out = StringIO()
        with mock.patch(
            "apps.billing.management.commands.migrate_legacy_finance.getpass.getpass",
            return_value=password,
        ):
            call_command(
                "migrate_legacy_finance",
                "--batch-uid",
                self.batch.batch_uid,
                "--mapping-file",
                str(self.mapping_path),
                "--executor",
                "mig-ops",
                *extra,
                stdout=out,
            )
        return out.getvalue()

    def test_dry_run_classifies_without_writing(self):
        out = self._call("--dry-run")
        self.assertIn("C1=2", out)
        self.assertIn("C4=1", out)
        self.assertIn("C5=2", out)
        self.assertEqual(LedgerEntry.objects.filter(reference__startswith="legacy:").count(), 0)
        self.assertEqual(LegacyRecordLink.objects.count(), 0)

    def test_execute_posts_entries_and_links(self):
        out = self._call()
        self.assertIn("迁移完成", out)
        entry = LedgerEntry.objects.get(reference="legacy:Folio:9001")
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.PAYMENT)
        self.assertEqual(entry.amount, Decimal("-688.00"))
        self.assertEqual(entry.posted_by_id, self.executor.pk)
        self.assertEqual(
            LegacyRecordLink.objects.filter(
                migration_batch=self.batch.batch_uid,
                mapping_status=LegacyRecordLink.MappingStatus.LINKED,
            ).count(),
            2,
        )
        # C4/C5 只写映射，绝不产新凭证（分类泄漏 = 阻断级）。
        self.assertEqual(LedgerEntry.objects.filter(reference__startswith="legacy:").count(), 2)
        conflict = LegacyRecordLink.objects.get(source_model="Folio", source_pk="9003")
        self.assertEqual(conflict.mapping_status, LegacyRecordLink.MappingStatus.CONFLICT)
        ignored = LegacyRecordLink.objects.get(source_model="Folio", source_pk="9002")
        self.assertEqual(ignored.mapping_status, LegacyRecordLink.MappingStatus.IGNORED)

    def test_execute_rejects_bad_password_and_audits(self):
        with self.assertRaises(CommandError):
            self._call(password="wrong")
        self.assertTrue(
            AuditEvent.objects.filter(action="billing.legacy_migration_denied").exists()
        )
        self.assertEqual(LedgerEntry.objects.filter(reference__startswith="legacy:").count(), 0)

    def test_execute_rejects_non_registered_executor(self):
        out = StringIO()
        with (
            mock.patch(
                "apps.billing.management.commands.migrate_legacy_finance.getpass.getpass",
                return_value="pw-intruder",
            ),
            self.assertRaises(CommandError),
        ):
            call_command(
                "migrate_legacy_finance",
                "--batch-uid",
                self.batch.batch_uid,
                "--mapping-file",
                str(self.mapping_path),
                "--executor",
                "mig-intruder",
                stdout=out,
            )

    def test_missing_mapping_file_fails(self):
        with self.assertRaises(CommandError):
            call_command(
                "migrate_legacy_finance",
                "--batch-uid",
                self.batch.batch_uid,
                "--mapping-file",
                str(self.tmpdir / "absent.json"),
                "--dry-run",
                stdout=StringIO(),
            )

    def test_reconcile_reports_no_diff_after_execute(self):
        self._call()
        out = StringIO()
        call_command(
            "reconcile_legacy_finance",
            "--batch-uid",
            self.batch.batch_uid,
            "--fail-on-diff",
            stdout=out,
        )
        self.assertIn("对账无差异", out.getvalue())

    def test_reconcile_detects_category_leak(self):
        """C3 声明却确实存在新建凭证 → 分类泄漏，--fail-on-diff 必须报错。"""
        self._call()
        # 把 9001 的映射改标成 C3（"已覆盖"）：它其实建了新凭证 = 分类泄漏。
        LegacyRecordLink.objects.filter(
            source_model="Folio",
            source_pk="9001",
            migration_batch=self.batch.batch_uid,
        ).update(match_method="legacy_migration_duplicate")
        report = reconcile_batch(self.batch)
        self.assertTrue(any("泄漏" in diff for diff in report["diffs"]))
        with self.assertRaises(CommandError):
            call_command(
                "reconcile_legacy_finance",
                "--batch-uid",
                self.batch.batch_uid,
                "--fail-on-diff",
                stdout=StringIO(),
            )

    def test_reconcile_detects_missing_source_link(self):
        """R3：删除一条来源映射后，来源记录数与映射数不守恒。"""
        self._call()
        django_models.QuerySet(LegacyRecordLink).filter(
            migration_batch=self.batch.batch_uid,
            source_model="Folio",
            source_pk="9001",
        ).delete()
        report = reconcile_batch(self.batch)
        self.assertTrue(any("分类守恒不成立" in diff for diff in report["diffs"]))
        self.assertIn("9001", report["categories"]["Folio"]["missing_source_links"])

    def test_reconcile_detects_amount_mismatch(self):
        """R2：旧金额与新凭证金额不一致时生成可归因差异。"""
        self._call()
        entry = LedgerEntry.objects.get(reference="legacy:Folio:9001")
        django_models.QuerySet(LedgerEntry).filter(pk=entry.pk).update(amount=Decimal("-600.00"))
        report = reconcile_batch(self.batch)
        self.assertTrue(any("Folio#9001" in diff for diff in report["diffs"]))
        self.assertTrue(any("金额分组不守恒" in diff for diff in report["diffs"]))

    def test_reconcile_detects_duplicate_c1_target(self):
        """R4：两条旧来源映射到同一凭证，必须阻断收口。"""
        self._call()
        folio_link = LegacyRecordLink.objects.get(
            migration_batch=self.batch.batch_uid,
            source_model="Folio",
            source_pk="9001",
        )
        finance_link = LegacyRecordLink.objects.get(
            migration_batch=self.batch.batch_uid,
            source_model="FinanceRecord",
            source_pk="7001",
        )
        django_models.QuerySet(LegacyRecordLink).filter(pk=finance_link.pk).update(
            target_pk=folio_link.target_pk
        )
        report = reconcile_batch(self.batch)
        self.assertTrue(any("多条旧记录指向同一新凭证" in diff for diff in report["diffs"]))

    def test_reconcile_is_read_only(self):
        """R5：对账只读；新旧事实、映射、归档元数据和审计数均不得变化。"""
        self._call()
        from hotelapp.models import FinanceRecord as LegacyFinanceRecord
        from hotelapp.models import Folio as LegacyFolio

        source_before = {
            "Folio": list(LegacyFolio.objects.order_by("id").values()),
            "FinanceRecord": list(LegacyFinanceRecord.objects.order_by("id").values()),
        }
        ledger_before = list(LedgerEntry.objects.order_by("pk").values())
        links_before = list(LegacyRecordLink.objects.order_by("pk").values())
        archive_before = list(LegacyArchiveRun.objects.order_by("pk").values())
        audit_count_before = AuditEvent.objects.count()

        report = reconcile_batch(self.batch)

        self.assertEqual(report["diffs"], [])
        self.assertEqual(source_before["Folio"], list(LegacyFolio.objects.order_by("id").values()))
        self.assertEqual(
            source_before["FinanceRecord"],
            list(LegacyFinanceRecord.objects.order_by("id").values()),
        )
        self.assertEqual(ledger_before, list(LedgerEntry.objects.order_by("pk").values()))
        self.assertEqual(links_before, list(LegacyRecordLink.objects.order_by("pk").values()))
        self.assertEqual(archive_before, list(LegacyArchiveRun.objects.order_by("pk").values()))
        self.assertEqual(audit_count_before, AuditEvent.objects.count())

    def test_execute_is_idempotent_and_can_resume_after_partial_failure(self):
        """M9/R6：部分失败后整批重执行，已成功记录跳过，失败记录补做，不产生重复事实。"""
        from apps.billing.services_v2 import BillingService

        original = BillingService.post_legacy_migration_entry
        state = {"failed": False}

        def fail_once(**kwargs):
            if not state["failed"]:
                state["failed"] = True
                raise LegacyMigrationDenied("测试注入的一次性失败")
            return original(**kwargs)

        with (
            mock.patch.object(
                BillingService, "post_legacy_migration_entry", side_effect=fail_once
            ),
            self.assertRaises(CommandError),
        ):
            self._call()

        first_entry_count = LedgerEntry.objects.filter(reference__startswith="legacy:").count()
        first_link_count = LegacyRecordLink.objects.filter(
            migration_batch=self.batch.batch_uid
        ).count()
        self.assertGreater(first_entry_count, 0)
        self.assertGreater(first_link_count, 0)

        out = self._call()
        self.assertIn("迁移完成且对账无差异", out)
        second_entry_count = LedgerEntry.objects.filter(reference__startswith="legacy:").count()
        second_link_count = LegacyRecordLink.objects.filter(
            migration_batch=self.batch.batch_uid
        ).count()
        self.assertEqual(second_entry_count, 2)
        self.assertEqual(second_link_count, 5)
        self.assertEqual(first_entry_count, second_entry_count)
        self.assertEqual(first_link_count + 1, second_link_count)

        # 已完成批次再次重跑：全部幂等跳过，不新增凭证、映射或成功审计。
        success_audits_before = AuditEvent.objects.filter(action="billing.legacy_migration").count()
        out = self._call()
        self.assertIn("迁移完成且对账无差异", out)
        self.assertEqual(
            LedgerEntry.objects.filter(reference__startswith="legacy:").count(),
            second_entry_count,
        )
        self.assertEqual(
            LegacyRecordLink.objects.filter(migration_batch=self.batch.batch_uid).count(),
            second_link_count,
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="billing.legacy_migration").count(),
            success_audits_before,
        )

    def test_reconcile_amount_groups_do_not_net_different_currencies(self):
        """R7：不同币种分别守恒；CNY 与 USD 的反向差额不得相互抵消。"""
        from hotelapp.models import Accommodation

        old_order = Accommodation.objects.get(pk=8101)
        Accommodation.objects.create(
            id=8102,
            顾客id=old_order.顾客id,
            客房编号=old_order.客房编号,
            状态=old_order.状态,
            入住时间=old_order.入住时间,
        )
        LegacyFolio = __import__("hotelapp.models", fromlist=["Folio"]).Folio
        LegacyFolio.objects.create(
            id=9004,
            order_id=8102,
            类型="收款",
            金额="688.00",
            事由="USD 旧收款",
            日期="2026-01-02",
        )

        guest = Guest.objects.create(property=self.prop, full_name="美元迁移测试客")
        usd_reservation = Reservation.objects.create(
            property=self.prop,
            guest=guest,
            booking_no="LEGACY-MIG-USD",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("688.00"),
            currency="USD",
        )
        Folio.objects.create(
            property=self.prop,
            reservation=usd_reservation,
            guest=guest,
            currency="USD",
        )
        payload = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        payload["orders"]["8102"] = {
            "reservation": usd_reservation.pk,
            "basis": "客人+日期+房型人工确认（USD）",
            "confirmed_by": "finance-lead",
        }
        self.mapping_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        self._call()
        cny_entry = LedgerEntry.objects.get(reference="legacy:Folio:9001")
        usd_entry = LedgerEntry.objects.get(reference="legacy:Folio:9004")
        django_models.QuerySet(LedgerEntry).filter(pk=cny_entry.pk).update(
            amount=Decimal("-600.00")
        )
        django_models.QuerySet(LedgerEntry).filter(pk=usd_entry.pk).update(
            amount=Decimal("-776.00")
        )

        report = reconcile_batch(self.batch)
        groups = {
            item["currency"]: item
            for item in report["amount_groups"]
            if item["entry_type"] == LedgerEntry.EntryType.PAYMENT
        }
        self.assertEqual(groups["CNY"]["expected"], "-1376.00")
        self.assertEqual(groups["CNY"]["actual"], "-1288.00")
        self.assertEqual(groups["USD"]["expected"], "-688.00")
        self.assertEqual(groups["USD"]["actual"], "-776.00")
        self.assertTrue(any("currency=CNY" in diff for diff in report["diffs"]))
        self.assertTrue(any("currency=USD" in diff for diff in report["diffs"]))

    def test_duplicate_legacy_reference_is_rejected_by_database(self):
        """R4：reference 唯一约束阻止同一旧来源凭证被重复落账。"""
        self._call()
        entry = LedgerEntry.objects.get(reference="legacy:Folio:9001")
        with self.assertRaises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.create(
                property=entry.property,
                folio=entry.folio,
                entry_type=entry.entry_type,
                amount=entry.amount,
                currency=entry.currency,
                business_date=entry.business_date,
                reference=entry.reference,
                description="重复 reference 测试",
            )

    @skipIf(
        connection.vendor != "sqlite",
        "仅在 SQLite（无触发器能力）下验证命令 fail-closed；MySQL 上命令须真实安装/校验",
    )
    def test_database_guard_commands_refuse_sqlite(self):
        """SQLite 无触发器：安装/校验命令必须明确拒绝，不得伪造验收。"""
        for command in ("install_legacy_batch_guards", "check_legacy_batch_guards"):
            with self.assertRaises(CommandError):
                call_command(command, stdout=StringIO())
