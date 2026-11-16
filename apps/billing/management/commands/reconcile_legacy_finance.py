"""历史迁移对账（分类守恒 + 金额守恒 + 归档摘要），只报告不修账。"""

from django.core.management.base import BaseCommand, CommandError

from apps.billing.legacy_archive import get_spec, verify_archive_run
from apps.billing.legacy_migration import reconcile_batch
from apps.billing.models import LegacyArchiveRun, LegacyMigrationBatch


class Command(BaseCommand):
    help = "对账一个迁移批次：C1~C5 分类守恒、金额守恒、归档摘要一致性（只报告）。"

    def add_arguments(self, parser):
        parser.add_argument("--batch-uid", required=True)
        parser.add_argument(
            "--fail-on-diff",
            action="store_true",
            help="存在差异时以非零退出（CI/收口门禁用）",
        )
        parser.add_argument("--check-archive", action="store_true", help="同时重算归档摘要")

    def handle(self, *args, **options):
        uid = options["batch_uid"].strip()
        batch = LegacyMigrationBatch.objects.filter(batch_uid=uid).first()
        if batch is None:
            raise CommandError(f"批次不存在：{uid}")
        report = reconcile_batch(batch)
        self.stdout.write(f"批次 {uid}（{batch.status}）对账：")
        for source_model, rows in report["categories"].items():
            self.stdout.write(
                f"  {source_model}: "
                + "，".join(
                    f"{key}={value}" for key, value in rows.items() if key != "C1_missing_entries"
                )
            )
            if rows.get("C1_missing_entries"):
                self.stdout.write(
                    self.style.ERROR(f"    C1 映射指向的凭证不存在：{rows['C1_missing_entries']}")
                )
            if rows.get("leaked_new_entries"):
                self.stdout.write(
                    self.style.ERROR(
                        f"    C2/C3 分类泄漏（不该建凭证）：{rows['leaked_new_entries']}"
                    )
                )
        self.stdout.write(f"  金额比对 {len(report['amounts'])} 条")
        for archive in report["archive"]:
            self.stdout.write(
                f"  归档 {archive['source_table']}: {archive['row_count']} 行 "
                f"摘要 {archive['table_digest']}"
            )
            if options["check_archive"]:
                run = LegacyArchiveRun.objects.filter(
                    batch_uid=uid, source_table=archive["source_table"]
                ).first()
                spec = get_spec(run.source_table) if run else None
                if run is not None and spec is not None:
                    result = verify_archive_run(run)
                    if not result["ok"]:
                        report["diffs"].append(f"归档 {run.source_table}: 摘要/行数与元数据不一致")
        if report["diffs"]:
            for diff in report["diffs"]:
                self.stdout.write(self.style.WARNING(f"  差异：{diff}"))
            if options["fail_on_diff"]:
                raise CommandError(f"批次 {uid} 对账存在 {len(report['diffs'])} 项差异")
            return
        self.stdout.write(self.style.SUCCESS("对账无差异。"))
