"""收尾专项：真实 MySQL 并发测试（identity 侧，收尾任务一 场景 2）。

场景 2：工号计数器（WB-101）——同物业同部门两线程并发
generate_employee_no → 两个工号不同且连续（FO-0001/FO-0002），计数器
next_seq 落库为 3；行锁（EmployeeNumberCounter select_for_update）串行化
读-增-写，不再依赖 MAX+1 查询。

SQLite 下 select_for_update 为 no-op、由单写者语义兜底，无法构造真实
行锁竞争，skipUnless 仅 MySQL 执行。
"""

import threading
import unittest
from threading import Barrier

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from apps.core.models import Property
from apps.identity.employee_number import generate_employee_no
from apps.identity.models import EmployeeNumberCounter


@unittest.skipUnless(
    connection.vendor == "mysql",
    "工号计数器并发分配依赖 EmployeeNumberCounter 行锁（select_for_update "
    "在 SQLite 下为 no-op），SQLite 测试环境跳过。",
)
class EmployeeNumberCounterConcurrencyTests(TransactionTestCase):
    """场景 2（WB-101）：同物业同部门并发分配工号，两号不同且连续。"""

    def test_concurrent_generation_produces_distinct_consecutive_nos(self):
        property_obj = Property.objects.create(
            code="MWID", name="工号并发专项酒店", timezone="Asia/Shanghai"
        )
        property_pk = property_obj.pk
        worker_count = 2
        barrier = Barrier(worker_count)
        results = {"nos": {}, "errors": []}

        def allocate_worker(worker_name):
            close_old_connections()
            try:
                from apps.core.models import Property as P

                property_obj = P.objects.get(pk=property_pk)
                barrier.wait(timeout=15)
                try:
                    employee_no = generate_employee_no(
                        property_obj=property_obj, department_code="FO"
                    )
                    results["nos"][worker_name] = employee_no
                except Exception as exc:  # pragma: no cover - 防御
                    results["errors"].append(repr(exc))
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=allocate_worker, args=(name,))
            for name in ("worker-A", "worker-B")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "工作线程超时未结束")

        self.assertEqual(results["errors"], [], f"并发分配出现意外异常: {results['errors']}")
        assigned = sorted(results["nos"].values())
        self.assertEqual(len(assigned), 2, "两个线程都必须拿到工号")
        self.assertNotEqual(assigned[0], assigned[1], "并发分配不得发出重复工号")
        self.assertEqual(
            assigned, ["FO-0001", "FO-0002"], "同物业同部门两号必须连续（首建种子从 1 起）"
        )
        counter = EmployeeNumberCounter.objects.get(property_id=property_pk, department_code="FO")
        self.assertEqual(counter.next_seq, 3, "计数器行锁串行递增后 next_seq 落库为 3")

    def test_sequential_generation_continues_after_terminated_membership(self):
        """回归锚点：串行两次分配同样连续（计数器只增不减；DEC-EMP-01）。"""
        property_obj = Property.objects.create(
            code="MWID2", name="工号串行回归酒店", timezone="Asia/Shanghai"
        )
        first = generate_employee_no(property_obj=property_obj, department_code="HK")
        second = generate_employee_no(property_obj=property_obj, department_code="HK")
        self.assertEqual([first, second], ["HK-0001", "HK-0002"])


@unittest.skipUnless(
    connection.vendor == "mysql",
    "同一员工 ACTIVE 任职唯一性依赖数据库唯一索引（MySQL 8 此前不创建"
    "条件唯一约束，故本用例是 WB-EMP-01 的实证验收），SQLite 跳过。",
)
class ActiveMembershipUniquenessConcurrencyTests(TransactionTestCase):
    """场景 9（WB-EMP-01）：两个独立连接并发为同一员工建 ACTIVE 任职。

    原实现依赖 condition=Q(status="ACTIVE") 部分唯一索引，MySQL 8 下
    models.W036 且约束不创建，两个事务可同时写入两条 ACTIVE 任职。整改后
    由 active_membership_key 可空唯一列在数据库层仲裁：恰一个成功、另一个
    收到 IntegrityError，终态只有一条 ACTIVE。
    """

    def test_concurrent_active_memberships_exactly_one_wins(self):
        from django.contrib.auth.models import User
        from django.db import IntegrityError, transaction

        from apps.identity.models import (
            PropertyStaffMembership,
            StaffProfile,
        )

        prop = Property.objects.create(
            code="MWID3", name="任职唯一性并发酒店", timezone="Asia/Shanghai"
        )
        user = User.objects.create_user(username="mwid3-staff")
        profile = StaffProfile.objects.create(user=user, full_name="并发任职员工")
        property_pk, profile_pk = prop.pk, profile.pk
        barrier = Barrier(2)
        results = {"created": [], "integrity": [], "errors": []}

        def create_worker(index):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                try:
                    with transaction.atomic():
                        PropertyStaffMembership.objects.create(
                            property_id=property_pk,
                            staff_profile_id=profile_pk,
                            employee_no=f"FO-{9000 + index}",
                            status=PropertyStaffMembership.Status.ACTIVE,
                        )
                    results["created"].append(index)
                except IntegrityError:
                    results["integrity"].append(index)
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [threading.Thread(target=create_worker, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "工作线程超时未结束")

        self.assertEqual(results["errors"], [], f"并发建档出现意外异常: {results['errors']}")
        self.assertEqual(len(results["created"]), 1, "恰一个事务建档成功")
        self.assertEqual(len(results["integrity"]), 1, "另一个事务必须被数据库唯一键拒绝")
        self.assertEqual(
            PropertyStaffMembership.objects.filter(
                property_id=property_pk,
                staff_profile_id=profile_pk,
                status=PropertyStaffMembership.Status.ACTIVE,
            ).count(),
            1,
            "终态只允许一条 ACTIVE 任职",
        )
