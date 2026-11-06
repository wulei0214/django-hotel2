"""Verify MySQL append-only triggers required by PMS2 production operations."""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


EXPECTED = {
    "trg_core_auditevent_no_update": ("core_auditevent", "UPDATE", "BEFORE"),
    "trg_core_auditevent_no_delete": ("core_auditevent", "DELETE", "BEFORE"),
    "trg_billing_ledgerentry_no_update": ("billing_ledgerentry", "UPDATE", "BEFORE"),
    "trg_billing_ledgerentry_no_delete": ("billing_ledgerentry", "DELETE", "BEFORE"),
    "trg_billing_shiftmovement_no_update": (
        "billing_cashiershiftmovement", "UPDATE", "BEFORE"),
    "trg_billing_shiftmovement_no_delete": (
        "billing_cashiershiftmovement", "DELETE", "BEFORE"),
    "trg_billing_shiftmovement_open_shift_insert": (
        "billing_cashiershiftmovement", "INSERT", "BEFORE"),
}


class Command(BaseCommand):
    help = "Verify the seven MySQL financial integrity triggers for PMS2 audit/ledger/cash facts."

    def handle(self, *args, **options):
        if connection.vendor != "mysql":
            raise CommandError(
                "此检查必须连接真实 MySQL；SQLite 结果不能作为生产数据库验收。")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, "
                "ACTION_TIMING, ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE()")
            actual = {
                row[0]: (row[1], row[2], row[3], row[4])
                for row in cursor.fetchall()
            }
        missing = sorted(set(EXPECTED) - set(actual))
        incorrect = {}
        for name, expected in EXPECTED.items():
            if name not in actual:
                continue
            observed = actual[name]
            if observed[:3] != expected or "SIGNAL SQLSTATE '45000'" not in observed[3].upper():
                incorrect[name] = {
                    "expected": expected + ("SIGNAL SQLSTATE '45000'",),
                    "actual": observed,
                }
        if missing or incorrect:
            details = []
            if missing:
                details.append("缺失触发器：" + ", ".join(missing))
            if incorrect:
                details.append("定义不匹配：" + repr(incorrect))
            raise CommandError("MySQL 只追加事实保护未通过；" + "；".join(details))
        self.stdout.write(self.style.SUCCESS(
            "MySQL financial guard 通过：6 条只追加触发器和 1 条现金变动班次保护触发器全部存在。"))
