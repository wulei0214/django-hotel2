import functools
# API 安全阅读提示：公共预订与订单查询属于匿名访问面；必须关注限流、审计、字段脱敏、幂等和生产环境 Mock 支付门禁。页面参数和 booking_no + phone 组合不能代替访问频率控制。
import json
import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.http import JsonResponse
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.core import rate_limit
from apps.billing.models import Payment
from apps.core.models import AuditEvent
from apps.billing.services import (
    PaymentConflict,
    create_mock_payment_intent,
    process_mock_payment_callback,
    sign_mock_callback,
    verify_mock_callback,
)
from apps.guests.models import Guest
from apps.inventory.models import RoomType
from apps.inventory.services import InventoryUnavailable, get_room_type_availability
from apps.reservations.models import Reservation
from apps.reservations.models import BookingAccessToken
from apps.reservations.services import (
    ReservationConflict,
    create_reservation,
    cancel_pending_reservation,
    normalize_phone,
)

logger = logging.getLogger("hotel.api")

# 与预订服务/前端预订页同口径的业务边界
MAX_ROOMS = 10          # 单笔预订最多间数
MAX_GUESTS = 10         # 成人/儿童各自上限
MAX_NIGHTS = 30         # 单笔预订最长连住晚数


def api_guard(view):
    """API 统一兜底：未预期异常记日志并返回 JSON 500，不向外暴露堆栈；
    405 方法错误也统一为 JSON；已预期的业务异常（ValueError/Conflict 等）
    由各视图自行转换为 4xx。"""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            result = view(request, *args, **kwargs)
        except Exception:
            logger.exception("API 未预期错误：%s %s", request.method, request.path)
            return JsonResponse(
                {"error": "服务器内部错误，请稍后重试或联系管理员。"}, status=500
            )
        if getattr(result, "status_code", None) == 405:
            return JsonResponse({"error": "不支持的 HTTP 方法，请检查请求方式。"}, status=405)
        return result

    return wrapper


def csrf_failure(request, reason=""):
    """CSRF 失败视图：/api/ 与 /platform/api/ 路径返回 JSON（保持 API 错误契约），其余走默认 HTML 页。"""
    if (request.path.startswith("/api/")
            or request.path.startswith("/platform/api/")):
        return JsonResponse(
            {"error": f"CSRF 校验失败：{reason or '缺少 CSRF 令牌，请先获取 csrftoken。'}"},
            status=403,
        )
    from django.views.csrf import csrf_failure as default_csrf_failure
    return default_csrf_failure(request, reason)


def _body(request):
    try:
        value = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("请求体必须是有效 JSON。")
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象。")
    return value


def _date(value, label):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须使用 YYYY-MM-DD 格式。")


def _integer(value, label, default=None):
    if value is None and default is not None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是整数。")


@require_GET
@api_guard
def availability(request):
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        return JsonResponse({"error": "酒店物业不可用。"}, status=503)

    try:
        check_in = _date(request.GET.get("check_in"), "入住日期")
        check_out = _date(request.GET.get("check_out"), "离店日期")
        rooms = _integer(request.GET.get("rooms", "1"), "房间数")
        if rooms < 1 or rooms > MAX_ROOMS:
            raise ValueError(f"房间数必须在 1 至 {MAX_ROOMS} 间之间。")
        # 与创建预订同口径：不接受历史日期的查询（否则库存服务会静默跳过，返回误导性的空列表）
        if check_in < date.today():
            raise ValueError("入住日期不能早于今天。")
        if check_out <= check_in:
            raise ValueError("离店日期必须晚于入住日期。")
        if (check_out - check_in).days > MAX_NIGHTS:
            raise ValueError(f"单次查询最长 {MAX_NIGHTS} 晚。")
        room_type_id = request.GET.get("room_type_id")
        room_types = RoomType.objects.filter(property=property_obj, is_active=True)
        if room_type_id:
            room_types = room_types.filter(pk=_integer(room_type_id, "房型编号"))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    data = []
    for room_type in room_types.order_by("code"):
        try:
            result = get_room_type_availability(
                property_obj, room_type, check_in, check_out, rooms=rooms
            )
        except ValueError:
            continue
        data.append(result)

    return JsonResponse(
        {
            "property": property_obj.code,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "rooms": rooms,
            "data": data,
        }
    )


