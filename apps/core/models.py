from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Property(models.Model):
    """酒店物业。单店部署只初始化一条记录，多物业能力通过 property_id 预留。"""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "营业中"
        INACTIVE = "INACTIVE", "停用"

    code = models.CharField(
        max_length=32, unique=True, verbose_name="物业编码",
        help_text="酒店唯一编码，如 DEMO；创建后不要修改（预订号/账务引用它）",
    )
    name = models.CharField(max_length=120, verbose_name="酒店名称")
    timezone = models.CharField(
        max_length=64, default="Asia/Shanghai", verbose_name="营业时区",
        help_text="IANA 时区名，决定营业日（夜审）切换时间",
    )
    currency = models.CharField(
        max_length=3, default="CNY", verbose_name="结算币种",
        help_text="ISO 4217 三位币种代码",
    )
    address = models.TextField(blank=True, verbose_name="酒店地址")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE,
        verbose_name="营业状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("code",)
        verbose_name = "酒店物业"
        verbose_name_plural = "酒店物业"

    def __str__(self):
        return f"{self.code} - {self.name}"


class AuditEvent(models.Model):
    """不可变审计事件（V1.1 M-07 P0）。

    关键写操作（员工/授权/PII 揭示/导出/退款/冲账/夜审/权限变更）必须落事件，
    支持按 entity_type+entity_id+created_at 查询时间线。
    只追加：模型层禁用 update/delete；数据库层建议对应用账号
    REVOKE UPDATE/DELETE（DBA 步骤，见 readme 安全护栏）。
    """

    property = models.ForeignKey(
        Property, null=True, blank=True, on_delete=models.PROTECT,
        related_name="audit_events", verbose_name="物业")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="audit_events",
        verbose_name="操作人", help_text="系统操作为空")
    action = models.CharField(max_length=64, verbose_name="动作代码",
                              help_text="如 guest.pii_reveal / identity.role_assign / billing.refund_approve")
    entity_type = models.CharField(max_length=64, verbose_name="实体类型")
    entity_id = models.CharField(max_length=64, verbose_name="实体ID")
    before_json = models.JSONField(null=True, blank=True, verbose_name="变更前",
                                   help_text="敏感字段写入前脱敏")
    after_json = models.JSONField(null=True, blank=True, verbose_name="变更后",
                                  help_text="敏感字段写入前脱敏")
    reason = models.CharField(max_length=240, blank=True, verbose_name="原因")
    request_id = models.CharField(max_length=64, blank=True, verbose_name="请求链路ID")
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="来源IP")
    user_agent = models.CharField(max_length=240, blank=True, verbose_name="User-Agent")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="时间")

    class Meta:
        verbose_name = "审计事件"
        verbose_name_plural = "审计事件"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("entity_type", "entity_id", "-created_at"),
                         name="idx_audit_entity_time"),
            models.Index(fields=("action", "-created_at"), name="idx_audit_action_time"),
        ]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.entity_type}#{self.entity_id}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("审计事件不可修改（只追加）")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("审计事件不可删除")

    @classmethod
    def record(cls, *, action, entity_type, entity_id, actor=None,
               property_obj=None, reason="", before=None, after=None,
               request=None):
        """便捷落事件入口：自动取 request_id/ip/user_agent；敏感值由调用方脱敏。"""
        request_id = ""
        ip = None
        user_agent = ""
        if request is not None:
            request_id = getattr(request, "request_id", "") or request.META.get(
                "HTTP_X_REQUEST_ID", "")[:64]
            ip = request.META.get("REMOTE_ADDR") or None
            user_agent = (request.META.get("HTTP_USER_AGENT") or "")[:240]
        return cls.objects.create(
            property=property_obj, actor=actor, action=action,
            entity_type=entity_type, entity_id=str(entity_id),
            before_json=before, after_json=after, reason=reason[:240],
            request_id=request_id, ip=ip, user_agent=user_agent)


