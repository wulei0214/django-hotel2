"""Django Admin 中的 PMS2 真实业务概览（只读）。

后台菜单已隐藏旧 hotelapp 存档；本页把 PMS2 前台产生的业务事实与审计事件
直接呈现在后台侧边栏入口，作为「前台写入 → 后台可见」的只读闭环证据。
不提供任何写操作，也不绕过 PMS 业务服务的权限与状态机。
"""

from django.contrib import admin
from django.db.models import Count
from django.shortcuts import render

from apps.billing.models import Folio, LedgerEntry, Payment, Refund
from apps.core.models import AuditEvent
from apps.guests.models import Guest
from apps.inventory.models import Room, RoomType
from apps.reservations.models import Reservation, Stay

_STATUS_LABELS = {
    "PENDING": "待确认",
    "CONFIRMED": "已确认",
    "CHECKED_IN": "已入住",
    "CHECKED_OUT": "已退房",
    "CANCELLED": "已取消",
    "NO_SHOW": "未到店",
}


def _snapshot():
    """返回后台概览用的业务快照，全部来自 PMS2 真实表。"""
    status_rows = list(
        Reservation.objects.values("status").annotate(total=Count("id")).order_by("-total")
    )
    statuses = []
    for row in status_rows:
        status = row["status"]
        statuses.append(
            {
                "status": status,
                "label": _STATUS_LABELS.get(status, status),
                "total": row["total"],
            }
        )
    recent_events = list(
        AuditEvent.objects.select_related("property", "actor").order_by("-created_at", "-id")[:12]
    )
    return {
        "counts": [
            {"label": "住客档案", "total": Guest.objects.count(), "url": "/admin/guests/guest/"},
            {
                "label": "预订主单",
                "total": Reservation.objects.count(),
                "url": "/admin/reservations/reservation/",
            },
            {
                "label": "实际入住",
                "total": Stay.objects.count(),
                "url": "/admin/reservations/stay/",
            },
            {
                "label": "房型",
                "total": RoomType.objects.count(),
                "url": "/admin/inventory/roomtype/",
            },
            {"label": "物理客房", "total": Room.objects.count(), "url": "/admin/inventory/room/"},
            {"label": "住客账单", "total": Folio.objects.count(), "url": "/admin/billing/folio/"},
            {
                "label": "账本凭证",
                "total": LedgerEntry.objects.count(),
                "url": "/admin/billing/ledgerentry/",
            },
            {
                "label": "支付流水",
                "total": Payment.objects.count(),
                "url": "/admin/billing/payment/",
            },
            {"label": "退款申请", "total": Refund.objects.count(), "url": "/admin/billing/refund/"},
            {
                "label": "审计事件",
                "total": AuditEvent.objects.count(),
                "url": "/admin/core/auditevent/",
            },
        ],
        "statuses": statuses,
        "recent_events": recent_events,
    }


@admin.site.admin_view
def pms_business_overview(request):
    """只读业务概览；沿用 Django admin 登录门禁，不赋予任何业务写权限。"""
    context = {"title": "PMS2 业务概览", **_snapshot()}
    return render(request, "admin/pms_business_overview.html", context)
