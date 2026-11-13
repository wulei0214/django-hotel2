# -*- coding: utf-8 -*-
"""白盒 API/视图/中间件层审查针对性回归（WB-46，docs/audit/phase0/46-whitebox-api.md）。

每个测试对应报告中的一个编号缺陷（API-BUG-01~12）或四态/越权抽查口径：
  * 非对象 JSON 请求体一律 400（night_audit / staff_center / guest_center，此前 500）；
  * 参数类型校验（guest_id 非整数、排房 room_id 缺失/非整数、bool 冒充整数）；
  * 脱敏短值不原样回传（staff/guest phone_masked、front_desk._mask_phone）；
  * 公共端点 DEFAULT 物业缺失 → 503 而非 500（availability 同口径）；
  * 跨物业对象访问一律 404 不泄露存在性（guest/membership/staff/folio 通知抽查）；
  * 认证四态抽查（匿名 401 / 无任职 404 / 无权限 403）；
  * OpenAPI 权限声明与视图实际门禁一致（audit-timeline → core.audit_view）。

只断言 API 层行为契约，不触碰服务层实现。
"""
import json
from datetime import date, timedelta

from django.contrib.auth.models import Group, Permission, User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.billing.models import Folio
from apps.core.models import Notification, Property
from apps.guests.models import Guest, GuestDocument, GuestMembership, GuestPhone
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.reservations.models import Reservation

from . import views as public_views

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（Mimosa L3 门禁）。
from django.utils.crypto import get_random_string  # noqa: E402
_TEST_PW_A73B46BB = get_random_string(24)
_TEST_PW_057CC5EF = get_random_string(24)




def _post_json(client, url, payload, **extra):
    return client.post(url, data=json.dumps(payload),
                       content_type="application/json", **extra)


class WhiteboxApiTestBase(TestCase):
    """DEFAULT 物业 + 员工账号工厂 + 跨物业（OTHER）数据工厂。"""

    maxDiff = None

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.other_property = Property.objects.create(
            code="OTHER-WB46", name="白盒审查跨物业",
            status=Property.Status.ACTIVE)
        self.factory = RequestFactory()
        self._seq = 0

    # ------------------------------------------------------------------
    # 员工账号工厂（对齐 test_billing_center 身份链）
    # ------------------------------------------------------------------
    def _make_staff(self, codenames=(), app_label="hotelapp", with_phone=None):
        self._seq += 1
        username = f"wb46_user{self._seq}"
        user = User.objects.create_user(username=username,
                                        password=f"{username}-pass123")
        profile = StaffProfile.objects.create(
            user=user, full_name=username, phone=with_phone or "",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile, property=self.property,
            employee_no=f"WB46-{username}",
            status=PropertyStaffMembership.Status.ACTIVE)
        if codenames:
            group = Group.objects.create(name=f"WB46-G-{username}")
            group.permissions.add(*Permission.objects.filter(
                codename__in=set(codenames),
                content_type__app_label__in=(app_label, "hotelapp")))
            StaffRoleAssignment.objects.create(
                membership=membership, group=group,
                starts_at=timezone.now() - timezone.timedelta(minutes=1))
        return user

    def _grant(self, user, codenames, app_label="hotelapp"):
        assignment = StaffRoleAssignment.objects.filter(
            membership__staff_profile__user=user).first()
        assignment.group.permissions.add(*Permission.objects.filter(
            codename__in=set(codenames),
            content_type__app_label__in=(app_label, "hotelapp")))

    # ------------------------------------------------------------------
    # 跨物业数据工厂
    # ------------------------------------------------------------------
    def _other_guest(self, phone="13800001111"):
        return Guest.objects.create(
            property=self.other_property, full_name="别店住客", phone=phone,
            phone_normalized="".join(ch for ch in phone if ch.isdigit()))

    def _other_membership(self, guest, member_no="WB46-OTHER-M1"):
        return GuestMembership.objects.create(
            property=self.other_property, guest=guest, member_no=member_no,
            level_code="STANDARD", status=GuestMembership.Status.ACTIVE,
            joined_at=date.today())


