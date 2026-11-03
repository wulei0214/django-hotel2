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
