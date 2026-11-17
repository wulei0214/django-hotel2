"""受控历史迁移入口 TDD（批次 3）：N2/N4/N5/N6/N7/N9/N10/N11 与凭证落库。

被测对象：``BillingService.post_legacy_migration_entry``（阶段 D §5.1/§5.2）。
性质：**新增被测对象**的先行测试；共享创建流重构保证 ``post_entry`` 签名不变。
"""

import inspect
import unittest
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.billing.legacy_credentials import (
    LegacyMigrationDenied,
    mint_credential,
)
from apps.billing.models import Folio, LedgerEntry, LegacyMigrationBatch
from apps.billing.services_v2 import BillingService
from apps.core.models import AuditEvent, LegacyRecordLink, Property
from apps.guests.models import Guest
from apps.reservations.models import Reservation


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ControlledLegacyEntryTests(TestCase):
    """受控入口：凭证 + 映射 + 审计同事务；越权/越范围/无确认一律拒绝。"""

    prop: Property
    approver: User
    executor: User
    folio: Folio

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.approver = User.objects.create_user(
            username="entry-approver", password="pw-entry", is_staff=True
        )
        cls.executor = User.objects.create_user(
            username="entry-executor", password="pw-entry", is_staff=True
        )
        guest = Guest.objects.create(property=cls.prop, full_name="迁移入口测试客")
        reservation = Reservation.objects.create(
            property=cls.prop,
            guest=guest,
            booking_no="LEGACY-ENTRY-1",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("206.40"),
            currency="CNY",
        )
        cls.folio = Folio.objects.create(
            property=cls.prop, reservation=reservation, guest=guest, currency="CNY"
        )

    def setUp(self):
        self.batch = LegacyMigrationBatch.objects.create(
            batch_uid="BATCH-ENTRY-001",
            approved_by=self.approver,
            approved_at=timezone.now(),
            approval_basis="GOV-03 准入通过（测试）",
            scope_property=self.prop,
            scope_source_models=["Folio"],
            scope_record_range={"pk_min": 1, "pk_max": 100},
            executed_by=self.executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )

    def _token(self, batch=None):
        target = batch or self.batch
        return mint_credential(
            batch_uid=target.batch_uid,
            executor_user_pk=target.executed_by_id,
        )

    def _post(self, **overrides):
        payload = {
            "credential_token": self._token(),
            "batch": self.batch,
            "property_obj": self.prop,
            "folio": self.folio,
            "entry_type": LedgerEntry.EntryType.PAYMENT,
            "amount": Decimal("-206.40"),
            "reference": "legacy:folio:1",
            "source_model": "Folio",
            "source_pk": "1",
            "description": "旧收款迁移",
            "business_date": timezone.localdate(),
        }
        payload.update(overrides)
        return BillingService.post_legacy_migration_entry(**payload)

    # ---- 结构与身份 -------------------------------------------------------
    def test_signature_is_keyword_only(self):
        params = inspect.signature(BillingService.post_legacy_migration_entry).parameters
        self.assertIn("credential_token", params)
        for name, param in params.items():
            self.assertEqual(
                param.kind, inspect.Parameter.KEYWORD_ONLY, msg=f"{name} 必须 keyword-only"
            )

    def test_post_entry_still_has_no_legacy_switch(self):
        """N2：普通入口签名不变——结构上无法设置历史迁移标识。"""
        params = inspect.signature(BillingService.post_entry).parameters
        self.assertNotIn("legacy_workflow", params)
        entry, _ = BillingService.post_entry(
            property_obj=self.prop,
            folio=self.folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=Decimal("100.00"),
            reference="normal:ROOM:1",
            business_date=timezone.localdate(),
        )
        self.assertFalse(entry.legacy_workflow)

    def test_missing_credential_denied(self):
        with self.assertRaises(LegacyMigrationDenied):
            self._post(credential_token="")

    def test_credential_bound_to_other_batch_denied(self):
        other = LegacyMigrationBatch.objects.create(
            batch_uid="BATCH-ENTRY-OTHER",
            approved_by=self.approver,
            approved_at=timezone.now(),
            scope_property=self.prop,
            scope_source_models=["Folio"],
            scope_record_range={"pk_min": 1, "pk_max": 100},
            executed_by=self.executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )
        with self.assertRaises(LegacyMigrationDenied):
            self._post(credential_token=self._token(other))

    # ---- 成功路径 ---------------------------------------------------------
    def test_entry_link_and_audit_written_together(self):
        """N6：凭证 + LegacyRecordLink + AuditEvent 同事务落库。"""
        entry, link = self._post()
        self.assertEqual(entry.reference, "legacy:folio:1")
        self.assertEqual(entry.posted_by_id, self.executor.pk)
        self.assertFalse(entry.legacy_workflow)
        self.assertEqual(link.mapping_status, LegacyRecordLink.MappingStatus.LINKED)
        self.assertEqual(link.target_pk, str(entry.pk))
        self.assertEqual(link.migration_batch, self.batch.batch_uid)
        event = AuditEvent.objects.get(action="billing.legacy_migration")
        self.assertEqual(event.entity_id, str(entry.pk))
        self.assertEqual(event.actor_id, self.executor.pk)
        self.assertEqual(event.after_json["batch_uid"], self.batch.batch_uid)

    def test_legacy_reversal_requires_confirmation(self):
        """N4：无人工确认记录不得创建历史 REVERSAL（不补造审批人）。"""
        original, _ = self._post()
        with self.assertRaises(LegacyMigrationDenied):
            self._post(
                entry_type=LedgerEntry.EntryType.REVERSAL,
                amount=Decimal("206.40"),
                reference="legacy:folio:2",
                source_pk="2",
                void_of=original,
                void_reason="旧冲账",
            )

    def test_legacy_reversal_with_confirmation_sets_flag(self):
        original, _ = self._post()
        reversal, _ = self._post(
            entry_type=LedgerEntry.EntryType.REVERSAL,
            amount=Decimal("206.40"),
            reference="legacy:folio:2",
            source_pk="2",
            void_of=original,
            void_reason="旧冲账（人工配对于 2026-11-16）",
            reversal_confirmation={
                "confirmed_by": "finance-lead",
                "basis": "原始凭证影像 + 旧冲账行同订单同金额",
            },
        )
        self.assertTrue(reversal.legacy_workflow)
        self.assertIsNone(reversal.approval_record_id)
        self.assertEqual(reversal.void_of_id, original.pk)

    # ---- 拒绝与回滚 -------------------------------------------------------
    def test_record_outside_scope_denied_and_audited(self):
        """N7：记录 ∉ scope → 拒绝并落 denied 审计（回滚也保留证据）。"""
        with self.assertRaises(LegacyMigrationDenied):
            self._post(source_pk="101")
        self.assertTrue(
            AuditEvent.objects.filter(
                action="billing.legacy_migration_denied",
                entity_id=str(self.batch.pk),
            ).exists()
        )
        self.assertFalse(LedgerEntry.objects.filter(reference="legacy:folio:1").exists())

    def test_source_model_outside_scope_denied(self):
        with self.assertRaises(LegacyMigrationDenied):
            self._post(source_model="FinanceRecord")

    def test_property_outside_batch_denied(self):
        other_prop = Property.objects.create(code="OTHER-PROP", name="另一物业")
        with self.assertRaises(LegacyMigrationDenied):
            self._post(property_obj=other_prop)

    def test_link_conflict_rolls_back_entry(self):
        """N5：映射已存在时整条回滚——不残留凭证/审计半成品。"""
        LegacyRecordLink.objects.create(
            property=self.prop,
            source_system="hotelapp",
            source_model="Folio",
            source_pk="1",
            target_model="LedgerEntry",
            target_pk="9999",
            mapping_status=LegacyRecordLink.MappingStatus.LINKED,
        )
        with self.assertRaises(LegacyMigrationDenied):
            self._post()
        self.assertFalse(LedgerEntry.objects.filter(reference="legacy:folio:1").exists())
        self.assertFalse(AuditEvent.objects.filter(action="billing.legacy_migration").exists())

    def test_replayed_credential_denied(self):
        """N11：同一凭证 nonce 第二次使用被拒绝（重放防护）。"""
        token = self._token()
        self._post(credential_token=token)
        with self.assertRaises(LegacyMigrationDenied) as ctx:
            self._post(
                credential_token=token,
                reference="legacy:folio:3",
                source_pk="3",
            )
        self.assertIn("重放", str(ctx.exception))
        self.assertEqual(LedgerEntry.objects.filter(reference__startswith="legacy:").count(), 1)

    def test_repeated_record_denied_by_source_uniqueness(self):
        """同一旧记录二次迁移：reference 幂等命中后映射唯一键拒绝，整体回滚。"""
        self._post()
        with self.assertRaises(LegacyMigrationDenied):
            self._post()
        self.assertEqual(LedgerEntry.objects.filter(reference="legacy:folio:1").count(), 1)

    def test_closed_batch_denied(self):
        """N8：CLOSED 后入口永久拒绝。"""
        self.batch.status = LegacyMigrationBatch.Status.CLOSED
        self.batch.save()
        with self.assertRaises(LegacyMigrationDenied):
            self._post()

    def test_active_batch_without_approver_is_denied(self):
        """ACTIVE 但缺少正式批准人不得通过受控入口。"""
        self.batch.approved_by = None
        self.batch.save()
        with self.assertRaises(LegacyMigrationDenied):
            self._post()
        self.assertFalse(LedgerEntry.objects.filter(reference="legacy:folio:1").exists())
        self.assertTrue(
            AuditEvent.objects.filter(action="billing.legacy_migration_denied").exists()
        )

    def test_stale_credential_denied(self):
        """N11：TTL 过期（等价于超时凭证）拒绝。"""
        token = self._token()
        patcher = mock.patch("apps.billing.legacy_credentials.CREDENTIAL_MAX_AGE", -1)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(LegacyMigrationDenied):
            self._post(credential_token=token)

