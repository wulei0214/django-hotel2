"""ARCH-02 / ARCH-03 旧路由与旧模板退役契约测试（ARCH-02/03 处置矩阵批次）。

覆盖 ARCH-02 旧路由处置矩阵 §三 REDIRECT 与 ARCH-03 旧模板处置矩阵 §二 的
落地终态，断言以下四类事实：

1. **重定向目标**：10 条旧只读入口一律 302 → 对应 PMS2 新管理中心，目标逐条钉死；
2. **门禁不退化**：退役的是旧页渲染能力，不是访问控制——匿名仍走登录墙、
   无对应权限码仍 403、重定向路由仍携带权限码与员工门禁证据；
3. **旧模板退役**：ARCH-03 判定 DELETE/MIGRATE 的 8 个旧模板文件确已从
   templates/ 移除；同时以 KEEP 模板作对照面，防止顺带删除；
4. **零覆盖缺口**：ARCH-02 §七.1 登记的 ``search_staff/`` 唯一零覆盖路由，
   本文件补齐明确断言。

旧读路径的业务数据等价断言不在本文件（见 apps/api/test_legacy_equivalence.py：
E2/E3/E4 归档通道与旧直读逐字段恒等），本文件只钉退役契约本身。
"""

from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import resolve

from apps.identity.test_module_gates import _gate_evidence, _grant
from hotelapp.models import Client, Vip

# ARCH-02 §三 REDIRECT 10 条：
# (旧 URL, 新中心 URL, 该入口原权限码 codename, 目标中心额外门禁码)
# 第 4 项是「目标中心比旧入口更严」的差额门禁：给出时旧入口必须同时要求它，
# 以守住「旧入口放行 ⇒ 新中心放行」不变式（否则 302 会把人送进 403 死胡同）。
REDIRECT_CASES = (
    ("/checkin/legacy/", "/checkin/", "view_accommodation", None),
    ("/index/", "/manage/staff/", "view_staff", "identity.staff_manage"),
    ("/search_staff/", "/manage/staff/", "view_staff", "identity.staff_manage"),
    ("/show_client/", "/manage/guests/", "view_client", None),
    ("/search_client/", "/manage/guests/", "view_client", None),
    ("/show_vip/", "/manage/memberships/", "view_vip", None),
    ("/search_vip/", "/manage/memberships/", "view_vip", None),
    ("/search_room/", "/manage/rooms/", "view_room", None),
    ("/search_accomodation/", "/manage/reservations/", "view_accommodation", None),
    ("/show_accomodation/", "/manage/reservations/", "view_accommodation", None),
)

# ARCH-03 §二：DELETE（零引用）2 个 + MIGRATE（先迁内部链接、随路由重定向后删页）6 个
RETIRED_TEMPLATES = (
    "booking.html",
    "nightaudit.html",
    "index.html",
    "html1.html",
    "vip.html",
    "room.html",
    "accomodation.html",
    "checkin.html",
)

# ARCH-03 §二 KEEP 对照面：无 PMS2 替代中心的 3 个旧模板 + 共享片段 + 现行页面模板
KEPT_TEMPLATES = (
    "html4.html",
    "park.html",
    "order.html",
    "403.html",
    "_sidebar.html",
    "_ux_assets.html",
    "home.html",
    "login.html",
    "frontdesk.html",
    "force_password_change.html",
    "staff_accounts.html",
    "staff_accounts_detail.html",
)


def _make_user(username, group_name):
    """员工测试账号（复用 hotelapp.tests 既有工厂，身份链同口径）。"""
    from hotelapp.tests import _make_user as _factory

    return _factory(username, group_name)


def _grant_identity(group_name, *codenames):
    """给岗位组授予 identity.* 权限码（_grant 只覆盖 hotelapp.*）。"""
    group = Group.objects.get(name=group_name)
    group.permissions.add(
        *Permission.objects.filter(content_type__app_label="identity", codename__in=codenames)
    )


