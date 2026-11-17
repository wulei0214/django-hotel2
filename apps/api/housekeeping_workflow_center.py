"""清洁/维修任务级工作台 API（MC-006）。

房间级状态流转入口保留在 housekeeping_center.py；本模块提供任务级命令，
所有写动作经 apps.inventory.services_housekeeping_tasks 调用既有领域服务，
任务/房态/库存同事务，失败整体回滚。
"""

import json
from datetime import date
from django.db.models import Q
from django.contrib.auth.models import User


from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.api_contract import (
    ApiError,
    api_response,
    api_view,
    idempotent,
    paginate,
)
from apps.identity.authorization import AuthorizationService
from apps.identity.models import StaffProfile
from apps.inventory import services_housekeeping_tasks as task_services
from apps.inventory.models import HousekeepingTask, MaintenanceTicket
from apps.inventory.services import HousekeepingOperationError


def _context(request, permission):
    user = request.user
    if not user.is_authenticated or not user.is_active:
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, permission):
        raise ApiError("PERMISSION_DENIED", "当前岗位无权访问清洁维修任务。")
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


def _int(payload, field, *, required=True):
    value = payload.get(field)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError("VALIDATION_ERROR", f"{field} 必须是整数。")
    return value


def _day(value, field):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ApiError("VALIDATION_ERROR", f"{field} 必须是 YYYY-MM-DD。") from None


def _map_error(exc):
    if isinstance(exc, HousekeepingOperationError):
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    if isinstance(exc, ValueError):
        raise ApiError("VALIDATION_ERROR", str(exc))
    raise exc


def _task_dto(task):
    return {
        "id": task.pk,
        "room_id": task.room_id,
        "room_number": task.room.number,
        "task_type": task.task_type,
        "task_type_label": task.get_task_type_display(),
        "status": task.status,
        "status_label": task.get_status_display(),
        "priority": task.priority,
        "assigned_to": task.assigned_to_id,
        "created_by": task.created_by_id,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "completed_by": task.completed_by_id,
        "inspected_by": task.inspected_by_id,
        "inspected_at": task.inspected_at.isoformat() if task.inspected_at else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "overdue": bool(task.due_at and task.due_at < timezone.now()),
        "checklist": task.checklist,
        "notes": task.notes,
        "created_at": task.created_at.isoformat(),
        "source_system": "PMS2",
    }


def _ticket_dto(ticket):
    return {
        "id": ticket.pk,
        "ticket_no": ticket.ticket_no,
        "room_id": ticket.room_id,
        "room_number": ticket.room.number,
        "category": ticket.category,
        "severity": ticket.severity,
        "status": ticket.status,
        "status_label": ticket.get_status_display(),
        "reason": ticket.reason,
        "from_date": ticket.from_date.isoformat(),
        "to_date": ticket.to_date.isoformat() if ticket.to_date else None,
        "reported_by": ticket.reported_by_id,
        "assigned_to": ticket.assigned_to_id,
        "started_at": ticket.started_at.isoformat() if ticket.started_at else None,
        "verified_by": ticket.verified_by_id,
        "verified_at": ticket.verified_at.isoformat() if ticket.verified_at else None,
        "restored_by": ticket.restored_by_id,
        "restored_at": ticket.restored_at.isoformat() if ticket.restored_at else None,
        "notes": ticket.notes,
        "created_at": ticket.created_at.isoformat(),
        "source_system": "PMS2",
    }


def _task_or_404(property_obj, task_id):
    task = (
        HousekeepingTask.objects.select_related("room")
        .filter(pk=task_id, property=property_obj)
        .first()
    )
    if task is None:
        raise ApiError("RESOURCE_NOT_FOUND", "清洁任务不存在或不属于当前物业。")
    return task


def _ticket_or_404(property_obj, ticket_id):
    ticket = (
        MaintenanceTicket.objects.select_related("room")
        .filter(pk=ticket_id, property=property_obj)
        .first()
    )
    if ticket is None:
        raise ApiError("RESOURCE_NOT_FOUND", "维修工单不存在或不属于当前物业。")
    return ticket


def _profile(user):
    profile = StaffProfile.objects.filter(user=user).first()
    if profile is None:
        raise ApiError("PERMISSION_DENIED", "当前账号没有员工档案。")
    return profile


