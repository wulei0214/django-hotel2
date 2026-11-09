from django.conf import settings
# 业务阅读提示：User 仅表示认证账号；StaffProfile 表示员工档案；PropertyStaffMembership 表示某物业任职；StaffRoleAssignment 表示任职期间的岗位授权。不要把手机号当作认证凭据，也不要把 User.groups 当作员工任职事实。新增字段/约束前先核对 docs 中 D11/D12 决策与数据迁移影响。
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
        help_text="一对一关联 Django 用户；空表示尚未开通后台账号",
    )
    legacy_staff = models.OneToOneField(
        "hotelapp.Staff",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pms_staff_profile",
        verbose_name="旧版员工记录",
        help_text="与 hotelapp 员工表的迁移映射；未迁移的员工为空",
    )
    full_name = models.CharField(max_length=80, verbose_name="员工姓名")
    phone = models.CharField(max_length=20, blank=True, verbose_name="手机号")
    employment_status = models.CharField(
        max_length=16,
        choices=EmploymentStatus.choices,
        default=EmploymentStatus.ACTIVE,
        verbose_name="雇佣状态",
    )
    must_change_password = models.BooleanField(
        default=False, verbose_name="须强制改密",
        help_text="开通账号/重置密码后置位；首次登录强制修改（D-07）")
    last_password_change = models.DateTimeField(
        null=True, blank=True, verbose_name="最近密码变更时间")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

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
        verbose_name="员工档案",
    )
    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="staff_memberships",
        verbose_name="任职酒店",
    )
    employee_no = models.CharField(
        max_length=32, verbose_name="店内工号",
        help_text="同酒店内唯一；前台展示用",
    )
    position_code = models.CharField(
        max_length=32, default="UNASSIGNED", verbose_name="岗位代码",
        help_text="如 GM（总经理）/ FO（前台）；UNASSIGNED 为未定岗",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE,
        verbose_name="任职状态",
    )
    started_at = models.DateField(default=timezone.localdate, verbose_name="入职日期")
    ended_at = models.DateField(
        null=True, blank=True, verbose_name="离职日期",
        help_text="不得早于入职日期",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

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
        verbose_name="任职关系",
    )
    group = models.ForeignKey(
        Group,
        on_delete=models.PROTECT,
        related_name="pms_role_assignments",
        verbose_name="角色组",
        help_text="权限模板（前台/值班经理/财务/店长），生效中按 starts_at~ends_at 判定",
    )
    starts_at = models.DateTimeField(default=timezone.now, verbose_name="授权开始时间")
    ends_at = models.DateTimeField(
        null=True, blank=True, verbose_name="授权结束时间",
        help_text="空表示长期有效；必须晚于开始时间",
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="granted_pms_roles",
        verbose_name="授权人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "员工岗位授权"
        verbose_name_plural = "员工岗位授权"
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

    def __str__(self):
        return f"{self.membership} -> {self.group.name}"


class Invitation(models.Model):
    """员工邀请激活（AUTH-14，方案 §5.1-§5.3）。

    token 绑定员工档案与任职关系，不可改绑（激活时复核一致）；
    数据库只存 token 的 SHA-256 摘要，明文仅在创建/重发时返回一次
    （§5.3），绝不落库、不落审计。状态机（§5.2）：
      PENDING --员工激活--> ACCEPTED（一次性消费，consumed_at 落定）
      PENDING --超过 TTL--> EXPIRED；PENDING --管理员撤销--> REVOKED
      重新邀请 = 关闭旧邀请 + 新建 PENDING（resend_count 在链上递增）
    待激活期间账号以 unusable password 存在（§5.4），激活前不可登录。
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "待激活"
        ACCEPTED = "ACCEPTED", "已接受"
        EXPIRED = "EXPIRED", "已过期"
        REVOKED = "REVOKED", "已撤销"

    staff_profile = models.ForeignKey(
        StaffProfile,
        on_delete=models.PROTECT,
        related_name="invitations",
        verbose_name="员工档案",
    )
    membership = models.ForeignKey(
        PropertyStaffMembership,
        on_delete=models.PROTECT,
        related_name="invitations",
        verbose_name="任职关系",
        help_text="token 绑定的任职关系；必须属于同一员工档案，激活时复核",
    )
    token_hash = models.CharField(
        max_length=64, unique=True, verbose_name="token 摘要",
        help_text="SHA-256(token) 十六进制；明文 token 不落库不落审计",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING,
        verbose_name="邀请状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    expires_at = models.DateTimeField(verbose_name="失效时间")
    consumed_at = models.DateTimeField(
        null=True, blank=True, verbose_name="消费时间",
        help_text="员工激活成功时间；仅 ACCEPTED 状态允许非空",
    )
    revoked_at = models.DateTimeField(
        null=True, blank=True, verbose_name="撤销时间",
        help_text="管理员撤销时间；仅 REVOKED 状态允许非空",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="created_invitations",
        verbose_name="发起人",
    )
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_invitations",
        verbose_name="撤销人",
    )
    resend_count = models.PositiveIntegerField(
        default=0, verbose_name="重发次数",
        help_text="同一员工邀请链上的重发序号；首次创建为 0，重发 +1",
    )
    last_sent_at = models.DateTimeField(
        verbose_name="最近发出时间",
        help_text="本次 token 的生成时间；人工复制发送同样记录",
    )

    class Meta:
        ordering = ("-id",)
        verbose_name = "员工邀请激活"
        verbose_name_plural = "员工邀请激活"
        constraints = [
            # 业务阅读提示：Meta 内不能引用外层类的 Status 命名空间，此处用字面量，
            # 与 Invitation.Status.ACCEPTED/REVOKED 取值严格一致。
            models.CheckConstraint(
                check=Q(consumed_at__isnull=True) | Q(status="ACCEPTED"),
                name="ck_invitation_consumed_only_accepted",
            ),
            models.CheckConstraint(
                check=~Q(status="ACCEPTED") | Q(consumed_at__isnull=False),
                name="ck_invitation_accepted_has_consumed_at",
            ),
            models.CheckConstraint(
                check=Q(revoked_at__isnull=True) | Q(status="REVOKED"),
                name="ck_invitation_revoked_only_revoked",
            ),
            models.CheckConstraint(
                check=Q(expires_at__gt=F("created_at")),
                name="ck_invitation_expires_after_created",
            ),
        ]
        indexes = [
            models.Index(fields=("staff_profile", "status"),
                         name="idx_invitation_profile_status"),
            models.Index(fields=("status", "expires_at"),
                         name="idx_invitation_status_expiry"),
        ]

    def __str__(self):
        # 业务阅读提示：字符串展示不含 token 摘要（日志/管理界面对齐 §16.2 最小化）。
        return (f"邀请#{self.pk} 员工档案#{self.staff_profile_id} "
                f"任职#{self.membership_id} {self.get_status_display()}")
