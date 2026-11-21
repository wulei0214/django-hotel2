from django.urls import path

from . import (
    approval,
    audit_center,
    audit_governance,
    billing_center,
    front_desk,
    guest_center,
    guest_subresources,
    housekeeping_center,
    housekeeping_workflow_center,
    inventory_admin_center,
    night_audit,
    notification_center,
    report_center,
    report_export,
    reservation_center,
    staff_center,
    task_center,
    views,
)

app_name = "api"

urlpatterns = [
    path("night-audit/preview", night_audit.night_audit_preview, name="night_audit_preview"),
    path(
        "night-audit/blockers",
        night_audit.night_audit_blockers,
        name="night_audit_blockers",
    ),
    path("night-audit/run", night_audit.night_audit_run, name="night_audit_run"),
    # §4.2 营业日中心只读端点：列表/当前/详情 + 夜审历史 + break-glass 列表
    path(
        "night-audit/business-dates",
        night_audit.night_audit_business_dates,
        name="night_audit_business_dates",
    ),
    path(
        "night-audit/business-dates/current",
        night_audit.night_audit_business_date_current,
        name="night_audit_business_date_current",
    ),
    path(
        "night-audit/business-dates/<int:business_date_id>",
        night_audit.night_audit_business_date_detail,
        name="night_audit_business_date_detail",
    ),
    path(
        "night-audit/break-glass",
        night_audit.night_audit_break_glass_list,
        name="night_audit_break_glass_list",
    ),
    path("night-audit/runs", night_audit.night_audit_runs, name="night_audit_runs"),
    path(
        "night-audit/runs/<int:run_id>",
        night_audit.night_audit_run_detail,
        name="night_audit_run_detail",
    ),
    path(
        "night-audit/business-dates/<int:business_date_id>/reopen-request",
        night_audit.night_audit_reopen_request,
        name="night_audit_reopen_request",
    ),
    path(
        "night-audit/business-dates/<int:business_date_id>/approvals/<int:approval_id>/approve",
        night_audit.night_audit_reopen_approve,
        name="night_audit_reopen_approve",
    ),
    path(
        "night-audit/business-dates/<int:business_date_id>/break-glass",
        night_audit.night_audit_break_glass_request,
        name="night_audit_break_glass_request",
    ),
    path(
        "night-audit/business-dates/<int:business_date_id>/break-glass/<int:record_id>/execute",
        night_audit.night_audit_break_glass_execute,
        name="night_audit_break_glass_execute",
    ),
    path(
        "night-audit/break-glass/<int:record_id>/review",
        night_audit.night_audit_break_glass_review,
        name="night_audit_break_glass_review",
    ),
    path("front-desk/overview", front_desk.overview, name="front_desk_overview"),
    path("front-desk/arrivals", front_desk.arrivals, name="front_desk_arrivals"),
    path("front-desk/departures", front_desk.departures, name="front_desk_departures"),
    path("front-desk/in-house", front_desk.in_house, name="front_desk_in_house"),
    path("front-desk/reservations", front_desk.reservations, name="front_desk_reservations"),
    path(
        "front-desk/reservations/<str:booking_no>",
        front_desk.reservation_detail,
        name="front_desk_reservation_detail",
    ),
    path("front-desk/rooms", front_desk.rooms, name="front_desk_rooms"),
    path(
        "front-desk/rooms/assignable",
        front_desk.assignable_rooms,
        name="front_desk_assignable_rooms",
    ),
    path(
        "front-desk/folios/<str:booking_no>",
        front_desk.folio_detail,
        name="front_desk_folio_detail",
    ),
    path(
        "front-desk/legacy-reservations",
        front_desk.legacy_reservations,
        name="front_desk_legacy_reservations",
    ),
    path(
        "front-desk/legacy-reservations/<int:legacy_id>",
        front_desk.legacy_reservation_detail,
        name="front_desk_legacy_reservation_detail",
    ),
    path(
        "front-desk/stays/<int:stay_id>/available-rooms",
        front_desk.available_stay_rooms,
        name="front_desk_available_stay_rooms",
    ),
    path(
        "front-desk/stays/<int:stay_id>/extend",
        front_desk.extend_stay_api,
        name="front_desk_extend_stay",
    ),
    path(
        "front-desk/stays/<int:stay_id>/change-room",
        front_desk.change_stay_room_api,
        name="front_desk_change_stay_room",
    ),
    path(
        "front-desk/reservation-rooms/<int:reservation_room_id>/no-show",
        front_desk.mark_no_show_api,
        name="front_desk_no_show",
    ),
    path(
        "front-desk/reservations/<str:booking_no>/deposits",
        front_desk.receive_deposit_api,
        name="front_desk_receive_deposit",
    ),
    path(
        "front-desk/payments/<int:payment_id>/refunds",
        front_desk.request_refund_api,
        name="front_desk_request_refund",
    ),
    path(
        "front-desk/refunds/<int:refund_id>/approve",
        front_desk.approve_refund_api,
        name="front_desk_approve_refund",
    ),
    path(
        "front-desk/refunds/<int:refund_id>/reject",
        front_desk.reject_refund_api,
        name="front_desk_reject_refund",
    ),
    path(
        "front-desk/reservation-rooms/<int:reservation_room_id>/assign",
        front_desk.assign_room,
        name="front_desk_assign_room",
    ),
    # TASK-03 day-use 转过夜（DU-11 服务层唯一事实源；重复提交 409 拒绝）
    path(
        "front-desk/reservation-rooms/<int:reservation_room_id>/convert-to-overnight",
        front_desk.day_use_convert_to_overnight,
        name="front_desk_day_use_convert_to_overnight",
    ),
    path(
        "front-desk/reservation-rooms/<int:reservation_room_id>/check-in",
        front_desk.check_in,
        name="front_desk_check_in",
    ),
    path(
        "front-desk/reservations/<str:booking_no>/payments",
        front_desk.collect_payment,
        name="front_desk_collect_payment",
    ),
    path(
        "front-desk/stays/<int:stay_id>/check-out",
        front_desk.check_out,
        name="front_desk_check_out",
    ),
    path("front-desk/walk-in", front_desk.walk_in_create, name="front_desk_walk_in"),
    # A1 散客直入（契约口径：业务冲突 409、幂等重放 200+created=false、含服务端计押金）
    path("front-desk/walk-ins", front_desk.walk_ins_create, name="front_desk_walk_ins_create"),
    # TASK-02 散客建单表单选项（房型/价格计划/日租政策价，只读）
    path(
        "front-desk/walk-in-options",
        front_desk.walk_in_options,
        name="front_desk_walk_in_options",
    ),
    path("availability", views.availability, name="availability"),
    path("reservations", views.reservations_create, name="reservations_create"),
    path("reservations/<str:booking_no>", views.reservation_detail, name="reservation_detail"),
    path(
        "reservations/<str:booking_no>/cancel", views.reservation_cancel, name="reservation_cancel"
    ),
    path("payments/intents", views.payment_intent_create, name="payment_intent_create"),
    path("payments/callback", views.payment_callback, name="payment_callback"),
    path("openapi.json", views.openapi_document, name="openapi"),
    # --- Guest 360 (V1.1 Phase 1 只读原型) ---
    path("guests", guest_center.guest_list, name="guest_list"),
    path("guests/<int:guest_id>", guest_center.guest_detail, name="guest_detail"),
    path("guests/create", guest_center.guest_create, name="guest_create"),
    path("guests/<int:guest_id>/update", guest_center.guest_update, name="guest_update"),
    # A3 客人合并（排期 V2.0 第二主线）：duplicate 软归档入 primary
    path(
        "guest-center/guests/<int:guest_id>/merge",
        guest_center.guest_merge,
        name="guest_merge",
    ),
    # 基线 2.0 §3.2：Guest 360 时间线与账单摘要（只读聚合）
    path("guests/<int:guest_id>/timeline", guest_center.guest_timeline, name="guest_timeline"),
    path("guests/<int:guest_id>/folios", guest_center.guest_folios, name="guest_folios"),
    path(
        "guests/<int:guest_id>/documents/<int:document_id>/reveal",
        guest_center.guest_reveal_document,
        name="guest_reveal_document",
    ),
    path(
        "guests/<int:guest_id>/phones/<int:phone_id>/reveal",
        guest_center.guest_reveal_phone,
        name="guest_reveal_phone",
    ),
    # MC-003：电话/证件子资源生命周期（全部复用 GuestService）
    path(
        "guests/<int:guest_id>/phones", guest_subresources.phone_create, name="guest_phone_create"
    ),
    path(
        "guests/<int:guest_id>/phones/<int:phone_id>/primary",
        guest_subresources.phone_set_primary,
        name="guest_phone_set_primary",
    ),
    path(
        "guests/<int:guest_id>/phones/<int:phone_id>/verify",
        guest_subresources.phone_verify,
        name="guest_phone_verify",
    ),
    path(
        "guests/<int:guest_id>/documents",
        guest_subresources.document_create,
        name="guest_document_create",
    ),
    path(
        "guests/<int:guest_id>/documents/<int:document_id>/update",
        guest_subresources.document_update,
        name="guest_document_update",
    ),
    path(
        "guests/<int:guest_id>/documents/<int:document_id>/verify",
        guest_subresources.document_verify,
        name="guest_document_verify",
    ),
    path(
        "guests/<int:guest_id>/documents/<int:document_id>/invalidate",
        guest_subresources.document_invalidate,
        name="guest_document_invalidate",
    ),
    # --- Room 中心 (V1.1 Phase 1 只读原型) ---
    path("rooms/status", guest_center.room_status_list, name="room_status_list"),
    path("room-types", guest_center.room_type_list, name="room_type_list"),
    path("rate-calendar", guest_center.rate_calendar, name="rate_calendar"),
    path("financial-summary", guest_center.financial_summary_api, name="financial_summary"),
    # MC-004：库存中心受控写工作台（主数据/关房/停售/差异修复）
    path(
        "inventory/daily-rates/<int:rate_id>/close",
        inventory_admin_center.daily_rate_close,
        name="inventory_rate_close",
    ),
    path(
        "inventory/stock/<int:inventory_id>/stop-sell",
        inventory_admin_center.stock_stop_sell,
        name="inventory_stock_stop_sell",
    ),
    path(
        "inventory/room-types/create",
        inventory_admin_center.room_type_create,
        name="inventory_room_type_create",
    ),
    path(
        "inventory/room-types/<int:room_type_id>/update",
        inventory_admin_center.room_type_update,
        name="inventory_room_type_update",
    ),
    path(
        "inventory/rooms/create",
        inventory_admin_center.room_create,
        name="inventory_room_create",
    ),
    path(
        "inventory/rooms/<int:room_id>/update",
        inventory_admin_center.room_update,
        name="inventory_room_update",
    ),
    path(
        "inventory/discrepancies",
        inventory_admin_center.discrepancy_list,
        name="inventory_discrepancy_list",
    ),
    path(
        "inventory/assignment-conflicts",
        inventory_admin_center.assignment_conflict_list,
        name="inventory_assignment_conflicts",
    ),
    path(
        "inventory/discrepancies/scan",
        inventory_admin_center.discrepancy_scan,
        name="inventory_discrepancy_scan",
    ),
    path(
        "inventory/discrepancies/<int:discrepancy_id>/fix-request",
        inventory_admin_center.discrepancy_fix_request,
        name="inventory_discrepancy_fix_request",
    ),
    path(
        "inventory/discrepancies/<int:discrepancy_id>/approve",
        inventory_admin_center.discrepancy_approve,
        name="inventory_discrepancy_approve",
    ),
    path(
        "inventory/discrepancies/<int:discrepancy_id>/execute",
        inventory_admin_center.discrepancy_execute,
        name="inventory_discrepancy_execute",
    ),
    path(
        "inventory/discrepancies/<int:discrepancy_id>/dismiss",
        inventory_admin_center.discrepancy_dismiss,
        name="inventory_discrepancy_dismiss",
    ),
    # --- ApprovalRecord (V1.2 Q-05/Q-06/Q-14) ---
    path("approvals", approval.approval_list, name="approval_list"),
    path("approvals/request", approval.approval_request, name="approval_request"),
    path("approvals/<int:record_id>/approve", approval.approval_approve, name="approval_approve"),
    path("approvals/<int:record_id>/reject", approval.approval_reject, name="approval_reject"),
    # --- Reservation 管理 (V1.1 Phase 1) ---
    path("reservations-mgmt", reservation_center.reservation_list, name="reservation_mgmt_list"),
    path(
        "reservations-mgmt/<str:booking_no>",
        reservation_center.reservation_detail_api,
        name="reservation_mgmt_detail",
    ),
    # --- AuditEvent 时间线 (P1-14) ---
    path("audit-timeline", reservation_center.audit_timeline, name="audit_timeline"),
    # --- 审计与合规中心（基线 2.0 §4.4；读=core.audit_view 唯一门禁，
    #     与 audit-timeline 并存同口径；列表不下发 before/after 载荷） ---
    path("audit/events", audit_center.audit_events, name="audit_events"),
    path("audit/events/<int:event_id>", audit_center.audit_event_detail, name="audit_event_detail"),
    path("audit/summary", audit_center.audit_summary, name="audit_summary"),
    path("audit/actions", audit_center.audit_actions, name="audit_actions"),
    # MC-008：受控导出、留存状态与只读完整性摘要
    path("audit/export", audit_governance.audit_export, name="audit_export"),
    path(
        "audit/export/<str:token>",
        audit_governance.audit_export_download,
        name="audit_export_download",
    ),
    path(
        "audit/retention/status",
        audit_governance.audit_retention_status,
        name="audit_retention_status",
    ),
    path(
        "audit/integrity/check",
        audit_governance.audit_integrity_check,
        name="audit_integrity_check",
    ),
    path(
        "daily-rates/<int:rate_id>/adjust",
        guest_center.adjust_daily_rate_api,
        name="adjust_daily_rate",
    ),
    # --- Staff 中心 (V1.1 Phase 1) ---
    path("staff", staff_center.staff_list, name="staff_list"),
    path("staff/<int:profile_id>", staff_center.staff_detail, name="staff_detail"),
    # --- Membership (V1.1 Phase 1) ---
    path("memberships", staff_center.membership_list, name="membership_list"),
    path(
        "memberships/<int:membership_id>", staff_center.membership_detail, name="membership_detail"
    ),
    path("memberships/create", staff_center.membership_create, name="membership_create"),
    path(
        "memberships/<int:membership_id>/level",
        staff_center.membership_change_level,
        name="membership_change_level",
    ),
    # 基线 2.0 §3.4：会员暂停/恢复（action=suspend|restore）
    path(
        "memberships/<int:membership_id>/status",
        staff_center.membership_change_status,
        name="membership_change_status",
    ),
    # --- 财务与账单中心 (基线 2.0 §4.1；读=finance_access，写走 BillingService) ---
    path("billing/folios", billing_center.folio_list, name="billing_folio_list"),
    path("billing/folios/<int:folio_id>", billing_center.folio_detail, name="billing_folio_detail"),
    path("billing/ledger", billing_center.ledger_entry_list, name="billing_ledger"),
    path("billing/payments", billing_center.billing_payments, name="billing_payments"),
    path("billing/refunds", billing_center.billing_refunds, name="billing_refunds"),
    path(
        "billing/refund-liabilities",
        billing_center.refund_liability_list,
        name="billing_refund_liabilities",
    ),
    # DEC-02-A1：账务调整 / 抵扣（财务发起、值班经理审批、执行生凭证）
    path("billing/adjustments", billing_center.adjustment_list, name="billing_adjustments"),
    path(
        "billing/adjustments/request",
        billing_center.adjustment_request,
        name="billing_adjustment_request",
    ),
    path(
        "billing/adjustments/<int:adjustment_id>/approve",
        billing_center.adjustment_approve,
        name="billing_adjustment_approve",
    ),
    path(
        "billing/adjustments/<int:adjustment_id>/reject",
        billing_center.adjustment_reject,
        name="billing_adjustment_reject",
    ),
    path(
        "billing/adjustments/<int:adjustment_id>/execute",
        billing_center.adjustment_execute,
        name="billing_adjustment_execute",
    ),
    # DEC-02-B2：退房异常放行与待财务核查
    path(
        "billing/exception-releases",
        billing_center.exception_release_list,
        name="billing_exception_releases",
    ),
    path(
        "billing/exception-releases/<int:release_id>/approve",
        billing_center.exception_release_approve,
        name="billing_exception_approve",
    ),
    path(
        "billing/exception-releases/<int:release_id>/reject",
        billing_center.exception_release_reject,
        name="billing_exception_reject",
    ),
    path(
        "billing/exception-releases/<int:release_id>/settle",
        billing_center.exception_release_settle,
        name="billing_exception_settle",
    ),
    path(
        "front-desk/folios/<str:booking_no>/exception-release-request",
        front_desk.exception_release_request,
        name="front_desk_exception_request",
    ),
    path(
        "billing/refunds/<int:refund_id>/approve",
        billing_center.approve_refund_api,
        name="billing_refund_approve",
    ),
    path(
        "billing/refunds/<int:refund_id>/reject",
        billing_center.reject_refund_api,
        name="billing_refund_reject",
    ),
    path(
        "billing/refunds/<int:refund_id>/execute",
        billing_center.execute_refund_api,
        name="billing_refund_execute",
    ),
    path("billing/reversals", billing_center.reverse_entry_api, name="billing_reverse_entry"),
    path("billing/shifts", billing_center.shift_list, name="billing_shift_list"),
    # 基线 2.0 §3.5：收银班次开班/关班（BillingService.open_shift/close_shift）
    path("billing/shifts/open", billing_center.open_shift_api, name="billing_shift_open"),
    path(
        "billing/shifts/<int:shift_id>/close",
        billing_center.close_shift_api,
        name="billing_shift_close",
    ),
    path(
        "billing/shifts/<int:shift_id>/movements",
        billing_center.shift_cash_movement_api,
        name="billing_shift_cash_movement",
    ),
    path("billing/summary", billing_center.billing_summary, name="billing_summary"),
    # 归档只读查询通道（归档门禁 §4.1：超管 + finance_access；脱敏 + 访问审计）
    path(
        "billing/legacy-archives",
        billing_center.legacy_archive_batch_list,
        name="billing_legacy_archive_batches",
    ),
    path(
        "billing/legacy-archives/<str:table_key>",
        billing_center.legacy_archive_row_list,
        name="billing_legacy_archive_rows",
    ),
    # A2 支付窗口延长（D2 服务端策略：max_extensions=1；财务域 finance_access 门禁）
    path(
        "billing/payment-intents/<int:intent_id>/window-extension",
        front_desk.payment_intent_window_extension,
        name="billing_payment_intent_window_extension",
    ),
    # --- 清洁与维修工作台（读/清洁/维修使用独立权限；净房放行叠加高危专项权限） ---
    path("housekeeping/rooms", housekeeping_center.housekeeping_rooms, name="housekeeping_rooms"),
    path(
        "housekeeping/summary",
        housekeeping_center.housekeeping_summary_api,
        name="housekeeping_summary",
    ),
    path(
        "housekeeping/rooms/<int:room_id>/status",
        housekeeping_center.housekeeping_status_change,
        name="housekeeping_status_change",
    ),
    path(
        "housekeeping/rooms/<int:room_id>/maintenance",
        housekeeping_center.housekeeping_maintenance_change,
        name="housekeeping_maintenance_change",
    ),
    # MC-006：任务级接单/派工/开始/完成/检查 + 维修工单全生命周期
    path("housekeeping/tasks", housekeeping_workflow_center.task_list, name="housekeeping_tasks"),
    path(
        "housekeeping/tasks/<int:task_id>",
        housekeeping_workflow_center.task_detail,
        name="housekeeping_task_detail",
    ),
    path(
        "housekeeping/tasks/<int:task_id>/claim",
        housekeeping_workflow_center.task_claim,
        name="housekeeping_task_claim",
    ),
    path(
        "housekeeping/tasks/<int:task_id>/assign",
        housekeeping_workflow_center.task_assign,
        name="housekeeping_task_assign",
    ),
    path(
        "housekeeping/tasks/<int:task_id>/start",
        housekeeping_workflow_center.task_start,
        name="housekeeping_task_start",
    ),
    path(
        "housekeeping/tasks/<int:task_id>/complete",
        housekeeping_workflow_center.task_complete,
        name="housekeeping_task_complete",
    ),
    path(
        "housekeeping/tasks/<int:task_id>/inspect",
        housekeeping_workflow_center.task_inspect,
        name="housekeeping_task_inspect",
    ),
    path(
        "housekeeping/tasks/<int:task_id>/cancel",
        housekeeping_workflow_center.task_cancel,
        name="housekeeping_task_cancel",
    ),
    path(
        "maintenance/tickets",
        housekeeping_workflow_center.ticket_list,
        name="maintenance_tickets",
    ),
    path(
        "maintenance/tickets/<int:ticket_id>",
        housekeeping_workflow_center.ticket_detail,
        name="maintenance_ticket_detail",
    ),
    path(
        "maintenance/tickets/create",
        housekeeping_workflow_center.ticket_create,
        name="maintenance_ticket_create",
    ),
    path(
        "maintenance/tickets/<int:ticket_id>/assign",
        housekeeping_workflow_center.ticket_assign,
        name="maintenance_ticket_assign",
    ),
    path(
        "maintenance/tickets/<int:ticket_id>/start",
        housekeeping_workflow_center.ticket_start,
        name="maintenance_ticket_start",
    ),
    path(
        "maintenance/tickets/<int:ticket_id>/resolve",
        housekeeping_workflow_center.ticket_resolve,
        name="maintenance_ticket_resolve",
    ),
    path(
        "maintenance/tickets/<int:ticket_id>/verify",
        housekeeping_workflow_center.ticket_verify,
        name="maintenance_ticket_verify",
    ),
    path(
        "maintenance/tickets/<int:ticket_id>/cancel",
        housekeeping_workflow_center.ticket_cancel,
        name="maintenance_ticket_cancel",
    ),
    # --- 报表分析中心 (基线 2.0 §4.5；只读聚合。收入/ADR=finance_access，
    #     入住率/客源/清洁=view_accommodation；期间上限 92 天) ---
    path("reports/occupancy", report_center.occupancy_report, name="report_occupancy"),
    path("reports/adr", report_center.adr_report, name="report_adr"),
    path("reports/revenue", report_center.revenue_report, name="report_revenue"),
    path("reports/guests", report_center.guests_report, name="report_guests"),
    path("reports/housekeeping", report_center.housekeeping_report, name="report_housekeeping"),
    # 第 3 批快照型报表（A4/A5：无 from/to 区间，固定近 30 天趋势窗口；
    # 会员=GuestMembership 口径、渠道来源=Reservation.source 口径，
    # 均挂运营类门禁 hotelapp.view_accommodation）
    path("reports/memberships", report_center.memberships_report, name="report_memberships"),
    path("reports/sources", report_center.sources_report, name="report_sources"),
    path("reports/export", report_export.report_export, name="report_export"),
    path(
        "reports/export/<str:token>",
        report_export.report_export_download,
        name="report_export_download",
    ),
    # --- 通知与待办中心 (基线 2.0 §4.6；站内通知，门禁=认证+当前物业有效任职，
    #     不设专项权限码——全员工作入口；refresh=实时派生，按物业隔离) ---
    path("notifications", notification_center.notification_list, name="notification_list"),
    path(
        "notifications/unread-count",
        notification_center.unread_count,
        name="notification_unread_count",
    ),
    path("notifications/read-all", notification_center.read_all, name="notification_read_all"),
    path("notifications/refresh", notification_center.refresh, name="notification_refresh"),
    path(
        "notifications/<int:notification_id>/read",
        notification_center.mark_read,
        name="notification_mark_read",
    ),
    # --- 可执行待办（MC-007）：通知已读与任务完成完全分离 ---
    path("tasks", task_center.task_list, name="task_list"),
    path("tasks/<int:work_item_id>", task_center.task_detail, name="task_detail"),
    path("tasks/<int:work_item_id>/claim", task_center.task_claim, name="task_claim"),
    path("tasks/<int:work_item_id>/assign", task_center.task_assign, name="task_assign"),
    path("tasks/<int:work_item_id>/complete", task_center.task_complete, name="task_complete"),
]
