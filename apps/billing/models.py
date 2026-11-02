from django.core.exceptions import ValidationError
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.db.models import Q


class Folio(models.Model):
    """住客账单（户头）：V1.1 M-04 支持多账单（主账/住客/公司/内部/押金）。

    规则：每张预订只有一个 MASTER 账单（数据库约束）；预订余额 = 所有未关闭
    Folio 余额之和；不可变凭证不搬迁——拆账/转账经 FolioTransfer 记录表达。
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "未结"
        CLOSED = "CLOSED", "已结"
        VOIDED = "VOIDED", "已作废"

    class FolioType(models.TextChoices):
        MASTER = "MASTER", "主账单"
        GUEST = "GUEST", "住客分账"
        COMPANY = "COMPANY", "公司账"
        HOUSE = "HOUSE", "内部账"
        DEPOSIT = "DEPOSIT", "押金单"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="folios",
        verbose_name="所属酒店",
    )
    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.PROTECT,
        related_name="folios",
        verbose_name="预订主单",
        help_text="M-04：一单可多账单；MASTER 唯一（数据库约束）",
    )
    folio_type = models.CharField(
        max_length=12, choices=FolioType.choices, default=FolioType.MASTER,
        verbose_name="账单类型")
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
        constraints = [
            models.UniqueConstraint(
                fields=("reservation",),
                condition=models.Q(folio_type="MASTER"),
                name="uq_folio_master_per_reservation",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.folio_type == self.FolioType.MASTER and self.reservation_id:
            dup = Folio.objects.filter(
                reservation_id=self.reservation_id,
                folio_type=self.FolioType.MASTER,
            ).exclude(pk=self.pk).exists()
            if dup:
                raise ValidationError("该预订已存在主账单（M-04：MASTER 唯一）")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reservation.booking_no} / {self.status}"


class FolioTransfer(models.Model):
    """账务转移记录（V1.1 M-04）：不搬迁原凭证，成对表达转出/转入。"""

    from_folio = models.ForeignKey(
        Folio, on_delete=models.PROTECT, related_name="transfers_out",
        verbose_name="转出账单")
    to_folio = models.ForeignKey(
        Folio, on_delete=models.PROTECT, related_name="transfers_in",
        verbose_name="转入账单")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="转移金额")
    reason = models.CharField(max_length=240, verbose_name="原因")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="folio_transfers_approved",
        verbose_name="审批人")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="时间")

    class Meta:
        verbose_name = "账务转移记录"
        verbose_name_plural = "账务转移记录"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.from_folio_id}→{self.to_folio_id} {self.amount}"


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



class Refund(models.Model):
    """退款申请与审批记录；审批仅登记人工退款已实际完成，不调用支付渠道。"""

    class Status(models.TextChoices):
        PENDING = "PENDING", "待审批"
        SUCCEEDED = "SUCCEEDED", "已确认完成"
        REJECTED = "REJECTED", "已驳回"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="refunds",
        verbose_name="所属酒店",
    )
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="refunds",
        verbose_name="原支付流水",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="退款金额")
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    reason = models.CharField(max_length=240, verbose_name="退款原因")
    idempotency_key = models.CharField(max_length=64, verbose_name="幂等键")
    provider_ref = models.CharField(max_length=80, unique=True, verbose_name="退款登记编号")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING,
        verbose_name="退款状态",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="requested_refunds", verbose_name="申请人",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="approved_refunds", verbose_name="审批人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="申请时间")
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name="处理时间")

    class Meta:
        verbose_name = "退款申请"
        verbose_name_plural = "退款申请"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "idempotency_key"),
                name="uq_refund_property_idempotency",
            ),
            models.CheckConstraint(check=Q(amount__gt=0), name="ck_refund_amount_positive"),
        ]
        permissions = (
            ("refund_request", "可以登记退款申请"),
            ("refund_approve", "可以审批退款申请"),
        )

    def __str__(self):
        return f"{self.provider_ref} / {self.status}"


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
    revenue_code = models.CharField(max_length=32, blank=True, verbose_name="收入科目")
    description = models.CharField(max_length=240, blank=True, verbose_name="摘要")
    quantity = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("1.00"), verbose_name="数量")
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="单价")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="金额")
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
