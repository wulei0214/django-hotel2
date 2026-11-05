# -*- coding: utf-8 -*-
"""审计与合规中心 API（基线 2.0 §4.4）。

只读端点，全部以 ``core.audit_view`` 为唯一读取门禁（P0-04 定版；
migration core.0010 已授予店长/值班经理）。与既有
apps/api/reservation_center.py::audit_timeline 并存，共享同一门禁与
物业过滤口径（QuerySet 强制当前物业），不改既有端点。

PII 防泄露口径：
  * 列表端点绝不回传 before_json/after_json——审计事件可能包含揭示类
    记录，列表只给摘要（action/entity/actor/时间/reason）；
  * 变更载荷（before/after）只在详情端点对同一 audit_view 持有者展示；
  * 明文 PII 不落审计是写入侧纪律（guest_center / guests.services 的
    揭示事件只存 masked 值），本模块不做二次改写，由回归测试锁定
    "证件明文/手机明文/Session Key 不出现在任何响应"。

留存（§4.4 权限与留存）：
  * AuditEvent 模型层只追加（save/delete 防护），生产库由
    apps/core/sql/audit_immutability.sql 触发器兜底（P0-06）；
  * 保留期限为待法务/业务负责人确认项——响应 meta 统一携带
    RETENTION_NOTE 常量说明，不虚构年限；归档/备份策略见该 SQL 附注。
"""
from datetime import date, timedelta

from django.db.models import Count, Q
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.core.api_contract import ApiError, api_view, api_response, paginate
from apps.core.models import AuditEvent
from apps.identity.authorization import AuthorizationService

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# §4.4：保留期限待业务/法务确认，不虚构年限；随每个响应 meta 下发。
RETENTION_NOTE = (
    "审计事件为只追加存储：模型层禁改禁删，生产库另有数据库触发器兜底"
    "（apps/core/sql/audit_immutability.sql）；保留期限为待法务/业务负责人"
    "确认项，当前不设定固定年限，归档与备份恢复策略见该 SQL 文件附注。"
)

# SPECIAL_PERMISSIONS（apps/identity/registry.py，14 个高危/敏感专项权限点）
# 在本仓库已落 AuditEvent 的对应动作清单，供 summary 敏感动作单独计数与
# 列表/字典敏感标记。映射关系（权限点 → 审计动作）：
#   guest_document_reveal / guest_phone_reveal → guest.pii_reveal(.document/.phone)、
#     guest.document_reveal、guest.phone_reveal（揭示审计只存脱敏值）
#   refund_request / refund_approve → billing.refund_request、billing.refund_completed
#   红字冲销（reverse_entry 语义，PMS2 action_code=billing.reverse_entry）→ 同名
#   no_show_handle → frontdesk.no_show_processed
#   night_audit_execute → night_audit.run
#   business_date_reopen / breakglass_review → night_audit.reopen、night_audit.break_glass_*
#   export_pii / export_approve → privacy.export_staff
#   manage_rates → inventory.daily_rate_adjust
#   staff_manage → permissions.matrix_saved、identity.role_assign/role_end/
#     password_reset/account_provision
SENSITIVE_ACTIONS = frozenset({
    "guest.pii_reveal", "guest.pii_reveal.document", "guest.pii_reveal.phone",
    "guest.document_reveal", "guest.phone_reveal",
    "billing.refund_request", "billing.refund_completed", "billing.reverse_entry",
    "frontdesk.no_show_processed",
    "night_audit.run", "night_audit.reopen",
    "night_audit.break_glass_requested", "night_audit.break_glass_executed",
    "night_audit.break_glass_reviewed", "night_audit.break_glass_expired",
    "privacy.export_staff",
    "inventory.daily_rate_adjust",
    "permissions.matrix_saved",
    "identity.role_assign", "identity.role_end",
    "identity.password_reset", "identity.account_provision",
})

# summary Top 操作者条数（§4.4 高危事件视图的运营口径，固定小窗口）
TOP_ACTORS_LIMIT = 5


def _context(request):
    """统一门禁：登录 → DEFAULT 物业上下文 → core.audit_view（唯一读取门禁）。"""
    user = request.user
    if not user.is_authenticated or not user.is_active:
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, "core.audit_view"):
        raise ApiError("PERMISSION_DENIED", "缺少审计查看权限。")
    return property_obj


def _parse_day(value, field):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "%s 必须使用 YYYY-MM-DD 格式。" % field)


def _actor_label(actor):
    """操作人展示名：系统操作（actor 为空）统一标 system（同 audit_timeline）。"""
    return actor.username if actor else "system"


def _sensitive_flag(action):
    return action in SENSITIVE_ACTIONS


# ---------------------------------------------------------------------------
# GET /api/v1/audit/events —— 分页检索（列表只给摘要，不含 before/after）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def audit_events(request):
    """审计事件分页列表（§4.4 审计检索）。

    筛选：action（精确，供字典下拉）/entity_type/entity_id/actor（用户名）/
    date_from + date_to（created_at 自然日闭区间）。排序沿用模型稳定排序
    -created_at,-id（P0-03 ensure_stable_ordering 校验通过）。
    """
    property_obj = _context(request)
    qs = AuditEvent.objects.filter(property=property_obj).select_related("actor")

    action = (request.GET.get("action") or "").strip()
    if action:
        qs = qs.filter(action=action)
    entity_type = (request.GET.get("entity_type") or "").strip()
    if entity_type:
        qs = qs.filter(entity_type=entity_type)
    entity_id = (request.GET.get("entity_id") or "").strip()
    if entity_id:
        qs = qs.filter(entity_id=entity_id)
    actor = (request.GET.get("actor") or "").strip()
    if actor:
        qs = qs.filter(actor__username=actor)

    raw_from, raw_to = request.GET.get("date_from"), request.GET.get("date_to")
    if raw_from:
        day = _parse_day(raw_from, "date_from")
        qs = qs.filter(created_at__date__gte=day)
    if raw_to:
        day = _parse_day(raw_to, "date_to")
        qs = qs.filter(created_at__date__lte=day)
    if raw_from and raw_to:
        if _parse_day(raw_from, "date_from") > _parse_day(raw_to, "date_to"):
            raise ApiError("VALIDATION_ERROR", "date_from 不能晚于 date_to。")

    items, pagination = paginate(qs, request)
    # 列表摘要：action/entity/actor/时间/reason + 敏感标记；
    # before/after 一律不下发（§4.4 PII 最小化，详情端点才展示）。
    data = [{
        "id": event.pk,
        "action": event.action,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "actor": _actor_label(event.actor),
        "reason": event.reason or None,
        "is_sensitive": _sensitive_flag(event.action),
        "created_at": event.created_at.isoformat(),
    } for event in items]
    return api_response(request, data, pagination=pagination,
                        extra_meta={"retention_note": RETENTION_NOTE})


