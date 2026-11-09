from django.contrib import admin

from django.contrib import admin

from .models import Guest


class ReadOnlyAdminMixin:
    """P0 安全护栏：Admin 对 PMS2 模型默认只读（V1.1 §12.2）。

    业务写操作必须走领域服务/API；Admin 仅保留检索和审计查看能力。
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Guest)
class GuestAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "property", "full_name", "phone", "created_at")
    list_filter = ("property",)
    search_fields = ("full_name", "phone", "phone_normalized")
    readonly_fields = ("created_at", "updated_at")
