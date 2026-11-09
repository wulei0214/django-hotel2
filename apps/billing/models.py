from django.core.exceptions import ValidationError
from decimal import Decimal
from django.conf import settings
from django.db import models, transaction
from django.db.models import F, Q
from django.db.models.signals import m2m_changed


class ImmutableFinancialFactQuerySet(models.QuerySet):
    """支付、退款和现金变动禁止绕过领域服务进行批量修改或删除。"""

    def update(self, **kwargs):
        raise ValidationError("财务事实禁止 QuerySet.update；请调用 BillingService。")

    def delete(self):
        raise ValidationError("财务事实禁止批量删除；请保留账务审计轨迹。")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("财务事实禁止 bulk_create；请逐笔调用领域服务。")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("财务事实禁止 bulk_update；请调用 BillingService。")




class ApprovalRecord(models.Model):
    """不可变高危审批事实；退款、冲销、夜审重开等通过此记录执行双人控制。"""

    objects = models.Manager.from_queryset(ImmutableFinancialFactQuerySet)()

    class Status(models.TextChoices):
        PENDING = "PENDING", "待审批"
        APPROVED = "APPROVED", "已批准"
        REJECTED = "REJECTED", "已驳回"
        EXPIRED = "EXPIRED", "已过期"
        CANCELLED = "CANCELLED", "已撤销"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT,
        related_name="approval_records", verbose_name="物业")
    action_code = models.CharField(max_length=64, verbose_name="动作代码")
    entity_type = models.CharField(max_length=64, verbose_name="目标实体类型")
    entity_id = models.CharField(max_length=64, verbose_name="目标实体ID")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="approvals_requested", verbose_name="申请人")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="approvals_approved",
        verbose_name="审批人")
    status = models.CharField(
        max_length=12, choices=Status.choices,
        default=Status.PENDING, verbose_name="审批状态")
    reason = models.CharField(max_length=240, verbose_name="申请原因")
    payload_snapshot = models.JSONField(
        null=True, blank=True, verbose_name="申请参数快照")
    idempotency_key = models.CharField(
        max_length=64, unique=True, verbose_name="幂等键")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="审批时间")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="过期时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="申请时间")

    class Meta:
        verbose_name = "高危审批记录"
        verbose_name_plural = "高危审批记录"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("property", "action_code", "status"),
                         name="idx_approval_prop_action"),
            models.Index(fields=("entity_type", "entity_id"),
                         name="idx_approval_entity"),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.status != self.Status.PENDING or self.approved_by_id or self.approved_at:
                raise ValidationError("审批记录必须从待审批状态创建。")
        else:
            previous = type(self).objects.filter(pk=self.pk).values(
                "property_id", "action_code", "entity_type", "entity_id",
                "requested_by_id", "reason", "payload_snapshot",
                "idempotency_key", "expires_at", "status").first()
            if previous:
                immutable = (
                    "property_id", "action_code", "entity_type", "entity_id",
                    "requested_by_id", "reason", "payload_snapshot",
                    "idempotency_key", "expires_at")
                if any(previous[field] != getattr(self, field) for field in immutable):
                    raise ValidationError("审批申请事实不可修改。")
                if previous["status"] != self.Status.PENDING:
                    raise ValidationError("已决策审批记录不可修改。")
                if self.status not in (
                        self.Status.APPROVED, self.Status.REJECTED,
                        self.Status.EXPIRED, self.Status.CANCELLED):
                    raise ValidationError("审批记录只允许从待审批转为最终状态。")
                if self.status in (self.Status.APPROVED, self.Status.REJECTED):
                    if not self.approved_by_id or not self.approved_at:
                        raise ValidationError("审批通过/驳回必须记录审批人和时间。")
                    if self.approved_by_id == self.requested_by_id:
                        raise ValidationError("申请人不能审批自己的申请。")
                elif self.approved_by_id:
                    raise ValidationError("过期/撤销审批不得伪造审批人。")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("审批记录不可删除")

    def __str__(self):
        return f"{self.action_code} {self.entity_type}#{self.entity_id} {self.status}"


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
    master_reservation_key = models.CharField(
        max_length=32, null=True, blank=True, unique=True, editable=False,
        verbose_name="主账唯一键")
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

    def save(self, *args, **kwargs):
        desired_key = str(self.reservation_id) if self.reservation_id and self.folio_type == self.FolioType.MASTER else None
        if self.master_reservation_key != desired_key:
            self.master_reservation_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"master_reservation_key"}
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
        raise ValidationError("LedgerEntry 不允许批量物理删除；请创建红字冲销凭证。")

    def update(self, **kwargs):
        raise ValidationError("LedgerEntry 不允许直接修改；请创建红字冲销凭证。")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("LedgerEntry 必须通过 BillingService 逐笔追加。")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("LedgerEntry 不允许批量修改；请创建红字冲销凭证。")


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
    approval_record = models.ForeignKey(
        "ApprovalRecord", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversal_entries", verbose_name="冲销审批记录",
        help_text="PMS2 新冲销必须关联值班经理/店长已批准的审批记录；历史冲销可为空",
    )
    legacy_workflow = models.BooleanField(
        default=False, verbose_name="历史旧流程冲销",
        help_text="仅迁移前无法证明审批关联的历史红字凭证为 True；新冲销必须为 False",
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
            models.CheckConstraint(
                check=(
                    Q(entry_type="REVERSAL", legacy_workflow=True,
                      approval_record__isnull=True)
                    | Q(entry_type="REVERSAL", legacy_workflow=False,
                        approval_record__isnull=False, void_of__isnull=False,
                        void_reason__gt="")
                    | (~Q(entry_type="REVERSAL") & Q(
                        legacy_workflow=False, approval_record__isnull=True,
                        void_of__isnull=True))
                ),
                name="ck_ledger_reversal_approval_link",
            ),
        ]
        # 红字冲销使用 billing.reverse_entry（财务申请/执行）与
        # billing.reversal_approve（值班经理/店长审批），不复用旧权限码。
        permissions = (
            ("reverse_entry", "可以申请并执行已批准的账本红字冲销（LedgerEntry）"),
            ("reversal_approve", "可以审批账本红字冲销申请（LedgerEntry）"),
        )

    def __str__(self):
        return f"{self.reference}: {self.amount} {self.currency}"

    def delete(self, *args, **kwargs):
        raise RuntimeError("LedgerEntry 不允许物理删除；请创建红字冲销凭证。")