# ---------------------------------------------------------------------------
# GET /api/v1/audit/events/<id> —— 单事件详情（before/after 完整展示）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def audit_event_detail(request, event_id):
    """审计事件详情：before/after JSON、原因、请求链路、IP/UA 全量字段。

    访问者即 audit_view 持有者（同一门禁）；跨物业事件一律 404，
    与列表物业过滤同口径。明文 PII 不泄露依赖写入侧脱敏纪律
    （揭示类事件只存 masked），回归测试锁定。
    """
    property_obj = _context(request)
    event = (AuditEvent.objects.filter(pk=event_id)
             .select_related("actor", "property").first())
    if event is None or event.property_id != property_obj.pk:
        raise ApiError("RESOURCE_NOT_FOUND", "审计事件不存在。")
    data = {
        "id": event.pk,
        "action": event.action,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "actor": _actor_label(event.actor),
        "reason": event.reason or None,
        "before": event.before_json,
        "after": event.after_json,
        "request_id": event.request_id or None,
        "ip": event.ip,
        "user_agent": event.user_agent or None,
        "is_sensitive": _sensitive_flag(event.action),
        "created_at": event.created_at.isoformat(),
        "property": {"code": event.property.code, "name": event.property.name}
        if event.property else None,
    }
    return api_response(request, data,
                        extra_meta={"retention_note": RETENTION_NOTE})


# ---------------------------------------------------------------------------
# GET /api/v1/audit/summary —— 审计统计（近 7 天/30 天、Top 操作者、敏感动作）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def audit_summary(request):
    """审计统计：按 action 分组近 7 天/30 天计数、Top 操作者、敏感动作单独计数。

    单查询双窗口：30 天窗口内用条件聚合 Count(filter=created_at>=now-7d)
    拆出 7 天计数；敏感动作为 SENSITIVE_ACTIONS（SPECIAL_PERMISSIONS 相关
    动作）子集统计。
    """
    property_obj = _context(request)
    now = timezone.now()
    start_7d = now - timedelta(days=7)
    start_30d = now - timedelta(days=30)
    recent_7d = Q(created_at__gte=start_7d)

    base = AuditEvent.objects.filter(
        property=property_obj, created_at__gte=start_30d)
    by_action = [{
        "action": row["action"],
        "count_7d": row["recent"],
        "count_30d": row["total"],
    } for row in base.values("action").annotate(
        total=Count("id"), recent=Count("id", filter=recent_7d),
    ).order_by("-total", "action")]
    top_actors = [{
        # actor 为空（系统操作）统一标 system（同 audit_timeline 口径）
        "actor": row["actor__username"] or "system",
        "count_7d": row["recent"],
        "count_30d": row["total"],
    } for row in base.values("actor__username").annotate(
        total=Count("id"), recent=Count("id", filter=recent_7d),
    ).order_by("-total", "actor__username")[:TOP_ACTORS_LIMIT]]

    sensitive_rows = base.filter(action__in=SENSITIVE_ACTIONS)
    sensitive = {
        "count_7d": sensitive_rows.filter(recent_7d).count(),
        "count_30d": sensitive_rows.count(),
        "by_action": [{
            "action": row["action"],
            "count_7d": row["recent"],
            "count_30d": row["total"],
        } for row in sensitive_rows.values("action").annotate(
            total=Count("id"), recent=Count("id", filter=recent_7d),
        ).order_by("-total", "action")],
    }
    data = {
        "window": {
            "now": now.isoformat(),
            "start_7d": start_7d.isoformat(),
            "start_30d": start_30d.isoformat(),
        },
        "total_7d": base.filter(recent_7d).count(),
        "total_30d": base.count(),
        "by_action": by_action,
        "top_actors": top_actors,
        "sensitive": sensitive,
        "sensitive_actions_catalog": sorted(SENSITIVE_ACTIONS),
        "property": {"code": property_obj.code, "name": property_obj.name},
    }
    return api_response(request, data,
                        extra_meta={"retention_note": RETENTION_NOTE})


# ---------------------------------------------------------------------------
# GET /api/v1/audit/actions —— action 字典（去重清单 + 计数，供筛选下拉）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def audit_actions(request):
    """当前物业出现过的 action 去重清单 + 事件计数（供筛选下拉）。"""
    property_obj = _context(request)
    rows = (AuditEvent.objects.filter(property=property_obj)
            .values("action").annotate(total=Count("id"))
            .order_by("-total", "action"))
    data = [{
        "action": row["action"],
        "count": row["total"],
        "is_sensitive": _sensitive_flag(row["action"]),
    } for row in rows]
    return api_response(request, data,
                        extra_meta={"retention_note": RETENTION_NOTE})
