"""D4-4/M4-3：库存对账扫描调度入口（每小时）。

复用 reconciliation.scan_inventory_discrepancies 唯一扫描口径：
  * 扫描只报告差异，不自动覆盖库存权威计数（held/sold 只读重算核对）；
  * 重复调度幂等：同一（物业, 房型, 住宿日）只保留一条 OPEN 差异报告，
    重算值变化时原位更新（M4-3 批次语义）；
  * OPEN 差异经 MC-007 任务源 inventory.discrepancy_open 派生 WorkItem
    待办（refresh_operational_todos，不新增第二套待办/通知表）；
  * 排房维度只报告不改派：本命令不触碰 ReservationRoom/排房明细。
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Property
from apps.core.services_notifications import refresh_operational_todos
from apps.inventory.models import InventoryDiscrepancy
from apps.inventory.reconciliation import scan_inventory_discrepancies


class Command(BaseCommand):
    help = "扫描库存对账差异并经 MC-007 派生待办（调度安全：幂等，只报告不修复）。"

    def add_arguments(self, parser):
        parser.add_argument("--property-code", default="DEFAULT")
        parser.add_argument("--days-back", type=int, default=30)

    def handle(self, *args, **options):
        days_back = options["days_back"]
        if not 1 <= days_back <= 180:
            raise CommandError("days_back 必须是 1-180 的整数。")
        try:
            property_obj = Property.objects.get(code=options["property_code"])
        except Property.DoesNotExist as exc:
            raise CommandError("物业不存在。") from exc

        run = scan_inventory_discrepancies(property_obj=property_obj, days_back=days_back)

        open_discs = list(
            InventoryDiscrepancy.objects.filter(
                property=property_obj, status=InventoryDiscrepancy.Status.OPEN
            )
            .select_related("room_type")
            .order_by("stay_date", "id")
        )
        for disc in open_discs:
            self.stdout.write(
                f"差异 #{disc.pk} {disc.room_type.code} "
                f"{disc.stay_date:%Y-%m-%d}：账面 {disc.current_total} / "
                f"重算 {disc.recomputed_total}（差 {disc.diff}），"
                f"held={disc.held_count} sold={disc.sold_count} "
                f"oos={disc.oos_count} → 待办 inventory.discrepancy_open"
            )

        # OPEN 差异经 task_registry 任务源对账为 WorkItem/通知（MC-007 机制，
        # 同一事务 upsert；源差异关闭后待办随刷新自动销项）。
        summary = refresh_operational_todos(property_obj)

        self.stdout.write(
            self.style.SUCCESS(
                f"对账运行 #{run.pk}：扫描 {run.scanned_types} 房型 / "
                f"{run.scanned_dates} 住宿日（回看 {days_back} 天），"
                f"差异 {run.diff_count} 条；待办对账 "
                f"active={summary['active']} created={summary['created']} "
                f"updated={summary['updated']} resolved={summary['resolved']}。"
            )
        )
