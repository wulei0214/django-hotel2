"""权限矩阵第二人审批的真实 MySQL 并发竞争测试（迭代-1 A-01 验收项）。

负责人验收要求（2026-11-13）：A-01 的顺序竞争测试只证明最终状态守卫有效，
不构成真实并发证据。本文件用 TransactionTestCase + Barrier + 双线程独立
数据库连接（MySQL 行锁语义），直接竞争同一审批记录：

  * 并发双批准 → 恰一个 200、一个 409 record_already_decided；
    权限矩阵只变更一次、matrix_saved 审计恰一次；
  * 并发批准+驳回 → 恰一个 200、一个 409；终态与先落库方一致；
    状态与矩阵结果无不一致。

关键实现约束（与 test_mysql_concurrency.py 同范式）：
  * TransactionTestCase（真实事务 + 独立连接；TestCase 的事务包裹会使
    行锁竞争不可构造）；
  * 每个工作线程 close_old_connections() 取独立连接；
  * Barrier 对齐起跑，之后同时打端点；
  * select_for_update 在 SQLite 为 no-op，skipUnless 仅 MySQL 执行；
  * 两线程使用两个不同的平台超管账号（SoD：均不得是申请人），
    各持独立 requests/Django test Client 会话。
"""

import threading
import unittest
from threading import Barrier

from django.contrib.auth.models import Group
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.utils.crypto import get_random_string

from apps.core.models import AuditEvent
from apps.identity.models import PermissionMatrixApproval

_TEST_PW_REQ = get_random_string(24)
_TEST_PW_A1 = get_random_string(24)
_TEST_PW_A2 = get_random_string(24)


def _get_user_model():
    from django.contrib.auth import get_user_model

    return get_user_model()


@unittest.skipUnless(
    connection.vendor == "mysql",
    "审批决策互斥依赖 select_for_update 行锁（SQLite 下为 no-op 且双连接并发写"
    "触发 database table is locked，无法构造真实行锁竞争），SQLite 跳过——"
    "与 test_mysql_concurrency.py 同口径，真实竞争证据由 MySQL CI 提供。",
)
class PermissionApprovalConcurrencyTestBase(TransactionTestCase):
    """真实 MySQL 并发夹具：申请人 + 两名审批人（独立平台会话）。"""

    @staticmethod
    def _platform_login(client, user):
        """平台会域登录（与 test_permission_center 同口径）。"""
        client.force_login(user)
        sessionid = client.cookies["sessionid"].value
        client.cookies["platform_sessionid"] = sessionid
        client.cookies["django_admin_sessionid"] = sessionid

    def setUp(self):
        user_model = _get_user_model()
        self.requester = user_model.objects.create_superuser("conc_req", password=_TEST_PW_REQ)
        self.approver_a = user_model.objects.create_superuser("conc_app_a", password=_TEST_PW_A1)
        self.approver_b = user_model.objects.create_superuser("conc_app_b", password=_TEST_PW_A2)
        self.client_a = Client()
        self.client_b = Client()
        self._platform_login(self.client_a, self.approver_a)
        self._platform_login(self.client_b, self.approver_b)
        # TransactionTestCase 的 truncate 式清理会删除迁移 0001 种子的角色组
        # 且不恢复（MySQL CI 实证 DoesNotExist）——并发夹具自持建组，不依赖
        # 迁移种子数据（与 test_mysql_concurrency 自行 create Property 同原则）。
        self.group, _ = Group.objects.get_or_create(name="客房服务员")
        self.special_code = "billing.refund_approve"

    def _held(self):
        return {
            f"{a}.{c}"
            for a, c in self.group.permissions.values_list("content_type__app_label", "codename")
        }

    def _create_pending_record(self):
        """构造一条与真实状态一致的待审批记录（并发测试聚焦决策端点竞争）。

        version 必须是组当前权限码全集的真实内容 hash——执行端
        _execute_approved_matrix 会复核 version/diff/allowed（治理设计），
        伪造 version 会让并发双批准全部 409 version_conflict（首轮 CI 实证）。
        """
        from apps.identity.permission_center import content_version, diff_hash

        requested = sorted(self._held() | {self.special_code})
        added: list[str] = [self.special_code]
        removed: list[str] = []
        return PermissionMatrixApproval.objects.create(
            group=self.group,
            requested_by=self.requester,
            reason="真实并发竞争测试",
            diff_hash=diff_hash(added, removed),
            payload_snapshot={
                "added": added,
                "removed": removed,
                "preserved": [],
                "requested": requested,
                "version": content_version(self._held()),
            },
        )

    def _approve_url(self, record_id):
        return f"/platform/api/permissions/{self.group.pk}/approvals/{record_id}/approve/"

    def _reject_url(self, record_id):
        return f"/platform/api/permissions/{self.group.pk}/approvals/{record_id}/reject/"

    def _post_concurrent(self, url_by_client):
        """Barrier 对齐后同时打两个端点；返回 {name: (status, json)}。

        url_by_client: {名称: (client, url)}——两线程独立连接
        （close_old_connections 后各自建立），真实竞争数据库行锁。
        """
        barrier = Barrier(2)
        results = {}

        def worker(name):
            client, url = url_by_client[name]
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                response = client.post(url, data="{}", content_type="application/json")
                body = {}
                try:
                    body = response.json()
                except Exception:  # pragma: no cover - 非 JSON 防御
                    body = {"raw_status": response.status_code}
                results[name] = (response.status_code, body)
            except Exception as exc:  # pragma: no cover - 防御
                results[name] = ("exception", {"repr": repr(exc)})
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker, args=(name,)) for name in url_by_client]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "工作线程超时未结束")
        return results

    def _assert_exactly_one_decision(self, results):
        """断言恰一个 200、一个 409 record_already_decided。"""
        statuses = sorted(status for status, _ in results.values())
        self.assertEqual(
            statuses,
            [200, 409],
            f"并发决策必须恰一个成功一个 409，实际 {results}",
        )
        for status, body in results.values():
            if status == 409:
                self.assertEqual(body.get("code"), "record_already_decided")


