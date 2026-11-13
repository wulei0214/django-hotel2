"""A3：Guest 重复档案治理回归测试。

覆盖《PMS2 身份认证与员工、住客账户体系改造开发方案 2.0》§8.2 与 §18.1：

  * 只读候选报告（§8.2-7 允许的形态）：共享号码 / 同名 / 证件哈希冲突
    三类证据信号被正确识别；
  * **报告不得夹带业务策略**：不出现置信度评分、不设自动阈值、绝不自动合并；
  * 报告与盘点**不写库**（不产生审计、不改档案）；
  * 影响面盘点（§8.2-5）：Reservation / Folio / LedgerEntry 等引用面逐类
    统计正确，冲突事实如实列出，且 executable 恒为 False；
  * 人工建立重复档案关联（§8.2-3 本期固定执行）：理由必填、权限校验、
    映射留痕（源/目标/操作者/时间/理由/信号），**不移动业务引用、
    不物理删除源档案**；
  * 物理合并入口显式冻结（§8.2-7 / §18.1）：execute_merge 恒定拒绝。
"""

import json
from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry
from apps.core.models import AuditEvent, Property
from apps.guests.merge_services import (
    ACTION_DISMISSED,
    ACTION_LINKED,
    FROZEN_REASON,
    SIGNAL_DOCUMENT_HASH,
    SIGNAL_SAME_NAME,
    SIGNAL_SHARED_PHONE,
    dismiss_duplicate_candidate,
    execute_merge,
    find_duplicate_candidates,
    link_duplicate_guests,
    list_duplicate_links,
    preview_merge_impact,
)
from apps.guests.models import Guest, GuestDocument, GuestMembership, GuestPhone
from apps.guests.services import GuestService, GuestServiceError
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.reservations.models import Reservation


class _DuplicateGovernanceBase(TestCase):
    """自建物业与账号，不依赖 DEFAULT 物业数据。"""

    def setUp(self):
        self.property = Property.objects.create(code="DUPG", name="重复档案治理测试酒店")
        self.other_property = Property.objects.create(code="DUPG2", name="他店")
        self.operator = self._make_staff("dupg-operator", with_write=True)

    def _make_staff(self, username, *, with_write):
        user = User.objects.create_user(username=username)
        profile = StaffProfile.objects.create(
            user=user, full_name=username, employment_status="ACTIVE"
        )
        membership = PropertyStaffMembership.objects.create(
            property=self.property,
            staff_profile=profile,
            employee_no=f"DUPG-{username}",
            status="ACTIVE",
        )
        group = Group.objects.create(name=f"DUPG-{username}-group")
        codenames = ["change_client"] if with_write else []
        group.permissions.add(
            *Permission.objects.filter(content_type__app_label="hotelapp", codename__in=codenames)
        )
        StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=timezone.now()
        )
        return user

    def _guest(self, full_name, phone=""):
        return GuestService.create_guest(
            property_obj=self.property, full_name=full_name, phone=phone
        )


