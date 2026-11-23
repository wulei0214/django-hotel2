"""D4 恢复演练的 TDD（归档门禁 D4 / §4.1 第 4 条 / §4.2 跨库一致性）。

覆盖：
* 演练通过路径：四表摘要复核 + 隔离库复现 + 等价性 + 金额分组一致 + 关联完整；
* 摘要篡改可检出（不静默通过）；
* 主键空洞如实登记（旧表历史删除留下的空洞不算失败，但必须列出）；
* 关联缺失（folio 指向不存在的旧订单）进入阻断项；
* 命令：正常出报告；``--fail-on-blocker`` 时阻断项以非零退出；
* 未知批次与缺执行人明确报错（不产出"看起来成功"的空报告）。
"""

import tempfile
from datetime import datetime
from datetime import timezone as dt_timezone
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db.models import QuerySet
from django.test import TestCase, override_settings

from apps.billing.legacy_archive import ARCHIVE_SPECS, archive_table
from apps.billing.legacy_restore_drill import (
    LegacyRestoreDrillError,
    run_drill,
    write_report,
)
from apps.billing.models import LegacyAccommodationArchive, LegacyArchiveRun, LegacyMigrationBatch
from apps.core.models import Property


def _make_legacy_fixtures():
    """旧表夹具：folio 主键留一个空洞（9202）；一条订单、一条收入、一条夜审。"""
    from hotelapp.models import (
        Accommodation,
        Client,
        FinanceRecord,
        NightAudit,
        Room,
        Staff,
    )
    from hotelapp.models import (
        Folio as LegacyFolio,
    )

    staff = Staff.objects.create(姓名="演练员工", 职务="服务员", 月薪="3000", 工龄="1")
    room = Room.objects.create(
        房间号=9701, 客房类型="标准间", 价格=300, 负责人编号=staff, 房间状态="空闲"
    )
    client = Client.objects.create(姓名="演练顾客", 身份证号="110101199004015555")
    order = Accommodation.objects.create(
        id=8801,
        顾客id=client,
        客房编号=room,
        状态="已退房",
        入住时间="2026-03-01",
        联系电话="13800001234",
    )
    LegacyFolio.objects.create(
        id=9201, order=order, 类型="房费", 金额="688.00", 事由="旧房费", 日期="2026-03-01"
    )
    LegacyFolio.objects.create(
        id=9203, order=order, 类型="收款", 金额="688.00", 事由="旧收款", 日期="2026-03-01"
    )
    FinanceRecord.objects.create(
        id=7301, 类型="收入", 金额="100.00", 事由="旧收入", 关联订单="8801", 日期="2026-03-01"
    )
    NightAudit.objects.create(
        id=6301,
        营业日期="2026-03-01",
        在住单数=1,
        过租总额="688.00",
        未到处理数=0,
        操作人="旧夜审员",
        执行时间="2026-03-02 01:00:00",
    )
    return order


