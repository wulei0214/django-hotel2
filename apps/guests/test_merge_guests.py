"""A3 客人合并专项测试：服务迁移断言 / 拒绝路径 / 列表隐去 / 审计 / 命令 / API。

覆盖（任务书验收点）：
  * 正常合并各表迁移断言（GuestDocument/GuestPhone/GuestMembership/
    Reservation/ReservationGuest/StayGuest + duplicate 软归档）；
  * 同人拒绝、跨物业拒绝、已合并拒绝、理由缺失拒绝；
  * 合并后 duplicate 列表不可见（guest_list exclude merged）；
  * 审计存在（guest.merged，before/after 摘要）；
  * 账务事实不动（Folio/Payment 归属原预订，随预订 guest FK 迁移）；
  * 管理命令 merge_guests 与 API 端点 /api/v1/guest-center/guests/{id}/merge。
"""

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import Group, Permission, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Folio
from apps.core.models import AuditEvent, Property
from apps.guests.models import (
    Guest,
    GuestDocument,
    GuestDocumentIndex,
    GuestMembership,
    GuestPhone,
)
from apps.guests.services import (
    GuestService,
    GuestServiceError,
    doc_no_hash,
    encrypt_doc_no,
    encrypt_phone,
    normalize_phone,
    phone_hash,
)
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.inventory.models import RatePlan, Room, RoomType
from apps.reservations.models import Reservation, ReservationGuest, ReservationRoom, Stay, StayGuest

_TEST_PW = uuid.uuid4().hex + uuid.uuid4().hex  # 测试专用随机密码（不落字面量）


