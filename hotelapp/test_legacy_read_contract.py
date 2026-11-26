"""hotelapp 旧只读入口的现状契约测试（M3-2 缺口补齐 + ARCH-02 退役契约）。

入口清单 = 模块③提案 §3.2 态②「9 入口 + 1 Admin 面」之 9 入口
（派发单 GOV-DISPATCH-M3M4M5-001 M3-2，分类沿旧财务入口依赖清单 Ⅰ/Ⅱ/Ⅲ）：

  #  入口                                            分类  本批次处置
  1  /checkin/legacy/   checkin_manage               Ⅲ    ARCH-02 REDIRECT → /checkin/
  2  /show_accomodation/  E2                          Ⅱ    ARCH-02 REDIRECT → /manage/reservations/
  3  /search_accomodation/（E2 页面家族 POST 查询）      Ⅱ    ARCH-02 REDIRECT → /manage/reservations/
  4  /api/v1/front-desk/legacy-reservations   E3       Ⅲ    数据源切归档查询通道
  5  /api/v1/front-desk/legacy-reservations/<id>  E4   Ⅱ    数据源切归档查询通道
  6  /folio/            folio_api                     Ⅰ    D3-4 甲：只登记不动码
  7  /show_order/                                     Ⅲ    餐饮 module_gate 默认关闭
  8  /search_order/                                   Ⅲ    同上
  9  /export/order/                                   Ⅲ    同上（gate 先于权限判定）

契约口径：断言**当前行为**（状态码 + 响应内容关键结构），不锚定业务数据量。
入口 1/2/3 的旧页渲染能力已按 ARCH-02 REDIRECT 退役（302 跳新中心），本文件
改钉**退役契约**：跳转目标 + 门禁四态（匿名登录墙 / 无权限 403 / 有权限 302）；
入口 4~9 仍是活跃读面，继续钉读路径契约，切换不得改变其状态码与响应结构。
"""

from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（AGENTS.md 会话纪律）。
from django.utils.crypto import get_random_string  # noqa: E402

from apps.core.models import Property
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from hotelapp.models import Accommodation, Client, Folio, Room, Staff

_TEST_PW_77B3D9A1 = get_random_string(24)
_TEST_PW_2C64EF08 = get_random_string(24)


def _make_member(username, group_name):
    """前台/客房服务员等内置岗位账号（对齐 hotelapp/tests.py 造数模式）。"""
    prop = Property.objects.get(code="DEFAULT")
    user = User.objects.create_user(username=username, password=_TEST_PW_77B3D9A1)
    profile = StaffProfile.objects.create(
        user=user, full_name=username, employment_status=StaffProfile.EmploymentStatus.ACTIVE
    )
    membership = PropertyStaffMembership.objects.create(
        property=prop,
        staff_profile=profile,
        employee_no=f"M3C-{username}",
        status=PropertyStaffMembership.Status.ACTIVE,
    )
    StaffRoleAssignment.objects.create(
        membership=membership,
        group=Group.objects.get(name=group_name),
        starts_at=timezone.now() - timedelta(minutes=1),
    )
    return user


def _make_legacy_chain(seq):
    """旧表三件套造数：Staff/Room/Client/Accommodation + 旧 Folio 一条凭证。"""
    staffer = Staff.objects.create(姓名=f"契约保洁{seq}", 职务="服务员", 月薪="3000", 工龄="1")
    room = Room.objects.create(
        房间号=9300 + seq, 客房类型="标准间", 价格=300, 负责人编号=staffer.pk, 房间状态="空闲"
    )
    client = Client.objects.create(姓名=f"契约住客{seq}", 身份证号=f"11010119900101{seq:04d}")
    order = Accommodation.objects.create(
        id=7300 + seq,
        顾客id=client,
        客房编号=room,
        状态="已入住",
        入住时间="2026-11-02",
        联系电话=f"138{seq:08d}",
        收款金额="300",
        押金="100",
    )
    Folio.objects.create(
        order=order,
        类型="房费",
        金额="300",
        事由="契约核验房费",
        日期="2026-11-02",
        经手人="契约核验",
    )
    return order


class LegacyReadContractBase(TestCase):
    """前台（有 view_accommodation）与客房服务员（无）双岗位基线。"""

    @classmethod
    def setUpTestData(cls):
        cls.front = _make_member("m3c-front", "前台")
        cls.housekeeper = _make_member("m3c-housekeeper", "客房服务员")


class CheckinLegacyRetiredContractTests(LegacyReadContractBase):
    """入口 1 /checkin/legacy/：ARCH-02 REDIRECT 退役为 302 → /checkin/。

    退役的是旧页渲染能力（checkin.html 随 ARCH-03 一并删除），门禁不变：
    匿名走登录墙、无 view_accommodation 仍 403、有权限才 302 到新工作台。
    """

    def test_front_desk_gets_302_to_frontdesk_workspace(self):
        self.client.force_login(self.front)
        response = self.client.get("/checkin/legacy/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/checkin/")

    def test_anonymous_redirects_to_login(self):
        response = self.client.get("/checkin/legacy/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_housekeeper_without_view_permission_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/checkin/legacy/")
        self.assertEqual(response.status_code, 403)


