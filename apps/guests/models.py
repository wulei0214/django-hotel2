from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Guest(models.Model):
    """住客档案：手机号是可共享联系方式，不作为自然人唯一身份键。

    PII 基线 11.3-E（WB-103）：手机号只存密文（phone_ciphertext，Fernet），
    检索/去重用 phone_hash（HMAC-SHA256(PII_KEY, normalized)）；
    完整号码只能经 reveal_phone（权限+审计）解密获取，不留明文旁路。
    """

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="guests",
        verbose_name="所属酒店",
    )
    full_name = models.CharField(max_length=80, verbose_name="姓名")
    phone = models.CharField(
        max_length=24,
        null=True,
        blank=True,
        verbose_name="联系电话（历史列）",
        help_text="已迁移（WB-103）：留列兼容历史 DDL，新数据不再写入明文；"
        "完整号码经 phone_ciphertext 加密存储",
    )
    phone_normalized = models.CharField(
        max_length=24,
        null=True,
        blank=True,
        verbose_name="标准化手机号（历史列）",
        help_text="已迁移（WB-103）：留列兼容历史 DDL，新数据不再写入明文；"
        "检索/去重使用 phone_hash",
    )
    phone_ciphertext = models.TextField(
        null=True,
        blank=True,
        verbose_name="手机号密文",
        help_text="Fernet(phone_ciphertext)=标准化号码；揭示需独立权限并写审计",
    )
    phone_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name="手机号查询哈希",
        help_text="HMAC-SHA256(PII_KEY, normalized)，仅用于检索与共享判定，"
        "不可逆、不代表自然人唯一身份",
    )
    email = models.EmailField(blank=True, verbose_name="电子邮箱")
    notes = models.TextField(blank=True, verbose_name="备注")
    consent_version = models.CharField(
        max_length=32,
        blank=True,
        verbose_name="隐私协议版本",
        help_text="客人同意隐私条款时的版本号，空表示未采集",
    )
    # A3 客人合并（排期 V2.0 第二主线，2026-11-17 批准交付）：
    # 合并是软归档——duplicate 保留行并指向 primary，不物理删除（§8.2-4
    # 要求保留主档案、源档案与完整关联映射）；账务事实（Folio/Payment）
    # 归属原预订不动，随预订 guest FK 一起迁移。
    merged_into: models.ForeignKey = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="merged_sources",
        verbose_name="合并入主档案",
        help_text="非空表示本档案已合并入该主档案（软归档，保留行不物理删除）",
    )
    merged_at: models.DateTimeField = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="合并时间",
        help_text="执行合并动作的时间；与 merged_into 成对写入",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    # ---------- 姓名 PII 加密（V1.0 任务书 TASK-03，阶段 1 双写） ----------
    # 渐进迁移：新写入同时落密文/HMAC/密钥版本；明文 full_name 在观察期内
    # 保留（受控回滚能力），读路径切换与明文列退役需负责人批准后另行迁移。
    # 姓名规范化口径：strip + 全角空格折叠（服务层 normalize_guest_name）。
    full_name_ciphertext: models.TextField = models.TextField(
        null=True,
        blank=True,
        verbose_name="姓名密文",
        help_text="Fernet(PII_KEY, 规范化姓名)；姓名完整值揭示权限待负责人逐项审批（NS-E-05），当前未开放",
    )
    full_name_hmac: models.CharField = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name="姓名盲索引",
        help_text="HMAC-SHA256(PII_KEY, 规范化姓名)，精确检索用；不可逆",
        db_index=True,
    )
    pii_key_version: models.CharField = models.CharField(
        max_length=16,
        null=True,
        blank=True,
        verbose_name="PII 密钥版本",
        help_text="姓名密文使用的密钥版本标识（v1=当前 DJANGO_PII_KEY；轮换后可区分）",
    )

    # FK attname / 反向管理器注解：django-stubs 不生成这些运行期属性，
    # 统一显式声明，避免调用方出现 attr-defined 误报。
    property_id: Any
    merged_into_id: Any
    reservations: Any  # Reservation.guest 的 related_name

    class Meta:
        verbose_name = "住客档案"
        verbose_name_plural = "住客档案"
        ordering = ("id",)
        indexes = [
            models.Index(fields=("property", "full_name"), name="idx_guest_prop_name"),
            models.Index(fields=("property", "phone_normalized"), name="idx_guest_prop_phone"),
            models.Index(fields=("property", "phone_hash"), name="idx_guest_prop_hash"),
            models.Index(fields=("merged_into",), name="idx_guest_merged_into"),
        ]

    def __str__(self):
        # WB-103：不再把明文手机号拼进字符串表示（可能进入日志/管理页）。
        return f"{self.full_name}#{self.pk}"