class Payment(models.Model):
    """支付流水：一笔支付意图及其结果，与预订关联、入账后落到账本凭证。"""

    objects = models.Manager.from_queryset(ImmutableFinancialFactQuerySet)()

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
    cashier_shift = models.ForeignKey(
        "CashierShift", null=True, blank=True, on_delete=models.PROTECT,
        related_name="payments", verbose_name="收银班次",
        help_text="人工现金收款必须关联当班；线上/非现金支付可为空",
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

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.status not in (
                    self.Status.CREATED, self.Status.PENDING, self.Status.SUCCEEDED,
                    self.Status.FAILED, self.Status.CANCELLED):
                raise ValidationError("支付流水不得以退款完成状态直接创建。")
        else:
            previous = type(self).objects.filter(pk=self.pk).values(
                "cashier_shift_id", "provider", "provider_ref", "method", "amount",
                "currency", "property_id", "reservation_id", "folio_id",
                "paid_at", "status").first()
            if previous:
                immutable = ("cashier_shift_id", "provider", "provider_ref", "method",
                             "amount", "currency", "property_id", "reservation_id", "folio_id")
                if any(previous[field] != getattr(self, field) for field in immutable):
                    raise ValidationError("支付流水的原始交易事实及班次归属不可修改。")
                if previous["paid_at"] and self.paid_at != previous["paid_at"]:
                    raise ValidationError("支付成功时间不可回写。")
                allowed = {
                    self.Status.CREATED: {
                        self.Status.CREATED, self.Status.PENDING,
                        self.Status.FAILED, self.Status.CANCELLED},
                    self.Status.PENDING: {
                        self.Status.PENDING, self.Status.SUCCEEDED,
                        self.Status.FAILED, self.Status.CANCELLED},
                    self.Status.SUCCEEDED: {
                        self.Status.SUCCEEDED, self.Status.REFUND_PENDING,
                        self.Status.PARTIALLY_REFUNDED, self.Status.REFUNDED},
                    self.Status.REFUND_PENDING: {
                        self.Status.REFUND_PENDING, self.Status.SUCCEEDED,
                        self.Status.PARTIALLY_REFUNDED, self.Status.REFUNDED},
                    self.Status.PARTIALLY_REFUNDED: {
                        self.Status.PARTIALLY_REFUNDED, self.Status.REFUND_PENDING,
                        self.Status.REFUNDED},
                    # 渠道迟到 SUCCESS 是外部资金事实，允许纠正本地先前的
                    # FAILED/CANCELLED 意图状态；必须由回调服务同时生成补偿审计。
                    self.Status.FAILED: {self.Status.FAILED, self.Status.SUCCEEDED},
                    self.Status.CANCELLED: {self.Status.CANCELLED, self.Status.SUCCEEDED},
                    self.Status.REFUNDED: {self.Status.REFUNDED},
                }
                if self.status not in allowed.get(previous["status"], {previous["status"]}):
                    raise ValidationError("支付状态不允许该迁移。")
                if (self.status != previous["status"]
                        and self.status in (self.Status.PARTIALLY_REFUNDED, self.Status.REFUNDED)):
                    refunded_total = _current_refund_facts(
                        payment_id=self.pk, status=Refund.Status.SUCCEEDED
                    ).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")
                    has_pending_refund = _current_refund_facts(
                        payment_id=self.pk,
                        status__in=(Refund.Status.PENDING, Refund.Status.APPROVED),
                    ).exists()
                    if has_pending_refund:
                        raise ValidationError("存在待审批/已批准退款时，支付状态必须保持 REFUND_PENDING。")
                    if self.status == self.Status.PARTIALLY_REFUNDED:
                        if not (Decimal("0.00") < refunded_total < self.amount):
                            raise ValidationError("部分退款状态必须与已执行退款累计金额一致。")
                    elif refunded_total < self.amount:
                        raise ValidationError("已退款状态必须有足额成功退款凭证。")
                if (self.status != previous["status"]
                        and self.status == self.Status.REFUND_PENDING
                        and not _current_refund_facts(
                            payment_id=self.pk,
                            status__in=(Refund.Status.PENDING, Refund.Status.APPROVED)).exists()):
                    raise ValidationError("退款处理中状态必须存在待审批或已批准退款申请。")
                if (self.status != previous["status"]
                        and previous["status"] == self.Status.REFUND_PENDING
                        and self.status == self.Status.SUCCEEDED):
                    if _current_refund_facts(
                            payment_id=self.pk,
                            status=Refund.Status.SUCCEEDED).exists():
                        raise ValidationError("已有成功退款的支付不得回退为全额未退款状态。")
                    if _current_refund_facts(
                            payment_id=self.pk,
                            status__in=(Refund.Status.PENDING, Refund.Status.APPROVED)).exists():
                        raise ValidationError("仍有待处理退款的支付不得回退为 SUCCEEDED。")
        return super().save(*args, **kwargs)


    def delete(self, *args, **kwargs):
        raise ValidationError("支付流水不可物理删除；请按业务规则退款或冲销。")

    def __str__(self):
        return f"{self.provider_ref} / {self.status}"



class Refund(models.Model):
    """退款申请、审批和执行三段独立事实；只有执行成功才追加 REFUND 凭证。"""

    objects = models.Manager.from_queryset(ImmutableFinancialFactQuerySet)()

    class Status(models.TextChoices):
        PENDING = "PENDING", "待审批"
        APPROVED = "APPROVED", "已批准待执行"
        SUCCEEDED = "SUCCEEDED", "已执行完成"
        REJECTED = "REJECTED", "已驳回"
        EXPIRED = "EXPIRED", "已过期"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="refunds",
        verbose_name="所属酒店",
    )
    approval_record = models.ForeignKey(
        ApprovalRecord, null=True, blank=True, on_delete=models.PROTECT,
        related_name="refunds", verbose_name="关联审批记录")
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="refunds",
        verbose_name="原支付流水",
    )
    refund_liability = models.ForeignKey(
        "RefundLiability", null=True, blank=True, on_delete=models.PROTECT,
        related_name="refunds", verbose_name="关联待退款负债",
        help_text="仅用于财务后续主动发起退款时关联待退款负债；不自动发起退款。",
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
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="executed_refunds", verbose_name="执行人",
    )
    executed_at = models.DateTimeField(null=True, blank=True, verbose_name="执行时间")
    execution_reference = models.CharField(
        max_length=80, blank=True, verbose_name="退款渠道/凭据参考号")
    execution_idempotency_key = models.CharField(
        max_length=96, null=True, blank=True, unique=True,
        verbose_name="退款执行幂等键")
    execution_shift = models.ForeignKey(
        "CashierShift", null=True, blank=True, on_delete=models.PROTECT,
        related_name="executed_refunds", verbose_name="退款执行班次")
    legacy_workflow = models.BooleanField(
        default=False, editable=False, verbose_name="历史退款流程记录",
        help_text="迁移前无法证明申请/审批/执行三方分离的历史记录；只读保留，不代表新流程验收。")
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
            models.CheckConstraint(
                check=(Q(legacy_workflow=True) | Q(approved_by__isnull=True)
                       | ~Q(requested_by=F("approved_by"))),
                name="ck_refund_sod_request_approve"),
            models.CheckConstraint(
                check=(Q(legacy_workflow=True) | Q(executed_by__isnull=True)
                       | ~Q(requested_by=F("executed_by"))),
                name="ck_refund_sod_request_execute"),
            models.CheckConstraint(
                check=(Q(legacy_workflow=True) | Q(executed_by__isnull=True)
                       | Q(approved_by__isnull=True)
                       | ~Q(approved_by=F("executed_by"))),
                name="ck_refund_sod_approve_execute"),
            models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | (Q(status="PENDING", approved_by__isnull=True)
                       | Q(status__in=("APPROVED", "SUCCEEDED", "REJECTED"),
                           approved_by__isnull=False)
                       | Q(status="EXPIRED", approved_by__isnull=True))
                ),
                name="ck_refund_approval_facts"),
            models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | (Q(status="SUCCEEDED", executed_by__isnull=False,
                         executed_at__isnull=False,
                         execution_idempotency_key__isnull=False))
                    | (Q(status__in=("PENDING", "APPROVED", "REJECTED", "EXPIRED"),
                         executed_by__isnull=True, executed_at__isnull=True,
                         execution_idempotency_key__isnull=True,
                         execution_shift__isnull=True))
                ),
                name="ck_refund_execution_facts"),
            models.CheckConstraint(
                check=(Q(legacy_workflow=True) | Q(status="PENDING")
                       | Q(approval_record__isnull=False)),
                name="ck_refund_nonpending_approval_record"),
            models.CheckConstraint(
                check=(Q(legacy_workflow=True)
                       | (Q(status__in=("PENDING", "APPROVED"),
                            processed_at__isnull=True)
                          | Q(status__in=("REJECTED", "SUCCEEDED", "EXPIRED"),
                              processed_at__isnull=False))),
                name="ck_refund_processed_at_state"),
            models.CheckConstraint(
                check=(Q(legacy_workflow=True) | Q(status="SUCCEEDED")
                       | Q(execution_reference="")),
                name="ck_refund_execution_reference_state"),
        ]
        permissions = (
            ("refund_request", "可以登记退款申请"),
            ("refund_approve", "可以审批退款申请"),
            ("refund_execute", "可以执行已批准退款"),
        )

    def save(self, *args, **kwargs):
        if self.payment_id and self.property_id != self.payment.property_id:
            raise ValidationError("退款与原支付必须属于同一物业。")
        if self.payment_id and self.currency != self.payment.currency:
            raise ValidationError("退款币种必须与原支付币种一致。")
        if self.refund_liability_id:
            liability = self.refund_liability
            if (liability.property_id != self.property_id
                    or liability.reservation_id != self.payment.reservation_id
                    or liability.folio_id != self.payment.folio_id
                    or liability.currency != self.currency):
                raise ValidationError("退款、待退款负债与原支付必须属于同一物业、预订、账单和币种。")
            if not liability.source_payments.filter(pk=self.payment_id).exists():
                raise ValidationError("退款原支付必须属于该待退款负债登记的候选支付流水。")
        if not self.legacy_workflow and self.status == self.Status.SUCCEEDED:
            if self.payment.method == "CASH":
                if not self.execution_shift_id:
                    raise ValidationError("现金退款执行成功必须关联执行人本人收银班次。")
                shift = self.execution_shift
                if (shift.property_id != self.property_id
                        or shift.cashier_id != self.executed_by_id):
                    raise ValidationError("现金退款执行班次必须属于当前物业和实际执行人。")
            elif self.execution_shift_id:
                raise ValidationError("非现金退款不得伪造现金执行班次。")
        if self._state.adding:
            if self.legacy_workflow:
                raise ValidationError("legacy_workflow 仅允许历史数据迁移设置，新退款不得伪装成历史记录。")
            if (self.status != self.Status.PENDING or self.approved_by_id
                    or self.executed_by_id or self.executed_at or self.execution_reference
                    or self.execution_idempotency_key or self.execution_shift_id
                    or self.processed_at):
                raise ValidationError("退款必须从待审批申请开始，不能直接创建已审批/已执行记录。")
        else:
            previous = type(self).objects.filter(pk=self.pk).values(
                "payment_id", "refund_liability_id", "property_id", "requested_by_id", "amount",
                "currency", "reason", "idempotency_key", "provider_ref",
                "approval_record_id", "approved_by_id", "executed_by_id",
                "executed_at", "execution_reference", "execution_idempotency_key",
                "execution_shift_id", "processed_at", "status",
                "legacy_workflow").first()
            if previous:
                immutable_request = (
                    "payment_id", "refund_liability_id", "property_id", "requested_by_id", "amount",
                    "currency", "reason", "idempotency_key", "provider_ref",
                    "legacy_workflow")
                if any(previous[field] != getattr(self, field)
                       for field in immutable_request):
                    raise ValidationError("退款申请的原支付、物业、申请人、金额、原因和编号不可修改。")
                if previous["legacy_workflow"] != self.legacy_workflow:
                    raise ValidationError("历史退款流程标记由数据迁移维护，业务代码不得修改。")
                immutable_once_set = (
                    "approval_record_id", "approved_by_id", "executed_by_id",
                    "executed_at", "execution_reference",
                    "execution_idempotency_key", "execution_shift_id",
                    "processed_at")
                if previous["legacy_workflow"]:
                    historical_fields = immutable_once_set + ("status",)
                    if any(previous[field] != getattr(self, field)
                           for field in historical_fields):
                        raise ValidationError("历史退款记录只读，不能补写或改写审批/执行事实。")
                elif previous["status"] in (self.Status.SUCCEEDED, self.Status.REJECTED,
                                            self.Status.EXPIRED):
                    # 终态后所有审批/执行字段（包括空参考号、空班次）都冻结；
                    # 否则现金退款的空 execution_reference 可在事后被补写篡改。
                    terminal_fields = immutable_once_set + ("status",)
                    if any(previous[field] != getattr(self, field)
                           for field in terminal_fields):
                        raise ValidationError("已完成/已驳回退款的审批与执行事实不可修改。")
                else:
                    for field in immutable_once_set:
                        old_value = previous[field]
                        if old_value not in (None, "") and getattr(self, field) != old_value:
                            raise ValidationError("退款审批/执行事实不可覆盖或改写。")
                    allowed = {
                        self.Status.PENDING: {self.Status.PENDING, self.Status.APPROVED,
                                              self.Status.REJECTED, self.Status.EXPIRED},
                        self.Status.APPROVED: {self.Status.APPROVED, self.Status.SUCCEEDED},
                        self.Status.SUCCEEDED: {self.Status.SUCCEEDED},
                        self.Status.REJECTED: {self.Status.REJECTED},
                        self.Status.EXPIRED: {self.Status.EXPIRED},
                    }
                    if self.status not in allowed.get(previous["status"], {previous["status"]}):
                        raise ValidationError("退款状态不允许该迁移。")
        if not self.legacy_workflow:
            if self.approved_by_id and self.approved_by_id == self.requested_by_id:
                raise ValidationError("退款申请人与审批人必须是不同账号。")
            if self.executed_by_id and self.executed_by_id == self.requested_by_id:
                raise ValidationError("退款申请人与执行人必须是不同账号。")
            if (self.executed_by_id and self.approved_by_id
                    and self.executed_by_id == self.approved_by_id):
                raise ValidationError("退款审批人与执行人必须是不同账号。")
            if self.status == self.Status.PENDING and self.approved_by_id:
                raise ValidationError("待审批退款不得预先设置审批人。")
            if self.status in (self.Status.APPROVED, self.Status.REJECTED,
                               self.Status.SUCCEEDED) and not self.approved_by_id:
                raise ValidationError("已审批/驳回/执行退款必须记录审批人。")
            if self.status in (self.Status.APPROVED, self.Status.REJECTED,
                               self.Status.SUCCEEDED, self.Status.EXPIRED) and not self.approval_record_id:
                raise ValidationError("已审批/驳回/执行/过期退款必须关联审批记录。")
            if self.status == self.Status.EXPIRED and self.approved_by_id:
                raise ValidationError("已过期退款不得伪造审批人。")
            if self.status == self.Status.SUCCEEDED:
                if (not self.executed_by_id or not self.executed_at
                        or not self.execution_idempotency_key or not self.processed_at):
                    raise ValidationError("退款执行成功必须完整记录执行人、时间、处理时间和幂等键。")
            elif (self.executed_by_id or self.executed_at or self.execution_reference
                  or self.execution_idempotency_key or self.execution_shift_id):
                raise ValidationError("退款未执行成功前不得写入执行事实。")
            if self.status in (self.Status.PENDING, self.Status.APPROVED) and self.processed_at:
                raise ValidationError("待审批/已批准待执行退款不得预先写入处理完成时间。")
            if self.status in (self.Status.REJECTED, self.Status.EXPIRED) and not self.processed_at:
                raise ValidationError("已驳回/已过期退款必须记录处理时间。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("退款事实不可物理删除；必须保留申请、审批与执行轨迹。")

    def __str__(self):
        return f"{self.provider_ref} / {self.status}"


class RefundLiability(models.Model):
    """退房后待财务处理的退款负债；登记负债不等于申请或执行退款。"""

    objects = models.Manager.from_queryset(ImmutableFinancialFactQuerySet)()

    class Status(models.TextChoices):
        OPEN = "OPEN", "待处理"
        PARTIALLY_SETTLED = "PARTIALLY_SETTLED", "部分处理"
        SETTLED = "SETTLED", "已处理"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT,
        related_name="refund_liabilities", verbose_name="所属酒店")
    reservation = models.ForeignKey(
        "reservations.Reservation", on_delete=models.PROTECT,
        related_name="refund_liabilities", verbose_name="预订主单")
    folio = models.OneToOneField(
        Folio, on_delete=models.PROTECT, related_name="refund_liability",
        verbose_name="来源账单")
    source_payments = models.ManyToManyField(
        Payment, blank=True, related_name="candidate_refund_liabilities",
        verbose_name="候选原支付流水",
        help_text="仅记录可供财务核对的原支付候选，不代表系统已分配退款金额。")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="负债原始金额")
    remaining_amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="待处理余额")
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN,
        verbose_name="负债状态")
    idempotency_key = models.CharField(max_length=96, unique=True, verbose_name="幂等键")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_refund_liabilities", verbose_name="登记人")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="登记时间")
    settled_at = models.DateTimeField(null=True, blank=True, verbose_name="结清时间")

    class Meta:
        verbose_name = "待退款负债"
        verbose_name_plural = "待退款负债"
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name="ck_rfl_amt_pos"),
            models.CheckConstraint(
                check=Q(remaining_amount__gte=0) & Q(remaining_amount__lte=F("amount")),
                name="ck_rfl_rem_rng"),
            models.CheckConstraint(
                check=(Q(status="OPEN", remaining_amount=F("amount"), settled_at__isnull=True)
                       | Q(status="PARTIALLY_SETTLED", remaining_amount__gt=0,
                           remaining_amount__lt=F("amount"), settled_at__isnull=True)
                       | Q(status="SETTLED", remaining_amount=0, settled_at__isnull=False)),
                name="ck_rfl_status"),
        ]

    def save(self, *args, **kwargs):
        if self.folio_id:
            folio = self.folio
            if (folio.property_id != self.property_id
                    or folio.reservation_id != self.reservation_id
                    or self.reservation.property_id != self.property_id
                    or self.reservation.currency != self.currency):
                raise ValidationError("待退款负债、账单与预订必须属于同一物业、预订和币种。")
            if folio.currency != self.currency:
                raise ValidationError("待退款负债币种必须与来源账单一致。")
        if self.amount is None or self.amount <= 0:
            raise ValidationError("待退款负债金额必须大于零。")
        if (self.remaining_amount is None or self.remaining_amount < 0
                or self.remaining_amount > self.amount):
            raise ValidationError("待退款负债余额必须在零与原始金额之间。")
        expected = (
            self.Status.OPEN if self.remaining_amount == self.amount
            else self.Status.SETTLED if self.remaining_amount == 0
            else self.Status.PARTIALLY_SETTLED
        )
        if self.status != expected:
            raise ValidationError("待退款负债状态必须与剩余金额一致。")
        if (self.status == self.Status.SETTLED) != bool(self.settled_at):
            raise ValidationError("待退款负债结清时间必须与结清状态一致。")
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "property_id", "reservation_id", "folio_id", "amount",
                "remaining_amount", "status", "settled_at",
                "currency", "idempotency_key", "created_by_id").first()
            if previous:
                immutable = ("property_id", "reservation_id", "folio_id",
                             "amount", "currency", "idempotency_key", "created_by_id")
                if any(previous[field] != getattr(self, field) for field in immutable):
                    raise ValidationError("待退款负债来源、原始金额和登记事实不可修改。")
                if self.remaining_amount > previous["remaining_amount"]:
                    raise ValidationError("待退款负债余额只能因退款成功而减少，不得回增。")
                if previous["status"] == self.Status.SETTLED and (
                        self.status != self.Status.SETTLED
                        or self.settled_at != previous["settled_at"]):
                    raise ValidationError("已结清待退款负债不可重新打开或修改结清时间。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("待退款负债是财务追踪事实，禁止物理删除。")

    def __str__(self):
        return f"{self.folio_id} / {self.remaining_amount} {self.currency} / {self.status}"



def _validate_refund_liability_payment_candidates(sender, instance, action,
                                                   reverse, pk_set, **kwargs):
    """候选原支付只能属于负债相同的物业、预订、账单和币种。"""
    if action != "pre_add" or not pk_set:
        return
    if reverse:
        payments = [instance]
        liabilities = RefundLiability.objects.filter(pk__in=pk_set).values(
            "property_id", "reservation_id", "folio_id", "currency")
        payment = payments[0]
        for liability in liabilities:
            if (payment.property_id != liability["property_id"]
                    or payment.reservation_id != liability["reservation_id"]
                    or payment.folio_id != liability["folio_id"]
                    or payment.currency != liability["currency"]):
                raise ValidationError("候选原支付必须与待退款负债属于同一物业、预订、账单和币种。")
        return

    payments = Payment.objects.filter(pk__in=pk_set).values(
        "property_id", "reservation_id", "folio_id", "currency")
    for payment in payments:
        if (payment["property_id"] != instance.property_id
                or payment["reservation_id"] != instance.reservation_id
                or payment["folio_id"] != instance.folio_id
                or payment["currency"] != instance.currency):
            raise ValidationError("候选原支付必须与待退款负债属于同一物业、预订、账单和币种。")


m2m_changed.connect(
    _validate_refund_liability_payment_candidates,
    sender=RefundLiability.source_payments.through,
    dispatch_uid="billing.validate_refund_liability_payment_candidates",
)

def _current_refund_facts(**filters):
    """支付状态机校验读取退款事实必须基于“当前读”。

    MySQL REPEATABLE READ 下，事务内普通读使用事务首个一致读建立的旧快照；
    支付行锁等待期间其他事务可能已提交新的退款申请/执行。若用普通读校验
    支付状态迁移，会依据过期快照放行与事实不符的状态。因此事务内一律用
    SELECT ... FOR UPDATE 当前读（与 BillingService 的 Payment→Refund 全局
    锁顺序一致）；autocommit 下每条语句本身即当前读，无需加锁。
    """
    qs = Refund.objects.all()
    if transaction.get_connection().in_atomic_block:
        qs = qs.select_for_update()
    return qs.filter(**filters)


class CashierShift(models.Model):
    """现金班次对账（V1.1 M-10 P1）：前台现金收支按班次闭合，差异必须说明。"""

    objects = models.Manager.from_queryset(ImmutableFinancialFactQuerySet)()

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
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="closed_cashier_shifts", verbose_name="关班人")
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
    open_shift_key = models.CharField(
        max_length=96, null=True, blank=True, unique=True, editable=False,
        verbose_name="开放班次唯一键")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "现金班次"
        verbose_name_plural = "现金班次"
        ordering = ("-opened_at",)
        constraints = [
            models.CheckConstraint(check=Q(opening_cash__gte=0),
                                   name="ck_cashier_shift_opening_cash_nonnegative"),
            models.CheckConstraint(
                check=Q(expected_cash__isnull=True) | Q(expected_cash__gte=0),
                name="ck_cashier_shift_expected_cash_nonnegative"),
            models.CheckConstraint(
                check=Q(actual_cash__isnull=True) | Q(actual_cash__gte=0),
                name="ck_cashier_shift_actual_cash_nonnegative"),
        ]
        permissions = (
            ("cashier_shift_manage", "可以开立和关闭收银班次"),
            ("cash_movement_manage", "可以登记班次现金收入与支出"),
        )

    def save(self, *args, **kwargs):
        if self.opening_cash is None or self.opening_cash < 0:
            raise ValidationError("开班备用金不能为负数。")
        if self._state.adding:
            if self.status != self.ShiftStatus.OPEN:
                raise ValidationError("收银班次必须从 OPEN 状态创建。")
            if any(value is not None for value in (
                    self.closed_at, self.closed_by_id, self.expected_cash,
                    self.actual_cash, self.variance)):
                raise ValidationError("新班次不得预先写入关班事实。")
        else:
            previous = type(self).objects.filter(pk=self.pk).values(
                "property_id", "cashier_id", "opened_at", "opening_cash",
                "status", "closed_at", "closed_by_id", "expected_cash",
                "actual_cash", "variance", "variance_reason").first()
            if previous:
                immutable = ("property_id", "cashier_id", "opened_at", "opening_cash")
                if any(previous[field] != getattr(self, field) for field in immutable):
                    raise ValidationError("班次物业、收银员、开班时间和备用金不可修改。")
                if previous["status"] == self.ShiftStatus.OPEN:
                    if self.status not in (self.ShiftStatus.OPEN, self.ShiftStatus.CLOSED):
                        raise ValidationError("班次只允许从 OPEN 关账为 CLOSED。")
                    if self.status == self.ShiftStatus.OPEN:
                        if any(value is not None for value in (
                                self.closed_at, self.closed_by_id, self.expected_cash,
                                self.actual_cash, self.variance)):
                            raise ValidationError("OPEN 班次不得提前写入关班事实。")
                    if self.status == self.ShiftStatus.CLOSED:
                        if (not self.closed_at or not self.closed_by_id
                                or self.expected_cash is None
                                or self.actual_cash is None or self.variance is None):
                            raise ValidationError("关班必须完整记录关班人、时间、应有现金、实盘现金和差异。")
                        if self.expected_cash < 0 or self.actual_cash < 0:
                            raise ValidationError("关班应有现金和实盘现金不能为负数。")
                        if self.variance != self.actual_cash - self.expected_cash:
                            raise ValidationError("关班差异必须等于实盘现金减去应有现金。")
                        if self.variance != 0 and not (self.variance_reason or "").strip():
                            raise ValidationError("现金差异非零必须填写差异说明。")
                else:
                    closure_fields = (
                        "status", "closed_at", "closed_by_id", "expected_cash",
                        "actual_cash", "variance", "variance_reason")
                    if any(previous[field] != getattr(self, field) for field in closure_fields):
                        raise ValidationError("已关班的财务事实不可修改或重新打开。")
        desired_key = (
            f"{self.property_id}:{self.cashier_id}"
            if self.property_id and self.cashier_id and self.status == self.ShiftStatus.OPEN
            else None
        )
        if self.open_shift_key != desired_key:
            self.open_shift_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"open_shift_key"}
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("收银班次是财务事实，禁止物理删除。")

    def __str__(self):
        return f"{self.property_id}/{self.cashier_id} {self.status}"


