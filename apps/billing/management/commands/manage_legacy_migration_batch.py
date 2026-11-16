"""登记/批准/驳回/关闭历史迁移批次（PENDING → ACTIVE → CLOSED；SoD 强制）。"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.billing.legacy_batches import (
    LegacyBatchError,
    approve_batch,
    close_batch,
    register_batch,
    reject_batch,
)
from apps.billing.models import LegacyMigrationBatch
from apps.core.models import Property


def _user(username):
    user = get_user_model().objects.filter(username=username).first()
    if user is None:
        raise CommandError(f"账号不存在：{username}")
    return user


class Command(BaseCommand):
    help = (
        "历史迁移批次授权闭环：register（登记 PENDING）/ approve（批准 ACTIVE）/ "
        "reject（驳回）/ close（关闭终态）/ list（查看）。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--action",
            choices=("list", "register", "approve", "reject", "close"),
            default="list",
        )
        parser.add_argument("--batch-uid", default="")
        parser.add_argument("--property", default="", help="物业编码，如 DEFAULT")
        parser.add_argument("--source-models", default="Folio,FinanceRecord")
        parser.add_argument("--pk-min", type=int, default=None)
        parser.add_argument("--pk-max", type=int, default=None)
        parser.add_argument("--pks", default="", help="逗号分隔的 id 清单（替代 pk 区间）")
        parser.add_argument("--executed-by", default="", help="拟执行人账号")
        parser.add_argument("--approved-by", default="", help="批准人账号")
        parser.add_argument("--basis", default="", help="审批依据（文档/Issue 链接）")
        parser.add_argument("--reason", default="", help="驳回原因")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "list":
            return self._list()
        uid = options["batch_uid"].strip()
        if not uid:
            raise CommandError("--batch-uid 必填")
        if action == "register":
            return self._register(uid, options)
        batch = LegacyMigrationBatch.objects.filter(batch_uid=uid).first()
        if batch is None:
            raise CommandError(f"批次不存在：{uid}")
        try:
            if action == "approve":
                approve_batch(
                    batch=batch,
                    approved_by=_user(options["approved_by"]),
                    basis=options["basis"],
                )
            elif action == "reject":
                reject_batch(
                    batch=batch,
                    approved_by=_user(options["approved_by"]),
                    reason=options["reason"],
                )
            else:
                close_batch(
                    batch=batch,
                    actor=_user(options["approved_by"]) if options["approved_by"] else None,
                )
        except LegacyBatchError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"{uid} → {batch.status}"))

    def _list(self):
        batches = LegacyMigrationBatch.objects.select_related(
            "scope_property", "approved_by", "executed_by"
        ).order_by("-created_at")
        if not batches:
            self.stdout.write("尚无迁移批次")
            return
        for batch in batches:
            self.stdout.write(
                f"{batch.batch_uid}\t{batch.status}\t{batch.scope_property.code}\t"
                f"models={batch.scope_source_models}\tscope={batch.scope_record_range}\t"
                f"approved_by={getattr(batch.approved_by, 'username', '-')}\t"
                f"executed_by={getattr(batch.executed_by, 'username', '-')}"
            )

    def _register(self, uid, options):
        code = options["property"].strip()
        prop = Property.objects.filter(code=code).first()
        if prop is None:
            raise CommandError(f"物业不存在：{code or '(未提供 --property)'}")
        if not options["executed_by"]:
            raise CommandError("--executed-by 必填（拟执行人）")
        if options["pks"]:
            record_range = {"pks": [item for item in options["pks"].split(",") if item]}
        else:
            if options["pk_min"] is None or options["pk_max"] is None:
                raise CommandError("须提供 --pks 或 --pk-min/--pk-max")
            record_range = {"pk_min": options["pk_min"], "pk_max": options["pk_max"]}
        try:
            batch = register_batch(
                batch_uid=uid,
                property_obj=prop,
                source_models=options["source_models"].split(","),
                record_range=record_range,
                executed_by=_user(options["executed_by"]),
                approval_basis=options["basis"],
            )
        except LegacyBatchError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"已登记批次 {batch.batch_uid}（{batch.status}）"))
