from django.conf import settings
from django.db import models
from django.db.models import Q


class Folio(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "未结"
        CLOSED = "CLOSED", "已结"
        VOIDED = "VOIDED", "已作废"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="folios"
    )
    reservation = models.OneToOneField(
        "reservations.Reservation",
        on_delete=models.PROTECT,
        related_name="folio",
    )
    guest = models.ForeignKey(
        "guests.Guest", on_delete=models.PROTECT, related_name="folios"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    currency = models.CharField(max_length=3, default="CNY")
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

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
    objects = models.Manager.from_queryset(ImmutableLedgerEntryQuerySet)()
    class EntryType(models.TextChoices):
        ROOM_CHARGE = "ROOM_CHARGE", "房费"
        DEPOSIT = "DEPOSIT", "押金"
        PAYMENT = "PAYMENT", "收款"
        REFUND = "REFUND", "退款"
        ADJUSTMENT = "ADJUSTMENT", "调整"
        REVERSAL = "REVERSAL", "红字冲销"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="ledger_entries"
    )
    folio = models.ForeignKey(
        Folio, on_delete=models.PROTECT, related_name="entries"
    )
    stay = models.ForeignKey(
        "reservations.Stay",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    # Folio 视角：消费/房费为正；押金/收款为负；余额为正表示客人仍需支付。
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="CNY")
    business_date = models.DateField()
    reference = models.CharField(max_length=160, unique=True)
    description = models.CharField(max_length=240, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="posted_ledger_entries",
    )
    void_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversals",
    )
    void_reason = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=("property", "business_date"), name="billing_ledg_propert_9f4ef8_idx"),
            models.Index(fields=("folio", "business_date"), name="billing_ledg_folio_i_31c7c2_idx"),
        ]
        constraints = [
            models.CheckConstraint(check=~Q(amount=0), name="ck_ledger_amount_nonzero"),
            models.CheckConstraint(
                check=Q(void_of__isnull=True) | Q(void_reason__gt=""),
                name="ck_ledger_reversal_has_reason",
            ),
        ]
        verbose_name = "不可变账本凭证"
        verbose_name_plural = "不可变账本凭证"

    def __str__(self):
        return f"{self.reference}: {self.amount} {self.currency}"

    def delete(self, *args, **kwargs):
        raise RuntimeError("LedgerEntry 不允许物理删除；请创建红字冲销凭证。")


class Payment(models.Model):
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
        "core.Property", on_delete=models.PROTECT, related_name="payments"
    )
    reservation = models.ForeignKey(
        "reservations.Reservation", on_delete=models.PROTECT, related_name="payments"
    )
    folio = models.ForeignKey(Folio, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=24, default="mock")
    provider_ref = models.CharField(max_length=80, unique=True)
    method = models.CharField(max_length=24, default="MOCK")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="CNY")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name="ck_payment_amount_positive"),
        ]
        verbose_name = "支付流水"
        verbose_name_plural = "支付流水"

    def __str__(self):
        return f"{self.provider_ref} / {self.status}"
