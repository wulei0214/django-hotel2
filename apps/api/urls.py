from django.urls import path

from . import approval, audit_center, billing_center, front_desk, guest_center, housekeeping_center, night_audit, notification_center, report_center, reservation_center, staff_center, views

app_name = "api"

urlpatterns = [
    path("night-audit/preview", night_audit.night_audit_preview, name="night_audit_preview"),
    path("night-audit/run", night_audit.night_audit_run, name="night_audit_run"),
    # §4.2 营业日中心只读端点：列表/当前/详情 + 夜审历史 + break-glass 列表
    path("night-audit/business-dates", night_audit.night_audit_business_dates, name="night_audit_business_dates"),
    path("night-audit/business-dates/current", night_audit.night_audit_business_date_current, name="night_audit_business_date_current"),
    path("night-audit/business-dates/<int:business_date_id>", night_audit.night_audit_business_date_detail, name="night_audit_business_date_detail"),
    path("night-audit/break-glass", night_audit.night_audit_break_glass_list, name="night_audit_break_glass_list"),
    path("night-audit/runs", night_audit.night_audit_runs, name="night_audit_runs"),
    path("night-audit/runs/<int:run_id>", night_audit.night_audit_run_detail, name="night_audit_run_detail"),
    path("night-audit/business-dates/<int:business_date_id>/reopen-request", night_audit.night_audit_reopen_request, name="night_audit_reopen_request"),
    path("night-audit/business-dates/<int:business_date_id>/approvals/<int:approval_id>/approve", night_audit.night_audit_reopen_approve, name="night_audit_reopen_approve"),
    path("night-audit/business-dates/<int:business_date_id>/break-glass", night_audit.night_audit_break_glass_request, name="night_audit_break_glass_request"),
    path("night-audit/business-dates/<int:business_date_id>/break-glass/<int:record_id>/execute", night_audit.night_audit_break_glass_execute, name="night_audit_break_glass_execute"),
    path("night-audit/break-glass/<int:record_id>/review", night_audit.night_audit_break_glass_review, name="night_audit_break_glass_review"),
    path("front-desk/overview", front_desk.overview, name="front_desk_overview"),
    path("front-desk/arrivals", front_desk.arrivals, name="front_desk_arrivals"),
    path("front-desk/departures", front_desk.departures, name="front_desk_departures"),
    path("front-desk/in-house", front_desk.in_house, name="front_desk_in_house"),
    path("front-desk/reservations", front_desk.reservations, name="front_desk_reservations"),
    path("front-desk/reservations/<str:booking_no>", front_desk.reservation_detail, name="front_desk_reservation_detail"),
    path("front-desk/rooms", front_desk.rooms, name="front_desk_rooms"),
    path("front-desk/rooms/assignable", front_desk.assignable_rooms, name="front_desk_assignable_rooms"),
    path("front-desk/folios/<str:booking_no>", front_desk.folio_detail, name="front_desk_folio_detail"),
    path("front-desk/legacy-reservations", front_desk.legacy_reservations, name="front_desk_legacy_reservations"),
    path("front-desk/legacy-reservations/<int:legacy_id>", front_desk.legacy_reservation_detail, name="front_desk_legacy_reservation_detail"),
    path("front-desk/stays/<int:stay_id>/available-rooms", front_desk.available_stay_rooms, name="front_desk_available_stay_rooms"),
    path("front-desk/stays/<int:stay_id>/extend", front_desk.extend_stay_api, name="front_desk_extend_stay"),
    path("front-desk/stays/<int:stay_id>/change-room", front_desk.change_stay_room_api, name="front_desk_change_stay_room"),
    path("front-desk/reservation-rooms/<int:reservation_room_id>/no-show", front_desk.mark_no_show_api, name="front_desk_no_show"),
    path("front-desk/reservations/<str:booking_no>/deposits", front_desk.receive_deposit_api, name="front_desk_receive_deposit"),
    path("front-desk/payments/<int:payment_id>/refunds", front_desk.request_refund_api, name="front_desk_request_refund"),
    path("front-desk/refunds/<int:refund_id>/approve", front_desk.approve_refund_api, name="front_desk_approve_refund"),
    path("front-desk/reservation-rooms/<int:reservation_room_id>/assign", front_desk.assign_room, name="front_desk_assign_room"),
    path("front-desk/reservation-rooms/<int:reservation_room_id>/check-in", front_desk.check_in, name="front_desk_check_in"),
    path("front-desk/reservations/<str:booking_no>/payments", front_desk.collect_payment, name="front_desk_collect_payment"),
    path("front-desk/stays/<int:stay_id>/check-out", front_desk.check_out, name="front_desk_check_out"),
    path("availability", views.availability, name="availability"),
    path("reservations", views.reservations_create, name="reservations_create"),
    path("reservations/<str:booking_no>", views.reservation_detail, name="reservation_detail"),
    path("reservations/<str:booking_no>/cancel", views.reservation_cancel, name="reservation_cancel"),
    path("payments/intents", views.payment_intent_create, name="payment_intent_create"),
    path("payments/callback", views.payment_callback, name="payment_callback"),
    path("openapi.json", views.openapi_document, name="openapi"),
    # --- Guest 360 (V1.1 Phase 1 只读原型) ---
    path("guests", guest_center.guest_list, name="guest_list"),
    path("guests/<int:guest_id>", guest_center.guest_detail, name="guest_detail"),
    path("guests/create", guest_center.guest_create, name="guest_create"),
    path("guests/<int:guest_id>/update", guest_center.guest_update, name="guest_update"),
    # 基线 2.0 §3.2：Guest 360 时间线与账单摘要（只读聚合）
    path("guests/<int:guest_id>/timeline", guest_center.guest_timeline, name="guest_timeline"),
    path("guests/<int:guest_id>/folios", guest_center.guest_folios, name="guest_folios"),
    path("guests/<int:guest_id>/documents/<int:document_id>/reveal", guest_center.guest_reveal_document, name="guest_reveal_document"),
    path("guests/<int:guest_id>/phones/<int:phone_id>/reveal", guest_center.guest_reveal_phone, name="guest_reveal_phone"),
    # --- Room 中心 (V1.1 Phase 1 只读原型) ---
    path("rooms/status", guest_center.room_status_list, name="room_status_list"),
    path("room-types", guest_center.room_type_list, name="room_type_list"),
    path("rate-calendar", guest_center.rate_calendar, name="rate_calendar"),
    path("financial-summary", guest_center.financial_summary_api, name="financial_summary"),
    # --- ApprovalRecord (V1.2 Q-05/Q-06/Q-14) ---
    path("approvals", approval.approval_list, name="approval_list"),
    path("approvals/request", approval.approval_request, name="approval_request"),
    path("approvals/<int:record_id>/approve", approval.approval_approve, name="approval_approve"),
    path("approvals/<int:record_id>/reject", approval.approval_reject, name="approval_reject"),
    # --- Reservation 管理 (V1.1 Phase 1) ---
    path("reservations-mgmt", reservation_center.reservation_list, name="reservation_mgmt_list"),
    path("reservations-mgmt/<str:booking_no>", reservation_center.reservation_detail_api, name="reservation_mgmt_detail"),
    # --- AuditEvent 时间线 (P1-14) ---
    path("audit-timeline", reservation_center.audit_timeline, name="audit_timeline"),
    # --- 审计与合规中心（基线 2.0 §4.4；读=core.audit_view 唯一门禁，
    #     与 audit-timeline 并存同口径；列表不下发 before/after 载荷） ---
    path("audit/events", audit_center.audit_events, name="audit_events"),
    path("audit/events/<int:event_id>", audit_center.audit_event_detail, name="audit_event_detail"),
    path("audit/summary", audit_center.audit_summary, name="audit_summary"),
    path("audit/actions", audit_center.audit_actions, name="audit_actions"),
    path("daily-rates/<int:rate_id>/adjust", guest_center.adjust_daily_rate_api, name="adjust_daily_rate"),
    # --- Staff 中心 (V1.1 Phase 1) ---
    path("staff", staff_center.staff_list, name="staff_list"),
    path("staff/<int:profile_id>", staff_center.staff_detail, name="staff_detail"),
    # --- Membership (V1.1 Phase 1) ---
    path("memberships", staff_center.membership_list, name="membership_list"),
    path("memberships/<int:membership_id>", staff_center.membership_detail, name="membership_detail"),
    path("memberships/create", staff_center.membership_create, name="membership_create"),
    path("memberships/<int:membership_id>/level", staff_center.membership_change_level, name="membership_change_level"),
    # 基线 2.0 §3.4：会员暂停/恢复（action=suspend|restore）
    path("memberships/<int:membership_id>/status", staff_center.membership_change_status, name="membership_change_status"),
    # --- 财务与账单中心 (基线 2.0 §4.1；读=finance_access，写走 BillingService) ---
    path("billing/folios", billing_center.folio_list, name="billing_folio_list"),
    path("billing/folios/<int:folio_id>", billing_center.folio_detail, name="billing_folio_detail"),
    path("billing/ledger", billing_center.ledger_entry_list, name="billing_ledger"),
    path("billing/payments", billing_center.billing_payments, name="billing_payments"),
    path("billing/refunds", billing_center.billing_refunds, name="billing_refunds"),
    path("billing/reversals", billing_center.reverse_entry_api, name="billing_reverse_entry"),
    path("billing/shifts", billing_center.shift_list, name="billing_shift_list"),
    # 基线 2.0 §3.5：收银班次开班/关班（BillingService.open_shift/close_shift）
    path("billing/shifts/open", billing_center.open_shift_api, name="billing_shift_open"),
    path("billing/shifts/<int:shift_id>/close", billing_center.close_shift_api, name="billing_shift_close"),
    path("billing/summary", billing_center.billing_summary, name="billing_summary"),
    # --- 清洁与维修工作台（读/清洁/维修使用独立权限；净房放行叠加高危专项权限） ---
    path("housekeeping/rooms", housekeeping_center.housekeeping_rooms, name="housekeeping_rooms"),
    path("housekeeping/summary", housekeeping_center.housekeeping_summary_api, name="housekeeping_summary"),
    path("housekeeping/rooms/<int:room_id>/status", housekeeping_center.housekeeping_status_change, name="housekeeping_status_change"),
    path("housekeeping/rooms/<int:room_id>/maintenance", housekeeping_center.housekeeping_maintenance_change, name="housekeeping_maintenance_change"),
    # --- 报表分析中心 (基线 2.0 §4.5；只读聚合。收入/ADR=finance_access，
    #     入住率/客源/清洁=view_accommodation；期间上限 92 天) ---
    path("reports/occupancy", report_center.occupancy_report, name="report_occupancy"),
    path("reports/adr", report_center.adr_report, name="report_adr"),
    path("reports/revenue", report_center.revenue_report, name="report_revenue"),
    path("reports/guests", report_center.guests_report, name="report_guests"),
    path("reports/housekeeping", report_center.housekeeping_report, name="report_housekeeping"),
    # --- 通知与待办中心 (基线 2.0 §4.6；站内通知，门禁=认证+当前物业有效任职，
    #     不设专项权限码——全员工作入口；refresh=实时派生，按物业隔离) ---
    path("notifications", notification_center.notification_list, name="notification_list"),
    path("notifications/unread-count", notification_center.unread_count, name="notification_unread_count"),
    path("notifications/read-all", notification_center.read_all, name="notification_read_all"),
    path("notifications/refresh", notification_center.refresh, name="notification_refresh"),
    path("notifications/<int:notification_id>/read", notification_center.mark_read, name="notification_mark_read"),
]
