# -*- coding: utf-8 -*-
"""ApprovalRecord REST API（V1.2 Q-05/Q-06/Q-14 P0；P0-01/P0-02 加固）。

高危动作的双人审批 SoD 流程：申请人≠审批人；同一审批只能执行一次。

P0-01（审批列表匿名读取）：
  * 列表/发起/审批/驳回全部要求 认证 + is_active + 当前物业有效任职
    （AuthorizationService.get_active_membership），不把“有物业上下文”当授权；
  * 列表专项只读权限 billing.approval_view；QuerySet 按当前物业过滤，
    且默认只返回当前用户可见范围（持有任一注册动作审批权者见全部，
    否则仅见本人发起的记录）；
  * 审批/驳回先做对象级物业校验：非本物业记录一律 404，不泄露存在性。

P0-02（审批动作白名单）：
  * action_code/entity_type/payload 以 ApprovalActionRegistry 为唯一白名单；
    未知动作 400，不写 ApprovalRecord、不写 AuditEvent；
  * entity 目标对象必须存在、属于当前物业、状态允许申请（注册表校验器）；
  * 每个动作的申请/审批权限码由注册表绑定（双层校验：Service 层同样复核）。
"""
import json as _json

from django.db import transaction
from django.views.decorators.http import require_GET, require_POST

from apps.billing.approval_registry import ApprovalActionRegistry
from apps.billing.models import ApprovalRecord
from apps.billing.services_v2 import ApprovalService
from apps.core.api_contract import (
    ApiError, api_view, api_response, idempotent, paginate,
)
from apps.identity.authorization import AuthorizationService


def _require_membership(request):
    """P0-01：认证 + 激活 + 当前物业有效任职。

    匿名 → 401；已登录但停用/无有效任职（含任职在其他物业）→ 403，
    不泄露任何审批记录存在性。
    """
    user = request.user
    if not getattr(user, "is_authenticated", False):
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    if not getattr(user, "is_active", False):
        raise ApiError("PERMISSION_DENIED", "账号已停用，无法访问审批功能。")
    property_obj = getattr(request, "property", None)
    if property_obj is not None:
        membership = AuthorizationService.get_active_membership(
            user, property_obj)
        if membership is not None:
            return property_obj
    raise ApiError("PERMISSION_DENIED", "员工任职验证失败，无法访问审批功能。")


def _has_any_approver_permission(user, property_obj):
    """是否持有任一已注册动作的审批权（决定列表可见范围）。"""
    return any(
        AuthorizationService.has_permission(user, property_obj, code)
        for code in ApprovalActionRegistry.approver_permissions())


def _json_body(request):
    try:
        return _json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, _json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是有效 JSON。")


def _get_property_record(record_id, property_obj):
    """对象级物业校验：非本物业 / 不存在的记录一律 404。"""
    record = ApprovalRecord.objects.filter(
        pk=record_id, property=property_obj).first()
    if record is None:
        raise ApiError("RESOURCE_NOT_FOUND", "审批记录不存在或不属于当前物业。")
    return record


def _approve_permission_for(record):
    """审批权限码由注册表按 action_code 绑定；未登记历史码退回退款审批权。"""
    spec = ApprovalActionRegistry.get(record.action_code)
    return spec.approve_permission if spec else "billing.refund_approve"


@require_GET
@api_view
def approval_list(request):
    """审批记录列表（P0-01：专项只读权限 + 物业过滤 + 可见范围收敛）。"""
    property_obj = _require_membership(request)
    if not AuthorizationService.has_permission(
            request.user, property_obj, "billing.approval_view"):
        raise ApiError("PERMISSION_DENIED", "查看审批记录需要专项只读权限。")
    qs = ApprovalRecord.objects.filter(
        property=property_obj).select_related("requested_by", "approved_by")
    if not _has_any_approver_permission(request.user, property_obj):
        # 保守口径：无审批权者只看自己发起的审批
        qs = qs.filter(requested_by=request.user)
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
    items, pagination = paginate(qs, request)
    data = [{
        "id": a.pk, "action_code": a.action_code,
        "entity_type": a.entity_type, "entity_id": a.entity_id,
        "status": a.status,
        "requested_by": a.requested_by.username if a.requested_by else None,
        "approved_by": a.approved_by.username if a.approved_by else None,
        "reason": a.reason, "created_at": a.created_at.isoformat(),
        "approved_at": a.approved_at.isoformat() if a.approved_at else None,
    } for a in items]
    return api_response(request, data, pagination=pagination)


