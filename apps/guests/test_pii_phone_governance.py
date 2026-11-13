"""WB-102/103/104/105：Guest PII 与证件治理回归测试。

覆盖：
  * WB-103：手机号密文+HMAC 全面迁移——服务层统一入口（encrypt/normalize/
    hash/lookup）、明文列不再写入、迁移回填与清空、reveal 解密、
    官网预订路径收口、walk-in 占位号走统一服务、seed 同步（seed 由
    hotelapp 命令，路径正确性经 create_guest 同构断言覆盖）；
  * WB-102：GuestDocumentIndex 物业级并发唯一（同物业跨 guest 冲突被拒、
    同 guest 更新占用、跨物业不冲突、IntegrityError 竞态转换）；
  * WB-104：主号码切换事务化（唯一约束冲突/审计失败时旧主号码不降级）；
  * WB-105：register_document verified_by/verified_at 成对写入与模型校验。
"""

import importlib
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.guests.models import (
    Guest,
    GuestDocument,
    GuestDocumentIndex,
    GuestPhone,
)
from apps.guests.services import (
    GuestService,
    GuestServiceError,
    _mask,
    decrypt_phone,
    normalize_phone,
    phone_hash,
    reveal_phone,
)
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.inventory.models import DailyRate, RatePlan, RoomType, RoomTypeInventory

_MIGRATION_0007 = importlib.import_module(
    "apps.guests.migrations.0007_phone_pii_ciphertext_and_document_index"
)


class _AppsShim:
    """把迁移函数签名 (apps, schema_editor) 适配到当前真实模型注册表。"""

    def get_model(self, app_label, model_name):
        from django.apps import apps as global_apps

        return global_apps.get_model(app_label, model_name)


class _GuestGuardBase(TestCase):
    """自建物业与账号，不依赖 DEFAULT 物业数据。"""

    def setUp(self):
        self.property = Property.objects.create(code="PIIG", name="PII 治理测试酒店")
        self.operator = User.objects.create_user(username="piig-operator")


class PhoneMigrationBackfillTests(_GuestGuardBase):
    """WB-103：迁移 0007 回填密文+hash 后清空明文。"""

    def _make_plaintext_rows(self, phone="138 0000-1111", normalized="13800001111"):
        guest = Guest.objects.create(
            property=self.property,
            full_name="明文迁移客人",
            phone=phone,
            phone_normalized=normalized,
        )
        phone_row = GuestPhone.objects.create(
            guest=guest,
            phone=phone,
            phone_normalized=normalized,
            is_primary=True,
            source="前台登记",
        )
        return guest, phone_row

    def test_backfill_encrypts_hashes_then_clear_clears_plaintext(self):
        guest, phone_row = self._make_plaintext_rows()
        _MIGRATION_0007.backfill_phone_pii(_AppsShim(), None)
        guest.refresh_from_db()
        phone_row.refresh_from_db()
        self.assertEqual(decrypt_phone(guest.phone_ciphertext), "13800001111")
        self.assertEqual(guest.phone_hash, phone_hash("13800001111"))
        self.assertEqual(decrypt_phone(phone_row.phone_ciphertext), "13800001111")
        self.assertEqual(phone_row.phone_hash, phone_hash("13800001111"))

        _MIGRATION_0007.clear_phone_plaintext(_AppsShim(), None)
        guest.refresh_from_db()
        phone_row.refresh_from_db()
        self.assertIsNone(guest.phone)
        self.assertIsNone(guest.phone_normalized)
        self.assertIsNone(phone_row.phone)
        self.assertIsNone(phone_row.phone_normalized)
        # 清空后密文仍可解密、哈希仍可检索
        self.assertEqual(decrypt_phone(guest.phone_ciphertext), "13800001111")
        self.assertEqual(GuestPhone.objects.filter(phone_hash=phone_hash("13800001111")).count(), 1)

    def test_backfill_failure_fails_migration_and_keeps_original(self):
        guest, phone_row = self._make_plaintext_rows()
        with mock.patch.object(
            _MIGRATION_0007, "_encrypt_phone", side_effect=RuntimeError("密钥不可用")
        ):
            with self.assertRaises(RuntimeError):
                _MIGRATION_0007.backfill_phone_pii(_AppsShim(), None)
        guest.refresh_from_db()
        self.assertIsNone(guest.phone_ciphertext)
        self.assertEqual(
            guest.phone_normalized, "13800001111", "回填失败行必须保留原值（迁移显式失败）"
        )

    def test_backfill_rejects_same_guest_duplicate_hash(self):
        guest = Guest.objects.create(
            property=self.property,
            full_name="重复行客人",
            phone="13800002222",
            phone_normalized="13800002222",
        )
        GuestPhone.objects.create(guest=guest, phone="13800002222", phone_normalized="13800002222")
        GuestPhone.objects.create(guest=guest, phone="13800002222", phone_normalized="13800002222")
        with self.assertRaises(RuntimeError) as ctx:
            _MIGRATION_0007.backfill_phone_pii(_AppsShim(), None)
        self.assertIn("哈希冲突", str(ctx.exception))


