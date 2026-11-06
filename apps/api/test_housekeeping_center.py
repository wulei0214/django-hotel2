# -*- coding: utf-8 -*-
"""§4.3 清洁与维修工作台回归测试包。

覆盖：
  * 读 API 权限四态（匿名 401 / 无权限 403 / 有权限 200 / 跨物业 404）；
  * 工作清单分页契约、稳定排序（floor,number,id）与筛选（楼层/清洁/维修/占用）；
  * 今日统计（各状态计数、待放行数、可售口径、按楼层分布、今日审计流转量）；
  * 清洁链式流转：inventory.housekeeping_update 控制 DIRTY→CLEANING→INSPECTING→CLEAN 合法链 +
    跳步拒绝（DIRTY→CLEAN/DIRTY→INSPECTING 409）+ 非法状态值 400；
  * 净房放行权限（F-08 补执行点）：客房服务员（housekeeping_update 无 room_release_ready）
    →CLEAN 403（API 层 + 服务层双重校验）；值班经理放行成功且可售口径翻转；
  * 维修登记：reason 必填、inventory.maintenance_manage 独立权限口径、登记/恢复/升级链、审计；
  * 占用维度不变量（本工作台不改占用状态）；
  * /manage/housekeeping/ 页面门禁与按钮显隐标志；
  * 注册表同步：room_release_ready 恢复登记且 validate_registry 无 ERROR。
"""
import json

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.core.models import AuditEvent, Property
from apps.identity.authorization import AuthorizationService
from apps.identity.registry import (
    MODULE_REGISTRY_V2, SPECIAL_PERMISSIONS, get_special, registry_errors,
    validate_registry,
)
from apps.inventory.models import Room
from apps.inventory.services import (
    HousekeepingOperationError, change_housekeeping_status,
    change_maintenance_status,
)


