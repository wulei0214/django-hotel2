"""Install MySQL guards for legacy migration batches (CLOSED terminal / ACTIVE scope frozen)."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

TRIGGERS = (
    (
        "trg_billing_legacybatch_no_delete",
        "CREATE TRIGGER trg_billing_legacybatch_no_delete "
        "BEFORE DELETE ON billing_legacymigrationbatch FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 migration batches cannot be deleted'",
    ),
    (
        "trg_billing_legacybatch_guard_update",
        "CREATE TRIGGER trg_billing_legacybatch_guard_update "
        "BEFORE UPDATE ON billing_legacymigrationbatch FOR EACH ROW "
        "BEGIN "
        "IF OLD.status = 'CLOSED' THEN "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 closed migration batch is terminal'; "
        "END IF; "
        "IF NEW.status <> OLD.status THEN "
        "IF NOT ((OLD.status = 'PENDING' "
        "AND NEW.status IN ('ACTIVE', 'CLOSED', 'REJECTED')) "
        "OR (OLD.status = 'ACTIVE' AND NEW.status = 'CLOSED')) THEN "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 illegal migration batch status transition'; "
        "END IF; "
        "END IF; "
        "IF OLD.status = 'ACTIVE' AND ("
        "NOT (NEW.scope_property_id <=> OLD.scope_property_id) "
        "OR NOT (NEW.executed_by_id <=> OLD.executed_by_id) "
        "OR NOT (CAST(NEW.scope_source_models AS CHAR) "
        "<=> CAST(OLD.scope_source_models AS CHAR)) "
        "OR NOT (CAST(NEW.scope_record_range AS CHAR) "
        "<=> CAST(OLD.scope_record_range AS CHAR))"
        ") THEN "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 active migration batch scope is frozen'; "
        "END IF; "
        "END",
    ),
)


class Command(BaseCommand):
    help = "Install two MySQL triggers guarding legacy migration batch authority."

    def handle(self, *args, **options):
        if connection.vendor != "mysql":
            raise CommandError("只允许在真实 MySQL 上安装迁移批次触发器；SQLite 不支持此验收。")
        with connection.cursor() as cursor:
            for name, statement in TRIGGERS:
                cursor.execute(f"DROP TRIGGER IF EXISTS `{name}`")
                cursor.execute(statement)
        self.stdout.write(
            self.style.SUCCESS("已安装 2 条迁移批次触发器（CLOSED 终态 / ACTIVE scope 冻结）。")
        )
