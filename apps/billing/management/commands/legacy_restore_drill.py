"""D4 恢复演练命令：归档表 → 隔离库 → 历史查询复现 + 等价性/金额/关联校验。

默认产物落 ``.work/``（项目治理规则：临时产物不进仓库根，也不入库）：
  * 隔离库：``.work/restore-drill-<批次>.sqlite3``
  * 报告：``.work/restore-drill-<批次>-report.json``

本命令只做**可用性演练**。生产 MySQL 恢复须按运维方案在独立实例执行，
并由运维与财务负责人共同确认；本命令不代签、不替代那一步。
"""

from django.core.management.base import BaseCommand, CommandError

from apps.billing.legacy_restore_drill import (
    LegacyRestoreDrillError,
    run_drill,
    write_report,
)


class Command(BaseCommand):
    help = "执行归档恢复演练（隔离库复现 + 摘要/等价性/金额/关联校验），产出可归档报告。"

    def add_arguments(self, parser):
        parser.add_argument("--batch-uid", required=True, help="归档批次号")
        parser.add_argument(
            "--scratch", default="", help="隔离库路径（默认 .work/restore-drill-<批次>.sqlite3）"
        )
        parser.add_argument(
            "--report-out", default="", help="报告 JSON 路径（默认 .work/...-report.json）"
        )
        parser.add_argument("--operator", required=True, help="演练执行人（登记进报告）")
        parser.add_argument(
            "--tables",
            default="",
            help="仅演练指定源表（逗号分隔：folio,finance_record,night_audit,accommodation）",
        )
        parser.add_argument(
            "--fail-on-blocker", action="store_true", help="存在阻断项时以非零退出（D4 收口门禁用）"
        )

    def handle(self, *args, **options):
        uid = options["batch_uid"].strip()
        scratch = options["scratch"].strip() or f".work/restore-drill-{uid}.sqlite3"
        report_out = options["report_out"].strip() or f".work/restore-drill-{uid}-report.json"
        tables = [item.strip() for item in options["tables"].split(",") if item.strip()]
        try:
            report = run_drill(
                batch_uid=uid,
                scratch_path=scratch,
                operator=options["operator"],
                tables=tables or None,
            )
        except LegacyRestoreDrillError as exc:
            raise CommandError(str(exc)) from exc

        for table in report["tables"]:
            if table.get("status") != "checked":
                self.stdout.write(self.style.WARNING(f"{table['source_table']}\t未归档，跳过"))
                continue
            self.stdout.write(
                f"{table['source_table']}\t行数 {table['archived_rows']}/{table['expected_rows']}"
                f"\t摘要 {'一致' if table['digest_ok'] else '不一致'}"
                f"\t等价性 {'通过' if table['equivalence_ok'] else '不通过'}"
                f"\t主键空洞 {len(table['pk_continuity']['gaps'])} 段"
                f"\t源表 {'可读' if table['source_available'] else '不可读（已退役）'}"
            )
        relation = report["relations"].get("folio_order_to_accommodation")
        if relation:
            self.stdout.write(
                f"关联完整率 {relation['link_rate']:.4%}" f"（{relation['missing_rows']} 条孤儿）"
            )
        for amount in report["amounts"]:
            self.stdout.write(
                f"{amount['source_table']} 金额分组："
                f"归档vs隔离库 {'一致' if amount['archive_vs_scratch']['ok'] else '不一致'}；"
                f"归档vs源表 "
                + (
                    "源表不可读"
                    if amount["archive_vs_source"] is None
                    else ("一致" if amount["archive_vs_source"]["ok"] else "不一致")
                )
                + f"；脏金额 {len(amount['bad_amount_rows'])} 条"
            )
        self.stdout.write(f"LegacyRecordLink：{report['links']['status']}")

        target = write_report(report, report_out)
        self.stdout.write(f"报告已写入 {target}")

        if report["blockers"]:
            for blocker in report["blockers"]:
                self.stdout.write(self.style.ERROR(f"阻断项：{blocker}"))
            if options["fail_on_blocker"]:
                raise CommandError("恢复演练存在阻断项，D4 不得收口")
            self.stdout.write(
                self.style.WARNING("演练存在阻断项（未加 --fail-on-blocker 不中断）。")
            )
            return
        self.stdout.write(self.style.SUCCESS("恢复演练通过：摘要、等价性、金额、关联全部一致。"))
