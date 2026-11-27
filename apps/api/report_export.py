"""报表中心受控导出（MC-009）。

导出直接调用各报表只读视图（同一门禁、同一日期口径、同一聚合服务），
只把响应 data.series 写成 CSV，不在导出路径重写任何聚合逻辑。

每次导出登记一张带下载有效期的 ExportCredential：token 只映射回同一
报表口径，供到期前再次下载；过期一律拒绝。下载仍逐次复核报表门禁，
导出与下载各自写 AuditEvent。
"""

import csv
import io
import json

from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.core.api_contract import ApiError, api_view
from apps.core.models import AuditEvent, ExportCredential

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
# 下载凭证的报表筛选参数（除 reason 外）。
PARAM_KEYS = ("report", "from", "to", "group_by")


def _required_reason(request):
    reason = str(request.GET.get("reason", "") or "").strip()
    if not 8 <= len(reason) <= 240:
        raise ApiError("VALIDATION_ERROR", "报表导出必须填写 8 至 240 个字符的用途说明。")
    return reason


def _report_params(request):
    return {key: request.GET.get(key, "") for key in PARAM_KEYS if request.GET.get(key)}


def _render_report_csv(request, report_type):
    """调用报表只读视图并取 data.series 渲染 CSV；权限/参数错误原样透传。"""
    view = REPORT_VIEWS.get(report_type)
    if view is None:
        raise ApiError("VALIDATION_ERROR", "report 必须是已登记的报表代码。")
    response = view(request)
    if response.status_code != 200:
        return response, None, None
    payload = json.loads(response.content.decode("utf-8"))["data"]
    series = payload.get("series") or []
    if not isinstance(series, list):
        raise ApiError("VALIDATION_ERROR", "该报表没有可导出的行级数据。")
    series = series[:MAX_EXPORT_ROWS]
    columns = list(series[0].keys()) if series else []
    buffer = io.StringIO()
    # MC11-RPT-02：CSV 头部携带口径 metadata（期间/日期口径/状态口径/币种），
    # 脱离页面流转时统计口径不丢失；导出超限如实标注截断。
    meta = payload.get("metadata") or {}
    for meta_key in ("period", "date_basis", "status_scope", "currency", "generated_at"):
        value = meta.get(meta_key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        buffer.write(f"# {meta_key}: {value}\n")
    if len(payload.get("series") or []) > MAX_EXPORT_ROWS:
        buffer.write(f"# truncated: 仅导出前 {MAX_EXPORT_ROWS} 行\n")
    writer = csv.DictWriter(buffer, fieldnames=columns or ["empty"])
    writer.writeheader()
    for row in series:
        writer.writerow({key: ("" if value is None else value) for key, value in row.items()})
    return None, ("\ufeff" + buffer.getvalue()), (series, payload)


@require_GET
@api_view
def report_export(request):
    """导出当前报表口径的聚合行（不含 PII 明细），并签发带有效期的下载凭证。"""
    report_type = str(request.GET.get("report", "") or "").strip()
    reason = _required_reason(request)
    early, payload_text, extracted = _render_report_csv(request, report_type)
    if early is not None:
        return early
    series, payload = extracted
    property_obj = getattr(request, "property", None)
    columns = list(series[0].keys()) if series else []
    # 凭据签发收口（宪法 §3.2/§6.1 口径）：token/TTL/落盘统一走 core 服务唯一入口。
    from apps.core.services import issue_export_credential

    credential = issue_export_credential(
        property_obj=property_obj,
        kind=ExportCredential.Kind.REPORT,
        report_code=report_type,
        params=_report_params(request),
        columns=columns,
        row_count=len(series),
        reason=reason,
        created_by=request.user if request.user.is_authenticated else None,
    )
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
            "credential_id": credential.pk,
            "expires_at": credential.expires_at.isoformat(),
        },
        reason=reason,
        request=request,
    )
    result = HttpResponse(payload_text, content_type="text/csv; charset=utf-8-sig")
    result["Content-Disposition"] = f'attachment; filename="report-{report_type}.csv"'
    result["X-Export-Credential"] = credential.token
    return result


@require_GET
@api_view
def report_export_download(request, token):
    """按未过期的导出凭证 token 重新下载同一报表口径。

    下载仍走同一报表只读视图（权限/参数逐次复核）；口径必须与签发一致，
    否则要求重新导出。每次下载写 report.export_download。
    """
    property_obj = getattr(request, "property", None)
    credential = ExportCredential.objects.filter(
        property=property_obj, token=token, kind=ExportCredential.Kind.REPORT
    ).first()
    if credential is None:
        raise ApiError("RESOURCE_NOT_FOUND", "导出凭证不存在或不属于当前物业。")
    if credential.expires_at <= timezone.now():
        raise ApiError("BUSINESS_CONFLICT", "导出凭证已过期，请重新导出。")
    if _report_params(request) != (credential.params or {}):
        raise ApiError("VALIDATION_ERROR", "下载参数与凭证签发口径不一致，请重新导出。")
    early, payload_text, extracted = _render_report_csv(request, credential.report_code)
    if early is not None:
        return early
    series, _ = extracted
    AuditEvent.record(
        action="report.export_download",
        entity_type="ExportCredential",
        entity_id=str(credential.pk),
        actor=request.user,
        property_obj=property_obj,
        after={
            "report": credential.report_code,
            "row_count": len(series),
            "expires_at": credential.expires_at.isoformat(),
        },
        reason=credential.reason,
        request=request,
    )
    result = HttpResponse(payload_text, content_type="text/csv; charset=utf-8-sig")
    result["Content-Disposition"] = f'attachment; filename="report-{credential.report_code}.csv"'
    return result
