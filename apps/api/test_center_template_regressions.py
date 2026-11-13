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
            self.assertIn("form.querySelector('[name=\"%s\"]')" % field, source)
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
        self.assertIn('/api/v1/billing/shifts/open', source)
        self.assertIn('/api/v1/billing/shifts/" + activeShiftId + "/movements', source)
        self.assertIn('/api/v1/billing/refunds/" + refund.id + "/approve', source)
        self.assertIn('/api/v1/billing/refunds/" + refund.id + "/execute', source)
        self.assertIn("canApproveRefund", source)
        self.assertIn("canExecuteRefund", source)

    def test_billing_center_exposes_refund_liabilities_and_manual_source_selection(self):
        source = self.read_template("billing_center.html")
        self.assertIn('aria-label="待退款负债"', source)
        self.assertIn('id="fin-refund-liability-body"', source)
        self.assertIn('/api/v1/billing/refund-liabilities?page=1&page_size=100', source)
        self.assertIn('function refundForm(payment, refundLiabilityId)', source)
        self.assertIn('requestBody.refund_liability_id = refundLiabilityId', source)
        self.assertIn('openLiabilityRefund(item, Object.assign({id: selected.payment_id}, selected))', source)
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
