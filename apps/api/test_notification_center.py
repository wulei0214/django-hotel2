"""§4.6 通知与待办中心回归测试包。

覆盖：
  * refresh 派生正确性：待确认预订 / 今日预抵 / 今日离店 / 过期未到 /
    待清洁房 / 待放行 / 待审批 / break-glass 事后复核——来源、类别、级别、
    岗位广播目标、跳转链接逐项断言；
  * upsert 幂等：两次 refresh 不重复堆积；计数变化只更新不新建；
  * 自动销项：源业务解决后再次 refresh 自动置已读（"自动消失"）；
  * 模型约束：通知必须有接收人（recipient 或 role_group 二选一）；
  * API 权限四态：匿名 401 / 无任职（无物业上下文）404 / 有效任职 200；
  * 读取范围：recipient=本人 或 role_group∈当前有效岗位组（调岗口径）；
  * 物业隔离：他物业通知不可见、refresh 按物业隔离互不污染；
  * 已读操作：单条已读（幂等，不覆盖首次 read_at）/ 全部已读（仅我的范围）；
  * unread-count（含按类别分布）；
  * 统一分页契约（非法 page/page_size 400、稳定排序）；
  * /manage/notifications/ 页面门禁（匿名 302 / 无任职 403 / 员工 200）
    与 MANAGEMENT_CENTERS 注册契约；
  * 侧边栏未读徽标注入（pms_unread_notifications，与 API 同口径）；
  * 约束核对：通知生成不产生 AuditEvent（§4.6：通知非审计动作）。

口径：站内通知（无第三方渠道）；refresh 为实时派生 + upsert（无 celery）。
"""
import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（Mimosa L3 门禁）。
from django.utils.crypto import get_random_string  # noqa: E402

from apps.api import notification_center
from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.billing.models import ApprovalRecord
from apps.core.models import (
    AuditEvent,
    BreakGlassRecord,
    BusinessDate,
    Notification,
    NotificationRead,
    Property,
)
from apps.core.services_notifications import refresh_operational_todos
from apps.guests.models import Guest
from apps.identity.models import PropertyStaffMembership, StaffProfile
from apps.inventory.models import Room, RoomType
from apps.reservations.models import Reservation, ReservationRoom

_TEST_PW_EA5E4F64 = get_random_string(24)
_TEST_PW_5A092416 = get_random_string(24)




class NotificationCenterTestBase(E2EFlowTestBase):
    """DEFAULT 物业 + 前台/值班经理/客房服务员三岗位 + 业务工厂。"""

    maxDiff = None

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("nt_frontdesk", "前台")
        self.dm = self._make_staff("nt_duty", "值班经理")
        self.housekeeper = self._make_staff("nt_housekeeper", "客房服务员")
        self.client.force_login(self.fd)

    # ------------------------------------------------------------------
    # 业务工厂
    # ------------------------------------------------------------------
    def _new_property(self, code="NT-OTHER"):
        """他物业工厂（物业隔离测试用）。"""
        property_obj, _ = Property.objects.get_or_create(
            code=code, defaults={"name": "通知测试他物业"})
        return property_obj

    def _reservation(self, status=Reservation.Status.PENDING,
                     check_in_offset=2, nights=1, booking_no=None):
        """预订工厂：默认后天抵店（不落入任何待办来源，按需覆写）。"""
        check_in = self.today + timedelta(days=check_in_offset)
        booking_no = booking_no or f"NT-{uuid.uuid4().hex[:10].upper()}"
        guest = Guest.objects.create(
            property=self.property, full_name="通知测试住客",
            phone=f"138{uuid.uuid4().int % 10**8:08d}")
        reservation = Reservation.objects.create(
            property=self.property, booking_no=booking_no, guest=guest,
            source="WEBSITE", status=status, check_in=check_in,
            check_out=check_in + timedelta(days=nights),
            total_amount=Decimal("288.00"))
        ReservationRoom.objects.create(
            property=self.property, reservation=reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=check_in, check_out=check_in + timedelta(days=nights),
            amount=Decimal("288.00"), nightly_rates=[])
        return reservation

    def _approval(self, action_code="billing.refund_approve"):
        return ApprovalRecord.objects.create(
            property=self.property, action_code=action_code,
            entity_type="Refund", entity_id=str(uuid.uuid4().int % 10000),
            requested_by=self.fd, reason="通知中心测试审批",
            idempotency_key=f"nt-appr-{uuid.uuid4().hex[:16]}")

    def _set_room(self, room, housekeeping_status):
        Room.objects.filter(pk=room.pk).update(
            housekeeping_status=housekeeping_status)
        room.refresh_from_db()

    def _breakglass_used(self):
        business_date = BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.CLOSED)
        return BreakGlassRecord.objects.create(
            property=self.property, business_date=business_date,
            requested_by=self.dm, reason="通知中心测试紧急重开",
            password_verified_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
            status=BreakGlassRecord.Status.USED, used_at=timezone.now())

    def _direct_notification(self, staff_user, title="定向通知",
                             category=Notification.Category.SYSTEM,
                             severity=Notification.Severity.INFO):
        """定向通知工厂（recipient 非空，不参与自动销项）。"""
        profile = StaffProfile.objects.get(user=staff_user)
        return Notification.objects.create(
            property=self.property, recipient=profile, category=category,
            title=title, body="定向通知内容", link="/home/")

    def _rows(self, **filters):
        return list(Notification.objects.filter(**filters).order_by("id"))


