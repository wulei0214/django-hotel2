"""MC-003 Guest 360 电话/证件子资源生命周期回归。"""

import json
import uuid

from django.test import override_settings

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.core.models import AuditEvent
from apps.guests.models import Guest, GuestDocument, GuestDocumentIndex, GuestPhone


@override_settings(DEBUG=True, DJANGO_ENV="test")
class GuestSubresourceTests(E2EFlowTestBase):
    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("guest_sub_front", "前台")
        self.client.force_login(self.fd)

    def _guest(self, name="子资源测试客人"):
        return Guest.objects.create(
            property=self.property, full_name=name, phone=f"139{uuid.uuid4().int % 10**8:08d}"
        )

    def _post(self, url, payload=None):
        return self.client.post(
            url, data=json.dumps(payload or {}, ensure_ascii=False), content_type="application/json"
        )

    def test_phone_add_set_primary_and_verify(self):
        guest = self._guest()
        response = self._post(
            f"/api/v1/guests/{guest.pk}/phones", {"phone": "13800001111", "is_primary": True}
        )
        self.assertEqual(response.status_code, 201, response.content)
        phone = GuestPhone.objects.get(guest=guest)
        self.assertIsNotNone(phone.phone_ciphertext)
        self.assertIsNone(phone.phone_normalized)
        guest.refresh_from_db()
        self.assertEqual(guest.phone_hash, phone.phone_hash)

        second = self._post(f"/api/v1/guests/{guest.pk}/phones", {"phone": "13800002222"})
        self.assertEqual(second.status_code, 201, second.content)
        second_id = second.json()["data"]["id"]
        response = self._post(f"/api/v1/guests/{guest.pk}/phones/{second_id}/primary")
        self.assertEqual(response.status_code, 200, response.content)
        phone.refresh_from_db()
        guest.refresh_from_db()
        self.assertFalse(phone.is_primary)
        self.assertTrue(GuestPhone.objects.get(pk=second_id).is_primary)
        self.assertEqual(guest.phone_hash, GuestPhone.objects.get(pk=second_id).phone_hash)

        response = self._post(f"/api/v1/guests/{guest.pk}/phones/{second_id}/verify")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(GuestPhone.objects.get(pk=second_id).verified_at)
        self.assertTrue(
            AuditEvent.objects.filter(
                property=self.property, action="guest.phone_verified"
            ).exists()
        )

    def test_document_register_verify_update_and_invalidate(self):
        guest = self._guest()
        doc_no = f"11010119900101{uuid.uuid4().int % 10000:04d}"
        response = self._post(
            f"/api/v1/guests/{guest.pk}/documents",
            {"doc_type": "ID_CARD", "doc_no": doc_no, "expiry_date": "2035-01-01"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        document = GuestDocument.objects.get(guest=guest)
        self.assertNotEqual(document.doc_no_ciphertext, doc_no)
        self.assertTrue(
            GuestDocumentIndex.objects.filter(
                property=self.property, doc_no_hash=document.doc_no_hash
            ).exists()
        )

        response = self._post(f"/api/v1/guests/{guest.pk}/documents/{document.pk}/verify")
        self.assertEqual(response.status_code, 200)
        document.refresh_from_db()
        self.assertEqual(document.verified_by, self.fd)
        self.assertIsNotNone(document.verified_at)

        response = self._post(
            f"/api/v1/guests/{guest.pk}/documents/{document.pk}/update",
            {"expiry_date": "2036-02-02"},
        )
        self.assertEqual(response.status_code, 200)
        document.refresh_from_db()
        self.assertEqual(document.expiry_date.isoformat(), "2036-02-02")

        response = self._post(
            f"/api/v1/guests/{guest.pk}/documents/{document.pk}/invalidate",
            {"reason": "证件过期失效"},
        )
        self.assertEqual(response.status_code, 200)
        document.refresh_from_db()
        self.assertIsNotNone(document.retention_until)
        self.assertFalse(document.is_primary)
        # 失效不等于解除物业级占用，避免证件号被另一个客人误用。
        self.assertTrue(
            GuestDocumentIndex.objects.filter(
                property=self.property, doc_no_hash=document.doc_no_hash
            ).exists()
        )

    def test_duplicate_document_across_guests_is_rejected(self):
        first = self._guest("证件所有人")
        second = self._guest("重复登记人")
        doc_no = f"31010119920202{uuid.uuid4().int % 10000:04d}"
        self.assertEqual(
            self._post(f"/api/v1/guests/{first.pk}/documents", {"doc_no": doc_no}).status_code, 201
        )
        response = self._post(f"/api/v1/guests/{second.pk}/documents", {"doc_no": doc_no})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(GuestDocument.objects.count(), 1)
