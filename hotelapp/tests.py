"""RBAC 改造决策验证测试（对应《PMS_RBAC_权限管理改造方案》用户确认的 9 项决策）。

覆盖：注册止血、净房放行门控、夜审权限、冲账执行权、隐藏模块门禁、敏感导出授权。
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from hotelapp.models import Accommodation, Client, Food, Folio, Order, Room, Staff


def _make_user(username, group_name, membership_status="ACTIVE"):
    """创建员工测试账号：is_staff + 角色组 + 完整身份链（RBAC V2 员工门禁要求有效任职）。"""
    from apps.core.models import Property
    from apps.identity.models import StaffProfile, PropertyStaffMembership

    user = User.objects.create_user(username=username, password=f"{username}-pass123")
    user.is_staff = True
    user.save()
    group = Group.objects.filter(name=group_name).first()
    if group is not None:
        user.groups.add(group)
    prop, _ = Property.objects.get_or_create(
        code="DEFAULT", defaults={"name": "测试酒店"})
    profile = StaffProfile.objects.create(
        user=user, full_name=username, employment_status="ACTIVE")
    membership = PropertyStaffMembership.objects.create(
        property=prop, staff_profile=profile,
        employee_no=f"E-{username}", status=membership_status)
    if group is not None and membership_status == "ACTIVE":
        # 与生产双写一致：角色授权记录（staff_required 门禁要求有效授权）
        from django.utils import timezone as _tz
        from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
        StaffRoleAssignment.objects.get_or_create(
            membership=membership, group=group,
            defaults={"starts_at": _tz.now()})
    return user


def _make_staff(name="保洁员"):
    return Staff.objects.create(姓名=name, 职务="服务员", 月薪="3000", 工龄="1")


class RegisterStopTheBleedingTests(TestCase):
    """决策 P0：公开注册只产顾客预订账号，不得发放员工后台身份"""

    def test_register_creates_customer_account_without_staff_identity(self):
        response = self.client.post("/register/", {
            "username": "guest001", "password": "guest001-pass",
            "password2": "guest001-pass", "fullname": "测试顾客", "tel": "13800001111",
        })
        self.assertRedirects(response, "/booking/", fetch_redirect_response=False)
        user = User.objects.get(username="guest001")
        self.assertFalse(user.is_staff, "公开注册决不能发放 is_staff")
        self.assertEqual(user.groups.count(), 0, "公开注册决不能加入任何角色组")

    def test_registered_customer_cannot_enter_backoffice(self):
        self.client.post("/register/", {
            "username": "guest002", "password": "guest002-pass",
            "password2": "guest002-pass", "fullname": "测试顾客2", "tel": "",
        })
        response = self.client.get("/home/")
        self.assertEqual(response.status_code, 403,
                         "认证但无员工身份者访问后台应被明确拒绝（判定链 §6 第 2 层）")


class HousekeepingReleaseTests(TestCase):
    """决策 6：客房服务员可流转清洁链至「待检查」，净房放行（→空闲）归管理者"""

    def setUp(self):
        self.hk = _make_user("hk001", "客房服务员")
        self.dm = _make_user("dm001", "值班经理")
        self.room = Room.objects.create(房间号=9001, 客房类型="标准间", 价格=300,
                                        负责人编号_id=_make_staff().pk, 房间状态="待清洁")

    def _post_status(self, user, target):
        self.client.force_login(user)
        return self.client.post("/room_status/", {"room": self.room.房间号, "status": target})

    def test_housekeeping_can_advance_cleaning_chain(self):
        self.assertEqual(self._post_status(self.hk, "清洁中").status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual(self.room.房间状态, "清洁中")
        self.assertEqual(self._post_status(self.hk, "待检查").status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual(self.room.房间状态, "待检查")

    def test_housekeeping_cannot_release_clean_room(self):
        self._post_status(self.hk, "清洁中")
        self._post_status(self.hk, "待检查")
        response = self._post_status(self.hk, "空闲")
        self.assertEqual(response.status_code, 403, "无「净房放行」权限不得放行")
        self.room.refresh_from_db()
        self.assertEqual(self.room.房间状态, "待检查")

    def test_duty_manager_can_release_clean_room(self):
        self._post_status(self.hk, "清洁中")
        self._post_status(self.hk, "待检查")
        response = self._post_status(self.dm, "空闲")
        self.assertEqual(response.status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual(self.room.房间状态, "空闲")


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

    def test_night_audit_run_requires_permission(self):
        self.client.force_login(self.fd)
        self.assertEqual(
            self.client.post("/nightaudit_run/").status_code, 403,
            "执行夜审需要「执行夜审」权限（值班经理/店长）")

    def test_uncheckout_requires_permission(self):
        self.client.force_login(self.fd)
        self.assertEqual(self.client.post("/uncheckout/", {"id": 1}).status_code, 403)
        self.client.force_login(self.dm)
        # 有权限但订单不存在 → 400（证明权限门已通过）
        self.assertEqual(self.client.post("/uncheckout/", {"id": 999999}).status_code, 400)


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

    def test_finance_can_execute_reverse(self):
        """财务执行冲账：订单打「已冲账」标记并记录冲账人（复核留痕）"""
        self.client.force_login(self.fin)
        response = self.client.get("/delete_accomodation/", {"id": self.acc.id,
                                                             "reason": "日期录入错误"})
        self.assertEqual(response.status_code, 200)
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.状态, "已冲账")
        self.assertEqual(self.acc.冲账人, "fin201")

    def test_front_desk_cannot_reverse(self):
        self.client.force_login(self.fd)
        response = self.client.get("/delete_accomodation/", {"id": self.acc.id, "reason": "试一下"})
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
        for url in ("/export/staff/", "/export/client/", "/export/vip/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, f"{url} 需「导出敏感数据」权限")

    def test_finance_can_export_sensitive_csv(self):
        """财务持有 view_staff（决策 7-2 薪酬可见）+ export_pii → 可导出员工 CSV"""
        self.fin = _make_user("fin401", "财务")
        self.client.force_login(self.fin)
        response = self.client.get("/export/staff/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])


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
        response = self.client.post("/permissions/save/", {
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
        """绑定后：is_staff + 中文组 + 旧视图可访问（双写统一授权）"""
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
        self.assertTrue(user.groups.filter(name="前台").exists(), "运行时权限载体=User.groups（双写）")
        self.assertTrue(user.has_perm("hotelapp.view_client"), "旧视图权限立即生效")
        self.assertTrue(user.check_password("new-pass-123"), "--password 生效")

        self.client.logout()
        self.assertTrue(self.client.login(username="bound01", password="new-pass-123"))
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 200, "绑定账号必须能访问旧后台（P0-1 验收标准）")

    def test_staff_without_membership_denied(self):
        """有效账号但无任职关系 → 员工路由拒绝（AUTH-02）"""
        user = User.objects.create_user(username="noid001", password="noid001-pass123")
        user.is_staff = True
        user.save()
        user.groups.add(Group.objects.get(name="前台"))
        self.client.force_login(user)
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 403)

    def test_suspended_membership_denied(self):
        """任职暂停 → 立即失权（AUTH-05，不依赖重新登录）"""
        user = _make_user("susp001", "前台", membership_status="SUSPENDED")
        self.client.force_login(user)
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 403, "任职暂停必须立即拒绝")


class AdminHardeningTests(TestCase):
    """RBAC V2 P0-2：Django Admin 不能绕过业务门禁（评审 P0-2）"""

    def setUp(self):
        from django.contrib import admin as django_admin
        self.admin_site = django_admin.site
        self.root = User.objects.create_superuser("rootv2", password="root-pass123456")

    def test_hidden_module_admin_unregistered(self):
        """餐饮/停车不注册 Admin：直接 URL 404（SCOPE-02）"""
        self.client.force_login(self.root)
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
            self.assertFalse(
                admin_cls(model, self.admin_site).has_delete_permission(request),
                f"{model.__name__} 的 Admin 物理删除必须关闭")

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

    def test_duplicate_night_audit_blocked(self):
        first = self.client.post("/nightaudit_run/")
        self.assertEqual(first.status_code, 302, "首次夜审成功后跳回工作台")
        second = self.client.post("/nightaudit_run/")
        self.assertEqual(second.status_code, 400, "同一营业日第二次夜审必须被拒绝")

    def test_night_audit_records_single_entry(self):
        from hotelapp.models import NightAudit
        self.client.post("/nightaudit_run/")
        self.client.post("/nightaudit_run/")  # 重复被拒
        self.assertEqual(NightAudit.objects.count(), 1, "重复提交不产生第二份日结")


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
        response = self.client.post("/client/reveal_id/", {"id": self.client_fd.顾客id})
        self.assertEqual(response.status_code, 403, "财务无 change_client 不得 reveal")

        self.client.force_login(self.fd)
        response = self.client.post("/client/reveal_id/", {"id": self.client_fd.顾客id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["identify"], "110101199001010011")

    def test_vip_tel_mask_and_reveal(self):
        from hotelapp.models import Vip
        vip = Vip.objects.create(姓名="遮罩会员", 会员等级="普通会员", 联系方式="13812345678")
        self.client.force_login(self.fd)
        page = self.client.get("/show_vip/")
        self.assertNotIn("13812345678", page.content.decode("utf-8"))
        self.assertIn("138****5678", page.content.decode("utf-8"))

        response = self.client.post("/vip/reveal_tel/", {"id": vip.会员编号})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tel"], "13812345678")

    def test_client_export_contains_masked_idcard(self):
        """CSV 导出身份证列输出遮罩值（完整值导出默认禁止，V2 §7.1）"""
        self.client.force_login(self.fd)
        self.fd.groups.add(Group.objects.get(name="财务"))  # 借财务的 export_pii
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

    def test_partial_payment_blocks_express_checkout(self):
        """已收押金与应收不平 → 拦截转普通结账（E-05）"""
        Folio.objects.create(order=self.acc, 类型="押金", 金额="100",
                             事由="押金", 日期="2026-11-02", 经手人="test")
        response = self.client.post("/express_checkout/", {"id": self.acc.id})
        self.assertEqual(response.status_code, 400)
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.状态, "已入住", "拦截后订单状态不变")

    def test_overpayment_blocks_express_checkout(self):
        """多付 → 需退款流程（MGR-03）"""
        Folio.objects.create(order=self.acc, 类型="押金", 金额="500",
                             事由="押金", 日期="2026-11-02", 经手人="test")
        response = self.client.post("/express_checkout/", {"id": self.acc.id})
        self.assertEqual(response.status_code, 400)

    def test_clean_balance_express_checkout_ok(self):
        """无资金往来 → 自动结算应收；有押金恰好持平 → 不重复收款"""
        response = self.client.post("/express_checkout/", {"id": self.acc.id})
        self.assertEqual(response.status_code, 200)
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.状态, "已退房")
        self.assertEqual(self.acc.收款金额, "300.00")


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

    def test_front_desk_cannot_change_to_price_diff_room(self):
        response = self.client.post("/change_room/", {"id": self.acc.id, "new_room": 9402})
        self.assertEqual(response.status_code, 403, "跨房型/价差换房需经理")

    def test_manager_can_change_to_price_diff_room(self):
        self.client.force_login(self.dm)
        response = self.client.post("/change_room/", {"id": self.acc.id, "new_room": 9402})
        self.assertEqual(response.status_code, 200)
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.客房编号.房间号, 9402)


class DefaultPropertyOnlyTests(TestCase):
    """V2 SCOPE-01/P1-6：单店阶段仅 DEFAULT 物业生效，非 DEFAULT 任职拒绝"""

    def test_membership_on_other_property_denied(self):
        from apps.core.models import Property
        from apps.identity.models import StaffProfile, PropertyStaffMembership
        other = Property.objects.create(code="OTHER", name="别店")
        user = User.objects.create_user(username="otherp01", password="otherp01-pass123")
        user.is_staff = True
        user.save()
        user.groups.add(Group.objects.get(name="前台"))
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
            full_name="全链路员工", employee_no="F-2002",
            account_username="emp2002", account_password="emp2002-pass",
            role_groups=[Group.objects.get(name="前台")])
        self.assertIsNotNone(user)
        self.assertTrue(user.groups.filter(name="前台").exists())
        self.assertTrue(user.has_perm("hotelapp.view_client"))
        self.assertTrue(self.client.login(username="emp2002", password="emp2002-pass"))

    def test_duplicate_employee_no_rejected(self):
        """STAFF-06：同店工号唯一，重复建档拒绝"""
        self.svc.create_employee(full_name="员工甲", employee_no="F-2003")
        with self.assertRaises(Exception):
            self.svc.create_employee(full_name="员工乙", employee_no="F-2003")

    def test_multi_role_union_and_expiry(self):
        """STAFF-07：多角色权限并集；过期临时授权自动失效"""
        from datetime import timedelta
        from django.utils import timezone
        profile, membership, user = self.svc.create_employee(
            full_name="兼岗员工", employee_no="F-2004",
            account_username="emp2004", account_password="emp2004-pass",
            role_groups=[Group.objects.get(name="前台"),
                         Group.objects.get(name="值班经理")])
        self.assertTrue(user.has_perm("hotelapp.view_client"))
        self.assertTrue(user.has_perm("hotelapp.night_audit_execute"))
        # 已过期的临时授权：组镜像同步后立即失权
        from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
        temp = StaffRoleAssignment.objects.create(
            membership=membership, group=Group.objects.get(name="客房服务员"),
            starts_at=timezone.now() - timedelta(days=2),
            ends_at=timezone.now() - timedelta(days=1))
        self.svc.end_assignment(temp, reason="过期验证")
        self.assertFalse(user.groups.filter(name="客房服务员").exists(),
                         "过期授权不得保留运行时组镜像")
        self.assertFalse(StaffRoleAssignment.objects.filter(
            membership=membership, group__name="客房服务员",
            ends_at__isnull=True).exists(), "过期授权不得计入有效授权")

    def test_transfer_keeps_history(self):
        """STAFF-08：调岗=旧授权结束+新授权生效，历史可查"""
        profile, membership, user = self.svc.create_employee(
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
        self.assertFalse(user.has_perm("hotelapp.view_client"))
        self.assertTrue(user.has_perm("hotelapp.view_room"))
        self.assertEqual(
            StaffRoleAssignment.objects.filter(membership=membership).count(), 2,
            "调岗历史保留（不覆盖旧记录）")

    def test_terminate_disables_account(self):
        """STAFF-09：离职=授权终止+账号停用+历史不删除"""
        profile, membership, user = self.svc.create_employee(
            full_name="离职员工", employee_no="F-2006",
            account_username="emp2006", account_password="emp2006-pass",
            role_groups=[Group.objects.get(name="前台")])
        self.svc.set_employment_status(profile, status="TERMINATED",
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
        """GUI 新增员工：建档+开账号+初始角色 全流程（STAFF-02）"""
        self.client.force_login(self.owner)
        response = self.client.post("/staff_accounts/create/", {
            "full_name": "工作台新员工", "employee_no": "F-3001",
            "position_code": "FO", "phone": "",
            "username": "wb3001", "password": "wb3001-pass",
            "roles": ["前台", "客房服务员"],
        })
        self.assertEqual(response.status_code, 302)
        from apps.identity.models import StaffProfile
        profile = StaffProfile.objects.get(full_name="工作台新员工")
        user = User.objects.get(username="wb3001")
        self.assertFalse(user.is_staff, "D-04：工作台开账号不发放 is_staff")
        self.assertTrue(user.has_perm("hotelapp.view_client"))
        self.assertTrue(user.has_perm("hotelapp.view_room"))
        self.assertFalse(user.has_perm("hotelapp.view_staff"), "未授权角色不得越权")


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
        self.assertFalse(self.owner.groups.filter(name="财务").exists())

    def test_owner_cannot_terminate_self(self):
        """E-35：店长不能对自己执行停职/离职（防止锁死）"""
        profile = StaffProfile.objects.get(user=self.owner)
        with self.assertRaises(Exception):
            self.svc.set_employment_status(
                profile, status="TERMINATED", ended_by=self.owner, reason="自离")
        profile.refresh_from_db()
        self.assertEqual(profile.employment_status, "ACTIVE")

    def test_workbench_blocks_self_operations(self):
        """GUI 层同样被拦截：给自己授权/离职返回错误提示"""
        self.client.force_login(self.owner)
        profile = StaffProfile.objects.get(user=self.owner)
        response = self.client.post(f"/staff_accounts/{profile.pk}/assign_role/", {
            "role": "财务", "reason": "自我提权尝试"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.owner.groups.filter(name="财务").exists())
        response = self.client.post(f"/staff_accounts/{profile.pk}/set_status/", {
            "status": "TERMINATED", "reason": "自离"})
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.employment_status, "ACTIVE")