class GuestPhone(models.Model):
    """客人电话（V1.1 M-03 P0）：可多条、有历史；不以手机号作为自然人唯一主键。"""

    class PhoneType(models.TextChoices):
        MOBILE = "MOBILE", "手机"
        HOME = "HOME", "家庭电话"
        WORK = "WORK", "工作电话"
        OTHER = "OTHER", "其他"

    guest = models.ForeignKey(
        Guest, on_delete=models.CASCADE, related_name="phones", verbose_name="所属客人"
    )
    phone = models.CharField(
        max_length=24,
        null=True,
        blank=True,
        verbose_name="原始号码（历史列）",
        help_text="已迁移（WB-103）：留列兼容历史 DDL，新数据不再写入明文",
    )
    phone_normalized = models.CharField(
        max_length=24,
        null=True,
        blank=True,
        verbose_name="标准化号码（历史列）",
        help_text="已迁移（WB-103）：留列兼容历史 DDL，新数据不再写入明文；" "检索使用 phone_hash",
    )
    phone_ciphertext = models.TextField(
        null=True,
        blank=True,
        verbose_name="号码密文",
        help_text="Fernet(标准化号码)；完整值揭示需独立权限并写审计",
    )
    phone_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name="号码查询哈希",
        help_text="HMAC-SHA256(PII_KEY, normalized)，检索与共享号码判定专用",
    )
    phone_type = models.CharField(
        max_length=8, choices=PhoneType.choices, default=PhoneType.MOBILE, verbose_name="号码类型"
    )
    is_primary = models.BooleanField(default=False, verbose_name="主号码")
    primary_guest_key = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        unique=True,
        editable=False,
        verbose_name="主号码唯一键",
    )
    shared_review_required = models.BooleanField(
        default=False,
        verbose_name="共享号码待人工核验",
        help_text="同一标准化号码关联多个客人时置位（V1.1 M-03 规则 2）",
    )
    source = models.CharField(
        max_length=32,
        blank=True,
        verbose_name="来源",
        help_text="预订 / 前台登记 / 会员 / 历史迁移",
    )
    verified_at = models.DateTimeField(null=True, blank=True, verbose_name="验证时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    guest_id: Any

    class Meta:
        verbose_name = "客人电话"
        verbose_name_plural = "客人电话"
        indexes = [
            models.Index(fields=("phone_normalized",), name="idx_guestphone_norm"),
            models.Index(fields=("phone_hash",), name="idx_guestphone_hash"),
        ]
        constraints = [
            # WB-103：同一客人同一号码（hash）只登记一次——以 DB 唯一约束
            # 替代应用层明文查重；共享号码（跨客人）语义保持不变
            # （0005 迁移已明确电话不是自然人唯一身份键）。
            models.UniqueConstraint(
                fields=("guest", "phone_hash"),
                name="uq_guestphone_guest_hash",
            ),
        ]

    def save(self, *args, **kwargs):
        desired_key = str(self.guest_id) if self.guest_id and self.is_primary else None
        if self.primary_guest_key != desired_key:
            self.primary_guest_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"primary_guest_key"}
        if self.is_primary and self.guest_id:
            dup = (
                GuestPhone.objects.filter(
                    guest_id=self.guest_id,
                    is_primary=True,
                )
                .exclude(pk=self.pk)
                .exists()
            )
            if dup:
                raise ValidationError("该客人已有主号码（M-03：每客人一个主号码）")
        super().save(*args, **kwargs)

    def __str__(self):
        # WB-103：字符串表示不再携带明文号码，只展示哈希前缀。
        return (
            f"{self.guest_id}:"
            f"{(self.phone_hash or '')[:8]}({'主' if self.is_primary else '副'})"
        )