class RetiredRouteRedirectTests(TestCase):
    """退役路由的 302 跳转目标与门禁终态。"""

    def setUp(self):
        _grant(
            "前台",
            "view_staff",
            "view_client",
            "view_vip",
            "view_room",
            "view_accommodation",
        )
        # 「旧入口放行 ⇒ 新中心放行」不变式的前置条件：测试用户同时持有
        # 目标中心的门禁码（/manage/staff/ 要求 identity.staff_manage）。
        _grant_identity("前台", "staff_manage")
        self.user = _make_user("arch-retire", "前台")
        self.client.force_login(self.user)

    def test_every_retired_route_redirects_to_its_new_center(self):
        """10 条 REDIRECT 入口一律 302，且 Location 指向矩阵登记的新中心。"""
        for old_url, new_url, _codename, _extra in REDIRECT_CASES:
            with self.subTest(old_url=old_url):
                response = self.client.get(old_url)
                self.assertEqual(response.status_code, 302, f"{old_url} 必须 302")
                self.assertEqual(response["Location"], new_url, f"{old_url} 跳转目标不符")

    def test_redirect_target_is_reachable_for_permitted_user(self):
        """跟随重定向后不得落在 403 死胡同（旧入口放行 ⇒ 新中心放行）。"""
        for old_url, new_url, _codename, _extra in REDIRECT_CASES:
            with self.subTest(old_url=old_url):
                response = self.client.get(old_url, follow=True)
                self.assertNotEqual(
                    response.status_code,
                    403,
                    f"{old_url} → {new_url} 落到 403：旧入口放行但新中心无权（死胡同）",
                )
                self.assertLessEqual(
                    len(response.redirect_chain),
                    1,
                    f"{old_url} 出现链式重定向：{response.redirect_chain}",
                )

    def test_search_staff_redirect_is_explicitly_covered(self):
        """ARCH-02 §七.1：search_staff/ 此前零测试覆盖——本条补齐明确断言。

        该入口由旧员工列表页内部调用（模板链接已先迁到 /manage/staff/），
        退役后仍须落到员工与账号中心。
        """
        response = self.client.get("/search_staff/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/staff/")

    def test_redirect_targets_are_resolvable_management_centers(self):
        """跳转目标必须是仍注册在路由表上的真实中心（不得指向已下线页面）。"""
        for _old_url, new_url, _codename, _extra in REDIRECT_CASES:
            with self.subTest(center=new_url):
                self.assertTrue(resolve(new_url).func, f"{new_url} 必须是有效中心入口")

    def test_retired_routes_render_no_legacy_template(self):
        """旧路由不再渲染任何旧模板（302 响应不产出模板上下文）。"""
        for old_url, _new_url, _codename, _extra in REDIRECT_CASES:
            with self.subTest(old_url=old_url):
                response = self.client.get(old_url)
                self.assertEqual(response.status_code, 302)
                rendered = [t.name for t in response.templates if t.name]
                self.assertEqual(rendered, [], f"{old_url} 不得再渲染旧模板")

    def test_retired_routes_leak_no_legacy_table_data(self):
        """旧页退役后其响应体不得携带旧表业务数据（含 PII）。"""
        guest = Client.objects.create(姓名="退役回归客", 身份证号="110101199001010011")
        member = Vip.objects.create(
            姓名="退役回归会员", 会员等级="普通会员", 联系方式="13812345678"
        )
        secrets = (guest.姓名, guest.身份证号, member.姓名, member.联系方式)
        for old_url, _new_url, _codename, _extra in REDIRECT_CASES:
            with self.subTest(old_url=old_url):
                body = self.client.get(old_url).content.decode("utf-8")
                for secret in secrets:
                    self.assertNotIn(secret, body, f"{old_url} 不得输出旧表数据")

    def test_anonymous_hits_login_wall_on_every_retired_route(self):
        """匿名访问退役入口仍走登录墙（不得因改重定向而降级为公开面）。"""
        for old_url, _new_url, _codename, _extra in REDIRECT_CASES:
            with self.subTest(old_url=old_url):
                self.client.logout()
                response = self.client.get(old_url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])
                self.client.force_login(self.user)

    def test_route_without_permission_still_gets_403(self):
        """无对应权限码仍 403（退役不放宽权限面）。"""
        for old_url, _new_url, codename, _extra in REDIRECT_CASES:
            with self.subTest(old_url=old_url):
                group = Group.objects.get(name="前台")
                group.permissions.remove(
                    *Permission.objects.filter(
                        content_type__app_label="hotelapp", codename=codename
                    )
                )
                response = self.client.get(old_url)
                self.assertEqual(response.status_code, 403, f"{old_url} 无权限时必须 403")


