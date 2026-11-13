# -*- coding: utf-8 -*-
"""定时关闭审批超时的退款申请，释放原支付可退额度。"""
from django.core.management.base import BaseCommand, CommandError

from apps.billing.services_v2 import BillingService
from apps.core.models import Property


class Command(BaseCommand):
    help = "关闭审批超时退款并同步 ApprovalRecord、Refund、Payment。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=100,
            help="单次最多处理条数（1-500，默认 100）。")
        parser.add_argument(
            "--property-id", type=int, default=None,
            help="仅处理指定物业；省略时处理全部物业。")

    def handle(self, *args, **options):
        property_obj = None
        property_id = options.get("property_id")
        if property_id is not None:
            property_obj = Property.objects.filter(pk=property_id).first()
            if property_obj is None:
                raise CommandError("指定物业不存在。")
        try:
            expired_ids = BillingService.expire_due_refunds(
                property_obj=property_obj, limit=options["limit"])
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(
            "本次关闭 %s 笔审批超时退款。" % len(expired_ids)))
        if expired_ids:
            self.stdout.write("退款申请 ID：" + ", ".join(map(str, expired_ids)))
