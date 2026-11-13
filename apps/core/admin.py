from django.contrib import admin

from .models import Property


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "timezone", "currency", "status")
    list_filter = ("status", "currency")
    search_fields = ("code", "name", "address")
    readonly_fields = ("created_at", "updated_at")


from .models import AuditEvent, LegacyRecordLink


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """审计事件只读检索（V1.1 M-07 §2.7）；禁止修改/删除。"""
    list_display = ("id", "action", "entity_type", "entity_id",
                    "actor", "property", "reason", "created_at")
    list_filter = ("action", "property")
    search_fields = ("entity_id", "reason", "actor__username")
    readonly_fields = tuple(f.name for f in AuditEvent._meta.fields)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LegacyRecordLink)
class LegacyRecordLinkAdmin(admin.ModelAdmin):
    """历史映射核验工作台（V1.1 §4.2）；只读查看，修改走映射服务。"""
    list_display = ("id", "source_model", "source_pk", "target_model",
                    "target_pk", "mapping_status", "reviewed_by")
    list_filter = ("source_model", "mapping_status", "property")
    search_fields = ("source_pk", "target_pk", "review_note")
    readonly_fields = ("created_at", "updated_at")

    def has_delete_permission(self, request, obj=None):
        return False


from .models import BreakGlassRecord


@admin.register(BreakGlassRecord)
class BreakGlassRecordAdmin(admin.ModelAdmin):
    """Break-glass 安全记录仅供审计查看，后台不得新增、修改或删除。"""
    list_display = (
        "id", "property", "business_date", "requested_by", "reason",
        "status", "expires_at", "used_at", "reviewed_by", "review_decision",
    )
    list_filter = ("property", "status", "review_decision")
    search_fields = ("reason", "review_reason", "requested_by__username")
    readonly_fields = tuple(f.name for f in BreakGlassRecord._meta.fields)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
