"""TASK-03 退款双入口契约与资金安全测试。

入口矩阵（结论：保留双入口，不删除、不合并响应封装）：
  * 前台/值班经理：``front-desk/payments/<id>/refunds``、
    ``front-desk/refunds/<id>/approve|reject``；只校验专项权限，不要求
    ``hotelapp.finance_access``；没有 execute 端点。
  * 财务中心：``billing/refunds``（支持 ``refund_liability_id``）、
    ``approve|reject|execute``；强制 ``hotelapp.finance_access`` + 专项权限。
  * 通用审批面：``approvals/request`` 只能为既有 PENDING 退款补建审批；
    ``approvals/<id>/approve|reject`` 对 ``billing.refund`` 一律 409，并指向
    上述两个专用入口。

本文件只锁定可观测契约与资金安全不变量：
  1) 同一次退款申请只产生一条 Refund / ApprovalRecord / 审计事实；
  2) 跨入口重复审批、审批后驳回均不得迁移状态或追加审计；
  3) 执行人必须是第三人，且只有财务入口存在执行端点；
  4) 通用审批面对退款必须给出双入口指引，不得误导为单一路径。
"""

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.urls import Resolver404, resolve
from django.utils import timezone

from apps.api.test_approval import ApprovalApiTestBase
from apps.billing.models import (
    ApprovalRecord,
    Folio,
    LedgerEntry,
    Payment,
    Refund,
)
from apps.core.models import AuditEvent
from apps.guests.models import Guest
from apps.identity.models import PropertyStaffMembership, StaffRoleAssignment
from apps.reservations.models import Reservation


def _post(client, path, payload=None, idempotency_key=""):
    extra = {"HTTP_IDEMPOTENCY_KEY": idempotency_key} if idempotency_key else {}
    return client.post(
        path,
        data=json.dumps(payload or {}),
        content_type="application/json",
        **extra,
    )


