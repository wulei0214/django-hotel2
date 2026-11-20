from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.functional import cached_property


class RoomType(models.Model):
    """房型：可售卖的房间类别（大床房/双床房…），库存与价格按房型管理。"""

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="room_types",
        verbose_name="所属酒店",
    )
    code = models.CharField(
        max_length=32,
        verbose_name="房型代码",
        help_text="同酒店内唯一，如 QUEEN；接口按它引用房型",
    )
    name = models.CharField(max_length=80, verbose_name="房型名称")
    capacity = models.PositiveSmallIntegerField(
        default=2,
        verbose_name="核定人数",
        help_text="该房型最多可住人数（含成人+儿童），预订人数校验依据",
    )
    bed_type = models.CharField(max_length=40, blank=True, verbose_name="床型")
    amenities = models.JSONField(
        default=list,
        blank=True,
        verbose_name="设施清单",
        help_text='JSON 数组，如 ["WiFi", "空调"]',
    )
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "房型"
        verbose_name_plural = "房型"
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(fields=("property", "code"), name="uq_room_type_property_code"),
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
        "core.Property",
        on_delete=models.PROTECT,
        related_name="rooms",
        verbose_name="所属酒店",
    )
    room_type = models.ForeignKey(
        RoomType,
        on_delete=models.PROTECT,
        related_name="rooms",
        verbose_name="房型",
    )
    number = models.CharField(
        max_length=16,
        verbose_name="房号",
        help_text="同酒店内唯一，如 8301",
    )
    floor = models.CharField(max_length=16, blank=True, verbose_name="楼层")
    occupancy_status = models.CharField(
        max_length=16,
        choices=OccupancyStatus.choices,
        default=OccupancyStatus.VACANT,
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
            models.UniqueConstraint(fields=("property", "number"), name="uq_room_property_number")
        ]
        indexes = [
            models.Index(
                fields=("property", "occupancy_status", "housekeeping_status"),
                name="idx_room_prop_occ_clean",
            ),
        ]
        permissions = (
            ("housekeeping_view", "可以查看清洁与维修工作台"),
            ("housekeeping_update", "可以流转客房清洁状态"),
            ("maintenance_manage", "可以登记和恢复客房维修状态"),
        )

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
        "core.Property",
        on_delete=models.PROTECT,
        related_name="rate_plans",
        verbose_name="所属酒店",
    )
    code = models.CharField(
        max_length=32,
        verbose_name="价格代码",
        help_text="同酒店内唯一，如 BAR（Best Available Rate）",
    )
    name = models.CharField(max_length=80, verbose_name="计划名称")
    valid_from = models.DateField(verbose_name="生效日期")
    valid_to = models.DateField(
        null=True,
        blank=True,
        verbose_name="失效日期",
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
            models.UniqueConstraint(fields=("property", "code"), name="uq_rate_plan_property_code"),
            models.CheckConstraint(
                check=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="ck_rate_plan_valid_dates",
            ),
        ]
        permissions = [
            ("manage_rates", "能管理酒店每日房价"),
        ]

    def __str__(self):
        return f"{self.code} - {self.name}"


class DailyRate(models.Model):
    """每日房价：某价格计划+某房型在具体住宿日的卖价。"""

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="daily_rates",
        verbose_name="所属酒店",
    )
    rate_plan = models.ForeignKey(
        RatePlan,
        on_delete=models.PROTECT,
        related_name="daily_rates",
        verbose_name="房价计划",
    )
    room_type = models.ForeignKey(
        RoomType,
        on_delete=models.PROTECT,
        related_name="daily_rates",
        verbose_name="房型",
    )
    stay_date = models.DateField(
        verbose_name="住宿日期",
        help_text="该价格适用的入住晚（按晚计价）",
    )
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="每晚价格",
        help_text="不得为负数",
    )
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    closed = models.BooleanField(
        default=False,
        verbose_name="是否关房",
        help_text="关房后该日期该计划不可预订",
    )
    min_stay = models.PositiveSmallIntegerField(
        default=1,
        verbose_name="最少连住晚数",
    )
    max_stay = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="最多连住晚数",
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
            models.Index(
                fields=("property", "room_type", "stay_date"), name="idx_rate_prop_type_date"
            ),
        ]

    def __str__(self):
        return f"{self.stay_date} {self.room_type} {self.price}"


