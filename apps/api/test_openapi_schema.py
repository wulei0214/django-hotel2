"""PMS2 OpenAPI 契约一致性测试（方案 §9.8）。

覆盖：
1. openapi.json 可解析且 OpenAPI 3.1 结构合法（operationId 唯一、$ref 全部可解析、
   每个 operation 有 summary/tags/成功响应 schema）。
2. schema 与 apps.api.urls 双向一致：无遗漏路由、无幽灵路由。
3. 每个业务 operation 都有 x-contract-status（verified/partial/planned），
   非 planned 端点必须有具体请求/响应 schema。
4. 抽样断言：公开预订创建请求字段与视图实际一致；错误结构统一。
5. x-required-permission 与视图装饰器/AuthorizationService 校验一致（全量抽样核对）。
6. 公开预订三端点特别标注：Idempotency-Key 幂等、§6.3 限流阈值（与
   apps.core.rate_limit.DEFAULT_LIMITS 对齐）、Booking Access Token、审计事件。
"""
import json
import re
from pathlib import Path

from django.conf import settings

from django.test import TestCase
from django.urls import URLPattern, resolve

from apps.api.openapi_schema import BASE, build_openapi
from apps.core.rate_limit import DEFAULT_LIMITS

CONTRACT_STATUSES = {"verified", "partial", "planned"}

# 与视图源码逐条核对后的权限码矩阵（视图装饰器/AuthorizationService 调用点）。
PERMISSION_MATRIX = {
    ("get", "/front-desk/overview"): "hotelapp.view_accommodation",
    ("get", "/front-desk/arrivals"): "hotelapp.view_accommodation",
    ("get", "/front-desk/reservations"): "hotelapp.view_accommodation",
    ("get", "/front-desk/rooms"): "hotelapp.view_accommodation",
    ("post", "/front-desk/reservation-rooms/{reservation_room_id}/assign"):
        "hotelapp.change_accommodation",
    ("post", "/front-desk/reservation-rooms/{reservation_room_id}/check-in"):
        "hotelapp.change_accommodation",
    ("post", "/front-desk/stays/{stay_id}/check-out"): "hotelapp.change_accommodation",
    ("post", "/front-desk/reservation-rooms/{reservation_room_id}/no-show"):
        "hotelapp.no_show_handle",
    ("post", "/front-desk/payments/{payment_id}/refunds"): "billing.refund_request",
    ("post", "/front-desk/refunds/{refund_id}/approve"): "billing.refund_approve",
    ("get", "/night-audit/preview"): "core.night_audit_view",
    ("post", "/night-audit/run"): "hotelapp.night_audit_execute",
    ("get", "/night-audit/business-dates"): "core.night_audit_view",
    ("get", "/night-audit/business-dates/current"): "core.night_audit_view",
    ("get", "/night-audit/business-dates/{business_date_id}"): "core.night_audit_view",
    ("get", "/night-audit/break-glass"): "core.night_audit_view",
    ("get", "/night-audit/runs"): "core.night_audit_view",
    ("get", "/night-audit/runs/{run_id}"): "core.night_audit_view",
    ("post", "/night-audit/business-dates/{business_date_id}/reopen-request"):
        "hotelapp.business_date_reopen",
    ("post", "/night-audit/break-glass/{record_id}/review"): "core.breakglass_review",
    ("get", "/guests"): "hotelapp.view_client",
    ("get", "/guests/{guest_id}"): "hotelapp.view_client",
    ("post", "/guests/create"): "hotelapp.add_client",
    ("post", "/guests/{guest_id}/update"): "hotelapp.change_client",
    ("post", "/guests/{guest_id}/documents/{document_id}/reveal"):
        "hotelapp.guest_document_reveal",
    ("post", "/guests/{guest_id}/phones/{phone_id}/reveal"):
        "hotelapp.guest_phone_reveal",
    ("get", "/rooms/status"): "hotelapp.view_room",
    ("get", "/room-types"): "hotelapp.view_room",
    ("get", "/housekeeping/rooms"): "inventory.housekeeping_view",
    ("get", "/housekeeping/summary"): "inventory.housekeeping_view",
    ("post", "/housekeeping/rooms/{room_id}/status"): "inventory.housekeeping_update",
    ("post", "/housekeeping/rooms/{room_id}/maintenance"): "inventory.maintenance_manage",
    ("get", "/rate-calendar"): "hotelapp.view_room",
    ("post", "/daily-rates/{rate_id}/adjust"): "inventory.manage_rates",
    ("get", "/financial-summary"): "hotelapp.finance_access",
    ("get", "/billing/folios"): "hotelapp.finance_access",
    ("get", "/billing/folios/{folio_id}"): "hotelapp.finance_access",
    ("get", "/billing/ledger"): "hotelapp.finance_access",
    ("get", "/billing/payments"): "hotelapp.finance_access",
    ("post", "/billing/payments"): "hotelapp.finance_access",
    ("get", "/billing/refunds"): "hotelapp.finance_access",
    ("post", "/billing/refunds"): "billing.refund_request",
    ("post", "/billing/refunds/{refund_id}/approve"): "billing.refund_approve",
    ("post", "/billing/refunds/{refund_id}/reject"): "billing.refund_approve",
    ("post", "/billing/refunds/{refund_id}/execute"): "billing.refund_execute",
    ("post", "/billing/shifts/open"): "billing.cashier_shift_manage",
    ("post", "/billing/shifts/{shift_id}/close"): "billing.cashier_shift_manage",
    ("post", "/billing/shifts/{shift_id}/movements"): "billing.cash_movement_manage",
    ("post", "/billing/reversals"): "billing.reverse_entry",
    ("get", "/billing/shifts"): "billing.cashier_shift_manage",
    ("get", "/billing/summary"): "hotelapp.finance_access",
    ("get", "/approvals"): "billing.approval_view",  # P0-01：专项只读权限
    ("post", "/approvals/request"): "ApprovalActionRegistry",  # P0-02：动作白名单
    ("post", "/approvals/{record_id}/approve"): "ApprovalActionRegistry.approve_permission",
    ("post", "/approvals/{record_id}/reject"): "ApprovalActionRegistry.approve_permission",
    ("get", "/reservations-mgmt"): "hotelapp.view_accommodation",
    ("get", "/reservations-mgmt/{booking_no}"): "hotelapp.view_accommodation",
    # P0-04 定版：audit-timeline 与 /audit/events 同门禁 core.audit_view
    # （WB-46-09：契约矩阵/文档此前仍写旧码 identity.staff_manage）
    ("get", "/audit-timeline"): "core.audit_view",
    ("get", "/staff"): "identity.staff_manage",
    ("get", "/staff/{profile_id}"): "identity.staff_manage",
    ("get", "/memberships"): "hotelapp.view_vip",
    ("get", "/memberships/{membership_id}"): "hotelapp.view_vip",
    ("post", "/memberships/create"): "hotelapp.add_vip",
    ("post", "/memberships/{membership_id}/level"): "hotelapp.change_vip",
}

