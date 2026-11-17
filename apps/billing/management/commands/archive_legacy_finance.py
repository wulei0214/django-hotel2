"""四张旧财务表归档（归档门禁 §5）：建归档行 + 行摘要 + 整表摘要元数据 + 摘要核验。

只读旧表、只写归档表；``--dry-run`` 全只读、``--verify`` 只重算摘要。
物业快照只接受 ``--property-map-file`` 的逐记录人工确认映射，不从 DEFAULT、房号或批次自动推断。
归档行、批次元数据与审计事件在同一事务提交。
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.billing.legacy_archive import (
    LegacyArchiveError,
    archive_table,
    get_spec,
    read_source_rows,
    summarize_source,
    verify_archive_run,
)
from apps.billing.models import LegacyArchiveRun, LegacyMigrationBatch
from apps.core.models import AuditEvent, Property


class Command(BaseCommand):
    help = "归档旧财务四表（folio/finance_record/night_audit/accommodation）并核验 checksum-v2。"

    def add_arguments(self, parser):
        parser.add_argument("--batch-uid", default="", help="归档批次号（执行时必填）")
        parser.add_argument(
            "--tables",
            default="Folio,FinanceRecord,NightAudit,Accommodation",
            help="逗号分隔的来源模型；默认四张全归档",
        )
        parser.add_argument("--operator", default="", help="执行人（OS 账号/姓名，登记进元数据）")
        parser.add_argument(
            "--property-map-file",
            default="",
            help="逐记录人工确认的物业归属 JSON；未提供的记录 property 快照保持 NULL",
        )
        parser.add_argument("--dry-run", action="store_true", help="只盘点行数，不写任何表")
        parser.add_argument("--verify", action="store_true", help="重算已归档批次的整表摘要")

    def handle(self, *args, **options):
        specs = self._specs(options["tables"])
        if options["verify"]:
            return self._verify(options["batch_uid"])
        try:
            property_assignments = self._load_property_assignments(
                options["property_map_file"], specs
            )
        except LegacyArchiveError as exc:
            raise CommandError(str(exc)) from exc
        if options["dry_run"]:
            return self._dry_run(specs, property_assignments)
        batch_uid = options["batch_uid"].strip()
        if not batch_uid:
            raise CommandError("--batch-uid 必填（归档批次号会写进归档行与元数据）")
        if not options["operator"].strip():
            raise CommandError("--operator 必填（元数据须登记执行人）")
        return self._execute(specs, batch_uid, options["operator"].strip(), property_assignments)

    def _specs(self, raw):
        names = [item.strip() for item in raw.split(",") if item.strip()]
        try:
            return [get_spec(name) for name in names]
        except LegacyArchiveError as exc:
            raise CommandError(str(exc)) from exc

    def _load_property_assignments(self, raw_path, specs):
        """读取逐记录人工确认的物业映射；不自动推断 DEFAULT 或房号归属。"""
        if not str(raw_path or "").strip():
            return {}
        try:
            payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LegacyArchiveError(f"物业映射文件不可读取或 JSON 非法：{exc}") from exc
        if not isinstance(payload, dict):
            raise LegacyArchiveError("物业映射文件根节点必须是对象。")

        allowed = {spec.source_model: spec for spec in specs}
        unknown_models = sorted(set(payload) - set(allowed))
        if unknown_models:
            raise LegacyArchiveError(
                "物业映射包含未在 --tables 中选择的来源模型："
                + ", ".join(unknown_models)
            )

        result = {}
        for source_model, rows_map in payload.items():
            if not isinstance(rows_map, dict):
                raise LegacyArchiveError(f"{source_model} 的物业映射必须是对象（源主键→物业代码）。")
            spec = allowed[source_model]
            source_pks = {str(row["id"]) for row in read_source_rows(spec)}
            unknown_pks = sorted(set(map(str, rows_map)) - source_pks)
            if unknown_pks:
                raise LegacyArchiveError(
                    f"{source_model} 物业映射包含不存在的源主键："
                    + ", ".join(unknown_pks[:20])
                )
            resolved = {}
            for source_pk, property_code in rows_map.items():
                code = str(property_code or "").strip()
                if not code:
                    raise LegacyArchiveError(
                        f"{source_model}#{source_pk} 的物业代码不能为空；未确认记录请省略该键。"
                    )
                try:
                    resolved[str(source_pk)] = Property.objects.get(code=code)
                except Property.DoesNotExist as exc:
                    raise LegacyArchiveError(
                        f"{source_model}#{source_pk} 指向不存在的物业代码 {code!r}"
                    ) from exc
            result[source_model] = resolved
        return result

    def _dry_run(self, specs, property_assignments):
        for spec in specs:
            summary = summarize_source(spec)
            assigned = len(property_assignments.get(spec.source_model, {}))
            self.stdout.write(
                f"[dry-run] {summary['source_table']} → {summary['archive_table']}: "
                f"{summary['row_count']} 行（pk {summary['min_pk']}~{summary['max_pk']}），"
                f"已确认物业 {assigned} 行 / 未确认 {summary['row_count'] - assigned} 行"
            )
        if not property_assignments:
            self.stdout.write(
                self.style.WARNING("未提供物业映射：所有归档行 property 快照保持 NULL，不自动推断。")
            )
        self.stdout.write(self.style.WARNING("dry-run：未写入任何归档行与元数据。"))

    def _verify(self, batch_uid):
        uid = batch_uid.strip()
        runs = LegacyArchiveRun.objects.filter(batch_uid=uid).order_by("source_table")
        if not uid or not runs:
            raise CommandError(f"批次 {uid or '(未提供 --batch-uid)'} 无归档元数据可核验")
        failed = []
        for run in runs:
            result = verify_archive_run(run)
            prefix = "OK" if result["ok"] else "MISMATCH"
            self.stdout.write(
                f"{prefix}\t{run.source_table}\t登记 {result['expected_rows']} 行/"
                f"实测 {result['actual_rows']} 行\t摘要 {result['actual_digest']}"
            )
            if not result["ok"]:
                failed.append(run.source_table)
        if failed:
            raise CommandError("归档摘要不一致：" + ", ".join(failed))
        self.stdout.write(self.style.SUCCESS(f"批次 {uid} 全部表摘要与行数一致。"))

    def _execute(self, specs, batch_uid, operator, property_assignments):
        batch = LegacyMigrationBatch.objects.filter(batch_uid=batch_uid).first()
        if batch is not None and batch.status != LegacyMigrationBatch.Status.ACTIVE:
            raise CommandError(f"批次 {batch_uid} 状态为 {batch.status}：仅 ACTIVE 批次可执行归档")
        errors = []
        for spec in specs:
            try:
                with transaction.atomic():
                    run = archive_table(
                        spec=spec,
                        batch_uid=batch_uid,
                        operator=operator,
                        property_assignments=property_assignments.get(spec.source_model, {}),
                    )
                    AuditEvent.record(
                        action="billing.legacy_archive",
                        entity_type="LegacyArchiveRun",
                        entity_id=str(run.pk),
                        property_obj=batch.scope_property if batch else None,
                        after={
                            "batch_uid": batch_uid,
                            "source_table": spec.source_table,
                            "archive_table": spec.archive_table,
                            "row_count": run.row_count,
                            "table_digest": run.table_digest,
                            "operator": operator,
                            "property_assigned_rows": len(
                                property_assignments.get(spec.source_model, {})
                            ),
                        },
                        reason=f"batch={batch_uid}",
                    )
            except Exception as exc:
                errors.append(f"{spec.source_table}: {exc}")
                continue
            self.stdout.write(
                f"{spec.source_table} → {spec.archive_table}: {run.row_count} 行，"
                f"整表摘要 {run.table_digest}"
            )
        if errors:
            raise CommandError("归档未全部完成：" + "；".join(errors))
        self.stdout.write(self.style.SUCCESS(f"批次 {batch_uid} 归档完成（{len(specs)} 张表）。"))
        self.stdout.write(f"提示：可执行 --verify --batch-uid {batch_uid} 复核摘要。")
