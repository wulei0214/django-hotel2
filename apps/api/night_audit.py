# -*- coding: utf-8 -*-
"""夜审与营业日中心 API（§4.2）。

写操作（preview/run/reopen/break-glass）沿用 NightAuditService 既有入口；
本模块同时提供营业日/夜审运行/break-glass 的只读查询端点，
统一 {data, meta} 封套与统一分页契约（apps.core.api_contract.paginate）。

读取口径：所有只读端点统一使用 core.night_audit_view；
执行夜审使用 hotelapp.night_audit_execute；重开营业日/Break-glass 使用
独立高危审批权限。查看权不得隐含执行权。
"""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.billing.models import ApprovalRecord, LedgerEntry
from apps.billing.night_audit import (
    NightAuditError, NightAuditService, property_date,
)
from apps.core.api_contract import ApiError, api_view, api_response, paginate
from apps.core.models import BreakGlassRecord, BusinessDate, NightAuditRun
from apps.identity.authorization import AuthorizationService
from apps.inventory.models import Room
from apps.reservations.models import Reservation, ReservationRoom

BUSINESS_DATE_STATUS_LABELS = dict(BusinessDate.Status.choices)
RUN_STATUS_LABELS = dict(NightAuditRun.RunStatus.choices)
BREAK_GLASS_STATUS_LABELS = dict(BreakGlassRecord.Status.choices)
BREAK_GLASS_REVIEW_LABELS = dict(BreakGlassRecord.ReviewDecision.choices)
# 列表内失败原因截断长度（详情端点返回完整入库文本）
_LIST_ERROR_SNIPPET_LEN = 300


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
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是有效 JSON。")
    if not isinstance(payload, dict):
        # WB-46-01：非对象 JSON（数组/字符串/数字）此前会带出 body.get 的
        # AttributeError → 500；与其他中心 _body 同口径 400。
        raise ApiError("VALIDATION_ERROR", "请求体必须是 JSON 对象。")
    return payload


def _parse_date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "营业日期必须使用 YYYY-MM-DD 格式。")


@require_GET
@api_view
def night_audit_preview(request):
    property_obj = _context(request, "core.night_audit_view")
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


# ---------------------------------------------------------------------------
# §4.2 只读查询端点（营业日中心）：列表/当前/详情 + 夜审历史 + break-glass 列表
# ---------------------------------------------------------------------------

def _clean_error(text, limit=_LIST_ERROR_SNIPPET_LEN):
    """失败原因统一截断（服务端生成文本，无请求方原文）。"""
    text = str(text or "")
    return text[:limit] if len(text) > limit else text


def _run_dto(run, with_summary=True):
    duration = None
    if run.started_at and run.finished_at:
        duration = round((run.finished_at - run.started_at).total_seconds(), 3)
    data = {
        "id": run.pk,
        "business_date_id": run.business_date_id,
        "business_date": run.business_date.business_date.isoformat(),
        "business_date_status": run.business_date.status,
        "status": run.status,
        "status_label": RUN_STATUS_LABELS.get(run.status, run.status),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": duration,
        "executed_by_id": run.executed_by_id,
        "error_message": _clean_error(run.error_message),
    }
    if with_summary:
        data["summary"] = run.summary
    return data


def _latest_runs_by_business_date(business_dates):
    """一次查询取每个营业日的最近一次夜审运行（防列表 N+1）。"""
    runs = NightAuditRun.objects.filter(
        business_date__in=business_dates
    ).select_related("business_date").order_by(
        "business_date_id", "-started_at", "-id")
    latest = {}
    for run in runs:
        latest.setdefault(run.business_date_id, run)
    return latest


def _business_date_dto(bd, latest_run=None):
    return {
        "id": bd.pk,
        "business_date": bd.business_date.isoformat(),
        "status": bd.status,
        "status_label": BUSINESS_DATE_STATUS_LABELS.get(bd.status, bd.status),
        "opened_at": bd.opened_at.isoformat() if bd.opened_at else None,
        "closed_at": bd.closed_at.isoformat() if bd.closed_at else None,
        "closed_by_id": bd.closed_by_id,
        "reopened_at": bd.reopened_at.isoformat() if bd.reopened_at else None,
        "reopened_by_id": bd.reopened_by_id,
        "reopen_reason": bd.reopen_reason,
        "latest_run": _run_dto(latest_run, with_summary=False) if latest_run else None,
    }


