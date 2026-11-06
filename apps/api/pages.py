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
    # 基线 2.0 §4.1：财务与账单中心（专属工作台模板；写操作走 BillingService API）
    "finance": {
        "title": "财务与账单中心", "subtitle": "账单、流水、收款、退款与班次（PMS2 真实账务口径）",
        "permission": "hotelapp.finance_access",
        "endpoint": "/api/v1/billing/folios",
        "template": "pms/billing_center.html",
        "enable_collect_payment": True,
        "columns": [
            {"label": "预订编号", "path": "booking_no"},
            {"label": "住客", "path": "guest_name"},
            {"label": "账单类型", "path": "folio_type_label"},
            {"label": "状态", "path": "status_label", "badge": "status"},
            {"label": "币种", "path": "currency"},
            {"label": "余额", "path": "balance"},
            {"label": "开账时间", "path": "opened_at"},
            {"label": "结账时间", "path": "closed_at"},
        ],
        "detail": {
            "id_path": "id", "endpoint": "/api/v1/billing/folios/{id}",
            "overview": [["booking_no", "预订编号"], ["guest_name", "住客"],
                         ["status_label", "账单状态"], ["summary.charges", "消费合计"],
                         ["summary.credits", "已收合计"], ["summary.balance", "账单余额"],
                         ["currency", "币种"], ["opened_at", "开账时间"], ["closed_at", "结账时间"]],
        },
    },
    # 基线 2.0 §4.2：营业日与夜审中心（专属工作台模板；高危写操作走
    # NightAuditService API：preview → run 两步确认、重开双人审批、break-glass 复核）。
    # 键名与 URL 段一致（/manage/night-audit/）。
    "night-audit": {
        "title": "营业日与夜审中心",
        "subtitle": "营业日历、夜审预览与执行、重开审批、紧急重开（break-glass）复核",
        "permission": "core.night_audit_view",
        "endpoint": "/api/v1/night-audit/business-dates",
        "template": "pms/night_audit_center.html",
        "enable_night_audit_actions": True,
        "columns": [
            {"label": "营业日期", "path": "business_date"},
            {"label": "状态", "path": "status_label", "badge": "status"},
            {"label": "最近夜审", "path": "latest_run.status_label"},
            {"label": "关账时间", "path": "closed_at"},
            {"label": "关账人", "path": "closed_by_id"},
            {"label": "重开原因", "path": "reopen_reason"},
        ],
        "detail": {
            "id_path": "id", "endpoint": "/api/v1/night-audit/business-dates/{id}",
            "overview": [["business_date", "营业日期"], ["status_label", "状态"],
                         ["opened_at", "开启时间"], ["closed_at", "关账时间"],
                         ["no_show_review_candidates", "未到处理候选"],
                         ["reopen_reason", "重开原因"]],
        },
    },
    # 基线 2.0 §4.4：审计与合规中心（专属工作台模板；只读，唯一读取门禁
    # core.audit_view——P0-04 定版，migration core.0010 已授予店长/值班经理。
    # 列表端点不下发 before/after 载荷，明细在详情抽屉按同一权限展示）。
    # 键名与 URL 段一致（/manage/audit/）。
    "audit": {
        "title": "审计与合规中心",
        "subtitle": "审计事件检索、事件详情（before/after）与高危动作统计（只读）",
        "permission": "core.audit_view",
        "endpoint": "/api/v1/audit/events",
        "template": "pms/audit_center.html",
        "columns": [
            {"label": "时间", "path": "created_at"},
            {"label": "动作", "path": "action"},
            {"label": "实体", "path": "entity_type"},
            {"label": "实体ID", "path": "entity_id"},
            {"label": "操作人", "path": "actor"},
            {"label": "原因", "path": "reason"},
            {"label": "敏感", "path": "is_sensitive"},
        ],
    },
    # 基线 2.0 §4.3：清洁与维修工作台（专属工作台模板）。挂在客房运营
    # （rooms 模块）权限面：读=view_room；清洁链流转=change_room；
    # 净房放行=room_release_ready（F-08 补执行点）；维修登记=no_show_handle。
    # 键名与 URL 段一致（/manage/housekeeping/）。
    "housekeeping": {
        "title": "清洁与维修工作台",
        "subtitle": "清洁链流转、净房放行、维修停用登记与今日清洁统计",
        "permission": "hotelapp.view_room",
        "endpoint": "/api/v1/housekeeping/rooms",
        "template": "pms/housekeeping_center.html",
        "enable_housekeeping_actions": True,
        "columns": [
            {"label": "房间号", "path": "number"},
            {"label": "楼层", "path": "floor"},
            {"label": "房型", "path": "room_type.name"},
            {"label": "占用状态", "path": "occupancy_label"},
            {"label": "清洁状态", "path": "housekeeping_label", "badge": "status"},
            {"label": "维修状态", "path": "maintenance_label"},
            {"label": "当前可售", "path": "is_sellable_now"},
        ],
    },
    # 基线 2.0 §4.5：报表分析中心（专属工作台模板；纯只读聚合，不作为第二账本）。
    # 页面门禁取报表中心最高敏感档 hotelapp.finance_access（含收入/ADR 报表）；
    # 各报表 API 按自身敏感度逐请求复核：收入/ADR=finance_access，
    # 入住率/客源/清洁=view_accommodation（口径见 apps/api/report_center.py 模块注）。
    # 键名与 URL 段一致（/manage/reports/）。
    "reports": {
        "title": "报表分析中心",
        "subtitle": "入住率、ADR、收入、客源与清洁效率（只读聚合，含统计口径说明）",
        "permission": "hotelapp.finance_access",
        "endpoint": "/api/v1/reports/occupancy",
        "template": "pms/report_center.html",
        "columns": [],
    },
    # 基线 2.0 §4.6：通知与待办中心（专属工作台模板）。
    # 页面门禁=认证 + 当前物业有效任职（permission 留空 → management_center_page
    # 按任职判定）：§4.6 口径"所有员工都能收通知"，不设专项权限码。
    # 键名与 URL 段一致（/manage/notifications/）。
    "notifications": {
        "title": "通知与待办中心",
        "subtitle": "需要我处理的事项：待确认预订 / 今日抵离 / 待清洁放行 / 待审批 / 过期未到",
        "permission": "",
        "endpoint": "/api/v1/notifications",
        "template": "pms/notification_center.html",
        "columns": [
            {"label": "时间", "path": "created_at"},
            {"label": "类别", "path": "category_label"},
            {"label": "级别", "path": "severity_label"},
            {"label": "标题", "path": "title"},
            {"label": "已读", "path": "is_read"},
        ],
    },
}


