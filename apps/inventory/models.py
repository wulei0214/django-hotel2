from django.db import models
from django.db.models import F, Q
from django.utils.functional import cached_property


class RoomType(models.Model):
    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="room_types"
    )
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=80)
    capacity = models.PositiveSmallIntegerField(default=2)
    bed_type = models.CharField(max_length=40, blank=True)
    amenities = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "code"), name="uq_room_type_property_code"
            ),
            models.CheckConstraint(check=Q(capacity__gte=1), name="ck_room_type_capacity_positive"),
        ]
        verbose_name = "房型"
        verbose_name_plural = "房型"

    def __str__(self):
        return f"{self.code} - {self.name}"


class Room(models.Model):
    class OccupancyStatus(models.TextChoices):
        VACANT = "VACANT", "空闲"
        OCCUPIED = "OCCUPIED", "在住"

    class HousekeepingStatus(models.TextChoices):
        CLEAN = "CLEAN", "净房"
        DIRTY = "DIRTY", "脏房"
        CLEANING = "CLEANING", "清洁中"
        INSPECTING = "INSPECTING", "待检查"

    class MaintenanceStatus(models.TextChoices):
        NORMAL = "NORMAL", "正常"
        OUT_OF_ORDER = "OUT_OF_ORDER", "维修停用"
        OUT_OF_SERVICE = "OUT_OF_SERVICE", "停售"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="rooms"
    )
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, related_name="rooms"
    )
    number = models.CharField(max_length=16)
    floor = models.CharField(max_length=16, blank=True)
    occupancy_status = models.CharField(
        max_length=16, choices=OccupancyStatus.choices, default=OccupancyStatus.VACANT
    )
    housekeeping_status = models.CharField(
        max_length=16,
        choices=HousekeepingStatus.choices,
        default=HousekeepingStatus.CLEAN,
    )
    maintenance_status = models.CharField(
        max_length=20,
        choices=MaintenanceStatus.choices,
        default=MaintenanceStatus.NORMAL,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("number",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "number"), name="uq_room_property_number"
            )
        ]
        indexes = [
            models.Index(fields=("property", "occupancy_status", "housekeeping_status"), name="inventory_room_propert_09dc8a_idx"),
        ]
        verbose_name = "物理客房"
        verbose_name_plural = "物理客房"

    def __str__(self):
        return self.number

    @cached_property
    def is_sellable_now(self):
        return (
            self.is_active
            and self.occupancy_status == self.OccupancyStatus.VACANT
            and self.housekeeping_status == self.HousekeepingStatus.CLEAN
            and self.maintenance_status == self.MaintenanceStatus.NORMAL
        )


class RatePlan(models.Model):
    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="rate_plans"
    )
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=80)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    cancellation_policy = models.TextField(blank=True)
    deposit_policy = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "code"), name="uq_rate_plan_property_code"
            ),
            models.CheckConstraint(
                check=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="ck_rate_plan_valid_dates",
            ),
        ]
        verbose_name = "房价计划"
        verbose_name_plural = "房价计划"

    def __str__(self):
        return f"{self.code} - {self.name}"


class DailyRate(models.Model):
    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="daily_rates"
    )
    rate_plan = models.ForeignKey(
        RatePlan, on_delete=models.PROTECT, related_name="daily_rates"
    )
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, related_name="daily_rates"
    )
    stay_date = models.DateField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="CNY")
    closed = models.BooleanField(default=False)
    min_stay = models.PositiveSmallIntegerField(default=1)
    max_stay = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("stay_date",)
        constraints = [
            models.UniqueConstraint(
                fields=("rate_plan", "room_type", "stay_date"),
                name="uq_daily_rate_plan_room_date",
            ),
            models.CheckConstraint(check=Q(price__gte=0), name="ck_daily_rate_nonnegative"),
        ]
        indexes = [
            models.Index(fields=("property", "room_type", "stay_date"), name="inventory_daily_propert_5fce20_idx"),
        ]
        verbose_name = "每日房价"
        verbose_name_plural = "每日房价"

    def __str__(self):
        return f"{self.stay_date} {self.room_type} {self.price}"


class RoomTypeInventory(models.Model):
    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="room_type_inventory"
    )
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, related_name="daily_inventory"
    )
    stay_date = models.DateField()
    total_rooms = models.PositiveIntegerField(default=0)
    held = models.PositiveIntegerField(default=0)
    sold = models.PositiveIntegerField(default=0)
    out_of_service = models.PositiveIntegerField(default=0)
    stop_sell = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("stay_date",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "room_type", "stay_date"),
                name="uq_inventory_property_type_date",
            ),
            models.CheckConstraint(
                check=Q(total_rooms__gte=F("held") + F("sold") + F("out_of_service")),
                name="ck_inventory_capacity_not_exceeded",
            ),
        ]
        indexes = [
            models.Index(fields=("property", "room_type", "stay_date"), name="inventory_roomt_propert_a6a02f_idx"),
        ]
        verbose_name = "房型每日库存"
        verbose_name_plural = "房型每日库存"

    @cached_property
    def available_count(self):
        return self.total_rooms - self.held - self.sold - self.out_of_service

    def __str__(self):
        return f"{self.room_type} / {self.stay_date} / 可售 {self.available_count}"