def _no_show_candidates(property_obj, day):
    return ReservationRoom.objects.filter(
        property=property_obj,
        check_in__lte=day,
        check_out__gt=day,
        reservation__status__in=(Reservation.Status.CONFIRMED,
                                 Reservation.Status.CHECKED_IN),
        stay__isnull=True,
        no_show_record__isnull=True,
    ).count()


def _ledger_summary(property_obj, day):
    """该营业日已入账凭证汇总（过租结果核对用）。"""
    rows = LedgerEntry.objects.filter(
        property=property_obj, business_date=day
    ).values_list("amount", "currency")
    by_currency = {}
    for amount, currency in rows:
        bucket = by_currency.setdefault(
            currency, {"entry_count": 0, "charge_total": Decimal("0.00"),
                       "credit_total": Decimal("0.00")})
        bucket["entry_count"] += 1
        if amount >= 0:
            bucket["charge_total"] += amount
        else:
            bucket["credit_total"] += -amount
    for bucket in by_currency.values():
        for key in ("charge_total", "credit_total"):
            bucket[key] = str(bucket[key].quantize(Decimal("0.01")))
    return {"business_date": day.isoformat(), "currencies": by_currency}


def _room_status_summary(property_obj):
    """异常房态汇总：三维状态计数 + 维修/停用分组。"""
    counts = Room.objects.filter(property=property_obj).values(
        "occupancy_status", "housekeeping_status", "maintenance_status"
    ).annotate(total=Count("id"))
    occupancy, housekeeping, maintenance, abnormal = {}, {}, {}, []
    for row in counts:
        occupancy[row["occupancy_status"]] = (
            occupancy.get(row["occupancy_status"], 0) + row["total"])
        housekeeping[row["housekeeping_status"]] = (
            housekeeping.get(row["housekeeping_status"], 0) + row["total"])
        maintenance[row["maintenance_status"]] = (
            maintenance.get(row["maintenance_status"], 0) + row["total"])
        if row["maintenance_status"] != Room.MaintenanceStatus.NORMAL:
            abnormal.append({"maintenance_status": row["maintenance_status"],
                             "room_count": row["total"]})
    return {"total_rooms": sum(occupancy.values()),
            "occupancy": occupancy, "housekeeping": housekeeping,
            "maintenance": maintenance, "abnormal": abnormal}


