"""归档层 TDD（批次 3）：四张 legacy_*_archive + checksum-v2 元数据 + 只读守卫。

覆盖归档门禁 §5（结构：源表全列快照 + archived_at + archive_batch + verified_checksum）
与 §4.2（行摘要/整表摘要/元数据登记），以及 D3 收口的 `--verify` 核验路径。
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import models as django_models
from django.test import TestCase, override_settings

from apps.billing.legacy_archive import (
    ARCHIVE_SPECS,
    LegacyArchiveError,
    archive_table,
    get_spec,
    summarize_source,
    verify_archive_run,
)
from apps.billing.legacy_checksum import ALGORITHM_VERSION, row_digest, table_digest
from apps.billing.models import (
    LegacyAccommodationArchive,
    LegacyArchiveRun,
    LegacyFinanceRecordArchive,
    LegacyFolioArchive,
    LegacyNightAuditArchive,
)
from apps.core.models import Property


def _legacy_fixtures():
    """构造四张旧表的最小可归档数据（旧表只读，测试里写入夹具）。"""
    from hotelapp.models import Accommodation, Client, FinanceRecord, Folio, NightAudit, Room, Staff

    staff = Staff.objects.create(姓名="归档员工", 职务="服务员", 月薪="3000", 工龄="1")
    room = Room.objects.create(
        房间号=9201, 客房类型="标准间", 价格=300, 负责人编号=staff, 房间状态="空闲"
    )
    client = Client.objects.create(姓名="归档顾客", 身份证号="110101199001019999")
    acc = Accommodation.objects.create(
        id=8101,
        顾客id=client,
        客房编号=room,
        状态="已退房",
        入住时间="2026-01-02",
        收款金额="688.00",
        押金="206.40",
    )
    Folio.objects.create(
        id=9001,
        order=acc,
        类型="收款",
        金额="688.00",
        事由="旧收款",
        日期="2026-01-02",
        经手人="旧前台",
    )
    Folio.objects.create(
        id=9002,
        order=acc,
        类型="押金",
        金额="206.40",
        事由="旧押金",
        日期="2026-01-02",
        经手人="旧前台",
    )
    FinanceRecord.objects.create(
        id=7001,
        类型="收入",
        金额="688.00",
        事由="旧收入",
        关联订单="8101",
        日期="2026-01-02",
        经手人="旧财务",
    )
    NightAudit.objects.create(
        id=6001,
        营业日期="2026-01-03",
        在住单数=3,
        过租总额="900.00",
        未到处理数=1,
        操作人="旧夜审",
        执行时间="2026-01-02 23:00",
    )
    return acc


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ArchiveTableTests(TestCase):
    """归档写入 + 摘要元数据 + 只读守卫。"""

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        _legacy_fixtures()

    def test_archive_specs_cover_four_tables(self):
        self.assertEqual(
            {spec.archive_table for spec in ARCHIVE_SPECS},
            {
                "legacy_folio_archive",
                "legacy_finance_record_archive",
                "legacy_night_audit_archive",
                "legacy_accommodation_archive",
            },
        )

    def test_folio_archive_rows_match_source_and_metadata(self):
        spec = get_spec("Folio")
        run = archive_table(
            spec=spec,
            batch_uid="ARC-001",
            operator="ops-archive",
            property_assignments={"9001": self.prop},
        )
        rows = list(LegacyFolioArchive.objects.order_by("id"))
        self.assertEqual([row.id for row in rows], [9001, 9002])
        self.assertEqual(rows[0].amount_text, "688.00")
        self.assertEqual(rows[0].archive_batch, "ARC-001")
        self.assertEqual(run.row_count, 2)
        self.assertEqual((run.min_pk, run.max_pk), (9001, 9002))
        self.assertEqual(run.algorithm_version, ALGORITHM_VERSION)
        # 行摘要与整表摘要都可从归档表重算并按元数据复现。
        self.assertEqual(
            rows[0].verified_checksum,
            row_digest(
                {
                    "id": rows[0].id,
                    "order_id": rows[0].order_id,
                    "entry_type": rows[0].entry_type,
                    "amount_text": rows[0].amount_text,
                    "subject": rows[0].subject,
                    "date_text": rows[0].date_text,
                    "handler": rows[0].handler,
                },
                spec.columns,
            ).hex(),
        )
        recomputed = table_digest(
            list(LegacyFolioArchive.objects.values(*spec.columns).order_by("id")),
            spec.columns,
            "id",
        )
        self.assertEqual(run.table_digest, recomputed)
        self.assertTrue(verify_archive_run(run)["ok"])

    def test_all_four_tables_archive_and_verify(self):
        source_pks = {
            "Folio": "9001",
            "FinanceRecord": "7001",
            "NightAudit": "6001",
            "Accommodation": "8101",
        }
        for spec in ARCHIVE_SPECS:
            run = archive_table(
                spec=spec,
                batch_uid="ARC-ALL",
                operator="ops",
                property_assignments={source_pks[spec.source_model]: self.prop},
            )
            self.assertTrue(verify_archive_run(run)["ok"], msg=spec.archive_table)
            mapped_row = spec.archive_model.objects.get(
                archive_batch="ARC-ALL", pk=source_pks[spec.source_model]
            )
            self.assertEqual(mapped_row.property_id, self.prop.pk)
            unattributed_rows = spec.archive_model.objects.filter(archive_batch="ARC-ALL").exclude(
                pk=source_pks[spec.source_model]
            )
            self.assertTrue(all(row.property_id is None for row in unattributed_rows))
        self.assertEqual(LegacyAccommodationArchive.objects.count(), 1)
        self.assertEqual(LegacyFinanceRecordArchive.objects.count(), 1)
        self.assertEqual(LegacyNightAuditArchive.objects.count(), 1)
        self.assertEqual(LegacyArchiveRun.objects.filter(batch_uid="ARC-ALL").count(), 4)

    def test_same_batch_and_table_archived_once(self):
        spec = get_spec("Folio")
        archive_table(spec=spec, batch_uid="ARC-DUP", operator="ops")
        with self.assertRaises(LegacyArchiveError):
            archive_table(spec=spec, batch_uid="ARC-DUP", operator="ops")

    def test_verify_detects_digest_mismatch(self):
        spec = get_spec("NightAudit")
        run = archive_table(spec=spec, batch_uid="ARC-BAD", operator="ops")
        # 直接改内存对象模拟"元数据被写错"（归档行本身不可变，改不动）。
        run.table_digest = "0" * 64
        result = verify_archive_run(run)
        self.assertFalse(result["ok"])

    def test_archive_rows_are_immutable(self):
        spec = get_spec("Folio")
        archive_table(spec=spec, batch_uid="ARC-RO", operator="ops")
        row = cast(LegacyFolioArchive, LegacyFolioArchive.objects.first())
        row.amount_text = "1.00"
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(ValidationError):
            LegacyFolioArchive.objects.all().update(amount_text="1.00")
        with self.assertRaises(ValidationError):
            LegacyFolioArchive.objects.all().delete()
        with self.assertRaises(ValidationError):
            LegacyFolioArchive.objects.bulk_create([])
        with self.assertRaises(ValidationError):
            LegacyFolioArchive.objects.bulk_update([row], ["amount_text"])
        with self.assertRaises(ValidationError):
            row.delete()

    def test_archive_run_is_immutable(self):
        spec = get_spec("FinanceRecord")
        run = archive_table(spec=spec, batch_uid="ARC-META", operator="ops")
        run.row_count = 99
        with self.assertRaises(ValidationError):
            run.save()
        with self.assertRaises(ValidationError):
            LegacyArchiveRun.objects.all().delete()

    def test_unknown_source_model_rejected(self):
        with self.assertRaises(LegacyArchiveError):
            get_spec("Order")

    def test_summary_is_read_only(self):
        summary = summarize_source(get_spec("Accommodation"))
        self.assertEqual(summary["row_count"], 1)
        self.assertEqual(summary["min_pk"], 8101)
        self.assertEqual(LegacyAccommodationArchive.objects.count(), 0)

    def test_property_snapshot_is_not_inferred_when_mapping_is_absent(self):
        archive_table(spec=get_spec("Accommodation"), batch_uid="ARC-NO-PROP", operator="ops")
        row = LegacyAccommodationArchive.objects.get(pk=8101)
        self.assertIsNone(row.property_id)

    def test_unknown_source_pk_in_property_mapping_is_rejected(self):
        with self.assertRaises(LegacyArchiveError):
            archive_table(
                spec=get_spec("Accommodation"),
                batch_uid="ARC-BAD-PROP",
                operator="ops",
                property_assignments={"999999": self.prop},
            )


@override_settings(DEBUG=True, DJANGO_ENV="test")
class EmptyTableArchiveTests(TestCase):
    """空表归档：摘要为 SHA-256(空串)，verify 仍须判一致（不得因空表误报漂移）。"""

    def test_empty_table_archives_and_verifies(self):
        spec = get_spec("FinanceRecord")
        run = archive_table(spec=spec, batch_uid="ARC-EMPTY", operator="ops")
        self.assertEqual(run.row_count, 0)
        self.assertIsNone(run.min_pk)
        self.assertIsNone(run.max_pk)
        self.assertTrue(verify_archive_run(run)["ok"])


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ArchiveCommandTests(TestCase):
    """`archive_legacy_finance` 命令：dry-run / 执行 / verify。"""

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        _legacy_fixtures()

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("archive_legacy_finance", "--dry-run", stdout=out)
        self.assertIn("[dry-run]", out.getvalue())
        self.assertEqual(LegacyArchiveRun.objects.count(), 0)
        self.assertEqual(LegacyFolioArchive.objects.count(), 0)

    def test_execute_requires_batch_operator_and_approval_ref(self):
        with self.assertRaises(CommandError):
            call_command("archive_legacy_finance", "--execute", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command(
                "archive_legacy_finance",
                "--execute",
                "--batch-uid",
                "ARC-CMD",
                stdout=StringIO(),
            )
        with self.assertRaises(CommandError):
            call_command(
                "archive_legacy_finance",
                "--execute",
                "--batch-uid",
                "ARC-CMD",
                "--operator",
                "ops-cmd",
                stdout=StringIO(),
            )

    def test_execute_then_verify(self):
        out = StringIO()
        call_command(
            "archive_legacy_finance",
            "--batch-uid",
            "ARC-CMD",
            "--execute",
            "--approval-ref",
            "TEST-GOV-ARCHIVE-01",
            "--operator",
            "ops-cmd",
            "--tables",
            "Folio,NightAudit",
            stdout=out,
        )
        self.assertEqual(LegacyArchiveRun.objects.filter(batch_uid="ARC-CMD").count(), 2)
        verify_out = StringIO()
        call_command(
            "archive_legacy_finance",
            "--batch-uid",
            "ARC-CMD",
            "--verify",
            stdout=verify_out,
        )
        self.assertIn("一致", verify_out.getvalue())

    def test_property_map_file_sets_only_explicit_snapshots(self):
        payload = {"Folio": {"9001": "DEFAULT", "9002": "DEFAULT"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "property-map.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            call_command(
                "archive_legacy_finance",
                "--batch-uid",
                "ARC-PROP-CMD",
                "--execute",
                "--approval-ref",
                "TEST-GOV-ARCHIVE-02",
                "--operator",
                "ops-cmd",
                "--tables",
                "Folio",
                "--property-map-file",
                str(path),
                stdout=StringIO(),
            )
        rows = LegacyFolioArchive.objects.filter(archive_batch="ARC-PROP-CMD")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(set(rows.values_list("property_id", flat=True)), {self.prop.pk})

    def test_archive_and_audit_are_atomic(self):
        with patch(
            "apps.billing.management.commands.archive_legacy_finance.AuditEvent.record",
            side_effect=RuntimeError("audit unavailable"),
        ):
            self.assertRaises(
                CommandError,
                call_command,
                "archive_legacy_finance",
                "--batch-uid",
                "ARC-AUDIT-ATOMIC",
                "--execute",
                "--approval-ref",
                "TEST-GOV-ARCHIVE-03",
                "--operator",
                "ops-cmd",
                "--tables",
                "Folio",
                stdout=StringIO(),
            )
        self.assertFalse(LegacyArchiveRun.objects.filter(batch_uid="ARC-AUDIT-ATOMIC").exists())
        self.assertFalse(
            LegacyFolioArchive.objects.filter(archive_batch="ARC-AUDIT-ATOMIC").exists()
        )

    def test_verify_without_runs_fails(self):
        with self.assertRaises(CommandError):
            call_command(
                "archive_legacy_finance",
                "--batch-uid",
                "ARC-NONE",
                "--verify",
                stdout=StringIO(),
            )

    def test_verify_reports_mismatch_as_command_error(self):
        call_command(
            "archive_legacy_finance",
            "--batch-uid",
            "ARC-BAD2",
            "--execute",
            "--approval-ref",
            "TEST-GOV-ARCHIVE-04",
            "--operator",
            "ops",
            "--tables",
            "Folio",
            stdout=StringIO(),
        )
        run = LegacyArchiveRun.objects.get(batch_uid="ARC-BAD2")
        # 绕过只读管理器改元数据（模拟"元数据被写错"，用裸 QuerySet 是唯一绕过方式）。
        django_models.QuerySet(LegacyArchiveRun).filter(pk=run.pk).update(table_digest="f" * 64)
        with self.assertRaises(CommandError):
            call_command(
                "archive_legacy_finance",
                "--batch-uid",
                "ARC-BAD2",
                "--verify",
                stdout=StringIO(),
            )
