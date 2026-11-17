"""此前 25 个 x-contract-status=partial 操作的端点级契约核验回归测试（转 verified 证据包）。

用途 / 证据链（详见 docs 下《API契约与测试规范》）：
  * 权威快照 docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json
    （当前 123 ops）全部标记 ``x-contract-status: verified``；此前 25 个 partial
    端点的端点级成功/认证/权限与错误响应回归测试继续保留；
  * verified 定义 = 契约字段经源码核对 + 端点级 HTTP 测试（成功/认证/权限/
    错误响应按接口性质适用）；本模块为这 25 个操作提供端点级证据；
  * 数据驱动：运行时加载权威快照 JSON，按 operationId 收集 2xx 响应 schema
    的顶层 properties 键集（schema 级 $ref 解析到 components/schemas 一层），
    断言"实际响应顶层键 ⊆ 契约声明键 ∪ 允许的分页/封套键
    （count/next/previous/results；{data,meta} 封套键以该端点声明为准）"，
    不手写期望字段；同时反向断言契约声明键不在运行时缺失；
  * 每端点三类断言：a) 合法角色 → 成功状态码（以视图实际为准）+ 响应键 ⊆
    契约键；b) 匿名 → 401/403（以该视图认证机制实际为准）；c) 越权角色 →
    403（以实际为准）；POST 端点额外断言业务效果与非法载荷 400；
  * 造数复用既有测试模式：身份链对齐 test_e2e_flows（岗位组由迁移预置，
    见 apps/identity/roles.py 九角色白名单）；审批链对齐 test_approval；
    列表造数对齐 test_pagination_contract；ReservationRoom/Folio 造数对齐
    test_billing_center；旧订单造数对齐 hotelapp/tests.py。密码为
    get_random_string 模块级随机值（源码不落凭据字面量）。

P4-FINDING（视图行为与契约声明矛盾，断言以实际行为为准放行，不改契约/视图）：
  见各测试方法内 # P4-FINDING 注释；汇总：
  * F1-F5：前台 5 个列表/查询端点 200 schema 的 properties 只声明 data，
    实际响应含 meta 分页/统计键（描述文本已写明"列表附 meta 分页"、
    additionalProperties=true 允许，但键未显式声明）——见 RUNTIME_ENVELOPE_EXTRAS；
  * F6：rate-calendar 契约 RateCalendarData 只声明 inventory/rates，实际 data
    含 inventory_pagination/rates_pagination（P0-03 双集合分页补齐晚于快照）；
  * F7：financial-summary 契约 FinancialSummaryData 未声明 metadata，实际 data
    含 metadata（P0-07 统计口径注记晚于快照）；
  * F8：已在 SYS-06/G14-11 收敛；审批 reject 非对象请求体返回 400 VALIDATION_ERROR，
    由本包端点回归测试持续守卫。
"""
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django.utils.crypto import get_random_string
from hotelapp.models import Accommodation as LegacyAccommodation
from hotelapp.models import Client as LegacyClient
from hotelapp.models import Folio as LegacyFolio
from hotelapp.models import Room as LegacyRoom
from hotelapp.models import Staff as LegacyStaff

from apps.billing.models import ApprovalRecord, Folio
from apps.core.models import AuditEvent, BusinessDate, Property
from apps.guests.models import Guest, GuestMembership, MembershipLevelHistory
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory
from apps.reservations.models import Reservation, ReservationRoom

_TEST_PW_3D7B9A21 = get_random_string(24)

# 权威快照路径（与 test_openapi_schema.OPENAPI_AUTHORITATIVE_SNAPSHOT 同源）。
OPENAPI_AUTHORITATIVE_SNAPSHOT = (
    Path(settings.BASE_DIR)
    / "docs"
    / "PMS2.0_当前版本"
    / "03_API接口与前后端规范"
    / "接口文档"
    / "openapi.json"
)

with open(OPENAPI_AUTHORITATIVE_SNAPSHOT, encoding="utf-8") as _fh:
    _CONTRACT = json.load(_fh)

# 此前 25 个 partial 端点的回归用例保留在下方各测试类；快照守卫检查全量 operation 状态。

# 允许的分页封套键（DRF 风格分页；本仓 25 个端点均未使用，保留同口径）。
PAGINATION_LIST_KEYS = frozenset({"count", "next", "previous", "results"})

# P4-FINDING F1-F5：契约 200 schema properties 只声明 data、实际响应含 meta
# 的前台端点（快照描述已注明"列表附 meta 分页"；additionalProperties=true
# 允许但未显式声明）。断言以实际行为为准放行。
RUNTIME_ENVELOPE_EXTRAS = {
    ("GET", "/api/v1/front-desk/departures"): frozenset({"meta"}),
    ("GET", "/api/v1/front-desk/in-house"): frozenset({"meta"}),
    ("GET", "/api/v1/front-desk/reservations"): frozenset({"meta"}),
    ("GET", "/api/v1/front-desk/rooms/assignable"): frozenset({"meta"}),
    ("GET", "/api/v1/front-desk/stays/{stay_id}/available-rooms"): frozenset({"meta"}),
}


def _deref_component(schema):
    """把 {$ref: #/components/schemas/X} 解析到 components/schemas 一层。"""
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = str(ref).rsplit("/", 1)[-1]
    return _CONTRACT.get("components", {}).get("schemas", {}).get(name, {})