class CandidateReportTests(_DuplicateGovernanceBase):
    """只读候选报告（§8.2-7）。"""

    def test_shared_phone_hash_pair_is_reported(self):
        first = self._guest("共享一号", "13800001111")
        second = self._guest("共享二号", "13800001111")
        report = find_duplicate_candidates(self.property)
        pairs = {(c["source_guest_id"], c["target_guest_id"]) for c in report["candidates"]}
        self.assertIn((first.pk, second.pk), pairs)
        candidate = next(c for c in report["candidates"] if c["source_guest_id"] == first.pk)
        self.assertIn(SIGNAL_SHARED_PHONE, candidate["signals"])
        self.assertTrue(candidate["shared_phone_review_required"])
        self.assertFalse(candidate["auto_merge"])

    def test_same_name_pair_is_reported(self):
        first = self._guest("同名客人", "13800002222")
        second = self._guest("同名客人", "13800003333")
        report = find_duplicate_candidates(self.property)
        candidate = next(
            c
            for c in report["candidates"]
            if {c["source_guest_id"], c["target_guest_id"]} == {first.pk, second.pk}
        )
        self.assertIn(SIGNAL_SAME_NAME, candidate["signals"])

    def test_document_hash_conflict_is_reported(self):
        """证件哈希冲突通常被 WB-102 占用索引拦截；此用例覆盖历史遗留数据。"""
        first = self._guest("证件冲突一", "13800004444")
        second = self._guest("证件冲突二", "13800005555")
        digest = "a" * 64
        GuestDocument.objects.create(
            guest=first,
            doc_type=GuestDocument.DocType.ID_CARD,
            doc_no_ciphertext="cipher-a",
            doc_no_hash=digest,
        )
        GuestDocument.objects.create(
            guest=second,
            doc_type=GuestDocument.DocType.ID_CARD,
            doc_no_ciphertext="cipher-b",
            doc_no_hash=digest,
        )
        report = find_duplicate_candidates(self.property)
        candidate = next(
            c
            for c in report["candidates"]
            if {c["source_guest_id"], c["target_guest_id"]} == {first.pk, second.pk}
        )
        self.assertIn(SIGNAL_DOCUMENT_HASH, candidate["signals"])

    def test_report_never_scores_confidence_or_auto_merges(self):
        """匹配置信度属业务待确认项：报告不得夹带评分或自动结论。"""
        # 两个共用同一号码的档案（弱/共享信号），报告不得据此给出评分
        self._guest("评分一号", "13800006666")
        self._guest("评分二号", "13800006666")
        report = find_duplicate_candidates(self.property)
        self.assertFalse(report["policy"]["auto_merge"])
        self.assertFalse(report["policy"]["confidence_scored"])
        self.assertTrue(report["policy"]["approval_required"])
        for candidate in report["candidates"]:
            for key in candidate:
                self.assertNotIn("confidence", key.lower())
                self.assertNotIn("score", key.lower())
        # 报告本身不含任何**带值的**置信度字段（policy 段的
        # confidence_scored=False 是「明确不评分」的声明，不是评分）
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotRegex(serialized, r'"confidence"\s*:')
        self.assertNotRegex(serialized, r'"score"\s*:')
        self.assertNotRegex(serialized, r'"similarity"\s*:')

    def test_report_does_not_write_anything(self):
        self._guest("只读一号", "13800007777")
        self._guest("只读二号", "13800007777")
        guests_before = Guest.objects.count()
        audit_before = AuditEvent.objects.count()
        find_duplicate_candidates(self.property)
        self.assertEqual(Guest.objects.count(), guests_before)
        self.assertEqual(AuditEvent.objects.count(), audit_before)

    def test_report_isolates_other_property(self):
        other = GuestService.create_guest(
            property_obj=self.other_property, full_name="他店同名", phone="13800008888"
        )
        mine = self._guest("本店同名", "13800008888")
        report = find_duplicate_candidates(self.property)
        for candidate in report["candidates"]:
            self.assertNotIn(other.pk, (candidate["source_guest_id"], candidate["target_guest_id"]))
            self.assertNotIn(mine.pk, (candidate["source_guest_id"], candidate["target_guest_id"]))


