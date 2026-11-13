"""A8：历史快照回填（Dry-run / 幂等 / 审计 / 可回滚）。

默认仅扫描并输出报告，不写库；--mode apply 实际回填缺失快照；
--mode clear 回滚（清除 backfilled 标记快照，恢复 NONE 兜底）。
用法：
  python manage.py backfill_early_departure_snapshots                # 仅扫描+报告
  python manage.py backfill_early_departure_snapshots --mode apply  # 回填缺失快照
  python manage.py backfill_early_departure_snapshots --mode clear  # 回滚 backfilled 快照
"""
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Property
from apps.reservations.backfill_services import (
    backfill_early_departure_snapshots,
    clear_backfilled_early_departure_snapshots,
)


class Command(BaseCommand):
    help = "回填 ReservationRoom.early_departure_snapshot 历史快照（A8：Dry-run/幂等/审计/可回滚）。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode", choices=["dry-run", "apply", "clear"], default="dry-run",
            help="dry-run：仅扫描报告（默认）；apply：回填缺失快照；"
                 "clear：回滚 backfilled 标记快照，恢复 NONE 兜底。")
        parser.add_argument(
            "--property-id", type=int, default=None,
            help="仅处理指定物业；省略时处理全部物业。")
        parser.add_argument(
            "--rate-plan-id", type=int, default=None,
            help="仅处理指定房价计划；省略时处理全部。")
        parser.add_argument(
            "--batch-size", type=int, default=500,
            help="分批处理/回滚条数（默认 500）。")

    def handle(self, *args, **options):
        property_obj = None
        property_id = options.get("property_id")
        if property_id is not None:
            property_obj = Property.objects.filter(pk=property_id).first()
            if property_obj is None:
                raise CommandError("指定物业不存在。")
        rate_plan_id = options.get("rate_plan_id")
        batch_size = options["batch_size"]

        mode = options["mode"]
        if mode == "clear":
            try:
                summary = clear_backfilled_early_departure_snapshots(
                    dry_run=False, property_obj=property_obj,
                    rate_plan_id=rate_plan_id, batch_size=batch_size)
            except ValueError as exc:
                raise CommandError(str(exc)) from None
            self.stdout.write(self.style.SUCCESS(
                f"A8 回滚完成：清除 backfilled 快照 {summary['cleared']} 条。"))
            return

        try:
            summary = backfill_early_departure_snapshots(
                dry_run=(mode == "dry-run"), property_obj=property_obj,
                rate_plan_id=rate_plan_id, batch_size=batch_size)
        except ValueError as exc:
            raise CommandError(str(exc)) from None

        if mode == "apply":
            self.stdout.write(self.style.SUCCESS(
                f"A8 历史快照回填完成：回填 {summary['backfilled']} 条"
                f"（按政策类型 {summary['by_policy_type']}）。"))
        else:
            self.stdout.write(self.style.WARNING(
                f"[DRY-RUN] 未写入任何记录。扫描 {summary['scanned_reservations']} 个预订，"
                f"待回填 {summary['would_backfill']} 条"
                f"（按政策类型 {summary['by_policy_type']}）。"
                f"确认无误后加 --mode apply 执行。"))
