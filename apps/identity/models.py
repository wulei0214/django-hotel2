from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class StaffProfile(models.Model):
    """员工自然人档案，不承担密码、会话或登录权限职责。"""

    class EmploymentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "在职"
        INACTIVE = "INACTIVE", "停用"
        TERMINATED = "TERMINATED", "离职"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staff_profile",
        verbose_name="登录账号",
    )
    legacy_staff = models.OneToOneField(
        "hotelapp.Staff",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pms_staff_profile",
        verbose_name="旧版员工记录",
    )
    full_name = models.CharField(max_length=80, verbose_name="员工姓名")
    phone = models.CharField(max_length=20, blank=True, verbose_name="手机号")
    employment_status = models.CharField(
        max_length=16,
        choices=EmploymentStatus.choices,
        default=EmploymentStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("id",)
        verbose_name = "员工档案"
        verbose_name_plural = "员工档案"

    def __str__(self):
        return self.full_name


class PropertyStaffMembership(models.Model):
    """员工在某一家酒店的任职关系；店内工号和岗位属于任职关系。"""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "在职"
        SUSPENDED = "SUSPENDED", "暂停"
        TERMINATED = "TERMINATED", "已离职"

    staff_profile = models.ForeignKey(
        StaffProfile,
        on_delete=models.PROTECT,
        related_name="property_memberships",
    )
    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="staff_memberships",
    )
    employee_no = models.CharField(max_length=32, verbose_name="店内工号")
    position_code = models.CharField(max_length=32, default="UNASSIGNED")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    started_at = models.DateField(default=timezone.localdate)
    ended_at = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("property_id", "employee_no")
        constraints = [
            models.UniqueConstraint(
                fields=("property", "employee_no"),
                name="uq_membership_property_employee_no",
            ),
            models.UniqueConstraint(
                fields=("property", "staff_profile"),
                name="uq_membership_property_staff",
            ),
            models.CheckConstraint(
                check=Q(ended_at__isnull=True) | Q(ended_at__gte=F("started_at")),
                name="ck_membership_valid_dates",
            ),
        ]
        verbose_name = "员工酒店任职关系"
        verbose_name_plural = "员工酒店任职关系"

    def __str__(self):
        return f"{self.property.code} / {self.employee_no} / {self.staff_profile.full_name}"


class StaffRoleAssignment(models.Model):
    """岗位权限模板分配到员工的某次物业任职，不能写入 User.groups。"""

    membership = models.ForeignKey(
        PropertyStaffMembership,
        on_delete=models.PROTECT,
        related_name="role_assignments",
    )
    group = models.ForeignKey(
        Group,
        on_delete=models.PROTECT,
        related_name="pms_role_assignments",
    )
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="granted_pms_roles",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "岗位角色分配"
        verbose_name_plural = verbose_name
        ordering = ("membership_id", "group_id", "starts_at")
        constraints = [
            models.UniqueConstraint(
                fields=("membership", "group", "starts_at"),
                name="uq_staff_role_assignment_start",
            ),
            models.CheckConstraint(
                check=Q(ends_at__isnull=True) | Q(ends_at__gt=F("starts_at")),
                name="ck_staff_role_assignment_valid_period",
            ),
        ]
        verbose_name = "员工岗位授权"
        verbose_name_plural = "员工岗位授权"

    def __str__(self):
        return f"{self.membership} -> {self.group.name}"
