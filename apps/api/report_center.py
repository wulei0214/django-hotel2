"""报表分析中心只读 API（基线 2.0 §4.5）。

目标职责：提供运营决策所需的可解释汇总，不作为第二账本，不修改业务数据。
全部端点为只读数据库聚合（values().annotate() 单查询，无 N+1、无快照落库），
每报表响应携带统计口径 metadata（物业/日期口径/状态范围/币种/生成时间——
P0-07 模式，对齐 apps/api/guest_center.py::financial_summary_api）。

首期报表（§4.5 首期报表范围的运营子集 + P2 收益指标）：
  * GET /api/v1/reports/occupancy?from=&to=   入住率（inventory 口径）
  * GET /api/v1/reports/adr?from=&to=         ADR（房费收入/已售间夜）
  * GET /api/v1/reports/revenue?from=&to=&group_by=day|type
                                              收入/支出/净额（LedgerEntry 口径）
  * GET /api/v1/reports/guests?from=&to=      客源（新客/回头客/来源分布）
  * GET /api/v1/reports/housekeeping?from=&to= 清洁效率（AuditEvent 事件口径）

第 3 批快照型报表（无 from/to 区间参数，固定近 30 天趋势窗口，A4/A5）：
  * GET /api/v1/reports/memberships           会员（GuestMembership 口径）
  * GET /api/v1/reports/sources               渠道来源（Reservation.source 口径）

授权口径（按报表敏感度分两档，均为 hotelapp.* 门店经营权限）：
  * 收入类（revenue / adr）→ hotelapp.finance_access：
    分子是已过账账本凭证金额，与 §4.1 财务中心读取同一门禁，不以
    view_accommodation 等运营权限替代；
  * 运营类（occupancy / guests / housekeeping / memberships / sources）
    → hotelapp.view_accommodation：
    入住率/客源/清洁事件/会员结构/渠道来源是预订与入住模块的运营统计，
    不含账务凭证金额与客人 PII（会员报表仅输出 member_no 与聚合数值）；
    不借用 core.audit_view（审计查看是独立合规职责 P0-04，看运营报表
    不应授予审计权），也不借用 view_room（清洁事件计数挂在入住运营口径）。

计算口径（每报表 metadata 同步下发）：
  * OCC = 已售间夜(RoomTypeInventory.sold 求和) / 可售间夜(total − out_of_service)；
    停用房从分母剔除（§4.5 要求明确），待支付冻结（held）计入分母不计入分子；
  * ADR = 房费收入(ROOM_CHARGE 凭证及其红字冲销的带符号合计) / 已售间夜；
    不含押金/收款/调整（支付金额不直接当收入）；
  * 收入 = 正数凭证合计；支出 = 负数凭证合计的绝对值（含红字冲销）；
    净额 = 全部凭证带符号合计（已体现冲销关系）；
  * 多币种不直接相加：主序列仅取物业结算币种，其他币种单独分组列出；
  * 期间上限 92 天（防全表扫描，与 §4.1 billing/summary 同口径）；
  * 空区间返回空序列与零汇总，不报错；金额一律 Decimal → str。
"""
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from typing import TypedDict
from zoneinfo import ZoneInfo

from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.billing.models import LedgerEntry
from apps.core import labels as L
from apps.core.api_contract import ApiError, api_response, api_view
from apps.core.models import AuditEvent
from apps.guests.models import Guest, GuestMembership
from apps.identity.authorization import AuthorizationService
from apps.inventory.models import RoomTypeInventory
from apps.inventory.services import _property_today
from apps.reservations.models import Reservation, Stay

from .billing_center import _money_str  # 金额两位小数字符串，与账务中心同口径


class _MemberAggregate(TypedDict):
    room_nights: int
    spend: Decimal


def _property_tz_bounds(property_obj, date_from, date_to):
    """验收缺陷修复（§6 时间口径）：日期边界按**物业时区**换算为 aware datetime。

    此前 created_at__date 查找按 settings 时区（UTC）切日，上海凌晨时段
    写入的数据会落到 UTC 的"昨天"，导致 MySQL+USE_TZ 下"今天"过滤漏数据。
    """
    tz = ZoneInfo(getattr(property_obj, "timezone", None) or "Asia/Shanghai")
    start = timezone.make_aware(datetime.combine(date_from, dtime.min), tz)
    end = timezone.make_aware(datetime.combine(date_to, dtime.max), tz)
    return start, end


