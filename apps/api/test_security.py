# -*- coding: utf-8 -*-
import json
from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest, GuestDocument, GuestPhone
from apps.guests.services import encrypt_doc_no
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（Mimosa L3 门禁）。
from django.utils.crypto import get_random_string  # noqa: E402
_TEST_PW_510C52C1 = get_random_string(24)




class PropertyScopeAndPiiSecurityTests(TestCase):
    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.other_property = Property.objects.create(code="OTHER", name="其他物业")
        self.user = User.objects.create_user(
            username="pms-security-user", password=_TEST_PW_510C52C1
        )
        self.profile = StaffProfile.objects.create(
            user=self.user, full_name="安全测试前台",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        self.membership = PropertyStaffMembership.objects.create(
            property=self.property, staff_profile=self.profile,
            employee_no="SEC-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        self.role = Group.objects.get(name="前台")
        StaffRoleAssignment.objects.create(
            membership=self.membership, group=self.role,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        self._grant("view_client", "change_client", "staff_manage",
                    "guest_document_reveal", "guest_phone_reveal")
        self.client.force_login(self.user)

    def _grant(self, *codenames):
        for app_label, codes in (
            ("hotelapp", [c for c in codenames if c != "staff_manage"]),
            ("identity", [c for c in codenames if c == "staff_manage"]),
        ):
            self.role.permissions.add(*Permission.objects.filter(
                content_type__app_label=app_label, codename__in=codes
            ))

    def test_staff_list_and_detail_are_scoped_to_current_property(self):
        foreign_profile = StaffProfile.objects.create(
            full_name="其他物业员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        PropertyStaffMembership.objects.create(
            property=self.other_property, staff_profile=foreign_profile,
            employee_no="OTHER-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        listing = self.client.get("/api/v1/staff")
        self.assertEqual(listing.status_code, 200)
        returned_ids = {item["id"] for item in listing.json()["data"]}
        self.assertNotIn(foreign_profile.pk, returned_ids)
        detail = self.client.get(f"/api/v1/staff/{foreign_profile.pk}")
        self.assertEqual(detail.status_code, 404)

    def test_guest_update_cannot_mutate_another_property_guest(self):
        foreign_guest = Guest.objects.create(
            property=self.other_property, full_name="外店客人",
            phone="13800009999", phone_normalized="13800009999",
        )
        response = self.client.post(
            f"/api/v1/guests/{foreign_guest.pk}/update",
            data=json.dumps({"full_name": "被越权修改"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        foreign_guest.refresh_from_db()
        self.assertEqual(foreign_guest.full_name, "外店客人")

    def test_guest_detail_never_returns_full_document_or_phone(self):
        guest = Guest.objects.create(
            property=self.property, full_name="PII 测试客人",
            phone="13812345678", phone_normalized="13812345678",
        )
        GuestPhone.objects.create(
            guest=guest, phone="13812345678",
            phone_normalized="13812345678", is_primary=True,
        )
        doc_no = "110101199001011234"
        GuestDocument.objects.create(
            guest=guest, doc_type=GuestDocument.DocType.ID_CARD,
            doc_no_ciphertext=encrypt_doc_no(doc_no),
            doc_no_hash="security-test-doc-hash",
        )
        response = self.client.get(f"/api/v1/guests/{guest.pk}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["phones"][0]["phone"], "138****5678")
        self.assertEqual(payload["documents"][0]["masked"], "11" + ("*" * 14) + "34")
        self.assertNotIn(doc_no, response.content.decode("utf-8"))
        self.assertNotIn("13812345678", response.content.decode("utf-8"))

    def test_pii_reveal_requires_reason_and_records_masked_audit(self):
        guest = Guest.objects.create(
            property=self.property, full_name="PII 揭示测试",
            phone="13812345678", phone_normalized="13812345678",
        )
        phone = GuestPhone.objects.create(
            guest=guest, phone="13812345678",
            phone_normalized="13812345678", is_primary=True,
        )
        doc_no = "110101199001011234"
        doc = GuestDocument.objects.create(
            guest=guest, doc_type=GuestDocument.DocType.ID_CARD,
            doc_no_ciphertext=encrypt_doc_no(doc_no),
            doc_no_hash="security-test-doc-hash-2",
        )
        no_reason = self.client.post(
            f"/api/v1/guests/{guest.pk}/documents/{doc.pk}/reveal",
            data=json.dumps({"reason": "核验"}),
            content_type="application/json",
        )
        self.assertEqual(no_reason.status_code, 400)
        revealed = self.client.post(
            f"/api/v1/guests/{guest.pk}/documents/{doc.pk}/reveal",
            data=json.dumps({"reason": "办理入住身份核验"}),
            content_type="application/json",
        )
        self.assertEqual(revealed.status_code, 200)
        self.assertEqual(revealed.json()["data"]["document_no"], doc_no)
        phone_revealed = self.client.post(
            f"/api/v1/guests/{guest.pk}/phones/{phone.pk}/reveal",
            data=json.dumps({"reason": "办理入住联系电话核验"}),
            content_type="application/json",
        )
        self.assertEqual(phone_revealed.status_code, 200)
        self.assertEqual(phone_revealed.json()["data"]["phone"], "13812345678")
        events = AuditEvent.objects.filter(
            action__in=("guest.pii_reveal.document", "guest.pii_reveal.phone")
        )
        self.assertEqual(events.count(), 2)
        self.assertTrue(all(bool(event.reason) for event in events))
        audited_after = json.dumps(list(events.values_list("after_json", flat=True)))
        self.assertNotIn(doc_no, audited_after)
