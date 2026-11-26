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
from apps.core.phone import normalize_phone

# §6.3 初始限流策略：action -> [(维度, 次数, 窗口秒)]
DEFAULT_LIMITS = {
    "auth.login": [(("ip", "username"), 5, 300)],
    "public.reservation_create": [(("ip",), 10, 600), (("phone",), 3, 600)],
    "public.reservation_query": [(("ip",), 30, 300), (("booking", "ip"), 10, 300)],
    "public.reservation_cancel": [(("ip",), 10, 600), (("booking", "ip"), 5, 600)],
    "public.payment_intent": [(("ip",), 10, 300), (("booking",), 5, 300)],
    "public.invitation_activate": [(("ip",), 5, 900)],
    # AUTH-17（§5.3 邀请创建/验证/激活必须限流）：员工中心邀请管理面（已认证），
    # 覆盖创建/重发/撤销；正常批量入职场景远低于该阈值
    "identity.invitation_manage": [(("ip",), 30, 600)],
    # 通知待办重算是物业级聚合操作，限制单员工/IP 的重复触发。
    "pms.notification_refresh": [(("ip", "username"), 10, 60)],
    # R5：自助改密「当前密码」二次认证的失败限流。
    #   * 第一条与 auth.login 同口径（IP + username，5 次 / 300 秒）；
    #   * 第二条是 username 维度的全局桶（20 次 / 300 秒），防分布式换 IP 爆破
    #     （被猜的是「某个账号的密码」，只按 IP 分桶挡不住换源地址的并行尝试）。
    # 计数落在 RateLimitCounter（DB 内 F() 原子自增），跨会话、跨进程有效——
    # 会话级读-改-写会在并发请求下丢计数（2026-11-26 复核实证缺陷，已修）。
    "identity.pwd_reauth": [(("ip", "username"), 5, 300), (("username",), 20, 300)],
}


def _secret():
    return (getattr(settings, "RATE_LIMIT_HMAC_KEY", None) or settings.SECRET_KEY).encode("utf-8")


def fingerprint(value):
    """服务端密钥 HMAC 指纹；不可逆，不存明文手机号/用户名。"""
    if not value:
        return ""
    return hmac.new(_secret(), str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _client_ip(request):
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _limits_for(action):
    configured = getattr(settings, "PUBLIC_RATE_LIMITS", {})
    return configured.get(action, DEFAULT_LIMITS.get(action, []))


def _bucket_specs(request, action, *, phone="", username="", booking_no="", token=""):
    """返回该动作在各维度组合下的 (limit, window_seconds, key_hash)。

    check() / reset() / remaining_seconds() 共用同一套桶计算，避免
    「判定用的桶」与「清理用的桶」算不一致。
    """
    rules = _limits_for(action)
    if not rules:
        return []
    ip = _client_ip(request)
    values = {
        "ip": ip,
        "phone": fingerprint(normalize_phone(phone)),
        "username": fingerprint(username),
        "booking": (booking_no or "").strip().upper(),
        "token": fingerprint(token)[:16],
    }
    specs = []
    for dimensions, limit, window_seconds in rules:
        parts = [values[d] for d in dimensions if values.get(d)]
        if not parts:
            # 全部维度缺失（如无手机号且拿不到 IP）→ 用纯 IP 兜底；
            # IP 也缺失时按错误请求独立桶，绝不用空串共享桶
            parts = [ip or "malformed"]
        key_hash = hashlib.sha256((action + "|" + "|".join(parts)).encode("utf-8")).hexdigest()[:48]
        specs.append((limit, int(window_seconds), key_hash))
    return specs


def _window_bounds(now, window_seconds):
    window_start = now.replace(microsecond=0) - timedelta(
        seconds=int(now.timestamp()) % window_seconds
    )
    return window_start, window_start + timedelta(seconds=window_seconds)


def reset(request, action, *, phone="", username="", booking_no="", token=""):
    """清除某动作在当前维度组合下的计数（连续失败语义复位）。

    典型用途：自助改密二次认证在「当前密码」校验通过后清零失败配额。
    与 check() 使用同一套桶计算（_bucket_specs），不会算错桶。
    """
    deleted = 0
    for _limit, _window, key_hash in _bucket_specs(
        request,
        action,
        phone=phone,
        username=username,
        booking_no=booking_no,
        token=token,
    ):
        deleted += RateLimitCounter.objects.filter(key_hash=key_hash, action=action).delete()[0]
    return deleted


def remaining_seconds(request, action, *, phone="", username="", booking_no="", token=""):
    """返回当前维度组合下计数窗口的剩余秒数（无计数返回 0）。

    用于给用户「请在 N 秒后重试」这类提示；取各桶中最长的剩余时间
    （先被它挡住的那个桶才是真正的约束）。
    """
    remain = 0
    now = timezone.now()
    for _limit, _window, key_hash in _bucket_specs(
        request,
        action,
        phone=phone,
        username=username,
        booking_no=booking_no,
        token=token,
    ):
        counter = (
            RateLimitCounter.objects.filter(key_hash=key_hash, action=action)
            .order_by("-expires_at")
            .first()
        )
        if counter is None:
            continue
        seconds = int((counter.expires_at - now).total_seconds())
        if seconds > remain:
            remain = seconds
    return max(0, remain)


def cleanup_expired(now=None):
    """删除已过期计数行（可挂定时任务；返回删除数）。"""
    now = now or timezone.now()
    deleted, _ = RateLimitCounter.objects.filter(expires_at__lt=now).delete()
    return deleted


def check(
    request,
    action,
    *,
    phone="",
    username="",
    booking_no="",
    token="",
    fail_closed=True,
    consume=True,
):
    """多维度限流检查：任一维度超限返回 False（调用方回 429 + Retry-After）。

    - 每个维度原子自增（F 表达式），超限后计数继续累加便于观测但不放行。
    - ``consume=False`` 只查询当前计数不自增（peek），用于 SEC-02：登录
      场景需要先判断是否已超限，但只有认证**失败**才应消耗配额，避免
      高频正常登入登出的前台账号被误锁。
    - 存储异常：fail_closed=True（默认）拒绝请求；只读面可显式 fail_open。
    - 缺失维度指纹（如空手机号）跳过该维度，不产生共享桶（P0-02）。
    - **phone 维度先规范化再指纹化**（TASK-02）：`+86 138...`、`86138...`、
      `138-0000-1111` 必须落进同一个桶，否则同一号码换写法即可成倍放大
      配额。规范化在服务端完成，不依赖各调用方自行处理。
    """
    specs = _bucket_specs(
        request,
        action,
        phone=phone,
        username=username,
        booking_no=booking_no,
        token=token,
    )
    if not specs:
        return True
    now = timezone.now()
    for limit, window_seconds, key_hash in specs:
        window_start, expires_at = _window_bounds(now, window_seconds)
        try:
            with transaction.atomic():
                if consume:
                    counter, created = RateLimitCounter.objects.get_or_create(
                        key_hash=key_hash,
                        action=action,
                        window_start=window_start,
                        defaults={"count": 1, "expires_at": expires_at},
                    )
                    if not created:
                        RateLimitCounter.objects.filter(pk=counter.pk).update(count=F("count") + 1)
                        counter.refresh_from_db(fields=("count",))
                    current_count = counter.count
                    exceeded = current_count > limit
                else:
                    counter = RateLimitCounter.objects.filter(
                        key_hash=key_hash, action=action, window_start=window_start
                    ).first()
                    current_count = counter.count if counter else 0
                    # peek 不占用本次配额：计数已用满即拒绝下一次请求。
                    exceeded = current_count >= limit
                if exceeded:
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
