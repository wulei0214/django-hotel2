from django.contrib import admin

from .legacy_archive_query import mask_phone
from .models import (
    ApprovalRecord,
    Folio,
    LedgerEntry,
    LegacyAccommodationArchive,
    LegacyArchiveRun,
    LegacyFinanceRecordArchive,
    LegacyFolioArchive,
    LegacyNightAuditArchive,
    LegacyOrderReservationMap,
    Payment,
    Refund,
    RefundLiability,
)


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0
    can_delete = False
    readonly_fields = tuple(field.name for field in LedgerEntry._meta.fields)
    fields = readonly_fields


@admin.register(Folio)
class FolioAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "property",
        "reservation",
        "guest",
        "status",
        "currency",
        "opened_at",
        "closed_at",
    )
    list_filter = ("property", "status", "currency")
    search_fields = ("reservation__booking_no", "guest__full_name", "guest__phone_hash")
    readonly_fields = tuple(field.name for field in Folio._meta.fields)
    inlines = (LedgerEntryInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "property",
        "folio",
        "entry_type",
        "amount",
        "business_date",
        "posted_by",
    )
    list_filter = ("property", "entry_type", "business_date")
    search_fields = ("reference", "folio__reservation__booking_no", "description")
    readonly_fields = tuple(field.name for field in LedgerEntry._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "provider_ref",
        "property",
        "reservation",
        "amount",
        "currency",
        "status",
        "paid_at",
    )
    list_filter = ("property", "provider", "status")
    search_fields = ("provider_ref", "reservation__booking_no")
    readonly_fields = tuple(field.name for field in Payment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# 支付流水的只读展示对后台维护者更友好：默认按创建时间倒序已由 Meta 保证


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    """退款记录只读检索（V1.1 §4.4）；审批走 lifecycle service。"""

    list_display = (
        "id",
        "property",
        "payment",
        "refund_liability",
        "amount",
        "status",
        "requested_by",
        "approved_by",
        "created_at",
    )
    list_filter = ("status", "property")
    search_fields = ("provider_ref", "reason")
    readonly_fields = tuple(f.name for f in Refund._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ApprovalRecord)
class ApprovalRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "property",
        "action_code",
        "entity_type",
        "entity_id",
        "requested_by",
        "approved_by",
        "status",
        "created_at",
    )
    list_filter = ("property", "action_code", "status")
    search_fields = ("entity_type", "entity_id", "reason", "requested_by__username")
    readonly_fields = tuple(field.name for field in ApprovalRecord._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RefundLiability)
class RefundLiabilityAdmin(admin.ModelAdmin):
    """待退款负债只读查询；余额变更只能由退款执行服务维护。"""

    list_display = (
        "id",
        "property",
        "reservation",
        "folio",
        "amount",
        "remaining_amount",
        "currency",
        "status",
        "created_at",
    )
    list_filter = ("property", "status", "currency")
    search_fields = ("reservation__booking_no", "idempotency_key")
    readonly_fields = tuple(field.name for field in RefundLiability._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# 历史归档与关系映射：平台超管的只读通道（归档门禁 §4.1 第一类主体）
# 写入只能经 archive_legacy_finance / confirm_legacy_order_map；后台不提供增删改。
# ---------------------------------------------------------------------------


class ReadOnlyLegacyAdmin(admin.ModelAdmin):
    """归档/映射事实只读展示：拒绝新增、修改、删除（模型层守卫为第二道防线）。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LegacyFolioArchive)
class LegacyFolioArchiveAdmin(ReadOnlyLegacyAdmin):
    list_display = (
        "id",
        "archive_batch",
        "order_id",
        "entry_type",
        "amount_text",
        "date_text",
        "archived_at",
    )
    list_filter = ("archive_batch", "entry_type")
    search_fields = ("subject", "handler")
    readonly_fields = tuple(field.name for field in LegacyFolioArchive._meta.fields)


@admin.register(LegacyFinanceRecordArchive)
class LegacyFinanceRecordArchiveAdmin(ReadOnlyLegacyAdmin):
    list_display = (
        "id",
        "archive_batch",
        "record_type",
        "amount_text",
        "related_order",
        "date_text",
        "archived_at",
    )
    list_filter = ("archive_batch", "record_type")
    search_fields = ("subject", "handler", "related_order")
    readonly_fields = tuple(field.name for field in LegacyFinanceRecordArchive._meta.fields)


@admin.register(LegacyNightAuditArchive)
class LegacyNightAuditArchiveAdmin(ReadOnlyLegacyAdmin):
    list_display = (
        "id",
        "archive_batch",
        "business_date_text",
        "in_house_count",
        "rollover_total_text",
        "operator",
        "archived_at",
    )
    list_filter = ("archive_batch",)
    search_fields = ("operator",)
    readonly_fields = tuple(field.name for field in LegacyNightAuditArchive._meta.fields)


@admin.register(LegacyAccommodationArchive)
class LegacyAccommodationArchiveAdmin(ReadOnlyLegacyAdmin):
    """含历史联系电话——后台一律只显示脱敏值（§4.1：完整值不出现在任何输出）。"""

    list_display = (
        "id",
        "archive_batch",
        "client_id",
        "check_in_text",
        "status",
        "contact_phone_masked",
        "received_amount_text",
        "deposit_text",
        "archived_at",
    )
    list_filter = ("archive_batch", "status")
    search_fields = ("reversed_by",)
    readonly_fields = tuple(
        field.name
        for field in LegacyAccommodationArchive._meta.fields
        if field.name != "contact_phone"
    ) + ("contact_phone_masked",)

    @admin.display(description="旧联系电话（脱敏）")
    def contact_phone_masked(self, obj):
        return mask_phone(obj.contact_phone)


@admin.register(LegacyArchiveRun)
class LegacyArchiveRunAdmin(ReadOnlyLegacyAdmin):
    """归档批次元数据（整表摘要/行数/min-max 主键）——D3 收口与 D4 演练的比对基准。"""

    list_display = (
        "batch_uid",
        "source_table",
        "archive_table",
        "algorithm_version",
        "row_count",
        "min_pk",
        "max_pk",
        "computed_at",
        "operator",
    )
    list_filter = ("batch_uid", "source_table", "algorithm_version")
    search_fields = ("operator", "table_digest")
    readonly_fields = tuple(field.name for field in LegacyArchiveRun._meta.fields)


@admin.register(LegacyOrderReservationMap)
class LegacyOrderReservationMapAdmin(ReadOnlyLegacyAdmin):
    """旧订单-新预订关系（§1.5.2 追加式版本链）；改映射只能追加 supersedes 新行。"""

    list_display = (
        "id",
        "legacy_order_id",
        "reservation_id",
        "property",
        "relation_type",
        "confirmed_by",
        "confirmed_at",
        "supersedes",
    )
    list_filter = ("property", "relation_type")
    search_fields = ("match_basis", "legacy_order_id", "reservation__booking_no")
    readonly_fields = tuple(field.name for field in LegacyOrderReservationMap._meta.fields)
