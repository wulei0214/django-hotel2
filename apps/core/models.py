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
