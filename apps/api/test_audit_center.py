# -*- coding: utf-8 -*-
"""§4.4 审计与合规中心回归测试包。

覆盖：
  * 只读 API 权限四态（匿名 401 / 无任职 404 / 无 core.audit_view 403 /
    有权限 200）——core.audit_view 为唯一读取门禁（P0-04）；
  * 统一分页契约（默认/边界/非法参数 400）；
  * 六种筛选：action / entity_type / entity_id / actor / date_from / date_to；
  * 物业隔离：他物业与 property=None 审计事件一律不可见（列表/详情/统计/字典）；
  * 详情 before/after 完整展示；
  * summary 双窗口（7 天/30 天）计数、Top 操作者、敏感动作单独计数；
  * PII 防泄露：列表不下发 before/after；揭示类审计只存 masked 值，
    证件明文/手机明文/Session Key 不出现在任何响应；
  * meta.retention_note 留存说明常量（不虚构年限）；
  * /manage/audit/ 页面门禁与侧边栏入口（core.audit_view 持有者可见）。

口径：不改 reservation_center.audit_timeline 既有端点；本模块与其并存，
共享 core.audit_view 门禁与物业过滤口径。
"""
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from apps.api import audit_center
from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.core.models import AuditEvent, Property

# 测试用"明文 PII"（揭示类审计的写入侧纪律：只允许 masked 值入库）
PLAIN_DOC_NO = "110101199003071234"
MASKED_DOC_NO = "1101********34"
PLAIN_PHONE = "13812345678"
MASKED_PHONE = "138****5678"


class AuditCenterTestBase(E2EFlowTestBase):
    """DEFAULT 物业 + 审计查看权限岗位 + 事件工厂。"""

    maxDiff = None

    def setUp(self):
        super().setUp()
        self.dm = self._make_staff("audit_center_duty", "值班经理")
        self.fd = self._make_staff("audit_center_frontdesk", "前台")
        self.client.force_login(self.dm)

    def _event(self, action="billing.collect", entity_type="Folio",
               entity_id="1", actor="default", property_obj="default",
               reason="", before=None, after=None):
        """审计事件工厂：actor/property 传 None 表示系统操作/无物业事件。"""
        return AuditEvent.record(
            action=action, entity_type=entity_type, entity_id=entity_id,
            actor=self.dm if actor == "default" else actor,
            property_obj=(
                self.property if property_obj == "default" else property_obj),
            reason=reason, before=before, after=after)

    def _backdate(self, event, days):
        """把事件 created_at 回拨 N 天（只追加模型禁改实例；查询集 update 为
        固化基线行为，见 apps/core/test_audit_immutability.py）。"""
        AuditEvent._base_manager.filter(pk=event.pk).update(
            created_at=timezone.now() - timedelta(days=days))


class AuditCenterPermissionTests(AuditCenterTestBase):
    """权限四态：匿名 / 无任职 / 无 audit_view / 有 audit_view。"""

    READ_ENDPOINTS = (
        "/api/v1/audit/events",
        "/api/v1/audit/summary",
        "/api/v1/audit/actions",
    )

    def test_read_endpoints_require_authentication(self):
        self.client.logout()
        for url in self.READ_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 401, url)
            self.assertEqual(
                response.json()["error"]["code"], "AUTHENTICATION_REQUIRED", url)

    def test_event_detail_requires_authentication(self):
        event = self._event()
        self.client.logout()
        response = self.client.get(f"/api/v1/audit/events/{event.pk}")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_read_endpoints_require_property_membership(self):
        """已登录但无任何物业任职：无物业上下文 → 404（与夜审中心同口径）。"""
        outsider = User.objects.create_user(
            username="audit_no_membership", password="No-Membership-2026!")
        self.client.force_login(outsider)
        for url in self.READ_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404, url)
            self.assertEqual(
                response.json()["error"]["code"], "RESOURCE_NOT_FOUND", url)

    def test_read_endpoints_require_audit_view_permission(self):
        """前台有任职但无 core.audit_view：403（员工管理权/业务权 ≠ 审计查看权）。"""
        self.client.force_login(self.fd)
        for url in self.READ_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(
                response.json()["error"]["code"], "PERMISSION_DENIED", url)
        event = self._event()
        detail = self.client.get(f"/api/v1/audit/events/{event.pk}")
        self.assertEqual(detail.status_code, 403)

    def test_read_endpoints_ok_for_audit_view_holder(self):
        self._event()
        for url in self.READ_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            body = response.json()
            self.assertIn("data", body)
            self.assertIn("meta", body)
        events = self.client.get("/api/v1/audit/events").json()
        self.assertEqual(len(events["data"]), 1)
        self.assertEqual(events["meta"]["pagination"]["total"], 1)