@unittest.skipUnless(connection.vendor == "mysql", "SQLite 的 select_for_update 为 no-op")
@override_settings(DEBUG=True, DJANGO_ENV="test")
class ControlledLegacyEntryConcurrencyTests(TransactionTestCase):
    """真实 MySQL：同一旧来源并发迁移，只允许一份凭证和一条映射落库。"""

    reset_sequences = True

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        self.approver = User.objects.create_user(
            username="entry-concurrent-approver", password="pw", is_staff=True
        )
        self.executor = User.objects.create_user(
            username="entry-concurrent-executor", password="pw", is_staff=True
        )
        guest = Guest.objects.create(property=self.prop, full_name="并发迁移测试客")
        today = timezone.localdate()
        reservation = Reservation.objects.create(
            property=self.prop,
            guest=guest,
            booking_no="LEGACY-CONCURRENT-ENTRY",
            status=Reservation.Status.CONFIRMED,
            check_in=today,
            check_out=today + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("206.40"),
            currency="CNY",
        )
        self.folio = Folio.objects.create(
            property=self.prop, reservation=reservation, guest=guest, currency="CNY"
        )
        self.batch = LegacyMigrationBatch.objects.create(
            batch_uid="BATCH-ENTRY-CONCURRENT",
            approved_by=self.approver,
            approved_at=timezone.now(),
            approval_basis="并发迁移测试批准",
            scope_property=self.prop,
            scope_source_models=["Folio"],
            scope_record_range={"pk_min": 1, "pk_max": 100},
            executed_by=self.executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )

    def test_concurrent_same_source_posts_exactly_one_entry_and_link(self):
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        results = []
        executor_id = self.executor.pk
        batch_id = self.batch.pk
        folio_id = self.folio.pk
        property_id = self.prop.pk

        def worker():
            close_old_connections()
            try:
                executor = User.objects.get(pk=executor_id)
                batch = LegacyMigrationBatch.objects.get(pk=batch_id)
                folio = Folio.objects.get(pk=folio_id)
                property_obj = Property.objects.get(pk=property_id)
                token = mint_credential(
                    batch_uid=batch.batch_uid, executor_user_pk=executor.pk
                )
                barrier.wait(timeout=30)
                entry, link = BillingService.post_legacy_migration_entry(
                    credential_token=token,
                    batch=batch,
                    property_obj=property_obj,
                    folio=folio,
                    entry_type=LedgerEntry.EntryType.PAYMENT,
                    amount=Decimal("-206.40"),
                    reference="legacy:folio:99",
                    source_model="Folio",
                    source_pk="99",
                    description="并发旧收款迁移",
                    business_date=timezone.localdate(),
                )
                with result_lock:
                    results.append(("ok", entry.pk, link.pk))
            except LegacyMigrationDenied as exc:
                with result_lock:
                    results.append(("rejected", str(exc)))
            except Exception as exc:  # 测试必须把线程异常显式带回主线程
                with result_lock:
                    results.append(("error", repr(exc)))
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
            self.assertFalse(thread.is_alive(), "并发迁移线程超时（疑似死锁）")

        self.assertEqual(len([item for item in results if item[0] == "ok"]), 1, results)
        self.assertEqual(len([item for item in results if item[0] == "rejected"]), 1, results)
        self.assertFalse([item for item in results if item[0] == "error"], results)
        self.assertEqual(LedgerEntry.objects.filter(reference="legacy:folio:99").count(), 1)
        self.assertEqual(
            LegacyRecordLink.objects.filter(
                migration_batch=self.batch.batch_uid,
                source_model="Folio",
                source_pk="99",
            ).count(),
            1,
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="billing.legacy_migration").count(), 1
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="billing.legacy_migration_denied").count(), 1
        )

