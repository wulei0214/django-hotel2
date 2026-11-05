# -*- coding: utf-8 -*-
"""Guest 360 + Room 中心只读 API（V1.1 §10 Phase 1：五模块只读原型）。

统一使用 {data, meta} 封套（api_contract）；只读不写；物业范围过滤；
敏感字段按权限脱敏；列表分页。
"""
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.db.models import Prefetch
from django.views.decorators.http import require_GET, require_POST

from apps.core import labels as L
from apps.core.api_contract import (ApiError, api_view, api_response, paginate, idempotent)
from apps.core.models import AuditEvent
from django.db.models import Q
from apps.guests.models import (
    Guest, GuestDocument, GuestMembership, GuestPhone, MembershipLevelHistory,
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
from apps.billing.models import Folio, LedgerEntry, Payment, Refund
from apps.core.models import LegacyRecordLink
from decimal import Decimal

logger = logging.getLogger("hotel.api")

ZERO = Decimal("0.00")


def _guest_dto(guest, user, property_obj, include_detail=False,
               current_stay_map=None):
    """Guest DTO：按权限脱敏。

    P0-07 N+1：guest_list 已 prefetch 电话与当前物业有效会员关系；
    guest_detail 等未 prefetch 的调用方回退为逐条查询，行为不变。
    G10：current_stay_map 可由列表页传入（本页客人 id → 在住 Stay
    的预取映射），消除深层反向链逐行查询；None=调用方未预取，回退
    逐行查询（guest_detail 等单对象调用方，行为不变）。
    """
    cached_phones = getattr(guest, "prefetched_phones", None)
    if cached_phones is None:
        cached_phones = list(GuestPhone.objects.filter(guest=guest))
    primary_phone = next(
        (p for p in cached_phones if p.is_primary), None)
    if primary_phone is None and cached_phones:
        primary_phone = cached_phones[0]
    phone_masked = None
    if primary_phone:
        raw = primary_phone.phone
        phone_masked = raw[:3] + "****" + raw[-4:] if len(raw) >= 8 else raw
    cached_memberships = getattr(guest, "prefetched_memberships", None)
    if cached_memberships is None:
        membership = GuestMembership.objects.filter(
            guest=guest, property=property_obj,
            status=GuestMembership.Status.ACTIVE).first()
    else:
        membership = next(
            (m for m in cached_memberships
             if m.status == GuestMembership.Status.ACTIVE), None)
    if current_stay_map is not None:
        # 预取模式：map 中无键 = 该客人确无在住（不是回退信号）
        current_stay = current_stay_map.get(guest.pk)
    else:
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
            "level_label": L.member_level_label(membership.level_code),
            "status": membership.status,
            "status_label": L.gmembership_status_labels().get(membership.status, membership.status),
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
            for p in cached_phones
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

    qs = Guest.objects.filter(property=property_obj).order_by(
        "-created_at", "-id").prefetch_related(
        # P0-07 N+1：电话与当前物业会员关系一次取回，避免逐行查询。
        Prefetch("phones", queryset=GuestPhone.objects.order_by("id"),
                 to_attr="prefetched_phones"),
        Prefetch("memberships",
                 queryset=GuestMembership.objects.filter(
                     property=property_obj,
                     status=GuestMembership.Status.ACTIVE),
                 to_attr="prefetched_memberships"),
    )
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(full_name__icontains=q) |
            Q(phones__phone_normalized__icontains=q.replace(" ", ""))
        ).distinct()
    items, pagination = paginate(qs, request)
    # G10：本页客人的在住 Stay 一次取回（深层反向链逐行查询是压测
    # P50 1040ms 的主因；50 行 = 50 次跨 4 表 JOIN 查询）
    guest_ids = [g.pk for g in items]
    stay_map = {}
    if guest_ids:
        for stay in Stay.objects.filter(
                property=property_obj, status=Stay.Status.IN_HOUSE,
                reservation_room__reservation__guest_id__in=guest_ids,
        ).select_related("room", "reservation_room").order_by("id"):
            stay_map.setdefault(
                stay.reservation_room.reservation.guest_id, stay)
    data = [_guest_dto(g, user, property_obj,
                       current_stay_map=stay_map) for g in items]
    return api_response(request, data, pagination=pagination)


