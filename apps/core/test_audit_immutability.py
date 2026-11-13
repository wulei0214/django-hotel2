"""P0-06（闭环补齐 2.0）：AuditEvent 数据库层不可变性验收。

模型层：save（pk 非空拒绝）与 delete（一律拒绝）已实现；
本文件补齐绕过路径测试与 DB 触发器部署说明。

DB 层（运维部署项，apps/core/sql/audit_immutability.sql）：
应用账号对 core_auditevent 的 UPDATE/DELETE 由 MySQL 触发器拒绝；
迁移/运维账号单独保管，不在应用 settings 中。
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.core.models import AuditEvent, Property


class AuditEventImmutabilityTests(TestCase):
    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        self.event = AuditEvent.record(
            action="test.immutability",
            entity_type="Test",
            entity_id="1",
            property_obj=self.prop,
            after={"k": "v"},
            reason="不可变性测试",
        )

    def test_model_save_rejects_update(self):
        self.event.reason = "篡改"
        with self.assertRaises(ValueError):
            self.event.save()

    def test_model_delete_rejects(self):
        with self.assertRaises(ValueError):
            self.event.delete()

    def test_queryset_update_is_blocked_before_database_write(self):
        """ORM 批量更新必须被拦截；生产 MySQL 另部署数据库触发器防原生 SQL 旁路。"""
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=self.event.pk).update(reason="orm-bypass")
        self.event.refresh_from_db()
        self.assertEqual(self.event.reason, "不可变性测试")

    def test_queryset_delete_is_blocked(self):
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=self.event.pk).delete()
        self.assertTrue(AuditEvent.objects.filter(pk=self.event.pk).exists())

    def test_record_is_append_only_utility(self):
        """record() 只创建新行；同 action 重复记录不合并（审计事实链）"""
        before = AuditEvent.objects.filter(action="test.immutability").count()
        AuditEvent.record(
            action="test.immutability", entity_type="Test", entity_id="1", property_obj=self.prop
        )
        after = AuditEvent.objects.filter(action="test.immutability").count()
        self.assertEqual(after, before + 1)