# 公开面限流标注 → apps.core.rate_limit.DEFAULT_LIMITS 的 action 对应关系。
RATE_LIMIT_ANNOTATIONS = {
    "/reservations": "public.reservation_create",
    "/reservations/{booking_no}": "public.reservation_query",
    "/reservations/{booking_no}/cancel": "public.reservation_cancel",
    "/payments/intents": "public.payment_intent",
}


def _iter_operations(spec):
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            yield path, method, operation


def _route_paths():
    """把 apps.api.urls 的路由渲染成 OpenAPI path 形式（/api/v1 前缀 + {var}）。"""
    from apps.api import urls as api_urls
    result = set()
    for pattern in api_urls.urlpatterns:
        if isinstance(pattern, URLPattern):
            openapi_path = re.sub(r"<(int|str):(\w+)>", r"{\2}", str(pattern.pattern))
            result.add(f"{BASE}/{openapi_path}")
    return result


class PMS2OpenApiDocumentationTests(TestCase):
    def test_openapi_endpoint_is_openapi_31_and_contains_registered_business_apis(self):
        response = self.client.get("/api/v1/openapi.json")
        self.assertEqual(response.status_code, 200)
        spec = response.json()
        self.assertEqual(spec["openapi"], "3.1.0")
        paths = spec["paths"]
        self.assertEqual(set(paths), _route_paths(), "OpenAPI paths 必须与 apps.api.urls 注册路由集合完全一致")
        expected = {
            "/api/v1/availability": {"get"},
            "/api/v1/reservations": {"post"},
            "/api/v1/reservations/{booking_no}": {"get"},
            "/api/v1/reservations/{booking_no}/cancel": {"post"},
            "/api/v1/reports/occupancy": {"get"},
            "/api/v1/reports/adr": {"get"},
            "/api/v1/reports/revenue": {"get"},
            "/api/v1/reports/guests": {"get"},
            "/api/v1/reports/housekeeping": {"get"},
            "/api/v1/front-desk/overview": {"get"},
            "/api/v1/front-desk/reservation-rooms/{reservation_room_id}/check-in": {"post"},
            "/api/v1/night-audit/preview": {"get"},
            "/api/v1/night-audit/run": {"post"},
            "/api/v1/night-audit/business-dates": {"get"},
            "/api/v1/night-audit/business-dates/current": {"get"},
            "/api/v1/night-audit/business-dates/{business_date_id}": {"get"},
            "/api/v1/night-audit/break-glass": {"get"},
            "/api/v1/night-audit/runs": {"get"},
            "/api/v1/night-audit/runs/{run_id}": {"get"},
            "/api/v1/guests": {"get"},
            "/api/v1/guests/create": {"post"},
            "/api/v1/approvals": {"get"},
            "/api/v1/audit-timeline": {"get"},
            "/api/v1/audit/events": {"get"},
            "/api/v1/audit/events/{event_id}": {"get"},
            "/api/v1/audit/summary": {"get"},
            "/api/v1/audit/actions": {"get"},
            "/api/v1/staff": {"get"},
            "/api/v1/memberships": {"get"},
            "/api/v1/openapi.json": {"get"},
        }
        for path, methods in expected.items():
            self.assertIn(path, paths)
            for method in methods:
                self.assertIn(method, paths[path])
        self.assertNotIn("/api/v1/login", paths)
        self.assertNotIn("/api/v1/register", paths)

    def test_public_booking_contract_uses_booking_access_token_not_phone(self):
        spec = self.client.get("/api/v1/openapi.json").json()
        detail = spec["paths"]["/api/v1/reservations/{booking_no}"]["get"]
        parameter_names = {item["name"] for item in detail["parameters"]}
        self.assertIn("X-Booking-Token", parameter_names)
        self.assertIn("token", parameter_names)
        self.assertNotIn("phone", parameter_names)
        create = spec["paths"]["/api/v1/reservations"]["post"]
        self.assertIn("access_token", create["description"].lower())

    def test_payment_intent_rejects_non_post_methods(self):
        response = self.client.get("/api/v1/payments/intents")
        self.assertEqual(response.status_code, 405)

    def test_swagger_and_redoc_pages_are_available(self):
        swagger = self.client.get("/api/docs/")
        redoc = self.client.get("/api/redoc/")
        self.assertEqual(swagger.status_code, 200)
        self.assertContains(swagger, "SwaggerUIBundle")
        self.assertContains(swagger, "/api/v1/openapi.json")
        self.assertEqual(redoc.status_code, 200)
        self.assertContains(redoc, "redoc spec-url")


