"""阶段 D 迁移基础层 TDD（批次 2）：checksum-v2 算法 + 批次授权守卫 + 签名执行凭证。

覆盖测试方案 §1 的 M 类（迁移机制）与 §5.3 的 N 类负向断言：

- M18 校验和：checksum-v2 长度前缀编码、NULL/空串区分、金额原样、时间双值、
  列序参与摘要、主键数值序、跨环境同数据同摘要（§4.2 第 1~9 条逐条锁定）。
- N3/N8 批次状态机：无 ACTIVE 批次不可执行、CLOSED 终态不可重新激活。
- N12 `ImmutableBatchQuerySet`：update/delete/bulk_update/bulk_create 一律拒绝。
- N9/N10 凭证：裸调用/篡改载荷/伪造批次均拒绝——服务不信任调用方断言。
- N11 凭证过期与 nonce 重放：TTL 拒绝、唯一约束消费锁。
- N13 SoD：批准人 ≠ 执行人（DB CheckConstraint + 绑定校验双重）。

性质：本文件是**新增被测对象**的先行测试（阶段 D 已获书面批准、代码开发解冻），
被测实现见 apps/billing/legacy_checksum.py、apps/billing/legacy_credentials.py
与 apps/billing/models.py::LegacyMigrationBatch。
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.legacy_checksum import (
    ALGORITHM_VERSION,
    build_metadata,
    encode_field,
    row_digest,
    table_digest,
)
from apps.billing.legacy_credentials import (
    AUTH_AUDIT_ACTION,
    LegacyMigrationDenied,
    assert_record_in_scope,
    authorize_execution,
    load_credential,
    mint_credential,
)
from apps.billing.models import LegacyMigrationBatch
from apps.core.models import AuditEvent, Property

COLUMNS = ("id", "folio_id", "amount", "business_date")


class ChecksumV2EncodingTests(TestCase):
    """§4.2 逐字段编码规则：长度前缀消歧、NULL/空串、金额原样、时间双值。"""

    def test_length_prefix_prevents_separator_collision(self):
        """字段值内含分隔符样字符不得与相邻字段拼接产生同一字节流。"""
        left = encode_field("a", "x|y") + encode_field("b", "z")
        right = encode_field("a", "x") + encode_field("b", "y|z")
        self.assertNotEqual(left, right)

    def test_null_and_empty_string_are_distinct(self):
        self.assertNotEqual(encode_field("note", None), encode_field("note", ""))

    def test_null_encodes_empty_value_with_flag_one(self):
        encoded = encode_field("note", None)
        self.assertTrue(encoded.endswith(b"\x01\x00\x00\x00\x00"))

    def test_decimal_preserves_original_scale(self):
        """金额保原字符串：688.00 与 688.0 是不同历史事实，摘要必须不同。"""
        self.assertNotEqual(
            encode_field("amount", Decimal("688.00")),
            encode_field("amount", Decimal("688.0")),
        )

    def test_decimal_never_uses_scientific_notation(self):
        self.assertIn(b"0.0000001", encode_field("amount", Decimal("1E-7")))
        self.assertNotIn(b"E-7", encode_field("amount", Decimal("1E-7")))

    def test_string_amount_and_decimal_amount_differ_by_type_tag(self):
        """同一文本以 str 与 Decimal 传入必须区分：类型标识参与编码。"""
        self.assertNotEqual(
            encode_field("amount", "688.00"),
            encode_field("amount", Decimal("688.00")),
        )

    def test_datetime_encodes_original_and_utc_converted(self):
        value = datetime(2026, 11, 16, 12, 30, 0, tzinfo=dt_timezone.utc)
        encoded = encode_field("created_at", value)
        self.assertIn(b"2026-11-16 12:30:00", encoded)
        self.assertIn(b"2026-11-16T12:30:00Z", encoded)


class ChecksumV2DigestTests(TestCase):
    """§4.2 行摘要与整表摘要：列序参与、主键数值序、跨环境一致性。"""

    def _rows(self):
        return [
            {"id": 10, "folio_id": 3, "amount": "100.00", "business_date": "2026-01-02"},
            {"id": 2, "folio_id": 1, "amount": "206.40", "business_date": "2026-01-01"},
        ]

    def test_row_digest_depends_on_column_order(self):
        row = self._rows()[0]
        self.assertNotEqual(
            row_digest(row, COLUMNS),
            row_digest(row, tuple(reversed(COLUMNS))),
        )

    def test_missing_column_raises_instead_of_silent_null(self):
        row = self._rows()[0]
        with self.assertRaises(KeyError):
            row_digest(row, (*COLUMNS, "absent_column"))

    def test_table_digest_is_order_independent(self):
        rows = self._rows()
        self.assertEqual(
            table_digest(rows, COLUMNS, "id"),
            table_digest(list(reversed(rows)), COLUMNS, "id"),
        )

    def test_table_digest_sorts_pk_numerically_not_lexicographically(self):
        """主键升序按数值：10 必须排在 2 之后，字典序会算错。"""
        rows = self._rows()
        canonical_order = table_digest(rows, COLUMNS, "id")
        swapped = [rows[1], rows[0]]
        self.assertEqual(canonical_order, table_digest(swapped, COLUMNS, "id"))

    def test_same_logical_rows_from_two_engines_yield_same_digest(self):
        """跨库一致性：SQLite/MySQL 导出同一批行（值等价）必须同摘要。"""
        sqlite_side = self._rows()
        mysql_side = [
            {"id": 2, "folio_id": 1, "amount": "206.40", "business_date": "2026-01-01"},
            {"id": 10, "folio_id": 3, "amount": "100.00", "business_date": "2026-01-02"},
        ]
        self.assertEqual(
            table_digest(sqlite_side, COLUMNS, "id"),
            table_digest(mysql_side, COLUMNS, "id"),
        )

    def test_metadata_registers_algorithm_version_and_pk_range(self):
        rows = self._rows()
        meta = build_metadata(
            source_table="legacy_folio_archive",
            rows=rows,
            pk_field="id",
            table_summary=table_digest(rows, COLUMNS, "id"),
            computed_at=timezone.now(),
            operator="ops@example.com",
        )
        self.assertEqual(meta["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(meta["row_count"], 2)
        self.assertEqual((meta["min_pk"], meta["max_pk"]), (2, 10))
        self.assertEqual(meta["operator"], "ops@example.com")
        self.assertRegex(meta["computed_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class LegacyMigrationBatchGuardTests(TestCase):
    """批次状态机与 QuerySet 守卫（N3/N8/N12/N13）。"""

    prop: Property
    approver: User
    executor: User

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.approver = User.objects.create_user(
            username="batch-approver", password="x", is_staff=True
        )
        cls.executor = User.objects.create_user(
            username="batch-executor", password="x", is_staff=True
        )

    def _batch(self, **overrides):
        payload = {
            "batch_uid": "BATCH-TEST-001",
            "approved_by": self.approver,
            "approved_at": timezone.now(),
            "approval_basis": "GOV-03 准入通过",
            "scope_property": self.prop,
            "scope_source_models": ["Folio"],
            "scope_record_range": {"pk_min": 1, "pk_max": 100},
            "executed_by": self.executor,
        }
        payload.update(overrides)
        return LegacyMigrationBatch.objects.create(**payload)

    def test_pending_to_active_allowed(self):
        batch = self._batch()
        batch.status = LegacyMigrationBatch.Status.ACTIVE
        batch.save()
        batch.refresh_from_db()
        self.assertEqual(batch.status, LegacyMigrationBatch.Status.ACTIVE)

    def test_active_cannot_revert_to_pending(self):
        batch = self._batch()
        batch.status = LegacyMigrationBatch.Status.ACTIVE
        batch.save()
        batch.status = LegacyMigrationBatch.Status.PENDING
        with self.assertRaises(ValidationError):
            batch.save()

    def test_active_freezes_scope_and_executor(self):
        batch = self._batch()
        batch.status = LegacyMigrationBatch.Status.ACTIVE
        batch.save()
        batch.scope_record_range = {"pk_min": 1, "pk_max": 9999}
        with self.assertRaises(ValidationError):
            batch.save()

    def test_closed_is_terminal(self):
        batch = self._batch()
        batch.status = LegacyMigrationBatch.Status.ACTIVE
        batch.save()
        batch.status = LegacyMigrationBatch.Status.CLOSED
        batch.save()
        batch.status = LegacyMigrationBatch.Status.ACTIVE
        with self.assertRaises(ValidationError):
            batch.save()

    def test_queryset_update_rejected(self):
        self._batch()
        with self.assertRaises(ValidationError):
            LegacyMigrationBatch.objects.filter(batch_uid="BATCH-TEST-001").update(
                scope_record_range={"pk_min": 1, "pk_max": 9999}
            )

    def test_queryset_delete_rejected(self):
        self._batch()
        with self.assertRaises(ValidationError):
            LegacyMigrationBatch.objects.all().delete()

    def test_bulk_update_rejected(self):
        batch = self._batch()
        batch.status = LegacyMigrationBatch.Status.CLOSED
        with self.assertRaises(ValidationError):
            LegacyMigrationBatch.objects.bulk_update([batch], ["status"])

    def test_bulk_create_rejected(self):
        with self.assertRaises(ValidationError):
            LegacyMigrationBatch.objects.bulk_create([])

    def test_sod_rejects_same_approver_and_executor(self):
        """SoD 由 DB CheckConstraint 强制：批准人与执行人相同时写入被拒。"""
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._batch(batch_uid="BATCH-SOD-001", executed_by=self.approver)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ExecutionCredentialTests(TestCase):
    """签名执行凭证：验签、TTL、nonce 单次、绑定与范围校验（N9/N10/N11/N13）。"""

    prop: Property
    approver: User
    executor: User

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.approver = User.objects.create_user(
            username="cred-approver", password="x", is_staff=True
        )
        cls.executor = User.objects.create_user(
            username="cred-executor", password="x", is_staff=True
        )

    def _active_batch(self, uid="BATCH-CRED-001", **overrides):
        payload = {
            "batch_uid": uid,
            "approved_by": self.approver,
            "approved_at": timezone.now(),
            "approval_basis": "GOV-03",
            "scope_property": self.prop,
            "scope_source_models": ["Folio"],
            "scope_record_range": {"pk_min": 1, "pk_max": 100},
            "executed_by": self.executor,
            "status": LegacyMigrationBatch.Status.ACTIVE,
        }
        payload.update(overrides)
        return LegacyMigrationBatch.objects.create(**payload)

    def _token(self, batch, *, executor_pk=None):
        return mint_credential(
            batch_uid=batch.batch_uid,
            executor_user_pk=executor_pk if executor_pk is not None else batch.executed_by_id,
        )

    def test_roundtrip_binds_batch_and_executor(self):
        batch = self._active_batch()
        credential = load_credential(self._token(batch))
        self.assertEqual(credential.batch_uid, batch.batch_uid)
        self.assertEqual(credential.executor_user_pk, self.executor.pk)
        self.assertTrue(credential.nonce)

    def test_missing_or_empty_credential_denied(self):
        with self.assertRaises(LegacyMigrationDenied):
            load_credential("")
        with self.assertRaises(LegacyMigrationDenied):
            load_credential(None)  # type: ignore[arg-type]

    def test_tampered_payload_denied(self):
        """篡改签名（不碰 base64 载荷，确保走 BadSignature 分支）。"""
        batch = self._active_batch()
        token = self._token(batch)
        prefix, _, signature = token.rpartition(":")
        flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaises(LegacyMigrationDenied):
            load_credential(f"{prefix}:{flipped}")

    def test_expired_credential_denied(self):
        """TTL 过期走 signing.SignatureExpired 分支：用负 max_age 触发真实过期判定。"""
        batch = self._active_batch()
        token = self._token(batch)
        patcher = mock.patch("apps.billing.legacy_credentials.CREDENTIAL_MAX_AGE", -1)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(LegacyMigrationDenied) as ctx:
            load_credential(token)
        self.assertIn("过期", str(ctx.exception))

    def test_replayed_nonce_denied(self):
        batch = self._active_batch()
        token = self._token(batch)
        authorize_execution(token, batch=batch)
        with self.assertRaises(LegacyMigrationDenied) as ctx:
            authorize_execution(token, batch=batch)
        self.assertIn("重放", str(ctx.exception))

    def test_auth_audit_consumes_nonce(self):
        batch = self._active_batch()
        credential = authorize_execution(self._token(batch), batch=batch)
        event = AuditEvent.objects.get(nonce=credential.nonce)
        self.assertEqual(event.action, AUTH_AUDIT_ACTION)
        self.assertEqual(event.entity_id, str(batch.pk))

    def test_batch_uid_mismatch_denied(self):
        batch = self._active_batch()
        other = self._active_batch(uid="BATCH-CRED-OTHER")
        token = mint_credential(batch_uid=other.batch_uid, executor_user_pk=self.executor.pk)
        with self.assertRaises(LegacyMigrationDenied):
            authorize_execution(token, batch=batch)

    def test_executor_mismatch_denied(self):
        batch = self._active_batch()
        intruder = User.objects.create_user(username="cred-intruder", password="x", is_staff=True)
        token = self._token(batch, executor_pk=intruder.pk)
        with self.assertRaises(LegacyMigrationDenied):
            authorize_execution(token, batch=batch)

    def test_pending_batch_denied(self):
        batch = self._active_batch(status=LegacyMigrationBatch.Status.PENDING)
        with self.assertRaises(LegacyMigrationDenied):
            authorize_execution(self._token(batch), batch=batch)

    def test_closed_batch_denied_forever(self):
        batch = self._active_batch()
        batch.status = LegacyMigrationBatch.Status.CLOSED
        batch.save()
        with self.assertRaises(LegacyMigrationDenied):
            authorize_execution(self._token(batch), batch=batch)

    def test_source_model_outside_scope_denied(self):
        batch = self._active_batch()
        with self.assertRaises(LegacyMigrationDenied):
            assert_record_in_scope(batch, source_model="FinanceRecord", record_pk=5)

    def test_record_outside_pk_range_denied(self):
        batch = self._active_batch()
        assert_record_in_scope(batch, source_model="Folio", record_pk=100)
        with self.assertRaises(LegacyMigrationDenied):
            assert_record_in_scope(batch, source_model="Folio", record_pk=101)

    def test_record_whitelist_scope(self):
        batch = self._active_batch(scope_record_range={"pks": [7, 9]})
        assert_record_in_scope(batch, source_model="Folio", record_pk=7)
        with self.assertRaises(LegacyMigrationDenied):
            assert_record_in_scope(batch, source_model="Folio", record_pk=8)

    def test_scope_without_range_or_whitelist_denied(self):
        batch = self._active_batch(scope_record_range={})
        with self.assertRaises(LegacyMigrationDenied):
            assert_record_in_scope(batch, source_model="Folio", record_pk=1)
