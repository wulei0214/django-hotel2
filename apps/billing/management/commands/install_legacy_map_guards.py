"""Install MySQL guards for the legacy order-reservation map (append-only facts)."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

TRIGGERS = (
    (
        "trg_billing_legacyordermap_no_update",
        "CREATE TRIGGER trg_billing_legacyordermap_no_update "
        "BEFORE UPDATE ON billing_legacyorderreservationmap FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 legacy order-reservation map is immutable'",
    ),
    (
        "trg_billing_legacyordermap_no_delete",
        "CREATE TRIGGER trg_billing_legacyordermap_no_delete "
        "BEFORE DELETE ON billing_legacyorderreservationmap FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'PMS2 legacy order-reservation map cannot be deleted'",
    ),
)


class Command(BaseCommand):
    help = "Install two MySQL triggers making the legacy order-reservation map append-only."

    def handle(self, *args, **options):
        if connection.vendor != "mysql":
            raise CommandError("只允许在真实 MySQL 上安装关系映射触发器；SQLite 不支持此验收。")
        with connection.cursor() as cursor:
            for name, statement in TRIGGERS:
                cursor.execute(f"DROP TRIGGER IF EXISTS `{name}`")
                cursor.execute(statement)
        self.stdout.write(
            self.style.SUCCESS("已安装 2 条关系映射触发器（UPDATE / DELETE 一律拒绝）。")
        )
