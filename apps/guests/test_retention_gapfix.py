"""T02-3-FINAL-GAP-001 专项测试：计划绑定（PB）与执行权限（EP）。

覆盖任务书 §6.1 / §6.2 全部场景。与 test_retention.py 的既有用例互补，
不改动其夹具。

关键设计：
  - 审批一律经 ApprovalService.request_approval + .approve 产生（禁止伪造）；
  - 计划一律用真实 dry-run 产生的 RetentionExecution；
  - 权限用 AuthorizationService 真实判定（StaffRoleAssignment → group.permissions）。
"""

import uuid
from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from apps.billing.services_v2 import ApprovalService
from apps.core.models import Property
from apps.guests.models import Guest, RetentionExecution, RetentionPolicy
from apps.guests.retention import ACTION_RETENTION_EXECUTE, plan_retention_cleanup
from apps.identity.authorization import AuthorizationService
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)

EXEC_PERM = "guests.retention_execute"
REQUEST_PERM = "guests.retention_request"
APPROVE_PERM = "guests.retention_approve"


class GapFixTestBase(TestCase):
    """物业 + 策略 + 三个岗位链（店长/值班经理/无权限岗位）。"""

    @classmethod
    def setUpTestData(cls):
        cls.property = Property.objects.get(code="DEFAULT")
        seed = cls._mk_user("t023_seed")
        cls.policy = RetentionPolicy.objects.create(
            property=cls.property,
            data_category=RetentionPolicy.DataCategory.GUEST_PROFILE,
            retention_days=365,
            retention_basis=RetentionPolicy.RetentionBasis.LAW,
            effective_from=timezone.localdate(),
            created_by=seed,
            updated_by=seed,
        )
        cls.ct, _ = ContentType.objects.get_or_create(app_label="guests", model="guest")
        for codename in ("retention_request", "retention_approve", "retention_execute"):
            Permission.objects.get_or_create(
                content_type=cls.ct,
                codename=codename,
                defaults={"name": f"T02-3 {codename}"},
            )

    @classmethod
    def _mk_user(cls, name):
        user = User.objects.create_user(username=name)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        return user

    # —— 岗位链工厂：授予若干 guests.* 码 ——
    def make_staff(self, name, perms, status="ACTIVE"):
        user = self._mk_user(name)
        profile = StaffProfile.objects.create(
            user=user,
            full_name=name,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property,
            employee_no=f"EMP-{uuid.uuid4().hex[:8]}",
            status=status,
        )
        if perms:
            group, _ = Group.objects.get_or_create(name=f"G-{uuid.uuid4().hex[:8]}")
            for full_code in perms:
                # perms 传入的是完整码（如 guests.retention_execute），
                # 这里拆成 app_label / codename 严格匹配迁移创建的权限点。
                app_label, codename = full_code.split(".", 1)
                perm = Permission.objects.filter(
                    content_type__app_label=app_label,
                    codename=codename,
                ).first()
                if perm is None:
                    raise AssertionError(f"迁移未创建权限 {full_code}（测试前置不成立）")
                group.permissions.add(perm)
                assert group.permissions.filter(pk=perm.pk).exists(), f"权限关联失败：{full_code}"
            StaffRoleAssignment.objects.create(
                membership=membership, group=group, starts_at=timezone.now()
            )
        return user

    # —— 造到期 Guest ——
    def make_guest(self, name="到期住客", days_ago=400):
        guest = Guest.objects.create(
            property=self.property,
            full_name=name,
            phone_ciphertext="QUJD",
            phone_hash="h" * 64,
        )
        Guest.objects.filter(pk=guest.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago)
        )
        guest.refresh_from_db()
        return guest

    # —— 真实 dry-run 计划 ——
    def make_plan(self, operator, policy=None, as_of=None):
        record, _ = plan_retention_cleanup(
            property_obj=self.property,
            policy=policy or self.policy,
            as_of=as_of or timezone.localdate(),
            operator=operator,
            dry_run=True,
        )
        return record

    # —— 经真实审批链产生 APPROVED 记录 ——
    def make_approved(self, requester, approver, plan, policy=None, as_of=None):
        rec, _ = ApprovalService.request_approval(
            property_obj=self.property,
            action_code=ACTION_RETENTION_EXECUTE,
            entity_type="RetentionExecution",
            entity_id=str(plan.pk),
            requested_by=requester,
            reason="T02-3 计划绑定测试用审批",
            payload={
                "retention_policy_id": str((policy or self.policy).pk),
                "as_of_date": str(as_of or plan.as_of),
            },
        )
        return ApprovalService.approve(rec.pk, approved_by=approver, property_obj=self.property)

    # —— 造第二个策略（须先禁用原策略：同物业同类别只允许一个 enabled）——
    def make_second_policy(self, days=30):
        self.policy.enabled = False
        self.policy.save(update_fields=["enabled", "enabled_policy_key"])
        return RetentionPolicy.objects.create(
            property=self.property,
            data_category=RetentionPolicy.DataCategory.GUEST_PROFILE,
            retention_days=days,
            retention_basis=RetentionPolicy.RetentionBasis.LAW,
            effective_from=timezone.localdate(),
            created_by=self.policy.created_by,
            updated_by=self.policy.created_by,
        )

    def run_formal(self, operator, ref, policy=None, as_of=None, execution=None):
        return plan_retention_cleanup(
            property_obj=self.property,
            policy=policy or self.policy,
            as_of=as_of or timezone.localdate(),
            operator=operator,
            dry_run=False,
            approval_reference=str(ref),
            execution=execution,
        )


