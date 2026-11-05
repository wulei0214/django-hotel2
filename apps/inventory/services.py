from datetime import datetime as dt_datetime
from datetime import time as _time, timedelta

from django.db import transaction
from django.db.models import Sum

from .models import DailyRate, Room, RoomTypeInventory


class InventoryUnavailable(Exception):
    """The requested room type has insufficient inventory for at least one night."""


class HousekeepingOperationError(Exception):
    """清洁/维修状态流转不合法或房间不可操作（基线 2.0 §4.3 清洁与维修工作台）。"""


# ---------------------------------------------------------------------------
# §4.3 清洁与维修工作台：三维房态流转规则与领域服务
# ---------------------------------------------------------------------------
# 清洁链正向流转（接单→清洁→待检查→净房放行），INSPECTING→DIRTY 为检查不通过
# 返工；CLEAN 之后弄脏（退房/住中）由 reservations 退房服务负责，不在本工作台。
HOUSEKEEPING_TRANSITIONS = {
    Room.HousekeepingStatus.DIRTY: frozenset({
        Room.HousekeepingStatus.CLEANING,
    }),
    Room.HousekeepingStatus.CLEANING: frozenset({
        Room.HousekeepingStatus.INSPECTING,
    }),
    Room.HousekeepingStatus.INSPECTING: frozenset({
        Room.HousekeepingStatus.CLEAN,   # 检查通过：净房放行（需 room_release_ready）
        Room.HousekeepingStatus.DIRTY,   # 检查不通过：返工
    }),
    Room.HousekeepingStatus.CLEAN: frozenset(),
}

# 维修维度：报修停用（NORMAL→OUT_OF_ORDER/OUT_OF_SERVICE）、停用级别互换、
# 维修完成恢复（→NORMAL）。恢复不联动清洁状态——净房与否由清洁链独立判断。
MAINTENANCE_TRANSITIONS = {
    Room.MaintenanceStatus.NORMAL: frozenset({
        Room.MaintenanceStatus.OUT_OF_ORDER,
        Room.MaintenanceStatus.OUT_OF_SERVICE,
    }),
    Room.MaintenanceStatus.OUT_OF_ORDER: frozenset({
        Room.MaintenanceStatus.NORMAL,
        Room.MaintenanceStatus.OUT_OF_SERVICE,
    }),
    Room.MaintenanceStatus.OUT_OF_SERVICE: frozenset({
        Room.MaintenanceStatus.NORMAL,
        Room.MaintenanceStatus.OUT_OF_ORDER,
    }),
}


def _release_ready_permitted(user, property_obj):
    """净房放行权限判定（F-08 补执行点：hotelapp.room_release_ready）。"""
    from apps.identity.authorization import AuthorizationService
    return AuthorizationService.has_permission(
        user, property_obj, "hotelapp.room_release_ready")


def _property_today(property_obj):
    """物业时区当前日期（与夜审 property_date 同口径）。"""
    from zoneinfo import ZoneInfo
    from django.utils import timezone
    try:
        tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return timezone.localdate(timezone.now(), tz)