class RoomTypeInventory(models.Model):
    """房型每日库存：某房型在某住宿日的总量与占用拆分（乐观锁 version 防超卖）。"""

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="room_type_inventory",
        verbose_name="所属酒店",
    )
    room_type = models.ForeignKey(
        RoomType,
        on_delete=models.PROTECT,
        related_name="daily_inventory",
        verbose_name="房型",
    )
    stay_date = models.DateField(verbose_name="住宿日期")
    total_rooms = models.PositiveIntegerField(
        default=0,
        verbose_name="总房量",
        help_text="当日可参与售卖的房间总数",
    )
    held = models.PositiveIntegerField(
        default=0,
        verbose_name="冻结房量",
        help_text="待支付预订临时冻结的库存；支付成功转 sold，超时释放",
    )
    sold = models.PositiveIntegerField(default=0, verbose_name="已售房量")
    out_of_service = models.PositiveIntegerField(
        default=0,
        verbose_name="停用房量",
        help_text="维修/停售占用的库存",
    )
    stop_sell = models.BooleanField(
        default=False,
        verbose_name="是否停售",
        help_text="整房型关房开关",
    )
    version = models.PositiveIntegerField(
        default=0,
        verbose_name="乐观锁版本号",
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
            models.Index(
                fields=("property", "room_type", "stay_date"), name="idx_inv_prop_type_date"
            ),
        ]

    @cached_property
    def available_count(self):
        """可售房量 = 总量 − 冻结 − 已售 − 停用。"""
        return self.total_rooms - self.held - self.sold - self.out_of_service

    def __str__(self):
        return f"{self.room_type} / {self.stay_date} / 可售 {self.available_count}"


class HousekeepingTask(models.Model):
    """清洁任务（V1.1 M-06 P0）：退房自动生成；检查放行权限独立（决策 6）。"""

    class TaskType(models.TextChoices):
        DEPARTURE = "DEPARTURE", "退房清洁"
        STAYOVER = "STAYOVER", "住中清扫"
        DEEP_CLEAN = "DEEP_CLEAN", "深度清洁"
        INSPECTION = "INSPECTION", "检查"
        TURNDOWN = "TURNDOWN", "夜床"

    class TaskStatus(models.TextChoices):
        PENDING = "PENDING", "待清洁"
        IN_PROGRESS = "IN_PROGRESS", "清洁中"
        DONE = "DONE", "清洁完成待检查"
        INSPECTED = "INSPECTED", "检查通过"
        REJECTED = "REJECTED", "检查退回"
        CANCELLED = "CANCELLED", "已取消"

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="housekeeping_tasks",
        verbose_name="所属酒店",
    )
    room = models.ForeignKey(
        Room, on_delete=models.PROTECT, related_name="housekeeping_tasks", verbose_name="房间"
    )
    task_type = models.CharField(
        max_length=12, choices=TaskType.choices, default=TaskType.DEPARTURE, verbose_name="任务类型"
    )
    status = models.CharField(
        max_length=12,
        choices=TaskStatus.choices,
        default=TaskStatus.PENDING,
        verbose_name="任务状态",
        help_text="状态机：PENDING→IN_PROGRESS→DONE→INSPECTED；REJECTED 回 IN_PROGRESS",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="housekeeping_tasks_assigned",
        verbose_name="负责员工",
    )
    priority: models.CharField = models.CharField(
        max_length=8,
        choices=(("P1", "加急"), ("P2", "普通"), ("P3", "低")),
        default="P2",
        verbose_name="优先级",
    )
    created_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="housekeeping_tasks_created",
        verbose_name="建单人",
    )
    due_at: models.DateTimeField = models.DateTimeField(
        null=True, blank=True, verbose_name="要求完成时间"
    )
    completed_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="housekeeping_tasks_completed",
        verbose_name="完成人",
    )
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="开始时间")
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="完成时间")
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="housekeeping_tasks_inspected",
        verbose_name="检查人",
        help_text="净房放行由当天管理者确认（决策 6）",
    )
    inspected_at = models.DateTimeField(null=True, blank=True, verbose_name="检查时间")
    checklist = models.JSONField(default=list, blank=True, verbose_name="检查项")
    notes = models.TextField(blank=True, verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "清洁任务"
        verbose_name_plural = "清洁任务"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("property", "status"), name="idx_hk_prop_status"),
            models.Index(fields=("property", "assigned_to", "status"), name="idx_hk_prop_assignee"),
            models.Index(fields=("property", "due_at"), name="idx_hk_prop_due"),
        ]

    def __str__(self):
        return f"{self.room_id} {self.get_task_type_display()} {self.status}"


