"""财务与账单中心 API（基线 2.0 §4.1）。

读取面（hotelapp.finance_access + 当前物业有效任职）：
  * GET /api/v1/billing/folios          账单列表（状态/预订号筛选 + 余额汇总 + 分页）
  * GET /api/v1/billing/folios/{id}     账单详情（entries/payments/refunds 明细）
  * GET /api/v1/billing/ledger          不可变流水列表（只追加事实展示；类型/日期区间筛选）
  * GET /api/v1/billing/payments        支付流水列表（状态筛选）
  * GET /api/v1/billing/refunds         退款列表（申请/审批/执行状态）
  * GET /api/v1/billing/shifts          收银班次列表（CashierShift）
  * GET /api/v1/billing/summary         可配置期间汇总（BillingService.financial_summary）

写操作面（全部经 BillingService 领域服务，本模块零直接模型写入）：
  * POST /api/v1/billing/payments   收款（collect_payment；provider_ref 幂等 + @idempotent）
  * POST /api/v1/billing/refunds    退款申请（request_refund；billing.refund_request）
  * POST /api/v1/billing/reversals  红字冲销（reverse_entry；billing.reverse_entry）

安全口径（对齐 apps/api/approval.py P0-01）：
  * 匿名 → 401；已登录但停用/无 DEFAULT 有效任职 → 403（request.property 为 None）；
  * 有任职但缺 hotelapp.finance_access → 403（明细可见性 = finance_access，
    不以 view_accommodation 等替代）；
  * 所有 QuerySet 强制 property 过滤；对象详情非本物业一律 404，不泄露存在性；
  * 金额一律 Decimal → str；账本只读展示，写操作只经 BillingService 追加凭证。
"""
import json
from datetime import date
from decimal import Decimal

from django.db.models import Prefetch, Q, Sum
from django.views.decorators.http import require_GET, require_POST

from apps.billing import dec02
from apps.billing.models import (
    ApprovalRecord,
    BillingAdjustment,
    CashierShift,
    CheckoutExceptionRelease,
    Folio,
    LedgerEntry,
    Payment,
    Refund,
    RefundLiability,
)
from apps.billing.services_v2 import BillingService
from apps.core import labels as L
from apps.core.api_contract import (
    ApiError,
    api_response,
    api_view,
    idempotent,
    paginate,
)
from apps.identity.authorization import AuthorizationService
from apps.reservations.models import Reservation

ZERO = Decimal("0.00")


# ---------------------------------------------------------------------------
# 门禁与通用工具
# ---------------------------------------------------------------------------
def _require_finance_access(request, extra_permission=None):
    """认证 + 激活 + DEFAULT 有效任职 + hotelapp.finance_access（可叠加专项权）。

    匿名 → 401；无有效任职（物业上下文缺失）→ 403；
    有任职但无 finance_access / 专项权限 → 403。
    """
    user = request.user
    if not getattr(user, "is_authenticated", False):
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    if not getattr(user, "is_active", False):
        raise ApiError("PERMISSION_DENIED", "账号已停用，无法访问财务功能。")
    property_obj = getattr(request, "property", None)
    if property_obj is not None:
        membership = AuthorizationService.get_active_membership(user, property_obj)
        if membership is not None and AuthorizationService.has_permission(
                user, property_obj, "hotelapp.finance_access"):
            if extra_permission and not AuthorizationService.has_permission(
                    user, property_obj, extra_permission):
                raise ApiError("PERMISSION_DENIED",
                               "当前岗位没有该项财务操作权限。")
            return property_obj
    raise ApiError("PERMISSION_DENIED", "员工任职或财务权限验证失败，无法访问财务中心。")



def _require_cashier_shift_manage(request):
    """收银班次由前台、财务及管理岗位按专项权限开闭，不要求财务中心浏览权。"""
    user = request.user
    if not getattr(user, "is_authenticated", False):
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    if not getattr(user, "is_active", False):
        raise ApiError("PERMISSION_DENIED", "账号已停用。")
    property_obj = getattr(request, "property", None)
    if property_obj is None or AuthorizationService.get_active_membership(
            user, property_obj) is None:
        raise ApiError("PERMISSION_DENIED", "当前账号没有有效物业任职。")
    if not AuthorizationService.has_permission(
            user, property_obj, "billing.cashier_shift_manage"):
        raise ApiError("PERMISSION_DENIED", "当前岗位没有收银班次管理权限。")
    return property_obj


def _json_body(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是有效 JSON。")
    if not isinstance(payload, dict):
        raise ApiError("VALIDATION_ERROR", "请求体必须是 JSON 对象。")
    return payload


def _parse_date_param(value, label):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", f"{label}必须使用 YYYY-MM-DD 格式。")


def _money_str(value):
    """金额统一 Decimal → str（两位小数字符串，契约口径）。"""
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.01")))


def _folio_balances(folios):
    """批量计算账单余额（账本口径：带符号 LedgerEntry 汇总，非 Payment 汇总）。"""
    aggregated = {
        row["folio"]: row["total"]
        for row in LedgerEntry.objects.filter(
            folio__in=[f.pk for f in folios]).values("folio").annotate(
            total=Sum("amount"))
    }
    return {folio.pk: aggregated.get(folio.pk, ZERO) for folio in folios}



def _invalidate_billing_summary(property_obj):
    """账务写入后失效当前物业未结余额摘要缓存。

    账本写入已经提交后，缓存失效失败不得把成功的财务操作伪装成 500；
    记录异常并依赖 60 秒 TTL 兜底。
    """
    import logging

    from django.core.cache import cache

    try:
        cache.delete("billing:open-summary:%s" % property_obj.pk)
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to invalidate billing summary cache for property %s",
            property_obj.pk)


