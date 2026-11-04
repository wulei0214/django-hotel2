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
    response = render(
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
    # 页面内联 JS 与后端接口同版本迭代；禁止启发式缓存，
    # 避免浏览器拿旧脚本调新接口导致"查询无反应"
    response["Cache-Control"] = "no-cache"
    return response


# V1.2 Phase 1: five real-data, read-only management workspaces.
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from apps.identity.authorization import AuthorizationService

MANAGEMENT_CENTERS = {
    # §10.1：staff 中心的通用注册表页已被员工中心专属工作台
    # （apps/identity/staff_center_views.py + templates/pms/staff_center.html）取代，
    # /manage/staff/ 路由优先命中专属视图；本条目仅保留读取契约（列/端点）备查。
    "staff": {
        "title": "员工中心", "subtitle": "员工档案、物业任职、角色授权与账号状态",
        "permission": "identity.staff_manage", "endpoint": "/api/v1/staff",
        "columns": [
            {"label": "姓名", "path": "full_name"},
            {"label": "物业工号", "path": "employee_no"},
            {"label": "岗位", "path": "position_code"},
            {"label": "联系电话", "path": "phone_masked"},
            {"label": "任职状态", "path": "membership_status_label"},
            {"label": "账号", "path": "account.username"},
            {"label": "生效角色", "path": "active_roles"},
        ],
        "detail": {
            "id_path": "id", "endpoint": "/api/v1/staff/{id}",
            "overview": [["full_name", "姓名"], ["employee_no", "物业工号"], ["position_code", "岗位"],
                         ["phone_masked", "联系电话"], ["employment_status_label", "雇用状态"],
                         ["membership_status_label", "任职状态"], ["account.username", "登录账号"],
                         ["account.is_active", "账号启用"], ["account.last_login", "最后登录"],
                         ["active_roles", "生效角色"], ["source_system", "数据来源"]],
            "tables": [
                {"title": "岗位授权记录", "key": "assignments", "columns": [
                    ["role", "角色"], ["active", "生效"], ["starts_at", "开始"], ["ends_at", "结束"]]},
            ],
        },
    },
    "guests": {
        "title": "Guest 360", "subtitle": "客人档案、联系方式、会员关系与当前入住",
        "permission": "hotelapp.view_client", "endpoint": "/api/v1/guests",
        "columns": [
            {"label": "客人姓名", "path": "full_name"},
            {"label": "联系电话", "path": "phone_masked"},
            {"label": "电子邮箱", "path": "email"},
            {"label": "会员等级", "path": "member.level_label"},
            {"label": "当前房间", "path": "current_stay.room_number"},
            {"label": "数据来源", "path": "source_system"},
        ],
        "detail": {
            "id_path": "id", "endpoint": "/api/v1/guests/{id}",
            "overview": [["full_name", "客人姓名"], ["phone_masked", "联系电话"], ["email", "电子邮箱"],
                         ["member.member_no", "会员编号"], ["member.level_label", "会员等级"],
                         ["member.status_label", "会员状态"], ["current_stay.room_number", "当前房间"],
                         ["current_stay.check_in", "入住日期"], ["source_system", "数据来源"],
                         ["created_at", "建档时间"]],
            "tables": [
                {"title": "预订记录（最近 10 条）", "key": "reservations", "columns": [
                    ["booking_no", "预订编号"], ["status_label", "状态"], ["check_in", "入住"],
                    ["check_out", "离店"], ["rooms", "间数"], ["total_amount", "金额"]]},
            ],
        },
    },
    "rooms": {
        "title": "房态与库存中心", "subtitle": "物理客房占用、清洁、维修与可售状态",
        "permission": "hotelapp.view_room", "endpoint": "/api/v1/rooms/status",
        "secondary_endpoint": "/api/v1/rate-calendar",
        "secondary_sections": [
            {"title": "每日房价", "data_key": "rates", "columns": [
                {"label": "住宿日期", "path": "stay_date"},
                {"label": "房型", "path": "room_type.name"},
                {"label": "房价计划", "path": "rate_plan.name"},
                {"label": "每晚价格", "path": "price"},
                {"label": "币种", "path": "currency_label"},
                {"label": "是否关房", "path": "closed"},
            ]},
            {"title": "房型每日库存", "data_key": "inventory", "columns": [
                {"label": "住宿日期", "path": "stay_date"},
                {"label": "房型", "path": "room_type.name"},
                {"label": "总房量", "path": "total_rooms"},
                {"label": "冻结", "path": "held"},
                {"label": "已售", "path": "sold"},
                {"label": "停用", "path": "out_of_service"},
                {"label": "可售", "path": "available_count"},
                {"label": "停售", "path": "stop_sell"},
            ]},
        ],
        "columns": [
            {"label": "房间号", "path": "number"},
            {"label": "楼层", "path": "floor"},
            {"label": "房型", "path": "room_type.name"},
            {"label": "占用状态", "path": "occupancy_label"},
            {"label": "清洁状态", "path": "housekeeping_label"},
            {"label": "维修状态", "path": "maintenance_label"},
            {"label": "当前可售", "path": "is_ready"},
        ],
    },
    "memberships": {
        "title": "会员中心", "subtitle": "会员等级、有效状态与历史映射编号",
        "permission": "hotelapp.view_vip", "endpoint": "/api/v1/memberships",
        "columns": [
            {"label": "会员编号", "path": "member_no"},
            {"label": "客人", "path": "guest.full_name"},
            {"label": "会员等级", "path": "level_label"},
            {"label": "状态", "path": "status_label"},
            {"label": "加入日期", "path": "joined_at"},
            {"label": "有效期", "path": "expires_at"},
            {"label": "旧系统编号", "path": "legacy_vip_id"},
        ],
        "detail": {
            "id_path": "id", "endpoint": "/api/v1/memberships/{id}",
            "overview": [["member_no", "会员编号"], ["guest.full_name", "关联客人"], ["level_label", "会员等级"],
                         ["status_label", "会员状态"], ["joined_at", "加入日期"], ["expires_at", "有效期"],
                         ["legacy_vip_id", "旧系统编号"], ["source_system", "数据来源"]],
            "tables": [
                {"title": "等级变更历史", "key": "level_history", "columns": [
                    ["from_level", "原等级"], ["to_level", "新等级"],
                    ["reason", "原因"], ["changed_at", "变更时间"]]},
            ],
        },
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
        "detail": {
            "id_path": "booking_no", "endpoint": "/api/v1/front-desk/reservations/{id}",
            "overview": [["booking_no", "预订编号"], ["status_label", "预订状态"], ["source_label", "预订来源"],
                         ["guest.full_name", "客人"], ["guest.phone_masked", "联系电话"],
                         ["check_in", "入住日期"], ["check_out", "离店日期"], ["nights", "晚数"],
                         ["room_count", "房间数"], ["adult_count", "成人"], ["child_count", "儿童"],
                         ["total_amount", "订单金额"], ["currency_label", "币种"],
                         ["folio_summary.status_label", "账单状态"], ["folio_summary.charges", "账单消费"],
                         ["folio_summary.credits", "账单已收"], ["folio_summary.balance", "账单余额"]],
            "tables": [
                {"title": "房间明细", "key": "rooms", "columns": [
                    ["room_type.name", "房型"], ["assigned_room.number", "分配房间"],
                    ["check_in", "入住"], ["check_out", "离店"], ["nights", "晚数"],
                    ["amount", "金额"], ["stay.status_label", "入住状态"]]},
                {"title": "入住记录（Stay）", "key": "timeline.stays", "columns": [
                    ["id", "Stay"], ["status_label", "状态"],
                    ["actual_check_in", "实际入住"], ["actual_check_out", "实际退房"]]},
            ],
        },
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
        "secondary_endpoint": config.get("secondary_endpoint", ""),
        "secondary_sections": config.get("secondary_sections", []),
        "detail_config": config.get("detail", {}),
        "property_name": property_obj.name,
    })
