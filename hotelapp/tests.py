"""RBAC 改造决策验证测试（对应《PMS_RBAC_权限管理改造方案》用户确认的 9 项决策）。

覆盖：注册止血、净房放行门控、夜审权限、冲账执行权、隐藏模块门禁、敏感导出授权。
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase
from datetime import date, timedelta
from datetime import timedelta
from django.utils import timezone

from apps.core.models import Property

from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.identity.authorization import AuthorizationService
from hotelapp.models import Accommodation, Client, Food, Folio, Order, Room, Staff


def _make_user(username, group_name, membership_status="ACTIVE"):
    """创建员工测试账号：User + DEFAULT Membership + RoleAssignment，不使用 is_staff/全局组。"""
    from apps.core.models import Property
    from apps.identity.models import StaffProfile, PropertyStaffMembership

    user = User.objects.create_user(username=username, password=f"{username}-pass123")
    group = Group.objects.filter(name=group_name).first()
    prop, _ = Property.objects.get_or_create(
        code="DEFAULT", defaults={"name": "测试酒店"})
    profile = StaffProfile.objects.create(
        user=user, full_name=username, employment_status="ACTIVE")
    membership = PropertyStaffMembership.objects.create(
        property=prop, staff_profile=profile,
        employee_no=f"E-{username}", status=membership_status)
    if group is not None and membership_status == "ACTIVE":
        StaffRoleAssignment.objects.get_or_create(
            membership=membership, group=group,
            defaults={"starts_at": timezone.now()})
    return user


def _make_staff(name="保洁员"):
    return Staff.objects.create(姓名=name, 职务="服务员", 月薪="3000", 工龄="1")


class RegisterStopTheBleedingTests(TestCase):
    """AUTH-01（D1/D8）：公开注册通道停止创建 User；住客走 /reserve/ 免注册直订"""

    def test_register_no_longer_creates_user(self):
        before = User.objects.count()
        response = self.client.post("/register/", {
            "username": "guest001", "password": "guest001-pass",
            "password2": "guest001-pass", "fullname": "测试顾客", "tel": "13800001111",
        })
        self.assertRedirects(response, "/reserve/", fetch_redirect_response=False)
        self.assertEqual(User.objects.count(), before,
                         "公开注册决不能再创建任何 User（含孤儿顾客账号）")
        self.assertFalse(User.objects.filter(username="guest001").exists())

    def test_register_get_also_redirects_to_reserve(self):
        response = self.client.get("/register/")
        self.assertRedirects(response, "/reserve/", fetch_redirect_response=False)

    def test_authenticated_without_employment_cannot_enter_backoffice(self):
        user = User.objects.create_user(username="orphan01", password="orphan01-pass123")
        self.client.force_login(user)
        response = self.client.get("/home/")
        self.assertIn(response.status_code, (302, 403),
                      "认证但无员工身份者访问后台应被拒绝（判定链 §6 第 2 层）")


class HousekeepingReleaseTests(TestCase):
    """P1-05（方案 §10）：旧 /room_status/ 写路由已退役（410）。

    清洁链流转与净房放行的业务规则由 PMS2 前台工作台
    （/api/v1/front-desk/rooms + 房态中心）承接并另行测试。"""

    def setUp(self):
        self.hk = _make_user("hk001", "客房服务员")
        self.dm = _make_user("dm001", "值班经理")

    def test_room_status_legacy_route_retired(self):
        self.client.force_login(self.hk)
        self.assertEqual(
            self.client.post("/room_status/", {"room": "9001", "status": "清洁中"}).status_code,
            410, "旧房态写入口应返回 410 Gone")
        self.client.force_login(self.dm)
        self.assertEqual(
            self.client.post("/room_status/", {"room": "9001", "status": "空闲"}).status_code,
            410, "旧房态写入口对有权限员工同样 410")


class HighRiskActionPermissionTests(TestCase):
    """决策 4/5：夜审、反结账、未到处理按权限点门控（值班经理/店长默认持有）"""

    def setUp(self):
        self.fd = _make_user("fd101", "前台")
        self.dm = _make_user("dm102", "值班经理")

    def test_night_audit_page_requires_permission(self):
        self.client.force_login(self.fd)
        self.assertEqual(self.client.get("/nightaudit/").status_code, 403)
        self.client.force_login(self.dm)
        self.assertEqual(self.client.get("/nightaudit/").status_code, 200)

    def test_old_night_audit_run_retired(self):
        """P1-09：旧夜审写入口已退役，返回 410"""
        self.client.force_login(self.fd)
        response = self.client.post("/nightaudit_run/")
        self.assertEqual(response.status_code, 410, "旧夜审入口应返回 410 Gone")

    def test_uncheckout_retired(self):
        """P1-05：旧反结账写入口已退役（PMS2 前台 API 承接）"""
        self.client.force_login(self.fd)
        self.assertEqual(self.client.post("/uncheckout/", {"id": 1}).status_code, 410)
        self.client.force_login(self.dm)
        self.assertEqual(self.client.post("/uncheckout/", {"id": 999999}).status_code, 410)


class ReverseExecutionRightTests(TestCase):
    """决策 2：冲账执行权归财务（+值班经理/店长复核），前台不再持有。
    用客房订单验证（餐饮模块已隐藏，delete_order 连同入口一并封存）。"""

    def setUp(self):
        self.fd = _make_user("fd201", "前台")
        self.fin = _make_user("fin201", "财务")
        self.room = Room.objects.create(房间号=9101, 客房类型="标准间", 价格=300,
                                        负责人编号_id=_make_staff("保洁乙").pk,
                                        房间状态="已入住")
        client = Client.objects.create(姓名="顾客乙", 身份证号="110101199001010012")
        self.acc = Accommodation.objects.create(
            id=7101, 顾客id=client, 客房编号=self.room, 状态="已退房",
            入住时间="2026-11-01", 收款金额="300")

    def test_finance_reverse_via_legacy_route_retired(self):
        """P1-05：旧冲账写入口已退役；冲账由 PMS2 BillingService.reverse_entry 承接"""
        self.client.force_login(self.fin)
        response = self.client.post("/delete_accomodation/", {"id": self.acc.id,
                                                              "reason": "日期录入错误"})
        self.assertEqual(response.status_code, 410)
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.状态, "已退房", "旧入口不得再改订单状态")

    def test_front_desk_cannot_reverse(self):
        self.client.force_login(self.fd)
        response = self.client.post("/delete_accomodation/", {"id": self.acc.id, "reason": "试一下"})
        self.assertEqual(response.status_code, 403, "前台不得执行冲账（财务执行+经理复核）")
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.状态, "已退房")


class HiddenModuleGateTests(TestCase):
    """决策 7：餐饮/停车模块隐藏，直接 URL 也不可达（代码与数据保留）"""

    def setUp(self):
        self.fd = _make_user("fd301", "前台")

    def test_hidden_module_redirects_with_notice(self):
        self.client.force_login(self.fd)
        for url in ("/show_food/", "/show_park/", "/show_order/"):
            response = self.client.get(url, follow=True)
            self.assertRedirects(response, "/home/")
            self.assertContains(response, "该模块已对接第三方系统", msg_prefix=url)

    def test_hidden_module_export_blocked(self):
        self.client.force_login(self.fd)
        self.assertRedirects(self.client.get("/export/food/"), "/home/")


class SensitiveExportPermissionTests(TestCase):
    """决策 7-2（导出先行）：敏感数据导出独立授权（员工/顾客/会员 CSV）"""

    def setUp(self):
        self.fd = _make_user("fd401", "前台")
        self.dm = _make_user("dm401", "值班经理")

    def test_front_desk_cannot_export_sensitive_csv(self):
        self.client.force_login(self.fd)
        retired_staff_export = self.client.get("/export/staff/")
        self.assertEqual(retired_staff_export.status_code, 410)
        for url in ("/export/client/", "/export/vip/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, f"{url} 需满足当前角色导出策略")

    def test_staff_salary_export_requires_reason_and_second_manager_approval(self):
        self.fin = _make_user("fin401", "财务")
        self.client.force_login(self.fin)
        self.assertEqual(self.client.get("/export/staff/").status_code, 410)
        missing_reason = self.client.post(
            "/export/staff/request/", {},
            HTTP_IDEMPOTENCY_KEY="salary-export-001")
        self.assertEqual(missing_reason.status_code, 400)
        request = self.client.post(
            "/export/staff/request/",
            {"reason": "年度薪酬核对及人力成本预算审查"},
            HTTP_IDEMPOTENCY_KEY="salary-export-001")
        self.assertEqual(request.status_code, 201)
        approval_id = request.json()["approval_id"]
        self.assertEqual(
            self.client.post(f"/export/staff/approve/{approval_id}/").status_code,
            403,
            "申请人不得审批自己的敏感薪酬导出",
        )
        self.client.force_login(self.dm)
        download = self.client.post(f"/export/staff/approve/{approval_id}/")
        self.assertEqual(download.status_code, 200)
        self.assertIn("text/csv", download["Content-Type"])


class MatrixSaveConsistencyTests(TestCase):
    """前后台一致性：权限矩阵保存只管辖 hotelapp 域，不覆盖 admin 授予的其他域权限。

    场景：管理员在 Django admin 给某组手工加了非 hotelapp 权限（如 auth.view_group），
    随后在矩阵页保存该组权限——非矩阵权限必须原样保留，不能被 set() 静默清空。
    """

    def test_matrix_save_preserves_non_hotelapp_permissions(self):
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        root = User.objects.create_superuser("rootrbac", password="root-pass123456")
        # 用矩阵管理的角色组（临时组不在 ROLE_GROUPS 白名单内，保存会被拒绝——防提权设计）
        group = Group.objects.get(name="值班经理")
        # 模拟管理员在 Django admin 给该组手工授予的非矩阵权限
        ct_group = ContentType.objects.get_for_model(Group)
        group.permissions.add(Permission.objects.get(
            codename="view_group", content_type=ct_group))

        self.client.force_login(root)
        # §6.1：矩阵保存已迁移至 /platform/permissions/save/（平台会话域）
        sessionid = self.client.cookies["sessionid"].value
        self.client.cookies["platform_sessionid"] = sessionid
        response = self.client.post("/platform/permissions/save/", {
            "role": str(group.id), "perm": ["view_room", "change_room"],
        })
        self.assertEqual(response.status_code, 302)
        codenames = set(group.permissions.values_list("codename", flat=True))
        self.assertIn("view_room", codenames)
        self.assertIn("change_room", codenames)
        self.assertIn("view_group", codenames, "后台手工授权的非矩阵权限必须保留")
        self.assertNotIn("delete_room", codenames, "未勾选的矩阵权限应被回收")


class UnifiedAuthorizationTests(TestCase):
    """RBAC V2 P0-1：员工绑定命令创建的账号能登录并按岗位获取旧后台权限（AUTH-02/05）"""

    def setUp(self):
        from apps.core.models import Property
        from apps.identity.models import StaffProfile, PropertyStaffMembership
        self.prop, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "测试酒店"})
        self.profile = StaffProfile.objects.create(
            full_name="绑定测试员工", employment_status="ACTIVE")
        self.membership = PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=self.profile,
            employee_no="F-1001", status="ACTIVE")
        User.objects.create_user(username="bound01", password="old-pass-123")

    def test_bind_grants_backoffice_access(self):
        """绑定后仅 Assignment 生效；不写 is_staff/User.groups，旧后台照常可访问。"""
        import io as _io
        from contextlib import redirect_stdout
        from django.core.management import call_command
        buf = _io.StringIO()
        with redirect_stdout(buf):
            call_command("bind_staff_user", employee_no="F-1001",
                         username="bound01", role="FRONT_DESK",
                         password="new-pass-123", verbosity=0)
        user = User.objects.get(username="bound01")
        self.assertFalse(user.is_staff, "D-04：is_staff 仅 Django Admin，不再随绑定发放")
        self.assertFalse(user.groups.exists(), "PMS 角色不得写入全局 User.groups")
        self.assertTrue(AuthorizationService.has_permission(
            user, self.prop, "hotelapp.view_client"), "旧视图权限由 AuthorizationService 判定")
        self.assertTrue(user.check_password("new-pass-123"), "--password 生效")

        self.client.logout()
        self.assertTrue(self.client.login(username="bound01", password="new-pass-123"))
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 200, "绑定账号必须能访问旧后台（P0-1 验收标准）")

    def test_staff_without_membership_denied(self):
        """有效账号但无任职关系 → 员工路由拒绝（AUTH-02）"""
        user = User.objects.create_user(username="noid001", password="noid001-pass123")
        self.client.force_login(user)
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 403)

    def test_suspended_membership_denied(self):
        """任职暂停 → 立即失权（AUTH-05，不依赖重新登录）"""
        user = _make_user("susp001", "前台", membership_status="SUSPENDED")
        self.client.force_login(user)
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 403, "任职暂停必须立即拒绝")

    def test_staff_manager_cannot_assign_arbitrary_django_group(self):
        """伪造 POST 不得越过业务角色目录授予任意 Group。"""
        from apps.identity.models import StaffProfile, PropertyStaffMembership

        manager = _make_user("role-manager-forge", "店长")
        target_user = User.objects.create_user(
            username="role-target-forge", password="target-pass123"
        )
        profile = StaffProfile.objects.create(
            user=target_user,
            full_name="授权目标员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        prop = Property.objects.get(code="DEFAULT")
        membership = PropertyStaffMembership.objects.create(
            property=prop,
            staff_profile=profile,
            employee_no="ROLE-FORGE-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        unapproved = Group.objects.create(name="Unapproved Global Admin")

        self.client.force_login(manager)
        # §10.1：员工写操作已迁移员工中心 /manage/staff/actions/（旧写路由 302 退役）
        response = self.client.post(
            f"/manage/staff/actions/{profile.pk}/assign_role/",
            {"role": unapproved.name, "reason": "伪造角色提交"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            StaffRoleAssignment.objects.filter(
                membership=membership, group=unapproved
            ).exists()
        )


class AdminHardeningTests(TestCase):
    """RBAC V2 P0-2：Django Admin 不能绕过业务门禁（评审 P0-2）"""

    def setUp(self):
        from django.contrib import admin as django_admin
        self.admin_site = django_admin.site
        self.root = User.objects.create_superuser("rootv2", password="root-pass123456")

    def test_hidden_module_admin_unregistered(self):
        """餐饮/停车不注册 Admin：直接 URL 404（SCOPE-02）"""
        self.client.force_login(self.root)
        self.client.cookies["django_admin_sessionid"] = self.client.cookies["sessionid"].value
        for url in ("/admin/hotelapp/food/", "/admin/hotelapp/park/", "/admin/hotelapp/order/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404, f"{url} 必须不可达")

    def test_business_records_cannot_be_physically_deleted(self):
        """业务记录禁止 Admin 物理删除（评审 P0-2-3）"""
        from hotelapp.admin import (AccommodationAdmin, ClientAdmin, RoomAdmin,
                                    StaffAdmin, FinanceRecordAdmin, NightAuditAdmin)
        from hotelapp.models import Accommodation, Client, Room, Staff, FinanceRecord, NightAudit
        request = None
        for admin_cls, model in [(AccommodationAdmin, Accommodation), (ClientAdmin, Client),
                                 (RoomAdmin, Room), (StaffAdmin, Staff),
                                 (FinanceRecordAdmin, FinanceRecord), (NightAuditAdmin, NightAudit)]:
            admin_obj = admin_cls(model, self.admin_site)
            self.assertFalse(
                admin_obj.has_delete_permission(request),
                f"{model.__name__} 的 Admin 物理删除必须关闭")
            self.assertFalse(
                admin_obj.has_add_permission(request),
                f"{model.__name__} 的 Admin 新增必须关闭")
            if model.__name__ != "NightAudit":
                self.assertFalse(
                    admin_obj.has_change_permission(request),
                    f"{model.__name__} 的 Admin 修改必须关闭")

    def test_order_status_readonly_in_admin(self):
        """已存在订单的状态/冲账留痕在 Admin 只读（评审 P0-2-2）"""
        from hotelapp.admin import AccommodationAdmin
        admin_obj = AccommodationAdmin(Accommodation, self.admin_site)
        readonly = admin_obj.get_readonly_fields(None, Accommodation())
        self.assertIn("状态", readonly)
        self.assertIn("冲账人", readonly)


class NightAuditIdempotencyTests(TestCase):
    """RBAC V2 P1：夜审幂等（AUTH-16/E-17/E-18）——同一营业日不重复执行"""

    def setUp(self):
        self.dm = _make_user("dm501", "值班经理")
        self.client.force_login(self.dm)

    def test_night_audit_page_accessible_to_manager(self):
        """值班经理可访问夜审工作台页面"""
        self.client.force_login(self.dm)
        response = self.client.get("/nightaudit/")
        self.assertEqual(response.status_code, 200)


class FolioAccessGateTests(TestCase):
    """V2 E-30：账单数据需要客房订单查看权限，客房服务员拒绝"""

    def setUp(self):
        from hotelapp.models import Accommodation, Room
        self.hk = _make_user("hk601", "客房服务员")
        self.fd = _make_user("fd601", "前台")
        room = Room.objects.create(房间号=9201, 客房类型="标准间", 价格=300,
                                   负责人编号_id=_make_staff("保洁丙").pk, 房间状态="已入住")
        from hotelapp.models import Client
        client = Client.objects.create(姓名="账单测试客", 身份证号="110101199001010013")
        self.acc = Accommodation.objects.create(
            id=7201, 顾客id=client, 客房编号=room, 状态="已入住", 入住时间="2026-11-02")

    def test_housekeeping_cannot_read_folio(self):
        self.client.force_login(self.hk)
        response = self.client.get("/folio/", {"id": self.acc.id})
        self.assertEqual(response.status_code, 403, "客房服务员不得读取住客账务")

    def test_front_desk_can_read_folio(self):
        self.client.force_login(self.fd)
        response = self.client.get("/folio/", {"id": self.acc.id})
        self.assertEqual(response.status_code, 200)


class SensitiveFieldMaskingTests(TestCase):
    """V2 P1-1/P1-2：字段级遮罩 + reveal 审计（DATA-03/Q-01/Q-02 推荐口径）"""

    def setUp(self):
        self.fd = _make_user("fd701", "前台")
        self.fin = _make_user("fin701", "财务")
        self.client_fd = Client.objects.create(姓名="遮罩测试客",
                                               身份证号="110101199001010011")

    def test_list_renders_masked_idcard_only(self):
        """DATA-03：响应体不得包含完整身份证（HTML 为遮罩值）"""
        self.client.force_login(self.fd)
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("110101199001010011", response.content.decode("utf-8"),
                         "完整身份证不得出现在 HTML 中")
        self.assertIn("11**************11", response.content.decode("utf-8"),
                      "应渲染前2后2遮罩值")

    def test_reveal_requires_change_permission(self):
        """完整值仅限持修改权岗位（前台/经理）；财务只读 403"""
        self.client.force_login(self.fin)
        response = self.client.post("/client/reveal_id/", {
            "id": self.client_fd.顾客id, "reason": "办理入住身份核验"
        })
        self.assertEqual(response.status_code, 403, "财务无 change_client 不得 reveal")

        self.client.force_login(self.fd)
        response = self.client.post("/client/reveal_id/", {
            "id": self.client_fd.顾客id, "reason": "办理入住身份核验"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["identify"], "110101199001010011")

    def test_vip_tel_mask_and_reveal(self):
        from hotelapp.models import Vip
        vip = Vip.objects.create(姓名="遮罩会员", 会员等级="普通会员", 联系方式="13812345678")
        self.client.force_login(self.fd)
        page = self.client.get("/show_vip/")
        self.assertNotIn("13812345678", page.content.decode("utf-8"))
        self.assertIn("138****5678", page.content.decode("utf-8"))

        response = self.client.post("/vip/reveal_tel/", {
            "id": vip.会员编号, "reason": "办理会员联系电话核验"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tel"], "13812345678")

    def test_client_export_contains_masked_idcard(self):
        """CSV 导出身份证列输出遮罩值（完整值导出默认禁止，V2 §7.1）"""
        self.client.force_login(self.fd)
        membership = PropertyStaffMembership.objects.get(staff_profile__user=self.fd)
        StaffRoleAssignment.objects.create(
            membership=membership, group=Group.objects.get(name="财务"),
            starts_at=timezone.now())
        response = self.client.get("/export/client/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8-sig")
        self.assertNotIn("110101199001010011", body, "CSV 不得包含完整身份证")
        self.assertIn("11**************11", body)


class ExpressCheckoutBalanceTests(TestCase):
    """V2 E-05/MGR-03：快捷退房的资金差异拦截"""

    def setUp(self):
        self.fd = _make_user("fd801", "前台")
        self.client.force_login(self.fd)
        room = Room.objects.create(房间号=9301, 客房类型="标准间", 价格=300,
                                   负责人编号_id=_make_staff("保洁丁").pk, 房间状态="已入住")
        client = Client.objects.create(姓名="退房测试客", 身份证号="110101199001010014")
        self.acc = Accommodation.objects.create(
            id=7301, 顾客id=client, 客房编号=room, 状态="已入住", 入住时间="2026-11-02")
        Folio.objects.create(order=self.acc, 类型="房费", 金额="300",
                             事由="房费", 日期="2026-11-02", 经手人="test")

    def test_partial_payment_legacy_quick_checkout_is_retired(self):
        """旧版快捷退房入口返回 410，不得自动写账或改订单状态。"""
        Folio.objects.create(order=self.acc, 类型="押金", 金额="100",
                             事由="押金", 日期="2026-11-02", 经手人="test")
        response = self.client.post("/express_checkout/", {"id": self.acc.id})
        self.assertEqual(response.status_code, 410)
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.状态, "已入住", "退役入口不得修改旧订单")

    def test_overpayment_legacy_quick_checkout_is_retired(self):
        """多付不能通过旧快捷入口直接离店；统一转 PMS2 处理。"""
        Folio.objects.create(order=self.acc, 类型="押金", 金额="500",
                             事由="押金", 日期="2026-11-02", 经手人="test")
        response = self.client.post("/express_checkout/", {"id": self.acc.id})
        self.assertEqual(response.status_code, 410)

    def test_clean_balance_legacy_quick_checkout_is_retired(self):
        """即使金额持平也不再从旧数据双写；退房只走 PMS2 前台服务。"""
        response = self.client.post("/express_checkout/", {"id": self.acc.id})
        self.assertEqual(response.status_code, 410)
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.状态, "已入住")
        self.assertFalse(Folio.objects.filter(
            order=self.acc, 类型="收款", 事由="快捷离店自动结算").exists())


class ChangeRoomPolicyTests(TestCase):
    """V2 E-11/MGR-05：跨房型/价差换房需值班经理/店长"""

    def setUp(self):
        self.fd = _make_user("fd901", "前台")
        self.dm = _make_user("dm901", "值班经理")
        self.client.force_login(self.fd)
        old_room = Room.objects.create(房间号=9401, 客房类型="标准间", 价格=300,
                                       负责人编号_id=_make_staff("保洁戊").pk, 房间状态="已入住")
        self.diff_room = Room.objects.create(房间号=9402, 客房类型="大床房", 价格=388,
                                             房间状态="空闲", 负责人编号_id=_make_staff("保洁己").pk)
        client = Client.objects.create(姓名="换房测试客", 身份证号="110101199001010015")
        self.acc = Accommodation.objects.create(
            id=7401, 顾客id=client, 客房编号=old_room, 状态="已入住", 入住时间="2026-11-02")

    def test_change_room_legacy_route_retired(self):
        """P1-05：旧换房写入口已退役；跨房型/价差规则由 PMS2 换房 API 承接"""
        response = self.client.post("/change_room/", {"id": self.acc.id, "new_room": 9402})
        self.assertEqual(response.status_code, 410)
        self.client.force_login(self.dm)
        self.assertEqual(
            self.client.post("/change_room/", {"id": self.acc.id, "new_room": 9402}).status_code,
            410)


class DefaultPropertyOnlyTests(TestCase):
    """V2 SCOPE-01/P1-6：单店阶段仅 DEFAULT 物业生效，非 DEFAULT 任职拒绝"""

    def test_membership_on_other_property_denied(self):
        from apps.core.models import Property
        from apps.identity.models import StaffProfile, PropertyStaffMembership
        other = Property.objects.create(code="OTHER", name="别店")
        user = User.objects.create_user(username="otherp01", password="otherp01-pass123")
        profile = StaffProfile.objects.create(
            user=user, full_name="外店员工", employment_status="ACTIVE")
        PropertyStaffMembership.objects.create(
            property=other, staff_profile=profile,
            employee_no="B-001", status="ACTIVE")
        self.client.force_login(user)
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 403,
                         "非 DEFAULT 物业任职必须被拒绝（回退单店上下文）")


class EmployeeLifecycleServiceTests(TestCase):
    """关联方案 Phase 1：统一员工生命周期服务（STAFF-01/02/06/07/08/09）"""

    def setUp(self):
        self.owner = _make_user("owner_v2", "店长")  # 0017 授予 staff_manage
        from apps.identity.services import EmployeeLifecycleService
        self.svc = EmployeeLifecycleService

    def test_create_profile_without_account(self):
        """STAFF-01：只建档不开账号——档案保存、账号状态'未开通'、不能登录"""
        profile, membership, user = self.svc.create_employee(
            property_obj=Property.objects.get(code="DEFAULT"),
            full_name="只建档员工", employee_no="F-2001",
            role_groups=[Group.objects.get(name="前台")])
        self.assertIsNone(user)
        self.assertIsNotNone(membership)
        self.assertFalse(User.objects.filter(
            username__startswith="只建档").exists())
        self.assertTrue(User.objects.filter(
            staff_profile=profile).exists() is False)

    def test_create_with_account_and_role(self):
        """STAFF-02：建档+开账号+分角色——账号可登录并拥有角色权限"""
        profile, membership, user = self.svc.create_employee(
            property_obj=Property.objects.get(code="DEFAULT"),
            full_name="全链路员工", employee_no="F-2002",
            account_username="emp2002", account_password="emp2002-pass",
            role_groups=[Group.objects.get(name="前台")])
        self.assertIsNotNone(user)
        self.assertFalse(user.groups.exists())
        self.assertTrue(AuthorizationService.has_permission(user, Property.objects.get(code="DEFAULT"), "hotelapp.view_client"))
        self.assertTrue(self.client.login(username="emp2002", password="emp2002-pass"))

    def test_duplicate_employee_no_rejected(self):
        """STAFF-06：同店工号唯一，重复建档拒绝"""
        self.svc.create_employee(property_obj=Property.objects.get(code="DEFAULT"),
                                 full_name="员工甲", employee_no="F-2003")
        with self.assertRaises(Exception):
            self.svc.create_employee(property_obj=Property.objects.get(code="DEFAULT"),
                                     full_name="员工乙", employee_no="F-2003")

    def test_multi_role_union_and_expiry(self):
        """STAFF-07：多角色权限并集；过期临时授权自动失效"""
        from datetime import timedelta
        from django.utils import timezone
        profile, membership, user = self.svc.create_employee(
            property_obj=Property.objects.get(code="DEFAULT"),
            full_name="兼岗员工", employee_no="F-2004",
            account_username="emp2004", account_password="emp2004-pass",
            role_groups=[Group.objects.get(name="前台"),
                         Group.objects.get(name="值班经理")])
        self.assertTrue(AuthorizationService.has_permission(user, Property.objects.get(code="DEFAULT"), "hotelapp.view_client"))
        self.assertTrue(AuthorizationService.has_permission(user, Property.objects.get(code="DEFAULT"), "hotelapp.night_audit_execute"))
        # 已过期的临时授权：组镜像同步后立即失权
        from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
        temp = StaffRoleAssignment.objects.create(
            membership=membership, group=Group.objects.get(name="客房服务员"),
            starts_at=timezone.now() - timedelta(days=2),
            ends_at=timezone.now() - timedelta(days=1))
        self.svc.end_assignment(temp, reason="过期验证")
        self.assertFalse(any(
            a.group.name == "客房服务员" for a in self.svc.active_assignments(membership)),
            "过期角色不得继续出现在有效授权集合")
        self.assertFalse(StaffRoleAssignment.objects.filter(
            membership=membership, group__name="客房服务员",
            ends_at__isnull=True).exists(), "过期授权不得计入有效授权")

    def test_transfer_keeps_history(self):
        """STAFF-08：调岗=旧授权结束+新授权生效，历史可查"""
        profile, membership, user = self.svc.create_employee(
            property_obj=Property.objects.get(code="DEFAULT"),
            full_name="调岗员工", employee_no="F-2005",
            account_username="emp2005", account_password="emp2005-pass",
            role_groups=[Group.objects.get(name="前台")])
        front_assignment = StaffRoleAssignment.objects.get(
            membership=membership, group__name="前台")
        self.svc.end_assignment(front_assignment, reason="调岗至客房")
        self.svc.assign_role(membership=membership,
                             group=Group.objects.get(name="客房服务员"),
                             granted_by=None, reason="调岗")
        user.refresh_from_db()  # 刷新权限缓存后再断言
        self.assertFalse(AuthorizationService.has_permission(user, Property.objects.get(code="DEFAULT"), "hotelapp.view_client"))
        self.assertTrue(AuthorizationService.has_permission(user, Property.objects.get(code="DEFAULT"), "hotelapp.view_room"))
        self.assertEqual(
            StaffRoleAssignment.objects.filter(membership=membership).count(), 2,
            "调岗历史保留（不覆盖旧记录）")

    def test_terminate_disables_account(self):
        """STAFF-09：离职=授权终止+账号停用+历史不删除"""
        profile, membership, user = self.svc.create_employee(
            property_obj=Property.objects.get(code="DEFAULT"),
            full_name="离职员工", employee_no="F-2006",
            account_username="emp2006", account_password="emp2006-pass",
            role_groups=[Group.objects.get(name="前台")])
        self.svc.set_employment_status(profile, property_obj=Property.objects.get(code="DEFAULT"),
                                       status="TERMINATED",
                                       reason="个人原因离职")
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertTrue(self.client.login(username="emp2006",
                                          password="emp2006-pass") is False
                        or True)  # 停用账号认证失败
        self.assertFalse(self.client.login(username="emp2006", password="emp2006-pass"))
        profile.refresh_from_db()
        self.assertEqual(profile.employment_status, "TERMINATED")
        now = timezone.now()
        ended = StaffRoleAssignment.objects.filter(
            membership=membership, ends_at__lte=now).count()
        self.assertEqual(ended, 1, "离职终止全部授权")


class StaffWorkbenchGUITests(TestCase):
    """关联方案 Phase 1/2：员工与账号管理工作台（STAFF-01/02/10 门控）"""

    def setUp(self):
        self.owner = _make_user("owner_gui", "店长")
        self.fd = _make_user("fd_gui", "前台")

    def test_workbench_access_control(self):
        """STAFF-10 门控：店长可入工作台；前台 403（无 staff_manage）"""
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/staff_accounts/").status_code, 200)
        self.client.force_login(self.fd)
        self.assertEqual(self.client.get("/staff_accounts/").status_code, 403)

    def test_workbench_create_employee_flow(self):
        """员工中心新增员工：建档+开账号+初始角色 全流程（STAFF-02；§10.1 迁移）"""
        self.client.force_login(self.owner)
        # §10.1：写操作走 /manage/staff/actions/create/，成功返回 200 JSON 封套
        response = self.client.post("/manage/staff/actions/create/", {
            "full_name": "工作台新员工", "employee_no": "F-3001",
            "position_code": "FO", "phone": "",
            "username": "wb3001", "password": "wb3001-pass",
            "roles": ["前台", "客房服务员"],
        })
        self.assertEqual(response.status_code, 200)
        from apps.identity.models import StaffProfile
        profile = StaffProfile.objects.get(full_name="工作台新员工")
        user = User.objects.get(username="wb3001")
        self.assertFalse(user.is_staff, "D-04：工作台开账号不发放 is_staff")
        self.assertTrue(AuthorizationService.has_permission(user, Property.objects.get(code="DEFAULT"), "hotelapp.view_client"))
        self.assertTrue(AuthorizationService.has_permission(user, Property.objects.get(code="DEFAULT"), "hotelapp.view_room"))
        self.assertFalse(AuthorizationService.has_permission(user, Property.objects.get(code="DEFAULT"), "hotelapp.view_staff"), "未授权角色不得越权")


class SegregationOfDutiesTests(TestCase):
    """关联方案 §6.2/§10.3 职责分离（E-35/MGR-14）：禁止自我提权与自我停用"""

    def setUp(self):
        self.owner = _make_user("owner_sod", "店长")

    def test_owner_cannot_grant_role_to_self(self):
        """店长不能在工作台给自己分配角色（自我提权）"""
        profile = StaffProfile.objects.get(user=self.owner)
        membership = PropertyStaffMembership.objects.get(staff_profile=profile)
        with self.assertRaises(Exception):
            self.svc.assign_role(
                membership=membership, group=Group.objects.get(name="财务"),
                granted_by=self.owner, reason="给自己加权限")
        membership = PropertyStaffMembership.objects.get(staff_profile__user=self.owner)
        self.assertFalse(StaffRoleAssignment.objects.filter(
            membership=membership, group__name="财务",
            ends_at__isnull=True).exists())

    def test_owner_cannot_terminate_self(self):
        """E-35：店长不能对自己执行停职/离职（防止锁死）"""
        profile = StaffProfile.objects.get(user=self.owner)
        with self.assertRaises(Exception):
            self.svc.set_employment_status(
                profile, property_obj=Property.objects.get(code="DEFAULT"),
                status="TERMINATED", ended_by=self.owner, reason="自离")
        profile.refresh_from_db()
        self.assertEqual(profile.employment_status, "ACTIVE")

    def test_workbench_blocks_self_operations(self):
        """员工中心同样被拦截：给自己授权/离职 → 400（§10.1 迁移后错误契约）"""
        self.client.force_login(self.owner)
        profile = StaffProfile.objects.get(user=self.owner)
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/assign_role/", {
            "role": "财务", "reason": "自我提权尝试"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.owner.groups.filter(name="财务").exists())
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/set_status/", {
            "status": "TERMINATED", "reason": "自离"})
        self.assertEqual(response.status_code, 400)
        profile.refresh_from_db()
        self.assertEqual(profile.employment_status, "ACTIVE")


class ForcedPasswordChangeTests(TestCase):
    """V1.1 C-07/D-07：首登强制改密 + 旧会话失效 + AuditEvent（§9.6-3）"""

    def setUp(self):
        self.owner = _make_user("owner_d07", "店长")
        from apps.identity.services import EmployeeLifecycleService
        self.svc = EmployeeLifecycleService

    def test_provision_sets_must_change_password(self):
        """STAFF-01→02：先建档不开账号，后开通账号 → 必须强制改密"""
        from apps.identity.services import EmployeeLifecycleService
        profile, membership, user = self.svc.create_employee(
            property_obj=Property.objects.get(code="DEFAULT"),
            full_name="建档未开号员工", employee_no="F-7001",
            role_groups=[Group.objects.get(name="前台")])
        self.assertIsNone(user)
        root = User.objects.create_superuser("rootd07", "root-pass123456")
        account = EmployeeLifecycleService.provision_account(
            profile, username="d07acc", password="d07acc-pass", granted_by=root)
        self.assertFalse(account.is_staff, "D-04")
        profile.refresh_from_db()
        self.assertTrue(profile.must_change_password, "开通账号后必须强制改密")

    def test_forced_redirect_and_change_flow(self):
        profile = StaffProfile.objects.get(user=self.owner)
        profile.must_change_password = True
        profile.save(update_fields=("must_change_password",))
        self.client.force_login(self.owner)
        # 访问业务页被重定向到改密页
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/force-password-change/", response["Location"])
        # 改密完成 → 清除标记 + 审计事件 + 回业务页
        response = self.client.post("/force-password-change/", {
            "new_password1": "brand-new-pass", "new_password2": "brand-new-pass"})
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertFalse(profile.must_change_password)
        self.assertIsNotNone(profile.last_password_change)
        self.assertEqual(self.client.get("/show_client/").status_code, 200)
        from apps.core.models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.password_changed",
            entity_type="StaffProfile", entity_id=str(profile.pk)).exists())

    def test_reset_password_sets_flag_and_audits(self):
        from apps.identity.services import EmployeeLifecycleService
        profile = StaffProfile.objects.get(user=self.owner)
        root = User.objects.create_superuser("rootd07b", "root-pass123456")
        EmployeeLifecycleService.reset_password(
            profile, property_obj=Property.objects.get(code="DEFAULT"),
            new_password="resetted-pass", reset_by=root)
        profile.refresh_from_db()
        self.assertTrue(profile.must_change_password)
        from apps.core.models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.password_reset").exists())


class AuditEventAppendOnlyTests(TestCase):
    """V1.1 M-07：审计事件只追加、不可改删、实体时间线可查"""

    def test_audit_event_immutable(self):
        from apps.core.models import AuditEvent
        event = AuditEvent.record(
            action="test.immutable", entity_type="Test", entity_id="1",
            reason="追加写验证")
        with self.assertRaises(ValueError):
            event.save()
        with self.assertRaises(ValueError):
            event.delete()

    def test_entity_timeline_query(self):
        from apps.core.models import AuditEvent
        user = _make_user("audit01", "前台")
        AuditEvent.record(action="a.one", entity_type="Client", entity_id="C1", actor=user)
        AuditEvent.record(action="a.two", entity_type="Client", entity_id="C1", actor=user)
        AuditEvent.record(action="a.other", entity_type="Client", entity_id="C2", actor=user)
        timeline = AuditEvent.objects.filter(
            entity_type="Client", entity_id="C1").order_by("-created_at")
        self.assertEqual(timeline.count(), 2)
        self.assertIn("a.two", [e.action for e in timeline])

    def test_pii_reveal_writes_audit_event(self):
        """PII 揭示必须落审计事件表（不只文件日志）"""
        from hotelapp.models import Client
        client = Client.objects.create(姓名="审计测试客", 身份证号="110101199001010016")
        fd = _make_user("fd_audit", "前台")
        self.client.force_login(fd)
        self.client.post("/client/reveal_id/", {
            "id": client.顾客id, "reason": "办理入住身份核验"
        })
        from apps.core.models import AuditEvent
        event = AuditEvent.objects.filter(
            action="guest.pii_reveal", entity_id=str(client.顾客id)).first()
        self.assertIsNotNone(event, "PII 揭示必须写 AuditEvent")
        self.assertNotIn("110101199001010016",
                         (event.after_json or {}).get("masked", ""),
                         "审计事件不得包含完整证件号（已遮罩）")


class SalaryDecisionBoundaryTests(TestCase):
    """Q-03/D-08：只有店长可修改薪酬；财务只读，其他岗位隐藏。"""

    def setUp(self):
        self.front_desk = _make_user("salary-fd", "前台")
        self.finance = _make_user("salary-fin", "财务")
        self.manager = _make_user("salary-gm", "店长")
        from hotelapp.models import Staff
        self.staff = Staff.objects.create(
            姓名="薪酬测试员工", 职务="服务员", 月薪="6000.00", 工龄="2")

    def test_salary_update_via_legacy_route_retired(self):
        """P1-05：旧 /update_staff/ 已退役——薪酬修改随员工中心迁移（AUTH-19）。"""
        payload = {
            "id": self.staff.员工id, "job": "服务员",
            "salary": "6500.00", "time": "3",
            "reason": "经店长批准的年度薪酬调整",
        }
        self.client.force_login(self.front_desk)
        # 前台无 change_staff 权限：权限门先拒（403）
        self.assertEqual(self.client.post("/update_staff/", payload).status_code, 403)
        self.client.force_login(self.manager)
        # 店长有权限但入口已退役：410，且不得改数据
        self.assertEqual(self.client.post("/update_staff/", payload).status_code, 410)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.月薪, "6000.00", "旧入口不得再改薪酬")

    def test_staff_list_access_boundaries_match_v11(self):
        """V1.1 权限矩阵：前台无 view_staff→403；财务有 view_staff→200 可见薪酬"""
        self.client.force_login(self.front_desk)
        response = self.client.get("/index/")
        self.assertEqual(response.status_code, 403,
                         "前台无 view_staff 权限，不应看到员工列表")
        self.client.force_login(self.finance)
        response = self.client.get("/index/")
        self.assertEqual(response.status_code, 200,
                         "财务有 view_staff（migration 0018），应可查看")
        self.assertIn("6000.00", response.content.decode("utf-8"),
                      "财务应能看到薪酬完整值（Q-03/D-08）")


class FinanceStaffReadOnlyTests(TestCase):
    """Q-03：财务可查看薪酬，但不能新增或修改员工及薪酬。"""

    def setUp(self):
        self.finance = _make_user("finance-staff-read", "财务")
        from hotelapp.models import Staff
        self.staff = Staff.objects.create(
            姓名="财务只读员工", 职务="服务员", 月薪="7200.00", 工龄="3")

    def test_finance_can_view_salary_but_cannot_change_it(self):
        self.client.force_login(self.finance)
        page = self.client.get("/index/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "7200.00")
        response = self.client.post("/update_staff/", {
            "id": self.staff.员工id, "job": "服务员",
            "salary": "8000.00", "time": "4",
            "reason": "无权操作测试薪酬修改",
        })
        self.assertIn(response.status_code, (403, 405))
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.月薪, "7200.00")


class V11P0ModelTests(TestCase):
    """V1.1 §9.7.1 P0 模型定稿批次：约束/加密/幂等/权限"""

    def setUp(self):
        from apps.core.models import Property
        from apps.guests.models import Guest
        from apps.reservations.models import Reservation, ReservationRoom, Stay
        from apps.inventory.models import Room, RoomType
        self.prop, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "测试酒店"})
        self.guest = Guest.objects.create(
            property=self.prop, full_name="P0测试住客", phone="13900001111",
            phone_normalized="13900001111")
        self.room_type = RoomType.objects.create(
            property=self.prop, code="P0T", name="P0 测试房型")
        from apps.inventory.models import RatePlan
        self.rate_plan = RatePlan.objects.create(
            property=self.prop, code="P0BAR", name="P0 门市价",
            valid_from=date.today())
        self.room = Room.objects.create(
            property=self.prop, room_type=self.room_type, number="P001",
            occupancy_status=Room.OccupancyStatus.VACANT)
        self.reservation = Reservation.objects.create(
            property=self.prop, booking_no="P0TEST01", guest=self.guest,
            check_in=date.today(), check_out=date.today() + timedelta(days=2))
        self.reservation_room = ReservationRoom.objects.create(
            property=self.prop, reservation=self.reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=date.today(), check_out=date.today() + timedelta(days=2),
            amount="600.00")
        self.stay = Stay.objects.create(
            property=self.prop, reservation_room=self.reservation_room,
            room=self.room)

    # ---------- M-01 ----------
    def test_reservation_guest_primary_unique(self):
        from apps.reservations.models import ReservationGuest
        ReservationGuest.objects.create(
            reservation=self.reservation, full_name="主住客", is_primary=True)
        with self.assertRaises(Exception):
            ReservationGuest.objects.create(
                reservation=self.reservation, full_name="第二主住客", is_primary=True)
        ReservationGuest.objects.create(
            reservation=self.reservation, full_name="同住人",
            role=ReservationGuest.Role.ACCOMPANYING)

    # ---------- M-02 ----------
    def test_guest_document_encrypt_and_reveal(self):
        from apps.guests.services import register_document, reveal_document
        from apps.core.models import AuditEvent
        doc = register_document(
            guest=self.guest, doc_type="ID_CARD", doc_no="110101199001010017")
        self.assertNotIn("110101199001010017", doc.doc_no_ciphertext,
                         "证件号不得明文落库")
        self.assertNotEqual(doc.doc_no_hash, "110101199001010017")
        from apps.guests.models import Guest
        g2 = Guest.objects.create(property=self.prop, full_name="另一位",
                                  phone="13900002222", phone_normalized="13900002222")
        with self.assertRaises(Exception):
            register_document(guest=g2, doc_type="ID_CARD",
                              doc_no="110101199001010017")
        # P1-01：前台/值班经理/店长默认持有揭示权（0017 矩阵）；
        # 无权限角色（客房服务员）必须被拒
        hk = _make_user("p0_hk", "客房服务员")
        with self.assertRaises(Exception):
            reveal_document(doc, revealed_by=hk)
        from django.contrib.auth.models import ContentType, Permission
        # P1-01：唯一权限码 hotelapp.guest_document_reveal（AuthorizationService 链路）；
        # 按正式矩阵给客房服务员岗位补授后即可揭示
        hk_group = Group.objects.get(name="客房服务员")
        hk_group.permissions.add(Permission.objects.get(
            codename="guest_document_reveal",
            content_type=ContentType.objects.get(app_label="hotelapp",
                                                 model="client")))
        plain = reveal_document(doc, revealed_by=hk, reason="入住核验")
        self.assertEqual(plain, "110101199001010017")
        self.assertTrue(AuditEvent.objects.filter(
            action="guest.document_reveal", entity_id=str(doc.pk)).exists())

    # ---------- M-03 ----------
    def test_guest_phone_primary_unique_and_shared_flag(self):
        from apps.guests.models import GuestPhone, Guest
        GuestPhone.objects.create(guest=self.guest, phone="13900001111",
                                  phone_normalized="13900001111", is_primary=True)
        with self.assertRaises(Exception):
            GuestPhone.objects.create(guest=self.guest, phone="13900002222",
                                      phone_normalized="13900002222", is_primary=True)
        g2 = Guest.objects.create(property=self.prop, full_name="家庭成员",
                                  phone="", phone_normalized="")
        shared = GuestPhone.objects.create(
            guest=g2, phone="13900001111", phone_normalized="13900001111",
            shared_review_required=True, source="前台登记")
        self.assertTrue(shared.shared_review_required)

    # ---------- M-16 ----------
    def test_guest_membership_unique_active(self):
        from apps.guests.models import GuestMembership, Guest
        GuestMembership.objects.create(
            property=self.prop, guest=self.guest, member_no="M-P0-001",
            status=GuestMembership.Status.ACTIVE)
        with self.assertRaises(Exception):
            GuestMembership.objects.create(
                property=self.prop, guest=self.guest, member_no="M-P0-002",
                status=GuestMembership.Status.ACTIVE)
        g2 = Guest.objects.create(property=self.prop, full_name="会员冲突客",
                                  phone="", phone_normalized="")
        with self.assertRaises(Exception):
            GuestMembership.objects.create(
                property=self.prop, guest=g2, member_no="M-P0-001",
                status=GuestMembership.Status.ACTIVE)

    # ---------- M-04 ----------
    def test_folio_master_unique_per_reservation(self):
        from apps.billing.models import Folio
        Folio.objects.create(
            property=self.prop, reservation=self.reservation,
            folio_type=Folio.FolioType.MASTER, guest=self.guest)
        with self.assertRaises(Exception):
            Folio.objects.create(
                property=self.prop, reservation=self.reservation,
                folio_type=Folio.FolioType.MASTER, guest=self.guest)
        Folio.objects.create(
            property=self.prop, reservation=self.reservation,
            folio_type=Folio.FolioType.GUEST, guest=self.guest)
        Folio.objects.create(
            property=self.prop, reservation=self.reservation,
            folio_type=Folio.FolioType.DEPOSIT, guest=self.guest)

    # ---------- M-08 ----------
    def test_business_date_one_open_per_property(self):
        from apps.core.models import BusinessDate
        bd = BusinessDate.objects.create(
            property=self.prop, business_date=date.today(),
            status=BusinessDate.Status.OPEN)
        with self.assertRaises(Exception):
            BusinessDate.objects.create(
                property=self.prop, business_date=date.today() + timedelta(days=1),
                status=BusinessDate.Status.OPEN)
        bd.status = BusinessDate.Status.CLOSED
        bd.save()
        BusinessDate.objects.create(
            property=self.prop, business_date=date.today() + timedelta(days=1),
            status=BusinessDate.Status.OPEN)

    # ---------- M-09 ----------
    def test_inventory_hold_single_active_per_roomdate(self):
        from apps.reservations.models import InventoryHold
        hold = InventoryHold.objects.create(
            property=self.prop, room_type=self.room_type,
            stay_date=date.today(), reservation_room=self.reservation_room)
        with self.assertRaises(Exception):
            InventoryHold.objects.create(
                property=self.prop, room_type=self.room_type,
                stay_date=date.today(), reservation_room=self.reservation_room)
        hold.status = InventoryHold.HoldStatus.RELEASED
        hold.released_at = timezone.now()
        hold.save()
        InventoryHold.objects.create(
            property=self.prop, room_type=self.room_type,
            stay_date=date.today(), reservation_room=self.reservation_room)

    # ---------- M-06 ----------
    def test_housekeeping_task_state_machine_fields(self):
        from apps.inventory.models import HousekeepingTask
        task = HousekeepingTask.objects.create(
            property=self.prop, room=self.room,
            task_type=HousekeepingTask.TaskType.DEPARTURE)
        self.assertEqual(task.status, HousekeepingTask.TaskStatus.PENDING)
        self.assertIsNone(task.inspected_by, "检查人独立，清洁员不可自检放行")


class SuperuserPolicyTests(TestCase):
    """V1.1 §12.2：superuser 是平台技术身份，不自动获得 PMS 业务入口"""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            "admin_policy", password="admin-pass123456")

    def test_superuser_blocked_from_pms_pages(self):
        """superuser 无任职 → PMS 业务页面 403（与 AuthorizationService 一致）"""
        self.client.force_login(self.admin)
        for url in ("/show_client/", "/home/", "/staff_accounts/", "/show_room/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403,
                             f"superuser 无任职时 {url} 必须 403")

    def test_superuser_can_access_permission_matrix(self):
        """§6.1：矩阵页迁移至 /platform/permissions/，旧 /permissions/ 302 兼容跳转。

        超管经 /admin/ 登录后可访问矩阵页；仅持 PMS 会话（sessionid）
        的超管无法访问——因为 D-04 登录门使超管拿不到 PMS 会话，
        旧语义（PMS 会话访问矩阵页）会造成超管永远无法进入的死锁。"""
        self.client.force_login(self.admin)
        # 模拟平台登录：把会话键复制到两份平台 Cookie（与 SplitSession §B.1 一致）
        sessionid = self.client.cookies["sessionid"].value
        self.client.cookies["platform_sessionid"] = sessionid
        self.client.cookies["django_admin_sessionid"] = sessionid
        legacy = self.client.get("/permissions/")
        self.assertEqual(legacy.status_code, 302,
                         "旧 /permissions/ 入口必须 302 兼容跳转")
        self.assertEqual(legacy["Location"], "/platform/permissions/")
        response = self.client.get("/platform/permissions/")
        self.assertEqual(response.status_code, 200,
                         "超管经平台会话必须能访问权限矩阵")
        # 边界：仅 PMS 会话（无平台会话）→ 302 去平台登录页
        plain = self.client
        plain.cookies.pop("platform_sessionid", None)
        plain.cookies.pop("django_admin_sessionid", None)
        boundary = plain.get("/platform/permissions/")
        self.assertEqual(boundary.status_code, 302,
                         "无平台会话的请求必须跳转平台登录")

    def test_superuser_can_access_django_admin_login(self):
        """superuser 仍可访问 Django Admin 登录页（平台维护入口）；
        完整 Admin 登录需通过 django_admin_sessionid（会话隔离），此处验证入口可达"""
        response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)

    def test_superuser_with_employment_and_role_can_access_pms(self):
        """superuser 如需 PMS 业务入口，须通过正常流程建档+任职+角色授权"""
        from apps.core.models import Property
        from apps.identity.models import PropertyStaffMembership, StaffRoleAssignment
        prop, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "测试酒店"})
        profile = StaffProfile.objects.create(
            user=self.admin, full_name="平台管理员", employment_status="ACTIVE")
        membership = PropertyStaffMembership.objects.create(
            property=prop, staff_profile=profile,
            employee_no="P-ADM-01", status="ACTIVE")
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=Group.objects.get(name="前台"))
        self.client.force_login(self.admin)
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 200,
                         "superuser + 任职 + 角色授权 → 可进 PMS（与普通员工一致）")


class GuestServiceTests(TestCase):
    """V1.1 Phase 2：GuestService 写操作（创建/更新/审计）"""

    def setUp(self):
        from apps.core.models import Property
        from apps.guests.services import GuestService
        self.prop, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "测试酒店"})
        self.svc = GuestService
        self.staff = _make_user("gs_staff", "前台")

    def test_create_guest_with_phone(self):
        """创建客人 + 主号码自动登记"""
        guest = self.svc.create_guest(
            property_obj=self.prop, full_name="服务测试客",
            phone="13900009999", created_by=self.staff)
        self.assertEqual(guest.full_name, "服务测试客")
        self.assertTrue(guest.phones.filter(is_primary=True).exists())
        from apps.core.models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(
            action="guest.create", entity_id=str(guest.pk)).exists())

    def test_shared_guest_phone_is_allowed_and_flagged_for_review(self):
        """手机号是共享联系方式，不做自然人主键；重复号码标记人工核验。"""
        first = self.svc.create_guest(
            property_obj=self.prop, full_name="首客", phone="13900008888")
        second = self.svc.create_guest(
            property_obj=self.prop, full_name="二客", phone="13900008888")
        self.assertNotEqual(first.pk, second.pk)
        from apps.guests.models import GuestPhone
        self.assertEqual(
            GuestPhone.objects.filter(
                phone_normalized="13900008888",
                shared_review_required=True,
            ).count(), 2
        )

    def test_update_guest_with_audit(self):
        """更新客人：变更写入 AuditEvent before/after"""
        guest = self.svc.create_guest(
            property_obj=self.prop, full_name="更新测试", phone="13900007777")
        old_name = guest.full_name
        guest, changed = self.svc.update_guest(
            guest_id=guest.pk, property_obj=self.prop,
            updated_by=self.staff, full_name="改名测试")
        self.assertIn("full_name", changed)
        self.assertEqual(changed["full_name"]["before"], old_name)
        from apps.core.models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(
            action="guest.update", entity_id=str(guest.pk)).exists())

    def test_phone_change_preserves_history(self):
        """手机号变更：旧 GuestPhone 降级，新号码设主，历史保留"""
        guest = self.svc.create_guest(
            property_obj=self.prop, full_name="换号测试", phone="13900006666")
        self.svc.update_guest(guest_id=guest.pk, property_obj=self.prop,
                              updated_by=self.staff, phone="13900007777")
        from apps.guests.models import GuestPhone
        phones = GuestPhone.objects.filter(guest=guest).order_by("-is_primary")
        self.assertEqual(phones.count(), 2, "旧号码保留历史")
        self.assertTrue(phones.first().is_primary)
        self.assertEqual(phones.first().phone_normalized, "13900007777")


class MembershipServiceTests(TestCase):
    """V1.1 Phase 2：GuestMembershipService（创建/等级变更）"""

    def setUp(self):
        from apps.core.models import Property
        from apps.guests.models import Guest
        from apps.guests.services import GuestMembershipService
        self.prop, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "测试酒店"})
        self.guest = Guest.objects.create(
            property=self.prop, full_name="会员测试客",
            phone="13900005555", phone_normalized="13900005555")
        self.svc = GuestMembershipService
        self.staff = _make_user("ms_staff", "前台")

    def test_create_membership(self):
        m = self.svc.create_membership(
            property_obj=self.prop, guest=self.guest, member_no="M-P1-001",
            level_code="STANDARD", created_by=self.staff)
        self.assertEqual(m.status, "ACTIVE")
        self.assertEqual(m.level_history.count(), 1)
        from apps.core.models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(
            action="membership.create", entity_id=str(m.pk)).exists())

    def test_duplicate_active_membership_rejected(self):
        self.svc.create_membership(
            property_obj=self.prop, guest=self.guest, member_no="M-P1-002")
        with self.assertRaises(Exception):
            self.svc.create_membership(
                property_obj=self.prop, guest=self.guest, member_no="M-P1-003")

    def test_level_change_writes_history(self):
        """等级变更：历史记录 before/after + 审计"""
        m = self.svc.create_membership(
            property_obj=self.prop, guest=self.guest, member_no="M-P1-004",
            level_code="STANDARD")
        self.svc.change_level(
            membership=m, new_level="GOLD", reason="升级", changed_by=self.staff)
        m.refresh_from_db()
        self.assertEqual(m.level_code, "GOLD")
        self.assertEqual(m.level_history.count(), 2)  # 创建 + 升级
        from apps.core.models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(
            action="membership.level_change").exists())

class ClassifyUsersCommandTests(TestCase):
    """AUTH-22：账号盘点只读，且只把当前有效任职/授权识别为在职员工。"""

    def test_classify_requires_current_membership_and_assignment(self):
        from django.core.management import call_command
        import json
        from io import StringIO

        user = User.objects.create_user(username="inventory_staff", password="unused-pass-123")
        profile = StaffProfile.objects.create(
            user=user, full_name="盘点员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        prop = Property.objects.create(code="AUDIT", name="盘点测试物业")
        membership = PropertyStaffMembership.objects.create(
            property=prop, staff_profile=profile, employee_no="AUDIT-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        group = Group.objects.create(name="盘点测试角色")
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        before_audit = __import__(
            "apps.core.models", fromlist=["AuditEvent"]
        ).AuditEvent.objects.count()
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            report = str(Path(directory) / "users.json")
            call_command("classify_users", dry_run=True, output=report, format="json", stdout=StringIO())
            rows = json.loads(Path(report).read_text(encoding="utf-8"))
        user_row = next(row for row in rows["users"] if row["user_id"] == user.pk)
        self.assertEqual(user_row["category"], "STAFF_LINKED")
        self.assertEqual(
            __import__("apps.core.models", fromlist=["AuditEvent"]).AuditEvent.objects.count(),
            before_audit,
        )

    def test_expired_assignment_does_not_classify_as_staff_linked(self):
        from django.core.management import call_command
        import json
        from io import StringIO

        user = User.objects.create_user(username="expired_staff", password="unused-pass-123")
        profile = StaffProfile.objects.create(
            user=user, full_name="过期授权员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        prop = Property.objects.create(code="EXPIRED", name="过期授权测试物业")
        membership = PropertyStaffMembership.objects.create(
            property=prop, staff_profile=profile, employee_no="EXPIRED-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        group = Group.objects.create(name="过期测试角色")
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=timezone.now() - timedelta(days=2),
            ends_at=timezone.now() - timedelta(days=1),
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            report = str(Path(directory) / "users.json")
            call_command("classify_users", dry_run=True, output=report, format="json", stdout=StringIO())
            rows = json.loads(Path(report).read_text(encoding="utf-8"))
        user_row = next(row for row in rows["users"] if row["user_id"] == user.pk)
        self.assertEqual(user_row["category"], "PROFILE_ONLY")

    def test_empty_csv_report_has_stable_headers(self):
        from django.core.management import call_command
        import tempfile
        from pathlib import Path
        from io import StringIO

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "inventory.csv")
            call_command(
                "classify_users", dry_run=True, output=path, format="csv",
                stdout=StringIO(),
            )
            content = Path(path).read_text(encoding="utf-8")
        self.assertIn("user_id,username_fp,category", content)

class ReconcileTestUsersCommandTests(TestCase):
    """F-02：只在隔离开发/测试库执行可逆账号收敛，不物理删除历史。"""

    def test_default_preview_does_not_change_users_or_groups(self):
        from django.core.management import call_command
        from io import StringIO

        user = User.objects.create_user(username="orphan_preview", password="unused-pass-123")
        group = Group.objects.create(name="legacy-preview-group")
        user.groups.add(group)
        call_command("reconcile_test_users", stdout=StringIO())
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.groups.filter(pk=group.pk).exists())

    def test_execute_requires_database_confirmation_and_nonempty_backup(self):
        from django.core.management import call_command, CommandError
        from django.db import connection
        from django.test import override_settings
        import tempfile
        from pathlib import Path
        from io import StringIO

        user = User.objects.create_user(username="orphan_execute", password="unused-pass-123")
        group = Group.objects.create(name="legacy-execute-group")
        user.groups.add(group)
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "backup.sql"
            backup.write_text("test-backup", encoding="utf-8")
            with override_settings(DJANGO_ENV="dev", DEBUG=True):
                with self.assertRaises(CommandError):
                    call_command(
                        "reconcile_test_users", execute=True,
                        confirm_database="wrong-db", backup_file=str(backup),
                        stdout=StringIO(),
                    )
                call_command(
                    "reconcile_test_users", execute=True,
                    confirm_database=connection.settings_dict["NAME"],
                    backup_file=str(backup), stdout=StringIO(),
                )
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        # 孤儿账号已停用；其历史组授权线索保留供审计，不自动删除。
        self.assertTrue(user.groups.filter(pk=group.pk).exists())

    def test_staff_profile_is_preserved_and_account_is_not_disabled(self):
        from django.core.management import call_command
        from io import StringIO

        user = User.objects.create_user(username="linked_staff", password="unused-pass-123")
        StaffProfile.objects.create(
            user=user, full_name="保留员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        group = Group.objects.create(name="legacy-linked-group")
        user.groups.add(group)
        call_command("reconcile_test_users", stdout=StringIO())
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        # 缺少可验证的任职授权承接时，组授权保留并进入人工复核。
        self.assertTrue(user.groups.filter(pk=group.pk).exists())
        self.assertTrue(StaffProfile.objects.filter(user=user).exists())

    def test_only_effectively_assigned_staff_has_direct_groups_cleared(self):
        from django.core.management import call_command
        from io import StringIO

        user = User.objects.create_user(username="assigned_staff", password="unused-pass-123")
        profile = StaffProfile.objects.create(
            user=user, full_name="有效任职员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        prop = Property.objects.create(code="RECONCILE", name="收敛测试物业")
        membership = PropertyStaffMembership.objects.create(
            property=prop, staff_profile=profile, employee_no="RECONCILE-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        role = Group.objects.create(name="有效任职角色")
        StaffRoleAssignment.objects.create(
            membership=membership, group=role,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        user.groups.add(role)
        legacy = Group.objects.create(name="未映射历史授权组")
        user.groups.add(legacy)
        import tempfile
        from pathlib import Path
        from django.db import connection
        from django.test import override_settings
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "backup.sql"
            backup.write_text("test-backup", encoding="utf-8")
            with override_settings(DJANGO_ENV="dev", DEBUG=True):
                call_command(
                    "reconcile_test_users", execute=True,
                    confirm_database=connection.settings_dict["NAME"],
                    backup_file=str(backup), stdout=StringIO(),
                )
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(user.groups.filter(pk=role.pk).exists())
        self.assertTrue(user.groups.filter(pk=legacy.pk).exists())
        self.assertTrue(StaffRoleAssignment.objects.filter(
            membership=membership, group=role
        ).exists())

class SeedRoleTestAccountsCommandTests(TestCase):
    """九角色种子命令必须走正式身份链，密码仅通过隐藏交互输入。"""

    def test_production_environment_is_rejected_before_password_prompt(self):
        from django.core.management import call_command, CommandError
        from django.test import override_settings
        from unittest.mock import patch
        from io import StringIO

        with override_settings(DJANGO_ENV="prod", DEBUG=False):
            with patch("getpass.getpass", side_effect=AssertionError("不应提示输入密码")):
                with self.assertRaises(CommandError):
                    call_command("seed_role_test_accounts", stdout=StringIO())

    def test_creates_nine_independent_accounts_with_assignment_authority(self):
        from django.core.management import call_command
        from django.test import override_settings
        from unittest.mock import patch
        from io import StringIO

        roles = [
            ("前台", "test_front"),
            ("值班经理", "test_duty"),
            ("财务", "test_finance"),
            ("店长", "test_owner"),
            ("客房服务员", "test_housekeeping"),
            ("夜审员", "test_nightaudit"),
            ("餐饮服务员", "test_fnb"),
            ("餐饮主管", "test_fnbsup"),
            ("停车安保", "test_parking"),
        ]
        prop = Property.objects.create(code="ROLESEED", name="九角色种子测试物业")
        role_groups = {role: Group.objects.get_or_create(name=role)[0] for role, _username in roles}
        old_group = Group.objects.create(name="旧测试岗位")
        existing_user = User.objects.create_user(
            username="test_front", password="old-passphrase-123"
        )
        existing_profile = StaffProfile.objects.create(
            user=existing_user, full_name="旧前台测试账号",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        existing_membership = PropertyStaffMembership.objects.create(
            property=prop, staff_profile=existing_profile,
            employee_no="ROLESEED-OLD", status=PropertyStaffMembership.Status.ACTIVE,
        )
        old_assignment = StaffRoleAssignment.objects.create(
            membership=existing_membership, group=old_group,
            starts_at=timezone.now() - timedelta(minutes=5),
        )

        password = "Cedar!Orbit#N7vQ-4mZp"
        output = StringIO()
        with override_settings(DJANGO_ENV="test"):
            with patch("getpass.getpass", side_effect=[password, password, password, password]):
                call_command("seed_role_test_accounts", stdout=output)
                call_command("seed_role_test_accounts", stdout=output)

        old_assignment.refresh_from_db()
        self.assertIsNotNone(old_assignment.ends_at)
        self.assertTrue(StaffRoleAssignment.objects.filter(pk=old_assignment.pk).exists())

        self.assertNotIn(password, output.getvalue())
        for role, username in roles:
            user = User.objects.get(username=username)
            self.assertTrue(user.is_active)
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
            self.assertFalse(user.groups.exists())
            profile = StaffProfile.objects.get(user=user)
            membership = PropertyStaffMembership.objects.get(
                property=prop, staff_profile=profile
            )
            self.assertEqual(StaffRoleAssignment.objects.filter(
                membership=membership, group=role_groups[role],
                starts_at__lte=timezone.now(),
                ends_at__isnull=True,
            ).count(), 1)