class RefreshDerivationTests(NotificationCenterTestBase):
    """refresh_operational_todos 派生正确性（来源清单逐项）。"""

    def test_pending_confirm_reservation_creates_todo(self):
        reservation = self._reservation(status=Reservation.Status.PENDING)
        summary = refresh_operational_todos(self.property)
        self.assertEqual(summary["created"], 2)  # 前台 + 值班经理 岗位广播
        rows = self._rows(category=Notification.Category.RESERVATION)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.category, "reservation")
            self.assertEqual(row.severity, "warning")
            self.assertIn(reservation.booking_no, row.title)
            self.assertIn("待确认", row.title)
            self.assertEqual(row.link, "/manage/reservations/")
            self.assertIsNotNone(row.role_group_id)
            self.assertIsNone(row.recipient_id)
            self.assertTrue(row.dedupe_key)
        self.assertEqual(
            {row.role_group.name for row in rows}, {"前台", "值班经理"})

    def test_arrival_today_creates_todo(self):
        reservation = self._reservation(
            status=Reservation.Status.CONFIRMED, check_in_offset=0)
        refresh_operational_todos(self.property)
        rows = self._rows(category=Notification.Category.RESERVATION)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("今日预抵", row.title)
            self.assertIn(reservation.booking_no, row.title)
            self.assertEqual(row.severity, "info")
            self.assertEqual(row.link, "/checkin/")

    def test_departure_today_creates_todo(self):
        reservation = self._reservation(
            status=Reservation.Status.CHECKED_IN, check_in_offset=-1)
        # 已入住主单 check_out=今天（工厂默认 1 晚 → 今天离店）
        refresh_operational_todos(self.property)
        rows = self._rows(category=Notification.Category.RESERVATION)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("今日离店", row.title)
            self.assertIn(reservation.booking_no, row.title)
            self.assertEqual(row.severity, "info")
            self.assertEqual(row.link, "/checkin/")

    def test_overdue_reservation_creates_critical_todo(self):
        reservation = self._reservation(
            status=Reservation.Status.CONFIRMED,
            check_in_offset=-3, nights=2)  # check_in 已过 3 天仍未入住
        refresh_operational_todos(self.property)
        rows = self._rows(category=Notification.Category.RESERVATION)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("过期未到", row.title)
            self.assertIn(reservation.booking_no, row.title)
            self.assertEqual(row.severity, "critical")
            self.assertEqual(row.link, "/checkin/")

    def test_dirty_room_creates_cleaning_todo_with_count(self):
        self._set_room(self.room1, Room.HousekeepingStatus.DIRTY)
        refresh_operational_todos(self.property)
        rows = self._rows(category=Notification.Category.HOUSEKEEPING)
        self.assertEqual(len(rows), 2)  # 客房服务员 + 值班经理
        for row in rows:
            self.assertEqual(row.category, "housekeeping")
            self.assertEqual(row.severity, "warning")
            self.assertIn("待清洁房 1 间", row.title)
            self.assertEqual(row.link, "/manage/housekeeping/")
        self.assertEqual(
            {row.role_group.name for row in rows}, {"客房服务员", "值班经理"})

    def test_inspecting_room_creates_release_todo_for_managers(self):
        self._set_room(self.room2, Room.HousekeepingStatus.INSPECTING)
        refresh_operational_todos(self.property)
        rows = self._rows(category=Notification.Category.HOUSEKEEPING)
        self.assertEqual(len(rows), 1)  # 净房放行=值班经理口径
        self.assertIn("待放行净房 1 间", rows[0].title)
        self.assertEqual(rows[0].role_group.name, "值班经理")
        self.assertEqual(rows[0].severity, "warning")

    def test_pending_approval_creates_todo_with_action_routing(self):
        record = self._approval(action_code="billing.refund_approve")
        reopen = self._approval(action_code="night_audit.reopen")
        refresh_operational_todos(self.property)
        rows = self._rows(category=Notification.Category.APPROVAL)
        self.assertEqual(len(rows), 4)  # 2 条记录 × (值班经理 + 店长)
        refund_rows = [row for row in rows if record.action_code in row.title]
        reopen_rows = [row for row in rows if reopen.action_code in row.title]
        self.assertEqual(len(refund_rows), 2)
        self.assertEqual(len(reopen_rows), 2)
        for row in refund_rows:
            self.assertEqual(row.link, "/manage/finance/")
        for row in reopen_rows:
            self.assertEqual(row.link, "/manage/night-audit/")
        self.assertEqual(
            {row.role_group.name for row in rows}, {"值班经理", "店长"})
        self.assertTrue(all(row.severity == "warning" for row in rows))
        self.assertTrue(all(record.entity_type in row.body for row in refund_rows))

    def test_used_breakglass_creates_audit_review_todo(self):
        record = self._breakglass_used()
        refresh_operational_todos(self.property)
        rows = self._rows(category=Notification.Category.AUDIT)
        self.assertEqual(len(rows), 2)  # 值班经理 + 店长
        for row in rows:
            self.assertEqual(row.category, "audit")
            self.assertEqual(row.severity, "critical")
            self.assertEqual(row.link, "/manage/night-audit/")
            self.assertIn(str(record.business_date.business_date), row.title)

    def test_clean_property_and_future_reservations_produce_nothing(self):
        self._reservation(status=Reservation.Status.CONFIRMED, check_in_offset=3)
        summary = refresh_operational_todos(self.property)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(Notification.objects.count(), 0)


