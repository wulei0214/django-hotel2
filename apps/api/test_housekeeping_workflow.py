"""MC-006 清洁与维修任务级闭环回归（任务/房态/库存同事务）。"""

import json
from datetime import timedelta

from django.test import override_settings

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.api.test_housekeeping_center import HousekeepingCenterTestBase
from apps.identity.models import StaffProfile
from apps.inventory.models import HousekeepingTask, MaintenanceTicket, Room, RoomInspection
from apps.inventory.services import get_room_type_availability


@override_settings(DEBUG=True, DJANGO_ENV="test")
class HousekeepingTaskWorkflowTests(HousekeepingCenterTestBase):
    def _task(self, room, status=HousekeepingTask.TaskStatus.PENDING, assigned_to=None):
        return HousekeepingTask.objects.create(
            property=self.property,
            room=room,
            status=status,
            assigned_to=assigned_to,
            task_type=HousekeepingTask.TaskType.DEPARTURE,
        )

    def _post(self, url, payload=None, user=None):
        if user:
            self.client.force_login(user)
        return self.client.post(
            url, data=json.dumps(payload or {}, ensure_ascii=False), content_type="application/json"
        )

    def test_full_cleaning_chain_with_inspection(self):
        room = self._make_dirty_room("HK-WF-01")
        task = self._task(room)
        self.assertEqual(
            self._post(f"/api/v1/housekeeping/tasks/{task.pk}/claim", user=self.hk).status_code, 200
        )
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.hk)

        response = self._post(f"/api/v1/housekeeping/tasks/{task.pk}/start", user=self.hk)
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(room.housekeeping_status, Room.HousekeepingStatus.CLEANING)
        self.assertEqual(task.status, HousekeepingTask.TaskStatus.IN_PROGRESS)

        response = self._post(f"/api/v1/housekeeping/tasks/{task.pk}/complete", user=self.hk)
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(room.housekeeping_status, Room.HousekeepingStatus.INSPECTING)
        self.assertEqual(task.status, HousekeepingTask.TaskStatus.DONE)

        response = self._post(
            f"/api/v1/housekeeping/tasks/{task.pk}/inspect",
            {"result": "PASS", "checklist_result": {"floor": True}},
            user=self.dm,
        )
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(room.housekeeping_status, Room.HousekeepingStatus.CLEAN)
        self.assertEqual(task.status, HousekeepingTask.TaskStatus.INSPECTED)
        self.assertTrue(RoomInspection.objects.filter(task=task, result="PASS").exists())

    def test_self_inspection_pass_is_rejected(self):
        room = self._make_dirty_room("HK-WF-02")
        task = self._task(room, status=HousekeepingTask.TaskStatus.DONE, assigned_to=self.hk)
        self.client.force_login(self.dm)
        self.assertEqual(self._post(f"/api/v1/housekeeping/tasks/{task.pk}/start").status_code, 409)
        self.client.force_login(self.hk)
        response = self._post(f"/api/v1/housekeeping/tasks/{task.pk}/inspect", {"result": "PASS"})
        self.assertEqual(response.status_code, 409)

    def test_failed_inspection_reopens_room_and_creates_rework_task(self):
        room = self._make_dirty_room("HK-WF-03")
        task = self._task(room, status=HousekeepingTask.TaskStatus.DONE, assigned_to=self.hk)
        response = self._post(
            f"/api/v1/housekeeping/tasks/{task.pk}/inspect",
            {"result": "FAIL", "failed_items": ["地面有水渍"], "notes": "需重新清洁"},
            user=self.hk,
        )
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(room.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        self.assertEqual(task.status, HousekeepingTask.TaskStatus.REJECTED)
        self.assertTrue(
            HousekeepingTask.objects.filter(
                room=room, status=HousekeepingTask.TaskStatus.PENDING
            ).exists()
        )
        inspection = RoomInspection.objects.get(task=task)
        self.assertEqual(inspection.failed_items, ["地面有水渍"])

    def test_rejected_task_can_restart_to_in_progress(self):
        """MC11-HK-10 伴随缺陷：任务池 REJECTED 行「重新开始」按钮自
        c73aaf2 起调用 start API，但服务层此前只接受 PENDING——REJECTED
        重开必然 409，按钮从未可用。模型 help_text 既有口径「REJECTED 回
        IN_PROGRESS」；本测试固化服务层补齐后的语义：REJECTED→IN_PROGRESS
        且同事务把检查退回的房间 DIRTY→CLEANING；DONE/CANCELLED 等其余
        状态仍拒绝开始（不放宽状态机）。"""
        room = self._make_dirty_room("HK-WF-05")
        task = self._task(room, status=HousekeepingTask.TaskStatus.DONE, assigned_to=self.hk)
        response = self._post(
            f"/api/v1/housekeeping/tasks/{task.pk}/inspect",
            {"result": "FAIL", "failed_items": ["地面有水渍"], "notes": "需重新清洁"},
            user=self.dm,
        )
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, HousekeepingTask.TaskStatus.REJECTED)

        response = self._post(f"/api/v1/housekeeping/tasks/{task.pk}/start", user=self.hk)
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        room.refresh_from_db()
        self.assertEqual(task.status, HousekeepingTask.TaskStatus.IN_PROGRESS)
        self.assertEqual(room.housekeeping_status, Room.HousekeepingStatus.CLEANING)

        # 其余状态不放宽：终态（INSPECTED/CANCELLED）开始仍然 409。
        cancelled = self._task(self._make_dirty_room("HK-WF-06"))
        cancel = self._post(
            f"/api/v1/housekeeping/tasks/{cancelled.pk}/cancel",
            {"reason": "重复任务撤销"},
            user=self.dm,
        )
        self.assertEqual(cancel.status_code, 200, cancel.content)
        self.assertEqual(
            self._post(
                f"/api/v1/housekeeping/tasks/{cancelled.pk}/start", user=self.hk
            ).status_code,
            409,
        )

    def test_assigned_group_filter_isolates_cross_group_tasks(self):
        """MC-006-R2：assigned=group 必须真过滤——本组任务可见，跨组任务不可见。"""
        hk2 = self._make_staff("hk_center_cleaner2", "客房服务员")
        fnb = self._make_staff("fnb_cross_group", "餐饮服务员")
        t_mine = self._task(self._make_dirty_room("HK-R2-1"), assigned_to=self.hk)
        t_same = self._task(self._make_dirty_room("HK-R2-2"), assigned_to=hk2)
        t_cross = self._task(self._make_dirty_room("HK-R2-3"), assigned_to=fnb)
        self.client.force_login(self.hk)
        response = self.client.get("/api/v1/housekeeping/tasks?assigned=group")
        self.assertEqual(response.status_code, 200, response.content)
        ids = {item["id"] for item in response.json()["data"]["items"]}
        self.assertIn(t_mine.pk, ids)
        self.assertIn(t_same.pk, ids)
        self.assertNotIn(t_cross.pk, ids)

    def test_task_api_permissions_and_cross_property_isolation(self):
        room = self._make_dirty_room("HK-WF-04")
        task = self._task(room)
        self.client.force_login(self.no_perm)
        self.assertEqual(self.client.get("/api/v1/housekeeping/tasks").status_code, 403)
        self.client.force_login(self.hk)
        self.assertEqual(self.client.get(f"/api/v1/housekeeping/tasks/{task.pk}").status_code, 200)

        other = self._new_property("HK-WF-OTHER") if hasattr(self, "_new_property") else None
        if other is not None:
            foreign = HousekeepingTask.objects.create(
                property=other, room=room, task_type=HousekeepingTask.TaskType.DEPARTURE
            )
            # 跨物业任务必须不可见（此处 foreign.room 只用于测试夹具，门禁按 task.property 判定）
            self.assertEqual(
                self.client.get(f"/api/v1/housekeeping/tasks/{foreign.pk}").status_code, 404
            )

    def test_cross_property_task_isolation_is_real(self):
        """MC11-HK-06：任务级跨物业隔离断言此前是死分支（hasattr 恒 False）；
        现以真实他物业夹具执行 404 断言。"""
        from apps.core.models import Property as PropertyModel
        from apps.inventory.models import RoomType

        other_property, _ = PropertyModel.objects.get_or_create(
            code="HK-XPROP", defaults={"name": "任务隔离他物业"}
        )
        other_type = RoomType.objects.create(
            property=other_property, code="STD-X", name="标准房-他店", capacity=2
        )
        other_room = Room.objects.create(
            property=other_property, room_type=other_type, number="X-0001", floor=1
        )
        foreign = HousekeepingTask.objects.create(
            property=other_property, room=other_room, task_type=HousekeepingTask.TaskType.DEPARTURE
        )
        self.client.force_login(self.hk)
        response = self.client.get(f"/api/v1/housekeeping/tasks/{foreign.pk}")
        self.assertEqual(response.status_code, 404, response.content)

    def test_cleaning_task_cancel_endpoint(self):
        """MC11-HK-04：清洁任务取消端点此前零测试零页面入口。"""
        room = self._make_dirty_room("HK-CANCEL-01")
        task = self._task(room)
        self.client.force_login(self.dm)
        response = self._post(
            f"/api/v1/housekeeping/tasks/{task.pk}/cancel", {"reason": "重复任务撤销"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, HousekeepingTask.TaskStatus.CANCELLED)
        room.refresh_from_db()
        self.assertEqual(
            room.housekeeping_status,
            Room.HousekeepingStatus.DIRTY,
            "取消待清洁任务不影响房态",
        )


@override_settings(DEBUG=True, DJANGO_ENV="test")
class MaintenanceTicketWorkflowTests(E2EFlowTestBase):
    def setUp(self):
        super().setUp()
        self.hk = self._make_staff("mt_workflow_cleaner", "客房服务员")
        self.dm = self._make_staff("mt_workflow_duty", "值班经理")
        self.manager = self._make_staff("mt_workflow_manager", "店长")
        self.client.force_login(self.dm)

    def _post(self, url, payload=None, user=None):
        if user:
            self.client.force_login(user)
        return self.client.post(
            url, data=json.dumps(payload or {}, ensure_ascii=False), content_type="application/json"
        )

    def test_full_maintenance_chain_restores_inventory_only_after_acceptance(self):
        room = self._make_room("MT-WF-01")
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.NORMAL)
        available_before = get_room_type_availability(
            self.property, self.room_type, self.today, self.today + timedelta(days=1), rooms=1
        )["available_count"]

        response = self._post(
            "/api/v1/maintenance/tickets/create",
            {"room_id": room.pk, "reason": "空调漏水，需停用维修", "severity": "HIGH"},
            user=self.dm,
        )
        self.assertEqual(response.status_code, 201, response.content)
        ticket_id = response.json()["data"]["id"]
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.OUT_OF_ORDER)
        available_stopped = get_room_type_availability(
            self.property, self.room_type, self.today, self.today + timedelta(days=1), rooms=1
        )["available_count"]
        self.assertLess(available_stopped, available_before)

        self.assertEqual(
            self._post(
                f"/api/v1/maintenance/tickets/{ticket_id}/assign",
                # API 契约：assigned_to=StaffProfile 主键（端点按档案表解析被指派
                # 人再映射到其 User）；此前误传 User 主键——隔离跑时两表自增序列
                # 恰好对齐掩盖了错位，包序下序列错位即 400（MC-006-R1 根因）。
                {"assigned_to": StaffProfile.objects.get(user=self.dm).pk},
                user=self.dm,
            ).status_code,
            200,
        )
        self.assertEqual(
            self._post(f"/api/v1/maintenance/tickets/{ticket_id}/start", user=self.dm).status_code,
            200,
        )
        response = self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/resolve",
            {"notes": "已更换排水管"},
            user=self.dm,
        )
        self.assertEqual(response.status_code, 200, response.content)
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.OUT_OF_ORDER)

        response = self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/verify",
            {"result": "PASS", "notes": "验收通过"},
            user=self.manager,
        )
        self.assertEqual(response.status_code, 200, response.content)
        ticket = MaintenanceTicket.objects.get(pk=ticket_id)
        room.refresh_from_db()
        self.assertEqual(ticket.status, MaintenanceTicket.TicketStatus.VERIFIED)
        self.assertEqual(ticket.verified_by, self.manager)
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.NORMAL)
        available_after = get_room_type_availability(
            self.property, self.room_type, self.today, self.today + timedelta(days=1), rooms=1
        )["available_count"]
        self.assertEqual(available_after, available_before)

    def test_reporter_cannot_self_accept(self):
        room = self._make_room("MT-WF-02")
        response = self._post(
            "/api/v1/maintenance/tickets/create",
            {"room_id": room.pk, "reason": "门锁故障需报修"},
            user=self.dm,
        )
        ticket_id = response.json()["data"]["id"]
        self._post(f"/api/v1/maintenance/tickets/{ticket_id}/start", user=self.dm)
        self._post(f"/api/v1/maintenance/tickets/{ticket_id}/resolve", user=self.dm)
        response = self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/verify", {"result": "PASS"}, user=self.dm
        )
        self.assertEqual(response.status_code, 409)
        ticket = MaintenanceTicket.objects.get(pk=ticket_id)
        self.assertEqual(ticket.status, MaintenanceTicket.TicketStatus.PENDING_VERIFY)

    def test_cleaner_cannot_create_maintenance_ticket(self):
        room = self._make_room("MT-WF-03")
        response = self._post(
            "/api/v1/maintenance/tickets/create",
            {"room_id": room.pk, "reason": "客房服务员越权报修"},
            user=self.hk,
        )
        self.assertEqual(response.status_code, 403)

    # ------------------------------------------------------------------
    # MC11-HK-02/03/04/05（11C 盘点整改）：恢复守卫 / 验收驳回 / 取消 / 执行人 SoD
    # ------------------------------------------------------------------
    def test_room_level_restore_blocked_while_ticket_active(self):
        """MC11-HK-02：房间级维修登记不得绕过未完结工单直接恢复可售。"""
        room = self._make_room("MT-GUARD-01")
        create = self._post(
            "/api/v1/maintenance/tickets/create",
            {"room_id": room.pk, "reason": "空调故障"},
            user=self.dm,
        )
        ticket_id = create.json()["data"]["id"]
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.OUT_OF_ORDER)

        blocked = self._post(
            f"/api/v1/housekeeping/rooms/{room.pk}/maintenance",
            {"status": "NORMAL", "reason": "绕过验收直接恢复"},
            user=self.manager,
        )
        self.assertEqual(blocked.status_code, 409, blocked.content)
        self.assertIn("未完结维修工单", blocked.json()["error"]["message"])
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.OUT_OF_ORDER)

        # 工单取消后（不再活跃）：房间级恢复放行
        self.assertEqual(
            self._post(
                f"/api/v1/maintenance/tickets/{ticket_id}/cancel",
                {"reason": "重复报修，撤销工单"},
                user=self.dm,
            ).status_code,
            200,
        )
        restored = self._post(
            f"/api/v1/housekeeping/rooms/{room.pk}/maintenance",
            {"status": "NORMAL", "reason": "工单已撤销，恢复可售"},
            user=self.manager,
        )
        self.assertEqual(restored.status_code, 200, restored.content)
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.NORMAL)

    def test_verify_fail_returns_ticket_to_in_progress(self):
        """MC11-HK-03：验收驳回 → 工单回维修中，房态与库存均不恢复。"""
        room = self._make_room("MT-GUARD-02")
        available_before = get_room_type_availability(
            self.property, self.room_type, self.today, self.today + timedelta(days=1), rooms=1
        )["available_count"]
        create = self._post(
            "/api/v1/maintenance/tickets/create",
            {"room_id": room.pk, "reason": "电视黑屏"},
            user=self.dm,
        )
        ticket_id = create.json()["data"]["id"]
        self._post(f"/api/v1/maintenance/tickets/{ticket_id}/start", user=self.dm)
        self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/resolve", {"notes": "换了线"}, user=self.dm
        )

        response = self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/verify",
            {"result": "FAIL", "notes": "仍然黑屏，驳回重修"},
            user=self.manager,
        )
        self.assertEqual(response.status_code, 200, response.content)
        ticket = MaintenanceTicket.objects.get(pk=ticket_id)
        room.refresh_from_db()
        self.assertEqual(ticket.status, MaintenanceTicket.TicketStatus.IN_PROGRESS)
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.OUT_OF_ORDER)
        available_after_fail = get_room_type_availability(
            self.property, self.room_type, self.today, self.today + timedelta(days=1), rooms=1
        )["available_count"]
        self.assertEqual(available_after_fail, available_before - 1)

    def test_executor_cannot_self_verify(self):
        """MC11-HK-05：维修执行人不得自行验收（对齐清洁链自检禁令）。"""
        room = self._make_room("MT-GUARD-03")
        create = self._post(
            "/api/v1/maintenance/tickets/create",
            {"room_id": room.pk, "reason": "冰箱不制冷"},
            user=self.dm,
        )
        ticket_id = create.json()["data"]["id"]
        # 执行人=duty2（第二值班经理）：报修人=dm，验收人=manager，三方分离
        duty2 = self._make_staff("mt_guard_duty2", "值班经理")
        self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/assign",
            {"assigned_to": StaffProfile.objects.get(user=duty2).pk},
            user=self.dm,
        )
        self._post(f"/api/v1/maintenance/tickets/{ticket_id}/start", user=duty2)
        self._post(f"/api/v1/maintenance/tickets/{ticket_id}/resolve", user=duty2)

        denied = self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/verify", {"result": "PASS"}, user=duty2
        )
        self.assertEqual(denied.status_code, 409, denied.content)
        self.assertIn("维修执行人不得自行验收", denied.json()["error"]["message"])
        ticket = MaintenanceTicket.objects.get(pk=ticket_id)
        self.assertEqual(ticket.status, MaintenanceTicket.TicketStatus.PENDING_VERIFY)

        # 第三人（店长，非报修人非执行人）验收通过 → 恢复
        approved = self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/verify", {"result": "PASS"}, user=self.manager
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.NORMAL)

    def test_ticket_cancel_endpoint_and_room_release(self):
        """MC11-HK-04：工单取消端点此前零测试；取消后房间级恢复放行（工单不再活跃）。"""
        room = self._make_room("MT-CANCEL-01")
        create = self._post(
            "/api/v1/maintenance/tickets/create",
            {"room_id": room.pk, "reason": "误报维修"},
            user=self.dm,
        )
        ticket_id = create.json()["data"]["id"]
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.OUT_OF_ORDER)

        response = self._post(
            f"/api/v1/maintenance/tickets/{ticket_id}/cancel", {"reason": "误报撤销"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        ticket = MaintenanceTicket.objects.get(pk=ticket_id)
        self.assertEqual(ticket.status, MaintenanceTicket.TicketStatus.CANCELLED)

        restored = self._post(
            f"/api/v1/housekeeping/rooms/{room.pk}/maintenance",
            {"status": "NORMAL", "reason": "工单已撤销，恢复可售"},
            user=self.manager,
        )
        self.assertEqual(restored.status_code, 200, restored.content)
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status, Room.MaintenanceStatus.NORMAL)
