from django.contrib import admin


class ReadOnlyAdminMixin:
    """P0 安全护栏：Admin 对 PMS2 预订/入住模型默认只读（V1.1 §12.2）。

    状态、日期、房间、金额等禁止 Admin 直改；业务操作走前台工作台服务。
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from .models import Reservation, ReservationRoom, Stay


class ReservationRoomInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = ReservationRoom
    extra = 0
    can_delete = False
    readonly_fields = ("property", "room_type", "rate_plan", "assigned_room", "check_in", "check_out", "nightly_rates", "amount")


@admin.register(Reservation)
class ReservationAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("booking_no", "property", "guest", "check_in", "check_out", "room_count", "total_amount", "status", "expires_at")
    list_filter = ("property", "status", "source")
    search_fields = ("booking_no", "guest__full_name", "guest__phone_hash")
    readonly_fields = ("booking_no", "created_at", "updated_at", "request_fingerprint")
    inlines = (ReservationRoomInline,)


@admin.register(Stay)
class StayAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("property", "room", "reservation_room", "actual_check_in", "actual_check_out", "status")
    list_filter = ("property", "status")
    search_fields = ("room__number", "reservation_room__reservation__booking_no")
    readonly_fields = ("created_at", "updated_at")