def contract_response_shape(method, path):
    """返回 (2xx 200/201 schema 顶层 properties 键集, 允许的 2xx 状态码列表)。

    schema 级 $ref 解析到 components/schemas 一层；多个 2xx 时取字典序首个
    （approvals/request 的 200/201 声明同构）。
    """
    op = _CONTRACT["paths"][path][method.lower()]
    codes = sorted(code for code in op["responses"] if str(code).startswith("2"))
    schema = op["responses"][codes[0]].get("content", {}).get(
        "application/json", {}).get("schema", {})
    schema = _deref_component(schema)
    return set(schema.get("properties", {}).keys()), [int(c) for c in codes]

def _contract_required_keys(method, path):
    """契约 2xx schema 的 required 键集（取首 2xx 响应，$ref 解析一层）。"""
    op = _CONTRACT["paths"][path][method.lower()]
    codes = sorted(code for code in op["responses"] if str(code).startswith("2"))
    schema = op["responses"][codes[0]].get("content", {}).get(
        "application/json", {}).get("schema", {})
    schema = _deref_component(schema)
    return set(schema.get("required", ()))




def contract_data_keys(method, path):
    """2xx schema 内 data 属性的键集（对象/数组 items 各解析一层）。"""
    op = _CONTRACT["paths"][path][method.lower()]
    codes = sorted(code for code in op["responses"] if str(code).startswith("2"))
    schema = op["responses"][codes[0]].get("content", {}).get(
        "application/json", {}).get("schema", {})
    schema = _deref_component(schema)
    data_schema = _deref_component(schema.get("properties", {}).get("data", {}))
    if data_schema.get("type") == "array":
        data_schema = _deref_component(data_schema.get("items", {}))
    return set(data_schema.get("properties", {}).keys())


