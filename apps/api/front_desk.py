"""PMS 2.0 前台接待查询与写操作 API。

查询接口组装预订、Stay、客房和 Folio DTO；写接口只负责认证/权限/参数边界，
具体事务和状态迁移由 apps.reservations.front_desk_services 负责。
"""
import json
from datetime import date
from decimal import Decimal
from functools import wraps
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Prefetch, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.billing.models import Folio, Payment, Refund
from apps.core import labels as L
from apps.core.api_contract import ApiError, parse_page_args
from apps.identity.authorization import AuthorizationService
from apps.inventory.models import Room
from apps.reservations.models import (
    NoShowRecord, Reservation, ReservationRoom, Stay, StayRoomSegment,
)
from apps.reservations.front_desk_services import (
    FrontDeskOperationError,
    assign_room as assign_room_service,
    check_in_stay,
    collect_manual_payment,
    check_out_stay,
)
from apps.reservations.lifecycle_services import (
    LifecycleOperationError,
    approve_refund as approve_refund_service,
    change_stay_room,
    extend_stay,
    mark_reservation_room_no_show,
    receive_manual_deposit,
    request_refund as request_refund_service,
)
from hotelapp.models import Accommodation as LegacyAccommodation, Folio as LegacyFolio

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
    """P0-03 统一分页契约：唯一解析器为 apps.core.api_contract.parse_page_args。

    非整数/空字符串/0/负数/超上限/超大页码 → ApiError VALIDATION_ERROR（400），
    视图层统一按 `except (ValueError, ApiError)` 转成 400 响应。
    """
    return parse_page_args(request, page_size_default=PAGE_DEFAULT, page_size_max=PAGE_MAX)


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
    # WB-46-08：<8 位数字串用 3+4 掩码会把短号全部还原（伪脱敏=明文）；
    # 阈值提高到 8，与 staff/guest 中心口径一致。
    if len(digits) < 8:
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
    return {"WEBSITE": "官网预订", "WALK_IN": "散客直入", "OTA": "渠道预订",
                "PHONE": "电话预订"}.get(source, source or "其他")


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
            "status_label": L.folio_summary_status_label(folio.status),
            "charges": str(charges.quantize(Decimal("0.01"))),
            "credits": str(credits.quantize(Decimal("0.01"))),
            "balance": str(balance.quantize(Decimal("0.01"))),
            "pending_payment_count": pending_payments,
            "currency": folio.currency,
            "currency_label": L.currency_label(folio.currency),
        }
    return result


def _empty_folio_summary(currency="CNY"):
    return {
        "folio_id": None, "status": "NOT_OPENED",
        "status_label": L.folio_summary_status_label("NOT_OPENED"),
        "charges": "0.00", "credits": "0.00", "balance": "0.00",
        "pending_payment_count": 0, "currency": currency,
        "currency_label": L.currency_label(currency),
    }