def _user_name(user):
    if user is None:
        return None
    return getattr(user, "username", None) or str(user.pk)


# ---------------------------------------------------------------------------
# 读取端点
# ---------------------------------------------------------------------------
@require_GET
@api_view
def folio_list(request):
    """GET /api/v1/billing/folios — 账单列表（状态/预订号/类型筛选 + 余额汇总）。"""
    property_obj = _require_finance_access(request)
    qs = Folio.objects.filter(property=property_obj).select_related(
        "reservation", "guest")
    status_filter = request.GET.get("status", "").strip().upper()
    if status_filter:
        if status_filter not in Folio.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 只支持 OPEN/CLOSED/VOIDED。")
        qs = qs.filter(status=status_filter)
    booking_no = request.GET.get("booking_no", "").strip()
    if booking_no:
        qs = qs.filter(reservation__booking_no=booking_no)
    folio_type = request.GET.get("folio_type", "").strip().upper()
    if folio_type:
        if folio_type not in Folio.FolioType.values:
            raise ApiError("VALIDATION_ERROR", "folio_type 取值不合法。")
        qs = qs.filter(folio_type=folio_type)
    query = request.GET.get("q", "").strip()
    if query:
        qs = qs.filter(
            Q(reservation__booking_no__icontains=query)
            | Q(guest__full_name__icontains=query))
    items, pagination = paginate(qs.order_by("-opened_at"), request)
    balances = _folio_balances(items)
    data = [{
        "id": folio.pk,
        "folio_type": folio.folio_type,
        "folio_type_label": folio.get_folio_type_display(),
        "status": folio.status,
        "status_label": folio.get_status_display(),
        "currency": folio.currency,
        "currency_label": L.currency_label(folio.currency),
        "balance": _money_str(balances.get(folio.pk, ZERO)),
        "reservation_id": folio.reservation_id,
        "booking_no": folio.reservation.booking_no if folio.reservation_id else None,
        "reservation_status": folio.reservation.status if folio.reservation_id else None,
        "guest_id": folio.guest_id,
        "guest_name": folio.guest.full_name if folio.guest_id else None,
        "opened_at": folio.opened_at.isoformat() if folio.opened_at else None,
        "closed_at": folio.closed_at.isoformat() if folio.closed_at else None,
    } for folio in items]
    # G10：摘要聚合走 Redis 缓存（60s TTL，P0-05 共享缓存）——
    # 4000+ 账本行 Sum 在高并发下排队是压测 P95 超标主因；
    # 账单写操作（collect/refund/reverse）后由调用方场景自然过期。
    # DEC-18 缓存韧性（用户报告：Redis 未运行时本端点 500）——
    # 缓存不是权威（INV-A09），故障时降级直查数据库，只损失性能不损失可用性。
    import logging

    from django.core.cache import cache
    _summary_key = "billing:open-summary:%s" % property_obj.pk
    summary_cached = None
    try:
        summary_cached = cache.get(_summary_key)
    except Exception:
        logging.getLogger(__name__).exception(
            "billing summary cache read failed for property %s; degrade to DB",
            property_obj.pk)
    if summary_cached is None:
        open_totals = Folio.objects.filter(
            property=property_obj, status=Folio.Status.OPEN)
        open_balance = ZERO
        aggregate = LedgerEntry.objects.filter(
            folio__property=property_obj, folio__status=Folio.Status.OPEN,
        ).values("folio").annotate(total=Sum("amount"))
        for row in aggregate:
            open_balance += row["total"] or ZERO
        summary_cached = {
            "open_folios": open_totals.count(),
            "open_balance": _money_str(open_balance),
        }
        try:
            cache.set(_summary_key, summary_cached, 60)
        except Exception:
            logging.getLogger(__name__).exception(
                "billing summary cache write failed for property %s",
                property_obj.pk)
    pagination["summary"] = {
        "open_folios": summary_cached["open_folios"],
        "open_balance": summary_cached["open_balance"],
        "basis": "LedgerEntry 带符号金额汇总（账本口径，60 秒缓存）",
    }
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def folio_detail(request, folio_id):
    """GET /api/v1/billing/folios/{id} — 账单详情：entries/payments/refunds 明细。"""
    property_obj = _require_finance_access(request)
    folio = (Folio.objects.filter(pk=folio_id, property=property_obj)
             .select_related("reservation", "guest").first())
    if folio is None:
        raise ApiError("RESOURCE_NOT_FOUND", "账单不存在或不属于当前物业。")
    entries = list(folio.entries.order_by("created_at", "id"))
    charges = sum((e.amount for e in entries if e.amount > 0), ZERO)
    credits = sum((-e.amount for e in entries if e.amount < 0), ZERO)
    balance = sum((e.amount for e in entries), ZERO)
    payments = (Payment.objects.filter(folio=folio)
                .prefetch_related(Prefetch(
                    "refunds",
                    queryset=Refund.objects.select_related(
                        "requested_by", "approved_by")
                    .order_by("-created_at", "-id")))
                .order_by("-created_at", "-id"))
    reservation = folio.reservation
    return api_response(request, {
        "id": folio.pk,
        "folio_type": folio.folio_type,
        "folio_type_label": folio.get_folio_type_display(),
        "status": folio.status,
        "status_label": folio.get_status_display(),
        "currency": folio.currency,
        "currency_label": L.currency_label(folio.currency),
        "opened_at": folio.opened_at.isoformat() if folio.opened_at else None,
        "closed_at": folio.closed_at.isoformat() if folio.closed_at else None,
        "reservation": {
            "id": reservation.pk,
            "booking_no": reservation.booking_no,
            "status": reservation.status,
            "status_label": L.reservation_status_labels().get(reservation.status),
        } if reservation else None,
        "guest": {"id": folio.guest_id,
                  "full_name": folio.guest.full_name if folio.guest_id else None},
        "summary": {
            "charges": _money_str(charges),
            "credits": _money_str(credits),
            "balance": _money_str(balance),
        },
        "entries": [{
            "id": entry.pk,
            "entry_type": entry.entry_type,
            "entry_type_label": entry.get_entry_type_display(),
            "amount": _money_str(entry.amount),
            "currency": entry.currency,
            "business_date": entry.business_date.isoformat(),
            "reference": entry.reference,
            "description": entry.description,
            "void_of_id": entry.void_of_id,
            "void_reason": entry.void_reason or "",
            "approval_id": entry.approval_record_id,
            "legacy_workflow": entry.legacy_workflow,
            "posted_by": _user_name(entry.posted_by),
            "created_at": entry.created_at.isoformat(),
        } for entry in entries],
        "payments": [{
            "id": payment.pk,
            "provider": payment.provider,
            "provider_ref": payment.provider_ref,
            "method": payment.method,
            "amount": _money_str(payment.amount),
            "currency": payment.currency,
            "status": payment.status,
            "status_label": L.payment_status_labels().get(payment.status),
            "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        "cashier_shift_id": payment.cashier_shift_id,
            "created_at": payment.created_at.isoformat(),
            "refunds": [{
                "id": refund.pk,
                "amount": _money_str(refund.amount),
                "reason": refund.reason,
                "status": refund.status,
                "status_label": L.refund_status_labels().get(refund.status),
                "provider_ref": refund.provider_ref,
                "requested_by": _user_name(refund.requested_by),
                "approved_by": _user_name(refund.approved_by),
        "executed_by": _user_name(refund.executed_by),
        "executed_at": refund.executed_at.isoformat() if refund.executed_at else None,
        "execution_reference": refund.execution_reference or None,
        "execution_shift_id": refund.execution_shift_id,
        "legacy_workflow": refund.legacy_workflow,
                "created_at": refund.created_at.isoformat(),
                "processed_at": (refund.processed_at.isoformat()
                                 if refund.processed_at else None),
            } for refund in payment.refunds.all().order_by("-created_at", "-id")],
        } for payment in payments],
    })