@transaction.atomic
def change_housekeeping_status(*, property_obj, room_id, target_status,
                               changed_by=None, request=None, notes=""):
    """清洁状态流转唯一写入口（§4.3 链式规则 + 审计）。

    规则：
      * 合法流转见 HOUSEKEEPING_TRANSITIONS；跳步（如 DIRTY→CLEAN）拒绝；
      * 同状态重复提交为幂等空操作，返回 changed=False（不落审计）；
      * →CLEAN（净房放行）在服务层复核 hotelapp.room_release_ready（三层复核
        中的服务层一道；API 层先行校验同一权限码）；其余流转的
        hotelapp.change_room 由 API 层门禁（与房态 CRUD 同码，客房服务员在列）；
      * 占用（occupancy）与维修（maintenance）维度不在本服务修改；
      * 每次实际变更落 AuditEvent housekeeping.status_changed。

    返回 (room, changed)。
    """
    from apps.core.models import AuditEvent

    if target_status not in Room.HousekeepingStatus.values:
        raise HousekeepingOperationError(
            "清洁状态必须是 DIRTY/CLEANING/INSPECTING/CLEAN 之一。")

    room = (
        Room.objects.select_for_update()
        .select_related("room_type")
        .filter(pk=room_id, property=property_obj)
        .first()
    )
    if room is None:
        raise HousekeepingOperationError("房间不存在或不属于当前物业。")

    current = room.housekeeping_status
    if current == target_status:
        return room, False
    allowed = HOUSEKEEPING_TRANSITIONS.get(current, frozenset())
    if target_status not in allowed:
        raise HousekeepingOperationError(
            f"清洁状态不允许从 {current} 流转到 {target_status}"
            f"（合法链路：DIRTY→CLEANING→INSPECTING→CLEAN，"
            f"待检查可退回 DIRTY 返工）。")

    if (target_status == Room.HousekeepingStatus.CLEAN
            and not _release_ready_permitted(changed_by, property_obj)):
        raise HousekeepingOperationError(
            "净房放行需要 hotelapp.room_release_ready 权限"
            "（值班经理/店长）。")

    room.housekeeping_status = target_status
    room.save(update_fields=("housekeeping_status", "updated_at"))
    AuditEvent.record(
        action="housekeeping.status_changed",
        entity_type="Room", entity_id=str(room.pk),
        actor=changed_by, property_obj=property_obj,
        before={"housekeeping_status": current,
                "occupancy_status": room.occupancy_status,
                "maintenance_status": room.maintenance_status},
        after={"housekeeping_status": target_status,
               "occupancy_status": room.occupancy_status,
               "maintenance_status": room.maintenance_status,
               "is_sellable_now": room.is_sellable_now},
        reason=str(notes or "")[:240], request=request,
    )
    return room, True


def _sync_inventory_out_of_service(room, delta, *, property_obj):
    """基线 2.0 §3.3：维修停用/恢复 → 未来库存 out_of_service 同步。

    口径：
      * 仅同步已有 RoomTypeInventory 行中 stay_date >= 物业当前营业日的行
        （未来日期库存；历史行不回写）；未生成的日期由
        sync_inventory_calendar 命令按物理房主数据初始化时携带停用数；
      * delta=+1：房间进入 OUT_OF_ORDER/OUT_OF_SERVICE；delta=-1：恢复 NORMAL；
      * 行锁逐行更新并复核 CheckConstraint 口径
        （total_rooms >= held + sold + out_of_service）：停用导致容量不足时
        整体失败（fail-closed），由操作者先处理 Hold/Sold 再停用；
      * version 乐观计数 +1，与日历同步命令一致。
    返回同步的行数。
    """
    if delta == 0:
        return 0
    today = _property_today(property_obj)
    synced = 0
    rows = list(
        RoomTypeInventory.objects.select_for_update()
        .filter(
            property=property_obj,
            room_type=room.room_type,
            stay_date__gte=today,
        )
        .order_by("stay_date", "id")
    )
    for stock in rows:
        new_value = stock.out_of_service + delta
        if new_value < 0:
            # 口径保护：不把停用计数减成负数（如命令先行归零后的人工恢复）
            new_value = 0
        if delta > 0 and stock.total_rooms < (
                stock.held + stock.sold + new_value):
            raise HousekeepingOperationError(
                f"{stock.stay_date.isoformat()} 库存不足：总房量 "
                f"{stock.total_rooms}，已 Hold/Sold {stock.held + stock.sold}，"
                f"无法再停用 1 间；请先处理该日期的预订或库存。")
        stock.out_of_service = new_value
        stock.version = (stock.version or 0) + 1
        stock.save(update_fields=("out_of_service", "version", "updated_at"))
        synced += 1
    return synced


