from django.contrib import admin

from .models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory


@admin.register(RoomType)
class RoomTypeAdmin(admin.ModelAdmin):
    list_display = ("property", "code", "name", "capacity", "is_active")
    list_filter = ("property", "is_active")
    search_fields = ("code", "name")


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("property", "number", "room_type", "occupancy_status", "housekeeping_status", "maintenance_status")
    list_filter = ("property", "room_type", "occupancy_status", "housekeeping_status", "maintenance_status")
    search_fields = ("number",)


@admin.register(RatePlan)
class RatePlanAdmin(admin.ModelAdmin):
    list_display = ("property", "code", "name", "valid_from", "valid_to", "is_active")
    list_filter = ("property", "is_active")
    search_fields = ("code", "name")


@admin.register(DailyRate)
class DailyRateAdmin(admin.ModelAdmin):
    list_display = ("property", "rate_plan", "room_type", "stay_date", "price", "currency", "closed")
    list_filter = ("property", "rate_plan", "room_type", "closed")
    date_hierarchy = "stay_date"


@admin.register(RoomTypeInventory)
class RoomTypeInventoryAdmin(admin.ModelAdmin):
    list_display = ("property", "room_type", "stay_date", "total_rooms", "held", "sold", "out_of_service", "available_count", "stop_sell")
    list_filter = ("property", "room_type", "stop_sell")
    date_hierarchy = "stay_date"
    readonly_fields = ("version", "updated_at")

# 模型 cached_property 的列头默认显示英文属性名，这里给中文
RoomTypeInventory.available_count.short_description = "可售房量"