# ---------------------------------------------------------------------------
# 清洁任务
# ---------------------------------------------------------------------------
@require_GET
@api_view
def task_list(request):
    property_obj, user = _context(request, "inventory.housekeeping_view")
    qs = (
        HousekeepingTask.objects.filter(property=property_obj)
        .select_related("room")
        .order_by("-created_at", "-id")
    )
    status = request.GET.get("status", "").strip()
    if status:
        if status not in HousekeepingTask.TaskStatus.values:
            raise ApiError("VALIDATION_ERROR", "status 不是合法任务状态。")
        qs = qs.filter(status=status)
    task_type = request.GET.get("task_type", "").strip()
    if task_type:
        qs = qs.filter(task_type=task_type)
    assigned = request.GET.get("assigned", "all").strip()
    if assigned == "me":
        qs = qs.filter(assigned_to=_profile(user))
    elif assigned == "group":
        # MC-006-R2（2026-11-18）：本岗位组任务 = 指派给「与当前员工同物业、
        # 同有效岗位组」在职员工的任务（此前该分支未执行过滤，返回全物业列表）。
        membership = AuthorizationService.get_active_membership(user, property_obj)
        now = timezone.now()
        my_group_ids = list(
            membership.role_assignments.filter(starts_at__lte=now)
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
            .values_list("group_id", flat=True)
        )
        if not my_group_ids:
            qs = qs.none()
        else:
            same_group_users = User.objects.filter(
                staff_profile__property_memberships__property=property_obj,
                staff_profile__property_memberships__role_assignments__group_id__in=my_group_ids,
                staff_profile__property_memberships__role_assignments__starts_at__lte=now,
            ).filter(
                Q(staff_profile__property_memberships__role_assignments__ends_at__isnull=True)
                | Q(staff_profile__property_memberships__role_assignments__ends_at__gt=now)
            )
            qs = qs.filter(assigned_to__in=same_group_users)
    elif assigned not in ("all", "group"):
        raise ApiError("VALIDATION_ERROR", "assigned 只能是 all/me/group。")
    room_id = request.GET.get("room_id", "").strip()
    if room_id:
        if not room_id.isdigit():
            raise ApiError("VALIDATION_ERROR", "room_id 必须是整数。")
        qs = qs.filter(room_id=int(room_id))
    items, pagination = paginate(qs, request)
    return api_response(
        request, {"items": [_task_dto(task) for task in items]}, pagination=pagination
    )


@require_GET
@api_view
def task_detail(request, task_id):
    property_obj, _ = _context(request, "inventory.housekeeping_view")
    return api_response(request, _task_dto(_task_or_404(property_obj, task_id)))


@require_POST
@api_view
@idempotent
def task_claim(request, task_id):
    property_obj, user = _context(request, "inventory.housekeeping_update")
    _task_or_404(property_obj, task_id)
    try:
        task, changed = task_services.claim_housekeeping_task(
            property_obj=property_obj, task_id=task_id, actor=user
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, {**_task_dto(task), "changed": changed})


@require_POST
@api_view
@idempotent
def task_assign(request, task_id):
    property_obj, user = _context(request, "inventory.housekeeping_update")
    _task_or_404(property_obj, task_id)
    payload = _body(request)
    assignee_id = _int(payload, "assigned_to")
    assignee = StaffProfile.objects.filter(pk=assignee_id).first()
    if assignee is None:
        raise ApiError("VALIDATION_ERROR", "被指派员工档案不存在。")
    try:
        task = task_services.assign_housekeeping_task(
            property_obj=property_obj, task_id=task_id, assignee=assignee.user, actor=user
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, _task_dto(task))


@require_POST
@api_view
@idempotent
def task_start(request, task_id):
    property_obj, user = _context(request, "inventory.housekeeping_update")
    _task_or_404(property_obj, task_id)
    try:
        task, changed = task_services.start_housekeeping_task(
            property_obj=property_obj, task_id=task_id, actor=user
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, {**_task_dto(task), "changed": changed})


@require_POST
@api_view
@idempotent
def task_complete(request, task_id):
    property_obj, user = _context(request, "inventory.housekeeping_update")
    _task_or_404(property_obj, task_id)
    try:
        task, changed = task_services.complete_housekeeping_task(
            property_obj=property_obj, task_id=task_id, actor=user
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, {**_task_dto(task), "changed": changed})


