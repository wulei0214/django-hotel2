# -*- coding: utf-8 -*-
"""WB-122/123/124 验收：手机号 PII 迁移守卫与独立密钥治理。

不真正回滚迁移，而是把迁移 0007 的 RunPython 函数作用于当前（已迁移）
模型做行为验证；迁移步骤的不可逆性做静态断言。

覆盖：
  * WB-122：回填扫描必须覆盖 phone / phone_normalized 任一非空的记录，
    标准化字段优先；清明文守卫拦截「明文非空但密文/哈希缺失」的行；
  * WB-123：DJANGO_PII_KEY 必须真正进入加密/HMAC 派生链路；生产缺失
    fail-fast；SECRET_KEY 轮换不影响 PII_KEY 稳定的密文可读性；
  * WB-124：清明文步骤不可逆（无 reverse_code），回退迁移被显式拒绝。
"""
import hashlib
import hmac as hmac_mod
import importlib

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from apps.core.models import Property
from apps.guests import services as guest_services
from apps.guests.models import Guest

# 迁移模块名以数字开头，不能用包属性导入
mig0007 = importlib.import_module(
    "apps.guests.migrations.0007_phone_pii_ciphertext_and_document_index")


class _AppsShim:
    """让迁移函数作用于当前真实模型（本测试库结构已迁移到位）。"""

    def __init__(self):
        from apps.guests.models import GuestPhone

        self._models = {
            ("guests", "Guest"): Guest,
            ("guests", "GuestPhone"): GuestPhone,
        }

    def get_model(self, app_label, model_name):
        return self._models[(app_label, model_name)]


@override_settings(DEBUG=True, DJANGO_ENV="test")
class PhoneBackfillCoverageTests(TestCase):
    """WB-122：回填条件覆盖任一明文字段非空的记录。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")

    def _guest(self, suffix, *, phone, normalized):
        return Guest.objects.create(
            property=self.prop, full_name=f"WB122 客{suffix}",
            phone=phone or None, phone_normalized=normalized or None)

    def test_normalized_only_guest_is_backfilled(self):
        """phone 为空、phone_normalized 有值：必须回填，不得清明文丢号码。"""
        guest = self._guest(1, phone="", normalized="13800001111")
        backfilled = mig0007.backfill_phone_pii(_AppsShim(), None)
        self.assertIsNone(backfilled)
        guest.refresh_from_db()
        self.assertTrue(guest.phone_ciphertext)
        self.assertTrue(guest.phone_hash)
        self.assertEqual(guest_services.decrypt_phone(guest.phone_ciphertext),
                         "13800001111")
        self.assertEqual(guest.phone_hash,
                         guest_services.phone_hash("13800001111"))

    def test_phone_only_guest_is_backfilled_from_raw_value(self):
        """phone 有值、phone_normalized 为空：从原始值标准化后回填。"""
        guest = self._guest(2, phone="138-0000-1112", normalized="")
        mig0007.backfill_phone_pii(_AppsShim(), None)
        guest.refresh_from_db()
        self.assertEqual(guest_services.decrypt_phone(guest.phone_ciphertext),
                         "13800001112")
        self.assertEqual(guest.phone_hash,
                         guest_services.phone_hash("13800001112"))

    def test_both_empty_guests_are_not_fabricated(self):
        """两个字段均为空：不产生伪造号码。"""
        guest = self._guest(3, phone="", normalized="")
        mig0007.backfill_phone_pii(_AppsShim(), None)
        guest.refresh_from_db()
        self.assertFalse(guest.phone_ciphertext)
        self.assertFalse(guest.phone_hash)

    def test_clear_guard_blocks_unbackfilled_plaintext(self):
        """明文非空但密文/哈希缺失：清明文步骤必须显式失败且不丢数据。"""
        guest = self._guest(4, phone="", normalized="13800001114")
        # 故意不回填，直接触发清明文 → 守卫拦截
        with self.assertRaises(RuntimeError) as ctx:
            mig0007.clear_phone_plaintext(_AppsShim(), None)
        self.assertIn("WB-124", str(ctx.exception))
        guest.refresh_from_db()
        # 明文保留，未被清空
        self.assertEqual(guest.phone_normalized, "13800001114")
        # 回填成功后守卫放行（本行已回填则不再拦截）
        mig0007.backfill_phone_pii(_AppsShim(), None)
        mig0007.clear_phone_plaintext(_AppsShim(), None)
        guest.refresh_from_db()
        self.assertEqual(guest_services.decrypt_phone(guest.phone_ciphertext),
                         "13800001114")
        self.assertIsNone(guest.phone_normalized)

    def test_clear_step_is_irreversible(self):
        """WB-124：清明文 RunPython 无 reverse_code，迁移声明为不可逆。"""
        from django.db import migrations

        clear_ops = [
            op for op in mig0007.Migration.operations
            if isinstance(op, migrations.RunPython)
            and op.code is mig0007.clear_phone_plaintext
        ]
        self.assertEqual(len(clear_ops), 1)
        self.assertFalse(clear_ops[0].reversible,
                         "清明文步骤必须不可逆：回退迁移应被 Django 显式拒绝")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class PiiKeyIsolationTests(TestCase):
    """WB-123：独立 PII 密钥必须真正进入加密/哈希派生链路。"""

    PII_KEY = "p" * 40

    def test_settings_pii_key_is_used_for_hash_and_cipher(self):
        default_hash = guest_services.phone_hash("13800001121")
        with override_settings(DJANGO_PII_KEY=self.PII_KEY):
            pii_hash = guest_services.phone_hash("13800001121")
            expected = hmac_mod.new(
                hashlib.sha256(self.PII_KEY.encode("utf-8")).digest(),
                b"13800001121", hashlib.sha256).hexdigest()
            self.assertEqual(pii_hash, expected)
            self.assertNotEqual(pii_hash, default_hash,
                                "PII 密钥必须独立于 SECRET_KEY 派生")
            cipher = guest_services.encrypt_phone("13800001121")
        # PII_KEY 稳定、SECRET_KEY 轮换：密文仍可解密（密钥隔离验收）
        with override_settings(DJANGO_PII_KEY=self.PII_KEY,
                               SECRET_KEY="rotated-" + "s" * 40):
            self.assertEqual(
                guest_services.decrypt_phone(cipher), "13800001121")

    def test_production_without_pii_key_fails_fast(self):
        with override_settings(DJANGO_PII_KEY="", IS_PRODUCTION=True):
            with self.assertRaises(ImproperlyConfigured):
                guest_services._pii_key_bytes()

    def test_migration_key_derivation_matches_service(self):
        """迁移内联派生与服务层必须同源（同 PII_KEY 下哈希一致）。"""
        with override_settings(DJANGO_PII_KEY=self.PII_KEY):
            service_hash = guest_services.phone_hash("13800001122")
            mig_hash = mig0007._phone_hash("13800001122")
            self.assertEqual(service_hash, mig_hash)
        with override_settings(DJANGO_PII_KEY="", IS_PRODUCTION=True):
            with self.assertRaises(RuntimeError):
                mig0007._pii_key_bytes()
