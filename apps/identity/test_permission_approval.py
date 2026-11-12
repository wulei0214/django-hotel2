"""权限矩阵高危变更第二人审批流测试（治理 V1.1 第三阶段迭代-1 变更 A，L4 已批）。

覆盖：approval_required 拦截与记录创建 / 幂等复用 / 新 diff 取代旧待审批 /
申请人自审批拒绝（应用层+DB 双层）/ 第二人批准原子执行 / 驳回不变更矩阵 /
版本漂移 409 且记录保持待审批 / 低危变更不受影响 / 非平台超管拒绝 / 审计断言。
"""

import json

from django.contrib.auth.models import Group, Permission
from django.test import Client, TestCase

from apps.core.models import AuditEvent

from .models import PermissionMatrixApproval
from .permission_center import allowed_codenames, special_codenames

_TEST_PW_APPROVAL = "pw-" + "a" * 20
_TEST_PW_APPROVER = "pw-" + "b" * 20

User = None  # 延迟到 setUp 解析（与 test_permission_center 一致使用 get_user_model）


def _get_user_model():
    from django.contrib.auth import get_user_model

    return get_user_model()


def _held(group):
    """组当前权限码全集（"app_label.codename"）。"""
    return {
        f"{a}.{c}" for a, c in group.permissions.values_list("content_type__app_label", "codename")
    }


class PermissionApprovalTestBase(TestCase):
    """公共夹具：申请人/审批人双平台超管 + 默认角色组（客房服务员）。"""

    def setUp(self):
        user_model = _get_user_model()
        self.requester = user_model.objects.create_superuser(
            "approval_requester", password=_TEST_PW_APPROVAL
        )
        self.approver = user_model.objects.create_superuser(
            "approval_approver", password=_TEST_PW_APPROVER
        )
        self.requester_client = Client()
        self.approver_client = Client()
        self._platform_login(self.requester_client, self.requester)
        self._platform_login(self.approver_client, self.approver)
        self.group = Group.objects.get(name="客房服务员")

    @staticmethod
    def _platform_login(client, user):
        """平台会域登录：force_login 后把会话键复制到两份平台 Cookie。"""
        client.force_login(user)
        sessionid = client.cookies["sessionid"].value
        client.cookies["platform_sessionid"] = sessionid
        client.cookies["django_admin_sessionid"] = sessionid

    def _held(self, group=None):
        return _held(group or self.group)

    def _proposed_with_special(self):
        """当前持有 ∪ 一个高危专项码（billing.refund_approve，finance 模块）。"""
        special = "billing.refund_approve"
        assert special in allowed_codenames()
        return sorted(self._held() | {special})

    def _proposed_low_risk(self):
        """当前持有 ∪ 一个低危非专项码（排除全部 SPECIAL，避免误入高危分支）。"""
        non_special = allowed_codenames() - special_codenames()
        return sorted(self._held() | {sorted(non_special - self._held())[0]})

    def _read_version(self):
        response = self.requester_client.get(f"/platform/api/permissions/{self.group.id}/")
        self.assertEqual(response.status_code, 200)
        return response.json()["version"]

    def _confirm_token(self, permissions):
        return self.requester_client.post(
            f"/platform/api/permissions/{self.group.id}/confirm-token/",
            data=json.dumps({"permissions": permissions}),
            content_type="application/json",
        ).json()

    def _save(self, permissions, version=None, confirm_token=None):
        version = version or self._read_version()
        payload = {
            "permissions": permissions,
            "version": version,
            "reason": "迭代-1 变更A 测试：高危矩阵变更",
        }
        if confirm_token is not None:
            payload["confirm_token"] = confirm_token
        return self.requester_client.post(
            f"/platform/api/permissions/{self.group.id}/save/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _request_approval(self):
        """走完整高危申请链：confirm-token → save(带 token) → 409 + 记录。"""
        proposed = self._proposed_with_special()
        token_payload = self._confirm_token(proposed)
        response = self._save(proposed, confirm_token=token_payload["token"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "approval_required")
        record = PermissionMatrixApproval.objects.get(pk=response.json()["approval_id"])
        return response, record, proposed

    def _approve_url(self, record_id):
        return f"/platform/api/permissions/{self.group.id}/" f"approvals/{record_id}/approve/"

    def _reject_url(self, record_id):
        return f"/platform/api/permissions/{self.group.id}/" f"approvals/{record_id}/reject/"


class PermissionApprovalFlowTests(PermissionApprovalTestBase):

    def test_high_risk_save_returns_approval_required_and_creates_record(self):
        held = self._held()
        response, record, proposed = self._request_approval()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.PENDING)
        self.assertEqual(record.requested_by, self.requester)
        self.assertEqual(record.payload_snapshot["added"], sorted(set(proposed) - held))
        # 矩阵未变更（申请不等于授权）
        self.assertEqual(_held(self.group), held)
        # 申请审计已落库
        self.assertTrue(
            AuditEvent.objects.filter(
                action="permissions.approval_requested",
                entity_type="Group",
                entity_id=str(self.group.pk),
            ).exists()
        )

    def test_same_diff_reuses_existing_pending_record(self):
        _, record_first, _ = self._request_approval()
        response = self.requester_client.post(
            f"/platform/api/permissions/{self.group.id}/save/",
            data=json.dumps(
                {
                    "permissions": self._proposed_with_special(),
                    "version": self._read_version(),
                    "reason": "幂等复用",
                    "confirm_token": self._confirm_token(self._proposed_with_special())["token"],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["approval_id"], record_first.pk)
        self.assertEqual(
            PermissionMatrixApproval.objects.filter(
                status=PermissionMatrixApproval.Status.PENDING
            ).count(),
            1,
        )

    def test_new_diff_supersedes_stale_pending_record(self):
        _, stale_record, _ = self._request_approval()
        # 换一个不同的高危 diff：撤销另一专项码
        alternative = sorted(self._held() | {"identity.staff_manage"})
        token_payload = self._confirm_token(alternative)
        response = self._save(alternative, confirm_token=token_payload["token"])
        self.assertEqual(response.status_code, 409)
        stale_record.refresh_from_db()
        self.assertEqual(stale_record.status, PermissionMatrixApproval.Status.CANCELLED)
        self.assertEqual(
            PermissionMatrixApproval.objects.filter(
                status=PermissionMatrixApproval.Status.PENDING
            ).count(),
            1,
        )

    def test_requester_cannot_self_approve(self):
        _, record, _ = self._request_approval()
        held = self._held()
        response = self.requester_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "sod_violation")
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.PENDING)
        self.assertEqual(_held(self.group), held)

    def test_second_superadmin_approval_applies_matrix_atomically(self):
        _, record, proposed = self._request_approval()
        response = self.approver_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.APPROVED)
        self.assertEqual(record.approved_by, self.approver)
        self.assertIsNotNone(record.approved_at)
        # 矩阵已在批准事务内生效（含新增的高危专项码）
        self.assertTrue({"billing.refund_approve"} <= _held(self.group))
        # 执行审计：matrix_saved 以审批人为 actor
        event = (
            AuditEvent.objects.filter(
                action="permissions.matrix_saved", entity_type="Group", entity_id=str(self.group.pk)
            )
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.actor, self.approver)

    def test_reject_keeps_matrix_unchanged(self):
        _, record, _ = self._request_approval()
        held = self._held()
        response = self.approver_client.post(
            self._reject_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["rejected"])
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.REJECTED)
        self.assertEqual(record.approved_by, self.approver)
        self.assertEqual(_held(self.group), held)
        self.assertTrue(AuditEvent.objects.filter(action="permissions.approval_rejected").exists())

    def test_version_drift_at_execute_returns_409_keeps_pending(self):
        _, record, proposed = self._request_approval()
        # 审批期间矩阵被他改（Admin 侧直接加一个低危码）→ version 漂移
        extra = allowed_codenames() - set(proposed) - self._held()
        code = sorted(extra)[0]
        app_label, codename = code.split(".", 1)
        permission = Permission.objects.get(content_type__app_label=app_label, codename=codename)
        self.group.permissions.add(permission)
        response = self.approver_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "version_conflict")
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.PENDING)

    def test_low_risk_diff_saves_directly_without_approval(self):
        proposed = self._proposed_low_risk()
        response = self._save(proposed)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["saved"])
        self.assertFalse(PermissionMatrixApproval.objects.exists())

    def test_non_superadmin_cannot_approve(self):
        _, record, _ = self._request_approval()
        user_model = _get_user_model()
        staffer = user_model.objects.create_user(username="pms-staff", password="pms-staff-pass123")
        staff_client = Client()
        self._platform_login(staff_client, staffer)
        response = staff_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertIn(response.status_code, (302, 403))

    def test_already_decided_record_cannot_be_reprocessed(self):
        _, record, _ = self._request_approval()
        self.approver_client.post(
            self._reject_url(record.pk), data="{}", content_type="application/json"
        )
        response = self.approver_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "record_already_decided")


