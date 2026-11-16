"""历史财务迁移执行命令（C1~C5 分类 + 受控入口逐条建凭证/映射）。

安全闭环（迁移方案 §5.2）：
1. 交互式密码认证（Django ``authenticate``，与 Admin 同一认证链）；
2. 校验账号：is_active / is_staff / pk == batch.executed_by_id / ≠ batch.approved_by；
3. 认证通过后铸造签名执行凭证（TTL 900s，nonce 单次）；
4. 逐条迁移均由 ``BillingService.post_legacy_migration_entry`` 二次验签 + scope 校验。

``--dry-run`` 全程只读（不认证、不写表），只输出分类统计与可选计划 JSON。
"""

import getpass

from django.contrib.auth import authenticate
from django.core.management.base import BaseCommand, CommandError

from apps.billing.legacy_credentials import mint_credential, record_denial
from apps.billing.legacy_migration import (
    LegacyMigrationError,
    build_plan,
    execute_plan,
    load_mapping,
    plan_summary,
    reconcile_batch,
    record_failures,
    write_plan_json,
)
from apps.billing.models import LegacyMigrationBatch


class Command(BaseCommand):
    help = "按批次授权执行历史财务迁移：dry-run 出分类清单，执行时经受控入口逐条建凭证。"

    def add_arguments(self, parser):
        parser.add_argument("--batch-uid", required=True)
        parser.add_argument("--mapping-file", required=True, help="人工确认映射 JSON 路径")
        parser.add_argument("--dry-run", action="store_true", help="只分类不写库")
        parser.add_argument("--plan-out", default="", help="把分类清单写到指定 JSON 路径")
        parser.add_argument("--executor", default="", help="执行人账号（须 == 批次 executed_by）")

    def handle(self, *args, **options):
        batch = LegacyMigrationBatch.objects.filter(batch_uid=options["batch_uid"].strip()).first()
        if batch is None:
            raise CommandError(f"批次不存在：{options['batch_uid']}")
        try:
            mapping = load_mapping(options["mapping_file"])
        except LegacyMigrationError as exc:
            raise CommandError(str(exc)) from exc
        try:
            plan = build_plan(batch=batch, mapping=mapping)
        except LegacyMigrationError as exc:
            raise CommandError(str(exc)) from exc

        summary = plan_summary(plan)
        self.stdout.write(
            "分类统计：" + "，".join(f"{key}={value}" for key, value in summary.items())
        )
        for decision in plan.decisions:
            if decision.category in {"C4", "C5"}:
                self.stdout.write(
                    f"  {decision.category}\t{decision.source_model}#{decision.source_pk}"
                    f"\t{decision.reason}"
                )
        if options["plan_out"]:
            write_plan_json(plan, options["plan_out"])
            self.stdout.write(f"分类清单已写入 {options['plan_out']}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run：未写任何凭证、映射或审计。"))
            return

        executor = self._authenticate(batch, options)
        result = execute_plan(
            batch=batch,
            plan=plan,
            credential_factory=lambda: mint_credential(
                batch_uid=batch.batch_uid, executor_user_pk=executor.pk
            ),
            executor=executor,
        )
        counts = result["counts"]
        self.stdout.write(
            "执行结果：" + "，".join(f"{key}={value}" for key, value in counts.items())
        )
        if result["failures"]:
            record_failures(batch=batch, failures=result["failures"])
            for failure in result["failures"]:
                self.stdout.write(self.style.ERROR(f"  失败：{failure}"))
        report = reconcile_batch(batch)
        for diff in report["diffs"]:
            self.stdout.write(self.style.WARNING(f"  对账差异：{diff}"))
        if result["failures"] or report["diffs"]:
            raise CommandError("迁移未完全通过：存在失败记录或对账差异，禁止批次收口")
        self.stdout.write(self.style.SUCCESS("迁移完成且对账无差异。"))

    def _authenticate(self, batch, options):
        username = options["executor"].strip()
        if not username:
            raise CommandError("--executor 必填（须为批次登记的 executed_by）")
        password = getpass.getpass(f"请输入 {username} 的密码以铸造执行凭证：")
        user = authenticate(username=username, password=password)
        reason = ""
        if user is None:
            reason = "密码认证失败"
        elif not user.is_active:
            reason = "账号已停用"
        elif not user.is_staff:
            reason = "账号非平台身份（is_staff=False）"
        elif user.pk != batch.executed_by_id:
            reason = "认证账号与批次登记的 executed_by 不一致"
        elif batch.approved_by_id and user.pk == batch.approved_by_id:
            reason = "批准人不得以本人账号执行迁移（职责分离）"
        if reason:
            record_denial(batch=batch, reason=reason, actor_id=getattr(user, "pk", None))
            raise CommandError(f"执行身份校验失败：{reason}")
        return user
