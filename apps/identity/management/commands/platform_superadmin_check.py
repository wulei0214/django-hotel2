"""平台超管三项不变量体检（方案 §6.1 第 8 项）。

只读报告：列出所有 is_superuser 用户的 is_active / is_superuser / is_staff
状态与异常分类，供运维核对半状态账号（如 is_staff 被误关导致 /platform/ 403）。
本命令不自动修改任何数据——无论是否带 --dry-run 都只输出报告。
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

# 异常分类：三项不变量 is_active + is_superuser + is_staff
CLASSIFICATION_OK = "OK"
CLASSIFICATION_INACTIVE = "INACTIVE_SUPERUSER"          # is_active=False
CLASSIFICATION_NO_STAFF = "STAFF_FLAG_MISSING"          # is_staff=False
CLASSIFICATION_INACTIVE_AND_NO_STAFF = "INACTIVE_AND_NO_STAFF"  # 双缺


def classify(user):
    """按三项不变量分类单个用户（is_superuser 恒为 True，调用方保证）。"""
    inactive = not user.is_active
    no_staff = not user.is_staff
    if inactive and no_staff:
        return CLASSIFICATION_INACTIVE_AND_NO_STAFF, ["is_active", "is_staff"]
    if inactive:
        return CLASSIFICATION_INACTIVE, ["is_active"]
    if no_staff:
        return CLASSIFICATION_NO_STAFF, ["is_staff"]
    return CLASSIFICATION_OK, []


class Command(BaseCommand):
    help = ("平台超管三项不变量体检（is_active+is_superuser+is_staff）："
            "只读输出报告，不自动修改任何账号。")

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="空跑模式：只打印报告（本命令本身即为只读体检，永不写库）")

    def handle(self, *args, **options):
        User = get_user_model()
        if options["dry_run"]:
            self.stdout.write("dry-run：只读体检，不修改任何数据。")
        else:
            self.stdout.write(
                "提示：本命令为只读体检（等价 --dry-run），不自动修改任何数据。")

        rows = []
        counts = {}
        for user in User.objects.filter(is_superuser=True).order_by("id"):
            classification, missing = classify(user)
            counts[classification] = counts.get(classification, 0) + 1
            rows.append((user, classification, missing))

        self.stdout.write(self.style.MIGRATE_HEADING(
            "平台超管三项不变量体检报告（is_active + is_superuser + is_staff）"))
        self.stdout.write("-" * 88)
        for user, classification, missing in rows:
            flag = self.style.SUCCESS("OK      ") if classification == CLASSIFICATION_OK \
                else self.style.ERROR("ANOMALY ")
            detail = f"missing={','.join(missing)}" if missing else "invariants satisfied"
            self.stdout.write(
                f"{flag} #{user.pk:<6} {user.username:<24} "
                f"is_active={user.is_active!s:<5} is_staff={user.is_staff!s:<5} "
                f"{classification:<24} {detail}")
        self.stdout.write("-" * 88)
        total = len(rows)
        anomalies = total - counts.get(CLASSIFICATION_OK, 0)
        self.stdout.write(
            f"合计 {total} 个 superuser：正常 {counts.get(CLASSIFICATION_OK, 0)}，"
            f"异常 {anomalies}"
            + (f"（{ {k: v for k, v in counts.items() if k != CLASSIFICATION_OK} }）"
               if anomalies else ""))
        if anomalies:
            self.stdout.write(self.style.WARNING(
                "存在半状态超管：/platform/ 将对其返回 403 并落 "
                "security.platform_denied 审计；请在 Django Admin 修复三项标记。"))
        else:
            self.stdout.write(self.style.SUCCESS("全部超管满足平台身份不变量。"))
