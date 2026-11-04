# -*- coding: utf-8 -*-
"""AUTH-20：员工邀请过期扫描（方案 §5.2 转换 3：待激活 → 邀请过期）。

用法：python manage.py expire_invitations
把全部 PENDING 且超过 expires_at 的邀请置为 EXPIRED；幂等，可挂定时任务。
"""
from django.core.management.base import BaseCommand

from apps.identity.invitation_services import EmployeeInvitationService


class Command(BaseCommand):
    help = ("将超过有效期的员工邀请标记为 EXPIRED"
            "（AUTH-20 过期扫描；方案 §5.2 转换 3）。")

    def handle(self, *args, **options):
        count = EmployeeInvitationService.expire_stale()
        self.stdout.write(self.style.SUCCESS(f"邀请过期扫描完成：归档 {count} 条"))
