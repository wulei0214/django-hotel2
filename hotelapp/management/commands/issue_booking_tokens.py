# -*- coding: utf-8 -*-
"""P0-08 运维工具：为存量预订补签 Booking Access Token。

仅 dev/test 或显式 --confirm 时执行；输出明文 token 供人工交付（一次性）。
"""
from django.core.management.base import BaseCommand, CommandError

from apps.reservations.models import BookingAccessToken, Reservation


class Command(BaseCommand):
    help = "为缺少访问令牌的预订补签 Booking Access Token（明文仅输出一次）"

    def add_arguments(self, parser):
        parser.add_argument("--booking-no", type=str, default="",
                            help="指定订单号；留空则处理全部无令牌的有效订单")
        parser.add_argument("--confirm", action="store_true",
                            help="确认执行（防误操作）")

    def handle(self, *args, **options):
        from django.conf import settings
        if not options["confirm"]:
            raise CommandError("请加 --confirm 确认执行；明文 token 将直接打印。")
        if not settings.DEBUG and getattr(settings, "DJANGO_ENV", "dev") not in {"dev", "test"}:
            self.stderr.write("警告：正在生产环境签发订单令牌，请确认渠道安全。")
        qs = Reservation.objects.all().order_by("-id")
        if options["booking_no"]:
            qs = qs.filter(booking_no=options["booking_no"].strip().upper())
        count = 0
        for reservation in qs:
            if hasattr(reservation, "access_token"):
                continue
            if reservation.status not in (
                    Reservation.Status.PENDING, Reservation.Status.CONFIRMED,
                    Reservation.Status.CHECKED_IN):
                continue
            plain = BookingAccessToken.issue_for(reservation)
            self.stdout.write("%s  %s" % (reservation.booking_no, plain))
            count += 1
        self.stdout.write(self.style.SUCCESS("共补签 %d 个订单访问令牌" % count))