@require_GET
@api_view
def ledger_entry_list(request):
    """GET /api/v1/billing/ledger — 不可变流水列表（只追加事实展示）。"""
    property_obj = _require_finance_access(request)
    qs = LedgerEntry.objects.filter(property=property_obj).select_related(
        "folio__reservation", "posted_by")
    entry_type = request.GET.get("entry_type", "").strip().upper()
    if entry_type:
        if entry_type not in LedgerEntry.EntryType.values:
            raise ApiError("VALIDATION_ERROR", "entry_type 取值不合法。")
        qs = qs.filter(entry_type=entry_type)
    date_from = _parse_date_param(request.GET.get("from"), "from")
    date_to = _parse_date_param(request.GET.get("to"), "to")
    if date_from:
        qs = qs.filter(business_date__gte=date_from)
    if date_to:
        qs = qs.filter(business_date__lte=date_to)
    folio_id = request.GET.get("folio_id", "").strip()
    if folio_id:
        if not folio_id.isdigit():
            raise ApiError("VALIDATION_ERROR", "folio_id 必须是正整数。")
        qs = qs.filter(folio_id=int(folio_id))
    items, pagination = paginate(qs.order_by("-business_date", "-id"), request)
    data = [{
        "id": entry.pk,
        "folio_id": entry.folio_id,
        "booking_no": entry.folio.reservation.booking_no if entry.folio_id else None,
        "entry_type": entry.entry_type,
        "entry_type_label": entry.get_entry_type_display(),
        "amount": _money_str(entry.amount),
        "currency": entry.currency,
        "business_date": entry.business_date.isoformat(),
        "reference": entry.reference,
        "description": entry.description,
        "void_of_id": entry.void_of_id,
        "void_reason": entry.void_reason or "",
        "approval_id": entry.approval_record_id,
        "legacy_workflow": entry.legacy_workflow,
        "posted_by": _user_name(entry.posted_by),
        "created_at": entry.created_at.isoformat(),
    } for entry in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def payment_list(request):
    """GET /api/v1/billing/payments — 支付流水列表（状态筛选）。"""
    property_obj = _require_finance_access(request)
    qs = Payment.objects.filter(property=property_obj).select_related(
        "folio", "reservation")
    status_filter = request.GET.get("status", "").strip().upper()
    if status_filter:
        if status_filter not in Payment.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 取值不合法。")
        qs = qs.filter(status=status_filter)
    method = request.GET.get("method", "").strip().upper()
    if method:
        qs = qs.filter(method=method)
    booking_no = request.GET.get("booking_no", "").strip()
    if booking_no:
        qs = qs.filter(reservation__booking_no=booking_no)
    items, pagination = paginate(qs.order_by("-created_at"), request)
    data = [{
        "id": payment.pk,
        "folio_id": payment.folio_id,
        "booking_no": payment.reservation.booking_no if payment.reservation_id else None,
        "provider": payment.provider,
        "provider_ref": payment.provider_ref,
        "method": payment.method,
        "amount": _money_str(payment.amount),
        "currency": payment.currency,
        "status": payment.status,
        "status_label": L.payment_status_labels().get(payment.status),
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        "created_at": payment.created_at.isoformat(),
    } for payment in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def refund_list(request):
    """GET /api/v1/billing/refunds — 退款列表（申请/审批/执行状态）。"""
    property_obj = _require_finance_access(request)
    qs = Refund.objects.filter(property=property_obj).select_related(
        "payment", "requested_by", "approved_by", "executed_by", "approval_record")
    status_filter = request.GET.get("status", "").strip().upper()
    if status_filter:
        if status_filter not in Refund.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 取值不合法。")
        qs = qs.filter(status=status_filter)
    items, pagination = paginate(qs.order_by("-created_at"), request)
    data = [{
        "id": refund.pk,
        "payment_id": refund.payment_id,
        "refund_liability_id": refund.refund_liability_id,
        "folio_id": refund.payment.folio_id if refund.payment_id else None,
        "amount": _money_str(refund.amount),
        "currency": refund.currency,
        "reason": refund.reason,
        "status": refund.status,
        "status_label": L.refund_status_labels().get(refund.status),
        "provider_ref": refund.provider_ref,
        "requested_by": _user_name(refund.requested_by),
        "approved_by": _user_name(refund.approved_by),
        "executed_by": _user_name(refund.executed_by),
        "executed_at": refund.executed_at.isoformat() if refund.executed_at else None,
        "execution_reference": refund.execution_reference or None,
        "execution_shift_id": refund.execution_shift_id,
        "legacy_workflow": refund.legacy_workflow,
        "approval_status": (refund.approval_record.status
                            if refund.approval_record_id else None),
        "created_at": refund.created_at.isoformat(),
        "processed_at": (refund.processed_at.isoformat()
                         if refund.processed_at else None),
    } for refund in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def refund_liability_list(request):
    """GET /api/v1/billing/refund-liabilities — 财务待退款负债及候选原支付。"""
    property_obj = _require_finance_access(request)
    qs = RefundLiability.objects.filter(property=property_obj).select_related(
        "reservation", "folio").prefetch_related("source_payments").annotate(
            refunded_amount_total=Sum(
                "refunds__amount",
                filter=Q(refunds__status=Refund.Status.SUCCEEDED),
            )
        )
    status_filter = request.GET.get("status", "").strip().upper()
    if status_filter:
        if status_filter not in RefundLiability.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 取值不合法。")
        qs = qs.filter(status=status_filter)
    items, pagination = paginate(qs.order_by("-created_at"), request)
    data = [{
        "id": item.pk,
        "reservation_id": item.reservation_id,
        "booking_no": item.reservation.booking_no,
        "folio_id": item.folio_id,
        "amount": _money_str(item.amount),
        "refunded_amount": _money_str(item.refunded_amount_total or ZERO),
        "remaining_amount": _money_str(item.remaining_amount),
        "currency": item.currency,
        "status": item.status,
        "status_label": {
            RefundLiability.Status.OPEN: "待处理",
            RefundLiability.Status.PARTIALLY_SETTLED: "部分处理",
            RefundLiability.Status.SETTLED: "已处理",
        }.get(item.status, item.status),
        "candidate_payments": [{
            "payment_id": payment.pk,
            "provider_ref": payment.provider_ref,
            "method": payment.method,
            "amount": _money_str(payment.amount),
            "currency": payment.currency,
            "status": payment.status,
        } for payment in item.source_payments.all()],
        "created_at": item.created_at.isoformat(),
        "settled_at": item.settled_at.isoformat() if item.settled_at else None,
    } for item in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def shift_list(request):
    """GET /api/v1/billing/shifts — 财务可看本物业全部；前台仅看本人班次。"""
    property_obj = getattr(request, "property", None)
    can_view_all = bool(
        property_obj is not None and getattr(request.user, "is_authenticated", False)
        and AuthorizationService.has_permission(
            request.user, property_obj, "hotelapp.finance_access"))
    if can_view_all:
        property_obj = _require_finance_access(request)
    else:
        property_obj = _require_cashier_shift_manage(request)
    qs = CashierShift.objects.filter(property=property_obj).select_related("cashier", "closed_by")
    if not can_view_all:
        qs = qs.filter(cashier=request.user)
    if request.GET.get("mine") == "1":
        qs = qs.filter(cashier=request.user)
    status_filter = request.GET.get("status", "").strip().upper()
    if status_filter:
        if status_filter not in CashierShift.ShiftStatus.values:
            raise ApiError("VALIDATION_ERROR", "status 只支持 OPEN/CLOSED。")
        qs = qs.filter(status=status_filter)
    items, pagination = paginate(qs.order_by("-opened_at"), request)
    data = [{
        "id": shift.pk,
        "cashier_id": shift.cashier_id,
        "cashier": _user_name(shift.cashier),
        "closed_by": _user_name(shift.closed_by),
        "status": shift.status,
        "status_label": shift.get_status_display(),
        "opening_cash": _money_str(shift.opening_cash),
        "expected_cash": _money_str(shift.expected_cash),
        "actual_cash": _money_str(shift.actual_cash),
        "variance": _money_str(shift.variance),
        "variance_reason": shift.variance_reason or "",
        "opened_at": shift.opened_at.isoformat() if shift.opened_at else None,
        "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
    } for shift in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def billing_summary(request):
    """GET /api/v1/billing/summary?from=&to= — 可配置期间财务汇总。

    缺省为当前营业日（物业时区口径）；期间上限 92 天，防全表扫描。
    """
    property_obj = _require_finance_access(request)
    default_day = BillingService._business_date(property_obj)
    date_from = _parse_date_param(request.GET.get("from"), "from") or default_day
    date_to = _parse_date_param(request.GET.get("to"), "to") or default_day
    if date_from > date_to:
        raise ApiError("VALIDATION_ERROR", "from 不能晚于 to。")
    if (date_to - date_from).days > 92:
        raise ApiError("VALIDATION_ERROR", "汇总期间最长 92 天。")
    summary = BillingService.financial_summary(
        property_obj=property_obj, start_date=date_from, end_date=date_to)
    summary["period"] = {"start": str(date_from), "end": str(date_to)}
    summary["basis"] = {
        "ledger_entries": "营业日（business_date）",
        "payments_refunds": "自然日（created_at）",
    }
    summary["property"] = {"code": property_obj.code, "name": property_obj.name}
    return api_response(request, summary)


# ---------------------------------------------------------------------------
# 写操作端点（全部经 BillingService；本模块零直接模型写入）
# ---------------------------------------------------------------------------
@require_POST
@api_view
@idempotent
def collect_payment_api(request):
    """POST /api/v1/billing/payments — 统一收款（BillingService.collect_payment）。

    body: {reservation_id, amount, method, reference?}；reference 即服务层
    provider_ref 幂等键（同号同事实重放返回原支付），叠加 @idempotent 头幂等。
    """
    property_obj = _require_finance_access(request)
    body = _json_body(request)
    raw_id = body.get("reservation_id")
    try:
        reservation_id = int(str(raw_id))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "reservation_id 必须是整数。")
    reservation = Reservation.objects.filter(
        pk=reservation_id, property=property_obj).first()
    if reservation is None:
        raise ApiError("RESOURCE_NOT_FOUND", "预订不存在或不属于当前物业。")
    method = str(body.get("method") or "").strip().upper()
    reference = str(body.get("reference") or "").strip()
    # 服务层同 reference 幂等重放返回原支付；创建/重放由 API 层以
    # “调用前 reference 是否已存在”区分（无 reference 必为新收）。
    # WB-46-12：预检按当前物业过滤——跨物业同名 reference 不再被误判为
    # 本物业重放（服务层对跨物业占用仍独立拒绝为 400）。
    replay = bool(reference) and Payment.objects.filter(
        provider_ref=reference, property=property_obj).exists()
    try:
        payment = BillingService.collect_payment(
            property_obj=property_obj, reservation=reservation,
            amount=body.get("amount"), method=method,
            collected_by=request.user, reference=reference)
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "id": payment.pk,
        "payment_id": payment.pk,
        "folio_id": payment.folio_id,
        "reservation_id": payment.reservation_id,
        "provider_ref": payment.provider_ref,
        "method": payment.method,
        "amount": _money_str(payment.amount),
        "currency": payment.currency,
        "status": payment.status,
    }, status=200 if replay else 201)


@require_POST
@api_view
@idempotent
def request_refund_api(request):
    """POST /api/v1/billing/refunds — 退款申请（BillingService.request_refund）。

    权限：hotelapp.finance_access + billing.refund_request（SoD：申请≠审批）。
    幂等键：body.idempotency_key 优先，缺省取 Idempotency-Key 头。
    """
    property_obj = _require_finance_access(request, extra_permission="billing.refund_request")
    body = _json_body(request)
    raw_id = body.get("payment_id")
    try:
        payment_id = int(str(raw_id))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "payment_id 必须是整数。")
    payment = Payment.objects.filter(pk=payment_id, property=property_obj).first()
    if payment is None:
        raise ApiError("RESOURCE_NOT_FOUND", "支付流水不存在或不属于当前物业。")
    raw_liability_id = body.get("refund_liability_id")
    try:
        refund_liability_id = (
            int(str(raw_liability_id)) if raw_liability_id not in (None, "") else None)
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "refund_liability_id 必须是整数。")
    idempotency_key = str(body.get("idempotency_key")
                          or request.headers.get("Idempotency-Key", "")).strip()
    try:
        refund, created = BillingService.request_refund(
            property_obj=property_obj, payment_id=payment.pk,
            amount=body.get("amount"), reason=body.get("reason"),
            requested_by=request.user, idempotency_key=idempotency_key,
            request=request, refund_liability_id=refund_liability_id)
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "id": refund.pk,
        "refund_id": refund.pk,
        "payment_id": refund.payment_id,
        "refund_liability_id": refund.refund_liability_id,
        "amount": _money_str(refund.amount),
        "currency": refund.currency,
        "reason": refund.reason,
        "status": refund.status,
        "provider_ref": refund.provider_ref,
        "approval_record_id": refund.approval_record_id,
    }, status=201 if created else 200)



