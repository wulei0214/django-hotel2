"""PMS 2.0 前台接待查询与写操作 API。

查询接口组装预订、Stay、客房和 Folio DTO；写接口只负责认证/权限/参数边界，
具体事务和状态迁移由 apps.reservations.front_desk_services 负责。
"""
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.billing.models import Folio, Payment, PaymentIntent, Refund
from apps.billing.payment_intents import extend_intent_window, get_channel_window
from apps.core import labels as L
from apps.core.api_contract import ApiError, api_response, api_view, idempotent, parse_page_args
from apps.guests.services import guest_phone_plain, phone_hash
from apps.identity.authorization import AuthorizationService
from apps.inventory.models import Room, RoomType
from apps.inventory.services import InventoryUnavailable, _property_today
from apps.reservations.models import (
    NoShowRecord, Reservation, ReservationRoom, Stay, StayRoomSegment,
)
from apps.reservations.front_desk_services import (
    FrontDeskOperationError, FrontDeskValidationError,
    assign_room as assign_room_service,
    check_in_stay,
    collect_manual_payment,
    check_out_stay,
)
from apps.reservations.lifecycle_services import (
    LifecycleOperationError, LifecycleValidationError,
    approve_refund as approve_refund_service,
    reject_refund as reject_refund_service,
    change_stay_room,
    extend_stay,
    mark_reservation_room_no_show,
    receive_manual_deposit,
    request_refund as request_refund_service,
)
from apps.reservations.services import ReservationConflict
from apps.reservations.walkin import create_walk_in
from hotelapp.models import Accommodation as LegacyAccommodation, Folio as LegacyFolio

from .billing_center import _require_finance_access
from .views import _body, api_guard


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
    """按预订汇总所有未结账单余额；付款操作权限/状态仍以 MASTER 账单为准。

    Folio 模型允许一个预订有多个账单。不能把每张账单直接写入
    result[reservation_id]，否则后处理账单会覆盖之前账单的摘要。
    预订余额口径按账单模型约定：所有 OPEN Folio 的带符号凭证合计；
    CLOSED/VOIDED Folio 保留历史，不计入当前余额。
    """
    if not reservation_ids:
        return {}
    result = {}
    folios = (
        Folio.objects.filter(property=property_obj, reservation_id__in=reservation_ids)
        .select_related("reservation")
        .prefetch_related("entries", "payments")
        .order_by("reservation_id", "folio_type", "id")
    )
    for folio in folios:
        summary = result.setdefault(folio.reservation_id, {
            "folio_id": None,
            "status": "NOT_OPENED",
            "status_label": L.folio_summary_status_label("NOT_OPENED"),
            "charges": Decimal("0.00"),
            "credits": Decimal("0.00"),
            "balance": Decimal("0.00"),
            "pending_payment_count": 0,
            "currency": folio.reservation.currency,
            "currency_label": L.currency_label(folio.reservation.currency),
            "folio_count": 0,
            "open_folio_count": 0,
        })
        summary["folio_count"] += 1
        # 收款/押金表单只写入主账单，因此状态与 folio_id 明确取 MASTER。
        if folio.folio_type == Folio.FolioType.MASTER:
            summary["folio_id"] = folio.pk
            summary["status"] = folio.status
            summary["status_label"] = L.folio_summary_status_label(folio.status)

        # 预订当前余额只统计未结账单；已结/作废凭证仍保留在账单详情中。
        if folio.status != Folio.Status.OPEN:
            continue
        if folio.currency != summary["currency"]:
            # 不允许把不同币种金额直接相加；保留冲突标记，提示必须人工核对。
            summary["currency_mismatch"] = True
            continue
        entries = list(folio.entries.all())
        summary["charges"] += sum(
            (entry.amount for entry in entries if entry.amount > 0),
            Decimal("0.00"))
        summary["credits"] += sum(
            (-entry.amount for entry in entries if entry.amount < 0),
            Decimal("0.00"))
        summary["balance"] += sum(
            (entry.amount for entry in entries), Decimal("0.00"))
        summary["pending_payment_count"] += sum(
            1 for item in folio.payments.all()
            if item.status in (
                Payment.Status.CREATED, Payment.Status.PENDING,
                Payment.Status.REFUND_PENDING)
        )
        summary["open_folio_count"] += 1

    for summary in result.values():
        summary.setdefault("currency_mismatch", False)
        for key in ("charges", "credits", "balance"):
            summary[key] = (
                None if summary["currency_mismatch"] else
                str(summary[key].quantize(Decimal("0.01")))
            )
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
            "phone_masked": _mask_phone(guest_phone_plain(reservation.guest)),
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
        "stay_status_label": (
            L.stay_status_labels().get(stay.status, stay.status) if stay else
            ("待办理入住" if reservation.status in (
                Reservation.Status.CONFIRMED, Reservation.Status.CHECKED_IN
            ) else ("待确认" if reservation.status == Reservation.Status.PENDING
                   else "无实际入住记录"))
        ),
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
        | Q(assigned_room__number__icontains=value)
    )
    # WB-103：电话检索改为 phone_hash 精确匹配（HMAC 不可逆，不支持部分
    # 匹配）；不再查询 guest.phone / phone_normalized 明文列。
    if digits:
        conditions = conditions | Q(
            reservation__guest__phone_hash=phone_hash(digits))
    return queryset.filter(conditions)


