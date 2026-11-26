"""M5-2 僵尸交互与 410 桩清理回归（派发单 GOV-DISPATCH-M3M4M5-001 · 派发单 M5-2）。

覆盖裁定落地断言：
* D5-3 甲：check_unique 失焦查重端点摘除（路由不再注册、视图/注册表/缓存助手
  随删）——旧页僵尸表单已无消费面，明文等值查重路径（模块⑤缺口 P-04）随之关闭
* 态① 收口（D4-5 同款口径）：html1.html/vip.html 的僵尸写交互先摘除（D5-1 乙），
  两页随 ARCH-03 模板退役一并删除，只读入口改为 302 → /manage/guests/、
  /manage/memberships/（ARCH-02 REDIRECT）；源级守卫随模板退役同步退役
* D5-9 甲：vip.html 权限口径统一——全局 perms 显隐随态①摘除一并清零；该页随
  ARCH-03 模板退役删除，源级守卫同步退役（若未来恢复权限显隐必须走 pms_perms）
"""

from django.test import TestCase
from django.urls import Resolver404, resolve

from apps.identity.test_module_gates import _grant
from hotelapp.models import Client, Vip


def _staff_user(username, group_name):
    """员工测试账号（复用 hotelapp.tests 既有工厂，身份链同口径）。"""
    from hotelapp.tests import _make_user

    return _make_user(username, group_name)


def _make_client(idcard="110101199001010011"):
    return Client.objects.create(姓名="治理回归客", 身份证号=idcard)


def _make_vip(tel="13812345678"):
    return Vip.objects.create(姓名="治理回归会员", 会员等级="普通会员", 联系方式=tel)


class CheckUniqueEndpointRemovedTests(TestCase):
    """D5-3 甲：check_unique 端点摘除——路由不存在（404）、视图符号随删。"""

    def test_check_unique_route_no_longer_resolves(self):
        with self.assertRaises(Resolver404):
            resolve("/check_unique/")

    def test_check_unique_returns_404_for_any_method(self):
        response = self.client.get("/check_unique/", {"model": "client", "value": "x"})
        self.assertEqual(response.status_code, 404, "查重路由摘除后必须 404")
        self.assertEqual(self.client.post("/check_unique/", {"model": "vip"}).status_code, 404)

    def test_view_symbols_removed_from_views_module(self):
        import hotelapp.views as views

        for symbol in ("check_unique", "UNIQUE_FIELDS", "_unique_field_exists"):
            self.assertFalse(
                hasattr(views, symbol), f"hotelapp.views 不应再暴露 {symbol}（已随 D5-3 甲摘除）"
            )

    def test_retained_legacy_readonly_and_reveal_routes_still_resolve(self):
        """冻结项边界：D5-1 乙/D5-2 乙——只读入口与 reveal 双端点不得被顺带摘除。

        ARCH-02 REDIRECT 后：旧只读入口仍在路由表上（改为 302 跳新中心），
        reveal 双端点行为不变。"""
        for url in (
            "/show_client/",
            "/search_client/",
            "/show_vip/",
            "/search_vip/",
            "/client/reveal_id/",
            "/vip/reveal_tel/",
        ):
            resolve(url)  # 不抛 Resolver404 即通过


class ClientRetiredRedirectTests(TestCase):
    """态① 收口：html1.html 随 ARCH-03 退役，只读入口改为 302 → /manage/guests/。

    门禁不变（无 view_client 仍 403、匿名仍走登录墙）；旧页退役后其响应体
    不再携带任何旧表客户数据（含脱敏展示面）。
    """

    def setUp(self):
        _grant("前台", "view_client")
        self.client.force_login(_staff_user("m52c", "前台"))

    def test_show_client_redirects_to_guests_center(self):
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/guests/")

    def test_search_client_redirects_to_guests_center(self):
        response = self.client.post("/search_client/", {"columns": "顾客id", "value": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/guests/")

    def test_retired_response_carries_no_legacy_client_data(self):
        client_obj = _make_client()
        response = self.client.get("/show_client/")
        body = response.content.decode("utf-8")
        self.assertNotIn(client_obj.身份证号, body, "退役入口不得输出完整身份证")
        self.assertNotIn(client_obj.姓名, body, "退役入口不得输出旧表客户名目")


class VipRetiredRedirectTests(TestCase):
    """态① 收口：vip.html 随 ARCH-03 退役，只读入口改为 302 → /manage/memberships/。

    门禁不变（无 view_vip 仍 403、匿名仍走登录墙）；旧页退役后其响应体
    不再携带任何旧表会员数据（含脱敏展示面）。
    """

    def setUp(self):
        _grant("前台", "view_vip")
        self.client.force_login(_staff_user("m52v", "前台"))

    def test_show_vip_redirects_to_memberships_center(self):
        response = self.client.get("/show_vip/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/memberships/")

    def test_search_vip_redirects_to_memberships_center(self):
        response = self.client.post("/search_vip/", {"columns": "会员编号", "value": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/memberships/")

    def test_retired_response_carries_no_legacy_vip_data(self):
        vip_obj = _make_vip()
        response = self.client.get("/show_vip/")
        body = response.content.decode("utf-8")
        self.assertNotIn(vip_obj.联系方式, body, "退役入口不得输出完整会员电话")
        self.assertNotIn(vip_obj.姓名, body, "退役入口不得输出旧表会员名目")