@require_POST
@api_view
@idempotent
def approve_refund_api(request, refund_id):
    """审批退款：仅更新审批事实，不执行退款、不产生 REFUND 凭证。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_approve")
    try:
        refund, changed = BillingService.approve_refund(
            property_obj=property_obj, refund_id=refund_id,
            approved_by=request.user)
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "refund_id": refund.pk, "status": refund.status,
        "approved_by_id": refund.approved_by_id,
        "approval_record_id": refund.approval_record_id,
        "changed": changed,
    })


@require_POST
@api_view
@idempotent
def reject_refund_api(request, refund_id):
    """驳回退款：仅登记审批结果，不写退款凭证。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_approve")
    body = _json_body(request)
    try:
        refund, changed = BillingService.reject_refund(
            property_obj=property_obj, refund_id=refund_id,
            rejected_by=request.user, reason=body.get("reason"))
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "refund_id": refund.pk, "status": refund.status,
        "approved_by_id": refund.approved_by_id, "changed": changed,
    })


@require_POST
@api_view
@idempotent
def execute_refund_api(request, refund_id):
    """执行退款：申请人、审批人、执行人三者必须不同。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_execute")
    body = _json_body(request)
    key = str(body.get("idempotency_key")
              or request.headers.get("Idempotency-Key", "")).strip()
    try:
        refund, created = BillingService.execute_refund(
            property_obj=property_obj, refund_id=refund_id,
            executed_by=request.user,
            confirm_refund_paid=body.get("confirm_refund_paid") is True,
            execution_reference=body.get("execution_reference", ""),
            idempotency_key=key)
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "refund_id": refund.pk, "payment_id": refund.payment_id,
        "status": refund.status, "amount": _money_str(refund.amount),
        "approved_by_id": refund.approved_by_id,
        "executed_by_id": refund.executed_by_id,
        "executed_at": refund.executed_at.isoformat() if refund.executed_at else None,
        "execution_shift_id": refund.execution_shift_id,
        "execution_reference": refund.execution_reference,
    }, status=201 if created else 200)


@require_POST
@api_view
@idempotent
def shift_cash_movement_api(request, shift_id):
    """登记班次现金补入/支出；已关班班次不可追加。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.cash_movement_manage")
    if not CashierShift.objects.filter(
            pk=shift_id, property=property_obj).exists():
        raise ApiError("RESOURCE_NOT_FOUND", "班次不存在或不属于当前物业。")
    body = _json_body(request)
    key = str(body.get("idempotency_key")
              or request.headers.get("Idempotency-Key", "")).strip()
    try:
        movement, created = BillingService.record_shift_movement(
            shift_id=shift_id, direction=body.get("direction"),
            amount=body.get("amount"), reason=body.get("reason"),
            created_by=request.user, idempotency_key=key)
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "id": movement.pk, "shift_id": movement.shift_id,
        "direction": movement.direction, "amount": _money_str(movement.amount),
        "reason": movement.reason, "created_at": movement.created_at.isoformat(),
    }, status=201 if created else 200)


