"""M5-2 僵尸交互与 410 桩清理回归（派发单 GOV-DISPATCH-M3M4M5-001 · 派发单 M5-2）。

覆盖裁定落地断言：
* D5-3 甲：check_unique 失焦查重端点摘除（路由不再注册、视图/注册表/缓存助手
  随删）——旧页僵尸表单已无消费面，明文等值查重路径（模块⑤缺口 P-04）随之关闭
* 态① 残余（D4-5 同款口径）：html1.html/vip.html 僵尸写交互（录入表单/删除/修改
  按钮与挂载 JS，指向 410 旧写桩）摘除，只读数据展示保留（D5-1 乙：两页保留至
  生产迁移，只读展示不动）
* D5-9 甲：vip.html 权限口径统一——全局 perms 显隐随态①摘除一并清零，源级守卫
  阻断"非物业范围口径"回流（若未来恢复权限显隐必须走 pms_perms）
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import Resolver404, resolve

from apps.identity.test_module_gates import _grant
from hotelapp.models import Client, Vip

# M5-2 已摘除的僵尸交互标记：410 旧写端点、查重接口、挂载面与提交按钮
_ZOMBIE_MARKERS = (
    "/insert_client/",
    "/delete_client/",
    "/update_client/",
    "/insert_vip/",
    "/delete_vip/",
    "/update_vip/",
    "/check_unique/",
    "data-unique",
    "btn-add",
    "form-card",
    'type="submit" value="提交"',
    'class="btn btn-primary delete"',
    'class="btn btn-primary update"',
)

# 全局 perms 显隐（D5-9 甲禁用口径）：不得出现非 pms_perms 前缀的 perms.hotelapp
_GLOBAL_PERMS_RE = re.compile(r"(?<![A-Za-z_])perms\.hotelapp")


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
        """冻结项边界：D5-1 乙/D5-2 乙——只读入口与 reveal 双端点不得被顺带摘除。"""
        for url in (
            "/show_client/",
            "/search_client/",
            "/show_vip/",
            "/search_vip/",
            "/client/reveal_id/",
            "/vip/reveal_tel/",
        ):
            resolve(url)  # 不抛 Resolver404 即通过


class ClientReadOnlyPageTests(TestCase):
    """态① 残余：html1.html 僵尸写交互摘除，只读数据展示保留。"""

    def setUp(self):
        _grant("前台", "view_client")
        self.client.force_login(_staff_user("m52c", "前台"))

    def test_show_client_renders_readonly_page_without_write_interactions(self):
        client_obj = _make_client()
        response = self.client.get("/show_client/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        for marker in ("data-table", "page-bar"):
            self.assertIn(marker, content, f"只读展示标记 {marker} 必须保留")
        for marker in _ZOMBIE_MARKERS:
            self.assertNotIn(marker, content, f"僵尸写交互 {marker} 必须已摘除")
        self.assertContains(response, str(client_obj.pk))
        self.assertIn("11**************11", content, "脱敏展示口径不得改变")

    def test_search_client_post_flow_kept(self):
        client_obj = _make_client()
        response = self.client.post(
            "/search_client/", {"columns": "顾客id", "value": str(client_obj.pk)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(client_obj.pk), msg_prefix="POST 检索行为不得改变")


class VipReadOnlyPageTests(TestCase):
    """态① 残余：vip.html 僵尸写交互摘除，只读数据展示保留。"""

    def setUp(self):
        _grant("前台", "view_vip")
        self.client.force_login(_staff_user("m52v", "前台"))

    def test_show_vip_renders_readonly_page_without_write_interactions(self):
        vip_obj = _make_vip()
        response = self.client.get("/show_vip/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        for marker in ("data-table", "page-bar"):
            self.assertIn(marker, content, f"只读展示标记 {marker} 必须保留")
        for marker in _ZOMBIE_MARKERS:
            self.assertNotIn(marker, content, f"僵尸写交互 {marker} 必须已摘除")
        self.assertContains(response, str(vip_obj.pk))
        self.assertIn("138****5678", content, "脱敏展示口径不得改变")

    def test_search_vip_post_flow_kept(self):
        vip_obj = _make_vip()
        response = self.client.post(
            "/search_vip/", {"columns": "会员编号", "value": str(vip_obj.pk)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(vip_obj.pk), msg_prefix="POST 检索行为不得改变")


class TemplateSourceGuardTests(TestCase):
    """模板源级守卫（不依赖视图与登录态，防手改回流）。"""

    def _source(self, name):
        return (Path(settings.BASE_DIR) / "templates" / name).read_text(encoding="utf-8")

    def test_html1_html_source_has_no_zombie_interactions(self):
        source = self._source("html1.html")
        for marker in _ZOMBIE_MARKERS:
            self.assertNotIn(marker, source, f"html1.html 源码不得残留僵尸交互 {marker}")
        for marker in ("data-table", "page-bar", "mask_idcard", "/search_client/"):
            self.assertIn(marker, source, f"html1.html 源码必须保留只读展示 {marker}")

    def test_vip_html_source_has_no_zombie_interactions(self):
        source = self._source("vip.html")
        for marker in _ZOMBIE_MARKERS:
            self.assertNotIn(marker, source, f"vip.html 源码不得残留僵尸交互 {marker}")
        for marker in ("data-table", "page-bar", "mask_tel", "/search_vip/"):
            self.assertIn(marker, source, f"vip.html 源码必须保留只读展示 {marker}")

    def test_vip_html_has_no_global_perms_display(self):
        """D5-9 甲：vip.html 权限显隐统一 pms_perms 口径——全局 perms 显隐清零。

        态① 摘除后模板暂无任何权限显隐点；守卫阻断全局 perms 口径回流
        （pms_perms.hotelapp 前缀不受影响）。
        """
        source = self._source("vip.html")
        self.assertIsNone(
            _GLOBAL_PERMS_RE.search(source),
            "vip.html 不得使用全局 perms.hotelapp 显隐（D5-9 甲：统一 pms_perms 口径）",
        )
