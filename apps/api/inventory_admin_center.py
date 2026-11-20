"""房态库存中心受控写 API（MC-004）。

房间/房型主数据、关房/开房、停售/开售、库存差异修复申请均通过既有领域服务，
不在视图里直接改模型；available_count 始终为派生事实。
"""

import json

from django.db import IntegrityError
from django.views.decorators.http import require_GET, require_POST

from apps.core.api_contract import ApiError, api_response, api_view, idempotent, paginate
from apps.identity.authorization import AuthorizationService
from apps.inventory import reconciliation
from apps.inventory import services_inventory_admin as admin_services
from apps.inventory.models import InventoryDiscrepancy, RoomAssignmentConflict
from apps.inventory.services import HousekeepingOperationError


def _context(request, permission):
    user = request.user
    if not user.is_authenticated or not user.is_active:
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, permission):
        raise ApiError("PERMISSION_DENIED", "当前岗位无权维护房态库存。")
    return property_obj, user


def _body(request):
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是合法 JSON 对象。") from None
    if not isinstance(payload, dict):
        raise ApiError("VALIDATION_ERROR", "请求体必须是 JSON 对象。")
    return payload


def _map_error(exc):
    if isinstance(exc, HousekeepingOperationError):
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    if isinstance(exc, ValueError):
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    raise exc


def _rate_dto(rate):
    return {
        "id": rate.pk,
        "room_type_id": rate.room_type_id,
        "rate_plan_id": rate.rate_plan_id,
        "stay_date": rate.stay_date.isoformat(),
        "price": str(rate.price),
        "currency": rate.currency,
        "closed": rate.closed,
    }


def _stock_dto(row):
    return {
        "id": row.pk,
        "room_type_id": row.room_type_id,
        "stay_date": row.stay_date.isoformat(),
        "total_rooms": row.total_rooms,
        "held": row.held,
        "sold": row.sold,
        "out_of_service": row.out_of_service,
        "stop_sell": row.stop_sell,
        "available_count": row.available_count,
        "version": row.version,
    }


def _room_type_dto(room_type):
    return {
        "id": room_type.pk,
        "code": room_type.code,
        "name": room_type.name,
        "capacity": room_type.capacity,
        "bed_type": room_type.bed_type,
        "amenities": room_type.amenities,
        "is_active": room_type.is_active,
    }


def _room_dto(room):
    return {
        "id": room.pk,
        "room_type_id": room.room_type_id,
        "number": room.number,
        "floor": room.floor,
        "occupancy_status": room.occupancy_status,
        "housekeeping_status": room.housekeeping_status,
        "maintenance_status": room.maintenance_status,
        "is_active": room.is_active,
    }


def _discrepancy_dto(disc):
    return {
        "id": disc.pk,
        "room_type_id": disc.room_type_id,
        "stay_date": disc.stay_date.isoformat(),
        "current_total": disc.current_total,
        "recomputed_total": disc.recomputed_total,
        "diff": disc.diff,
        "held_count": disc.held_count,
        "sold_count": disc.sold_count,
        "oos_count": disc.oos_count,
        "status": disc.status,
        "fix_reason": disc.fix_reason,
        "fix_evidence": disc.fix_evidence,
        "requested_by": disc.requested_by_id,
        "approved_by": disc.approved_by_id,
        "executed_by": disc.executed_by_id,
    }


@require_POST
@api_view
@idempotent
def daily_rate_close(request, rate_id):
    property_obj, user = _context(request, "inventory.manage_rates")
    payload = _body(request)
    reason = str(payload.get("reason", "") or "")
    try:
        rate, changed = admin_services.set_daily_rate_closed(
            property_obj=property_obj,
            rate_id=rate_id,
            closed=bool(payload.get("closed", True)),
            reason=reason,
            actor=user,
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, {**_rate_dto(rate), "changed": changed})


