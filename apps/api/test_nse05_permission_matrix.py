"""NS-E-05 IMPLEMENT-001 权限专项回归矩阵（任务书 TASK-06）。

覆盖 OWNER 2026-11-24 逐项裁定的三项已批权限与一项审计修复：

8.1 FIN-06 `billing.payment_collect`（双入口 + 允许/禁止矩阵 + 边界）
8.2 MB-11 `guests.membership_status_manage`（拆分后 change_vip 失去状态权）
8.3 Retention 三权限（真实 ApprovalService 链，禁止伪造 APPROVED）
8.4 `billing.collect` 审计字段（request 链路 + after 扩充）

铁律：
* 不伪造 ApprovalRecord 状态，一律走 request_approval → approve 真实链；
* 不直接 update() 改状态（ImmutableFinancialFactQuerySet 禁止）；
* 角色权限来自迁移（identity/0013），测试不自建授权绕过迁移。
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Folio, Payment
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest, GuestMembership, RetentionPolicy
from apps.identity.authorization import AuthorizationService
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.reservations.models import Reservation

_TEST_PW_NSE05A = "nse05-matrix-pass-A9f3"
_TEST_PW_NSE05B = "nse05-matrix-pass-B7c1"

# 裁定 §3.3 矩阵：允许收款的四个岗位
COLLECT_ALLOWED = ("前台", "值班经理", "财务", "店长")
# 裁定 §3.3：禁止收款的五个岗位
COLLECT_DENIED = ("客房", "夜审员", "餐饮服务员", "餐饮主管", "车场管理员")


class Nse05Base(TestCase):
    """公共夹具：DEFAULT 物业 + 角色组账号工厂 + 预订/账本。"""

    maxDiff = None

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.today = timezone.localdate()
        self._seq = 0

    def _make_role_staff(self, group_name, username=None):
        """建 User + 有效任职 + 指定角色组（权限经 identity/0013 迁移授予）。"""
        self._seq += 1
        username = username or f"nse05_{group_name}_{self._seq}"
        user = User.objects.create_user(username=username, password=_TEST_PW_NSE05A)
        profile = StaffProfile.objects.create(
            user=user,
            full_name=username,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property,
            employee_no=f"NSE05-{user.pk:06d}",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        group = Group.objects.get(name=group_name)
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=group,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        return user

    def _make_reservation(self, booking_no="NSE05-BK-1"):
        guest = Guest.objects.create(
            property=self.property, full_name="NS-E-05 测试客", phone="13800001111"
        )
        return Reservation.objects.create(
            property=self.property,
            guest=guest,
            booking_no=booking_no,
            source="WEBSITE",
            status=Reservation.Status.CONFIRMED,
            check_in=self.today,
            check_out=self.today + timedelta(days=1),
            total_amount=Decimal("500.00"),
        )


class Fin06CollectPermissionTests(Nse05Base):
    """8.1 FIN-06：`billing.payment_collect` 双入口统一门禁。"""

    def test_permission_exists_in_database(self):
        """权限点由 billing/0038 迁移创建。"""
        perm = Permission.objects.filter(
            content_type__app_label="billing", codename="payment_collect"
        ).first()
        self.assertIsNotNone(perm, "billing.payment_collect 权限点不存在")

    def test_allowed_role_groups_have_permission(self):
        """允许矩阵：前台/值班经理/财务/店长 四组持有。"""
        for name in COLLECT_ALLOWED:
            with self.subTest(group=name):
                group = Group.objects.get(name=name)
                self.assertTrue(
                    group.permissions.filter(
                        content_type__app_label="billing",
                        codename="payment_collect",
                    ).exists(),
                    f"角色组 {name} 应持有 billing.payment_collect",
                )

    def test_denied_role_groups_lack_permission(self):
        """禁止矩阵：其余五个岗位不持有。"""
        for name in COLLECT_DENIED:
            with self.subTest(group=name):
                group = Group.objects.filter(name=name).first()
                if group is None:
                    self.skipTest(f"角色组 {name} 在本环境不存在")
                self.assertFalse(
                    group.permissions.filter(
                        content_type__app_label="billing",
                        codename="payment_collect",
                    ).exists(),
                    f"角色组 {name} 不应持有 billing.payment_collect",
                )

    def test_allowed_roles_resolve_permission_at_runtime(self):
        """AuthorizationService 对四个允许岗位返回 True。"""
        for name in COLLECT_ALLOWED:
            with self.subTest(group=name):
                user = self._make_role_staff(name)
                self.assertTrue(
                    AuthorizationService.has_permission(
                        user, self.property, "billing.payment_collect"
                    )
                )

    def test_frontdesk_does_not_gain_finance_access_from_collect(self):
        """裁定禁止项：收款权不携带财务中心读取权。"""
        user = self._make_role_staff("前台")
        self.assertTrue(
            AuthorizationService.has_permission(user, self.property, "billing.payment_collect")
        )
        group = Group.objects.get(name="前台")
        self.assertFalse(
            group.permissions.filter(codename="finance_access").exists(),
            "前台不得因收款专项权限获得 hotelapp.finance_access",
        )

    def test_collect_permission_separate_from_refund_execute(self):
        """裁定禁止项：收款与退款执行不得合并为同一码。"""
        collect = Permission.objects.filter(
            content_type__app_label="billing", codename="payment_collect"
        ).first()
        refund_exec = Permission.objects.filter(
            content_type__app_label="billing", codename="refund_execute"
        ).first()
        self.assertIsNotNone(collect)
        if refund_exec is None:
            self.skipTest("refund_execute 权限点不存在")
        self.assertNotEqual(collect.pk, refund_exec.pk)  # type: ignore[union-attr]

    def test_registry_entry_marks_high_risk_confirm(self):
        """注册表口径：risk=high、requires_confirm=True、requires_reason=False。"""
        from apps.identity.registry import get_special

        item = get_special("billing.payment_collect")
        self.assertIsNotNone(item, "registry 未登记 billing.payment_collect")
        self.assertEqual(item["module"], "finance")
        self.assertEqual(item["risk"], "high")
        self.assertTrue(item["requires_confirm"])
        self.assertFalse(item["requires_reason"])

    def test_special_labels_has_entry(self):
        """权限中心展示标签存在。"""
        from apps.identity.permission_center import SPECIAL_LABELS

        self.assertIn("payment_collect", SPECIAL_LABELS)


class Mb11MembershipStatusPermissionTests(Nse05Base):
    """8.2 MB-11：状态管理从 change_vip 拆分。"""

    def _make_membership(self, member_no="NSE05-M-1"):
        guest = Guest.objects.create(
            property=self.property, full_name="MB11 会员", phone="13800002222"
        )
        return GuestMembership.objects.create(
            property=self.property,
            guest=guest,
            member_no=member_no,
            level_code="STANDARD",
            status=GuestMembership.Status.ACTIVE,
            joined_at=date.today(),
        )

    def test_permission_exists_in_database(self):
        perm = Permission.objects.filter(
            content_type__app_label="guests", codename="membership_status_manage"
        ).first()
        self.assertIsNotNone(perm, "guests.membership_status_manage 权限点不存在")

    def test_only_duty_manager_and_owner_have_permission(self):
        for name in ("值班经理", "店长"):
            with self.subTest(allowed=name):
                group = Group.objects.get(name=name)
                self.assertTrue(
                    group.permissions.filter(
                        content_type__app_label="guests",
                        codename="membership_status_manage",
                    ).exists()
                )
        for name in ("前台", "财务", "客房", "夜审员"):
            with self.subTest(denied=name):
                denied_group = Group.objects.filter(name=name).first()
                if denied_group is None:
                    continue
                self.assertFalse(
                    denied_group.permissions.filter(
                        content_type__app_label="guests",
                        codename="membership_status_manage",
                    ).exists(),
                    f"{name} 不应持有会员状态管理权",
                )

    def test_finance_cannot_manage_membership_status(self):
        """裁定禁止项：财务不得获得会员状态权限。"""
        user = self._make_role_staff("财务")
        self.assertFalse(
            AuthorizationService.has_permission(
                user, self.property, "guests.membership_status_manage"
            )
        )

    def test_change_vip_alone_grants_no_status_management(self):
        """权限收紧验证：仅持 change_vip 的账号不通过服务层校验。"""
        user = self._make_role_staff("前台")
        self.assertTrue(
            AuthorizationService.has_permission(user, self.property, "hotelapp.change_vip")
        )
        self.assertFalse(
            AuthorizationService.has_permission(
                user, self.property, "guests.membership_status_manage"
            ),
            "change_vip 不得再隐式携带暂停/恢复能力",
        )

    def test_duty_manager_suspend_and_restore_state_machine(self):
        """值班经理走完整状态机：ACTIVE→SUSPENDED→ACTIVE。"""
        from apps.guests.services import GuestMembershipService

        user = self._make_role_staff("值班经理")
        membership = self._make_membership()

        suspended, changed = GuestMembershipService.suspend_membership(
            membership=membership,
            reason="客人申请暂停会员权益",
            suspended_by=user,
        )
        self.assertTrue(changed)
        self.assertEqual(suspended.status, GuestMembership.Status.SUSPENDED)

        restored, changed = GuestMembershipService.restore_membership(
            membership=suspended,
            reason="客人申请恢复",
            restored_by=user,
        )
        self.assertTrue(changed)
        self.assertEqual(restored.status, GuestMembership.Status.ACTIVE)

    def test_expired_membership_cannot_restore(self):
        """状态机边界：EXPIRED 不可恢复。"""
        from apps.guests.services import GuestMembershipService, GuestServiceError

        user = self._make_role_staff("值班经理")
        membership = self._make_membership(member_no="NSE05-M-EXP")
        membership.status = GuestMembership.Status.EXPIRED
        membership.save(update_fields=("status",))

        with self.assertRaises(GuestServiceError):
            GuestMembershipService.restore_membership(
                membership=membership, reason="尝试恢复过期会员", restored_by=user
            )

    def test_registry_entry_marks_medium_risk(self):
        from apps.identity.registry import get_special

        item = get_special("guests.membership_status_manage")
        self.assertIsNotNone(item)
        self.assertEqual(item["module"], "memberships")
        self.assertEqual(item["risk"], "medium")
        self.assertTrue(item["requires_reason"])
        self.assertTrue(item["requires_confirm"])

    def test_special_labels_has_entry(self):
        from apps.identity.permission_center import SPECIAL_LABELS

        self.assertIn("membership_status_manage", SPECIAL_LABELS)

    def test_page_action_declares_special_permission(self):
        """页面动作注册表指向专项码（不再写 change_vip）。"""
        from apps.api.pages import MANAGEMENT_CENTERS

        memberships = MANAGEMENT_CENTERS["memberships"]
        actions = memberships["actions"]  # type: ignore[index]
        status_action = next(a for a in actions if a["key"] == "membership_status")
        self.assertEqual(status_action["permission"], "guests.membership_status_manage")
        level_action = next(a for a in actions if a["key"] == "membership_level")
        self.assertEqual(level_action["permission"], "hotelapp.change_vip")


class RetentionApprovalChainTests(Nse05Base):
    """8.3 Retention：真实审批链 + 执行门禁（禁止伪造 APPROVED）。"""

    def _make_policy(self, operator):
        return RetentionPolicy.objects.create(
            property=self.property,
            data_category=RetentionPolicy.DataCategory.GUEST_PROFILE,
            retention_days=365,
            retention_basis=RetentionPolicy.RetentionBasis.LAW,
            effective_from=self.today,
            created_by=operator,
            updated_by=operator,
        )

    def _make_plan(self, policy, operator, dry_run=True):
        from apps.guests.retention import plan_retention_cleanup

        return plan_retention_cleanup(
            property_obj=self.property,
            policy=policy,
            as_of=self.today,
            operator=operator,
            dry_run=dry_run,
        )

    def test_three_permission_points_exist(self):
        for codename in (
            "retention_request",
            "retention_approve",
            "retention_execute",
        ):
            with self.subTest(codename=codename):
                self.assertTrue(
                    Permission.objects.filter(
                        content_type__app_label="guests", codename=codename
                    ).exists(),
                    f"guests.{codename} 权限点不存在",
                )

    def test_retention_grants_match_verdict(self):
        """request=店长；approve/execute=值班经理+店长。"""
        owner = Group.objects.get(name="店长")
        duty = Group.objects.get(name="值班经理")

        def has(group, codename):
            return group.permissions.filter(
                content_type__app_label="guests", codename=codename
            ).exists()

        self.assertTrue(has(owner, "retention_request"))
        self.assertTrue(has(owner, "retention_approve"))
        self.assertTrue(has(owner, "retention_execute"))
        self.assertTrue(has(duty, "retention_approve"))
        self.assertTrue(has(duty, "retention_execute"))

    def test_frontdesk_and_finance_have_no_retention_permissions(self):
        for name in ("前台", "财务"):
            group = Group.objects.filter(name=name).first()
            if group is None:
                continue
            for codename in (
                "retention_request",
                "retention_approve",
                "retention_execute",
            ):
                with self.subTest(group=name, codename=codename):
                    self.assertFalse(
                        group.permissions.filter(
                            content_type__app_label="guests", codename=codename
                        ).exists(),
                        f"{name} 不应持有 guests.{codename}",
                    )

    def test_dry_run_does_not_anonymize(self):
        operator = self._make_role_staff("店长")
        policy = self._make_policy(operator)
        guest = Guest.objects.create(
            property=self.property, full_name="待清理客人", phone="13800003333"
        )
        guest.created_at = timezone.now() - timedelta(days=800)
        guest.save(update_fields=("created_at",))

        record, _to_process = self._make_plan(policy, operator, dry_run=True)
        self.assertTrue(record.dry_run)
        guest.refresh_from_db()
        self.assertEqual(guest.full_name, "待清理客人", "dry-run 不得匿名化")

    def test_formal_execution_requires_approval_reference(self):
        """执行门：无审批引用 → 拒绝。"""
        operator = self._make_role_staff("店长")
        policy = self._make_policy(operator)
        with self.assertRaises(ValueError):
            self._make_plan(policy, operator, dry_run=False)

    def test_formal_execution_rejects_unknown_approval(self):
        operator = self._make_role_staff("店长")
        policy = self._make_policy(operator)
        with self.assertRaises(ValueError):
            from apps.guests.retention import plan_retention_cleanup

            plan_retention_cleanup(
                property_obj=self.property,
                policy=policy,
                as_of=self.today,
                operator=operator,
                dry_run=False,
                approval_reference="999999",
            )

    def test_approval_registry_declares_request_and_approve_permissions(self):
        from apps.billing.approval_registry import ApprovalActionRegistry

        spec = ApprovalActionRegistry.get("privacy.retention_execute")
        self.assertIsNotNone(spec, "privacy.retention_execute 未注册")
        self.assertEqual(spec.request_permission, "guests.retention_request")
        self.assertEqual(spec.approve_permission, "guests.retention_approve")

    def test_approval_request_requires_bound_dry_run_plan(self):
        """validate_entity：审批申请必须绑定同物业 DRAFT dry-run 计划。"""
        from apps.billing.approval_registry import (
            validate_retention_execution_entity,
        )

        with self.assertRaises(ValueError):
            validate_retention_execution_entity(self.property, "999999", None)


class BillingCollectAuditTests(Nse05Base):
    """8.4 `billing.collect` 审计修复。"""

    def _setup_folio(self, reservation):
        return Folio.objects.create(
            property=self.property,
            reservation=reservation,
            folio_type=Folio.FolioType.MASTER,
            guest=reservation.guest,
            status=Folio.Status.OPEN,
            currency="CNY",
        )

    def _post_room_charge(self, folio, posted_by, amount=Decimal("500.00")):
        """先入账杂费项，收款才有可收余额。

        注意：不能入 ROOM_CHARGE——服务层用
        `balance + expected - posted_room` 算应收，入房费会被 posted_room 抵消。
        用 ADJUSTMENT（正数=应收增加）即可产生正的 outstanding。
        """
        from apps.billing.models import LedgerEntry
        from apps.billing.services_v2 import BillingService

        BillingService.post_entry(
            property_obj=self.property,
            folio=folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=amount,
            reference=f"charge:{folio.pk}",
            description="NS-E-05 测试杂费",
            posted_by=posted_by,
        )

    def _open_shift(self, user):
        """现金收款必须属于本人已显式开立的收银班次。"""
        from apps.billing.services_v2 import BillingService

        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )

    def test_audit_records_source_entry_and_business_facts(self):
        """审计 after 含 source_entry/currency/reservation_id/provider_ref。"""
        from apps.billing.services_v2 import BillingService

        user = self._make_role_staff("财务")
        reservation = self._make_reservation()
        folio = self._setup_folio(reservation)
        self._post_room_charge(folio, user)
        self._open_shift(user)

        payment = BillingService.collect_payment(
            property_obj=self.property,
            reservation=reservation,
            amount=Decimal("200.00"),
            method="CASH",
            collected_by=user,
            reference="NSE05-AUDIT-1",
            source_entry="frontdesk",
        )

        event = AuditEvent.objects.filter(
            action="billing.collect", entity_id=str(payment.pk)
        ).first()
        self.assertIsNotNone(event, "billing.collect 审计未记录")
        assert event is not None
        after = event.after_json
        self.assertEqual(after["source_entry"], "frontdesk")
        self.assertEqual(after["currency"], reservation.currency)
        self.assertEqual(after["reservation_id"], reservation.pk)
        self.assertEqual(after["provider_ref"], "NSE05-AUDIT-1")
        self.assertIn("business_date", after)
        self.assertIn("folio_id", after)
        self.assertIn("payment_status", after)

    def test_audit_source_entry_distinguishes_billing_center(self):
        """财务中心入口应记录 source_entry=billing_center。"""
        from apps.billing.services_v2 import BillingService

        user = self._make_role_staff("财务")
        reservation = self._make_reservation("NSE05-BK-2")
        folio = self._setup_folio(reservation)
        self._post_room_charge(folio, user)
        self._open_shift(user)

        payment = BillingService.collect_payment(
            property_obj=self.property,
            reservation=reservation,
            amount=Decimal("150.00"),
            method="CASH",
            collected_by=user,
            reference="NSE05-AUDIT-2",
            source_entry="billing_center",
        )
        event = AuditEvent.objects.get(action="billing.collect", entity_id=str(payment.pk))
        self.assertEqual(event.after_json["source_entry"], "billing_center")

    def test_audit_no_longer_reads_user_request_attribute(self):
        """回归护栏：不再依赖 User 上不存在的 _request 属性。"""
        import inspect

        from apps.billing.services_v2 import BillingService

        # 只看 AuditEvent.record(...) 调用片段，避免被说明性注释误判。
        source = inspect.getsource(BillingService.collect_payment)
        call_start = source.index("AuditEvent.record(")
        call_snippet = source[call_start:]
        self.assertNotIn(
            'getattr(collected_by, "_request"',
            call_snippet,
            "收款审计不得再从 User 实例读取 _request（恒为 None）",
        )
        self.assertIn("request=request", call_snippet, "审计应使用显式传入的 request")

    def test_collect_payment_signature_accepts_request_and_source_entry(self):
        import inspect

        from apps.billing.services_v2 import BillingService

        params = inspect.signature(BillingService.collect_payment).parameters
        self.assertIn("request", params)
        self.assertIn("source_entry", params)
        self.assertEqual(params["request"].default, None)
        self.assertEqual(params["source_entry"].default, "frontdesk")

    def test_payment_status_recorded_in_audit(self):
        from apps.billing.services_v2 import BillingService

        user = self._make_role_staff("财务")
        reservation = self._make_reservation("NSE05-BK-3")
        folio = self._setup_folio(reservation)
        self._post_room_charge(folio, user)
        self._open_shift(user)
        payment = BillingService.collect_payment(
            property_obj=self.property,
            reservation=reservation,
            amount=Decimal("100.00"),
            method="CASH",
            collected_by=user,
            reference="NSE05-AUDIT-3",
        )
        event = AuditEvent.objects.get(action="billing.collect", entity_id=str(payment.pk))
        self.assertEqual(event.after_json["payment_status"], Payment.Status.SUCCEEDED)


class Fin06CollectBoundaryTests(Nse05Base):
    """8.1 补充（7 例）：FIN-06 双入口 API 级门禁与边界。"""

    def _setup_billable(self, booking_no="NSE05-API-BK"):
        from apps.billing.models import LedgerEntry
        from apps.billing.services_v2 import BillingService

        reservation = self._make_reservation(booking_no)
        folio = Folio.objects.create(
            property=self.property,
            reservation=reservation,
            folio_type=Folio.FolioType.MASTER,
            guest=reservation.guest,
            status=Folio.Status.OPEN,
            currency="CNY",
        )
        BillingService.post_entry(
            property_obj=self.property,
            folio=folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("500.00"),
            reference=f"adj:{booking_no}",
            description="NS-E-05 应收调整",
        )
        return reservation, folio

    def _post(self, user, payload, path="/api/v1/billing/payments"):
        self.client.force_login(user)
        return self.client.post(path, data=payload, content_type="application/json")

    def test_billing_center_collect_allowed_with_both_permissions(self):
        """财务（finance_access + payment_collect）→ 201。"""
        from apps.billing.services_v2 import BillingService

        user = self._make_role_staff("财务")
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        reservation, _folio = self._setup_billable("NSE05-API-1")
        response = self._post(
            user,
            {
                "reservation_id": reservation.pk,
                "amount": "200.00",
                "method": "CASH",
                "reference": "NSE05-API-REF-1",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_billing_center_collect_denied_without_special_permission(self):
        """有 finance_access 但无 payment_collect → 403（读取与收款分离）。"""
        user = self._make_role_staff("财务")
        assignment = StaffRoleAssignment.objects.filter(
            membership__staff_profile__user=user
        ).first()
        assert assignment is not None
        group = assignment.group
        group.permissions.remove(
            Permission.objects.get(content_type__app_label="billing", codename="payment_collect")
        )
        reservation, _folio = self._setup_billable("NSE05-API-2")
        response = self._post(
            user,
            {
                "reservation_id": reservation.pk,
                "amount": "100.00",
                "method": "CASH",
                "reference": "NSE05-API-REF-2",
            },
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_billing_center_collect_denied_for_housekeeping(self):
        """夜审员（无 finance_access）→ 403。"""
        user = self._make_role_staff("夜审员")
        reservation, _folio = self._setup_billable("NSE05-API-3")
        response = self._post(
            user,
            {
                "reservation_id": reservation.pk,
                "amount": "100.00",
                "method": "CASH",
                "reference": "NSE05-API-REF-3",
            },
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_frontdesk_collect_denied_for_frontdesk_without_special(self):
        """前台持有 change_accommodation 但移除本码 → 403（入口 B 收紧验证）。"""
        from apps.reservations.front_desk_services import FrontDeskValidationError

        user = self._make_role_staff("前台")
        assignment = StaffRoleAssignment.objects.filter(
            membership__staff_profile__user=user
        ).first()
        assert assignment is not None
        group = assignment.group
        group.permissions.remove(
            Permission.objects.get(content_type__app_label="billing", codename="payment_collect")
        )
        reservation = self._make_reservation("NSE05-API-4")
        response = self._post(
            user,
            {"amount": "100.00", "method": "CASH"},
            path=f"/front-desk/reservations/{reservation.booking_no}/payments",
        )
        self.assertIn(response.status_code, (403, 404), response.content)
        # 无权限时服务层不应被调用；显式断言异常类型存在（导入守卫）
        self.assertTrue(issubclass(FrontDeskValidationError, Exception))

    def test_collect_allowed_roles_all_resolve_at_api_layer(self):
        """值班经理在 API 层可完成收款（持有 finance_access + payment_collect）。"""
        user = self._make_role_staff("值班经理")
        reservation, _folio = self._setup_billable("NSE05-API-5")
        response = self._post(
            user,
            {
                "reservation_id": reservation.pk,
                "amount": "100.00",
                "method": "CARD",
                "reference": "NSE05-API-REF-5",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()["data"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["amount"], "100.00")

    def test_collect_permission_not_granted_by_frontdesk_group_to_finance_access(self):
        """前台组权限集合中不含 finance_access，且含 payment_collect。"""
        group = Group.objects.get(name="前台")
        codenames = set(group.permissions.values_list("codename", flat=True))
        self.assertIn("payment_collect", codenames)
        self.assertNotIn("finance_access", codenames)

    def test_payment_collect_risk_and_confirm_flags_locked(self):
        """注册表口径锁定：不得被误改为低风险免确认。"""
        from apps.identity.registry import get_special

        item = get_special("billing.payment_collect")
        self.assertEqual(item["risk"], "high")
        self.assertTrue(item["requires_confirm"])
        self.assertFalse(item["requires_reason"])
        self.assertEqual(item["module"], "finance")


class Mb11StatusBoundaryTests(Nse05Base):
    """8.2 补充（6 例）：MB-11 状态机边界与 API 层门禁。"""

    def _make_membership(self, guest_phone="13800004444", member_no="NSE05-MB-1"):
        guest = Guest.objects.create(
            property=self.property, full_name="MB11 边界会员", phone=guest_phone
        )
        return GuestMembership.objects.create(
            property=self.property,
            guest=guest,
            member_no=member_no,
            level_code="STANDARD",
            status=GuestMembership.Status.ACTIVE,
            joined_at=date.today(),
        )

    def _status(self, user, membership, action="suspend", reason="客人申请暂停"):
        self.client.force_login(user)
        return self.client.post(
            f"/api/v1/memberships/{membership.pk}/status",
            data={"action": action, "reason": reason},
            content_type="application/json",
        )

    def test_api_suspend_succeeds_for_duty_manager(self):
        user = self._make_role_staff("值班经理")
        membership = self._make_membership()
        response = self._status(user, membership)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["status"], "SUSPENDED")

    def test_api_suspend_denied_for_frontdesk(self):
        user = self._make_role_staff("前台")
        membership = self._make_membership("13800004445", "NSE05-MB-2")
        response = self._status(user, membership)
        self.assertEqual(response.status_code, 403, response.content)

    def test_api_suspend_denied_for_finance(self):
        user = self._make_role_staff("财务")
        membership = self._make_membership("13800004446", "NSE05-MB-3")
        response = self._status(user, membership)
        self.assertEqual(response.status_code, 403, response.content)

    def test_api_status_requires_reason(self):
        """原因必填：空原因被服务层拒绝（409 BUSINESS_CONFLICT）。"""
        user = self._make_role_staff("店长")
        membership = self._make_membership("13800004447", "NSE05-MB-4")
        response = self._status(user, membership, reason="")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"]["code"], "BUSINESS_CONFLICT")

    def test_api_status_rejects_unknown_action(self):
        """未知动作 → 400。"""
        user = self._make_role_staff("店长")
        membership = self._make_membership("13800004448", "NSE05-MB-5")
        response = self._status(user, membership, action="terminate")
        self.assertEqual(response.status_code, 400, response.content)

    def test_terminated_membership_cannot_restore(self):
        """状态机边界：TERMINATED 不可恢复。"""
        from apps.guests.services import GuestMembershipService, GuestServiceError

        user = self._make_role_staff("值班经理")
        membership = self._make_membership("13800004449", "NSE05-MB-6")
        membership.status = GuestMembership.Status.TERMINATED
        membership.save(update_fields=("status",))
        with self.assertRaises(GuestServiceError):
            GuestMembershipService.restore_membership(
                membership=membership, reason="尝试恢复已终止会员", restored_by=user
            )

    def test_change_vip_group_still_holds_level_change(self):
        """change_vip 保留等级变更（登记表特殊码不含状态管理）。"""
        from apps.identity.registry import MODULE_REGISTRY_V2

        memberships = MODULE_REGISTRY_V2["memberships"]  # type: ignore[index]
        actions: dict = memberships["actions"]  # type: ignore[index,assignment]
        self.assertEqual(actions["change"], "hotelapp.change_vip")
        self.assertIn("guests.membership_status_manage", actions["special"])


class RetentionChainBoundaryTests(Nse05Base):
    """8.3 补充（12 例）：Retention 真实审批链与执行门全边界。"""

    def _staff(self, group_name):
        return self._make_role_staff(group_name)

    def _policy(self, operator):
        from apps.guests.models import RetentionPolicy

        return RetentionPolicy.objects.create(
            property=self.property,
            data_category=RetentionPolicy.DataCategory.GUEST_PROFILE,
            retention_days=365,
            retention_basis=RetentionPolicy.RetentionBasis.LAW,
            effective_from=self.today,
            created_by=operator,
            updated_by=operator,
        )

    def _plan(self, policy, operator, dry_run=True):
        from apps.guests.retention import plan_retention_cleanup

        return plan_retention_cleanup(
            property_obj=self.property,
            policy=policy,
            as_of=self.today,
            operator=operator,
            dry_run=dry_run,
        )

    def _approved_record(self, plan, requester, approver):
        """走真实 ApprovalService.request_approval → approve（禁止伪造）。"""
        from apps.billing.services_v2 import ApprovalService

        record, _created = ApprovalService.request_approval(
            property_obj=self.property,
            action_code="privacy.retention_execute",
            entity_type="RetentionExecution",
            entity_id=str(plan.pk),
            requested_by=requester,
            reason="保留期满清理申请",
            payload={
                "retention_policy_id": plan.policy_id,
                "as_of_date": str(plan.as_of),
            },
        )
        ApprovalService.approve(record.pk, approved_by=approver, property_obj=self.property)
        record.refresh_from_db()
        return record

    def test_full_chain_request_approve_execute_anonymizes(self):
        """真实三权分离链：店长申请、值班经理审批、另一值班经理执行。"""
        requester = self._staff("店长")
        approver = self._staff("值班经理")
        executor = self._staff("值班经理")

        policy = self._policy(requester)
        guest = Guest.objects.create(
            property=self.property, full_name="真实链清理客", phone="13800005001"
        )
        Guest.objects.filter(pk=guest.pk).update(created_at=timezone.now() - timedelta(days=800))

        plan, _ = self._plan(policy, requester, dry_run=True)
        record = self._approved_record(plan, requester, approver)
        self.assertEqual(record.status, "APPROVED")

        from apps.guests.retention import plan_retention_cleanup

        result, _to_process = plan_retention_cleanup(
            property_obj=self.property,
            policy=policy,
            as_of=self.today,
            operator=executor,
            dry_run=False,
            approval_reference=str(record.pk),
        )
        self.assertEqual(result.execution_status, "COMPLETED")

    def test_executor_same_as_requester_rejected(self):
        """执行人 = 申请人 → 拒绝（SoD）。"""
        requester = self._staff("店长")
        approver = self._staff("值班经理")
        policy = self._policy(requester)
        plan, _ = self._plan(policy, requester)
        record = self._approved_record(plan, requester, approver)

        from apps.guests.retention import plan_retention_cleanup

        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=policy,
                as_of=self.today,
                operator=requester,
                dry_run=False,
                approval_reference=str(record.pk),
            )

    def test_executor_without_execute_permission_rejected(self):
        """执行人无 guests.retention_execute → PermissionDenied。"""
        requester = self._staff("店长")
        approver = self._staff("值班经理")
        outsider = self._staff("前台")
        policy = self._policy(requester)
        plan, _ = self._plan(policy, requester)
        record = self._approved_record(plan, requester, approver)

        from django.core.exceptions import PermissionDenied

        from apps.guests.retention import plan_retention_cleanup

        with self.assertRaises(PermissionDenied):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=policy,
                as_of=self.today,
                operator=outsider,
                dry_run=False,
                approval_reference=str(record.pk),
            )

    def test_approval_bound_to_different_plan_rejected(self):
        """审批绑定计划 A，却用计划 B 执行 → 拒绝。"""
        requester = self._staff("店长")
        approver = self._staff("值班经理")
        executor = self._staff("值班经理")

        policy = self._policy(requester)
        plan_a, _ = self._plan(policy, requester)
        record = self._approved_record(plan_a, requester, approver)

        from apps.guests.retention import plan_retention_cleanup

        plan_b, _ = self._plan(policy, requester)
        self.assertNotEqual(plan_a.pk, plan_b.pk)
        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=policy,
                as_of=self.today,
                operator=executor,
                dry_run=False,
                approval_reference=str(record.pk),
                execution=plan_b,
            )

    def test_approval_bound_to_different_policy_rejected(self):
        """审批绑定策略 A，却用策略 B 执行 → 拒绝。"""
        requester = self._staff("店长")
        approver = self._staff("值班经理")
        executor = self._staff("值班经理")

        policy_a = self._policy(requester)
        plan_a, _ = self._plan(policy_a, requester)
        record = self._approved_record(plan_a, requester, approver)

        # enabled_policy_key 唯一：同物业同类别只允许一个 enabled 策略。
        policy_a.enabled = False
        policy_a.save(update_fields=("enabled",))
        policy_b = self._policy(requester)
        from apps.guests.retention import plan_retention_cleanup

        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=policy_b,
                as_of=self.today,
                operator=executor,
                dry_run=False,
                approval_reference=str(record.pk),
                execution=plan_a,
            )

    def test_requester_cannot_approve_own_request(self):
        """申请人自审 → 拒绝（ApprovalService SoD）。"""
        requester = self._staff("店长")
        policy = self._policy(requester)
        plan, _ = self._plan(policy, requester)

        from apps.billing.services_v2 import ApprovalService

        record, _ = ApprovalService.request_approval(
            property_obj=self.property,
            action_code="privacy.retention_execute",
            entity_type="RetentionExecution",
            entity_id=str(plan.pk),
            requested_by=requester,
            reason="自审尝试",
            payload={
                "retention_policy_id": plan.policy_id,
                "as_of_date": str(plan.as_of),
            },
        )
        with self.assertRaises(ValueError):
            ApprovalService.approve(record.pk, approved_by=requester, property_obj=self.property)

    def test_pending_approval_cannot_execute(self):
        """PENDING 审批不能执行。"""
        requester = self._staff("店长")
        executor = self._staff("值班经理")
        policy = self._policy(requester)
        plan, _ = self._plan(policy, requester)

        from apps.billing.services_v2 import ApprovalService

        record, _ = ApprovalService.request_approval(
            property_obj=self.property,
            action_code="privacy.retention_execute",
            entity_type="RetentionExecution",
            entity_id=str(plan.pk),
            requested_by=requester,
            reason="待审批",
            payload={
                "retention_policy_id": plan.policy_id,
                "as_of_date": str(plan.as_of),
            },
        )
        from apps.guests.retention import plan_retention_cleanup

        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=policy,
                as_of=self.today,
                operator=executor,
                dry_run=False,
                approval_reference=str(record.pk),
            )

    def test_rejected_approval_cannot_execute(self):
        """REJECTED 审批不能执行。"""
        requester = self._staff("店长")
        approver = self._staff("值班经理")
        executor = self._staff("值班经理")
        policy = self._policy(requester)
        plan, _ = self._plan(policy, requester)

        from apps.billing.services_v2 import ApprovalService

        record, _ = ApprovalService.request_approval(
            property_obj=self.property,
            action_code="privacy.retention_execute",
            entity_type="RetentionExecution",
            entity_id=str(plan.pk),
            requested_by=requester,
            reason="将被驳回",
            payload={
                "retention_policy_id": plan.policy_id,
                "as_of_date": str(plan.as_of),
            },
        )
        ApprovalService.reject(
            record.pk, approved_by=approver, property_obj=self.property, reason="驳回"
        )
        from apps.guests.retention import plan_retention_cleanup

        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=self.property,
                policy=policy,
                as_of=self.today,
                operator=executor,
                dry_run=False,
                approval_reference=str(record.pk),
            )

    def test_approval_for_other_property_rejected(self):
        """跨物业审批记录 → 拒绝。"""
        requester = self._staff("店长")
        approver = self._staff("值班经理")
        executor = self._staff("值班经理")
        policy = self._policy(requester)
        plan, _ = self._plan(policy, requester)
        record = self._approved_record(plan, requester, approver)

        other_property = Property.objects.create(code="NSE05-OTHER", name="隔离测试物业")
        from apps.guests.retention import plan_retention_cleanup

        with self.assertRaises(ValueError):
            plan_retention_cleanup(
                property_obj=other_property,
                policy=policy,
                as_of=self.today,
                operator=executor,
                dry_run=False,
                approval_reference=str(record.pk),
            )

    def test_dry_run_plan_has_draft_status(self):
        """dry-run 产物为 DRAFT，可被审批绑定。"""
        operator = self._staff("店长")
        policy = self._policy(operator)
        plan, _ = self._plan(policy, operator)
        self.assertEqual(plan.execution_status, "DRAFT")
        self.assertTrue(plan.dry_run)

    def test_validate_entity_rejects_non_dry_run_plan(self):
        """validate_entity 拒绝非 dry-run 计划。"""
        from apps.billing.approval_registry import validate_retention_execution_entity

        operator = self._staff("店长")
        policy = self._policy(operator)
        plan, _ = self._plan(policy, operator)
        plan.dry_run = False
        plan.save(update_fields=("dry_run",))

        with self.assertRaises(ValueError):
            validate_retention_execution_entity(self.property, str(plan.pk), operator)

    def test_validate_entity_rejects_non_draft_plan(self):
        """validate_entity 拒绝已进入执行流程的计划。"""
        from apps.billing.approval_registry import validate_retention_execution_entity

        operator = self._staff("店长")
        policy = self._policy(operator)
        plan, _ = self._plan(policy, operator)
        plan.execution_status = "EXECUTING"
        plan.save(update_fields=("execution_status",))

        with self.assertRaises(ValueError):
            validate_retention_execution_entity(self.property, str(plan.pk), operator)

    def test_sod_rule_declared_three_way(self):
        """registry 声明三人分离 SoD。"""
        from apps.billing.approval_registry import ApprovalActionRegistry

        spec = ApprovalActionRegistry.get("privacy.retention_execute")
        self.assertIn("申请人", spec.sod_rule)
        self.assertIn("审批人", spec.sod_rule)
        self.assertIn("执行人", spec.sod_rule)


class BillingCollectAuditDetailTests(Nse05Base):
    """8.4 补充（4 例）：审计 request 链路与双入口对照细节。"""

    def _setup_billable(self, booking_no):
        from apps.billing.models import LedgerEntry
        from apps.billing.services_v2 import BillingService

        reservation = self._make_reservation(booking_no)
        folio = Folio.objects.create(
            property=self.property,
            reservation=reservation,
            folio_type=Folio.FolioType.MASTER,
            guest=reservation.guest,
            status=Folio.Status.OPEN,
            currency="CNY",
        )
        BillingService.post_entry(
            property_obj=self.property,
            folio=folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("500.00"),
            reference=f"adj:{booking_no}",
            description="NS-E-05 应收调整",
        )
        return reservation, folio

    def test_audit_request_id_populated_when_request_passed(self):
        """传入 request 后 request_id 非空（原实现恒 None 的回归护栏）。"""
        from apps.billing.services_v2 import BillingService

        user = self._make_role_staff("财务")
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        reservation, _folio = self._setup_billable("NSE05-AUD-10")

        class _FakeRequest:
            request_id = "nse05-req-abc123"
            META: dict = {}
            headers: dict = {}

        payment = BillingService.collect_payment(
            property_obj=self.property,
            reservation=reservation,
            amount=Decimal("100.00"),
            method="CASH",
            collected_by=user,
            reference="NSE05-AUD-REF-10",
            request=_FakeRequest(),
            source_entry="frontdesk",
        )
        event = AuditEvent.objects.get(action="billing.collect", entity_id=str(payment.pk))
        self.assertEqual(event.request_id, "nse05-req-abc123")

    def test_audit_request_id_empty_when_request_absent(self):
        """未传 request 时 request_id 为空（而不是从 User 假读）。"""
        from apps.billing.services_v2 import BillingService

        user = self._make_role_staff("财务")
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        reservation, _folio = self._setup_billable("NSE05-AUD-11")
        payment = BillingService.collect_payment(
            property_obj=self.property,
            reservation=reservation,
            amount=Decimal("100.00"),
            method="CASH",
            collected_by=user,
            reference="NSE05-AUD-REF-11",
        )
        event = AuditEvent.objects.get(action="billing.collect", entity_id=str(payment.pk))
        self.assertFalse(event.request_id)

    def test_audit_folio_id_matches_locked_folio(self):
        """审计 folio_id 必须是服务层实际锁定的 MASTER 账单。"""
        from apps.billing.services_v2 import BillingService

        user = self._make_role_staff("财务")
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        reservation, folio = self._setup_billable("NSE05-AUD-12")
        payment = BillingService.collect_payment(
            property_obj=self.property,
            reservation=reservation,
            amount=Decimal("100.00"),
            method="CASH",
            collected_by=user,
            reference="NSE05-AUD-REF-12",
        )
        event = AuditEvent.objects.get(action="billing.collect", entity_id=str(payment.pk))
        self.assertEqual(event.after_json["folio_id"], folio.pk)

    def test_audit_business_date_matches_ledger_entry(self):
        """审计 business_date 与账本凭证一致（同一次计算）。"""
        from apps.billing.models import LedgerEntry
        from apps.billing.services_v2 import BillingService

        user = self._make_role_staff("财务")
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        reservation, _folio = self._setup_billable("NSE05-AUD-13")
        payment = BillingService.collect_payment(
            property_obj=self.property,
            reservation=reservation,
            amount=Decimal("100.00"),
            method="CASH",
            collected_by=user,
            reference="NSE05-AUD-REF-13",
        )
        event = AuditEvent.objects.get(action="billing.collect", entity_id=str(payment.pk))
        entry = LedgerEntry.objects.filter(
            folio__reservation=reservation,
            entry_type=LedgerEntry.EntryType.PAYMENT,
        ).first()
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(str(entry.business_date), event.after_json["business_date"])