@require_GET
@api_guard
@_front_desk_required
def overview(request):
    property_obj = request.front_desk_property
    try:
        day = _parse_date(
            request.GET.get("date"), "业务日期", _property_today(property_obj)
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
        day = _parse_date(request.GET.get("date"), "抵店日期", _property_today(property_obj))
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
        day = _parse_date(request.GET.get("date"), "离店日期", _property_today(property_obj))
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
        # WB-103：电话检索改为 guest.phone_hash 精确匹配（HMAC 不可逆）。
        conditions = Q(booking_no__icontains=value) | Q(
            guest__full_name__icontains=value)
        if digits:
            conditions = conditions | Q(guest__phone_hash=phone_hash(digits))
        queryset = queryset.filter(conditions)
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
            "guest": {"id": reservation.guest_id, "full_name": reservation.guest.full_name, "phone_masked": _mask_phone(guest_phone_plain(reservation.guest))},
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
            "phone_masked": _mask_phone(guest_phone_plain(reservation.guest)),
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
            + (["approve_refund", "reject_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_approve") else [])),
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
    # MAINT-02b：显式锁定主账单并固定顺序，避免多账单时取到非 MASTER 行。
    folio = (Folio.objects
             .filter(property=property_obj, reservation=reservation,
                     folio_type=Folio.FolioType.MASTER)
             .order_by("pk")
             .prefetch_related("entries", "payments").first())
    if folio is None:
        return _response({"data": {
            "folio": None, "summary": _empty_folio_summary(reservation.currency),
            "entries": [], "payments": [],
            "allowed_actions": (["request_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_request") else [])
                + (["approve_refund", "reject_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_approve") else []),
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
                + (["approve_refund", "reject_refund"] if AuthorizationService.has_permission(request.user, property_obj, "billing.refund_approve") else []),
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
    except FrontDeskValidationError as exc:
        # RSV-10：子类必须先于父类捕获，否则金额格式错误仍被归为 409。
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
        reservation_rooms__check_out__gt=_property_today(property_obj),
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
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    try:
        payment, created = receive_manual_deposit(
            property_obj=request.front_desk_property, booking_no=booking_no,
            amount=body.get("amount"), method=body.get("method"),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            received_by=request.user,
        )
    except LifecycleValidationError as exc:
        # RSV-10：仅入参格式/精度错误 → 400（子类须先于父类捕获）。
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    except LifecycleOperationError as exc:
        # 累计押金超额等业务状态冲突 → 409。
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
    """审批只改变审批状态；实际退款必须由独立执行人走 billing execute API。"""
    try:
        _body(request)  # 校验请求体格式；不再接受“审批即确认付款”的语义
        refund, changed = approve_refund_service(
            property_obj=request.front_desk_property, refund_id=refund_id,
            approved_by=request.user,
        )
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR", "message": str(exc)}}, 400)
    except LifecycleOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({"data": {
        "refund_id": refund.pk, "status": refund.status,
        "approved_by": refund.approved_by_id,
        "execution_required": refund.status == Refund.Status.APPROVED,
    }, "changed": changed})


@api_guard
@require_POST
@idempotent
@_front_desk_action_required("billing.refund_approve")
def reject_refund_api(request, refund_id):
    """经理岗位驳回退款；只登记审批结果，不执行退款、不追加 REFUND 凭证。

    Idempotency-Key 必填，避免网络重试把已成功驳回误报为重复操作失败。
    """
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        return _response({"error": {
            "code": "VALIDATION_ERROR",
            "message": "驳回退款必须提供 Idempotency-Key。",
        }}, 400)
    try:
        body = _body(request)
        refund, changed = reject_refund_service(
            property_obj=request.front_desk_property, refund_id=refund_id,
            rejected_by=request.user, reason=body.get("reason"))
    except LifecycleOperationError as exc:
        return _response({"error": {
            "code": "BUSINESS_CONFLICT", "message": str(exc)}}, 409)
    return _response({"data": {
        "refund_id": refund.pk, "status": refund.status,
        "approved_by": refund.approved_by_id,
        "changed": changed,
    }})


@api_guard
@require_POST
@_front_desk_action_required("billing.refund_request")
def exception_release_request(request, booking_no):
    """POST /api/v1/front-desk/folios/{booking_no}/exception-release-request
    — 前台发起退房异常放行申请（DEC-02-B2），待值班经理审批。"""
    try:
        body = _body(request)
        # FIN-01：select_for_update 必须在事务内求值。MySQL/PostgreSQL 在
        # autocommit 下会抛 TransactionManagementError（SQLite 静默忽略，
        # 因此只有真实 MySQL 请求才能暴露）。与服务层 atomic 嵌套为同一事务，
        # 保证「锁 Reservation → 锁 Folio → 建申请」与业务写入原子提交。
        with transaction.atomic():
            reservation = Reservation.objects.select_for_update().filter(
                property=request.front_desk_property, booking_no=booking_no).first()
            if reservation is None:
                return _response({"error": {"code": "RESOURCE_NOT_FOUND",
                                            "message": "未找到该物业下的预订。"}}, 404)
            from apps.billing.models import Folio
            folio = Folio.objects.filter(
                property=request.front_desk_property, reservation=reservation,
                folio_type=Folio.FolioType.MASTER).order_by("pk").first()
            if folio is None:
                return _response({"error": {"code": "RESOURCE_NOT_FOUND",
                                            "message": "未找到该预订的主账单。"}}, 404)
            from apps.billing.dec02 import request_exception_release
            release, created = request_exception_release(
                property_obj=request.front_desk_property,
                reservation=reservation, folio=folio,
                amount=body.get("amount", "0"),
                reason_code=str(body.get("reason_code") or ""),
                reason_text=str(body.get("reason_text") or ""),
                requested_by=request.user,
                idempotency_key=request.headers.get("Idempotency-Key", ""))
    except LifecycleOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT",
                                    "message": str(exc)}}, 409)
    except ValueError as exc:
        return _response({"error": {"code": "VALIDATION_ERROR",
                                    "message": str(exc)}}, 400)
    return _response({"data": {
        "release_id": release.pk, "status": release.status,
        "status_label": release.get_status_display(),
        "reason_code": release.reason_code,
        "amount": str(release.amount),
    }, "created": created}, 201 if created else 200)


