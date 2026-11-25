"""ARCH-05 / Q6：结构化日志与请求关联标识的负向与契约测试。

任务书要求：日志采用结构化 JSON；关键请求具备 request_id 或等效关联标识；
审计/应用日志分离且不得记录敏感明文。本文件先验证缺口，后固定契约。
"""

import json
import logging

from django.test import SimpleTestCase

from apps.core.request_context import RequestIDMiddleware


class RequestIDMiddlewareTests(SimpleTestCase):
    """入站 X-Request-ID 复用、缺失时生成，并回写响应头。"""

    def _run(self, inbound=None):
        middleware = RequestIDMiddleware(lambda request: _FakeResponse())
        request = _FakeRequest(inbound)
        response = middleware(request)
        return request, response

    def test_accepts_inbound_request_id(self):
        request, response = self._run("abc123")
        self.assertEqual(request.request_id, "abc123")
        self.assertEqual(response.headers["X-Request-ID"], "abc123")

    def test_generates_request_id_when_absent(self):
        request, response = self._run(None)
        self.assertTrue(request.request_id)
        self.assertEqual(response.headers["X-Request-ID"], request.request_id)

    def test_rejects_untrusted_long_or_invalid_value(self):
        request, response = self._run("x" * 200)
        self.assertLessEqual(len(request.request_id), 64)
        self.assertNotEqual(request.request_id, "x" * 200)


class StructuredLoggingContractTests(SimpleTestCase):
    """日志必须为 JSON 且关键 logger 有 handler（旧实现 hotel.api 无 handler）。"""

    def test_hotel_api_logger_has_handler(self):
        api_logger = logging.getLogger("hotel.api")
        self.assertTrue(
            api_logger.handlers or api_logger.propagate,
            "hotel.api 必须落 handler 或向上传播，否则 ERROR 丢失",
        )

    def test_json_formatter_emits_structured_fields(self):
        from typing import Any, cast

        from hotel.settings import LOGGING

        formatters = cast("dict[str, Any]", LOGGING["formatters"])
        formatter_spec = formatters.get("json")
        if formatter_spec is None:
            self.fail("必须存在结构化 JSON formatter")
        formatter = formatter_spec.get("()", None)
        self.assertTrue(
            formatter_spec.get("class", "").endswith("JsonFormatter") or formatter,
            "formatter 必须为 JSON 结构化输出",
        )

    def test_formatter_output_is_parseable_json(self):
        from apps.core.logging_utils import JsonFormatter

        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="hotel.api",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="boom",
            args=(),
            exc_info=None,
        )
        record.request_id = "rid-1"
        record.property_id = 3
        payload = json.loads(formatter.format(record))
        self.assertEqual(payload["level"], "ERROR")
        self.assertEqual(payload["logger"], "hotel.api")
        self.assertEqual(payload["request_id"], "rid-1")
        self.assertEqual(payload["property_id"], 3)
        self.assertIn("timestamp", payload)

    def test_formatter_redacts_sensitive_fields(self):
        from apps.core.logging_utils import JsonFormatter

        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="hotel.api",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="phone=13800001234 id_card=110101199001019999",
            args=(),
            exc_info=None,
        )
        record.request_id = ""
        text = formatter.format(record)
        self.assertNotIn("13800001234", text)
        self.assertNotIn("110101199001019999", text)


class _FakeResponse:
    def __init__(self):
        self.headers = {}


class _FakeRequest:
    def __init__(self, inbound):
        self.META = {}
        if inbound is not None:
            self.META["HTTP_X_REQUEST_ID"] = inbound
