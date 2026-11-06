# -*- coding: utf-8 -*-
"""PMS2 权威数据仪表盘服务（P1-11：/home/ 统计切权威新域）。

所有指标从 PMS2 模型聚合（Reservation/Stay/Room/Folio/Guest/GuestMembership）；
旧 hotelapp 模型数据标记为 LEGACY 单独区域。
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.billing.models import LedgerEntry, Payment, Refund
from apps.guests.models import Guest, GuestMembership
from apps.inventory.models import Room, RoomType
from apps.reservations.models import Reservation, Stay


def dashboard_data(property_obj, today=None):
    """PMS2 权威仪表盘聚合（前台 + 后台共用同一数据服务）。"""
    if today is None:
        today = timezone.localdate()

    # --- 预订 ---
    res_qs = Reservation.objects.filter(property=property_obj)
    pending = res_qs.filter(status=Reservation.Status.PENDING).count()
    confirmed = res_qs.filter(status=Reservation.Status.CONFIRMED).count()
    arrivals_today = res_qs.filter(
        status__in=(Reservation.Status.PENDING, Reservation.Status.CONFIRMED),
        check_in=today).count()
    departures_today = Stay.objects.filter(
        property=property_obj, status=Stay.Status.IN_HOUSE,
        actual_check_out__date=today).count()

    # --- 在住 ---
    in_house = Stay.objects.filter(
        property=property_obj, status=Stay.Status.IN_HOUSE).count()

    # --- 房态（三维） ---
    rooms = Room.objects.filter(property=property_obj, is_active=True)
    vacant_clean = rooms.filter(
        occupancy_status=Room.OccupancyStatus.VACANT,
        housekeeping_status=Room.HousekeepingStatus.CLEAN,
        maintenance_status=Room.MaintenanceStatus.NORMAL).count()
    occupied = rooms.filter(
        occupancy_status=Room.OccupancyStatus.OCCUPIED).count()
    dirty = rooms.filter(
        housekeeping_status=Room.HousekeepingStatus.DIRTY).count()
    cleaning = rooms.filter(
        housekeeping_status=Room.HousekeepingStatus.CLEANING).count()
    inspecting = rooms.filter(
        housekeeping_status=Room.HousekeepingStatus.INSPECTING).count()
    maintenance = rooms.filter(
        ~Q(maintenance_status=Room.MaintenanceStatus.NORMAL)).count()
    total_rooms = rooms.count()

    # --- 财务（PMS2 Ledger） ---
    ledger_qs = LedgerEntry.objects.filter(property=property_obj)
    total_revenue = ledger_qs.filter(amount__gt=0).aggregate(
        s=Sum("amount"))["s"] or Decimal("0.00")
    total_expense = abs(ledger_qs.filter(amount__lt=0).aggregate(
        s=Sum("amount"))["s"] or Decimal("0.00"))
    pending_payments = Payment.objects.filter(
        property=property_obj, status=Payment.Status.PENDING).count()
    pending_refunds = Refund.objects.filter(
        property=property_obj, status=Refund.Status.PENDING).count()

    # --- 客人与会员 ---
    guest_count = Guest.objects.filter(property=property_obj).count()
    member_count = GuestMembership.objects.filter(
        property=property_obj, status=GuestMembership.Status.ACTIVE).count()

    # --- 预警 ---
    overdue_reservations = res_qs.filter(
        status__in=(Reservation.Status.PENDING, Reservation.Status.CONFIRMED),
        check_in__lt=today).count()

    return {
        "source_system": "PMS2",
        "business_date": str(today),
        "property_code": property_obj.code,
        "reservations": {
            "pending": pending, "confirmed": confirmed,
            "arrivals_today": arrivals_today,
            "departures_today": departures_today,
            "overdue": overdue_reservations,
        },
        "rooms": {
            "total": total_rooms, "vacant_clean": vacant_clean,
            "occupied": occupied, "dirty": dirty,
            "cleaning": cleaning, "inspecting": inspecting,
            "maintenance": maintenance,
        },
        "finance": {
            "total_revenue": str(total_revenue),
            "total_expense": str(total_expense),
            "net": str(total_revenue - total_expense),
            "pending_payments": pending_payments,
            "pending_refunds": pending_refunds,
        },
        "guests": {"total": guest_count, "members": member_count},
    }


def dashboard_charts(property_obj, today=None):
    """主界面图表数据（PMS2 权威模型聚合；全部中文标签）。

    修复 P1-11 遗留：/home/ 视图切了 dashboard_data 但模板仍在消费
    旧扁平键与旧图表数据，导致统计卡与全部图表为空。
    """
    from apps.core.labels import (
        member_level_label, choice_labels)
    from apps.guests.models import Guest, GuestMembership
    from apps.inventory.models import Room, RoomType

    if today is None:
        today = timezone.localdate()
    maps = {
        "occ": choice_labels(Room.OccupancyStatus),
        "hk": choice_labels(Room.HousekeepingStatus),
        "mt": choice_labels(Room.MaintenanceStatus),
    }
    rooms = Room.objects.filter(property=property_obj, is_active=True)

    # --- 房间状态分布（占用×清洁×维修 归并为可读六类） ---
    vacant_clean = rooms.filter(
        occupancy_status=Room.OccupancyStatus.VACANT,
        housekeeping_status=Room.HousekeepingStatus.CLEAN,
        maintenance_status=Room.MaintenanceStatus.NORMAL).count()
    occupied = rooms.filter(occupancy_status=Room.OccupancyStatus.OCCUPIED).count()
    dirty = rooms.filter(
        housekeeping_status=Room.HousekeepingStatus.DIRTY,
        occupancy_status=Room.OccupancyStatus.VACANT).count()
    cleaning = rooms.filter(
        housekeeping_status__in=(Room.HousekeepingStatus.CLEANING,
                                 Room.HousekeepingStatus.INSPECTING)).count()
    maintenance = rooms.filter(
        ~Q(maintenance_status=Room.MaintenanceStatus.NORMAL)).count()
    other = max(0, rooms.count() - vacant_clean - occupied - dirty - cleaning - maintenance)
    room_status_counts = [
        {"name": "空闲净房", "value": vacant_clean},
        {"name": maps["occ"]["OCCUPIED"], "value": occupied},
        {"name": maps["hk"]["DIRTY"], "value": dirty},
        {"name": "清洁中/待检查", "value": cleaning},
        {"name": "维修/停售", "value": maintenance},
    ]
    if other:
        room_status_counts.append({"name": "其他", "value": other})

    # --- 房型库存分布（物理房数 vs 今日可售） ---
    room_type_inventory = []
    for rt in RoomType.objects.filter(property=property_obj, is_active=True):
        total = rooms.filter(room_type=rt).count()
        available = rooms.filter(
            room_type=rt,
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL).count()
        room_type_inventory.append({"name": rt.name, "total": total, "available": available})

    # --- 会员等级分布（中文标签） ---
    vip_counts = [
        {"name": member_level_label(m["level_code"]), "value": m["c"]}
        for m in GuestMembership.objects.filter(
            property=property_obj, status=GuestMembership.Status.ACTIVE)
        .values("level_code").annotate(c=Count("level_code"))
    ]

    # --- 顾客证件登记（GuestDocument 已登记 vs 未登记） ---
    guest_total = Guest.objects.filter(property=property_obj).count()
    with_doc = Guest.objects.filter(
        property=property_obj, documents__isnull=False).distinct().count()
    client_dash = [
        {"name": "已登记证件", "value": with_doc},
        {"name": "未登记证件", "value": max(0, guest_total - with_doc)},
    ]
    return {
        "room_status_counts": room_status_counts,
        "room_type_inventory": room_type_inventory,
        "vip_counts": vip_counts,
        "client_dash": client_dash,
    }


def dashboard_warnings(property_obj, today=None):
    """预约预警（PMS2）：过期未入住 + 今日预抵。"""
    if today is None:
        today = timezone.localdate()
    overdue = Reservation.objects.filter(
        property=property_obj,
        status__in=(Reservation.Status.PENDING, Reservation.Status.CONFIRMED),
        check_in__lt=today).select_related("guest").order_by("-check_in", "-id")[:10]
    return [{
        "level": "过期未入住",
        "guest": r.guest.full_name,
        "tel": (r.guest.phone[:3] + "****" + r.guest.phone[-4:]
                if len(r.guest.phone or "") >= 8 else (r.guest.phone or "—")),
        "date": str(r.check_in),
    } for r in overdue]


def dashboard_finance(property_obj, today=None):
    """主界面消费统计（PMS2 Ledger/Payment；仅财务/管理岗可见）。"""
    from apps.billing.models import LedgerEntry, Payment
    if today is None:
        today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    year_start = today.replace(month=1, day=1)

    def _sum(qs, field, positive):
        agg = qs.aggregate(s=Sum(field))["s"]
        v = agg or Decimal("0.00")
        return abs(v)

    led = LedgerEntry.objects.filter(property=property_obj)
    fin = {}
    for key, start in (("today", today), ("week", week_start), ("year", year_start)):
        qs = led.filter(created_at__date__gte=start)
        fin[key + "_in"] = _sum(qs.filter(amount__gt=0), "amount", True)
        fin[key + "_out"] = _sum(qs.filter(amount__lt=0), "amount", False)
    fin["settled"] = _sum(
        Payment.objects.filter(property=property_obj,
                               status=Payment.Status.SUCCEEDED),
        "amount", True)
    return {k: str(v) for k, v in fin.items()}
