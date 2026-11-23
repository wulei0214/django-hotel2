"""TASK-03 姓名 PII 加密测试（阶段 1 双写）。

锁定 name_pii.py 加解密与 create/update_guest 双写接入：
* 规范化：strip + 全角/连续空白折叠；空姓名 → 三件套清空；
* 密文可解密还原、盲索引确定性（同姓名同 HMAC）、密钥版本 v1；
* 双写：create_guest/update_guest 后密文+HMAC+版本齐备；
* 检索辅助：full_name_hmac 可精确命中。

注：受控揭示 `reveal_guest_name` 及其权限门测试已按 2026-11-23 负责人治理
指令移除（NS-E-05 未逐项批准前不得新增权限码）。恢复条件见 name_pii.py 末尾。
"""

from typing import Any

from django.contrib.auth.models import User
from django.test import TestCase

from apps.core.models import Property
from apps.guests.models import Guest
from apps.guests.name_pii import (
    PII_KEY_VERSION_V1,
    decrypt_guest_name,
    encrypt_guest_name,
    fill_guest_name_pii,
    guest_name_hash,
    normalize_guest_name,
)
from apps.guests.services import GuestService


class NamePiiServiceTests(TestCase):
    """规范化/加密/盲索引单元行为。"""

    def test_normalize_strips_and_folds_whitespace(self):
        self.assertEqual(normalize_guest_name("  张三　 "), "张三")
        self.assertEqual(normalize_guest_name("张　  三"), "张 三")
        self.assertIsNone(normalize_guest_name("   "))
        self.assertIsNone(normalize_guest_name(None))

    def test_encrypt_decrypt_roundtrip(self):
        normalized = normalize_guest_name("李四")
        cipher = encrypt_guest_name(normalized)
        self.assertNotEqual(cipher, normalized)
        self.assertNotIn(normalized, cipher)
        self.assertEqual(decrypt_guest_name(cipher), normalized)

    def test_hash_is_deterministic_and_differs_by_name(self):
        h1 = guest_name_hash(normalize_guest_name("王五"))
        h2 = guest_name_hash(normalize_guest_name("王五"))
        h3 = guest_name_hash(normalize_guest_name("王六"))
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 64)

    def test_fill_sets_trio_and_empty_clears(self):
        prop = Property.objects.get(code="DEFAULT")
        guest = Guest.objects.create(property=prop, full_name="  赵六　")
        normalized = fill_guest_name_pii(guest, "  赵六　")
        self.assertEqual(normalized, "赵六")
        self.assertEqual(guest.pii_key_version, PII_KEY_VERSION_V1)
        self.assertEqual(decrypt_guest_name(guest.full_name_ciphertext), "赵六")
        self.assertEqual(guest.full_name_hmac, guest_name_hash("赵六"))
        # 空姓名 → 三件套清空
        fill_guest_name_pii(guest, "  ")
        self.assertIsNone(guest.full_name_ciphertext)
        self.assertIsNone(guest.full_name_hmac)
        self.assertIsNone(guest.pii_key_version)


class NamePiiDualWriteTests(TestCase):
    property: Any
    creator: Any
    """create_guest / update_guest 双写接入。"""

    @classmethod
    def setUpTestData(cls):
        cls.property = Property.objects.get(code="DEFAULT")

    def _creator(self):
        user = User.objects.create_user(username="name_creator")
        user.set_unusable_password()
        user.save(update_fields=["password"])
        return user

    def test_create_guest_fills_name_pii(self):
        guest = GuestService.create_guest(
            property_obj=self.property,
            full_name="双写测试",
            phone="13800001111",
            created_by=self._creator(),
        )
        self.assertEqual(guest.pii_key_version, PII_KEY_VERSION_V1)
        self.assertEqual(decrypt_guest_name(guest.full_name_ciphertext), "双写测试")
        self.assertEqual(guest.full_name_hmac, guest_name_hash("双写测试"))

    def test_update_guest_refreshes_name_pii(self):
        user = self._creator()
        guest = GuestService.create_guest(
            property_obj=self.property, full_name="旧姓名", phone="13800002222", created_by=user
        )
        old_hmac = guest.full_name_hmac
        GuestService.update_guest(guest_id=guest.pk, property_obj=self.property, full_name="新姓名")
        guest.refresh_from_db()
        self.assertEqual(decrypt_guest_name(guest.full_name_ciphertext), "新姓名")
        self.assertEqual(guest.full_name_hmac, guest_name_hash("新姓名"))
        self.assertNotEqual(guest.full_name_hmac, old_hmac)


class NameHmacSearchTests(TestCase):
    property: Any
    creator: Any
    """盲索引精确检索辅助（阶段 2 切换后的检索路径）。"""

    @classmethod
    def setUpTestData(cls):
        cls.property = Property.objects.get(code="DEFAULT")
        cls.creator = User.objects.create_user(username="hmac_creator")
        cls.creator.set_unusable_password()
        cls.creator.save(update_fields=["password"])

    def test_hmac_exact_match_finds_guest(self):
        guest = GuestService.create_guest(
            property_obj=self.property,
            full_name="检索目标",
            phone="13800003333",
            created_by=self.creator,
        )
        GuestService.create_guest(
            property_obj=self.property,
            full_name="其他客人",
            phone="13800004444",
            created_by=self.creator,
        )
        hit = Guest.objects.filter(
            property=self.property, full_name_hmac=guest_name_hash("检索目标")
        )
        self.assertEqual(hit.count(), 1)
        found = hit.first()
        assert found is not None  # mypy 收窄：上方已断言 count==1
        self.assertEqual(found.pk, guest.pk)
