# -*- coding: utf-8 -*-
import json
from datetime import date

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from apps.billing.night_audit import NightAuditError, NightAuditService
from apps.core.api_contract import ApiError, api_view, api_response
from apps.identity.authorization import AuthorizationService


def _context(request, permission):
    user = request.user
    property_obj = getattr(request, "property", None)
    if not user.is_authenticated or not user.is_active:
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, permission):
        raise ApiError("PERMISSION_DENIED", "当前岗位没有该项夜审权限。")
    return property_obj


def _body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是有效 JSON。")


def _parse_date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "营业日期必须使用 YYYY-MM-DD 格式。")


@require_GET
@api_view
def night_audit_preview(request):
    property_obj = _context(request, "hotelapp.night_audit_execute")
    day = _parse_date(request.GET.get("date")) if request.GET.get("date") else None
    try:
        data = NightAuditService.preview(property_obj=property_obj, business_date=day)
    except NightAuditError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, data)


@require_POST
@api_view
def night_audit_run(request):
    property_obj = _context(request, "hotelapp.night_audit_execute")
    body = _body(request)
    day = _parse_date(body.get("date")) if body.get("date") else None
    try:
        run, created = NightAuditService.run(
            property_obj=property_obj, executed_by=request.user, business_date=day)
    except NightAuditError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {
        "run_id": run.pk, "status": run.status,
        "summary": run.summary, "created": created,
    }, status=201 if created else 200)


@require_POST
@api_view
def night_audit_reopen_request(request, business_date_id):
    property_obj = _context(request, "hotelapp.business_date_reopen")
    body = _body(request)
    try:
        approval, created = NightAuditService.request_reopen(
            property_obj=property_obj, business_date_id=business_date_id,
            requested_by=request.user, reason=body.get("reason"))
    except NightAuditError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {
        "approval_id": approval.pk, "status": approval.status,
        "business_date_id": business_date_id, "created": created,
    }, status=201 if created else 200)


@require_POST
@api_view
def night_audit_reopen_approve(request, business_date_id, approval_id):
    property_obj = _context(request, "hotelapp.business_date_reopen")
    try:
        bd = NightAuditService.approve_reopen(
            property_obj=property_obj, business_date_id=business_date_id,
            approval_id=approval_id, approved_by=request.user)
    except NightAuditError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {
        "business_date_id": bd.pk, "business_date": bd.business_date.isoformat(),
        "status": bd.status, "reopened_by": bd.reopened_by_id,
    })


@require_POST
@api_view
def night_audit_break_glass_request(request, business_date_id):
    """仅单一有效经理的紧急情况可申请，要求二次密码验证。"""
    property_obj = _context(request, "hotelapp.business_date_reopen")
    body = _body(request)
    try:
        record = NightAuditService.request_break_glass(
            property_obj=property_obj,
            business_date_id=business_date_id,
            requested_by=request.user,
            password=body.get("password"),
            reason=body.get("reason"),
        )
    except NightAuditError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {
        "break_glass_id": record.pk,
        "business_date_id": business_date_id,
        "status": record.status,
        "expires_at": record.expires_at.isoformat(),
        "action": record.action_code,
        "message": "十分钟紧急授权已发放；请立即使用原申请账号执行一次，执行后必须由另一账号事后复核。",
    }, status=201)


@require_POST
@api_view
def night_audit_break_glass_execute(request, business_date_id, record_id):
    property_obj = _context(request, "hotelapp.business_date_reopen")
    try:
        business_date = NightAuditService.execute_break_glass(
            property_obj=property_obj,
            business_date_id=business_date_id,
            record_id=record_id,
            executed_by=request.user,
        )
    except NightAuditError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    if business_date is None:
        raise ApiError("BUSINESS_CONFLICT", "紧急授权已过期，未执行任何重开操作。")
    return api_response(request, {
        "business_date_id": business_date.pk,
        "business_date": business_date.business_date.isoformat(),
        "status": business_date.status,
        "reopened_by": business_date.reopened_by_id,
        "review_required": True,
    })


@require_POST
@api_view
def night_audit_break_glass_review(request, record_id):
    """由另一名获授权经理完成紧急重开事后复核。"""
    property_obj = _context(request, "core.breakglass_review")
    body = _body(request)
    try:
        record = NightAuditService.review_break_glass(
            property_obj=property_obj,
            record_id=record_id,
            reviewed_by=request.user,
            decision=body.get("decision"),
            reason=body.get("reason"),
        )
    except NightAuditError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {
        "break_glass_id": record.pk,
        "status": record.status,
        "review_decision": record.review_decision,
        "reviewed_by": record.reviewed_by_id,
        "reviewed_at": record.reviewed_at.isoformat(),
    })