class GuestDocument(models.Model):
    """客人证件（V1.1 M-02 P0）：密文落库、hash 查重、揭示需独立权限并写审计。

    安全规则（§1.4.3）：
      * 证件号不得明文落库，不得进入普通 DTO/日志/导出；
      * 查重用 doc_no_hash（HMAC-SHA256 加盐），不查明文；
      * 揭示需 `guests.document.reveal` 权限，每次写 AuditEvent；
      * 旧 Client.身份证号 迁移时先加密再写入。
    """

    class DocType(models.TextChoices):
        ID_CARD = "ID_CARD", "身份证"
        PASSPORT = "PASSPORT", "护照"
        HK_MACAO = "HK_MACAO", "港澳通行证"
        TAIWAN = "TAIWAN", "台湾通行证"
        OTHER = "OTHER", "其他"

    guest = models.ForeignKey(
        Guest, on_delete=models.CASCADE, related_name="documents", verbose_name="所属客人"
    )
    doc_type = models.CharField(
        max_length=12, choices=DocType.choices, default=DocType.ID_CARD, verbose_name="证件类型"
    )
    doc_no_ciphertext = models.TextField(verbose_name="证件号密文")
    doc_no_hash = models.CharField(
        max_length=64, verbose_name="证件号哈希", help_text="HMAC-SHA256，用于查重，不存明文"
    )
    issuing_country = models.CharField(
        max_length=8, blank=True, default="CN", verbose_name="签发国家/地区"
    )
    expiry_date = models.DateField(null=True, blank=True, verbose_name="有效期至")
    verified_at = models.DateTimeField(null=True, blank=True, verbose_name="核验时间")
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="guest_documents_verified",
        verbose_name="核验人",
    )
    retention_until: models.DateField = models.DateField(
        null=True, blank=True, verbose_name="保留截止日"
    )
    is_primary = models.BooleanField(default=True, verbose_name="主证件")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    guest_id: Any
    verified_by_id: Any

    class Meta:
        verbose_name = "客人证件"
        verbose_name_plural = "客人证件"
        constraints = [
            # 同一物业内同一证件（hash）只能登记一次：重复登记提示已存在，不自动合并
            models.UniqueConstraint(
                fields=("guest", "doc_type", "doc_no_hash"),
                name="uq_guest_doc_hash",
            ),
        ]

    def __str__(self):
        return f"{self.guest_id}:{self.get_doc_type_display()}(hash {self.doc_no_hash[:8]}…)"

    def save(self, *args, **kwargs):
        # WB-105：核验人与核验时间必须成对（同空或同非空）。
        if bool(self.verified_at) != bool(self.verified_by_id):
            raise ValidationError("证件核验人与核验时间必须同时提供或同时为空。")
        super().save(*args, **kwargs)


