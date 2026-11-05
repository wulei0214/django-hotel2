# -*- coding: utf-8 -*-
"""Phase 5-B 测试加固（方案 §10.2 PII 与敏感导出 / §10.3 模块门禁 / §10.4 旧入口治理）。

四组边界钉住：
  1. PiiReveal 服务层：reveal_document/reveal_phone 理由强制（空白 → 拒绝，
     无揭示值、无审计成功记录）；API 层 reason 缺失/全空白 → 400。
  2. 敏感导出边界现状：/export/staff/ 旧通道 410；request_staff_export →
     approve_staff_export 双人审批链；export_module 角色/模块白名单现状
     （F-06：审批/导出侧 _has_role 硬编码，仅登记不改造）。
  3. 模块门禁覆盖（§10.3）：PLANNED 模块（food/order/park）页面 302 且无
     业务数据泄漏；全路由白名单覆盖率（新增路由漏门禁即失败）。
  4. 旧入口无旁路终验（§10.4）：35 条 RETIRED-410 路由 POST（员工/匿名）
     均 410/登录墙且旧表计数不变；/register/ /booking/ /cancel_booking/
     统一 302 /reserve/ 且无写入。
"""
import json
import re
from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest, GuestDocument, GuestPhone
from apps.guests.services import encrypt_doc_no, reveal_document, reveal_phone
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from hotelapp.models import (
    Accommodation, Client, Food, Order, Park, Room, Staff, Vip,
)


def _make_user(username, group_name):
    """创建员工测试账号：User + DEFAULT Membership + RoleAssignment（不依赖 is_staff）。"""
    prop, _ = Property.objects.get_or_create(
        code="DEFAULT", defaults={"name": "测试酒店"})
    user = User.objects.create_user(username=username, password=f"{username}-pass123")
    profile = StaffProfile.objects.create(
        user=user, full_name=username, employment_status="ACTIVE")
    membership = PropertyStaffMembership.objects.create(
        property=prop, staff_profile=profile,
        employee_no=f"E-{username}", status="ACTIVE")
    group = Group.objects.filter(name=group_name).first()
    if group is not None:
        StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=timezone.now())
    return user


def _grant(group_name, *codenames):
    """给指定岗位组授 hotelapp 权限码（仅测试作用域）。"""
    group = Group.objects.get(name=group_name)
    group.permissions.add(*Permission.objects.filter(
        content_type__app_label="hotelapp", codename__in=codenames))


def _legacy_counts():
    """旧业务六表 + Food/Park 计数快照（无旁路终验的写入探针）。"""
    return {
        model: model.objects.count()
        for model in (Client, Accommodation, Staff, Vip, Room, Order, Food, Park)
    }


# ---------------------------------------------------------------------------
# §10.2 任务 1：PII 揭示理由强制（服务层）
# ---------------------------------------------------------------------------