@override_settings(DEBUG=True, DJANGO_ENV="test")
class GuestMergeServiceTests(TestCase):
    """服务层：迁移断言与拒绝路径（不涉权限——命令/世界面由调用方门禁）。"""

    def setUp(self):
        self.prop = Property.objects.create(code=f"MG-{uuid.uuid4().hex[:8]}", name="合并测试物业")
        self.primary = Guest.objects.create(property=self.prop, full_name="主档案")
        self.duplicate = Guest.objects.create(property=self.prop, full_name="重复档案")

    def _add_phone(self, guest, raw, is_primary=False):
        norm = normalize_phone(raw)
        return GuestPhone.objects.create(
            guest=guest,
            phone_ciphertext=encrypt_phone(norm),
            phone_hash=phone_hash(norm),
            is_primary=is_primary,
        )

    def _add_document(self, guest, doc_no, doc_type="ID_CARD"):
        return GuestDocument.objects.create(
            guest=guest,
            doc_type=doc_type,
            doc_no_ciphertext=encrypt_doc_no(doc_no),
            doc_no_hash=doc_no_hash(doc_no),
        )

    def _make_reservation(self, guest, with_folio=True, nights=1):
        check_in = timezone.localdate() - timedelta(days=30)
        room_type = RoomType.objects.create(
            property=self.prop,
            code=f"MG-RT-{uuid.uuid4().hex[:6]}",
            name="合并测试房型",
            capacity=2,
        )
        rate_plan = RatePlan.objects.create(
            property=self.prop,
            code=f"MG-RP-{uuid.uuid4().hex[:6]}",
            name="合并测试房价",
            valid_from=check_in,
        )
        reservation = Reservation.objects.create(
            property=self.prop,
            guest=guest,
            booking_no=f"RM-{uuid.uuid4().hex[:12].upper()}",
            status=Reservation.Status.CHECKED_OUT,
            check_in=check_in,
            check_out=check_in + timedelta(days=nights),
            room_count=1,
            total_amount=Decimal("300.00"),
            currency="CNY",
        )
        line = ReservationRoom.objects.create(
            property=self.prop,
            reservation=reservation,
            room_type=room_type,
            rate_plan=rate_plan,
            check_in=check_in,
            check_out=check_in + timedelta(days=nights),
            nightly_rates=[{"date": check_in.isoformat(), "price": "300.00", "currency": "CNY"}],
            amount=Decimal("300.00"),
        )
        folio = None
        if with_folio:
            folio = Folio.objects.create(
                property=self.prop, reservation=reservation, guest=guest, currency="CNY"
            )
        return reservation, line, folio

    def test_merge_moves_all_references_and_tombstones_duplicate(self):
        """正常合并：证件/电话/会员/预订/住客快照迁移 + duplicate 软归档 + 审计。"""
        # duplicate 侧资产
        dup_phone_main = self._add_phone(self.duplicate, "13811112222", is_primary=True)
        dup_phone_extra = self._add_phone(self.duplicate, "13833334444")
        dup_doc = self._add_document(self.duplicate, "E110101199001011234")
        dup_index = GuestDocumentIndex.objects.create(
            property=self.prop, guest=self.duplicate, doc_no_hash=dup_doc.doc_no_hash
        )
        membership = GuestMembership.objects.create(
            property=self.prop,
            guest=self.duplicate,
            member_no=f"M-{uuid.uuid4().hex[:8]}",
            level_code="GOLD",
        )
        reservation, line, folio = self._make_reservation(self.duplicate)
        res_guest = ReservationGuest.objects.create(
            reservation=reservation,
            guest=self.duplicate,
            role=ReservationGuest.Role.PRIMARY,
            full_name="重复档案",
            is_primary=True,
        )
        room = Room.objects.create(property=self.prop, room_type=line.room_type, number="MG101")
        stay = Stay.objects.create(
            property=self.prop, reservation_room=line, room=room, status=Stay.Status.CHECKED_OUT
        )
        stay_guest = StayGuest.objects.create(stay=stay, guest=self.duplicate, full_name="重复档案")

        # primary 侧已有一个不同号码（不与 duplicate 冲突）
        self._add_phone(self.primary, "13800001111", is_primary=True)
        # primary 侧已有与 duplicate 相同的号码（合并去重：duplicate 的丢弃）
        dup_dedup = self._add_phone(self.duplicate, "13955556666")
        self._add_phone(self.primary, "13955556666")

        result = GuestService.merge_guests(
            self.primary, self.duplicate, performed_by=None, reason="前台核对为同一客人"
        )

        # —— 迁移断言 ——
        self.primary.refresh_from_db()
        self.duplicate.refresh_from_db()
        dup_doc.refresh_from_db()
        dup_phone_main.refresh_from_db()
        dup_phone_extra.refresh_from_db()
        membership.refresh_from_db()
        reservation.refresh_from_db()
        res_guest.refresh_from_db()
        stay_guest.refresh_from_db()
        folio.refresh_from_db()
        self.assertEqual(result["primary_guest_id"], self.primary.pk)
        self.assertEqual(dup_doc.guest_id, self.primary.pk)
        self.assertEqual(dup_phone_extra.guest_id, self.primary.pk)
        # 主号码保留：primary 原有主号码，duplicate 的主号码降级保留
        self.assertFalse(dup_phone_main.is_primary)
        self.assertTrue(GuestPhone.objects.filter(guest=self.primary, is_primary=True).count() == 1)
        self.assertEqual(membership.guest.pk, self.primary.pk)
        self.assertEqual(reservation.guest_id, self.primary.pk)
        self.assertEqual(res_guest.guest.pk, self.primary.pk)
        self.assertEqual(stay_guest.guest.pk, self.primary.pk)
        self.assertEqual(GuestDocumentIndex.objects.get(pk=dup_index.pk).guest.pk, self.primary.pk)
        # 电话按 phone_hash 去重：duplicate 的同号行被丢弃
        self.assertEqual(result["phones_deduped"], 1)
        self.assertFalse(GuestPhone.objects.filter(pk=dup_dedup.pk).exists())
        # duplicate 软归档
        self.assertEqual(
            self.duplicate.merged_into.pk if self.duplicate.merged_into else None, self.primary.pk
        )
        self.assertIsNotNone(self.duplicate.merged_at)
        # 账务事实不动：Folio 归属原预订，预订 guest FK 已迁移
        self.assertEqual(folio.guest_id, self.duplicate.pk)
        self.assertEqual(folio.reservation.guest_id, self.primary.pk)
        # 审计存在：guest.merged，before/after 摘要完整
        event = AuditEvent.objects.filter(
            action="guest.merged", entity_type="Guest", entity_id=str(self.primary.pk)
        ).get()
        self.assertEqual(event.after_json["duplicate_guest_id"], self.duplicate.pk)
        self.assertEqual(event.after_json["mapping"]["status"], "merged")
        self.assertEqual(event.after_json["moved"]["reservations"], 1)
        self.assertEqual(event.after_json["dropped"]["phones_deduped"], 1)
        self.assertTrue(event.after_json["billing_untouched"])
        self.assertEqual(event.before_json["duplicate"]["guest_id"], self.duplicate.pk)
        self.assertIn("前台核对", event.reason)

    def test_merge_promotes_duplicate_primary_phone_when_primary_has_none(self):
        """primary 无主号码时，duplicate 的主号码转移后保留主标记。"""
        self._add_phone(self.duplicate, "13877778888", is_primary=True)
        GuestService.merge_guests(self.primary, self.duplicate, reason="合并主号码转移")
        moved = GuestPhone.objects.get(guest=self.primary, is_primary=True)
        self.assertEqual(moved.primary_guest_key, str(self.primary.pk))
        # 顶层电话字段回填（primary 原为空）
        self.primary.refresh_from_db()
        self.assertEqual(self.primary.phone_hash, phone_hash("13877778888"))

    def test_merge_terminates_transferred_active_membership_on_conflict(self):
        """双方都有有效会员：转移行终止留史，主档案有效会员保持唯一。"""
        GuestMembership.objects.create(
            property=self.prop, guest=self.primary, member_no=f"MP-{uuid.uuid4().hex[:8]}"
        )
        dup_member = GuestMembership.objects.create(
            property=self.prop, guest=self.duplicate, member_no=f"MD-{uuid.uuid4().hex[:8]}"
        )
        result = GuestService.merge_guests(self.primary, self.duplicate, reason="重复会员档案合并")
        dup_member.refresh_from_db()
        self.assertEqual(dup_member.guest.pk, self.primary.pk)
        self.assertEqual(dup_member.status, GuestMembership.Status.TERMINATED)
        self.assertEqual(result["memberships_terminated"], 1)
        self.assertEqual(
            GuestMembership.objects.filter(
                property=self.prop, guest=self.primary, status=GuestMembership.Status.ACTIVE
            ).count(),
            1,
        )

    def test_merge_moves_inactive_membership_as_is(self):
        """非有效会员转移保留原状态与等级历史（历史不丢）。"""
        from apps.guests.models import MembershipLevelHistory

        membership = GuestMembership.objects.create(
            property=self.prop,
            guest=self.duplicate,
            member_no=f"MX-{uuid.uuid4().hex[:8]}",
            status=GuestMembership.Status.SUSPENDED,
        )
        MembershipLevelHistory.objects.create(
            membership=membership, to_level="STANDARD", reason="初始"
        )
        GuestService.merge_guests(self.primary, self.duplicate, reason="合并")
        membership.refresh_from_db()
        self.assertEqual(membership.guest.pk, self.primary.pk)
        self.assertEqual(membership.status, GuestMembership.Status.SUSPENDED)
        self.assertTrue(MembershipLevelHistory.objects.filter(membership=membership).exists())

    def test_merge_rejects_same_guest(self):
        with self.assertRaises(GuestServiceError):
            GuestService.merge_guests(self.primary, self.primary, reason="x")

    def test_merge_rejects_cross_property(self):
        other = Property.objects.create(code=f"MG2-{uuid.uuid4().hex[:8]}", name="另一物业")
        outsider = Guest.objects.create(property=other, full_name="别店客人")
        with self.assertRaises(GuestServiceError):
            GuestService.merge_guests(self.primary, outsider, reason="跨物业")
        self.duplicate.refresh_from_db()
        self.assertIsNone(self.duplicate.merged_into)

    def test_merge_rejects_already_merged_participants(self):
        GuestService.merge_guests(self.primary, self.duplicate, reason="第一次合并")
        third = Guest.objects.create(property=self.prop, full_name="第三档案")
        with self.assertRaises(GuestServiceError):
            GuestService.merge_guests(self.primary, self.duplicate, reason="重复合并")
        with self.assertRaises(GuestServiceError):
            GuestService.merge_guests(self.duplicate, third, reason="已合并档案再并入")
        with self.assertRaises(GuestServiceError):
            GuestService.merge_guests(third, self.duplicate, reason="并入已合并档案")

    def test_merge_requires_reason(self):
        with self.assertRaises(GuestServiceError):
            GuestService.merge_guests(self.primary, self.duplicate, reason="   ")
        self.duplicate.refresh_from_db()
        self.assertIsNone(self.duplicate.merged_into)

    def test_merge_rejects_overlong_reason(self):
        with self.assertRaises(GuestServiceError):
            GuestService.merge_guests(self.primary, self.duplicate, reason="长" * 241)