class GuestDocumentIndex(models.Model):
    """证件物业级占用索引（WB-102）：并发登记的唯一仲裁载体。

    UniqueConstraint(property, doc_no_hash) 保证同物业内同一证件（hash）
    只能被一个客人档案占用；register_document 在同一事务内对其
    select_for_update + upsert：
      * 同一 guest 重复登记 → 占用不变（更新语义）；
      * 不同 guest 争抢同一证件 → IntegrityError → 服务层转友好中文错误。
    当前系统没有证件解除关联/删除路径（GuestDocument 无 delete 服务，
    模型层亦无级联删除入口），故索引仅做登记语义，不做解除维护；
    若未来增加换证/解绑服务，必须在同一事务内同步删除/迁移本索引行。
    """

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="guest_document_indexes",
        verbose_name="所属酒店",
    )
    guest = models.ForeignKey(
        Guest, on_delete=models.PROTECT, related_name="document_indexes", verbose_name="占用客人"
    )
    doc_no_hash = models.CharField(
        max_length=64,
        verbose_name="证件号哈希",
        help_text="HMAC-SHA256，与 GuestDocument.doc_no_hash 同源",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    property_id: Any
    guest_id: Any

    class Meta:
        verbose_name = "证件物业级占用索引"
        verbose_name_plural = "证件物业级占用索引"
        constraints = [
            models.UniqueConstraint(
                fields=("property", "doc_no_hash"), name="uq_guestdocindex_property_hash"
            ),
        ]

    def __str__(self):
        return f"{self.property_id}:{self.doc_no_hash[:8]}… → guest {self.guest_id}"


class GuestMembership(models.Model):
    """会员关系（V1.1 M-16 P0）：Guest 之上的物业会员关系，不是第二份自然人主档。"""

    class Status(models.TextChoices):
        PENDING = "PENDING", "待生效"
        ACTIVE = "ACTIVE", "有效"
        SUSPENDED = "SUSPENDED", "暂停"
        EXPIRED = "EXPIRED", "已过期"
        TERMINATED = "TERMINATED", "已终止"

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="guest_memberships",
        verbose_name="所属酒店",
    )
    guest = models.ForeignKey(
        Guest, on_delete=models.PROTECT, related_name="memberships", verbose_name="客人"
    )
    member_no = models.CharField(
        max_length=32, verbose_name="会员编号", help_text="按物业唯一；旧编号冲突进入人工复核"
    )
    level_code = models.CharField(max_length=32, default="STANDARD", verbose_name="当前等级代码")
    active_guest_key = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        editable=False,
        verbose_name="有效会员唯一键",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE, verbose_name="会员状态"
    )
    joined_at = models.DateField(null=True, blank=True, verbose_name="加入日期")
    expires_at = models.DateField(null=True, blank=True, verbose_name="有效期至")
    source = models.CharField(
        max_length=32, blank=True, verbose_name="来源", help_text="新建 / 历史导入 / 人工核验"
    )
    legacy_vip_id = models.CharField(
        max_length=32,
        blank=True,
        verbose_name="旧 Vip 来源映射",
        help_text="旧 hotelapp.Vip 主键；未核验记录不得计入新会员总数",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    property_id: Any
    guest_id: Any

    class Meta:
        verbose_name = "会员关系"
        verbose_name_plural = "会员关系"
        constraints = [
            models.UniqueConstraint(
                fields=("property", "member_no"), name="uq_membership_property_no"
            ),
        ]

    def save(self, *args, **kwargs):
        desired_key = (
            f"{self.property_id}:{self.guest_id}"
            if self.property_id and self.guest_id and self.status == self.Status.ACTIVE
            else None
        )
        if self.active_guest_key != desired_key:
            self.active_guest_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"active_guest_key"}
        if self.status == self.Status.ACTIVE and self.guest_id and self.property_id:
            dup = (
                GuestMembership.objects.filter(
                    property_id=self.property_id,
                    guest_id=self.guest_id,
                    status=self.Status.ACTIVE,
                )
                .exclude(pk=self.pk)
                .exists()
            )
            if dup:
                raise ValidationError("该客人已有有效会员关系（M-16 规则 1）")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.member_no} ({self.guest_id}) {self.level_code}"


class MembershipLevelHistory(models.Model):
    """会员等级历史（V1.1 M-16）：不可变，记录旧/新等级、原因、操作者。"""

    membership = models.ForeignKey(
        GuestMembership,
        on_delete=models.PROTECT,
        related_name="level_history",
        verbose_name="会员关系",
    )
    from_level = models.CharField(max_length=32, blank=True, verbose_name="原等级")
    to_level = models.CharField(max_length=32, verbose_name="新等级")
    reason = models.CharField(max_length=240, blank=True, verbose_name="原因")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="membership_level_changes",
        verbose_name="操作者",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="生效时间")

    class Meta:
        verbose_name = "会员等级历史"
        verbose_name_plural = "会员等级历史"
        ordering = ("-created_at",)

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("会员等级历史不可修改（只追加）")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("会员等级历史不可删除")


# ---------------------------------------------------------------------------
# 隐私数据生命周期治理（V1.0 任务书 TASK-02）：保留策略 / 法律冻结 / 清理执行记录。
# 审批复用 billing.ApprovalRecord（action_code=privacy.legal_hold_*），不重复建设审批模型。
# 详见 apps/guests/retention.py 服务层说明（清理规则/不级联删账务/LegalHold 冻结语义）。