class RetiredRoutePermissionEquivalenceTests(TestCase):
    """旧入口与新中心的权限等价性：旧入口不得把人引导进无权的新中心。

    实证缺陷（2026-11-26 复核）：/index/ 与 /search_staff/ 的门禁是
    hotelapp.view_staff，而 /manage/staff/ 要求 identity.staff_manage。
    财务岗位持有前者却不持有后者 → 旧入口 302 放行 → 新中心 403，
    用户被引导进死胡同，且「财务只读查看薪酬」的旧能力静默消失。
    修复口径：旧入口额外要求目标中心门禁码，缺码时直接 403，不做误导性跳转。
    """

    def setUp(self):
        # 只持旧码 hotelapp.view_staff 的岗位组（模拟财务：可读旧员工页，
        # 但无 identity.staff_manage → 无权进入 /manage/staff/）
        group = Group.objects.create(name="arch-retire-readonly")
        group.permissions.add(
            *Permission.objects.filter(content_type__app_label="hotelapp", codename="view_staff")
        )
        self.user = _make_user("arch-retire-readonly", group.name)
        self.client.force_login(self.user)

    def test_old_code_without_target_code_gets_403_not_misleading_redirect(self):
        """持旧码但不持目标中心门禁码 → 旧入口直接 403，不得 302 到 403 死胡同。"""
        for old_url in ("/index/", "/search_staff/"):
            with self.subTest(old_url=old_url):
                response = self.client.get(old_url)
                self.assertEqual(
                    response.status_code,
                    403,
                    f"{old_url} 不得 302 到无权访问的 /manage/staff/（死胡同）",
                )
                self.assertNotIn(
                    "/manage/staff/",
                    response.get("Location", ""),
                    f"{old_url} 不得把用户引导到自己无权的新中心",
                )


class RetiredRouteGateEvidenceTests(TestCase):
    """退役路由仍携带门禁证据（权限码 + 员工门禁），不退化为裸 RedirectView。

    与 apps/identity/test_module_gates.py 的全路由门禁互为对照：那里保证
    「每条路由可归类」，这里保证「本批退役的 10 条重定向仍带原门禁证据」。
    """

    def test_redirect_routes_keep_permission_and_staff_gate_evidence(self):
        for old_url, _new_url, _codename, _extra in REDIRECT_CASES:
            with self.subTest(old_url=old_url):
                evidence = _gate_evidence(resolve(old_url).func)
                self.assertIn("perm", evidence, f"{old_url} 丢失权限码门禁证据")
                self.assertIn("staff_gate", evidence, f"{old_url} 丢失员工门禁证据")


class RetiredTemplateFileTests(TestCase):
    """ARCH-03：退役模板文件确已移除；KEEP 模板不得被顺带删除。"""

    def setUp(self):
        self.templates_dir = Path(settings.BASE_DIR) / "templates"

    def test_retired_template_files_no_longer_exist(self):
        for name in RETIRED_TEMPLATES:
            with self.subTest(template=name):
                self.assertFalse(
                    (self.templates_dir / name).exists(),
                    f"templates/{name} 属 ARCH-03 DELETE/MIGRATE，应已退役删除",
                )

    def test_kept_templates_still_exist(self):
        """对照面：KEEP 模板（含餐饮/停车/订单与现行中心页）不得被顺带删除。"""
        for name in KEPT_TEMPLATES:
            with self.subTest(template=name):
                self.assertTrue(
                    (self.templates_dir / name).exists(),
                    f"templates/{name} 属 ARCH-03 KEEP，不得删除",
                )
