"""报表与源业务数据一致性核对（MC11-RPT-01，总任务书 V3.0 TASK-05 / V3.2 TASK-14）。

对指定物业（和营业日）复算报表聚合并与源表逐项比对：
  * occupancy：RoomTypeInventory 源表重算 sold/sellable/occupancy_pct
    → 对比报表逐日行 + 汇总行；双向对齐源库存日历与报表 series 的日期集合
    （报表缺行 / 报表多行均记差异，不静默 PASS）；
  * revenue：LedgerEntry 按 business_date 重算收入/支出/净额（正数凭证、
    负数凭证、红字冲销自然落入带符号求和；主币种过滤与报表一致）
    → 对比报表逐日行，双向对齐日期集合（含报表缺行 / 报表多行）；
  * adr：分子（ROOM_CHARGE 及其红字冲销带符号合计）与分母（已售间夜）
    独立重算，对比报表 room_revenue 与 sold_room_nights，再验算报表
    adr 最终金额 = room_revenue / sold_room_nights；双向对齐日期集合。

不新增统计口径——全部沿用 report_center 既有声明的口径做读侧独立重算
（不复用 report_center 内部计算函数，避免"两个同源计算函数互证"）；主序列按
物业结算币种过滤，与报表一致。营业日口径：账务凭证按 LedgerEntry.business_date
（夜审口径）复算，不把自然日支付时间当作营业日入账时间。

输出契约（JSON 模式供脚本解析）：
  {"property", "range", "result": "PASS"|"FAIL",
   "checks": [{"name", "passed", "detail", "diffs": [{"name", "date",
   "report_value", "source_value", "detail"}...]}]}
失败时退出码 1；每条差异携带差异名称、日期、报表值、源表值。
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q, Sum
from django.utils import timezone

from apps.billing.models import LedgerEntry
from apps.core.models import Property
from apps.inventory.models import RoomTypeInventory

# 统计区间上限（与 report_center.MAX_RANGE_DAYS 同口径；命令侧独立声明）
MAX_RANGE_DAYS = 92
# 金额两位小数（与契约口径一致；独立实现，不 import billing_center）
CENT = Decimal("0.01")


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

        # ---- 参数校验（先于任何报表调用）----
        business_date = options["business_date"]
        days = options["days"]
        if business_date is not None:
            try:
                date.fromisoformat(str(business_date))
            except ValueError:
                raise CommandError("--business-date 必须使用 YYYY-MM-DD 格式。") from None
        if days < 1:
            raise CommandError("--days 必须 ≥ 1。")
        if days > MAX_RANGE_DAYS:
            raise CommandError(f"--days 最长 {MAX_RANGE_DAYS} 天。")

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
        if business_date:
            date_from = date_to = business_date
        else:
            date_from = (today - timedelta(days=max(1, days) - 1)).isoformat()
            date_to = today.isoformat()

        factory = RequestFactory()

        def run_report(name, url):
            request = factory.get(url)
            request.user = owner  # type: ignore[assignment]
            request.property = property_obj  # type: ignore[attr-defined]
            response = view_fn(request)
            if response.status_code != 200:
                raise RuntimeError(f"{name} 报表返回 {response.status_code}")
            return json.loads(response.content.decode("utf-8"))["data"]

        # 1. occupancy：报表 vs 库存源表重算（分子/分母/比率 + 日历完整性）
        view_fn = report_center.occupancy_report
        occupancy = run_report(
            "occupancy", f"/api/v1/reports/occupancy?from={date_from}&to={date_to}"
        )
        occ_diffs = _check_occupancy(occupancy, property_obj, date_from, date_to)
        checks = [_check_entry("occupancy vs RoomTypeInventory", occ_diffs)]

        # 2. revenue：报表 vs LedgerEntry 重算（收入/支出/净额 + 双向日期对齐）
        view_fn = report_center.revenue_report
        revenue = run_report("revenue", f"/api/v1/reports/revenue?from={date_from}&to={date_to}")
        rev_diffs = _check_revenue(revenue, property_obj, date_from, date_to)
        checks.append(_check_entry("revenue vs LedgerEntry", rev_diffs))

        # 3. adr：分子 + 分母 + 最终 ADR 金额全部独立核对
        view_fn = report_center.adr_report
        adr = run_report("adr", f"/api/v1/reports/adr?from={date_from}&to={date_to}")
        adr_diffs = _check_adr(adr, property_obj, date_from, date_to)
        checks.append(_check_entry("adr 分子/分母/比率 vs 源账本与库存", adr_diffs))

        passed_all = all(item["passed"] for item in checks)
        summary = {
            "property": property_obj.code,
            "range": f"{date_from}..{date_to}",
            "checks": checks,
            "result": "PASS" if passed_all else "FAIL",
        }
        if options["as_json"]:
            self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            for item in checks:
                self.stdout.write(
                    self.style.SUCCESS(f"PASS  {item['name']} — {item['detail']}")
                    if item["passed"]
                    else self.style.ERROR(f"FAIL  {item['name']} — {item['detail']}")
                )
            self.stdout.write(self.style.SUCCESS(f"合计 {'PASS' if passed_all else 'FAIL'}"))
        if not passed_all:
            raise SystemExit(1)


def _money(value):
    """统一两位小数字符串（契约口径；None/空 → "0.00"）。"""
    return str(Decimal(str(value or "0")).quantize(CENT))


def _diff(name, day, report_value, source_value, detail=""):
    """结构化差异记录：差异名称 + 日期 + 报表值 + 源表值。"""
    return {
        "name": name,
        "date": str(day),
        "report_value": str(report_value) if report_value is not None else None,
        "source_value": str(source_value) if source_value is not None else None,
        "detail": detail,
    }


def _check_entry(name, diffs):
    """把差异列表装订成单个 check 结果（passed + 汇总 detail + diffs）。"""
    passed = not diffs
    if passed:
        return {"name": name, "passed": True, "detail": "全部日期一致", "diffs": []}
    lines = ["；".join(d["detail"] for d in diffs[:3])]
    if len(diffs) > 3:
        lines.append(f"（共 {len(diffs)} 条差异）")
    return {"name": name, "passed": False, "detail": "".join(lines), "diffs": diffs}


def _dates_between(date_from, date_to):
    """闭区间 [date_from, date_to] 内的全部日期（date 对象）。"""
    start = date.fromisoformat(str(date_from))
    end = date.fromisoformat(str(date_to))
    return {start + timedelta(days=offset) for offset in range((end - start).days + 1)}


def _check_occupancy(occupancy, property_obj, date_from, date_to):
    """入住率核对：源表独立重算 sold/sellable/比率；双向对齐日期集合；验算比率。"""
    diffs = []
    occ_rows = {row["date"]: row for row in occupancy.get("series", [])}
    inventory_days = list(
        RoomTypeInventory.objects.filter(
            property=property_obj, stay_date__gte=date_from, stay_date__lte=date_to
        )
        .values_list("stay_date", flat=True)
        .distinct()
    )
    source_rows = {}
    for stay_date in inventory_days:
        day_rows = RoomTypeInventory.objects.filter(property=property_obj, stay_date=stay_date)
        agg = day_rows.aggregate(
            sold=Sum("sold"), total=Sum("total_rooms"), oos=Sum("out_of_service")
        )
        source_rows[stay_date] = {
            "sold": agg["sold"] or 0,
            "sellable": (agg["total"] or 0) - (agg["oos"] or 0),
        }

    # 逐日核对（分子、分母、入住率最终值）
    for stay_date in sorted(source_rows):
        src = source_rows[stay_date]
        report_row = occ_rows.get(stay_date.isoformat())
        if report_row is None:
            diffs.append(
                _diff(
                    "occupancy_report_missing_row",
                    stay_date,
                    None,
                    f"sold={src['sold']}/sellable={src['sellable']}",
                    f"{stay_date}: 报表缺行（源库存日历有该住宿日）",
                )
            )
            continue
        report_sold = report_row.get("sold_room_nights")
        report_sellable = report_row.get("sellable_room_nights")
        if report_sold != src["sold"]:
            diffs.append(
                _diff(
                    "occupancy_sold_mismatch",
                    stay_date,
                    report_sold,
                    src["sold"],
                    f"{stay_date}: 报表 sold={report_sold} vs 源表 sold={src['sold']}",
                )
            )
        if report_sellable != src["sellable"]:
            diffs.append(
                _diff(
                    "occupancy_sellable_mismatch",
                    stay_date,
                    report_sellable,
                    src["sellable"],
                    f"{stay_date}: 报表 sellable={report_sellable} vs 源表 sellable={src['sellable']}",
                )
            )
        # 验算入住率最终值（不能只核对原始数量）
        expected_pct = _pct(src["sold"], src["sellable"])
        report_pct = report_row.get("occupancy_pct")
        if expected_pct is not None and report_pct != expected_pct:
            diffs.append(
                _diff(
                    "occupancy_rate_mismatch",
                    stay_date,
                    report_pct,
                    expected_pct,
                    f"{stay_date}: 报表 occupancy_pct={report_pct} vs 源表重算 {expected_pct}",
                )
            )

    # 报表有行但源表无库存数据（双向对齐，不允许静默 PASS）
    source_days = set(source_rows)
    for day_key in sorted(occ_rows):
        if date.fromisoformat(day_key) not in source_days:
            diffs.append(
                _diff(
                    "occupancy_report_extra_row",
                    day_key,
                    f"sold={occ_rows[day_key].get('sold_room_nights')}",
                    None,
                    f"{day_key}: 报表有行但源库存日历无该住宿日数据",
                )
            )
    # 汇总行核对
    summary = occupancy.get("summary") or {}
    src_sold_total = sum(row["sold"] for row in source_rows.values())
    src_sellable_total = sum(row["sellable"] for row in source_rows.values())
    if summary.get("sold_room_nights") != src_sold_total:
        diffs.append(
            _diff(
                "occupancy_summary_sold_mismatch",
                f"{date_from}..{date_to}",
                summary.get("sold_room_nights"),
                src_sold_total,
                f"汇总 sold_room_nights 报表 {summary.get('sold_room_nights')} vs 源表 {src_sold_total}",
            )
        )
    if summary.get("sellable_room_nights") != src_sellable_total:
        diffs.append(
            _diff(
                "occupancy_summary_sellable_mismatch",
                f"{date_from}..{date_to}",
                summary.get("sellable_room_nights"),
                src_sellable_total,
                f"汇总 sellable_room_nights 报表 {summary.get('sellable_room_nights')} vs 源表 {src_sellable_total}",
            )
        )
    return diffs


def _check_revenue(revenue, property_obj, date_from, date_to):
    """收入核对：LedgerEntry 独立重算收入/支出/净额；双向对齐营业日集合。"""
    diffs = []
    rev_rows = {row["business_date"]: row for row in revenue.get("series", [])}
    base_currency = property_obj.currency
    ledgers = (
        LedgerEntry.objects.filter(
            property=property_obj,
            business_date__gte=date_from,
            business_date__lte=date_to,
            currency=base_currency,
        )
        .values("business_date")
        .annotate(
            income=Sum("amount", filter=Q(amount__gt=0)),
            expense=Sum("amount", filter=Q(amount__lt=0)),
            net=Sum("amount"),
        )
    )
    source_rows = {row["business_date"]: row for row in ledgers}

    for day in sorted(source_rows):
        row = source_rows[day]
        income = row["income"] or Decimal("0")
        expense = abs(row["expense"] or Decimal("0"))
        net = row["net"] or Decimal("0")
        report_row = rev_rows.get(day.isoformat())
        if report_row is None:
            diffs.append(
                _diff(
                    "revenue_report_missing_row",
                    day,
                    None,
                    f"revenue={_money(income)}/expense={_money(expense)}",
                    f"{day}: 报表缺行（源账本当日有凭证）",
                )
            )
            continue
        report_revenue = report_row.get("revenue")
        report_expense = report_row.get("expense")
        report_net = report_row.get("net")
        if _money(report_revenue) != _money(income):
            diffs.append(
                _diff(
                    "revenue_income_mismatch",
                    day,
                    report_revenue,
                    _money(income),
                    f"{day}: 报表 revenue={report_revenue} vs 源账本 income={_money(income)}",
                )
            )
        if _money(report_expense) != _money(expense):
            diffs.append(
                _diff(
                    "revenue_expense_mismatch",
                    day,
                    report_expense,
                    _money(expense),
                    f"{day}: 报表 expense={report_expense} vs 源账本 expense={_money(expense)}",
                )
            )
        if _money(report_net) != _money(net):
            diffs.append(
                _diff(
                    "revenue_net_mismatch",
                    day,
                    report_net,
                    _money(net),
                    f"{day}: 报表 net={report_net} vs 源账本 net={_money(net)}",
                )
            )

    # 报表有行但源账本零凭证（主币种口径）
    source_days = set(source_rows)
    for day_key in sorted(rev_rows):
        if date.fromisoformat(day_key) not in source_days:
            diffs.append(
                _diff(
                    "revenue_report_extra_row",
                    day_key,
                    f"revenue={rev_rows[day_key].get('revenue')}",
                    None,
                    f"{day_key}: 报表有行但源账本零凭证（主币种口径）",
                )
            )
    return diffs


def _check_adr(adr, property_obj, date_from, date_to):
    """ADR 核对：分子（房费凭证带符号合计）与分母（已售间夜）独立重算，
    再验算报表 adr 最终金额；红字冲销自然落入带符号求和。"""
    diffs = []
    adr_rows = {row["date"]: row for row in adr.get("series", [])}
    base_currency = property_obj.currency
    # 分子：ROOM_CHARGE + 其红字冲销（REVERSAL→ROOM_CHARGE）带符号合计
    charge_rows = (
        LedgerEntry.objects.filter(
            property=property_obj,
            business_date__gte=date_from,
            business_date__lte=date_to,
        )
        .filter(currency=base_currency)
        .filter(
            Q(entry_type="ROOM_CHARGE")
            | Q(entry_type="REVERSAL", void_of__entry_type="ROOM_CHARGE")
        )
        .values("business_date")
        .annotate(total=Sum("amount"))
    )
    revenue_by_day = {row["business_date"]: row["total"] or Decimal("0") for row in charge_rows}
    # 分母：RoomTypeInventory.sold 独立重算
    sold_rows = (
        RoomTypeInventory.objects.filter(
            property=property_obj, stay_date__gte=date_from, stay_date__lte=date_to
        )
        .values("stay_date")
        .annotate(sold=Sum("sold"))
    )
    sold_by_day = {row["stay_date"]: row["sold"] or 0 for row in sold_rows}

    for day in sorted(set(revenue_by_day) | set(sold_by_day)):
        src_revenue = revenue_by_day.get(day, Decimal("0"))
        src_sold = sold_by_day.get(day, 0)
        report_row = adr_rows.get(day.isoformat())
        if report_row is None:
            if src_revenue != 0 or src_sold != 0:
                diffs.append(
                    _diff(
                        "adr_report_missing_row",
                        day,
                        None,
                        f"room_revenue={_money(src_revenue)}/sold={src_sold}",
                        f"{day}: 报表缺行（源数据房费凭证合计 {_money(src_revenue)}，已售间夜 {src_sold}）",
                    )
                )
            continue
        # 分子核对
        report_revenue = report_row.get("room_revenue")
        if _money(report_revenue) != _money(src_revenue):
            diffs.append(
                _diff(
                    "adr_room_revenue_mismatch",
                    day,
                    report_revenue,
                    _money(src_revenue),
                    f"{day}: 报表 room_revenue={report_revenue} vs 源账本 {_money(src_revenue)}",
                )
            )
        # 分母核对（ADR 已售间夜必须与源数据核对，不能只看 room_revenue）
        report_sold = report_row.get("sold_room_nights")
        if report_sold != src_sold:
            diffs.append(
                _diff(
                    "adr_sold_room_nights_mismatch",
                    day,
                    report_sold,
                    src_sold,
                    f"{day}: 报表 sold_room_nights={report_sold} vs 源库存 {src_sold}",
                )
            )
        # 最终 ADR 金额验算（分子/分母均一致后核对最终计算结果）
        report_adr = report_row.get("adr")
        expected_adr = _money(Decimal(str(src_revenue)) / Decimal(src_sold)) if src_sold else None
        if expected_adr is not None and report_adr is None:
            diffs.append(
                _diff(
                    "adr_value_missing",
                    day,
                    None,
                    expected_adr,
                    f"{day}: 报表缺 adr 值（源表重算 {expected_adr}）",
                )
            )
        elif expected_adr is not None and _money(report_adr) != expected_adr:
            diffs.append(
                _diff(
                    "adr_value_mismatch",
                    day,
                    report_adr,
                    expected_adr,
                    f"{day}: 报表 adr={report_adr} vs 源表重算 {expected_adr}",
                )
            )

    # 报表有行但源数据（房费凭证与库存）全空
    source_days = set(revenue_by_day) | set(sold_by_day)
    for day_key in sorted(adr_rows):
        if date.fromisoformat(day_key) not in source_days:
            reported = Decimal(str(adr_rows[day_key].get("room_revenue") or "0"))
            if reported != Decimal("0") or adr_rows[day_key].get("sold_room_nights"):
                diffs.append(
                    _diff(
                        "adr_report_extra_row",
                        day_key,
                        f"room_revenue={adr_rows[day_key].get('room_revenue')}",
                        None,
                        f"{day_key}: 报表有行但源账本/源库存均无该日数据",
                    )
                )
    return diffs


def _pct(numerator, denominator):
    """numerator/denominator×100 两位小数字符串；分母 0 → None（与报表口径一致）。"""
    if not denominator:
        return None
    return str((Decimal(numerator) * 100 / Decimal(denominator)).quantize(CENT))