class PermissionApprovalModelGuardTests(PermissionApprovalTestBase):

    def _make_pending(self, requester=None):
        record = PermissionMatrixApproval.objects.create(
            group=self.group,
            requested_by=requester or self.requester,
            reason="模型守卫测试",
            diff_hash="abc123def4567890",
            payload_snapshot={"added": [], "removed": [], "version": "v"},
        )
        return record

    def test_create_must_start_pending(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            PermissionMatrixApproval.objects.create(
                group=self.group,
                requested_by=self.requester,
                approved_by=self.approver,
                status="APPROVED",
                reason="越权创建",
                diff_hash="abc123def4567891",
                payload_snapshot={},
            )

    def test_self_approval_blocked_by_model_and_db(self):
        from django.core.exceptions import ValidationError
        from django.db import IntegrityError, transaction

        record = self._make_pending(requester=self.requester)
        record.status = PermissionMatrixApproval.Status.APPROVED
        record.approved_by = self.requester
        record.approved_at = record.created_at or None
        with self.assertRaises(ValidationError):
            record.save()
        # DB CheckConstraint 双保险：绕过应用层直接 update 也被库拒绝
        with transaction.atomic(), self.assertRaises(IntegrityError):
            PermissionMatrixApproval.objects.filter(pk=record.pk).update(
                status="APPROVED", approved_by=self.requester, approved_at=record.created_at
            )

    def test_decision_requires_approver_and_time(self):
        from django.core.exceptions import ValidationError

        record = self._make_pending()
        record.status = PermissionMatrixApproval.Status.APPROVED
        with self.assertRaises(ValidationError):
            record.save()

    def test_immutable_after_decision(self):
        from django.core.exceptions import ValidationError

        record = self._make_pending()
        record.status = PermissionMatrixApproval.Status.APPROVED
        record.approved_by = self.approver
        record.approved_at = record.created_at
        record.save()
        record.reason = "事后篡改理由"
        with self.assertRaises(ValidationError):
            record.save()

    def test_delete_forbidden(self):
        from django.db.models import ProtectedError  # noqa: F401

        record = self._make_pending()
        with self.assertRaises(RuntimeError):
            record.delete()
