"""可执行待办任务中心 API（MC-007）。

职责边界：Notification 只提示，NotificationRead 只记个人已读；本模块的
claim/assign/complete 只编排任务过程，complete 必须由 apps.core.task_registry
调用源领域服务，源业务成功后才关闭 WorkItem。
"""

import json

from django.views.decorators.http import require_GET, require_POST

from apps.core.api_contract import ApiError, api_response, api_view, idempotent, paginate
from apps.core.models import WorkItem
from apps.core.services_work_items import (
    WorkItemError,
    assign_work_item,
    claim_work_item,
    complete_work_item,
)
from apps.core.task_registry import get_spec
from apps.identity.authorization import AuthorizationService
from apps.identity.models import StaffProfile

from .notification_center import _context

OPEN_STATUSES = (WorkItem.Status.PENDING, WorkItem.Status.CLAIMED)


def _visible_work_items(property_obj, profile, group_ids):
    from django.db.models import Q

    return WorkItem.objects.filter(property=property_obj).filter(
        Q(assigned_profile=profile) | Q(claimed_by=profile) | Q(assigned_group_id__in=group_ids)
    )


def _work_item_or_404(property_obj, work_item_id, queryset=None):
    qs = queryset if queryset is not None else WorkItem.objects.all()
    work_item = qs.filter(pk=work_item_id, property=property_obj).first()
    if work_item is None:
        raise ApiError("RESOURCE_NOT_FOUND", "待办不存在或不属于当前物业。")
    return work_item


def _dto(work_item):
    return {
        "id": work_item.pk,
        "task_code": work_item.task_code,
        "task_name": work_item.title,
        "source_type": work_item.source_type,
        "source_id": work_item.source_id,
        "title": work_item.title,
        "body": work_item.body,
        "link": work_item.link,
        "severity": work_item.severity,
        "status": work_item.status,
        "status_label": work_item.get_status_display(),
        "resolution": work_item.resolution,
        "assigned_to": work_item.assigned_profile_id,
        "claimed_by": work_item.claimed_by_id,
        "claimed_at": work_item.claimed_at.isoformat() if work_item.claimed_at else None,
        "completed_by": work_item.completed_by_id,
        "completed_at": work_item.completed_at.isoformat() if work_item.completed_at else None,
        "due_at": work_item.due_at.isoformat() if work_item.due_at else None,
        "last_error_code": work_item.last_error_code,
        "last_error_summary": work_item.last_error_summary,
        "can_complete": bool(
            get_spec(work_item.task_code) and get_spec(work_item.task_code).executor
        ),
        "created_at": work_item.created_at.isoformat(),
        "updated_at": work_item.updated_at.isoformat(),
        "source_system": "PMS2",
    }


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


def _map_work_item_error(exc):
    if isinstance(exc, WorkItemError):
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    raise exc


@require_GET
@api_view
def task_list(request):
    """我的可执行待办列表（按当前有效任职/岗位动态可见）。"""
    property_obj, profile, group_ids = _context(request)
    qs = _visible_work_items(property_obj, profile, group_ids)
    status = request.GET.get("status", "").strip()
    if status:
        if status not in WorkItem.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 不是合法的待办状态。")
        qs = qs.filter(status=status)
    else:
        qs = qs.filter(status__in=OPEN_STATUSES)
    task_code = request.GET.get("task_code", "").strip()
    if task_code:
        qs = qs.filter(task_code=task_code)
    assigned = request.GET.get("assigned", "all").strip()
    if assigned == "me":
        qs = qs.filter(assigned_profile=profile)
    elif assigned == "group":
        qs = qs.filter(assigned_profile__isnull=True, assigned_group_id__in=group_ids)
    elif assigned != "all":
        raise ApiError("VALIDATION_ERROR", "assigned 只能是 me/group/all。")
    items, pagination = paginate(qs, request)
    return api_response(
        request,
        {
            "items": [_dto(item) for item in items],
            "note": "通知已读不代表任务完成；完成动作必须调用源领域服务。",
        },
        pagination=pagination,
    )