class ImpactPreviewTests(_DuplicateGovernanceBase):
    """只读影响面盘点（§8.2-5）。"""

    def setUp(self):
        super().setUp()
        self.source = self._guest("源档案", "13800009999")
        self.target = self._guest("目标档案", "13800008888")
        reservation = Reservation.objects.create(
            property=self.property,
            booking_no="DUPG-RSV-1",
            guest=self.source,
            check_in=timezone.now().date(),
            check_out=timezone.now().date() + timedelta(days=1),
        )
        folio = Folio.objects.create(
            property=self.property, reservation=reservation, guest=self.source
        )
        LedgerEntry.objects.create(
            property=self.property,
            folio=folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount="100.00",
            currency="CNY",
            business_date=timezone.now().date(),
            reference="DUPG-LE-1",
        )

    def test_reference_inventory_counts_business_facts(self):
        preview = preview_merge_impact(
            self.property, source_guest=self.source, target_guest=self.target
        )
        refs = preview["source"]["references"]
        self.assertEqual(refs["reservations"], 1)
        self.assertEqual(refs["folios"], 1)
        self.assertEqual(refs["ledger_entries"], 1)
        self.assertEqual(refs["open_folios"], 1)
        self.assertEqual(refs["phones"], 1)
        self.assertEqual(preview["target"]["references"]["reservations"], 0)

    def test_conflicts_are_reported_as_facts(self):
        GuestMembership.objects.create(
            property=self.property,
            guest=self.source,
            member_no="DUPG-M-1",
            status=GuestMembership.Status.ACTIVE,
        )
        GuestMembership.objects.create(
            property=self.property,
            guest=self.target,
            member_no="DUPG-M-2",
            status=GuestMembership.Status.ACTIVE,
        )
        preview = preview_merge_impact(
            self.property, source_guest=self.source, target_guest=self.target
        )
        kinds = {c["kind"] for c in preview["conflicts"]}
        self.assertIn("BOTH_ACTIVE_MEMBERSHIP", kinds)
        self.assertIn("BOTH_PRIMARY_PHONE", kinds)
        self.assertIn("SOURCE_HAS_LEDGER_ENTRIES", kinds)
        self.assertIn("SOURCE_HAS_OPEN_FOLIO", kinds)

    def test_preview_is_never_executable(self):
        preview = preview_merge_impact(
            self.property, source_guest=self.source, target_guest=self.target
        )
        self.assertFalse(preview["executable"])
        self.assertTrue(preview["read_only"])
        self.assertIn("§8.2-7", preview["frozen_reason"])

    def test_preview_rejects_cross_property_pair(self):
        outsider = GuestService.create_guest(property_obj=self.other_property, full_name="他店客人")
        with self.assertRaises(GuestServiceError):
            preview_merge_impact(self.property, source_guest=self.source, target_guest=outsider)

    def test_preview_does_not_write_anything(self):
        audit_before = AuditEvent.objects.count()
        preview_merge_impact(self.property, source_guest=self.source, target_guest=self.target)
        self.assertEqual(AuditEvent.objects.count(), audit_before)


