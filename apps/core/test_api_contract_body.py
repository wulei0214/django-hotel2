"""TASK-04：`apps.core.api_contract.parse_json_object` 行为契约。

背景：`apps/api` 下曾有 4 份逐字相同的 `_body`（guest_subresources、
housekeeping_workflow_center、inventory_admin_center、task_center），
已收敛到 core 唯一实现。本测试锁定三件事：

1. 空请求体视为 `{}`（无参 POST 不报错）；
2. 非法 JSON / 非 UTF-8 字节 → 400 `VALIDATION_ERROR`，且文案与收敛前逐字一致；
3. 顶层非对象 → 400 `VALIDATION_ERROR`，文案与收敛前逐字一致。

文案断言刻意逐字比对：这些 message 会直接出现在对客错误体里，改字即行为变更。
"""

from django.test import RequestFactory, SimpleTestCase

from apps.core.api_contract import ApiError, parse_json_object


class ParseJsonObjectTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, body):
        if body is None:
            # 显式空 body：RequestFactory 默认 data=None 会走 multipart 编码，
            # 这里必须给 bytes 才能得到真正的 b""。
            return self.factory.post("/probe", data=b"", content_type="application/json")
        return self.factory.post("/probe", data=body, content_type="application/json")

    def test_empty_body_returns_empty_dict(self):
        self.assertEqual(parse_json_object(self._post(None)), {})

    def test_valid_object_returns_payload(self):
        self.assertEqual(
            parse_json_object(self._post(b'{"reason": "ok", "count": 2}')),
            {"reason": "ok", "count": 2},
        )

    def test_invalid_json_raises_validation_error(self):
        with self.assertRaises(ApiError) as ctx:
            parse_json_object(self._post(b"{not json"))
        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.message, "请求体必须是合法 JSON 对象。")

    def test_non_utf8_bytes_raise_validation_error(self):
        with self.assertRaises(ApiError) as ctx:
            parse_json_object(self._post(b"\xff\xfe\x00"))
        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")
        self.assertEqual(ctx.exception.message, "请求体必须是合法 JSON 对象。")

    def test_top_level_array_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            parse_json_object(self._post(b"[1, 2, 3]"))
        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")
        self.assertEqual(ctx.exception.message, "请求体必须是 JSON 对象。")

    def test_top_level_scalar_rejected(self):
        for raw in (b'"text"', b"42", b"true", b"null"):
            with self.subTest(raw=raw):
                with self.assertRaises(ApiError) as ctx:
                    parse_json_object(self._post(raw))
                self.assertEqual(ctx.exception.message, "请求体必须是 JSON 对象。")