def billing_payments(request):
    """路径分发：GET /api/v1/billing/payments → 列表；POST → 收款。"""
    if request.method == "POST":
        return collect_payment_api(request)
    return payment_list(request)


def billing_refunds(request):
    """路径分发：GET /api/v1/billing/refunds → 列表；POST → 退款申请。"""
    if request.method == "POST":
        return request_refund_api(request)
    return refund_list(request)


# ---------------------------------------------------------------------------
# 收银班次（基线 2.0 §3.5 闭环 / §4.1 班次开闭；BillingService 唯一写入口）
# ---------------------------------------------------------------------------
@require_POST
@api_view
@idempotent
def open_shift_api(request):
    """POST /api/v1/billing/shifts/open — 开班（BillingService.open_shift）。

    body: {opening_cash?: "0.00"}；同一收银员已有 OPEN 班次时幂等返回原班次
    （created=false）。权限口径：billing.cashier_shift_manage（前台/财务/管理岗位）。
    """
    property_obj = _require_cashier_shift_manage(request)
    body = _json_body(request)
    try:
        shift, created = BillingService.open_shift(
            property_obj=property_obj, cashier=request.user,
            opening_cash=body.get("opening_cash", "0.00"))
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "id": shift.pk,
        "status": shift.status,
        "opening_cash": _money_str(shift.opening_cash),
        "opened_at": shift.opened_at.isoformat() if shift.opened_at else None,
        "created": created,
    }, status=201 if created else 200)


