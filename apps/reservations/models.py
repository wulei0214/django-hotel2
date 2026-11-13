import datetime
import hashlib
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
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

    def get_master_folio(self):
        """M-04：取主账单（多账单后预订余额/支付仍以 MASTER 为锚）。"""
        return self.folios.filter(folio_type="MASTER").first()

    def __str__(self):
        return self.booking_no
    # V1.1 M-01 语义说明：guest 字段=预订联系人/预订人（booker），不等于实际入住人；
    # 实际住客由 ReservationGuest / StayGuest 管理（勿把联系人混入入住人数）


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
    # WB-098（DEC-ED-01 §2.1/§11.3-A）：预订确认时保存的提前离店政策版本
    # 快照；历史事实不可覆盖（§11.1-5），legacy 无快照行按 NONE 兜底并审计。
    early_departure_snapshot = models.JSONField(
        null=True, blank=True, default=None,
        verbose_name="提前离店政策快照",
        help_text="预订确认时写入：{policy_type, policy_value, rate_plan_id, "
                  "snapshot_at}；无政策配置写 {\"policy_type\": \"NONE\"}。"
                  "退房结算优先读本快照，修改 RatePlan 当前政策不影响已确认订单。",
    )
    # RSV-03：提前离店释放库存的起始住宿日（含）。保留合同原始 check_out
    # 不改写，避免污染历史订单事实；库存对账重算 sold 时据此排除
    # [inventory_released_from, check_out) 已释放的夜晚，防止把释放量算回
    # sold 制造永久假差异并写坏库存。NULL 表示未发生提前离店释放。
    inventory_released_from = models.DateField(
        null=True, blank=True, verbose_name="提前离店库存释放起始日")
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
    active_stay_key = models.CharField(
        max_length=32, null=True, blank=True, unique=True, editable=False,
        verbose_name="当前房段唯一键")
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
            models.CheckConstraint(
                check=models.Q(ended_at__isnull=True) | models.Q(ended_at__gt=models.F("started_at")),
                name="ck_stay_room_segment_time_order",
            ),
        ]

    def save(self, *args, **kwargs):
        desired_key = str(self.stay_id) if self.stay_id and self.ended_at is None else None
        if self.active_stay_key != desired_key:
            self.active_stay_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"active_stay_key"}
        super().save(*args, **kwargs)

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


class ReservationGuest(models.Model):
    """预订住客快照（V1.1 M-01 P0）：预订联系人与实际预订住客分离。

    规则：每张预订最多一条 PRIMARY 主住客；BOOKER 仅表示联系人，不计入住人数；
    快照不随 Guest 档案修改而覆盖；变更保留历史不物理删除。
    """

    class Role(models.TextChoices):
        BOOKER = "BOOKER", "预订联系人"
        PRIMARY = "PRIMARY", "主住客"
        ACCOMPANYING = "ACCOMPANYING", "同住人"
        PAYER = "PAYER", "付款人"

    reservation = models.ForeignKey(
        Reservation, on_delete=models.PROTECT, related_name="reservation_guests",
        verbose_name="预订主单")
    guest = models.ForeignKey(
        "guests.Guest", null=True, blank=True,
        on_delete=models.PROTECT, related_name="reservation_guest_records",
        verbose_name="关联客人档案",
        help_text="允许先登记后建档")
    role = models.CharField(
        max_length=16, choices=Role.choices, default=Role.PRIMARY,
        verbose_name="住客角色")
    full_name = models.CharField(max_length=80, verbose_name="姓名快照")
    phone = models.CharField(max_length=24, blank=True, verbose_name="电话快照")
    doc_type = models.CharField(max_length=12, blank=True, verbose_name="证件类型")
    doc_no_ciphertext = models.TextField(blank=True, verbose_name="证件号密文")
    doc_no_hash = models.CharField(max_length=64, blank=True, verbose_name="证件号哈希")
    birth_date = models.DateField(null=True, blank=True, verbose_name="出生日期")
    nationality = models.CharField(max_length=32, blank=True, verbose_name="国籍/地区")
    is_primary = models.BooleanField(default=False, verbose_name="主住客标记")
    primary_reservation_key = models.CharField(
        max_length=32, null=True, blank=True, unique=True, editable=False,
        verbose_name="主住客唯一键")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "预订住客"
        verbose_name_plural = "预订住客"
        constraints = [
        ]

    def save(self, *args, **kwargs):
        desired_key = str(self.reservation_id) if self.reservation_id and self.is_primary else None
        if self.primary_reservation_key != desired_key:
            self.primary_reservation_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"primary_reservation_key"}
        if self.is_primary and self.reservation_id:
            dup = ReservationGuest.objects.filter(
                reservation_id=self.reservation_id, is_primary=True,
            ).exclude(pk=self.pk).exists()
            if dup:
                raise ValidationError("该预订已有主住客（M-01：最多一条 PRIMARY）")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reservation.booking_no}:{self.full_name}({self.role})"