class ManualLinkTests(_DuplicateGovernanceBase):
    """人工建立重复档案关联（§8.2-3 本期固定执行）。"""

    def setUp(self):
        super().setUp()
        self.source = self._guest("待关联源", "13800010000")
        self.target = self._guest("待关联目标", "13800010000")

    def test_link_records_mapping_and_audit(self):
        result = link_duplicate_guests(
            property_obj=self.property,
            source_guest=self.source,
            target_guest=self.target,
            reason="前台核对：同一自然人重复建档",
            operator=self.operator,
        )
        self.assertFalse(result["merged"])
        self.assertFalse(result["data_moved"])
        event = AuditEvent.objects.get(action=ACTION_LINKED)
        self.assertEqual(event.entity_type, "Guest")
        self.assertEqual(event.entity_id, str(self.source.pk))
        self.assertEqual(event.reason, "前台核对：同一自然人重复建档")
        self.assertEqual(event.actor_id, self.operator.pk)
        self.assertEqual(event.after_json["source_guest_id"], self.source.pk)
        self.assertEqual(event.after_json["target_guest_id"], self.target.pk)
        self.assertFalse(event.after_json["data_moved"])
        self.assertFalse(event.after_json["source_deleted"])
        self.assertEqual(event.after_json["mapping"]["status"], "not_merged")

    def test_link_does_not_move_or_delete_source(self):
        reservation = Reservation.objects.create(
            property=self.property,
            booking_no="DUPG-RSV-2",
            guest=self.source,
            check_in=timezone.now().date(),
            check_out=timezone.now().date() + timedelta(days=1),
        )
        link_duplicate_guests(
            property_obj=self.property,
            source_guest=self.source,
            target_guest=self.target,
            reason="人工核对重复建档",
            operator=self.operator,
        )
        # 源档案与业务引用原封不动
        self.assertTrue(Guest.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).guest.pk, self.source.pk)
        self.assertEqual(GuestPhone.objects.filter(guest=self.source).count(), 1)
        self.assertEqual(Guest.objects.count(), 2)

    def test_link_requires_reason(self):
        for blank in ("", "   "):
            with self.assertRaises(GuestServiceError):
                link_duplicate_guests(
                    property_obj=self.property,
                    source_guest=self.source,
                    target_guest=self.target,
                    reason=blank,
                    operator=self.operator,
                )
        self.assertEqual(AuditEvent.objects.filter(action=ACTION_LINKED).count(), 0)

    def test_link_requires_permission(self):
        read_only = self._make_staff("dupg-readonly", with_write=False)
        with self.assertRaises(PermissionDenied):
            link_duplicate_guests(
                property_obj=self.property,
                source_guest=self.source,
                target_guest=self.target,
                reason="越权尝试",
                operator=read_only,
            )
        self.assertEqual(AuditEvent.objects.filter(action=ACTION_LINKED).count(), 0)

    def test_link_rejects_self_pair(self):
        with self.assertRaises(GuestServiceError):
            link_duplicate_guests(
                property_obj=self.property,
                source_guest=self.source,
                target_guest=self.source,
                reason="自关联",
                operator=self.operator,
            )

    def test_link_rejects_cross_property_pair(self):
        outsider = GuestService.create_guest(property_obj=self.other_property, full_name="他店档案")
        with self.assertRaises(GuestServiceError):
            link_duplicate_guests(
                property_obj=self.property,
                source_guest=self.source,
                target_guest=outsider,
                reason="跨物业",
                operator=self.operator,
            )

    def test_link_rejects_duplicate_link(self):
        link_duplicate_guests(
            property_obj=self.property,
            source_guest=self.source,
            target_guest=self.target,
            reason="首次关联",
            operator=self.operator,
        )
        with self.assertRaises(GuestServiceError):
            link_duplicate_guests(
                property_obj=self.property,
                source_guest=self.source,
                target_guest=self.target,
                reason="重复关联",
                operator=self.operator,
            )
        self.assertEqual(AuditEvent.objects.filter(action=ACTION_LINKED).count(), 1)

    def test_dismiss_records_negative_decision(self):
        result = dismiss_duplicate_candidate(
            property_obj=self.property,
            source_guest=self.source,
            target_guest=self.target,
            reason="核对后确认为同住家人，非同一人",
            operator=self.operator,
        )
        self.assertEqual(result["decision"], "not_same_person")
        event = AuditEvent.objects.get(action=ACTION_DISMISSED)
        self.assertEqual(event.after_json["decision"], "not_same_person")
        self.assertFalse(event.after_json["data_moved"])

    def test_list_links_returns_recorded_mapping(self):
        link_duplicate_guests(
            property_obj=self.property,
            source_guest=self.source,
            target_guest=self.target,
            reason="关联留痕",
            operator=self.operator,
        )
        links = list_duplicate_links(self.property)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["source_guest_id"], self.source.pk)
        self.assertEqual(links[0]["target_guest_id"], self.target.pk)
        self.assertFalse(links[0]["merged"])
        self.assertEqual(links[0]["operator_id"], self.operator.pk)


class MergeFreezeTests(_DuplicateGovernanceBase):
    """物理合并入口冻结（§8.2-7 / §18.1）。"""

    def test_execute_merge_always_raises(self):
        source = self._guest("冻结源", "13800011111")
        target = self._guest("冻结目标", "13800011111")
        with self.assertRaises(GuestServiceError) as ctx:
            execute_merge(
                property_obj=self.property,
                source_guest=source,
                target_guest=target,
                reason="尝试合并",
                operator=self.operator,
            )
        self.assertIn("冻结", str(ctx.exception))
        self.assertIn("§8.2-7", FROZEN_REASON)
        # 冻结入口不得产生任何数据变化
        self.assertEqual(Guest.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.filter(action="guest.duplicate_merged").count(), 0)