@require_POST
@api_view
@idempotent
def close_shift_api(request, shift_id):
    """POST /api/v1/billing/shifts/{id}/close — 关班（BillingService.close_shift）。

    body: {actual_cash: "必填", variance_reason?: "差异非零必填"}。
    差异（actual - expected_cash）非零时必须说明并落审计
    （billing.shift_close）；expected_cash 按班次现金收款、退款、补入和支出聚合。
    重复关班 400。
    """
    property_obj = _require_cashier_shift_manage(request)
    shift = CashierShift.objects.filter(
        pk=shift_id, property=property_obj).only("pk", "cashier_id").first()
    if shift is None:
        raise ApiError("RESOURCE_NOT_FOUND", "班次不存在或不属于当前物业。")
    allow_other_cashier = AuthorizationService.has_permission(
        request.user, property_obj, "hotelapp.finance_access")
    if shift.cashier_id != request.user.pk and not allow_other_cashier:
        raise ApiError("PERMISSION_DENIED", "只能关闭本人收银班次。")
    body = _json_body(request)
    if body.get("actual_cash") in (None, ""):
        raise ApiError("VALIDATION_ERROR", "actual_cash（实收现金）必填。")
    try:
        shift = BillingService.close_shift(
            property_obj=property_obj, shift_id=shift_id,
            actual_cash=body.get("actual_cash"),
            closed_by=request.user,
            variance_reason=str(body.get("variance_reason") or ""),
            allow_other_cashier=allow_other_cashier)
    except CashierShift.DoesNotExist:
        raise ApiError("RESOURCE_NOT_FOUND", "班次不存在。")
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "id": shift.pk,
        "status": shift.status,
        "status_label": shift.get_status_display(),
        "opening_cash": _money_str(shift.opening_cash),
        "expected_cash": _money_str(shift.expected_cash),
        "actual_cash": _money_str(shift.actual_cash),
        "variance": _money_str(shift.variance),
        "variance_reason": shift.variance_reason or "",
        "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
    })