class ConcurrentDoubleApproveTests(PermissionApprovalConcurrencyTestBase):
    """并发双批准：一个决策生效、矩阵一次变更、审计一次。"""

    def test_concurrent_double_approve_exactly_one_wins(self):
        record = self._create_pending_record()
        held_before = self._held()
        results = self._post_concurrent(
            {
                "approver-a": (self.client_a, self._approve_url(record.pk)),
                "approver-b": (self.client_b, self._approve_url(record.pk)),
            }
        )

        self._assert_exactly_one_decision(results)

        # 1) 终态：恰一个最终决策（APPROVED），矩阵只变更一次
        record.refresh_from_db()
        self.assertEqual(record.status, PermissionMatrixApproval.Status.APPROVED)
        self.assertIsNotNone(record.approved_by)
        self.assertIsNotNone(record.approved_at)
        held_after = self._held()
        self.assertIn(self.special_code, held_after)
        self.assertNotEqual(held_after, held_before, "矩阵必须恰好发生一次变更")

        # 2) 审计唯一性：matrix_saved 恰一次（本测试专用 diff_hash 隔离）
        saved_events = AuditEvent.objects.filter(
            action="permissions.matrix_saved",
            entity_type="Group",
            entity_id=str(self.group.pk),
            before__isnull=False,
        )
        # 审计 before 含基线态：以 after 含专项码且 actor 非申请人计数
        executed = [
            event for event in saved_events if self.special_code in (event.after_json or [])
        ]
        self.assertEqual(
            len(executed),
            1,
            f"matrix_saved 审计必须恰一次有效执行，实际 {len(executed)}",
        )

        # 3) 数据库一致性：决策状态与矩阵结果一致（APPROVED 且专项码在矩阵中）
        self.assertEqual(record.status, PermissionMatrixApproval.Status.APPROVED)
        self.assertIn(self.special_code, self._held())


class ConcurrentApproveRejectTests(PermissionApprovalConcurrencyTestBase):
    """并发批准 vs 驳回：互斥——终态与先落库方一致，无状态/矩阵不一致。"""

    def test_concurrent_approve_and_reject_exactly_one_wins(self):
        record = self._create_pending_record()
        results = self._post_concurrent(
            {
                "approver": (self.client_a, self._approve_url(record.pk)),
                "rejecter": (self.client_b, self._reject_url(record.pk)),
            }
        )

        self._assert_exactly_one_decision(results)

        record.refresh_from_db()
        held_after = self._held()
        # 终态与先落库方一致：APPROVED → 专项码在矩阵；REJECTED → 不在
        if record.status == PermissionMatrixApproval.Status.APPROVED:
            self.assertIn(self.special_code, held_after)
            self.assertTrue(
                AuditEvent.objects.filter(
                    action="permissions.matrix_saved",
                    entity_type="Group",
                    entity_id=str(self.group.pk),
                ).exists(),
                "批准方生效必须有 matrix_saved 审计",
            )
            # 驳回不得再落审计后的矩阵变更：approval_rejected 不存在（驳回方 409）
            self.assertFalse(
                AuditEvent.objects.filter(
                    action="permissions.approval_rejected",
                    entity_type="Group",
                    entity_id=str(self.group.pk),
                ).exists(),
                "失败的驳回方不得留下 approval_rejected 审计",
            )
        else:
            self.assertEqual(record.status, PermissionMatrixApproval.Status.REJECTED)
            self.assertNotIn(self.special_code, held_after)
            self.assertFalse(
                AuditEvent.objects.filter(
                    action="permissions.matrix_saved",
                    entity_type="Group",
                    entity_id=str(self.group.pk),
                ).exists(),
                "驳回方生效时不得有 matrix_saved 审计",
            )
        # 无状态/矩阵不一致（验收标准 5）
        self.assertNotEqual(record.status, PermissionMatrixApproval.Status.PENDING)

    def test_concurrent_reject_and_approve_exactly_one_wins(self):
        """反序对偶：驳回在 A 连接、批准在 B 连接，结论与上一条对齐。"""
        record = self._create_pending_record()
        results = self._post_concurrent(
            {
                "rejecter": (self.client_a, self._reject_url(record.pk)),
                "approver": (self.client_b, self._approve_url(record.pk)),
            }
        )

        self._assert_exactly_one_decision(results)

        record.refresh_from_db()
        self.assertIn(
            record.status,
            (PermissionMatrixApproval.Status.APPROVED, PermissionMatrixApproval.Status.REJECTED),
        )
        held_after = self._held()
        if record.status == PermissionMatrixApproval.Status.APPROVED:
            self.assertIn(self.special_code, held_after)
        else:
            self.assertNotIn(self.special_code, held_after)
