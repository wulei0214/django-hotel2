"""归档层 TDD（批次 3）：四张 legacy_*_archive + checksum-v2 元数据 + 只读守卫。

覆盖归档门禁 §5（结构：源表全列快照 + archived_at + archive_batch + verified_checksum）
与 §4.2（行摘要/整表摘要/元数据登记），以及 D3 收口的 `--verify` 核验路径。
"""

from io import StringIO
from typing import Any, cast

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
    LegacyClientArchive,
    LegacyFinanceRecordArchive,
    LegacyFolioArchive,
    LegacyMigrationBatch,
    LegacyNightAuditArchive,
    LegacyStaffArchive,
    LegacyVipArchive,
)
from apps.core.models import Property


def _legacy_fixtures():
    """构造四张旧表的最小可归档数据（旧表只读，测试里写入夹具）。"""
    from hotelapp.models import Accommodation, Client, FinanceRecord, Folio, NightAudit, Room, Staff

    staff = Staff.objects.create(姓名="归档员工", 职务="服务员", 月薪="3000", 工龄="1")
    room = Room.objects.create(
        房间号=9201, 客房类型="标准间", 价格=300, 负责人编号=staff.pk, 房间状态="空闲"
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

    def test_archive_specs_cover_legacy_tables(self):
        self.assertEqual(
            {spec.archive_table for spec in ARCHIVE_SPECS},
            {
                "legacy_folio_archive",
                "legacy_finance_record_archive",
                "legacy_night_audit_archive",
                "legacy_accommodation_archive",
                "legacy_client_archive",
                "legacy_vip_archive",
                # ARCH-04 C8：旧 staff 表的归档落点
                "legacy_staff_archive",
            },
        )

    def test_folio_archive_rows_match_source_and_metadata(self):
        spec = get_spec("Folio")
        run = archive_table(spec=spec, batch_uid="ARC-001", operator="ops-archive")
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

    def test_archive_rows_carry_registered_batch_property_snapshot(self):
        """FIN-02：物业快照只取自已登记批次的授权范围，不从房间号反推。"""
        batch_uid = "ARC-PROP-1"
        LegacyMigrationBatch.objects.create(
            batch_uid=batch_uid,
            scope_property=self.prop,
            scope_source_models=["Folio"],
        )
        archive_table(spec=get_spec("Folio"), batch_uid=batch_uid, operator="ops")
        rows = list(LegacyFolioArchive.objects.filter(archive_batch=batch_uid))
        self.assertTrue(rows)
        self.assertEqual({row.property_id for row in rows}, {self.prop.pk})

    def test_archive_rows_keep_null_property_for_unregistered_batch(self):
        """未登记来源批次不推定物业，留 NULL 由授权人员逐记录核验后回填。"""
        archive_table(spec=get_spec("Folio"), batch_uid="ARC-NO-BATCH", operator="ops")
        rows = LegacyFolioArchive.objects.filter(archive_batch="ARC-NO-BATCH")
        self.assertTrue(rows.exists())
        self.assertEqual({row.property_id for row in rows}, {None})

    def test_all_seven_tables_archive_and_verify(self):
        # client/vip 夹具不在 _legacy_fixtures 内（各自测试类自建）——本测试补建
        from hotelapp.models import Vip

        Vip.objects.create(姓名="全表归档会员", 会员等级="普通会员", 联系方式="13800007777")
        for spec in ARCHIVE_SPECS:
            run = archive_table(spec=spec, batch_uid="ARC-ALL", operator="ops")
            self.assertTrue(verify_archive_run(run)["ok"], msg=spec.archive_table)
        self.assertEqual(LegacyAccommodationArchive.objects.count(), 1)
        self.assertEqual(LegacyFinanceRecordArchive.objects.count(), 1)
        self.assertEqual(LegacyNightAuditArchive.objects.count(), 1)
        self.assertEqual(LegacyClientArchive.objects.count(), 1)
        self.assertEqual(LegacyVipArchive.objects.count(), 1)
        # ARCH-04 C8：旧员工表同样有归档落点
        self.assertEqual(LegacyStaffArchive.objects.count(), 1)
        self.assertEqual(LegacyArchiveRun.objects.filter(batch_uid="ARC-ALL").count(), 7)

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

    def test_execute_requires_batch_and_operator(self):
        with self.assertRaises(CommandError):
            call_command("archive_legacy_finance", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("archive_legacy_finance", "--batch-uid", "ARC-CMD", stdout=StringIO())

    def test_execute_then_verify(self):
        out = StringIO()
        call_command(
            "archive_legacy_finance",
            "--batch-uid",
            "ARC-CMD",
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


class ClientVipArchiveTests(TestCase):
    property: Any
    legacy_client: Any
    vip: Any
    """TASK-04：client/vip 归档 spec（PII 密文+盲索引门禁版）。

    锁定：①明文不落归档行任何列（V1.1 §3.2）；②密文可解密、盲索引判等；
    ③主键沿用源表主键（顾客id/会员编号）；④归档行不可变；⑤dry-run 零写入。
    """

    @classmethod
    def setUpTestData(cls):
        from hotelapp.models import Client, Vip

        cls.property = Property.objects.get(code="DEFAULT")
        cls.legacy_client = Client.objects.create(姓名="归档顾客甲", 身份证号="110101199001019999")
        cls.vip = Vip.objects.create(姓名="归档会员乙", 会员等级="高级会员", 联系方式="13800009999")

    def test_client_archive_encrypts_pii(self):
        spec = get_spec("Client")
        rows = __import__(
            "apps.billing.legacy_archive", fromlist=["read_source_rows"]
        ).read_source_rows(spec)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # 明文键已弹出
        self.assertNotIn("name_text", row)
        self.assertNotIn("id_no_text", row)
        # 密文可解密还原
        from apps.guests.name_pii import decrypt_guest_name, normalize_guest_name
        from apps.guests.services import decrypt_doc_no

        self.assertEqual(
            decrypt_guest_name(row["name_ciphertext"]), normalize_guest_name("归档顾客甲")
        )
        self.assertEqual(decrypt_doc_no(row["id_no_ciphertext"]), "110101199001019999")
        # 盲索引判等
        from apps.guests.name_pii import guest_name_hash

        self.assertEqual(row["name_hash"], guest_name_hash(normalize_guest_name("归档顾客甲")))
        # 主键沿用源表主键（顾客id）
        self.assertEqual(row["id"], self.legacy_client.pk)

    def test_vip_archive_encrypts_contact(self):
        from apps.billing.legacy_archive import read_source_rows
        from apps.core.phone import normalize_phone
        from apps.guests.name_pii import decrypt_guest_name, normalize_guest_name
        from apps.guests.services import decrypt_phone, phone_hash

        rows = read_source_rows(get_spec("Vip"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotIn("name_text", row)
        self.assertNotIn("contact_text", row)
        self.assertEqual(
            decrypt_guest_name(row["name_ciphertext"]), normalize_guest_name("归档会员乙")
        )
        self.assertEqual(decrypt_phone(row["contact_ciphertext"]), normalize_phone("13800009999"))
        self.assertEqual(row["contact_hash"], phone_hash(normalize_phone("13800009999")))
        self.assertEqual(row["rank_text"], "高级会员")
        self.assertEqual(row["id"], self.vip.pk)

    def test_archive_roundtrip_and_immutability(self):
        spec = get_spec("Client")
        batch_uid = "ARC-CV-1"
        run = archive_table(spec=spec, batch_uid=batch_uid, operator="ops-test")
        self.assertEqual(run.row_count, 1)
        # 核验通过
        verdict = verify_archive_run(run)
        self.assertTrue(verdict["ok"])
        # 归档行不可变
        archived = LegacyClientArchive.objects.get(pk=self.legacy_client.pk)
        with self.assertRaises(ValidationError):
            archived.name_hash = "tampered"
            archived.save()

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command(
            "archive_legacy_finance",
            "--tables",
            "Client,Vip",
            "--dry-run",
            stdout=out,
        )
        self.assertIn("dry-run", out.getvalue())
        self.assertIn("未写入任何归档行", out.getvalue())
        self.assertFalse(LegacyArchiveRun.objects.filter(source_table="client").exists())
        self.assertFalse(LegacyArchiveRun.objects.filter(source_table="vip").exists())
