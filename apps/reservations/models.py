from django.db import models
from django.db.models import F, Q


class Reservation(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "待支付/待确认"
        CONFIRMED = "CONFIRMED", "已确认"
        CANCELLED = "CANCELLED", "已取消"
        EXPIRED = "EXPIRED", "已过期"
        CHECKED_IN = "CHECKED_IN", "已入住"
        CHECKED_OUT = "CHECKED_OUT", "已退房"
        NO_SHOW = "NO_SHOW", "未到店"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="reservations"
    )
    booking_no = models.CharField(max_length=32, unique=True)
    guest = models.ForeignKey(
        "guests.Guest", on_delete=models.PROTECT, related_name="reservations"
    )
    source = models.CharField(max_length=24, default="WEBSITE")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    check_in = models.DateField()
    check_out = models.DateField()
    room_count = models.PositiveSmallIntegerField(default=1)
    adult_count = models.PositiveSmallIntegerField(default=1)
    child_count = models.PositiveSmallIntegerField(default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="CNY")
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    request_fingerprint = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "idempotency_key"),
                name="uq_reservation_property_idempotency",
            ),
            models.CheckConstraint(
                check=Q(check_out__gt=F("check_in")),
                name="ck_reservation_checkout_after_checkin",
            ),
            models.CheckConstraint(check=Q(room_count__gte=1), name="ck_reservation_rooms_positive"),
            models.CheckConstraint(check=Q(adult_count__gte=1), name="ck_reservation_adults_positive"),
        ]
        indexes = [
            models.Index(fields=("property", "status", "check_in"), name="idx_res_prop_status_date"),
        ]
        verbose_name = "预订主单"
        verbose_name_plural = "预订主单"

    def __str__(self):
        return self.booking_no


class ReservationRoom(models.Model):
    """一条记录代表预订中的一间房，支持一张预订单多房型/多房间。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="reservation_rooms"
    )
    reservation = models.ForeignKey(
        Reservation, on_delete=models.PROTECT, related_name="reservation_rooms"
    )
    room_type = models.ForeignKey(
        "inventory.RoomType", on_delete=models.PROTECT, related_name="reservation_rooms"
    )
    rate_plan = models.ForeignKey(
        "inventory.RatePlan", on_delete=models.PROTECT, related_name="reservation_rooms"
    )
    assigned_room = models.ForeignKey(
        "inventory.Room",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reservation_rooms",
    )
    check_in = models.DateField()
    check_out = models.DateField()
    nightly_rates = models.JSONField(default=list)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(
                check=Q(check_out__gt=F("check_in")),
                name="ck_reservation_room_checkout_after_checkin",
            ),
            models.CheckConstraint(check=Q(amount__gte=0), name="ck_reservation_room_amount_nonnegative"),
        ]
        verbose_name = "预订房间明细"
        verbose_name_plural = "预订房间明细"

    def __str__(self):
        return f"{self.reservation.booking_no} / {self.room_type.name}"


class Stay(models.Model):
    class Status(models.TextChoices):
        IN_HOUSE = "IN_HOUSE", "在住"
        CHECKED_OUT = "CHECKED_OUT", "已退房"
        VOIDED = "VOIDED", "已作废"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="stays"
    )
    reservation_room = models.OneToOneField(
        ReservationRoom, on_delete=models.PROTECT, related_name="stay"
    )
    room = models.ForeignKey(
        "inventory.Room", on_delete=models.PROTECT, related_name="stays"
    )
    actual_check_in = models.DateTimeField(null=True, blank=True)
    actual_check_out = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IN_HOUSE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("property", "status"), name="idx_stay_prop_status"),
        ]
        verbose_name = "实际入住记录"
        verbose_name_plural = "实际入住记录"

    def __str__(self):
        return f"{self.reservation_room.reservation.booking_no} / {self.room.number}"