class PMS2OpenApiStructureValidityTests(TestCase):
    """契约文档结构合法性：OpenAPI 3.1 必备节点、operationId 唯一、$ref 全解析。"""

    def setUp(self):
        self.spec = build_openapi()

    def test_committed_openapi_snapshot_matches_runtime_contract(self):
        snapshot_path = (
            Path(settings.BASE_DIR) / "docs" / "接口文档" / "openapi.json")
        self.assertTrue(snapshot_path.exists(), "必须提交运行时 OpenAPI 快照")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["openapi"], self.spec["openapi"])
        self.assertEqual(snapshot["paths"], self.spec["paths"])
        self.assertEqual(snapshot["components"], self.spec["components"])
        self.assertEqual(snapshot["tags"], self.spec["tags"])

    def test_document_is_parseable_and_openapi_31(self):
        raw = json.dumps(self.spec, ensure_ascii=False)  # 可 JSON 序列化
        parsed = json.loads(raw)
        self.assertEqual(parsed["openapi"], "3.1.0")
        self.assertIn("title", parsed["info"])
        self.assertIn("version", parsed["info"])
        self.assertTrue(parsed["paths"])
        declared_tags = {t["name"] for t in parsed["tags"]}
        self.assertTrue(declared_tags)

    def test_every_operation_has_unique_operation_id_summary_tags_and_success_schema(self):
        seen_ids = set()
        declared_tags = {t["name"] for t in self.spec["tags"]}
        for path, method, operation in _iter_operations(self.spec):
            op_id = operation["operationId"]
            self.assertNotIn(op_id, seen_ids, f"operationId 重复：{op_id}")
            seen_ids.add(op_id)
            self.assertTrue(operation["summary"], f"{method.upper()} {path} 缺少 summary")
            self.assertTrue(operation["tags"], f"{op_id} 缺少 tags")
            for tag in operation["tags"]:
                self.assertIn(tag, declared_tags, f"{op_id} 使用了未声明的 tag：{tag}")
            success = [code for code in operation["responses"] if code.startswith("2")]
            self.assertTrue(success, f"{op_id} 缺少 2xx 成功响应")
            for code in success:
                schema = operation["responses"][code]["content"]["application/json"]["schema"]
                self.assertTrue(schema, f"{op_id} {code} 缺少响应 schema")

    def test_every_ref_resolves_to_declared_component(self):
        declared = set(self.spec["components"]["schemas"])

        def walk(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    name = node["$ref"].rsplit("/", 1)[-1]
                    self.assertIn(
                        name, declared,
                        f"未解析的 $ref：{node['$ref']}")
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.spec)

    def test_post_operations_have_request_body_or_explicit_no_body_marker(self):
        for path, method, operation in _iter_operations(self.spec):
            if method != "post":
                self.assertNotIn("requestBody", operation,
                                 f"GET {path} 不应声明 requestBody")
                continue
            has_body = "requestBody" in operation
            has_marker = operation.get("x-no-request-body") is True
            self.assertTrue(
                has_body or has_marker,
                f"POST {path} 既无 requestBody 也无 x-no-request-body 标记")

    def test_security_declarations_session_or_anonymous(self):
        for path, method, operation in _iter_operations(self.spec):
            security = operation.get("security")
            self.assertIsNotNone(security, f"{operation['operationId']} 缺少 security 声明")
            if security == []:
                self.assertNotIn("x-required-permission", operation,
                                 f"匿名端点 {path} 不应声明权限码")
            else:
                self.assertEqual(
                    security, [{"PMS2Session": []}],
                    f"{operation['operationId']} 只能使用 PMS2Session cookie")
                self.assertIn("x-required-permission", operation,
                              f"Session 端点 {path} 必须声明 x-required-permission")


