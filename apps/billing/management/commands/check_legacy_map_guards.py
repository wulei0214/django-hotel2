"""Verify MySQL guards on the legacy order-reservation map (§1.5.2 database layer)."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

EXPECTED = {
    "trg_billing_legacyordermap_no_update": (
        "billing_legacyorderreservationmap",
        "UPDATE",
        "BEFORE",
    ),
    "trg_billing_legacyordermap_no_delete": (
        "billing_legacyorderreservationmap",
        "DELETE",
        "BEFORE",
    ),
}


class Command(BaseCommand):
    help = "Verify the two MySQL triggers guarding the legacy order-reservation map."

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
            if observed[:3] != expected or "SIGNAL SQLSTATE '45000'" not in sql:
                incorrect[name] = {"expected": expected, "actual": observed}
        if missing or incorrect:
            details = []
            if missing:
                details.append("缺失触发器：" + ", ".join(missing))
            if incorrect:
                details.append("定义不匹配：" + repr(incorrect))
            raise CommandError("关系映射数据库守卫未通过；" + "；".join(details))
        self.stdout.write(self.style.SUCCESS("关系映射守卫通过：UPDATE 与 DELETE 均被触发器拒绝。"))
