"""拦截验证测试（治理报告证据）：guest_name_reveal 权限码在各层均不可达。

以 Django 测试形式运行，确保使用测试库与真实迁移链：
    manage.py test apps.guests.test_name_reveal_blocked
"""

from django.contrib.auth.models import Group, Permission
from django.test import TestCase

from apps.identity import registry


class GuestNameRevealBlockedTests(TestCase):
    """NS-E-05 未逐项批准前，该权限码不得存在于任何一层。"""

    TARGET = "hotelapp.guest_name_reveal"
    CODENAME = "guest_name_reveal"

    def test_permission_not_in_database(self):
        """迁移链中不含创建该权限的步骤（0011 已移除）。"""
        self.assertEqual(
            Permission.objects.filter(codename=self.CODENAME).count(),
            0,
            "auth_permission 中仍存在 guest_name_reveal，权限码未被拦截",
        )

    def test_no_group_grants(self):
        perms = Permission.objects.filter(codename=self.CODENAME)
        self.assertEqual(
            Group.objects.filter(permissions__in=perms).count(),
            0,
            "仍有岗位组被授予 guest_name_reveal",
        )

    def test_not_registered_in_special_permissions(self):
        codes = {item.get("codename") for item in registry.SPECIAL_PERMISSIONS}
        self.assertNotIn(self.TARGET, codes, "registry.SPECIAL_PERMISSIONS 仍登记该权限码")

    def test_reveal_function_removed(self):
        from apps.guests import name_pii

        self.assertFalse(
            hasattr(name_pii, "reveal_guest_name"),
            "name_pii.reveal_guest_name 应已移除（依赖未获批权限码）",
        )

    def test_encryption_capability_retained(self):
        """拦截不得误伤合规的加密/哈希/双写能力。"""
        from apps.guests import name_pii

        for name in (
            "normalize_guest_name",
            "guest_name_hash",
            "encrypt_guest_name",
            "decrypt_guest_name",
            "fill_guest_name_pii",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(name_pii, name), f"{name} 应保留（加密能力不受权限拦截影响）"
                )
