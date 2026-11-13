"""P0-02 审批动作注册表（ApprovalActionRegistry）与服务层白名单校验测试。

覆盖：未知 action_code、entity_type 错配、payload 白名单/长度/敏感值、
待实现动作不得注册、内部专用动作不对通用审批面开放。
"""

from django.test import TestCase

from apps.billing.approval_registry import (
    ApprovalActionRegistry,
    _is_sensitive_key,
    _is_sensitive_value,
)
from apps.billing.models import ApprovalRecord
from apps.billing.services_v2 import ApprovalService
from apps.core.models import AuditEvent, Property


class ApprovalRegistryCatalogTests(TestCase):
    """注册表目录本身的不变量。"""

    def test_registered_actions_cover_implemented_flows_only(self):
        self.assertIn("billing.refund", ApprovalActionRegistry.registered_codes())
        self.assertIn("billing.reversal", ApprovalActionRegistry.registered_codes())
        self.assertIn("night_audit.reopen", ApprovalActionRegistry.registered_codes())
        self.assertIn("privacy.export_staff", ApprovalActionRegistry.registered_codes())

    def test_reversal_is_registered_and_other_unimplemented_actions_stay_pending(self):
        reversal = ApprovalActionRegistry.get("billing.reversal")
        self.assertIsNotNone(reversal)
        self.assertEqual(reversal.entity_type, "LedgerEntry")
        self.assertEqual(reversal.request_permission, "billing.reverse_entry")
        self.assertEqual(reversal.approve_permission, "billing.reversal_approve")
        self.assertIn("BillingService.reverse_entry", reversal.executor)
        self.assertNotIn("billing.reversal", ApprovalActionRegistry.PENDING_ACTIONS)
        # 通用审批面的 PII 批量导出审批仍未实现：继续登记为待实现项
        self.assertIn(
            "privacy.pii_export_unified", ApprovalActionRegistry.PENDING_ACTIONS)

    def test_sensitive_export_is_internal_only(self):
        spec = ApprovalActionRegistry.get("privacy.export_staff")
        self.assertFalse(spec.api_requestable)
        self.assertNotIn(
            "privacy.export_staff",
            ApprovalActionRegistry.api_requestable_codes())

    def test_specs_bind_permission_executor_and_entity_type(self):
        refund = ApprovalActionRegistry.get("billing.refund")
        self.assertEqual(refund.entity_type, "Refund")
        self.assertEqual(refund.request_permission, "billing.refund_request")
        self.assertEqual(refund.approve_permission, "billing.refund_approve")
        self.assertIn("execute_refund", refund.executor)
        self.assertTrue(refund.sod_rule)
        reopen = ApprovalActionRegistry.get("night_audit.reopen")
        self.assertEqual(reopen.entity_type, "BusinessDate")
        self.assertEqual(
            reopen.request_permission, "hotelapp.business_date_reopen")
        self.assertEqual(
            reopen.approve_permission, "hotelapp.business_date_reopen")
        self.assertEqual(
            set(refund.payload_fields), {"payment_id", "amount", "currency", "refund_liability_id"})

    def test_sensitive_key_and_value_detectors(self):
        for key in ("password", "api_key", "id_card", "支付密钥", "idCard"):
            self.assertTrue(_is_sensitive_key(key))
        self.assertFalse(_is_sensitive_key("payment_id"))
        self.assertTrue(_is_sensitive_value("11010119900101123X"))
        self.assertTrue(_is_sensitive_value("6222020200112233445"))
        self.assertFalse(_is_sensitive_value("100.00"))
        self.assertFalse(_is_sensitive_value("2026-11-05"))


class ApprovalServiceWhitelistTests(TestCase):
    """Service 层白名单（P0-02 §3/§5/§6）：拒绝时不写记录、不写审计。"""

    def setUp(self):
        self.property = Property.objects.create(code="REG01", name="注册表白名单酒店")
        from django.contrib.auth.models import User
        self.requester = User.objects.create_user(username="registry-requester")

    def _request(self, **overrides):
        kwargs = dict(
            property_obj=self.property, action_code="billing.refund",
            entity_type="Refund", entity_id="9001",
            requested_by=self.requester, reason="注册表白名单校验",
            payload={"payment_id": 1, "amount": "100.00", "currency": "CNY"})
        kwargs.update(overrides)
        return ApprovalService.request_approval(**kwargs)

    def _audit_count(self):
        return AuditEvent.objects.filter(action="approval.request").count()

    def test_valid_request_still_creates_record_and_audit(self):
        record, created = self._request()
        self.assertTrue(created)
        self.assertEqual(record.status, ApprovalRecord.Status.PENDING)
        self.assertEqual(self._audit_count(), 1)

    def test_unknown_action_code_raises_without_side_effects(self):
        before_records = ApprovalRecord.objects.count()
        with self.assertRaises(ValueError):
            self._request(action_code="billing.void_everything")
        self.assertEqual(ApprovalRecord.objects.count(), before_records)
        self.assertEqual(self._audit_count(), 0)

    def test_entity_type_mismatch_raises(self):
        with self.assertRaises(ValueError):
            self._request(entity_type="Payment")
        self.assertEqual(ApprovalRecord.objects.count(), 0)
        self.assertEqual(self._audit_count(), 0)

    def test_payload_field_outside_whitelist_is_rejected(self):
        with self.assertRaises(ValueError):
            self._request(payload={"operator_note": "额外字段"})
        self.assertEqual(ApprovalRecord.objects.count(), 0)
        self.assertEqual(self._audit_count(), 0)

    def test_payload_sensitive_key_is_rejected(self):
        with self.assertRaises(ValueError):
            self._request(payload={"bank_password": "abc"})
        self.assertEqual(ApprovalRecord.objects.count(), 0)

    def test_payload_id_card_like_value_is_rejected(self):
        with self.assertRaises(ValueError):
            self._request(payload={"payment_id": "11010119900101123X"})
        self.assertEqual(ApprovalRecord.objects.count(), 0)

    def test_payload_length_limit_is_enforced(self):
        with self.assertRaises(ValueError):
            self._request(payload={"currency": "CNYX"})
        self.assertEqual(ApprovalRecord.objects.count(), 0)

    def test_payload_nested_and_float_values_are_rejected(self):
        with self.assertRaises(ValueError):
            self._request(payload={"amount": {"nested": True}})
        with self.assertRaises(ValueError):
            self._request(payload={"amount": 10.5})
        self.assertEqual(ApprovalRecord.objects.count(), 0)

    def test_payload_subset_of_whitelist_is_accepted(self):
        record, created = self._request(payload={"amount": "66.00"})
        self.assertTrue(created)
        self.assertEqual(record.payload_snapshot, {"amount": "66.00"})