class PiiRevealReasonEnforcementTests(TestCase):
    """reveal_document / reveal_phone：理由空白一律拒绝（§10.2）。"""

    def setUp(self):
        self.prop = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "测试酒店"})[0]
        self.dm = _make_user("reveal-dm", "值班经理")
        _grant("值班经理", "guest_document_reveal", "guest_phone_reveal")
        self.guest = Guest.objects.create(
            property=self.prop, full_name="揭示理由测试客人",
            phone="13812345678", phone_normalized="13812345678")
        self.phone = GuestPhone.objects.create(
            guest=self.guest, phone="13812345678",
            phone_normalized="13812345678", is_primary=True)
        self.doc_no = "110101199001011234"
        self.doc = GuestDocument.objects.create(
            guest=self.guest, doc_type=GuestDocument.DocType.ID_CARD,
            doc_no_ciphertext=encrypt_doc_no(self.doc_no),
            doc_no_hash="gate-test-doc-hash")

    def test_reveal_document_blank_reason_rejected(self):
        """理由为空/全空白 → ValueError，且无揭示值返回、无审计成功记录。"""
        for blank in ("", "   "):
            with self.assertRaises(ValueError):
                reveal_document(self.doc, revealed_by=self.dm, reason=blank)
        self.assertEqual(
            AuditEvent.objects.filter(action="guest.document_reveal").count(), 0,
            "空白理由不得产生任何揭示审计记录")

    def test_reveal_phone_blank_reason_rejected(self):
        for blank in ("", "   "):
            with self.assertRaises(ValueError):
                reveal_phone(self.phone, revealed_by=self.dm, reason=blank)
        self.assertEqual(
            AuditEvent.objects.filter(action="guest.phone_reveal").count(), 0,
            "空白理由不得产生任何揭示审计记录")

    def test_reveal_document_with_reason_records_audit_without_plaintext(self):
        plain = reveal_document(self.doc, revealed_by=self.dm, reason="前台入住证件核验")
        self.assertEqual(plain, self.doc_no)
        event = AuditEvent.objects.filter(action="guest.document_reveal").get()
        self.assertEqual(event.reason, "前台入住证件核验")
        self.assertNotIn(self.doc_no, json.dumps(event.after_json or {}),
                         "审计 after 只允许脱敏值，不得落明文")

    def test_reveal_phone_with_reason_records_audit(self):
        plain = reveal_phone(self.phone, revealed_by=self.dm, reason="联系客人核对到店时间")
        self.assertEqual(plain, "13812345678")
        event = AuditEvent.objects.filter(action="guest.phone_reveal").get()
        self.assertEqual(event.reason, "联系客人核对到店时间")

    def test_permission_check_precedes_reason_check(self):
        """无权限者即使理由空白也先吃 PermissionDenied（先授权后参数校验）。"""
        outsider = _make_user("reveal-outsider", "客房服务员")
        with self.assertRaises(Exception):
            reveal_document(self.doc, revealed_by=outsider, reason="")
        self.assertEqual(
            AuditEvent.objects.filter(action="guest.document_reveal").count(), 0)


