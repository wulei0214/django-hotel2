# -*- coding: utf-8 -*-
"""RSV-07：旧过期释放命令已退役，统一转发到唯一权威入口。

此前两套命令并存且行为漂移：旧命令写审计但不取消支付意图，新命令反之；
15 分钟 TTL 两处硬编码。为避免运维挂旧任务导致审计/状态不一致，这里
只做薄封装并给出退役提示，实际逻辑一律由
``apps.reservations.management.commands.expire_reservation_holds`` 执行。
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "【已退役】转发到 expire_reservation_holds（唯一权威入口）"

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING(
            "release_expired_reservations 已退役；请改用 "
            "expire_reservation_holds。本次已自动转发。"))
        call_command("expire_reservation_holds")