@login_required
def management_center_page(request, center):
    """真实 PMS2 只读管理页面；数据由物业范围 API 提供，不注入静态假数据。

    配置可声明 ``template`` 选择专属工作台模板（如 §4.1 财务中心的
    pms/billing_center.html）；默认仍渲染通用注册表模板。
    """
    config = MANAGEMENT_CENTERS.get(center)
    if config is None:
        from django.http import Http404
        raise Http404("管理中心不存在")
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        return HttpResponseForbidden("当前岗位无权查看此管理中心。")
    # §4.6 通知与待办中心：permission 留空 → 仅要求认证 + 当前物业有效任职
    #（§4.6 规则"所有员工都能收通知"，不设专项权限码）；其余中心维持原门禁。
    required_permission = config.get("permission")
    if required_permission:
        allowed = AuthorizationService.has_permission(
            request.user, property_obj, required_permission)
    else:
        allowed = (AuthorizationService.get_active_membership(
            request.user, property_obj) is not None)
    if not allowed:
        return HttpResponseForbidden("当前岗位无权查看此管理中心。")
    # 写操作入口可见性（服务端仍逐请求复核专项权限，页面标志仅控制表单渲染）
    user = request.user
    can_refund_request = AuthorizationService.has_permission(
        user, property_obj, "billing.refund_request")
    can_reverse = AuthorizationService.has_permission(
        user, property_obj, "billing.reverse_entry")
    enable_night_audit = config.get("enable_night_audit_actions", False)
    can_reopen = enable_night_audit and AuthorizationService.has_permission(
        user, property_obj, "hotelapp.business_date_reopen")
    can_review_break_glass = (
        enable_night_audit and AuthorizationService.has_permission(
            user, property_obj, "core.breakglass_review"))
    # §4.3 清洁与维修工作台：按钮显隐标志（服务端仍逐请求复核同一权限码）
    enable_housekeeping = config.get("enable_housekeeping_actions", False)
    can_flow_housekeeping = (
        enable_housekeeping and AuthorizationService.has_permission(
            user, property_obj, "hotelapp.change_room"))
    can_release_room = (
        enable_housekeeping and AuthorizationService.has_permission(
            user, property_obj, "hotelapp.room_release_ready"))
    can_register_maintenance = (
        enable_housekeeping and AuthorizationService.has_permission(
            user, property_obj, "hotelapp.no_show_handle"))
    return render(request, config.get("template", "pms/management_center.html"), {
        "center": center,
        "title": config["title"],
        "subtitle": config["subtitle"],
        "api_endpoint": config["endpoint"],
        "columns": config["columns"],
        "secondary_endpoint": config.get("secondary_endpoint", ""),
        "secondary_sections": config.get("secondary_sections", []),
        "detail_config": config.get("detail", {}),
        "property_name": property_obj.name,
        "can_collect_payment": config.get("enable_collect_payment", False),
        "can_refund_request": can_refund_request,
        "can_reverse_entry": can_reverse,
        "can_run_night_audit": (
            enable_night_audit and AuthorizationService.has_permission(
                user, property_obj, "hotelapp.night_audit_execute")),
        # 页面读取权限与夜审执行权限分离；按钮仅对执行授权者展示。
        "can_reopen_business_date": can_reopen,
        "can_review_break_glass": can_review_break_glass,
        "can_flow_housekeeping": can_flow_housekeeping,
        "can_release_room": can_release_room,
        "can_register_maintenance": can_register_maintenance,
    })
