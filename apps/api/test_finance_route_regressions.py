# -*- coding: utf-8 -*-
"""财务入口收敛回归：旧 /finance/ 只能兼容跳转，不再渲染旧账务页。"""
from django.test import SimpleTestCase


class LegacyFinanceRouteTests(SimpleTestCase):
    def test_legacy_finance_page_redirects_to_pms2_finance_center(self):
        response = self.client.get("/finance/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/finance/")