@require_POST
@api_guard
def reservations_create(request):
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        return JsonResponse({"error": "酒店物业不可用。"}, status=503)

    try:
        body = _body(request)
        # §6.3/§6.4 公开预订防刷：IP 10/10min + phone_hash 3/10min（fail-closed）
        raw_phone = str(body.get("phone") or "")
        if not rate_limit.check(request, "public.reservation_create", phone=raw_phone):
            AuditEvent.record(
                action="public.reservation_blocked", entity_type="PublicEndpoint",
                entity_id="rate_limited", request=request)
            return _throttle_deny(request, "public.reservation_create")
        payload = {
            "full_name": str(body.get("full_name") or ""),
            "phone": str(body.get("phone") or ""),
            "room_type_id": _integer(body.get("room_type_id"), "房型编号"),
            "rate_plan_id": (
                _integer(body.get("rate_plan_id"), "房价计划编号")
                if body.get("rate_plan_id") is not None
                else None
            ),
            "check_in": _date(body.get("check_in"), "入住日期"),
            "check_out": _date(body.get("check_out"), "离店日期"),
            "rooms": _integer(body.get("rooms", 1), "房间数"),
            "adults": _integer(body.get("adults", 1), "成人数"),
            "children": _integer(body.get("children", 0), "儿童数"),
        }
        # 业务边界校验（与 OpenAPI schema 声明一致，创建服务兜底）
        if payload["rooms"] < 1 or payload["rooms"] > MAX_ROOMS:
            raise ValueError(f"房间数必须在 1 至 {MAX_ROOMS} 间之间。")
        if payload["adults"] < 1 or payload["adults"] > MAX_GUESTS:
            raise ValueError(f"成人数必须在 1 至 {MAX_GUESTS} 人之间。")
        if payload["children"] < 0 or payload["children"] > MAX_GUESTS:
            raise ValueError(f"儿童数必须在 0 至 {MAX_GUESTS} 人之间。")
        if payload["check_in"] < date.today():
            raise ValueError("入住日期不能早于今天。")
        if payload["check_out"] <= payload["check_in"]:
            raise ValueError("离店日期必须晚于入住日期。")
        if (payload["check_out"] - payload["check_in"]).days > MAX_NIGHTS:
            raise ValueError(f"单笔预订最长 {MAX_NIGHTS} 晚。")
        reservation, created, access_token = create_reservation(
            property_obj=property_obj,
            payload=payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        )
        if created:
            AuditEvent.record(
                action="public.reservation_created", entity_type="Reservation",
                entity_id=reservation.booking_no, property_obj=property_obj,
                after={"rooms": reservation.room_count,
                       "total": str(reservation.total_amount)},
                request=request)
    except ReservationConflict as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except RoomType.DoesNotExist:
        return JsonResponse({"error": "房型不存在或未启用。"}, status=404)
    except InventoryUnavailable as exc:
        return JsonResponse({"error": str(exc)}, status=409)

    response_body = {
        "booking_no": reservation.booking_no,
        "status": reservation.status,
        "check_in": reservation.check_in.isoformat(),
        "check_out": reservation.check_out.isoformat(),
        "room_count": reservation.room_count,
        "total_amount": str(reservation.total_amount),
        "currency": reservation.currency,
        "expires_at": reservation.expires_at.isoformat() if reservation.expires_at else None,
        "created": created,
    }
    if access_token:
        # P0-08：明文 token 仅此一次返回，请顾客保存
        response_body["access_token"] = access_token
    return JsonResponse(response_body, status=201 if created else 200)


