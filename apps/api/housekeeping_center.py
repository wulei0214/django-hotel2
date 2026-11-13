# -*- coding: utf-8 -*-
"""清洁与维修工作台 API（基线 2.0 §4.3）。

围绕 Room 三维状态（占用/清洁/维修互相独立）提供工作清单、统计与两个写入口；
全部写操作走 apps.inventory.services 领域服务（状态流转合法性、物业校验、
AuditEvent 均在服务层强制），本模块只做认证/权限/参数边界。

权限口径（与 registry.py、页面按钮显隐及测试一致）：
  * 读（工作清单/统计）：inventory.housekeeping_view；
  * 清洁链流转（清洁中/待检查/检查退回）：inventory.housekeeping_update；
  * 净房放行（待检查→净房）：额外要求 hotelapp.room_release_ready；
  * 维修停用/恢复登记：inventory.maintenance_manage，独立于未到订单处理权限。

响应封套：统一 {data, meta}（apps.core.api_contract），列表端点统一分页契约。
"""

from django.views.decorators.http import require_GET, require_POST

from apps.core.api_contract import ApiError, api_response, api_view, paginate
from apps.identity.authorization import AuthorizationService
from .night_audit import _body  # 与夜审中心同口径的 JSON 请求体解析
from apps.inventory.models import Room
from apps.inventory.services import (
    HousekeepingOperationError,
    change_housekeeping_status,
    change_maintenance_status,
    housekeeping_summary,
)