def _reservation_room_dto(line, folio_map=None, stay_map=None):
    reservation = line.reservation
    room = line.assigned_room
    stay = (stay_map or {}).get(line.pk)
    # N+1 修复：no_show/room_segments 由调用方 prefetch_related 提供（P0-07）；
    # 未 prefetch 时回退为单条查询，行为不变。
    try:
        no_show = line.no_show_record
    except NoShowRecord.DoesNotExist:
        no_show = None
    # StayRoomSegment Meta.ordering = ("started_at", "id")，与原显式排序一致。
    room_segments = list(stay.room_segments.all()) if stay else []
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
            "status_label": L.stay_status_labels().get(stay.status, stay.status),
            "actual_check_in": stay.actual_check_in.isoformat() if stay.actual_check_in else None,
            "room_segments": [{
                "room_number": segment.room.number,
                "started_at": segment.started_at.isoformat(),
                "ended_at": segment.ended_at.isoformat() if segment.ended_at else None,
                "reason": segment.move_reason,
            } for segment in room_segments],
        } if stay else None),
        "no_show": ({
            "id": no_show.pk, "reason": no_show.reason,
            "processed_at": no_show.processed_at.isoformat(),
            "released_nights": no_show.released_nights,
        } if no_show else None),
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
        ).select_related("reservation__guest", "room_type", "assigned_room").order_by(
            "reservation__created_at", "id"
        ).prefetch_related("no_show_record")
        queryset = _apply_search(queryset, request.GET.get("q"))
    except (ValueError, ApiError) as exc:
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
    except (ValueError, ApiError) as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    queryset = Stay.objects.filter(
        property=property_obj, status=Stay.Status.IN_HOUSE, reservation_room__check_out=day
    ).select_related(
        "reservation_room__reservation__guest", "reservation_room__room_type", "room"
    ).order_by("reservation_room__check_out", "id").prefetch_related(
        Prefetch("room_segments", queryset=StayRoomSegment.objects.select_related("room")),
        "reservation_room__no_show_record",
    )
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
    except (ValueError, ApiError) as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    queryset = Stay.objects.filter(
        property=property_obj, status=Stay.Status.IN_HOUSE
    ).select_related(
        "reservation_room__reservation__guest", "reservation_room__room_type", "room"
    ).order_by("room__number", "id").prefetch_related(
        Prefetch("room_segments", queryset=StayRoomSegment.objects.select_related("room")),
        "reservation_room__no_show_record",
    )
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
    except (ValueError, ApiError) as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    queryset = Reservation.objects.filter(
        property=property_obj
    ).select_related("guest").order_by("-created_at", "-id").prefetch_related(
        Prefetch("reservation_rooms",
                 queryset=ReservationRoom.objects.select_related("room_type", "assigned_room")),
    )
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
        # N+1 修复：明细经主查询 prefetch_related 一次取回（P0-07）。
        lines = list(reservation.reservation_rooms.all())
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
    lines = list(reservation.reservation_rooms.select_related(
        "room_type", "assigned_room").order_by("id").prefetch_related("no_show_record"))
    folio_map = _folio_summaries(property_obj, {reservation.pk})
    # 保持既有 Meta("-created_at") 最新在前的展示顺序，仅补主键兜底（P0-07）。
    stays = list(Stay.objects.filter(
        property=property_obj, reservation_room__reservation=reservation
    ).select_related("room").order_by("-created_at", "-id").prefetch_related(
        Prefetch("room_segments", queryset=StayRoomSegment.objects.select_related("room")),
    ))
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
        "currency_label": L.currency_label(reservation.currency),
        "rooms": [_reservation_room_dto(line, folio_map, stay_map) for line in lines],
        "folio_summary": folio_summary,
        "timeline": {
            "created_at": reservation.created_at.isoformat(),
            "updated_at": reservation.updated_at.isoformat(),
            "stays": [{"id": stay.pk, "status": stay.status, "status_label": L.stay_status_labels().get(stay.status, stay.status), "created_at": stay.created_at.isoformat(), "actual_check_in": stay.actual_check_in.isoformat() if stay.actual_check_in else None, "actual_check_out": stay.actual_check_out.isoformat() if stay.actual_check_out else None} for stay in stays],
        },
        "source_system": "PMS2",
        "allowed_actions": (["view_detail"]
            + (["operate"] if AuthorizationService.has_permission(request.user, property_obj, "hotelapp.change_accommodation") else [])
            + (["mark_no_show"] if AuthorizationService.has_permission(request.user, property_obj, "hotelapp.no_show_handle") else [])
            + (["request_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_request") else [])
            + (["approve_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_approve") else [])),
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
        "occupancy_label": L.room_label_maps()["occupancy"].get(room.occupancy_status, room.occupancy_status),
        "housekeeping_status": room.housekeeping_status,
        "housekeeping_label": L.room_label_maps()["housekeeping"].get(room.housekeeping_status, room.housekeeping_status),
        "maintenance_status": room.maintenance_status,
        "maintenance_label": L.room_label_maps()["maintenance"].get(room.maintenance_status, room.maintenance_status),
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
        return _response({"data": {
            "folio": None, "summary": _empty_folio_summary(reservation.currency),
            "entries": [], "payments": [],
            "allowed_actions": (["request_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_request") else [])
                + (["approve_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_approve") else []),
        }})
    entries = list(folio.entries.order_by("created_at", "id"))
    charges = sum((item.amount for item in entries if item.amount > 0), Decimal("0.00"))
    credits = sum((-item.amount for item in entries if item.amount < 0), Decimal("0.00"))
    balance = sum((item.amount for item in entries), Decimal("0.00"))
    summary = {"folio_id": folio.pk, "status": folio.status,
               "status_label": L.folio_summary_status_label(folio.status),
               "charges": str(charges), "credits": str(credits), "balance": str(balance), "currency": folio.currency,
               "currency_label": L.currency_label(folio.currency)}
    return _response({
        "data": {
            "summary": summary,
            "entries": [{"id": item.pk, "entry_type": item.entry_type, "amount": str(item.amount), "currency": item.currency, "business_date": item.business_date.isoformat(), "reference": item.reference, "description": item.description, "created_at": item.created_at.isoformat()} for item in entries],
            "payments": [{
                "id": item.pk, "provider": item.provider, "provider_ref": item.provider_ref,
                "method": item.method, "amount": str(item.amount), "status": item.status,
                "status_label": L.payment_status_labels().get(item.status, item.status),
                "paid_at": item.paid_at.isoformat() if item.paid_at else None,
                "refunds": [{
                    "id": refund.pk, "amount": str(refund.amount), "reason": refund.reason,
                    "status": refund.status,
                    "status_label": L.refund_status_labels().get(refund.status, refund.status),
                    "provider_ref": refund.provider_ref,
                } for refund in item.refunds.order_by("-created_at", "-id")],
            } for item in folio.payments.order_by("-created_at", "-id")],
            "allowed_actions": (["request_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_request") else [])
                + (["approve_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_approve") else []),
        }
    })


