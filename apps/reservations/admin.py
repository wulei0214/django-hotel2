from django.contrib import admin

from .models import Reservation, ReservationRoom, Stay


class ReservationRoomInline(admin.TabularInline):
    model = ReservationRoom
    extra = 0
    can_delete = False
    readonly_fields = ("property", "room_type", "rate_plan", "assigned_room", "check_in", "check_out", "nightly_rates", "amount")


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("booking_no", "property", "guest", "check_in", "check_out", "room_count", "total_amount", "status", "expires_at")
    list_filter = ("property", "status", "source")
    search_fields = ("booking_no", "guest__full_name", "guest__phone")
    readonly_fields = ("booking_no", "created_at", "updated_at", "request_fingerprint")
    inlines = (ReservationRoomInline,)


@admin.register(Stay)
class StayAdmin(admin.ModelAdmin):
    list_display = ("property", "room", "reservation_room", "actual_check_in", "actual_check_out", "status")
    list_filter = ("property", "status")
    search_fields = ("room__number", "reservation_room__reservation__booking_no")
    readonly_fields = ("created_at", "updated_at")
