"""报表中心受控导出（MC-009）。

导出直接调用各报表只读视图（同一门禁、同一日期口径、同一聚合服务），
只把响应 data.series 写成 CSV，不在导出路径重写任何聚合逻辑。
"""

import csv
import io
import json

from django.http import HttpResponse
from django.views.decorators.http import require_GET

from apps.core.api_contract import ApiError, api_view
from apps.core.models import AuditEvent

from . import report_center

REPORT_VIEWS = {
    "occupancy": report_center.occupancy_report,
    "adr": report_center.adr_report,
    "revenue": report_center.revenue_report,
    "guests": report_center.guests_report,
    "housekeeping": report_center.housekeeping_report,
    "memberships": report_center.memberships_report,
    "sources": report_center.sources_report,
}
MAX_EXPORT_ROWS = 5000


@require_GET
@api_view
def report_export(request):
    """导出当前报表口径的聚合行（不含 PII 明细）。"""
    report_type = str(request.GET.get("report", "") or "").strip()
    view = REPORT_VIEWS.get(report_type)
    if view is None:
        raise ApiError("VALIDATION_ERROR", "report 必须是已登记的报表代码。")
    reason = str(request.GET.get("reason", "") or "").strip()
    if not 8 <= len(reason) <= 240:
        raise ApiError("VALIDATION_ERROR", "报表导出必须填写 8 至 240 个字符的用途说明。")
    response = view(request)
    if response.status_code != 200:
        return response
    payload = json.loads(response.content.decode("utf-8"))["data"]
    series = payload.get("series") or []
    if not isinstance(series, list):
        raise ApiError("VALIDATION_ERROR", "该报表没有可导出的行级数据。")
    series = series[:MAX_EXPORT_ROWS]
    columns = list(series[0].keys()) if series else []
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns or ["empty"])
    writer.writeheader()
    for row in series:
        writer.writerow({key: ("" if value is None else value) for key, value in row.items()})
    property_obj = getattr(request, "property", None)
    AuditEvent.record(
        action="report.export",
        entity_type="ReportExport",
        entity_id=report_type,
        actor=request.user,
        property_obj=property_obj,
        after={
            "report": report_type,
            "row_count": len(series),
            "metadata": payload.get("metadata") or {},
        },
        reason=reason,
        request=request,
    )
    result = HttpResponse("\ufeff" + buffer.getvalue(), content_type="text/csv; charset=utf-8-sig")
    result["Content-Disposition"] = f'attachment; filename="report-{report_type}.csv"'
    return result
