"""D4-3/M4-3：库存日历滚动覆盖检测（调度证据留痕 + 待办告警）。

口径（apps/inventory/services.calendar_coverage_gaps，与待办派生共用）：
  * 对每个启用房型取 MAX(stay_date)，无库存行视为未覆盖；
  * MAX(stay_date) < 物业时区"今天"+7 天即覆盖不足；
  * 命中时输出告警行到 stdout（scheduler_runner 落 logs/scheduler/ 日志留痕），
    并经 MC-007 任务源 inventory.calendar_coverage 派生待办——不新增通知表；
  * 补齐走唯一同步命令 sync_inventory_calendar（调度每日 02:30 自动滚动），
    覆盖恢复后待办随刷新自动销项；本命令只读检测，不写库存。
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Property
from apps.core.services_notifications import refresh_operational_todos
from apps.inventory.services import calendar_coverage_gaps


class Command(BaseCommand):
    help = "检测库存日历滚动覆盖（MAX(stay_date) < 今天+7 告警），并经 MC-007 派生待办。"

    def add_arguments(self, parser):
        parser.add_argument("--property-code", default="DEFAULT")
        parser.add_argument(
            "--horizon-days",
            type=int,
            default=7,
            help="覆盖阈值：要求日历至少覆盖到今天+N 天（默认 7）",
        )

    def handle(self, *args, **options):
        horizon_days = options["horizon_days"]
        if not 1 <= horizon_days <= 365:
            raise CommandError("horizon-days 必须是 1-365 的整数。")
        try:
            property_obj = Property.objects.get(code=options["property_code"])
        except Property.DoesNotExist as exc:
            raise CommandError("物业不存在。") from exc

        gaps = calendar_coverage_gaps(property_obj, horizon_days=horizon_days)
        if not gaps:
            self.stdout.write(self.style.SUCCESS("日历覆盖检查通过：全部启用房型滚动覆盖达标。"))
        for gap in gaps:
            room_type = gap["room_type"]
            max_stay = gap["max_stay"]
            covered = f"{max_stay:%Y-%m-%d}" if max_stay is not None else "未建立（无库存行）"
            self.stdout.write(
                self.style.WARNING(
                    f"[覆盖不足] {room_type.name}（{room_type.code}）：日历最远 "
                    f"{covered}，要求 ≥ {gap['horizon']:%Y-%m-%d}（今天+{horizon_days} 天）；"
                    f"补齐走 sync_inventory_calendar（调度每日 02:30）。"
                )
            )

        # 覆盖缺口经 task_registry 任务源 inventory.calendar_coverage 对账为
        # 待办/通知（MC-007 机制；刷新幂等，恢复后自动销项）。
        summary = refresh_operational_todos(property_obj)
        self.stdout.write(
            f"待办对账：active={summary['active']} created={summary['created']} "
            f"updated={summary['updated']} resolved={summary['resolved']}。"
        )