@api_guard
@require_POST
@idempotent
@_front_desk_write_required
def walk_in_create(request):
    """POST /api/v1/front-desk/walk-in — 散客直入（DEC-WALKIN-01）。

    .. deprecated:: 2026-11-14
        与 POST /api/v1/front-desk/walk-ins（A1 契约口径：重放 200 created=false、
        响应含服务端 30% 押金、入参校验更严）构成同一业务动作的双端点并存。
        收敛决策待负责人裁定后二选一退役；两者复用同一服务层事实源
        create_walk_in，不存在双写分叉。

    复用 apps.reservations.walkin.create_walk_in：同一事务内完成
    Guest→Reservation(source=WALK_IN)→ReservationRoom→Folio→Hold→确认→排房→入住→Stay。
    无手机号时由服务层生成确定性占位号；住客档案不按手机号自动合并
    （共享号码仅标记人工核验）。Idempotency-Key 必填，业务层幂等重放
    返回同一 Stay，无重复写入。
    """
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _response({"error": {"code": "VALIDATION_ERROR",
                                    "message": "Walk-in 必须提供 Idempotency-Key 请求头。"}}, 400)
    try:
        body = _body(request)
        full_name = str(body.get("full_name") or "").strip()
        if not full_name:
            raise ValueError("住客姓名必填。")
        # 允许空：无手机号散客（服务层生成确定性占位号）。
        phone = str(body.get("phone") or "").strip()
        room_type_id = int(body.get("room_type_id"))
        rate_plan_id = body.get("rate_plan_id")
        if rate_plan_id is not None:
            rate_plan_id = int(rate_plan_id)
        nights = int(body.get("nights") or 1)
        adults = int(body.get("adults") or 1)
        children = int(body.get("children") or 0)
        deposit_raw = body.get("deposit_amount")
        deposit_amount = None if deposit_raw in (None, "") else Decimal(str(deposit_raw))
    except (ValueError, TypeError, InvalidOperation):
        return _response({"error": {"code": "VALIDATION_ERROR",
                                    "message": "请求参数格式不正确（姓名/房型/晚数/押金等）。"}}, 400)
    try:
        result = create_walk_in(
            property_obj=request.front_desk_property,
            full_name=full_name, phone=phone,
            room_type_id=room_type_id, rate_plan_id=rate_plan_id,
            nights=nights, adults=adults, children=children,
            deposit_amount=deposit_amount, created_by=request.user,
            idempotency_key=key,
        )
    except ValueError as exc:
        # create_walk_in 对入参校验、库存不足、无净房等统一抛 ValueError；
        # 该链路原子回滚、无部分写入，按 400 可重试/可改参处理。
        return _response({"error": {"code": "VALIDATION_ERROR",
                                    "message": str(exc)}}, 400)
    return _response({"data": {
        "booking_no": result["booking_no"],
        "reservation_id": result["reservation"].pk,
        "stay_id": result["stay"].pk,
        "room_id": result["room"].pk,
        "room_number": result["room"].number,
        "source": result["reservation"].source,
        "guest_name": full_name,
        "phone_provided": bool(phone),
    }}, 201)


