"""前台接待工作台只读查询 API。

本模块只负责把 PMS 2.0 预订、入住、房间和账单组合为前台 DTO。
不执行排房/入住/续住/换房/收款/退房写操作；这些操作须由后续事务化领域服务实现。
"""
import json
from datetime import date
from decimal import Decimal
from functools import wraps
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.billing.models import Folio, Payment
from apps.identity.authorization import AuthorizationService
from apps.inventory.models import Room
from apps.reservations.models import Reservation, ReservationRoom, Stay
from apps.reservations.front_desk_services import (
    FrontDeskOperationError,
    assign_room as assign_room_service,
    check_in_stay,
    collect_manual_payment,
    check_out_stay,
)

from .views import api_guard


PAGE_DEFAULT = 20
PAGE_MAX = 100
ARRIVAL_STATUSES = (Reservation.Status.PENDING, Reservation.Status.CONFIRMED)


def _response(data, status=200):
    response = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store, private"
    response["Vary"] = "Cookie"
    return response


def _front_desk_required(view):
    """员工门禁 + DEFAULT 物业 + 入住管理查看权限，统一返回 JSON 错误。"""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated or not user.is_active:
            return _response({"error": {"code": "AUTHENTICATION_REQUIRED", "message": "请先登录员工账号。"}}, 401)
        property_obj = getattr(request, "property", None)
        membership = getattr(request, "staff_membership", None)
        if property_obj is None or property_obj.code != getattr(settings, "DEFAULT_PROPERTY_CODE", "DEFAULT"):
            return _response({"error": {"code": "PROPERTY_UNAVAILABLE", "message": "当前酒店物业不可用。"}}, 503)
        if membership is None or membership.property_id != property_obj.pk:
            return _response({"error": {"code": "EMPLOYEE_MEMBERSHIP_REQUIRED", "message": "当前账号没有该酒店的有效任职关系。"}}, 403)
        if not AuthorizationService.has_permission(user, property_obj, "hotelapp.view_accommodation"):
            return _response({"error": {"code": "PERMISSION_DENIED", "message": "当前岗位没有入住管理查看权限。"}}, 403)
        request.front_desk_property = property_obj
        return view(request, *args, **kwargs)
    return wrapped



def _front_desk_write_required(view):
    """写操作要求当前物业任职同时具备入住查看与修改权限。"""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated or not user.is_active:
            return _response({"error": {"code": "AUTHENTICATION_REQUIRED", "message": "请先登录员工账号。"}}, 401)
        property_obj = getattr(request, "property", None)
        membership = getattr(request, "staff_membership", None)
        if property_obj is None or property_obj.code != getattr(settings, "DEFAULT_PROPERTY_CODE", "DEFAULT"):
            return _response({"error": {"code": "PROPERTY_UNAVAILABLE", "message": "当前酒店物业不可用。"}}, 503)
        if membership is None or membership.property_id != property_obj.pk:
            return _response({"error": {"code": "EMPLOYEE_MEMBERSHIP_REQUIRED", "message": "当前账号没有该酒店的有效任职关系。"}}, 403)
        if not (
            AuthorizationService.has_permission(user, property_obj, "hotelapp.view_accommodation")
            and AuthorizationService.has_permission(user, property_obj, "hotelapp.change_accommodation")
        ):
            return _response({"error": {"code": "PERMISSION_DENIED", "message": "当前岗位没有入住管理操作权限。"}}, 403)
        request.front_desk_property = property_obj
        return view(request, *args, **kwargs)
    return wrapped


