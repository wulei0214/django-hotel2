"""会员有效期到期扫描（MC11-MB-01）。

GuestMembership.Status.EXPIRED 此前无任何置位路径（盘点缺口）：
本命令将 ACTIVE 且 expires_at < 今日 的会员置 EXPIRED，逐条落
membership.expire 审计；重复执行幂等（已到期行不再命中）。
调度口径：每日 03:30（install_scheduler_tasks.ps1 注册）。
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.guests.models import GuestMembership


class Command(BaseCommand):
    help = "将超过有效期（expires_at < 今日）的 ACTIVE 会员置为 EXPIRED 并落审计。"

    def handle(self, *args, **options):
        today = timezone.localdate()
        stale = (
            GuestMembership.objects.select_for_update()
            .filter(
                status=GuestMembership.Status.ACTIVE,
                expires_at__isnull=False,
                expires_at__lt=today,
            )
            .order_by("id")
        )
        expired = 0
        for membership in stale:
            membership.status = GuestMembership.Status.EXPIRED
            membership.save(update_fields=("status", "updated_at"))
            AuditEvent.record(
                action="membership.expire",
                entity_type="GuestMembership",
                entity_id=membership.pk,
                property_obj=membership.property,
                after={
                    "member_no": membership.member_no,
                    "expires_at": str(membership.expires_at),
                },
                reason="有效期到期自动置 EXPIRED",
            )
            expired += 1
        self.stdout.write(self.style.SUCCESS(f"已到期会员 {expired} 条。"))
