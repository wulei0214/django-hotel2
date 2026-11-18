from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from apps.core.models import Property
from apps.inventory.models import Room, RoomType, RoomTypeInventory


def _sync_room_type_calendar(property_obj, room_type, start, end):
    """按物理房间主数据同步单一房型指定日期区间的日库存。

    D4-3/M4-3：滚动生成的唯一实现（单房型与 --all-room-types 共用）；
    每个住宿日 get_or_create + 容量校验（total 不小于 Hold/Sold/OOS 合计），
    重复执行幂等，可安全挂调度。
    """
    room_counts = Room.objects.filter(
        property=property_obj,
        room_type=room_type,
        is_active=True,
    ).aggregate(
        total=Count("id"),
        unavailable=Count(
            "id",
            filter=Q(
                maintenance_status__in=[
                    Room.MaintenanceStatus.OUT_OF_ORDER,
                    Room.MaintenanceStatus.OUT_OF_SERVICE,
                ]
            ),
        ),
    )
    total = room_counts["total"] or 0
    out_of_service = room_counts["unavailable"] or 0
    if total == 0:
        raise CommandError("该房型没有启用的物理房间，不能生成库存。")

    count = 0
    current = start
    while current <= end:
        with transaction.atomic():
            stock, created = RoomTypeInventory.objects.select_for_update().get_or_create(
                property=property_obj,
                room_type=room_type,
                stay_date=current,
                defaults={
                    "total_rooms": total,
                    "out_of_service": out_of_service,
                },
            )
            if not created:
                used = stock.held + stock.sold
                if total < used + out_of_service:
                    raise CommandError(
                        f"{current} 库存不足以容纳当前 Hold/Sold，请先处理预订或维修状态。"
                    )
                stock.total_rooms = total
                stock.out_of_service = out_of_service
                stock.version += 1
                stock.save(
                    update_fields=(
                        "total_rooms",
                        "out_of_service",
                        "version",
                        "updated_at",
                    )
                )
        count += 1
        current += timedelta(days=1)
    return count, total, out_of_service


class Command(BaseCommand):
    help = (
        "按物理房间主数据初始化/同步指定日期区间的房型库存。"
        "默认同步 --room-type-code 指定房型；--all-room-types 同步该物业全部"
        "启用房型（D4-3/M4-3 调度滚动口径，无启用房间的房型跳过并告警）。"
    )

    def add_arguments(self, parser):
        parser.add_argument("--property-code", default="DEFAULT")
        parser.add_argument("--room-type-code", default=None)
        parser.add_argument(
            "--all-room-types",
            action="store_true",
            help="同步该物业全部启用房型（与 --room-type-code 互斥）",
        )
        parser.add_argument("--start-date", required=True)
        parser.add_argument("--end-date", required=True, help="包含结束日期")

    def handle(self, *args, **options):
        if options["all_room_types"] and options["room_type_code"]:
            raise CommandError("--all-room-types 与 --room-type-code 互斥，二选一。")
        if not options["all_room_types"] and not options["room_type_code"]:
            raise CommandError("必须指定 --room-type-code 或使用 --all-room-types。")

        try:
            start = date.fromisoformat(options["start_date"])
            end = date.fromisoformat(options["end_date"])
        except ValueError as exc:
            raise CommandError("日期必须使用 YYYY-MM-DD 格式。") from exc
        if end < start:
            raise CommandError("结束日期不能早于开始日期。")

        try:
            property_obj = Property.objects.get(code=options["property_code"])
        except Property.DoesNotExist as exc:
            raise CommandError("物业不存在。") from exc

        if options["all_room_types"]:
            room_types = list(
                RoomType.objects.filter(property=property_obj, is_active=True).order_by("code")
            )
            if not room_types:
                raise CommandError("该物业没有启用中的房型。")
        else:
            try:
                room_types = [
                    RoomType.objects.get(
                        property=property_obj,
                        code=options["room_type_code"],
                        is_active=True,
                    )
                ]
            except RoomType.DoesNotExist as exc:
                raise CommandError("物业或启用中的房型不存在。") from exc

        failed = []
        for room_type in room_types:
            try:
                count, total, out_of_service = _sync_room_type_calendar(
                    property_obj, room_type, start, end
                )
            except CommandError as exc:
                if not options["all_room_types"]:
                    # 单房型模式保持既有失败语义：显式报错、非零退出。
                    raise
                # D4-3：调度滚动不因单一房型失败中断其他房型；
                # 残留缺口由 check_inventory_calendar_coverage 次日告警兜底。
                failed.append(str(room_type.code))
                self.stdout.write(
                    self.style.WARNING(f"[跳过] {room_type.name}（{room_type.code}）：{exc}")
                )
                continue
            self.stdout.write(
                self.style.SUCCESS(
                    f"已同步 {room_type.name}：{start} 至 {end}，共 {count} 个住宿日；"
                    f"总房量 {total}，停用 {out_of_service}。"
                )
            )

        if failed:
            self.stdout.write(
                self.style.WARNING(f"未完成房型：{'、'.join(failed)}（容量校验未过或无启用房间）。")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"全部 {len(room_types)} 个启用房型日历已滚动至 {end}。")
            )