class RefreshIdempotencyAndResolveTests(NotificationCenterTestBase):
    """upsert 幂等 / 自动销项 / 计数更新 / 无审计副作用。"""

    def test_refresh_twice_does_not_duplicate(self):
        self._reservation(status=Reservation.Status.PENDING)
        self._set_room(self.room1, Room.HousekeepingStatus.DIRTY)
        first = refresh_operational_todos(self.property)
        second = refresh_operational_todos(self.property)
        self.assertEqual(first["created"], 4)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(Notification.objects.count(), first["created"])
        self.assertEqual(first["active"], second["active"])

    def test_resolved_source_marks_todo_read(self):
        reservation = self._reservation(status=Reservation.Status.PENDING,
                                        check_in_offset=5)
        refresh_operational_todos(self.property)
        self.assertEqual(len(self._rows(resolved_at__isnull=True)), 2)
        # 源业务解决：预订确认（状态权威仍是 Reservation，通知只是投影）
        Reservation.objects.filter(pk=reservation.pk).update(
            status=Reservation.Status.CONFIRMED)
        summary = refresh_operational_todos(self.property)
        self.assertEqual(summary["resolved"], 2)
        self.assertFalse(Notification.objects.filter(
            dedupe_key__isnull=False, resolved_at__isnull=True
        ).exclude(dedupe_key="").exists())
        resolved_at_values = [row.resolved_at for row in self._rows()]
        self.assertTrue(all(resolved_at_values))

    def test_count_change_updates_body_without_new_rows(self):
        self._set_room(self.room1, Room.HousekeepingStatus.DIRTY)
        refresh_operational_todos(self.property)
        before_pks = {row.pk for row in self._rows(
            category=Notification.Category.HOUSEKEEPING)}
        self._set_room(self.room2, Room.HousekeepingStatus.CLEANING)
        summary = refresh_operational_todos(self.property)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["updated"], 2)
        after_rows = self._rows(category=Notification.Category.HOUSEKEEPING)
        self.assertEqual({row.pk for row in after_rows}, before_pks)
        self.assertTrue(all("待清洁房 2 间" in row.title for row in after_rows))

    def test_manual_notification_never_auto_resolved(self):
        """dedupe_key 为空的人工/公告类通知不参与自动销项。"""
        manual = self._direct_notification(self.fd, title="系统公告")
        refresh_operational_todos(self.property)
        manual.refresh_from_db()
        self.assertFalse(manual.is_read)
        self.assertIsNone(manual.read_at)

    def test_refresh_writes_no_audit_event(self):
        """约束核对：通知生成/销项不是审计动作，不产生 AuditEvent。"""
        self._reservation(status=Reservation.Status.PENDING)
        before = AuditEvent.objects.count()
        refresh_operational_todos(self.property)
        Reservation.objects.update(status=Reservation.Status.CONFIRMED)
        refresh_operational_todos(self.property)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_notification_requires_audience(self):
        """recipient 与 role_group 都为空 → 模型约束拒绝（4.1 full_clean）。"""
        orphan = Notification(
            property=self.property, category=Notification.Category.SYSTEM,
            title="无接收人", body="")
        with self.assertRaises(ValidationError):
            orphan.full_clean()


