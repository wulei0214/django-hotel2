from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.shortcuts import render
from django.utils import timezone

from apps.core.models import Property


def public_booking_page(request):
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        property_obj = Property.objects.filter(
            code=getattr(settings, "DEFAULT_PROPERTY_CODE", "DEFAULT"),
            status=Property.Status.ACTIVE,
        ).first()
    if property_obj is None:
        return render(
            request,
            "public/booking.html",
            {"property_name": "酒店预订", "booking_unavailable": True},
            status=503,
        )

    today = timezone.localdate(timezone.now(), ZoneInfo(property_obj.timezone))
    return render(
        request,
        "public/booking.html",
        {
            "property_name": property_obj.name,
            "default_check_in": (today + timedelta(days=1)).isoformat(),
            "default_check_out": (today + timedelta(days=2)).isoformat(),
            "mock_payments_enabled": (
                settings.DEBUG
                and getattr(settings, "DJANGO_ENV", "dev") in {"dev", "test"}
            ),
            "booking_unavailable": False,
        },
    )


# V1.2 Phase 1: five real-data, read-only management workspaces.
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from apps.identity.authorization import AuthorizationService

MANAGEMENT_CENTERS = {
    "staff": {
        "title": "员工中心", "subtitle": "员工档案、物业任职、角色授权与账号状态",
        "permission": "identity.staff_manage", "endpoint": "/api/v1/staff",
        "columns": [
            {"label": "姓名", "path": "full_name"},
            {"label": "物业工号", "path": "employee_no"},
            {"label": "岗位", "path": "position_code"},
            {"label": "联系电话", "path": "phone_masked"},
            {"label": "任职状态", "path": "membership_status"},
            {"label": "账号", "path": "account.username"},
            {"label": "生效角色", "path": "active_roles"},
        ],
    },
    "guests": {
        "title": "Guest 360", "subtitle": "客人档案、联系方式、会员关系与当前入住",
        "permission": "hotelapp.view_client", "endpoint": "/api/v1/guests",
        "columns": [
            {"label": "客人姓名", "path": "full_name"},
            {"label": "联系电话", "path": "phone_masked"},
            {"label": "电子邮箱", "path": "email"},
            {"label": "会员等级", "path": "member.level"},
            {"label": "当前房间", "path": "current_stay.room_number"},
            {"label": "数据来源", "path": "source_system"},
        ],
    },
    "rooms": {
        "title": "房态与库存中心", "subtitle": "物理客房占用、清洁、维修与可售状态",
        "permission": "hotelapp.view_room", "endpoint": "/api/v1/rooms/status",
        "columns": [
            {"label": "房间号", "path": "number"},
            {"label": "楼层", "path": "floor"},
            {"label": "房型", "path": "room_type.name"},
            {"label": "占用状态", "path": "occupancy_status"},
            {"label": "清洁状态", "path": "housekeeping_status"},
            {"label": "维修状态", "path": "maintenance_status"},
            {"label": "当前可售", "path": "is_ready"},
        ],
    },
    "memberships": {
        "title": "会员中心", "subtitle": "会员等级、有效状态与历史映射编号",
        "permission": "hotelapp.view_vip", "endpoint": "/api/v1/memberships",
        "columns": [
            {"label": "会员编号", "path": "member_no"},
            {"label": "客人", "path": "guest.full_name"},
            {"label": "会员等级", "path": "level_code"},
            {"label": "状态", "path": "status"},
            {"label": "加入日期", "path": "joined_at"},
            {"label": "有效期", "path": "expires_at"},
            {"label": "旧系统编号", "path": "legacy_vip_id"},
        ],
    },
    "reservations": {
        "title": "预订与入住中心", "subtitle": "预订、排房、在住与账单摘要（写操作仍走前台工作流）",
        "permission": "hotelapp.view_accommodation",
        "endpoint": "/api/v1/front-desk/reservations",
        "columns": [
            {"label": "预订编号", "path": "booking_no"},
            {"label": "客人", "path": "guest.full_name"},
            {"label": "状态", "path": "status_label"},
            {"label": "入住日期", "path": "check_in"},
            {"label": "离店日期", "path": "check_out"},
            {"label": "房间数", "path": "room_count"},
            {"label": "订单金额", "path": "total_amount"},
        ],
    },
}


@login_required
def management_center_page(request, center):
    """真实 PMS2 只读管理页面；数据由物业范围 API 提供，不注入静态假数据。"""
    config = MANAGEMENT_CENTERS.get(center)
    if config is None:
        from django.http import Http404
        raise Http404("管理中心不存在")
    property_obj = getattr(request, "property", None)
    if property_obj is None or not AuthorizationService.has_permission(
            request.user, property_obj, config["permission"]):
        return HttpResponseForbidden("当前岗位无权查看此管理中心。")
    return render(request, "pms/management_center.html", {
        "center": center,
        "title": config["title"],
        "subtitle": config["subtitle"],
        "api_endpoint": config["endpoint"],
        "columns": config["columns"],
        "property_name": property_obj.name,
    })
