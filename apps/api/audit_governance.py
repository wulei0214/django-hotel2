"""审计中心受控导出与只读完整性/留存状态端点（MC-008）。

导出与页面检索共用 audit_center._filtered_events，不复制筛选口径；
列白名单不含 before/after；每次导出写审计。留存期限不由应用自行裁定，
只返回已确认的事实与证据指针。
"""

import csv
import hashlib
import io
import json

from django.db.models import Count
from django.http import HttpResponse
from django.views.decorators.http import require_GET, require_POST

from apps.core.api_contract import ApiError, api_response, api_view
from apps.core.models import AuditEvent
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


def _require_export(request, property_obj):
    reason = str(request.GET.get("reason", "") or "").strip()
    if not 8 <= len(reason) <= 240:
        raise ApiError("VALIDATION_ERROR", "审计导出必须填写 8 至 240 个字符的用途说明。")
    if not AuthorizationService.has_permission(request.user, property_obj, "core.audit_view"):
        raise ApiError("PERMISSION_DENIED", "缺少审计查看权限，不能导出。")
    return reason


@require_GET
@api_view
def audit_export(request):
    """按当前检索条件导出审计摘要 CSV（不含 before/after）。"""
    property_obj = _context(request)
    reason = _require_export(request, property_obj)
    qs = _filtered_events(property_obj, request).order_by("-created_at", "-id")[:MAX_EXPORT_ROWS]
    rows = list(qs)
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
    filters = {
        key: request.GET.get(key, "")
        for key in ("action", "entity_type", "entity_id", "actor", "date_from", "date_to")
        if request.GET.get(key)
    }
    AuditEvent.record(
        action="audit.export",
        entity_type="AuditExport",
        entity_id=hashlib.sha256(
            json.dumps(filters, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:32],
        actor=request.user,
        property_obj=property_obj,
        after={"row_count": len(rows), "filters": filters, "columns": EXPORT_COLUMNS},
        reason=reason,
        request=request,
    )
    response = HttpResponse(
        "\ufeff" + buffer.getvalue(), content_type="text/csv; charset=utf-8-sig"
    )
    response["Content-Disposition"] = (
        f'attachment; filename="audit-{property_obj.code}-{rows[0].created_at:%Y%m%d}.csv"'
        if rows
        else f'attachment; filename="audit-{property_obj.code}-empty.csv"'
    )
    return response


@require_GET
@api_view
def audit_retention_status(request):
    """只返回已确认的留存事实与证据指针，不编造法定年限。"""
    property_obj = _context(request)
    return api_response(
        request,
        {
            "retention_note": RETENTION_NOTE,
            "retention_years": None,
            "retention_decision": "待法务/合规负责人裁定，应用不得自行设定年限",
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
