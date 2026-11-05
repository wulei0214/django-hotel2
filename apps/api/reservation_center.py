# -*- coding: utf-8 -*-
"""预订管理 + 审计时间线只读 API（V1.1 Phase 1 五模块补齐）。"""
from django.views.decorators.http import require_GET

from apps.core.api_contract import ApiError, api_view, api_response, paginate
from apps.identity.authorization import AuthorizationService
from apps.reservations.models import Reservation, ReservationRoom, Stay


@require_GET
@api_view
def reservation_list(request):
    """预订管理：Reservation 列表（分页/状态筛选/搜索）。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_accommodation"):
        raise ApiError("PERMISSION_DENIED", "缺少预订查看权限。")
    qs = Reservation.objects.filter(
        property=property_obj).select_related("guest").order_by("-created_at", "-id")
    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(booking_no__icontains=q) |
            Q(guest__full_name__icontains=q)
        ).distinct()
    items, pagination = paginate(qs, request)
    data = [{
        "id": r.pk, "booking_no": r.booking_no,
        "guest": {"id": r.guest.pk, "full_name": r.guest.full_name},
        "status": r.status, "source": r.source,
        "check_in": str(r.check_in), "check_out": str(r.check_out),
        "room_count": r.room_count, "total_amount": str(r.total_amount),
        "currency": r.currency, "source_system": "PMS2",
    } for r in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def reservation_detail_api(request, booking_no):
    """预订详情：主单 + 房间明细 + 住客。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_accommodation"):
        raise ApiError("PERMISSION_DENIED", "缺少预订查看权限。")
    try:
        r = Reservation.objects.select_related("guest").get(
            property=property_obj, booking_no=booking_no)
    except Reservation.DoesNotExist:
        raise ApiError("RESOURCE_NOT_FOUND", "预订不存在。")
    rooms = ReservationRoom.objects.filter(
        reservation=r).select_related("room_type", "assigned_room")
    data = {
        "id": r.pk, "booking_no": r.booking_no,
        "guest": {"id": r.guest.pk, "full_name": r.guest.full_name},
        "status": r.status, "source": r.source,
        "check_in": str(r.check_in), "check_out": str(r.check_out),
        "room_count": r.room_count, "adult_count": r.adult_count,
        "child_count": r.child_count,
        "total_amount": str(r.total_amount), "currency": r.currency,
        "rooms": [{
            "id": rr.pk, "room_type": rr.room_type.name,
            "assigned_room": rr.assigned_room.number if rr.assigned_room else None,
            "check_in": str(rr.check_in), "check_out": str(rr.check_out),
            "amount": str(rr.amount),
        } for rr in rooms],
        "source_system": "PMS2",
    }
    return api_response(request, data)


@require_GET
@api_view
def audit_timeline(request):
    """审计时间线：AuditEvent 按实体/动作检索（P1-14）。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    # P0-04（闭环补齐 2.0）：审计查看用专项权限 core.audit_view，
    # 不再借用 identity.staff_manage（员工管理权 ≠ 审计查看权）
    if not AuthorizationService.has_permission(
            user, property_obj, "core.audit_view"):
        raise ApiError("PERMISSION_DENIED", "缺少审计查看权限。")
    from apps.core.models import AuditEvent
    # P0-07：不依赖模型 Meta，显式稳定排序 `-created_at,-id`；actor 预连接防 N+1。
    qs = AuditEvent.objects.filter(
        property=property_obj).select_related("actor").order_by("-created_at", "-id")
    entity_type = request.GET.get("entity_type")
    if entity_type:
        qs = qs.filter(entity_type=entity_type)
    entity_id = request.GET.get("entity_id")
    if entity_id:
        qs = qs.filter(entity_id=entity_id)
    action = request.GET.get("action")
    if action:
        qs = qs.filter(action__icontains=action)
    items, pagination = paginate(qs, request, page_size_default=30)
    data = [{
        "id": e.pk, "action": e.action,
        "entity_type": e.entity_type, "entity_id": e.entity_id,
        "actor": e.actor.username if e.actor else "system",
        "reason": e.reason or None,
        "before": e.before_json, "after": e.after_json,
        "created_at": e.created_at.isoformat(),
    } for e in items]
    return api_response(request, data, pagination=pagination)