@api_guard
@require_POST
@_front_desk_write_required
def assign_room(request, reservation_room_id):
    try:
        body = _body(request)
        # WB-46-04：room_id 缺失/为 null 时 int(None) 抛 TypeError 未被捕获
        # → 500；与 change_stay_room_api 同口径捕获 (ValueError, TypeError) → 400。
        room_id = int(body.get("room_id"))
        line, changed = assign_room_service(
            property_obj=request.front_desk_property,
            reservation_room_id=reservation_room_id,
            room_id=room_id,
            assigned_by=request.user,
        )
    except (ValueError, TypeError):
        return _response({"error": {"code": "VALIDATION_ERROR", "message": "room_id 必须是整数。"}}, 400)
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



def _front_desk_action_required(permission_code):
    """要求当前物业员工具有指定的独立业务权限，不隐式继承入住修改权。"""
    def decorator(view):
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
            if not AuthorizationService.has_permission(user, property_obj, permission_code):
                return _response({"error": {"code": "PERMISSION_DENIED", "message": "当前岗位没有该项业务操作权限。"}}, 403)
            request.front_desk_property = property_obj
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def _legacy_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return value


def _legacy_money(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        number = Decimal(raw)
        return str(number.quantize(Decimal("0.01")))
    except Exception:
        return None


def _legacy_reservation_dto(item):
    guest = item.顾客id
    room = item.客房编号
    return {
        "booking_no": f"LEGACY-{item.pk}",
        "legacy_id": item.pk,
        "status": item.状态 or "历史记录",
        "status_label": item.状态 or "历史记录",
        "source": "LEGACY",
        "source_label": "历史 hotelapp 订单",
        "guest": {
            "id": guest.pk if guest else None,
            "full_name": (guest.姓名 if guest else "") or "历史住客",
            "phone_masked": _mask_phone(item.联系电话),
        },
        "check_in": _legacy_date(item.入住时间),
        "check_out": _legacy_date(item.退房时间),
        "room_count": 1,
        "adult_count": None,
        "child_count": None,
        "total_amount": None,
        "currency": "CNY",
        "rooms": [{
            "id": None,
            "room_type": {"name": room.客房类型 if room else "历史房型"},
            "assigned_room": {"number": str(room.房间号)} if room else None,
        }],
        "room_type": {"name": room.客房类型 if room else "历史房型"},
        "assigned_room": {"number": str(room.房间号)} if room else None,
        "legacy_paid_amount": _legacy_money(item.收款金额),
        "legacy_deposit_amount": _legacy_money(item.押金),
        "folio_summary": {"status": "LEGACY_ARCHIVE", "currency": "CNY"},
        "source_system": "LEGACY",
        "read_only": True,
    }


@require_GET
@api_guard
@_front_desk_required
def legacy_reservations(request):
    """历史 hotelapp 客房订单只读适配器；不把旧记录伪造为 PMS2 Reservation。"""
    try:
        page, page_size = _page_args(request)
    except (ValueError, ApiError) as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    queryset = LegacyAccommodation.objects.select_related("顾客id", "客房编号").order_by("-id")
    value = (request.GET.get("q") or "").strip()
    if value:
        if len(value) > 80:
            return _response({"error": {"code": "VALIDATION_ERROR", "message": "搜索内容不能超过 80 个字符。"}}, 400)
        condition = (
            Q(顾客id__姓名__icontains=value)
            | Q(联系电话__icontains=value)
        )
        if value.isdigit():
            condition |= Q(id=int(value))
        queryset = queryset.filter(condition)
    status = (request.GET.get("status") or "").strip()
    if status:
        queryset = queryset.filter(状态=status)
    rows, meta = _paginate(queryset, page, page_size)
    return _response({
        "data": [_legacy_reservation_dto(item) for item in rows],
        "meta": {**meta, "source_system": "LEGACY", "read_only": True},
    })


@require_GET
@api_guard
@_front_desk_required
def legacy_reservation_detail(request, legacy_id):
    """历史订单及旧 Folio 原样只读展示，金额不参与 PMS2 账单余额计算。"""
    item = LegacyAccommodation.objects.select_related("顾客id", "客房编号").filter(pk=legacy_id).first()
    if item is None:
        return _response({"error": {"code": "RESOURCE_NOT_FOUND", "message": "未找到历史订单。"}}, 404)
    entries = []
    for entry in LegacyFolio.objects.filter(order=item).order_by("id"):
        entries.append({
            "id": entry.pk,
            "entry_type": entry.类型,
            "amount_raw": entry.金额,
            "business_date": _legacy_date(entry.日期),
            "description": entry.事由,
            "operator": entry.经手人,
            "source_system": "LEGACY",
        })
    data = _legacy_reservation_dto(item)
    data["legacy_entries"] = entries
    data["legacy_note"] = "历史账单来自旧 hotelapp.Folio，仅按原始凭证展示，不与 PMS2 Folio 汇总或对账。"
    data["timeline"] = {"created_at": None, "updated_at": None, "stays": []}
    return _response({"data": data})


@require_GET
@api_guard
@_front_desk_required
def available_stay_rooms(request, stay_id):
    property_obj = request.front_desk_property
    stay = Stay.objects.filter(pk=stay_id, property=property_obj, status=Stay.Status.IN_HOUSE).select_related("reservation_room").first()
    if stay is None:
        return _response({"error": {"code": "RESOURCE_NOT_FOUND", "message": "未找到在住记录。"}}, 404)
    line = stay.reservation_room
    candidates = Room.objects.filter(
        property=property_obj, room_type_id=line.room_type_id, is_active=True,
        occupancy_status=Room.OccupancyStatus.VACANT,
        housekeeping_status=Room.HousekeepingStatus.CLEAN,
        maintenance_status=Room.MaintenanceStatus.NORMAL,
    ).exclude(pk=stay.room_id).exclude(
        reservation_rooms__property=property_obj,
        reservation_rooms__check_in__lt=line.check_out,
        reservation_rooms__check_out__gt=_business_date(property_obj),
        reservation_rooms__reservation__status__in=(
            Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.CHECKED_IN
        ),
    ).order_by("floor", "number").distinct()
    data = [{"id": room.pk, "number": room.number, "floor": room.floor} for room in candidates]
    return _response({"data": data, "meta": {"total": len(data), "stay_id": stay.pk}})


@api_guard
@require_POST
@_front_desk_write_required
def extend_stay_api(request, stay_id):
    try:
        body = _body(request)
        new_check_out = _parse_date(body.get("new_check_out"), "新的离店日期", None)
        if new_check_out is None:
            raise ValueError("必须提供 new_check_out。")
        stay, line, amount = extend_stay(
            property_obj=request.front_desk_property, stay_id=stay_id,
            new_check_out=new_check_out, reason=body.get("reason"),
            extended_by=request.user,
        )
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    except LifecycleOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({"data": {
        "stay_id": stay.pk, "check_out": line.check_out.isoformat(),
        "extension_amount": str(amount), "new_room_amount": str(line.amount),
    }})


@api_guard
@require_POST
@_front_desk_write_required
def change_stay_room_api(request, stay_id):
    try:
        body = _body(request)
        room_id = int(body.get("room_id"))
        stay, changed = change_stay_room(
            property_obj=request.front_desk_property, stay_id=stay_id,
            new_room_id=room_id, reason=body.get("reason"), moved_by=request.user,
        )
    except (ValueError, TypeError) as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": "room_id 必须是整数。"}}, 400)
    except LifecycleOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({"data": {
        "stay_id": stay.pk, "room_id": stay.room_id, "room_number": stay.room.number,
        "room_segments": [{
            "room_number": segment.room.number,
            "started_at": segment.started_at.isoformat(),
            "ended_at": segment.ended_at.isoformat() if segment.ended_at else None,
            "reason": segment.move_reason,
        } for segment in stay.room_segments.select_related("room").order_by("started_at", "id")],
    }, "changed": changed})


@api_guard
@require_POST
@_front_desk_action_required("hotelapp.no_show_handle")
def mark_no_show_api(request, reservation_room_id):
    try:
        body = _body(request)
        record, created = mark_reservation_room_no_show(
            property_obj=request.front_desk_property, reservation_room_id=reservation_room_id,
            reason=body.get("reason"), processed_by=request.user,
        )
    except LifecycleOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({"data": {
        "no_show_id": record.pk, "reservation_room_id": record.reservation_room_id,
        "released_nights": record.released_nights, "status": "NO_SHOW",
    }, "created": created}, 201 if created else 200)


@api_guard
@require_POST
@_front_desk_write_required
def receive_deposit_api(request, booking_no):
    try:
        body = _body(request)
        payment, created = receive_manual_deposit(
            property_obj=request.front_desk_property, booking_no=booking_no,
            amount=body.get("amount"), method=body.get("method"),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            received_by=request.user,
        )
    except LifecycleOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({"data": {
        "payment_id": payment.pk, "provider_ref": payment.provider_ref,
        "amount": str(payment.amount), "method": payment.method,
        "status": payment.status, "payment_kind": "DEPOSIT",
    }, "created": created}, 201 if created else 200)


@api_guard
@require_POST
@_front_desk_action_required("billing.refund_request")
def request_refund_api(request, payment_id):
    try:
        body = _body(request)
        refund, created = request_refund_service(
            property_obj=request.front_desk_property, payment_id=payment_id,
            amount=body.get("amount"), reason=body.get("reason"),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            requested_by=request.user,
        )
    except LifecycleOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({"data": {
        "refund_id": refund.pk, "provider_ref": refund.provider_ref,
        "payment_id": refund.payment_id, "amount": str(refund.amount),
        "status": refund.status,
    }, "created": created}, 201 if created else 200)


@api_guard
@require_POST
@_front_desk_action_required("billing.refund_approve")
def approve_refund_api(request, refund_id):
    try:
        body = _body(request)
        if body.get("confirm_refund_paid") is not True:
            raise ValueError("请确认人工退款已经实际完成后再审批入账。")
        refund, changed = approve_refund_service(
            property_obj=request.front_desk_property, refund_id=refund_id,
            approved_by=request.user,
            confirm_refund_paid=True,
        )
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    except LifecycleOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({"data": {
        "refund_id": refund.pk, "status": refund.status,
        "approved_by": refund.approved_by_id,
    }, "changed": changed})
