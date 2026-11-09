# -*- coding: utf-8 -*-
"""AUTH-18：员工邀请激活页（/invite/accept?token=...，匿名公开 + 限流）。

安全口径（方案 §5.3/§6.1）：
  * GET 只读校验 token 摘要 → 渲染设置密码表单；POST 调
    EmployeeInvitationService.consume 一次性消费（行锁 + 状态条件更新）；
  * token 无效/过期/撤销/重复消费/非法输入 → 统一显示
    "邀请链接无效或已过期"，不泄露员工/邀请存在性；
  * 密码策略错误透出 validate_password 中文消息（不涉及存在性）；
  * 限流：apps.core.rate_limit action="public.invitation_activate"，
    IP 5 次/15 分钟（§6.3），fail-closed；GET 与 POST 共用同一桶；
  * 页面为独立匿名页，风格对齐 /login/（templates/identity/invite_accept.html）；
  * 激活成功 → 重定向 /login/?activated=1，登录页展示成功提示。
"""
import logging

from django.shortcuts import redirect, render
from django.utils import timezone

from apps.core import rate_limit
from apps.core.models import AuditEvent

from .invitation_services import (
    UNIFIED_INVALID_MESSAGE,
    EmployeeInvitationService,
    InvitationError,
)

audit = logging.getLogger("hotel.audit")

# §6.3：激活动作限流（IP 5 次/15 分钟，fail-closed），GET 验证与 POST 激活共用
RATE_LIMIT_ACTION = "public.invitation_activate"


def _render_page(request, *, state="form", token="", username="", full_name="",
                 expires_at=None, error="", status=200):
    """渲染激活页；state: form（表单）/ invalid（统一失败）/ rate_limited。"""
    return render(request, "identity/invite_accept.html", {
        "state": state,
        "token": token,
        "username": username,
        "full_name": full_name,
        "expires_at": (timezone.localtime(expires_at) if expires_at else None),
        "error": error,
    }, status=status)


def _rate_limited(request):
    # fail-closed（默认 True）：计数存储异常时拒绝请求（§6.2）
    return not rate_limit.check(request, RATE_LIMIT_ACTION)


def _too_many(request):
    """429：与 apps/api/views._throttle_deny 同口径——public.rate_limited 审计。"""
    AuditEvent.record(
        action="public.rate_limited", entity_type="PublicEndpoint",
        entity_id=RATE_LIMIT_ACTION,
        reason="ip=%s" % request.META.get("REMOTE_ADDR", ""), request=request)
    response = _render_page(
        request, state="rate_limited",
        error="尝试次数过多，请 15 分钟后重试或联系管理员。", status=429)
    response["Retry-After"] = str(rate_limit.retry_after_seconds(RATE_LIMIT_ACTION))
    return response


def _form_context(raw_token):
    """token 仍可激活时返回表单展示参数；已失效返回 None（统一失败页）。"""
    context = EmployeeInvitationService.describe_for_activation(raw_token)
    if context is None:
        return None
    return {
        "token": raw_token,
        "username": context["username"],
        "full_name": context["full_name"],
        "expires_at": context["expires_at"],
    }


def _accept_get(request, raw_token):
    if _rate_limited(request):
        return _too_many(request)
    context = _form_context(raw_token)
    if context is None:
        # 统一文案：无效/过期/撤销/重复消费同一种反馈（§5.3 不泄露存在性）
        return _render_page(request, state="invalid", error=UNIFIED_INVALID_MESSAGE)
    return _render_page(request, state="form", **context)


def _accept_post(request, raw_token):
    if _rate_limited(request):
        return _too_many(request)
    context = _form_context(raw_token)
    if context is None:
        return _render_page(request, state="invalid",
                            error=UNIFIED_INVALID_MESSAGE)
    password = request.POST.get("password") or ""
    confirm = request.POST.get("password_confirm") or ""
    if password != confirm:
        return _render_page(request, state="form",
                            error="两次输入的密码不一致，请重新输入。", **context)
    try:
        EmployeeInvitationService.consume(raw_token, password, request=request)
    except InvitationError as exc:
        # §5.3：激活失败按安全审计策略记录——只记日志摘要，不记 token 明文/摘要
        # （§16.1 未定义激活失败事件名，AuditEvent 保持既有事件名单不扩名）
        audit.warning("邀请激活失败 ip=%s 原因=%s",
                      request.META.get("REMOTE_ADDR", ""), exc)
        # 消费失败：token 若已在过程中失效（过期/被抢先消费）→ 统一失败页；
        # 密码策略等可重试错误 → 回到表单并展示中文原因（不泄露存在性）
        context = _form_context(raw_token)
        if context is None:
            return _render_page(request, state="invalid",
                                error=UNIFIED_INVALID_MESSAGE)
        return _render_page(request, state="form", error=str(exc), status=400,
                            **context)
    # 成功 → 登录页 + 成功提示（员工使用自设密码登录）
    return redirect("/login/?activated=1")


def invite_accept(request):
    """GET 校验并渲染表单 / POST 一次性消费激活（AUTH-18）。"""
    raw_token = (request.POST.get("token") or request.GET.get("token") or "").strip()
    if request.method == "POST":
        return _accept_post(request, raw_token)
    return _accept_get(request, raw_token)
