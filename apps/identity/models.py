from django.conf import settings

# 业务阅读提示：User 仅表示认证账号；StaffProfile 表示员工档案；PropertyStaffMembership 表示某物业任职；StaffRoleAssignment 表示任职期间的岗位授权。不要把手机号当作认证凭据，也不要把 User.groups 当作员工任职事实。新增字段/约束前先核对 docs 中 D11/D12 决策与数据迁移影响。
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
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
        default=False,
        verbose_name="须强制改密",
        help_text="开通账号/重置密码后置位；首次登录强制修改（D-07）",
    )
    last_password_change = models.DateTimeField(
        null=True, blank=True, verbose_name="最近密码变更时间"
    )

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
        max_length=32,
        verbose_name="店内工号",
        help_text="同酒店内唯一；前台展示用",
    )
    position_code = models.CharField(
        max_length=32,
        default="UNASSIGNED",
        verbose_name="岗位代码",
        help_text="如 GM（总经理）/ FO（前台）；UNASSIGNED 为未定岗",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        verbose_name="任职状态",
    )
    started_at = models.DateField(default=timezone.localdate, verbose_name="入职日期")
    ended_at = models.DateField(
        null=True,
        blank=True,
        verbose_name="离职日期",
        help_text="不得早于入职日期",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    # WB-EMP-01 / DEC-EMP-01：MySQL 8 不支持条件唯一约束（models.W036），
    # 因此改用「可空唯一键」在数据库层强制「同一物业同一员工同时点最多一条
    # ACTIVE 任职」。仅 ACTIVE 行写入 {property_id}:{staff_profile_id}，
    # SUSPENDED/TERMINATED 行为 NULL（MySQL 唯一索引允许多个 NULL）。
    # 与 apps/core/migrations/0006、billing/0006 的治理方案保持一致。
    active_membership_key = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        editable=False,
        verbose_name="有效任职唯一键",
    )

    class Meta:
        ordering = ("property_id", "employee_no")
        constraints = [
            models.UniqueConstraint(
                fields=("property", "employee_no"),
                name="uq_membership_property_employee_no",
            ),
            models.CheckConstraint(
                check=Q(ended_at__isnull=True) | Q(ended_at__gte=F("started_at")),
                name="ck_membership_valid_dates",
            ),
        ]
        verbose_name = "员工酒店任职关系"
        verbose_name_plural = "员工酒店任职关系"

    def save(self, *args, **kwargs):
        """在模型层统一维护可空唯一键，任何 ORM 写入路径都无法绕过。

        update_fields 由调用方指定时必须同步并入本字段，否则状态由
        ACTIVE 改为非 ACTIVE（或反向）时唯一键会残留在旧值上。
        """
        if self.status == self.Status.ACTIVE and self.property_id and self.staff_profile_id:
            self.active_membership_key = f"{self.property_id}:{self.staff_profile_id}"
        else:
            self.active_membership_key = None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"active_membership_key"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.property.code} / {self.employee_no} / {self.staff_profile.full_name}"


class EmployeeNumberCounter(models.Model):
    """WB-101/DEC-EMP-02：员工工号流水号计数器（每物业每部门一行）。

    generate_employee_no() 在事务内对本行 select_for_update 行锁后读取并
    递增 next_seq，与 Membership 创建同事务提交（失败即回滚，编号不消耗）；
    并发分配由行锁串行化，替代旧"按已有工号前缀 MAX+1"查询。
    离职工号不回收（DEC-EMP-01）：计数器只增不减。
    """

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="employee_number_counters",
        verbose_name="物业",
    )
    department_code = models.CharField(
        max_length=8,
        verbose_name="部门代码",
        help_text="受控部门代码（FO/HK/FIN/MG/ENG）；工号前缀即该代码",
    )
    next_seq = models.PositiveIntegerField(
        default=1,
        verbose_name="下一个流水号",
        help_text="行锁保护下递增；生成工号为该值，落库后 +1",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("property", "department_code"),
                name="uq_emp_no_counter_property_department",
            ),
        ]
        verbose_name = "员工工号计数器"
        verbose_name_plural = "员工工号计数器"

    def __str__(self):
        return f"{self.property.code} / {self.department_code} 下一个 {self.next_seq:04d}"


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
        null=True,
        blank=True,
        verbose_name="授权结束时间",
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
        max_length=64,
        unique=True,
        verbose_name="token 摘要",
        help_text="SHA-256(token) 十六进制；明文 token 不落库不落审计",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name="邀请状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    expires_at = models.DateTimeField(verbose_name="失效时间")
    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="消费时间",
        help_text="员工激活成功时间；仅 ACCEPTED 状态允许非空",
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="撤销时间",
        help_text="管理员撤销时间；仅 REVOKED 状态允许非空",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_invitations",
        verbose_name="发起人",
    )
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_invitations",
        verbose_name="撤销人",
    )
    resend_count = models.PositiveIntegerField(
        default=0,
        verbose_name="重发次数",
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
            models.Index(fields=("staff_profile", "status"), name="idx_invitation_profile_status"),
            models.Index(fields=("status", "expires_at"), name="idx_invitation_status_expiry"),
        ]

    def __str__(self):
        # 业务阅读提示：字符串展示不含 token 摘要（日志/管理界面对齐 §16.2 最小化）。
        return (
            f"邀请#{self.pk} 员工档案#{self.staff_profile_id} "
            f"任职#{self.membership_id} {self.get_status_display()}"
        )