class HousekeepingCenterTestBase(E2EFlowTestBase):
    """清洁与维修工作台公共基础：客房服务员 + 值班经理 + 财务只读账号。"""

    def setUp(self):
        super().setUp()
        self.hk = self._make_staff("hk_center_cleaner", "客房服务员")
        self.dm = self._make_staff("hk_center_duty", "值班经理")
        self.no_perm = self._make_finance_view_only("hk_center_noperm")
        self.client.force_login(self.hk)

    def _make_dirty_room(self, number, floor="8", occupied=False):
        return Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number=number,
            floor=floor,
            occupancy_status=(Room.OccupancyStatus.OCCUPIED if occupied
                              else Room.OccupancyStatus.VACANT),
            housekeeping_status=Room.HousekeepingStatus.DIRTY,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )

    def _flow(self, room, target, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.post(
            f"/api/v1/housekeeping/rooms/{room.pk}/status",
            data=json.dumps({"status": target}),
            content_type="application/json")

    def _maintain(self, room, target, reason="空调漏水需停用维修", user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.post(
            f"/api/v1/housekeeping/rooms/{room.pk}/maintenance",
            data=json.dumps({"status": target, "reason": reason}),
            content_type="application/json")


class HousekeepingReadApiTests(HousekeepingCenterTestBase):
    """读端点：权限四态、分页/排序/筛选、统计口径。"""

    def test_read_endpoints_require_authentication(self):
        self.client.logout()
        for url in ("/api/v1/housekeeping/rooms",
                    "/api/v1/housekeeping/summary"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 401, url)
            self.assertEqual(response.json()["error"]["code"],
                             "AUTHENTICATION_REQUIRED", url)

    def test_read_endpoints_require_housekeeping_view_permission(self):
        """读取清洁维修工作台使用独立 inventory.housekeeping_view；财务只读账号 403。"""
        self.client.force_login(self.no_perm)
        self.assertFalse(AuthorizationService.has_permission(
            self.no_perm, self.property, "inventory.housekeeping_view"))
        for url in ("/api/v1/housekeeping/rooms",
                    "/api/v1/housekeeping/summary"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(response.json()["error"]["code"],
                             "PERMISSION_DENIED", url)

    def test_room_list_pagination_order_filters_and_isolation(self):
        other_property = Property.objects.create(code="HK-OTHER", name="其他物业")
        Room.objects.create(
            property=other_property, room_type=self.room_type,
            number="X-9001", floor="9",
            housekeeping_status=Room.HousekeepingStatus.DIRTY)
        room_b1 = self._make_dirty_room("HK-8101", floor="8")
        room_b2 = self._make_dirty_room("HK-8102", floor="8")
        room_b2.maintenance_status = Room.MaintenanceStatus.OUT_OF_ORDER
        room_b2.save(update_fields=("maintenance_status",))
        room_a1 = self._make_dirty_room("HK-0501", floor="5")

        response = self.client.get("/api/v1/housekeeping/rooms")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        # 稳定排序 floor→number→id（含基础夹具 3 间 E2E-82xx，楼层 "8"）；
        # 跨物业房间不出现
        self.assertEqual([item["number"] for item in payload["data"]],
                         ["HK-0501", "E2E-8201", "E2E-8202", "E2E-8203",
                          "HK-8101", "HK-8102"])
        self.assertTrue(all(item["number"] != "X-9001"
                            for item in payload["data"]))
        self.assertEqual(payload["meta"]["pagination"]["total"], 6)
        # 分页 page_size=1 → 第 2 页
        paged = self.client.get(
            "/api/v1/housekeeping/rooms?page=2&page_size=1").json()
        self.assertEqual(paged["data"][0]["number"], "E2E-8201")
        self.assertTrue(paged["meta"]["pagination"]["has_previous"])
        self.assertTrue(paged["meta"]["pagination"]["has_next"])
        # 筛选：楼层 / 清洁 / 维修 / 占用
        floor5 = self.client.get(
            "/api/v1/housekeeping/rooms?floor=5").json()
        self.assertEqual([item["number"] for item in floor5["data"]],
                         ["HK-0501"])
        dirty = self.client.get(
            "/api/v1/housekeeping/rooms?housekeeping=DIRTY").json()
        self.assertEqual(dirty["meta"]["pagination"]["total"], 3)
        abnormal = self.client.get(
            "/api/v1/housekeeping/rooms?maintenance=OUT_OF_ORDER").json()
        self.assertEqual([item["number"] for item in abnormal["data"]],
                         ["HK-8102"])
        vacant = self.client.get(
            "/api/v1/housekeeping/rooms?occupancy=VACANT").json()
        self.assertEqual(vacant["meta"]["pagination"]["total"], 6)
        # 非法筛选与非法分页参数 → 400
        bad = self.client.get("/api/v1/housekeeping/rooms?housekeeping=NOPE")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], "VALIDATION_ERROR")
        bad_page = self.client.get("/api/v1/housekeeping/rooms?page_size=101")
        self.assertEqual(bad_page.status_code, 400)
        return room_a1

    def test_summary_counts_pending_release_sellable_and_floors(self):
        dirty = self._make_dirty_room("HK-8201")
        cleaning = self._make_dirty_room("HK-8202")
        cleaning.housekeeping_status = Room.HousekeepingStatus.CLEANING
        cleaning.save(update_fields=("housekeeping_status",))
        inspecting = self._make_dirty_room("HK-8203")
        inspecting.housekeeping_status = Room.HousekeepingStatus.INSPECTING
        inspecting.save(update_fields=("housekeeping_status",))
        out_of_order = self._make_dirty_room("HK-8204")
        out_of_order.maintenance_status = Room.MaintenanceStatus.OUT_OF_ORDER
        out_of_order.save(update_fields=("maintenance_status",))
        clean_sellable = self._make_dirty_room("HK-8205")
        clean_sellable.housekeeping_status = Room.HousekeepingStatus.CLEAN
        clean_sellable.save(update_fields=("housekeeping_status",))
        occupied_clean = self._make_dirty_room("HK-8206", occupied=True)
        occupied_clean.housekeeping_status = Room.HousekeepingStatus.CLEAN
        occupied_clean.save(update_fields=("housekeeping_status",))

        response = self.client.get("/api/v1/housekeeping/summary")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        # E2E 基础夹具 room1/2/3 默认 CLEAN（楼层 "8"），加上本测试创建的 6 间
        self.assertEqual(data["housekeeping"]["CLEAN"], 5)   # 3 夹具 + 2 本测
        self.assertEqual(data["housekeeping"]["DIRTY"], 2)   # 脏房 + 维修房
        self.assertEqual(data["housekeeping"]["CLEANING"], 1)
        self.assertEqual(data["housekeeping"]["INSPECTING"], 1)
        self.assertEqual(data["maintenance"]["OUT_OF_ORDER"], 1)
        self.assertEqual(data["pending_release"], 1)
        # 可售口径 = 启用+空闲+净房+无维修：仅夹具 3 间 + HK-8205
        self.assertEqual(data["sellable_now"], 4)
        self.assertEqual(data["total_rooms"], 9)
        floors = {item["floor"]: item for item in data["by_floor"]}
        self.assertEqual(floors["8"]["total"], 9)
        self.assertEqual(floors["8"]["maintenance_abnormal"], 1)

    def test_summary_today_audit_counts(self):
        room = self._make_dirty_room("HK-8301")
        self._flow(room, "CLEANING")           # 客房服务员
        self._flow(room, "INSPECTING")
        self._flow(room, "CLEAN", user=self.dm)  # 净房放行
        other = self._make_dirty_room("HK-8302")
        self.client.force_login(self.dm)
        self._maintain(other, "OUT_OF_ORDER")

        data = self.client.get("/api/v1/housekeeping/summary").json()["data"]
        self.assertEqual(data["today"]["housekeeping_changes"], 3)
        self.assertEqual(data["today"]["released"], 1)
        self.assertEqual(data["today"]["maintenance_changes"], 1)


class HousekeepingStatusFlowTests(HousekeepingCenterTestBase):
    """清洁链写端点：链式流转、跳步拒绝、净房放行权限（F-08）。"""

    def test_status_change_requires_authentication(self):
        room = self._make_dirty_room("HK-8401")
        self.client.logout()
        response = self.client.post(
            f"/api/v1/housekeeping/rooms/{room.pk}/status",
            data=json.dumps({"status": "CLEANING"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"],
                         "AUTHENTICATION_REQUIRED")

    def test_status_change_requires_change_room_permission(self):
        room = self._make_dirty_room("HK-8402")
        response = self._flow(room, "CLEANING", user=self.no_perm)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
        room.refresh_from_db()
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.DIRTY)

    def test_housekeeping_chain_legal_flow_by_cleaner(self):
        """DIRTY→CLEANING→INSPECTING 由客房服务员完成；每次落审计。"""
        room = self._make_dirty_room("HK-8403")
        room.occupancy_status = Room.OccupancyStatus.OCCUPIED
        room.save(update_fields=("occupancy_status",))
        for target in ("CLEANING", "INSPECTING"):
            response = self._flow(room, target, user=self.hk)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()["data"]["changed"])
        room.refresh_from_db()
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.INSPECTING)
        # 占用维度不变（入住/退房服务负责）
        self.assertEqual(room.occupancy_status, Room.OccupancyStatus.OCCUPIED)
        events = list(AuditEvent.objects.filter(
            action="housekeeping.status_changed", entity_id=str(room.pk),
            property=self.property).order_by("created_at", "id"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].before_json["housekeeping_status"], "DIRTY")
        self.assertEqual(events[0].after_json["housekeeping_status"],
                         "CLEANING")
        self.assertEqual(events[1].after_json["housekeeping_status"],
                         "INSPECTING")

    def test_inspection_reject_rework_to_dirty(self):
        """INSPECTING→DIRTY 检查不通过返工（change_room 权限内）。"""
        room = self._make_dirty_room("HK-8404")
        room.housekeeping_status = Room.HousekeepingStatus.INSPECTING
        room.save(update_fields=("housekeeping_status",))
        response = self._flow(room, "DIRTY", user=self.hk)
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.DIRTY)

    def test_status_skip_transitions_rejected(self):
        """跳步拒绝（DIRTY→CLEAN / DIRTY→INSPECTING / CLEANING→CLEAN）→ 409。"""
        dirty = self._make_dirty_room("HK-8405")
        for target in ("CLEAN", "INSPECTING"):
            response = self._flow(dirty, target, user=self.dm)  # dm 有放行权
            self.assertEqual(response.status_code, 409, target)
            self.assertEqual(response.json()["error"]["code"],
                             "BUSINESS_CONFLICT", target)
        cleaning = self._make_dirty_room("HK-8406")
        cleaning.housekeeping_status = Room.HousekeepingStatus.CLEANING
        cleaning.save(update_fields=("housekeeping_status",))
        response = self._flow(cleaning, "CLEAN", user=self.dm)
        self.assertEqual(response.status_code, 409)
        dirty.refresh_from_db()
        self.assertEqual(dirty.housekeeping_status,
                         Room.HousekeepingStatus.DIRTY)
        # CLEAN 为终态（弄脏由退房服务负责）→ 409
        clean = self._make_dirty_room("HK-8407")
        clean.housekeeping_status = Room.HousekeepingStatus.CLEAN
        clean.save(update_fields=("housekeeping_status",))
        response = self._flow(clean, "DIRTY", user=self.dm)
        self.assertEqual(response.status_code, 409)
        # 非法状态值 → 400
        response = self._flow(dirty, "SPOTLESS", user=self.hk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_release_requires_room_release_ready_permission(self):
        """F-08 补执行点：客房服务员（无 room_release_ready）→CLEAN 403，
        API 层与领域服务层双重拦截。"""
        room = self._make_dirty_room("HK-8408")
        room.housekeeping_status = Room.HousekeepingStatus.INSPECTING
        room.save(update_fields=("housekeeping_status",))
        self.assertTrue(AuthorizationService.has_permission(
            self.hk, self.property, "inventory.housekeeping_update"))
        self.assertFalse(AuthorizationService.has_permission(
            self.hk, self.property, "hotelapp.room_release_ready"))
        response = self._flow(room, "CLEAN", user=self.hk)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
        # 服务层防御：绕过 API 直调服务同样拒绝
        with self.assertRaises(HousekeepingOperationError):
            change_housekeeping_status(
                property_obj=self.property, room_id=room.pk,
                target_status=Room.HousekeepingStatus.CLEAN,
                changed_by=self.hk)
        room.refresh_from_db()
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.INSPECTING)
        self.assertFalse(AuditEvent.objects.filter(
            action="housekeeping.status_changed", entity_id=str(room.pk),
            property=self.property).exists())

    def test_release_by_duty_manager_flips_sellable_semantics(self):
        """值班经理净房放行：INSPECTING→CLEAN；空闲+净房+无维修 → 可售。"""
        room = self._make_dirty_room("HK-8409")
        room.housekeeping_status = Room.HousekeepingStatus.INSPECTING
        room.save(update_fields=("housekeeping_status",))
        self.client.force_login(self.dm)
        response = self._flow(room, "CLEAN", user=self.dm)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["changed"])
        self.assertEqual(data["housekeeping_status"], "CLEAN")
        self.assertTrue(data["is_sellable_now"])
        room.refresh_from_db()
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.CLEAN)
        self.assertTrue(room.is_sellable_now)
        event = AuditEvent.objects.get(
            action="housekeeping.status_changed", entity_id=str(room.pk))
        self.assertEqual(event.after_json["is_sellable_now"], True)
        self.assertEqual(event.actor_id, self.dm.pk)
        # 幂等空操作：重复放行 changed=false，不新增审计
        repeat = self._flow(room, "CLEAN", user=self.dm)
        self.assertEqual(repeat.status_code, 200)
        self.assertFalse(repeat.json()["data"]["changed"])
        self.assertEqual(AuditEvent.objects.filter(
            action="housekeeping.status_changed",
            entity_id=str(room.pk)).count(), 1)

    def test_release_on_occupied_room_stays_not_sellable(self):
        """净房放行不改占用：在住房间放行为 CLEAN 后仍不可售（语义核对）。"""
        room = self._make_dirty_room("HK-8410", occupied=True)
        room.housekeeping_status = Room.HousekeepingStatus.INSPECTING
        room.save(update_fields=("housekeeping_status",))
        response = self._flow(room, "CLEAN", user=self.dm)
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.CLEAN)
        self.assertEqual(room.occupancy_status, Room.OccupancyStatus.OCCUPIED)
        self.assertFalse(room.is_sellable_now)

    def test_status_change_cross_property_room_404(self):
        other_property = Property.objects.create(code="HK-OTHER2", name="其他物业")
        foreign = Room.objects.create(
            property=other_property, room_type=self.room_type,
            number="F-0001", floor="1",
            housekeeping_status=Room.HousekeepingStatus.DIRTY)
        response = self._flow(foreign, "CLEANING", user=self.dm)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_service_rejects_unknown_room_and_bad_target(self):
        with self.assertRaises(HousekeepingOperationError):
            change_housekeeping_status(
                property_obj=self.property, room_id=999999,
                target_status=Room.HousekeepingStatus.CLEANING,
                changed_by=self.hk)
        room = self._make_dirty_room("HK-8411")
        with self.assertRaises(HousekeepingOperationError):
            change_housekeeping_status(
                property_obj=self.property, room_id=room.pk,
                target_status="WATER",
                changed_by=self.hk)


