"""PMS2 工作台模板回归守卫。

这些测试用于捕获 API 单测难以发现的模板/JavaScript 回归：
- CSRF_USE_SESSIONS 场景下，所有含写操作的中心模板必须输出服务端 CSRF token；
- 不允许从 document.cookie 读取 CSRF（CSRF secret 存储于 Session）；
- 财务动态表单必须在表单仍连接 DOM 时读取字段；
- 夜审 DOM helper 必须支持 Node 子节点，防止表格单元格被转成 [object ...]。
"""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

TEMPLATE_DIR = Path(settings.BASE_DIR) / "templates" / "pms"


class CenterTemplateRegressionTests(SimpleTestCase):
    def read_template(self, name):
        return (TEMPLATE_DIR / name).read_text(encoding="utf-8")

    def test_write_centers_render_session_backed_csrf_token(self):
        self.assertTrue(settings.CSRF_USE_SESSIONS)
        for name in (
            "billing_center.html",
            "housekeeping_center.html",
            "night_audit_center.html",
            "notification_center.html",
            "management_center.html",
        ):
            with self.subTest(template=name):
                source = self.read_template(name)
                self.assertIn("{% csrf_token %}", source)
                self.assertIn('name="csrfmiddlewaretoken"', source)
                self.assertIn("X-CSRFToken", source)
                self.assertIn("CSRF", source)
                self.assertNotIn("document.cookie", source)

    def test_billing_forms_read_values_before_any_dom_replacement(self):
        source = self.read_template("billing_center.html")
        for field in ("amount", "method", "reference", "reason"):
            self.assertIn(f"form.querySelector('[name=\"{field}\"]')", source)
        self.assertNotIn('document.getElementById("fin-collect-amount")', source)
        self.assertNotIn('document.getElementById("fin-refund-amount")', source)
        self.assertNotIn('document.getElementById("fin-reverse-reason")', source)

    def test_billing_write_forms_keep_idempotency_keys_across_retries(self):
        source = self.read_template("billing_center.html")
        self.assertIn('headers["Idempotency-Key"] = idempotencyKey', source)
        self.assertIn("generatedReference", source)
        self.assertIn("idempotency_key: key", source)
        self.assertIn("requestKey ||", source)
        self.assertIn("executionKey ||", source)
        self.assertIn("movementKey ||", source)
        self.assertIn("approveKey", source)
        self.assertIn("rejectKey ||", source)
        self.assertIn("openShiftKey ||", source)
        self.assertIn("closeShiftKey ||", source)

    def test_billing_center_exposes_shift_controls_and_separate_refund_actions(self):
        source = self.read_template("billing_center.html")
        self.assertNotIn('href="/finance/"', source)
        self.assertIn('id="fin-shift-open"', source)
        self.assertIn('id="fin-shift-close"', source)
        self.assertIn('id="fin-shift-movement-form"', source)
        self.assertIn("/api/v1/billing/shifts/open", source)
        self.assertIn('/api/v1/billing/shifts/" + activeShiftId + "/movements', source)
        self.assertIn('/api/v1/billing/refunds/" + refund.id + "/approve', source)
        self.assertIn('/api/v1/billing/refunds/" + refund.id + "/execute', source)
        self.assertIn("canApproveRefund", source)
        self.assertIn("canExecuteRefund", source)

    def test_billing_center_exposes_refund_liabilities_and_manual_source_selection(self):
        source = self.read_template("billing_center.html")
        self.assertIn('aria-label="待退款负债"', source)
        self.assertIn('id="fin-refund-liability-body"', source)
        self.assertIn("/api/v1/billing/refund-liabilities?page=1&page_size=100", source)
        self.assertIn("function refundForm(payment, refundLiabilityId)", source)
        self.assertIn("requestBody.refund_liability_id = refundLiabilityId", source)
        self.assertIn(
            "openLiabilityRefund(item, Object.assign({id: selected.payment_id}, selected))", source
        )
        self.assertIn("候选支付仅供财务核对，不代表自动分配", source)

    def test_billing_refund_ui_requires_non_cash_reference_and_marks_legacy(self):
        source = self.read_template("billing_center.html")
        self.assertIn('payment.method !== "CASH"', source)
        self.assertIn("executionReference.trim()", source)
        self.assertIn("refund.legacy_workflow", source)
        self.assertIn("无法证明申请、审批、执行三方分离", source)

    def test_night_audit_dom_helper_appends_node_children(self):
        source = self.read_template("night_audit_center.html")
        helper = source.split("function el(", 1)[1].split("\n  function value", 1)[0]
        self.assertIn("text instanceof Node", helper)
        self.assertIn("node.appendChild(text)", helper)
        self.assertIn("node.textContent = String(text)", helper)

    def test_management_center_resolves_page_action_path_placeholders_before_fetch(self):
        """D-R4-04：submitAction 必须先按表单字段解析 endpoint 路径占位符
        （{rate_id}/{inventory_id} 等）再 fetch。page 级渲染路径没有选中行
        上下文，此前直接 fetch endpoint 原文 → 字面 {rate_id} 路径 404，
        前端统一显示"网络错误"误导排障（房态中心三个写操作不可用）。"""
        source = self.read_template("management_center.html")
        self.assertIn("function resolveEndpoint(", source)
        self.assertIn("const requestUrl = resolveEndpoint(endpoint, payload);", source)
        self.assertIn("fetch(requestUrl, {", source)
        # 未解析成功的占位符不得打到服务端（给出明确提示而非 404 网络错误）
        self.assertIn("/\\{\\w+\\}/.test(requestUrl)", source)
        self.assertIn("缺少定位操作行所需的 ID 字段", source)

    def test_housekeeping_task_pool_pending_row_offers_start_action(self):
        """MC11-HK-10（任务书 TASK-01）：清洁任务池 PENDING 任务行必须有
        「开始」按钮（复用已有 start API），否则客房服务员无法从正式页面
        开始新任务，只能走 API 路径完成测试。已指派给他人的 PENDING 任务
        不显示「开始」（服务端守卫为幂等兜底，前端不发起必然 409 的请求）。"""
        source = self.read_template("housekeeping_center.html")
        # PENDING 分支内的 start 按钮（认领/派工之外，服务层 PENDING→IN_PROGRESS）
        self.assertIn('"开始", "", "start"', source)
        self.assertIn('"认领", "", "claim"', source)
        # 显示守卫：只有未指派或指派给本人的 PENDING 任务才出现开始按钮
        self.assertIn("!task.assigned_to || task.assigned_to === currentUserId", source)
        # REJECTED 的「重新开始」入口必须保留（状态机对齐：REJECTED→start→IN_PROGRESS）
        self.assertIn('"重新开始", "", "start"', source)
