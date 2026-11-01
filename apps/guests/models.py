from django.db import models


class Guest(models.Model):
    """住客档案：预订/在住客人的自然人与联系方式记录，同物业内手机号唯一。"""

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
        help_text="去分隔符后的纯数字手机号，同物业内唯一（预订查询凭据）",
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
        constraints = [
            models.UniqueConstraint(
                fields=("property", "phone_normalized"),
                name="uq_guest_property_phone",
            )
        ]
        indexes = [
            models.Index(fields=("property", "full_name"), name="idx_guest_prop_name"),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.phone})"
