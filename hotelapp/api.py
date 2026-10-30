"""对外 API v1：查房 → hold → 下单 → 支付回调 → 确认。

所有接口返回 JSON；写接口为 POST，支付回调使用 HMAC-SHA256 验签。
"""
import datetime
import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Reservation
from .services import inventory, reservations


def _json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None


def _parse_date(value, field):
    try:
        return datetime.datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 格式")


def _error(message, status=400, code="bad_request"):
    return JsonResponse({"ok": False, "code": code, "message": message}, status=status)


def _reservation_payload(reservation):
    return {
        "booking_no": reservation.booking_no,
        "status": reservation.status,
        "room_type": reservation.room_type,
        "check_in": str(reservation.check_in),
        "check_out": str(reservation.check_out),
        "rooms": reservation.rooms,
        "nights": reservation.nights,
        "total_amount": f"{reservation.total_amount:.2f}",
        "deposit_amount": f"{reservation.deposit_amount:.2f}",
        "expires_at": timezone.localtime(reservation.expires_at).isoformat(),
        "guest": {"name": reservation.guest.姓名, "phone": reservation.guest.手机号 or ""},
    }


@require_http_methods(["GET"])
def availability(request):
    """GET /api/v1/availability?room_type=大床房&check_in=2026-11-01&check_out=2026-11-03"""
    room_type = (request.GET.get("room_type") or "").strip()
    if not room_type:
        return _error("缺少 room_type")
    try:
        check_in = _parse_date(request.GET.get("check_in"), "check_in")
        check_out = _parse_date(request.GET.get("check_out"), "check_out")
        count = inventory.availability(room_type, check_in, check_out)
        price = inventory.room_type_price(room_type)
    except inventory.InventoryError as e:
        return _error(str(e))
    except ValueError as e:
        return _error(str(e))
    return JsonResponse({
        "ok": True,
        "room_type": room_type,
        "check_in": str(check_in),
        "check_out": str(check_out),
        "nights": (check_out - check_in).days,
        "available": count,
        "price_from": f"{price:.2f}",
    })


@csrf_exempt
@require_http_methods(["POST"])
def create_reservation(request):
    """POST /api/v1/reservations：创建 hold 并返回待支付预订。"""
    body = _json_body(request)
    if body is None:
        return _error("请求体必须是合法 JSON")
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    room_type = (body.get("room_type") or "").strip()
    try:
        check_in = _parse_date(body.get("check_in"), "check_in")
        check_out = _parse_date(body.get("check_out"), "check_out")
    except ValueError as e:
        return _error(str(e))
    try:
        rooms = int(body.get("rooms") or 1)
    except (TypeError, ValueError):
        return _error("rooms 必须是整数")
    idempotency_key = (request.headers.get("Idempotency-Key")
                       or body.get("idempotency_key") or "").strip() or None
    try:
        reservation, created = reservations.create_reservation(
            name=name, phone=phone, room_type=room_type,
            check_in=check_in, check_out=check_out, rooms=rooms,
            idempotency_key=idempotency_key, source="api",
            idcard=(body.get("idcard") or "").strip() or None,
        )
    except reservations.ReservationError as e:
        return _error(str(e), status=409, code="reservation_failed")
    except inventory.InventoryError as e:
        return _error(str(e), status=409, code="sold_out")
    payload = _reservation_payload(reservation)
    payload.update({"ok": True, "created": created})
    return JsonResponse(payload, status=201 if created else 200)


@require_http_methods(["GET"])
def reservation_detail(request, booking_no):
    """GET /api/v1/reservations/<booking_no>"""
    reservation = Reservation.objects.filter(booking_no=booking_no).first()
    if not reservation:
        return _error("预订不存在", status=404, code="not_found")
    payload = _reservation_payload(reservation)
    payload["ok"] = True
    payload["payments"] = [
        {"provider": p.provider, "provider_ref": p.provider_ref,
         "amount": f"{p.amount:.2f}", "status": p.status,
         "paid_at": timezone.localtime(p.paid_at).isoformat() if p.paid_at else None}
        for p in reservation.payments.order_by("id")
    ]
    return JsonResponse(payload)


def _verify_signature(request):
    secret = getattr(settings, "PAYMENT_WEBHOOK_SECRET", "")
    if not secret:
        return False
    signature = (request.headers.get("X-Signature") or "").strip()
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(signature, expected)


@csrf_exempt
@require_http_methods(["POST"])
def payment_callback(request):
    """POST /api/v1/payments/callback：支付机构回调，HMAC 验签 + 幂等确认。"""
    if not _verify_signature(request):
        return _error("签名校验失败", status=401, code="invalid_signature")
    body = _json_body(request)
    if body is None:
        return _error("请求体必须是合法 JSON")
    booking_no = (body.get("booking_no") or "").strip()
    provider_ref = (body.get("provider_ref") or "").strip()
    if not booking_no or not provider_ref:
        return _error("booking_no 与 provider_ref 不能为空")
    try:
        amount = Decimal(str(body.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return _error("amount 必须是数字")
    try:
        reservation, payment, created = reservations.confirm_payment(
            booking_no=booking_no,
            provider_ref=provider_ref,
            amount=amount,
            provider=(body.get("provider") or "mock").strip(),
            raw_payload=body,
        )
    except reservations.PaymentError as e:
        return _error(str(e), status=409, code="payment_rejected")
    return JsonResponse({
        "ok": True,
        "booking_no": reservation.booking_no,
        "status": reservation.status,
        "payment_status": payment.status if payment else "SUCCESS",
        "duplicate": not created,
    })


@require_http_methods(["GET"])
def openapi(request):
    """GET /api/v1/openapi.json：最小可用的 OpenAPI 3.0 描述。"""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "酒店预订 API", "version": "1.0.0"},
        "paths": {
            "/api/v1/availability": {
                "get": {
                    "summary": "查询房型可售数量",
                    "parameters": [
                        {"name": "room_type", "in": "query", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "check_in", "in": "query", "required": True,
                         "schema": {"type": "string", "format": "date"}},
                        {"name": "check_out", "in": "query", "required": True,
                         "schema": {"type": "string", "format": "date"}},
                    ],
                }
            },
            "/api/v1/reservations": {
                "post": {
                    "summary": "创建预订并 hold 库存",
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {"type": "object", "required": [
                            "name", "phone", "room_type", "check_in", "check_out"],
                            "properties": {
                                "name": {"type": "string"},
                                "phone": {"type": "string"},
                                "room_type": {"type": "string"},
                                "check_in": {"type": "string", "format": "date"},
                                "check_out": {"type": "string", "format": "date"},
                                "rooms": {"type": "integer", "default": 1},
                            }}}}},
                }
            },
            "/api/v1/reservations/{booking_no}": {
                "get": {
                    "summary": "查询预订详情",
                    "parameters": [{"name": "booking_no", "in": "path", "required": True,
                                    "schema": {"type": "string"}}],
                }
            },
            "/api/v1/payments/callback": {
                "post": {
                    "summary": "支付回调（HMAC-SHA256 验签）",
                    "parameters": [{"name": "X-Signature", "in": "header",
                                    "required": True, "schema": {"type": "string"}}],
                }
            },
        },
    }
    return JsonResponse(spec)