class MaintenanceTicket(models.Model):
    """维修工单（V1.1 M-06 P0）：按日期区间影响库存，不能只改当前房态。"""

    class TicketStatus(models.TextChoices):
        OPEN = "OPEN", "已报修"
        IN_PROGRESS = "IN_PROGRESS", "维修中"
        PENDING_VERIFY = "PENDING_VERIFY", "待验收"
        RESTORED = "RESTORED", "已恢复并验收"
        VERIFIED = "VERIFIED", "验收通过"
        CANCELLED = "CANCELLED", "已取消"

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="maintenance_tickets",
        verbose_name="所属酒店",
    )
    room = models.ForeignKey(
        Room, on_delete=models.PROTECT, related_name="maintenance_tickets", verbose_name="房间"
    )
    from_date = models.DateField(verbose_name="停用开始日期")
    to_date: models.DateField = models.DateField(
        null=True, blank=True, verbose_name="停用结束日期", help_text="空=恢复前一直停用"
    )
    reason = models.CharField(max_length=240, verbose_name="报修原因")
    category: models.CharField = models.CharField(
        max_length=32, blank=True, verbose_name="报修类别"
    )
    severity: models.CharField = models.CharField(
        max_length=8,
        choices=(("LOW", "低"), ("NORMAL", "普通"), ("HIGH", "紧急")),
        default="NORMAL",
        verbose_name="紧急程度",
    )
    status = models.CharField(
        max_length=16,
        choices=TicketStatus.choices,
        default=TicketStatus.OPEN,
        verbose_name="工单状态",
    )
    ticket_no = models.CharField(max_length=32, unique=True, verbose_name="工单号")
    assigned_to: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="maintenance_assigned",
        verbose_name="维修负责人",
    )
    started_at: models.DateTimeField = models.DateTimeField(
        null=True, blank=True, verbose_name="维修开始时间"
    )
    verified_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="maintenance_verified",
        verbose_name="验收人",
    )
    verified_at: models.DateTimeField = models.DateTimeField(
        null=True, blank=True, verbose_name="验收时间"
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="maintenance_reported",
        verbose_name="报修人",
    )
    restored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="maintenance_restored",
        verbose_name="恢复人",
    )
    restored_at = models.DateTimeField(null=True, blank=True, verbose_name="恢复时间")
    notes = models.TextField(blank=True, verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "维修工单"
        verbose_name_plural = "维修工单"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("property", "status"), name="idx_mt_prop_status"),
            models.Index(fields=("room", "status"), name="idx_mt_room_status"),
        ]

    def __str__(self):
        return f"{self.ticket_no} {self.room_id} {self.status}"


class RoomInspection(models.Model):
    """客房检查记录（V1.1 M-06 P0）：PASS 才放行净房。"""

    class InspectResult(models.TextChoices):
        PASS = "PASS", "通过"
        FAIL = "FAIL", "不通过"

    task = models.ForeignKey(
        HousekeepingTask,
        on_delete=models.PROTECT,
        related_name="inspections",
        verbose_name="清洁任务",
    )
    room = models.ForeignKey(
        Room, on_delete=models.PROTECT, related_name="inspections", verbose_name="房间"
    )
    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="room_inspections",
        verbose_name="检查人",
    )
    result: models.CharField = models.CharField(
        max_length=6, choices=InspectResult.choices, verbose_name="检查结果"
    )
    checklist_result = models.JSONField(default=dict, blank=True, verbose_name="检查项结果")
    failed_items = models.JSONField(default=list, blank=True, verbose_name="不合格项")
    reason: models.CharField = models.CharField(
        max_length=240, blank=True, verbose_name="不合格原因"
    )
    notes = models.TextField(blank=True, verbose_name="问题描述")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="检查时间")

    class Meta:
        verbose_name = "客房检查记录"
        verbose_name_plural = "客房检查记录"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.room_id} {self.result} @ {self.created_at:%Y-%m-%d %H:%M}"


