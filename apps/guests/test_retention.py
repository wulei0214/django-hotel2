"""隐私数据生命周期治理测试（V1.0 任务书 TASK-02）。

锁定 retention.py 全链行为：
* 到期候选：到期进入、未到期不进、已匿名化不进；
* LegalHold：冻结对象一律跳过（skipped），解除后可清理；
* 业务关联：未结束预订/未结账 Folio/待审批退款 → 跳过；
* dry-run：只统计不落写（Guest 数据不变）；
* 正式执行：不可逆匿名化（密文/哈希抹除、姓名置标记）+ 执行记录 + 审计；
* 幂等：重复执行不重复破坏（已匿名化不再进候选）；
* 模型约束：执行记录审批引用必填、冻结记录不可改/不可删；
* 权限/越权属 API 层（后续 TASK-02-2 管理中心接口补充），本文件覆盖
  服务层与模型层不变量。
"""

import uuid
from datetime import timedelta
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import ApprovalRecord, Folio, LedgerEntry
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest, LegalHold, RetentionExecution, RetentionPolicy
from apps.guests.retention import (
    ACTION_RETENTION_EXECUTE,
    ANONYMIZED_MARKER,
    anonymize_guest,
    candidate_guests,
    plan_retention_cleanup,
    release_legal_hold,
)
from apps.reservations.models import Reservation


