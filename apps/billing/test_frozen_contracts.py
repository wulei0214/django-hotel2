"""冻结契约回归测试（五主线开发第一批 · TDD 守卫锁定）。

本文件锁定两类"不得被后续开发破坏"的既有权威契约：

1. 押金冻结决策 FD（DEC-DEP-01）：固定 30%、服务端 Decimal 计算、
   明确的 ROUND_HALF_UP 舍入、不采信客户端金额、无第二套可覆盖来源。
   验收锚点 = 688.00 × 30% = 206.40（用户冻结决策明确指定的回归值）。
2. 阶段 D 迁移不得绕过的权威入口契约：post_entry 关键词签名冻结
   （无 legacy_workflow）、EntryType 六值冻结（无 MIGRATION/LEGACY）、
   ImmutableLedgerEntryQuerySet 四方法全拦截、具名约束存在。

性质：这些测试针对**已实现的权威代码**，是"防回归锁定"而非"待测新功能"，
因此先行落盘不会红灯、不重复建设（不新建任何模型/命令/算法）。真正的阶段 D
被测对象（迁移命令、归档表、执行凭证、受控入口、checksum-v2）仍按其依赖顺序
在被批准后才 TDD 实现，见 GOV03 报告 §2。

依赖基线：main `0f59b4f`；授权见 docs/PMS2.0_当前版本/05_测试审计与缺陷整改/
五主线开发授权与代签边界记录_2026-11-16.md。
"""

import inspect
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry
from apps.billing.services_v2 import BillingService
from apps.core.models import Property
from apps.guests.models import Guest
from apps.inventory.models import RatePlan, RoomType
from apps.reservations.models import Reservation, ReservationRoom


@override_settings(DEBUG=True, DJANGO_ENV="test")
class DepositFrozenDecisionTests(TestCase):
    """冻结决策 FD：688.00 × 30% = 206.40，服务端权威、客户端金额不采信。"""

    prop: Property
    rt: RoomType
    rate: RatePlan
    finance: User

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.rt = RoomType.objects.create(
            property=cls.prop,
            code="FD-DEP-STD",
            name="押金冻结测试房型",
            capacity=2,
        )
        cls.rate = RatePlan.objects.create(
            property=cls.prop,
            code="FD-DEP-BAR",
            name="押金冻结门市价",
            valid_from=timezone.localdate(),
        )
        cls.finance = User.objects.create_user(
            username="fd_dep_finance",
            password="x",
        )

    def _make_booking(self, total, suffix):
        guest = Guest.objects.create(
            property=self.prop,
            full_name="押金冻结测试客",
        )
        res = Reservation.objects.create(
            property=self.prop,
            guest=guest,
            booking_no=f"FD-{suffix}",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1,
            total_amount=Decimal(str(total)),
            currency="CNY",
        )
        ReservationRoom.objects.create(
            property=self.prop,
            reservation=res,
            room_type=self.rt,
            rate_plan=self.rate,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal(str(total)),
        )
        Folio.objects.create(
            property=self.prop,
            reservation=res,
            guest=guest,
            currency="CNY",
        )
        return res

    def test_688_times_30_percent_is_206_40(self):
        """冻结验收锚点：688.00 → 206.40（ROUND_HALF_UP，逐分精确）。"""
        res = self._make_booking("688.00", "688")
        deposit, created = BillingService.record_deposit(
            property_obj=self.prop,
            reservation=res,
            amount="0.01",  # 故意传极小值；服务端必须忽略并自算
            method="TRANSFER",
            idempotency_key="fd-dep-688",
            received_by=self.finance,
        )
        self.assertTrue(created)
        self.assertEqual(deposit.amount, Decimal("206.40"))

    def test_client_display_amount_never_overrides_server_value(self):
        """客户端展示金额不采信：即使传入 999.99，仍按服务端 206.40。"""
        res = self._make_booking("688.00", "688C")
        deposit, _ = BillingService.record_deposit(
            property_obj=self.prop,
            reservation=res,
            amount="999.99",
            method="TRANSFER",
            idempotency_key="fd-dep-688-client",
            received_by=self.finance,
            client_display_amount="1.00",
        )
        self.assertEqual(deposit.amount, Decimal("206.40"))

    def test_no_deposit_policy_still_uses_server_30_percent(self):
        """无 DepositPolicy 配置时仍强制 30%，不存在第二套可覆盖来源。

        本测试房型/房价计划刻意不创建 DepositPolicy，验证固定比例是服务端
        唯一权威，不依赖可选政策行。
        """
        guest = Guest.objects.create(
            property=self.prop,
            full_name="押金无政策测试客",
        )
        res = Reservation.objects.create(
            property=self.prop,
            guest=guest,
            booking_no="FD-NOPOL",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("688.00"),
            currency="CNY",
        )
        ReservationRoom.objects.create(
            property=self.prop,
            reservation=res,
            room_type=self.rt,
            rate_plan=self.rate,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal("688.00"),
        )
        Folio.objects.create(
            property=self.prop,
            reservation=res,
            guest=guest,
            currency="CNY",
        )
        deposit, _ = BillingService.record_deposit(
            property_obj=self.prop,
            reservation=res,
            amount="300.00",
            method="TRANSFER",
            idempotency_key="fd-dep-688-nopol",
            received_by=self.finance,
        )
        self.assertEqual(deposit.amount, Decimal("206.40"))


@override_settings(DEBUG=True, DJANGO_ENV="test")
class AuthoritativeEntryContractTests(TestCase):
    """阶段 D 迁移不得绕过的账本权威入口契约（防回归锁定）。"""

    def test_post_entry_signature_has_no_legacy_workflow(self):
        """post_entry 关键词签名冻结：无 legacy_workflow 参数。

        历史 REVERSAL 只能经阶段 D 的独立受控入口（带执行凭证）创建，
        普通记账入口不得提供绕过开关。
        """
        params = inspect.signature(BillingService.post_entry).parameters
        self.assertIn("channel_payment", params)
        self.assertNotIn("legacy_workflow", params)
        self.assertNotIn("credential", params)
        for name, p in params.items():
            self.assertEqual(
                p.kind,
                inspect.Parameter.KEYWORD_ONLY,
                msg=f"post_entry.{name} 应为 keyword-only",
            )

    def test_entry_type_choices_are_frozen_to_six(self):
        """EntryType 恰六值，无 MIGRATION/LEGACY_IMPORT 新增。"""
        self.assertEqual(
            set(LedgerEntry.EntryType.values),
            {
                "ROOM_CHARGE",
                "DEPOSIT",
                "PAYMENT",
                "REFUND",
                "ADJUSTMENT",
                "REVERSAL",
            },
        )

    def test_immutable_queryset_blocks_all_four_mutation_methods(self):
        """不可变 QuerySet 四方法（update/delete/bulk_create/bulk_update）全拦截。"""
        qs = LedgerEntry.objects.all()
        for call in (
            lambda: qs.update(amount=Decimal("1.00")),
            lambda: qs.delete(),
            lambda: qs.bulk_create([]),
            lambda: qs.bulk_update([], ["amount"]),
        ):
            with self.assertRaises(ValidationError):
                call()

    def test_reversal_approval_constraint_is_named_and_present(self):
        """具名约束 ck_ledger_reversal_approval_link 存在（库级 SoD 守卫）。"""
        names = {c.name for c in LedgerEntry._meta.constraints}
        self.assertIn("ck_ledger_reversal_approval_link", names)
        self.assertIn("ck_ledger_amount_nonzero", names)
        self.assertIn("ck_ledger_reversal_has_reason", names)