class GuestPhoneCipherServiceTests(_GuestGuardBase):
    """WB-103：服务层统一入口；明文列零写入。"""

    def test_create_guest_writes_ciphertext_hash_and_no_plaintext(self):
        guest = GuestService.create_guest(
            property_obj=self.property,
            full_name="密文客人",
            phone="138 0000-1111",
            created_by=self.operator,
        )
        self.assertEqual(decrypt_phone(guest.phone_ciphertext), "13800001111")
        self.assertEqual(guest.phone_hash, phone_hash("13800001111"))
        self.assertFalse(guest.phone)
        self.assertFalse(guest.phone_normalized)
        primary = guest.phones.get(is_primary=True)
        self.assertEqual(decrypt_phone(primary.phone_ciphertext), "13800001111")
        self.assertEqual(primary.phone_hash, phone_hash("13800001111"))
        self.assertFalse(primary.phone)
        self.assertFalse(primary.phone_normalized)
        event = AuditEvent.objects.get(action="guest.create", entity_id=guest.pk)
        # 审计掩码沿用既有口径：对原始输入（含分隔符）打码，不落明文
        self.assertEqual(event.after_json.get("phone_masked"), _mask("138 0000-1111"))
        self.assertNotIn("13800001111", json.dumps(event.after_json))

    def test_guest_without_phone_has_no_hash(self):
        guest = GuestService.create_guest(property_obj=self.property, full_name="无电话客人")
        self.assertFalse(guest.phone_ciphertext)
        self.assertFalse(guest.phone_hash)
        self.assertEqual(guest.phones.count(), 0)

    def test_shared_phone_detection_uses_hash_and_keeps_guests_separate(self):
        first = GuestService.create_guest(
            property_obj=self.property, full_name="共享客人一", phone="13800003333"
        )
        second = GuestService.create_guest(
            property_obj=self.property, full_name="共享客人二", phone="13800003333"
        )
        # M-03：不自动合并，两个档案并存；共享标记经 hash 命中
        self.assertNotEqual(first.pk, second.pk)
        self.assertTrue(first.phones.get().shared_review_required)
        self.assertTrue(second.phones.get().shared_review_required)
        self.assertEqual(GuestPhone.objects.filter(phone_hash=phone_hash("13800003333")).count(), 2)

    def test_add_phone_duplicate_same_guest_rejected(self):
        guest = GuestService.create_guest(
            property_obj=self.property, full_name="重复号码客人", phone="13800004444"
        )
        before = GuestPhone.objects.count()
        with self.assertRaises(GuestServiceError):
            GuestService.add_phone(guest=guest, phone="13800004444", is_primary=False)
        self.assertEqual(GuestPhone.objects.count(), before)