# ---------------------------------------------------------------------------
# A1/A2 新端点（PMS2.0 接口规范口径；与既有端点并存，契约差异见各 docstring）
# ---------------------------------------------------------------------------
WALK_IN_MAX_NIGHTS = 30    # 与公开预订 MAX_NIGHTS（apps.api.views）同口径
WALK_IN_MAX_GUESTS = 10    # 与公开预订 MAX_GUESTS 同口径


def _request_meta(request):
    """{data, meta} 封套的 meta 节点（apps.core.api_contract._meta 同构）。"""
    return {
        "request_id": getattr(request, "request_id", "") or uuid.uuid4().hex[:16],
        "source_system": "PMS2",
        "generated_at": timezone.now().isoformat(),
    }


@api_guard
@require_POST
@_front_desk_write_required
def walk_ins_create(request):
    """POST /api/v1/front-desk/walk-ins — 散客直入（A1 契约口径）。

    复用 apps.reservations.walkin.create_walk_in 原子链路（RSV-05：禁止零散
    重写）：Guest→Reservation(source=WALK_IN)→ReservationRoom→Folio→Hold→确认
    →排房→入住→Stay；押金由服务端按 Reservation.total_amount×30% 强制计得
    （DEC-DEP-01/WB-030），请求体 deposit_amount 仅用于展示核对。

    与既有 POST /front-desk/walk-in（DEC-WALKIN-01）的差异（A1 规范口径）：
    * 业务冲突（无净房/库存不足/房价计划缺失/幂等指纹冲突）→ 409
      BUSINESS_CONFLICT（消息透传）；入参结构错误 → 400 VALIDATION_ERROR
      （FrontDeskValidationError 子类优先捕获，RSV-10）；
    * 幂等重放 → 200 + created=false：幂等由服务层 (property, idempotency_key)
      唯一约束 + 请求指纹仲裁（与公开 POST /reservations 同口径），不经
      @idempotent 装饰器短路——装饰器会缓存并重放首次 201 响应，无法表达
      created=false 语义；
    * 响应 {data, meta} 含服务端计得的 deposit_amount 与 created 标志。
    """
    property_obj = request.front_desk_property
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key or len(key) > 128:
        return _response({"error": {
            "code": "VALIDATION_ERROR",
            "message": "Walk-in 必须提供 1 至 128 字符的 Idempotency-Key 请求头。"}}, 400)
    try:
        body = _body(request)
        full_name = str(body.get("full_name") or "").strip()
        # 允许空：无手机号散客（服务层生成确定性占位号）。
        phone = str(body.get("phone") or "").strip()
        room_type_id = int(body.get("room_type_id"))
        rate_plan_id = int(body.get("rate_plan_id"))
        nights = _int_or_default(body.get("nights"), 1)
        adults = _int_or_default(body.get("adults"), 1)
        children = _int_or_default(body.get("children"), 0)
        deposit_raw = body.get("deposit_amount")
        deposit_amount = None if deposit_raw in (None, "") else Decimal(str(deposit_raw))
        if not full_name:
            raise ValueError("住客姓名必填。")
        if not 1 <= nights <= WALK_IN_MAX_NIGHTS:
            raise ValueError(f"住宿晚数必须在 1 至 {WALK_IN_MAX_NIGHTS} 晚之间。")
        if not 1 <= adults <= WALK_IN_MAX_GUESTS:
            raise ValueError(f"成人数必须在 1 至 {WALK_IN_MAX_GUESTS} 人之间。")
        if not 0 <= children <= WALK_IN_MAX_GUESTS:
            raise ValueError(f"儿童数必须在 0 至 {WALK_IN_MAX_GUESTS} 人之间。")
        if deposit_amount is not None and deposit_amount <= 0:
            raise ValueError("押金金额必须大于零。")
    except (ValueError, TypeError, InvalidOperation):
        return _response({"error": {
            "code": "VALIDATION_ERROR",
            "message": "请求参数格式或取值范围不正确"
                       "（姓名/房型/价格计划/晚数 1-30/人数/押金）。"}}, 400)

    # 幂等重放判定（A1：重放 200 + created=false）。预订表以
    # (property, idempotency_key) 定位首次订单；是否真正命中重放（同键同内容）
    # 与冲突（同键不同内容 → ReservationConflict）由 create_reservation
    # 请求指纹仲裁，本层不做旁路判断。并发首建窗口内两个请求都可能按
    # created=true 应答，但唯一约束保证只有一个写入者，无重复事实。
    is_replay = Reservation.objects.filter(
        property=property_obj, idempotency_key=key).exists()
    try:
        result = create_walk_in(
            property_obj=property_obj,
            full_name=full_name, phone=phone,
            room_type_id=room_type_id, rate_plan_id=rate_plan_id,
            nights=nights, adults=adults, children=children,
            deposit_amount=deposit_amount, created_by=request.user,
            idempotency_key=key,
        )
    except FrontDeskValidationError as exc:
        # RSV-10：入参校验失败（金额格式等）必须与业务状态冲突区分，
        # 子类在前单独捕获返回 400。
        return _response({"error": {"code": "VALIDATION_ERROR",
                                    "message": str(exc)}}, 400)
    except RoomType.DoesNotExist:
        return _response({"error": {"code": "VALIDATION_ERROR",
                                    "message": "房型不存在或未启用。"}}, 400)
    except InventoryUnavailable as exc:
        # 库存不可售（Hold 创建失败/售罄）：409，消息透传。
        return _response({"error": {"code": "BUSINESS_CONFLICT",
                                    "message": str(exc)}}, 409)
    except ReservationConflict as exc:
        # 同 Idempotency-Key 不同预订内容（create_reservation 指纹校验）。
        return _response({"error": {"code": "BUSINESS_CONFLICT",
                                    "message": str(exc)}}, 409)
    except FrontDeskOperationError as exc:
        return _response({"error": {"code": "BUSINESS_CONFLICT",
                                    "message": str(exc)}}, 409)
    except ValueError as exc:
        # create_walk_in 对无净房/库存确认失败/押金约束等业务冲突统一抛
        # ValueError（RSV-05）；链路原子回滚无部分写入，409 消息透传。
        return _response({"error": {"code": "BUSINESS_CONFLICT",
                                    "message": str(exc)}}, 409)

    reservation = result["reservation"]
    # record_deposit 在行锁下重取预订行并回写 deposit_amount（30% 服务端
    # 强制计得），create_walk_in 持有的实例不含该写入，需刷新后对外输出。
    reservation.refresh_from_db(fields=["deposit_amount"])
    return _response({
        "data": {
            "booking_no": result["booking_no"],
            "reservation_id": reservation.pk,
            "stay_id": result["stay"].pk,
            "room_number": result["room"].number,
            "deposit_amount": str(
                reservation.deposit_amount.quantize(Decimal("0.01"))),
            "source": "WALK_IN",
            "created": not is_replay,
        },
        "meta": _request_meta(request),
    }, 200 if is_replay else 201)


