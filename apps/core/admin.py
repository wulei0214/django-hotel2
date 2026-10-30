from django.contrib import admin

from .models import Property


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "timezone", "currency", "status")
    list_filter = ("status", "currency")
    search_fields = ("code", "name", "address")
    readonly_fields = ("created_at", "updated_at")