class RetentionPolicy(models.Model):
    """数据保留策略：某类 Guest 数据保留多久、依据什么、是否启用。"""

    class DataCategory(models.TextChoices):
        GUEST_PROFILE = "GUEST_PROFILE", "住客主档（含姓名密文/联系密文）"
        GUEST_DOCUMENT = "GUEST_DOCUMENT", "证件登记记录"
        MARKETING_CONSENT = "MARKETING_CONSENT", "营销同意记录"

    class RetentionBasis(models.TextChoices):
        LAW = "LAW", "法律法规要求"
        CONTRACT = "CONTRACT", "合同义务"
        LEGITIMATE_INTEREST = "LEGITIMATE_INTEREST", "正当业务目的"

    property: models.ForeignKey = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="retention_policies",
        verbose_name="物业",
    )
    data_category: models.CharField = models.CharField(
        max_length=32, choices=DataCategory.choices, verbose_name="数据类别"
    )
    retention_days: models.PositiveIntegerField = models.PositiveIntegerField(
        verbose_name="保留天数（自最后退房/最后交互起算）"
    )
    retention_basis: models.CharField = models.CharField(
        max_length=24, choices=RetentionBasis.choices, verbose_name="保留依据"
    )
    basis_note: models.CharField = models.CharField(
        max_length=240, blank=True, verbose_name="依据说明（法条/条款号）"
    )
    enabled: models.BooleanField = models.BooleanField(default=True, verbose_name="启用")
    enabled_policy_key: models.CharField = models.CharField(
        max_length=96,
        null=True,
        blank=True,
        unique=True,
        editable=False,
        verbose_name="启用策略唯一键",
        help_text="property:data_category；仅 enabled=True 时有值，保证每物业每类仅一条启用策略",
    )
    effective_from: models.DateField = models.DateField(verbose_name="生效日期")
    created_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="retention_policies_created",
        verbose_name="创建人",
    )
    updated_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="retention_policies_updated",
        verbose_name="最后修改人",
    )
    created_at: models.DateTimeField = models.DateTimeField(
        auto_now_add=True, verbose_name="创建时间"
    )
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "隐私保留策略"
        verbose_name_plural = "隐私保留策略"
        ordering = ("property", "data_category")
        constraints = [
            # MySQL 8 不支持条件唯一约束（会被静默忽略）：用「可空唯一键」替代
            # 条件 UniqueConstraint，由 save() 维护 enabled_policy_key。
            # 降级口径与 GuestMembership.active_guest_key 一致。
            models.CheckConstraint(
                check=models.Q(retention_days__gt=0), name="ck_retention_days_positive"
            ),
        ]

    def save(self, *args, **kwargs):
        desired_key = (
            f"{self.property_id}:{self.data_category}"
            if self.property_id and self.enabled
            else None
        )
        if self.enabled_policy_key != desired_key:
            self.enabled_policy_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"enabled_policy_key"}
        super().save(*args, **kwargs)

    def clean(self):
        if self.retention_days <= 0:
            raise ValidationError("保留天数必须大于零。")

    def __str__(self):
        return f"{self.property.code}/{self.data_category}/{self.retention_days}d"