@require_GET
@api_guard
def reservation_detail(request, booking_no):
    """P0-08/09/10：订单查询凭 Booking Access Token；字段白名单脱敏。"""
    property_obj = getattr(request, "property", None)
    token = request.headers.get("X-Booking-Token", "") or request.GET.get("token", "")
    from apps.core import rate_limit
    if not rate_limit.check(request, "public.reservation_query", booking_no=booking_no):
        return _throttle_deny(request, "public.reservation_query")
    reservation = BookingAccessToken.validate(booking_no, token)
    if reservation is None or reservation.property_id != property_obj.pk:
        AuditEvent.record(
            action="guest.booking_access_denied", entity_type="Reservation",
            entity_id=booking_no[:32], reason="detail_token_invalid",
            request=request)
        return JsonResponse({"error": "预订编号或访问令牌不正确。"}, status=404)
    AuditEvent.record(
        action="guest.booking_access_viewed", entity_type="Reservation",
        entity_id=reservation.booking_no, property_obj=property_obj,
        request=request)
    # §6.5 字段白名单：姓名脱敏；不返回完整手机号/证件号/provider_ref/内部流水
    guest_name = reservation.guest.full_name or ""
    masked_name = guest_name[:1] + "*" * max(len(guest_name) - 1, 1)
    payments = reservation.payments.order_by("-created_at")
    return JsonResponse(
        {
            "booking_no": reservation.booking_no,
            "status": reservation.status,
            "guest_name": masked_name,
            "check_in": reservation.check_in.isoformat(),
            "check_out": reservation.check_out.isoformat(),
            "rooms": reservation.room_count,
            "total_amount": str(reservation.total_amount),
            "currency": reservation.currency,
            "payments": [
                {
                    "amount": str(item.amount),
                    "status": item.status,
                    "paid_at": item.paid_at.isoformat() if item.paid_at else None,
                }
                for item in payments
            ],
        }
    )


def _is_dev_env():
    return settings.DEBUG and getattr(settings, "DJANGO_ENV", "dev") in {"dev", "test"}


def _throttle_deny(request, action):
    """429 统一响应：稳定 JSON + Retry-After；落 public.rate_limited 审计。"""
    from apps.core import rate_limit
    from apps.core.models import AuditEvent
    AuditEvent.record(
        action="public.rate_limited", entity_type="PublicEndpoint",
        entity_id=action, reason="ip=%s" % request.META.get("REMOTE_ADDR", ""),
        request=request)
    retry = rate_limit.retry_after_seconds(action)
    response = JsonResponse(
        {"error": "请求过于频繁，请稍后再试。", "retry_after": retry}, status=429)
    response["Retry-After"] = str(retry)
    return response