class GuestMergeCommandTests(TestCase):
    """管理命令（备用入口）：内部走同一 GuestService.merge_guests。"""

    def setUp(self):
        self.prop = Property.objects.create(
            code=f"MC-{uuid.uuid4().hex[:8]}", name="命令合并测试物业"
        self.primary = Guest.objects.create(property=self.prop, full_name="命令主档案")
        self.duplicate = Guest.objects.create(property=self.prop, full_name="命令重复档案")
        Reservation.objects.create(
            property=self.prop, guest=self.duplicate,
            booking_no=f"MC-{uuid.uuid4().hex[:12].upper()}",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(), check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("300.00"), currency="CNY")

    def test_command_merges_and_reports(self):
        out = StringIO()
        call_command("merge_guests", primary=self.primary.pk,
                     duplicate=self.duplicate.pk, reason="命令核对同一客人",
                     stdout=out)
        self.duplicate.refresh_from_db()
        self.assertEqual(self.duplicate.merged_into.pk if self.duplicate.merged_into else None, self.primary.pk)
        self.assertEqual(Reservation.objects.filter(guest=self.primary).count(), 1)
        self.assertIn("客人合并完成", out.getvalue())
        self.assertTrue(AuditEvent.objects.filter(action="guest.merged").exists())

    def test_command_requires_reason(self):
        with self.assertRaises(CommandError):
            call_command("merge_guests", primary=self.primary.pk,
                         duplicate=self.duplicate.pk, reason="", stdout=StringIO())

    def test_command_rejects_same_guest(self):
        with self.assertRaises(CommandError):
            call_command("merge_guests", primary=self.primary.pk,
                         duplicate=self.primary.pk, reason="同人", stdout=StringIO())

    def test_command_rejects_missing_guest(self):
        with self.assertRaises(CommandError):
            call_command("merge_guests", primary=999999,
                         duplicate=self.duplicate.pk, reason="缺档案", stdout=StringIO())


@override_settings(DEBUG=True, DJANGO_ENV="test")
class GuestMergeApiTests(TestCase):
    """API：POST /api/v1/guest-center/guests/{primary_id}/merge。

    权限沿 guest_center 既有写端点门禁（hotelapp.change_client）；
    幂等键头沿 api_contract.idempotent。
    """

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.user = User.objects.create_user(
            username=f"merge-user-{uuid.uuid4().hex[:8]}", password=_TEST_PW)
        self.profile = StaffProfile.objects.create(
            user=self.user, full_name="客人合并测试员",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        self.membership = PropertyStaffMembership.objects.create(
            staff_profile=self.profile, property=self.property,
            employee_no=f"MG-{uuid.uuid4().hex[:6]}",
            status=PropertyStaffMembership.Status.ACTIVE)
        self.role = Group.objects.create(name=f"合并测试组-{uuid.uuid4().hex[:6]}")
        self.permission = Permission.objects.get(
            content_type__app_label="hotelapp", codename="change_client")
        self.role.permissions.add(self.permission)
        # 列表断言需要查看权限（guest_list 门禁 hotelapp.view_client）
        self.role.permissions.add(Permission.objects.get(
            content_type__app_label="hotelapp", codename="view_client"))
        StaffRoleAssignment.objects.create(
            membership=self.membership, group=self.role,
            starts_at=timezone.now() - timedelta(minutes=1))
        self.primary = Guest.objects.create(property=self.property, full_name="API主档案")
        self.duplicate = Guest.objects.create(property=self.property, full_name="API重复档案")
        Reservation.objects.create(
            property=self.property, guest=self.duplicate,
            booking_no=f"MA-{uuid.uuid4().hex[:12].upper()}",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(), check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("300.00"), currency="CNY")

    def _merge_payload(self):
        return {"duplicate_id": self.duplicate.pk, "reason": "API 核对为同一客人"}

    def _login(self):
        self.client.force_login(self.user)

    def _guest_list_ids(self):
        response = self.client.get("/api/v1/guests")
        self.assertEqual(response.status_code, 200)
        return [row["id"] for row in response.json()["data"]]

    def test_merge_api_requires_permission(self):
        self.role.permissions.clear()
        self._login()
        response = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps(self._merge_payload()), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_merge_api_requires_authentication(self):
        response = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps(self._merge_payload()), content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_merge_api_merges_and_hides_duplicate_from_list(self):
        self._login()
        self.assertIn(self.duplicate.pk, self._guest_list_ids())
        response = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps(self._merge_payload()), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["primary_guest_id"], self.primary.pk)
        self.assertEqual(data["duplicate_guest_id"], self.duplicate.pk)
        self.assertEqual(data["reservations_moved"], 1)
        self.assertIsNotNone(data["merged_at"])
        self.duplicate.refresh_from_db()
        self.assertEqual(self.duplicate.merged_into.pk if self.duplicate.merged_into else None, self.primary.pk)
        # 合并后 duplicate 列表不可见
        self.assertNotIn(self.duplicate.pk, self._guest_list_ids())
        self.assertTrue(AuditEvent.objects.filter(
            action="guest.merged", entity_id=str(self.primary.pk)).exists())

    def test_merge_api_validates_body(self):
        self._login()
        for payload in (
            {"reason": "缺少 duplicate_id"},
            {"duplicate_id": "abc", "reason": "非法 duplicate_id"},
            {"duplicate_id": self.duplicate.pk, "reason": ""},
            {"duplicate_id": self.duplicate.pk, "reason": "长" * 241},
        ):
            response = self.client.post(
                f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
                data=json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 400, payload)
            self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_merge_api_missing_guest_returns_404(self):
        self._login()
        response = self.client.post(
            "/api/v1/guest-center/guests/999999/merge",
            data=json.dumps(self._merge_payload()), content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_merge_api_cross_property_duplicate_returns_404(self):
        other = Property.objects.create(
            code=f"MA2-{uuid.uuid4().hex[:8]}", name="API 另一物业")
        outsider = Guest.objects.create(property=other, full_name="别店客人")
        self._login()
        response = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps({"duplicate_id": outsider.pk, "reason": "跨物业拒绝"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_merge_api_replay_with_idempotency_key(self):
        self._login()
        key = f"merge-{uuid.uuid4().hex[:12]}"
        first = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps(self._merge_payload()), content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=key)
        second = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps(self._merge_payload()), content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"], second.json()["data"])
        self.assertEqual(
            AuditEvent.objects.filter(action="guest.merged").count(), 1)

    def test_merge_api_repeat_without_key_conflicts(self):
        self._login()
        first = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps(self._merge_payload()), content_type="application/json")
        second = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps(self._merge_payload()), content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "BUSINESS_CONFLICT")

    def test_merge_api_same_guest_conflicts(self):
        self._login()
        response = self.client.post(
            f"/api/v1/guest-center/guests/{self.primary.pk}/merge",
            data=json.dumps({"duplicate_id": self.primary.pk, "reason": "同人"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 409)
