# -*- coding: utf-8 -*-
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

    def test_night_audit_dom_helper_appends_node_children(self):
        source = self.read_template("night_audit_center.html")
        helper = source.split("function el(", 1)[1].split("\n  function value", 1)[0]
        self.assertIn("text instanceof Node", helper)
        self.assertIn("node.appendChild(text)", helper)
        self.assertIn("node.textContent = String(text)", helper)