# 区间上限（§4.5 技术要求：复杂报表查询需最大日期范围控制）
MAX_RANGE_DAYS = 92
# 缺省统计期间：最近 7 天（含物业时区今天）
DEFAULT_RANGE_DAYS = 7

# 第 3 批快照型报表（A4/A5）：近 30 天趋势窗口（含物业时区今天与 0 计数日）
TREND_DAYS = 30
# 会员报表 Top 会员条数
TOP_MEMBERS_LIMIT = 10
# 渠道来源"确认转化"状态集：CONFIRMED 及其后的生命周期状态
#（未到店 NO_SHOW 亦视为确认生效——订单已确认、违约发生在确认之后）；
# PENDING / CANCELLED / EXPIRED 不计入。
CONFIRMED_STATES = (Reservation.Status.CONFIRMED, Reservation.Status.CHECKED_IN,
                    Reservation.Status.CHECKED_OUT, Reservation.Status.NO_SHOW)
# 渠道来源"入住转化"：存在非作废 Stay（在住/已退房）；VOIDED 不算入住
STAY_STATES = (Stay.Status.IN_HOUSE, Stay.Status.CHECKED_OUT)
# 会员"累计入住晚数/消费"口径：仅实际发生入住的预订（已入住/已退房）；
# 待支付/已确认未入住/未到店/取消/过期均不计入
STAYED_RESERVATION_STATES = (Reservation.Status.CHECKED_IN,
                             Reservation.Status.CHECKED_OUT)

# 入口类型标签（避免逐行 get_Foo_display 查询，聚合行只有代码值）
ENTRY_TYPE_LABELS = dict(LedgerEntry.EntryType.choices)

# 客源来源标签（同 front_desk._source_label 口径）
SOURCE_LABELS = {"WEBSITE": "官网预订", "WALK_IN": "散客直入",
                 "OTA": "渠道预订", "PHONE": "电话预订"}


# ---------------------------------------------------------------------------
# 门禁与通用工具
# ---------------------------------------------------------------------------
def _context(request, permission_code):
    """统一门禁：认证 → 激活 → 物业上下文 → 门店权限码（口径同 audit_center）。"""
    user = request.user
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, permission_code):
        raise ApiError("PERMISSION_DENIED", "当前岗位无权查看该报表。")
    return property_obj


def _parse_day(value, field):
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", f"{field} 必须使用 YYYY-MM-DD 格式。") from None


def _parse_range(request, property_obj):
    """解析 from/to：缺省最近 7 天；from>to 或跨度 >92 天 → 400。"""
    date_to = _parse_day(request.GET.get("to"), "to") if request.GET.get("to") \
        else _property_today(property_obj)
    date_from = _parse_day(request.GET.get("from"), "from") if request.GET.get("from") \
        else date_to - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if date_from > date_to:
        raise ApiError("VALIDATION_ERROR", "from 不能晚于 to。")
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise ApiError("VALIDATION_ERROR", f"统计区间最长 {MAX_RANGE_DAYS} 天。")
    return date_from, date_to


def _pct_str(numerator, denominator):
    """百分比 = numerator / denominator × 100，两位小数字符串；分母 0 → None。"""
    if not denominator:
        return None
    return str((Decimal(numerator) * 100 / Decimal(denominator)).quantize(Decimal("0.01")))


def _metadata(property_obj, date_from, date_to, date_basis, status_scope, extra=None):
    """统计口径 metadata（P0-07 模式）：物业/期间/日期口径/状态范围/币种/生成时间。"""
    meta = {
        "property": {"code": property_obj.code, "name": property_obj.name},
        "period": {"start": str(date_from), "end": str(date_to),
                   "days": (date_to - date_from).days + 1},
        "date_basis": date_basis,
        "status_scope": status_scope,
        "currency": property_obj.currency,
        "generated_at": timezone.now().isoformat(),
        "source_system": "PMS2",
    }
    if extra:
        meta.update(extra)
    return meta


def _entry_type_label(code):
    return ENTRY_TYPE_LABELS.get(code, code)


def _trend_window(property_obj):
    """快照型报表（A4/A5）趋势窗口：近 TREND_DAYS 天（含物业时区今天，含 0 日）。

    返回 (start_day, end_day, tz_start, tz_end)：日期闭区间用于零值补齐，
    aware datetime 边界用于 created_at 过滤（与 _parse_range 同换算口径）。
    """
    end_day = _property_today(property_obj)
    start_day = end_day - timedelta(days=TREND_DAYS - 1)
    tz_start, tz_end = _property_tz_bounds(property_obj, start_day, end_day)
    return start_day, end_day, tz_start, tz_end