class RevealPhoneCipherTests(_GuestGuardBase):
    """WB-103：完整号码只经 reveal_phone 解密；审计口径不变。"""

    def setUp(self):
        super().setUp()
        self.revealer = self._make_revealer("piig-revealer")
        self.guest = GuestService.create_guest(
            property_obj=self.property, full_name="揭示客人", phone="13800005555"
        )
        self.phone_row = self.guest.phones.get(is_primary=True)

    def _make_revealer(self, username):
        user = User.objects.create_user(username=username)
        profile = StaffProfile.objects.create(
            user=user, full_name=username, employment_status="ACTIVE"
        )
        membership = PropertyStaffMembership.objects.create(
            property=self.property,
            staff_profile=profile,
            employee_no=f"PIIG-{username}",
            status="ACTIVE",
        )
        group = Group.objects.create(name=f"PIIG-{username}-group")
        group.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="hotelapp", codename__in=["guest_phone_reveal"]
            )
        )
        StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=timezone.now()
        )
        return user

    def test_reveal_phone_decrypts_from_ciphertext_and_audits_masked(self):
        plain = reveal_phone(
            self.phone_row, revealed_by=self.revealer, reason="前台需要拨打客人电话核对到店时间"
        )
        self.assertEqual(plain, "13800005555")
        event = AuditEvent.objects.get(action="guest.phone_reveal")
        self.assertEqual(event.after_json.get("masked"), "13*******55")
        self.assertNotIn("13800005555", json.dumps(event.after_json))

    def test_unmigrated_legacy_row_still_revealable(self):
        legacy = GuestPhone.objects.create(
            guest=self.guest,
            phone="13800006666",
            phone_normalized="13800006666",
            is_primary=False,
            source="历史迁移",
        )
        plain = reveal_phone(legacy, revealed_by=self.revealer, reason="历史行揭示核对信息")
        self.assertEqual(plain, "13800006666")


class PrimaryPhoneSwitchAtomicityTests(_GuestGuardBase):
    """WB-104：主号码切换事务化，任一步失败整体回滚。"""

    def setUp(self):
        super().setUp()
        self.guest = GuestService.create_guest(
            property_obj=self.property, full_name="切换客人", phone="13800007777"
        )

    def _primary_hash(self):
        row = self.guest.phones.filter(is_primary=True).first()
        return row.phone_hash if row else None

    def test_update_guest_rolls_back_when_unique_hash_conflicts(self):
        # 构造重复 phone_hash 场景：先以非主号码登记 B，再把主号码切换到 B
        GuestService.add_phone(guest=self.guest, phone="13800008888", is_primary=False)
        self.assertEqual(GuestPhone.objects.filter(guest=self.guest).count(), 2)
        with self.assertRaises(GuestServiceError):
            GuestService.update_guest(
                guest_id=self.guest.pk,
                property_obj=self.property,
                updated_by=self.operator,
                phone="13800008888",
            )
        # 旧主号码未被降级，Guest 主字段未被改写
        self.assertEqual(self._primary_hash(), phone_hash("13800007777"))
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.phone_hash, phone_hash("13800007777"))
        self.assertEqual(GuestPhone.objects.filter(guest=self.guest).count(), 2)

    def test_add_phone_primary_conflict_keeps_old_primary(self):
        with self.assertRaises(GuestServiceError):
            GuestService.add_phone(guest=self.guest, phone="13800007777", is_primary=True)
        self.assertEqual(self._primary_hash(), phone_hash("13800007777"))
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.phone_hash, phone_hash("13800007777"))

    def test_update_guest_rolls_back_when_audit_write_fails(self):
        original_hash = self.guest.phone_hash
        with mock.patch.object(AuditEvent, "record", side_effect=RuntimeError("审计写入失败")):
            with self.assertRaises(RuntimeError):
                GuestService.update_guest(
                    guest_id=self.guest.pk,
                    property_obj=self.property,
                    updated_by=self.operator,
                    phone="13800009999",
                )
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.phone_hash, original_hash)
        primary = self.guest.phones.filter(is_primary=True).first()
        self.assertEqual(primary.phone_hash, phone_hash("13800007777"))

    def test_successful_primary_switch_downgrades_old_row(self):
        guest, changed = GuestService.update_guest(
            guest_id=self.guest.pk,
            property_obj=self.property,
            updated_by=self.operator,
            phone="13800009000",
        )
        self.assertIn("phone", changed)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.phone_hash, phone_hash("13800009000"))
        old_row = self.guest.phones.get(phone_hash=phone_hash("13800007777"))
        self.assertFalse(old_row.is_primary)
        new_row = self.guest.phones.get(phone_hash=phone_hash("13800009000"))
        self.assertTrue(new_row.is_primary)