class StayGuest(models.Model):
    """实际入住人员（V1.1 M-01 P0）：占床位者须核验证件；变更留历史。"""

    stay = models.ForeignKey(
        Stay, on_delete=models.PROTECT, related_name="stay_guests",
        verbose_name="实际入住")
    reservation_guest = models.ForeignKey(
        ReservationGuest, null=True, blank=True,
        on_delete=models.PROTECT, related_name="stay_guest_records",
        verbose_name="关联预订住客", help_text="现场加人时可为空")
    guest = models.ForeignKey(
        "guests.Guest", null=True, blank=True,
        on_delete=models.PROTECT, related_name="stay_guest_records",
        verbose_name="客人档案")
    full_name = models.CharField(max_length=80, verbose_name="姓名")
    phone = models.CharField(max_length=24, blank=True, verbose_name="电话")
    doc_type = models.CharField(max_length=12, blank=True, verbose_name="证件类型")
    doc_no_ciphertext = models.TextField(blank=True, verbose_name="证件号密文")
    doc_no_hash = models.CharField(max_length=64, blank=True, verbose_name="证件号哈希")
    checked_in_at = models.DateTimeField(null=True, blank=True, verbose_name="入住时间")
    checked_out_at = models.DateTimeField(null=True, blank=True, verbose_name="离店时间")
    is_primary = models.BooleanField(default=False, verbose_name="主住客")
    note = models.TextField(blank=True, verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "在住人员"
        verbose_name_plural = "在住人员"

    def __str__(self):
        return f"{self.stay_id}:{self.full_name}"


class InventoryHold(models.Model):
    """库存冻结明细（V1.1 M-09 P0）：回答"这间被哪张订单冻结"。

    held/sold 计数仍由库存服务维护；本表提供逐日逐单可追溯明细。
    释放必须幂等：同一 hold 重复释放不重复加库存。
    """

    class HoldStatus(models.TextChoices):
        HELD = "HELD", "冻结中"
        CONFIRMED = "CONFIRMED", "已确认占用"
        RELEASED = "RELEASED", "已释放"
        EXPIRED = "EXPIRED", "已过期"

    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="inventory_holds",
        verbose_name="所属酒店")
    room_type = models.ForeignKey(
        "inventory.RoomType", on_delete=models.PROTECT, related_name="holds",
        verbose_name="房型")
    stay_date = models.DateField(verbose_name="住宿日期")
    reservation_room = models.ForeignKey(
        ReservationRoom, on_delete=models.PROTECT, related_name="inventory_holds",
        verbose_name="预订房间明细")
    quantity = models.PositiveSmallIntegerField(default=1, verbose_name="冻结数量")
    status = models.CharField(
        max_length=12, choices=HoldStatus.choices, default=HoldStatus.HELD,
        verbose_name="状态")
    active_hold_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True, editable=False,
        verbose_name="有效冻结唯一键")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="过期时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    released_at = models.DateTimeField(null=True, blank=True, verbose_name="释放时间")

    class Meta:
        verbose_name = "库存冻结明细"
        verbose_name_plural = "库存冻结明细"
        constraints = [
        ]
        indexes = [
            models.Index(fields=("room_type", "stay_date", "status"),
                         name="idx_hold_type_date_status"),
        ]

    def save(self, *args, **kwargs):
        active = self.status in (self.HoldStatus.HELD, self.HoldStatus.CONFIRMED)
        desired_key = f"{self.reservation_room_id}:{self.stay_date.isoformat()}" if active and self.reservation_room_id and self.stay_date else None
        if self.active_hold_key != desired_key:
            self.active_hold_key = desired_key
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"active_hold_key"}
        if active and self.reservation_room_id and self.stay_date:
            dup = InventoryHold.objects.filter(
                reservation_room_id=self.reservation_room_id,
                stay_date=self.stay_date,
                status__in=(self.HoldStatus.HELD, self.HoldStatus.CONFIRMED),
            ).exclude(pk=self.pk).exists()
            if dup:
                raise ValidationError("该订单明细在该住宿日已有活跃冻结（M-09）")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.room_type_id}@{self.stay_date} {self.status}"


