# -*- coding: utf-8 -*-
"""Guest 360 + Room 中心只读 API（V1.1 §10 Phase 1：五模块只读原型）。

统一使用 {data, meta} 封套（api_contract）；只读不写；物业范围过滤；
敏感字段按权限脱敏；列表分页。
"""
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.api_contract import (ApiError, api_view, api_response, paginate, idempotent)
from apps.core.models import AuditEvent
from apps.guests.models import (
    Guest, GuestDocument, GuestMembership, GuestPhone,
)
import json as _json

from apps.guests.services import (
    decrypt_doc_no, _mask,
    GuestService, GuestServiceError, GuestMembershipService,
)
from apps.identity.authorization import AuthorizationService
from apps.inventory.models import (
    DailyRate, RatePlan, Room, RoomType, RoomTypeInventory,
)
from apps.reservations.models import Reservation, ReservationRoom, Stay

logger = logging.getLogger("hotel.api")


def _guest_dto(guest, user, property_obj, include_detail=False):
    """Guest DTO：按权限脱敏。"""
    phones = GuestPhone.objects.filter(guest=guest)
    primary_phone = phones.filter(is_primary=True).first() or phones.first()
    phone_masked = None
    if primary_phone:
        raw = primary_phone.phone
        phone_masked = raw[:3] + "****" + raw[-4:] if len(raw) >= 8 else raw
    membership = GuestMembership.objects.filter(
        guest=guest, property=property_obj,
        status=GuestMembership.Status.ACTIVE).first()
    current_stay = Stay.objects.filter(
        property=property_obj, room__property=property_obj,
        reservation_room__reservation__guest=guest,
        status=Stay.Status.IN_HOUSE).select_related("room").first()
    dto = {
        "id": guest.pk,
        "full_name": guest.full_name,
        "phone_masked": phone_masked,
        "email": guest.email or None,
        "member": ({
            "member_no": membership.member_no,
            "level": membership.level_code,
            "status": membership.status,
        } if membership else None),
        "current_stay": ({
            "room_number": current_stay.room.number,
            "check_in": str(current_stay.reservation_room.check_in),
        } if current_stay else None),
        "source_system": "PMS2",
        "created_at": guest.created_at.isoformat(),
    }
    if include_detail:
        docs = []
        for doc in GuestDocument.objects.filter(guest=guest):
            try:
                masked = _mask(decrypt_doc_no(doc.doc_no_ciphertext))
            except Exception:
                masked = "解密失败"
            docs.append({
                "id": doc.pk, "doc_type": doc.doc_type,
                "masked": masked, "is_primary": doc.is_primary,
                "verified": doc.verified_at is not None,
            })
        dto["documents"] = docs
        dto["phones"] = [
            {"phone": (p.phone[:3] + "****" + p.phone[-4:]
                       if len(p.phone) >= 8 else "已隐藏"),
             "type": p.phone_type, "is_primary": p.is_primary}
            for p in phones
        ]
        dto["notes"] = guest.notes or None
    return dto


@require_GET
@api_view
def guest_list(request):
    """客人中心：Guest 列表（分页/搜索/脱敏）。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_client"):
        raise ApiError("PERMISSION_DENIED", "缺少客人查看权限。")

    qs = Guest.objects.filter(property=property_obj).order_by("-created_at")
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(full_name__icontains=q) |
            Q(phones__phone_normalized__icontains=q.replace(" ", ""))
        ).distinct()
    items, pagination = paginate(qs, request)
    data = [_guest_dto(g, user, property_obj) for g in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def guest_detail(request, guest_id):
    """Guest 360 详情：档案 + 证件 + 电话 + 会员 + 入住历史。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_client"):
        raise ApiError("PERMISSION_DENIED", "缺少客人查看权限。")

    try:
        guest = Guest.objects.get(pk=guest_id, property=property_obj)
    except Guest.DoesNotExist:
        raise ApiError("RESOURCE_NOT_FOUND", "客人不存在。")

    reservations = Reservation.objects.filter(
        property=property_obj, guest=guest).order_by("-created_at")[:10]
    reservation_list = [{
        "booking_no": r.booking_no, "status": r.status,
        "check_in": str(r.check_in), "check_out": str(r.check_out),
        "total_amount": str(r.total_amount), "rooms": r.room_count,
    } for r in reservations]

    data = _guest_dto(guest, user, property_obj, include_detail=True)
    data["reservations"] = reservation_list
    return api_response(request, data)