def _context(request, permission):
    """员工门禁 + 当前物业 + 指定权限码（与 night_audit._context 同构）。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if not user.is_authenticated or not user.is_active:
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, permission):
        raise ApiError("PERMISSION_DENIED", "当前岗位没有清洁与维修工作台权限。")
    return property_obj


def _room_dto(room, labels=None):
    labels = labels or _room_labels()
    return {
        "id": room.pk,
        "number": room.number,
        "floor": room.floor or "",
        "room_type": {"id": room.room_type_id, "code": room.room_type.code,
                      "name": room.room_type.name},
        "occupancy_status": room.occupancy_status,
        "occupancy_label": labels["occupancy"].get(
            room.occupancy_status, room.occupancy_status),
        "housekeeping_status": room.housekeeping_status,
        "housekeeping_label": labels["housekeeping"].get(
            room.housekeeping_status, room.housekeeping_status),
        "maintenance_status": room.maintenance_status,
        "maintenance_label": labels["maintenance"].get(
            room.maintenance_status, room.maintenance_status),
        "is_sellable_now": room.is_sellable_now,
        "source_system": "PMS2",
    }


def _room_labels():
    from apps.core import labels as L
    return L.room_label_maps()


def _require_room(property_obj, room_id):
    """物业隔离预检：跨物业/不存在的房间一律 404（服务层保留同校验兜底）。"""
    if not Room.objects.filter(pk=room_id, property=property_obj).exists():
        raise ApiError("RESOURCE_NOT_FOUND", "房间不存在或不属于当前物业。")


# ---------------------------------------------------------------------------
# 读取端点
# ---------------------------------------------------------------------------

@require_GET
@api_view
def housekeeping_rooms(request):
    """清洁/维修工作清单：按楼层、清洁状态、维修状态筛选；稳定排序。"""
    property_obj = _context(request, "inventory.housekeeping_view")
    queryset = (
        Room.objects.filter(property=property_obj, is_active=True)
        .select_related("room_type")
        .order_by("floor", "number", "id")
    )
    floor = (request.GET.get("floor") or "").strip()
    if floor:
        queryset = queryset.filter(floor=floor)
    housekeeping = (request.GET.get("housekeeping") or "").strip()
    if housekeeping:
        if housekeeping not in Room.HousekeepingStatus.values:
            raise ApiError(
                "VALIDATION_ERROR",
                "housekeeping 只支持 CLEAN/DIRTY/CLEANING/INSPECTING。")
        queryset = queryset.filter(housekeeping_status=housekeeping)
    maintenance = (request.GET.get("maintenance") or "").strip()
    if maintenance:
        if maintenance not in Room.MaintenanceStatus.values:
            raise ApiError(
                "VALIDATION_ERROR",
                "maintenance 只支持 NORMAL/OUT_OF_ORDER/OUT_OF_SERVICE。")
        queryset = queryset.filter(maintenance_status=maintenance)
    occupancy = (request.GET.get("occupancy") or "").strip()
    if occupancy:
        if occupancy not in Room.OccupancyStatus.values:
            raise ApiError("VALIDATION_ERROR", "occupancy 只支持 VACANT/OCCUPIED。")
        queryset = queryset.filter(occupancy_status=occupancy)

    items, pagination = paginate(queryset, request)
    labels = _room_labels()
    return api_response(
        request, [_room_dto(room, labels) for room in items],
        pagination=pagination)


@require_GET
@api_view
def housekeeping_summary_api(request):
    """今日清洁统计：各状态计数、待放行数、楼层分布与今日审计流转量。"""
    property_obj = _context(request, "inventory.housekeeping_view")
    data = housekeeping_summary(property_obj)
    data["labels"] = _room_labels()
    return api_response(request, data)


# ---------------------------------------------------------------------------
# 写入端点（领域服务唯一入口）
# ---------------------------------------------------------------------------

@require_POST
@api_view
def housekeeping_status_change(request, room_id):
    """清洁状态流转：DIRTY→CLEANING→INSPECTING→CLEAN 链式规则（§4.3）。

    * inventory.housekeeping_update：清洁链推进与检查退回（客房服务员在列）；
    * →CLEAN 净房放行：额外要求 room_release_ready（服务层复核同一权限）。
    """
    property_obj = _context(request, "inventory.housekeeping_update")
    body = _body(request)
    target = str(body.get("status") or "").strip()
    if not target:
        raise ApiError("VALIDATION_ERROR", "status 不能为空。")
    if target not in Room.HousekeepingStatus.values:
        raise ApiError(
            "VALIDATION_ERROR",
            "status 只支持 CLEAN/DIRTY/CLEANING/INSPECTING。")

    if (target == Room.HousekeepingStatus.CLEAN
            and not AuthorizationService.has_permission(
                request.user, property_obj, "hotelapp.room_release_ready")):
        raise ApiError(
            "PERMISSION_DENIED",
            "净房放行需要 hotelapp.room_release_ready 权限（值班经理/店长）。")

    _require_room(property_obj, room_id)
    try:
        room, changed = change_housekeeping_status(
            property_obj=property_obj, room_id=room_id,
            target_status=target, changed_by=request.user, request=request,
            notes=body.get("notes") or "")
    except HousekeepingOperationError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    data = _room_dto(room)
    data["changed"] = changed
    return api_response(request, data)


@require_POST
@api_view
def housekeeping_maintenance_change(request, room_id):
    """维修状态登记：OUT_OF_ORDER/OUT_OF_SERVICE/NORMAL，reason 必填。

    门禁使用 inventory.maintenance_manage（值班经理/店长专项权限口径），与未到订单处理分离。
    """
    property_obj = _context(request, "inventory.maintenance_manage")
    body = _body(request)
    target = str(body.get("status") or "").strip()
    if not target:
        raise ApiError("VALIDATION_ERROR", "status 不能为空。")
    if target not in Room.MaintenanceStatus.values:
        raise ApiError(
            "VALIDATION_ERROR",
            "status 只支持 NORMAL/OUT_OF_ORDER/OUT_OF_SERVICE。")
    reason = str(body.get("reason") or "").strip()
    if not reason or len(reason) > 240:
        raise ApiError(
            "VALIDATION_ERROR", "维修登记必须填写 1 至 240 个字符的原因。")

    _require_room(property_obj, room_id)
    try:
        room, changed = change_maintenance_status(
            property_obj=property_obj, room_id=room_id,
            target_status=target, reason=reason,
            changed_by=request.user, request=request)
    except HousekeepingOperationError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    data = _room_dto(room)
    data["changed"] = changed
    return api_response(request, data)
