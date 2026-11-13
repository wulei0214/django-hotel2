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
    (
        "trg_billing_shiftmovement_open_shift_insert",
        "CREATE TRIGGER trg_billing_shiftmovement_open_shift_insert "
        "BEFORE INSERT ON billing_cashiershiftmovement FOR EACH ROW "
        "BEGIN "
        "DECLARE v_shift_status VARCHAR(16) DEFAULT NULL; "
        "DECLARE v_shift_cashier BIGINT DEFAULT NULL; "
        "SELECT status, cashier_id INTO v_shift_status, v_shift_cashier "
        "FROM billing_cashiershift WHERE id = NEW.shift_id FOR UPDATE; "
        "IF v_shift_status IS NULL OR v_shift_status <> 'OPEN' "
        "OR v_shift_cashier <> NEW.created_by_id THEN "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 cash movement requires its own open cashier shift'; "
        "END IF; "
        "END",
    ),
)


class Command(BaseCommand):
    help = "Install six MySQL append-only triggers plus one open-cashier-shift insert guard."

    def handle(self, *args, **options):
        if connection.vendor != "mysql":
            raise CommandError(
                "只允许在真实 MySQL 上安装 append-only triggers；SQLite 不支持此验收。")
        with connection.cursor() as cursor:
            for name, statement in TRIGGERS:
                cursor.execute(f"DROP TRIGGER IF EXISTS `{name}`")
                cursor.execute(statement)
        self.stdout.write(self.style.SUCCESS(
            "已安装 6 条只追加触发器及 1 条现金变动 OPEN 班次插入保护触发器。"))