@transaction.atomic
def change_maintenance_status(*, property_obj, room_id, target_status, reason,
                              changed_by=None, request=None):
    """维修状态登记唯一写入口（§4.3 维修停用/恢复 + 审计）。

    规则：
      * 合法流转见 MAINTENANCE_TRANSITIONS；reason 必填（1-240 字符）；
      * hotelapp.no_show_handle（值班经理/店长专项权限）由 API 层先行校验；
        选择该口径而非 change_room：维修停用直接影响可售库存，属管理者动作，
        不能开放给客房服务员（change_room 持有者含客房服务员）；
      * 恢复（→NORMAL）不联动清洁状态：房间仍脏则继续清洁链；
      * 基线 2.0 §3.3：停用/恢复与未来库存 out_of_service 同事务同步，
        维修停用立即降低未来可售（不再依赖手动日历同步）；
      * 每次实际变更落 AuditEvent maintenance.status_changed。

    返回 (room, changed)。
    """
    from apps.core.models import AuditEvent

    reason = str(reason or "").strip()
    if not reason or len(reason) > 240:
        raise HousekeepingOperationError("维修登记必须填写 1 至 240 个字符的原因。")
    if target_status not in Room.MaintenanceStatus.values:
        raise HousekeepingOperationError(
            "维修状态必须是 NORMAL/OUT_OF_ORDER/OUT_OF_SERVICE 之一。")

    room = (
        Room.objects.select_for_update()
        .select_related("room_type")
        .filter(pk=room_id, property=property_obj)
        .first()
    )
    if room is None:
        raise HousekeepingOperationError("房间不存在或不属于当前物业。")

    current = room.maintenance_status
    if current == target_status:
        return room, False
    allowed = MAINTENANCE_TRANSITIONS.get(current, frozenset())
    if target_status not in allowed:
        raise HousekeepingOperationError(
            f"维修状态不允许从 {current} 变更为 {target_status}。")

    entering = target_status in (
        Room.MaintenanceStatus.OUT_OF_ORDER,
        Room.MaintenanceStatus.OUT_OF_SERVICE,
    )
    leaving = current in (
        Room.MaintenanceStatus.OUT_OF_ORDER,
        Room.MaintenanceStatus.OUT_OF_SERVICE,
    )
    delta = 0
    if entering and not leaving:
        delta = 1
    elif leaving and not entering:
        delta = -1

    # 先同步库存（可能因容量不足抛错回滚），再落房间状态，保证原子一致
    synced_rows = _sync_inventory_out_of_service(
        room, delta, property_obj=property_obj)

    room.maintenance_status = target_status
    room.save(update_fields=("maintenance_status", "updated_at"))
    AuditEvent.record(
        action="maintenance.status_changed",
        entity_type="Room", entity_id=str(room.pk),
        actor=changed_by, property_obj=property_obj,
        before={"maintenance_status": current,
                "occupancy_status": room.occupancy_status,
                "housekeeping_status": room.housekeeping_status},
        after={"maintenance_status": target_status,
               "occupancy_status": room.occupancy_status,
               "housekeeping_status": room.housekeeping_status,
               "is_sellable_now": room.is_sellable_now,
               "inventory_out_of_service_rows_synced": synced_rows},
        reason=reason, request=request,
    )
    return room, True