class PartialContractVerificationBase(TestCase):
    """DEFAULT 物业 + 库存日历 + 九角色岗位账号工厂 + 契约键断言助手。

    角色账号沿用迁移预置的九岗位组（apps/identity/roles.py 白名单），
    身份链对齐 test_e2e_flows._make_staff；本包覆盖其中六个岗位
    （店长/前台/财务/值班经理/客房服务员/餐饮服务员）。
    """

    maxDiff = None

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.today = timezone.localdate()
        self._seq = 0
        self._setup_inventory()
        self.owner = self._make_staff("pcv_owner", "店长")
        self.front = self._make_staff("pcv_front", "前台")
        self.finance = self._make_staff("pcv_finance", "财务")
        self.duty = self._make_staff("pcv_duty", "值班经理")
        self.housekeeper = self._make_staff("pcv_housekeeper", "客房服务员")
        self.catering = self._make_staff("pcv_catering", "餐饮服务员")

    # ------------------------------------------------------------------
    # 基础数据（对齐 test_e2e_flows.E2EFlowTestBase._setup_inventory）
    # ------------------------------------------------------------------
    def _setup_inventory(self):
        self.room_type = RoomType.objects.create(
            property=self.property, code="PCV-KING",
            name="契约核验大床房", capacity=2)
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="PCV-BAR",
            name="契约核验门市价", valid_from=self.today - timedelta(days=1))
        for offset in range(4):
            DailyRate.objects.create(
                property=self.property, rate_plan=self.rate_plan,
                room_type=self.room_type, stay_date=self.today + timedelta(days=offset),
                price=Decimal("300.00"))
            RoomTypeInventory.objects.create(
                property=self.property, room_type=self.room_type,
                stay_date=self.today + timedelta(days=offset), total_rooms=5)
        self.room1 = self._make_room("PCV-8201")
        self.room2 = self._make_room("PCV-8202")
        self.room3 = self._make_room("PCV-8203")

    def _make_room(self, number, floor="8"):
        return Room.objects.create(
            property=self.property, room_type=self.room_type,
            number=number, floor=floor,
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL)

    # ------------------------------------------------------------------
    # 员工账号工厂（对齐 test_e2e_flows._make_staff 身份链）
    # ------------------------------------------------------------------
    def _make_staff(self, username, group_name):
        user = User.objects.create_user(
            username=username, password=_TEST_PW_3D7B9A21)
        profile = StaffProfile.objects.create(
            user=user, full_name=username,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile, property=self.property,
            employee_no=f"PCV-{username}",
            status=PropertyStaffMembership.Status.ACTIVE)
        group = Group.objects.get(name=group_name)
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=timezone.now() - timedelta(minutes=1))
        return user

    # ------------------------------------------------------------------
    # HTTP / 业务对象工厂（对齐既有测试模式，不重新发明）
    # ------------------------------------------------------------------
    def _post_json(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload, ensure_ascii=False),
            content_type="application/json")

    def _make_guest(self, name=None):
        self._seq += 1
        return Guest.objects.create(
            property=self.property, full_name=name or f"契约核验住客{self._seq:02d}",
            phone=f"138{self._seq:08d}",
            phone_normalized=f"138{self._seq:08d}")

    def _make_reservation(self, *, status=Reservation.Status.CONFIRMED, nights=1,
                          check_in=None):
        """预订主单 + 房间明细 + 主账单（造数模式对齐 test_billing_center）。"""
        self._seq += 1
        guest = self._make_guest()
        check_in = check_in or self.today
        check_out = check_in + timedelta(days=nights)
        reservation = Reservation.objects.create(
            property=self.property, booking_no=f"PCV{self._seq:06d}",
            guest=guest, status=status,
            check_in=check_in, check_out=check_out,
            room_count=1, adult_count=1, total_amount=Decimal("300.00"))
        line = ReservationRoom.objects.create(
            property=self.property, reservation=reservation,
            room_type=self.room_type, rate_plan=self.rate_plan,
            check_in=check_in, check_out=check_out,
            nightly_rates={str(check_in): 300.0}, amount=Decimal("300.00"))
        Folio.objects.create(
            property=self.property, reservation=reservation,
            guest=guest, currency="CNY")
        return reservation, line

    def _make_inhouse_stay(self):
        """经前台排房/入住 API 造一名在住客人（链路对齐 test_e2e_flows）。

        返回 (stay_id, reservation, line)；入住要求预订已确认、已排房、
        已建账单、当日营业日未关账（本包不造当日 CLOSED 营业日）。
        """
        reservation, line = self._make_reservation()
        self.client.force_login(self.front)
        assigned = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/assign",
            {"room_id": self.room1.pk})
        self.assertEqual(assigned.status_code, 201, assigned.content)
        checked = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/check-in", {})
        self.assertEqual(checked.status_code, 201, checked.content)
        return checked.json()["data"]["stay_id"], reservation, line

    def _make_membership(self, *, level_code="STANDARD", member_no=None):
        self._seq += 1
        return GuestMembership.objects.create(
            property=self.property, guest=self._make_guest(),
            member_no=member_no or f"PCV-M-{self._seq:04d}",
            level_code=level_code, status=GuestMembership.Status.ACTIVE,
            joined_at=self.today)

    def _make_business_date(self, *, status="CLOSED"):
        """已关账营业日工厂（对齐 test_approval.ApprovalApiTestBase）。"""
        self._seq += 1
        return BusinessDate.objects.create(
            property=self.property,
            business_date=self.today + timedelta(days=self._seq),
            status=status,
            closed_at=timezone.now() if status == "CLOSED" else None)

    def _make_legacy_order(self):
        """旧 hotelapp 客房订单链（对齐 hotelapp/tests.py 造数模式）。

        legacy-reservations/{legacy_id} 端点语义：旧 Accommodation 只读适配器，
        无物业过滤（历史数据面），键结构与 PMS2 预订无关。
        """
        self._seq += 1
        seq = self._seq
        housekeeper = LegacyStaff.objects.create(
            姓名=f"契约保洁{seq:02d}", 职务="服务员", 月薪="3000", 工龄="1")
        room = LegacyRoom.objects.create(
            房间号=920000 + seq, 客房类型="标准间", 价格=300,
            负责人编号=housekeeper, 房间状态="空闲")
        client = LegacyClient.objects.create(
            姓名="契约历史顾客", 身份证号=f"110101199001{seq:02d}{seq:04d}")
        order = LegacyAccommodation.objects.create(
            id=910000 + seq, 顾客id=client, 客房编号=room, 状态="已退房",
            入住时间="2026-11-01", 收款金额="300", 联系电话=f"138{seq:08d}")
        LegacyFolio.objects.create(
            order=order, 类型="房费", 金额="300", 事由="契约核验历史房费",
            日期="2026-11-01", 经手人="契约核验")
        return order

    def _make_reopen_request_via_api(self, requester):
        """经通用审批面发起 night_audit.reopen 申请（对齐 test_approval）。

        返回 (response, record)；发起人为值班经理（持有 business_date_reopen）。
        """
        bd = self._make_business_date()
        self.client.force_login(requester)
        response = self._post_json("/api/v1/approvals/request", {
            "action_code": "night_audit.reopen",
            "entity_type": "BusinessDate",
            "entity_id": str(bd.pk),
            "reason": "契约核验：夜审后补记漏记凭证",
            "payload": {"business_date": bd.business_date.isoformat()},
        })
        record = ApprovalRecord.objects.get(pk=response.json()["data"]["id"])
        return response, record

    # ------------------------------------------------------------------
    # 契约键断言助手
    # ------------------------------------------------------------------
    def assertTopLevelKeysWithinContract(self, method, path, response,
                                         finding_extras=()):
        """实际响应顶层键 ⊆ 契约声明键 ∪ 分页封套键 ∪ P4-FINDING 观测键；
        并反向断言契约声明键不在运行时缺失。"""
        payload = response.json()
        declared, _ = contract_response_shape(method, path)
        allowed = (declared | PAGINATION_LIST_KEYS
                   | set(finding_extras)
                   | RUNTIME_ENVELOPE_EXTRAS.get((method, path), frozenset()))
        unexpected = set(payload) - allowed
        self.assertEqual(
            unexpected, set(),
            f"{method} {path} 实际响应顶层键超出契约声明 ∪ 允许封套键：{unexpected}")
        # 可选声明键（未入 required，如 fd 封套的可选 meta）不作"必须在场"断言；
        # 仅 required 键缺失才算契约违约（2026-11-16 fd 封套补可选 meta 后同步口径）。
        schema_required = set(_contract_required_keys(method, path))
        missing = {k for k in (declared & schema_required) if k not in payload}
        self.assertEqual(
            missing, set(),
            f"{method} {path} 契约 required 顶层键在实际响应中缺失：{missing}")

    def assertDataKeysWithinContract(self, method, path, response,
                                     finding_extras=()):
        """嵌套 data 键 ⊆ 契约 data schema properties ∪ P4-FINDING 观测键。"""
        payload = response.json()
        declared = contract_data_keys(method, path)
        actual = set(payload["data"].keys())
        unexpected = actual - declared - set(finding_extras)
        self.assertEqual(
            unexpected, set(),
            f"{method} {path} data 键超出契约声明 ∪ 观测键：{unexpected}")


