from django.contrib import admin

from .models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment


class IdentityLifecycleReadOnlyAdmin(admin.ModelAdmin):
    """员工身份关系通过 EmployeeLifecycleService 变更，Admin 仅供审计查看。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StaffProfile)
class StaffProfileAdmin(IdentityLifecycleReadOnlyAdmin):
    list_display = ("id", "full_name", "user", "employment_status")
    list_filter = ("employment_status",)
    search_fields = ("full_name", "phone", "user__username")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PropertyStaffMembership)
class PropertyStaffMembershipAdmin(IdentityLifecycleReadOnlyAdmin):
    list_display = ("property", "employee_no", "staff_profile", "position_code", "status")
    list_filter = ("property", "position_code", "status")
    search_fields = ("employee_no", "staff_profile__full_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(StaffRoleAssignment)
class StaffRoleAssignmentAdmin(IdentityLifecycleReadOnlyAdmin):
    list_display = ("membership", "group", "starts_at", "ends_at", "granted_by")
    list_filter = ("group",)
    search_fields = ("membership__employee_no", "membership__staff_profile__full_name")
    readonly_fields = ("created_at",)
