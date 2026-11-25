"""隐私数据生命周期治理 API（V1.0 TASK-02 / T02-3 OWNER 裁定）。

三条端点均绑定 ApprovalActionRegistry 注册的 privacy.retention_execute：
  POST /api/v1/privacy/retention/plans                生成 dry-run 计划（申请权）
  POST /api/v1/privacy/retention/plans/<id>/approve   审批（独立审批权）
  POST /api/v1/privacy/retention/plans/<id>/execute   执行（独立执行权）

执行端点只接受 plan_id 与 approval_reference，从审批快照与 plan 反查
policy/as_of，再调用 plan_retention_cleanup；执行门（服务层）独立校验
执行权限、申请人与执行人分离、计划一致性。生产真实批量清理仍为
PRODUCTION_PENDING（OWNER-05 数据保留期限与 Legal Hold 责任人未确认）。
"""

from datetime import date

from django.db import transaction
from django.views.decorators.http import require_POST

from apps.billing.models import ApprovalRecord
from apps.billing.services_v2 import ApprovalService
from apps.core.api_contract import ApiError, api_response, api_view
from apps.guests.models import RetentionExecution, RetentionPolicy
from apps.guests.retention import plan_retention_cleanup
from apps.identity.authorization import AuthorizationService


def _require_retention_permission(request, codename):
    """认证 + 有效任职 + guests.retention_* 专项权限（匿名 401，无权限 403）。"""
    user = request.user
    if not getattr(user, "is_authenticated", False):
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    if not getattr(user, "is_active", False):
        raise ApiError("PERMISSION_DENIED", "账号已停用。")
    property_obj = getattr(request, "property", None)
    if (
        property_obj is None
        or AuthorizationService.get_active_membership(user, property_obj) is None
    ):
        raise ApiError("PERMISSION_DENIED", "员工任职验证失败，无法访问隐私清理功能。")
    if not AuthorizationService.has_permission(user, property_obj, codename):
        raise ApiError("PERMISSION_DENIED", "当前岗位没有该项隐私清理权限。")
    return property_obj


def _parse_as_of(raw):
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "as_of 必须为 YYYY-MM-DD 格式。") from None


@require_POST
@api_view
def retention_plan_create(request):
    """生成 dry-run 清理计划；不产生任何匿名化或删除。"""
    from apps.api.approval import _json_body

    property_obj = _require_retention_permission(request, "guests.retention_request")
    body = _json_body(request)
    if not isinstance(body, dict):
        raise ApiError("VALIDATION_ERROR", "请求体必须是 JSON 对象。")
    policy_id = body.get("policy_id")
    as_of = body.get("as_of")
    if not policy_id or not as_of:
        raise ApiError("VALIDATION_ERROR", "policy_id 与 as_of（YYYY-MM-DD）均为必填。")
    as_of_date = _parse_as_of(as_of)
    policy = RetentionPolicy.objects.filter(
        pk=policy_id, property=property_obj, enabled=True
    ).first()
    if policy is None:
        raise ApiError("RESOURCE_NOT_FOUND", "保留策略不存在、未启用或不属于当前物业。")
    with transaction.atomic():
        execution, _ = plan_retention_cleanup(
            property_obj=property_obj,
            policy=policy,
            as_of=as_of_date,
            operator=request.user,
            dry_run=True,
        )
    return api_response(
        request,
        {
            "id": execution.pk,
            "dry_run": True,
            "candidate_count": execution.candidate_count,
            "skipped_count": execution.skipped_count,
            "execution_status": execution.execution_status,
        },
        status=201,
    )