class ContractSnapshotIntegrityTests(SimpleTestCase):
    """快照完整性守卫：全量 OpenAPI operation 均为 verified，不允许回退为 partial。"""

    def test_all_openapi_operations_are_verified(self):
        paths = cast(dict[str, dict[str, Any]], _CONTRACT["paths"])
        http_methods = {"get", "post", "put", "patch", "delete", "options", "head"}
        operation_count = 0
        operation_ids = []
        for path, path_item in paths.items():
            for method, operation in path_item.items():
                if method.lower() not in http_methods:
                    continue
                operation_count += 1
                with self.subTest(operation=f"{method.upper()} {path}"):
                    self.assertEqual(
                        operation.get("x-contract-status"),
                        "verified",
                        "全量 operation 必须保持 verified，不得回退为 partial",
                    )
                    operation_id = operation.get("operationId")
                    self.assertTrue(operation_id, "每个 operation 必须有 operationId")
                    operation_ids.append(operation_id)
        self.assertGreater(operation_count, 0)
        self.assertEqual(
            len(operation_ids),
            len(set(operation_ids)),
            "OpenAPI operationId 必须全局唯一",
        )


# ---------------------------------------------------------------------------
# 会员中心（GET /memberships、GET /memberships/{id}、POST create、POST level）
# 门禁：view_vip / add_vip / change_vip；匿名与无权者 → 403 PERMISSION_DENIED
# （guest_center/staff_center 族无独立认证前置，匿名落在权限判定 403）。
# ---------------------------------------------------------------------------
class MembershipCenterContractTests(PartialContractVerificationBase):

    def test_membership_list_ok_for_front_desk_and_keys_within_contract(self):
        membership = self._make_membership()
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/memberships")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn(membership.pk, [row["id"] for row in payload["data"]])
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/memberships", response)

    def test_membership_list_anonymous_gets_403(self):
        response = self.client.get("/api/v1/memberships")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_membership_list_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/memberships")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_membership_detail_ok_for_front_desk_and_keys_within_contract(self):
        membership = self._make_membership(level_code="GOLD")
        self.client.force_login(self.front)
        response = self.client.get(f"/api/v1/memberships/{membership.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["id"], membership.pk)
        self.assertEqual(response.json()["data"]["level_code"], "GOLD")
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/memberships/{membership_id}", response)

    def test_membership_detail_anonymous_gets_403(self):
        response = self.client.get("/api/v1/memberships/1")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_membership_detail_housekeeper_gets_403(self):
        membership = self._make_membership()
        self.client.force_login(self.housekeeper)
        response = self.client.get(f"/api/v1/memberships/{membership.pk}")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_membership_create_ok_for_front_desk_and_persists(self):
        guest = self._make_guest()
        self.client.force_login(self.front)
        response = self._post_json("/api/v1/memberships/create", {
            "guest_id": guest.pk, "member_no": "PCV-M-CREATE-001",
            "level_code": "STANDARD"})
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertTrue(payload["data"]["id"])
        membership = GuestMembership.objects.get(pk=payload["data"]["id"])
        self.assertEqual(membership.member_no, "PCV-M-CREATE-001")
        self.assertEqual(membership.guest.pk, guest.pk)
        self.assertTopLevelKeysWithinContract(
            "POST", "/api/v1/memberships/create", response)

    def test_membership_create_invalid_guest_id_gets_400(self):
        self.client.force_login(self.front)
        response = self._post_json(
            "/api/v1/memberships/create", {"guest_id": "abc"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_membership_create_anonymous_gets_403(self):
        response = self._post_json(
            "/api/v1/memberships/create", {"guest_id": 1})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_membership_create_housekeeper_gets_403(self):
        guest = self._make_guest()
        self.client.force_login(self.housekeeper)
        response = self._post_json(
            "/api/v1/memberships/create", {"guest_id": guest.pk})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_membership_level_change_ok_for_owner_and_writes_history(self):
        # 等级变更 change_vip 仅管理岗持有（前台无此权限）；店长为合法角色。
        membership = self._make_membership(level_code="STANDARD")
        self.client.force_login(self.owner)
        response = self._post_json(
            f"/api/v1/memberships/{membership.pk}/level",
            {"level_code": "GOLD", "reason": "契约核验：长期入住升级"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["level_code"], "GOLD")
        membership.refresh_from_db()
        self.assertEqual(membership.level_code, "GOLD")
        self.assertTrue(MembershipLevelHistory.objects.filter(
            membership=membership, from_level="STANDARD",
            to_level="GOLD").exists())
        self.assertTopLevelKeysWithinContract(
            "POST", "/api/v1/memberships/{membership_id}/level", response)

    def test_membership_level_change_without_reason_gets_400(self):
        membership = self._make_membership()
        self.client.force_login(self.owner)
        response = self._post_json(
            f"/api/v1/memberships/{membership.pk}/level",
            {"level_code": "GOLD"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_membership_level_change_anonymous_gets_403(self):
        membership = self._make_membership()
        response = self._post_json(
            f"/api/v1/memberships/{membership.pk}/level",
            {"level_code": "GOLD", "reason": "x"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_membership_level_change_housekeeper_gets_403(self):
        membership = self._make_membership()
        self.client.force_login(self.housekeeper)
        response = self._post_json(
            f"/api/v1/memberships/{membership.pk}/level",
            {"level_code": "GOLD", "reason": "越权尝试"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")


# ---------------------------------------------------------------------------
# 住客中心（GET /guests、POST /guests/create）
# 门禁：view_client / add_client（前台持有）；匿名 → 403。
# ---------------------------------------------------------------------------
class GuestCenterContractTests(PartialContractVerificationBase):

    def test_guest_list_ok_for_front_desk_and_keys_within_contract(self):
        guest = self._make_guest()
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/guests")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(guest.pk, [row["id"] for row in response.json()["data"]])
        self.assertTopLevelKeysWithinContract("GET", "/api/v1/guests", response)

    def test_guest_list_anonymous_gets_403(self):
        response = self.client.get("/api/v1/guests")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_guest_list_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/guests")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_guest_create_ok_for_front_desk_and_persists(self):
        self.client.force_login(self.front)
        response = self._post_json("/api/v1/guests/create", {
            "full_name": "契约核验新建住客", "phone": "13957712345"})
        self.assertEqual(response.status_code, 201, response.content)
        guest_id = response.json()["data"]["id"]
        guest = Guest.objects.get(pk=guest_id)
        self.assertEqual(guest.full_name, "契约核验新建住客")
        self.assertTopLevelKeysWithinContract(
            "POST", "/api/v1/guests/create", response)

    def test_guest_create_non_object_body_gets_400(self):
        self.client.force_login(self.front)
        response = self.client.post(
            "/api/v1/guests/create", data="[1, 2]",
            content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_guest_create_anonymous_gets_403(self):
        response = self._post_json(
            "/api/v1/guests/create", {"full_name": "匿名访客"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_guest_create_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self._post_json(
            "/api/v1/guests/create", {"full_name": "越权住客"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")


# ---------------------------------------------------------------------------
# 房态与房价（GET /rooms/status、GET /room-types、GET /rate-calendar、
# POST /daily-rates/{rate_id}/adjust）
# 读门禁 view_room（九岗位中餐饮服务员无任何权限码 → 明显越权样本）；
# 调价门禁 inventory.manage_rates（仅店长，矩阵外点 F-09）。
# ---------------------------------------------------------------------------
class RoomRateContractTests(PartialContractVerificationBase):

    def test_room_status_ok_for_front_desk_and_keys_within_contract(self):
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/rooms/status")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"])
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/rooms/status", response)

    def test_room_status_anonymous_gets_403(self):
        response = self.client.get("/api/v1/rooms/status")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_room_status_catering_waiter_gets_403(self):
        # 餐饮服务员为迁移预置的零权限岗位组，对房态读构成明显越权。
        self.client.force_login(self.catering)
        response = self.client.get("/api/v1/rooms/status")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_room_type_list_ok_for_front_desk_and_keys_within_contract(self):
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/room-types")
        self.assertEqual(response.status_code, 200, response.content)
        codes = [row["code"] for row in response.json()["data"]]
        self.assertIn("PCV-KING", codes)
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/room-types", response)

    def test_room_type_list_anonymous_gets_403(self):
        response = self.client.get("/api/v1/room-types")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_room_type_list_catering_waiter_gets_403(self):
        self.client.force_login(self.catering)
        response = self.client.get("/api/v1/room-types")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_rate_calendar_ok_for_front_desk_and_keys_within_contract(self):
        self.client.force_login(self.front)
        response = self.client.get(
            f"/api/v1/rate-calendar?start={self.today.isoformat()}&days=2")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["data"]["rates"])
        self.assertTrue(payload["data"]["inventory"])
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/rate-calendar", response)
        self.assertDataKeysWithinContract(
            "GET", "/api/v1/rate-calendar", response,
            finding_extras={"inventory_pagination", "rates_pagination"})
        # P4-FINDING F6: 契约 RateCalendarData 只声明 inventory/rates，
        # 实际 data 含 inventory_pagination/rates_pagination（P0-03 双集合
        # 分页补齐晚于契约快照，additionalProperties=true 允许）；
        # 断言以实际行为为准放行（观测键已并入上方 finding_extras）。

    def test_rate_calendar_anonymous_gets_403(self):
        response = self.client.get("/api/v1/rate-calendar")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_rate_calendar_catering_waiter_gets_403(self):
        self.client.force_login(self.catering)
        response = self.client.get("/api/v1/rate-calendar")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_daily_rate_adjust_ok_for_owner_and_updates_price(self):
        # 调价权限 inventory.manage_rates 仅店长持有（矩阵外点 F-09）。
        rate = DailyRate.objects.get(
            property=self.property, room_type=self.room_type,
            stay_date=self.today)
        self.client.force_login(self.owner)
        response = self._post_json(
            f"/api/v1/daily-rates/{rate.pk}/adjust",
            {"price": "350.00", "reason": "契约核验：旺季调价"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["changed"])
        rate.refresh_from_db()
        self.assertEqual(rate.price, Decimal("350.00"))
        self.assertTopLevelKeysWithinContract(
            "POST", "/api/v1/daily-rates/{rate_id}/adjust", response)

    def test_daily_rate_adjust_non_object_body_gets_400(self):
        rate = DailyRate.objects.get(
            property=self.property, room_type=self.room_type,
            stay_date=self.today)
        self.client.force_login(self.owner)
        response = self.client.post(
            f"/api/v1/daily-rates/{rate.pk}/adjust", data='"350"',
            content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_daily_rate_adjust_anonymous_gets_403(self):
        rate = DailyRate.objects.get(
            property=self.property, room_type=self.room_type,
            stay_date=self.today)
        response = self._post_json(
            f"/api/v1/daily-rates/{rate.pk}/adjust",
            {"price": "350.00", "reason": "匿名调价"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_daily_rate_adjust_front_desk_gets_403(self):
        # 前台为明显无权角色：manage_rates 仅店长（矩阵外点 F-09）。
        rate = DailyRate.objects.get(
            property=self.property, room_type=self.room_type,
            stay_date=self.today)
        self.client.force_login(self.front)
        response = self._post_json(
            f"/api/v1/daily-rates/{rate.pk}/adjust",
            {"price": "350.00", "reason": "前台越权调价"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
        rate.refresh_from_db()
        self.assertEqual(rate.price, Decimal("300.00"), "越权调用不得改价")


# ---------------------------------------------------------------------------
# 财务汇总（GET /financial-summary）
# 门禁 finance_access（值班经理/财务/店长）；客房服务员明显越权 → 403。
# ---------------------------------------------------------------------------
class FinancialSummaryContractTests(PartialContractVerificationBase):

    def test_financial_summary_ok_for_duty_manager_and_keys_within_contract(self):
        self.client.force_login(self.duty)
        response = self.client.get("/api/v1/financial-summary")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn("total_revenue", payload["data"])
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/financial-summary", response)
        self.assertDataKeysWithinContract(
            "GET", "/api/v1/financial-summary", response,
            finding_extras={"metadata"})
        # P4-FINDING F7: 契约 FinancialSummaryData 未声明 metadata，
        # 实际 data 含 metadata（P0-07 统计口径注记晚于契约快照，
        # additionalProperties=true 允许）；断言以实际行为为准放行。

    def test_financial_summary_anonymous_gets_403(self):
        response = self.client.get("/api/v1/financial-summary")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_financial_summary_housekeeper_gets_403(self):
        # 客房服务员调财务端点：无 finance_access → 403。
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/financial-summary")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")


# ---------------------------------------------------------------------------
# 审批（GET /approvals、POST /approvals/request、POST approve、POST reject）
# 门禁：认证前置 _require_membership（匿名 401）+ billing.approval_view /
# ApprovalActionRegistry 权限码（越权 403）。approve/reject 用 night_audit.reopen
# 记录驱动（billing.refund 记录在通用面上被 409 拦截，必须走退款专用入口）。
# ---------------------------------------------------------------------------
class ApprovalContractTests(PartialContractVerificationBase):

    def test_approval_list_ok_for_duty_manager_and_keys_within_contract(self):
        _, record = self._make_reopen_request_via_api(self.duty)
        self.client.force_login(self.duty)
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(record.pk, [row["id"] for row in response.json()["data"]])
        self.assertTopLevelKeysWithinContract("GET", "/api/v1/approvals", response)

    def test_approval_list_anonymous_gets_401(self):
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_approval_list_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/approvals")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_approval_request_ok_for_duty_manager_and_creates_record(self):
        response, record = self._make_reopen_request_via_api(self.duty)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["data"]["created"])
        record.refresh_from_db()
        self.assertEqual(record.status, ApprovalRecord.Status.PENDING)
        self.assertEqual(record.action_code, "night_audit.reopen")
        self.assertTopLevelKeysWithinContract(
            "POST", "/api/v1/approvals/request", response)

    def test_approval_request_unknown_action_code_gets_400(self):
        self.client.force_login(self.duty)
        response = self._post_json("/api/v1/approvals/request", {
            "action_code": "billing.void_everything",
            "entity_type": "Refund", "entity_id": "1", "reason": "未知动作"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_approval_request_anonymous_gets_401(self):
        response = self._post_json("/api/v1/approvals/request", {
            "action_code": "night_audit.reopen",
            "entity_type": "BusinessDate", "entity_id": "1", "reason": "匿名"})
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_approval_request_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self._post_json("/api/v1/approvals/request", {
            "action_code": "night_audit.reopen",
            "entity_type": "BusinessDate", "entity_id": "1", "reason": "越权"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_approval_approve_ok_for_owner_and_updates_status(self):
        # SoD：发起（值班经理）≠ 审批（店长），店长持有 business_date_reopen。
        _, record = self._make_reopen_request_via_api(self.duty)
        self.client.force_login(self.owner)
        response = self._post_json(
            f"/api/v1/approvals/{record.pk}/approve", {})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["data"]["status"], ApprovalRecord.Status.APPROVED)
        record.refresh_from_db()
        self.assertEqual(record.status, ApprovalRecord.Status.APPROVED)
        self.assertTopLevelKeysWithinContract(
            "POST", "/api/v1/approvals/{record_id}/approve", response)
        # 说明：approve 不解析请求体（任何载荷同等处理），无 400 路径——
        # "非法载荷 400" 断言对本端点不适用。

    def test_approval_approve_anonymous_gets_401(self):
        response = self._post_json("/api/v1/approvals/1/approve", {})
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_approval_approve_housekeeper_gets_403(self):
        _, record = self._make_reopen_request_via_api(self.duty)
        self.client.force_login(self.housekeeper)
        response = self._post_json(
            f"/api/v1/approvals/{record.pk}/approve", {})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
        record.refresh_from_db()
        self.assertEqual(
            record.status, ApprovalRecord.Status.PENDING, "越权不得改状态")

    def test_approval_reject_ok_for_owner_and_updates_status(self):
        _, record = self._make_reopen_request_via_api(self.duty)
        self.client.force_login(self.owner)
        response = self._post_json(
            f"/api/v1/approvals/{record.pk}/reject",
            {"reason": "契约核验：凭证不符驳回"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["data"]["status"], ApprovalRecord.Status.REJECTED)
        record.refresh_from_db()
        self.assertEqual(record.status, ApprovalRecord.Status.REJECTED)
        self.assertTopLevelKeysWithinContract(
            "POST", "/api/v1/approvals/{record_id}/reject", response)

    def test_approval_reject_non_object_body_returns_400(self):
        _, record = self._make_reopen_request_via_api(self.duty)
        self.client.force_login(self.owner)
        response = self.client.post(
            f"/api/v1/approvals/{record.pk}/reject", data='["reason"]',
            content_type="application/json")
        # F8 已修复（2026-11-16）：approval.py 非 JSON 对象体显式
        # 400 VALIDATION_ERROR（WB-46 同款口径），恢复契约声明行为。
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_approval_reject_anonymous_gets_401(self):
        response = self._post_json(
            "/api/v1/approvals/1/reject", {"reason": "匿名驳回"})
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_approval_reject_housekeeper_gets_403(self):
        _, record = self._make_reopen_request_via_api(self.duty)
        self.client.force_login(self.housekeeper)
        response = self._post_json(
            f"/api/v1/approvals/{record.pk}/reject", {"reason": "越权驳回"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")


# ---------------------------------------------------------------------------
# 审计时间线（GET /audit-timeline）
# 门禁 core.audit_view（值班经理/店长）；匿名与客房服务员 → 403。
# ---------------------------------------------------------------------------
class AuditTimelineContractTests(PartialContractVerificationBase):

    def test_audit_timeline_ok_for_duty_manager_and_keys_within_contract(self):
        AuditEvent.record(
            action="contract.verify.event", entity_type="ContractProbe",
            entity_id="1", actor=self.duty, property_obj=self.property,
            reason="契约核验事件")
        self.client.force_login(self.duty)
        response = self.client.get("/api/v1/audit-timeline")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"])
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/audit-timeline", response)

    def test_audit_timeline_anonymous_gets_403(self):
        response = self.client.get("/api/v1/audit-timeline")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_audit_timeline_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/audit-timeline")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")


# ---------------------------------------------------------------------------
# 预订管理（GET /reservations-mgmt、GET /reservations-mgmt/{booking_no}）
# 门禁 view_accommodation（前台持有）；匿名 → 403；客房服务员 → 403。
# ---------------------------------------------------------------------------
class ReservationMgmtContractTests(PartialContractVerificationBase):

    def test_reservation_mgmt_list_ok_for_front_desk_and_keys_within_contract(self):
        reservation, _ = self._make_reservation()
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/reservations-mgmt")
        self.assertEqual(response.status_code, 200, response.content)
        nos = [row["booking_no"] for row in response.json()["data"]]
        self.assertIn(reservation.booking_no, nos)
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/reservations-mgmt", response)

    def test_reservation_mgmt_list_anonymous_gets_403(self):
        response = self.client.get("/api/v1/reservations-mgmt")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_reservation_mgmt_list_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/reservations-mgmt")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_reservation_mgmt_detail_ok_for_front_desk_and_keys_within_contract(self):
        reservation, _ = self._make_reservation()
        self.client.force_login(self.front)
        response = self.client.get(
            f"/api/v1/reservations-mgmt/{reservation.booking_no}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["data"]["booking_no"], reservation.booking_no)
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/reservations-mgmt/{booking_no}", response)

    def test_reservation_mgmt_detail_unknown_booking_no_gets_404(self):
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/reservations-mgmt/PCV-NOT-EXIST")
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_reservation_mgmt_detail_anonymous_gets_403(self):
        response = self.client.get("/api/v1/reservations-mgmt/PCV-ANY")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_reservation_mgmt_detail_housekeeper_gets_403(self):
        reservation, _ = self._make_reservation()
        self.client.force_login(self.housekeeper)
        response = self.client.get(
            f"/api/v1/reservations-mgmt/{reservation.booking_no}")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")


# ---------------------------------------------------------------------------
# 前台只读端点（departures / in-house / reservations / reservations/{booking_no}
# / rooms/assignable / stays/{stay_id}/available-rooms / legacy-reservations/{id}）
# 门禁 _front_desk_required：认证（匿名 401）+ DEFAULT 物业任职 +
# view_accommodation（客房服务员无此权限 → 403 PERMISSION_DENIED）。
# ---------------------------------------------------------------------------
class FrontDeskContractTests(PartialContractVerificationBase):

    def test_departures_ok_for_front_desk_and_keys_within_contract(self):
        _, _, line = self._make_inhouse_stay()
        self.client.force_login(self.front)
        response = self.client.get(
            f"/api/v1/front-desk/departures?date={line.check_out.isoformat()}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"], "当日待离店在住记录非空")
        # P4-FINDING F1: 契约 200 schema properties 只声明 data，实际响应含
        # meta（分页 + date + source_system）；快照描述已注明"列表附 meta 分页"，
        # additionalProperties=true 允许但键未显式声明——以实际行为为准放行。
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/front-desk/departures", response)

    def test_departures_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/departures")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_departures_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/front-desk/departures")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_in_house_ok_for_front_desk_and_keys_within_contract(self):
        self._make_inhouse_stay()
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/front-desk/in-house")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"], "在住列表非空")
        # P4-FINDING F2: 同 F1——契约只声明 data，实际响应含 meta 分页键。
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/front-desk/in-house", response)

    def test_in_house_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/in-house")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_in_house_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/front-desk/in-house")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_front_desk_reservations_ok_for_front_desk_and_keys_within_contract(self):
        reservation, _ = self._make_reservation()
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/front-desk/reservations")
        self.assertEqual(response.status_code, 200, response.content)
        nos = [row["booking_no"] for row in response.json()["data"]]
        self.assertIn(reservation.booking_no, nos)
        # P4-FINDING F3: 同 F1——契约只声明 data，实际响应含 meta 分页键。
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/front-desk/reservations", response)

    def test_front_desk_reservations_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/reservations")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_front_desk_reservations_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/front-desk/reservations")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_front_desk_reservation_detail_ok_and_keys_within_contract(self):
        reservation, _ = self._make_reservation()
        self.client.force_login(self.front)
        response = self.client.get(
            f"/api/v1/front-desk/reservations/{reservation.booking_no}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["data"]["booking_no"], reservation.booking_no)
        # 该端点实际响应仅 {data}（无 meta），与契约声明一致。
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/front-desk/reservations/{booking_no}", response)

    def test_front_desk_reservation_detail_unknown_booking_no_gets_404(self):
        self.client.force_login(self.front)
        response = self.client.get(
            "/api/v1/front-desk/reservations/PCV-NOT-EXIST")
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_front_desk_reservation_detail_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/reservations/PCV-ANY")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_front_desk_reservation_detail_housekeeper_gets_403(self):
        reservation, _ = self._make_reservation()
        self.client.force_login(self.housekeeper)
        response = self.client.get(
            f"/api/v1/front-desk/reservations/{reservation.booking_no}")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_assignable_rooms_ok_for_front_desk_and_keys_within_contract(self):
        # 未排房的已确认预订明细：可排候选房非空（room2/room3 空闲干净）。
        _, line = self._make_reservation()
        self.client.force_login(self.front)
        response = self.client.get(
            f"/api/v1/front-desk/rooms/assignable?reservation_room_id={line.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        numbers = [row["number"] for row in response.json()["data"]]
        self.assertIn("PCV-8202", numbers)
        # P4-FINDING F4: 同 F1——契约只声明 data，实际响应含 meta
        # （total + reservation_room_id）。
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/front-desk/rooms/assignable", response)

    def test_assignable_rooms_missing_param_gets_400(self):
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/front-desk/rooms/assignable")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_assignable_rooms_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/rooms/assignable")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_assignable_rooms_housekeeper_gets_403(self):
        self.client.force_login(self.housekeeper)
        response = self.client.get("/api/v1/front-desk/rooms/assignable")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_available_stay_rooms_ok_for_front_desk_and_keys_within_contract(self):
        stay_id, _, _ = self._make_inhouse_stay()
        self.client.force_login(self.front)
        response = self.client.get(
            f"/api/v1/front-desk/stays/{stay_id}/available-rooms")
        self.assertEqual(response.status_code, 200, response.content)
        numbers = [row["number"] for row in response.json()["data"]]
        self.assertIn("PCV-8202", numbers, "同房型空闲干净房为换房候选")
        # P4-FINDING F5: 同 F1——契约只声明 data，实际响应含 meta
        # （total + stay_id）。
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/front-desk/stays/{stay_id}/available-rooms", response)

    def test_available_stay_rooms_unknown_stay_gets_404(self):
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/front-desk/stays/999999/available-rooms")
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_available_stay_rooms_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/stays/1/available-rooms")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_available_stay_rooms_housekeeper_gets_403(self):
        stay_id, _, _ = self._make_inhouse_stay()
        self.client.force_login(self.housekeeper)
        response = self.client.get(
            f"/api/v1/front-desk/stays/{stay_id}/available-rooms")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_legacy_reservation_detail_ok_for_front_desk_and_keys_within_contract(self):
        # 端点语义：旧 hotelapp.Accommodation 只读适配器（无物业过滤、
        # 键结构与 PMS2 预订无关）；造数可命中 200 路径，另附 404 错误体断言。
        order = self._make_legacy_order()
        self.client.force_login(self.front)
        response = self.client.get(
            f"/api/v1/front-desk/legacy-reservations/{order.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["data"]["legacy_id"], order.pk)
        self.assertTrue(payload["data"]["legacy_entries"])
        self.assertTopLevelKeysWithinContract(
            "GET", "/api/v1/front-desk/legacy-reservations/{legacy_id}", response)

    def test_legacy_reservation_detail_unknown_id_gets_404_with_error_body(self):
        self.client.force_login(self.front)
        response = self.client.get(
            "/api/v1/front-desk/legacy-reservations/999999")
        self.assertEqual(response.status_code, 404, response.content)
        payload = response.json()
        self.assertEqual(
            payload["error"]["code"], "RESOURCE_NOT_FOUND")
        self.assertEqual(
            set(payload.keys()), {"error"},
            "404 错误体为前台错误封套（仅 error 键，无 data/meta）")

    def test_legacy_reservation_detail_anonymous_gets_401(self):
        response = self.client.get("/api/v1/front-desk/legacy-reservations/1")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_legacy_reservation_detail_housekeeper_gets_403(self):
        order = self._make_legacy_order()
        self.client.force_login(self.housekeeper)
        response = self.client.get(
            f"/api/v1/front-desk/legacy-reservations/{order.pk}")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