class PMS2ContractStatusTests(TestCase):
    """x-contract-status 完整性：逐端点标注且非 planned 端点必须有具体 schema。"""

    def setUp(self):
        self.spec = build_openapi()

    def test_every_business_operation_has_contract_status(self):
        business = 0
        for path, method, operation in _iter_operations(self.spec):
            status = operation.get("x-contract-status")
            self.assertIn(
                status, CONTRACT_STATUSES,
                f"{method.upper()} {path} 缺少合法 x-contract-status")
            if path != BASE + "/openapi.json":
                business += 1
        # §4.3 客房服务中心 +4、§4.4 审计与合规中心 +4、§4.5 报表分析中心 +5、
        # §4.6 通知与待办中心 +5、基线 2.0 §3.1-§3.5 五大中心闭环补齐 +5
        # （guests timeline/folios、memberships status、billing shifts open/close）
        self.assertEqual(business, 103, "业务 operation 应为 103 个（排除 openapi.json 自身）")

    def test_no_operation_is_marked_planned(self):
        """全部 60 条路由均已实现，不允许出现 planned 端点（虚构计划外接口）。"""
        planned = [
            (path, method)
            for path, method, op in _iter_operations(self.spec)
            if op["x-contract-status"] == "planned"
        ]
        self.assertEqual(planned, [], f"存在 planned 端点：{planned}")

    def test_non_planned_operations_have_concrete_request_and_response_schemas(self):
        for path, method, operation in _iter_operations(self.spec):
            if operation["x-contract-status"] == "planned":
                continue
            success_codes = [c for c in operation["responses"] if c.startswith("2")]
            for code in success_codes:
                schema = operation["responses"][code]["content"]["application/json"]["schema"]
                self.assertTrue(
                    schema.get("type") or "$ref" in schema or "properties" in schema,
                    f"{operation['operationId']} {code} 响应 schema 为空")
            if method == "post" and "requestBody" in operation:
                body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
                self.assertEqual(body_schema.get("type"), "object",
                                 f"{operation['operationId']} 请求体必须是 JSON 对象")
            if method == "post" and operation.get("x-no-request-body") is not True:
                self.assertIn(
                    "requestBody", operation,
                    f"POST {path} 缺少 requestBody（视图读取请求体）")

    def test_verified_operations_exceed_partial(self):
        """verified 端点必须有明确的字段级 schema/参数描述（partial 为补测目标）。"""
        statuses = [op["x-contract-status"] for _, _, op in _iter_operations(self.spec)]
        self.assertGreaterEqual(statuses.count("verified"), 30)
        self.assertGreater(statuses.count("verified"), statuses.count("partial"))


