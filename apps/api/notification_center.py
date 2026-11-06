# -*- coding: utf-8 -*-
"""通知与待办中心 API（基线 2.0 §4.6）。

门禁口径（§4.6 规则："角色待办必须按当前有效任职和角色动态授权"——
通知是全员工作入口，不设专项权限码）：
  * 匿名/未认证 → 401 AUTHENTICATION_REQUIRED；
  * 已认证但无当前物业上下文（无 DEFAULT 有效任职）→ 404 RESOURCE_NOT_FOUND，
    与审计/夜审中心同口径；
  * 有物业上下文但任职已失效（兜底复核）→ 403 PERMISSION_DENIED；
  * 读取范围 = 当前物业 且（recipient=本人档案 或 role_group∈本人当前有效
    岗位组）。调岗/离职后旧岗位广播自动不可见（授权链动态计算，§4.6 规则）。

待办刷新（POST /notifications/refresh）：员工可触发，按物业隔离——只重算
当前物业的实时待办集合（apps.core.services_notifications）；待办是只读投影，
无高危副作用，故门禁同读取（有效任职即可）。

响应封套：统一 {data, meta}（apps.core.api_contract），列表端点统一分页契约。
站内通知不发第三方渠道（短信/邮件暂缓）；通知读写不产生 AuditEvent
（§4.6：通知非审计动作）。
"""
from django.db.models import Count, Q
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.api_contract import ApiError, api_response, api_view, paginate
from apps.core.models import Notification
from apps.core.services_notifications import refresh_operational_todos
from apps.identity.authorization import AuthorizationService

# 可筛选类别 = Notification.Category 全集（下拉字典与校验同源）
FILTERABLE_CATEGORIES = frozenset(Notification.Category.values)
FILTERABLE_SEVERITIES = frozenset(Notification.Severity.values)

# refresh 触发上限说明常量：实时派生（无 celery），随 meta 下发
REFRESH_NOTE = (
    "运营待办为实时派生（无后台队列）：每次刷新按当前物业重算"
    "待处理集合并 upsert，已解决项自动标记已读；不产生审计事件。"
)


def _context(request):
    """统一门禁：登录 → 物业上下文 → 有效任职兜底复核。

    返回 (property_obj, staff_profile, active_group_ids)。
    """
    user = request.user
    if not user.is_authenticated or not user.is_active:
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    membership = AuthorizationService.get_active_membership(user, property_obj)
    if membership is None:
        raise ApiError("PERMISSION_DENIED", "当前账号没有有效任职，无法使用通知中心。")
    now = timezone.now()
    group_ids = list(
        membership.role_assignments.filter(starts_at__lte=now)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .values_list("group_id", flat=True))
    return property_obj, membership.staff_profile, group_ids


def _visible_notifications(property_obj, profile, group_ids):
    """我的通知范围：当前物业 +（定向本人 或 岗位广播组∈我的有效组）。"""
    return Notification.objects.filter(
        property=property_obj,
    ).filter(
        Q(recipient=profile) | Q(role_group_id__in=group_ids),
    ).select_related("role_group", "recipient")


def _dto(notification):
    return {
        "id": notification.pk,
        "category": notification.category,
        "category_label": notification.get_category_display(),
        "severity": notification.severity,
        "severity_label": notification.get_severity_display(),
        "title": notification.title,
        "body": notification.body,
        "link": notification.link or None,
        "is_read": notification.is_read,
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
        "created_at": notification.created_at.isoformat(),
        "expires_at": notification.expires_at.isoformat() if notification.expires_at else None,
        "audience": {
            "type": "staff" if notification.recipient_id else "role_group",
            "role_group": notification.role_group.name if notification.role_group_id else None,
        },
        "source_system": "PMS2",
    }