def _trend_day_keys(start_day):
    """趋势窗口内的全部自然日升序键（含 0 计数日，dict 保序）。"""
    return {start_day + timedelta(days=offset): 0 for offset in range(TREND_DAYS)}


def _bucket_day(moment, tz, day_keys):
    """把 aware datetime 归入趋势窗口的自然日；窗口外返回 None。"""
    day = timezone.localdate(moment, tz)
    return day if day in day_keys else None


# ---------------------------------------------------------------------------
# GET /api/v1/reports/occupancy —— 入住率（inventory 口径）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def occupancy_report(request):
    """入住率报表（按日）：可售房数/已住房数/入住率%。

    口径：RoomTypeInventory 每日库存快照（逐日留存的库存事实，可回溯历史，
    优于物理房三维"当前状态"近似）；OCC = Σsold / Σ(total − out_of_service)。
    """
    property_obj = _context(request, "hotelapp.view_accommodation")
    date_from, date_to = _parse_range(request, property_obj)
    rows = (RoomTypeInventory.objects.filter(
        property=property_obj, stay_date__gte=date_from, stay_date__lte=date_to,
    ).values("stay_date").annotate(
        total=Sum("total_rooms"), sold=Sum("sold"), held=Sum("held"),
        out_of_service=Sum("out_of_service"),
    ).order_by("stay_date"))
    series = []
    total_sellable = total_sold = 0
    for row in rows:
        sellable = (row["total"] or 0) - (row["out_of_service"] or 0)
        sold = row["sold"] or 0
        total_sellable += sellable
        total_sold += sold
        series.append({
            "date": str(row["stay_date"]),
            "total_rooms": row["total"] or 0,
            "out_of_service": row["out_of_service"] or 0,
            "held": row["held"] or 0,
            "sold_room_nights": sold,
            "sellable_room_nights": sellable,
            "occupancy_pct": _pct_str(sold, sellable),
        })
    data = {
        "series": series,
        "summary": {
            "sellable_room_nights": total_sellable,
            "sold_room_nights": total_sold,
            "occupancy_pct": _pct_str(total_sold, total_sellable),
        },
        "metadata": _metadata(
            property_obj, date_from, date_to,
            date_basis={"inventory": "住宿日（stay_date，逐日库存快照）"},
            status_scope={
                "occ_numerator": "RoomTypeInventory.sold（已售房量求和）",
                "occ_denominator": "total_rooms − out_of_service（停用房从分母剔除）；"
                                   "held（待支付冻结）计入分母不计入分子",
            }),
    }
    return api_response(request, data)