class HousekeepingMaintenanceTests(HousekeepingCenterTestBase):
    """维修登记：reason 必填、inventory.maintenance_manage 独立权限、登记/恢复链与审计。"""

    def test_maintenance_requires_authentication(self):
        room = self._make_dirty_room("HK-8501")
        self.client.logout()
        response = self.client.post(
            f"/api/v1/housekeeping/rooms/{room.pk}/maintenance",
            data=json.dumps({"status": "OUT_OF_ORDER", "reason": "测试"}),
            content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_maintenance_requires_maintenance_manage_permission(self):
        """维修权限独立于 no_show_handle；客房服务员不得执行维修停用。"""
        room = self._make_dirty_room("HK-8502")
        response = self._maintain(room, "OUT_OF_ORDER", user=self.hk)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status,
                         Room.MaintenanceStatus.NORMAL)

    def test_maintenance_reason_required(self):
        room = self._make_dirty_room("HK-8503")
        self.client.force_login(self.dm)
        for reason in ("", "   ", None, "长" * 241):
            payload = {"status": "OUT_OF_ORDER"}
            if reason is not None:
                payload["reason"] = reason
            response = self.client.post(
                f"/api/v1/housekeeping/rooms/{room.pk}/maintenance",
                data=json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 400, payload)
            self.assertEqual(response.json()["error"]["code"],
                             "VALIDATION_ERROR")
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status,
                         Room.MaintenanceStatus.NORMAL)
        self.assertFalse(AuditEvent.objects.filter(
            action="maintenance.status_changed",
            entity_id=str(room.pk)).exists())

    def test_maintenance_invalid_status_value(self):
        room = self._make_dirty_room("HK-8504")
        response = self._maintain(room, "BROKEN", user=self.dm)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_maintenance_register_restore_and_audit(self):
        """NORMAL→OUT_OF_ORDER 登记 → 恢复 NORMAL；清洁状态独立不联动；
        审计含 reason 与可售翻转。"""
        room = self._make_dirty_room("HK-8505")
        self.client.force_login(self.dm)
        response = self._maintain(room, "OUT_OF_ORDER",
                                  reason="空调漏水停用维修")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["changed"])
        self.assertFalse(data["is_sellable_now"])
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status,
                         Room.MaintenanceStatus.OUT_OF_ORDER)
        # 停用仍可继续清洁链（维修与清洁维度独立）
        flow = self._flow(room, "CLEANING", user=self.hk)
        self.assertEqual(flow.status_code, 200, flow.content)
        # 升级停售 OUT_OF_SERVICE（OUT_OF_ORDER→OUT_OF_SERVICE 合法）
        upgrade = self._maintain(room, "OUT_OF_SERVICE",
                                 reason="结构问题停售", user=self.dm)
        self.assertEqual(upgrade.status_code, 200, upgrade.content)
        # 恢复 NORMAL
        restore = self._maintain(room, "NORMAL",
                                 reason="维修完成验收恢复", user=self.dm)
        self.assertEqual(restore.status_code, 200, restore.content)
        self.assertTrue(restore.json()["data"]["changed"])
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status,
                         Room.MaintenanceStatus.NORMAL)
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.CLEANING)  # 清洁状态独立
        events = list(AuditEvent.objects.filter(
            action="maintenance.status_changed",
            entity_id=str(room.pk)).order_by("created_at", "id"))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].reason, "空调漏水停用维修")
        self.assertEqual(events[0].before_json["maintenance_status"], "NORMAL")
        self.assertEqual(
            events[0].after_json["maintenance_status"], "OUT_OF_ORDER")
        self.assertEqual(
            events[2].after_json["maintenance_status"], "NORMAL")

    def test_maintenance_idempotent_same_status(self):
        room = self._make_dirty_room("HK-8506")
        self.client.force_login(self.dm)
        response = self._maintain(room, "NORMAL", reason="重复恢复")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["changed"])
        self.assertFalse(AuditEvent.objects.filter(
            action="maintenance.status_changed",
            entity_id=str(room.pk)).exists())

    def test_maintenance_cross_property_room_404(self):
        other_property = Property.objects.create(code="HK-OTHER3", name="其他物业")
        foreign = Room.objects.create(
            property=other_property, room_type=self.room_type,
            number="F-0002", floor="1")
        response = self._maintain(foreign, "OUT_OF_ORDER", user=self.dm)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "RESOURCE_NOT_FOUND")


    def test_store_manager_can_register_maintenance(self):
        """店长岗位必须拥有维修停用/恢复专项权限，不能出现全权员工 403。"""
        owner = self._make_staff("hk_center_owner", "店长")
        room = self._make_dirty_room("HK-OWNER-01")
        response = self._maintain(
            room, "OUT_OF_ORDER", reason="店长权限回归测试", user=owner)
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        self.assertEqual(
            room.maintenance_status, Room.MaintenanceStatus.OUT_OF_ORDER)