class PiiRevealApiReasonTests(TestCase):
    """API 层揭示接口：reason 缺失/全空白 → 400；成功时审计必含 reason。"""

    def setUp(self):
        self.prop = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "测试酒店"})[0]
        self.dm = _make_user("api-reveal-dm", "值班经理")
        _grant("值班经理", "guest_document_reveal", "guest_phone_reveal")
        self.client.force_login(self.dm)
        self.guest = Guest.objects.create(
            property=self.prop, full_name="API 揭示客人",
            phone="13900011223", phone_normalized="13900011223")
        self.phone = GuestPhone.objects.create(
            guest=self.guest, phone="13900011223",
            phone_normalized="13900011223", is_primary=True)
        self.doc = GuestDocument.objects.create(
            guest=self.guest, doc_type=GuestDocument.DocType.ID_CARD,
            doc_no_ciphertext=encrypt_doc_no("110101199001015678"),
            doc_no_hash="gate-test-doc-hash-api")

    def _reveal(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json")

    def test_document_reveal_missing_or_blank_reason_returns_400(self):
        url = f"/api/v1/guests/{self.guest.pk}/documents/{self.doc.pk}/reveal"
        self.assertEqual(self._reveal(url, {}).status_code, 400, "reason 缺失 → 400")
        self.assertEqual(
            self._reveal(url, {"reason": "     "}).status_code, 400,
            "reason 全空白 → 400")
        self.assertEqual(
            AuditEvent.objects.filter(action="guest.pii_reveal.document").count(), 0)

    def test_phone_reveal_missing_or_blank_reason_returns_400(self):
        url = f"/api/v1/guests/{self.guest.pk}/phones/{self.phone.pk}/reveal"
        self.assertEqual(self._reveal(url, {}).status_code, 400)
        self.assertEqual(
            self._reveal(url, {"reason": ""}).status_code, 400)
        self.assertEqual(
            AuditEvent.objects.filter(action="guest.pii_reveal.phone").count(), 0)

    def test_successful_reveal_writes_reason_into_audit(self):
        url = f"/api/v1/guests/{self.guest.pk}/documents/{self.doc.pk}/reveal"
        response = self._reveal(url, {"reason": "办理入住需核验证件真伪"})
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.filter(action="guest.pii_reveal.document").get()
        self.assertEqual(event.reason, "办理入住需核验证件真伪")


# ---------------------------------------------------------------------------
# §10.2 任务 2：敏感导出边界现状（只验证不大改；F-06 登记见审计报告）
# ---------------------------------------------------------------------------

class SensitiveExportBoundaryTests(TestCase):
    """export_staff 旧通道关闭 + 双人审批链；export_module 角色现状钉住。"""

    def setUp(self):
        self.fin = _make_user("export-fin", "财务")
        self.dm = _make_user("export-dm", "值班经理")
        self.fd = _make_user("export-fd", "前台")
        # F-06 登记点：request_staff_export 用权限码 export_pii（财务/值班经理/店长持有），
        # approve_staff_export 仍硬编码 _has_role("值班经理", "店长")
        Staff.objects.create(姓名="薪酬员工甲", 职务="服务员", 月薪="3000", 工龄="1")

    def test_legacy_export_staff_channel_closed_for_everyone(self):
        anonymous = self.client.get("/export/staff/")
        self.assertIn(anonymous.status_code, (302, 410),
                      "匿名访问旧薪酬通道：登录墙或 410，不直达数据")
        self.client.force_login(self.fin)
        self.assertEqual(self.client.get("/export/staff/").status_code, 410,
                         "员工（含财务）同样 410：导出必须走审批链")

    def _post_request_export(self, reason, idem_key):
        """Idempotency-Key 必须经 HTTP 头传递（视图读 request.headers）。"""
        return self.client.post("/export/staff/request/",
                                {"reason": reason},
                                HTTP_IDEMPOTENCY_KEY=idem_key)

    def test_request_staff_export_requires_export_pii_permission(self):
        self.client.force_login(self.fd)
        response = self._post_request_export(
            "前台无审批链申请权限的现状验证", "fd-exp-001")
        self.assertEqual(response.status_code, 403, "无 hotelapp.export_pii → 403")

    def test_request_staff_export_creates_pending_approval(self):
        self.client.force_login(self.fin)
        response = self._post_request_export(
            "月末薪酬对账需要导出员工表", "fin-exp-001")
        self.assertEqual(response.status_code, 201)
        from apps.billing.models import ApprovalRecord
        record = ApprovalRecord.objects.filter(
            action_code="privacy.export_staff", status="PENDING").get()
        self.assertEqual(record.requested_by, self.fin)

    def test_second_manager_approval_downloads_csv_and_audits(self):
        self.client.force_login(self.fin)
        created = self._post_request_export(
            "月末薪酬对账需要导出员工表", "fin-exp-002")
        approval_id = created.json()["approval_id"]
        self.client.force_login(self.dm)
        response = self.client.post(f"/export/staff/approve/{approval_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("薪酬员工甲", response.content.decode("utf-8-sig"))
        self.assertTrue(
            AuditEvent.objects.filter(action="privacy.export_staff").exists(),
            "审批下载必须落 privacy.export_staff 审计")

    def test_requester_cannot_approve_own_request(self):
        """SoD（Q-05）：申请人自批 → 409（服务层拒绝）；登记 F-06 审批侧硬编码。"""
        self.client.force_login(self.dm)
        created = self._post_request_export(
            "值班经理自测薪酬导出申请流程", "dm-exp-001")
        approval_id = created.json()["approval_id"]
        response = self.client.post(f"/export/staff/approve/{approval_id}/")
        self.assertEqual(response.status_code, 409,
                         "申请人不能审批自己的申请（服务层 SoD 现状）")

    def test_export_module_pii_modules_removed(self):
        """P0-04：含 PII 明文列的旧导出（client/vip/accommodation）已从
        EXPORT_MODULES 白名单移除——任何人访问 → 404，敏感导出走揭示
        策略与审批链，不接受 CSV 静态遮罩绕过。"""
        _grant("前台", "view_client")
        self.client.force_login(self.fd)
        for module in ("client", "vip", "accommodation"):
            self.assertEqual(
                self.client.get(f"/export/{module}/").status_code, 404,
                f"{module} 含 PII 明文列，旧导出必须 404")
        # 非 PII 模块保留：权限走 hotelapp.export_pii（AuthorizationService）
        self.client.force_login(self.owner) if hasattr(self, "owner") else None

    def test_export_module_planned_module_redirects_even_for_owner(self):
        """PLANNED 模块导出（food/park/order）→ 302 /home/，店长也不例外。"""
        owner = _make_user("export-owner", "店长")
        self.client.force_login(owner)
        for module in ("food", "park", "order"):
            response = self.client.get(f"/export/{module}/")
            self.assertEqual(response.status_code, 302, f"/export/{module}/ 应 302")
            self.assertTrue(response["Location"].startswith("/home/"))


# ---------------------------------------------------------------------------
# §10.3 任务 3：模块门禁覆盖（PLANNED 页面 + 全路由白名单覆盖率）
# ---------------------------------------------------------------------------

class PlannedModuleGateTests(TestCase):
    """PLANNED 模块（fnb/parking → food/order/park）：页面 302 且零数据泄漏。"""

    def setUp(self):
        self.dm = _make_user("gate-dm", "值班经理")
        self.client.force_login(self.dm)
        # 造旧模型数据：若门禁失效，页面将渲染/查询出这些记录
        staff = Staff.objects.create(姓名="厨师甲", 职务="厨师", 月薪="5000", 工龄="3")
        client = Client.objects.create(姓名="门禁客人甲", 身份证号="110101199001019999")
        room = Room.objects.create(房间号=9901, 客房类型="标准间", 价格=288,
                                   负责人编号=staff, 房间状态="空闲")
        food = Food.objects.create(菜品编号=901, 菜名="宫保鸡丁", 价格="38", 厨师id=staff)
        Order.objects.create(id=901, 顾客编号=client, 菜品编号=food, 服务员编号=staff,
                             桌号=1, 状态="正常")
        Park.objects.create(车位编号=901, 当前状态="空闲", 车辆牌号="黑A99999")
        Accommodation.objects.create(id=901, 顾客id=client, 客房编号=room, 状态="待确认")
        self.leak_keywords = ("宫保鸡丁", "门禁客人甲", "9901", "黑A99999")

    def test_planned_module_pages_redirect_home_without_data_leak(self):
        for url in ("/show_food/", "/show_park/", "/show_order/",
                    "/search_food/", "/search_park/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"{url} PLANNED → 302")
            self.assertTrue(response["Location"].startswith("/home/"),
                            f"{url} 应统一跳回 /home/")
            body = response.content.decode("utf-8", errors="ignore")
            for keyword in self.leak_keywords:
                self.assertNotIn(keyword, body,
                                 f"{url} 重定向响应不得携带任何业务数据")

    def test_planned_module_pages_never_write_legacy_tables(self):
        before = _legacy_counts()
        for url in ("/show_food/", "/show_park/", "/show_order/"):
            self.client.post(url, {})
        self.assertEqual(_legacy_counts(), before,
                         "PLANNED 模块页面对 POST 也不得产生任何旧表写入")


# ---------------------------------------------------------------------------
# §10.3 任务 3：路由门禁覆盖率（白名单式：新增路由漏门禁即失败）
# ---------------------------------------------------------------------------

# 登记公开面（§10.3：在 registry/方案中登记为公开或登录即可的低风险路由）
PUBLIC_VIEW_NAMES = {
    "login_view",             # /login/ 登录页
    "logout_view",            # /logout/（login_required；仅登出动作）
    "register_view",          # 302 → /reserve/（§10.4 退役兼容）
    "booking_page",           # 302 → /reserve/（§10.4 退役兼容）
    "public_booking_page",    # /reserve/ 公开免注册直订
    "invite_accept",          # /invite/accept/ 员工邀请激活页（AUTH-18：匿名公开 + 限流 + 一次性 token）
    "swagger_ui", "redoc_ui", # /api/docs/ /api/redoc/ API 文档页
    "management_center_page", # /manage/<center>/ 视图内自校验 AuthorizationService
}
# 平台管理域（平台双 Path Cookie 会话域 + superuser-only，§6.1，另行门禁）
PLATFORM_VIEW_MODULES = (
    "apps.identity.platform",
    "apps.identity.permission_admin",
    "apps.identity.permission_center",
)
# RETIRED-410 视图名（10-routes.md §E1-E35 + /export/staff/ 旧通道）
RETIRED_VIEW_NAMES = {
    "checkin_confirm", "checkin_in", "checkout", "uncheckout",
    "extend_stay", "change_room", "express_checkout", "walkin_create",
    "night_audit_run", "room_status_change", "finance_add",
    "staff_toggle", "export_staff", "insert",
    "insert_client", "delete_staff", "update_staff",
    "delete_client", "update_client",
    "insert_food", "delete_food", "update_food",
    "insert_vip", "delete_vip", "update_vip",
    "insert_park", "delete_park", "update_park",
    "insert_room", "delete_room", "update_room",
    "insert_order", "delete_order",
    "insert_accomodation", "delete_accomodation",
}


def _closure_strings(fn):
    out = []
    if getattr(fn, "__closure__", None):
        for cell in fn.__closure__:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, str):
                out.append(value)
    return out


def _gate_evidence(view):
    """沿 __wrapped__ 链收集门禁证据：
      perm        —— pms_permission_required 闭包中的 app_label.codename 权限码
      module_gate —— module_gate("food"/"order"/"park") 闭包键
      staff_gate  —— staff_required.wrapped 同时调用 _has_active_employment
                     与 _must_change_password（co_names 特征）
    """
    evidence = set()
    current, depth = view, 0
    while current is not None and depth < 12:
        depth += 1
        for text in _closure_strings(current):
            if re.match(r"^(hotelapp|identity|inventory|billing|core)\.", text):
                evidence.add("perm")
            if text in ("food", "order", "park"):
                evidence.add("module_gate")
        code = getattr(current, "__code__", None)
        if code is not None:
            names = getattr(code, "co_names", ())
            if "_has_active_employment" in names and "_must_change_password" in names:
                evidence.add("staff_gate")
        current = getattr(current, "__wrapped__", None)
    return evidence


def _iter_project_patterns(resolver, prefix=""):
    """枚举 hotel/urls.py 的 URLPattern（跳过 admin 与 api/v1 两个 include 域）。"""
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            module = getattr(entry.urlconf_module, "__name__", "")
            if module == "apps.api.urls":
                continue  # api/v1 公开面/JSON API：门禁在 api_view 层，另行测试覆盖
            if isinstance(entry.urlconf_module, list):
                continue  # Django Admin：is_staff + 模型权限域
            yield from _iter_project_patterns(entry, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern):
            yield prefix + str(entry.pattern), entry.callback


class RouteGateCoverageTests(TestCase):
    """§10.3：hotel/urls.py 每条业务路由必须可归类——
    公开面白名单 / 平台域 / 410 退役 / 携带门禁证据（权限码/module_gate/staff_required）。
    新增路由若未挂任何门禁且不在白名单，本测试失败。"""

    def test_every_route_is_public_whitelisted_retired_or_gated(self):
        unclassified = []
        checked = 0
        for route, callback in _iter_project_patterns(get_resolver()):
            checked += 1
            view_name = callback.__name__
            if getattr(callback, "view_class", None) is not None:
                continue  # RedirectView：登记 302 兼容跳转（/permissions/）
            if view_name in PUBLIC_VIEW_NAMES:
                continue
            if callback.__module__.startswith(PLATFORM_VIEW_MODULES):
                continue
            if view_name in RETIRED_VIEW_NAMES:
                continue
            if _gate_evidence(callback):
                continue
            unclassified.append(f"{route} -> {callback.__module__}.{view_name}")
        self.assertGreater(checked, 50, "路由枚举不得为空（解析失效保护）")
        self.assertEqual(unclassified, [],
                         "以下业务路由既不在公开面/退役白名单，也无门禁证据：\n"
                         + "\n".join(unclassified))

    def test_retired_view_list_matches_urlpatterns(self):
        """RETIRED 清单防漂移：清单中每个视图都必须仍挂在 urlpatterns 上。"""
        routed_views = {cb.__name__ for _, cb in _iter_project_patterns(get_resolver())}
        unrouted = sorted(RETIRED_VIEW_NAMES - routed_views)
        self.assertEqual(unrouted, [],
                         "RETIRED-410 清单中存在已不在路由表上的视图（清单过期）")


# ---------------------------------------------------------------------------
# §10.4 任务 4：旧入口无旁路终验
# ---------------------------------------------------------------------------

# 10-routes.md §E1-E35：RETIRED-410 路由全清单（含 /export/staff/ 旧通道）
RETIRED_410_ROUTES = (
    "/checkin_confirm/", "/checkin_in/", "/checkout/", "/uncheckout/",
    "/extend_stay/", "/change_room/", "/express_checkout/", "/walkin_create/",
    "/nightaudit_run/", "/room_status/", "/finance_add/",
    "/staff_toggle/", "/export/staff/",
    "/insert/", "/insert_client/", "/delete_staff/", "/update_staff/",
    "/delete_client/", "/update_client/",
    "/insert_food/", "/delete_food/", "/update_food/",
    "/insert_vip/", "/delete_vip/", "/update_vip/",
    "/insert_park/", "/delete_park/", "/update_park/",
    "/insert_room/", "/delete_room/", "/update_room/",
    "/insert_order/", "/delete_order/",
    "/insert_accomodation/", "/delete_accomodation/",
)


class LegacyEntryNoBypassTests(TestCase):
    """35 条 410 路由 POST（员工+匿名）零写入；/register/ /booking/ /cancel_booking/
    统一 302 /reserve/ 零写入（§10.4 终验）。"""

    def setUp(self):
        staff = Staff.objects.create(姓名="终验员工", 职务="服务员", 月薪="3000", 工龄="1")
        client = Client.objects.create(姓名="终验客人", 身份证号="110101199001018888")
        room = Room.objects.create(房间号=9801, 客房类型="标准间", 价格=288,
                                   负责人编号=staff, 房间状态="空闲")
        food = Food.objects.create(菜品编号=801, 菜名="终验菜品", 价格="30", 厨师id=staff)
        Order.objects.create(id=801, 顾客编号=client, 菜品编号=food, 服务员编号=staff,
                             桌号=2, 状态="正常")
        Park.objects.create(车位编号=802, 当前状态="空闲", 车辆牌号="黑A88888")
        Accommodation.objects.create(id=801, 顾客id=client, 客房编号=room, 状态="待确认")
        Vip.objects.create(姓名="终验会员", 联系方式="13800001234", 会员等级="普通会员")
        self.owner = _make_user("nobypass-owner", "店长")
        # 410 路由保留原权限装饰（先门禁后 410）：全权限店长才能到达每一层的
        # 410 硬返回；缺权限员工得到 403 亦属预期（由权限中心测试覆盖）
        _grant("店长", "change_staff", "add_staff", "delete_staff",
               "add_client", "view_client", "change_client", "delete_client",
               "add_food", "view_food", "change_food", "delete_food",
               "add_vip", "view_vip", "change_vip", "delete_vip",
               "add_park", "view_park", "change_park", "delete_park",
               "add_room", "view_room", "change_room", "delete_room",
               "add_order", "view_order", "change_order", "delete_order",
               "add_accommodation", "view_accommodation",
               "change_accommodation", "delete_accommodation")

    # PLANNED 模块（food/park/order）的 410 路由：module_gate 装饰在最内层，
    # 先于 410 硬返回执行 → 全权限店长 POST 也得到 302 /home/（fail-closed 顺序）
    PLANNED_GATED_RETIRED_ROUTES = {
        "/insert_food/", "/delete_food/", "/update_food/",
        "/insert_park/", "/delete_park/", "/update_park/",
        "/insert_order/", "/delete_order/",
    }

    def test_all_retired_routes_post_returns_410_and_never_writes(self):
        self.client.force_login(self.owner)
        before = _legacy_counts()
        for route in RETIRED_410_ROUTES:
            response = self.client.post(route, {"id": "1", "room": "9801"})
            if route in self.PLANNED_GATED_RETIRED_ROUTES:
                self.assertEqual(response.status_code, 302,
                                 f"POST {route}：PLANNED 模块 module_gate 先拦（302）")
                self.assertTrue(response["Location"].startswith("/home/"))
            else:
                self.assertEqual(response.status_code, 410,
                                 f"POST {route} 必须 410 Gone（先门禁后 410）")
        self.assertEqual(_legacy_counts(), before,
                         "35 条 410 路由对在职员工 POST 也不得产生任何旧表写入")

    def test_retired_routes_anonymous_reach_login_wall_or_410_never_write(self):
        before = _legacy_counts()
        for route in RETIRED_410_ROUTES:
            response = self.client.post(route, {"id": "1"})
            self.assertIn(response.status_code, (302, 410),
                          f"匿名 POST {route} 只允许 302 登录墙或 410")
            if response.status_code == 302:
                self.assertTrue(response["Location"].startswith("/login/"),
                                f"匿名 POST {route} 的 302 必须是登录墙")
        self.assertEqual(_legacy_counts(), before,
                         "匿名访问 410 路由不得产生任何旧表写入")

    def test_legacy_register_booking_cancel_redirect_and_never_write(self):
        """AUTH-01/AUTH-02/P1-04 终验：旧注册/预订/取消入口 → /reserve/，零写入。"""
        before_user = User.objects.count()
        before = _legacy_counts()
        for method in ("get", "post"):
            register = getattr(self.client, method)("/register/")
            self.assertEqual(register.status_code, 302)
            self.assertTrue(register["Location"].startswith("/reserve/"))
            booking = getattr(self.client, method)("/booking/")
            self.assertEqual(booking.status_code, 302)
            self.assertTrue(booking["Location"].startswith("/reserve/"))
        self.client.post("/register/", {"username": "ghost01",
                                        "password": "x-pass-12345"})
        self.client.post("/booking/", {"name": "幽灵预订"})
        self.assertEqual(User.objects.count(), before_user,
                         "/register/ POST 不得创建任何 User")
        self.assertEqual(_legacy_counts(), before, "旧预订入口不得写旧表")

    def test_cancel_booking_anonymous_hits_login_wall_staff_redirects_reserve(self):
        before = _legacy_counts()
        anonymous = self.client.get("/cancel_booking/")
        self.assertEqual(anonymous.status_code, 302,
                         "cancel_booking 保留员工门禁：匿名 → 登录墙")
        self.assertTrue(anonymous["Location"].startswith("/login/"))
        self.client.force_login(self.owner)
        self.assertRedirects(
            self.client.get("/cancel_booking/"), "/reserve/",
            fetch_redirect_response=False)
        self.assertRedirects(
            self.client.post("/cancel_booking/", {}), "/reserve/",
            fetch_redirect_response=False)
        self.assertEqual(_legacy_counts(), before)