@require_POST
@api_view
@idempotent
def stock_stop_sell(request, inventory_id):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    payload = _body(request)
    try:
        row, changed = admin_services.set_inventory_stop_sell(
            property_obj=property_obj,
            inventory_id=inventory_id,
            stop_sell=bool(payload.get("stop_sell", True)),
            reason=str(payload.get("reason", "") or ""),
            actor=user,
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, {**_stock_dto(row), "changed": changed})


@require_POST
@api_view
@idempotent
def room_type_create(request):
    property_obj, user = _context(request, "hotelapp.change_room")
    payload = _body(request)
    try:
        room_type = admin_services.create_room_type(
            property_obj=property_obj,
            code=payload.get("code"),
            name=payload.get("name"),
            capacity=payload.get("capacity", 2),
            bed_type=payload.get("bed_type", ""),
            amenities=payload.get("amenities", []),
            actor=user,
            reason=payload.get("reason", ""),
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    except ValueError as exc:
        # MC11-INV-03：capacity 非整数等入参错误 → 400（对齐 room_create 口径）
        raise ApiError("VALIDATION_ERROR", str(exc)) from None
    except IntegrityError as exc:
        # MC11-INV-03：房型 code 物业内唯一约束违反 → 409 业务冲突
        raise ApiError("BUSINESS_CONFLICT", "房型代码在该物业已存在。") from None
    return api_response(request, _room_type_dto(room_type), 201)


@require_POST
@api_view
@idempotent
def room_type_update(request, room_type_id):
    property_obj, user = _context(request, "hotelapp.change_room")
    payload = _body(request)
    reason = payload.pop("reason", "")
    try:
        room_type, changed = admin_services.update_room_type(
            property_obj=property_obj,
            room_type_id=room_type_id,
            actor=user,
            reason=reason,
            **payload,
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    except ValueError as exc:
        # MC11-INV-03：capacity 等入参错误 → 400（对齐 create/update 同口径）
        raise ApiError("VALIDATION_ERROR", str(exc)) from None
    return api_response(request, {**_room_type_dto(room_type), "changed": changed})


@require_POST
@api_view
@idempotent
def room_create(request):
    property_obj, user = _context(request, "hotelapp.change_room")
    payload = _body(request)
    try:
        room = admin_services.create_room(
            property_obj=property_obj,
            room_type_id=payload.get("room_type_id"),
            number=payload.get("number"),
            floor=payload.get("floor", ""),
            actor=user,
            reason=payload.get("reason", ""),
        )
    except (HousekeepingOperationError, ValueError) as exc:
        _map_error(exc)
    except IntegrityError as exc:
        # MC11-INV-03：房间号物业内唯一约束违反 → 409 业务冲突
        raise ApiError("BUSINESS_CONFLICT", "房间号在该物业已存在。") from None
    return api_response(request, _room_dto(room), 201)


@require_POST
@api_view
@idempotent
def room_update(request, room_id):
    property_obj, user = _context(request, "hotelapp.change_room")
    payload = _body(request)
    reason = payload.pop("reason", "")
    try:
        room, changed = admin_services.update_room(
            property_obj=property_obj, room_id=room_id, actor=user, reason=reason, **payload
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, {**_room_dto(room), "changed": changed})


@require_GET
@api_view
def discrepancy_list(request):
    property_obj, _ = _context(request, "inventory.housekeeping_view")
    qs = (
        InventoryDiscrepancy.objects.filter(property=property_obj)  # type: ignore[has-type]
        .select_related("room_type")
        .order_by("-stay_date", "-id")
    )
    status = request.GET.get("status", "").strip()
    if status:
        if status not in InventoryDiscrepancy.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 不是合法差异状态。")
        qs = qs.filter(status=status)
    items, pagination = paginate(qs, request)
    return api_response(
        request, {"items": [_discrepancy_dto(disc) for disc in items]}, pagination=pagination
    )


@require_GET
@api_view
def assignment_conflict_list(request):
    """MC11-INV-02：排房冲突只读列表（此前对管理者完全不可见，仅调度留痕）。

    只报告不改派（M4-4 约束）；修复经前台换房/取消后由任务中心复核闭环。"""
    property_obj, _ = _context(request, "inventory.housekeeping_view")
    qs = (
        RoomAssignmentConflict.objects.filter(property=property_obj)  # type: ignore[has-type]
        .select_related("room", "room_type")
        .order_by("-stay_date", "-id")
    )
    status = request.GET.get("status", "").strip()
    if status:
        if status not in RoomAssignmentConflict.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 只支持 OPEN/DISMISSED。")
        qs = qs.filter(status=status)
    items, pagination = paginate(qs, request)
    return api_response(
        request,
        {
            "items": [
                {
                    "id": conflict.pk,
                    "room_number": conflict.room.number,
                    "room_type_code": conflict.room_type.code,
                    "stay_date": conflict.stay_date.isoformat(),
                    "conflict_count": conflict.conflict_count,
                    "details": conflict.details,
                    "status": conflict.status,
                    "status_label": conflict.get_status_display(),
                    "created_at": conflict.created_at.isoformat(),
                    "source_system": "PMS2",
                }
                for conflict in items
            ]
        },
        pagination=pagination,
    )


@require_POST
@api_view
@idempotent
def discrepancy_scan(request):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    days_back = _body(request).get("days_back", 30)
    if isinstance(days_back, bool) or not isinstance(days_back, int) or not 1 <= days_back <= 180:
        raise ApiError("VALIDATION_ERROR", "days_back 必须是 1-180 的整数。")
    run = reconciliation.scan_inventory_discrepancies(
        property_obj=property_obj, days_back=days_back, checked_by=user
    )
    return api_response(
        request,
        {
            "run_id": run.pk,
            "diff_count": run.diff_count,
            "scanned_dates": run.scanned_dates,
            "scanned_types": run.scanned_types,
        },
    )


def _discrepancy_action(request, discrepancy_id, action):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    if not InventoryDiscrepancy.objects.filter(  # type: ignore[has-type]
        pk=discrepancy_id, property=property_obj
    ).exists():
        raise ApiError("RESOURCE_NOT_FOUND", "库存差异不存在或不属于当前物业。")
    payload = _body(request)
    try:
        if action == "fix-request":
            disc = reconciliation.request_inventory_fix(
                property_obj=property_obj,
                discrepancy_id=discrepancy_id,
                fix_reason=payload.get("fix_reason", ""),
                fix_evidence=payload.get("fix_evidence", ""),
                requested_by=user,
            )
        elif action == "approve":
            disc = reconciliation.approve_inventory_fix(
                property_obj=property_obj, discrepancy_id=discrepancy_id, approved_by=user
            )
        elif action == "execute":
            disc = reconciliation.execute_inventory_fix(
                property_obj=property_obj, discrepancy_id=discrepancy_id, executed_by=user
            )
        else:
            disc = reconciliation.dismiss_discrepancy(
                property_obj=property_obj,
                discrepancy_id=discrepancy_id,
                dismissed_by=user,
                reason=payload.get("reason", ""),
            )
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc)) from None
    return api_response(request, _discrepancy_dto(disc))


@require_POST
@api_view
@idempotent
def discrepancy_fix_request(request, discrepancy_id):
    return _discrepancy_action(request, discrepancy_id, "fix-request")


@require_POST
@api_view
@idempotent
def discrepancy_approve(request, discrepancy_id):
    return _discrepancy_action(request, discrepancy_id, "approve")


@require_POST
@api_view
@idempotent
def discrepancy_execute(request, discrepancy_id):
    return _discrepancy_action(request, discrepancy_id, "execute")


@require_POST
@api_view
@idempotent
def discrepancy_dismiss(request, discrepancy_id):
    return _discrepancy_action(request, discrepancy_id, "dismiss")
