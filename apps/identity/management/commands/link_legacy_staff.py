"""ARCH-04 B1 / C11：旧员工映射台账的离线盘点与登记命令。

**这是唯一允许读取旧 `staff` 表的新增入口**（C1 例外清单②：一次性盘点/映射
命令）。放在 `apps/identity` 而不是 `hotelapp`，是因为映射台账与新员工档案同
属员工身份域；但旧表的读取必须留在这类**离线、无请求上下文**的命令里——
在线业务路径一律不得触碰旧表（C1）。

用法
----
    # ① 盘点：未映射 / 已映射 / 映射失效（旧行已清理）三类清单（只读，不改库）
    python manage.py link_legacy_staff --report

    # ② 登记一条映射（人工确认后执行；幂等）
    python manage.py link_legacy_staff --link 12 --profile 34 --property DEFAULT

    # ③ 恢复一条失效映射（旧行被清理过、台账行仍在）
    python manage.py link_legacy_staff --recover 12

安全边界
--------
* 不删除、不修改旧 `staff` 表任何一列；
* 不新增任何旧表写路径；
* 登记/恢复都走 `EmployeeLifecycleService`（映射唯一 + 审计 + 台账不可删除）；
* `--report` 纯只读，可在生产库上跑（不写任何行）。
"""

from typing import cast

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.models import Property
from apps.identity.models import (
    LegacyStaffMapping,
    StaffProfile,
)
from apps.identity.services import EmployeeLifecycleError, EmployeeLifecycleService


class Command(BaseCommand):
    help = "旧员工（hotelapp.staff）与新员工档案的映射台账：盘点 / 登记 / 恢复。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--report",
            action="store_true",
            help="只读盘点：输出未映射 / 已映射 / 映射失效三类清单（不改库）",
        )
        parser.add_argument(
            "--link",
            type=int,
            metavar="LEGACY_PK",
            help="登记映射：把指定旧员工主键映射到指定新档案",
        )
        parser.add_argument(
            "--profile",
            type=int,
            metavar="PROFILE_PK",
            help="配合 --link 使用：新员工档案主键",
        )
        parser.add_argument(
            "--recover",
            type=int,
            metavar="LEGACY_PK",
            help="恢复映射：台账行存在但旧行指向已失效时重新指向",
        )
        parser.add_argument(
            "--property-code",
            default="DEFAULT",
            help="物业代码（默认 DEFAULT）",
        )
        parser.add_argument(
            "--match-method",
            default="人工确认",
            help="匹配方式（写入台账，默认「人工确认」）",
        )
        parser.add_argument("--reason", default="", help="变更原因（写入台账备注与审计）")

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        from hotelapp.models import Staff  # 旧表：仅本离线命令允许读取

        did_something = False
        if options["report"]:
            self._report(Staff)
            did_something = True
        if options["link"] is not None:
            self._link(options, Staff)
            did_something = True
        if options["recover"] is not None:
            self._recover(options, Staff)
            did_something = True
        if not did_something:
            raise CommandError("请至少指定 --report / --link / --recover 之一")

    # ------------------------------------------------------------------
    def _report(self, Staff):
        """只读盘点：未映射 / 已映射 / 映射失效三类（C11 裁定材料的数据来源）。"""
        linked_profiles = set(
            StaffProfile.objects.exclude(legacy_staff_id=None).values_list(
                "legacy_staff_id", flat=True
            )
        )
        mapped_legacy_pks = set(LegacyStaffMapping.objects.values_list("legacy_pk", flat=True))
        legacy_rows = list(Staff.objects.order_by("pk"))

        unmapped = [
            row
            for row in legacy_rows
            if row.pk not in linked_profiles and row.pk not in mapped_legacy_pks
        ]
        dangling = [
            row
            for row in legacy_rows
            if row.pk in mapped_legacy_pks
            and not LegacyStaffMapping.objects.filter(
                legacy_pk=row.pk, legacy_staff__isnull=False
            ).exists()
        ]
        self.stdout.write(f"旧员工总行数：{len(legacy_rows)}")
        self.stdout.write(f"已映射（新档案外键）：{len(linked_profiles)}")
        self.stdout.write(f"台账登记过的旧主键：{len(mapped_legacy_pks)}")
        self.stdout.write(f"未映射（C11 待裁定）：{len(unmapped)}")
        self.stdout.write(f"映射失效（台账在、旧行指向为空）：{len(dangling)}")
        if unmapped:
            self.stdout.write("— 未映射明细（旧主键 / 姓名 / 职务 / 月薪是否非空）—")
            for row in unmapped[:200]:
                self.stdout.write(
                    f"  Staff#{row.pk} {row.姓名} / {row.职务} / "
                    f"月薪={'有' if row.月薪 else '无'}"
                )
        if dangling:
            self.stdout.write("— 映射失效明细（旧主键）—")
            for row in dangling[:200]:
                self.stdout.write(f"  Staff#{row.pk}")

    # ------------------------------------------------------------------
    @transaction.atomic
    def _link(self, options, Staff):
        legacy_pk = options["link"]
        profile_pk = options["profile"]
        if not profile_pk:
            raise CommandError("--link 必须同时提供 --profile（新员工档案主键）")
        try:
            legacy = Staff.objects.get(pk=legacy_pk)
        except Staff.DoesNotExist as exc:
            raise CommandError(f"旧员工记录不存在：Staff#{legacy_pk}") from exc
        try:
            profile = StaffProfile.objects.get(pk=profile_pk)
        except StaffProfile.DoesNotExist as exc:
            raise CommandError(f"新员工档案不存在：StaffProfile#{profile_pk}") from exc
        try:
            prop = Property.objects.get(code=options["property_code"])
        except Property.DoesNotExist as exc:
            raise CommandError(f"物业不存在：{options['property_code']}") from exc
        try:
            mapping = EmployeeLifecycleService.bind_legacy_staff_mapping(
                profile,
                legacy_staff=legacy,
                property_obj=prop,
                matched_by=None,
                match_method=options["match_method"],
                reason=options["reason"],
            )
        except EmployeeLifecycleError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"已登记映射：Staff#{legacy_pk} → StaffProfile#{profile_pk}"
                f"（台账 #{mapping.pk}，状态 {mapping.mapping_status}）"
            )
        )

    # ------------------------------------------------------------------
    @transaction.atomic
    def _recover(self, options, Staff):
        legacy_pk = options["recover"]
        mapping = LegacyStaffMapping.objects.filter(legacy_pk=legacy_pk).first()
        if mapping is None:
            raise CommandError(f"没有 legacy_pk={legacy_pk} 的映射台账行")
        legacy = Staff.objects.filter(pk=legacy_pk).first()
        try:
            mapping = EmployeeLifecycleService.recover_legacy_staff_mapping(
                mapping,
                legacy_staff=legacy,
                matched_by=None,
                reason=options["reason"],
            )
        except EmployeeLifecycleError as exc:
            raise CommandError(str(exc)) from exc
        mapping = cast(LegacyStaffMapping, mapping)
        self.stdout.write(
            self.style.SUCCESS(
                f"已恢复映射：Staff#{legacy_pk} → StaffProfile#{mapping.staff_profile.pk}"
                f"（台账 #{mapping.pk}）"
            )
        )
