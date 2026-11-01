from django.db import models
from django.db.models import F, Q
from django.utils.functional import cached_property


class RoomType(models.Model):
    """房型：可售卖的房间类别（大床房/双床房…），库存与价格按房型管理。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="room_types",
        verbose_name="所属酒店",
    )
    code = models.CharField(
        max_length=32, verbose_name="房型代码",
        help_text="同酒店内唯一，如 QUEEN；接口按它引用房型",
    )
    name = models.CharField(max_length=80, verbose_name="房型名称")
    capacity = models.PositiveSmallIntegerField(
        default=2, verbose_name="核定人数",
        help_text="该房型最多可住人数（含成人+儿童），预订人数校验依据",
    )
    bed_type = models.CharField(max_length=40, blank=True, verbose_name="床型")
    amenities = models.JSONField(
        default=list, blank=True, verbose_name="设施清单",
        help_text="JSON 数组，如 [\"WiFi\", \"空调\"]",
    )
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "房型"
        verbose_name_plural = "房型"
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "code"), name="uq_room_type_property_code"
            ),
            models.CheckConstraint(check=Q(capacity__gte=1), name="ck_room_type_capacity_positive"),
        ]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Room(models.Model):
    """物理客房：一间真实存在的房间，三种状态维度互相独立。"""

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
        "core.Property", on_delete=models.PROTECT, related_name="rooms",
        verbose_name="所属酒店",
    )
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, related_name="rooms",
        verbose_name="房型",
    )
    number = models.CharField(
        max_length=16, verbose_name="房号",
        help_text="同酒店内唯一，如 8301",
    )
    floor = models.CharField(max_length=16, blank=True, verbose_name="楼层")
    occupancy_status = models.CharField(
        max_length=16, choices=OccupancyStatus.choices, default=OccupancyStatus.VACANT,
        verbose_name="在住状态",
    )
    housekeeping_status = models.CharField(
        max_length=16,
        choices=HousekeepingStatus.choices,
        default=HousekeepingStatus.CLEAN,
        verbose_name="清洁状态",
        help_text="退房 → 脏房 → 清洁中 → 待检查 → 净房（净房才可售）",
    )
    maintenance_status = models.CharField(
        max_length=20,
        choices=MaintenanceStatus.choices,
        default=MaintenanceStatus.NORMAL,
        verbose_name="维修状态",
        help_text="维修停用/停售的房间不参与排房",
    )
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "物理客房"
        verbose_name_plural = "物理客房"
        ordering = ("number",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "number"), name="uq_room_property_number"
            )
        ]
        indexes = [
            models.Index(fields=("property", "occupancy_status", "housekeeping_status"), name="idx_room_prop_occ_clean"),
        ]

    def __str__(self):
        return self.number

    @cached_property
    def is_sellable_now(self):
        """当前是否可售：启用 + 空闲 + 净房 + 无维修。"""
        return (
            self.is_active
            and self.occupancy_status == self.OccupancyStatus.VACANT
            and self.housekeeping_status == self.HousekeepingStatus.CLEAN
            and self.maintenance_status == self.MaintenanceStatus.NORMAL
        )


class RatePlan(models.Model):
    """房价计划：一套价格政策（含取消/押金条款），每日房价挂在计划下。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="rate_plans",
        verbose_name="所属酒店",
    )
    code = models.CharField(
        max_length=32, verbose_name="价格代码",
        help_text="同酒店内唯一，如 BAR（Best Available Rate）",
    )
    name = models.CharField(max_length=80, verbose_name="计划名称")
    valid_from = models.DateField(verbose_name="生效日期")
    valid_to = models.DateField(
        null=True, blank=True, verbose_name="失效日期",
        help_text="空表示长期有效；不得早于生效日期",
    )
    cancellation_policy = models.TextField(blank=True, verbose_name="取消政策")
    deposit_policy = models.TextField(blank=True, verbose_name="押金政策")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "房价计划"
        verbose_name_plural = "房价计划"
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

    def __str__(self):
        return f"{self.code} - {self.name}"


class DailyRate(models.Model):
    """每日房价：某价格计划+某房型在具体住宿日的卖价。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="daily_rates",
        verbose_name="所属酒店",
    )
    rate_plan = models.ForeignKey(
        RatePlan, on_delete=models.PROTECT, related_name="daily_rates",
        verbose_name="房价计划",
    )
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, related_name="daily_rates",
        verbose_name="房型",
    )
    stay_date = models.DateField(
        verbose_name="住宿日期",
        help_text="该价格适用的入住晚（按晚计价）",
    )
    price = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="每晚价格",
        help_text="不得为负数",
    )
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    closed = models.BooleanField(
        default=False, verbose_name="是否关房",
        help_text="关房后该日期该计划不可预订",
    )
    min_stay = models.PositiveSmallIntegerField(
        default=1, verbose_name="最少连住晚数",
    )
    max_stay = models.PositiveSmallIntegerField(
        null=True, blank=True, verbose_name="最多连住晚数",
        help_text="空表示不限制",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "每日房价"
        verbose_name_plural = "每日房价"
        ordering = ("stay_date",)
        constraints = [
            models.UniqueConstraint(
                fields=("rate_plan", "room_type", "stay_date"),
                name="uq_daily_rate_plan_room_date",
            ),
            models.CheckConstraint(check=Q(price__gte=0), name="ck_daily_rate_nonnegative"),
        ]
        indexes = [
            models.Index(fields=("property", "room_type", "stay_date"), name="idx_rate_prop_type_date"),
        ]

    def __str__(self):
        return f"{self.stay_date} {self.room_type} {self.price}"


class RoomTypeInventory(models.Model):
    """房型每日库存：某房型在某住宿日的总量与占用拆分（乐观锁 version 防超卖）。"""

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="room_type_inventory",
        verbose_name="所属酒店",
    )
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, related_name="daily_inventory",
        verbose_name="房型",
    )
    stay_date = models.DateField(verbose_name="住宿日期")
    total_rooms = models.PositiveIntegerField(
        default=0, verbose_name="总房量",
        help_text="当日可参与售卖的房间总数",
    )
    held = models.PositiveIntegerField(
        default=0, verbose_name="冻结房量",
        help_text="待支付预订临时冻结的库存；支付成功转 sold，超时释放",
    )
    sold = models.PositiveIntegerField(default=0, verbose_name="已售房量")
    out_of_service = models.PositiveIntegerField(
        default=0, verbose_name="停用房量",
        help_text="维修/停售占用的库存",
    )
    stop_sell = models.BooleanField(
        default=False, verbose_name="是否停售",
        help_text="整房型关房开关",
    )
    version = models.PositiveIntegerField(
        default=0, verbose_name="乐观锁版本号",
        help_text="库存扣减/释放时 +1，防并发超卖（勿手工修改）",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "房型每日库存"
        verbose_name_plural = "房型每日库存"
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
            models.Index(fields=("property", "room_type", "stay_date"), name="idx_inv_prop_type_date"),
        ]

    @cached_property
    def available_count(self):
        """可售房量 = 总量 − 冻结 − 已售 − 停用。"""
        return self.total_rooms - self.held - self.sold - self.out_of_service

    def __str__(self):
        return f"{self.room_type} / {self.stay_date} / 可售 {self.available_count}"