def _body(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("请求体必须是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象。")
    return payload

def _business_date(property_obj):
    try:
        tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return timezone.localdate(timezone.now(), tz)


def _parse_date(value, label, fallback):
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须使用 YYYY-MM-DD 格式。")


def _page_args(request):
    try:
        page = max(1, int(request.GET.get("page", "1")))
        page_size = int(request.GET.get("page_size", str(PAGE_DEFAULT)))
    except (TypeError, ValueError):
        raise ValueError("page 和 page_size 必须是整数。")
    if page_size < 1 or page_size > PAGE_MAX:
        raise ValueError(f"page_size 必须在 1 至 {PAGE_MAX} 之间。")
    return page, page_size


def _paginate(queryset, page, page_size):
    total = queryset.count()
    rows = list(queryset[(page - 1) * page_size: page * page_size])
    return rows, {
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_previous": page > 1,
        "has_next": page * page_size < total,
    }


def _mask_phone(phone):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 7:
        return "已隐藏" if phone else "—"
    return f"{digits[:3]}****{digits[-4:]}"


def _status_label(status):
    return {
        Reservation.Status.PENDING: "待支付/待确认",
        Reservation.Status.CONFIRMED: "已确认",
        Reservation.Status.CANCELLED: "已取消",
        Reservation.Status.EXPIRED: "已过期",
        Reservation.Status.CHECKED_IN: "已入住",
        Reservation.Status.CHECKED_OUT: "已退房",
        Reservation.Status.NO_SHOW: "未到店",
    }.get(status, status)


def _source_label(source):
    return {"WEBSITE": "官网预订", "WALK_IN": "散客直入", "OTA": "渠道预订"}.get(source, source or "其他")


def _folio_summaries(property_obj, reservation_ids):
    """按预订批量读取账单摘要；不向前台列表泄漏完整账本流水。"""
    if not reservation_ids:
        return {}
    result = {}
    folios = Folio.objects.filter(
        property=property_obj, reservation_id__in=reservation_ids
    ).prefetch_related("entries", "payments")
    for folio in folios:
        entries = list(folio.entries.all())
        charges = sum((entry.amount for entry in entries if entry.amount > 0), Decimal("0.00"))
        credits = sum((-entry.amount for entry in entries if entry.amount < 0), Decimal("0.00"))
        balance = sum((entry.amount for entry in entries), Decimal("0.00"))
        pending_payments = sum(
            1 for item in folio.payments.all()
            if item.status in (Payment.Status.CREATED, Payment.Status.PENDING, Payment.Status.REFUND_PENDING)
        )
        result[folio.reservation_id] = {
            "folio_id": folio.pk,
            "status": folio.status,
            "charges": str(charges.quantize(Decimal("0.01"))),
            "credits": str(credits.quantize(Decimal("0.01"))),
            "balance": str(balance.quantize(Decimal("0.01"))),
            "pending_payment_count": pending_payments,
            "currency": folio.currency,
        }
    return result


def _empty_folio_summary(currency="CNY"):
    return {
        "folio_id": None, "status": "NOT_OPENED",
        "charges": "0.00", "credits": "0.00", "balance": "0.00",
        "pending_payment_count": 0, "currency": currency,
    }


def _reservation_room_dto(line, folio_map=None, stay_map=None):
    reservation = line.reservation
    room = line.assigned_room
    stay = (stay_map or {}).get(line.pk)
    return {
        "reservation_room_id": line.pk,
        "booking_no": reservation.booking_no,
        "reservation_status": reservation.status,
        "reservation_status_label": _status_label(reservation.status),
        "source": reservation.source,
        "source_label": _source_label(reservation.source),
        "guest": {
            "id": reservation.guest_id,
            "full_name": reservation.guest.full_name,
            "phone_masked": _mask_phone(reservation.guest.phone),
        },
        "room_type": {"id": line.room_type_id, "name": line.room_type.name},
        "assigned_room": ({"id": room.pk, "number": room.number, "floor": room.floor} if room else None),
        "check_in": line.check_in.isoformat(),
        "check_out": line.check_out.isoformat(),
        "nights": (line.check_out - line.check_in).days,
        "adults": reservation.adult_count,
        "children": reservation.child_count,
        "amount": str(line.amount),
        "folio_summary": (folio_map or {}).get(reservation.pk, _empty_folio_summary(reservation.currency)),
        "stay": ({
            "id": stay.pk,
            "status": stay.status,
            "actual_check_in": stay.actual_check_in.isoformat() if stay.actual_check_in else None
        } if stay else None),
        "source_system": "PMS2",
    }


def _apply_search(queryset, value):
    value = (value or "").strip()
    if not value:
        return queryset
    if len(value) > 80:
        raise ValueError("搜索内容不能超过 80 个字符。")
    digits = "".join(ch for ch in value if ch.isdigit())
    conditions = (
        Q(reservation__booking_no__icontains=value)
        | Q(reservation__guest__full_name__icontains=value)
        | Q(reservation__guest__phone__icontains=value)
        | Q(reservation__guest__phone_normalized__icontains=digits or value)
        | Q(assigned_room__number__icontains=value)
    )
    return queryset.filter(conditions)


@require_GET
@api_guard
@_front_desk_required
def overview(request):
    property_obj = request.front_desk_property
    try:
        day = _parse_date(
            request.GET.get("date"), "业务日期", _business_date(property_obj)
        )
    except ValueError as exc:
        return _response(
            {"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400
        )
    arrivals = ReservationRoom.objects.filter(
        property=property_obj, check_in=day, reservation__status__in=ARRIVAL_STATUSES
    )
    departures_qs = Stay.objects.filter(
        property=property_obj, status=Stay.Status.IN_HOUSE, reservation_room__check_out=day
    )
    rooms_qs = Room.objects.filter(property=property_obj, is_active=True)
    clean_available = rooms_qs.filter(
        occupancy_status=Room.OccupancyStatus.VACANT,
        housekeeping_status=Room.HousekeepingStatus.CLEAN,
        maintenance_status=Room.MaintenanceStatus.NORMAL,
    ).count()
    waiting_release = rooms_qs.filter(housekeeping_status=Room.HousekeepingStatus.INSPECTING).count()
    maintenance = rooms_qs.exclude(maintenance_status=Room.MaintenanceStatus.NORMAL).count()
    return _response({"data": {
        "property": {"code": property_obj.code, "name": property_obj.name},
        "business_date": day.isoformat(),
        "arrivals_count": arrivals.count(),
        "unassigned_count": arrivals.filter(assigned_room__isnull=True).count(),
        "departures_count": departures_qs.count(),
        "in_house_count": Stay.objects.filter(property=property_obj, status=Stay.Status.IN_HOUSE).count(),
        "clean_available_count": clean_available,
        "waiting_release_count": waiting_release,
        "maintenance_count": maintenance,
        "exception_counts": {
            "waiting_room_assignment": arrivals.filter(assigned_room__isnull=True).count(),
            "waiting_manager_release": waiting_release,
            "maintenance_rooms": maintenance,
        },
        "data_updated_at": timezone.now().isoformat(),
    }})


@require_GET
@api_guard
@_front_desk_required
def arrivals(request):
    property_obj = request.front_desk_property
    try:
        day = _parse_date(request.GET.get("date"), "抵店日期", _business_date(property_obj))
        page, page_size = _page_args(request)
        queryset = ReservationRoom.objects.filter(
            property=property_obj, check_in=day, reservation__status__in=ARRIVAL_STATUSES
        ).select_related("reservation__guest", "room_type", "assigned_room").order_by("reservation__created_at", "id")
        queryset = _apply_search(queryset, request.GET.get("q"))
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    assigned = request.GET.get("assigned", "all")
    if assigned == "yes":
        queryset = queryset.filter(assigned_room__isnull=False)
    elif assigned == "no":
        queryset = queryset.filter(assigned_room__isnull=True)
    elif assigned != "all":
        return _response({"error": {"code": "VALIDATION_ERROR", "message": "assigned 只能是 all/yes/no。"}}, 400)
    rows, meta = _paginate(queryset, page, page_size)
    reservation_ids = {line.reservation_id for line in rows}
    folio_map = _folio_summaries(property_obj, reservation_ids)
    data = [_reservation_room_dto(line, folio_map) for line in rows]
    return _response({"data": data, "meta": {**meta, "date": day.isoformat(), "source_system": "PMS2"}})


@require_GET
@api_guard
@_front_desk_required
def departures(request):
    property_obj = request.front_desk_property
    try:
        day = _parse_date(request.GET.get("date"), "离店日期", _business_date(property_obj))
        page, page_size = _page_args(request)
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    queryset = Stay.objects.filter(
        property=property_obj, status=Stay.Status.IN_HOUSE, reservation_room__check_out=day
    ).select_related(
        "reservation_room__reservation__guest", "reservation_room__room_type", "room"
    ).order_by("reservation_room__check_out", "id")
    value = (request.GET.get("q") or "").strip()
    if value:
        if len(value) > 80:
            return _response({"error": {"code": "VALIDATION_ERROR", "message": "搜索内容不能超过 80 个字符。"}}, 400)
        queryset = queryset.filter(
            Q(reservation_room__reservation__booking_no__icontains=value)
            | Q(reservation_room__reservation__guest__full_name__icontains=value)
            | Q(reservation_room__reservation__guest__phone__icontains=value)
            | Q(room__number__icontains=value)
        )
    rows, meta = _paginate(queryset, page, page_size)
    reservation_ids = {stay.reservation_room.reservation_id for stay in rows}
    folio_map = _folio_summaries(property_obj, reservation_ids)
    stay_map = {stay.reservation_room_id: stay for stay in rows}
    data = [_reservation_room_dto(stay.reservation_room, folio_map, stay_map) for stay in rows]
    return _response({"data": data, "meta": {**meta, "date": day.isoformat(), "source_system": "PMS2"}})


@require_GET
@api_guard
@_front_desk_required
def in_house(request):
    property_obj = request.front_desk_property
    try:
        page, page_size = _page_args(request)
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    queryset = Stay.objects.filter(
        property=property_obj, status=Stay.Status.IN_HOUSE
    ).select_related(
        "reservation_room__reservation__guest", "reservation_room__room_type", "room"
    ).order_by("room__number", "id")
    value = (request.GET.get("q") or "").strip()
    if value:
        if len(value) > 80:
            return _response({"error": {"code": "VALIDATION_ERROR", "message": "搜索内容不能超过 80 个字符。"}}, 400)
        queryset = queryset.filter(
            Q(reservation_room__reservation__booking_no__icontains=value)
            | Q(reservation_room__reservation__guest__full_name__icontains=value)
            | Q(reservation_room__reservation__guest__phone__icontains=value)
            | Q(room__number__icontains=value)
        )
    room_type_id = request.GET.get("room_type_id")
    if room_type_id:
        try:
            queryset = queryset.filter(reservation_room__room_type_id=int(room_type_id))
        except ValueError:
            return _response({"error": {"code": "VALIDATION_ERROR", "message": "room_type_id 必须是整数。"}}, 400)
    floor = (request.GET.get("floor") or "").strip()
    if floor:
        queryset = queryset.filter(room__floor=floor)
    rows, meta = _paginate(queryset, page, page_size)
    reservation_ids = {stay.reservation_room.reservation_id for stay in rows}
    folio_map = _folio_summaries(property_obj, reservation_ids)
    stay_map = {stay.reservation_room_id: stay for stay in rows}
    data = [_reservation_room_dto(stay.reservation_room, folio_map, stay_map) for stay in rows]
    return _response({"data": data, "meta": meta})


@require_GET
@api_guard
@_front_desk_required
def reservations(request):
    property_obj = request.front_desk_property
    try:
        page, page_size = _page_args(request)
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    queryset = Reservation.objects.filter(property=property_obj).select_related("guest").order_by("-created_at")
    value = (request.GET.get("q") or "").strip()
    if value:
        if len(value) > 80:
            return _response({"error": {"code": "VALIDATION_ERROR", "message": "搜索内容不能超过 80 个字符。"}}, 400)
        digits = "".join(ch for ch in value if ch.isdigit()) or value
        queryset = queryset.filter(
            Q(booking_no__icontains=value)
            | Q(guest__full_name__icontains=value)
            | Q(guest__phone__icontains=value)
            | Q(guest__phone_normalized__icontains=digits)
        )
    status = (request.GET.get("status") or "").strip().upper()
    valid_statuses = {choice[0] for choice in Reservation.Status.choices}
    if status:
        if status not in valid_statuses:
            return _response({"error": {"code": "VALIDATION_ERROR", "message": "订单状态无效。"}}, 400)
        queryset = queryset.filter(status=status)
    source = (request.GET.get("source") or "").strip().upper()
    if source:
        queryset = queryset.filter(source=source)
    rows, meta = _paginate(queryset, page, page_size)
    folio_map = _folio_summaries(property_obj, {item.pk for item in rows})
    data = []
    for reservation in rows:
        lines = list(reservation.reservation_rooms.select_related("room_type", "assigned_room"))
        data.append({
            "booking_no": reservation.booking_no,
            "status": reservation.status,
            "status_label": _status_label(reservation.status),
            "source": reservation.source,
            "source_label": _source_label(reservation.source),
            "guest": {"id": reservation.guest_id, "full_name": reservation.guest.full_name, "phone_masked": _mask_phone(reservation.guest.phone)},
            "check_in": reservation.check_in.isoformat(),
            "check_out": reservation.check_out.isoformat(),
            "nights": (reservation.check_out - reservation.check_in).days,
            "room_count": reservation.room_count,
            "adult_count": reservation.adult_count,
            "child_count": reservation.child_count,
            "total_amount": str(reservation.total_amount),
            "currency": reservation.currency,
            "rooms": [{"id": line.pk, "room_type": line.room_type.name, "assigned_room": line.assigned_room.number if line.assigned_room else None} for line in lines],
            "folio_summary": folio_map.get(reservation.pk, _empty_folio_summary(reservation.currency)),
            "source_system": "PMS2",
        })
    return _response({"data": data, "meta": meta})


@require_GET
@api_guard
@_front_desk_required
def reservation_detail(request, booking_no):
    property_obj = request.front_desk_property
    reservation = Reservation.objects.select_related("guest").filter(
        property=property_obj, booking_no=booking_no
    ).first()
    if reservation is None:
        return _response({"error": {"code": "RESOURCE_NOT_FOUND", "message": "未找到该预订。"}}, 404)
    lines = list(reservation.reservation_rooms.select_related("room_type", "assigned_room").order_by("id"))
    folio_map = _folio_summaries(property_obj, {reservation.pk})
    stays = list(Stay.objects.filter(property=property_obj, reservation_room__reservation=reservation).select_related("room"))
    stay_map = {stay.reservation_room_id: stay for stay in stays}
    folio_summary = folio_map.get(reservation.pk, _empty_folio_summary(reservation.currency))
    return _response({"data": {
        "booking_no": reservation.booking_no,
        "status": reservation.status,
        "status_label": _status_label(reservation.status),
        "source": reservation.source,
        "source_label": _source_label(reservation.source),
        "guest": {
            "id": reservation.guest_id,
            "full_name": reservation.guest.full_name,
            "phone_masked": _mask_phone(reservation.guest.phone),
        },
        "check_in": reservation.check_in.isoformat(),
        "check_out": reservation.check_out.isoformat(),
        "nights": (reservation.check_out - reservation.check_in).days,
        "room_count": reservation.room_count,
        "adult_count": reservation.adult_count,
        "child_count": reservation.child_count,
        "total_amount": str(reservation.total_amount),
        "currency": reservation.currency,
        "rooms": [_reservation_room_dto(line, folio_map, stay_map) for line in lines],
        "folio_summary": folio_summary,
        "timeline": {
            "created_at": reservation.created_at.isoformat(),
            "updated_at": reservation.updated_at.isoformat(),
            "stays": [{"id": stay.pk, "status": stay.status, "created_at": stay.created_at.isoformat(), "actual_check_in": stay.actual_check_in.isoformat() if stay.actual_check_in else None, "actual_check_out": stay.actual_check_out.isoformat() if stay.actual_check_out else None} for stay in stays],
        },
        "source_system": "PMS2",
        "allowed_actions": ["view_detail"],
    }})


@require_GET
@api_guard
@_front_desk_required
def rooms(request):
    property_obj = request.front_desk_property
    queryset = Room.objects.filter(property=property_obj, is_active=True).select_related("room_type").order_by("floor", "number")
    floor = (request.GET.get("floor") or "").strip()
    if floor:
        queryset = queryset.filter(floor=floor)
    readiness = (request.GET.get("readiness") or "").strip().upper()
    if readiness == "READY":
        queryset = queryset.filter(occupancy_status=Room.OccupancyStatus.VACANT, housekeeping_status=Room.HousekeepingStatus.CLEAN, maintenance_status=Room.MaintenanceStatus.NORMAL)
    elif readiness == "INSPECTING":
        queryset = queryset.filter(housekeeping_status=Room.HousekeepingStatus.INSPECTING)
    elif readiness == "MAINTENANCE":
        queryset = queryset.exclude(maintenance_status=Room.MaintenanceStatus.NORMAL)
    elif readiness not in ("", "ALL"):
        return _response({"error": {"code": "VALIDATION_ERROR", "message": "readiness 参数无效。"}}, 400)
    data = [{
        "id": room.pk,
        "number": room.number,
        "floor": room.floor,
        "room_type": {"id": room.room_type_id, "name": room.room_type.name},
        "occupancy_status": room.occupancy_status,
        "housekeeping_status": room.housekeeping_status,
        "maintenance_status": room.maintenance_status,
        "is_ready": room.is_sellable_now,
    } for room in queryset]
    return _response({"data": data, "meta": {"total": len(data)}})


@require_GET
@api_guard
@_front_desk_required
def assignable_rooms(request):
    property_obj = request.front_desk_property
    try:
        line_id = int(request.GET.get("reservation_room_id", ""))
    except (TypeError, ValueError):
        return _response({"error": {"code": "VALIDATION_ERROR", "message": "reservation_room_id 必须是整数。"}}, 400)
    line = ReservationRoom.objects.select_related("reservation", "room_type").filter(
        pk=line_id, property=property_obj, reservation__property=property_obj
    ).first()
    if line is None:
        return _response({"error": {"code": "RESOURCE_NOT_FOUND", "message": "未找到该预订房间明细。"}}, 404)
    if line.assigned_room_id:
        return _response({"data": [], "meta": {"reason": "该房间明细已经分配房间。"}})
    if line.reservation.status not in (
        Reservation.Status.CONFIRMED, Reservation.Status.CHECKED_IN
    ):
        return _response({"data": [], "meta": {"reason": "仅已确认预订可以排房。"}})
    candidates = Room.objects.filter(
        property=property_obj, room_type=line.room_type, is_active=True,
        occupancy_status=Room.OccupancyStatus.VACANT,
        housekeeping_status=Room.HousekeepingStatus.CLEAN,
        maintenance_status=Room.MaintenanceStatus.NORMAL,
    ).exclude(
        reservation_rooms__property=property_obj,
        reservation_rooms__check_in__lt=line.check_out,
        reservation_rooms__check_out__gt=line.check_in,
        reservation_rooms__reservation__status__in=(Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.CHECKED_IN),
    ).order_by("floor", "number").distinct()
    data = [{"id": room.pk, "number": room.number, "floor": room.floor, "room_type": room.room_type.name} for room in candidates]
    return _response({"data": data, "meta": {"total": len(data), "reservation_room_id": line.pk}})


@require_GET
@api_guard
@_front_desk_required
def folio_detail(request, booking_no):
    property_obj = request.front_desk_property
    reservation = Reservation.objects.filter(property=property_obj, booking_no=booking_no).first()
    if reservation is None:
        return _response({"error": {"code": "RESOURCE_NOT_FOUND", "message": "未找到该预订。"}}, 404)
    folio = Folio.objects.filter(property=property_obj, reservation=reservation).prefetch_related("entries", "payments").first()
    if folio is None:
        return _response({"data": {"folio": None, "summary": _empty_folio_summary(reservation.currency), "entries": [], "payments": []}})
    entries = list(folio.entries.order_by("created_at", "id"))
    charges = sum((item.amount for item in entries if item.amount > 0), Decimal("0.00"))
    credits = sum((-item.amount for item in entries if item.amount < 0), Decimal("0.00"))
    balance = sum((item.amount for item in entries), Decimal("0.00"))
    summary = {"folio_id": folio.pk, "status": folio.status, "charges": str(charges), "credits": str(credits), "balance": str(balance), "currency": folio.currency}
    return _response({
        "data": {
            "summary": summary,
            "entries": [{"id": item.pk, "entry_type": item.entry_type, "amount": str(item.amount), "currency": item.currency, "business_date": item.business_date.isoformat(), "reference": item.reference, "description": item.description, "created_at": item.created_at.isoformat()} for item in entries],
            "payments": [{"provider": item.provider, "provider_ref": item.provider_ref, "method": item.method, "amount": str(item.amount), "status": item.status, "paid_at": item.paid_at.isoformat() if item.paid_at else None} for item in folio.payments.order_by("-created_at")],
        }
    })


@api_guard
@require_POST
@_front_desk_write_required
def assign_room(request, reservation_room_id):
    try:
        body = _body(request)
        room_id = int(body.get("room_id"))
        line, changed = assign_room_service(
            property_obj=request.front_desk_property,
            reservation_room_id=reservation_room_id,
            room_id=room_id,
            assigned_by=request.user,
        )
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    except FrontDeskOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({
        "data": {
            "reservation_room_id": line.pk,
            "assigned_room": {
                "id": line.assigned_room_id,
                "number": line.assigned_room.number,
                "floor": line.assigned_room.floor,
            },
        },
        "changed": changed,
    }, 201 if changed else 200)


@api_guard
@require_POST
@_front_desk_write_required
def check_in(request, reservation_room_id):
    try:
        stay, created = check_in_stay(
            property_obj=request.front_desk_property,
            reservation_room_id=reservation_room_id,
            checked_in_by=request.user,
        )
    except FrontDeskOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({
        "data": {
            "stay_id": stay.pk,
            "reservation_room_id": stay.reservation_room_id,
            "room_id": stay.room_id,
            "room_number": stay.room.number,
            "status": stay.status,
            "actual_check_in": stay.actual_check_in.isoformat() if stay.actual_check_in else None,
        },
        "created": created,
    }, 201 if created else 200)


@api_guard
@require_POST
@_front_desk_write_required
def collect_payment(request, booking_no):
    try:
        body = _body(request)
        payment, created = collect_manual_payment(
            property_obj=request.front_desk_property,
            booking_no=booking_no,
            amount=body.get("amount"),
            method=body.get("method"),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            received_by=request.user,
        )
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    except FrontDeskOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({
        "data": {
            "payment_id": payment.pk,
            "provider_ref": payment.provider_ref,
            "method": payment.method,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "status": payment.status,
        },
        "created": created,
    }, 201 if created else 200)


@api_guard
@require_POST
@_front_desk_write_required
def check_out(request, stay_id):
    try:
        stay, changed = check_out_stay(
            property_obj=request.front_desk_property,
            stay_id=stay_id,
            checked_out_by=request.user,
        )
    except FrontDeskOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({
        "data": {
            "stay_id": stay.pk,
            "status": stay.status,
            "actual_check_out": stay.actual_check_out.isoformat() if stay.actual_check_out else None,
            "room_id": stay.room_id,
            "room_number": stay.room.number,
        },
        "changed": changed,
    })