# ---------- Room 中心只读 API ----------

@require_GET
@api_view
def room_status_list(request):
    """房态中心：三维房态列表（楼层/房型/清洁/维修/占用）。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_room"):
        raise ApiError("PERMISSION_DENIED", "缺少客房查看权限。")

    qs = Room.objects.filter(property=property_obj, is_active=True).order_by("number")
    floor = request.GET.get("floor")
    if floor:
        qs = qs.filter(floor=floor)
    hk = request.GET.get("housekeeping")
    if hk:
        qs = qs.filter(housekeeping_status=hk)
    room_type_id = request.GET.get("room_type_id")
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)

    items, pagination = paginate(qs, request)
    data = [{
        "id": r.pk, "number": r.number, "floor": r.floor or None,
        "room_type": {"id": r.room_type.pk, "code": r.room_type.code,
                      "name": r.room_type.name},
        "occupancy_status": r.occupancy_status,
        "housekeeping_status": r.housekeeping_status,
        "maintenance_status": r.maintenance_status,
        "is_ready": r.is_sellable_now,
        "source_system": "PMS2",
    } for r in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def room_type_list(request):
    """房型列表：含每日价格/库存摘要。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_room"):
        raise ApiError("PERMISSION_DENIED", "缺少客房查看权限。")

    qs = RoomType.objects.filter(property=property_obj, is_active=True).order_by("code")
    data = [{
        "id": rt.pk, "code": rt.code, "name": rt.name,
        "capacity": rt.capacity, "bed_type": rt.bed_type or None,
        "is_active": rt.is_active,
    } for rt in qs]
    return api_response(request, data)


@require_GET
@api_view
def rate_calendar(request):
    """房价日历：按日期展示房型每日价格与库存。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_room"):
        raise ApiError("PERMISSION_DENIED", "缺少客房查看权限。")

    from datetime import date, timedelta
    start = request.GET.get("start") or str(date.today())
    days = min(30, int(request.GET.get("days", "14")))
    try:
        start_date = date.fromisoformat(start)
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "start 必须使用 YYYY-MM-DD 格式。")
    end_date = start_date + timedelta(days=days)
    room_type_id = request.GET.get("room_type_id")

    inv_qs = RoomTypeInventory.objects.filter(
        property=property_obj, stay_date__gte=start_date,
        stay_date__lt=end_date).order_by("stay_date")
    if room_type_id:
        inv_qs = inv_qs.filter(room_type_id=room_type_id)

    rate_qs = DailyRate.objects.filter(
        property=property_obj, stay_date__gte=start_date,
        stay_date__lt=end_date).order_by("stay_date")
    if room_type_id:
        rate_qs = rate_qs.filter(room_type_id=room_type_id)

    inventory_data = [{
        "room_type": {"id": inv.room_type.pk, "code": inv.room_type.code},
        "stay_date": str(inv.stay_date),
        "total_rooms": inv.total_rooms, "held": inv.held,
        "sold": inv.sold, "out_of_service": inv.out_of_service,
        "available_count": inv.available_count,
        "stop_sell": inv.stop_sell,
    } for inv in inv_qs]
    rate_data = [{
        "room_type": {"id": dr.room_type.pk, "code": dr.room_type.code},
        "rate_plan": {"id": dr.rate_plan.pk, "code": dr.rate_plan.code},
        "stay_date": str(dr.stay_date),
        "price": str(dr.price), "currency": dr.currency,
        "closed": dr.closed,
    } for dr in rate_qs]
    return api_response(request, {"inventory": inventory_data, "rates": rate_data})


# ---------- Phase 2：Guest 写操作（走 GuestService，不直接写模型） ----------


def _parse_body(request):
    try:
        return _json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, _json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是有效 JSON。")


@require_POST
@api_view
@idempotent
def guest_create(request):
    """POST /api/v1/guests — 创建客人档案。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, "hotelapp.add_client"):
        raise ApiError("PERMISSION_DENIED", "缺少客人新增权限。")
    body = _parse_body(request)
    try:
        guest = GuestService.create_guest(
            property_obj=property_obj,
            full_name=body.get("full_name") or "",
            phone=body.get("phone") or "",
            email=body.get("email") or "",
            notes=body.get("notes") or "",
            created_by=user, request=request)
    except GuestServiceError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {"id": guest.pk, "full_name": guest.full_name}, status=201)


