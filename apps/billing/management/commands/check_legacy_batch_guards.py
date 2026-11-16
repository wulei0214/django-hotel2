"""Verify MySQL legacy-migration-batch guards (N12 database layer)."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

EXPECTED = {
    "trg_billing_legacybatch_no_delete": (
        "billing_legacymigrationbatch",
        "DELETE",
        "BEFORE",
    ),
    "trg_billing_legacybatch_guard_update": (
        "billing_legacymigrationbatch",
        "UPDATE",
        "BEFORE",
    ),
}

# 更新触发器必须真实包含的分支片段：缺任一片段即视为定义被裁剪。
REQUIRED_FRAGMENTS = (
    "OLD.STATUS = 'CLOSED'",
    "NEW.STATUS <> OLD.STATUS",
    "'PENDING'",
    "'ACTIVE'",
    "'CLOSED'",
    "'REJECTED'",
    "NEW.SCOPE_PROPERTY_ID",
    "NEW.EXECUTED_BY_ID",
    "NEW.SCOPE_SOURCE_MODELS",
    "NEW.SCOPE_RECORD_RANGE",
)


class Command(BaseCommand):
    help = "Verify the two MySQL triggers that guard legacy migration batch authority."

    def handle(self, *args, **options):
        if connection.vendor != "mysql":
            raise CommandError("此检查必须连接真实 MySQL；SQLite 结果不能作为生产数据库验收。")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, "
                "ACTION_TIMING, ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE()"
            )
            actual = {row[0]: row[1:] for row in cursor.fetchall()}
        missing = sorted(set(EXPECTED) - set(actual))
        incorrect = {}
        for name, expected in EXPECTED.items():
            if name not in actual:
                continue
            observed = actual[name]
            sql = observed[3].upper()
            valid = observed[:3] == expected and "SIGNAL SQLSTATE '45000'" in sql
            if name == "trg_billing_legacybatch_guard_update":
                valid = valid and all(fragment in sql for fragment in REQUIRED_FRAGMENTS)
            if not valid:
                incorrect[name] = {"expected": expected, "actual": observed}
        if missing or incorrect:
            details = []
            if missing:
                details.append("缺失触发器：" + ", ".join(missing))
            if incorrect:
                details.append("定义不匹配：" + repr(incorrect))
            raise CommandError("迁移批次数据库守卫未通过；" + "；".join(details))
        self.stdout.write(
            self.style.SUCCESS(
                "迁移批次守卫通过：删除拒绝 + 状态白名单 + ACTIVE scope 冻结均存在。"
            )
        )
