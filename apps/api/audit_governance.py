"""审计中心受控导出与只读完整性/留存状态端点（MC-008）。

导出与页面检索共用 audit_center._filtered_events，不复制筛选口径；
列白名单不含 before/after；每次导出写审计。留存期限采用已确认口径
（见 RETENTION 常量与 §8 裁定记录），不再返回 null。
"""

import csv
import hashlib
import io
import json
import secrets
from datetime import timedelta

from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.api_contract import ApiError, api_response, api_view
from apps.core.models import AuditEvent, ExportCredential
from apps.identity.authorization import AuthorizationService

from .audit_center import (
    RETENTION_NOTE,
    _action_label,
    _actor_label,
    _context,
    _entity_type_label,
    _filtered_events,
    _sensitive_flag,
)

MAX_EXPORT_ROWS = 10000
EXPORT_COLUMNS = (
    "created_at",
    "action",
    "action_label",
    "entity_type",
    "entity_type_label",
    "entity_id",
    "actor_label",
    "reason",
    "is_sensitive",
)
# 下载有效期（MC-009 治理口径）：导出凭证签发后在此窗口内可下载。
EXPORT_CREDENTIAL_TTL_SECONDS = 15 * 60
# 审计留存年限（MC-008，负责人 2026-11-17 裁定认可）：与财务凭证最低留存口径一致。
AUDIT_RETENTION_YEARS = 10
AUDIT_RETENTION_DECISION = (
    "经负责人 2026-11-17 裁定认可：审计事件最低留存 10 年，"
    "与财务凭证/账务事实留存口径一致；到期销毁须走独立审批与只读备份。"
)


def _require_export(request, property_obj):
    reason = str(request.GET.get("reason", "") or "").strip()
    if not 8 <= len(reason) <= 240:
        raise ApiError("VALIDATION_ERROR", "审计导出必须填写 8 至 240 个字符的用途说明。")
    if not AuthorizationService.has_permission(request.user, property_obj, "core.audit_view"):
        raise ApiError("PERMISSION_DENIED", "缺少审计查看权限，不能导出。")
    return reason


def _audit_export_rows(property_obj, request):
    """按检索条件取审计摘要行（导出与凭证下载共用同一筛选口径）。"""
    qs = _filtered_events(property_obj, request).order_by("-created_at", "-id")[:MAX_EXPORT_ROWS]
    return list(qs)


def _audit_filters(request):
    return {
        key: request.GET.get(key, "")
        for key in ("action", "entity_type", "entity_id", "actor", "date_from", "date_to")
        if request.GET.get(key)
    }


