from django.contrib import admin

from .models import Guest


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ("id", "property", "full_name", "phone", "created_at")
    list_filter = ("property",)
    search_fields = ("full_name", "phone", "phone_normalized")
    readonly_fields = ("created_at", "updated_at")
