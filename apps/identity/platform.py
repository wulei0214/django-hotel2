"""平台身份域（方案 §6.1）：/platform/ 平台管理面。

平台身份策略三项不变量：is_active + is_superuser + is_staff 必须同时满足。
  * 未登录（无平台会话 platform_sessionid/django_admin_sessionid）→ 302 去 /admin/login/
  * 已登录非超管 → 直接 403，不跳登录页（不把员工会话当作可升级凭证）
  * 违反不变量的半状态超管（如 is_staff 被误关）→ 403 + AuditEvent(security.platform_denied)

边界（§6.1 明确禁止）：
  * 平台超管不自动获得任何 PMS 业务权限（不写任职/角色，不改 AuthorizationService）
  * 不动 StaffRoleAssignment 模型
  * /platform/ 会话域 Cookie 见 session_middleware.resolve_session_cookie_spec
    （Cookie 网络隔离专项 §B.1：两份 Path Cookie 同一平台 Session）
"""
from functools import wraps

import logging

from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

audit = logging.getLogger("hotel.audit")

# 平台管理面版本号（/platform/system/ 展示；与旧系统页 1.0.0 保持一致）
PMS2_PLATFORM_VERSION = "1.0.0"

# 平台登录入口（Django Admin 登录页；平台会话域独立于 PMS sessionid）
PLATFORM_LOGIN_URL = "/admin/login/"


def _invariant_violations(user):
    """返回超管三项不变量的违反项列表；空列表 = 三项同时满足。"""
    violations = []
    if not user.is_active:
        violations.append("is_active")
    if not user.is_staff:
        violations.append("is_staff")
    if not user.is_superuser:
        violations.append("is_superuser")
    return violations


def platform_superadmin_required(view_func):
    """平台管理面门禁：三项不变量 + 平台会话域，未登录跳 /admin/login/。"""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            # 无平台会话（含仅持 PMS sessionid 的员工）→ 302 去平台登录页
            return redirect_to_login(request.get_full_path(), PLATFORM_LOGIN_URL)
        if not user.is_superuser:
            # 已登录但非超管：直接 403，绝不跳登录页（避免暴露可升级路径）
            raise PermissionDenied("平台管理面仅限超级管理员访问")
        violations = _invariant_violations(user)
        if violations:
            # 半状态超管（如 is_staff=False 的 superuser）：拒绝并落安全审计
            from apps.core.models import AuditEvent
            audit.warning(
                "平台拒绝：用户 %s 违反平台身份不变量(%s)",
                user.username, ",".join(violations))
            AuditEvent.record(
                action="security.platform_denied",
                entity_type="User",
                entity_id=str(user.pk),
                reason="invariant_violation:" + ",".join(violations),
                request=request)
            raise PermissionDenied(
                "平台账号状态不完整（%s），请先在 Django Admin 修复后再访问平台管理面"
                % "、".join(violations))
        return view_func(request, *args, **kwargs)

    return wrapped


@platform_superadmin_required
def platform_home(request):
    """/platform/ 首页：平台管理入口卡片。"""
    return render(request, "platform/home.html", {
        "nav_active": "home",
    })


@platform_superadmin_required
def platform_system_info(request):
    """/platform/system/ 平台环境信息：仅部署/运行环境，不含任何员工 PII。

    员工个人页在 /system/（PMS 会话域），两页严格分离。
    """
    session_age = int(settings.SESSION_COOKIE_AGE)
    ctx = {
        "nav_active": "system",
        "version": PMS2_PLATFORM_VERSION,
        "debug": settings.DEBUG,
        "run_mode": getattr(settings, "DJANGO_ENV", "dev"),
        "tz": settings.TIME_ZONE,
        "sys_lang": settings.LANGUAGE_CODE,
        "session_age_hours": session_age // 3600,
        "session_expire_at_browser_close": bool(
            settings.SESSION_EXPIRE_AT_BROWSER_CLOSE),
        # 以下是配置状态展示，不代表对反向代理或外部网络的主动探测。
        "https_redirect": bool(settings.SECURE_SSL_REDIRECT),
        "session_cookie_secure": bool(settings.SESSION_COOKIE_SECURE),
        "csrf_cookie_secure": bool(settings.CSRF_COOKIE_SECURE),
        "hsts_enabled": bool(settings.SECURE_HSTS_SECONDS),
        "hsts_seconds": int(settings.SECURE_HSTS_SECONDS),
        "csrf_use_sessions": bool(settings.CSRF_USE_SESSIONS),
    }
    return render(request, "platform/system.html", ctx)


@require_POST
@platform_superadmin_required
def platform_logout(request):
    """/platform/logout/（POST）：平台门户主动登出（Cookie 隔离专项 §5.4）。

    auth_logout() flush 平台会话 → 服务端平台 Session 失效；响应经
    SplitSessionMiddleware 平台域删除路径同步删除
    platform_sessionid（Path=/platform/）、django_admin_sessionid
    （Path=/admin/）及历史 admin_sessionid 全部 Name×Path 组合。
    PMS sessionid 不受影响。Django Admin 原生 /admin/logout/ 依赖
    同一 flush→删除机制自动双清。
    """
    auth_logout(request)
    return redirect(PLATFORM_LOGIN_URL)