class DocumentIndexGovernanceTests(_GuestGuardBase):
    """WB-102：证件物业级并发唯一；WB-105：核验字段成对。"""

    def setUp(self):
        super().setUp()
        self.guest_a = GuestService.create_guest(property_obj=self.property, full_name="证件客人甲")
        self.guest_b = GuestService.create_guest(property_obj=self.property, full_name="证件客人乙")
        self.other_property = Property.objects.create(code="PIIG2", name="PII 治理他店")
        self.guest_other = GuestService.create_guest(
            property_obj=self.other_property, full_name="他店客人"
        )

    def test_register_document_writes_property_scoped_index(self):
        from apps.guests.services import register_document

        doc = register_document(guest=self.guest_a, doc_type="ID_CARD", doc_no="110101199001011234")
        index = GuestDocumentIndex.objects.get(property=self.property, doc_no_hash=doc.doc_no_hash)
        self.assertEqual(index.guest_id, self.guest_a.pk)

    def test_same_guest_reregister_updates_occupation(self):
        from apps.guests.services import register_document

        register_document(guest=self.guest_a, doc_type="ID_CARD", doc_no="110101199001011234")
        doc2 = register_document(
            guest=self.guest_a, doc_type="PASSPORT", doc_no="110101199001011234"
        )
        # 同一客人重复登记：占用不变（仍指向本人），新类型证件允许建档
        self.assertEqual(
            GuestDocumentIndex.objects.filter(
                property=self.property, doc_no_hash=doc2.doc_no_hash
            ).count(),
            1,
        )
        self.assertEqual(GuestDocument.objects.filter(guest=self.guest_a).count(), 2)

    def test_cross_guest_same_property_rejected(self):
        from apps.guests.services import doc_no_hash, register_document

        register_document(guest=self.guest_a, doc_type="ID_CARD", doc_no="110101199001011234")
        with self.assertRaises(ValidationError) as ctx:
            register_document(guest=self.guest_b, doc_type="ID_CARD", doc_no="110101199001011234")
        # 首次登记成功后，跨客人冲突先被 GuestDocument 同物业预检拦下
        # （既有口径不变）；占用索引路径（竞态败者）由下方 IntegrityError
        # 转换测试覆盖。
        self.assertIn("该证件已登记在其他客人档案", str(ctx.exception))
        index = GuestDocumentIndex.objects.get(
            property=self.property, doc_no_hash=doc_no_hash("110101199001011234")
        )
        self.assertEqual(index.guest_id, self.guest_a.pk)  # 占用未被改写

    def test_cross_property_no_conflict(self):
        from apps.guests.services import register_document

        register_document(guest=self.guest_a, doc_type="ID_CARD", doc_no="110101199001011234")
        doc_other = register_document(
            guest=self.guest_other, doc_type="ID_CARD", doc_no="110101199001011234"
        )
        self.assertNotEqual(doc_other.guest_id, self.guest_a.pk)
        self.assertEqual(
            GuestDocumentIndex.objects.filter(doc_no_hash=doc_other.doc_no_hash).count(), 2
        )

    def test_integrity_error_race_converted_to_friendly_error(self):
        """竞态败者路径：索引已被他人占用且文档预检不命中（对方无文档行）。"""
        from apps.guests.services import doc_no_hash, register_document

        digest = doc_no_hash("110101199001011234")
        GuestDocumentIndex.objects.create(
            property=self.property, guest=self.guest_a, doc_no_hash=digest
        )
        with self.assertRaises(ValidationError) as ctx:
            register_document(guest=self.guest_b, doc_type="ID_CARD", doc_no="110101199001011234")
        self.assertIn("该证件已登记至其他客人档案", str(ctx.exception))
        index = GuestDocumentIndex.objects.get(property=self.property, doc_no_hash=digest)
        self.assertEqual(index.guest_id, self.guest_a.pk)

    def test_verified_by_writes_verified_at_together(self):
        from apps.guests.services import register_document

        doc = register_document(
            guest=self.guest_a,
            doc_type="ID_CARD",
            doc_no="110101199001011234",
            verified_by=self.operator,
        )
        self.assertIsNotNone(doc.verified_at)
        self.assertEqual(doc.verified_by_id, self.operator.pk)
        doc2 = register_document(
            guest=self.guest_a, doc_type="PASSPORT", doc_no="E12345678", verified_by=None
        )
        self.assertIsNone(doc2.verified_at)
        self.assertIsNone(doc2.verified_by_id)

    def test_document_save_requires_paired_verification_fields(self):
        with self.assertRaises(ValidationError):
            GuestDocument.objects.create(
                guest=self.guest_a,
                doc_type="ID_CARD",
                doc_no_ciphertext="x",
                doc_no_hash="y",
                verified_by=self.operator,
                verified_at=None,
            )
        with self.assertRaises(ValidationError):
            GuestDocument.objects.create(
                guest=self.guest_a,
                doc_type="ID_CARD",
                doc_no_ciphertext="x",
                doc_no_hash="y2",
                verified_at=timezone.now(),
                verified_by=None,
            )