class LegalHold(models.Model):
    """法律冻结：冻结期内对应对象的一切清理动作跳过（skipped），不得删除。

    target_type 当前取值：GUEST（住客主档）。审批走 billing.ApprovalRecord
    （action_code=privacy.legal_hold / privacy.legal_hold_release），
    approval_reference 保存对应审批记录的字符串引用（entity_id）。
    """

    class HoldStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "生效中"
        RELEASED = "RELEASED", "已解除"

    property: models.ForeignKey = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="legal_holds",
        verbose_name="物业",
    )
    target_type: models.CharField = models.CharField(max_length=32, verbose_name="冻结对象类型")
    target_id: models.CharField = models.CharField(max_length=64, verbose_name="冻结对象ID")
    hold_reason: models.CharField = models.CharField(max_length=240, verbose_name="冻结原因")
    hold_status: models.CharField = models.CharField(
        max_length=12,
        choices=HoldStatus.choices,
        default=HoldStatus.ACTIVE,
        verbose_name="冻结状态",
    )
    requested_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="legal_holds_requested",
        verbose_name="申请人",
    )
    approved_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="legal_holds_approved",
        verbose_name="批准人（审批记录通过后回填）",
    )
    approval_reference: models.CharField = models.CharField(
        max_length=64, blank=True, verbose_name="审批记录引用（ApprovalRecord.id）"
    )
    started_at: models.DateTimeField = models.DateTimeField(
        default=timezone.now, verbose_name="冻结开始"
    )
    released_at: models.DateTimeField = models.DateTimeField(
        null=True, blank=True, verbose_name="解除时间"
    )
    release_reason: models.CharField = models.CharField(
        max_length=240, blank=True, verbose_name="解除原因"
    )

    class Meta:
        verbose_name = "隐私法律冻结"
        verbose_name_plural = "隐私法律冻结"
        ordering = ("-started_at",)
        indexes = [
            models.Index(
                fields=("property", "target_type", "target_id"), name="idx_legalhold_target"
            ),
            models.Index(fields=("hold_status",), name="idx_legalhold_status"),
        ]
        constraints = [
            models.CheckConstraint(
                check=~models.Q(hold_status="ACTIVE") | models.Q(released_at__isnull=True),
                name="ck_legalhold_active_no_release",
            ),
            models.CheckConstraint(
                check=models.Q(hold_status="ACTIVE") | models.Q(release_reason__gt=""),
                name="ck_legalhold_release_has_reason",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(
                    "property_id",
                    "target_type",
                    "target_id",
                    "hold_reason",
                    "requested_by_id",
                    "hold_status",
                )
                .first()
            )
            if previous and any(
                previous[field] != getattr(self, field)
                for field in (
                    "property_id",
                    "target_type",
                    "target_id",
                    "hold_reason",
                    "requested_by_id",
                )
            ):
                raise ValidationError("冻结事实（对象/原因/申请人）不可修改；请解除后重新申请。")
            if previous and previous["hold_status"] == self.HoldStatus.RELEASED:
                raise ValidationError("已解除的冻结不可再变更（只追加新冻结）。")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("法律冻结记录不可删除；走解除流程留痕。")

    def __str__(self):
        return f"{self.target_type}#{self.target_id} {self.hold_status}"


class RetentionExecution(models.Model):
    """清理执行记录：一次 dry-run 或正式执行的完整统计与审批引用。

    execution_status：DRAFT(dry-run 完成)→APPROVED→EXECUTING→COMPLETED/FAILED。
    正式执行（dry_run=False）必须引用一条 APPROVED 的 ApprovalRecord
    （action_code=privacy.retention_execute），且申请人≠审批人由审批模型保证。
    """

    class ExecutionStatus(models.TextChoices):
        DRAFT = "DRAFT", "试运行完成（未执行）"
        APPROVED = "APPROVED", "已批准待执行"
        EXECUTING = "EXECUTING", "执行中"
        COMPLETED = "COMPLETED", "已完成"
        FAILED = "FAILED", "失败（可重试）"

    property: models.ForeignKey = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="retention_executions",
        verbose_name="物业",
    )
    policy: models.ForeignKey = models.ForeignKey(
        RetentionPolicy,
        on_delete=models.PROTECT,
        related_name="executions",
        verbose_name="执行策略",
    )
    execution_status: models.CharField = models.CharField(
        max_length=12,
        choices=ExecutionStatus.choices,
        default=ExecutionStatus.DRAFT,
        verbose_name="执行状态",
    )
    dry_run: models.BooleanField = models.BooleanField(
        default=True, verbose_name="试运行（只统计不写）"
    )
    as_of: models.DateField = models.DateField(verbose_name="清理基准日")
    candidate_count: models.PositiveIntegerField = models.PositiveIntegerField(
        default=0, verbose_name="候选数"
    )
    processed_count: models.PositiveIntegerField = models.PositiveIntegerField(
        default=0, verbose_name="已处理数"
    )
    skipped_count: models.PositiveIntegerField = models.PositiveIntegerField(
        default=0, verbose_name="冻结/关联跳过数"
    )
    failure_count: models.PositiveIntegerField = models.PositiveIntegerField(
        default=0, verbose_name="失败数"
    )
    failure_detail: models.JSONField = models.JSONField(
        null=True, blank=True, verbose_name="失败明细（可重试清单）"
    )
    operator: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="retention_executions_operated",
        verbose_name="执行人",
    )
    approval_reference: models.CharField = models.CharField(
        max_length=64, blank=True, verbose_name="审批记录引用（正式执行必填）"
    )
    started_at: models.DateTimeField = models.DateTimeField(
        default=timezone.now, verbose_name="开始时间"
    )
    completed_at: models.DateTimeField = models.DateTimeField(
        null=True, blank=True, verbose_name="完成时间"
    )

    class Meta:
        verbose_name = "隐私清理执行记录"
        verbose_name_plural = "隐私清理执行记录"
        ordering = ("-started_at",)
        indexes = [
            models.Index(fields=("property", "execution_status"), name="idx_retx_prop_status"),
        ]

    def clean(self):
        if not self.dry_run and not self.approval_reference:
            raise ValidationError("正式执行（dry_run=False）必须引用一条已批准的审批记录。")

    def __str__(self):
        return f"{self.policy.data_category}/{self.as_of}/{'dry' if self.dry_run else 'run'}"