class CancellationPolicy(models.Model):
    """取消政策（V1.1 M-12 P0）：可计算、可快照；预订创建时保存版本。"""

    class PenaltyType(models.TextChoices):
        NONE = "NONE", "免费取消"
        FIRST_NIGHT = "FIRST_NIGHT", "收首晚"
        PERCENT = "PERCENT", "按比例"
        FULL_STAY = "FULL_STAY", "全额"

    rate_plan = models.ForeignKey(
        "inventory.RatePlan", on_delete=models.PROTECT, related_name="cancellation_policies",
        verbose_name="房价计划")
    free_cancel_until = models.CharField(
        max_length=32, verbose_name="免费取消截止",
        help_text="如 24h_before_checkin 或具体时间表达")
    penalty_type = models.CharField(
        max_length=12, choices=PenaltyType.choices, default=PenaltyType.NONE,
        verbose_name="罚金类型")
    penalty_value = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        verbose_name="罚金值", help_text="PERCENT 时为比例（0-100）")
    no_show_penalty = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name="未到罚金")
    currency = models.CharField(max_length=3, default="CNY", verbose_name="币种")
    version = models.PositiveSmallIntegerField(default=1, verbose_name="版本")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "取消政策"
        verbose_name_plural = "取消政策"
        ordering = ("rate_plan_id", "-version")

    def __str__(self):
        return f"{self.rate_plan_id} v{self.version} {self.penalty_type}"


class DepositPolicy(models.Model):
    """押金政策（V1.1 M-12 P0）。"""

    class DepositType(models.TextChoices):
        NONE = "NONE", "无押金"
        FIXED = "FIXED", "固定金额"
        PERCENT = "PERCENT", "按比例"

    rate_plan = models.ForeignKey(
        "inventory.RatePlan", on_delete=models.PROTECT, related_name="deposit_policies",
        verbose_name="房价计划")
    deposit_type = models.CharField(
        max_length=8, choices=DepositType.choices, default=DepositType.NONE,
        verbose_name="押金类型")
    deposit_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name="押金值", help_text="FIXED 为金额；PERCENT 为比例")
    due_before = models.CharField(
        max_length=32, blank=True, verbose_name="支付截止",
        help_text="如 48h_before_checkin")
    refundable = models.BooleanField(default=True, verbose_name="可退")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "押金政策"
        verbose_name_plural = "押金政策"
        ordering = ("rate_plan_id", "-created_at")

    def __str__(self):
        return f"{self.rate_plan_id} {self.deposit_type}"


