"""Guest 重复档案候选报告（只读命令，A3）。

PMS2 身份方案 2.0 §8.2-7：在业务负责人批准合并策略前，**只能做只读盘点和
候选重复报告**。本命令因此是**纯只读**的：不创建、不修改、不删除任何业务
数据，也不写审计事件；它只读取并打印候选证据与影响面盘点结果。

用法：
    python manage.py guest_duplicate_candidates --property-id 1
    python manage.py guest_duplicate_candidates --property-id 1 --json
    python manage.py guest_duplicate_candidates --property-id 1 \
        --source-guest-id 12 --target-guest-id 15

指定 --source-guest-id 与 --target-guest-id 时，额外打印该对档案的合并
影响面盘点（引用面统计 + 冲突事实清单）。盘点结果中的 executable 恒为
False——物理合并在策略获批前处于冻结状态。
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Property
from apps.guests.merge_services import (
    find_duplicate_candidates,
    preview_merge_impact,
)
from apps.guests.models import Guest


class Command(BaseCommand):
    help = "只读输出 Guest 重复档案候选报告（不写库、不合并）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--property-id", type=int, default=None, help="物业 ID；省略时使用 DEFAULT 物业"
        )
        parser.add_argument(
            "--limit", type=int, default=200, help="最多返回的候选对数量（默认 200）"
        )
        parser.add_argument(
            "--source-guest-id",
            type=int,
            default=None,
            help="源档案 ID（与 --target-guest-id 同时提供）",
        )
        parser.add_argument(
            "--target-guest-id",
            type=int,
            default=None,
            help="目标档案 ID（与 --source-guest-id 同时提供）",
        )
        parser.add_argument("--json", action="store_true", help="以 JSON 输出（默认人类可读文本）")

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1:
            raise CommandError("--limit 必须大于 0。")

        property_obj = self._resolve_property(options["property_id"])

        source_id = options["source_guest_id"]
        target_id = options["target_guest_id"]
        if (source_id is None) != (target_id is None):
            raise CommandError("--source-guest-id 与 --target-guest-id 必须同时提供。")

        report = find_duplicate_candidates(property_obj, limit=limit)
        preview = None
        if source_id is not None:
            preview = self._preview(property_obj, source_id, target_id)
            report["impact_preview"] = preview

        if options["json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return

        self._print_text(report, preview)

    # ------------------------------------------------------------------
    def _resolve_property(self, property_id):
        if property_id is not None:
            try:
                return Property.objects.get(pk=property_id)
            except Property.DoesNotExist:
                raise CommandError(f"物业 ID {property_id} 不存在。") from None
        default = Property.objects.filter(code="DEFAULT").first()
        if default is None:
            raise CommandError("未找到 DEFAULT 物业，请用 --property-id 指定物业。")
        return default

    def _preview(self, property_obj, source_id, target_id):
        try:
            source = Guest.objects.get(pk=source_id, property=property_obj)
            target = Guest.objects.get(pk=target_id, property=property_obj)
        except Guest.DoesNotExist:
            raise CommandError(
                "源档案或目标档案不存在于该物业（请确认两个 ID 均属同一物业）。"
            ) from None
        return preview_merge_impact(property_obj, source_guest=source, target_guest=target)

    def _print_text(self, report, preview):
        counts = report["counts"]
        self.stdout.write("【只读报告】本命令不写库、不合并任何客人档案。")
        self.stdout.write(f"物业：{report['property_id']}　生成时间：{report['generated_at']}")
        self.stdout.write(
            f"候选对数量：{counts['candidate_pairs']}"
            + ("（已截断）" if counts["truncated"] else "")
        )
        if counts["by_signal"]:
            self.stdout.write("按信号统计：")
            for signal, number in sorted(counts["by_signal"].items()):
                self.stdout.write(f"  - {signal}: {number}")
        self.stdout.write("候选清单：")
        if not report["candidates"]:
            self.stdout.write("  （无）")
        for candidate in report["candidates"]:
            self.stdout.write(
                f"  档案 #{candidate['source_guest_id']} <-> "
                f"#{candidate['target_guest_id']}　信号："
                f"{', '.join(candidate['signals'])}"
            )
        self.stdout.write(f"口径声明：{report['policy']['note']}")

        if preview is not None:
            self.stdout.write("")
            self.stdout.write("—— 合并影响面盘点（只读）——")
            for side in ("source", "target"):
                block = preview[side]
                self.stdout.write(
                    f"{side} 档案 #{block['guest_id']}" f"（{block['full_name_masked']}）引用面："
                )
                for label, number in block["references"].items():
                    self.stdout.write(f"    {label}: {number}")
            self.stdout.write("冲突事实：")
            if not preview["conflicts"]:
                self.stdout.write("  （未发现冲突事实）")
            for conflict in preview["conflicts"]:
                self.stdout.write(f"  - {conflict['kind']}：{conflict['detail']}")
            self.stdout.write(f"可执行合并：{preview['executable']}")
            self.stdout.write(f"冻结原因：{preview['frozen_reason']}")
