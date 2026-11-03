from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Guest(models.Model):
    """住客档案：手机号是可共享联系方式，不作为自然人唯一身份键。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="guests",
        verbose_name="所属酒店",
    )
    full_name = models.CharField(max_length=80, verbose_name="姓名")
    phone = models.CharField(
        max_length=24, verbose_name="联系电话",
        help_text="展示用原格式；查询/去重使用 phone_normalized",
    )
    phone_normalized = models.CharField(
        max_length=24, null=True, blank=True, verbose_name="标准化手机号",
        help_text="去分隔符后的标准化号码；仅用于检索与候选匹配，不代表自然人唯一身份",
    )
    email = models.EmailField(blank=True, verbose_name="电子邮箱")
    notes = models.TextField(blank=True, verbose_name="备注")
    consent_version = models.CharField(
        max_length=32, blank=True, verbose_name="隐私协议版本",
        help_text="客人同意隐私条款时的版本号，空表示未采集",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "住客档案"
        verbose_name_plural = "住客档案"
        ordering = ("id",)
        indexes = [
            models.Index(fields=("property", "full_name"), name="idx_guest_prop_name"),
            models.Index(fields=("property", "phone_normalized"), name="idx_guest_prop_phone"),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.phone})"


class GuestPhone(models.Model):
    """客人电话（V1.1 M-03 P0）：可多条、有历史；不以手机号作为自然人唯一主键。"""

    class PhoneType(models.TextChoices):
        MOBILE = "MOBILE", "手机"
        HOME = "HOME", "家庭电话"
        WORK = "WORK", "工作电话"
        OTHER = "OTHER", "其他"

    guest = models.ForeignKey(
        Guest, on_delete=models.CASCADE, related_name="phones",
        verbose_name="所属客人")
    phone = models.CharField(max_length=24, verbose_name="原始号码")
    phone_normalized = models.CharField(max_length=24, verbose_name="标准化号码")
    phone_type = models.CharField(
        max_length=8, choices=PhoneType.choices, default=PhoneType.MOBILE,
        verbose_name="号码类型")
    is_primary = models.BooleanField(default=False, verbose_name="主号码")
    primary_guest_key = models.CharField(
        max_length=32, null=True, blank=True, unique=True, editable=False,
        verbose_name="主号码唯一键")
    shared_review_required = models.BooleanField(
        default=False, verbose_name="共享号码待人工核验",
        help_text="同一标准化号码关联多个客人时置位（V1.1 M-03 规则 2）")
    source = models.CharField(
        max_length=32, blank=True, verbose_name="来源",
        help_text="预订 / 前台登记 / 会员 / 历史迁移")
    verified_at = models.DateTimeField(null=True, blank=True, verbose_name="验证时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "客人电话"
        verbose_name_plural = "客人电话"
        indexes = [
            models.Index(fields=("phone_normalized",), name="idx_guestphone_norm"),
        ]

    def save(self, *args, **kwargs):
        desired_key = str(self.guest_id) if self.guest_id and self.is_primary else None
        if self.primary_guest_key != desired_key:
            self.primary_guest_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"primary_guest_key"}
        if self.is_primary and self.guest_id:
            dup = GuestPhone.objects.filter(
                guest_id=self.guest_id, is_primary=True,
            ).exclude(pk=self.pk).exists()
            if dup:
                raise ValidationError("该客人已有主号码（M-03：每客人一个主号码）")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.guest_id}:{self.phone_normalized}({'主' if self.is_primary else '副'})"


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
        Guest, on_delete=models.CASCADE, related_name="documents",
        verbose_name="所属客人")
    doc_type = models.CharField(
        max_length=12, choices=DocType.choices, default=DocType.ID_CARD,
        verbose_name="证件类型")
    doc_no_ciphertext = models.TextField(verbose_name="证件号密文")
    doc_no_hash = models.CharField(max_length=64, verbose_name="证件号哈希",
                                   help_text="HMAC-SHA256，用于查重，不存明文")
    issuing_country = models.CharField(max_length=8, blank=True, default="CN",
                                       verbose_name="签发国家/地区")
    expiry_date = models.DateField(null=True, blank=True, verbose_name="有效期至")
    verified_at = models.DateTimeField(null=True, blank=True, verbose_name="核验时间")
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="guest_documents_verified",
        verbose_name="核验人")
    retention_until = models.DateField(null=True, blank=True, verbose_name="保留截止日")
    is_primary = models.BooleanField(default=True, verbose_name="主证件")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

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


class GuestMembership(models.Model):
    """会员关系（V1.1 M-16 P0）：Guest 之上的物业会员关系，不是第二份自然人主档。"""

    class Status(models.TextChoices):
        PENDING = "PENDING", "待生效"
        ACTIVE = "ACTIVE", "有效"
        SUSPENDED = "SUSPENDED", "暂停"
        EXPIRED = "EXPIRED", "已过期"
        TERMINATED = "TERMINATED", "已终止"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="guest_memberships",
        verbose_name="所属酒店")
    guest = models.ForeignKey(
        Guest, on_delete=models.PROTECT, related_name="memberships",
        verbose_name="客人")
    member_no = models.CharField(max_length=32, verbose_name="会员编号",
                                 help_text="按物业唯一；旧编号冲突进入人工复核")
    level_code = models.CharField(max_length=32, default="STANDARD",
                                  verbose_name="当前等级代码")
    active_guest_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True, editable=False,
        verbose_name="有效会员唯一键")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE,
        verbose_name="会员状态")
    joined_at = models.DateField(null=True, blank=True, verbose_name="加入日期")
    expires_at = models.DateField(null=True, blank=True, verbose_name="有效期至")
    source = models.CharField(max_length=32, blank=True, verbose_name="来源",
                              help_text="新建 / 历史导入 / 人工核验")
    legacy_vip_id = models.CharField(
        max_length=32, blank=True, verbose_name="旧 Vip 来源映射",
        help_text="旧 hotelapp.Vip 主键；未核验记录不得计入新会员总数")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "会员关系"
        verbose_name_plural = "会员关系"
        constraints = [
            models.UniqueConstraint(fields=("property", "member_no"),
                                    name="uq_membership_property_no"),
        ]

    def save(self, *args, **kwargs):
        desired_key = f"{self.property_id}:{self.guest_id}" if self.property_id and self.guest_id and self.status == self.Status.ACTIVE else None
        if self.active_guest_key != desired_key:
            self.active_guest_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"active_guest_key"}
        if self.status == self.Status.ACTIVE and self.guest_id and self.property_id:
            dup = GuestMembership.objects.filter(
                property_id=self.property_id, guest_id=self.guest_id,
                status=self.Status.ACTIVE,
            ).exclude(pk=self.pk).exists()
            if dup:
                raise ValidationError("该客人已有有效会员关系（M-16 规则 1）")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.member_no} ({self.guest_id}) {self.level_code}"


class MembershipLevelHistory(models.Model):
    """会员等级历史（V1.1 M-16）：不可变，记录旧/新等级、原因、操作者。"""

    membership = models.ForeignKey(
        GuestMembership, on_delete=models.PROTECT, related_name="level_history",
        verbose_name="会员关系")
    from_level = models.CharField(max_length=32, blank=True, verbose_name="原等级")
    to_level = models.CharField(max_length=32, verbose_name="新等级")
    reason = models.CharField(max_length=240, blank=True, verbose_name="原因")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="membership_level_changes",
        verbose_name="操作者")
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
