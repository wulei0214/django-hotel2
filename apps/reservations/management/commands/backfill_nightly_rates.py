"""A8：ReservationRoom.nightly_rates 存量回填（Dry-run 默认 / --execute 写入）。

背景：nightly_rates（每晚房价明细快照）引入前的历史预订明细该列为空，
金额争议/对账时缺逐晚依据。本命令按每条明细的 rate_plan + room_type +
stay 日期区间 [check_in, check_out) 从 DailyRate 重建逐晚快照（与
inventory.services.get_room_type_availability 的取价口径一致）。

纪律：
  * 默认 --dry-run：只扫描打印影响行数与样例，不写任何数据；
  * --execute 才写入 nightly_rates；账务事实（ReservationRoom.amount、
    Folio/Payment）一律不改——写后核对明细金额与快照合计，不一致的行
    在 --report 中列出供人工核对，不强制改数；
  * 价格缺失的住宿日跳过（不进入快照）并在 --report 中列出；
  * 整晚都缺价的明细整行跳过（无可重建内容），单独计数；
  * 每次写入逐行写审计（reservation.nightly_rates_backfilled，before/after）。

用法：
    python manage.py backfill_nightly_rates                     # 仅扫描+样例
    python manage.py backfill_nightly_rates --report            # 扫描+跳过日清单
    python manage.py backfill_nightly_rates --execute           # 回填+审计
    python manage.py backfill_nightly_rates --execute --report  # 回填+金额核对清单
    python manage.py backfill_nightly_rates --property-id 1 --limit 200
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.models import AuditEvent
from apps.inventory.models import DailyRate
from apps.reservations.models import ReservationRoom

ACTION_BACKFILLED = "reservation.nightly_rates_backfilled"
SAMPLE_LIMIT = 10


def _stay_dates(check_in, check_out):
    """住宿晚区间 [check_in, check_out)，与库存/取价服务同口径（按晚计价）。"""
    nights = []
    cursor = check_in
    while cursor < check_out:
        nights.append(cursor)
        cursor += timedelta(days=1)
    return nights


def _rebuild_snapshot(room, daily_rates):
    """按 DailyRate 重建逐晚快照；缺价日跳过并返回缺失清单。"""
    nights = []
    missing = []
    for stay_date in _stay_dates(room.check_in, room.check_out):
        rate = daily_rates.get(stay_date)
        if rate is None:
            missing.append(stay_date)
            continue
        nights.append(
            {
                "date": stay_date.isoformat(),
                "price": str(rate.price),
                "currency": rate.currency,
            }
        )
    return nights, missing


def _collect_rows(property_id):
    """找出 nightly_rates 为空（历史遗留）的 ReservationRoom 明细。"""
    qs = (
        ReservationRoom.objects.filter(property_id=property_id).order_by("id")
        if property_id
        else ReservationRoom.objects.all().order_by("id")
    )
    rows = []
    for room in qs.iterator():
        if not room.nightly_rates:  # 空数组 / NULL 均视为无逐晚快照
            rows.append(room)
    return rows


class Command(BaseCommand):
    help = (
        "回填 ReservationRoom.nightly_rates 历史逐晚房价快照（A8："
        "默认 dry-run 只报告；--execute 写入；--report 列出跳过日与金额不一致行）。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="实际写入 nightly_rates（缺省为 dry-run 只扫描报告）。",
        )
        parser.add_argument(
            "--report",
            action="store_true",
            help="输出详细报告：价格缺失被跳过的日期、"
            "（--execute 时）快照合计与明细金额不一致的行。",
        )
        parser.add_argument(
            "--property-id", type=int, default=None, help="仅处理指定物业；省略时处理全部物业。"
        )
        parser.add_argument(
            "--limit", type=int, default=None, help="最多处理 N 条明细（抽查用）；省略时处理全部。"
        )

    def handle(self, *args, **options):
        execute = options["execute"]
        verbose_report = options["report"]
        property_id = options.get("property_id")
        limit = options.get("limit")
        if limit is not None and limit < 1:
            raise CommandError("limit 必须大于 0。")

        rows = _collect_rows(property_id)
        if limit is not None:
            rows = rows[:limit]

        scanned = len(rows)
        would_backfill = 0
        skipped_rows = []  # 整段住宿区间都无价的明细
        skipped_dates = []  # (room_id, booking_no, [缺失日])——部分缺价
        samples: list[dict[str, Any]] = []
        backfilled = 0
        amount_mismatches = []

        for room in rows:
            daily_rates = {
                rate.stay_date: rate
                for rate in DailyRate.objects.filter(
                    property_id=room.property_id,
                    rate_plan_id=room.rate_plan_id,
                    room_type_id=room.room_type_id,
                    stay_date__gte=room.check_in,
                    stay_date__lt=room.check_out,
                )
            }
            nights, missing = _rebuild_snapshot(room, daily_rates)
            booking_no = room.reservation.booking_no
            if not nights:
                skipped_rows.append((room.pk, booking_no))
                continue
            would_backfill += 1
            if missing:
                skipped_dates.append((room.pk, booking_no, missing))
            if len(samples) < SAMPLE_LIMIT:
                samples.append(
                    {
                        "reservation_room_id": room.pk,
                        "booking_no": booking_no,
                        "check_in": room.check_in.isoformat(),
                        "check_out": room.check_out.isoformat(),
                        "nights": len(nights),
                        "snapshot_sum": str(
                            sum((Decimal(n["price"]) for n in nights), start=Decimal("0.00"))
                        ),
                        "amount": str(room.amount),
                    }
                )
            if execute:
                with transaction.atomic():
                    ReservationRoom.objects.filter(pk=room.pk).update(nightly_rates=nights)
                    AuditEvent.record(
                        action=ACTION_BACKFILLED,
                        entity_type="ReservationRoom",
                        entity_id=room.pk,
                        property_obj=room.property,
                        before={"nightly_rates": room.nightly_rates},
                        after={
                            "nights": len(nights),
                            "missing_dates": [d.isoformat() for d in missing],
                        },
                    )
                    backfilled += 1
                # 写后断言：明细金额与快照合计一致（不一致仅列出，不强改）
                room.refresh_from_db()
                snapshot_sum = sum(
                    (Decimal(n["price"]) for n in room.nightly_rates), start=Decimal("0.00")
                )
                if Decimal(room.amount) != snapshot_sum:
                    amount_mismatches.append(
                        {
                            "reservation_room_id": room.pk,
                            "booking_no": booking_no,
                            "amount": str(room.amount),
                            "snapshot_sum": str(snapshot_sum),
                        }
                    )

        mode = "EXECUTE" if execute else "DRY-RUN"
        if execute:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[A8 nightly_rates 回填完成 mode={mode}] 扫描 {scanned} 条，"
                    f"回填 {backfilled} 条；整段缺价跳过 {len(skipped_rows)} 条；"
                    f"部分缺价日跳过 {len(skipped_dates)} 条明细；"
                    f"金额不一致 {len(amount_mismatches)} 条（未改账务，需人工核对）。"
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY-RUN] 未写入任何记录。扫描 {scanned} 条历史明细，"
                    f"可回填 {would_backfill} 条；整段缺价跳过 {len(skipped_rows)} 条；"
                    f"部分缺价日跳过 {len(skipped_dates)} 条明细。"
                    f"确认无误后加 --execute 执行。"
                )
            )
        if samples:
            self.stdout.write("样例（前 %d 条）：" % len(samples))
            for item in samples:
                self.stdout.write(
                    f"  room_line={item['reservation_room_id']} "
                    f"booking={item['booking_no']} "
                    f"{item['check_in']}~{item['check_out']} "
                    f"nights={item['nights']} "
                    f"snapshot_sum={item['snapshot_sum']} amount={item['amount']}"
                )
        if verbose_report:
            if skipped_rows:
                self.stdout.write("整段住宿区间缺价的明细（跳过，未回填）：")
                for room_id, booking_no in skipped_rows[:50]:
                    self.stdout.write(f"  room_line={room_id} booking={booking_no}")
                if len(skipped_rows) > 50:
                    self.stdout.write(f"  …（其余 {len(skipped_rows) - 50} 条略）")
            if skipped_dates:
                self.stdout.write("部分日期缺价被跳过的明细（--report）：")
                for room_id, booking_no, missing in skipped_dates[:50]:
                    self.stdout.write(
                        f"  room_line={room_id} booking={booking_no} 缺价日="
                        + ",".join(d.isoformat() for d in missing)
                    )
                if len(skipped_dates) > 50:
                    self.stdout.write(f"  …（其余 {len(skipped_dates) - 50} 条略）")
            if amount_mismatches:
                self.stdout.write(
                    self.style.NOTICE("快照合计与明细金额不一致的行（供人工核对，未强改）：")
                )
                for item in amount_mismatches[:50]:
                    self.stdout.write(
                        f"  room_line={item['reservation_room_id']} "
                        f"booking={item['booking_no']} "
                        f"amount={item['amount']} snapshot_sum={item['snapshot_sum']}"
                    )
                if len(amount_mismatches) > 50:
                    self.stdout.write(f"  …（其余 {len(amount_mismatches) - 50} 条略）")