class AuditCenterPaginationTests(AuditCenterTestBase):
    """统一分页契约：默认页、边界页、非法参数。"""

    def setUp(self):
        super().setUp()
        for index in range(25):
            self._event(entity_id=str(index))

    def test_default_pagination_and_stable_order(self):
        body = self.client.get("/api/v1/audit/events").json()
        pagination = body["meta"]["pagination"]
        self.assertEqual(pagination["page"], 1)
        self.assertEqual(pagination["page_size"], 20)
        self.assertEqual(pagination["total"], 25)
        self.assertTrue(pagination["has_next"])
        self.assertFalse(pagination["has_previous"])
        self.assertEqual(len(body["data"]), 20)
        # 稳定排序：-created_at,-id → 同页 id 严格递减
        ids = [row["id"] for row in body["data"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_last_page_has_remainder_only(self):
        body = self.client.get("/api/v1/audit/events", {"page": "2"}).json()
        self.assertEqual(len(body["data"]), 5)
        self.assertFalse(body["meta"]["pagination"]["has_next"])
        self.assertTrue(body["meta"]["pagination"]["has_previous"])

    def test_page_beyond_total_is_empty_not_error(self):
        body = self.client.get("/api/v1/audit/events", {"page": "99"}).json()
        self.assertEqual(body["data"], [])
        self.assertEqual(body["meta"]["pagination"]["total"], 25)

    def test_invalid_page_and_size_rejected(self):
        for params in ({"page": "0"}, {"page": "-1"}, {"page": "abc"},
                       {"page_size": "0"}, {"page_size": "101"},
                       {"page_size": "99999999999999999999"}):
            response = self.client.get("/api/v1/audit/events", params)
            self.assertEqual(response.status_code, 400, params)
            self.assertEqual(
                response.json()["error"]["code"], "VALIDATION_ERROR", params)

    def test_page_size_upper_bound_accepted(self):
        body = self.client.get("/api/v1/audit/events", {"page_size": "100"}).json()
        self.assertEqual(len(body["data"]), 25)


class AuditCenterFilterTests(AuditCenterTestBase):
    """六种筛选：action / entity_type / entity_id / actor / 日期区间。"""

    def setUp(self):
        super().setUp()
        self._event(action="billing.collect", entity_type="Folio", entity_id="11",
                    reason="收款")
        self._event(action="guest.pii_reveal.phone", entity_type="GuestPhone",
                    entity_id="22", actor=self.fd, reason="前台核验")
        self._event(action="night_audit.run", entity_type="BusinessDate",
                    entity_id="33", reason="夜审")
        old = self._event(action="identity.role_assign", entity_type="StaffProfile",
                          entity_id="44", reason="角色授权")
        self._backdate(old, 10)
        ancient = self._event(action="billing.refund_completed",
                              entity_type="Payment", entity_id="55", reason="退款")
        self._backdate(ancient, 40)

    def test_filter_by_action(self):
        body = self.client.get(
            "/api/v1/audit/events", {"action": "guest.pii_reveal.phone"}).json()
        self.assertEqual(body["meta"]["pagination"]["total"], 1)
        self.assertEqual(body["data"][0]["action"], "guest.pii_reveal.phone")
        self.assertEqual(body["data"][0]["entity_id"], "22")

    def test_filter_by_entity_type_and_id(self):
        body = self.client.get(
            "/api/v1/audit/events",
            {"entity_type": "Folio", "entity_id": "11"}).json()
        self.assertEqual(body["meta"]["pagination"]["total"], 1)
        self.assertEqual(body["data"][0]["action"], "billing.collect")

    def test_filter_by_actor(self):
        body = self.client.get(
            "/api/v1/audit/events", {"actor": self.fd.username}).json()
        self.assertEqual(body["meta"]["pagination"]["total"], 1)
        self.assertEqual(body["data"][0]["actor"], self.fd.username)
        # 操作者筛选与动作筛选叠加
        both = self.client.get(
            "/api/v1/audit/events",
            {"actor": self.fd.username, "action": "billing.collect"}).json()
        self.assertEqual(both["data"], [])

    def test_filter_by_date_range(self):
        today = timezone.localdate()
        window = self.client.get(
            "/api/v1/audit/events",
            {"date_from": str(today - timedelta(days=5)),
             "date_to": str(today)}).json()
        # 只含近 5 天内的 3 条；10/40 天前的不算
        self.assertEqual(window["meta"]["pagination"]["total"], 3)
        actions = {row["action"] for row in window["data"]}
        self.assertNotIn("identity.role_assign", actions)
        self.assertNotIn("billing.refund_completed", actions)
        # 只看截止 5 天前 → 命中 10 天前那条
        old_only = self.client.get(
            "/api/v1/audit/events",
            {"date_to": str(today - timedelta(days=5)),
             "date_from": str(today - timedelta(days=30))}).json()
        self.assertEqual(old_only["meta"]["pagination"]["total"], 1)
        self.assertEqual(old_only["data"][0]["action"], "identity.role_assign")

    def test_date_range_validation(self):
        for params in ({"date_from": "2026/11/01"}, {"date_to": "not-a-date"},
                       {"date_from": "2026-11-10", "date_to": "2026-11-01"}):
            response = self.client.get("/api/v1/audit/events", params)
            self.assertEqual(response.status_code, 400, params)
            self.assertEqual(
                response.json()["error"]["code"], "VALIDATION_ERROR", params)


class AuditCenterIsolationTests(AuditCenterTestBase):
    """物业隔离：他物业与 property=None 事件一律不可见。"""

    def setUp(self):
        super().setUp()
        self.other_property = Property.objects.create(
            code="AUDIT-OTHER", name="审计隔离他店", status=Property.Status.ACTIVE)
        self._event(reason="本物业事件")
        self._event(reason="他物业事件", property_obj=self.other_property)
        self._event(reason="无物业事件", property_obj=None)
        self.other_event = AuditEvent.objects.get(reason="他物业事件")
        self.local_event = AuditEvent.objects.get(reason="本物业事件")

    def test_list_excludes_other_property_and_null_property(self):
        body = self.client.get("/api/v1/audit/events").json()
        self.assertEqual(body["meta"]["pagination"]["total"], 1)
        self.assertEqual(body["data"][0]["id"], self.local_event.pk)

    def test_summary_and_actions_exclude_other_property(self):
        summary = self.client.get("/api/v1/audit/summary").json()["data"]
        self.assertEqual(summary["total_30d"], 1)
        self.assertEqual(
            [row["action"] for row in summary["by_action"]],
            ["billing.collect"])
        actions = self.client.get("/api/v1/audit/actions").json()["data"]
        self.assertEqual([row["action"] for row in actions], ["billing.collect"])
        self.assertEqual(actions[0]["count"], 1)

    def test_detail_of_other_property_event_is_404(self):
        response = self.client.get(f"/api/v1/audit/events/{self.other_event.pk}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "RESOURCE_NOT_FOUND")
        missing = self.client.get("/api/v1/audit/events/99999999")
        self.assertEqual(missing.status_code, 404)

    def test_local_event_detail_ok(self):
        response = self.client.get(f"/api/v1/audit/events/{self.local_event.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["id"], self.local_event.pk)


class AuditCenterDetailTests(AuditCenterTestBase):
    """详情端点：before/after 完整展示 + 元数据字段。"""

    def test_detail_returns_full_before_after_and_metadata(self):
        event = self._event(
            action="inventory.daily_rate_adjust", entity_type="DailyRate",
            entity_id="77", reason="调价",
            before={"price": "300.00"}, after={"price": "388.00"},
        )
        AuditEvent.objects.filter(pk=event.pk).update(
            request_id="req-detail-001", ip="10.0.0.9",
            user_agent="AuditCenterTest/1.0")
        body = self.client.get(f"/api/v1/audit/events/{event.pk}").json()
        data = body["data"]
        self.assertEqual(data["before"], {"price": "300.00"})
        self.assertEqual(data["after"], {"price": "388.00"})
        self.assertEqual(data["reason"], "调价")
        self.assertEqual(data["action_label"], "调整每日房价")
        self.assertEqual(data["entity_type_label"], "每日房价")
        self.assertEqual(data["request_id"], "req-detail-001")
        self.assertEqual(data["ip"], "10.0.0.9")
        self.assertEqual(data["user_agent"], "AuditCenterTest/1.0")
        self.assertTrue(data["is_sensitive"])
        self.assertEqual(data["property"]["code"], self.property.code)
        self.assertEqual(data["actor"], self.dm.username)
        self.assertEqual(data["actor_label"], "%s（%s）" % (self.dm.username, self.dm.username))
        self.assertIn("retention_note", body["meta"])

    def test_detail_null_payloads_returned_as_null(self):
        event = self._event(reason="无载荷事件", actor=None)
        data = self.client.get(f"/api/v1/audit/events/{event.pk}").json()["data"]
        self.assertIsNone(data["before"])
        self.assertIsNone(data["after"])
        self.assertEqual(data["actor_label"], "系统")


class AuditCenterPiiProtectionTests(AuditCenterTestBase):
    """PII 防泄露：列表无变更载荷；揭示审计只存 masked；无 Session Key。"""

    def setUp(self):
        super().setUp()
        # 按揭示服务的写入口径（guest_center / guests.services）造揭示审计：
        # after 只含 masked 值，明文不落审计。
        self.reveal_doc = AuditEvent.record(
            action="guest.pii_reveal.document", entity_type="GuestDocument",
            entity_id="9001", actor=self.fd, property_obj=self.property,
            reason="入住登记证件核验",
            after={"masked": MASKED_DOC_NO})
        self.reveal_phone = AuditEvent.record(
            action="guest.phone_reveal", entity_type="GuestPhone",
            entity_id="9002", actor=self.fd, property_obj=self.property,
            reason="订单联系客人",
            after={"masked": MASKED_PHONE})
        # 非揭示类事件携带变更载荷（列表必须整段隐去）
        self.payload_event = self._event(
            action="identity.password_reset", entity_type="StaffProfile",
            entity_id="88", reason="密码重置",
            before={"secret_before": "PLAINTEXT-BEFORE-XYZ"},
            after={"secret_after": "PLAINTEXT-AFTER-XYZ"})

    def test_reveal_events_store_masked_only(self):
        """写入侧纪律核对：揭示审计载荷不含明文（✓ 源码同口径）。"""
        doc = AuditEvent.objects.get(pk=self.reveal_doc.pk)
        phone = AuditEvent.objects.get(pk=self.reveal_phone.pk)
        self.assertEqual(doc.after_json, {"masked": MASKED_DOC_NO})
        self.assertEqual(phone.after_json, {"masked": MASKED_PHONE})
        self.assertNotIn(PLAIN_DOC_NO, str(doc.after_json))
        self.assertNotIn(PLAIN_PHONE, str(phone.after_json))

    def test_list_response_has_no_change_payloads(self):
        content = self.client.get("/api/v1/audit/events").content.decode("utf-8")
        self.assertNotIn("PLAINTEXT-BEFORE-XYZ", content)
        self.assertNotIn("PLAINTEXT-AFTER-XYZ", content)
        self.assertNotIn("before", content)
        self.assertNotIn("after", content)
        self.assertNotIn("masked", content)
        # 列表摘要字段齐备
        body = self.client.get("/api/v1/audit/events").json()
        row = next(r for r in body["data"] if r["id"] == self.payload_event.pk)
        self.assertEqual(set(row), {
            "id", "action", "action_label", "entity_type", "entity_type_label",
            "entity_id", "actor", "actor_label", "reason", "is_sensitive", "created_at"})

    def test_plaintext_pii_absent_from_list_and_detail(self):
        """证件/手机明文不得出现在列表或详情响应（详情只回 masked）。"""
        for url in ("/api/v1/audit/events",
                    f"/api/v1/audit/events/{self.reveal_doc.pk}",
                    f"/api/v1/audit/events/{self.reveal_phone.pk}",
                    "/api/v1/audit/summary", "/api/v1/audit/actions"):
            content = self.client.get(url).content.decode("utf-8")
            self.assertNotIn(PLAIN_DOC_NO, url + " → " + content)
            self.assertNotIn(PLAIN_PHONE, url + " → " + content)
        detail = self.client.get(
            f"/api/v1/audit/events/{self.reveal_doc.pk}").json()["data"]
        self.assertEqual(detail["after"], {"masked": MASKED_DOC_NO})

    def test_session_key_absent_from_all_responses(self):
        """§4.4：审计日志不得写入 Session ID——本模块任何响应不回显 Session Key。"""
        session_cookie = settings.SESSION_COOKIE_NAME
        urls = ("/api/v1/audit/events", "/api/v1/audit/summary",
                "/api/v1/audit/actions",
                f"/api/v1/audit/events/{self.payload_event.pk}")
        for url in urls:
            content = self.client.get(url).content.decode("utf-8")
            self.assertNotIn(session_cookie, content, url)
            self.assertNotIn("session_key", content, url)

    def test_summary_and_actions_sensitive_flags(self):
        summary = self.client.get("/api/v1/audit/summary").json()["data"]
        self.assertEqual(summary["sensitive"]["count_30d"], 3)
        self.assertEqual(summary["sensitive"]["count_7d"], 3)
        sensitive_actions = {row["action"]
                             for row in summary["sensitive"]["by_action"]}
        self.assertIn("guest.pii_reveal.document", sensitive_actions)
        self.assertIn("identity.password_reset", sensitive_actions)
        actions = self.client.get("/api/v1/audit/actions").json()["data"]
        flags = {row["action"]: row["is_sensitive"] for row in actions}
        self.assertTrue(flags["guest.pii_reveal.document"])
        self.assertTrue(flags["identity.password_reset"])


class AuditCenterSummaryTests(AuditCenterTestBase):
    """summary 统计正确性：双窗口计数、Top 操作者、敏感动作。"""

    def setUp(self):
        super().setUp()
        self._event(action="billing.collect", entity_id="1")            # 7d+30d, dm
        self._event(action="billing.collect", entity_id="2")            # 7d+30d, dm
        self._event(action="guest.pii_reveal.phone", entity_id="3",
                    actor=self.fd)                                      # 7d+30d, fd
        month_old = self._event(action="billing.collect", entity_id="4")  # 30d only
        self._backdate(month_old, 10)
        ancient = self._event(action="night_audit.run", entity_id="5")    # 之外
        self._backdate(ancient, 40)

    def test_window_totals(self):
        data = self.client.get("/api/v1/audit/summary").json()["data"]
        self.assertEqual(data["total_7d"], 3)
        self.assertEqual(data["total_30d"], 4)
        self.assertIn("window", data)
        self.assertIn("start_7d", data["window"])
        self.assertIn("start_30d", data["window"])

    def test_by_action_dual_window_counts(self):
        data = self.client.get("/api/v1/audit/summary").json()["data"]
        by_action = {row["action"]: row for row in data["by_action"]}
        self.assertEqual(by_action["billing.collect"]["count_7d"], 2)
        self.assertEqual(by_action["billing.collect"]["count_30d"], 3)
        self.assertEqual(by_action["guest.pii_reveal.phone"]["count_7d"], 1)
        self.assertEqual(by_action["guest.pii_reveal.phone"]["count_30d"], 1)
        # 40 天前事件不进入任何窗口
        self.assertNotIn("night_audit.run", by_action)

    def test_top_actors_ordering(self):
        data = self.client.get("/api/v1/audit/summary").json()["data"]
        top = {row["actor"]: row for row in data["top_actors"]}
        self.assertEqual(top[self.dm.username]["count_30d"], 3)
        self.assertEqual(top[self.dm.username]["count_7d"], 2)
        self.assertEqual(top[self.fd.username]["count_30d"], 1)
        # dm 计数最高，排第一
        self.assertEqual(data["top_actors"][0]["actor"], self.dm.username)

    def test_sensitive_counts_separate_from_total(self):
        data = self.client.get("/api/v1/audit/summary").json()["data"]
        self.assertEqual(data["sensitive"]["count_7d"], 1)
        self.assertEqual(data["sensitive"]["count_30d"], 1)
        self.assertEqual(data["sensitive"]["by_action"][0]["action"],
                         "guest.pii_reveal.phone")
        # 目录口径固定：SPECIAL_PERMISSIONS 相关已落审计动作
        self.assertIn("billing.refund_completed",
                      data["sensitive_actions_catalog"])
        self.assertIn("permissions.matrix_saved",
                      data["sensitive_actions_catalog"])


class AuditCenterRetentionNoteTests(AuditCenterTestBase):
    """留存说明：响应 meta.retention_note 常量，不虚构年限。"""

    def test_retention_note_present_on_all_read_endpoints(self):
        event = self._event()
        for url in ("/api/v1/audit/events", "/api/v1/audit/summary",
                    "/api/v1/audit/actions",
                    f"/api/v1/audit/events/{event.pk}"):
            meta = self.client.get(url).json()["meta"]
            self.assertEqual(meta.get("retention_note"),
                             audit_center.RETENTION_NOTE, url)

    def test_retention_note_states_pending_decision_without_fabricated_years(self):
        note = audit_center.RETENTION_NOTE
        self.assertIn("只追加", note)
        self.assertIn("确认", note)
        # 不虚构具体年限（如"保留 3 年"）
        self.assertIsNone(re.search(r"保留\s*\d+\s*年", note))
        self.assertIn("audit_immutability.sql", note)


class AuditCenterPageTests(AuditCenterTestBase):
    """/manage/audit/ 页面门禁 + 注册表 + 侧边栏入口。"""

    def test_center_page_requires_login_and_permission(self):
        self.client.logout()
        response = self.client.get("/manage/audit/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

        self.client.force_login(self.fd)
        forbidden = self.client.get("/manage/audit/")
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.dm)
        allowed = self.client.get("/manage/audit/")
        self.assertEqual(allowed.status_code, 200)
        content = allowed.content.decode("utf-8")
        self.assertIn("审计与合规中心", content)
        # 页面 JS 以 /api/v1/audit 为根拼接 events/summary/actions 端点
        self.assertIn('var api = "/api/v1/audit"', content)
        self.assertIn("留存说明", content)
        self.assertIn('id="au-page-size"', content)
        self.assertIn('id="au-goto-page"', content)
        self.assertIn('id="au-goto-btn"', content)

    def test_management_centers_registration(self):
        from apps.api.pages import MANAGEMENT_CENTERS
        config = MANAGEMENT_CENTERS["audit"]
        self.assertEqual(config["permission"], "core.audit_view")
        self.assertEqual(config["template"], "pms/audit_center.html")
        self.assertEqual(config["endpoint"], "/api/v1/audit/events")

    def test_sidebar_entry_visible_only_with_audit_view(self):
        # 前台可进预订中心页（view_accommodation），但看不到审计入口
        self.client.force_login(self.fd)
        page = self.client.get("/manage/reservations/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('href="/manage/audit/"', page.content.decode("utf-8"))
        # 值班经理（core.audit_view）可见"系统管理"组入口
        self.client.force_login(self.dm)
        content = self.client.get("/manage/audit/").content.decode("utf-8")
        self.assertIn("系统管理", content)
        self.assertIn('href="/manage/audit/"', content)

    def test_existing_audit_timeline_endpoint_untouched(self):
        """约束核对：既有 /api/v1/audit-timeline 端点行为不变（同门禁可用）。"""
        self._event(entity_id="tl-1")
        response = self.client.get("/api/v1/audit-timeline")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["entity_id"], "tl-1")
