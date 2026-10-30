from django.db import models


class Property(models.Model):
    """酒店物业。单店部署只初始化一条记录，多物业能力通过 property_id 预留。"""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "营业中"
        INACTIVE = "INACTIVE", "停用"

    code = models.CharField(max_length=32, unique=True, verbose_name="物业编码")
    name = models.CharField(max_length=120, verbose_name="酒店名称")
    timezone = models.CharField(max_length=64, default="Asia/Shanghai")
    currency = models.CharField(max_length=3, default="CNY")
    address = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)
        verbose_name = "酒店物业"
        verbose_name_plural = "酒店物业"

    def __str__(self):
        return f"{self.code} - {self.name}"