# ---------------------------------------------------------------------------
# GET /api/v1/notifications —— 我的通知分页列表
# ---------------------------------------------------------------------------
@require_GET
@api_view
def notification_list(request):
    """我的通知分页列表（§4.6 通知列表）。

    筛选：category（reservation/housekeeping/approval/audit/system）、
    is_read（true/false）、severity（info/warning/critical）。
    排序：-created_at,-id（模型稳定排序，含主键兜底）。
    """
    property_obj, profile, group_ids = _context(request)
    queryset = _visible_notifications(property_obj, profile, group_ids)

    category = (request.GET.get("category") or "").strip()
    if category:
        if category not in FILTERABLE_CATEGORIES:
            raise ApiError(
                "VALIDATION_ERROR",
                "category 只支持 reservation/housekeeping/approval/audit/system。")
        queryset = queryset.filter(category=category)
    severity = (request.GET.get("severity") or "").strip()
    if severity:
        if severity not in FILTERABLE_SEVERITIES:
            raise ApiError(
                "VALIDATION_ERROR", "severity 只支持 info/warning/critical。")
        queryset = queryset.filter(severity=severity)
    raw_read = (request.GET.get("is_read") or "").strip().lower()
    if raw_read:
        if raw_read not in ("true", "false"):
            raise ApiError("VALIDATION_ERROR", "is_read 只支持 true/false。")
        queryset = queryset.filter(is_read=(raw_read == "true"))

    items, pagination = paginate(queryset, request)
    return api_response(request, [_dto(n) for n in items], pagination=pagination,
                        extra_meta={"refresh_note": REFRESH_NOTE})


# ---------------------------------------------------------------------------
# GET /api/v1/notifications/unread-count —— 未读计数（侧边栏徽标）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def unread_count(request):
    """我的未读通知计数 + 按类别分布（侧边栏徽标数据源）。"""
    property_obj, profile, group_ids = _context(request)
    base = _visible_notifications(property_obj, profile, group_ids).filter(is_read=False)
    by_category = dict(base.values_list("category").annotate(count=Count("id")))
    return api_response(request, {
        "unread_count": sum(by_category.values()),
        "by_category": by_category,
    })


# ---------------------------------------------------------------------------
# POST /api/v1/notifications/<id>/read —— 标记已读（幂等）
# ---------------------------------------------------------------------------
@require_POST
@api_view
def mark_read(request, notification_id):
    """标记单条通知已读：范围外（他物业/非我的定向/非我的岗位广播）一律 404。

    幂等：已读通知重复调用返回 200 且不覆盖首次 read_at。
    """
    property_obj, profile, group_ids = _context(request)
    notification = (_visible_notifications(property_obj, profile, group_ids)
                    .filter(pk=notification_id).first())
    if notification is None:
        raise ApiError("RESOURCE_NOT_FOUND", "通知不存在或不在你的接收范围内。")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=("is_read", "read_at"))
    return api_response(request, _dto(notification))


# ---------------------------------------------------------------------------
# POST /api/v1/notifications/read-all —— 全部已读（仅我的范围）
# ---------------------------------------------------------------------------
@require_POST
@api_view
def read_all(request):
    """把"我的范围"内全部未读一次性标记已读；返回本次标记条数。"""
    property_obj, profile, group_ids = _context(request)
    updated = (_visible_notifications(property_obj, profile, group_ids)
               .filter(is_read=False)
               .update(is_read=True, read_at=timezone.now()))
    return api_response(request, {"marked_read": updated})


# ---------------------------------------------------------------------------
# POST /api/v1/notifications/refresh —— 触发运营待办实时派生
# ---------------------------------------------------------------------------
@require_POST
@api_view
def refresh(request):
    """触发当前物业运营待办重算（§4.6 首期口径：实时派生 + upsert，无 celery）。

    员工可触发；物业隔离——只重算当前物业。返回对账摘要
    {active, created, updated, resolved, by_code}。
    """
    property_obj, _profile, _group_ids = _context(request)
    from apps.core.rate_limit import check, retry_after_seconds
    if not check(request, "pms.notification_refresh",
                 username=request.user.get_username()):
        raise ApiError(
            "RATE_LIMITED",
            "待办刷新过于频繁，请稍后再试（建议等待 %s 秒）。" %
            retry_after_seconds("pms.notification_refresh"),
            retryable=True)
    summary = refresh_operational_todos(property_obj)
    AuditEvent.record(
        action="notification.refresh", entity_type="Property",
        entity_id=property_obj.pk, actor=request.user,
        property_obj=property_obj,
        after=summary, reason="员工手动触发物业待办刷新")
    return api_response(request, summary, extra_meta={"refresh_note": REFRESH_NOTE})
