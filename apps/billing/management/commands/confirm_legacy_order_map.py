"""确认/查看旧订单-新预订关系映射（迁移方案 §1.5.2，追加式版本链）。

用法：
  * 确认一对关系：``--order 100 --reservation 201 --property DEFAULT --confirmed-by zhang --basis "客人+日期+房型"`
  * 改映射（版本演进）：加 ``--supersedes <被取代关系行 id>``，旧行原样保留
  * 查看当前有效关系：``--list``（可按 ``--property`` / ``--order`` 过滤）
  * 查看某旧订单版本链：``--chain 100``
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.billing.legacy_order_map import (
    LegacyOrderMapError,
    confirm_pair,
    current_map_for_order,
    current_rows,
    superseded_chain,
)
from apps.billing.models import LegacyOrderReservationMap
from apps.core.models import Property


def _user(username):
    user = get_user_model().objects.filter(username=username).first()
    if user is None:
        raise CommandError(f"账号不存在：{username}")
    return user


def _property(code):
    prop = Property.objects.filter(code=code).first()
    if prop is None:
        raise CommandError(f"物业不存在：{code or '(未提供 --property)'}")
    return prop


class Command(BaseCommand):
    help = "确认旧订单-新预订关系映射（创建即不可变；改映射追加 supersedes 新行）。"

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="列出当前有效关系")
        parser.add_argument("--chain", type=int, default=None, help="查看某旧订单的版本链")
        parser.add_argument("--order", type=int, default=None, help="旧订单 id")
        parser.add_argument("--reservation", type=int, default=None, help="新预订 id")
        parser.add_argument("--property", default="", help="物业编码，如 DEFAULT")
        parser.add_argument("--confirmed-by", default="", help="确认人账号")
        parser.add_argument("--basis", default="", help="匹配依据（必填）")
        parser.add_argument(
            "--relation-type",
            default=LegacyOrderReservationMap.RelationType.ONE_TO_ONE,
            choices=LegacyOrderReservationMap.RelationType.values,
        )
        parser.add_argument("--supersedes", type=int, default=None, help="被取代的关系行 id")

    def handle(self, *args, **options):
        if options["list"]:
            return self._list(options)
        if options["chain"] is not None:
            return self._chain(options)
        return self._confirm(options)

    def _list(self, options):
        prop = _property(options["property"]) if options["property"].strip() else None
        queryset = current_rows(property_obj=prop)
        if options["order"] is not None:
            queryset = queryset.filter(legacy_order_id=options["order"])
        rows = list(queryset.select_related("property", "confirmed_by").order_by("id"))
        if not rows:
            self.stdout.write("尚无当前有效关系")
            return
        for row in rows:
            self.stdout.write(
                f"#{row.pk}\t旧订单 {row.legacy_order_id} → 预订 {row.reservation_id}\t"
                f"{row.property.code}\t{row.relation_type}\t"
                f"confirmed_by={getattr(row.confirmed_by, 'username', '-')}\t{row.match_basis}"
            )

    def _chain(self, options):
        prop = _property(options["property"]) if options["property"].strip() else None
        rows = superseded_chain(options["chain"], property_obj=prop)
        if not rows:
            self.stdout.write(f"旧订单 {options['chain']} 尚无关系映射")
            return
        current = current_map_for_order(options["chain"], property_obj=prop)
        for row in rows:
            marker = "当前有效" if current is not None and row.pk == current.pk else "历史"
            self.stdout.write(
                f"#{row.pk}\t{marker}\t预订 {row.reservation_id}\t{row.relation_type}\t"
                f"supersedes={row.supersedes_id or '-'}\t{row.match_basis}"
            )

    def _confirm(self, options):
        if options["order"] is None or options["reservation"] is None:
            raise CommandError("须提供 --order 与 --reservation")
        if not options["confirmed_by"].strip():
            raise CommandError("--confirmed-by 必填（人工确认留痕）")
        prop = _property(options["property"])
        supersedes = None
        if options["supersedes"] is not None:
            supersedes = LegacyOrderReservationMap.objects.filter(pk=options["supersedes"]).first()
            if supersedes is None:
                raise CommandError(f"被取代的关系行不存在：#{options['supersedes']}")
        try:
            row = confirm_pair(
                legacy_order_id=options["order"],
                reservation_id=options["reservation"],
                property_obj=prop,
                confirmed_by=_user(options["confirmed_by"]),
                match_basis=options["basis"],
                relation_type=options["relation_type"],
                supersedes=supersedes,
            )
        except LegacyOrderMapError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"已确认关系 #{row.pk}：旧订单 {row.legacy_order_id} → 预订 {row.reservation_id}"
                + (f"（取代 #{row.supersedes_id}）" if row.supersedes_id else "")
            )
        )