def housekeeping_summary(property_obj):
    """今日清洁/维修统计（§4.3 工作台统计卡）：各状态计数 + 待放行 + 今日流转。"""
    from django.db.models import Count
    from django.utils import timezone

    rooms = Room.objects.filter(property=property_obj, is_active=True)
    housekeeping, maintenance = {}, {}
    by_floor = {}
    for row in rooms.values(
            "floor", "housekeeping_status", "maintenance_status"
    ).annotate(total=Count("id")):
        hk, mt, total = (row["housekeeping_status"], row["maintenance_status"],
                         row["total"])
        housekeeping[hk] = housekeeping.get(hk, 0) + total
        maintenance[mt] = maintenance.get(mt, 0) + total
        bucket = by_floor.setdefault(row["floor"] or "", {
            "floor": row["floor"] or "", "total": 0,
            "CLEAN": 0, "DIRTY": 0, "CLEANING": 0, "INSPECTING": 0,
            "maintenance_abnormal": 0})
        bucket["total"] += total
        bucket[hk] = bucket.get(hk, 0) + total
        if mt != Room.MaintenanceStatus.NORMAL:
            bucket["maintenance_abnormal"] += total

    # WB-12：“今日流转”窗口必须按物业时区取当日零点；此前 make_aware 缺省
    # 用 settings.TIME_ZONE 解释物业日历日期，跨时区物业会把昨天/明天的
    # 流转错记进“今天”。
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    day_start = dt_datetime.combine(
        _property_today(property_obj), _time.min, tzinfo=tz)
    from apps.core.models import AuditEvent
    today_events = AuditEvent.objects.filter(
        property=property_obj, created_at__gte=day_start)
    housekeeping_changes = today_events.filter(
        action="housekeeping.status_changed").count()
    released_today = today_events.filter(
        action="housekeeping.status_changed",
        after_json__housekeeping_status=Room.HousekeepingStatus.CLEAN).count()
    reworked_today = today_events.filter(
        action="housekeeping.status_changed",
        after_json__housekeeping_status=Room.HousekeepingStatus.DIRTY).count()
    maintenance_changes = today_events.filter(
        action="maintenance.status_changed").count()

    return {
        "housekeeping": housekeeping,
        "maintenance": maintenance,
        "pending_release": housekeeping.get(Room.HousekeepingStatus.INSPECTING, 0),
        "sellable_now": rooms.filter(
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL).count(),
        "total_rooms": sum(housekeeping.values()),
        "by_floor": sorted(by_floor.values(), key=lambda item: item["floor"]),
        "today": {
            "housekeeping_changes": housekeeping_changes,
            "released": released_today,
            "reworked": reworked_today,
            "maintenance_changes": maintenance_changes,
        },
    }


def stay_dates(check_in, check_out):
    if check_out <= check_in:
        raise ValueError("离店日期必须晚于入住日期。")
    current = check_in
    while current < check_out:
        yield current
        current += timedelta(days=1)


def models_q_valid_to(last_night):
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gte=last_night)


