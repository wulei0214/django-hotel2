from django.contrib import admin

from .models import ApprovalRecord, Folio, LedgerEntry, Payment, Refund, RefundLiability


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
