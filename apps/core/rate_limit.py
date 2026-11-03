# -*- coding: utf-8 -*-
"""AUTH-02（P0-01~04）：公共面限流服务。

规则依据《身份账户体系改造开发方案 2.0》§6.2/§6.3：
- 独立 RateLimitCounter 表 + F() 原子自增；AuditEvent 只做审计不做计数。
- 维度：IP / HMAC(phone) / HMAC(username)+IP / booking_no / token 指纹；
  缺失维度（如空手机号）不形成共享全局桶，只按 IP 限（P0-02）。
- 阈值可经 settings.PUBLIC_RATE_LIMITS 覆盖；生产不得缺省为无限制。
- 高风险动作（登录/取消/支付意图/预订创建）默认 fail-closed：
  计数存储异常时拒绝请求；429 返回 Retry-After。
"""
import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.core.models import RateLimitCounter

# §6.3 初始限流策略：action -> [(维度, 次数, 窗口秒)]
DEFAULT_LIMITS = {
    "auth.login": [(("ip", "username"), 5, 300)],
    "public.reservation_create": [(("ip",), 10, 600), (("phone",), 3, 600)],
    "public.reservation_query": [(("ip",), 30, 300), (("booking", "ip"), 10, 300)],
    "public.reservation_cancel": [(("ip",), 10, 600), (("booking", "ip"), 5, 600)],
    "public.payment_intent": [(("ip",), 10, 300), (("booking",), 5, 300)],
    "public.invitation_activate": [(("ip",), 5, 900)],
}


def _secret():
    return (getattr(settings, "RATE_LIMIT_HMAC_KEY", None)
            or settings.SECRET_KEY).encode("utf-8")


def fingerprint(value):
    """服务端密钥 HMAC 指纹；不可逆，不存明文手机号/用户名。"""
    if not value:
        return ""
    return hmac.new(_secret(), str(value).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _client_ip(request):
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _limits_for(action):
    configured = getattr(settings, "PUBLIC_RATE_LIMITS", {})
    return configured.get(action, DEFAULT_LIMITS.get(action, []))


def cleanup_expired(now=None):
    """删除已过期计数行（可挂定时任务；返回删除数）。"""
    now = now or timezone.now()
    deleted, _ = RateLimitCounter.objects.filter(expires_at__lt=now).delete()
    return deleted


def check(request, action, *, phone="", username="", booking_no="", token="",
          fail_closed=True):
    """多维度限流检查：任一维度超限返回 False（调用方回 429 + Retry-After）。

    - 每个维度原子自增（F 表达式），超限后计数继续累加便于观测但不放行。
    - 存储异常：fail_closed=True（默认）拒绝请求；只读面可显式 fail_open。
    - 缺失维度指纹（如空手机号）跳过该维度，不产生共享桶（P0-02）。
    """
    rules = _limits_for(action)
    if not rules:
        return True
    ip = _client_ip(request)
    values = {
        "ip": ip,
        "phone": fingerprint(phone),
        "username": fingerprint(username),
        "booking": (booking_no or "").strip().upper(),
        "token": fingerprint(token)[:16],
    }
    now = timezone.now()
    for dimensions, limit, window_seconds in rules:
        parts = [values[d] for d in dimensions if values.get(d)]
        if not parts:
            # 全部维度缺失（如无手机号且拿不到 IP）→ 用纯 IP 兜底；
            # IP 也缺失时按错误请求独立桶，绝不用空串共享桶
            parts = [ip or "malformed"]
        key_hash = hashlib.sha256(
            (action + "|" + "|".join(parts)).encode("utf-8")).hexdigest()[:48]
        window_start = now.replace(microsecond=0) - timedelta(
            seconds=int(now.timestamp()) % window_seconds)
        expires_at = window_start + timedelta(seconds=window_seconds)
        try:
            with transaction.atomic():
                counter, created = RateLimitCounter.objects.get_or_create(
                    key_hash=key_hash, action=action, window_start=window_start,
                    defaults={"count": 1, "expires_at": expires_at})
                if not created:
                    RateLimitCounter.objects.filter(pk=counter.pk).update(
                        count=F("count") + 1)
                    counter.refresh_from_db(fields=("count",))
                if counter.count > limit:
                    return False
        except Exception:
            if fail_closed:
                return False
            continue
    return True


def retry_after_seconds(action):
    """429 Retry-After：取该动作最短窗口（秒）。"""
    rules = _limits_for(action)
    return min((w for _d, _n, w in rules), default=60)


