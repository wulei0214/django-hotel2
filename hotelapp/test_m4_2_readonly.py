"""M4-2 旧轨只读面收口回归（派发单 GOV-DISPATCH-M3M4M5-001 · 派发单 M4-2）。

覆盖裁定落地断言：
* D4-5 修订①：room.html 保留为只读兼容页——无效写交互（指向已退役 410/404
  旧写端点的录入表单与行内按钮）已摘除，只读数据展示保留
* D4-5 修订②：search_room 出口暂保留并登记访问观察点（只登记不改行为）
* D4-7 A：旧 Room 的 Django Admin 只读化（ReadOnlyLegacyAdmin 模式，参照
  apps/billing/admin.py 的 LegacyOrderReservationMapAdmin 先例）——写操作拒绝
"""

from pathlib import Path

from django.conf import settings
from django.contrib.admin import site as django_admin_site
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils.crypto import get_random_string

from apps.identity.test_module_gates import _grant
from hotelapp.admin import RoomAdmin
from hotelapp.models import Room, Staff

# 测试专用密码：运行时随机生成，源码不落凭据字面量（AGENTS.md 会话纪律）
_TEST_PW_6E1A0B44 = get_random_string(24)

# M4-2 已摘除的无效写交互标记：录入表单端点、行内编辑/删除端点、死按钮与挂载脚本
_WRITE_INTERACTION_MARKERS = (
    "/insert_room/",
    "/delete_room/",
    "/update_room/",
    "/room_status/",
    "btn-add",
    "form-card",
    "st-btn",
    'type="submit" value="提交"',
)

# 只读兼容页必须保留的数据展示标记（表格渲染与存活查询出口）
_READ_ONLY_KEEPERS = ("/manage/rooms/", "data-table", "房间号")


def _make_room(number=9101):
    """造一条旧 Room 只读展示数据（负责人须为服务员职务）。"""
    waiter = Staff.objects.create(姓名=f"保洁{number}", 职务="服务员", 月薪="3000", 工龄="1")
    return Room.objects.create(
        房间号=number, 客房类型="标准间", 价格=288, 负责人编号=waiter, 房间状态="空闲"
    )


class RoomAdminReadOnlyTests(TestCase):
    """D4-7 A：旧 Room Admin 只读化（ReadOnlyLegacyAdmin 模式），写操作拒绝。"""

    def setUp(self):
        self.admin_obj = RoomAdmin(Room, django_admin_site)

    def test_room_admin_write_permissions_all_closed(self):
        """admin 实例新增/修改/删除权限全 False（权限面第一道防线）。"""
        self.assertFalse(self.admin_obj.has_add_permission(request=None))
        self.assertFalse(self.admin_obj.has_change_permission(request=None, obj=None))
        self.assertFalse(self.admin_obj.has_change_permission(request=None, obj=_make_room(9101)))
        self.assertFalse(self.admin_obj.has_delete_permission(request=None, obj=None))
        self.assertFalse(self.admin_obj.has_delete_permission(request=None, obj=_make_room(9102)))

    def test_room_admin_readonly_fields_cover_all_model_fields(self):
        """全字段只读（ReadOnlyLegacyAdmin 先例：_meta.fields 全量收编）。"""
        readonly = set(self.admin_obj.get_readonly_fields(request=None, obj=None))
        model_fields = {field.name for field in Room._meta.fields}
        self.assertEqual(model_fields - readonly, set())
        self.assertTrue(model_fields.issubset(readonly))

    def test_admin_room_write_operations_rejected_for_superuser(self):
        """超级管理员经 admin 页面写旧 Room 也被拒绝（不依赖账号权限配置）。

        admin 采用路径隔离 Cookie（django_admin_sessionid）+ 平台登录白名单
        （PLATFORM_ADMIN_USERNAME），须走真实 /admin/login/ 流程并以白名单账号
        登录（同 apps/identity/test_cookie_isolation.py 先例）。
        """
        with override_settings(PLATFORM_ADMIN_USERNAME="m42admin"):
            User.objects.create_superuser("m42admin", password=_TEST_PW_6E1A0B44)
            response = self.client.post(
                "/admin/login/",
                {"username": "m42admin", "password": _TEST_PW_6E1A0B44, "next": "/admin/"},
            )
            self.assertEqual(response.status_code, 302)
            self._assert_room_admin_write_rejected()

    def _assert_room_admin_write_rejected(self):
        room = _make_room(9102)

        add_url = "/admin/hotelapp/room/add/"
        change_url = f"/admin/hotelapp/room/{room.pk}/change/"
        delete_url = f"/admin/hotelapp/room/{room.pk}/delete/"

        self.assertEqual(self.client.get(add_url).status_code, 403, "新增入口必须拒绝")
        self.assertEqual(self.client.post(add_url).status_code, 403)
        # 修改页保留只读查看（200），但提交写回必须 403（has_change_permission=False）
        self.assertEqual(self.client.get(change_url).status_code, 200)
        self.assertEqual(self.client.post(change_url).status_code, 403, "admin 修改提交必须拒绝")
        self.assertEqual(self.client.get(delete_url).status_code, 403, "删除入口必须拒绝")

        room.refresh_from_db()
        self.assertEqual(Room.objects.filter(pk=room.pk).count(), 1, "不得被物理删除")


class SearchRoomReadOnlyPageTests(TestCase):
    """D4-5 修订①/②：search_room 只读兼容页 + 访问观察点登记。"""

    def setUp(self):
        _grant("前台", "view_room")
        self.user = self._make_front_desk_user()
        self.client.force_login(self.user)

    @staticmethod
    def _make_front_desk_user():
        from hotelapp.tests import _make_user

        return _make_user("m42observer", "前台")

    def test_search_room_renders_readonly_page_without_write_interactions(self):
        """只读数据展示保留，无效写交互（表单/按钮/挂载脚本）无残留。"""
        room = _make_room(9103)
        response = self.client.get("/search_room/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        for marker in _READ_ONLY_KEEPERS:
            self.assertIn(marker, content, f"只读展示标记 {marker} 必须保留")
        for marker in _WRITE_INTERACTION_MARKERS:
            self.assertNotIn(marker, content, f"无效写交互 {marker} 必须已摘除")
        self.assertContains(response, str(room.pk))

    def test_search_room_post_search_flow_kept_and_logged(self):
        """查询行为不变（POST 检索照常返回结果），且登记 M4-2 观察点日志。"""
        room = _make_room(9104)
        with self.assertLogs("hotelapp.views", level="INFO") as logs:
            response = self.client.post(
                "/search_room/", {"columns": "房间号", "value": str(room.pk)}
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(room.pk), msg_prefix="POST 检索行为不得改变")
        self.assertTrue(
            any("M4-2 观察点" in line and "search_room" in line for line in logs.output),
            f"search_room 必须登记 M4-2 观察点日志，实际输出：{logs.output}",
        )


class RoomTemplateSourceGuardTests(TestCase):
    """模板源级守卫：room.html 写交互回归（不依赖视图与登录态，防手改回流）。"""

    def test_room_html_source_has_no_write_interactions(self):
        source = (Path(settings.BASE_DIR) / "templates" / "room.html").read_text(encoding="utf-8")
        for marker in _WRITE_INTERACTION_MARKERS:
            self.assertNotIn(marker, source, f"room.html 源码不得残留写交互 {marker}")
        for marker in _READ_ONLY_KEEPERS:
            self.assertIn(marker, source, f"room.html 源码必须保留只读展示 {marker}")
