from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import Property
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)


ROLE_CHOICES = (
    "FRONT_DESK",
    "HOUSEKEEPING",
    "NIGHT_AUDIT",
    "FINANCE",
    "DUTY_MANAGER",
    "PROPERTY_OWNER",
)

# RBAC 统一角色目录：英文稳定编码 → 中文组名（迁移 0014 已清理英文组，只保留中文目录）。
# 页面（权限矩阵）与命令（本命令）都写同一个中文组，双轨目录问题不再存在。
ROLE_CODE_TO_GROUP = {
    "FRONT_DESK": "前台",
    "HOUSEKEEPING": "客房服务员",
    "NIGHT_AUDIT": "夜审员",
    "FINANCE": "财务",
    "DUTY_MANAGER": "值班经理",
    "PROPERTY_OWNER": "店长",
}


class Command(BaseCommand):
    help = "将旧员工档案绑定到 Django User，并按物业任职关系分配岗位模板。"

    def add_arguments(self, parser):
        parser.add_argument("--employee-no", required=True)
        parser.add_argument("--username", required=True)
        parser.add_argument("--property-code", default="DEFAULT")
        parser.add_argument("--role", required=True, choices=ROLE_CHOICES)
        parser.add_argument(
            "--password", default="",
            help="可选：为该账号设置/重置登录密码（不传则要求账号已具备密码）")

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.select_for_update().get(username=options["username"])
            property_obj = Property.objects.get(code=options["property_code"])
            membership = (
                PropertyStaffMembership.objects.select_for_update()
                .select_related("staff_profile")
                .get(
                    property=property_obj,
                    employee_no=options["employee_no"],
                    status=PropertyStaffMembership.Status.ACTIVE,
                )
            )
            group = Group.objects.get(name=ROLE_CODE_TO_GROUP[options["role"]])
        except (User.DoesNotExist, Property.DoesNotExist, PropertyStaffMembership.DoesNotExist, Group.DoesNotExist) as exc:
            raise CommandError(f"无法完成绑定：{exc}") from exc

        if not user.is_active:
            raise CommandError("不能把在职员工绑定到已停用账号。")

        profile = membership.staff_profile
        if profile.user_id and profile.user_id != user.pk:
            raise CommandError("该员工档案已经绑定其他登录账号。")
        if StaffProfile.objects.filter(user=user).exclude(pk=profile.pk).exists():
            raise CommandError("该登录账号已经绑定其他员工档案。")

        profile.user = user
        profile.save(update_fields=("user", "updated_at"))

        now = timezone.now()
        active_assignment = StaffRoleAssignment.objects.filter(
            membership=membership,
            group=group,
            starts_at__lte=now,
        ).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now)).first()
        if active_assignment is None:
            StaffRoleAssignment.objects.create(
                membership=membership,
                group=group,
                granted_by=None,
            )

        # P0-1 授权载体统一（RBAC V2）：StaffRoleAssignment 是授权记录
        # （物业任职+有效期+授权人），User.groups 同步镜像同一中文组作为
        # 旧后台运行时权限载体——两条路径读写同一组，不再有两套目录。
        # D-04：is_staff 仅用于 Django Admin，不再随员工绑定发放；
        # PMS 权限由 StaffRoleAssignment（授权记录）+ User.groups（运行时镜像）承载
        if options.get("password"):
            user.set_password(options["password"])
        user.save()
        user.groups.add(group)

        self.stdout.write(
            self.style.SUCCESS(
                f"已绑定员工 {membership.employee_no} -> 账号 {user.username}，岗位 {group.name}。"
            )
        )
        self.stdout.write(
            "授权记录已写入 StaffRoleAssignment（含任职/有效期），"
            "并同步 User.groups 使旧后台权限立即生效（is_staff 仅 Django Admin，不再发放）。"
        )
