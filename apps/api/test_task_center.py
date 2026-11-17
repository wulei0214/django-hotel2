"""MC-007 可执行待办闭环回归（WorkItem 编排，不复制源业务状态）。"""

import json
import uuid

from django.db.models import QuerySet
from django.test import override_settings
from django.utils import timezone

from apps.api.test_notification_center import NotificationCenterTestBase
from apps.billing.models import ApprovalRecord
from apps.core.models import AuditEvent, NotificationRead, WorkItem
from apps.core.services_notifications import refresh_operational_todos


@override_settings(DEBUG=True, DJANGO_ENV="test")
class TaskCenterClosureTests(NotificationCenterTestBase):
    def setUp(self):
        super().setUp()
        self.manager = self._make_staff("task_duty_manager", "值班经理")
        self.other_manager = self._make_staff("task_other_manager", "值班经理")
        self.client.force_login(self.manager)

    def _approval_task(self):
        record = ApprovalRecord.objects.create(
            property=self.property,
            action_code="billing.refund_approve",
            entity_type="Refund",
            entity_id=str(uuid.uuid4().int % 10000),
            requested_by=self.fd,
            reason="待办完成链测试审批",
            idempotency_key=f"task-{uuid.uuid4().hex[:16]}",
        )
        refresh_operational_todos(self.property)
        task = WorkItem.objects.get(
            property=self.property, task_code="approval.pending", source_id=str(record.pk)
        )
        return record, task

    def _post(self, url, payload=None, key=None):
        headers = {"HTTP_X_CSRFTOKEN": "test"}
        if key:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        return self.client.post(
            url,
            data=json.dumps(payload or {}, ensure_ascii=False),
            content_type="application/json",
            **headers,  # type: ignore[arg-type]
        )

    def test_read_receipt_does_not_close_work_item(self):
        record, task = self._approval_task()
        notification = task.notifications.filter(role_group__name="值班经理").first()
        self.assertIsNotNone(notification)
        response = self.client.post(
            f"/api/v1/notifications/{notification.pk}/read",
            data="{}",
            content_type="application/json",
            HTTP_X_CSRFTOKEN="test",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            NotificationRead.objects.filter(
                notification=notification, staff_profile__user=self.manager
            ).exists()
        )
        task.refresh_from_db()
        self.assertEqual(task.status, WorkItem.Status.PENDING)
        record.refresh_from_db()
        self.assertEqual(record.status, ApprovalRecord.Status.PENDING)

    def test_refresh_is_idempotent_for_work_items(self):
        _, task = self._approval_task()
        first_id = task.pk
        refresh_operational_todos(self.property)
        refresh_operational_todos(self.property)
        self.assertEqual(
            WorkItem.objects.filter(property=self.property, task_code="approval.pending").count(), 1
        )
        self.assertEqual(WorkItem.objects.get(pk=first_id).status, WorkItem.Status.PENDING)

    def test_claim_is_idempotent_and_conflicts_for_other_employee(self):
        _, task = self._approval_task()
        url = f"/api/v1/tasks/{task.pk}/claim"
        first = self._post(url, key="task-claim-1")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["data"]["changed"])
        second = self._post(url, key="task-claim-1")
        self.assertEqual(second.status_code, 200)
        # 幂等装饰器返回首次响应；数据库副作用不得重复发生。
        task.refresh_from_db()
        self.assertEqual(task.status, WorkItem.Status.CLAIMED)
        self.assertEqual(task.claimed_by.user, self.manager)

        self.client.force_login(self.other_manager)
        other = self._post(url, key="task-claim-2")
        self.assertEqual(other.status_code, 409)

    def test_complete_calls_source_service_then_closes_task(self):
        record, task = self._approval_task()
        response = self._post(
            f"/api/v1/tasks/{task.pk}/complete",
            {"reason": "复核退款材料完整，同意审批"},
            key="task-complete-1",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["changed"])
        record.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(record.status, ApprovalRecord.Status.APPROVED)
        self.assertEqual(task.status, WorkItem.Status.DONE)
        self.assertEqual(task.resolution, WorkItem.Resolution.COMPLETED)
        self.assertEqual(task.completed_by.user, self.manager)
        self.assertIsNotNone(task.completed_at)
        self.assertTrue(
            AuditEvent.objects.filter(
                property=self.property,
                action="workitem.completed",
                entity_type="WorkItem",
                entity_id=task.pk,
            ).exists()
        )

    def test_failed_source_service_keeps_task_pending_with_error(self):
        # 申请人审批自己的记录必须被 ApprovalService 的 SoD 拒绝。
        record = ApprovalRecord.objects.create(
            property=self.property,
            action_code="billing.refund_approve",
            entity_type="Refund",
            entity_id=str(uuid.uuid4().int % 10000),
            requested_by=self.manager,
            reason="自批测试",
            idempotency_key=f"task-sod-{uuid.uuid4().hex[:16]}",
        )
        refresh_operational_todos(self.property)
        task = WorkItem.objects.get(
            property=self.property, task_code="approval.pending", source_id=str(record.pk)
        )
        response = self._post(f"/api/v1/tasks/{task.pk}/complete", {"reason": "尝试自批"})
        self.assertEqual(response.status_code, 409)
        task.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(task.status, WorkItem.Status.PENDING)
        self.assertEqual(task.last_error_code, "SOURCE_SERVICE_FAILED")
        self.assertIn("申请人不能审批", task.last_error_summary)
        self.assertEqual(record.status, ApprovalRecord.Status.PENDING)

    def test_source_completed_elsewhere_closes_task_without_completed_by(self):
        record, task = self._approval_task()
        QuerySet(ApprovalRecord).filter(pk=record.pk).update(
            status=ApprovalRecord.Status.APPROVED,
            approved_by=self.fd,
            approved_at=timezone.now(),
        )
        refresh_operational_todos(self.property)
        task.refresh_from_db()
        self.assertEqual(task.status, WorkItem.Status.DONE)
        self.assertEqual(task.resolution, WorkItem.Resolution.SOURCE_RESOLVED)
        self.assertIsNone(task.completed_by_id)

    def test_complete_not_visible_task_returns_404(self):
        """MC-007-R1：完成接口统一可见范围校验——任务改属他组后，原组管理者 404。"""
        from django.contrib.auth.models import Group

        record, task = self._approval_task()
        housekeeping_group = Group.objects.get(name="客房服务员")
        WorkItem.objects.filter(pk=task.pk).update(
            assigned_group=housekeeping_group, assigned_profile=None, claimed_by=None
        )
        response = self._post(f"/api/v1/tasks/{task.pk}/complete", {"note": "越权尝试"})
        self.assertEqual(response.status_code, 404, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, WorkItem.Status.PENDING)

    def test_assign_invisible_task_via_staff_manage_is_documented_path(self):
        """MC-007-R1 例外口径：staff_manage 管理者指派不可见任务=显式管理路径（审计留痕）。"""
        from django.contrib.auth.models import Group

        record, task = self._approval_task()
        housekeeping_group = Group.objects.get(name="客房服务员")
        WorkItem.objects.filter(pk=task.pk).update(
            assigned_group=housekeeping_group, assigned_profile=None, claimed_by=None
        )
        owner_manager = self._make_staff("task_owner_mgr", "店长")
        self.client.force_login(owner_manager)
        other = self._make_staff("task_hk_member", "客房服务员")
        from apps.identity.models import StaffProfile

        other_profile = StaffProfile.objects.get(user=other)
        response = self._post(
            f"/api/v1/tasks/{task.pk}/assign", {"assigned_to": other_profile.pk}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(
            AuditEvent.objects.filter(action="workitem.assigned", entity_id=str(task.pk)).exists()
        )

    def test_list_filters_and_cross_property_detail_is_404(self):
        _, task = self._approval_task()
        response = self.client.get("/api/v1/tasks?status=PENDING&assigned=all")
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.json()["data"]["items"]]
        self.assertIn(task.pk, ids)

        other = self._new_property("TASK-OTHER")
        WorkItem.objects.create(
            property=other,
            task_code="approval.pending",
            source_type="ApprovalRecord",
            source_id="999",
            title="他物业待办",
            dedupe_key="foreign",
        )
        response = self.client.get(
            f"/api/v1/tasks/{other.pk and WorkItem.objects.get(property=other).pk}"
        )
        self.assertEqual(response.status_code, 404)