@require_POST
@api_view
@idempotent
def reverse_entry_api(request):
    """POST /api/v1/billing/reversals — 红字冲销（BillingService.reverse_entry）。

    权限：财务 hotelapp.finance_access + billing.reverse_entry；执行必须携带
    值班经理/店长已批准的 billing.reversal ApprovalRecord。账本只追加，原凭证不可变。
    """
    property_obj = _require_finance_access(request, extra_permission="billing.reverse_entry")
    body = _json_body(request)
    raw_id = body.get("entry_id")
    try:
        entry_id = int(str(raw_id))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "entry_id 必须是整数。")
    original = LedgerEntry.objects.filter(pk=entry_id, property=property_obj).first()
    if original is None:
        raise ApiError("RESOURCE_NOT_FOUND", "账本凭证不存在或不属于当前物业。")
    if original.entry_type == LedgerEntry.EntryType.REVERSAL:
        raise ApiError("BUSINESS_CONFLICT", "红字冲销凭证不能再次冲销。")
    try:
        approval_id = int(str(body.get("approval_id") or ""))
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_ERROR", "approval_id 必须是已批准的冲销审批编号。")
    try:
        approval = ApprovalRecord.objects.filter(
            pk=approval_id, property=property_obj, action_code="billing.reversal",
            entity_type="LedgerEntry", entity_id=str(original.pk)).first()
        if approval is None:
            raise ApiError("RESOURCE_NOT_FOUND", "冲销审批不存在或不属于当前账本凭证。")
        reversal, created = BillingService.reverse_entry(
            property_obj=property_obj, entry_id=original.pk,
            approval_id=approval_id, reason=approval.reason,
            reversed_by=request.user, request=request)
    except LedgerEntry.DoesNotExist:
        raise ApiError("RESOURCE_NOT_FOUND", "账本凭证不存在。")
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    _invalidate_billing_summary(property_obj)
    return api_response(request, {
        "id": reversal.pk,
        "reversal_id": reversal.pk,
        "original_entry_id": original.pk,
        "void_of_id": reversal.void_of_id,
        "amount": _money_str(reversal.amount),
        "currency": reversal.currency,
        "reference": reversal.reference,
        "entry_type": reversal.entry_type,
        "approval_id": reversal.approval_record_id,
    }, status=201 if created else 200)


# ---------------------------------------------------------------------------
# DEC-02-A1：账务调整 / 抵扣（财务发起、值班经理审批、执行生凭证）
# ---------------------------------------------------------------------------
def _adjustment_dto(adjustment, net=None):
    data = {
        "id": adjustment.pk,
        "booking_no": adjustment.reservation.booking_no,
        "folio_id": adjustment.folio_id,
        "refund_liability_id": adjustment.refund_liability_id,
        "adjustment_type": adjustment.adjustment_type,
        "adjustment_type_label": adjustment.get_adjustment_type_display(),
        "amount": _money_str(adjustment.amount),
        "currency": adjustment.currency,
        "reason": adjustment.reason,
        "evidence_note": adjustment.evidence_note,
        "status": adjustment.status,
        "status_label": adjustment.get_status_display(),
        "requested_by": getattr(adjustment.requested_by, "username", None),
        "approved_by": getattr(adjustment.approved_by, "username", None),
        "executed_by": getattr(adjustment.executed_by, "username", None),
        "requested_at": adjustment.requested_at.isoformat(),
        "approved_at": adjustment.approved_at.isoformat() if adjustment.approved_at else None,
        "executed_at": adjustment.executed_at.isoformat() if adjustment.executed_at else None,
        "ledger_entry_id": adjustment.ledger_entry_id,
    }
    if net is not None:
        data["net"] = {k: _money_str(v) if isinstance(v, Decimal) else v
                       for k, v in net.items()}
    return data


@require_GET
@api_view
def adjustment_list(request):
    """GET /api/v1/billing/adjustments?status= — 账务调整列表（含净额重建）。"""
    property_obj = _require_finance_access(request)
    qs = BillingAdjustment.objects.filter(
        property=property_obj).select_related(
        "reservation", "folio", "refund_liability",
        "requested_by", "approved_by", "executed_by").order_by("-requested_at")
    status_filter = request.GET.get("status", "").strip().upper()
    if status_filter:
        if status_filter not in BillingAdjustment.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 取值不合法。")
        qs = qs.filter(status=status_filter)
    items, pagination = paginate(qs, request)
    data = []
    for adjustment in items:
        net = None
        if adjustment.refund_liability_id:
            net = dec02.liability_net_snapshot(adjustment.refund_liability)
        data.append(_adjustment_dto(adjustment, net=net))
    return api_response(request, data, pagination=pagination)


