"""MC-008 审计治理端点回归：受控导出 / 留存状态 / 只读完整性核对。

复用 AuditCenterTestBase 的 DEFAULT 物业、审计查看岗位与事件工厂，
不另建审计事实或复制筛选口径。
"""

from apps.api.test_audit_center import AuditCenterTestBase
from apps.core.models import AuditEvent


class AuditGovernanceTests(AuditCenterTestBase):
    EXPORT_URL = "/api/v1/audit/export"

    def test_export_requires_reason(self):
        self._event()
        response = self.client.get(self.EXPORT_URL, {"reason": "太短"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_export_requires_audit_view_permission(self):
        self._event()
        self.client.force_login(self.fd)
        response = self.client.get(self.EXPORT_URL, {"reason": "季度合规复查取证"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_export_reuses_filters_and_writes_audit_without_pii_payload(self):
        self._event(action="billing.collect", entity_id="1")
        self._event(action="housekeeping.status_changed", entity_id="2")
        before = AuditEvent.objects.filter(property=self.property, action="audit.export").count()
        response = self.client.get(
            self.EXPORT_URL, {"reason": "季度合规复查取证", "action": "billing.collect"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8-sig")
        text = response.content.decode("utf-8-sig")
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("billing.collect", text)
        self.assertNotIn("housekeeping.status_changed", text)
        self.assertNotIn("before", lines[0])
        self.assertNotIn("after", lines[0])
        after = AuditEvent.objects.filter(property=self.property, action="audit.export").count()
        self.assertEqual(after, before + 1)
        record = AuditEvent.objects.filter(property=self.property, action="audit.export").latest(
            "id"
        )
        self.assertEqual(record.reason, "季度合规复查取证")
        self.assertEqual(record.after_json["row_count"], 1)
        self.assertEqual(record.after_json["filters"]["action"], "billing.collect")

    def test_retention_status_never_invents_years(self):
        response = self.client.get("/api/v1/audit/retention/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIsNone(data["retention_years"])
        self.assertIn("retention_note", data)
        self.assertIn("immutability", data)
        self.assertTrue(data["evidence_paths"])

    def test_integrity_check_is_post_only_read_only_and_has_no_fake_gaps(self):
        self._event()
        self.assertEqual(self.client.get("/api/v1/audit/integrity/check").status_code, 405)
        response = self.client.post("/api/v1/audit/integrity/check")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total_events"], 1)
        self.assertEqual(data["checked_days"], 1)
        self.assertEqual(len(data["digest_sha256"]), 64)
        self.assertEqual(data["gaps"], [])
