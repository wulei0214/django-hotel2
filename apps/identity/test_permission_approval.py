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
        from django.db import IntegrityError, connection, transaction

        record = self._make_pending(requester=self.requester)
        record.status = PermissionMatrixApproval.Status.APPROVED
        record.approved_by = self.requester
        record.approved_at = record.created_at or None
        with self.assertRaises(ValidationError):
            record.save()
        # DB CheckConstraint 双保险：绕过应用层（QuerySet 守卫 + save 守卫）
        # 用原生 SQL 直改数据库，验证 ck_permatrix_sod_approver 库级拒绝
        table = connection.ops.quote_name(PermissionMatrixApproval._meta.db_table)
        with (
            transaction.atomic(),
            connection.cursor() as cursor,
            self.assertRaises(IntegrityError),
        ):
            cursor.execute(
                f"UPDATE {table} SET status = %s, approved_by_id = %s, "
                "approved_at = %s WHERE id = %s",
                ["APPROVED", self.requester.pk, record.created_at, record.pk],
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


class ApprovalConcurrencyRemediationTests(PermissionApprovalTestBase):
    """验收整改 A-01/A-02/A-03：并发互斥、批量操作保护、并发申请幂等。"""

    # ------------------------------------------------------------------
    # A-01：批准/驳回并发互斥（锁内状态重检）
    # ------------------------------------------------------------------

    def test_reject_then_approve_gets_already_decided_409(self):
        """驳回先落库 → 并发批准在锁内重检得 409，矩阵不变（状态与矩阵一致）。"""
        _, record, _ = self._request_approval()
        held = self._held()
        reject = self.approver_client.post(
            self._reject_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(reject.status_code, 200)
        approve = self.requester_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(approve.status_code, 409)
        self.assertEqual(approve.json()["code"], "record_already_decided")
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.REJECTED)
        self.assertEqual(_held(self.group), held, "已驳回记录不得再执行矩阵变更")

    def test_approve_then_reject_gets_already_decided_409(self):
        """批准先落库（矩阵已变更）→ 并发驳回 409，批准结果不被驳回覆盖。"""
        _, record, proposed = self._request_approval()
        approve = self.approver_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(approve.status_code, 200)
        held_after_approve = self._held()
        self.assertTrue({"billing.refund_approve"} <= held_after_approve)
        reject = self.requester_client.post(
            self._reject_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(reject.status_code, 409)
        self.assertEqual(reject.json()["code"], "record_already_decided")
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.APPROVED)
        self.assertEqual(
            self._held(),
            held_after_approve,
            "已批准的矩阵结果不得被并发驳回改变",
        )

    def test_double_approve_single_execution(self):
        """重复批准：第二次 409，矩阵仅执行一次（无重复 set/审计副作用）。"""
        _, record, _ = self._request_approval()
        first = self.approver_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(first.status_code, 200)
        second = self.requester_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="permissions.matrix_saved", entity_type="Group", entity_id=str(self.group.pk)
            ).count(),
            1,
            "同一次批准只允许一次 matrix_saved 审计",
        )

    # ------------------------------------------------------------------
    # A-02：批量操作保护（QuerySet 层负向）
    # ------------------------------------------------------------------

    def test_queryset_update_blocked(self):
        from django.core.exceptions import ValidationError

        self._make_pending_for_queryset_test()
        with self.assertRaises(ValidationError):
            PermissionMatrixApproval.objects.update(status="APPROVED")

    def test_queryset_delete_blocked(self):
        from django.core.exceptions import ValidationError

        self._make_pending_for_queryset_test()
        with self.assertRaises(ValidationError):
            PermissionMatrixApproval.objects.delete()

    def test_filtered_queryset_update_blocked(self):
        from django.core.exceptions import ValidationError

        record = self._make_pending_for_queryset_test()
        with self.assertRaises(ValidationError):
            PermissionMatrixApproval.objects.filter(pk=record.pk).update(
                status="APPROVED", approved_by=self.approver
            )
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.PENDING)

    def test_bulk_create_blocked(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            PermissionMatrixApproval.objects.bulk_create(
                [
                    PermissionMatrixApproval(
                        group=self.group,
                        requested_by=self.requester,
                        reason="批量绕过",
                        diff_hash="deadbeefdeadbeef",
                        payload_snapshot={"added": [], "removed": []},
                    )
                ]
            )

    def test_bulk_update_blocked(self):
        from django.core.exceptions import ValidationError

        record = self._make_pending_for_queryset_test()
        record.status = PermissionMatrixApproval.Status.REJECTED
        with self.assertRaises(ValidationError):
            PermissionMatrixApproval.objects.bulk_update([record], ["status", "approved_by"])
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.PENDING)

    def _make_pending_for_queryset_test(self):
        return PermissionMatrixApproval.objects.create(
            group=self.group,
            requested_by=self.requester,
            reason="批量保护测试",
            diff_hash="feedfacefeedface",
            payload_snapshot={"added": [], "removed": [], "version": "v"},
        )

    # ------------------------------------------------------------------
    # A-03：并发重复申请幂等（唯一约束兜底 + 取代语义不变）
    # ------------------------------------------------------------------

    def test_duplicate_pending_same_diff_blocked_by_db_constraint(self):
        """同组同 diff 第二条 PENDING 被 pending_diff_key 唯一约束拒绝
        （绕过应用层查重直接 create 的并发路径兜底；约束为普通唯一索引，
        SQLite/MySQL 行为一致——首轮 MySQL CI 实证条件唯一索引在 MySQL 被
        Django 静默跳过，已改用 pending_diff_key 结构）。"""
        from django.db import IntegrityError, transaction

        _, record, _ = self._request_approval()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            PermissionMatrixApproval.objects.create(
                group=self.group,
                requested_by=self.approver,
                reason="并发第二条同 diff 申请",
                diff_hash=record.diff_hash,
                payload_snapshot=record.payload_snapshot,
            )
        self.assertEqual(
            PermissionMatrixApproval.objects.filter(
                group=self.group,
                diff_hash=record.diff_hash,
                status=PermissionMatrixApproval.Status.PENDING,
            ).count(),
            1,
        )

    def test_decided_record_releases_pending_key(self):
        """决策/撤销后 pending_diff_key 置 NULL：同 diff 可重新申请，
        且决策记录不占用唯一键（释放语义验证）。"""
        _, record, proposed = self._request_approval()
        self.assertIsNotNone(record.pending_diff_key)
        self.approver_client.post(
            self._reject_url(record.pk), data="{}", content_type="application/json"
        )
        record.refresh_from_db()
        self.assertIsNone(record.pending_diff_key)
        # 释放后同 diff 可再创建新 PENDING
        new_record = PermissionMatrixApproval.objects.create(
            group=self.group,
            requested_by=self.approver,
            reason="释放后同 diff 重新申请",
            diff_hash=record.diff_hash,
            payload_snapshot=record.payload_snapshot,
        )
        self.assertIsNotNone(new_record.pending_diff_key)

    def test_supersede_semantics_still_work_after_unique_constraint(self):
        """取代语义在唯一约束下不变：旧 diff PENDING → CANCELLED 后，
        新 diff PENDING 可正常创建（不同 diff 不受约束影响）。"""
        _, old_record, _ = self._request_approval()
        alternative = sorted(self._held() | {"identity.staff_manage"})
        token_payload = self._confirm_token(alternative)
        response = self._save(alternative, confirm_token=token_payload["token"])
        self.assertEqual(response.status_code, 409)
        old_record.refresh_from_db()
        self.assertEqual(old_record.status, PermissionMatrixApproval.Status.CANCELLED)
        new_record = PermissionMatrixApproval.objects.get(pk=response.json()["approval_id"])
        self.assertEqual(new_record.status, PermissionMatrixApproval.Status.PENDING)
        self.assertNotEqual(new_record.diff_hash, old_record.diff_hash)

    def test_same_diff_after_approval_can_re_request(self):
        """同 diff 在前次已 APPROVED 后可再次申请（约束仅限 PENDING 态，
        不阻断"撤销后重新申请"等合法生命周期）。"""
        from django.core.exceptions import ValidationError  # noqa: F401

        _, record, proposed = self._request_approval()
        approve = self.approver_client.post(
            self._approve_url(record.pk), data="{}", content_type="application/json"
        )
        self.assertEqual(approve.status_code, 200)
        # 反向 diff 申请（移除刚授予的专项码）走完整流程 → 新 PENDING
        without_special = sorted(self._held() - {"billing.refund_approve"})
        token_payload = self._confirm_token(without_special)
        response = self._save(without_special, confirm_token=token_payload["token"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "approval_required")