@require_POST
@api_view
@idempotent
def guest_update(request, guest_id):
    """PUT/PATCH /api/v1/guests/<id> — 更新客人档案。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.change_client"):
        raise ApiError("PERMISSION_DENIED", "缺少客人修改权限。")
    body = _parse_body(request)
    try:
        guest, changed = GuestService.update_guest(
            guest_id=guest_id, property_obj=property_obj,
            updated_by=user, request=request,
            full_name=body.get("full_name"),
            phone=body.get("phone"),
            email=body.get("email"),
            notes=body.get("notes"))
    except Guest.DoesNotExist:
        raise ApiError("RESOURCE_NOT_FOUND", "客人不存在。")
    except GuestServiceError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {"id": guest.pk, "changed": list(changed.keys())})



@require_POST
@api_view
def guest_reveal_document(request, guest_id, document_id):
    """按需揭示证件完整值；独立权限、强制原因、逐次审计。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None or not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.guest_document_reveal"):
        raise ApiError("PERMISSION_DENIED", "当前岗位无证件完整值揭示权限。")
    guest = Guest.objects.filter(pk=guest_id, property=property_obj).first()
    if guest is None:
        raise ApiError("RESOURCE_NOT_FOUND", "客人不存在。")
    document = GuestDocument.objects.filter(pk=document_id, guest=guest).first()
    if document is None:
        raise ApiError("RESOURCE_NOT_FOUND", "客人证件不存在。")
    body = _parse_body(request)
    reason = str(body.get("reason") or "").strip()
    if len(reason) < 8 or len(reason) > 240:
        raise ApiError("VALIDATION_ERROR", "揭示证件必须填写 8 至 240 个字符的业务用途/原因。")
    try:
        plain = decrypt_doc_no(document.doc_no_ciphertext)
    except Exception:
        raise ApiError("BUSINESS_CONFLICT", "证件密文无法解密，请联系系统管理员。")
    AuditEvent.record(
        action="guest.pii_reveal.document", entity_type="GuestDocument",
        entity_id=document.pk, actor=user, property_obj=property_obj,
        after={"masked": _mask(plain)}, reason=reason, request=request)
    return api_response(request, {
        "guest_id": guest.pk, "document_id": document.pk,
        "doc_type": document.doc_type, "document_no": plain,
    })


@require_POST
@api_view
def guest_reveal_phone(request, guest_id, phone_id):
    """按需揭示电话完整值；与证件揭示分权，并强制记录业务理由。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None or not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.guest_phone_reveal"):
        raise ApiError("PERMISSION_DENIED", "当前岗位无客人电话完整值揭示权限。")
    guest = Guest.objects.filter(pk=guest_id, property=property_obj).first()
    if guest is None:
        raise ApiError("RESOURCE_NOT_FOUND", "客人不存在。")
    phone = GuestPhone.objects.filter(pk=phone_id, guest=guest).first()
    if phone is None:
        raise ApiError("RESOURCE_NOT_FOUND", "客人电话不存在。")
    body = _parse_body(request)
    reason = str(body.get("reason") or "").strip()
    if len(reason) < 8 or len(reason) > 240:
        raise ApiError("VALIDATION_ERROR", "揭示电话必须填写 8 至 240 个字符的业务用途/原因。")
    AuditEvent.record(
        action="guest.pii_reveal.phone", entity_type="GuestPhone",
        entity_id=phone.pk, actor=user, property_obj=property_obj,
        after={"masked": _mask(phone.phone)}, reason=reason, request=request)
    return api_response(request, {
        "guest_id": guest.pk, "phone_id": phone.pk, "phone": phone.phone,
    })
