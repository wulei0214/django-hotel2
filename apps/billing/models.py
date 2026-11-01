from django.conf import settings
from django.db import models
from django.db.models import Q


class Folio(models.Model):
    """住客账单（户头）：一张预订一个户头，账务凭证挂在户头下。"""

    class Status(models.TextChoices):
        OPEN = "OPEN", "未结"
        CLOSED = "CLOSED", "已结"
        VOIDED = "VOIDED", "已作废"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="folios",
        verbose_name="所属酒店",
    )
    reservation = models.OneToOneField(
        "reservations.Reservation",
        on_delete=models.PROTECT,
        related_name="folio",
        verbose_name="预订主单",
    )
    guest = models.ForeignKey(
        "guests.Guest", on_delete=models.PROTECT, related_name="folios",
        verbose_name="住客",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN,
        verbose_name="户头状态",
    )
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    opened_at = models.DateTimeField(auto_now_add=True, verbose_name="开户时间")
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="结账时间")

    class Meta:
        ordering = ("-opened_at",)
        verbose_name = "住客账单"
        verbose_name_plural = "住客账单"

    def __str__(self):
        return f"{self.reservation.booking_no} / {self.status}"


class ImmutableLedgerEntryQuerySet(models.QuerySet):
    def delete(self):
        raise RuntimeError("LedgerEntry 不允许批量物理删除；请创建红字冲销凭证。")

    def update(self, **kwargs):
        raise RuntimeError("LedgerEntry 不允许直接修改；请创建红字冲销凭证。")


class LedgerEntry(models.Model):
    """不可变账本凭证：账务事实只增不改不删，错账用红字冲销（void_of 指向原凭证）。"""

    objects = models.Manager.from_queryset(ImmutableLedgerEntryQuerySet)()

    class EntryType(models.TextChoices):
        ROOM_CHARGE = "ROOM_CHARGE", "房费"
        DEPOSIT = "DEPOSIT", "押金"
        PAYMENT = "PAYMENT", "收款"
        REFUND = "REFUND", "退款"
        ADJUSTMENT = "ADJUSTMENT", "调整"
        REVERSAL = "REVERSAL", "红字冲销"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="ledger_entries",
        verbose_name="所属酒店",
    )
    folio = models.ForeignKey(
        Folio, on_delete=models.PROTECT, related_name="entries",
        verbose_name="住客账单",
    )
    stay = models.ForeignKey(
        "reservations.Stay",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
        verbose_name="关联在住",
        help_text="房费凭证关联到具体在住记录；其他类型可空",
    )
    entry_type = models.CharField(
        max_length=20, choices=EntryType.choices, verbose_name="凭证类型",
    )
    # Folio 视角：消费/房费为正；押金/收款为负；余额为正表示客人仍需支付。
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="金额（带符号）",
        help_text="房费/消费为正，押金/收款为负，不得为 0；红字冲销为负数",
    )
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    business_date = models.DateField(
        verbose_name="营业日期",
        help_text="夜审口径的营业日，非自然日",
    )
    reference = models.CharField(
        max_length=160, unique=True, verbose_name="凭证号",
        help_text="全局唯一，幂等入账依据",
    )
    description = models.CharField(max_length=240, blank=True, verbose_name="摘要")
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="posted_ledger_entries",
        verbose_name="记账人",
    )
    void_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversals",
        verbose_name="冲销原凭证",
        help_text="红字冲销凭证指向被冲销的原凭证；普通凭证为空",
    )
    void_reason = models.CharField(
        max_length=240, blank=True, verbose_name="冲销原因",
        help_text="红字冲销凭证必填（数据库约束强制）",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="入账时间")

    class Meta:
        verbose_name = "不可变账本凭证"
        verbose_name_plural = "不可变账本凭证"
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=("property", "business_date"), name="idx_ledger_prop_date"),
            models.Index(fields=("folio", "business_date"), name="idx_ledger_folio_date"),
        ]
        constraints = [
            models.CheckConstraint(check=~Q(amount=0), name="ck_ledger_amount_nonzero"),
            models.CheckConstraint(
                check=Q(void_of__isnull=True) | Q(void_reason__gt=""),
                name="ck_ledger_reversal_has_reason",
            ),
        ]

    def __str__(self):
        return f"{self.reference}: {self.amount} {self.currency}"

    def delete(self, *args, **kwargs):
        raise RuntimeError("LedgerEntry 不允许物理删除；请创建红字冲销凭证。")


class Payment(models.Model):
    """支付流水：一笔支付意图及其结果，与预订关联、入账后落到账本凭证。"""

    class Status(models.TextChoices):
        CREATED = "CREATED", "已创建"
        PENDING = "PENDING", "待支付"
        SUCCEEDED = "SUCCEEDED", "成功"
        FAILED = "FAILED", "失败"
        CANCELLED = "CANCELLED", "已取消"
        REFUND_PENDING = "REFUND_PENDING", "退款处理中"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "部分退款"
        REFUNDED = "REFUNDED", "已退款"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="payments",
        verbose_name="所属酒店",
    )
    reservation = models.ForeignKey(
        "reservations.Reservation", on_delete=models.PROTECT, related_name="payments",
        verbose_name="预订主单",
    )
    folio = models.ForeignKey(
        Folio, on_delete=models.PROTECT, related_name="payments",
        verbose_name="住客账单",
    )
    provider = models.CharField(
        max_length=24, default="mock", verbose_name="支付渠道",
        help_text="如 mock（本地模拟）/ wechat / alipay",
    )
    provider_ref = models.CharField(
        max_length=80, unique=True, verbose_name="渠道流水号",
        help_text="支付渠道返回的唯一引用，回调幂等依据",
    )
    method = models.CharField(max_length=24, default="MOCK", verbose_name="支付方式")
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="支付金额",
        help_text="必须大于 0",
    )
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.PENDING,
        verbose_name="支付状态",
    )
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name="支付完成时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name="ck_payment_amount_positive"),
        ]
        verbose_name = "支付流水"
        verbose_name_plural = "支付流水"

    def __str__(self):
        return f"{self.provider_ref} / {self.status}"
