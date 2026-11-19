"""MC-002 通用管理中心动作白名单（按权限显隐，不造通用 CRUD）。

R4 浏览器验收回归（docs/acceptance/evidence/r4-browser-report-20261119.md）：
  * D-R4-04：page 级 action 的 endpoint 路径占位符必须能由注册表声明的
    同名字段解析（前端 management_center.html resolveEndpoint 依赖该契约），
    房态中心三个写操作（调整房价/关房开房/停售开售）此前打到字面
    {rate_id} 路径 404；
  * D-R4-01：membership_create 动作缺 member_no 必填字段声明 → UI 提交必 400；
  * D-R4-02：membership_level 动作字段名 level 与后端 body.level_code 不匹配；
  * D-R4-03：rate-calendar 房价/库存行补充行 id，供操作表单按行 ID 定位。
"""

import re

from apps.api.pages import MANAGEMENT_CENTERS
from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.inventory.models import DailyRate, RoomTypeInventory
from django.test import override_settings


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ManagementCenterActionsTests(E2EFlowTestBase):
    def setUp(self):
        super().setUp()
        self.manager = self._make_staff("mc_action_manager", "店长")
        self.fd = self._make_staff("mc_action_front", "前台")
        self.finance = self._make_finance_view_only("mc_action_finance")

    def test_guest_center_exposes_only_actions_current_role_can_use(self):
        self.client.force_login(self.fd)
        response = self.client.get("/manage/guests/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pms-center-actions")
        self.assertContains(response, "guest_create")
        self.assertContains(response, "phone_add")

        self.client.force_login(self.finance)
        self.assertEqual(self.client.get("/manage/guests/").status_code, 403)

    def test_room_center_exposes_workflow_links_but_no_generic_crud(self):
        self.client.force_login(self.manager)
        response = self.client.get("/manage/rooms/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rate_close")
        self.assertContains(response, "/manage/housekeeping/")
        self.assertNotContains(response, "通用编辑")
        self.assertContains(response, "X-CSRFToken")

    def test_reservation_center_points_to_single_front_desk_workflow(self):
        self.client.force_login(self.fd)
        response = self.client.get("/manage/reservations/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/checkin/")
        self.assertNotContains(response, "reservation_create")

    # ------------------------------------------------------------------
    # D-R4-04：page 级 action endpoint 占位符解析契约
    # ------------------------------------------------------------------
    def test_page_action_endpoint_placeholders_covered_by_declared_fields(self):
        """所有中心的 page 级动作：endpoint 路径占位符必须被声明的表单字段
        覆盖（前端 resolveEndpoint 提交时按同名字段解析 URL；缺字段则必然
        打到字面 {xxx} 路径 404）。row 级动作的 {id} 由详情行上下文注入，
        其余占位符同样必须有字段覆盖。"""
        for center_key, config in MANAGEMENT_CENTERS.items():
            for action in config.get("actions", ()):
                placeholders = set(re.findall(r"\{(\w+)\}", action["endpoint"]))
                if action["scope"] == "row":
                    placeholders.discard("id")
                field_names = {field["name"] for field in action["fields"]}
                self.assertTrue(
                    placeholders <= field_names,
                    msg=f"{center_key}.{action['key']} endpoint 占位符 "
                    f"{sorted(placeholders)} 未被表单字段 {sorted(field_names)} 覆盖",
                )
        # 房态中心三个页面级写操作的占位符逐一核对（R4 实测 404 场景）
        room_actions = {
            action["key"]: action for action in MANAGEMENT_CENTERS["rooms"]["actions"]
        }
        self.assertEqual(
            re.findall(r"\{(\w+)\}", room_actions["rate_adjust"]["endpoint"]), ["rate_id"])
        self.assertEqual(
            re.findall(r"\{(\w+)\}", room_actions["rate_close"]["endpoint"]), ["rate_id"])
        self.assertEqual(
            re.findall(r"\{(\w+)\}", room_actions["stop_sell"]["endpoint"]), ["inventory_id"])
        for key in ("rate_adjust", "rate_close"):
            self.assertIn("rate_id",
                          {f["name"] for f in room_actions[key]["fields"]})
        self.assertIn("inventory_id",
                      {f["name"] for f in room_actions["stop_sell"]["fields"]})

    def test_room_center_write_operations_return_200(self):
        """房态中心三个写操作按注册表声明的载荷直连真实端点必须 200
        （D-R4-04 回归：此前 URL 占位符不替换导致前端提交 404）。"""
        self.client.force_login(self.manager)
        rate = DailyRate.objects.filter(property=self.property).order_by("id").first()
        inventory = (
            RoomTypeInventory.objects.filter(property=self.property).order_by("id").first()
        )
        adjust = self._post_json(
            f"/api/v1/daily-rates/{rate.pk}/adjust",
            {"price": "588.00", "reason": "R4 回归：房态中心调价"},
        )
        self.assertEqual(adjust.status_code, 200, adjust.content)
        self.assertTrue(adjust.json()["data"]["changed"])

        close = self._post_json(
            f"/api/v1/inventory/daily-rates/{rate.pk}/close",
            {"closed": True, "reason": "R4 回归：房态中心关房"},
        )
        self.assertEqual(close.status_code, 200, close.content)
        self.assertTrue(close.json()["data"]["changed"])
        reopen = self._post_json(
            f"/api/v1/inventory/daily-rates/{rate.pk}/close",
            {"closed": False, "reason": "R4 回归：房态中心开房"},
        )
        self.assertEqual(reopen.status_code, 200, reopen.content)

        stop = self._post_json(
            f"/api/v1/inventory/stock/{inventory.pk}/stop-sell",
            {"stop_sell": True, "reason": "R4 回归：房态中心停售"},
        )
        self.assertEqual(stop.status_code, 200, stop.content)
        self.assertTrue(stop.json()["data"]["changed"])

    # ------------------------------------------------------------------
    # D-R4-01 / D-R4-02：会员动作字段与后端 body 契约一致
    # ------------------------------------------------------------------
    def test_membership_action_fields_match_backend_contract(self):
        """membership_create 必须声明 member_no（staff_center.membership_create
        → GuestMembershipService.create_membership 必填，缺失则 UI 提交必 400）；
        等级字段名对齐后端读取的 level_code（membership_change_level 读
        body.level_code，旧字段 level 会被忽略并必回 400）。"""
        actions = {
            action["key"]: action
            for action in MANAGEMENT_CENTERS["memberships"]["actions"]
        }
        create_fields = {
            field["name"]: field for field in actions["membership_create"]["fields"]
        }
        self.assertIn("member_no", create_fields)
        self.assertTrue(create_fields["member_no"]["required"])
        self.assertIn("level_code", create_fields)
        self.assertNotIn("level", create_fields)

        level_fields = {
            field["name"]: field for field in actions["membership_level"]["fields"]
        }
        self.assertIn("level_code", level_fields)
        self.assertNotIn("level", level_fields)

        # 端到端：按注册表字段提交新增会员 → 201（字段名与后端契约一致）
        from apps.guests.models import Guest

        self.client.force_login(self.manager)
        guest = Guest.objects.create(property=self.property, full_name="R4 契约回归客人")
        payload = {
            field["name"]: ("1" if field.get("type") == "number" else "")
            for field in actions["membership_create"]["fields"]
        }
        payload["guest_id"] = guest.pk
        payload["member_no"] = "R4-MEM-CONTRACT-001"
        payload["level_code"] = "GOLD"
        created = self._post_json("/api/v1/memberships/create", payload)
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["data"]["level_code"], "GOLD")

    # ------------------------------------------------------------------
    # D-R4-03：rate-calendar 行携带 id
    # ------------------------------------------------------------------
    def test_rate_calendar_rows_include_row_id(self):
        """房价行/库存行必须返回 id：房态中心操作表单按注册表声明的
        房价行 ID/库存行 ID 定位（此前行内无 id，用户无从取得合法 ID）。"""
        self.client.force_login(self.manager)
        response = self.client.get("/api/v1/rate-calendar")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["rates"])
        self.assertTrue(data["inventory"])
        rate_ids = {row["id"] for row in data["rates"]}
        inventory_ids = {row["id"] for row in data["inventory"]}
        self.assertIn(
            DailyRate.objects.filter(property=self.property).order_by("id").first().pk,
            rate_ids)
        self.assertIn(
            RoomTypeInventory.objects.filter(property=self.property)
            .order_by("id").first().pk,
            inventory_ids)