class BookingAccessToken(models.Model):
    """AUTH-05（P0-08）：订单访问令牌——手机号不得单独作为订单访问凭据。

    - 明文 token 仅在预订创建成功时向用户展示一次；库中只存 SHA-256 hash。
    - 绑定单一 Reservation；过期（预计离店后 30 天）或撤销后立即失效。
    - 查询/取消凭 token；取消仍遵守既有取消政策与状态机。
    """

    reservation = models.OneToOneField(
        Reservation, on_delete=models.CASCADE, related_name="access_token",
        verbose_name="预订")
    token_hash = models.CharField(max_length=64, unique=True, verbose_name="令牌哈希")
    expires_at = models.DateTimeField(verbose_name="过期时间")
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="撤销时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "订单访问令牌"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"token:{self.token_hash[:12]} → {self.reservation.booking_no}"

    @classmethod
    def issue_for(cls, reservation):
        """为预订签发新令牌（轮换语义：旧令牌被替换即失效）。返回明文一次。"""
        import secrets

        from django.utils import timezone as tz
        plain = secrets.token_urlsafe(32)  # 256 bit 熵
        token_hash = hashlib.sha256(plain.encode("utf-8")).hexdigest()
        default_expiry = reservation.check_out + timedelta(days=30)
        expires_at = tz.make_aware(datetime.datetime.combine(
            default_expiry, datetime.time.max))
        obj, _ = cls.objects.update_or_create(
            reservation=reservation,
            defaults={"token_hash": token_hash, "expires_at": expires_at,
                      "revoked_at": None})
        return plain

    @classmethod
    def validate(cls, booking_no, plain_token):
        """校验 booking_no + 明文 token；返回 Reservation 或 None（不区分失败原因）。"""
        from django.utils import timezone as tz
        if not plain_token or not booking_no:
            return None
        token_hash = hashlib.sha256(plain_token.encode("utf-8")).hexdigest()
        now = tz.now()
        try:
            token = cls.objects.select_related(
                "reservation", "reservation__guest").get(token_hash=token_hash)
        except cls.DoesNotExist:
            return None
        if token.revoked_at is not None or token.expires_at < now:
            return None
        if token.reservation.booking_no != booking_no.strip().upper():
            return None
        return token.reservation

    def revoke(self):
        from django.utils import timezone as tz
        self.revoked_at = tz.now()
        self.save(update_fields=("revoked_at",))


class EarlyDeparturePolicy(models.Model):
    """提前离店政策：按 RatePlan 关联，5 种类型，版本化快照。"""

    objects = models.Manager()

    class PolicyType(models.TextChoices):
        NONE = "NONE", "不收违约金"
        FIRST_NIGHT = "FIRST_NIGHT", "按第一晚价格收取"
        FIXED = "FIXED", "固定金额"
        PERCENT_UNUSED = "PERCENT_UNUSED", "未住晚数比例"
        REMAINING_NIGHTS = "REMAINING_NIGHTS", "按未住晚数全额"

    rate_plan = models.ForeignKey(
        "inventory.RatePlan", on_delete=models.PROTECT,
        related_name="early_departure_policies", verbose_name="房价计划")
    policy_type = models.CharField(
        max_length=20, choices=PolicyType.choices, default=PolicyType.NONE,
        verbose_name="政策类型")
    policy_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name="政策数值",
        help_text="FIXED 为金额；PERCENT_UNUSED 为比例（0-100）")
    is_active = models.BooleanField(default=True, verbose_name="启用")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_early_departure_policies", verbose_name="创建人")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "提前离店政策"
        verbose_name_plural = "提前离店政策"
        ordering = ("rate_plan_id", "-created_at")

    def __str__(self):
        return f"{self.rate_plan_id} [{self.policy_type}] {self.policy_value or ''}"


class PendingBookingGate(models.Model):
    """WB-106：官网未支付订单并发上限的串行化锁行。

    以（物业, 手机号哈希）为粒度的纯锁行：同一 phone_hash 的并发创建事务
    在 select_for_update 上排队，使"统计 PENDING 未支付数 → 创建预订"
    成为串行的先读后写，消除并发窗口内"双读同计数、双双通过上限"的竞态。
    行本身不承载业务状态，仅用于锁竞争；gate_key 与 guest.phone_hash 同源
    （HMAC-SHA256 摘要），不落明文（WB-103 口径）。幂等重放在进入锁行
    之前已按 idempotency_key 早退，不占用锁行、不重复计数。
    """

    property = models.ForeignKey(
        "core.Property", on_delete=models.CASCADE,
        related_name="pending_booking_gates", verbose_name="所属酒店")
    gate_key = models.CharField(max_length=64, verbose_name="锁键（手机号哈希）")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "预订并发锁行"
        verbose_name_plural = "预订并发锁行"
        constraints = [
            models.UniqueConstraint(fields=("property", "gate_key"),
                                    name="uq_pbg_property_gate_key"),
        ]

    def __str__(self):
        return f"gate:{self.property_id}:{self.gate_key[:12]}"