class LegacyRecordLink(models.Model):
    """历史数据 → PMS2 的映射核验记录（V1.1 §4.2，P0 设计锁定）。

    迁移期逐条核验：不自动合并；同一来源记录只能链接一个目标。
    """

    class MappingStatus(models.TextChoices):
        UNREVIEWED = "UNREVIEWED", "待核验"
        MATCH_CANDIDATE = "MATCH_CANDIDATE", "候选匹配"
        LINKED = "LINKED", "已链接"
        CONFLICT = "CONFLICT", "冲突待人工"
        IGNORED = "IGNORED", "忽略"

    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="legacy_record_links",
        verbose_name="物业")
    source_system = models.CharField(
        max_length=32, default="HOTELAPP_V1", verbose_name="来源系统")
    source_model = models.CharField(
        max_length=64, verbose_name="来源模型",
        help_text="Client / Vip / Room / Accommodation / Staff")
    source_pk = models.CharField(max_length=64, verbose_name="来源主键")
    target_model = models.CharField(max_length=64, verbose_name="目标模型")
    target_pk = models.CharField(max_length=64, verbose_name="目标主键")
    mapping_status = models.CharField(
        max_length=20, choices=MappingStatus.choices,
        default=MappingStatus.UNREVIEWED, verbose_name="映射状态")
    match_method = models.CharField(
        max_length=32, blank=True, verbose_name="匹配方式",
        help_text="人工确认 / 业务键验证 / 规则候选")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="legacy_links_reviewed",
        verbose_name="核验人")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="核验时间")
    review_note = models.CharField(max_length=240, blank=True, verbose_name="核验备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "历史数据映射"
        verbose_name_plural = "历史数据映射"
        constraints = [
            models.UniqueConstraint(
                fields=("property", "source_system", "source_model", "source_pk"),
                name="uq_legacy_link_source",
            ),
        ]
        indexes = [
            models.Index(fields=("source_model", "mapping_status"),
                         name="idx_legacy_src_status"),
        ]

    def __str__(self):
        return f"{self.source_model}#{self.source_pk} → {self.target_model}#{self.target_pk} ({self.mapping_status})"


class BusinessDate(models.Model):
    """营业日（V1.1 M-08 P0）：每物业同一时间只能有一个 OPEN 营业日。

    关账后禁止新增/修改该营业日的账务凭证；重开需独立权限、原因、审计。
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "营业中"
        CLOSED = "CLOSED", "已关账"
        REOPENED = "REOPENED", "已重开"

    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="business_dates",
        verbose_name="物业")
    business_date = models.DateField(verbose_name="营业日期")
    open_property_key = models.CharField(
        max_length=32, null=True, blank=True, unique=True, editable=False,
        verbose_name="开放营业日唯一键")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN,
        verbose_name="状态")
    opened_at = models.DateTimeField(auto_now_add=True, verbose_name="开启时间")
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="关账时间")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="business_dates_closed",
        verbose_name="关账人")
    reopened_at = models.DateTimeField(null=True, blank=True, verbose_name="重开时间")
    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="business_dates_reopened",
        verbose_name="重开人", related_query_name="reopened")
    reopen_reason = models.CharField(max_length=240, blank=True, verbose_name="重开原因")

    class Meta:
        verbose_name = "营业日"
        verbose_name_plural = "营业日"
        constraints = [
            models.UniqueConstraint(fields=("property", "business_date"),
                                    name="uq_business_date_per_property"),
        ]
        permissions = (
            ("breakglass_review", "可以复核紧急营业日重开"),
        )

    def save(self, *args, **kwargs):
        desired_key = (
            str(self.property_id)
            if self.property_id and self.status in (self.Status.OPEN, self.Status.REOPENED)
            else None
        )
        if self.open_property_key != desired_key:
            self.open_property_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"open_property_key"}
        if self.status in (self.Status.OPEN, self.Status.REOPENED) and self.property_id:
            dup = BusinessDate.objects.filter(
                property_id=self.property_id,
                status__in=(self.Status.OPEN, self.Status.REOPENED),
            ).exclude(pk=self.pk).exists()
            if dup:
                raise ValidationError("该物业已有 OPEN 营业日（M-08 规则 1）")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.property_id}@{self.business_date} {self.status}"


class BreakGlassRecord(models.Model):
    """营业日重开紧急例外授权：限时、限对象、二次认证并须独立事后复核。"""

    class Status(models.TextChoices):
        GRANTED = "GRANTED", "已授权待执行"
        USED = "USED", "已执行待复核"
        EXPIRED = "EXPIRED", "已过期"
        REVIEWED = "REVIEWED", "已完成复核"

    class ReviewDecision(models.TextChoices):
        CONFIRMED = "CONFIRMED", "复核确认"
        REJECTED = "REJECTED", "复核有异议"

    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="break_glass_records",
        verbose_name="物业")
    business_date = models.ForeignKey(
        BusinessDate, on_delete=models.PROTECT, related_name="break_glass_records",
        verbose_name="目标营业日")
    action_code = models.CharField(
        max_length=64, default="night_audit.reopen", editable=False,
        verbose_name="授权动作")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="break_glass_requested", verbose_name="紧急操作人")
    reason = models.CharField(max_length=240, verbose_name="紧急原因")
    password_verified_at = models.DateTimeField(verbose_name="二次密码验证时间")
    expires_at = models.DateTimeField(verbose_name="授权失效时间")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.GRANTED,
        verbose_name="状态")
    used_at = models.DateTimeField(null=True, blank=True, verbose_name="执行时间")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="break_glass_reviewed", verbose_name="事后复核人")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="复核时间")
    review_decision = models.CharField(
        max_length=12, choices=ReviewDecision.choices, blank=True,
        verbose_name="复核结论")
    review_reason = models.CharField(max_length=240, blank=True, verbose_name="复核说明")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="申请时间")

    class Meta:
        verbose_name = "营业日紧急重开授权"
        verbose_name_plural = "营业日紧急重开授权"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("property", "status", "expires_at"),
                         name="idx_breakglass_prop_status"),
            models.Index(fields=("business_date", "created_at"),
                         name="idx_breakglass_date_created"),
        ]

    def delete(self, *args, **kwargs):
        raise RuntimeError("紧急授权记录不可删除。")

    def __str__(self):
        return f"{self.property_id}/{self.business_date_id} {self.status}"


class NightAuditRun(models.Model):
    """夜审运行记录（V1.1 M-08 P0）：按营业日幂等；失败可恢复可追溯。"""

    class RunStatus(models.TextChoices):
        RUNNING = "RUNNING", "执行中"
        SUCCESS = "SUCCESS", "成功"
        FAILED = "FAILED", "失败"

    business_date = models.ForeignKey(
        BusinessDate, on_delete=models.PROTECT, related_name="night_audit_runs",
        verbose_name="营业日")
    status = models.CharField(
        max_length=10, choices=RunStatus.choices, default=RunStatus.RUNNING,
        verbose_name="运行状态")
    started_at = models.DateTimeField(auto_now_add=True, verbose_name="开始时间")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="完成时间")
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="night_audit_runs",
        verbose_name="执行人")
    summary = models.JSONField(null=True, blank=True, verbose_name="结果摘要",
                               help_text="在住、过租、No-Show、异常清单")
    error_message = models.TextField(blank=True, verbose_name="失败原因")

    class Meta:
        verbose_name = "夜审运行"
        verbose_name_plural = "夜审运行"
        ordering = ("-started_at",)

    def __str__(self):
        return f"{self.business_date_id} {self.status}"


class IdempotencyRecord(models.Model):
    """通用幂等记录（V1.1 M-14 P1）：统一幂等，不只预订。

    规则：同 key 同 body → 返回首次响应；同 key 不同 body → 409 冲突；
    并发同 key → 一个处理，其他等待或冲突；TTL 到期清理。
    """

    class RecordStatus(models.TextChoices):
        PROCESSING = "PROCESSING", "处理中"
        COMPLETED = "COMPLETED", "已完成"
        FAILED = "FAILED", "失败"

    property = models.ForeignKey(
        Property, null=True, blank=True, on_delete=models.PROTECT,
        related_name="idempotency_records", verbose_name="物业")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="idempotency_records",
        verbose_name="用户")
    key = models.CharField(max_length=128, verbose_name="幂等键")
    method = models.CharField(max_length=8, verbose_name="HTTP 方法")
    path = models.CharField(max_length=256, verbose_name="请求路径")
    request_hash = models.CharField(max_length=64, verbose_name="请求体哈希")
    response_status = models.PositiveIntegerField(null=True, blank=True, verbose_name="响应状态码")
    response_body = models.JSONField(null=True, blank=True, verbose_name="首次响应体")
    status = models.CharField(
        max_length=12, choices=RecordStatus.choices, default=RecordStatus.PROCESSING,
        verbose_name="记录状态")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="过期时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "幂等记录"
        verbose_name_plural = "幂等记录"
        constraints = [
            models.UniqueConstraint(fields=("property", "user", "key"),
                                    name="uq_idempotency_prop_user_key"),
        ]

    def __str__(self):
        return f"{self.key} {self.status}"


class StatusTransition(models.Model):
    """业务状态变更时间线（V1.1 M-15 P1/可选）：面向业务时间线（AuditEvent 面向合规）。"""

    entity_type = models.CharField(max_length=64, verbose_name="实体类型")
    entity_id = models.CharField(max_length=64, verbose_name="实体ID")
    from_status = models.CharField(max_length=32, blank=True, verbose_name="原状态")
    to_status = models.CharField(max_length=32, verbose_name="新状态")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="status_transitions",
        verbose_name="操作人")
    reason = models.CharField(max_length=240, blank=True, verbose_name="原因")
    request_id = models.CharField(max_length=64, blank=True, verbose_name="请求链路ID")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="时间")

    class Meta:
        verbose_name = "状态变更时间线"
        verbose_name_plural = "状态变更时间线"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("entity_type", "entity_id", "-created_at"),
                         name="idx_st_entity_time"),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("状态变更记录不可修改（只追加）")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("状态变更记录不可删除")

    def __str__(self):
        return f"{self.entity_type}#{self.entity_id} {self.from_status}→{self.to_status}"


class RateLimitCounter(models.Model):
    """AUTH-02（P0-01~04）：独立限流计数器——AuditEvent 不得充当计数器。

    设计要点（方案 2.0 §6.2）：
    - key_hash：维度指纹（IP / HMAC(phone) / HMAC(username)+IP / booking_no / token 指纹）；
      空手机号等缺失维度不形成共享全局桶（P0-02）。
    - 原子计数：hit() 用 F() 表达式单条 UPDATE 自增（P0-04 并发安全），
      首次写入用 get_or_create + 事务重试。
    - TTL：expires_at 到期即可被清理任务/惰性清理删除（P0-03）。
    """

    key_hash = models.CharField(max_length=64, verbose_name="限流键指纹")
    action = models.CharField(max_length=64, verbose_name="动作代码")
    window_start = models.DateTimeField(verbose_name="窗口起点")
    count = models.PositiveIntegerField(default=0, verbose_name="计数")
    expires_at = models.DateTimeField(verbose_name="过期时间", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "限流计数器"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=("key_hash", "action", "window_start"),
                name="uniq_ratelimit_key_action_window"),
        ]
        indexes = [
            models.Index(fields=("key_hash", "action", "expires_at"),
                         name="idx_ratelimit_lookup"),
        ]

    def __str__(self):
        return f"{self.action}/{self.key_hash[:12]} x{self.count}"


class Notification(models.Model):
    """站内通知与运营待办提示（基线 2.0 §4.6）。

    定位（§4.6 目标职责）：通知是提示，不是业务状态权威——本表是业务表
    而非审计表：生成/已读不落 AuditEvent，也不允许反向推导业务状态。
    首期口径：站内通知即可（短信/邮件/WhatsApp 等第三方渠道暂缓）。

    投递对象二选一（数据库约束兜底）：
      * recipient 非空：定向发给某员工档案；
      * role_group 非空（recipient 为空）：岗位广播——由读取方按"当前有效任职"
        动态匹配（§4.6 规则：调岗/离职后旧广播待办自动不可见，无需迁移数据）。

    运营待办去重：dedupe_key 非空的行由
    apps.core.services_notifications.refresh_operational_todos 按源业务状态
    实时派生并 upsert（(property, dedupe_key) 唯一，重复事件不重复生成）；
    源业务解决后自动置 is_read（"自动消失"）。dedupe_key 为空 = 人工/公告类
    通知，不参与自动销项。
    """

    class Category(models.TextChoices):
        RESERVATION = "reservation", "预订"
        HOUSEKEEPING = "housekeeping", "客房清洁"
        APPROVAL = "approval", "审批"
        AUDIT = "audit", "审计合规"
        SYSTEM = "system", "系统"

    class Severity(models.TextChoices):
        INFO = "info", "提示"
        WARNING = "warning", "警示"
        CRITICAL = "critical", "紧急"

    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="notifications",
        verbose_name="物业")
    recipient = models.ForeignKey(
        "identity.StaffProfile", null=True, blank=True,
        on_delete=models.PROTECT, related_name="notifications",
        verbose_name="接收员工",
        help_text="定向投递的员工档案；空表示岗位广播（此时 role_group 必填）")
    role_group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.PROTECT,
        related_name="notifications", verbose_name="广播岗位组",
        help_text="岗位广播目标组；读取方按当前有效任职动态匹配，不写入 User.groups")
    category = models.CharField(
        max_length=16, choices=Category.choices, verbose_name="类别",
        help_text="reservation/housekeeping/approval/audit/system")
    title = models.CharField(max_length=120, verbose_name="标题")
    body = models.CharField(max_length=240, blank=True, verbose_name="内容")
    link = models.CharField(
        max_length=200, blank=True, verbose_name="跳转链接",
        help_text="站内相对路径，如 /manage/reservations/")
    severity = models.CharField(
        max_length=8, choices=Severity.choices, default=Severity.INFO,
        verbose_name="级别")
    is_read = models.BooleanField(default=False, verbose_name="已读")
    read_at = models.DateTimeField(null=True, blank=True, verbose_name="已读时间")
    dedupe_key = models.CharField(
        max_length=128, null=True, blank=True, verbose_name="待办去重键",
        help_text="运营待办业务键（来源代码|业务键|目标组）；空=人工/公告类，不参与自动销项")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="失效时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "站内通知"
        verbose_name_plural = "站内通知"
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("property", "dedupe_key"),
                name="uq_notification_prop_dedupe"),
            models.CheckConstraint(
                check=Q(recipient__isnull=False) | Q(role_group__isnull=False),
                name="ck_notification_has_audience"),
        ]
        indexes = [
            models.Index(fields=("recipient", "is_read", "-created_at"),
                         name="idx_notif_rcpt_read_time"),
            models.Index(fields=("role_group", "is_read"),
                         name="idx_notif_group_read"),
        ]

    def __str__(self):
        audience = self.recipient_id if self.recipient_id else (
            f"组:{self.role_group.name}" if self.role_group else "无接收人")
        return f"[{self.get_category_display()}] {self.title} → {audience}"