# ---------------------------------------------------------------------------
# GET /api/v1/reports/adr —— ADR（房费收入 / 已售间夜）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def adr_report(request):
    """ADR 报表（按日）：房费收入/已售间夜/ADR。

    口径：分子 = LedgerEntry ROOM_CHARGE 及其红字冲销（REVERSAL→ROOM_CHARGE）
    的带符号合计（已过账账本口径）；分母 = RoomTypeInventory.sold 间夜。
    仅对物业结算币种计算 ADR；其他币种房费单列不参与计算（多币种不相加）。
    """
    property_obj = _context(request, "hotelapp.finance_access")
    date_from, date_to = _parse_range(request, property_obj)
    base_currency = property_obj.currency
    room_revenue_qs = LedgerEntry.objects.filter(
        property=property_obj, business_date__gte=date_from,
        business_date__lte=date_to,
    ).filter(
        Q(entry_type=LedgerEntry.EntryType.ROOM_CHARGE)
        | Q(entry_type=LedgerEntry.EntryType.REVERSAL,
            void_of__entry_type=LedgerEntry.EntryType.ROOM_CHARGE))
    # 90+ 天同样单查询聚合：按营业日×币种一次取回，Python 侧拆主币种序列。
    by_day = {
        row["business_date"]: row["revenue"]
        for row in room_revenue_qs.values("business_date", "currency").annotate(
            revenue=Sum("amount"))
        if row["currency"] == base_currency
    }
    per_currency = {
        row["currency"]: row["total"]
        for row in room_revenue_qs.values("currency").annotate(total=Sum("amount"))
    }
    sold_by_day = {
        row["stay_date"]: row["sold"]
        for row in RoomTypeInventory.objects.filter(
            property=property_obj, stay_date__gte=date_from,
            stay_date__lte=date_to).values("stay_date").annotate(sold=Sum("sold"))
    }
    # 序列只覆盖有数据（房费凭证或库存快照）的日期；空期间 → 空序列不报错。
    series = []
    total_revenue = Decimal("0.00")
    total_sold = 0
    for day in sorted(set(by_day) | set(sold_by_day)):
        revenue = by_day.get(day) or Decimal("0.00")
        sold = sold_by_day.get(day) or 0
        total_revenue += revenue
        total_sold += sold
        series.append({
            "date": str(day),
            "room_revenue": _money_str(revenue),
            "sold_room_nights": sold,
            "adr": _money_str(revenue / Decimal(sold)) if sold else None,
        })
    other = [
        {"currency": code, "room_revenue": _money_str(amount)}
        for code, amount in sorted(per_currency.items()) if code != base_currency
    ]
    data = {
        "series": series,
        "summary": {
            "currency": base_currency,
            "room_revenue": _money_str(total_revenue),
            "sold_room_nights": total_sold,
            "adr": _money_str(total_revenue / Decimal(total_sold)) if total_sold else None,
            "other_currencies": other,
        },
        "metadata": _metadata(
            property_obj, date_from, date_to,
            date_basis={
                "room_revenue": "营业日（LedgerEntry.business_date）",
                "sold_room_nights": "住宿日（RoomTypeInventory.stay_date）",
            },
            status_scope={
                "numerator": "ROOM_CHARGE 凭证 + 其红字冲销（REVERSAL）带符号合计；"
                             "不含押金/收款/调整（支付金额不直接当收入）",
                "denominator": "RoomTypeInventory.sold 求和（库存已售间夜）",
                "adr_currency": "仅对物业结算币种计算；其他币种房费单列不相加",
            }),
    }
    return api_response(request, data)


# ---------------------------------------------------------------------------
# GET /api/v1/reports/revenue —— 收入/支出/净额（LedgerEntry 口径）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def revenue_report(request):
    """收入报表：group_by=day（按营业日）或 type（按凭证类型）分组。

    口径：收入 = 正数凭证合计；支出 = 负数凭证合计的绝对值（含红字冲销）；
    净额 = 全部凭证带符号合计。主序列仅取物业结算币种，其他币种单列不相加。
    """
    property_obj = _context(request, "hotelapp.finance_access")
    date_from, date_to = _parse_range(request, property_obj)
    group_by = (request.GET.get("group_by") or "day").strip().lower()
    if group_by not in ("day", "type"):
        raise ApiError("VALIDATION_ERROR", "group_by 只支持 day 或 type。")
    base_currency = property_obj.currency
    dim = "business_date" if group_by == "day" else "entry_type"
    rows = (LedgerEntry.objects.filter(
        property=property_obj, business_date__gte=date_from,
        business_date__lte=date_to,
    ).values(dim, "currency").annotate(
        revenue=Sum("amount", filter=Q(amount__gt=0)),
        expense=Sum("amount", filter=Q(amount__lt=0)),
        net=Sum("amount"), entries=Count("id"),
    ).order_by(dim))

    def _row(row):
        expense = row["expense"] or Decimal("0.00")
        return {
            dim: str(row[dim]) if dim == "business_date" else row[dim],
            f"{dim}_label": _entry_type_label(row[dim]) if dim == "entry_type" else None,
            "revenue": _money_str(row["revenue"] or Decimal("0.00")),
            "expense": _money_str(-expense),
            "net": _money_str(row["net"] or Decimal("0.00")),
            "entries": row["entries"],
        }

    base_rows = [row for row in rows if row["currency"] == base_currency]
    series = [_row(row) for row in base_rows]
    other_currencies = [{
        "currency": row["currency"],
        "revenue": _money_str(row["revenue"] or Decimal("0.00")),
        "expense": _money_str(-(row["expense"] or Decimal("0.00"))),
        "net": _money_str(row["net"] or Decimal("0.00")),
        "entries": row["entries"],
    } for row in rows if row["currency"] != base_currency]

    # 汇总用原始 Decimal 聚合值求和（序列行已序列化为字符串）；
    # 负数聚合列（expense）取绝对值口径。
    total_revenue = sum((row["revenue"] or Decimal("0.00") for row in base_rows),
                        Decimal("0.00"))
    total_expense = sum((-row["expense"] for row in base_rows
                         if row["expense"] is not None), Decimal("0.00"))
    total_entries = sum(row["entries"] for row in base_rows)
    data = {
        "group_by": group_by,
        "series": series,
        "summary": {
            "currency": base_currency,
            "total_revenue": _money_str(total_revenue),
            "total_expense": _money_str(total_expense),
            "net": _money_str(total_revenue - total_expense),
            "entries": total_entries,
            "other_currencies": other_currencies,
        },
        "metadata": _metadata(
            property_obj, date_from, date_to,
            date_basis={"ledger_entries": "营业日（LedgerEntry.business_date）"},
            status_scope={
                "entries": "全部状态（账本只追加，凭证即事实）",
                "revenue": "收入=正数凭证合计（房费/消费/正向调整）；"
                           "支出=负数凭证合计的绝对值（收款/押金/退款/红字冲销等"
                           "负向凭证，账本资金变动口径，非损益口径）；"
                           "净额=全部凭证带符号合计，已体现冲销关系",
                "multi_currency": "主序列仅物业结算币种；其他币种单列不相加",
            }),
    }
    return api_response(request, data)


