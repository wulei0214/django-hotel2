"""房型按日库存服务：查房、hold、确认、释放、过期清理。"""
import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import Accommodation, Reservation, Room, RoomTypeInventory


class InventoryError(Exception):
    """库存不足或参数非法。"""


def stay_dates(check_in, check_out):
    if not check_in or not check_out or check_out <= check_in:
        raise InventoryError("离店日期必须晚于入住日期")
    current = check_in
    while current < check_out:
        yield current
        current += datetime.timedelta(days=1)


def room_type_price(room_type):
    prices = [Decimal(str(p)) for p in Room.objects.filter(客房类型=room_type)
              .values_list("价格", flat=True)]
    if not prices:
        raise InventoryError("房型不存在")
    return min(prices)


def room_type_total(room_type):
    return Room.objects.filter(客房类型=room_type).count()


def _ensure_row(room_type, stay_date):
    total = room_type_total(room_type)
    row, created = RoomTypeInventory.objects.get_or_create(
        room_type=room_type,
        stay_date=stay_date,
        defaults={"total_rooms": total},
    )
    if not created and row.total_rooms != total and total >= row.sold + row.held:
        row.total_rooms = total
        row.save(update_fields=["total_rooms", "updated_at"])
    return row


def _legacy_blocked(room_type, stay_date):
    """旧房单（没有 reservation 关联）也占用库存，避免新旧数据双卖。"""
    return Accommodation.objects.filter(
        Q(状态__in=("待确认", "已确认", "已入住")),
        Q(客房编号__客房类型=room_type),
        Q(reservation__isnull=True),
    ).filter(
        Q(入住日期__lte=stay_date, 离店日期__gt=stay_date)
        | Q(入住日期=stay_date, 离店日期__isnull=True)
    ).count()


def _available_without_lock(room_type, stay_date):
    row = _ensure_row(room_type, stay_date)
    return max(0, row.available - _legacy_blocked(room_type, stay_date)), row


def availability(room_type, check_in, check_out):
    """返回区间内每日可售房量的最小值。"""
    expire_stale_holds()
    counts = []
    for day in stay_dates(check_in, check_out):
        available, _ = _available_without_lock(room_type, day)
        counts.append(available)
    return min(counts) if counts else 0


def hold_inventory(reservation):
    """在事务内为预订占用库存；调用方必须已开启 transaction.atomic。"""
    for day in stay_dates(reservation.check_in, reservation.check_out):
        row = _ensure_row(reservation.room_type, day)
        row = RoomTypeInventory.objects.select_for_update().get(pk=row.pk)
        available = row.available - _legacy_blocked(reservation.room_type, day)
        if available < reservation.rooms:
            raise InventoryError(f"{day} 可售房量不足（剩余 {available} 间）")
        row.held += reservation.rooms
        row.save(update_fields=["held", "updated_at"])


def convert_hold_to_sold(reservation):
    for day in stay_dates(reservation.check_in, reservation.check_out):
        row = RoomTypeInventory.objects.select_for_update().get(
            room_type=reservation.room_type, stay_date=day)
        if row.held < reservation.rooms or row.sold + reservation.rooms > row.total_rooms:
            raise InventoryError("库存占用状态异常，无法确认预订")
        row.held -= reservation.rooms
        row.sold += reservation.rooms
        row.save(update_fields=["held", "sold", "updated_at"])


def release_hold(reservation):
    for day in stay_dates(reservation.check_in, reservation.check_out):
        row = RoomTypeInventory.objects.select_for_update().filter(
            room_type=reservation.room_type, stay_date=day).first()
        if not row:
            continue
        row.held = max(0, row.held - reservation.rooms)
        row.save(update_fields=["held", "updated_at"])


def expire_stale_holds(now=None):
    """把过期待支付预订标记为 EXPIRED 并释放 hold。"""
    now = now or timezone.now()
    stale = list(Reservation.objects.filter(
        status=Reservation.Status.PENDING, expires_at__lt=now).values_list("pk", flat=True))
    if not stale:
        return 0
    count = 0
    with transaction.atomic():
        for pk in stale:
            reservation = Reservation.objects.select_for_update().filter(pk=pk).first()
            if not reservation or reservation.status != Reservation.Status.PENDING:
                continue
            release_hold(reservation)
            reservation.status = Reservation.Status.EXPIRED
            reservation.save(update_fields=["status", "updated_at"])
            count += 1
    return count