class NotificationApiTests(NotificationCenterTestBase):
    """通知读取/操作 API：范围、权限、物业隔离、分页契约。"""

    def _login(self, user):
        self.client.force_login(user)

    # -- 权限四态 -------------------------------------------------------
    def test_list_requires_authentication(self):
        self.client.logout()
        response = self.client.get("/api/v1/notifications")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_endpoints_require_property_membership(self):
        outsider = User.objects.create_user(
            username="nt_outsider", password=_TEST_PW_EA5E4F64)
        self._login(outsider)
        for method, url in (("get", "/api/v1/notifications"),
                            ("get", "/api/v1/notifications/unread-count"),
                            ("post", "/api/v1/notifications/read-all"),
                            ("post", "/api/v1/notifications/refresh")):
            response = getattr(self.client, method)(url)
            self.assertEqual(response.status_code, 404, url)
            self.assertEqual(
                response.json()["error"]["code"], "RESOURCE_NOT_FOUND", url)

    def test_endpoints_reject_stale_membership_at_middleware_layer(self):
        """任职被停用：中间件不再提供物业上下文 → 404（与无任职同口径）。"""
        PropertyStaffMembership.objects.filter(
            staff_profile__user=self.fd).update(
            status=PropertyStaffMembership.Status.SUSPENDED)
        response = self.client.get("/api/v1/notifications")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_stale_membership_403_view_layer_defense(self):
        """视图层兜底：物业上下文存在但 get_active_membership 失效 → 403。

        （HTTP 链路上中间件先行拒绝；此处直调视图锁住防御分支行为。）
        """
        from django.test import RequestFactory
        PropertyStaffMembership.objects.filter(
            staff_profile__user=self.fd).update(
            status=PropertyStaffMembership.Status.SUSPENDED)
        request = RequestFactory().get("/api/v1/notifications")
        request.user = self.fd
        request.property = self.property
        response = notification_center.notification_list(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            json.loads(response.content)["error"]["code"], "PERMISSION_DENIED")

    # -- 列表与范围 -----------------------------------------------------
    def test_list_shows_role_broadcast_and_direct_only(self):
        self._reservation(status=Reservation.Status.PENDING)
        refresh_operational_todos(self.property)
        self._direct_notification(self.fd, title="给我的定向")

        self._login(self.fd)
        response = self.client.get("/api/v1/notifications")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        # 前台广播 1 条 + 定向 1 条；值班经理广播不可见
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["audience"]["type"] for row in rows},
            {"role_group", "staff"})
        self.assertIn("给我的定向", [row["title"] for row in rows])

        self._login(self.housekeeper)
        housekeeper_rows = self.client.get("/api/v1/notifications").json()["data"]
        self.assertEqual(housekeeper_rows, [])

    def test_list_filters_category_and_read_state(self):
        self._reservation(status=Reservation.Status.PENDING)
        self._set_room(self.room1, Room.HousekeepingStatus.DIRTY)
        refresh_operational_todos(self.property)
        self._login(self.dm)
        by_category = self.client.get(
            "/api/v1/notifications?category=housekeeping").json()["data"]
        self.assertTrue(
            all(row["category"] == "housekeeping" for row in by_category))
        # 值班经理只看到广播给本组的 1 条清洁待办（客房服务员行不可见）
        self.assertEqual(len(by_category), 1)
        marked = self.client.post(
            f"/api/v1/notifications/{by_category[0]['id']}/read")
        self.assertEqual(marked.status_code, 200)
        unread = self.client.get(
            "/api/v1/notifications?is_read=false").json()["data"]
        self.assertNotIn(
            by_category[0]["id"], [row["id"] for row in unread])

    def test_list_rejects_invalid_filters_and_pagination(self):
        for query in ("page=0", "page=abc", "page_size=0", "page_size=101",
                      "category=nonsense", "is_read=maybe"):
            response = self.client.get(f"/api/v1/notifications?{query}")
            self.assertEqual(response.status_code, 400, query)
            self.assertEqual(
                response.json()["error"]["code"], "VALIDATION_ERROR", query)

    def test_list_stable_ordering_newest_first(self):
        first = self._direct_notification(self.fd, title="第一条")
        second = self._direct_notification(self.fd, title="第二条")
        rows = self.client.get("/api/v1/notifications").json()["data"]
        self.assertEqual([row["id"] for row in rows], [second.pk, first.pk])

    # -- 未读计数 -------------------------------------------------------
    def test_unread_count_by_category(self):
        self._reservation(status=Reservation.Status.PENDING)
        refresh_operational_todos(self.property)
        self._direct_notification(self.fd, title="定向未读")
        data = self.client.get("/api/v1/notifications/unread-count").json()["data"]
        self.assertEqual(data["unread_count"], 2)
        self.assertEqual(data["by_category"],
                         {"reservation": 1, "system": 1})
        # 岗位广播之外：客房服务员计数为 0
        self._login(self.housekeeper)
        empty = self.client.get("/api/v1/notifications/unread-count").json()["data"]
        self.assertEqual(empty["unread_count"], 0)

    # -- 已读操作 -------------------------------------------------------
    def test_mark_read_is_idempotent_and_keeps_first_read_at(self):
        notification = self._direct_notification(self.fd, title="待读")
        first = self.client.post(f"/api/v1/notifications/{notification.pk}/read")
        self.assertEqual(first.status_code, 200)
        first_read_at = first.json()["data"]["read_at"]
        self.assertIsNotNone(first_read_at)
        second = self.client.post(f"/api/v1/notifications/{notification.pk}/read")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["data"]["read_at"], first_read_at)
        notification.refresh_from_db()
        self.assertFalse(notification.is_read)
        self.assertEqual(NotificationRead.objects.filter(
            notification=notification,
            staff_profile__user=self.fd).count(), 1)

    def test_broadcast_read_receipt_is_personal_per_employee(self):
        """同岗位广播由一名员工读过，不得影响同岗位其他员工的未读状态。"""
        profile = self.housekeeper.staff_profile
        group = profile.property_memberships.get(
            property=self.property).role_assignments.get(
                group__name="客房服务员").group
        broadcast = Notification.objects.create(
            property=self.property, role_group=group,
            category=Notification.Category.SYSTEM, title="岗位广播")
        self.client.force_login(self.housekeeper)
        first = self.client.post(
            f"/api/v1/notifications/{broadcast.pk}/read")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()["data"]["is_read"], True)

        second_housekeeper = self._make_staff(
            "nt_housekeeper_second", "客房服务员")
        self.client.force_login(second_housekeeper)
        unread = self.client.get(
            "/api/v1/notifications?is_read=false").json()["data"]
        self.assertIn(broadcast.pk, {item["id"] for item in unread})

    def test_mark_read_out_of_scope_is_404(self):
        self._set_room(self.room1, Room.HousekeepingStatus.DIRTY)
        refresh_operational_todos(self.property)
        housekeeper_row = Notification.objects.get(
            role_group__name="客房服务员")
        response = self.client.post(
            f"/api/v1/notifications/{housekeeper_row.pk}/read")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_read_all_scoped_to_my_audience(self):
        mine = self._direct_notification(self.fd, title="我的定向")
        other = self._direct_notification(self.dm, title="值班经理定向")
        response = self.client.post("/api/v1/notifications/read-all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["marked_read"], 1)
        self.assertTrue(NotificationRead.objects.filter(
            notification=mine, staff_profile__user=self.fd).exists())
        self.assertFalse(NotificationRead.objects.filter(
            notification=other, staff_profile__user=self.fd).exists())
        self.assertFalse(NotificationRead.objects.filter(
            notification=other).exists())
        # 旧全局字段不再被 API 修改，避免一个员工改变广播通知的全员状态。
        mine.refresh_from_db()
        self.assertFalse(mine.is_read)

    # -- refresh 端点 ---------------------------------------------------
    def test_refresh_endpoint_creates_and_resolves(self):
        reservation = self._reservation(status=Reservation.Status.PENDING,
                                        check_in_offset=5)
        response = self.client.post("/api/v1/notifications/refresh")
        self.assertEqual(response.status_code, 200)
        summary = response.json()["data"]
        self.assertEqual(summary["created"], 2)
        self.assertIn("refresh_note", response.json()["meta"])
        Reservation.objects.filter(pk=reservation.pk).update(
            status=Reservation.Status.CONFIRMED)
        second = self.client.post("/api/v1/notifications/refresh")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["data"]["resolved"], 2)
        self.assertFalse(Notification.objects.filter(
            dedupe_key__isnull=False, resolved_at__isnull=True
        ).exclude(dedupe_key="").exists())

    @override_settings(PUBLIC_RATE_LIMITS={
        "pms.notification_refresh": [(("ip", "username"), 1, 60)],
    })
    def test_refresh_endpoint_is_rate_limited_and_audited(self):
        first = self.client.post("/api/v1/notifications/refresh")
        self.assertEqual(first.status_code, 200, first.content)
        event = AuditEvent.objects.filter(
            action="notification.refresh", property=self.property,
            actor=self.fd).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.entity_type, "Property")
        self.assertEqual(str(event.entity_id), str(self.property.pk))

        second = self.client.post("/api/v1/notifications/refresh")
        self.assertEqual(second.status_code, 429, second.content)
        self.assertEqual(second.json()["error"]["code"], "RATE_LIMITED")
        self.assertEqual(AuditEvent.objects.filter(
            action="notification.refresh", property=self.property,
            actor=self.fd).count(), 1)

    def test_refresh_endpoint_requires_authentication(self):
        self.client.logout()
        response = self.client.post("/api/v1/notifications/refresh")
        self.assertEqual(response.status_code, 401)

    # -- 物业隔离 -------------------------------------------------------
    def test_property_isolation(self):
        other_property = self._new_property()
        other_room_type = RoomType.objects.create(
            property=other_property, code="NT-OTHER-KING",
            name="他物业测试房型", capacity=2)
        Room.objects.create(
            property=other_property, room_type=other_room_type,
            number="NT-9001", floor="9",
            housekeeping_status=Room.HousekeepingStatus.DIRTY)
        refresh_operational_todos(other_property)
        # 他物业待办对本物业员工不可见
        self.assertEqual(
            self.client.get("/api/v1/notifications").json()["data"], [])
        self.assertEqual(
            self.client.get("/api/v1/notifications/unread-count")
            .json()["data"]["unread_count"], 0)
        # 本物业 refresh 不销项他物业的待办（按 property 隔离）
        before = Notification.objects.filter(
            property=other_property, resolved_at__isnull=True).count()
        self.client.post("/api/v1/notifications/refresh")
        self.assertEqual(
            Notification.objects.filter(
                property=other_property, resolved_at__isnull=True).count(), before)
        # 他物业的待办行仍存在且未读
        self.assertTrue(Notification.objects.filter(
            property=other_property, role_group__name="客房服务员",
            resolved_at__isnull=True).exists())


