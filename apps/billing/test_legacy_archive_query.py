"""归档只读查询通道的 TDD（归档门禁 §4.1：权限、脱敏、访问审计、物业隔离）。

覆盖：
* 脱敏：≥8 位数字取前 3 后 4，不足 8 位一律「已隐藏」（不还原短号）；
* 批次清单：非超管仅见所属物业的已登记批次；未登记来源批次仅超管可见；
* 行查询：只读 + 电话掩码 + 每次访问写 ``billing.legacy_archive_query`` 审计；
* 拒绝路径：未知表键、未归档表、跨物业越权（AccessDenied）；
* API 四态：匿名 401 / 无任职 403 / 无 finance_access 403 / 财务 200；跨物业 404。
"""

from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import Group, Permission, User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.legacy_archive import ARCHIVE_SPECS, archive_table, get_spec
from apps.billing.legacy_archive_query import (
    LegacyArchiveAccessDenied,
    LegacyArchiveQueryError,
    authorize_batch,
    list_batches,
    list_rows,
    mask_phone,
    resolve_spec,
)
from apps.billing.models import LegacyMigrationBatch
from apps.core.models import AuditEvent, Property
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment


def _make_legacy_fixtures():
    """旧表夹具：四条流水/订单/夜审各一行，供四表归档。"""
    from hotelapp.models import (
        Accommodation,
        Client,
        FinanceRecord,
        NightAudit,
        Room,
        Staff,
    )
    from hotelapp.models import (
        Folio as LegacyFolio,
    )

    staff = Staff.objects.create(姓名="归档查询员工", 职务="服务员", 月薪="3000", 工龄="1")
    room = Room.objects.create(
        房间号=9601, 客房类型="标准间", 价格=300, 负责人编号=staff, 房间状态="空闲"
    )
    client = Client.objects.create(姓名="归档查询顾客", 身份证号="110101199003017777")
    order = Accommodation.objects.create(
        id=8701,
        顾客id=client,
        客房编号=room,
        状态="已退房",
        入住时间="2026-02-01",
        联系电话="13912345678",
        收款金额="688.00",
        押金="206.40",
    )
    LegacyFolio.objects.create(
        id=9201, order=order, 类型="房费", 金额="688.00", 事由="旧房费", 日期="2026-02-01"
    )
    FinanceRecord.objects.create(
        id=7201, 类型="收入", 金额="688.00", 事由="旧收入", 关联订单="8701", 日期="2026-02-01"
    )
    NightAudit.objects.create(
        id=6201,
        营业日期="2026-02-01",
        在住单数=1,
        过租总额="688.00",
        未到处理数=0,
        操作人="旧夜审员",
        执行时间="2026-02-02 01:00:00",
    )
    return order


def _make_staff(prop, username, *, finance_access=True, superuser=False):
    if superuser:
        return User.objects.create_superuser(username=username, password="x", email="")
    group, _ = Group.objects.get_or_create(name=f"ARC-{username}")
    wanted = {"finance_access"} if finance_access else set()
    if wanted:
        group.permissions.add(
            *Permission.objects.filter(
                codename__in=wanted, content_type__app_label__in=("hotelapp", "billing")
            )
        )
    user = User.objects.create_user(username=username, password="x")
    profile = StaffProfile.objects.create(
        user=user, full_name=username, employment_status=StaffProfile.EmploymentStatus.ACTIVE
    )
    membership = PropertyStaffMembership.objects.create(
        staff_profile=profile,
        property=prop,
        employee_no=f"ARC-{user.pk:06d}",
        status=PropertyStaffMembership.Status.ACTIVE,
    )
    StaffRoleAssignment.objects.create(
        membership=membership, group=group, starts_at=timezone.now() - timedelta(minutes=1)
    )
    return user


