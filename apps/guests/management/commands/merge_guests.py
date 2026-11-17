"""A3 客人合并备用入口：python manage.py merge_guests。

与 API 端点共用 GuestService.merge_guests（唯一写入口，同事务原子执行）：
证件改挂主档案、电话按 phone_hash 去重合并、会员转移留史、预订/住客快照
FK 迁移，duplicate 软归档（merged_into/merged_at）并写 guest.merged 审计。
账务事实（Folio/Payment/LedgerEntry）归属原预订不动。

用法：
    python manage.py merge_guests --primary 12 --duplicate 15 \
        --reason "前台核对为同一客人，重复建档"
"""

from django.core.management.base import BaseCommand, CommandError

from apps.guests.models import Guest
from apps.guests.services import GuestService, GuestServiceError


class Command(BaseCommand):
    help = "把 duplicate 客人档案合并入 primary（A3：软归档 + 全程审计，不改账务事实）。"

    def add_arguments(self, parser):
        parser.add_argument("--primary", type=int, required=True,
                            help="主档案 ID（合并保留方）。")
        parser.add_argument("--duplicate", type=int, required=True,
                            help="待合并档案 ID（软归档并入主档案）。")
        parser.add_argument("--reason", required=True,
                            help="业务理由（1-240 字符），完整进入审计。")

    def handle(self, *args, **options):
        primary_id = options["primary"]
        duplicate_id = options["duplicate"]
        if primary_id == duplicate_id:
            raise CommandError("主档案与待合并档案不能是同一人。")
        primary = Guest.objects.filter(pk=primary_id).first()
        duplicate = Guest.objects.filter(pk=duplicate_id).first()
        if primary is None:
            raise CommandError(f"主档案 {primary_id} 不存在。")
        if duplicate is None:
            raise CommandError(f"待合并档案 {duplicate_id} 不存在。")
        if primary.property != duplicate.property:
            raise CommandError("只能合并同一物业内的客人档案。")

        try:
            result = GuestService.merge_guests(
                primary, duplicate,
                performed_by=None, reason=options["reason"])
        except GuestServiceError as exc:
            raise CommandError(str(exc)) from None

        self.stdout.write(self.style.SUCCESS(
            "客人合并完成：duplicate {duplicate} → primary {primary}；"
            "迁移预订 {reservations}、预订住客 {reservation_guests}、"
            "在住人员 {stay_guests}；电话去重丢弃 {phones}、"
            "证件去重丢弃 {documents}、终止重复有效会员 {memberships}。".format(
                duplicate=result["duplicate_guest_id"],
                primary=result["primary_guest_id"],
                reservations=result["reservations_moved"],
                reservation_guests=result["reservation_guests_moved"],
                stay_guests=result["stay_guests_moved"],
                phones=result["phones_deduped"],
                documents=result["documents_deduped"],
                memberships=result["memberships_terminated"],
            )))