def get_room_type_availability(property_obj, room_type, check_in, check_out, rooms=1, rate_plan=None):
    dates = list(stay_dates(check_in, check_out))
    if room_type.property_id != property_obj.pk or not room_type.is_active:
        raise ValueError("房型不存在或未启用。")

    if rate_plan is None:
        # 自动选择房价计划必须落在"该房型在住宿区间实际有价格行"的计划上；
        # 全局取第一个会导致只有碰巧挂在默认计划下的房型可售（其余房型恒 0 间）
        scoped = room_type.property.rate_plans.filter(
            is_active=True,
            valid_from__lte=check_in,
        ).filter(models_q_valid_to(check_out - timedelta(days=1)))
        rate_plan = (
            scoped.filter(
                daily_rates__room_type=room_type,
                daily_rates__stay_date__gte=check_in,
                daily_rates__stay_date__lt=check_out,
            )
            .order_by("valid_from", "id")
            .first()
            or scoped.order_by("valid_from", "id").first()
        )
    if (
        rate_plan is None
        or rate_plan.property_id != property_obj.pk
        or not rate_plan.is_active
        or rate_plan.valid_from > check_in
        or (rate_plan.valid_to and rate_plan.valid_to < dates[-1])
    ):
        raise ValueError("该日期区间没有可用的房价计划。")

    daily_rates = {
        rate.stay_date: rate
        for rate in DailyRate.objects.filter(
            property=property_obj,
            rate_plan=rate_plan,
            room_type=room_type,
            stay_date__gte=check_in,
            stay_date__lt=check_out,
        )
    }
    inventory = {
        row.stay_date: row
        for row in RoomTypeInventory.objects.filter(
            property=property_obj,
            room_type=room_type,
            stay_date__gte=check_in,
            stay_date__lt=check_out,
        )
    }

    currencies = {daily_rates[day].currency for day in dates if day in daily_rates}
    if len(currencies) > 1:
        raise ValueError("同一预订日期区间不能混用不同币种。")

    nightly_details = []
    available_counts = []
    for stay_date in dates:
        rate = daily_rates.get(stay_date)
        stock = inventory.get(stay_date)
        stay_length = len(dates)
        if (
            rate is None
            or rate.closed
            or stock is None
            or stock.stop_sell
            or stay_length < rate.min_stay
            or (rate.max_stay is not None and stay_length > rate.max_stay)
        ):
            return {
                "room_type_id": room_type.pk,
                "room_type_name": room_type.name,
                "rate_plan_id": rate_plan.pk,
                "available_count": 0,
                "total_amount": None,
                "nightly_details": [],
            }
        nightly_details.append(
            {"date": stay_date.isoformat(), "price": str(rate.price), "currency": rate.currency}
        )
        available_counts.append(stock.available_count)

    total_per_room = sum(
        (daily_rates[day].price for day in dates),
        start=daily_rates[dates[0]].price * 0,
    )
    return {
        "room_type_id": room_type.pk,
        "room_type_name": room_type.name,
        "rate_plan_id": rate_plan.pk,
        "available_count": min(available_counts) if available_counts else 0,
        "total_amount": str(total_per_room * rooms),
        "nightly_details": nightly_details,
    }


def _group_reservation_room_quantities(reservation):
    quantities = {}
    for line in reservation.reservation_rooms.values(
        "room_type_id", "check_in", "check_out"
    ):
        key = (line["room_type_id"], line["check_in"], line["check_out"])
        quantities[key] = quantities.get(key, 0) + 1
    return quantities