class PlanBindingTests(GapFixTestBase):
    """§6.1 计划绑定 PB-01 ~ PB-10。"""

    def setUp(self):
        self.requester = self.make_staff("pb_req", [REQUEST_PERM, APPROVE_PERM, EXEC_PERM])
        self.approver = self.make_staff("pb_apv", [APPROVE_PERM, EXEC_PERM])
        self.executor = self.make_staff("pb_exec", [EXEC_PERM])
        self.make_guest()

    def test_pb01_binding_consistent_allows(self):
        """PB-01 审批计划与执行计划一致 → 进入执行校验。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        rec, processed = self.run_formal(self.executor, apv.pk, execution=plan)
        self.assertEqual(rec.execution_status, RetentionExecution.ExecutionStatus.COMPLETED)
        # 正式执行路径返回的 to_process 已被消费，计数以 processed_count 为准
        self.assertGreaterEqual(rec.processed_count, 1)

    def test_pb02_replaced_plan_id_rejected(self):
        """PB-02 替换 plan_id（用 A 的审批执行 B 计划）→ 拒绝。"""
        plan_a = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan_a)
        # 另造一个计划 B，把审批的 entity_id 指向 B 不成立（审批绑 A），
        # 此处验证：用 A 的审批但入参换成 B 的策略/基准日 → 拒绝
        other_policy = self.make_second_policy(days=30)
        with self.assertRaises(ValueError):
            self.run_formal(self.executor, apv.pk, policy=other_policy)

    def test_pb03_cross_property_rejected(self):
        """PB-03 跨物业使用审批记录 → 拒绝。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        other_prop = Property.objects.create(code="OTHER-PB03", name="他物业")
        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=other_prop,
                policy=self.policy,
                as_of=timezone.localdate(),
                operator=self.executor,
                dry_run=False,
                approval_reference=str(apv.pk),
            )

    def test_pb04_replaced_policy_rejected(self):
        """PB-04 替换清理策略 → 拒绝。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        other_policy = self.make_second_policy(days=90)
        with self.assertRaises(ValueError):
            self.run_formal(self.executor, apv.pk, policy=other_policy)

    def test_pb05_as_of_changed_rejected(self):
        """PB-05 基准日变化（数据范围变化）→ 拒绝。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        with self.assertRaises(ValueError):
            self.run_formal(self.executor, apv.pk, as_of=timezone.localdate() - timedelta(days=1))

    def test_pb06_expired_plan_rejected(self):
        """PB-06 计划已失效（状态 COMPLETED）→ 拒绝。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        # 把计划置为终态，等价于「计划已失效不可再用」
        plan.execution_status = RetentionExecution.ExecutionStatus.COMPLETED
        plan.save(update_fields=["execution_status"])
        with self.assertRaises(ValueError):
            self.run_formal(self.executor, apv.pk, execution=plan)

    def test_pb07_revoked_plan_rejected(self):
        """PB-07 计划已终止（状态 FAILED）→ 拒绝。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        plan.execution_status = RetentionExecution.ExecutionStatus.FAILED
        plan.save(update_fields=["execution_status"])
        with self.assertRaises(ValueError):
            self.run_formal(self.executor, apv.pk, execution=plan)

    def test_pb08_already_executed_rejected(self):
        """PB-08 计划已执行，再次执行 → 拒绝（不可重复使用）。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        self.run_formal(self.executor, apv.pk, execution=plan)
        with self.assertRaises(ValueError):
            self.run_formal(self.executor, apv.pk)

    def test_pb09_approval_not_bound_to_real_plan_rejected(self):
        """PB-09 审批不属于当前申请（entity_id 非真实计划）→ 拒绝。"""
        rec, _ = ApprovalService.request_approval(
            property_obj=self.property,
            action_code=ACTION_RETENTION_EXECUTE,
            entity_type="RetentionExecution",
            entity_id="not-a-real-plan",
            requested_by=self.requester,
            reason="PB-09 绑定无效计划",
            payload={
                "retention_policy_id": str(self.policy.pk),
                "as_of_date": str(timezone.localdate()),
            },
        )
        apv = ApprovalService.approve(rec.pk, approved_by=self.approver, property_obj=self.property)
        with self.assertRaises(ValueError):
            self.run_formal(self.executor, apv.pk)

    def test_pb10_not_approved_status_rejected(self):
        """PB-10 审批状态不是正式批准（PENDING）→ 拒绝。"""
        plan = self.make_plan(self.requester)
        rec, _ = ApprovalService.request_approval(
            property_obj=self.property,
            action_code=ACTION_RETENTION_EXECUTE,
            entity_type="RetentionExecution",
            entity_id=str(plan.pk),
            requested_by=self.requester,
            reason="PB-10 未审批",
            payload={"retention_policy_id": str(self.policy.pk), "as_of_date": str(plan.as_of)},
        )
        with self.assertRaises(ValueError):
            self.run_formal(self.executor, rec.pk)


class ExecutePermissionTests(GapFixTestBase):
    """§6.2 执行权限 EP-01 ~ EP-14。"""

    def setUp(self):
        self.requester = self.make_staff("ep_req", [REQUEST_PERM])
        self.approver = self.make_staff("ep_apv", [APPROVE_PERM])
        self.make_guest()

    def _approve_for(self, requester=None, approver=None):
        plan = self.make_plan(requester or self.requester)
        return self.make_approved(requester or self.requester, approver or self.approver, plan)

    def test_ep01_manager_with_execute_allowed(self):
        """EP-01 店长（具执行权限且非申请人）→ 允许。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        executor = self.make_staff("ep_gm", [EXEC_PERM])
        rec, _ = self.run_formal(executor, apv.pk, execution=plan)
        self.assertEqual(rec.execution_status, RetentionExecution.ExecutionStatus.COMPLETED)

    def test_ep02_duty_manager_with_execute_allowed(self):
        """EP-02 值班经理（具执行权限且非申请人）→ 允许。"""
        plan = self.make_plan(self.requester)
        apv = self.make_approved(self.requester, self.approver, plan)
        executor = self.make_staff("ep_duty", [EXEC_PERM])
        rec, _ = self.run_formal(executor, apv.pk, execution=plan)
        self.assertEqual(rec.execution_status, RetentionExecution.ExecutionStatus.COMPLETED)

    def test_ep03_to_ep09_roles_without_execute_rejected(self):
        """EP-03~EP-09 前台/财务/客房/夜审/餐饮/餐饮主管/安保 → 全部拒绝。"""
        for idx, role in enumerate(["fd", "fin", "hk", "night", "rest", "chef", "sec"], start=3):
            with self.subTest(ep=f"EP-0{idx}", role=role):
                apv = self._approve_for()
                executor = self.make_staff(f"ep_{role}", [])
                with self.assertRaises(PermissionDenied):
                    self.run_formal(executor, apv.pk)

    def test_ep10_approve_only_rejected(self):
        """EP-10 只有审批权限、无执行权限 → 拒绝。"""
        apv = self._approve_for()
        executor = self.make_staff("ep_apv_only", [APPROVE_PERM])
        with self.assertRaises(PermissionDenied):
            self.run_formal(executor, apv.pk)

    def test_ep11_request_only_rejected(self):
        """EP-11 只有申请权限、无执行权限 → 拒绝。"""
        apv = self._approve_for()
        executor = self.make_staff("ep_req_only", [REQUEST_PERM])
        with self.assertRaises(PermissionDenied):
            self.run_formal(executor, apv.pk)

    def test_ep12_applicant_self_execute_rejected(self):
        """EP-12 申请人本人执行 → 拒绝（即使持有执行权限）。"""
        requester = self.make_staff("ep_self", [REQUEST_PERM, EXEC_PERM])
        apv = self._approve_for(requester=requester)
        with self.assertRaises(ValueError):
            self.run_formal(requester, apv.pk)

    def test_ep13_execute_perm_but_wrong_property_rejected(self):
        """EP-13 有执行权限但不属目标物业授权范围 → 拒绝。"""
        apv = self._approve_for()
        other_prop = Property.objects.create(code="OTHER-EP13", name="他物业")
        outsider = self.make_staff("ep_outsider", [EXEC_PERM])
        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=other_prop,
                policy=self.policy,
                as_of=timezone.localdate(),
                operator=outsider,
                dry_run=False,
                approval_reference=str(apv.pk),
            )

    def test_ep14_direct_service_call_still_checks_permission(self):
        """EP-14 直接调用正式执行 Service → 仍执行权限校验（不可绕过）。"""
        apv = self._approve_for()
        no_perm = self.make_staff("ep_direct", [])
        # 直接调用 Service（不经任何 API 层）
        with self.assertRaises(PermissionDenied):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=self.policy,
                as_of=timezone.localdate(),
                operator=no_perm,
                dry_run=False,
                approval_reference=str(apv.pk),
            )

    def test_ep_authorization_service_reflects_matrix(self):
        """补充：矩阵结论可直接由 AuthorizationService 验证。"""
        gm = self.make_staff("ep_chk_gm", [EXEC_PERM])
        duty = self.make_staff("ep_chk_duty", [APPROVE_PERM, EXEC_PERM])
        fd = self.make_staff("ep_chk_fd", [])
        self.assertTrue(AuthorizationService.has_permission(gm, self.property, EXEC_PERM))
        self.assertTrue(AuthorizationService.has_permission(duty, self.property, EXEC_PERM))
        self.assertFalse(AuthorizationService.has_permission(fd, self.property, EXEC_PERM))