def _write_audit_csv(property_obj, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for event in rows:
        writer.writerow(
            [
                event.created_at.isoformat(),
                event.action,
                _action_label(event.action),
                event.entity_type,
                _entity_type_label(event.entity_type),
                event.entity_id,
                _actor_label(event.actor),
                event.reason or "",
                "是" if _sensitive_flag(event.action) else "否",
            ]
        )
    return "\ufeff" + buffer.getvalue()


@require_GET
@api_view
def audit_export(request):
    """按当前检索条件导出审计摘要 CSV（不含 before/after）。

    同时签发一张带下载有效期的受控导出凭证；凭证只登记口径与到期时间，
    不复制业务事实，供后续 token 化下载与到期控制。
    """
    property_obj = _context(request)
    reason = _require_export(request, property_obj)
    rows = _audit_export_rows(property_obj, request)
    filters = _audit_filters(request)
    payload = _write_audit_csv(property_obj, rows)
    credential = _issue_credential(
        property_obj=property_obj,
        request=request,
        kind=ExportCredential.Kind.AUDIT,
        report_code="",
        params=filters,
        columns=list(EXPORT_COLUMNS),
        row_count=len(rows),
        reason=reason,
    )
    AuditEvent.record(
        action="audit.export",
        entity_type="AuditExport",
        entity_id=hashlib.sha256(
            json.dumps(filters, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:32],
        actor=request.user,
        property_obj=property_obj,
        after={
            "row_count": len(rows),
            "filters": filters,
            "columns": EXPORT_COLUMNS,
            "credential_id": credential.pk,
            "expires_at": credential.expires_at.isoformat(),
        },
        reason=reason,
        request=request,
    )
    response = HttpResponse(payload, content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = (
        f'attachment; filename="audit-{property_obj.code}-{rows[0].created_at:%Y%m%d}.csv"'
        if rows
        else f'attachment; filename="audit-{property_obj.code}-empty.csv"'
    )
    response["X-Export-Credential"] = credential.token
    return response


@require_GET
@api_view
def audit_export_download(request, token):
    """按未过期的导出凭证 token 重新下载同一口径的审计摘要。

    凭证过期 → 410，凭证不存在/越权 → 404；每次下载写 audit.export_download。
    """
    property_obj = _context(request)
    credential = _get_live_credential(property_obj, token, ExportCredential.Kind.AUDIT)
    rows = _audit_export_rows(property_obj, request)
    # 复核筛选口径与签发时一致，避免用旧 token 拉取不同范围。
    if _audit_filters(request) != (credential.params or {}):
        raise ApiError("VALIDATION_ERROR", "下载参数与凭证签发口径不一致，请重新导出。")
    payload = _write_audit_csv(property_obj, rows)
    AuditEvent.record(
        action="audit.export_download",
        entity_type="ExportCredential",
        entity_id=str(credential.pk),
        actor=request.user,
        property_obj=property_obj,
        after={"row_count": len(rows), "expires_at": credential.expires_at.isoformat()},
        reason=credential.reason,
        request=request,
    )
    response = HttpResponse(payload, content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="audit-{property_obj.code}.csv"'
    return response


def _issue_credential(
    *, property_obj, request, kind, report_code, params, columns, row_count, reason
):
    token = secrets.token_urlsafe(32)
    return ExportCredential.objects.create(
        property=property_obj,
        kind=kind,
        report_code=report_code,
        token=token,
        params=params,
        columns=list(columns),
        row_count=row_count,
        reason=reason,
        created_by=request.user if request.user.is_authenticated else None,
        expires_at=timezone.now() + timedelta(seconds=EXPORT_CREDENTIAL_TTL_SECONDS),
    )


def _get_live_credential(property_obj, token, kind):
    credential = ExportCredential.objects.filter(
        property=property_obj, token=token, kind=kind
    ).first()
    if credential is None:
        raise ApiError("RESOURCE_NOT_FOUND", "导出凭证不存在或不属于当前物业。")
    if credential.expires_at <= timezone.now():
        raise ApiError("BUSINESS_CONFLICT", "导出凭证已过期，请重新导出。")
    return credential


@require_GET
@api_view
def audit_retention_status(request):
    """只返回已确认的留存事实与证据指针（留存年限为已裁定口径）。"""
    property_obj = _context(request)
    return api_response(
        request,
        {
            "retention_note": RETENTION_NOTE,
            "retention_years": AUDIT_RETENTION_YEARS,
            "retention_decision": AUDIT_RETENTION_DECISION,
            "immutability": {
                "model": "AuditEvent.append_only",
                "save_guard": "apps/core/models.py::AuditEvent.save",
                "mysql_guard_check": "check_mysql_append_only_guards",
                "sql_reference": "apps/core/sql/audit_immutability.sql",
            },
            "evidence_paths": [
                "docs/acceptance/evidence/",
                "logs/audit.log",
            ],
            "property": property_obj.code,
        },
    )


@require_POST
@api_view
def audit_integrity_check(request):
    """只读摘要核对：按物业+自然日统计并计算摘要；当前没有哈希链基线。"""
    property_obj = _context(request)
    from django.db.models.functions import TruncDate

    daily = list(
        AuditEvent.objects.filter(property=property_obj)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    digest_source = "|".join(
        f"{row['day']}:{row['count']}" for row in daily if row["day"] is not None
    )
    return api_response(
        request,
        {
            "checked_days": len(daily),
            "total_events": sum(row["count"] for row in daily),
            "daily_counts": daily,
            "digest_sha256": hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
            "gaps": [],
            "note": "当前无哈希链或外部留存基线；本检查仅为只读计数/摘要核对，"
            "不替代备份恢复演练。",
        },
    )
