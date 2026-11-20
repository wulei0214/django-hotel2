"""财务入口收敛回归：旧 /finance/ 只能兼容跳转，不再渲染旧账务页。"""

from pathlib import Path

from django.conf import settings
from django.test import TestCase


class LegacyFinanceRouteTests(TestCase):
    def test_legacy_finance_page_redirects_to_pms2_finance_center(self):
        response = self.client.get("/finance/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/finance/")

    def test_dead_finance_manage_view_and_template_are_removed(self):
        """TASK-05：未挂载的 finance_manage/_finance_context 与死模板不得复活。"""
        from hotelapp import views

        self.assertFalse(hasattr(views, "finance_manage"))
        self.assertFalse(hasattr(views, "_finance_context"))
        self.assertFalse((Path(settings.BASE_DIR) / "templates" / "finance.html").exists())