@require_POST
@api_view
def adjustment_request(request):
    """POST /api/v1/billing/adjustments/request — 财务发起账务调整。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_request")
    body = _json_body(request)
    folio_id = body.get("folio_id")
    if folio_id is None:
        raise ApiError("VALIDATION_ERROR", "folio_id 必填。")
    try:
        adjustment, created = dec02.request_adjustment(
            property_obj=property_obj, folio_id=int(folio_id),
            adjustment_type=str(body.get("adjustment_type") or ""),
            amount=body.get("amount"), reason=body.get("reason"),
            requested_by=request.user,
            evidence_note=body.get("evidence_note", ""),
            idempotency_key=request.headers.get(
                "Idempotency-Key", body.get("idempotency_key", "")))
    except (ValueError, TypeError) as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, _adjustment_dto(adjustment),
                        status=201 if created else 200)


@require_POST
@api_view
def adjustment_approve(request, adjustment_id):
    """POST /api/v1/billing/adjustments/{id}/approve — 值班经理审批。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_approve")
    body = _json_body(request)
    try:
        adjustment = dec02.approve_adjustment(
            property_obj=property_obj, adjustment_id=adjustment_id,
            approved_by=request.user, comment=str(body.get("comment") or ""))
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, _adjustment_dto(adjustment))


@require_POST
@api_view
def adjustment_reject(request, adjustment_id):
    """POST /api/v1/billing/adjustments/{id}/reject — 值班经理驳回。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_approve")
    body = _json_body(request)
    try:
        adjustment = dec02.reject_adjustment(
            property_obj=property_obj, adjustment_id=adjustment_id,
            rejected_by=request.user, reason=str(body.get("reason") or ""))
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, _adjustment_dto(adjustment))


@require_POST
@api_view
def adjustment_execute(request, adjustment_id):
    """POST /api/v1/billing/adjustments/{id}/execute — 执行调整并生成凭证。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_execute")
    try:
        adjustment = dec02.execute_adjustment(
            property_obj=property_obj, adjustment_id=adjustment_id,
            executed_by=request.user)
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    net = None
    if adjustment.refund_liability_id:
        net = dec02.liability_net_snapshot(adjustment.refund_liability)
    return api_response(request, _adjustment_dto(adjustment, net=net))


# ---------------------------------------------------------------------------
# DEC-02-B2：退房异常放行与待财务核查
# ---------------------------------------------------------------------------
def _exception_release_dto(release):
    return {
        "id": release.pk,
        "booking_no": release.reservation.booking_no,
        "folio_id": release.folio_id,
        "stay_id": release.stay_id,
        "amount": _money_str(release.amount),
        "currency": release.currency,
        "reason_code": release.reason_code,
        "reason_code_label": release.get_reason_code_display(),
        "reason_text": release.reason_text,
        "status": release.status,
        "status_label": release.get_status_display(),
        "requested_by": getattr(release.requested_by, "username", None),
        "approved_by": getattr(release.approved_by, "username", None),
        "verified_by": getattr(release.verified_by, "username", None),
        "requested_at": release.requested_at.isoformat(),
        "approved_at": release.approved_at.isoformat() if release.approved_at else None,
        "released_at": release.released_at.isoformat() if release.released_at else None,
        "verified_at": release.verified_at.isoformat() if release.verified_at else None,
        "verify_note": release.verify_note,
        "refund_liability_id": release.refund_liability_id,
    }


@require_GET
@api_view
def exception_release_list(request):
    """GET /api/v1/billing/exception-releases?status= — 待财务核查异常列表。"""
    property_obj = _require_finance_access(request)
    qs = CheckoutExceptionRelease.objects.filter(
        property=property_obj).select_related(
        "reservation", "folio", "requested_by", "approved_by",
        "verified_by").order_by("-requested_at")
    status_filter = request.GET.get("status", "").strip().upper()
    if status_filter:
        if status_filter not in CheckoutExceptionRelease.Status.values:
            raise ApiError("VALIDATION_ERROR", "status 取值不合法。")
        qs = qs.filter(status=status_filter)
    items, pagination = paginate(qs, request)
    return api_response(request, [_exception_release_dto(x) for x in items],
                        pagination=pagination)


@require_POST
@api_view
def exception_release_approve(request, release_id):
    """POST /api/v1/billing/exception-releases/{id}/approve — 值班经理审批。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_approve")
    body = _json_body(request)
    try:
        release = dec02.approve_exception_release(
            property_obj=property_obj, release_id=release_id,
            approved_by=request.user, comment=str(body.get("comment") or ""))
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, _exception_release_dto(release))


@require_POST
@api_view
def exception_release_reject(request, release_id):
    """POST /api/v1/billing/exception-releases/{id}/reject — 值班经理驳回。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_approve")
    body = _json_body(request)
    try:
        release = dec02.reject_exception_release(
            property_obj=property_obj, release_id=release_id,
            rejected_by=request.user, reason=str(body.get("reason") or ""))
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, _exception_release_dto(release))


@require_POST
@api_view
def exception_release_settle(request, release_id):
    """POST /api/v1/billing/exception-releases/{id}/settle — 财务核查结案。"""
    property_obj = _require_finance_access(
        request, extra_permission="billing.refund_execute")
    body = _json_body(request)
    try:
        release = dec02.settle_exception_release(
            property_obj=property_obj, release_id=release_id,
            settled_by=request.user,
            outcome=str(body.get("outcome") or ""),
            verify_note=str(body.get("verify_note") or ""))
    except ValueError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, _exception_release_dto(release))