class InventoryReconciliationRun(models.Model):
    """D4 库存对账运行（差异扫描批次）。"""

    objects = models.Manager.from_queryset(models.QuerySet)

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "已完成"
        IN_PROGRESS = "IN_PROGRESS", "进行中"

    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="inventory_reconciliation_runs",
        verbose_name="所属酒店",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.COMPLETED, verbose_name="状态"
    )
    scan_time = models.DateTimeField(auto_now_add=True, verbose_name="扫描时间")
    scanned_dates = models.PositiveIntegerField(default=0, verbose_name="扫描天数")
    scanned_types = models.PositiveIntegerField(default=0, verbose_name="扫描房型数")
    diff_count = models.PositiveIntegerField(default=0, verbose_name="差异数")
    room_conflict_count: models.PositiveIntegerField = models.PositiveIntegerField(
        default=0,
        verbose_name="排房冲突日数",
        help_text="本批扫描检出的（物理房, 住宿日）排房冲突数（M4-4/D4-2）",
    )
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inventory_reconciliation_runs",
        verbose_name="扫描人",
    )

    class Meta:
        verbose_name = "库存对账运行"
        verbose_name_plural = "库存对账运行"
        ordering = ("-scan_time",)

    def __str__(self):
        return f"对账 {self.property_id} {self.scan_time:%Y-%m-%d %H:%M} 差异 {self.diff_count}"


