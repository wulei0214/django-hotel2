# -*- coding: utf-8 -*-
"""P1 模型追加脚本：CashierShift / ChargeLine / TaxLine / IdempotencyRecord / StatusTransition
由 exec 运行（appended to core/billing models），生成迁移后删除本文件。"""
import io

# ---------- 1. billing/models.py 追加 CashierShift / ChargeLine / TaxLine ----------
p = "F:/Desktop/django-hotel/apps/billing/models.py"
with open(p, encoding="utf-8") as f:
    c = f.read()

if "CashierShift" not in c:
    c += '''

class CashierShift(models.Model):
    """现金班次对账（V1.1 M-10 P1）：前台现金收支按班次闭合，差异必须说明。"""

    class ShiftStatus(models.TextChoices):
        OPEN = "OPEN", "开班中"
        CLOSED = "CLOSED", "已关班"
        RECONCILED = "RECONCILED", "已对账"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="cashier_shifts",
        verbose_name="所属酒店")
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="cashier_shifts", verbose_name="收银员")
    opened_at = models.DateTimeField(auto_now_add=True, verbose_name="开班时间")
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="关班时间")
    opening_cash = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="开班备用金")
    expected_cash = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="应收现金",
        help_text="关班时由系统计算（开班备用金 + 现金收入 - 现金支出）")
    actual_cash = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="实收现金")
    variance = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="差异金额",
        help_text="actual - expected；差异必须填写说明")
    variance_reason = models.CharField(max_length=240, blank=True, verbose_name="差异说明")
    status = models.CharField(
        max_length=12, choices=ShiftStatus.choices, default=ShiftStatus.OPEN,
        verbose_name="班次状态")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "现金班次"
        verbose_name_plural = "现金班次"
        ordering = ("-opened_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "cashier"), condition=models.Q(status="OPEN"),
                name="uq_cashier_shift_one_open",
            ),
        ]

    def __str__(self):
        return f"{self.property_id}/{self.cashier_id} {self.status}"


class ChargeLine(models.Model):
    """账单科目明细（V1.1 M-11 P1）：一笔凭证拆成房费/餐饮/服务费/税等科目行。

    规则：sum(ChargeLine.amount) = LedgerEntry.amount。
    """

    class ChargeType(models.TextChoices):
        ROOM = "ROOM", "房费"
        FOOD = "FOOD", "餐饮"
        LAUNDRY = "LAUNDRY", "洗衣"
        EXTRA_BED = "EXTRA_BED", "加床"
        SERVICE = "SERVICE", "服务费"
        PENALTY = "PENALTY", "违约金"
        OTHER = "OTHER", "其他"

    ledger_entry = models.ForeignKey(
        LedgerEntry, on_delete=models.PROTECT, related_name="charge_lines",
        verbose_name="账本凭证")
    charge_type = models.CharField(
        max_length=12, choices=ChargeType.choices, verbose_name="科目类型")
    revenue_code = models.CharField(max_length=32, blank=True, verbose_name="收入科目",
                                    help_text="如 ROOM_RENT / FNB_FOOD / SPA_MASSAGE")
    description = models.CharField(max_length=240, blank=True, verbose_name="摘要")
    quantity = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("1.00"), verbose_name="数量")
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="单价")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="金额",
                                 help_text="quantity × unit_price；含税/不含税由物业配置")
    tax_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="税额")
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"), verbose_name="税率%")
    is_tax_inclusive = models.BooleanField(default=True, verbose_name="含税价")
    department = models.CharField(max_length=32, blank=True, verbose_name="部门")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "账单科目明细"
        verbose_name_plural = "账单科目明细"
        ordering = ("ledger_entry_id", "id")

    def __str__(self):
        return f"{self.ledger_entry_id}:{self.charge_type} {self.amount}"


class TaxLine(models.Model):
    """税费行（V1.1 M-11 P1）：发票/税额追溯。"""

    charge_line = models.ForeignKey(
        ChargeLine, on_delete=models.PROTECT, related_name="tax_lines",
        verbose_name="科目明细")
    tax_code = models.CharField(max_length=32, verbose_name="税种代码")
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, verbose_name="税率%")
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="税额")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "税费行"
        verbose_name_plural = "税费行"

    def __str__(self):
        return f"{self.charge_line_id}:{self.tax_code} {self.tax_amount}"
'''
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(c)
    print("billing P1 models appended")

# ---------- 2. core/models.py 追加 IdempotencyRecord / StatusTransition ----------
p = "F:/Desktop/django-hotel/apps/core/models.py"
with open(p, encoding="utf-8") as f:
    c = f.read()

if "IdempotencyRecord" not in c:
    c += '''

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
'''
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(c)
    print("core P1 models appended")
