# -*- coding: utf-8 -*-
"""统一 API 响应封套与幂等装饰器（V1.1 §5 契约标准）。

所有新模块 API 采用同一响应结构（{data, meta}）、同一错误结构（{error, meta}）、
同一分页格式、同一幂等语义。旧 API 逐步迁移到此处。
"""
import functools
import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone

from apps.core.models import IdempotencyRecord

logger = logging.getLogger("hotel.api")


class ApiError(Exception):
    """业务错误，携带 HTTP 状态码与错误码（V1.1 §5.3）。"""

    STATUS_CODES = {
        "VALIDATION_ERROR": 400,
        "AUTHENTICATION_REQUIRED": 401,
        "PERMISSION_DENIED": 403,
        "RESOURCE_NOT_FOUND": 404,
        "BUSINESS_CONFLICT": 409,
        "RATE_LIMITED": 429,
        "INTERNAL_ERROR": 500,
    }

    def __init__(self, code, message, field_errors=None, retryable=False):
        self.code = code
        self.message = message
        self.field_errors = field_errors or {}
        self.retryable = retryable
        self.status_code = self.STATUS_CODES.get(code, 500)
        super().__init__(message)


def _meta(request, pagination=None):
    """生成 meta 节点。"""
    meta = {
        "request_id": getattr(request, "request_id", "") or uuid.uuid4().hex[:16],
        "source_system": "PMS2",
        "generated_at": timezone.now().isoformat(),
    }
    if pagination:
        meta["pagination"] = pagination
    return meta


def api_response(request, data, status=200, pagination=None):
    """统一成功响应封套：{data, meta}。"""
    return JsonResponse(
        {"data": data, "meta": _meta(request, pagination)},
        status=status, json_dumps_params={"ensure_ascii": False})


def api_error_response(request, exc):
    """统一错误响应封套：{error, meta}。"""
    if isinstance(exc, ApiError):
        return JsonResponse(
            {"error": {"code": exc.code, "message": exc.message,
                       "field_errors": exc.field_errors, "retryable": exc.retryable},
             "meta": _meta(request)},
            status=exc.status_code, json_dumps_params={"ensure_ascii": False})
    logger.exception("API 未预期错误：%s %s", request.method, request.path)
    return JsonResponse(
        {"error": {"code": "INTERNAL_ERROR",
                   "message": "服务器内部错误，请稍后重试或联系管理员。",
                   "field_errors": {}, "retryable": True},
         "meta": _meta(request)},
        status=500, json_dumps_params={"ensure_ascii": False})


def api_view(view):
    """API 统一装饰器：捕获 ApiError/未预期异常 → JSON 响应；透传 request_id。"""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        request.request_id = uuid.uuid4().hex[:16]
        try:
            return view(request, *args, **kwargs)
        except ApiError as exc:
            return api_error_response(request, exc)
        except Exception:
            logger.exception("API 未预期错误：%s %s", request.method, request.path)
            return api_error_response(request, Exception("internal"))

    return wrapper


def paginate(queryset, request, page_size_default=20, page_size_max=100):
    """统一分页：返回 (items, pagination_meta)。"""
    try:
        page = max(1, int(request.GET.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(page_size_max, max(1, int(request.GET.get("page_size", str(page_size_default)))))
    except (TypeError, ValueError):
        size = page_size_default
    total = queryset.count()
    offset = (page - 1) * size
    items = queryset[offset:offset + size]
    return items, {
        "page": page, "page_size": size, "total": total,
        "has_next": offset + size < total,
        "has_previous": page > 1,
    }


def idempotent(view):
    """幂等装饰器（V1.1 M-14）：Idempotency-Key + 请求体 hash 去重。

    同 key 同 body → 返回首次响应；同 key 不同 body → 409；TTL 24h。
    仅对 POST/PUT/PATCH 生效；GET 自动透传。
    """

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method not in ("POST", "PUT", "PATCH"):
            return view(request, *args, **kwargs)
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return view(request, *args, **kwargs)
        from apps.core.models import IdempotencyRecord
        from apps.core.models import Property
        prop = getattr(request, "property", None)
        body_hash = hashlib.sha256(request.body or b"").hexdigest()
        user = request.user if request.user.is_authenticated else None
        now = timezone.now()
        record, created = IdempotencyRecord.objects.get_or_create(
            property=prop, user=user, key=key[:128],
            defaults={
                "method": request.method, "path": request.path[:256],
                "request_hash": body_hash,
                "status": IdempotencyRecord.RecordStatus.PROCESSING,
                "expires_at": now + timedelta(hours=24),
            })
        if not created:
            if record.request_hash != body_hash:
                return JsonResponse(
                    {"error": {"code": "BUSINESS_CONFLICT",
                               "message": "幂等键已用于不同请求体。",
                               "field_errors": {}, "retryable": False}},
                    status=409, json_dumps_params={"ensure_ascii": False})
            if record.status == IdempotencyRecord.RecordStatus.COMPLETED:
                return JsonResponse(record.response_body or {},
                                    status=record.response_status or 200,
                                    json_dumps_params={"ensure_ascii": False})
        try:
            response = view(request, *args, **kwargs)
        except Exception:
            record.status = IdempotencyRecord.RecordStatus.FAILED
            record.save(update_fields=("status",))
            raise
        if 200 <= response.status_code < 300:
            try:
                record.response_status = response.status_code
                record.response_body = json.loads(response.content)
                record.status = IdempotencyRecord.RecordStatus.COMPLETED
                record.save(update_fields=("response_status", "response_body", "status"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return response

    return wrapper