class NotificationCenterPageTests(NotificationCenterTestBase):
    """/manage/notifications/ 页面门禁、注册契约与侧边栏徽标注入。"""

    def test_management_centers_registration(self):
        from apps.api.pages import MANAGEMENT_CENTERS
        config = MANAGEMENT_CENTERS["notifications"]
        self.assertEqual(config["permission"], "")
        self.assertEqual(config["template"], "pms/notification_center.html")
        self.assertEqual(config["endpoint"], "/api/v1/notifications")

    def test_center_page_gates(self):
        self.client.logout()
        anonymous = self.client.get("/manage/notifications/")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/login/", anonymous.url)

        outsider = User.objects.create_user(
            username="nt_page_outsider", password=_TEST_PW_EA5E4F64)
        self.client.force_login(outsider)
        forbidden = self.client.get("/manage/notifications/")
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.housekeeper)
        allowed = self.client.get("/manage/notifications/")
        self.assertEqual(allowed.status_code, 200)
        content = allowed.content.decode("utf-8")
        self.assertIn("通知与待办中心", content)
        self.assertIn('var api = "/api/v1/notifications"', content)
        self.assertIn("全部已读", content)
        self.assertIn("立即刷新待办", content)
        self.assertIn('id="nt-page-size"', content)
        self.assertIn('id="nt-goto-page"', content)
        self.assertIn('id="nt-goto-btn"', content)

    def test_sidebar_badge_injection(self):
        self._reservation(status=Reservation.Status.PENDING)
        refresh_operational_todos(self.property)
        self.client.force_login(self.fd)
        page = self.client.get("/manage/notifications/")
        self.assertEqual(page.status_code, 200)
        content = page.content.decode("utf-8")
        self.assertIn('href="/manage/notifications/"', content)
        self.assertIn("side-badge", content)
        self.assertIn(">1</span>", content)  # 前台岗位广播 1 条未读

        # 全部已读后徽标归零消失（0 不渲染徽标）
        self.client.post("/api/v1/notifications/read-all")
        content_after = self.client.get("/home/").content.decode("utf-8")
        self.assertIn('href="/manage/notifications/"', content_after)
        self.assertNotIn("side-badge", content_after)

    def test_member_without_role_assignment_gets_direct_notifications(self):
        """无任何岗位授权的在职员工：入口可见（pms_is_staff）、定向通知可达，
        岗位广播不可见（§4.6：按当前有效任职动态授权）。"""
        user = User.objects.create_user(
            username="nt_norole", password=_TEST_PW_5A092416)
        profile = StaffProfile.objects.create(
            user=user, full_name="无岗员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        PropertyStaffMembership.objects.create(
            staff_profile=profile, property=self.property,
            employee_no="NT-NOROLE",
            status=PropertyStaffMembership.Status.ACTIVE)
        self._direct_notification(user, title="给无岗员工的定向")
        self.client.force_login(user)
        page = self.client.get("/manage/notifications/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            'href="/manage/notifications/"', page.content.decode("utf-8"))
        rows = self.client.get("/api/v1/notifications").json()["data"]
        self.assertEqual(
            [row["title"] for row in rows], ["给无岗员工的定向"])

    def test_severity_and_category_labels_in_api_payload(self):
        self._reservation(status=Reservation.Status.PENDING,
                          check_in_offset=-3, nights=2)
        self._set_room(self.room1, Room.HousekeepingStatus.INSPECTING)
        refresh_operational_todos(self.property)
        self.client.force_login(self.dm)
        rows = self.client.get("/api/v1/notifications").json()["data"]
        payload = {(row["category"], row["severity"]) for row in rows}
        self.assertIn(("reservation", "critical"), payload)
        self.assertIn(("housekeeping", "warning"), payload)
        for row in rows:
            self.assertTrue(row["category_label"])
            self.assertTrue(row["severity_label"])