@require_POST
@api_view
@idempotent
def approval_request(request):
    """POST /api/v1/approvals/request — 发起审批申请（P0-02 白名单）。"""
    property_obj = _require_membership(request)
    body = _json_body(request)
    action_code = str(body.get("action_code") or "").strip()
    entity_type = str(body.get("entity_type") or "").strip()
    entity_id = body.get("entity_id")
    reason = body.get("reason") or ""
    if not action_code or not entity_type or entity_id in (None, "") or not reason:
        raise ApiError("VALIDATION_ERROR", "action_code/entity_type/entity_id/reason 均为必填。")
    # 白名单第一层（API）：未知动作 / 未开放动作 → 400，零副作用
    spec = ApprovalActionRegistry.get(action_code)
    if spec is None:
        raise ApiError("VALIDATION_ERROR", f"未知审批动作代码：{action_code}。")
    if not spec.api_requestable:
        raise ApiError(
            "VALIDATION_ERROR",
            f"审批动作 {action_code} 不接受通用审批面申请，请使用其专用业务入口。")
    if entity_type != spec.entity_type:
        raise ApiError(
            "VALIDATION_ERROR",
            f"审批动作 {action_code} 的目标实体类型必须为 {spec.entity_type}。")
    if not AuthorizationService.has_permission(
            request.user, property_obj, spec.request_permission):
        raise ApiError("PERMISSION_DENIED", "当前岗位没有该项审批申请权限。")
    entity_id = str(entity_id).strip()
    if not entity_id or len(entity_id) > 64:
        raise ApiError("VALIDATION_ERROR", "entity_id 必须为 1 至 64 个字符。")
    try:
        # 目标对象校验、审批记录创建与业务对象关联必须在同一事务内完成。
        with transaction.atomic():
            entity = None
            if spec.validate_entity is not None:
                entity = spec.validate_entity(property_obj, entity_id, request.user)
            record, created = ApprovalService.request_approval(
                property_obj=property_obj, action_code=action_code,
                entity_type=entity_type, entity_id=entity_id,
                requested_by=request.user, reason=reason,
                payload=body.get("payload"), expires_hours=spec.expires_hours)
            if action_code == "billing.refund":
                # 通用审批入口允许为历史/待处理退款补建审批，但不能留下孤立记录。
                from apps.billing.models import Refund
                refund = Refund.objects.select_for_update().filter(
                    pk=entity_id, property=property_obj,
                    requested_by=request.user,
                    status=Refund.Status.PENDING).first()
                if refund is None:
                    raise ApiError(
                        "BUSINESS_CONFLICT", "退款状态已变化，不能关联审批记录。")
                if refund.approval_record_id not in (None, record.pk):
                    raise ApiError(
                        "BUSINESS_CONFLICT", "退款已关联其他审批记录，拒绝覆盖。")
                if refund.approval_record_id is None:
                    refund.approval_record = record
                    refund.save(update_fields=("approval_record",))
    except ValueError as exc:
        # 白名单第二层（Service）复核失败 / 业务校验失败 → 400，零副作用
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(
        request,
        {"id": record.pk, "status": record.status, "created": created},
        status=201 if created else 200)


@require_POST
@api_view
@idempotent
def approval_approve(request, record_id):
    """POST /api/v1/approvals/<id>/approve — 审批通过（Q-05 SoD）。"""
    property_obj = _require_membership(request)
    record = _get_property_record(record_id, property_obj)
    if not AuthorizationService.has_permission(
            request.user, property_obj, _approve_permission_for(record)):
        raise ApiError("PERMISSION_DENIED", "当前岗位没有该项审批权限。")
    try:
        record = ApprovalService.approve(record_id, approved_by=request.user,
                                         property_obj=property_obj)
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {"id": record.pk, "status": record.status})


@require_POST
@api_view
@idempotent
def approval_reject(request, record_id):
    """POST /api/v1/approvals/<id>/reject — 审批驳回。"""
    property_obj = _require_membership(request)
    record = _get_property_record(record_id, property_obj)
    if not AuthorizationService.has_permission(
            request.user, property_obj, _approve_permission_for(record)):
        raise ApiError("PERMISSION_DENIED", "当前岗位没有该项审批权限。")
    body = _json_body(request)
    reason = body.get("reason") or "驳回"
    try:
        record = ApprovalService.reject(
            record_id, approved_by=request.user,
            property_obj=property_obj, reason=reason)
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {"id": record.pk, "status": record.status})