@require_POST
@api_view
def retention_plan_approve(request, plan_id):
    """审批 dry-run 计划（走通用 ApprovalService，独立审批权限）。

    SoD：审批记录的申请人必须是 plan.operator（发起清理计划的人），
    审批人是当前持 guests.retention_approve 权限的用户——两人天然分离。
    若审批记录不存在，以 plan.operator 为申请人补建申请再批准。
    """
    from apps.api.approval import _json_body

    property_obj = _require_retention_permission(request, "guests.retention_approve")
    plan = RetentionExecution.objects.filter(pk=plan_id, property=property_obj).first()
    if plan is None:
        raise ApiError("RESOURCE_NOT_FOUND", "清理计划不存在或不属于当前物业。")
    body = _json_body(request)
    if not isinstance(body, dict):
        raise ApiError("VALIDATION_ERROR", "请求体必须是 JSON 对象。")
    with transaction.atomic():
        record = (
            ApprovalRecord.objects.filter(
                property=property_obj,
                action_code="privacy.retention_execute",
                entity_type="RetentionExecution",
                entity_id=str(getattr(plan, "pk", "")),
            )
            .order_by("-pk")
            .first()
        )
        if record is not None:
            if record.requested_by_id == request.user.pk:
                raise ApiError("PERMISSION_DENIED", "审批人不得为申请人（职责分离）。")
            if record.status == ApprovalRecord.Status.PENDING:
                record = ApprovalService.approve(
                    record.pk, approved_by=request.user, property_obj=property_obj
                )
            elif record.status != ApprovalRecord.Status.APPROVED:
                raise ApiError("BUSINESS_CONFLICT", "审批已驳回或过期，须重新发起。")
            # APPROVED：幂等返回，执行端点自行校验一致性。
        else:
            # 无审批记录：以 plan.operator 为申请人补建申请（dry-run 由其发起），
            # 再由当前审批人批准。两人身份不同，SoD 天然满足。
            applicant = getattr(plan, "operator", None)
            if applicant is not None and applicant.pk == request.user.pk:
                raise ApiError("PERMISSION_DENIED", "审批人不得为申请人（职责分离）。")
            record, _ = ApprovalService.request_approval(
                property_obj=property_obj,
                action_code="privacy.retention_execute",
                entity_type="RetentionExecution",
                entity_id=str(getattr(plan, "pk", "")),
                requested_by=applicant,
                reason=body.get("reason") or "保留期满清理审批",
                payload={
                    "retention_policy_id": str(getattr(getattr(plan, "policy", None), "pk", "")),
                    "as_of_date": str(plan.as_of),
                },
            )
            record = ApprovalService.approve(
                record.pk, approved_by=request.user, property_obj=property_obj
            )
    return api_response(
        request,
        {
            "approval_id": getattr(record, "pk", None),
            "status": getattr(record, "status", None),
            "plan_id": getattr(plan, "pk", None),
        },
        status=201,
    )


@require_POST
@api_view
def retention_plan_execute(request, plan_id):
    """按已批准审批执行 dry-run 计划；执行权限与审批快照双重校验。"""
    from apps.api.approval import _json_body

    property_obj = _require_retention_permission(request, "guests.retention_execute")
    plan = RetentionExecution.objects.filter(pk=plan_id, property=property_obj).first()
    if plan is None:
        raise ApiError("RESOURCE_NOT_FOUND", "清理计划不存在或不属于当前物业。")
    if not plan.dry_run or plan.execution_status != RetentionExecution.ExecutionStatus.DRAFT:
        raise ApiError("BUSINESS_CONFLICT", "清理计划已执行或已绑定审批，不得重复执行。")
    body = _json_body(request)
    if not isinstance(body, dict):
        raise ApiError("VALIDATION_ERROR", "请求体必须是 JSON 对象。")
    approval_reference = body.get("approval_reference")
    if not approval_reference:
        raise ApiError("VALIDATION_ERROR", "必须提供 approval_reference。")
    approval = ApprovalRecord.objects.filter(pk=approval_reference, property=property_obj).first()
    if approval is None:
        raise ApiError("RESOURCE_NOT_FOUND", "审批记录不存在或不属于当前物业。")
    if approval.action_code != "privacy.retention_execute" or str(approval.entity_id) != str(
        plan.pk
    ):
        raise ApiError("BUSINESS_CONFLICT", "审批记录与清理计划不一致，禁止执行。")
    if approval.status != ApprovalRecord.Status.APPROVED:
        raise ApiError("BUSINESS_CONFLICT", "审批未通过，禁止执行。")
    if approval.requested_by_id == request.user.pk:
        raise ApiError("PERMISSION_DENIED", "执行人不得为审批申请人。")
    with transaction.atomic():
        execution, _ = plan_retention_cleanup(
            property_obj=property_obj,
            policy=getattr(plan, "policy", None),
            as_of=plan.as_of,
            operator=request.user,
            dry_run=False,
            approval_reference=str(approval_reference),
            execution=plan,
        )
    return api_response(
        request,
        {
            "id": execution.pk,
            "dry_run": False,
            "processed_count": execution.processed_count,
            "skipped_count": execution.skipped_count,
            "failure_count": execution.failure_count,
            "execution_status": execution.execution_status,
        },
    )