# ---------------------------------------------------------------------------
# GET /api/v1/reports/guests —— 客源（新客/回头客/来源分布）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def guests_report(request):
    """客源报表：新客/回头客计数 + 预订来源 source 分布。

    口径：新客 = 期间内建档（Guest.created_at 自然日）的客人数；
    回头客 = 期间内有预订且在本物业历史预订总数 ≥ 2 的去重客人数；
    来源分布 = 期间内创建预订按 Reservation.source 分组计数。
    不输出任何客人 PII 字段（仅计数）。
    """
    property_obj = _context(request, "hotelapp.view_accommodation")
    date_from, date_to = _parse_range(request, property_obj)
    _tz_start, _tz_end = _property_tz_bounds(property_obj, date_from, date_to)
    new_guests = Guest.objects.filter(
        property=property_obj, created_at__gte=_tz_start,
        created_at__lte=_tz_end).count()
    period_qs = Reservation.objects.filter(
        property=property_obj, created_at__gte=_tz_start,
        created_at__lte=_tz_end)
    guest_ids = list(period_qs.values_list("guest_id", flat=True).distinct())
    history_counts = {
        row["guest_id"]: row["total"]
        for row in Reservation.objects.filter(
            property=property_obj, guest_id__in=guest_ids,
        ).values("guest_id").annotate(total=Count("id"))
    }
    returning_guests = sum(1 for total in history_counts.values() if total >= 2)
    by_source = [{
        "source": row["source"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"] or "其他"),
        "reservations": row["total"],
    } for row in period_qs.values("source").annotate(
        total=Count("id")).order_by("-total", "source")]
    data = {
        "summary": {
            "new_guests": new_guests,
            "returning_guests": returning_guests,
            "reservations": period_qs.count(),
            "unique_guests": len(guest_ids),
        },
        "by_source": by_source,
        # 报表中心前端统一读 data.series 渲染明细表（用户报告：
        # 客源报表 "Cannot read properties of undefined (reading 'length')"）；
        # by_source 保留为兼容别名，二者指向同一数组。
        "series": by_source,
        "metadata": _metadata(
            property_obj, date_from, date_to,
            date_basis={
                "new_guests": "自然日（Guest.created_at 建档时间）",
                "reservations": "自然日（Reservation.created_at 下单时间）",
            },
            status_scope={
                "new_guests": "全部建档（含未入住）",
                "returning_guests": "期间内有预订且本物业历史预订总数 ≥ 2 的去重客人"
                                    "（含已取消预订；一次预订计数一次）",
                "by_source": "全部预订状态",
                "pii": "仅聚合计数，不输出客人身份字段",
            }),
    }
    return api_response(request, data)


# ---------------------------------------------------------------------------
# GET /api/v1/reports/housekeeping —— 清洁效率（AuditEvent 事件口径）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def housekeeping_report(request):
    """清洁效率报表：housekeeping.status_changed 审计事件按自然日计数。

    口径：事件口径——AuditEvent（housekeeping.status_changed，由
    InventoryService.change_housekeeping_status 落事件）按 created_at
    自然日分组计数；非清洁任务台账，事件缺失/回补不影响账务。
    """
    property_obj = _context(request, "hotelapp.view_accommodation")
    date_from, date_to = _parse_range(request, property_obj)
    _tz_start, _tz_end = _property_tz_bounds(property_obj, date_from, date_to)
    qs = AuditEvent.objects.filter(
        property=property_obj, action="housekeeping.status_changed",
        created_at__gte=_tz_start, created_at__lte=_tz_end)
    # 分组按物业时区自然日（Python 侧分组：事件为操作级量级，可接受）
    buckets: dict[date, int] = {}
    for ev in qs.only("created_at"):
        local_day = timezone.localdate(ev.created_at, ZoneInfo(
            getattr(property_obj, "timezone", None) or "Asia/Shanghai"))
        buckets[local_day] = buckets.get(local_day, 0) + 1
    series = [{"date": str(day), "status_changes": total}
              for day, total in sorted(buckets.items())]
    total_changes = sum(buckets.values())
    distinct_rooms = qs.values("entity_id").distinct().count()
    period_days = (date_to - date_from).days + 1
    data = {
        "series": series,
        "summary": {
            "total_changes": total_changes,
            "rooms_involved": distinct_rooms,
            "avg_changes_per_day": _money_str(
                Decimal(total_changes) / Decimal(period_days)) if period_days else None,
        },
        "metadata": _metadata(
            property_obj, date_from, date_to,
            date_basis={"events": "自然日（AuditEvent.created_at，非营业日）"},
            status_scope={
                "events": "仅 action=housekeeping.status_changed 的审计事件计数"
                          "（清洁链每次流转记一次，含净房放行与检查退回）",
                "note": "事件口径为运营趋势参考，非清洁任务完成率",
            }),
    }
    return api_response(request, data)


# ---------------------------------------------------------------------------
# GET /api/v1/reports/memberships —— 会员报表（A4，GuestMembership 口径）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def memberships_report(request):
    """会员报表：总数/等级分布/状态分布/近 30 天新增趋势/Top 会员。

    口径：
      * 会员 = 物业维度 GuestMembership 记录（含待生效/暂停/过期/终止全部状态）；
      * 等级分布 = level_code 分组计数（level_code 为 CharField 无 choices，
        中文标签走 apps.core.labels.MEMBER_LEVEL_LABELS）；
      * 新增 = 会员关系建档日（GuestMembership.created_at 自然日，物业时区）；
        joined_at 允许为空（历史导入可缺省），故建档时间为可算口径；
      * Top 会员按累计入住晚数降序：间夜 = Σ(check_out−check_in)×room_count，
        仅取 CHECKED_IN/CHECKED_OUT 预订（以 Reservation.guest=预订联系人关联
        会员客人）；total_spend 为同状态预订 total_amount 合计（订单金额口径，
        非账务实收收入）。仅输出 member_no 与聚合数值，不输出客人姓名/手机号。
    """
    property_obj = _context(request, "hotelapp.view_accommodation")
    qs = GuestMembership.objects.filter(property=property_obj)
    total_members = qs.count()
    by_level = [{
        "level_code": row["level_code"],
        "level_label": L.member_level_label(row["level_code"]),
        "members": row["members"],
    } for row in qs.values("level_code").annotate(
        members=Count("id")).order_by("-members", "level_code")]
    status_labels = L.gmembership_status_labels()
    by_status = [{
        "status": row["status"],
        "status_label": status_labels.get(row["status"], row["status"]),
        "members": row["members"],
    } for row in qs.values("status").annotate(
        members=Count("id")).order_by("-members", "status")]
    status_counts = {row["status"]: row["members"] for row in by_status}

    # 近 30 天新增趋势（含 0 日）：建档时间按物业时区自然日归桶
    trend_start, trend_end, tz_start, tz_end = _trend_window(property_obj)
    tz = ZoneInfo(getattr(property_obj, "timezone", None) or "Asia/Shanghai")
    day_keys = _trend_day_keys(trend_start)
    for row in GuestMembership.objects.filter(
            property=property_obj, created_at__gte=tz_start,
            created_at__lte=tz_end).values("created_at"):
        day = _bucket_day(row["created_at"], tz, day_keys)
        if day is not None:
            day_keys[day] += 1
    new_trend = [{"date": str(day), "new_members": count}
                 for day, count in day_keys.items()]

    # Top 会员：全部会员关系的客人入住/消费聚合（1 次预订查询 + Python 归集）
    member_rows = list(qs.values(
        "id", "member_no", "guest_id", "level_code", "status"))
    per_guest: dict[int, _MemberAggregate] = {}
    guest_ids = {row["guest_id"] for row in member_rows}
    if guest_ids:
        for res in Reservation.objects.filter(
                property=property_obj, guest_id__in=guest_ids,
                status__in=STAYED_RESERVATION_STATES).values(
                "guest_id", "check_in", "check_out", "room_count", "total_amount"):
            agg = per_guest.setdefault(
                res["guest_id"], {"room_nights": 0, "spend": Decimal("0.00")})
            agg["room_nights"] += (res["check_out"] - res["check_in"]).days \
                * res["room_count"]
            agg["spend"] += res["total_amount"] or Decimal("0.00")

    def _top_key(row):
        agg = per_guest.get(row["guest_id"],
                            {"room_nights": 0, "spend": Decimal("0.00")})
        return (-agg["room_nights"], -agg["spend"], row["member_no"])

    top_members = [{
        "member_no": row["member_no"],
        "level_code": row["level_code"],
        "level_label": L.member_level_label(row["level_code"]),
        "status": row["status"],
        "room_nights": per_guest.get(
            row["guest_id"], {"room_nights": 0})["room_nights"],
        "total_spend": _money_str(per_guest.get(
            row["guest_id"], {"spend": Decimal("0.00")})["spend"]),
    } for row in sorted(member_rows, key=_top_key)[:TOP_MEMBERS_LIMIT]]

    data = {
        "summary": {
            "total_members": total_members,
            "pending_members": status_counts.get(GuestMembership.Status.PENDING, 0),
            "active_members": status_counts.get(GuestMembership.Status.ACTIVE, 0),
            "suspended_members": status_counts.get(GuestMembership.Status.SUSPENDED, 0),
            "expired_members": status_counts.get(GuestMembership.Status.EXPIRED, 0),
            "terminated_members": status_counts.get(GuestMembership.Status.TERMINATED, 0),
            "trend_window": {"start": str(trend_start), "end": str(trend_end),
                             "days": TREND_DAYS},
            "top_members_limit": TOP_MEMBERS_LIMIT,
            "top_metric": "room_nights",
        },
        "by_level": by_level,
        "by_status": by_status,
        "new_trend": new_trend,
        "top_members": top_members,
        "metadata": _metadata(
            property_obj, trend_start, trend_end,
            date_basis={
                "memberships": "全量快照（不限统计期间）",
                "new_trend": "自然日（GuestMembership.created_at 建档时间，"
                             "物业时区；joined_at 允许为空不作趋势口径）",
            },
            status_scope={
                "members": "全部 GuestMembership 状态（含待生效/暂停）",
                "new_trend": f"近 {TREND_DAYS} 天按日新增（含 0 计数日），"
                             "窗口=[物业时区今天−29, 物业时区今天]",
                "top_members": f"按累计入住晚数降序 Top {TOP_MEMBERS_LIMIT}："
                               "间夜=Σ(check_out−check_in)×room_count，仅取 "
                               "CHECKED_IN/CHECKED_OUT 预订（Reservation.guest="
                               "预订联系人口径）；total_spend=同状态预订 "
                               "total_amount 合计（订单金额口径，非账务实收）",
                "pii": "仅输出 member_no 与聚合数值，不输出客人姓名/手机号",
            }),
    }
    return api_response(request, data)


# ---------------------------------------------------------------------------
# GET /api/v1/reports/sources —— 渠道来源报表（A5，Reservation.source 口径）
# ---------------------------------------------------------------------------
@require_GET
@api_view
def sources_report(request):
    """渠道来源报表：各来源订单/确认转化/入住转化/GMV + 近 30 天趋势。

    口径：
      * 订单数 = 全部状态预订按 Reservation.source 分组计数；
      * 确认转化 = 状态 ∈ {CONFIRMED, CHECKED_IN, CHECKED_OUT, NO_SHOW}
        （确认生效口径：未到店发生在确认之后，计入转化）；
      * 入住转化 = 存在非作废 Stay（IN_HOUSE/CHECKED_OUT）的去重预订数；
      * GMV = 全部状态预订 total_amount 合计（订单金额口径，未扣取消/未入住，
        非账务实收收入）；仅统计物业结算币种预订（多币种不相加）；
      * 趋势 = 近 30 天按日下单量与 GMV（含 0 日， Reservation.created_at）。
    """
    property_obj = _context(request, "hotelapp.view_accommodation")
    base_currency = property_obj.currency
    qs = Reservation.objects.filter(property=property_obj)
    source_rows = qs.values("source").annotate(
        reservations=Count("id"),
        confirmed=Count("id", filter=Q(status__in=CONFIRMED_STATES)),
    ).order_by("-reservations", "source")
    # 入住转化单独一查询（join Stay 会复制行，distinct 去重预订计数）
    stay_by_source = {
        row["source"]: row["with_stay"]
        for row in qs.filter(
            reservation_rooms__stay__status__in=STAY_STATES,
        ).values("source").annotate(with_stay=Count("id", distinct=True))
    }
    gmv_by_source = {
        row["source"]: row["gmv"] or Decimal("0.00")
        for row in qs.filter(currency=base_currency).values("source").annotate(
            gmv=Sum("total_amount"))
    }
    by_source = [{
        "source": row["source"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"] or "其他"),
        "reservations": row["reservations"],
        "confirmed": row["confirmed"],
        "confirmed_rate": _pct_str(row["confirmed"], row["reservations"]),
        "with_stay": stay_by_source.get(row["source"], 0),
        "stay_rate": _pct_str(stay_by_source.get(row["source"], 0),
                              row["reservations"]),
        "gmv": _money_str(gmv_by_source.get(row["source"], Decimal("0.00"))),
    } for row in source_rows]

    total_reservations = sum(row["reservations"] for row in source_rows)
    total_confirmed = sum(row["confirmed"] for row in source_rows)
    total_with_stay = sum(stay_by_source.values())
    total_gmv = sum(gmv_by_source.values(), Decimal("0.00"))

    # 近 30 天趋势（含 0 日）：下单时间按物业时区自然日归桶
    trend_start, trend_end, tz_start, tz_end = _trend_window(property_obj)
    tz = ZoneInfo(getattr(property_obj, "timezone", None) or "Asia/Shanghai")
    day_keys = _trend_day_keys(trend_start)
    gmv_by_day = {day: Decimal("0.00") for day in day_keys}
    for row in Reservation.objects.filter(
            property=property_obj, created_at__gte=tz_start,
            created_at__lte=tz_end).values(
            "created_at", "currency", "total_amount"):
        day = _bucket_day(row["created_at"], tz, day_keys)
        if day is None:
            continue
        day_keys[day] += 1
        if row["currency"] == base_currency:
            gmv_by_day[day] += row["total_amount"] or Decimal("0.00")
    trend = [{"date": str(day), "reservations": day_keys[day],
              "gmv": _money_str(gmv_by_day[day])} for day in day_keys]

    data = {
        "summary": {
            "total_reservations": total_reservations,
            "confirmed_reservations": total_confirmed,
            "with_stay_reservations": total_with_stay,
            "gmv": _money_str(total_gmv),
            "confirmed_rate": _pct_str(total_confirmed, total_reservations),
            "stay_rate": _pct_str(total_with_stay, total_reservations),
            "trend_window": {"start": str(trend_start), "end": str(trend_end),
                             "days": TREND_DAYS},
        },
        "by_source": by_source,
        # 报表中心前端统一读 data.series 渲染明细表（同 guests 报表惯例），
        # by_source 保留为兼容别名，二者指向同一数组。
        "series": by_source,
        "trend": trend,
        "metadata": _metadata(
            property_obj, trend_start, trend_end,
            date_basis={
                "by_source": "全量快照（不限统计期间）",
                "trend": "自然日（Reservation.created_at 下单时间，物业时区）",
            },
            status_scope={
                "reservations": "全部预订状态",
                "confirmed": "状态 ∈ CONFIRMED/CHECKED_IN/CHECKED_OUT/NO_SHOW"
                             "（确认生效口径，未到店计入转化）",
                "with_stay": "存在非作废 Stay（IN_HOUSE/CHECKED_OUT）的去重预订数",
                "gmv": "全部状态预订 total_amount 合计（订单金额口径，未扣"
                       "取消/未入住，非账务实收）；仅物业结算币种（多币种不相加）",
                "trend": f"近 {TREND_DAYS} 天按日下单量与 GMV（含 0 计数日），"
                         "窗口=[物业时区今天−29, 物业时区今天]",
            }),
    }
    return api_response(request, data)
