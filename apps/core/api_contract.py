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


def api_response(request, data, status=200, pagination=None, extra_meta=None):
    """统一成功响应封套：{data, meta}。

    extra_meta：可选，合并进 meta 的附加常量（如审计中心的 retention_note），
    不影响既有调用方。
    """
    meta = _meta(request, pagination)
    if extra_meta:
        meta.update(extra_meta)
    return JsonResponse(
        {"data": data, "meta": meta},
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


# ---------------------------------------------------------------------------
# P0-03 统一分页契约：全仓库唯一分页参数解析器（api_contract 为权威，
# apps/api/front_desk.py::_page_args 仅作薄适配委托到这里）。
# ---------------------------------------------------------------------------
PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100
# 页码上限：防御 page=10^20 之类的超大数字造成病态 OFFSET 查询。
PAGE_NUMBER_MAX = 100000


def parse_page_args(request, page_size_default=PAGE_SIZE_DEFAULT,
                    page_size_max=PAGE_SIZE_MAX):
    """解析 page/page_size 查询参数，返回 (page, page_size)。

    统一规则（所有接口同一口径，不做静默截断或静默回退）：
      * page 缺省为 1；page_size 缺省为 page_size_default；
      * 非整数、空字符串、0、负数、page_size 超过 page_size_max、
        page 超过 PAGE_NUMBER_MAX → 一律抛 ApiError("VALIDATION_ERROR") → 400；
      * page 合法范围 [1, PAGE_NUMBER_MAX]；page_size 合法范围 [1, page_size_max]。
    """
    raw_page = request.GET.get("page", "1")
    raw_size = request.GET.get("page_size", str(page_size_default))
    try:
        page = int(str(raw_page).strip())
    except (TypeError, ValueError):
        raise ApiError(
            "VALIDATION_ERROR",
            "page 必须是 1 至 %d 之间的整数。" % PAGE_NUMBER_MAX)
    try:
        size = int(str(raw_size).strip())
    except (TypeError, ValueError):
        raise ApiError(
            "VALIDATION_ERROR",
            "page_size 必须是 1 至 %d 之间的整数。" % page_size_max)
    if not 1 <= page <= PAGE_NUMBER_MAX:
        raise ApiError(
            "VALIDATION_ERROR",
            "page 必须是 1 至 %d 之间的整数。" % PAGE_NUMBER_MAX)
    if not 1 <= size <= page_size_max:
        raise ApiError(
            "VALIDATION_ERROR",
            "page_size 必须是 1 至 %d 之间的整数。" % page_size_max)
    return page, size


def ensure_stable_ordering(queryset):
    """保证 QuerySet 稳定排序（P0-03）：排序键未包含主键时追加主键兜底。

    - 无任何排序（显式 order_by 与模型 Meta.ordering 均无）→ order_by("pk")；
    - 排序仅来自 Meta.ordering 且不含主键 → 用 order_by 显式化 Meta 排序
      后再追加主键（直接 add_ordering 会触发“显式排序覆盖 Meta 排序”语义，
      导致原排序丢失）；
    - 已有显式排序但不含主键 → 克隆 QuerySet 后追加主键；
    - 已含主键（pk/id 或模型实际主键名）→ 原样返回；
    - 表达式排序无法静态判断 → 追加主键兜底。
    """
    model = queryset.model
    pk_name = model._meta.pk.name
    if not queryset.ordered:
        return queryset.order_by("pk")
    existing = tuple(queryset.query.order_by)
    from_meta = not existing
    if from_meta:
        existing = tuple(model._meta.ordering or ())
    if not existing:
        return queryset.order_by("pk")
    if all(isinstance(item, str) for item in existing):
        names = {str(item).lstrip("+-").lower() for item in existing}
        if pk_name.lower() in names or "pk" in names:
            return queryset
        if from_meta:
            return queryset.order_by(*existing, pk_name)
    clone = queryset._chain()
    clone.query.add_ordering(pk_name)
    return clone


def paginate(queryset, request, page_size_default=PAGE_SIZE_DEFAULT,
             page_size_max=PAGE_SIZE_MAX):
    """统一分页（P0-03 唯一权威实现）：返回 (items, pagination_meta)。

    - 参数解析走 parse_page_args：非法 page/page_size 统一 400 VALIDATION_ERROR；
    - 稳定排序走 ensure_stable_ordering：调用方未写主键兜底时自动追加。
    """
    page, size = parse_page_args(
        request, page_size_default=page_size_default, page_size_max=page_size_max)
    queryset = ensure_stable_ordering(queryset)
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
            # P0-03：同键并发请求在首个请求 PROCESSING 期间返回 425 冲突，
            # 不得同时进入业务写逻辑；超时（5 分钟）后允许重试恢复。
            if record.status == IdempotencyRecord.RecordStatus.PROCESSING:
                _proc_timeout = timezone.timedelta(minutes=5)
                _proc_age = timezone.now() - (record.updated_at or record.created_at)
                if _proc_age < _proc_timeout:
                    return JsonResponse(
                        {"error": {"code": "IDEMPOTENCY_PROCESSING",
                                   "message": "相同幂等键的请求正在处理中，请稍后重试。",
                                   "field_errors": {}, "retryable": True}},
                        status=425, json_dumps_params={"ensure_ascii": False})
                # PROCESSING 超时 → 标记 FAILED 允许重试恢复
                record.status = IdempotencyRecord.RecordStatus.FAILED
                record.save(update_fields=("status", "updated_at"))
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
