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

授权口径（按报表敏感度分两档，均为 hotelapp.* 门店经营权限）：
  * 收入类（revenue / adr）→ hotelapp.finance_access：
    分子是已过账账本凭证金额，与 §4.1 财务中心读取同一门禁，不以
    view_accommodation 等运营权限替代；
  * 运营类（occupancy / guests / housekeeping）→ hotelapp.view_accommodation：
    入住率/客源/清洁事件是预订与入住模块的运营统计，不含金额与 PII；
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

from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo


def _property_tz_bounds(property_obj, date_from, date_to):
    """验收缺陷修复（§6 时间口径）：日期边界按**物业时区**换算为 aware datetime。

    此前 created_at__date 查找按 settings 时区（UTC）切日，上海凌晨时段
    写入的数据会落到 UTC 的"昨天"，导致 MySQL+USE_TZ 下"今天"过滤漏数据。
    """
    from datetime import datetime
    from datetime import time as dtime

    tz = ZoneInfo(getattr(property_obj, "timezone", None) or "Asia/Shanghai")
    start = timezone.make_aware(datetime.combine(date_from, dtime.min), tz)
    end = timezone.make_aware(datetime.combine(date_to, dtime.max), tz)
    return start, end


from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.billing.models import LedgerEntry
from apps.core.api_contract import ApiError, api_response, api_view
from apps.core.models import AuditEvent
from apps.guests.models import Guest
from apps.identity.authorization import AuthorizationService
from apps.inventory.models import RoomTypeInventory
from apps.inventory.services import _property_today
from apps.reservations.models import Reservation

from .billing_center import _money_str  # 金额两位小数字符串，与账务中心同口径

# 区间上限（§4.5 技术要求：复杂报表查询需最大日期范围控制）
MAX_RANGE_DAYS = 92
# 缺省统计期间：最近 7 天（含物业时区今天）
DEFAULT_RANGE_DAYS = 7

# 入口类型标签（避免逐行 get_Foo_display 查询，聚合行只有代码值）
ENTRY_TYPE_LABELS = dict(LedgerEntry.EntryType.choices)

# 客源来源标签（同 front_desk._source_label 口径）
SOURCE_LABELS = {
    "WEBSITE": "官网预订",
    "WALK_IN": "散客直入",
    "OTA": "渠道预订",
    "PHONE": "电话预订",
}


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
        raise ApiError("VALIDATION_ERROR", f"{field} 必须使用 YYYY-MM-DD 格式。")


