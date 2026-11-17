"""MC-010 夜审阻挡清单只读端点回归。

唯一阻挡判定来自 NightAuditService.preview；/night-audit/blockers 只做
字段投影，不复制规则，也不提供任何 schedule/trigger HTTP 入口。
"""

from apps.api.test_e2e_flows import E2EFlowTestBase


class NightAuditBlockersTests(E2EFlowTestBase):
    URL = "/api/v1/night-audit/blockers"

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("na_blockers_frontdesk", "前台")
        self.dm = self._make_staff("na_blockers_duty", "值班经理")
        self.client.force_login(self.dm)

    def test_requires_authentication_and_night_audit_view(self):
        self.client.logout()
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")
        self.client.force_login(self.fd)
        denied = self.client.get(self.URL)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "PERMISSION_DENIED")

    def test_projects_preview_blockers_without_new_rules(self):
        preview = self.client.get("/api/v1/night-audit/preview").json()["data"]
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["business_date"], preview["business_date"])
        self.assertEqual(data["blockers"], preview["blockers"])
        self.assertEqual(data["blocker_count"], preview["blocker_count"])
        self.assertEqual(data["can_run"], preview["can_run"])
        self.assertEqual(data["source"], "NightAuditService.preview")
        self.assertIn("/night-audit/run", data["schedule_note"])

    def test_date_parameter_matches_preview_scope(self):
        day = self.today.isoformat()
        preview = self.client.get("/api/v1/night-audit/preview", {"date": day}).json()["data"]
        blockers = self.client.get(self.URL, {"date": day}).json()["data"]
        self.assertEqual(blockers["business_date"], preview["business_date"])

    def test_no_public_schedule_or_trigger_routes(self):
        for method, url in (
            ("get", "/api/v1/night-audit/schedule"),
            ("post", "/api/v1/night-audit/trigger"),
        ):
            response = getattr(self.client, method)(url)
            self.assertEqual(response.status_code, 404, url)
