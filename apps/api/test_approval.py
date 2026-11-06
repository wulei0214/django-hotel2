# -*- coding: utf-8 -*-
"""P0-01 审批列表匿名读取 + P0-02 审批动作白名单 API 测试。

P0-01：匿名 401；已登录无任职/停用/他店员工 403；无 approval_view 403；
有权限 200；列表按物业过滤且无审批权者仅见本人发起；审批动作对象级物业校验
（跨物业 404，不泄露存在性）。

P0-02 §7：任意 action_code、错 entity_type、跨物业对象、重复请求、申请人自批、
过期审批、重复执行；未知动作 400 且不写 ApprovalRecord / AuditEvent。
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import (
    ApprovalRecord, Folio, Payment, Refund,
)
from apps.billing.services_v2 import ApprovalService
from apps.core.models import AuditEvent, BusinessDate, Property
from apps.guests.models import Guest
from apps.identity.models import (
    PropertyStaffMembership, StaffProfile, StaffRoleAssignment,
)
from apps.reservations.models import Reservation


class ApprovalApiTestBase(TestCase):
    """DEFAULT 物业 + 员工账号工厂 + JSON 助手。"""

    maxDiff = None

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.other_property = Property.objects.create(
            code="APX2", name="审批 API 测试他店", status="ACTIVE")
        self._seq = 0

    # -- 员工工厂（对齐 e2e_flows 身份链） ------------------------------
    def _make_staff(self, username, group_name=None, property_obj=None,
                    is_active=True):
        user = User.objects.create_user(
            username=username, password=f"{username}-pass123",
            is_active=is_active)
        profile = StaffProfile.objects.create(
            user=user, full_name=username,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=property_obj or self.property,
            employee_no=f"APIT-{username}",
            status=PropertyStaffMembership.Status.ACTIVE)
        if group_name:
            group = Group.objects.get(name=group_name)
            StaffRoleAssignment.objects.create(
                membership=membership, group=group,
                starts_at=timezone.now() - timedelta(minutes=1))
        return user

    def _make_group_with_permission(self, name, app_label, codename):
        group, _ = Group.objects.get_or_create(name=name)
        permission = Permission.objects.get(
            content_type__app_label=app_label, codename=codename)
        group.permissions.add(permission)
        return group

    # -- JSON 助手 ------------------------------------------------------
    def _post_json(self, path, body):
        return self.client.post(
            path, data=json.dumps(body), content_type="application/json")

    # -- 业务对象工厂 ----------------------------------------------------
    def _make_refund(self, property_obj, requester, *, status=Refund.Status.PENDING,
                     booking_suffix=""):
        self._seq += 1
        seq = self._seq
        guest = Guest.objects.create(
            property=property_obj, full_name=f"审批API客人{seq}",
            phone=f"13800009{seq:03d}", phone_normalized=f"13800009{seq:03d}")
        today = timezone.localdate()
        reservation = Reservation.objects.create(
            property=property_obj, booking_no=f"APIT{seq:06d}{booking_suffix}",
            guest=guest, status=Reservation.Status.CONFIRMED,
            check_in=today, check_out=today + timedelta(days=1),
            room_count=1, adult_count=1, total_amount=Decimal("500.00"))
        folio = Folio.objects.create(
            property=property_obj, reservation=reservation,
            guest=guest, currency="CNY")
        payment = Payment.objects.create(
            property=property_obj, reservation=reservation, folio=folio,
            provider="frontdesk_manual", provider_ref=f"APIT-PAY-{seq:06d}",
            method="CASH", amount=Decimal("300.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        from apps.billing.services_v2 import BillingService
        if status == Refund.Status.PENDING:
            # 本测试覆盖通用审批申请 API；退款本身先保持 PENDING 且不预建审批记录。
            return Refund.objects.create(
                property=property_obj, payment=payment,
                amount=Decimal("100.00"), currency="CNY",
                reason="审批API退款原因",
                idempotency_key=f"apit-rf-{seq:06d}",
                provider_ref=f"APIT-RFD-{seq:06d}",
                requested_by=requester)
        refund, _ = BillingService.request_refund(
            property_obj=property_obj, payment_id=payment.pk,
            amount="100.00", reason="审批API退款原因",
            idempotency_key=f"apit-rf-{seq:06d}", requested_by=requester)
        approver = User.objects.create_user(username=f"apit-approver-{seq}")
        BillingService.approve_refund(
            property_obj=property_obj, refund_id=refund.pk,
            approved_by=approver)
        if status == Refund.Status.SUCCEEDED:
            executor = User.objects.create_user(username=f"apit-executor-{seq}")
            BillingService.open_shift(
                property_obj=property_obj, cashier=executor,
                opening_cash=Decimal("100.00"))
            BillingService.execute_refund(
                property_obj=property_obj, refund_id=refund.pk,
                executed_by=executor, confirm_refund_paid=True,
                idempotency_key=f"apit-exec-{seq:06d}")
        return refund

    def _make_business_date(self, property_obj, *, status=BusinessDate.Status.CLOSED):
        self._seq += 1
        return BusinessDate.objects.create(
            property=property_obj,
            business_date=timezone.localdate() + timedelta(days=self._seq),
            status=status,
            closed_at=timezone.now() if status == BusinessDate.Status.CLOSED else None)

    def _make_approval(self, property_obj, requester, *, action_code="billing.refund",
                       entity_type="Refund", entity_id="9001", reason="测试审批记录"):
        payload = ({"payment_id": 1, "amount": "100.00", "currency": "CNY"}
                   if action_code == "billing.refund" else None)
        record, _ = ApprovalService.request_approval(
            property_obj=property_obj, action_code=action_code,
            entity_type=entity_type, entity_id=entity_id,
            requested_by=requester, reason=reason, payload=payload)
        return record


class ApprovalListAccessTests(ApprovalApiTestBase):
    """P0-01：审批列表认证、任职、权限与可见范围。"""

    def setUp(self):
        super().setUp()
        self.owner = self._make_staff("ap_owner", "店长")
        self.finance = self._make_staff("ap_finance", "财务")
        self.frontdesk = self._make_staff("ap_fd", "前台")
        self.plain = self._make_staff("ap_plain", None)
        self.record_by_owner = self._make_approval(
            self.property, self.owner, entity_id="9101")
        self.record_by_finance = self._make_approval(
            self.property, self.finance, entity_id="9102")
        self.record_other_property = self._make_approval(
            self.other_property, self.owner, entity_id="9103")

    def test_anonymous_cannot_list_approvals(self):
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")
        self.assertNotIn("data", response.json())

    def test_logged_in_without_membership_gets_403(self):
        stranger = self._make_staff("ap_no_member", None)
        self.client.force_login(stranger)
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "PERMISSION_DENIED")
        self.assertNotIn("data", response.json())

    def test_inactive_staff_cannot_list(self):
        inactive = self._make_staff("ap_inactive", "店长", is_active=False)
        self.client.force_login(inactive)
        response = self.client.get("/api/v1/approvals")
        # Django 会话认证把停用账号视为匿名 → 401；不泄露任何记录
        self.assertIn(response.status_code, (401, 403))
        self.assertNotIn("data", response.json())

    def test_other_property_staff_gets_403(self):
        other_staff = self._make_staff(
            "ap_other", "店长", property_obj=self.other_property)
        self.client.force_login(other_staff)
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertNotIn("data", response.json())

    def test_membership_without_approval_view_permission_gets_403(self):
        self.client.force_login(self.plain)
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertNotIn("data", response.json())

    def test_frontdesk_without_approval_view_permission_gets_403(self):
        # 前台有有效任职与 refund_request，但未授予只读权限 billing.approval_view
        self.client.force_login(self.frontdesk)
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertNotIn("data", response.json())

    def test_manager_with_permission_lists_property_records(self):
        self.client.force_login(self.owner)
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        ids = [item["id"] for item in payload["data"]]
        self.assertIn(self.record_by_owner.pk, ids)
        self.assertIn(self.record_by_finance.pk, ids)
        # 审批权持有者可见全部，但仅限当前物业
        self.assertNotIn(self.record_other_property.pk, ids)
        self.assertIn("pagination", payload["meta"])

    def test_finance_without_approver_permission_sees_only_own_requests(self):
        # 财务持有 approval_view 但无任一审批权 → 仅见本人发起
        self.client.force_login(self.finance)
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 200, response.content)
        ids = [item["id"] for item in response.json()["data"]]
        self.assertIn(self.record_by_finance.pk, ids)
        self.assertNotIn(self.record_by_owner.pk, ids)
        self.assertNotIn(self.record_other_property.pk, ids)

    def test_status_filter_is_respected(self):
        self.client.force_login(self.owner)
        response = self.client.get("/api/v1/approvals?status=PENDING")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(
            item["status"] == "PENDING" for item in response.json()["data"]))


class ApprovalRequestWhitelistTests(ApprovalApiTestBase):
    """P0-02：/approvals/request 白名单、entity 校验与零副作用。"""

    def setUp(self):
        super().setUp()
        self.owner = self._make_staff("apr_owner", "店长")
        self.finance = self._make_staff("apr_finance", "财务")
        self.frontdesk = self._make_staff("apr_fd", "前台")
        self.plain = self._make_staff("apr_plain", None)
        self.refund = self._make_refund(self.property, self.frontdesk)

    def _approval_request(self, **overrides):
        body = {
            "action_code": "billing.refund",
            "entity_type": "Refund",
            "entity_id": str(self.refund.pk),
            "reason": "客人重复扣款，协商退款",
            "payload": {"payment_id": self.refund.payment_id,
                        "amount": "100.00", "currency": "CNY"},
        }
        body.update(overrides)
        return self._post_json("/api/v1/approvals/request", body)

    def _audit_count(self):
        return AuditEvent.objects.filter(action="approval.request").count()

    def test_anonymous_cannot_request_approval(self):
        before = ApprovalRecord.objects.count()
        response = self._approval_request()
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(ApprovalRecord.objects.count(), before)

    def test_valid_request_creates_pending_record_with_audit(self):
        self.client.force_login(self.frontdesk)
        response = self._approval_request()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["data"]["created"])
        record = ApprovalRecord.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(record.action_code, "billing.refund")
        self.assertEqual(record.status, ApprovalRecord.Status.PENDING)
        self.assertEqual(record.requested_by_id, self.frontdesk.pk)
        self.assertEqual(self._audit_count(), 1)

    def test_duplicate_request_is_idempotent(self):
        self.client.force_login(self.frontdesk)
        first = self._approval_request()
        self.assertEqual(first.status_code, 201)
        replay = self._approval_request()
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertFalse(replay.json()["data"]["created"])
        self.assertEqual(ApprovalRecord.objects.count(), 1)
        self.assertEqual(self._audit_count(), 1)

    def test_unknown_action_code_returns_400_without_side_effects(self):
        self.client.force_login(self.frontdesk)
        before = ApprovalRecord.objects.count()
        response = self._approval_request(action_code="billing.void_everything")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(ApprovalRecord.objects.count(), before)
        self.assertEqual(self._audit_count(), 0)

    def test_internal_only_action_is_rejected_on_generic_endpoint(self):
        self.client.force_login(self.owner)
        before = ApprovalRecord.objects.count()
        response = self._approval_request(action_code="privacy.export_staff",
                                          entity_type="StaffExport",
                                          entity_id="a" * 64)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ApprovalRecord.objects.count(), before)
        self.assertEqual(self._audit_count(), 0)

    def test_wrong_entity_type_returns_400(self):
        self.client.force_login(self.frontdesk)
        before = ApprovalRecord.objects.count()
        response = self._approval_request(entity_type="Payment")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ApprovalRecord.objects.count(), before)
        self.assertEqual(self._audit_count(), 0)

    def test_request_permission_is_enforced(self):
        # 无任何岗位权限的任职员工 → 403
        self.client.force_login(self.plain)
        response = self._approval_request()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self._audit_count(), 0)
        # 店长无 refund_request 申请权 → 403
        self.client.force_login(self.owner)
        response = self._approval_request()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self._audit_count(), 0)

    def test_nonexistent_entity_returns_404(self):
        self.client.force_login(self.frontdesk)
        response = self._approval_request(entity_id="999999")
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(self._audit_count(), 0)

    def test_cross_property_entity_returns_404_without_leak(self):
        other_refund = self._make_refund(self.other_property, self.frontdesk)
        self.client.force_login(self.frontdesk)
        response = self._approval_request(entity_id=str(other_refund.pk))
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(ApprovalRecord.objects.count(), 0)
        self.assertEqual(self._audit_count(), 0)

    def test_refund_of_other_requester_returns_403(self):
        # 退款申请人是前台；财务不能为他人退款补建审批
        self.client.force_login(self.finance)
        response = self._approval_request()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(ApprovalRecord.objects.count(), 0)

    def test_refund_in_final_status_returns_409(self):
        done = self._make_refund(
            self.property, self.frontdesk, status=Refund.Status.SUCCEEDED)
        self.client.force_login(self.frontdesk)
        response = self._approval_request(entity_id=str(done.pk))
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(self._audit_count(), 0)

    def test_payload_outside_whitelist_returns_400(self):
        self.client.force_login(self.frontdesk)
        response = self._approval_request(payload={"operator_note": "注入字段"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ApprovalRecord.objects.count(), 0)
        self.assertEqual(self._audit_count(), 0)

    def test_payload_sensitive_value_returns_400(self):
        self.client.force_login(self.frontdesk)
        response = self._approval_request(
            payload={"payment_id": "110101199001011234", "amount": "100.00",
                     "currency": "CNY"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ApprovalRecord.objects.count(), 0)
        self.assertEqual(self._audit_count(), 0)

    def test_business_date_reopen_via_generic_endpoint(self):
        duty = self._make_staff("apr_duty", "值班经理")
        bd = self._make_business_date(self.property)
        self.client.force_login(duty)
        response = self._post_json("/api/v1/approvals/request", {
            "action_code": "night_audit.reopen",
            "entity_type": "BusinessDate",
            "entity_id": str(bd.pk),
            "reason": "夜审后需要补记漏记凭证",
            "payload": {"business_date": bd.business_date.isoformat()},
        })
        self.assertEqual(response.status_code, 201, response.content)
        record = ApprovalRecord.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(record.action_code, "night_audit.reopen")
        self.assertEqual(record.entity_type, "BusinessDate")

    def test_open_business_date_cannot_request_reopen(self):
        duty = self._make_staff("apr_duty2", "值班经理")
        bd = self._make_business_date(
            self.property, status=BusinessDate.Status.OPEN)
        self.client.force_login(duty)
        response = self._post_json("/api/v1/approvals/request", {
            "action_code": "night_audit.reopen",
            "entity_type": "BusinessDate",
            "entity_id": str(bd.pk),
            "reason": "营业中不可申请重开",
        })
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(self._audit_count(), 0)


class ApprovalDecisionTests(ApprovalApiTestBase):
    """P0-01/P0-02：审批/驳回对象级物业校验、SoD、过期与重复执行。"""

    def setUp(self):
        super().setUp()
        self.owner = self._make_staff("apd_owner", "店长")
        self.duty = self._make_staff("apd_duty", "值班经理")
        self.finance = self._make_staff("apd_finance", "财务")
        self.record = self._make_approval(
            self.property, self.finance, entity_id="9201")

    def _approve(self, record_id):
        return self._post_json(f"/api/v1/approvals/{record_id}/approve", {})

    def _reject(self, record_id, reason="驳回：凭证不符"):
        return self._post_json(
            f"/api/v1/approvals/{record_id}/reject", {"reason": reason})

    def test_anonymous_cannot_approve(self):
        response = self._approve(self.record.pk)
        self.assertEqual(response.status_code, 401, response.content)

    def test_cross_property_record_returns_404(self):
        other_record = self._make_approval(
            self.other_property, self.finance, entity_id="9202")
        self.client.force_login(self.owner)
        response = self._approve(other_record.pk)
        self.assertEqual(response.status_code, 404, response.content)
        other_record.refresh_from_db()
        self.assertEqual(other_record.status, ApprovalRecord.Status.PENDING)

    def test_nonexistent_record_returns_404(self):
        self.client.force_login(self.owner)
        response = self._approve(999999)
        self.assertEqual(response.status_code, 404, response.content)

    def test_approve_requires_registry_approve_permission(self):
        # 财务有 approval_view 但无 billing.refund_approve → 403
        self.client.force_login(self.finance)
        response = self._approve(self.record.pk)
        self.assertEqual(response.status_code, 403, response.content)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ApprovalRecord.Status.PENDING)

    def test_approve_permission_follows_action_registry(self):
        # 仅持有 billing.refund_approve 的账号不能审批 night_audit.reopen 记录
        reopen_record = self._make_approval(
            self.property, self.finance, action_code="night_audit.reopen",
            entity_type="BusinessDate", entity_id="1")
        refund_only_group = self._make_group_with_permission(
            "AP-仅退款审批", "billing", "refund_approve")
        user = self._make_staff("apd_refund_only", None)
        membership = PropertyStaffMembership.objects.get(
            staff_profile__user=user)
        StaffRoleAssignment.objects.create(
            membership=membership, group=refund_only_group,
            starts_at=timezone.now() - timedelta(minutes=1))
        self.client.force_login(user)
        response = self._approve(reopen_record.pk)
        self.assertEqual(response.status_code, 403, response.content)
        # 值班经理持有 hotelapp.business_date_reopen → 200
        self.client.force_login(self.duty)
        response = self._approve(reopen_record.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["data"]["status"], ApprovalRecord.Status.APPROVED)

    def test_requester_cannot_self_approve(self):
        # 值班经理同时持有申请与审批权限（business_date_reopen）→ SoD 拦截
        duty = self._make_staff("apd_duty_req", "值班经理")
        bd = self._make_business_date(self.property)
        self.client.force_login(duty)
        created = self._post_json("/api/v1/approvals/request", {
            "action_code": "night_audit.reopen",
            "entity_type": "BusinessDate",
            "entity_id": str(bd.pk),
            "reason": "自批拦截测试",
        })
        self.assertEqual(created.status_code, 201, created.content)
        record_id = created.json()["data"]["id"]
        response = self._approve(record_id)
        self.assertEqual(response.status_code, 409, response.content)
        record = ApprovalRecord.objects.get(pk=record_id)
        self.assertEqual(record.status, ApprovalRecord.Status.PENDING)

    def test_approve_then_repeated_execution_conflicts(self):
        self.client.force_login(self.duty)
        first = self._approve(self.record.pk)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(
            first.json()["data"]["status"], ApprovalRecord.Status.APPROVED)
        second = self._approve(self.record.pk)
        self.assertEqual(second.status_code, 409, second.content)

    def test_expired_approval_is_marked_expired_on_approve(self):
        type(self.record)._base_manager.filter(pk=self.record.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1))
        self.client.force_login(self.owner)
        response = self._approve(self.record.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["data"]["status"], ApprovalRecord.Status.EXPIRED)

    def test_reject_updates_status_and_audits(self):
        self.client.force_login(self.duty)
        response = self._reject(self.record.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["data"]["status"], ApprovalRecord.Status.REJECTED)
        # 审批事实审计以 entity_type/entity_id（退款申请）登记
        self.assertTrue(AuditEvent.objects.filter(
            action="approval.reject", entity_type=self.record.entity_type,
            entity_id=self.record.entity_id,
            property=self.property).exists())

    def test_reject_cross_property_record_returns_404(self):
        other_record = self._make_approval(
            self.other_property, self.finance, entity_id="9203")
        self.client.force_login(self.duty)
        response = self._reject(other_record.pk)
        self.assertEqual(response.status_code, 404, response.content)
