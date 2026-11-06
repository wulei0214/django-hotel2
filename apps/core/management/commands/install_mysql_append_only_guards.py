"""Install append-only MySQL triggers for PMS2 audit, ledger and cash movement facts."""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


TRIGGERS = (
    (
        "trg_core_auditevent_no_update",
        "CREATE TRIGGER trg_core_auditevent_no_update "
        "BEFORE UPDATE ON core_auditevent FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 audit events are append-only'",
    ),
    (
        "trg_core_auditevent_no_delete",
        "CREATE TRIGGER trg_core_auditevent_no_delete "
        "BEFORE DELETE ON core_auditevent FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 audit events cannot be deleted'",
    ),
    (
        "trg_billing_ledgerentry_no_update",
        "CREATE TRIGGER trg_billing_ledgerentry_no_update "
        "BEFORE UPDATE ON billing_ledgerentry FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 ledger entries are immutable; post a reversal'",
    ),
    (
        "trg_billing_ledgerentry_no_delete",
        "CREATE TRIGGER trg_billing_ledgerentry_no_delete "
        "BEFORE DELETE ON billing_ledgerentry FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 ledger entries cannot be deleted'",
    ),
    (
        "trg_billing_shiftmovement_no_update",
        "CREATE TRIGGER trg_billing_shiftmovement_no_update "
        "BEFORE UPDATE ON billing_cashiershiftmovement FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 cashier shift movements are append-only'",
    ),
    (
        "trg_billing_shiftmovement_no_delete",
        "CREATE TRIGGER trg_billing_shiftmovement_no_delete "
        "BEFORE DELETE ON billing_cashiershiftmovement FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 cashier shift movements cannot be deleted'",
    ),
)


class Command(BaseCommand):
    help = "Install six MySQL append-only triggers for PMS2 audit and financial facts."

    def handle(self, *args, **options):
        if connection.vendor != "mysql":
            raise CommandError(
                "只允许在真实 MySQL 上安装 append-only triggers；SQLite 不支持此验收。")
        with connection.cursor() as cursor:
            for name, statement in TRIGGERS:
                cursor.execute(f"DROP TRIGGER IF EXISTS `{name}`")
                cursor.execute(statement)
        self.stdout.write(self.style.SUCCESS(
            "已安装 6 条 MySQL BEFORE UPDATE/DELETE 只追加事实触发器。"))