class PrivacyGovernanceTestBase(TestCase):
    property: Any
    operator: Any
    policy: Any
    """公共夹具：物业 + 策略 + 造 Guest 工厂。"""

    @classmethod
    def setUpTestData(cls):
        from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment

        cls.property = Property.objects.get(code="DEFAULT")
        cls.operator = User.objects.create_user(username="privacy_op")
        cls.operator.set_unusable_password()
        cls.operator.save(update_fields=["password"])
        # 执行人完整岗位链（AuthorizationService 前提）+ 店长组 +
        # guests.retention_execute 独立执行权限（OWNER 裁定授权矩阵）。
        profile = StaffProfile.objects.create(
            user=cls.operator,
            full_name="隐私清理执行员",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=cls.property,
            employee_no="RT-EXEC-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        from django.contrib.auth.models import Group, Permission
        from django.contrib.contenttypes.models import ContentType

        group, _ = Group.objects.get_or_create(name="店长")
        ct, _ = ContentType.objects.get_or_create(app_label="guests", model="guest")
        execute_perm, _ = Permission.objects.get_or_create(
            content_type=ct,
            codename="retention_execute",
            defaults={"name": "能执行已批准的隐私数据清理"},
        )
        group.permissions.add(execute_perm)
        StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=timezone.now()
        )
        cls.policy = RetentionPolicy.objects.create(
            property=cls.property,
            data_category=RetentionPolicy.DataCategory.GUEST_PROFILE,
            retention_days=365,
            retention_basis=RetentionPolicy.RetentionBasis.LAW,
            effective_from=timezone.localdate(),
            created_by=cls.operator,
            updated_by=cls.operator,
        )

    def _make_guest(self, name="测试住客", days_ago=None, phone_cipher="ZGVm"):
        guest = Guest.objects.create(
            property=self.property,
            full_name=name,
            phone_ciphertext=phone_cipher,
            phone_hash="h" * 64,
        )
        if days_ago is not None:
            # 无预订历史：到期锚点=建档日（created_at 只读，用迁移同款 update 绕过）
            Guest.objects.filter(pk=guest.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
            guest.refresh_from_db()
        return guest

    def _as_of(self):
        return timezone.localdate()


class RetentionPolicyTests(PrivacyGovernanceTestBase):
    """策略模型：每物业每类别仅一条启用策略、天数必须为正。"""

    def test_duplicate_enabled_policy_forbidden(self):
        with self.assertRaises(IntegrityError):
            RetentionPolicy.objects.create(
                property=self.property,
                data_category=RetentionPolicy.DataCategory.GUEST_PROFILE,
                retention_days=100,
                retention_basis=RetentionPolicy.RetentionBasis.CONTRACT,
                effective_from=timezone.localdate(),
                created_by=self.operator,
                updated_by=self.operator,
            )

    def test_negative_days_forbidden(self):
        with self.assertRaises(IntegrityError):
            RetentionPolicy.objects.create(
                property=self.property,
                data_category=RetentionPolicy.DataCategory.GUEST_DOCUMENT,
                retention_days=0,
                retention_basis=RetentionPolicy.RetentionBasis.LAW,
                effective_from=timezone.localdate(),
                created_by=self.operator,
                updated_by=self.operator,
            )


class CandidateSelectionTests(PrivacyGovernanceTestBase):
    """到期候选集：到期/未到期/已匿名化三态。"""

    def test_expired_guest_enters_candidates(self):
        guest = self._make_guest(days_ago=400)
        result = candidate_guests(self.property, self.policy, self._as_of())
        self.assertIn(guest.pk, list(result.values_list("pk", flat=True)))

    def test_not_expired_guest_excluded(self):
        self._make_guest(days_ago=10)
        self.assertEqual(candidate_guests(self.property, self.policy, self._as_of()).count(), 0)

    def test_anonymized_guest_excluded(self):
        guest = self._make_guest(days_ago=400)
        anonymize_guest(guest)
        self.assertEqual(candidate_guests(self.property, self.policy, self._as_of()).count(), 0)


class DryRunTests(PrivacyGovernanceTestBase):
    """dry-run：只统计不写。"""

    def test_dry_run_counts_without_mutation(self):
        expired = self._make_guest("到期住客", days_ago=400)
        fresh = self._make_guest("未到期", days_ago=5)
        before_cipher = fresh.phone_ciphertext
        execution, _to_process = plan_retention_cleanup(
            property_obj=self.property,
            policy=self.policy,
            as_of=self._as_of(),
            operator=self.operator,
            dry_run=True,
        )
        self.assertEqual(execution.candidate_count, 1)
        self.assertEqual(execution.processed_count, 0)
        self.assertEqual(execution.execution_status, RetentionExecution.ExecutionStatus.DRAFT)
        expired.refresh_from_db()
        self.assertNotEqual(expired.full_name, ANONYMIZED_MARKER)  # dry-run 不动数据
        fresh.refresh_from_db()
        self.assertEqual(fresh.phone_ciphertext, before_cipher)
        # dry-run 落审计
        self.assertTrue(AuditEvent.objects.filter(action="privacy.retention_dry_run").exists())


class LegalHoldTests(PrivacyGovernanceTestBase):
    """法律冻结：冻结跳过；不可改不可删；解除后可清理。"""

    def _make_hold(self, guest):
        return LegalHold.objects.create(
            property=self.property,
            target_type="GUEST",
            target_id=str(guest.pk),
            hold_reason="诉讼保全",
            requested_by=self.operator,
            approved_by=self.operator,
            approval_reference="AR-1",
        )

    def test_hold_guest_skipped_not_failed(self):
        guest = self._make_guest("被冻结", days_ago=400)
        self._make_hold(guest)
        execution, _to = plan_retention_cleanup(
            property_obj=self.property,
            policy=self.policy,
            as_of=self._as_of(),
            operator=self.operator,
            dry_run=True,
        )
        self.assertEqual(execution.skipped_count, 1)
        self.assertEqual(execution.failure_count, 0)

    def test_hold_immutable_and_undeletable(self):
        guest = self._make_guest("冻结对象", days_ago=400)
        hold = self._make_hold(guest)
        hold.hold_reason = "试图改原因"
        with self.assertRaises(ValidationError):
            hold.save()
        with self.assertRaises(RuntimeError):
            hold.delete()

    def test_release_allows_cleanup(self):
        guest = self._make_guest("解冻后清理", days_ago=400)
        hold = self._make_hold(guest)
        release_legal_hold(hold, approved_by=self.operator, release_reason="诉讼终结")
        execution, to_process = plan_retention_cleanup(
            property_obj=self.property,
            policy=self.policy,
            as_of=self._as_of(),
            operator=self.operator,
            dry_run=True,
        )
        self.assertEqual(execution.skipped_count, 0)
        self.assertEqual(len(to_process), 1)

    def test_release_requires_reason(self):
        guest = self._make_guest("解冻缺因", days_ago=400)
        hold = self._make_hold(guest)
        hold.hold_status = LegalHold.HoldStatus.RELEASED
        hold.released_at = timezone.now()
        with self.assertRaises(IntegrityError):
            hold.save()


class BusinessLinkSkipTests(PrivacyGovernanceTestBase):
    """业务关联：未结束预订/未结账 Folio/待审批退款 → 跳过。"""

    def test_open_reservation_skips_cleanup(self):
        guest = self._make_guest("在住客人", days_ago=400)
        Reservation.objects.create(
            property=self.property,
            booking_no="PRT-RES-001",
            guest=guest,
            status=Reservation.Status.CHECKED_IN,
            check_in=self._as_of() - timedelta(days=2),
            check_out=self._as_of() + timedelta(days=1),
        )
        execution, to_process = plan_retention_cleanup(
            property_obj=self.property,
            policy=self.policy,
            as_of=self._as_of(),
            operator=self.operator,
            dry_run=True,
        )
        # 在住客人：到期锚点=未来离店日 > cutoff，本就不进候选集（第一道保险）；
        # 即便锚点被人为调旧，_guest_has_open_business 也会 skip（第二道保险）。
        self.assertEqual(execution.candidate_count, 0)
        self.assertEqual(len(to_process), 0)

    def test_stale_anchor_with_open_reservation_skips(self):
        """第二道保险：锚点旧（有历史退房）但当前又在住的客人必须跳过。"""
        guest = self._make_guest("续住客人", days_ago=400)
        # 历史已退房预订（把到期锚点拉到过去）
        Reservation.objects.create(
            property=self.property,
            booking_no="PRT-RES-000",
            guest=guest,
            status=Reservation.Status.CHECKED_OUT,
            check_in=self._as_of() - timedelta(days=10),
            check_out=self._as_of() - timedelta(days=8),
        )
        # 当前在住预订（check_out 在未来——但 Max(check_out) 会取未来值，
        # 因此这里直接验证 _guest_has_open_business 兜底逻辑）
        from apps.guests.retention import _guest_has_open_business

        Reservation.objects.create(
            property=self.property,
            booking_no="PRT-RES-001",
            guest=guest,
            status=Reservation.Status.CHECKED_IN,
            check_in=self._as_of() - timedelta(days=2),
            check_out=self._as_of() + timedelta(days=1),
        )
        self.assertTrue(_guest_has_open_business(guest))

    def test_open_folio_skips_cleanup(self):
        guest = self._make_guest("未结账", days_ago=400)
        # 未结束预订会被跳过——这里造「预订已退房但 Folio 仍 OPEN」的组合
        reservation = Reservation.objects.create(
            property=self.property,
            booking_no="PRT-RES-002",
            guest=guest,
            status=Reservation.Status.CHECKED_OUT,
            check_in=self._as_of() - timedelta(days=5),
            check_out=self._as_of() - timedelta(days=3),
        )
        # Folio 需要 guest；MASTER 唯一键由 save 维护
        Folio.objects.create(property=self.property, reservation=reservation, guest=guest)
        execution, to_process = plan_retention_cleanup(
            property_obj=self.property,
            policy=self.policy,
            as_of=self._as_of(),
            operator=self.operator,
            dry_run=True,
        )
        self.assertEqual(len(to_process), 0)


class ExecutionTests(PrivacyGovernanceTestBase):
    """正式执行：不可逆匿名化 + 执行记录 + 审计 + 幂等。"""

    def _make_approval(self, requester, approver):
        """造一条 APPROVED 清理审批：PENDING 创建后经裸 QuerySet 置 APPROVED。

        ApprovalRecord 只允许从 PENDING 创建（不可变守卫），测试绕过用
        django_models.QuerySet.update（与 test_legacy_archive 同模式）。
        """
        from django.db import models as django_models

        record = ApprovalRecord.objects.create(
            property=self.property,
            action_code=ACTION_RETENTION_EXECUTE,
            entity_type="RetentionExecution",
            entity_id="batch-test",
            requested_by=requester,
            reason="保留期满清理审批（测试）",
            idempotency_key=f"AR-{requester.pk}-{approver.pk}-{uuid.uuid4().hex[:8]}",
        )
        django_models.QuerySet(ApprovalRecord).filter(pk=record.pk).update(
            approved_by=approver,
            status=ApprovalRecord.Status.APPROVED,
            approved_at=timezone.now(),
        )
        record.refresh_from_db()
        return record

    def test_formal_execution_via_real_path_anonymizes(self):
        """DEFECT-T02-1/3 修复验证：dry_run=False 真实路径走通并匿名化。"""
        guest = self._make_guest("正式执行", days_ago=400, phone_cipher="QUJD")
        requester = User.objects.create_user(username="t02_requester")
        requester.set_unusable_password()
        requester.save(update_fields=["password"])
        approval = self._make_approval(requester, self.operator)
        execution, _processed = plan_retention_cleanup(
            property_obj=self.property,
            policy=self.policy,
            as_of=self._as_of(),
            operator=self.operator,
            dry_run=False,
            approval_reference=str(approval.pk),
        )
        self.assertEqual(execution.execution_status, RetentionExecution.ExecutionStatus.COMPLETED)
        self.assertEqual(execution.processed_count, 1)
        self.assertEqual(execution.approval_reference, str(approval.pk))
        guest.refresh_from_db()
        self.assertEqual(guest.full_name, ANONYMIZED_MARKER)
        self.assertIsNone(guest.phone_ciphertext)
        self.assertIsNone(guest.phone_hash)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="privacy.guest_anonymized", entity_id=str(guest.pk)
            ).exists()
        )

    def test_formal_execution_without_approval_rejected(self):
        """无审批引用 → 拒绝且不落任何正式执行记录。"""
        self._make_guest("无审批", days_ago=400)
        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=self.policy,
                as_of=self._as_of(),
                operator=self.operator,
                dry_run=False,
            )
        self.assertFalse(RetentionExecution.objects.filter(dry_run=False).exists())

    def test_formal_execution_unknown_approval_rejected(self):
        """审批不存在 → 拒绝。"""
        self._make_guest("假审批", days_ago=400)
        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=self.policy,
                as_of=self._as_of(),
                operator=self.operator,
                dry_run=False,
                approval_reference="999999",
            )

    def test_formal_execution_self_approval_rejected(self):
        """执行人=审批申请人（SoD）→ 拒绝。"""
        requester = User.objects.create_user(username="t02_self")
        requester.set_unusable_password()
        requester.save(update_fields=["password"])
        approval = self._make_approval(requester, requester)
        self._make_guest("自批拒绝", days_ago=400)
        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=self.policy,
                as_of=self._as_of(),
                operator=requester,
                dry_run=False,
                approval_reference=str(approval.pk),
            )

    def test_repeat_execution_is_idempotent(self):
        guest = self._make_guest("幂等执行", days_ago=400)
        anonymize_guest(guest)
        first = anonymize_guest(guest)
        self.assertFalse(first)  # 第二次返回 False（无操作）
        # 已匿名化不再进候选
        self.assertEqual(candidate_guests(self.property, self.policy, self._as_of()).count(), 0)

    def test_formal_execution_requires_approval_reference(self):
        execution = RetentionExecution(
            property=self.property,
            policy=self.policy,
            dry_run=False,
            as_of=self._as_of(),
        )
        with self.assertRaises(ValidationError):
            execution.full_clean()

    def test_ledger_and_audit_untouched(self):
        """账务凭证与审计记录永不在清理范围（依法保存不变量）。"""
        guest = self._make_guest("账务保全", days_ago=400)
        # 预置一条普通业务事件（既有审计事实，匿名化后必须仍存在）
        AuditEvent.record(
            action="frontdesk.checked_in",
            entity_type="Stay",
            entity_id="1",
            property_obj=self.property,
            request=None,
        )
        ledger_before = LedgerEntry.objects.count()
        audit_before = AuditEvent.objects.count()
        anonymize_guest(guest)
        self.assertEqual(LedgerEntry.objects.count(), ledger_before)
        # 审计只增（新增匿名化事件），原有事件全部保留
        self.assertGreaterEqual(AuditEvent.objects.count(), audit_before + 1)
        self.assertTrue(AuditEvent.objects.exclude(action="privacy.guest_anonymized").exists())
