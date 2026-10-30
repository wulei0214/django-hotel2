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


class Command(BaseCommand):
    help = "将旧员工档案绑定到 Django User，并按物业任职关系分配岗位模板。"

    def add_arguments(self, parser):
        parser.add_argument("--employee-no", required=True)
        parser.add_argument("--username", required=True)
        parser.add_argument("--property-code", default="DEFAULT")
        parser.add_argument("--role", required=True, choices=ROLE_CHOICES)

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
            group = Group.objects.get(name=options["role"])
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

        self.stdout.write(
            self.style.SUCCESS(
                f"已绑定员工 {membership.employee_no} -> 账号 {user.username}，岗位 {group.name}。"
            )
        )
        self.stdout.write(
            "注意：未修改 User.groups、is_staff 或 is_superuser；PMS 岗位权限按物业上下文解析。"
        )