class RefundDualEntryTestBase(ApprovalApiTestBase):
    """按最小权限面构造四个互不共享账号的岗位，避免角色默认授权掩盖 SoD。"""

    def setUp(self):
        super().setUp()
        self.seq = 0
        self.frontdesk = self._staff(
            "t03_frontdesk",
            [("hotelapp", "view_accommodation"), ("billing", "refund_request")],
        )
        self.manager = self._staff(
            "t03_manager",
            [("hotelapp", "view_accommodation"), ("billing", "refund_approve")],
        )
        self.finance_requester = self._staff(
            "t03_finance_requester",
            [("hotelapp", "finance_access"), ("billing", "refund_request")],
        )
        self.finance_approver = self._staff(
            "t03_finance_approver",
            [("hotelapp", "finance_access"), ("billing", "refund_approve")],
        )
        self.finance_executor = self._staff(
            "t03_finance_executor",
            [("hotelapp", "finance_access"), ("billing", "refund_execute")],
        )

    # -- 工厂 -----------------------------------------------------------
    def _staff(self, username, specs):
        user = self._make_staff(username)
        group = Group.objects.create(name=f"TASK03-{username}")
        for app_label, codename in specs:
            group.permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        StaffRoleAssignment.objects.create(
            membership=PropertyStaffMembership.objects.get(staff_profile__user=user),
            group=group,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        return user

    def _payment(self, amount=Decimal("300.00")):
        self.seq += 1
        seq = self.seq
        guest = Guest.objects.create(
            property=self.property,
            full_name=f"双入口客人{seq:03d}",
            phone=f"1370000{seq:04d}",
        )
        today = timezone.localdate()
        reservation = Reservation.objects.create(
            property=self.property,
            booking_no=f"T03{seq:07d}",
            guest=guest,
            status=Reservation.Status.CHECKED_IN,
            check_in=today,
            check_out=today + timedelta(days=1),
            room_count=1,
            adult_count=1,
            total_amount=amount,
        )
        folio = Folio.objects.create(
            property=self.property,
            reservation=reservation,
            guest=guest,
            currency="CNY",
        )
        return Payment.objects.create(
            property=self.property,
            reservation=reservation,
            folio=folio,
            provider="frontdesk_manual",
            provider_ref=f"T03-PAY-{seq:06d}",
            method="CARD",
            amount=amount,
            currency="CNY",
            status=Payment.Status.SUCCEEDED,
            paid_at=timezone.now(),
        )

    def _key(self, prefix):
        self.seq += 1
        return f"task03-{prefix}-{self.seq:04d}"

    def _grant(self, user, app_label, codename):
        permission = Permission.objects.get(content_type__app_label=app_label, codename=codename)
        StaffRoleAssignment.objects.get(membership__staff_profile__user=user).group.permissions.add(
            permission
        )

    # -- 入口动作 -------------------------------------------------------
    def _front_desk_request(self, payment, amount="100.00", key=None):
        key = key or self._key("front-request")
        self.client.force_login(self.frontdesk)
        response = _post(
            self.client,
            f"/api/v1/front-desk/payments/{payment.pk}/refunds",
            {"amount": amount, "reason": "TASK-03 前台入口申请"},
            key,
        )
        return response, key

    def _billing_request(self, payment, user, amount="100.00", key=None):
        key = key or self._key("billing-request")
        self.client.force_login(user)
        response = _post(
            self.client,
            "/api/v1/billing/refunds",
            {
                "payment_id": payment.pk,
                "amount": amount,
                "reason": "TASK-03 财务入口申请",
                "idempotency_key": key,
            },
            key,
        )
        return response, key

    def _audit_count(self, action, refund):
        return AuditEvent.objects.filter(
            action=action,
            entity_type="Refund",
            entity_id=refund.pk,
            property=self.property,
        ).count()


class RefundDualEntryContractTests(RefundDualEntryTestBase):
    def test_front_desk_and_billing_request_share_single_service_invariants(self):
        """两条申请入口都必须产出 PENDING + 一条 PENDING 审批 + 一条审计。"""
        payment = self._payment()
        response, key = self._front_desk_request(payment)
        self.assertEqual(response.status_code, 201, response.content)

        refund = Refund.objects.get(idempotency_key=key)
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(refund.requested_by_id, self.frontdesk.pk)
        approval = ApprovalRecord.objects.get(pk=refund.approval_record_id)
        self.assertEqual(approval.action_code, "billing.refund")
        self.assertEqual(approval.status, ApprovalRecord.Status.PENDING)
        self.assertEqual(self._audit_count("billing.refund_request", refund), 1)

        replay, _ = self._front_desk_request(payment, key=key)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(Refund.objects.filter(idempotency_key=key).count(), 1)
        self.assertEqual(
            ApprovalRecord.objects.filter(
                action_code="billing.refund", entity_id=str(refund.pk)
            ).count(),
            1,
        )
        self.assertEqual(self._audit_count("billing.refund_request", refund), 1)

    def test_front_desk_has_no_refund_execute_route(self):
        """执行只能走财务入口；前台入口即使被猜到路径也不得存在。"""
        with self.assertRaises(Resolver404):
            resolve("/api/v1/front-desk/refunds/1/execute")
        self.assertTrue(resolve("/api/v1/billing/refunds/1/execute"))

    def test_generic_approval_surface_points_to_both_refund_entries(self):
        """通用审批面 409 时必须同时给出前台与财务两个真实入口。"""
        payment = self._payment()
        _, key = self._front_desk_request(payment)
        refund = Refund.objects.get(idempotency_key=key)
        record = ApprovalRecord.objects.get(pk=refund.approval_record_id)

        self.client.force_login(self.manager)
        approve = _post(
            self.client,
            f"/api/v1/approvals/{record.pk}/approve",
            {},
            self._key("generic-approve"),
        )
        self.assertEqual(approve.status_code, 409, approve.content)
        message = approve.json()["error"]["message"]
        self.assertIn("/api/v1/front-desk/refunds/{id}/approve", message)
        self.assertIn("/api/v1/billing/refunds/{id}/approve", message)

        reject = _post(
            self.client,
            f"/api/v1/approvals/{record.pk}/reject",
            {"reason": "通用面驳回"},
            self._key("generic-reject"),
        )
        self.assertEqual(reject.status_code, 409, reject.content)
        message = reject.json()["error"]["message"]
        self.assertIn("/api/v1/front-desk/refunds/{id}/reject", message)
        self.assertIn("/api/v1/billing/refunds/{id}/reject", message)

        refund.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(record.status, ApprovalRecord.Status.PENDING)
        self.assertEqual(self._audit_count("billing.refund_approved", refund), 0)
        self.assertEqual(self._audit_count("billing.refund_rejected", refund), 0)


class RefundCrossEntrySafetyTests(RefundDualEntryTestBase):
    def test_front_desk_approve_cannot_be_duplicated_by_billing_entry(self):
        payment = self._payment()
        _, key = self._front_desk_request(payment)
        refund = Refund.objects.get(idempotency_key=key)

        self.client.force_login(self.manager)
        approved = _post(
            self.client,
            f"/api/v1/front-desk/refunds/{refund.pk}/approve",
            {},
            self._key("front-approve"),
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        self.assertEqual(refund.approved_by_id, self.manager.pk)
        self.assertEqual(self._audit_count("billing.refund_approved", refund), 1)

        # 另一名有财务审批权的人再经财务入口审批：必须 400，不得二次落审批。
        self.client.force_login(self.finance_approver)
        duplicate = _post(
            self.client,
            f"/api/v1/billing/refunds/{refund.pk}/approve",
            {},
            self._key("billing-approve-duplicate"),
        )
        self.assertEqual(duplicate.status_code, 400, duplicate.content)

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        self.assertEqual(refund.approved_by_id, self.manager.pk)
        self.assertEqual(self._audit_count("billing.refund_approved", refund), 1)

        # 原审批人重放前台入口：幂等返回 changed=False，仍不追加审计。
        self.client.force_login(self.manager)
        replay = _post(
            self.client,
            f"/api/v1/front-desk/refunds/{refund.pk}/approve",
            {},
            self._key("front-approve-replay"),
        )
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertFalse(replay.json()["changed"])
        self.assertEqual(self._audit_count("billing.refund_approved", refund), 1)

    def test_rejection_after_approval_cannot_change_state_across_entries(self):
        payment = self._payment()
        _, key = self._front_desk_request(payment)
        refund = Refund.objects.get(idempotency_key=key)

        self.client.force_login(self.manager)
        self.assertEqual(
            _post(
                self.client,
                f"/api/v1/front-desk/refunds/{refund.pk}/approve",
                {},
                self._key("front-approve"),
            ).status_code,
            200,
        )

        self.client.force_login(self.finance_approver)
        rejected = _post(
            self.client,
            f"/api/v1/billing/refunds/{refund.pk}/reject",
            {"reason": "审批后跨入口驳回"},
            self._key("billing-reject-after-approve"),
        )
        self.assertEqual(rejected.status_code, 400, rejected.content)

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        record = ApprovalRecord.objects.get(pk=refund.approval_record_id)
        self.assertEqual(record.status, ApprovalRecord.Status.APPROVED)
        self.assertEqual(self._audit_count("billing.refund_rejected", refund), 0)

    def test_billing_request_approved_by_manager_then_executed_by_third_person(self):
        """财务申请 → 值班经理（无 finance_access）前台入口审批 → 第三人财务执行。"""
        payment = self._payment()
        response, key = self._billing_request(payment, self.finance_requester)
        self.assertEqual(response.status_code, 201, response.content)
        refund = Refund.objects.get(idempotency_key=key)

        self.client.force_login(self.manager)
        approved = _post(
            self.client,
            f"/api/v1/front-desk/refunds/{refund.pk}/approve",
            {},
            self._key("front-approve"),
        )
        self.assertEqual(approved.status_code, 200, approved.content)

        execute_key = self._key("billing-execute")
        self.client.force_login(self.finance_executor)
        executed = _post(
            self.client,
            f"/api/v1/billing/refunds/{refund.pk}/execute",
            {
                "confirm_refund_paid": True,
                "execution_reference": f"T03-CHANNEL-{refund.pk}",
                "idempotency_key": execute_key,
            },
            execute_key,
        )
        self.assertEqual(executed.status_code, 201, executed.content)

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.SUCCEEDED)
        self.assertEqual(
            len({refund.requested_by_id, refund.approved_by_id, refund.executed_by_id}),
            3,
        )
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=f"refund:{refund.provider_ref}",
                entry_type=LedgerEntry.EntryType.REFUND,
            ).count(),
            1,
        )

        replay = _post(
            self.client,
            f"/api/v1/billing/refunds/{refund.pk}/execute",
            {
                "confirm_refund_paid": True,
                "execution_reference": f"T03-CHANNEL-{refund.pk}",
                "idempotency_key": execute_key,
            },
            execute_key,
        )
        self.assertIn(replay.status_code, (200, 201), replay.content)
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=f"refund:{refund.provider_ref}",
                entry_type=LedgerEntry.EntryType.REFUND,
            ).count(),
            1,
        )

    def test_same_account_cannot_request_then_approve_via_other_entry(self):
        payment = self._payment()
        response, key = self._billing_request(payment, self.finance_requester)
        self.assertEqual(response.status_code, 201, response.content)
        refund = Refund.objects.get(idempotency_key=key)

        # 申请人自批：财务审批权 + 申请人同一账号，经财务入口 → 400。
        self._grant(self.finance_requester, "billing", "refund_approve")
        self.client.force_login(self.finance_requester)
        self_approve = _post(
            self.client,
            f"/api/v1/billing/refunds/{refund.pk}/approve",
            {},
            self._key("self-approve"),
        )
        self.assertEqual(self_approve.status_code, 400, self_approve.content)

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(self._audit_count("billing.refund_approved", refund), 0)

    def test_approver_cannot_execute_and_executor_needs_finance_entry(self):
        payment = self._payment()
        _, key = self._billing_request(payment, self.finance_requester)
        refund = Refund.objects.get(idempotency_key=key)

        self.client.force_login(self.finance_approver)
        self.assertEqual(
            _post(
                self.client,
                f"/api/v1/billing/refunds/{refund.pk}/approve",
                {},
                self._key("billing-approve"),
            ).status_code,
            200,
        )

        # 审批人自己执行（同时具备执行权）→ 400，状态保持 APPROVED。
        self._grant(self.finance_approver, "billing", "refund_execute")
        self.client.force_login(self.finance_approver)
        self_execute_key = self._key("self-execute")
        self_execute = _post(
            self.client,
            f"/api/v1/billing/refunds/{refund.pk}/execute",
            {
                "confirm_refund_paid": True,
                "execution_reference": f"T03-SELF-{refund.pk}",
                "idempotency_key": self_execute_key,
            },
            self_execute_key,
        )
        self.assertEqual(self_execute.status_code, 400, self_execute.content)

        # 值班经理无 finance_access：即使尝试财务执行入口也必须 403。
        self.client.force_login(self.manager)
        denied_key = self._key("denied-execute")
        denied = _post(
            self.client,
            f"/api/v1/billing/refunds/{refund.pk}/execute",
            {
                "confirm_refund_paid": True,
                "execution_reference": f"T03-DENIED-{refund.pk}",
                "idempotency_key": denied_key,
            },
            denied_key,
        )
        self.assertEqual(denied.status_code, 403, denied.content)

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        self.assertFalse(
            LedgerEntry.objects.filter(reference=f"refund:{refund.provider_ref}").exists()
        )
