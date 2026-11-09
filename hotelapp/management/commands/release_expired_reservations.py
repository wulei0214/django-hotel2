# -*- coding: utf-8 -*-
"""P0-07（方案 §6.4.6）：过期预订库存 hold 释放任务。

幂等/可重试：按状态条件更新（PENDING→EXPIRED），重复执行不重复释放；
每次释放写审计（public.reservation_expired）。可挂 cron 定时执行。
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.reservations.models import Reservation
from apps.inventory.services import release_inventory_hold


class Command(BaseCommand):
    help = "释放超时未支付预订的库存 hold（幂等，可重复执行）"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200,
                            help="单次最多处理笔数")

    def handle(self, *args, **options):
        now = timezone.now()
        limit = options["limit"]
        expired_ids = list(Reservation.objects.filter(
            status=Reservation.Status.PENDING,
            expires_at__lte=now,
        ).values_list("pk", flat=True)[:limit])
        released = 0
        for pk in expired_ids:
            try:
                with transaction.atomic():
                    reservation = (
                        Reservation.objects.select_for_update()
                        .select_related("guest")
                        .get(pk=pk, status=Reservation.Status.PENDING)
                    )
                    if reservation.expires_at and reservation.expires_at <= now:
                        try:
                            release_inventory_hold(reservation)
                        except Exception as exc:
                            # 释放失败不阻塞其余订单；下一轮重试（幂等）
                            self.stderr.write(
                                "hold 释放失败 booking=%s: %s"
                                % (reservation.booking_no, exc))
                            continue
                        reservation.status = Reservation.Status.EXPIRED
                        reservation.save(update_fields=("status", "updated_at"))
                        AuditEvent.record(
                            action="public.reservation_expired",
                            entity_type="Reservation",
                            entity_id=reservation.booking_no,
                            property_obj=reservation.property,
                            reason="hold_ttl_released")
                        released += 1
            except Reservation.DoesNotExist:
                continue
        self.stdout.write("过期预订释放完成：本次处理 %d 笔，成功释放 %d 笔"
                          % (len(expired_ids), released))