@require_POST
@api_view
@idempotent
def payment_intent_window_extension(request, intent_id):
    """POST /api/v1/billing/payment-intents/{intent_id}/window-extension
    — D2 支付窗口有限延长（A2，财务域写端点口径）。

    复用 apps.billing.payment_intents.extend_intent_window（服务端策略：
    max_extensions/extension_seconds 由 CHANNEL_WINDOW_CONFIG 决定，客户端
    不得提交或修改窗口参数）。权限与财务中心既有写端点同门禁：
    hotelapp.finance_access（值班经理/财务按角色授权；POST /billing/payments
    同码）。

    错误映射：意图不存在/跨物业 → 404 RESOURCE_NOT_FOUND（前置定位，
    区分于业务冲突）；终态意图/超延长上限/超累计等待时长 → 409
    BUSINESS_CONFLICT（extend_intent_window 的 ValueError 消息透传）。
    """
    property_obj = _require_finance_access(request)
    intent = PaymentIntent.objects.filter(
        pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ApiError("RESOURCE_NOT_FOUND", "支付意图不存在或不属于当前物业。")
    try:
        intent = extend_intent_window(
            intent_id=intent.pk, property_obj=property_obj,
            extended_by=request.user)
    except ValueError as exc:
        # 终态不可延长 / 已达最大延长次数 / 已达最大累计等待时长：
        # 服务端策略冲突，消息透传（D2）。
        raise ApiError("BUSINESS_CONFLICT", str(exc)) from exc
    window = get_channel_window(intent.provider_code)
    return api_response(request, {
        "intent_id": intent.pk,
        "status": intent.status,
        "extension_count": intent.extension_count,
        "expires_at": (intent.expires_at.isoformat()
                       if intent.expires_at else None),
        "max_extensions": window["max_extensions"],
        "channel_window": {
            "provider_code": intent.provider_code,
            "initial_wait_seconds": window["initial_wait_seconds"],
            "max_total_wait_seconds": window["max_total_wait_seconds"],
            "max_extensions": window["max_extensions"],
            "extension_seconds": window["extension_seconds"],
        },
    })


def _int_or_default(value, default):
    """A1 入参整数解析：缺省/空串回退默认值；其余必须可转 int。"""
    if value in (None, ""):
        return default
    return int(value)