class ShowAccomodationRetiredContractTests(LegacyReadContractBase):
    """入口 2 /show_accomodation/：ARCH-02 REDIRECT 退役为 302 → /manage/reservations/。

    E2 归档读路径契约改由存活的 /api/v1/front-desk/legacy-reservations 通道
    承接（apps/api/test_legacy_equivalence.py）；本类只钉退役契约与门禁四态。
    """

    def test_front_desk_gets_302_to_reservations_center(self):
        self.client.force_login(self.front)
        response = self.client.get("/show_accomodation/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/reservations/")

    def test_anonymous_redirects_to_login(self):
        response = self.client.get("/show_accomodation/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_housekeeper_without_view_permission_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/show_accomodation/")
        self.assertEqual(response.status_code, 403)


class SearchAccomodationRetiredContractTests(LegacyReadContractBase):
    """入口 3 /search_accomodation/：ARCH-02 REDIRECT 退役为 302 → /manage/reservations/。

    旧页 POST 查询能力随页面退役；GET/POST 一律 302 到新中心，门禁不变。
    """

    def test_post_gets_302_to_reservations_center(self):
        self.client.force_login(self.front)
        response = self.client.post("/search_accomodation/", {"id": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/reservations/")

    def test_get_gets_302_to_reservations_center(self):
        self.client.force_login(self.front)
        response = self.client.get("/search_accomodation/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/reservations/")

    def test_anonymous_redirects_to_login(self):
        response = self.client.get("/search_accomodation/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_housekeeper_without_view_permission_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.post("/search_accomodation/", {"id": "1"})
        self.assertEqual(response.status_code, 403)


class LegacyReservationsListContractTests(LegacyReadContractBase):
    """入口 4 GET /api/v1/front-desk/legacy-reservations（E3，数据源切归档通道）。"""

    def test_front_desk_gets_legacy_envelope(self):
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/front-desk/legacy-reservations")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload.keys()), {"data", "meta"})
        self.assertEqual(payload["data"], [])
        meta = payload["meta"]
        for key in (
            "page",
            "page_size",
            "total",
            "has_previous",
            "has_next",
            "source_system",
            "read_only",
        ):
            self.assertIn(key, meta, key)
        self.assertEqual(meta["source_system"], "LEGACY")
        self.assertTrue(meta["read_only"])

    def test_overlong_search_gets_validation_error(self):
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/front-desk/legacy-reservations", {"q": "词" * 81})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/legacy-reservations")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/front-desk/legacy-reservations")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")


class LegacyReservationDetailContractTests(LegacyReadContractBase):
    """入口 5 GET /api/v1/front-desk/legacy-reservations/<id>（E4，数据源切归档通道）。"""

    def test_unknown_id_gets_404_error_envelope(self):
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/front-desk/legacy-reservations/999999")
        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(set(payload.keys()), {"error"})
        self.assertEqual(payload["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/legacy-reservations/1")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/front-desk/legacy-reservations/1")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")


class FolioApiContractTests(LegacyReadContractBase):
    """入口 6 /folio/（folio_api，Ⅰ 类财务管辖）：D3-4 甲只登记不动码，契约现状钉住。"""

    def test_front_desk_gets_aggregate_json_keys(self):
        order = _make_legacy_chain(2)
        self.client.force_login(self.front)
        response = self.client.get("/folio/", {"id": order.id})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for key in ("rows", "due", "deposit", "paid", "balance", "guest", "room", "status"):
            self.assertIn(key, payload, key)
        self.assertEqual(payload["status"], "已入住")
        self.assertEqual(payload["guest"], "契约住客2")
        self.assertEqual(payload["rows"][0]["类型"], "房费")

    def test_unknown_order_gets_404_text(self):
        self.client.force_login(self.front)
        response = self.client.get("/folio/", {"id": "999999"})
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "订单不存在", status_code=404)

    def test_housekeeper_gets_403(self):
        order = _make_legacy_chain(3)
        self.client.force_login(self.housekeeper)
        response = self.client.get("/folio/", {"id": order.id})
        self.assertEqual(response.status_code, 403)


class DiningModuleGateContractTests(LegacyReadContractBase):
    """入口 7/8/9（show_order/search_order/export/order，Ⅲ）：餐饮 module_gate 默认
    关闭（registry fnb=PLANNED），直接 URL 统一带提示跳回 /home/——现状契约。"""

    def test_show_order_redirects_home_with_notice(self):
        self.client.force_login(self.front)
        response = self.client.get("/show_order/", follow=True)
        self.assertRedirects(response, "/home/")
        self.assertContains(response, "该模块已对接第三方系统")

    def test_search_order_post_redirects_home_with_notice(self):
        self.client.force_login(self.front)
        response = self.client.post("/search_order/", {"id": "1"}, follow=True)
        self.assertRedirects(response, "/home/")
        self.assertContains(response, "该模块已对接第三方系统")

    def test_export_order_redirects_home_with_notice(self):
        self.client.force_login(self.front)
        response = self.client.get("/export/order/", follow=True)
        self.assertRedirects(response, "/home/")
        self.assertContains(response, "该模块已对接第三方系统")
