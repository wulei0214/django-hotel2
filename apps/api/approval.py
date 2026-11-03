# -*- coding: utf-8 -*-
"""ApprovalRecord REST API（V1.2 Q-05/Q-06/Q-14 P0）。

退款/冲销/营业日重开/PII 批量导出的双人审批 SoD 流程。
申请人≠审批人；同一审批只能执行一次。
"""
from django.views.decorators.http import require_GET, require_POST

from apps.billing.services_v2 import ApprovalService
from apps.core.api_contract import (
    ApiError, api_view, api_response, idempotent, paginate,
)
from apps.identity.authorization import AuthorizationService


def _check_approver(request, property_obj):
    """审批权限检查：值班经理或店长。"""
    if not (
        AuthorizationService.has_permission(request.user, property_obj, "hotelapp.finance_access")
        or request.user.groups.filter(name__in=("值班经理", "店长")).exists()
    ):
        raise ApiError("PERMISSION_DENIED", "审批需要值班经理或店长权限。")


@require_GET
@api_view
def approval_list(request):
    """审批记录列表（当前用户相关）。"""
    from apps.billing.models import ApprovalRecord
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    qs = ApprovalRecord.objects.filter(
        property=property_obj).select_related("requested_by", "approved_by")
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
    """POST /api/v1/approvals/request — 发起审批申请。"""
    from apps.core.models import Property
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    import json as _json
    try:
        body = _json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, _json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是有效 JSON。")
    action_code = body.get("action_code") or ""
    entity_type = body.get("entity_type") or ""
    entity_id = body.get("entity_id") or ""
    reason = body.get("reason") or ""
    if not action_code or not entity_type or not entity_id or not reason:
        raise ApiError("VALIDATION_ERROR", "action_code/entity_type/entity_id/reason 均为必填。")
    record, created = ApprovalService.request_approval(
        property_obj=property_obj, action_code=action_code,
        entity_type=entity_type, entity_id=entity_id,
        requested_by=request.user, reason=reason,
        payload=body.get("payload"), expires_hours=48)
    return api_response(
        request,
        {"id": record.pk, "status": record.status, "created": created},
        status=201 if created else 200)


@require_POST
@api_view
@idempotent
def approval_approve(request, record_id):
    """POST /api/v1/approvals/<id>/approve — 审批通过（Q-05 SoD）。"""
    property_obj = getattr(request, "property", None)
    _check_approver(request, property_obj)
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
    property_obj = getattr(request, "property", None)
    _check_approver(request, property_obj)
    import json as _json
    try:
        body = _json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, _json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是有效 JSON。")
    reason = body.get("reason") or "驳回"
    try:
        record = ApprovalService.reject(
            record_id, approved_by=request.user,
            property_obj=property_obj, reason=reason)
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {"id": record.pk, "status": record.status})
