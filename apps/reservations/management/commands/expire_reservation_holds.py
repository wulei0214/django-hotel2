from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.inventory.services import InventoryUnavailable, release_inventory_hold
from apps.reservations.models import Reservation


class Command(BaseCommand):
    help = "释放超过 15 分钟支付期限的待支付预订库存。"

    def handle(self, *args, **options):
        now = timezone.now()
        ids = list(
            Reservation.objects.filter(
                status=Reservation.Status.PENDING,
                expires_at__lte=now,
            ).order_by("id").values_list("id", flat=True)
        )
        expired = 0
        for reservation_id in ids:
            try:
                with transaction.atomic():
                    reservation = Reservation.objects.select_for_update().get(
                        pk=reservation_id
                    )
                    if (
                        reservation.status != Reservation.Status.PENDING
                        or reservation.expires_at is None
                        or reservation.expires_at > timezone.now()
                    ):
                        continue
                    release_inventory_hold(reservation)
                    reservation.status = Reservation.Status.EXPIRED
                    reservation.save(update_fields=("status", "updated_at"))
                    expired += 1
            except InventoryUnavailable as exc:
                self.stderr.write(
                    self.style.ERROR(f"预订 {reservation_id} 释放失败：{exc}")
                )
        self.stdout.write(self.style.SUCCESS(f"已释放 {expired} 笔超时预订的库存。"))