class InventoryDiscrepancy(models.Model):
    """D4 单条库存差异记录。"""

    objects = models.Manager.from_queryset(models.QuerySet)

    class Status(models.TextChoices):
        OPEN = "OPEN", "待处理"
        FIX_REQUESTED = "FIX_REQUESTED", "修复申请中"
        FIX_APPROVED = "FIX_APPROVED", "修复已批准"
        FIXED = "FIXED", "已修复"
        DISMISSED = "DISMISSED", "已关闭（无差异）"

    run = models.ForeignKey(
        InventoryReconciliationRun,
        on_delete=models.PROTECT,
        related_name="discrepancies",
        verbose_name="对账运行",
    )
    property = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="inventory_discrepancies",
        verbose_name="所属酒店",
    )
    room_type = models.ForeignKey(
        RoomType,
        on_delete=models.PROTECT,
        related_name="inventory_discrepancies",
        verbose_name="房型",
    )
    stay_date = models.DateField(verbose_name="住宿日期")
    current_total = models.PositiveIntegerField(verbose_name="当前库存总数")
    recomputed_total = models.PositiveIntegerField(verbose_name="重算应有总数")
    diff = models.IntegerField(verbose_name="差额（当前-应有）")
    held_count = models.PositiveIntegerField(default=0, verbose_name="有效 Hold 数")
    sold_count = models.PositiveIntegerField(default=0, verbose_name="已确认占用数")
    oos_count = models.PositiveIntegerField(default=0, verbose_name="维修/停用数")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, verbose_name="状态"
    )
    fix_reason: models.CharField = models.CharField(
        max_length=500, blank=True, default="", verbose_name="修复原因"
    )
    fix_evidence: models.CharField = models.CharField(
        max_length=500, blank=True, default="", verbose_name="修复证据"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requested_inventory_fixes",
        verbose_name="修复申请人",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_inventory_fixes",
        verbose_name="修复审批人",
    )
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="executed_inventory_fixes",
        verbose_name="修复执行人",
    )
    requested_at = models.DateTimeField(null=True, blank=True, verbose_name="申请时间")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="审批时间")
    fixed_at = models.DateTimeField(null=True, blank=True, verbose_name="修复时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "库存差异记录"
        verbose_name_plural = "库存差异记录"
        ordering = ("-stay_date",)
        constraints = [
            models.UniqueConstraint(
                fields=("run", "room_type", "stay_date"), name="uq_inv_disc_run_type_date"
            ),
        ]

    def __str__(self):
        return (
            f"{self.room_type_id}/{self.stay_date} "
            f"当前 {self.current_total} vs 重算 {self.recomputed_total}"
        )


class RoomAssignmentConflict(models.Model):
    """M4-4/D4-2 单条排房冲突日记录（只报告、不自动改派）。

    冲突判定式：某（物理房, 住宿日）存在 ≥2 条活跃排房明细（ReservationRoom
    已排房、合同区间覆盖该日、预订状态 PENDING/CONFIRMED/CHECKED_IN、排除
    No-Show 与提前离店已释放夜晚）即冲突日——物理房可售容量=1 间/晚。
    判定口径与事务内排房闸门（front_desk_services._check_room_assignment_conflict，
    WB-085）同款，释放语义与库存 sold 重算（RSV-03）一致。

    修复只能人工执行（前台换房/取消多余明细）；对账扫描永不改写排房明细。
    待办完成经 reconciliation.review_room_conflict 同口径复核，冲突消除才关闭。
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "待处理"
        DISMISSED = "DISMISSED", "已关闭（冲突消除/误报）"

    run: models.ForeignKey = models.ForeignKey(
        InventoryReconciliationRun,
        on_delete=models.PROTECT,
        related_name="room_conflicts",
        verbose_name="对账运行",
        help_text="首报运行；重扫描原位更新时归属不变（M4-3 幂等化同款）",
    )
    property: models.ForeignKey = models.ForeignKey(
        "core.Property",
        on_delete=models.PROTECT,
        related_name="room_assignment_conflicts",
        verbose_name="所属酒店",
    )
    room: models.ForeignKey = models.ForeignKey(
        Room,
        on_delete=models.PROTECT,
        related_name="assignment_conflicts",
        verbose_name="冲突物理房",
    )
    room_type: models.ForeignKey = models.ForeignKey(
        RoomType,
        on_delete=models.PROTECT,
        related_name="assignment_conflicts",
        verbose_name="房型",
    )
    # mypy attname 注册（Django 元类不建字段）：__str__ 与复核服务引用 room_id
    room_id: int
    stay_date: models.DateField = models.DateField(verbose_name="冲突住宿日")
    conflict_count: models.PositiveSmallIntegerField = models.PositiveSmallIntegerField(
        verbose_name="当日活跃排房明细数",
        help_text="按判定式统计的占用计数；≥2 即冲突",
    )
    details: models.JSONField = models.JSONField(
        default=list,
        blank=True,
        verbose_name="冲突明细快照",
        help_text="JSON 数组：[{line_id, booking_no, check_in, check_out}]，供人工修复定位",
    )
    status: models.CharField = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN, verbose_name="状态"
    )
    # V12-07：并发扫描互斥键。MySQL 8 不支持条件唯一约束（本仓库既有口径），
    # 用"可空唯一键"表达"同一（物理房, 住宿日）只允许一条 OPEN"——OPEN 时写
    # f"{room_id}:{stay_date}"，DISMISSED 置 NULL 释放键位（NULL 不参与唯一性）。
    # 由 save() 维护，多进程/多实例下由数据库而非 Python 进程内锁兜底。
    open_key: models.CharField = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        default=None,
        verbose_name="待处理唯一键",
        help_text="OPEN 时=房/日复合键，其余为 NULL；并发扫描的唯一性仲裁依据",
    )
    dismiss_reason: models.CharField = models.CharField(
        max_length=240, blank=True, default="", verbose_name="关闭原因"
    )
    dismissed_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="dismissed_room_conflicts",
        verbose_name="关闭人",
    )
    dismissed_at: models.DateTimeField = models.DateTimeField(
        null=True, blank=True, verbose_name="关闭时间"
    )
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "排房冲突日记录"
        verbose_name_plural = "排房冲突日记录"
        ordering = ("-stay_date",)
        constraints = [
            models.UniqueConstraint(
                fields=("run", "room", "stay_date"), name="uq_room_conflict_run_room_date"
            ),
            # V12-07：跨运行唯一性——并发/重复扫描不得对同一（房, 日）产生两条
            # 有效（OPEN）差异；DISMISSED 行为 NULL，可保留多条历史。
            models.UniqueConstraint(fields=("open_key",), name="uq_room_conflict_open_key"),
        ]
        indexes = [
            models.Index(
                fields=("property", "status", "stay_date"), name="idx_rac_prop_status_date"
            ),
        ]

    def save(self, *args, **kwargs):
        """维护 open_key：OPEN → 复合键；其他状态 → NULL。

        键位由模型统一维护，避免调用方（扫描、复核、关闭）各写一份而漏同步；
        `update_fields` 显式指定时补齐 open_key，防止状态变更后键位残留。
        """
        self.open_key = (
            f"{self.room_id}:{self.stay_date.isoformat()}"
            if self.status == self.Status.OPEN and self.room_id and self.stay_date
            else None
        )
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"open_key"}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.room_id}/{self.stay_date} 冲突 {self.conflict_count} 条明细 {self.status}"