@require_POST
@api_view
@idempotent
def task_inspect(request, task_id):
    property_obj, user = _context(request, "inventory.housekeeping_view")
    _task_or_404(property_obj, task_id)
    payload = _body(request)
    result = str(payload.get("result", "") or "").strip().upper()
    checklist = payload.get("checklist_result", {})
    failed_items = payload.get("failed_items", [])
    notes = str(payload.get("notes", "") or "")
    if not isinstance(checklist, dict) or not isinstance(failed_items, list):
        raise ApiError("VALIDATION_ERROR", "checklist_result 必须是对象，failed_items 必须是数组。")
    try:
        task, inspection = task_services.inspect_housekeeping_task(
            property_obj=property_obj,
            task_id=task_id,
            actor=user,
            result=result,
            checklist_result=checklist,
            failed_items=failed_items,
            notes=notes,
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(
        request,
        {
            "task": _task_dto(task),
            "inspection": {
                "id": inspection.pk,
                "result": inspection.result,
                "failed_items": inspection.failed_items,
                "reason": inspection.reason,
                "notes": inspection.notes,
            },
        },
    )


@require_POST
@api_view
@idempotent
def task_cancel(request, task_id):
    property_obj, user = _context(request, "inventory.housekeeping_update")
    _task_or_404(property_obj, task_id)
    reason = str(_body(request).get("reason", "") or "")
    try:
        task = task_services.cancel_housekeeping_task(
            property_obj=property_obj, task_id=task_id, actor=user, reason=reason
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, _task_dto(task))


# ---------------------------------------------------------------------------
# 维修工单
# ---------------------------------------------------------------------------
@require_GET
@api_view
def ticket_list(request):
    property_obj, _ = _context(request, "inventory.housekeeping_view")
    qs = (
        MaintenanceTicket.objects.filter(property=property_obj)
        .select_related("room")
        .order_by("-created_at", "-id")
    )
    status = request.GET.get("status", "").strip()
    if status:
        if status not in MaintenanceTicket.TicketStatus.values:
            raise ApiError("VALIDATION_ERROR", "status 不是合法工单状态。")
        qs = qs.filter(status=status)
    room_id = request.GET.get("room_id", "").strip()
    if room_id:
        if not room_id.isdigit():
            raise ApiError("VALIDATION_ERROR", "room_id 必须是整数。")
        qs = qs.filter(room_id=int(room_id))
    items, pagination = paginate(qs, request)
    return api_response(
        request, {"items": [_ticket_dto(ticket) for ticket in items]}, pagination=pagination
    )


@require_GET
@api_view
def ticket_detail(request, ticket_id):
    property_obj, _ = _context(request, "inventory.housekeeping_view")
    return api_response(request, _ticket_dto(_ticket_or_404(property_obj, ticket_id)))


@require_POST
@api_view
@idempotent
def ticket_create(request):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    payload = _body(request)
    room_id = _int(payload, "room_id")
    try:
        ticket = task_services.create_maintenance_ticket(
            property_obj=property_obj,
            room_id=room_id,
            reason=str(payload.get("reason", "") or ""),
            actor=user,
            category=str(payload.get("category", "") or ""),
            severity=str(payload.get("severity", "NORMAL") or "NORMAL"),
            from_date=_day(payload.get("from_date"), "from_date"),
            to_date=_day(payload.get("to_date"), "to_date"),
            deactivate_room=bool(payload.get("deactivate_room", True)),
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, _ticket_dto(ticket), status=201)


@require_POST
@api_view
@idempotent
def ticket_assign(request, ticket_id):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    _ticket_or_404(property_obj, ticket_id)
    assignee = StaffProfile.objects.filter(pk=_int(_body(request), "assigned_to")).first()
    if assignee is None:
        raise ApiError("VALIDATION_ERROR", "被指派员工档案不存在。")
    try:
        ticket = task_services.assign_maintenance_ticket(
            property_obj=property_obj, ticket_id=ticket_id, assignee=assignee.user, actor=user
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, _ticket_dto(ticket))


@require_POST
@api_view
@idempotent
def ticket_start(request, ticket_id):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    _ticket_or_404(property_obj, ticket_id)
    try:
        ticket, changed = task_services.start_maintenance_ticket(
            property_obj=property_obj, ticket_id=ticket_id, actor=user
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, {**_ticket_dto(ticket), "changed": changed})


@require_POST
@api_view
@idempotent
def ticket_resolve(request, ticket_id):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    _ticket_or_404(property_obj, ticket_id)
    notes = str(_body(request).get("notes", "") or "")
    try:
        ticket, changed = task_services.resolve_maintenance_ticket(
            property_obj=property_obj, ticket_id=ticket_id, actor=user, notes=notes
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, {**_ticket_dto(ticket), "changed": changed})


@require_POST
@api_view
@idempotent
def ticket_verify(request, ticket_id):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    _ticket_or_404(property_obj, ticket_id)
    payload = _body(request)
    result = str(payload.get("result", "") or "").strip().upper()
    notes = str(payload.get("notes", "") or "")
    try:
        ticket = task_services.verify_maintenance_ticket(
            property_obj=property_obj, ticket_id=ticket_id, actor=user, result=result, notes=notes
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, _ticket_dto(ticket))


@require_POST
@api_view
@idempotent
def ticket_cancel(request, ticket_id):
    property_obj, user = _context(request, "inventory.maintenance_manage")
    _ticket_or_404(property_obj, ticket_id)
    reason = str(_body(request).get("reason", "") or "")
    try:
        ticket = task_services.cancel_maintenance_ticket(
            property_obj=property_obj, ticket_id=ticket_id, actor=user, reason=reason
        )
    except HousekeepingOperationError as exc:
        _map_error(exc)
    return api_response(request, _ticket_dto(ticket))
