from django.conf import settings
from django.db import models
from django.db.models import F, Q


class Reservation(models.Model):
    """预订主单：一张预订单对应一位住客、一到多间房（明细见 ReservationRoom）。"""

    class Status(models.TextChoices):
        PENDING = "PENDING", "待支付/待确认"
        CONFIRMED = "CONFIRMED", "已确认"
        CANCELLED = "CANCELLED", "已取消"
        EXPIRED = "EXPIRED", "已过期"
        CHECKED_IN = "CHECKED_IN", "已入住"
        CHECKED_OUT = "CHECKED_OUT", "已退房"
        NO_SHOW = "NO_SHOW", "未到店"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="reservations",
        verbose_name="所属酒店",
    )
    booking_no = models.CharField(
        max_length=32, unique=True, verbose_name="预订编号",
        help_text="全局唯一；客人凭编号+手机号查询/支付",
    )
    guest = models.ForeignKey(
        "guests.Guest", on_delete=models.PROTECT, related_name="reservations",
        verbose_name="入住客人",
    )
    source = models.CharField(
        max_length=24, default="WEBSITE", verbose_name="预订来源",
        help_text="如 WEBSITE（官网）/ WALK_IN（散客直入）/ OTA",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
        verbose_name="订单状态",
    )
    check_in = models.DateField(verbose_name="入住日期")
    check_out = models.DateField(
        verbose_name="离店日期",
        help_text="必须晚于入住日期",
    )
    room_count = models.PositiveSmallIntegerField(default=1, verbose_name="房间数")
    adult_count = models.PositiveSmallIntegerField(default=1, verbose_name="成人数")
    child_count = models.PositiveSmallIntegerField(default=0, verbose_name="儿童数")
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name="订单总额",
        help_text="每晚房价 × 间数 × 晚数 汇总",
    )
    deposit_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name="押金金额",
    )
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    idempotency_key = models.CharField(
        max_length=128, null=True, blank=True, verbose_name="幂等键",
        help_text="创建预订时由调用方传入；同酒店内唯一，重放返回原单",
    )
    request_fingerprint = models.CharField(
        max_length=64, blank=True, verbose_name="请求指纹",
        help_text="幂等键相同但内容不同时用它识别冲突",
    )
    expires_at = models.DateTimeField(
        null=True, blank=True, verbose_name="支付截止时间",
        help_text="超时未支付自动过期并释放冻结库存",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "预订主单"
        verbose_name_plural = "预订主单"
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

    def __str__(self):
        return self.booking_no


class ReservationRoom(models.Model):
    """一条记录代表预订中的一间房，支持一张预订单多房型/多房间。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="reservation_rooms",
        verbose_name="所属酒店",
    )
    reservation = models.ForeignKey(
        Reservation, on_delete=models.PROTECT, related_name="reservation_rooms",
        verbose_name="预订主单",
    )
    room_type = models.ForeignKey(
        "inventory.RoomType", on_delete=models.PROTECT, related_name="reservation_rooms",
        verbose_name="房型",
    )
    rate_plan = models.ForeignKey(
        "inventory.RatePlan", on_delete=models.PROTECT, related_name="reservation_rooms",
        verbose_name="房价计划",
    )
    assigned_room = models.ForeignKey(
        "inventory.Room",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reservation_rooms",
        verbose_name="排定房间",
        help_text="入住办理时分配；未排房为空",
    )
    check_in = models.DateField(verbose_name="入住日期")
    check_out = models.DateField(verbose_name="离店日期")
    nightly_rates = models.JSONField(
        default=list, verbose_name="每晚房价明细",
        help_text="JSON 数组，按晚记录 [日期, 金额]",
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="该间房小计",
        help_text="不得为负数",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "预订房间明细"
        verbose_name_plural = "预订房间明细"
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(
                check=Q(check_out__gt=F("check_in")),
                name="ck_reservation_room_checkout_after_checkin",
            ),
            models.CheckConstraint(check=Q(amount__gte=0), name="ck_reservation_room_amount_nonnegative"),
        ]

    def __str__(self):
        return f"{self.reservation.booking_no} / {self.room_type.name}"


class Stay(models.Model):
    """实际入住记录：办理入住后由预订房间明细生成，一明细一在住。"""

    class Status(models.TextChoices):
        IN_HOUSE = "IN_HOUSE", "在住"
        CHECKED_OUT = "CHECKED_OUT", "已退房"
        VOIDED = "VOIDED", "已作废"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="stays",
        verbose_name="所属酒店",
    )
    reservation_room = models.OneToOneField(
        ReservationRoom, on_delete=models.PROTECT, related_name="stay",
        verbose_name="预订房间明细",
    )
    room = models.ForeignKey(
        "inventory.Room", on_delete=models.PROTECT, related_name="stays",
        verbose_name="实际入住房间",
    )
    actual_check_in = models.DateTimeField(null=True, blank=True, verbose_name="实际入住时间")
    actual_check_out = models.DateTimeField(null=True, blank=True, verbose_name="实际离店时间")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.IN_HOUSE,
        verbose_name="在住状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "实际入住记录"
        verbose_name_plural = "实际入住记录"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("property", "status"), name="idx_stay_prop_status"),
        ]

    def __str__(self):
        return f"{self.reservation_room.reservation.booking_no} / {self.room.number}"



class StayRoomSegment(models.Model):
    """在住期间的实际房间区间；Stay.room 始终指向当前房间。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="stay_room_segments",
        verbose_name="所属酒店",
    )
    stay = models.ForeignKey(
        "reservations.Stay", on_delete=models.PROTECT, related_name="room_segments",
        verbose_name="实际入住",
    )
    room = models.ForeignKey(
        "inventory.Room", on_delete=models.PROTECT, related_name="stay_room_segments",
        verbose_name="实际房间",
    )
    started_at = models.DateTimeField(verbose_name="入住该房间时间")
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name="离开该房间时间")
    move_reason = models.CharField(max_length=240, blank=True, verbose_name="变更原因")
    moved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="stay_room_segments_moved", verbose_name="操作人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "入住换房历史"
        verbose_name_plural = "入住换房历史"
        ordering = ("started_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("stay",),
                condition=models.Q(ended_at__isnull=True),
                name="uq_stay_one_active_room_segment",
            ),
            models.CheckConstraint(
                check=models.Q(ended_at__isnull=True) | models.Q(ended_at__gt=models.F("started_at")),
                name="ck_stay_room_segment_time_order",
            ),
        ]

    def __str__(self):
        return f"{self.stay_id} / {self.room.number}"


class NoShowRecord(models.Model):
    """按预订房间明细记录未到店，可支持多房订单中的部分 No-Show。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="no_show_records",
        verbose_name="所属酒店",
    )
    reservation_room = models.OneToOneField(
        "reservations.ReservationRoom", on_delete=models.PROTECT,
        related_name="no_show_record", verbose_name="预订房间明细",
    )
    reason = models.CharField(max_length=240, verbose_name="未到店处理原因")
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="processed_no_show_records", verbose_name="处理人",
    )
    processed_at = models.DateTimeField(auto_now_add=True, verbose_name="处理时间")
    inventory_released_from = models.DateField(null=True, blank=True, verbose_name="释放库存起始日")
    released_nights = models.PositiveSmallIntegerField(default=0, verbose_name="释放住宿晚数")

    class Meta:
        verbose_name = "未到店处理记录"
        verbose_name_plural = "未到店处理记录"
        ordering = ("-processed_at",)

    def __str__(self):
        return f"{self.reservation_room_id} / NO_SHOW"