@require_GET
@api_view
def task_detail(request, work_item_id):
    """待办详情；跨物业返回 404。"""
    property_obj, profile, group_ids = _context(request)
    work_item = _work_item_or_404(
        property_obj, work_item_id, _visible_work_items(property_obj, profile, group_ids)
    )
    return api_response(request, _dto(work_item))


@require_POST
@api_view
@idempotent
def task_claim(request, work_item_id):
    """认领待办；只改分配事实，不执行源业务。"""
    property_obj, profile, group_ids = _context(request)
    work_item = _work_item_or_404(
        property_obj, work_item_id, _visible_work_items(property_obj, profile, group_ids)
    )
    spec = get_spec(work_item.task_code)
    # 认领人须具备完成该类任务的现有业务权限；不新增任务专用权限码。
    if (
        spec
        and spec.permission
        and not AuthorizationService.has_permission(
            request.user, property_obj, spec.permission, obj=work_item
        )
    ):
        raise ApiError("PERMISSION_DENIED", "当前岗位无权处理该类待办。")
    try:
        item, changed = claim_work_item(
            property_obj=property_obj,
            work_item_id=work_item_id,
            profile=profile,
            actor=request.user,
        )
    except WorkItemError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc)) from None
    return api_response(request, {**_dto(item), "changed": changed})


@require_POST
@api_view
@idempotent
def task_assign(request, work_item_id):
    """管理者指派待办；只改负责人，不改源业务状态。

    MC-007-R1 口径：指派属显式管理权限路径（identity.staff_manage + 指派审计），
    不经任务可见范围过滤——这是负责人认可的管理者跨组处理例外，非隐式放开。"""
    property_obj, profile, group_ids = _context(request)
    work_item = _work_item_or_404(property_obj, work_item_id)
    if not AuthorizationService.has_permission(
        request.user, property_obj, "identity.staff_manage", obj=work_item
    ):
        raise ApiError("PERMISSION_DENIED", "当前岗位无权指派待办。")
    payload = _body(request)
    target_id = payload.get("assigned_to")
    if not isinstance(target_id, int):
        raise ApiError("VALIDATION_ERROR", "assigned_to 必须是员工档案 ID。")
    target = StaffProfile.objects.filter(pk=target_id).first()
    if (
        target is None
        or AuthorizationService.get_active_membership(target.user, property_obj) is None
    ):
        raise ApiError("VALIDATION_ERROR", "目标员工在当前物业没有有效任职。")
    try:
        item = assign_work_item(
            property_obj=property_obj,
            work_item_id=work_item_id,
            target_profile=target,
            actor=request.user,
        )
    except WorkItemError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc)) from None
    return api_response(request, _dto(item))


@require_POST
@api_view
@idempotent
def task_complete(request, work_item_id):
    """完成待办：必须经源领域服务；源业务失败时任务保留待处理。

    MC-007-R1（2026-11-18）：完成接口与 detail/claim 一致，统一走任务可见范围
    过滤——非指派给自己/本岗位组可见的任务一律 404，不依赖业务权限隐式放开。
    （指派接口保留 identity.staff_manage 显式管理权限规则，指派动作由
    assign_work_item 落审计，属负责人认可的例外路径。）"""
    property_obj, profile, group_ids = _context(request)
    work_item = _work_item_or_404(
        property_obj, work_item_id, _visible_work_items(property_obj, profile, group_ids)
    )
    spec = get_spec(work_item.task_code)
    if (
        spec
        and spec.permission
        and not AuthorizationService.has_permission(
            request.user, property_obj, spec.permission, obj=work_item
        )
    ):
        raise ApiError("PERMISSION_DENIED", "当前岗位无权完成该类待办。")
    payload = _body(request)
    try:
        item, changed = complete_work_item(
            property_obj=property_obj,
            work_item_id=work_item_id,
            profile=profile,
            actor=request.user,
            payload=payload,
            request=request,
        )
    except WorkItemError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc)) from None
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc)) from None
    return api_response(request, {**_dto(item), "changed": changed})