@require_GET
@api_view
def night_audit_business_dates(request):
    """营业日分页列表；status 筛选；稳定排序 -business_date,-id。"""
    property_obj = _context(request, "core.night_audit_view")
    queryset = BusinessDate.objects.filter(property=property_obj).order_by(
        "-business_date", "-id")
    status = request.GET.get("status", "")
    if status:
        if status not in BusinessDate.Status.values:
            raise ApiError("VALIDATION_ERROR",
                           "status 只支持 OPEN/CLOSED/REOPENED。")
        queryset = queryset.filter(status=status)
    items, pagination = paginate(queryset, request)
    latest_runs = _latest_runs_by_business_date(list(items))
    data = [_business_date_dto(bd, latest_runs.get(bd.pk)) for bd in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def night_audit_business_date_current(request):
    """当前营业日：无记录时按 OPEN 口径返回（与 NightAuditService.preview 一致）。"""
    property_obj = _context(request, "core.night_audit_view")
    day = property_date(property_obj)
    bd = BusinessDate.objects.filter(
        property=property_obj, business_date=day).first()
    if bd is None:
        return api_response(request, {
            "id": None, "business_date": day.isoformat(),
            "exists": False, "status": BusinessDate.Status.OPEN,
            "status_label": BUSINESS_DATE_STATUS_LABELS[BusinessDate.Status.OPEN],
        })
    data = _business_date_dto(bd)
    data["exists"] = True
    return api_response(request, data)


@require_GET
@api_view
def night_audit_business_date_detail(request, business_date_id):
    """营业日详情：运行历史 + 重开审批 + 过租/未到汇总 + 异常房态。"""
    property_obj = _context(request, "core.night_audit_view")
    bd = BusinessDate.objects.filter(
        pk=business_date_id, property=property_obj).first()
    if bd is None:
        raise ApiError("RESOURCE_NOT_FOUND", "营业日记录不存在或不属于当前物业。")
    runs = NightAuditRun.objects.filter(business_date=bd).order_by(
        "-started_at", "-id")
    latest_success = next((run for run in runs if run.status ==
                           NightAuditRun.RunStatus.SUCCESS), None)
    approvals = ApprovalRecord.objects.filter(
        property=property_obj, entity_type="BusinessDate",
        entity_id=str(bd.pk)).order_by("-created_at", "-id")[:20]
    data = _business_date_dto(bd, runs.first())
    data.update({
        "runs": [_run_dto(run) for run in runs],
        "latest_success_summary": latest_success.summary if latest_success else None,
        "approvals": [{
            "id": item.pk, "action_code": item.action_code,
            "status": item.status, "requested_by_id": item.requested_by_id,
            "approved_by_id": item.approved_by_id,
            "reason": item.reason,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        } for item in approvals],
        "no_show_review_candidates": _no_show_candidates(
            property_obj, bd.business_date),
        "ledger": _ledger_summary(property_obj, bd.business_date),
        "room_status_summary": _room_status_summary(property_obj),
    })
    return api_response(request, data)


@require_GET
@api_view
def night_audit_break_glass_list(request):
    """break-glass 记录分页列表；status/review_decision 筛选（复核状态即
    REVIEWED 已复核、USED 待复核等生命周期态）。"""
    property_obj = _context(request, "core.night_audit_view")
    queryset = BreakGlassRecord.objects.filter(
        property=property_obj).order_by("-created_at", "-id")
    status = request.GET.get("status", "")
    if status:
        if status not in BreakGlassRecord.Status.values:
            raise ApiError(
                "VALIDATION_ERROR",
                "status 只支持 GRANTED/USED/EXPIRED/REVIEWED。")
        queryset = queryset.filter(status=status)
    review_decision = request.GET.get("review_decision", "")
    if review_decision:
        if review_decision not in BreakGlassRecord.ReviewDecision.values:
            raise ApiError(
                "VALIDATION_ERROR", "review_decision 只支持 CONFIRMED/REJECTED。")
        queryset = queryset.filter(review_decision=review_decision)
    items, pagination = paginate(queryset, request)
    now = timezone.now()
    data = [{
        "id": item.pk,
        "business_date_id": item.business_date_id,
        "business_date": item.business_date.business_date.isoformat(),
        "status": item.status,
        "status_label": BREAK_GLASS_STATUS_LABELS.get(item.status, item.status),
        "requested_by_id": item.requested_by_id,
        "reason": item.reason,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "used_at": item.used_at.isoformat() if item.used_at else None,
        "reviewed_by_id": item.reviewed_by_id,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "review_decision": item.review_decision,
        "review_decision_label": BREAK_GLASS_REVIEW_LABELS.get(
            item.review_decision, item.review_decision) if item.review_decision else "",
        "review_reason": item.review_reason,
        "review_pending": item.status == BreakGlassRecord.Status.USED,
        "is_expired_grant": (
            item.status == BreakGlassRecord.Status.GRANTED
            and item.expires_at <= now),
    } for item in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def night_audit_runs(request):
    """夜审历史分页列表；business_date_id/status 筛选；稳定排序 -started_at,-id。"""
    property_obj = _context(request, "core.night_audit_view")
    queryset = NightAuditRun.objects.filter(
        business_date__property=property_obj
    ).select_related("business_date").order_by("-started_at", "-id")
    status = request.GET.get("status", "")
    if status:
        if status not in NightAuditRun.RunStatus.values:
            raise ApiError("VALIDATION_ERROR",
                           "status 只支持 RUNNING/SUCCESS/FAILED。")
        queryset = queryset.filter(status=status)
    business_date_id = request.GET.get("business_date_id", "")
    if business_date_id:
        try:
            bd_id = int(business_date_id)
        except (TypeError, ValueError):
            raise ApiError("VALIDATION_ERROR", "business_date_id 必须是整数。")
        queryset = queryset.filter(business_date_id=bd_id)
    items, pagination = paginate(queryset, request)
    data = [_run_dto(run) for run in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def night_audit_run_detail(request, run_id):
    """单次夜审运行详情（含完整摘要与失败原因）。"""
    property_obj = _context(request, "core.night_audit_view")
    run = NightAuditRun.objects.filter(
        pk=run_id, business_date__property=property_obj
    ).select_related("business_date").first()
    if run is None:
        raise ApiError("RESOURCE_NOT_FOUND", "夜审运行记录不存在或不属于当前物业。")
    data = _run_dto(run)
    data["error_message"] = str(run.error_message or "")[:4000]
    return api_response(request, data)