class WebsiteBookingFunnelTests(TestCase):
    """WB-103：官网预订路径收口为 GuestService 统一入口。"""

    def setUp(self):
        self.property = Property.objects.create(code="PIIW", name="官网收口测试酒店")
        self.today = timezone.localdate()
        self.operator = User.objects.create_user(username="piiw-operator")

    def _setup_inventory(self):
        from apps.inventory.models import Room

        room_type = RoomType.objects.create(
            property=self.property, code="PIIW-KING", name="收口大床房"
        )
        rate_plan = RatePlan.objects.create(
            property=self.property, code="PIIW-BAR", name="收口门市价", valid_from=self.today
        )
        for offset in range(3):
            day = self.today + timedelta(days=offset)
            DailyRate.objects.create(
                property=self.property,
                rate_plan=rate_plan,
                room_type=room_type,
                stay_date=day,
                price=Decimal("300.00"),
            )
            RoomTypeInventory.objects.create(
                property=self.property, room_type=room_type, stay_date=day, total_rooms=3
            )
        Room.objects.create(
            property=self.property,
            room_type=room_type,
            number="PIIW-101",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )
        return room_type, rate_plan

    def _payload(self, phone):
        return {
            "full_name": "官网收口住客",
            "phone": phone,
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": self.today,
            "check_out": self.today + timedelta(days=1),
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }

    def test_website_booking_guest_uses_cipher_and_hash(self):
        from apps.reservations.services import create_reservation

        self.room_type, self.rate_plan = self._setup_inventory()
        reservation, created, _token = create_reservation(
            property_obj=self.property,
            payload=self._payload("138 0000-1234"),
            idempotency_key="piig-website-001",
        )
        self.assertTrue(created)
        guest = reservation.guest
        self.assertEqual(decrypt_phone(guest.phone_ciphertext), "13800001234")
        self.assertEqual(guest.phone_hash, phone_hash("13800001234"))
        self.assertFalse(guest.phone)
        self.assertFalse(guest.phone_normalized)
        phone_row = guest.phones.get(is_primary=True)
        self.assertEqual(phone_row.source, "官网预订")
        self.assertEqual(decrypt_phone(phone_row.phone_ciphertext), "13800001234")

    def test_pending_reservation_cap_matches_by_hash(self):
        from apps.reservations.services import ReservationConflict, create_reservation

        self.room_type, self.rate_plan = self._setup_inventory()
        for seq in range(2):
            create_reservation(
                property_obj=self.property,
                payload=self._payload("13800002222"),
                idempotency_key=f"piig-cap-{seq}",
            )
        with self.assertRaises(ReservationConflict):
            create_reservation(
                property_obj=self.property,
                payload=self._payload("13800002222"),
                idempotency_key="piig-cap-overflow",
            )

    def test_walkin_placeholder_goes_through_unified_service(self):
        """无手机号 walk-in 占位号同样密文+哈希落库，不写明文。"""
        import uuid as _uuid

        from apps.reservations.models import Stay
        from apps.reservations.walkin import create_walk_in

        self.room_type, self.rate_plan = self._setup_inventory()
        result = create_walk_in(
            property_obj=self.property,
            full_name="占位散客",
            phone="",
            room_type_id=self.room_type.pk,
            rate_plan_id=self.rate_plan.pk,
            nights=1,
            created_by=self.operator,
            idempotency_key="piig-walkin-001",
        )
        guest = result["reservation"].guest
        expected = (
            "0000"
            + _uuid.uuid5(_uuid.NAMESPACE_URL, f"walkin:{self.property.pk}:piig-walkin-001").hex[:8]
        )
        # 占位号经预订服务 normalize（uuid5 hex 中的字母被剔除，历史口径）
        expected_norm = normalize_phone(expected)
        self.assertEqual(guest.phone_hash, phone_hash(expected_norm))
        self.assertEqual(decrypt_phone(guest.phone_ciphertext), expected_norm)
        self.assertFalse(guest.phone)
        self.assertFalse(guest.phone_normalized)
        self.assertTrue(Stay.objects.filter(pk=result["stay"].pk).exists())