def _guest_or_404(request, guest_id, permission_code="hotelapp.view_client"):
    """Guest 360 公共门禁：物业上下文 + view_client 权限 + 当前物业对象。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, permission_code):
        raise ApiError("PERMISSION_DENIED", "缺少客人查看权限。")
    try:
        return Guest.objects.get(pk=guest_id, property=property_obj), property_obj
    except Guest.DoesNotExist:
        raise ApiError("RESOURCE_NOT_FOUND", "客人不存在。")


def _guest_folio_queryset(guest, property_obj):
    """客人关联账单（Guest 直挂或经预订关联），账本口径余额在 DTO 层聚合。"""
    return Folio.objects.filter(
        property=property_obj).filter(
        Q(guest=guest) | Q(reservation__guest=guest)).distinct()


def _folio_balances_for(folios):
    """账本口径余额：带符号 LedgerEntry 汇总（与 billing_center 同口径）。"""
    from django.db.models import Sum
    aggregated = {
        row["folio"]: row["total"]
        for row in LedgerEntry.objects.filter(
            folio__in=[f.pk for f in folios]).values("folio").annotate(
            total=Sum("amount"))
    }
    return {folio.pk: aggregated.get(folio.pk, ZERO) for folio in folios}


def _folio_summary_dto(folio, balance):
    return {
        "id": folio.pk,
        "folio_type": folio.folio_type,
        "folio_type_label": folio.get_folio_type_display(),
        "status": folio.status,
        "status_label": folio.get_status_display(),
        "currency": folio.currency,
        "balance": str(Decimal(balance).quantize(Decimal("0.01"))),
        "booking_no": (folio.reservation.booking_no
                       if folio.reservation_id else None),
        "opened_at": folio.opened_at.isoformat() if folio.opened_at else None,
        "closed_at": folio.closed_at.isoformat() if folio.closed_at else None,
        "source_system": "PMS2",
    }


@require_GET
@api_view
def guest_detail(request, guest_id):
    """Guest 360 详情：档案 + 证件 + 电话 + 会员 + 预订/入住/账单/旧映射摘要。

    基线 2.0 §3.2 闭环口径：一次详情聚合展示预订历史、Stay、Folio（账本
    口径余额）、会员与 LegacyRecordLink（旧 Client/Vip → Guest 映射），
    全部只读聚合，不重复落库。
    """
    user = request.user
    guest, property_obj = _guest_or_404(request, guest_id)

    reservations = Reservation.objects.filter(
        property=property_obj, guest=guest).order_by("-created_at", "-id")[:10]
    _res_labels = L.reservation_status_labels()
    reservation_list = [{
        "booking_no": r.booking_no, "status": r.status,
        "status_label": _res_labels.get(r.status, r.status),
        "check_in": str(r.check_in), "check_out": str(r.check_out),
        "total_amount": str(r.total_amount), "rooms": r.room_count,
    } for r in reservations]

    stays = Stay.objects.filter(
        property=property_obj,
        reservation_room__reservation__guest=guest,
    ).select_related(
        "room", "reservation_room", "reservation_room__reservation",
    ).order_by("-created_at", "-id")[:10]
    stay_list = [{
        "id": s.pk,
        "status": s.status,
        "room_number": s.room.number if s.room_id else None,
        "booking_no": s.reservation_room.reservation.booking_no,
        "check_in": str(s.reservation_room.check_in),
        "check_out": str(s.reservation_room.check_out),
        "actual_check_in": (s.actual_check_in.isoformat()
                            if s.actual_check_in else None),
        "actual_check_out": (s.actual_check_out.isoformat()
                             if s.actual_check_out else None),
    } for s in stays]

    folios = _guest_folio_queryset(guest, property_obj).order_by(
        "-opened_at", "-id")[:10]
    balances = _folio_balances_for(folios)
    folio_list = [_folio_summary_dto(f, balances.get(f.pk, ZERO)) for f in folios]

    legacy_links = LegacyRecordLink.objects.filter(
        property=property_obj, target_model="Guest",
        target_pk=str(guest.pk)).order_by("id")
    legacy_list = [{
        "id": link.pk,
        "source_system": link.source_system,
        "source_model": link.source_model,
        "source_pk": link.source_pk,
        "mapping_status": link.mapping_status,
        "match_method": link.match_method or None,
        "reviewed_at": (link.reviewed_at.isoformat()
                        if getattr(link, "reviewed_at", None) else None),
    } for link in legacy_links]

    data = _guest_dto(guest, user, property_obj, include_detail=True)
    data["reservations"] = reservation_list
    data["stays"] = stay_list
    data["folios"] = folio_list
    data["legacy_links"] = legacy_list
    return api_response(request, data)


@require_GET
@api_view
def guest_folios(request, guest_id):
    """GET /api/v1/guests/{id}/folios — 客人账单摘要（分页；账本口径余额）。

    基线 2.0 §3.2 接口表：账单摘要按角色脱敏——本端点只返回 Folio 头与
    余额汇总，不展开逐条凭证；明细需另持 hotelapp.finance_access 访问
    /api/v1/billing/folios/{id}。
    """
    guest, property_obj = _guest_or_404(request, guest_id)
    folios = _guest_folio_queryset(guest, property_obj).select_related(
        "reservation").order_by("-opened_at", "-id")
    items, pagination = paginate(folios, request)
    balances = _folio_balances_for(items)
    data = [_folio_summary_dto(f, balances.get(f.pk, ZERO)) for f in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def guest_timeline(request, guest_id):
    """GET /api/v1/guests/{id}/timeline — 客人 360 操作时间线（基线 2.0 §3.2）。

    只读聚合六类事实并按时间倒序合并（同一时刻按插入序稳定输出）：
      * 预订（创建/取消等状态以当前状态展示）；
      * 入住/退房（Stay actual_check_in/out，未发生的入住不产生时间点）；
      * 账务凭证（LedgerEntry，含冲销关联）；
      * 支付与退款（Payment/Refund 状态）；
      * 会员等级历史（MembershipLevelHistory）；
      * 审计事件（AuditEvent：Guest 实体 + 会员/Pii 动作）。
    默认 limit=100（10-200），审计事件不返回 before/after 原文，仅摘要。
    """
    guest, property_obj = _guest_or_404(request, guest_id)
    try:
        limit = int(request.GET.get("limit", "100"))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "limit 必须是 10 至 200 的整数。")
    limit = max(10, min(200, limit))

    events = []

    for r in Reservation.objects.filter(
            property=property_obj, guest=guest).order_by("-created_at", "-id")[:limit]:
        events.append({
            "time": r.created_at.isoformat(), "kind": "reservation",
            "summary": {"booking_no": r.booking_no, "status": r.status,
                        "status_label": L.reservation_status_labels().get(
                            r.status, r.status),
                        "total_amount": str(r.total_amount)},
        })

    stay_rows = Stay.objects.filter(
        property=property_obj,
        reservation_room__reservation__guest=guest,
    ).select_related(
        "room", "reservation_room", "reservation_room__reservation",
    ).order_by("-created_at", "-id")[:limit]
    for s in stay_rows:
        booking_no = s.reservation_room.reservation.booking_no
        if s.actual_check_in:
            events.append({
                "time": s.actual_check_in.isoformat(), "kind": "stay.check_in",
                "summary": {"stay_id": s.pk, "booking_no": booking_no,
                            "room": s.room.number if s.room_id else None},
            })
        if s.actual_check_out:
            events.append({
                "time": s.actual_check_out.isoformat(), "kind": "stay.check_out",
                "summary": {"stay_id": s.pk, "booking_no": booking_no,
                            "room": s.room.number if s.room_id else None},
            })

    ledger_rows = LedgerEntry.objects.filter(
        property=property_obj).filter(
        Q(folio__guest=guest) | Q(folio__reservation__guest=guest),
    ).select_related("folio").order_by("-created_at", "-id")[:limit]
    for e in ledger_rows:
        events.append({
            "time": e.created_at.isoformat(), "kind": "ledger_entry",
            "summary": {"entry_id": e.pk, "entry_type": e.entry_type,
                        "amount": str(e.amount), "currency": e.currency,
                        "business_date": e.business_date.isoformat(),
                        "folio_id": e.folio_id,
                        "void_of_id": e.void_of_id},
        })

    payment_rows = Payment.objects.filter(
        property=property_obj).filter(
        Q(folio__guest=guest) | Q(folio__reservation__guest=guest),
    ).order_by("-created_at", "-id")[:limit]
    for p in payment_rows:
        events.append({
            "time": p.created_at.isoformat(), "kind": "payment",
            "summary": {"payment_id": p.pk, "method": p.method,
                        "amount": str(p.amount), "currency": p.currency,
                        "status": p.status},
        })

    refund_rows = Refund.objects.filter(
        payment__property=property_obj).filter(
        Q(payment__folio__guest=guest)
        | Q(payment__folio__reservation__guest=guest),
    ).select_related("payment").order_by("-created_at", "-id")[:limit]
    for rf in refund_rows:
        events.append({
            "time": rf.created_at.isoformat(), "kind": "refund",
            "summary": {"refund_id": rf.pk, "amount": str(rf.amount),
                        "currency": rf.currency, "status": rf.status,
                        "payment_id": rf.payment_id},
        })

    for h in MembershipLevelHistory.objects.filter(
            membership__guest=guest,
            membership__property=property_obj).order_by("-created_at", "-id")[:limit]:
        events.append({
            "time": h.created_at.isoformat(), "kind": "membership.level_change",
            "summary": {"from_level": h.from_level or None,
                        "to_level": h.to_level, "reason": h.reason or None},
        })

    audit_rows = AuditEvent.objects.filter(
        property=property_obj, entity_type="Guest",
        entity_id=str(guest.pk)).order_by("-created_at", "-id")[:limit]
    for ev in audit_rows:
        events.append({
            "time": ev.created_at.isoformat(), "kind": "audit",
            "summary": {"action": ev.action, "entity_type": ev.entity_type,
                        "reason": ev.reason or None,
                        "actor": (ev.actor.username if ev.actor_id else None)},
        })

    # 时间倒序合并；Python 排序稳定——同一时刻保持上列业务顺序（事实先于审计）
    events.sort(key=lambda item: item["time"], reverse=True)
    return api_response(request, {
        "guest_id": guest.pk,
        "count": len(events[:limit]),
        "items": events[:limit],
    })


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

    qs = Room.objects.filter(
        property=property_obj, is_active=True
    ).select_related("room_type").order_by("number", "id")
    floor = request.GET.get("floor")
    if floor:
        qs = qs.filter(floor=floor)
    hk = request.GET.get("housekeeping")
    if hk:
        qs = qs.filter(housekeeping_status=hk)
    room_type_id = request.GET.get("room_type_id")
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(number__icontains=q) | Q(room_type__name__icontains=q))

    items, pagination = paginate(qs, request)
    data = [{
        "id": r.pk, "number": r.number, "floor": r.floor or None,
        "room_type": {"id": r.room_type.pk, "code": r.room_type.code,
                      "name": r.room_type.name},
        "occupancy_status": r.occupancy_status,
        "occupancy_label": L.room_label_maps()["occupancy"].get(r.occupancy_status, r.occupancy_status),
        "housekeeping_status": r.housekeeping_status,
        "housekeeping_label": L.room_label_maps()["housekeeping"].get(r.housekeeping_status, r.housekeeping_status),
        "maintenance_status": r.maintenance_status,
        "maintenance_label": L.room_label_maps()["maintenance"].get(r.maintenance_status, r.maintenance_status),
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
    try:
        days = min(30, max(1, int(request.GET.get("days", "14"))))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "days 必须是 1 至 30 的整数。")
    try:
        start_date = date.fromisoformat(start)
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "start 必须使用 YYYY-MM-DD 格式。")
    end_date = start_date + timedelta(days=days)
    room_type_id = request.GET.get("room_type_id")

    # P0-07：同一 stay_date 会有多个房型/房价计划行，显式追加主键保证稳定排序；
    # room_type/rate_plan 预连接防 N+1。
    inv_qs = RoomTypeInventory.objects.filter(
        property=property_obj, stay_date__gte=start_date,
        stay_date__lt=end_date).select_related("room_type").order_by(
        "stay_date", "room_type_id", "id")
    if room_type_id:
        inv_qs = inv_qs.filter(room_type_id=room_type_id)

    rate_qs = DailyRate.objects.filter(
        property=property_obj, stay_date__gte=start_date,
        stay_date__lt=end_date).select_related("room_type", "rate_plan").order_by(
        "stay_date", "room_type_id", "rate_plan_id", "id")
    if room_type_id:
        rate_qs = rate_qs.filter(room_type_id=room_type_id)

    inventory_data = [{
        "room_type": {"id": inv.room_type.pk, "code": inv.room_type.code,
                      "name": inv.room_type.name},
        "stay_date": str(inv.stay_date),
        "total_rooms": inv.total_rooms, "held": inv.held,
        "sold": inv.sold, "out_of_service": inv.out_of_service,
        "available_count": inv.available_count,
        "stop_sell": inv.stop_sell,
    } for inv in inv_qs]
    rate_data = [{
        "room_type": {"id": dr.room_type.pk, "code": dr.room_type.code,
                      "name": dr.room_type.name},
        "rate_plan": {"id": dr.rate_plan.pk, "code": dr.rate_plan.code,
                      "name": dr.rate_plan.name},
        "stay_date": str(dr.stay_date),
        "price": str(dr.price), "currency": dr.currency,
        "currency_label": L.currency_label(dr.currency),
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


@require_POST
@api_view
@idempotent
def adjust_daily_rate_api(request, rate_id):
    """每日房价调整 API：店长独立权限、业务原因和领域服务审计。"""
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            request.user, property_obj, "inventory.manage_rates"):
        raise ApiError("PERMISSION_DENIED", "当前岗位没有房价调整权限。")
    body = _parse_body(request)
    from apps.inventory.services import adjust_daily_rate
    try:
        rate, changed = adjust_daily_rate(
            property_obj=property_obj, rate_id=rate_id,
            price=body.get("price"), reason=body.get("reason"),
            updated_by=request.user, request=request)
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {
        "daily_rate_id": rate.pk, "room_type_id": rate.room_type_id,
        "rate_plan_id": rate.rate_plan_id,
        "stay_date": rate.stay_date.isoformat(),
        "price": str(rate.price), "currency": rate.currency,
        "changed": changed,
    })


@require_GET
@api_view
def financial_summary_api(request):
    """财务汇总（P1-08/Q-13）：值班经理看汇总；完整流水仅财务/店长。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.finance_access"):
        raise ApiError("PERMISSION_DENIED", "缺少财务管理权限。")
    from datetime import date, timedelta
    from apps.billing.models import LedgerEntry
    from apps.billing.services_v2 import BillingService
    end = date.today()
    start = end - timedelta(days=7)
    summary = BillingService.financial_summary(
        property_obj=property_obj, start_date=start, end_date=end)
    summary["period"] = {"start": str(start), "end": str(end)}
    # P0-07 统计口径注记：物业、营业日/自然日、状态范围、币种、生成时间。
    currencies = sorted(set(LedgerEntry.objects.filter(
        property=property_obj, business_date__gte=start,
        business_date__lte=end).values_list("currency", flat=True)))
    summary["metadata"] = {
        "property": {"code": property_obj.code, "name": property_obj.name},
        "period": {"start": str(start), "end": str(end)},
        "date_basis": {
            "ledger_entries": "营业日（business_date）",
            "payments_refunds": "自然日（created_at）",
        },
        "status_scope": {
            "ledger_entries": "全部状态",
            "payments": "全部状态",
            "refunds": "全部状态",
        },
        "currency": currencies or ["CNY"],
        "generated_at": timezone.now().isoformat(),
        "source_system": "PMS2",
    }
    # P1-02（方案 §9.2）：财务明细查看 = hotelapp.finance_access；
    # 明细可见性不使用 refund_approve/staff_manage 替代，superuser 不自动获得业务授权
    summary["detail_available"] = AuthorizationService.has_permission(
        user, property_obj, "hotelapp.finance_access")
    if not summary["detail_available"]:
        summary["note"] = "值班经理仅可查看汇总；完整流水请联系财务或店长。"
    return api_response(request, summary)
