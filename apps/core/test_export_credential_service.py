"""受控导出凭证签发服务单测（2026-11-28 结构审查 P2 收口）。

历史实现里审计导出与报表导出各自复制一份「token 生成 + create」写块、TTL
常量也各自定义；收口后唯一写入口是 apps.core.services.issue_export_credential。
端点级行为（MC-008/MC-009：签发审计、过期 410、参数一致复核）由
apps.api.test_audit_governance 与 apps.api.test_report_export 覆盖，本模块只锁
服务本体的签发语义。
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.core.models import ExportCredential, Property
from apps.core.services import EXPORT_CREDENTIAL_TTL_SECONDS, issue_export_credential


class IssueExportCredentialServiceTests(TestCase):
    def setUp(self):
        self.property_obj = Property.objects.create(code="EXPCRED", name="凭证签发测试物业")
        self.user = None  # 匿名导出允许（created_by 为空）

    def test_ttl_constant_is_ruled_15_minutes(self):
        """MC-009 裁定口径：下载有效期 15 分钟，全仓唯一常量。"""
        self.assertEqual(EXPORT_CREDENTIAL_TTL_SECONDS, 15 * 60)

    def test_issues_credential_with_random_token_and_expiry(self):
        before = timezone.now()
        credential = issue_export_credential(
            property_obj=self.property_obj,
            kind=ExportCredential.Kind.REPORT,
            report_code="revenue_daily",
            params={"report": "revenue_daily", "from": "2026-11-01"},
            columns=["date", "revenue"],
            row_count=42,
            reason="月度经营汇报",
            created_by=self.user,
        )
        after = timezone.now()
        self.assertIsNotNone(credential.pk)
        self.assertGreaterEqual(len(credential.token), 40, "token_urlsafe(32) 应 ≥256bit 熵")
        self.assertEqual(
            ExportCredential.objects.filter(token=credential.token).count(), 1, "token 必须唯一落库"
        )
        expected_low = before + timedelta(seconds=EXPORT_CREDENTIAL_TTL_SECONDS)
        expected_high = after + timedelta(seconds=EXPORT_CREDENTIAL_TTL_SECONDS)
        self.assertLessEqual(expected_low, credential.expires_at)
        self.assertGreaterEqual(expected_high, credential.expires_at)
        self.assertEqual(credential.kind, ExportCredential.Kind.REPORT)
        self.assertEqual(credential.report_code, "revenue_daily")
        self.assertEqual(credential.row_count, 42)
        self.assertEqual(credential.columns, ["date", "revenue"])
        self.assertEqual(credential.reason, "月度经营汇报")
        self.assertIsNone(credential.created_by)

    def test_columns_input_copied_not_aliased(self):
        """columns 落库为副本：服务不得把调用方的可变列表原样别名存入。"""
        columns = ["col_a", "col_b"]
        credential = issue_export_credential(
            property_obj=self.property_obj,
            kind=ExportCredential.Kind.AUDIT,
            report_code="",
            params={},
            columns=columns,
            row_count=0,
            reason=get_random_string(12),
            created_by=self.user,
        )
        columns.append("col_c")
        credential.refresh_from_db()
        self.assertEqual(credential.columns, ["col_a", "col_b"])
