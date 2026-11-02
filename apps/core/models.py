from django.conf import settings
from django.db import models


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