class CashierShiftMovement(models.Model):
    """班次现金增减事实；只追加，作为备用金调整/现金支出的审计来源。"""

    objects = models.Manager.from_queryset(ImmutableFinancialFactQuerySet)()

    class Direction(models.TextChoices):
        IN = "IN", "现金收入/补入"
        OUT = "OUT", "现金支出/取出"

    shift = models.ForeignKey(
        CashierShift, on_delete=models.PROTECT, related_name="cash_movements",
        verbose_name="收银班次")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=240)
    idempotency_key = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="cashier_shift_movements", verbose_name="登记人")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("shift", "idempotency_key"),
                name="uq_shift_movement_idempotency"),
            models.CheckConstraint(check=Q(amount__gt=0),
                                   name="ck_shift_movement_amount_positive"),
            models.CheckConstraint(check=Q(direction__in=("IN", "OUT")),
                                   name="ck_shift_movement_direction"),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            shift = CashierShift.objects.filter(pk=self.shift_id).values(
                "status", "cashier_id").first()
            if shift is None:
                raise ValidationError("现金变动必须关联有效收银班次。")
            if shift["status"] != CashierShift.ShiftStatus.OPEN:
                raise ValidationError("已关班的收银班次不得新增现金变动。")
            if shift["cashier_id"] != self.created_by_id:
                raise ValidationError("现金变动登记人必须是该班次收银员。")
        elif self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("班次现金变动是不可变财务事实，禁止修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("班次现金变动是不可变财务事实，禁止删除。")


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