class NonObjectJsonBodyTests(WhiteboxApiTestBase):
    """API-BUG-01/02/03：非对象 JSON 请求体 → 400，不再 500。"""

    def test_night_audit_run_array_body_returns_400(self):
        """WB-46-01：夜审执行（night_audit._body）不校验对象类型此前 500。"""
        user = self._make_staff(codenames={"night_audit_execute"})
        self.client.force_login(user)
        response = self.client.post("/api/v1/night-audit/run", data="[]",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body["error"]["code"], "VALIDATION_ERROR")

    def test_membership_create_array_body_returns_400(self):
        """WB-46-02：会员创建（staff_center._membership_body）此前 500。"""
        user = self._make_staff(codenames={"add_vip"})
        self.client.force_login(user)
        response = self.client.post("/api/v1/memberships/create", data="[]",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_membership_change_status_array_body_returns_400(self):
        """WB-46-02：会员暂停/恢复同解析器，字符串体同样 400。"""
        user = self._make_staff(codenames={"view_vip", "change_vip"})
        self.client.force_login(user)
        guest = Guest.objects.create(property=self.property, full_name="本店会员",
                                     phone="13812345678")
        membership = GuestMembership.objects.create(
            property=self.property, guest=guest, member_no="WB46-M-1",
            level_code="STANDARD", status=GuestMembership.Status.ACTIVE,
            joined_at=date.today())
        response = self.client.post(
            f"/api/v1/memberships/{membership.pk}/status", data='"str"',
            content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_guest_create_array_body_returns_400(self):
        """WB-46-03：客人创建（guest_center._parse_body）此前 500。"""
        user = self._make_staff(codenames={"add_client"})
        self.client.force_login(user)
        response = self.client.post("/api/v1/guests/create", data="[]",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_guest_reveal_document_array_body_returns_400(self):
        """WB-46-03：证件揭示（body 解析先于原因校验）此前 500。"""
        user = self._make_staff(codenames={"view_client", "guest_document_reveal"})
        self.client.force_login(user)
        guest = Guest.objects.create(property=self.property, full_name="本店住客",
                                     phone="13812345678")
        document = GuestDocument.objects.create(
            guest=guest, doc_type=GuestDocument.DocType.ID_CARD,
            doc_no_ciphertext="cipher-placeholder")
        response = self.client.post(
            f"/api/v1/guests/{guest.pk}/documents/{document.pk}/reveal",
            data="[]", content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_adjust_daily_rate_array_body_returns_400(self):
        """WB-46-03：房价调整（body 解析先于对象加载）此前 500。"""
        user = self._make_staff(codenames={"manage_rates"}, app_label="inventory")
        self.client.force_login(user)
        response = self.client.post("/api/v1/daily-rates/1/adjust", data="[]",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")


class ParamValidationTests(WhiteboxApiTestBase):
    """API-BUG-04/05/10：参数类型/边界校验不产生 500、不放过类型混淆。"""

    def test_membership_create_non_integer_guest_id_returns_400(self):
        """WB-46-05：guest_id="abc" 经 Django 字段转换抛 ValueError，此前 500。"""
        user = self._make_staff(codenames={"add_vip"})
        self.client.force_login(user)
        response = _post_json(self.client, "/api/v1/memberships/create",
                              {"guest_id": "abc", "member_no": "WB46-M1"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_front_desk_assign_room_missing_room_id_returns_400(self):
        """WB-46-04：room_id 缺失 → int(None) TypeError，此前 500。"""
        user = self._make_staff(
            codenames={"view_accommodation", "change_accommodation"})
        self.client.force_login(user)
        response = self.client.post(
            "/api/v1/front-desk/reservation-rooms/1/assign",
            data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "VALIDATION_ERROR")
        self.assertIn("room_id", error["message"])

    def test_front_desk_assign_room_non_integer_room_id_returns_400(self):
        """WB-46-04：room_id 非数字串同样 400（消息统一中文）。"""
        user = self._make_staff(
            codenames={"view_accommodation", "change_accommodation"})
        self.client.force_login(user)
        response = self.client.post(
            "/api/v1/front-desk/reservation-rooms/1/assign",
            data=json.dumps({"room_id": "abc"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("room_id", response.json()["error"]["message"])

    def test_public_reservation_create_rejects_bool_rooms(self):
        """WB-46-10：bool 会被 int() 静默接受（true→1），必须 400。"""
        response = _post_json(self.client, "/api/v1/reservations", {
            "full_name": "张三", "phone": "13812345678",
            "room_type_id": 1, "check_in": str(date.today() + timedelta(days=1)),
            "check_out": str(date.today() + timedelta(days=2)),
            "rooms": True, "adults": 1, "children": 0,
        })
        self.assertEqual(response.status_code, 400, response.content)
        # MAINT-04：公开端点统一错误对象封套
        self.assertIn("房间数", response.json()["error"]["message"])

    def test_public_reservation_create_rejects_bool_adults(self):
        """WB-46-10：adults=false 此前被静默当 0 拒，语义应为类型错误。"""
        response = _post_json(self.client, "/api/v1/reservations", {
            "full_name": "张三", "phone": "13812345678",
            "room_type_id": 1, "check_in": str(date.today() + timedelta(days=1)),
            "check_out": str(date.today() + timedelta(days=2)),
            "rooms": 1, "adults": False, "children": 0,
        })
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("成人数", response.json()["error"]["message"])


class MaskingTests(WhiteboxApiTestBase):
    """API-BUG-06/07/08：脱敏短值不得原样回传（WB-10 同根因）。"""

    def test_staff_short_phone_fully_masked(self):
        """WB-46-06：staff phone_masked 短值此前原样返回。"""
        user = self._make_staff(codenames={"staff_manage"}, app_label="identity",
                                with_phone="12345")
        self.client.force_login(user)
        response = self.client.get(f"/api/v1/staff/{user.staff_profile.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["phone_masked"], "*" * 5)

    def test_staff_full_phone_masked(self):
        """WB-46-06：≥8 位仍走 3+4 掩码，行为不变。"""
        user = self._make_staff(codenames={"staff_manage"}, app_label="identity",
                                with_phone="13812345678")
        self.client.force_login(user)
        response = self.client.get(f"/api/v1/staff/{user.staff_profile.pk}")
        self.assertEqual(response.json()["data"]["phone_masked"], "138****5678")

    def test_guest_short_primary_phone_hidden(self):
        """WB-46-07：guest phone_masked 短值此前原样返回。"""
        user = self._make_staff(codenames={"view_client"})
        self.client.force_login(user)
        guest = Guest.objects.create(property=self.property, full_name="短号客人",
                                     phone="1234567")
        GuestPhone.objects.create(guest=guest, phone="1234567",
                                  phone_normalized="1234567", is_primary=True)
        response = self.client.get(f"/api/v1/guests/{guest.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["phone_masked"], "已隐藏")
        for phone_item in data["phones"]:
            self.assertNotIn("1234567", phone_item["phone"])

    def test_front_desk_mask_phone_short_digits_not_recoverable(self):
        """WB-46-08：7 位数字串 3+4 掩码可完整还原，必须隐藏。"""
        from .front_desk import _mask_phone
        self.assertEqual(_mask_phone("1234567"), "已隐藏")
        self.assertEqual(_mask_phone("123456"), "已隐藏")
        self.assertEqual(_mask_phone("13812345678"), "138****5678")
        self.assertEqual(_mask_phone(""), "—")
        self.assertEqual(_mask_phone(None), "—")


def _json(response):
    return json.loads(response.content)


class PublicPropertyGuardTests(WhiteboxApiTestBase):
    """API-BUG-11：DEFAULT 物业缺失时公共端点 503，不 500。

    直接以 RequestFactory 调视图（跳过 PropertyContextMiddleware），
    模拟中间件解析不到物业上下文的边界。
    """

    def _anonymous_request(self, method, path, data=None, **extra):
        request = getattr(self.factory, method)(path, data=data, **extra)
        request.user = type("AnonUser", (), {"is_authenticated": False})()
        request.property = None
        return request

    def test_reservation_detail_without_property_returns_503(self):
        response = public_views.reservation_detail(
            self._anonymous_request("get", "/api/v1/reservations/BK-X"), "BK-X")
        self.assertEqual(response.status_code, 503)
        self.assertIn("物业不可用", _json(response)["error"]["message"])

    def test_payment_intent_create_without_property_returns_503(self):
        response = public_views.payment_intent_create(
            self._anonymous_request(
                "post", "/api/v1/payments/intents",
                data=json.dumps({"booking_no": "BK-X", "token": "t"}),
                content_type="application/json"))
        self.assertEqual(response.status_code, 503)
        self.assertIn("物业不可用", _json(response)["error"]["message"])

    def test_reservation_cancel_without_property_returns_503(self):
        response = public_views.reservation_cancel(
            self._anonymous_request(
                "post", "/api/v1/reservations/BK-X/cancel",
                data=json.dumps({"token": "t"}),
                content_type="application/json"), "BK-X")
        self.assertEqual(response.status_code, 503)
        self.assertIn("物业不可用", _json(response)["error"]["message"])

    def test_availability_without_property_returns_503_reference(self):
        """参照口径：availability 在同边界早已 503（回归护栏）。"""
        response = public_views.availability(
            self._anonymous_request("get", "/api/v1/availability"))
        self.assertEqual(response.status_code, 503)


class HorizontalAccessTests(WhiteboxApiTestBase):
    """水平越权抽查：跨物业对象一律 404，不泄露存在性。"""

    def test_guest_detail_cross_property_returns_404(self):
        user = self._make_staff(codenames={"view_client"})
        self.client.force_login(user)
        guest = self._other_guest()
        response = self.client.get(f"/api/v1/guests/{guest.pk}")
        self.assertEqual(response.status_code, 404, response.content)

    def test_membership_detail_cross_property_returns_404(self):
        user = self._make_staff(codenames={"view_vip"})
        self.client.force_login(user)
        membership = self._other_membership(self._other_guest())
        response = self.client.get(f"/api/v1/memberships/{membership.pk}")
        self.assertEqual(response.status_code, 404, response.content)

    def test_staff_detail_cross_property_returns_404(self):
        user = self._make_staff(codenames={"staff_manage"}, app_label="identity")
        self.client.force_login(user)
        outsider = User.objects.create_user(username="wb46_outsider",
                                            password=_TEST_PW_A73B46BB)
        profile = StaffProfile.objects.create(
            user=outsider, full_name="别店员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        PropertyStaffMembership.objects.create(
            staff_profile=profile, property=self.other_property,
            employee_no="WB46-OTHER-STAFF",
            status=PropertyStaffMembership.Status.ACTIVE)
        response = self.client.get(f"/api/v1/staff/{profile.pk}")
        self.assertEqual(response.status_code, 404, response.content)

    def test_billing_folio_detail_cross_property_returns_404(self):
        user = self._make_staff(codenames={"finance_access"})
        self.client.force_login(user)
        guest = self._other_guest()
        reservation = Reservation.objects.create(
            property=self.other_property, guest=guest,
            booking_no="WB46-OTHER-BK1", source="WEBSITE",
            status=Reservation.Status.CONFIRMED,
            check_in=date.today(), check_out=date.today() + timedelta(days=1),
            total_amount=100)
        folio = Folio.objects.create(
            property=self.other_property, reservation=reservation, guest=guest,
            folio_type="MASTER", status=Folio.Status.OPEN, currency="CNY")
        response = self.client.get(f"/api/v1/billing/folios/{folio.pk}")
        self.assertEqual(response.status_code, 404, response.content)

    def test_notification_mark_read_cross_property_returns_404(self):
        user = self._make_staff()
        self.client.force_login(user)
        notification = Notification.objects.create(
            property=self.other_property, recipient=user.staff_profile,
            category=Notification.Category.SYSTEM, severity=Notification.Severity.INFO,
            title="别店通知", body="不属于当前物业")
        response = self.client.post(
            f"/api/v1/notifications/{notification.pk}/read")
        self.assertEqual(response.status_code, 404, response.content)


class AuthStateTests(WhiteboxApiTestBase):
    """认证四态抽查：匿名 401 / 无任职 404 / 有任职无权限 403 / 有权限 200。"""

    def test_anonymous_notification_list_returns_401(self):
        response = self.client.get("/api/v1/notifications")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_authenticated_without_membership_returns_404(self):
        """无 DEFAULT 任职（含任职别店）→ 无物业上下文 → 404，不静默回退。"""
        user = User.objects.create_user(username="wb46_nomember",
                                        password=_TEST_PW_057CC5EF)
        self.client.force_login(user)
        response = self.client.get("/api/v1/notifications")
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(response.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_membership_without_permission_returns_403(self):
        user = self._make_staff()
        self.client.force_login(user)
        response = self.client.get("/api/v1/memberships")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_membership_list_with_permission_returns_200(self):
        user = self._make_staff(codenames={"view_vip"})
        self.client.force_login(user)
        response = self.client.get("/api/v1/memberships")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("data", response.json())
        self.assertIn("meta", response.json())

    def test_get_only_endpoints_reject_post(self):
        """读端点 POST-only 纪律：写方法 405；公共面（api_guard）保持 JSON 封套。"""
        user = self._make_staff(codenames={"view_client"})
        self.client.force_login(user)
        response = self.client.post("/api/v1/guests", data="{}",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 405, response.content)
        # api_guard 兜底面（公共可用性查询）405 也统一 JSON
        response = self.client.post("/api/v1/availability", data="{}",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 405, response.content)
        self.assertEqual(response.json()["error"]["code"], "METHOD_NOT_ALLOWED")
        self.assertEqual(response.json()["error"]["message"],
                         "不支持的 HTTP 方法，请检查请求方式。")


class ContractConsistencyTests(WhiteboxApiTestBase):
    """API-BUG-09：契约声明与视图实际门禁一致。"""

    def test_openapi_audit_timeline_declares_audit_view_permission(self):
        from .openapi_schema import BASE, build_openapi
        spec = build_openapi()
        operation = spec["paths"][BASE + "/audit-timeline"]["get"]
        declared = operation.get("x-required-permission", "")
        self.assertIn("core.audit_view", declared)
        self.assertNotIn("identity.staff_manage", declared)

    def test_cross_property_payment_reference_rejected_not_replayed(self):
        """WB-46-12 护栏：跨物业同号 reference 不被当作本物业重放放行。

        服务层对跨物业占用拒绝（400 该收款参考号已用于不同支付），
        API 预检按物业过滤后状态语义与之自洽。
        """
        from apps.billing.models import Payment
        user = self._make_staff(codenames={"finance_access"})
        self.client.force_login(user)
        # 跨物业既有支付（provider_ref 全局唯一）
        other_guest = self._other_guest()
        other_reservation = Reservation.objects.create(
            property=self.other_property, guest=other_guest,
            booking_no="WB46-OTHER-BK2", source="WEBSITE",
            status=Reservation.Status.CONFIRMED,
            check_in=date.today(), check_out=date.today() + timedelta(days=1),
            total_amount=100)
        other_folio = Folio.objects.create(
            property=self.other_property, reservation=other_reservation,
            guest=other_guest, folio_type="MASTER", status=Folio.Status.OPEN,
            currency="CNY")
        Payment.objects.create(
            property=self.other_property, reservation=other_reservation,
            folio=other_folio, provider="frontdesk_manual",
            provider_ref="WB46-SHARED-REF", method="CASH", amount=100,
            currency="CNY", status=Payment.Status.SUCCEEDED)
        # 本物业带 OPEN 主账单的预订，复用同一 reference 收款
        local_guest = Guest.objects.create(property=self.property,
                                           full_name="本店收款客人",
                                           phone="13887654321")
        local_reservation = Reservation.objects.create(
            property=self.property, guest=local_guest,
            booking_no="WB46-LOCAL-BK1", source="WALK_IN",
            status=Reservation.Status.CHECKED_IN,
            check_in=date.today(), check_out=date.today() + timedelta(days=1),
            total_amount=100)
        Folio.objects.create(
            property=self.property, reservation=local_reservation,
            guest=local_guest, folio_type="MASTER", status=Folio.Status.OPEN,
            currency="CNY")
        response = _post_json(self.client, "/api/v1/billing/payments", {
            "reservation_id": local_reservation.pk, "amount": "50.00",
            "method": "CASH", "reference": "WB46-SHARED-REF",
        })
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("参考号", response.json()["error"]["message"])
        self.assertFalse(
            Payment.objects.filter(reservation=local_reservation).exists(),
            "跨物业占用 reference 不得在本物业产生任何新支付")