@transaction.atomic
def hold_inventory_for_reservation(reservation):
    """在同一事务中锁定每晚库存并增加 held。"""
    quantities = _group_reservation_room_quantities(reservation)

    for (room_type_id, check_in, check_out), quantity in sorted(
        quantities.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        dates = list(stay_dates(check_in, check_out))
        rows = list(
            RoomTypeInventory.objects.select_for_update()
            .filter(
                property=reservation.property,
                room_type_id=room_type_id,
                stay_date__in=dates,
            )
            .order_by("stay_date")
        )
        if len(rows) != len(dates):
            raise InventoryUnavailable("库存日历不完整，无法完成预订。")
        if any(row.stop_sell or row.available_count < quantity for row in rows):
            raise InventoryUnavailable("所选日期房量不足，请重新查询。")

        for row in rows:
            row.held += quantity
            row.version += 1
            row.save(update_fields=("held", "version", "updated_at"))


@transaction.atomic
def confirm_inventory_for_reservation(reservation):
    """支付成功时将 held 转为 sold；调用方需锁定预订记录。"""
    quantities = _group_reservation_room_quantities(reservation)

    for (room_type_id, check_in, check_out), quantity in sorted(
        quantities.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        dates = list(stay_dates(check_in, check_out))
        rows = list(
            RoomTypeInventory.objects.select_for_update()
            .filter(
                property=reservation.property,
                room_type_id=room_type_id,
                stay_date__in=dates,
            )
            .order_by("stay_date")
        )
        if len(rows) != len(dates) or any(row.held < quantity for row in rows):
            raise InventoryUnavailable("预订库存 Hold 已失效，不能确认订单。")
        for row in rows:
            row.held -= quantity
            row.sold += quantity
            row.version += 1
            row.save(update_fields=("held", "sold", "version", "updated_at"))


@transaction.atomic
def release_inventory_hold(reservation):
    """取消/过期时释放 Hold；重复调用由 Reservation 状态机保证幂等。"""
    quantities = _group_reservation_room_quantities(reservation)

    for (room_type_id, check_in, check_out), quantity in sorted(
        quantities.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        dates = list(stay_dates(check_in, check_out))
        rows = list(
            RoomTypeInventory.objects.select_for_update()
            .filter(
                property=reservation.property,
                room_type_id=room_type_id,
                stay_date__in=dates,
            )
            .order_by("stay_date")
        )
        if len(rows) != len(dates):
            raise InventoryUnavailable("库存日历不完整，无法释放预订 Hold。")
        if any(row.held < quantity for row in rows):
            raise InventoryUnavailable("库存 Hold 数量与预订状态不一致。")
        for row in rows:
            row.held -= quantity
            row.version += 1
            row.save(update_fields=("held", "version", "updated_at"))



@transaction.atomic
def release_sold_inventory_for_room_line(line, *, from_date):
    """No-Show 按房间明细释放尚未消耗的未来已售库存。"""
    dates = list(stay_dates(max(line.check_in, from_date), line.check_out))
    if not dates:
        return 0
    rows = list(
        RoomTypeInventory.objects.select_for_update()
        .filter(
            property=line.property,
            room_type_id=line.room_type_id,
            stay_date__in=dates,
        )
        .order_by("stay_date")
    )
    if len(rows) != len(dates):
        raise InventoryUnavailable("No-Show 对应库存日历不完整，不能安全释放库存。")
    if any(row.sold < 1 for row in rows):
        raise InventoryUnavailable("No-Show 对应已售库存不足，不能重复释放。")
    for row in rows:
        row.sold -= 1
        row.version += 1
        row.save(update_fields=("sold", "version", "updated_at"))
    return len(rows)


def adjust_daily_rate(*, property_obj, rate_id, price, reason,
                      updated_by=None, request=None):
    """调价唯一写入口：物业校验、价格精度、历史保护和审计。"""
    from decimal import Decimal, InvalidOperation
    from zoneinfo import ZoneInfo
    from django.db import transaction
    from django.utils import timezone
    from apps.core.models import AuditEvent
    from .models import DailyRate

    reason = str(reason or "").strip()
    if not reason or len(reason) > 240:
        raise ValueError("调价必须填写 1 至 240 个字符的原因。")
    try:
        amount = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("价格格式无效。") from exc
    try:
        exact_to_cent = (
            amount.is_finite() and amount >= 0
            and amount == amount.quantize(Decimal("0.01")))
    except InvalidOperation as exc:
        # WB-01：超出 Decimal 上下文精度的超大值统一拒绝为校验错误。
        raise ValueError("价格必须是非负且精确到分的金额。") from exc
    if not exact_to_cent:
        raise ValueError("价格必须是非负且精确到分的金额。")
    try:
        tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    today = timezone.localdate(timezone.now(), tz)

    with transaction.atomic():
        rate = DailyRate.objects.select_for_update().filter(
            pk=rate_id, property=property_obj,
            rate_plan__property=property_obj,
            room_type__property=property_obj,
        ).first()
        if rate is None:
            raise ValueError("每日房价不存在或不属于当前物业。")
        if rate.closed:
            raise ValueError("该住宿日已关房，不能调整房价。")
        if rate.stay_date < today:
            raise ValueError("历史住宿日的房价不可修改。")
        before = rate.price
        if before == amount:
            return rate, False
        rate.price = amount
        rate.save(update_fields=("price", "updated_at"))
        AuditEvent.record(
            action="inventory.daily_rate_adjust",
            entity_type="DailyRate", entity_id=rate.pk,
            actor=updated_by, property_obj=property_obj,
            before={"price": str(before), "stay_date": rate.stay_date.isoformat()},
            after={"price": str(amount), "stay_date": rate.stay_date.isoformat()},
            reason=reason, request=request,
        )
        return rate, True
