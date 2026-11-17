"""MC-009 报表受控导出回归：沿用报表门禁与口径，原因必填并写审计。

导出不得成为绕过报表权限或重写聚合逻辑的第二条读取路径；
本测试逐项比对导出 CSV 与对应查询接口的 series。
"""

from datetime import timedelta

from django.utils import timezone

from apps.api.test_report_center import ReportCenterTestBase
from apps.core.models import AuditEvent, ExportCredential


class ReportExportTests(ReportCenterTestBase):
    URL = "/api/v1/reports/export"
    REASON = "管理层周报数据核对"

    def test_export_requires_reason_and_known_report(self):
        response = self.client.get(self.URL, {"report": "occupancy"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        unknown = self.client.get(self.URL, {"report": "unknown", "reason": self.REASON})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["error"]["code"], "VALIDATION_ERROR")

    def test_export_keeps_report_permission_boundary(self):
        # 前台无 finance_access：收入类导出与收入类查询同样被拒绝。
        self.client.force_login(self.fd)
        denied = self.client.get(self.URL, {"report": "adr", "reason": self.REASON})
        self.assertEqual(denied.status_code, 403)
        # 客房服务员无 view_accommodation：运营类导出同样被拒绝。
        self.client.force_login(self.hk)
        denied = self.client.get(self.URL, {"report": "occupancy", "reason": self.REASON})
        self.assertEqual(denied.status_code, 403)

    def test_export_matches_query_series_and_writes_audit(self):
        day_from = self.today - timedelta(days=45)
        self._make_inventory(day_from, total=5, sold=2)
        params = {
            "report": "occupancy",
            "reason": self.REASON,
            "from": day_from.isoformat(),
            "to": self.today.isoformat(),
        }
        query = self.client.get("/api/v1/reports/occupancy", params)
        self.assertEqual(query.status_code, 200)
        expected = query.json()["data"]["series"]
        self.assertTrue(expected)

        before = AuditEvent.objects.filter(property=self.property, action="report.export").count()
        response = self.client.get(self.URL, params)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        lines = response.content.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(lines) - 1, len(expected))
        self.assertIn("occupancy_pct", lines[0])
        # 每次导出签发带下载有效期的受控凭证。
        credential = ExportCredential.objects.get(token=response["X-Export-Credential"])
        self.assertEqual(credential.kind, ExportCredential.Kind.REPORT)
        self.assertEqual(credential.report_code, "occupancy")
        self.assertEqual(credential.row_count, len(expected))
        self.assertGreater(credential.expires_at, timezone.now())
        after = AuditEvent.objects.filter(property=self.property, action="report.export").count()
        self.assertEqual(after, before + 1)
        record = AuditEvent.objects.filter(property=self.property, action="report.export").latest(
            "id"
        )
        self.assertEqual(record.reason, self.REASON)
        self.assertEqual(record.after_json["report"], "occupancy")
        self.assertEqual(record.after_json["row_count"], len(expected))
        self.assertEqual(record.after_json["metadata"]["property"]["code"], self.property.code)

    def test_credential_download_reuses_report_scope_and_audits(self):
        day_from = self.today - timedelta(days=45)
        self._make_inventory(day_from, total=5, sold=2)
        params = {
            "report": "occupancy",
            "reason": self.REASON,
            "from": day_from.isoformat(),
            "to": self.today.isoformat(),
        }
        issued = self.client.get(self.URL, params)
        token = issued["X-Export-Credential"]
        expected = self.client.get("/api/v1/reports/occupancy", params).json()["data"]["series"]
        before = AuditEvent.objects.filter(
            property=self.property, action="report.export_download"
        ).count()
        response = self.client.get(
            f"{self.URL}/{token}",
            {"report": "occupancy", "from": params["from"], "to": params["to"]},
        )
        self.assertEqual(response.status_code, 200)
        lines = response.content.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(lines) - 1, len(expected))
        after = AuditEvent.objects.filter(
            property=self.property, action="report.export_download"
        ).count()
        self.assertEqual(after, before + 1)

    def test_download_still_enforces_report_permission(self):
        # 签发凭证后，无权限账号仍不能凭 token 下载（下载走同一报表视图门禁）。
        day = self.today - timedelta(days=3)
        self._make_inventory(day, total=5, sold=2)
        issued = self.client.get(
            self.URL,
            {
                "report": "occupancy",
                "reason": self.REASON,
                "from": day.isoformat(),
                "to": day.isoformat(),
            },
        )
        self.assertEqual(issued.status_code, 200)
        token = issued["X-Export-Credential"]
        self.client.force_login(self.hk)
        denied = self.client.get(
            f"{self.URL}/{token}",
            {"report": "occupancy", "from": day.isoformat(), "to": day.isoformat()},
        )
        self.assertEqual(denied.status_code, 403)

    def test_expired_credential_download_is_rejected(self):
        day = self.today - timedelta(days=4)
        self._make_inventory(day, total=5, sold=1)
        issued = self.client.get(
            self.URL,
            {
                "report": "occupancy",
                "reason": self.REASON,
                "from": day.isoformat(),
                "to": day.isoformat(),
            },
        )
        token = issued["X-Export-Credential"]
        ExportCredential.objects.filter(token=token).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        response = self.client.get(
            f"{self.URL}/{token}",
            {"report": "occupancy", "from": day.isoformat(), "to": day.isoformat()},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "BUSINESS_CONFLICT")

    def test_unknown_credential_token_returns_404(self):
        response = self.client.get(f"{self.URL}/not-a-real-token", {"report": "occupancy"})
        self.assertEqual(response.status_code, 404)

    def test_memberships_snapshot_export_uses_same_series_as_page(self):
        guest = self._make_guest()
        self._make_membership(guest, level_code="STANDARD")
        query = self.client.get("/api/v1/reports/memberships").json()["data"]
        self.assertEqual(query["series"], query["by_level"])
        response = self.client.get(self.URL, {"report": "memberships", "reason": self.REASON})
        self.assertEqual(response.status_code, 200)
        lines = response.content.decode("utf-8-sig").strip().splitlines()
        self.assertIn("level_code", lines[0])
        self.assertEqual(len(lines) - 1, len(query["series"]))


class ReportExportPageTests(ReportCenterTestBase):
    """页面入口：会员/渠道快照卡 + 受控导出表单，服务端仍逐次复核权限。"""

    def test_page_exposes_snapshot_cards_and_controlled_export(self):
        self.client.force_login(self.dm)
        response = self.client.get("/manage/reports/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "memberships:")
        self.assertContains(response, "sources:")
        self.assertContains(response, "快照型报表")
        self.assertContains(response, "rp-export-btn")
        self.assertContains(response, "/api/v1/reports/export")
        self.assertContains(response, "用途说明")
