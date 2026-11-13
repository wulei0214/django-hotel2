"""P-08 IdempotencyRecord 清理：扫描优先 / dry-run / 审计 / 不误删。

只断言清理服务的契约行为：dry-run 不删、apply 只删已过期且超出保留缓冲的记录、
按状态区分、绝不删除仍处 TTL 内或无过期时间的记录；命令默认 dry-run。
"""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import IdempotencyRecord
from apps.core.services import cleanup_idempotency_records


def _make(key, status, expires_delta, property_obj=None):
    now = timezone.now()
    expires = None if expires_delta is None else now + expires_delta
    return IdempotencyRecord.objects.create(
        property=property_obj,
        key=key, method="POST", path="/api/v1/x", request_hash="h-" + key,
        status=status, expires_at=expires)


class IdempotencyCleanupServiceTests(TestCase):
    def test_dry_run_does_not_delete_anything(self):
        _make("a", IdempotencyRecord.RecordStatus.COMPLETED, -timedelta(days=2))
        _make("b", IdempotencyRecord.RecordStatus.FAILED, -timedelta(days=2))
        _make("c", IdempotencyRecord.RecordStatus.COMPLETED, timedelta(days=1))  # 仍处 TTL
        _make("d", IdempotencyRecord.RecordStatus.COMPLETED, None)               # 无过期时间
        summary = cleanup_idempotency_records(dry_run=True)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["would_delete"], 2)  # 仅 a/b 过期
        self.assertEqual(IdempotencyRecord.objects.count(), 4)  # 全部保留，不误删

    def test_apply_deletes_only_expired_beyond_retention(self):
        _make("a", IdempotencyRecord.RecordStatus.COMPLETED, -timedelta(days=2))
        _make("b", IdempotencyRecord.RecordStatus.FAILED, -timedelta(days=2))
        _make("c", IdempotencyRecord.RecordStatus.PROCESSING, -timedelta(days=2))
        _make("d", IdempotencyRecord.RecordStatus.COMPLETED, timedelta(days=1))  # TTL 内
        _make("e", IdempotencyRecord.RecordStatus.COMPLETED, None)               # 无过期时间
        summary = cleanup_idempotency_records(dry_run=False)
        self.assertEqual(summary["deleted"], 3)  # a/b/c 过期已删
        self.assertEqual(IdempotencyRecord.objects.count(), 2)  # d/e 保留
        self.assertTrue(IdempotencyRecord.objects.filter(key="d").exists())
        self.assertTrue(IdempotencyRecord.objects.filter(key="e").exists())

    def test_distinguishes_by_status_and_reports_null_expiry(self):
        _make("a", IdempotencyRecord.RecordStatus.COMPLETED, -timedelta(days=2))
        _make("b", IdempotencyRecord.RecordStatus.COMPLETED, -timedelta(days=2))
        _make("c", IdempotencyRecord.RecordStatus.FAILED, -timedelta(days=2))
        _make("d", IdempotencyRecord.RecordStatus.COMPLETED, None)
        summary = cleanup_idempotency_records(dry_run=True)
        self.assertEqual(
            summary["by_status"].get(IdempotencyRecord.RecordStatus.COMPLETED), 2)
        self.assertEqual(
            summary["by_status"].get(IdempotencyRecord.RecordStatus.FAILED), 1)
        self.assertEqual(summary["null_expiry_count"], 1)

    def test_older_than_days_filters_retention_window(self):
        _make("a", IdempotencyRecord.RecordStatus.COMPLETED, -timedelta(days=2))
        # 保留窗口 3 天：过期仅 2 天，未超出 → 不删
        self.assertEqual(
            cleanup_idempotency_records(older_than_days=3, dry_run=True)["would_delete"], 0)
        # 保留窗口 1 天：过期 2 天，已超出 → 候选 1
        self.assertEqual(
            cleanup_idempotency_records(older_than_days=1, dry_run=True)["would_delete"], 1)


class IdempotencyCleanupCommandTests(TestCase):
    def test_command_default_is_dry_run(self):
        _make("a", IdempotencyRecord.RecordStatus.COMPLETED, -timedelta(days=2))
        out = StringIO()
        call_command("cleanup_idempotency_records", stdout=out)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertEqual(IdempotencyRecord.objects.count(), 1)  # 默认不删

    def test_command_apply_deletes(self):
        _make("a", IdempotencyRecord.RecordStatus.COMPLETED, -timedelta(days=2))
        _make("b", IdempotencyRecord.RecordStatus.COMPLETED, timedelta(days=1))
        out = StringIO()
        call_command("cleanup_idempotency_records", "--apply", stdout=out)
        self.assertIn("清理完成", out.getvalue())
        self.assertEqual(IdempotencyRecord.objects.count(), 1)
        self.assertTrue(IdempotencyRecord.objects.filter(key="b").exists())
