from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from apps.core.models import Property
from apps.inventory.models import Room, RoomType, RoomTypeInventory


class Command(BaseCommand):
    help = "按物理房间主数据初始化/同步指定日期区间的房型库存。"

    def add_arguments(self, parser):
        parser.add_argument("--property-code", default="DEFAULT")
        parser.add_argument("--room-type-code", required=True)
        parser.add_argument("--start-date", required=True)
        parser.add_argument("--end-date", required=True, help="包含结束日期")

    def handle(self, *args, **options):
        try:
            start = date.fromisoformat(options["start_date"])
            end = date.fromisoformat(options["end_date"])
        except ValueError as exc:
            raise CommandError("日期必须使用 YYYY-MM-DD 格式。") from exc
        if end < start:
            raise CommandError("结束日期不能早于开始日期。")

        try:
            property_obj = Property.objects.get(code=options["property_code"])
            room_type = RoomType.objects.get(
                property=property_obj,
                code=options["room_type_code"],
                is_active=True,
            )
        except (Property.DoesNotExist, RoomType.DoesNotExist) as exc:
            raise CommandError("物业或启用中的房型不存在。") from exc

        room_counts = Room.objects.filter(
            property=property_obj,
            room_type=room_type,
            is_active=True,
        ).aggregate(
            total=Count("id"),
            unavailable=Count(
                "id",
                filter=Q(maintenance_status__in=[
                    Room.MaintenanceStatus.OUT_OF_ORDER,
                    Room.MaintenanceStatus.OUT_OF_SERVICE,
                ]),
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

        self.stdout.write(
            self.style.SUCCESS(
                f"已同步 {room_type.name}：{start} 至 {end}，共 {count} 个住宿日；"
                f"总房量 {total}，停用 {out_of_service}。"
            )
        )
