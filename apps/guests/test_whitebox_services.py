# -*- coding: utf-8 -*-
"""白盒服务层单元测试（子智能体 1：guests/services.py）。

覆盖 45-whitebox-services.md 登记缺陷的针对性回归：
  * WB-09 GuestService.create_guest 无事务保护（电话写失败产生孤儿档案）；
  * WB-10 _mask 对短值（<6 字符）原样透出，审计脱敏失效。
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest, GuestPhone
from apps.guests.services import GuestService, _mask


class MaskDisciplineTests(TestCase):
    """WB-10：审计脱敏不得把短值明文写进 AuditEvent。"""

    def test_mask_does_not_leak_short_values(self):
        self.assertEqual(_mask("12345"), "*****")
        self.assertEqual(_mask("123"), "***")
        self.assertEqual(_mask(""), "")
        self.assertEqual(_mask(None), "")
        # >=6 字符保持既有口径（前 2 + 星号 + 后 2）
        self.assertEqual(_mask("13800001234"), "13*******34")
        self.assertEqual(_mask("123456"), "12**56")

    def test_create_guest_audit_masks_short_phone(self):
        property_obj = Property.objects.create(code="WBG", name="白盒客人酒店")
        operator = User.objects.create_user(username="wbg-operator")
        guest = GuestService.create_guest(
            property_obj=property_obj, full_name="短号客人", phone="12345",
            created_by=operator)
        event = AuditEvent.objects.get(
            action="guest.create", entity_id=guest.pk)
        self.assertEqual(event.after_json.get("phone_masked"), "*****")
        self.assertNotIn("12345", event.after_json.get("phone_masked", ""))


class CreateGuestAtomicityTests(TestCase):
    """WB-09：客人档案与主电话必须同事务落库。"""

    def test_create_guest_rolls_back_when_phone_write_fails(self):
        property_obj = Property.objects.create(code="WBG2", name="白盒原子酒店")
        with mock.patch.object(
                GuestPhone.objects, "create",
                side_effect=RuntimeError("模拟电话写入失败")):
            with self.assertRaises(RuntimeError):
                GuestService.create_guest(
                    property_obj=property_obj, full_name="原子客人",
                    phone="13800009999")
        self.assertFalse(
            Guest.objects.filter(property=property_obj, full_name="原子客人").exists())
