"""RBAC 改造决策验证测试（对应《PMS_RBAC_权限管理改造方案》用户确认的 9 项决策）。

覆盖：注册止血、净房放行门控、夜审权限、冲账执行权、隐藏模块门禁、敏感导出授权。
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase

from hotelapp.models import Accommodation, Client, Food, Order, Room, Staff


def _make_user(username, group_name):
    """创建员工测试账号（is_staff=True 才能进员工后台）并授予指定角色组"""
    user = User.objects.create_user(username=username, password=f"{username}-pass123")
    user.is_staff = True
    user.save()
    group = Group.objects.filter(name=group_name).first()
    if group is not None:
        user.groups.add(group)
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
        self.assertEqual(response.status_code, 302, "无员工身份者访问后台应被弹回登录页")
        self.assertIn("/login/", response["Location"])


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