@override_settings(DEBUG=True, DJANGO_ENV="test")
class MaskPhoneTests(TestCase):
    """脱敏纯函数：短号不得被 3+4 掩码还原成明文。"""

    def test_long_number_masked(self):
        self.assertEqual(mask_phone("13912345678"), "139****5678")

    def test_short_number_hidden(self):
        self.assertEqual(mask_phone("1234567"), "已隐藏")
        self.assertEqual(mask_phone("123"), "已隐藏")

    def test_empty_passthrough(self):
        self.assertIsNone(mask_phone(None))
        self.assertEqual(mask_phone(""), "")

    def test_non_digit_chars_ignored(self):
        self.assertEqual(mask_phone("139-1234-5678"), "139****5678")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class LegacyArchiveQueryTests(TestCase):
    """归档查询服务：批次清单、行查询、审计、权限与隔离。"""

    prop: Property
    other_prop: Property
    finance: User
    outsider: User
    admin: User

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.other_prop = Property.objects.create(code="ARC-OTHER", name="归档查询他物业")
        cls.finance = _make_staff(cls.prop, "arc-finance")
        cls.outsider = _make_staff(cls.prop, "arc-outsider", finance_access=False)
        cls.admin = _make_staff(cls.prop, "arc-admin", superuser=True)
        _make_legacy_fixtures()
        executor = User.objects.create_user(username="arc-ops", password="x")
        LegacyMigrationBatch.objects.create(
            batch_uid="ARC-Q-1",
            scope_property=cls.prop,
            scope_source_models=["Folio", "FinanceRecord"],
            scope_record_range={"pk_min": 1, "pk_max": 999999},
            executed_by=executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )
        source_pks = {
            "Folio": "9201",
            "FinanceRecord": "7201",
            "NightAudit": "6201",
            "Accommodation": "8701",
        }
        for spec in ARCHIVE_SPECS:
            archive_table(
                spec=spec,
                batch_uid="ARC-Q-1",
                operator="归档执行人",
                property_assignments={source_pks[spec.source_model]: cls.prop},
            )

    # --- 批次清单 ---------------------------------------------------------
    def test_list_batches_scoped_by_property(self):
        items = list_batches(property_obj=self.prop, is_superuser=False)
        self.assertEqual(len(items), 4)
        self.assertEqual({item["property_code"] for item in items}, {"DEFAULT"})

    def test_list_batches_excludes_other_property(self):
        items = list_batches(property_obj=self.other_prop, is_superuser=False)
        self.assertEqual(items, [])

    def test_list_batches_includes_all_for_superuser(self):
        items = list_batches(property_obj=self.other_prop, is_superuser=True)
        self.assertEqual(len(items), 4)

    def test_list_batches_requires_property_context_for_non_superuser(self):
        with self.assertRaises(LegacyArchiveAccessDenied):
            list_batches(property_obj=None, is_superuser=False)

    def test_unregistered_batch_only_visible_to_superuser(self):
        """找不到登记批次的归档批次 = 来源未登记，非超管不可见。"""
        from apps.billing.models import LegacyArchiveRun

        run = LegacyArchiveRun.objects.get(batch_uid="ARC-Q-1", source_table="folio")
        LegacyArchiveRun.objects.create(
            batch_uid="ARC-ORPHAN",
            source_table="folio",
            archive_table="legacy_folio_archive",
            algorithm_version=run.algorithm_version,
            row_count=0,
            table_digest=run.table_digest,
            computed_at=run.computed_at,
            operator="未登记来源",
        )
        items = list_batches(property_obj=self.prop, is_superuser=False)
        self.assertNotIn("ARC-ORPHAN", {item["batch_uid"] for item in items})
        with self.assertRaises(LegacyArchiveAccessDenied):
            authorize_batch(batch_uid="ARC-ORPHAN", actor=self.finance, is_superuser=False)
        self.assertIsNone(
            authorize_batch(batch_uid="ARC-ORPHAN", actor=self.admin, is_superuser=True)
        )

    # --- 行查询 -----------------------------------------------------------
    def test_list_rows_requires_matching_property_context(self):
        with self.assertRaises(LegacyArchiveAccessDenied):
            list_rows(
                batch_uid="ARC-Q-1",
                table_key="folios",
                actor=self.finance,
                property_obj=None,
            )

    def test_list_rows_masks_phone_and_writes_audit(self):
        rows, pagination = list_rows(
            batch_uid="ARC-Q-1",
            table_key="accommodations",
            actor=self.finance,
            property_obj=self.prop,
        )
        self.assertEqual(pagination["total"], 1)
        self.assertEqual(rows[0]["contact_phone"], "139****5678")
        self.assertNotIn("13912345678", str(rows))
        self.assertTrue(AuditEvent.objects.filter(action="billing.legacy_archive_query").exists())

    def test_unattributed_rows_are_hidden_from_finance_but_visible_to_superuser(self):
        from apps.billing.models import LegacyAccommodationArchive

        LegacyAccommodationArchive.objects.create(
            id=8702,
            client_id=None,
            room_id=None,
            check_in_text="2026-02-02",
            status="已退房",
            contact_phone="13900001111",
            received_amount_text="0.00",
            deposit_text="0.00",
            check_out_text="2026-02-03",
            reversed_by=None,
            reversed_at_text=None,
            archive_batch="ARC-Q-1",
            verified_checksum="0" * 64,
            property=None,
        )
        finance_rows, finance_pagination = list_rows(
            batch_uid="ARC-Q-1",
            table_key="accommodations",
            actor=self.finance,
            property_obj=self.prop,
        )
        admin_rows, admin_pagination = list_rows(
            batch_uid="ARC-Q-1",
            table_key="accommodations",
            actor=self.admin,
            property_obj=None,
            is_superuser=True,
        )
        self.assertEqual(finance_pagination["total"], 1)
        self.assertEqual(len(finance_rows), 1)
        self.assertEqual(admin_pagination["total"], 2)
        self.assertEqual(len(admin_rows), 2)

    def test_list_rows_folio_content(self):
        rows, _ = list_rows(
            batch_uid="ARC-Q-1",
            table_key="folios",
            actor=self.finance,
            property_obj=self.prop,
        )
        self.assertEqual(rows[0]["amount_text"], "688.00")
        self.assertEqual(rows[0]["entry_type"], "房费")

    def test_list_rows_pk_range_filter(self):
        rows, pagination = list_rows(
            batch_uid="ARC-Q-1",
            table_key="folios",
            actor=self.finance,
            property_obj=self.prop,
            pk_min=9202,
        )
        self.assertEqual(rows, [])
        self.assertEqual(pagination["total"], 0)

    def test_unknown_table_key_rejected(self):
        with self.assertRaises(LegacyArchiveQueryError):
            resolve_spec("not-a-table")

    def test_unauthorized_batch_rejected(self):
        with self.assertRaises(LegacyArchiveAccessDenied):
            authorize_batch(batch_uid="ARC-Q-1", actor=self.outsider, is_superuser=False)

    def test_missing_batch_rejected(self):
        with self.assertRaises(LegacyArchiveQueryError):
            authorize_batch(batch_uid="ARC-NOPE", actor=self.finance, is_superuser=False)

    def test_table_without_archive_run_rejected(self):
        """批次存在但该表未归档 → 明确报错，不返回空表冒充成功。

        归档表主键沿用源表主键，同一源表跨批次重复归档会撞主键，故这里不重跑归档，
        只登记 folio 一张表的 run 元数据，构造"批次存在、finance_record 未归档"。
        """
        from apps.billing.models import LegacyArchiveRun

        LegacyMigrationBatch.objects.create(
            batch_uid="ARC-PARTIAL",
            scope_property=self.prop,
            scope_source_models=["Folio"],
            scope_record_range={"pk_min": 1, "pk_max": 999999},
            executed_by=User.objects.create_user(username="arc-partial-ops", password="x"),
            status=LegacyMigrationBatch.Status.ACTIVE,
        )
        template = LegacyArchiveRun.objects.get(batch_uid="ARC-Q-1", source_table="folio")
        LegacyArchiveRun.objects.create(
            batch_uid="ARC-PARTIAL",
            source_table="folio",
            archive_table="legacy_folio_archive",
            algorithm_version=template.algorithm_version,
            row_count=template.row_count,
            table_digest=template.table_digest,
            computed_at=template.computed_at,
            operator="部分归档",
        )
        with self.assertRaises(LegacyArchiveQueryError):
            list_rows(
                batch_uid="ARC-PARTIAL",
                table_key="finance-records",
                actor=self.finance,
                property_obj=self.prop,
            )