def _parse_range(request, property_obj):
    """解析 from/to：缺省最近 7 天；from>to 或跨度 >92 天 → 400。"""
    date_to = (
        _parse_day(request.GET.get("to"), "to")
        if request.GET.get("to")
        else _property_today(property_obj)
    )
    date_from = (
        _parse_day(request.GET.get("from"), "from")
        if request.GET.get("from")
        else date_to - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    )
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
        "period": {
            "start": str(date_from),
            "end": str(date_to),
            "days": (date_to - date_from).days + 1,
        },
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
    rows = (
        RoomTypeInventory.objects.filter(
            property=property_obj,
            stay_date__gte=date_from,
            stay_date__lte=date_to,
        )
        .values("stay_date")
        .annotate(
            total=Sum("total_rooms"),
            sold=Sum("sold"),
            held=Sum("held"),
            out_of_service=Sum("out_of_service"),
        )
        .order_by("stay_date")
    )
    series = []
    total_sellable = total_sold = 0
    for row in rows:
        sellable = (row["total"] or 0) - (row["out_of_service"] or 0)
        sold = row["sold"] or 0
        total_sellable += sellable
        total_sold += sold
        series.append(
            {
                "date": str(row["stay_date"]),
                "total_rooms": row["total"] or 0,
                "out_of_service": row["out_of_service"] or 0,
                "held": row["held"] or 0,
                "sold_room_nights": sold,
                "sellable_room_nights": sellable,
                "occupancy_pct": _pct_str(sold, sellable),
            }
        )
    data = {
        "series": series,
        "summary": {
            "sellable_room_nights": total_sellable,
            "sold_room_nights": total_sold,
            "occupancy_pct": _pct_str(total_sold, total_sellable),
        },
        "metadata": _metadata(
            property_obj,
            date_from,
            date_to,
            date_basis={"inventory": "住宿日（stay_date，逐日库存快照）"},
            status_scope={
                "occ_numerator": "RoomTypeInventory.sold（已售房量求和）",
                "occ_denominator": "total_rooms − out_of_service（停用房从分母剔除）；"
                "held（待支付冻结）计入分母不计入分子",
            },
        ),
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
        property=property_obj,
        business_date__gte=date_from,
        business_date__lte=date_to,
    ).filter(
        Q(entry_type=LedgerEntry.EntryType.ROOM_CHARGE)
        | Q(
            entry_type=LedgerEntry.EntryType.REVERSAL,
            void_of__entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
        )
    )
    # 90+ 天同样单查询聚合：按营业日×币种一次取回，Python 侧拆主币种序列。
    by_day = {
        row["business_date"]: row["revenue"]
        for row in room_revenue_qs.values("business_date", "currency").annotate(
            revenue=Sum("amount")
        )
        if row["currency"] == base_currency
    }
    per_currency = {
        row["currency"]: row["total"]
        for row in room_revenue_qs.values("currency").annotate(total=Sum("amount"))
    }
    sold_by_day = {
        row["stay_date"]: row["sold"]
        for row in RoomTypeInventory.objects.filter(
            property=property_obj, stay_date__gte=date_from, stay_date__lte=date_to
        )
        .values("stay_date")
        .annotate(sold=Sum("sold"))
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
        series.append(
            {
                "date": str(day),
                "room_revenue": _money_str(revenue),
                "sold_room_nights": sold,
                "adr": _money_str(revenue / Decimal(sold)) if sold else None,
            }
        )
    other = [
        {"currency": code, "room_revenue": _money_str(amount)}
        for code, amount in sorted(per_currency.items())
        if code != base_currency
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
            property_obj,
            date_from,
            date_to,
            date_basis={
                "room_revenue": "营业日（LedgerEntry.business_date）",
                "sold_room_nights": "住宿日（RoomTypeInventory.stay_date）",
            },
            status_scope={
                "numerator": "ROOM_CHARGE 凭证 + 其红字冲销（REVERSAL）带符号合计；"
                "不含押金/收款/调整（支付金额不直接当收入）",
                "denominator": "RoomTypeInventory.sold 求和（库存已售间夜）",
                "adr_currency": "仅对物业结算币种计算；其他币种房费单列不相加",
            },
        ),
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
    rows = (
        LedgerEntry.objects.filter(
            property=property_obj,
            business_date__gte=date_from,
            business_date__lte=date_to,
        )
        .values(dim, "currency")
        .annotate(
            revenue=Sum("amount", filter=Q(amount__gt=0)),
            expense=Sum("amount", filter=Q(amount__lt=0)),
            net=Sum("amount"),
            entries=Count("id"),
        )
        .order_by(dim)
    )

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
    other_currencies = [
        {
            "currency": row["currency"],
            "revenue": _money_str(row["revenue"] or Decimal("0.00")),
            "expense": _money_str(-(row["expense"] or Decimal("0.00"))),
            "net": _money_str(row["net"] or Decimal("0.00")),
            "entries": row["entries"],
        }
        for row in rows
        if row["currency"] != base_currency
    ]

    # 汇总用原始 Decimal 聚合值求和（序列行已序列化为字符串）；
    # 负数聚合列（expense）取绝对值口径。
    total_revenue = sum((row["revenue"] or Decimal("0.00") for row in base_rows), Decimal("0.00"))
    total_expense = sum(
        (-row["expense"] for row in base_rows if row["expense"] is not None), Decimal("0.00")
    )
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
            property_obj,
            date_from,
            date_to,
            date_basis={"ledger_entries": "营业日（LedgerEntry.business_date）"},
            status_scope={
                "entries": "全部状态（账本只追加，凭证即事实）",
                "revenue": "收入=正数凭证合计（房费/消费/正向调整）；"
                "支出=负数凭证合计的绝对值（收款/押金/退款/红字冲销等"
                "负向凭证，账本资金变动口径，非损益口径）；"
                "净额=全部凭证带符号合计，已体现冲销关系",
                "multi_currency": "主序列仅物业结算币种；其他币种单列不相加",
            },
        ),
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
        property=property_obj, created_at__gte=_tz_start, created_at__lte=_tz_end
    ).count()
    period_qs = Reservation.objects.filter(
        property=property_obj, created_at__gte=_tz_start, created_at__lte=_tz_end
    )
    guest_ids = list(period_qs.values_list("guest_id", flat=True).distinct())
    history_counts = {
        row["guest_id"]: row["total"]
        for row in Reservation.objects.filter(
            property=property_obj,
            guest_id__in=guest_ids,
        )
        .values("guest_id")
        .annotate(total=Count("id"))
    }
    returning_guests = sum(1 for total in history_counts.values() if total >= 2)
    by_source = [
        {
            "source": row["source"],
            "source_label": SOURCE_LABELS.get(row["source"], row["source"] or "其他"),
            "reservations": row["total"],
        }
        for row in period_qs.values("source")
        .annotate(total=Count("id"))
        .order_by("-total", "source")
    ]
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
            property_obj,
            date_from,
            date_to,
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
            },
        ),
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
        property=property_obj,
        action="housekeeping.status_changed",
        created_at__gte=_tz_start,
        created_at__lte=_tz_end,
    )
    # 分组按物业时区自然日（Python 侧分组：事件为操作级量级，可接受）
    buckets = {}
    for ev in qs.only("created_at"):
        local_day = timezone.localdate(
            ev.created_at, ZoneInfo(getattr(property_obj, "timezone", None) or "Asia/Shanghai")
        )
        buckets[local_day] = buckets.get(local_day, 0) + 1
    series = [{"date": str(day), "status_changes": total} for day, total in sorted(buckets.items())]
    total_changes = sum(row["status_changes"] for row in series)
    distinct_rooms = qs.values("entity_id").distinct().count()
    period_days = (date_to - date_from).days + 1
    data = {
        "series": series,
        "summary": {
            "total_changes": total_changes,
            "rooms_involved": distinct_rooms,
            "avg_changes_per_day": (
                _money_str(Decimal(total_changes) / Decimal(period_days)) if period_days else None
            ),
        },
        "metadata": _metadata(
            property_obj,
            date_from,
            date_to,
            date_basis={"events": "自然日（AuditEvent.created_at，非营业日）"},
            status_scope={
                "events": "仅 action=housekeeping.status_changed 的审计事件计数"
                "（清洁链每次流转记一次，含净房放行与检查退回）",
                "note": "事件口径为运营趋势参考，非清洁任务完成率",
            },
        ),
    }
    return api_response(request, data)