class HousekeepingPageAndRegistryTests(HousekeepingCenterTestBase):
    """/manage/housekeeping/ 页面门禁与注册表同步。"""

    def test_page_requires_login(self):
        self.client.logout()
        response = self.client.get("/manage/housekeeping/")
        self.assertEqual(response.status_code, 302)

    def test_page_requires_housekeeping_view_permission(self):
        self.client.force_login(self.no_perm)
        response = self.client.get("/manage/housekeeping/")
        self.assertEqual(response.status_code, 403)

    def test_page_renders_for_duty_manager_with_action_flags(self):
        self.client.force_login(self.dm)
        response = self.client.get("/manage/housekeeping/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTemplateUsed(response, "pms/housekeeping_center.html")
        content = response.content.decode("utf-8")
        self.assertIn("清洁与维修工作台", content)
        self.assertIn("/api/v1/housekeeping/rooms", content)
        self.assertIn("const canRelease = true", content)
        self.assertIn("const canMaintain = true", content)

    def test_page_api_parser_handles_html_error_responses(self):
        """403/500 HTML 响应应保留 HTTP 状态，不应直接触发 JSON 解析异常。"""
        self.client.force_login(self.dm)
        response = self.client.get("/manage/housekeeping/")
        self.assertEqual(response.status_code, 200, response.content)
        content = response.content.decode("utf-8")
        self.assertIn("const raw = await response.text()", content)
        self.assertIn("JSON.parse(raw)", content)
        self.assertIn("服务器返回非 JSON 响应（HTTP ", content)
        self.assertNotIn("const payload = await response.json()", content)

    def test_filter_controls_refresh_room_grid_without_query_click(self):
        self.client.force_login(self.dm)
        response = self.client.get("/manage/housekeeping/")
        self.assertEqual(response.status_code, 200, response.content)
        content = response.content.decode("utf-8")
        self.assertIn('getElementById("hk-hk").addEventListener("change"', content)
        self.assertIn('getElementById("hk-mt").addEventListener("change"', content)
        self.assertIn('event.key === "Enter"', content)
        self.assertIn('id="hk-page-size"', content)
        self.assertIn('id="hk-goto-page"', content)

    def test_page_flags_hidden_without_special_permissions(self):
        """客房服务员：清洁流转可见；净房放行/维修登记不渲染（按钮显隐）。"""
        self.client.force_login(self.hk)
        response = self.client.get("/manage/housekeeping/")
        self.assertEqual(response.status_code, 200, response.content)
        content = response.content.decode("utf-8")
        self.assertIn("const canFlow = true", content)
        self.assertIn("const canRelease = false", content)
        self.assertIn("const canMaintain = false", content)

    def test_sidebar_entry_under_housekeeping_group(self):
        self.client.force_login(self.dm)
        response = self.client.get("/manage/housekeeping/")
        content = response.content.decode("utf-8")
        self.assertIn('href="/manage/housekeeping/"', content)
        self.assertIn("客房运营", content)

    def test_registry_restores_room_release_ready_without_errors(self):
        """F-08 补执行点登记：SPECIAL_PERMISSIONS + rooms 模块 special 元组，
        validate_registry 无 ERROR。"""
        item = get_special("hotelapp.room_release_ready")
        self.assertIsNotNone(item)
        self.assertEqual(item["module"], "rooms")
        self.assertIn("hotelapp.room_release_ready",
                      MODULE_REGISTRY_V2["rooms"]["actions"]["special"])
        self.assertIn(
            "hotelapp.room_release_ready",
            {entry["codename"] for entry in SPECIAL_PERMISSIONS})
        findings = validate_registry()
        self.assertEqual(registry_errors(findings), [],
                         "注册表校验不应有 ERROR："
                         + "；".join(f["message"] for f in registry_errors(findings)))