@require_POST
@api_guard
def payment_intent_create(request):
    property_obj = getattr(request, "property", None)
    # AUTH-07（P0-12）：mock 支付门禁——生产环境拒绝创建模拟支付
    if not _is_dev_env():
        return JsonResponse({"error": "模拟支付仅在开发/测试环境开放。"}, status=403)
    try:
        body = _body(request)
        booking_no = str(body.get("booking_no") or "").strip()
        token = str(body.get("token") or "").strip()
    except Exception:
        return JsonResponse({"error": "请求体必须是有效 JSON。"}, status=400)
    # P0-08：订单访问一律凭 Booking Access Token；phone 不再作为凭据
    reservation = BookingAccessToken.validate(booking_no, token)
    if reservation is None or reservation.property_id != property_obj.pk:
        AuditEvent.record(
            action="guest.booking_access_denied", entity_type="Reservation",
            entity_id=booking_no[:32], reason="payment_intent_token_invalid",
            request=request)
        return JsonResponse({"error": "预订编号或访问令牌不正确。"}, status=404)
    # §6.3 支付意图限流：IP 10/5min，booking 5/5min（fail-closed）
    from apps.core import rate_limit
    if not rate_limit.check(request, "public.payment_intent", booking_no=booking_no):
        return _throttle_deny(request, "public.payment_intent")
    try:
        payment = create_mock_payment_intent(reservation)
    except PaymentConflict as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    response = {
        "provider": "mock",
        "provider_ref": payment.provider_ref,
        "status": payment.status,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "message": "这是本地模拟支付，不会发生真实资金扣款或清算。",
    }

    if settings.DEBUG and getattr(settings, "DJANGO_ENV", "dev") in {"dev", "test"}:
        callback = {
            "provider_ref": payment.provider_ref,
            "amount": str(payment.amount),
            "status": "SUCCESS",
        }
        raw_callback = json.dumps(
            callback, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        response["mock_callback_example"] = {
            "url": "/api/v1/payments/callback",
            "payload": callback,
            "signature": sign_mock_callback(raw_callback),
            "note": "仅用于本地测试；生产环境不提供此模拟回调样例。",
        }
    return JsonResponse(response, status=201)


@csrf_exempt
@require_POST
@api_guard
def payment_callback(request):
    if not settings.DEBUG or getattr(settings, "DJANGO_ENV", "dev") not in {"dev", "test"}:
        return JsonResponse({"error": "模拟支付回调仅允许在本地开发和测试环境使用。"}, status=404)

    if not verify_mock_callback(request.body, request.headers.get("X-Signature", "")):
        return JsonResponse({"error": "模拟支付回调签名验证失败。"}, status=401)

    try:
        body = _body(request)
        result = process_mock_payment_callback(
            provider_ref=str(body.get("provider_ref") or ""),
            amount=body.get("amount"),
            status=str(body.get("status") or "").upper(),
        )
    except PaymentConflict as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    reservation = result["reservation"]
    response = {
        "ok": True,
        "duplicate": result.get("duplicate", False),
        "expired": result.get("expired", False),
        "payment_status": result["payment"].status,
        "booking_no": reservation.booking_no,
        "reservation_status": reservation.status,
    }
    return JsonResponse(response)


@require_GET
@api_guard
def openapi_document(request):
    """兼容入口：返回完整 PMS2 OpenAPI 3.1 契约。"""
    from .openapi_schema import openapi_json
    return openapi_json(request)



@require_POST
@api_guard
def reservation_cancel(request, booking_no):
    """P0-08/10：订单取消凭 Booking Access Token；取消规则与幂等不变。"""
    property_obj = getattr(request, "property", None)
    try:
        body = _body(request)
    except Exception:
        return JsonResponse({"error": "请求体必须是有效 JSON。"}, status=400)
    token = str(body.get("token") or "").strip()
    from apps.core import rate_limit
    if not rate_limit.check(request, "public.reservation_cancel", booking_no=booking_no):
        return _throttle_deny(request, "public.reservation_cancel")
    reservation = BookingAccessToken.validate(booking_no, token)
    if reservation is None or reservation.property_id != property_obj.pk:
        AuditEvent.record(
            action="guest.booking_access_denied", entity_type="Reservation",
            entity_id=booking_no[:32], reason="cancel_token_invalid",
            request=request)
        return JsonResponse({"error": "预订编号或访问令牌不正确。"}, status=404)
    if reservation.status == Reservation.Status.CANCELLED:
        # 幂等：已取消的订单重复取消直接返回当前状态，不再变更
        return JsonResponse({
            "booking_no": reservation.booking_no,
            "status": reservation.status,
            "changed": False,
        })
    try:
        reservation, changed = cancel_pending_reservation(
            property_obj=property_obj,
            booking_no=booking_no,
            reservation_pk=reservation.pk,
        )
    except ReservationConflict as exc:
        AuditEvent.record(
            action="guest.booking_cancelled", entity_type="Reservation",
            entity_id=booking_no[:32], property_obj=property_obj,
            reason="cancelled_rejected: %s" % str(exc)[:80], request=request)
        return JsonResponse({"error": str(exc)}, status=409)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    AuditEvent.record(
        action="guest.booking_cancelled", entity_type="Reservation",
        entity_id=reservation.booking_no, property_obj=property_obj,
        after={"status": reservation.status, "changed": changed},
        request=request)
    return JsonResponse({
        "booking_no": reservation.booking_no,
        "status": reservation.status,
        "changed": changed,
    })
