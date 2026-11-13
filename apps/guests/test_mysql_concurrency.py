"""收尾专项：真实 MySQL 并发测试（guests 侧，收尾任务一 场景 3/4）。

场景 3：证件并发登记（WB-102）——同物业两个不同 Guest 两线程并发登记
同一证件号 → 恰一个成功，败者得到友好中文错误，且不产生脏索引行
（GuestDocumentIndex 恰一条、占用者为胜者）。

场景 4：主号码并发切换（WB-104）——同 Guest 两线程并发 add_phone 不同
号码且均设主 → 最终恰一个主号码，Guest.phone_hash 与唯一主 GuestPhone
一致（Guest 行锁串行化"降级旧主 → 建新主 → 更新 Guest 主字段"事务）。

SQLite 下 select_for_update 为 no-op，无法构造真实行锁竞争，
skipUnless 仅 MySQL 执行（SQLite 口径由既有白盒用例覆盖）。
"""
import threading
import unittest
from threading import Barrier

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from apps.core.models import Property
from apps.guests.models import Guest, GuestDocument, GuestDocumentIndex, GuestPhone
from apps.guests.services import (
    GuestService,
    decrypt_phone,
    doc_no_hash,
    phone_hash,
    register_document,
)


def _join(test, threads, timeout=60):
    for thread in threads:
        thread.join(timeout=timeout)
        test.assertFalse(thread.is_alive(), "工作线程超时未结束（疑似死锁）")


class GuestsConcurrencyBase(TransactionTestCase):
    """公共夹具：自建物业与两个客人档案。"""

    def setUp(self):
        self.property = Property.objects.create(
            code="MWGS", name="客人并发专项酒店", timezone="Asia/Shanghai")
        self.property_pk = self.property.pk
        self.guest_a = Guest.objects.create(
            property=self.property, full_name="并发客人甲")
        self.guest_b = Guest.objects.create(
            property=self.property, full_name="并发客人乙")


@unittest.skipUnless(
    connection.vendor == "mysql",
    "证件占用唯一仲裁依赖 GuestDocumentIndex 行锁 + 唯一约束（MySQL），"
    "SQLite 测试环境跳过。")
class GuestDocumentConcurrentRegisterTests(GuestsConcurrencyBase):
    """场景 3（WB-102）：两个 Guest 并发登记同一证件号，恰一个成功。"""

    def test_same_doc_no_concurrent_register_exactly_one_wins(self):
        doc_no = "MWGS-DOC-330106199001011234"
        digest = doc_no_hash(doc_no)
        worker_count = 2
        barrier = Barrier(worker_count)
        results = {"winners": [], "losers": [], "errors": []}

        def register_worker(guest):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                try:
                    register_document(
                        guest=guest, doc_type="ID_CARD", doc_no=doc_no)
                    results["winners"].append(guest.pk)
                except ValidationError as exc:
                    results["losers"].append((guest.pk, str(exc)))
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=register_worker, args=(guest,))
            for guest in (self.guest_a, self.guest_b)
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        self.assertEqual(results["errors"], [],
                         f"并发登记出现意外异常: {results['errors']}")
        self.assertEqual(len(results["winners"]), 1,
                         "同物业同一证件号并发登记恰一个成功")
        self.assertEqual(len(results["losers"]), 1,
                         "败者必须收到业务拒绝")
        loser_pk, loser_message = results["losers"][0]
        # 败者得到友好中文错误（两条文案路径均以"该证件已登记"开头）
        self.assertIn("该证件已登记", loser_message)
        self.assertIn("客人档案", loser_message)
        # 不产生脏索引行：恰一条占用记录，占用者是胜者
        index_rows = list(GuestDocumentIndex.objects.filter(
            property_id=self.property_pk, doc_no_hash=digest))
        self.assertEqual(len(index_rows), 1,
                         "证件占用索引不得出现重复/脏行")
        self.assertEqual(index_rows[0].guest_id, results["winners"][0])
        # 胜者有证件明细；败者没有该证件的明细（整体回滚，无脏数据）
        self.assertTrue(GuestDocument.objects.filter(
            guest_id=results["winners"][0], doc_no_hash=digest).exists())
        self.assertFalse(GuestDocument.objects.filter(
            guest_id=loser_pk, doc_no_hash=digest).exists())
        self.assertEqual(GuestDocument.objects.filter(
            doc_no_hash=digest).count(), 1)


@unittest.skipUnless(
    connection.vendor == "mysql",
    "主号码切换依赖 Guest 行锁串行化（select_for_update 在 SQLite 下为 "
    "no-op），SQLite 测试环境跳过。")
class GuestPrimaryPhoneConcurrentSwitchTests(GuestsConcurrencyBase):
    """场景 4（WB-104）：同 Guest 并发切换主号码，最终恰一个主号码。"""

    def test_concurrent_primary_phone_switch_single_primary_consistent(self):
        # 初始档案带主号码 P0
        guest = GuestService.create_guest(
            property_obj=self.property, full_name="主号码并发客人",
            phone="13800030000")
        guest_pk = guest.pk
        initial_digest = phone_hash("13800030000")
        worker_count = 2
        barrier = Barrier(worker_count)
        results = {"done": [], "errors": []}
        numbers = {"worker-A": "13811113111", "worker-B": "13822223222"}

        def switch_worker(worker_name):
            close_old_connections()
            try:
                current = Guest.objects.get(pk=guest_pk)
                barrier.wait(timeout=15)
                GuestService.add_phone(
                    guest=current, phone=numbers[worker_name],
                    is_primary=True, source="并发专项")
                results["done"].append(worker_name)
            except Exception as exc:  # pragma: no cover - 防御
                results["errors"].append(repr(exc))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=switch_worker, args=(name,))
            for name in ("worker-A", "worker-B")
        ]
        for thread in threads:
            thread.start()
        _join(self, threads)

        self.assertEqual(results["errors"], [],
                         f"并发切换出现意外异常: {results['errors']}")
        self.assertEqual(len(results["done"]), 2, "两次主号码切换都成功")

        # 终态不变量①：该客人恰一个主号码
        primaries = list(GuestPhone.objects.filter(
            guest_id=guest_pk, is_primary=True))
        self.assertEqual(len(primaries), 1, "并发切换后必须恰一个主号码")
        primary = primaries[0]
        # 终态不变量②：Guest 主字段与唯一主 GuestPhone 一致
        guest = Guest.objects.get(pk=guest_pk)
        self.assertEqual(guest.phone_hash, primary.phone_hash)
        self.assertEqual(
            decrypt_phone(guest.phone_ciphertext),
            decrypt_phone(primary.phone_ciphertext))
        self.assertIn(primary.phone_hash,
                      {phone_hash(numbers["worker-A"]),
                       phone_hash(numbers["worker-B"])},
                      "主号码必须是两个并发号码之一（后提交者获胜）")
        # 终态不变量③：历史号码保留且全部降级（P0 + 两个新号 = 3 条）
        all_numbers = GuestPhone.objects.filter(guest_id=guest_pk)
        self.assertEqual(all_numbers.count(), 3)
        for row in all_numbers:
            if row.pk == primary.pk:
                continue
            self.assertFalse(row.is_primary)
            self.assertIsNone(row.primary_guest_key)
        self.assertTrue(all_numbers.filter(
            phone_hash=initial_digest).exists(), "旧主号码保留为历史")
        # 终态不变量④：主号码唯一键（primary_guest_key）与唯一主行一致
        self.assertEqual(primary.primary_guest_key, str(guest_pk))
        self.assertEqual(
            GuestPhone.objects.filter(primary_guest_key=str(guest_pk)).count(),
            1)
