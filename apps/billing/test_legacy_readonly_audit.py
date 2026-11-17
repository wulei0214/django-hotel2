"""静态守卫生产财务只读盘点 SQL 的安全边界与关键列完整性。"""

import re
from pathlib import Path

from django.test import SimpleTestCase

SQL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "legacy_finance_readonly_audit.sql"
SQL_TEXT = SQL_PATH.read_text(encoding="utf-8")


class LegacyFinanceReadonlyAuditScriptTests(SimpleTestCase):
    def test_data_statements_are_select_or_transaction_controls(self) -> None:
        sql_without_comments = re.sub(r"(?m)^[ \t]*--.*$", "", SQL_TEXT)
        statements = [part.strip() for part in sql_without_comments.split(";") if part.strip()]
        self.assertTrue(statements)
        heads = {statement.split(None, 1)[0].upper() for statement in statements}
        self.assertEqual(heads, {"START", "SELECT", "COMMIT"})
        self.assertIn("START TRANSACTION READ ONLY", sql_without_comments.upper())

    def test_preflight_covers_every_referenced_schema_column(self) -> None:
        match = re.search(
            r"AND \(TABLE_NAME, COLUMN_NAME\) IN\s*\((.*?)\)\s*ORDER BY",
            SQL_TEXT,
            re.DOTALL,
        )
        if match is None:
            self.fail("未找到 information_schema 前置列清单")
        actual = set(re.findall(r"\('([^']+)',\s*'([^']+)'\)", match.group(1)))
        expected = {
            ("finance_record", "id"),
            ("finance_record", "金额"),
            ("finance_record", "日期"),
            ("finance_record", "类型"),
            ("finance_record", "关联订单"),
            ("folio", "id"),
            ("folio", "金额"),
            ("folio", "日期"),
            ("folio", "类型"),
            ("folio", "order_id"),
            ("accommodation", "id"),
            ("accommodation", "状态"),
            ("accommodation", "客房编号"),
            ("accommodation", "收款金额"),
            ("accommodation", "押金"),
            ("night_audit", "营业日期"),
            ("room", "房间号"),
            ("inventory_room", "number"),
            ("inventory_room", "property_id"),
            ("core_property", "id"),
            ("core_property", "code"),
            ("billing_folio", "id"),
            ("billing_folio", "property_id"),
            ("billing_folio", "reservation_id"),
            ("billing_ledgerentry", "id"),
            ("billing_ledgerentry", "entry_type"),
            ("billing_ledgerentry", "amount"),
            ("billing_ledgerentry", "business_date"),
            ("billing_ledgerentry", "folio_id"),
            ("billing_ledgerentry", "property_id"),
            ("billing_ledgerentry", "reference"),
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 31)
        self.assertIn("期望：31 行全部返回", SQL_TEXT)

    def test_property_attribution_has_six_explicit_states(self) -> None:
        match = re.search(
            r"SELECT\s+a\.\x60id\x60[\s\S]*?CASE(?P<body>.*?)END\s+AS\s+attribution_verdict",
            SQL_TEXT,
            re.DOTALL,
        )
        if match is None:
            self.fail("未找到 attribution_verdict CASE")
        labels = re.findall(r"(?:THEN|ELSE)\s+'([^']+)'", match.group("body"))
        self.assertEqual(
            set(labels),
            {
                "旧订单未关联旧房间（FK 为空）",
                "旧订单房间引用断裂（FK 目标不存在）",
                "证据不足（新侧无匹配房间）",
                "唯一匹配 DEFAULT（候选证据，须人工确认）",
                "唯一匹配非 DEFAULT（其他物业证据）",
                "同号多物业冲突（需人工核验）",
            },
        )
        self.assertEqual(len(labels), 6)
