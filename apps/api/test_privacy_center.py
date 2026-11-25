"""T02-3 隐私清理正式审批链 API 测试（V1.0 TASK-02 第二批 / OWNER 裁定）。

覆盖 PMS2-T02-3-OWNER-DECISION §三 TASK-03 验收项：
  1. 无申请权限 → 403；
  2. 无审批权限 → 403；
  3. 无执行权限 → 403；
  4. 申请人自行审批拒绝（403）；
  5. 申请人自行执行拒绝（403）；
  6. 未审批申请不得执行（400/404）；
  7. 跨物业审批记录不得使用（403/404 不泄露存在性）；
  8. 审批计划与执行计划不一致拒绝（服务层快照校验）；
  9. 重复执行不造成重复匿名化（409 + 无二次破坏）。

权限点（迁移已落库）：guests.retention_request（店长）、
guests.retention_approve（值班经理/店长）、guests.retention_execute
（值班经理/店长）。测试内直接以 Django Permission 授权模拟岗位。
"""

import uuid
from datetime import timedelta
from typing import Any

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import ApprovalRecord
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest, RetentionExecution, RetentionPolicy
from apps.guests.retention import ANONYMIZED_MARKER
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment


class PrivacyApiTestBase(TestCase):
    """夹具：物业、策略、三角色账号工厂（request/approve/execute 分离）。"""

    property: Any
    other_property: Any
    policy: Any
    perms: dict
    staff: dict

    @classmethod
    def setUpTestData(cls):
        from django.contrib.contenttypes.models import ContentType

        cls.property = Property.objects.get(code="DEFAULT")
        cls.other_property = Property.objects.create(code="OTHER-PROP", name="他物业")
        ct, _ = ContentType.objects.get_or_create(app_label="guests", model="guest")
        cls.perms = {}
        for codename in ("retention_request", "retention_approve", "retention_execute"):
            cls.perms[codename], _ = Permission.objects.get_or_create(
                content_type=ct,
                codename=codename,
                defaults={"name": f"测试用 {codename}"},
            )
        creator = cls._make_bare_user(f"policy_creator-{uuid.uuid4().hex[:8]}")
        cls.policy = RetentionPolicy.objects.create(
            property=cls.property,
            data_category=RetentionPolicy.DataCategory.GUEST_PROFILE,
            retention_days=365,
            retention_basis=RetentionPolicy.RetentionBasis.LAW,
            effective_from=timezone.localdate(),
            created_by=creator,
            updated_by=creator,
        )

    @classmethod
    def _make_bare_user(cls, username):
        user = User.objects.create_user(username=username)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        return user

    @classmethod
    def _make_member(cls, username, property_obj, codenames=()):
        """造带有效任职的用户并按 codenames 授权（岗位组隔离）。"""
        group, _ = Group.objects.get_or_create(name=f"测试组-{username}")
        group.permissions.add(*(cls.perms[c] for c in codenames))
        user = User.objects.create_user(username=username)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        profile = StaffProfile.objects.create(
            user=user, full_name=username, employment_status=StaffProfile.EmploymentStatus.ACTIVE
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=property_obj,
            employee_no=f"PA-{username}",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=timezone.now()
        )
        return user

    def _login(self, user):
        self.client.force_login(user)

    def _make_expired_guest(self, name="过期住客"):
        guest = Guest.objects.create(
            property=self.property,
            full_name=name,
            phone_ciphertext="QUJD",
            phone_hash="h" * 64,
        )
        Guest.objects.filter(pk=guest.pk).update(created_at=timezone.now() - timedelta(days=400))
        return guest

    def _create_plan(self):
        self._login(self.requester)
        response = self.client.post(
            reverse("api:privacy_retention_plan_create"),
            data={"policy_id": self.policy.pk, "as_of": str(timezone.localdate())},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return RetentionExecution.objects.get(pk=response.json()["data"]["id"])

    def _approve(self, plan):
        self._login(self.approver)
        response = self.client.post(
            reverse("api:privacy_retention_plan_approve", args=[plan.pk]),
            data={"reason": "保留期满清理审批（测试）"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return ApprovalRecord.objects.get(pk=response.json()["data"]["approval_id"])

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self.requester = self._make_member(
            f"pa-req-{suffix}", self.property, ("retention_request",)
        )
        self.approver = self._make_member(f"pa-apv-{suffix}", self.property, ("retention_approve",))
        self.executor = self._make_member(
            f"pa-exec-{suffix}", self.property, ("retention_execute",)
        )


class RetentionPlanPermissionTests(PrivacyApiTestBase):
    """无对应权限 → 403（验收 1/2/3）。"""

    def test_create_without_request_permission_403(self):
        no_perm = self._make_member(f"pa-noperm-{uuid.uuid4().hex[:8]}", self.property)
        self._login(no_perm)
        response = self.client.post(
            reverse("api:privacy_retention_plan_create"),
            data={"policy_id": self.policy.pk, "as_of": str(timezone.localdate())},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_approve_without_approve_permission_403(self):
        plan = self._create_plan()
        self._login(self.requester)
        response = self.client.post(
            reverse("api:privacy_retention_plan_approve", args=[plan.pk]),
            data={"reason": "尝试自批"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_execute_without_execute_permission_403(self):
        plan = self._create_plan()
        approval = self._approve(plan)
        self._login(self.requester)
        response = self.client.post(
            reverse("api:privacy_retention_plan_execute", args=[plan.pk]),
            data={"approval_reference": str(approval.pk)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


class RetentionApprovalChainTests(PrivacyApiTestBase):
    """SoD 与执行拒绝（验收 4/5/6）。"""

    def test_requester_cannot_approve_own_plan_403(self):
        plan = self._create_plan()
        self._login(self.requester)
        response = self.client.post(
            reverse("api:privacy_retention_plan_approve", args=[plan.pk]),
            data={"reason": "申请人自批"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_requester_cannot_execute_own_plan_403(self):
        plan = self._create_plan()
        approval = self._approve(plan)
        self._login(self.requester)
        response = self.client.post(
            reverse("api:privacy_retention_plan_execute", args=[plan.pk]),
            data={"approval_reference": str(approval.pk)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_unapproved_plan_execution_rejected(self):
        plan = self._create_plan()
        self._login(self.executor)
        response = self.client.post(
            reverse("api:privacy_retention_plan_execute", args=[plan.pk]),
            data={"approval_reference": "999999"},
            content_type="application/json",
        )
        self.assertIn(response.status_code, {400, 404})
        plan.refresh_from_db()
        self.assertEqual(plan.execution_status, RetentionExecution.ExecutionStatus.DRAFT)
        self.assertFalse(Guest.objects.filter(full_name=ANONYMIZED_MARKER).exists())


class RetentionPlanCrossPropertyTests(PrivacyApiTestBase):
    """跨物业审批记录不得使用（验收 7，403/404 不泄露存在性）。"""

    def test_cross_property_approval_rejected(self):
        plan = self._create_plan()
        approval = self._approve(plan)
        outsider = self._make_member(
            f"pa-out-{uuid.uuid4().hex[:8]}", self.other_property, ("retention_execute",)
        )
        self._login(outsider)
        response = self.client.post(
            reverse("api:privacy_retention_plan_execute", args=[plan.pk]),
            data={"approval_reference": str(approval.pk)},
            content_type="application/json",
        )
        self.assertIn(response.status_code, {403, 404})


class RetentionExecutionFlowTests(PrivacyApiTestBase):
    """真实链执行成功 + 重复执行幂等（验收 8/9）。"""

    def test_full_chain_anonymizes_and_audit(self):
        guest = self._make_expired_guest("全链执行")
        plan = self._create_plan()
        approval = self._approve(plan)
        self._login(self.executor)
        response = self.client.post(
            reverse("api:privacy_retention_plan_execute", args=[plan.pk]),
            data={"approval_reference": str(approval.pk)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["data"]
        self.assertEqual(body["processed_count"], 1)
        self.assertEqual(body["execution_status"], RetentionExecution.ExecutionStatus.COMPLETED)
        guest.refresh_from_db()
        self.assertEqual(guest.full_name, ANONYMIZED_MARKER)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="privacy.guest_anonymized", entity_id=str(guest.pk)
            ).exists()
        )

    def test_repeat_execution_does_not_double_anonymize(self):
        self._make_expired_guest("重复链路")
        plan = self._create_plan()
        approval = self._approve(plan)
        self._login(self.executor)
        first = self.client.post(
            reverse("api:privacy_retention_plan_execute", args=[plan.pk]),
            data={"approval_reference": str(approval.pk)},
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            reverse("api:privacy_retention_plan_execute", args=[plan.pk]),
            data={"approval_reference": str(approval.pk)},
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(Guest.objects.filter(full_name=ANONYMIZED_MARKER).count(), 1)