@override_settings(DEBUG=True, DJANGO_ENV="test")
class LegacyArchiveApiTests(TestCase):
    """API 通道四态权限 + 脱敏 + 跨物业 404。"""

    prop: Property
    other_prop: Property
    finance: User
    outsider: User
    admin: User

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.other_prop = Property.objects.create(code="ARC-API-OTHER", name="归档接口他物业")
        cls.finance = _make_staff(cls.prop, "arc-api-finance")
        cls.outsider = _make_staff(cls.prop, "arc-api-outsider", finance_access=False)
        cls.admin = _make_staff(cls.prop, "arc-api-admin", superuser=True)
        _make_legacy_fixtures()
        executor = User.objects.create_user(username="arc-api-ops", password="x")
        LegacyMigrationBatch.objects.create(
            batch_uid="ARC-API-1",
            scope_property=cls.prop,
            scope_source_models=["Folio"],
            scope_record_range={"pk_min": 1, "pk_max": 999999},
            executed_by=executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )
        for spec in ARCHIVE_SPECS:
            archive_table(spec=spec, batch_uid="ARC-API-1", operator="归档执行人")

    def test_batches_require_authentication(self):
        response = self.client.get("/api/v1/billing/legacy-archives")
        self.assertEqual(response.status_code, 401)

    def test_batches_denied_without_finance_access(self):
        self.client.force_login(self.outsider)
        response = self.client.get("/api/v1/billing/legacy-archives")
        self.assertEqual(response.status_code, 403)

    def test_batches_ok_for_finance(self):
        self.client.force_login(self.finance)
        response = self.client.get("/api/v1/billing/legacy-archives")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(len(payload), 4)
        self.assertTrue(all(item["property_code"] == "DEFAULT" for item in payload))

    def test_batches_ok_for_superuser(self):
        self.client.force_login(self.admin)
        response = self.client.get("/api/v1/billing/legacy-archives")
        self.assertEqual(response.status_code, 200)

    def test_rows_masked_and_audited(self):
        self.client.force_login(self.finance)
        response = self.client.get(
            "/api/v1/billing/legacy-archives/accommodations",
            {"batch_uid": "ARC-API-1"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"][0]["contact_phone"], "139****5678")
        self.assertNotIn("13912345678", response.content.decode("utf-8"))
        self.assertEqual(body["meta"]["pagination"]["total"], 1)
        self.assertTrue(AuditEvent.objects.filter(action="billing.legacy_archive_query").exists())

    def test_rows_require_batch_uid(self):
        self.client.force_login(self.finance)
        response = self.client.get("/api/v1/billing/legacy-archives/folios")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_rows_unknown_table_key_is_400(self):
        self.client.force_login(self.finance)
        response = self.client.get(
            "/api/v1/billing/legacy-archives/unknown-table", {"batch_uid": "ARC-API-1"}
        )
        self.assertEqual(response.status_code, 400)

    def test_rows_invalid_pk_bound_is_400(self):
        self.client.force_login(self.finance)
        response = self.client.get(
            "/api/v1/billing/legacy-archives/folios",
            {"batch_uid": "ARC-API-1", "pk_min": "abc"},
        )
        self.assertEqual(response.status_code, 400)

    def test_rows_cross_property_is_404(self):
        """他物业批次：财务账号看不到，且不泄露存在性（404 而非 403）。"""
        other_executor = User.objects.create_user(username="arc-api-other-ops", password="x")
        LegacyMigrationBatch.objects.create(
            batch_uid="ARC-API-OTHER-1",
            scope_property=self.other_prop,
            scope_source_models=["Folio"],
            scope_record_range={"pk_min": 1, "pk_max": 999999},
            executed_by=other_executor,
            status=LegacyMigrationBatch.Status.ACTIVE,
        )
        from apps.billing.models import LegacyArchiveRun

        LegacyArchiveRun.objects.create(
            batch_uid="ARC-API-OTHER-1",
            source_table="folio",
            archive_table="legacy_folio_archive",
            algorithm_version="checksum-v2",
            row_count=0,
            table_digest="x" * 64,
            computed_at=timezone.now(),
            operator="他物业",
        )
        self.client.force_login(self.finance)
        response = self.client.get(
            "/api/v1/billing/legacy-archives/folios", {"batch_uid": "ARC-API-OTHER-1"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_guard_commands_refuse_sqlite(self):
        from django.core.management import CommandError
        from django.db import connection

        if connection.vendor != "sqlite":
            self.skipTest("仅验证 SQLite 下 fail-closed")
        with self.assertRaises(CommandError):
            call_command("install_legacy_map_guards", stdout=StringIO())