class PMS2RouteContractSyncTests(TestCase):
    """schema 与 URLconf 双向一致：无遗漏路由、无幽灵路由。"""

    def setUp(self):
        self.spec = build_openapi()
        self.url_paths = _route_paths()

    def test_every_urlconf_route_has_an_operation(self):
        missing = sorted(self.url_paths - set(self.spec["paths"]))
        self.assertEqual(missing, [], f"以下路由未写入契约：{missing}")

    def test_no_ghost_routes_in_schema(self):
        ghosts = sorted(set(self.spec["paths"]) - self.url_paths)
        self.assertEqual(ghosts, [], f"schema 中存在未注册的幽灵路由：{ghosts}")

    def test_openapi_document_endpoint_resolves(self):
        match = resolve("/api/v1/openapi.json")
        # views.openapi_document 是 openapi_json 的兼容入口
        self.assertEqual(match.func.__module__, "apps.api.views")


class PMS2PublicBookingContractTests(TestCase):
    """公开预订三端点：请求字段与视图一致、幂等、限流、审计、Booking Access Token。"""

    def setUp(self):
        self.spec = build_openapi()

    def test_create_request_fields_match_view_payload(self):
        """POST /api/v1/reservations 请求字段必须与 views.reservations_create 一致。"""
        body = self.spec["paths"][BASE + "/reservations"]["post"]["requestBody"][
            "content"]["application/json"]["schema"]
        self.assertEqual(
            sorted(body["required"]),
            ["check_in", "check_out", "full_name", "phone", "room_type_id"])
        props = body["properties"]
        for name in ("full_name", "phone", "room_type_id", "rate_plan_id",
                     "check_in", "check_out", "rooms", "adults", "children"):
            self.assertIn(name, props, f"创建预订缺少视图实际读取的字段：{name}")
        self.assertEqual(props["rooms"]["minimum"], 1)
        self.assertEqual(props["rooms"]["maximum"], 10)
        self.assertEqual(props["adults"]["minimum"], 1)
        self.assertEqual(props["children"]["minimum"], 0)
        self.assertEqual(props["check_in"]["format"], "date")
        # 视图没有读取的其他字段不得虚构
        self.assertNotIn("email", props)
        self.assertNotIn("id_card", props)

    def test_create_declares_required_idempotency_key_and_replay_semantics(self):
        create = self.spec["paths"][BASE + "/reservations"]["post"]
        header = [p for p in create["parameters"] if p["name"] == "Idempotency-Key"]
        self.assertTrue(header, "创建预订必须声明 Idempotency-Key 头")
        self.assertEqual(header[0]["in"], "header")
        self.assertTrue(header[0]["required"],
                        "create_reservation 服务强校验幂等键，必须声明为必填")
        self.assertLessEqual(header[0]["schema"]["maxLength"], 128)
        self.assertIn("x-idempotency", create)
        replay = create["responses"]["200"]
        self.assertIn("幂等重放", replay["description"])

    def test_rate_limit_annotations_match_rate_limit_defaults(self):
        for path, action in RATE_LIMIT_ANNOTATIONS.items():
            method = "get" if path == "/reservations/{booking_no}" else "post"
            operation = self.spec["paths"][BASE + path][method]
            annotation = operation.get("x-rate-limit")
            self.assertIsNotNone(annotation, f"{path} 缺少 §6.3 限流标注")
            self.assertEqual(annotation["action"], action)
            self.assertTrue(annotation["fail_closed"])
            expected = {
                (frozenset(dims), limit, window)
                for dims, limit, window in DEFAULT_LIMITS[action]
            }
            actual = {
                (frozenset(rule["dimensions"]),
                 rule["limit"], rule["window_seconds"])
                for rule in annotation["rules"]
            }
            self.assertEqual(
                actual, expected,
                f"{path} 的限流标注与 apps.core.rate_limit.DEFAULT_LIMITS 不一致")

    def test_public_endpoints_declare_audit_events(self):
        create = self.spec["paths"][BASE + "/reservations"]["post"]
        self.assertIn("x-audit-events", create)
        self.assertIn("public.reservation_created", create["x-audit-events"])
        detail = self.spec["paths"][BASE + "/reservations/{booking_no}"]["get"]
        self.assertIn("guest.booking_access_viewed", detail["x-audit-events"])
        cancel = self.spec["paths"][BASE + "/reservations/{booking_no}/cancel"]["post"]
        self.assertIn("guest.booking_cancelled", cancel["x-audit-events"])

    def test_public_query_and_cancel_require_booking_access_token(self):
        detail = self.spec["paths"][BASE + "/reservations/{booking_no}"]["get"]
        self.assertIn("x-booking-access-token", detail)
        self.assertIn("token", detail["description"])
        cancel = self.spec["paths"][BASE + "/reservations/{booking_no}/cancel"]["post"]
        token_props = cancel["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertIn("token", token_props)
        self.assertEqual(
            cancel["responses"]["404"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PublicApiError")
        self.assertIn("x-booking-access-token", cancel)

    def test_mock_payment_endpoints_kept_local_only_and_signed(self):
        intents = self.spec["paths"][BASE + "/payments/intents"]["post"]
        self.assertIn("x-environment-gate", intents)
        self.assertIn("PERMISSION_DENIED", intents["responses"]["403"]["description"])
        callback = self.spec["paths"][BASE + "/payments/callback"]["post"]
        self.assertIn("x-signature", callback)
        signature_headers = [p for p in callback["parameters"]
                             if p["name"] == "X-Signature"]
        self.assertTrue(signature_headers)
        self.assertTrue(signature_headers[0]["required"])
        csrf_headers = [p for p in callback["parameters"] if p["name"] == "X-CSRFToken"]
        self.assertFalse(csrf_headers, "csrf_exempt 回调端点不应要求 CSRF")
        self.assertNotIn("x-rate-limit", callback)


class PMS2ErrorEnvelopeContractTests(TestCase):
    """错误结构统一：员工端 error{code,message} 封套；公开面字符串 error。"""

    def setUp(self):
        self.spec = build_openapi()

    def test_error_detail_requires_code_and_message(self):
        detail = self.spec["components"]["schemas"]["ErrorDetail"]
        self.assertIn("code", detail["required"])
        self.assertIn("message", detail["required"])
        envelope = self.spec["components"]["schemas"]["ApiErrorEnvelope"]
        self.assertEqual(envelope["properties"]["error"]["$ref"],
                         "#/components/schemas/ErrorDetail")

    def test_staff_error_responses_use_unified_envelope(self):
        for path, method, operation in _iter_operations(self.spec):
            if operation["security"] == []:
                continue
            for code, response in operation["responses"].items():
                if code.startswith("2"):
                    continue
                ref = response["content"]["application/json"]["schema"].get("$ref", "")
                self.assertEqual(
                    ref, "#/components/schemas/ApiErrorEnvelope",
                    f"{operation['operationId']} {code} 未使用统一错误封套")

    def test_public_429_uses_retry_after_schema(self):
        for path in ("/reservations", "/reservations/{booking_no}",
                     "/reservations/{booking_no}/cancel", "/payments/intents"):
            method = "get" if path.endswith("{booking_no}") and "cancel" not in path else "post"
            response = self.spec["paths"][BASE + path][method]["responses"]["429"]
            self.assertEqual(
                response["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/PublicRateLimitError",
                f"{path} 429 必须声明 retry_after 结构")
        schema = self.spec["components"]["schemas"]["PublicRateLimitError"]
        self.assertIn("retry_after", schema["properties"])

    def test_public_booking_error_is_string_error_structure(self):
        public_error = self.spec["components"]["schemas"]["PublicApiError"]
        self.assertEqual(public_error["properties"]["error"]["type"], "string")
        cancel = self.spec["paths"][BASE + "/reservations/{booking_no}/cancel"]["post"]
        self.assertEqual(
            cancel["responses"]["409"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PublicApiError")


class PMS2PermissionAnnotationTests(TestCase):
    """x-required-permission 与视图装饰器/AuthorizationService 校验一致。"""

    def setUp(self):
        self.spec = build_openapi()

    def test_permission_matrix_matches_views(self):
        for (method, path), permission in PERMISSION_MATRIX.items():
            operation = self.spec["paths"][BASE + path][method]
            declared = operation.get("x-required-permission")
            if permission is None:
                self.assertIsNone(
                    declared,
                    f"{method.upper()} {path} 视图未做专项权限码校验，契约不得虚构")
            else:
                self.assertIsNotNone(declared, f"{method.upper()} {path} 缺少权限声明")
                self.assertIn(
                    permission, declared,
                    f"{method.upper()} {path} 权限声明应包含 {permission}（视图实际校验值）")

    def test_shift_list_declares_both_personal_and_property_wide_access_paths(self):
        permission = self.spec["paths"][BASE + "/billing/shifts"]["get"]["x-required-permission"]
        self.assertIn("billing.cashier_shift_manage", permission)
        self.assertIn("hotelapp.finance_access", permission)

    def test_shift_close_contract_documents_supervisor_delegation(self):
        permission = self.spec["paths"][
            BASE + "/billing/shifts/{shift_id}/close"]["post"]["x-required-permission"]
        self.assertIn("billing.cashier_shift_manage", permission)
        self.assertIn("hotelapp.finance_access", permission)

    def test_session_post_operations_require_csrf_header(self):
        for path, method, operation in _iter_operations(self.spec):
            if method != "post" or path.endswith("/payments/callback"):
                continue
            csrf = [p for p in operation["parameters"] if p["name"] == "X-CSRFToken"]
            self.assertTrue(
                csrf, f"POST {path} 必须声明 X-CSRFToken（CSRF_USE_SESSIONS=True）")
            self.assertTrue(csrf[0]["required"])

    def test_break_glass_review_declares_strict_body_fields(self):
        review = self.spec["paths"][BASE + "/night-audit/break-glass/{record_id}/review"]["post"]
        body = review["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(sorted(body["required"]), ["decision", "reason"])
        self.assertIn("CONFIRMED", body["properties"]["decision"]["description"])
        execute = self.spec["paths"][
            BASE + "/night-audit/business-dates/{business_date_id}/break-glass/"
                   "{record_id}/execute"]["post"]
        execute_data = execute["responses"]["200"]["content"]["application/json"]["schema"]
        execute_component = execute_data["properties"]["data"]["$ref"].rsplit("/", 1)[-1]
        result = self.spec["components"]["schemas"][execute_component]
        self.assertTrue(result["properties"]["review_required"])


class PMS2IdempotencyContractTests(TestCase):
    """写端点幂等声明与 api_contract.idempotent / 服务侧强校验一致。"""

    def setUp(self):
        self.spec = build_openapi()

    def test_service_enforced_idempotency_endpoints_declare_required_key(self):
        for path in ("/front-desk/reservations/{booking_no}/payments",
                     "/front-desk/reservations/{booking_no}/deposits",
                     "/front-desk/payments/{payment_id}/refunds"):
            operation = self.spec["paths"][BASE + path]["post"]
            header = [p for p in operation["parameters"]
                      if p["name"] == "Idempotency-Key"]
            self.assertTrue(header, f"{path} 缺少 Idempotency-Key 声明")
            self.assertTrue(header[0]["required"],
                            f"{path} 服务层强校验幂等键，必须声明必填")
            self.assertIn("x-idempotency", operation)

    def test_api_contract_idempotent_endpoints_declare_optional_key(self):
        for path in ("/guests/create", "/guests/{guest_id}/update",
                     "/approvals/request", "/memberships/create",
                     "/memberships/{membership_id}/level",
                     "/daily-rates/{rate_id}/adjust"):
            operation = self.spec["paths"][BASE + path]["post"]
            header = [p for p in operation["parameters"]
                      if p["name"] == "Idempotency-Key"]
            self.assertTrue(header, f"{path} 缺少 Idempotency-Key 声明")
            self.assertFalse(header[0]["required"],
                             f"{path} 的 @idempotent 装饰器在缺少幂等键时放行，应声明可选")
            self.assertIn("x-idempotency", operation)


class PMS2FieldLevelConsistencyTests(TestCase):
    """抽样：组件 schema 字段与视图/服务实际返回键逐项一致（禁止虚构字段）。"""

    def setUp(self):
        self.spec = build_openapi()
        self.schemas = self.spec["components"]["schemas"]

    def test_staff_dto_matches_staff_center_dto(self):
        """StaffDto 必须覆盖 staff_center._staff_dto 的全部键（AUTH-14/17）。"""
        props = self.schemas["StaffDto"]["properties"]
        for name in ("id", "full_name", "phone_masked", "employee_no",
                     "position_code", "employment_status",
                     "employment_status_label", "membership_status",
                     "membership_status_label", "account", "invitation",
                     "active_roles", "assignments", "source_system"):
            self.assertIn(name, props, f"StaffDto 缺少视图实际返回字段：{name}")
        account = props["account"]["anyOf"][0]
        self.assertIn("username", account["properties"])
        self.assertIn("is_active", account["properties"])
        self.assertIn("password_pending", account["properties"],
                      "AUTH-14：unusable password 邀请待激活标记必须入契约")
        self.assertIn("last_login", account["properties"])
        invitation = props["invitation"]["anyOf"][0]
        for name in ("id", "status", "status_label", "expires_at", "resend_count"):
            self.assertIn(name, invitation["properties"],
                          f"AUTH-17 邀请摘要缺少字段：{name}")

    def test_folio_detail_data_declares_nullable_folio_and_summary(self):
        props = self.schemas["FolioDetailData"]["properties"]
        for name in ("folio", "summary", "entries", "payments", "allowed_actions"):
            self.assertIn(name, props, f"FolioDetailData 缺少视图实际返回键：{name}")
        self.assertEqual(
            props["summary"]["anyOf"][0]["$ref"],
            "#/components/schemas/FolioSummaryDto")

    def test_availability_query_params_match_view_reads(self):
        """availability 视图只读取 check_in/check_out/rooms/room_type_id。"""
        operation = self.spec["paths"][BASE + "/availability"]["get"]
        params = {p["name"]: p for p in operation["parameters"]}
        self.assertEqual(
            sorted(params), ["check_in", "check_out", "room_type_id", "rooms"],
            "availability 契约参数与视图实际读取不一致")
        self.assertTrue(params["check_in"]["required"])
        self.assertTrue(params["check_out"]["required"])
        self.assertFalse(params["rooms"]["required"])
        self.assertFalse(params["room_type_id"]["required"])
        self.assertEqual(params["rooms"]["schema"]["type"], "integer")
        self.assertEqual(params["check_in"]["schema"]["format"], "date")

    def test_night_audit_preview_query_and_run_body_match_view(self):
        preview = self.spec["paths"][BASE + "/night-audit/preview"]["get"]
        self.assertEqual(
            {p["name"] for p in preview["parameters"]}, {"date"},
            "夜审预览视图只读取 date 查询参数")
        run = self.spec["paths"][BASE + "/night-audit/run"]["post"]
        body = run["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(
            sorted(body["properties"]), ["date"],
            "夜审执行视图只读取 body.date")
        self.assertFalse(run["requestBody"]["required"],
                         "视图对空请求体兼容（json.loads('' or '{}')）")

    def test_error_detail_codes_cover_api_error_status_codes(self):
        from apps.core.api_contract import ApiError
        description = self.schemas["ErrorDetail"]["properties"]["code"]["description"]
        for code in ApiError.STATUS_CODES:
            self.assertIn(code, description,
                          f"ErrorDetail.code 说明缺少稳定错误码 {code}")


class PMS2ParameterConsistencyTests(TestCase):
    """路径模板变量、幂等键长度与业务域 tag 分组一致性。"""

    def setUp(self):
        self.spec = build_openapi()

    def test_path_template_variables_have_matching_path_parameters(self):
        for path, path_item in self.spec["paths"].items():
            for method, operation in path_item.items():
                declared = {p["name"] for p in operation.get("parameters", [])
                            if p["in"] == "path"}
                variables = set(re.findall(r"\{(\w+)\}", path))
                self.assertEqual(
                    variables, declared,
                    f"{method.upper()} {path} 路径变量 {variables} 与 "
                    f"path 参数声明 {declared} 不一致")

    def test_idempotency_key_max_length_matches_validation_site(self):
        """公开面 128（idempotent 装饰器截断）；前台收款/押金/退款 64（服务强校验）。"""
        create = self.spec["paths"][BASE + "/reservations"]["post"]
        header = next(p for p in create["parameters"]
                      if p["name"] == "Idempotency-Key")
        self.assertEqual(header["schema"]["maxLength"], 128)
        collect = self.spec["paths"][
            BASE + "/front-desk/reservations/{booking_no}/payments"]["post"]
        header = next(p for p in collect["parameters"]
                      if p["name"] == "Idempotency-Key")
        self.assertEqual(header["schema"]["maxLength"], 64)

    def test_tags_cover_required_business_domains(self):
        declared = {t["name"] for t in self.spec["tags"]}
        for domain in ("公开预订", "前台接待", "住客中心", "房态库存", "会员中心",
                       "员工中心", "审批", "夜审与紧急访问", "支付"):
            self.assertIn(domain, declared, f"缺少业务域分组 tag：{domain}")
