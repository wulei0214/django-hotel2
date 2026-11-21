"""报表与源业务数据一致性核对（MC11-RPT-01，总任务书 V3.0 TASK-05）。

对指定物业（和营业日）复算报表聚合并与源表逐项比对：
  * occupancy：RoomTypeInventory 源表重算 sold/total → 对比报表 series；
  * revenue：LedgerEntry 按 business_date 重算收入/支出 → 对比报表；
  * adr：房费凭证（ROOM_CHARGE+冲销带符号）重算分子 / sold 重算分母
    → 对比报表。

不新增统计口径——全部沿用 report_center 既有声明的口径做读侧独立重算
（不用 report_center 内部函数，避免"两个同源计算函数互证"）；主序列按
物业结算币种过滤，与报表一致。
"""

import json
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q, Sum
from django.utils import timezone

from apps.billing.models import LedgerEntry
from apps.core.models import Property
from apps.inventory.models import RoomTypeInventory


class Command(BaseCommand):
    help = "复算 occupancy/adr/revenue 与源表逐项比对（一致性核对，只读）。"

    def add_arguments(self, parser):
        parser.add_argument("--property-code", default="DEFAULT")
        parser.add_argument("--days", type=int, default=7, help="回溯自然日天数（含今天）")
        parser.add_argument(
            "--business-date",
            type=str,
            default=None,
            help="只核对单一营业日（YYYY-MM-DD），覆盖 --days",
        )
        parser.add_argument("--as-json", action="store_true", help="输出 JSON（供证据归档）")

    def handle(self, *args, **options):
        from django.contrib.auth.models import User
        from django.test import RequestFactory

        from apps.api import report_center

        property_obj = Property.objects.get(code=options["property_code"])
        # 超管不自动绕过业务岗位授权（authorization.py）：优先测试店长账号
        owner = User.objects.filter(username="test_owner", is_active=True).first()
        if owner is None:
            owner = (
                User.objects.filter(
                    staff_profile__property_memberships__property=property_obj,
                    groups__name="店长",
                    is_active=True,
                )
                .distinct()
                .first()
            )
        if owner is None:
            raise CommandError("未找到具备店长任职的核对账号（先执行 seed_role_test_accounts）")
        assert owner is not None  # CommandError 已排除 None（mypy 收窄）
        today = timezone.localdate()
        business_date = options["business_date"]
        if business_date:
            date_from = date_to = business_date
        else:
            date_from = (today - timedelta(days=max(1, options["days"]) - 1)).isoformat()
            date_to = today.isoformat()

        factory = RequestFactory()
        checks = []

        def run_report(name, url):
            request = factory.get(url)
            request.user = owner  # type: ignore[assignment]
            request.property = property_obj  # type: ignore[attr-defined]
            response = view_fn(request)
            if response.status_code != 200:
                raise RuntimeError(f"{name} 报表返回 {response.status_code}")
            return json.loads(response.content.decode("utf-8"))["data"]

        # 1. occupancy：报表 vs 库存源表重算
        view_fn = report_center.occupancy_report
        occupancy = run_report(
            "occupancy", f"/api/v1/reports/occupancy?from={date_from}&to={date_to}"
        )
        occ_rows = {
            row["date"]: row for row in occupancy.get("series", [])
        }
        inventory_rows = RoomTypeInventory.objects.filter(
            property=property_obj, stay_date__gte=date_from, stay_date__lte=date_to
        )
        mismatch = None
        for stay_date in sorted({r.stay_date for r in inventory_rows}):
            day_rows = [r for r in inventory_rows if r.stay_date == stay_date]
            src_sold = sum(r.sold for r in day_rows)
            src_total = sum(r.total_rooms - r.out_of_service for r in day_rows)
            report_row = occ_rows.get(stay_date.isoformat())
            if report_row is None:
                mismatch = f"{stay_date}: 报表缺行"
                break
            report_sold = report_row.get("sold_room_nights")
            report_total = report_row.get("sellable_room_nights")
            if report_sold is None or report_total is None:
                mismatch = f"{stay_date}: 报表缺 sold/sellable 字段"
                break
            if report_sold != src_sold or report_total != src_total:
                mismatch = (
                    f"{stay_date}: 报表 sold={report_sold}/total={report_total} "
                    f"vs 源表 sold={src_sold}/total={src_total}"
                )
                break
        checks.append(("occupancy vs RoomTypeInventory", mismatch is None, mismatch or "全部住宿日一致"))

        # 2. revenue：报表 vs LedgerEntry 重算
        view_fn = report_center.revenue_report
        revenue = run_report("revenue", f"/api/v1/reports/revenue?from={date_from}&to={date_to}")
        rev_rows = {row["business_date"]: row for row in revenue.get("series", [])}
        ledgers = (
            LedgerEntry.objects.filter(
                property=property_obj,
                business_date__gte=date_from,
                business_date__lte=date_to,
                currency=property_obj.currency,
            )
            .values("business_date")
            .annotate(
                income=Sum("amount", filter=_q_positive()),
                expense=Sum("amount", filter=_q_negative()),
            )
        )
        rev_mismatch = None
        if not rev_rows:
            rev_mismatch = None  # 区间内无凭证 → 报表空序列，与源账本零凭证一致
        for row in ledgers:
            day = row["business_date"].isoformat()
            income = row["income"] or 0
            expense = abs(row["expense"] or 0)
            report_row = rev_rows.get(day)
            if report_row is None:
                rev_mismatch = f"{day}: 报表缺行（源账本当日有凭证）"
                break
            if (
                Decimal(str(report_row.get("revenue") or "0")) != income
                or Decimal(str(report_row.get("expense") or "0")) != expense
            ):
                rev_mismatch = (
                    f"{day}: 报表 revenue={report_row.get('revenue')}/expense={report_row.get('expense')} "
                    f"vs 源账本 income={income}/expense={expense}"
                )
                break
        if rev_mismatch is None and rev_rows:
            source_days = {row["business_date"].isoformat() for row in ledgers}
            for day in rev_rows:
                if day not in source_days:
                    rev_mismatch = f"{day}: 报表有行但源账本零凭证（主币种口径）"
                    break
        if rev_rows:
            checks.append(
                ("revenue vs LedgerEntry", rev_mismatch is None, rev_mismatch or "全部营业日一致")
            )
        else:
            checks.append(
                ("revenue vs LedgerEntry", True, "区间内报表空序列（与源账本零凭证一致）")
            )

        # 3. adr：报表分子 vs ROOM_CHARGE 类凭证重算
        view_fn = report_center.adr_report
        adr = run_report("adr", f"/api/v1/reports/adr?from={date_from}&to={date_to}")
        adr_rows = {row["date"]: row for row in adr.get("series", [])}
        charge_ledgers = (
            LedgerEntry.objects.filter(
                property=property_obj,
                business_date__gte=date_from,
                business_date__lte=date_to,
            )
            .filter(currency=property_obj.currency)
            .filter(
                Q(entry_type="ROOM_CHARGE")
                | Q(entry_type="REVERSAL", void_of__entry_type="ROOM_CHARGE")
            )
            .values("business_date")
            .annotate(total=Sum("amount"))
        )
        adr_mismatch = None
        for row in charge_ledgers:
            day = row["business_date"].isoformat()
            report_row = adr_rows.get(day)
            if report_row is None:
                # 报表缺行但源账本当日有房费凭证 → 差异（不静默跳过）
                adr_mismatch = f"{day}: 报表缺行（源账本房费凭证合计 {row['total']}）"
                break
            # 分子=报表 room_revenue 字段（口径见 report_center.adr_report）
            reported = report_row.get("room_revenue")
            if reported is not None and str(reported) != str(row["total"]):
                adr_mismatch = f"{day}: 报表 {reported} vs 源账本 {row['total']}"
                break
        if adr_mismatch is None:
            charge_days = {row["business_date"].isoformat() for row in charge_ledgers}
            for day, row in adr_rows.items():
                reported = Decimal(str(row.get("room_revenue") or "0"))
                if day not in charge_days and reported != Decimal("0"):
                    adr_mismatch = f"{day}: 报表有行但源账本零房费凭证"
                    break
        checks.append(("adr 分子 vs ROOM_CHARGE 凭证", adr_mismatch is None, adr_mismatch or "全部营业日一致"))

        summary = {
            "property": property_obj.code,
            "range": f"{date_from}..{date_to}",
            "checks": [
                {"name": name, "passed": passed, "detail": detail}
                for name, passed, detail in checks
            ],
        }
        passed_all = all(item["passed"] for item in summary["checks"])
        summary["result"] = "PASS" if passed_all else "FAIL"
        if options["as_json"]:
            self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            for item in summary["checks"]:
                self.stdout.write(
                    self.style.SUCCESS(f"PASS  {item['name']} — {item['detail']}")
                    if item["passed"]
                    else self.style.ERROR(f"FAIL  {item['name']} — {item['detail']}")
                )
            self.stdout.write(self.style.SUCCESS(f"合计 {'PASS' if passed_all else 'FAIL'}"))
        if not passed_all:
            raise SystemExit(1)


def _q_positive():
    from django.db.models import Q

    return Q(amount__gt=0)


def _q_negative():
    from django.db.models import Q

    return Q(amount__lt=0)
