"""ARCH-11 §11.5：MySQL 追加写保护触发器的**行为验证**（非仅定义校验）。

现状缺口：CI 只跑 `install_mysql_append_only_guards` +
`check_mysql_append_only_guards`，后者核对 information_schema.TRIGGERS 里
触发器定义是否存在，但**从未验证触发器能否真正阻止 UPDATE/DELETE**。
定义存在而逻辑写错（如漏了 FOR EACH ROW、条件写反）仍会绿灯。

本测试直接对真实 MySQL 表执行 UPDATE/DELETE，断言被触发器拒绝。
SQLite 无触发器，整类跳过（`select_for_update` 与触发器均为 MySQL 语义）。

对应任务书 §11.5：
- "检查管理命令、后台操作和数据库权限是否可能绕过审计保护"；
- "关键审计记录具备追加写保护"必须有可执行证据。
"""

import unittest

from django.db import OperationalError, connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from apps.core.models import AuditEvent, Property


def _can_create_triggers() -> bool:
    """检测当前 MySQL 账号是否允许创建触发器。

    本机开发库若未授予 SUPER 且开启 binary log，创建触发器会报 1419
    （log_bin_trust_function_creators 关闭）。此时无法验证触发器行为，
    只能跳过——**不得**因此判定通过；CI 的 MySQL 容器具备该权限，会真实执行。
    """
    if connection.vendor != "mysql":
        return False
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER IF EXISTS arch11_probe_trigger")
            cursor.execute(
                "CREATE TRIGGER arch11_probe_trigger BEFORE UPDATE ON core_auditevent "
                "FOR EACH ROW SET @arch11_probe = 1"
            )
            cursor.execute("DROP TRIGGER IF EXISTS arch11_probe_trigger")
        return True
    except OperationalError:
        return False


TRIGGERS_AVAILABLE = _can_create_triggers()


@unittest.skipUnless(
    connection.vendor == "mysql",
    "追加写触发器仅 MySQL 存在；SQLite 下无触发器语义，须以 MySQL 结果为准",
)
@unittest.skipUnless(
    TRIGGERS_AVAILABLE,
    "当前 MySQL 账号无创建触发器权限（1419：SUPER + log_bin_trust_function_creators）；"
    "本机跳过，由 CI MySQL 容器执行真实验证",
)
class AuditEventTriggerEnforcementTests(TransactionTestCase):
    """验证已安装的触发器真的拒绝篡改，而不是只有定义。

    使用 TransactionTestCase：触发器是 schema 级 DDL，不能包在 TestCase 的
    外层事务里（MySQL 下 DDL 会隐式提交并破坏事务上下文）。

    flush 抑制：`available_apps = []` 让 Django 的 `_fixture_teardown` 不再
    执行全库 flush（TRUNCATE 会被 append-only 触发器拦截）。本类每个用例
    自建并清理自有数据，不依赖 flush 隔离。
    """

    # 关键：抑制全库 flush，避免 TRUNCATE 被本类安装的触发器拦截
    available_apps: list = []

    def setUp(self):
        # 确保本测试库已安装守卫（CI 在迁移后安装；本地测试库需显式安装）
        from django.core.management import call_command

        call_command("install_mysql_append_only_guards", verbosity=0)
        self.property = Property.objects.filter(code="DEFAULT").first()
        if self.property is None:
            self.property = Property.objects.create(code="DEFAULT", name="默认物业")
        self.event = AuditEvent.record(
            action="arch11.trigger_probe",
            entity_type="Probe",
            entity_id="1",
            property_obj=self.property,
            reason="触发器行为验证样本",
        )

    def _drop_guard_triggers(self):
        with connection.cursor() as cursor:
            for name in (
                "trg_core_auditevent_no_update",
                "trg_core_auditevent_no_delete",
            ):
                cursor.execute(f"DROP TRIGGER IF EXISTS `{name}`")

    @classmethod
    def tearDownClass(cls):
        """类结束时移除本类安装的触发器，恢复测试前状态。"""
        with connection.cursor() as cursor:
            for name in (
                "trg_core_auditevent_no_update",
                "trg_core_auditevent_no_delete",
            ):
                cursor.execute(f"DROP TRIGGER IF EXISTS `{name}`")
        super().tearDownClass()

    def _raw_execute(self, sql, params):
        """绕过 ORM 守卫，直接发 SQL——模拟管理命令/裸连接旁路。"""
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

    def test_raw_update_is_rejected_by_trigger(self):
        with self.assertRaises(Exception) as ctx:
            self._raw_execute(
                "UPDATE core_auditevent SET reason = %s WHERE id = %s",
                ["tampered", self.event.pk],
            )
        message = str(ctx.exception).lower()
        self.assertTrue(
            any(
                token in message for token in ("append-only", "append_only", "trigger", "immutable")
            ),
            f"UPDATE 未被追加写保护拒绝，实际异常：{ctx.exception}",
        )
        self.assertNotEqual(AuditEvent.objects.get(pk=self.event.pk).reason, "tampered")

    def test_raw_delete_is_rejected_by_trigger(self):
        with self.assertRaises(Exception) as ctx:
            self._raw_execute("DELETE FROM core_auditevent WHERE id = %s", [self.event.pk])
        message = str(ctx.exception).lower()
        self.assertTrue(
            any(
                token in message for token in ("append-only", "append_only", "trigger", "immutable")
            ),
            f"DELETE 未被追加写保护拒绝，实际异常：{ctx.exception}",
        )
        self.assertTrue(AuditEvent.objects.filter(pk=self.event.pk).exists())

    def test_append_still_works_after_guard_installed(self):
        """守卫不得误伤正常追加——否则审计本身无法写入。"""
        with CaptureQueriesContext(connection) as ctx:
            fresh = AuditEvent.record(
                action="arch11.trigger_probe_append",
                entity_type="Probe",
                entity_id="2",
                property_obj=self.property,
            )
        self.assertTrue(AuditEvent.objects.filter(pk=fresh.pk).exists())
        self.assertGreaterEqual(len(ctx.captured_queries), 1)

    def test_management_command_refuses_when_guards_missing(self):
        """守卫被移除后，check 命令必须失败——防止"未安装却报通过"。"""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER IF EXISTS trg_core_auditevent_no_update")
        try:
            with self.assertRaises(CommandError):
                call_command("check_mysql_append_only_guards", verbosity=0)
        finally:
            call_command("install_mysql_append_only_guards", verbosity=0)
