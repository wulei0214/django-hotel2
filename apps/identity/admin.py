from django.contrib import admin

from .models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "full_name", "user", "employment_status")
    list_filter = ("employment_status",)
    search_fields = ("full_name", "phone", "user__username")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PropertyStaffMembership)
class PropertyStaffMembershipAdmin(admin.ModelAdmin):
    list_display = ("property", "employee_no", "staff_profile", "position_code", "status")
    list_filter = ("property", "position_code", "status")
    search_fields = ("employee_no", "staff_profile__full_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(StaffRoleAssignment)
class StaffRoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("membership", "group", "starts_at", "ends_at", "granted_by")
    list_filter = ("group",)
    search_fields = ("membership__employee_no", "membership__staff_profile__full_name")
    readonly_fields = ("created_at",)
