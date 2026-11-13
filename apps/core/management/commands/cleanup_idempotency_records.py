"""P-08：IdempotencyRecord 清理（扫描优先 / Dry-run / 审计 / 不误删）。

默认仅扫描并输出审计报告，不删除任何记录；显式 --apply 才执行删除。
用法：
  python manage.py cleanup_idempotency_records            # 仅扫描 + 审计
  python manage.py cleanup_idempotency_records --apply    # 实际删除过期记录
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Property
from apps.core.services import cleanup_idempotency_records


class Command(BaseCommand):
    help = "清理过期 IdempotencyRecord（P-08：扫描优先、dry-run、审计、不误删）。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="实际删除过期记录；省略则仅扫描并输出审计报告，不删除。",
        )
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=1,
            help="过期后额外保留天数（默认 1：删除 expires_at 早于 now-1d 的记录）。",
        )
        parser.add_argument(
            "--batch-size", type=int, default=500, help="每批删除条数（默认 500）。"
        )
        parser.add_argument(
            "--property-id", type=int, default=None, help="仅处理指定物业；省略时处理全部物业。"
        )

    def handle(self, *args, **options):
        property_obj = None
        property_id = options.get("property_id")
        if property_id is not None:
            property_obj = Property.objects.filter(pk=property_id).first()
            if property_obj is None:
                raise CommandError("指定物业不存在。")
        try:
            summary = cleanup_idempotency_records(
                older_than_days=options["older_than_days"],
                dry_run=not options["apply"],
                property_obj=property_obj,
                batch_size=options["batch_size"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from None

        if options["apply"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"P-08 幂等记录清理完成：删除 {summary['deleted']} 条"
                    f"（按状态 {summary['by_status']}）。"
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY-RUN] 未删除任何记录。候选删除 {summary['would_delete']} 条，"
                    f"按状态 {summary['by_status']}；"
                    f"无过期时间记录 {summary['null_expiry_count']} 条"
                    f"（已跳过，需人工复核）。"
                )
            )
