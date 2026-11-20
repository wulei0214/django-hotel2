"""房态库存中心受控写服务（MC-004）。

不新增与既有服务平行的库存权威：
  * 每日房价关房/开房、房型停售/开售、房间/房型主数据维护在此收口；
  * 物理库存总量仍由 sync_inventory_calendar 从房间推导，人工不得直接写
    available_count；维修停用/恢复继续走 change_maintenance_status。
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.identity.authorization import AuthorizationService

from .models import DailyRate, Room, RoomType, RoomTypeInventory
from .services import HousekeepingOperationError, _property_today


def _sync_calendar_for_room_type(room_type):
    """MC11-INV-04：主数据写后即时联动库存日历（今日起 30 天窗口）。

    复用滚动生成的唯一实现（sync_inventory_calendar._sync_room_type_calendar，
    单房型与 --all-room-types 共用）；新建/停用/换房型不再等到次日 02:30
    调度才反映到可售量。
    """
    from apps.inventory.management.commands.sync_inventory_calendar import (
        _sync_room_type_calendar,
    )

    start = timezone.localdate()
    _sync_room_type_calendar(room_type.property, room_type, start, start + timedelta(days=30))


def _require(user, property_obj, permission, message):
    if not AuthorizationService.has_permission(user, property_obj, permission):
        raise HousekeepingOperationError(message)


def _audit(action, entity_type, entity_id, actor, property_obj, before, after, reason):
    AuditEvent.record(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        property_obj=property_obj,
        before=before,
        after=after,
        reason=str(reason or "")[:240],
    )


@transaction.atomic
def set_daily_rate_closed(*, property_obj, rate_id, closed, reason, actor):
    """关房/开房：只改 DailyRate.closed，不改价格，禁止改历史日。"""
    _require(actor, property_obj, "inventory.manage_rates", "缺少房价管理权限。")
    reason = str(reason or "").strip()
    if not 2 <= len(reason) <= 240:
        raise HousekeepingOperationError("关房/开房原因需为 2 至 240 个字符。")
    rate = DailyRate.objects.select_for_update().filter(pk=rate_id, property=property_obj).first()
    if rate is None:
        raise HousekeepingOperationError("每日房价不存在或不属于当前物业。")
    if rate.stay_date < _property_today(property_obj):
        raise HousekeepingOperationError("历史日期房价不可修改。")
    if rate.closed == bool(closed):
        return rate, False
    before = {"closed": rate.closed}
    rate.closed = bool(closed)
    rate.save(update_fields=("closed", "updated_at"))
    _audit(
        "inventory.daily_rate_closed",
        "DailyRate",
        rate.pk,
        actor,
        property_obj,
        before,
        {"closed": rate.closed},
        reason,
    )
    return rate, True


@transaction.atomic
def set_inventory_stop_sell(*, property_obj, inventory_id, stop_sell, reason, actor):
    """房型某日停售/开售；可售数仍由 total/held/sold/out_of_service 派生。"""
    _require(actor, property_obj, "inventory.maintenance_manage", "缺少库存维护权限。")
    reason = str(reason or "").strip()
    if not 2 <= len(reason) <= 240:
        raise HousekeepingOperationError("停售/开售原因需为 2 至 240 个字符。")
    row = (
        RoomTypeInventory.objects.select_for_update()
        .filter(pk=inventory_id, property=property_obj)
        .first()
    )
    if row is None:
        raise HousekeepingOperationError("库存行不存在或不属于当前物业。")
    if row.stay_date < _property_today(property_obj):
        raise HousekeepingOperationError("历史日期库存不可修改。")
    if row.stop_sell == bool(stop_sell):
        return row, False
    before = {"stop_sell": row.stop_sell, "available_count": row.available_count}
    row.stop_sell = bool(stop_sell)
    row.version += 1
    row.save(update_fields=("stop_sell", "version", "updated_at"))
    _audit(
        "inventory.stop_sell_changed",
        "RoomTypeInventory",
        row.pk,
        actor,
        property_obj,
        before,
        {"stop_sell": row.stop_sell, "available_count": row.available_count},
        reason,
    )
    return row, True


@transaction.atomic
def create_room_type(
    *,
    property_obj,
    code,
    name,
    capacity=2,
    bed_type="",
    amenities=None,
    actor,
    reason="",
):
    """房型主数据新增；代码物业内唯一由数据库约束兜底。"""
    _require(actor, property_obj, "hotelapp.change_room", "缺少房型资料维护权限。")
    code = str(code or "").strip().upper()
    name = str(name or "").strip()
    # MC11-INV-03：capacity 统一在此转换——非整数入参抛 ValueError
    # （视图层映射 400），不再让 str/int 比较穿透 TypeError → 500。
    try:
        capacity = int(capacity)
    except (TypeError, ValueError):
        raise ValueError("核定人数必须是正整数。") from None
    if not code or not name or len(name) > 80 or capacity < 1:
        raise HousekeepingOperationError("房型代码/名称/核定人数不合法。")
    room_type = RoomType.objects.create(
        property=property_obj,
        code=code,
        name=name,
        capacity=int(capacity),
        bed_type=str(bed_type or "")[:40],
        amenities=amenities or [],
    )
    _audit(
        "inventory.room_type_created",
        "RoomType",
        room_type.pk,
        actor,
        property_obj,
        {},
        {"code": code, "name": name},
        reason or "新增房型",
    )
    return room_type


@transaction.atomic
def update_room_type(*, property_obj, room_type_id, actor, reason="", **fields):
    """房型资料更新；代码不在此改，避免影响外部引用。"""
    _require(actor, property_obj, "hotelapp.change_room", "缺少房型资料维护权限。")
    allowed = {"name", "capacity", "bed_type", "amenities", "is_active"}
    unknown = set(fields) - allowed
    if unknown:
        raise HousekeepingOperationError("房型资料字段不在白名单：" + ",".join(sorted(unknown)))
    room_type = (
        RoomType.objects.select_for_update().filter(pk=room_type_id, property=property_obj).first()
    )
    if room_type is None:
        raise HousekeepingOperationError("房型不存在或不属于当前物业。")
    before = {}
    for field, value in fields.items():
        if getattr(room_type, field) != value:
            before[field] = getattr(room_type, field)
            setattr(room_type, field, value)
    if not before:
        return room_type, False
    room_type.save(update_fields=(*before.keys(), "updated_at"))
    _audit(
        "inventory.room_type_updated",
        "RoomType",
        room_type.pk,
        actor,
        property_obj,
        before,
        {key: getattr(room_type, key) for key in before},
        reason,
    )
    return room_type, True


@transaction.atomic
def create_room(*, property_obj, room_type_id, number, floor="", actor, reason=""):
    """物理房间新增；初始状态固定为空闲+脏房+正常，房态流转仍走专用服务。"""
    _require(actor, property_obj, "hotelapp.change_room", "缺少房间资料维护权限。")
    number = str(number or "").strip()
    if not number or len(number) > 32:
        raise HousekeepingOperationError("房号需为 1 至 32 个字符。")
    room_type = RoomType.objects.filter(pk=room_type_id, property=property_obj).first()
    if room_type is None:
        raise HousekeepingOperationError("房型不存在或不属于当前物业。")
    room = Room.objects.create(
        property=property_obj,
        room_type=room_type,
        number=number,
        floor=str(floor or "")[:16],
        occupancy_status=Room.OccupancyStatus.VACANT,
        housekeeping_status=Room.HousekeepingStatus.DIRTY,
        maintenance_status=Room.MaintenanceStatus.NORMAL,
    )
    _audit(
        "inventory.room_created",
        "Room",
        room.pk,
        actor,
        property_obj,
        {},
        {"number": number, "room_type": room_type.pk},
        reason or "新增房间",
    )
    # MC11-INV-04：新房间即时进入库存日历（可售量即时生效）
    _sync_calendar_for_room_type(room_type)
    return room


@transaction.atomic
def update_room(*, property_obj, room_id, actor, reason="", **fields):
    """房间资料更新；三维状态不得由此入口修改。"""
    _require(actor, property_obj, "hotelapp.change_room", "缺少房间资料维护权限。")
    allowed = {"floor", "room_type_id", "is_active"}
    unknown = set(fields) - allowed
    if unknown:
        raise HousekeepingOperationError("房间资料字段不在白名单：" + ",".join(sorted(unknown)))
    room = Room.objects.select_for_update().filter(pk=room_id, property=property_obj).first()
    if room is None:
        raise HousekeepingOperationError("房间不存在或不属于当前物业。")
    if (
        "room_type_id" in fields
        and not RoomType.objects.filter(pk=fields["room_type_id"], property=property_obj).exists()
    ):
        raise HousekeepingOperationError("目标房型不存在或不属于当前物业。")
    before = {}
    for field, value in fields.items():
        current = getattr(room, field)
        if current != value:
            before[field] = current
            setattr(room, field, value)
    if not before:
        return room, False
    room.save(update_fields=(*before.keys(), "updated_at"))
    _audit(
        "inventory.room_updated",
        "Room",
        room.pk,
        actor,
        property_obj,
        before,
        {key: getattr(room, key) for key in before},
        reason,
    )
    # MC11-INV-04：房型迁移/停用即时反映到库存日历（换房型/停用影响
    # 新旧两个房型的可售量）
    if "room_type_id" in before or "is_active" in before:
        # room 为 Room 实例；room_type_id 由 FK 生成（mypy 无 django-stubs
        # 不识别 FK _id 属性，运行时恒存在）
        _sync_calendar_for_room_type(
            RoomType.objects.get(pk=room.room_type_id)  # type: ignore[attr-defined]
        )
        if "room_type_id" in before and before["room_type_id"]:
            old_type = RoomType.objects.filter(pk=before["room_type_id"]).first()
            if old_type is not None:
                _sync_calendar_for_room_type(old_type)
    return room, True
