"""后台菜单与 PMS2 业务快照的可见性回归（前台写入 → 后台可见）。"""

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase


class AdminBridgeTests(TestCase):
    def test_legacy_hotelapp_group_is_hidden(self):
        names = [item["name"] for item in settings.SIMPLEUI_CONFIG["menus"]]
        self.assertNotIn("酒店业务模块（旧系统存档）", names)

    def test_pms_business_overview_is_readable_by_admin(self):
        user = User.objects.create_superuser(
            "admin_bridge", "bridge@example.com", "BridgePass2026!"
        )
        self.client.force_login(user)
        self.client.cookies["django_admin_sessionid"] = self.client.cookies["sessionid"].value
        response = self.client.get("/admin/pms-overview/", HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PMS2 业务概览")
        self.assertContains(response, "最近业务动作")
