from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.models import Property
from apps.identity.models import PropertyStaffMembership
from apps.identity.services import EmployeeLifecycleError, EmployeeLifecycleService

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
            "--password",
            default="",
            help="可选：为该账号设置/重置登录密码（不传则要求账号已具备密码）",
        )

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
        except (
            User.DoesNotExist,
            Property.DoesNotExist,
            PropertyStaffMembership.DoesNotExist,
            Group.DoesNotExist,
        ) as exc:
            raise CommandError(f"无法完成绑定：{exc}") from exc

        try:
            EmployeeLifecycleService.bind_existing_user(
                membership=membership,
                user=user,
                group=group,
                password=options.get("password") or "",
                granted_by=None,
            )
        except EmployeeLifecycleError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"已绑定员工 {membership.employee_no} -> 账号 {user.username}，岗位 {group.name}。"
            )
        )
        self.stdout.write(
            "授权记录已写入 StaffRoleAssignment（含任职/有效期），"
            "旧后台权限由 AuthorizationService 按 DEFAULT 任职和有效期判定；未写 is_staff 或 Django 全局 User.groups。"
        )
