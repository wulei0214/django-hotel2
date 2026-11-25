"""请求关联标识中间件（ARCH-05 / Q6）。

职责：为每个请求确定一个可追踪的 request_id，并回写响应头。
- 入站 `X-Request-ID` 仅在符合安全字符集与长度上限时复用；
- 缺失或非法时生成新的 16 位十六进制标识；
- 不记录、不回显任何敏感值。
"""

import contextlib
import re
import uuid

REQUEST_ID_HEADER = "X-Request-ID"
MAX_LENGTH = 64
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def resolve_request_id(inbound):
    """返回可信的 request_id：合法入站值优先，否则新生成。"""
    candidate = str(inbound or "").strip()
    if candidate and len(candidate) <= MAX_LENGTH and _SAFE_PATTERN.match(candidate):
        return candidate
    return uuid.uuid4().hex[:16]


class RequestIDMiddleware:
    """把 request_id 挂到 request 上并写入响应头。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = resolve_request_id(request.META.get("HTTP_X_REQUEST_ID", ""))
        response = self.get_response(request)
        with contextlib.suppress(AttributeError):  # 极少数响应对象无 headers（如测试替身）
            response.headers[REQUEST_ID_HEADER] = request.request_id
        return response
