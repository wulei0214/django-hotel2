"""营业日重开闭环审查回归（61-review-db-closure）。

覆盖：
  * D-01：重开申请被驳回/过期后，同一申请人必须能重新提交申请，
    不能被审批幂等键永久阻挡（§4.2 闭环"双人申请重开"）；
  * D-02：营业日再次关账后，旧的已批准审批不得重放驱动第二次重开；
  * 待审批申请的幂等重放契约保持不变。
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import ApprovalRecord
from apps.billing.night_audit import NightAuditError, NightAuditService
from apps.billing.services_v2 import ApprovalService
from apps.core.models import BusinessDate, Property


class ReopenReRequestTests(TestCase):
    """拒绝/过期后的重开申请必须可以重新发起（D-01/D-02）。"""

    def setUp(self):
        self.property = Property.objects.create(code="RR", name="重开回归酒店")
        self.day = timezone.localdate() - timedelta(days=1)
        self.bd = BusinessDate.objects.create(
            property=self.property, business_date=self.day,
            status=BusinessDate.Status.CLOSED)
        self.requester = User.objects.create_user(username="rr-requester")
        self.approver = User.objects.create_user(username="rr-approver")

    def _request(self, reason="夜审后发现漏记房费需要补记凭证"):
        return NightAuditService.request_reopen(
            property_obj=self.property, business_date_id=self.bd.pk,
            requested_by=self.requester, reason=reason)

    def test_rejected_reopen_request_can_be_resubmitted(self):
        approval, created = self._request()
        self.assertTrue(created)
        ApprovalService.reject(
            approval.pk, approved_by=self.approver,
            property_obj=self.property, reason="证据不足驳回")

        resubmitted, created2 = self._request(reason="补充证据后再次申请重开")
        self.assertTrue(created2)
        self.assertNotEqual(resubmitted.pk, approval.pk)
        self.assertEqual(resubmitted.status, ApprovalRecord.Status.PENDING)

        # 审批人通过夜审专用审批入口批准重开（PENDING → 本次批准）
        reopened = NightAuditService.approve_reopen(
            property_obj=self.property, business_date_id=self.bd.pk,
            approval_id=resubmitted.pk, approved_by=self.approver)
        self.assertEqual(reopened.status, BusinessDate.Status.REOPENED)
        resubmitted.refresh_from_db()
        self.assertEqual(resubmitted.status, ApprovalRecord.Status.APPROVED)
        self.assertEqual(resubmitted.approved_by_id, self.approver.pk)

    def test_expired_reopen_request_can_be_resubmitted(self):
        approval, _ = self._request()
        type(approval)._base_manager.filter(pk=approval.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1))
        expired = ApprovalService.approve(
            approval.pk, approved_by=self.approver, property_obj=self.property)
        self.assertEqual(expired.status, ApprovalRecord.Status.EXPIRED)

        resubmitted, created = self._request(reason="审批超时后重新申请重开")
        self.assertTrue(created)
        self.assertNotEqual(resubmitted.pk, approval.pk)
        self.assertEqual(resubmitted.status, ApprovalRecord.Status.PENDING)

    def test_pending_reopen_request_replay_is_still_idempotent(self):
        approval, created = self._request()
        self.assertTrue(created)
        replay, replay_created = self._request()
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, approval.pk)

    def test_stale_approved_reopen_approval_cannot_replay_after_reclose(self):
        approval, _ = self._request()
        NightAuditService.approve_reopen(
            property_obj=self.property, business_date_id=self.bd.pk,
            approval_id=approval.pk, approved_by=self.approver)
        self.bd.refresh_from_db()
        self.assertEqual(self.bd.status, BusinessDate.Status.REOPENED)

        # 模拟营业日再次夜审关账
        self.bd.status = BusinessDate.Status.CLOSED
        self.bd.save(update_fields=("status",))

        with self.assertRaises(NightAuditError):
            NightAuditService.approve_reopen(
                property_obj=self.property, business_date_id=self.bd.pk,
                approval_id=approval.pk, approved_by=self.approver)
        self.bd.refresh_from_db()
        self.assertEqual(self.bd.status, BusinessDate.Status.CLOSED)


class ApprovalTerminalReRequestTests(TestCase):
    """D-01 服务级通用契约：终态审批不再阻挡同一申请人重新申请。"""

    def setUp(self):
        self.property = Property.objects.create(code="AT", name="审批回归酒店")
        self.requester = User.objects.create_user(username="at-requester")
        self.approver = User.objects.create_user(username="at-approver")

    def _request(self, entity_id=501):
        return ApprovalService.request_approval(
            property_obj=self.property, action_code="billing.reversal",
            entity_type="LedgerEntry", entity_id=entity_id,
            requested_by=self.requester, reason="重复扣款需要红字冲销")

    def test_rejected_approval_does_not_block_new_request(self):
        first, created = self._request()
        self.assertTrue(created)
        ApprovalService.reject(
            first.pk, approved_by=self.approver,
            property_obj=self.property, reason="凭证核对未通过")

        second, created2 = self._request()
        self.assertTrue(created2)
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(second.status, ApprovalRecord.Status.PENDING)

    def test_cancelled_approval_does_not_block_new_request(self):
        first, _ = self._request(entity_id=601)
        type(first)._base_manager.filter(pk=first.pk).update(
            status=ApprovalRecord.Status.CANCELLED)

        second, created = self._request(entity_id=601)
        self.assertTrue(created)
        self.assertNotEqual(second.pk, first.pk)

    def test_pending_approval_still_replays_idempotently(self):
        first, created = self._request(entity_id=701)
        self.assertTrue(created)
        replay, replay_created = self._request(entity_id=701)
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, first.pk)