class ImmutablePermissionApprovalQuerySet(models.QuerySet):
    """权限矩阵审批事实禁止绕过模型守卫的批量修改/删除/生命周期外创建。

    边界声明（与 billing ImmutableFinancialFactQuerySet 同口径）：
      * 拦截 QuerySet 级 update / delete / bulk_create / bulk_update；
      * 单实例 save() 生命周期校验由模型 save() 守卫承担（此处不重复）；
      * 数据库层兜底 = ck_permatrix_* CheckConstraint（SoD/决策时间）与
        uq_permatrix_pending_diff 部分唯一约束（并发幂等）；触发器类保护不在
        本层范围（MySQL 触发器属部署面，identity 域未部署）。
    """

    def update(self, **kwargs):
        raise ValidationError("审批事实禁止 QuerySet.update；请走 approve/reject 端点。")

    def delete(self):
        raise ValidationError("审批事实禁止批量删除；保留审批审计轨迹。")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("审批事实禁止 bulk_create；创建必须走单实例 save 守卫。")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("审批事实禁止 bulk_update；决策必须走 approve/reject 端点。")


class PermissionMatrixApproval(models.Model):
    """平台权限矩阵高危变更的第二人审批事实（治理 V1.1 第三阶段迭代-1 变更 A）。

    镜像 apps.billing.ApprovalRecord 的双人 SoD 保证（仅 PENDING 创建 / 申请事实
    不可变 / 自审批禁止 / 不可删除），差异点：权限矩阵为平台域动作、无物业上下文，
    故不挂 property 外键，也不进入 billing 的 ApprovalActionRegistry 白名单；
    payload 快照保存 added/removed/version，执行时在组行锁事务内复核。
    """

    objects = models.Manager.from_queryset(ImmutablePermissionApprovalQuerySet)()

    class Status(models.TextChoices):
        PENDING = "PENDING", "待审批"
        APPROVED = "APPROVED", "已批准"
        REJECTED = "REJECTED", "已驳回"
        CANCELLED = "CANCELLED", "已撤销"

    group = models.ForeignKey(
        "auth.Group",
        on_delete=models.PROTECT,
        related_name="permission_matrix_approvals",
        verbose_name="目标角色组",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="permission_matrix_approvals_requested",
        verbose_name="申请人",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="permission_matrix_approvals_approved",
        verbose_name="审批人",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, verbose_name="审批状态"
    )
    reason = models.CharField(max_length=240, verbose_name="变更理由")
    diff_hash = models.CharField(
        max_length=16, verbose_name="diff 短哈希", help_text="added/removed 稳定哈希"
    )
    payload_snapshot = models.JSONField(
        verbose_name="变更快照", help_text="added/removed/preserved/requested/version"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="申请时间")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="审批时间")

    class Meta:
        verbose_name = "权限矩阵审批记录"
        verbose_name_plural = "权限矩阵审批记录"
        ordering = ("-created_at",)
        constraints = [
            # SoD 数据库级强制：批准/驳回必须有审批人且不得为申请人本人（镜像
            # billing ck_refund_sod_* 家族约束，第二人原则不依赖应用层纪律）
            models.CheckConstraint(
                check=~Q(status__in=["APPROVED", "REJECTED"])
                | (Q(approved_by__isnull=False) & ~Q(approved_by=F("requested_by"))),
                name="ck_permatrix_sod_approver",
            ),
            models.CheckConstraint(
                check=~Q(status__in=["APPROVED", "REJECTED"]) | Q(approved_at__isnull=False),
                name="ck_permatrix_decided_has_time",
            ),
            # A-03 并发幂等：同组同 diff 至多一条 PENDING（部分唯一约束，
            # SQLite/MySQL 8.0.16+ 均支持）；取代语义（先 CANCELLED 旧记录再
            # create）在应用层单事务内保证顺序。
            models.UniqueConstraint(
                fields=("group", "diff_hash"),
                condition=Q(status="PENDING"),
                name="uq_permatrix_pending_diff",
            ),
        ]
        indexes = [
            models.Index(fields=("group", "status"), name="idx_permatrix_group_status"),
            models.Index(fields=("diff_hash", "status"), name="idx_permatrix_diff_status"),
        ]

    def save(self, *args, **kwargs):
        """镜像 ApprovalRecord.save 的不可变性与 SoD 校验（identity 域自持）。"""
        if self._state.adding:
            if self.status != self.Status.PENDING or self.approved_by_id or self.approved_at:
                raise ValidationError("审批记录必须从待审批状态创建。")
            if not self.reason or not self.diff_hash:
                raise ValidationError("审批记录必须携带变更理由与 diff 哈希。")
        else:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(
                    "group_id",
                    "requested_by_id",
                    "reason",
                    "diff_hash",
                    "payload_snapshot",
                    "created_at",
                    "status",
                )
                .first()
            )
            if previous:
                immutable = (
                    "group_id",
                    "requested_by_id",
                    "reason",
                    "diff_hash",
                    "payload_snapshot",
                    "created_at",
                )
                if any(previous[field] != getattr(self, field) for field in immutable):
                    raise ValidationError("审批申请事实不可修改。")
                if previous["status"] != self.Status.PENDING:
                    raise ValidationError("已决策审批记录不可修改。")
                if self.status not in (
                    self.Status.APPROVED,
                    self.Status.REJECTED,
                    self.Status.CANCELLED,
                ):
                    raise ValidationError("审批记录只允许从待审批转为最终状态。")
                if self.status in (self.Status.APPROVED, self.Status.REJECTED):
                    if not self.approved_by_id or not self.approved_at:
                        raise ValidationError("审批通过/驳回必须记录审批人和时间。")
                    if self.approved_by_id == self.requested_by_id:
                        raise ValidationError("申请人不能审批自己的申请。")
                elif self.approved_by:
                    raise ValidationError("撤销审批不得伪造审批人。")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("权限矩阵审批记录不可删除")

    def __str__(self):
        return f"权限矩阵审批#{self.pk} Group#{self.group_id} " f"{self.diff_hash} {self.status}"
