"""MC-006 清洁与维修任务级闭环回归（任务/房态/库存同事务）。"""

import json
from datetime import timedelta

from django.test import override_settings

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.api.test_housekeeping_center import HousekeepingCenterTestBase
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
                {"assigned_to": self.dm.pk},
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