@override_settings(DEBUG=True, DJANGO_ENV="test")
class LegacyRestoreDrillTests(TestCase):
    prop: Property
    batch_uid: str
    scratch_dir: str

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.batch_uid = "DRILL-1"
        executor = User.objects.create_user(username="drill-ops", password="x")
        LegacyMigrationBatch.objects.create(
            batch_uid=cls.batch_uid,
            scope_property=cls.prop,
            scope_source_models=["Folio", "FinanceRecord"],
            scope_record_range={"pk_min": 1, "pk_max": 999999},
            executed_by=executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )
        _make_legacy_fixtures()
        for spec in ARCHIVE_SPECS:
            archive_table(spec=spec, batch_uid=cls.batch_uid, operator="归档执行人")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.scratch = Path(self._tmp.name) / "scratch.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def _drill(self, *, batch_uid=None, operator="演练执行人", tables=None, scratch_path=None):
        """演练入口：默认用本类夹具的批次/隔离库，可逐项覆盖。"""
        return run_drill(
            batch_uid=self.batch_uid if batch_uid is None else batch_uid,
            scratch_path=self.scratch if scratch_path is None else scratch_path,
            operator=operator,
            tables=tables,
        )

    # --- 通过路径 ---------------------------------------------------------
    def test_drill_passes_for_healthy_archive(self):
        report = self._drill()
        self.assertTrue(report["ok"], report["blockers"])
        self.assertEqual(len(report["tables"]), 6)
        for table in report["tables"]:
            self.assertEqual(table["status"], "checked")
            self.assertTrue(table["digest_ok"])
            self.assertTrue(table["equivalence_ok"])
            self.assertEqual(table["archived_rows"], table["restored_rows"])
        self.assertTrue(self.scratch.exists())

    def test_scratch_rows_match_archive(self):
        report = self._drill()
        folio = next(t for t in report["tables"] if t["source_table"] == "folio")
        self.assertEqual(folio["archived_rows"], 2)
        self.assertEqual(folio["source_row_count"], 2)

    def test_pk_gaps_reported(self):
        """旧表主键空洞（9202）如实登记，不当作失败。"""
        report = self._drill()
        folio = next(t for t in report["tables"] if t["source_table"] == "folio")
        self.assertEqual(folio["pk_continuity"]["gaps"], [[9202, 9202]])
        self.assertTrue(folio["ok"])

    def test_amount_grouping_consistent(self):
        report = self._drill()
        folio = next(a for a in report["amounts"] if a["source_table"] == "folio")
        self.assertTrue(folio["archive_vs_scratch"]["ok"])
        self.assertTrue(folio["archive_vs_source"]["ok"])
        self.assertEqual(folio["group_field"], "order_id")

    def test_relation_link_rate_full(self):
        report = self._drill()
        relation = report["relations"]["folio_order_to_accommodation"]
        self.assertEqual(relation["link_rate"], 1.0)
        self.assertTrue(relation["ok"])

    def test_link_check_not_applicable_without_links(self):
        report = self._drill()
        self.assertEqual(report["links"]["status"], "not_applicable")
        self.assertTrue(report["links"]["ok"])

    def test_report_written_to_disk(self):
        report = self._drill()
        target = write_report(report, Path(self._tmp.name) / "nested" / "report.json")
        self.assertTrue(target.exists())
        self.assertIn("batch_uid", target.read_text(encoding="utf-8"))

    # --- 失败路径 ---------------------------------------------------------
    def test_tampered_archive_detected(self):
        """篡改归档行（显式用 QuerySet(Model) 绕过不可变守卫）→ 摘要复核失败。"""
        QuerySet(LegacyAccommodationArchive).filter(archive_batch=self.batch_uid).update(
            contact_phone="13700007777"
        )
        report = self._drill()
        self.assertFalse(report["ok"])
        accommodation = next(t for t in report["tables"] if t["source_table"] == "accommodation")
        self.assertFalse(accommodation["digest_ok"])
        self.assertTrue(any("accommodation" in blocker for blocker in report["blockers"]))

    def test_orphan_order_detected(self):
        """删掉 accommodation 归档行（绕过守卫）→ folio 关系完整率下降并进阻断项。"""
        QuerySet(LegacyAccommodationArchive).filter(archive_batch=self.batch_uid).delete()
        report = self._drill()
        relation = report["relations"]["folio_order_to_accommodation"]
        self.assertEqual(relation["link_rate"], 0.0)
        self.assertFalse(relation["ok"])
        self.assertTrue(any("folio" in blocker for blocker in report["blockers"]))

    def test_missing_batch_rejected(self):
        with self.assertRaises(LegacyRestoreDrillError):
            self._drill(batch_uid="DRILL-NOPE")

    def test_missing_operator_rejected(self):
        with self.assertRaises(LegacyRestoreDrillError):
            self._drill(operator="  ")

    def test_table_subset_supported(self):
        report = self._drill(tables=["folio"])
        self.assertEqual(len(report["tables"]), 1)
        self.assertEqual(report["tables"][0]["source_table"], "folio")

    # --- 命令 -------------------------------------------------------------
    def test_command_writes_report(self):
        report_path = Path(self._tmp.name) / "report.json"
        out = StringIO()
        call_command(
            "legacy_restore_drill",
            "--batch-uid",
            self.batch_uid,
            "--operator",
            "演练执行人",
            "--scratch",
            str(self.scratch),
            "--report-out",
            str(report_path),
            stdout=out,
        )
        self.assertTrue(report_path.exists())
        self.assertIn("恢复演练通过", out.getvalue())

    def test_command_fail_on_blocker(self):
        QuerySet(LegacyAccommodationArchive).filter(archive_batch=self.batch_uid).update(
            contact_phone="13700007777"
        )
        with self.assertRaises(CommandError):
            call_command(
                "legacy_restore_drill",
                "--batch-uid",
                self.batch_uid,
                "--operator",
                "演练执行人",
                "--scratch",
                str(self.scratch),
                "--report-out",
                str(Path(self._tmp.name) / "report.json"),
                "--fail-on-blocker",
                stdout=StringIO(),
            )

    def test_command_unknown_batch_errors(self):
        with self.assertRaises(CommandError):
            call_command(
                "legacy_restore_drill",
                "--batch-uid",
                "DRILL-NOPE",
                "--operator",
                "演练执行人",
                "--scratch",
                str(self.scratch),
                stdout=StringIO(),
            )

    def test_archive_run_metadata_present(self):
        runs = LegacyArchiveRun.objects.filter(batch_uid=self.batch_uid)
        self.assertEqual(runs.count(), 6)
        self.assertTrue(all(run.algorithm_version == "checksum-v2" for run in runs))
        self.assertTrue(all(run.computed_at <= datetime.now(dt_timezone.utc) for run in runs))
