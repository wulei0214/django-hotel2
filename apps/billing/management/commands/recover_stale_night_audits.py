# -*- coding: utf-8 -*-
"""MAINT-02：恢复超时残留的夜审 RUNNING 记录。

夜审在独立事务中先落 RUNNING 再进主过账事务；进程被 OOM/断电硬杀时
except 分支不会执行，RUNNING 会永久残留，使该营业日再也无法执行夜审。
本命令把超过阈值的 RUNNING 标记为 FAILED 并写审计，之后可重新发起夜审。
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.billing.night_audit import (
    NIGHT_AUDIT_RUN_TIMEOUT_MINUTES,
    recover_stale_night_audit_runs,
)


class Command(BaseCommand):
    help = "把超时未结束的夜审 RUNNING 记录标记为 FAILED（可重跑夜审）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes", type=int,
            default=NIGHT_AUDIT_RUN_TIMEOUT_MINUTES,
            help=f"超时阈值（分钟，默认 {NIGHT_AUDIT_RUN_TIMEOUT_MINUTES}）")

    def handle(self, *args, **options):
        minutes = max(1, int(options["minutes"]))
        with transaction.atomic():
            count = recover_stale_night_audit_runs(timeout_minutes=minutes)
        self.stdout.write(
            self.style.SUCCESS(
                f"已恢复 {count} 条超时夜审 RUNNING 记录（阈值 {minutes} 分钟）。"))
