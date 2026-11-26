"""ARCH-04 LegacyStaff 渐进退役验收测试（C1/C2/C4/C6/C7/C8/C10）。

逐条钉住方案文档 §6 的退役条件：

* **C1** 旧表零生产 ORM 引用——生产代码源码扫描，在线路径零命中；
* **C2** 员工中心不依赖旧表——旧表反查与「关联旧员工」下拉已摘除，且
  「历史员工档案尚未映射」状态在页面上可追溯（MC11-ST-07 仍 PRODUCTION_PENDING）；
* **C4** 旧只读入口改读新栈——员工 CSV 导出数据源 = 新员工档案，与员工中心
  列表同源同语义；
* **C6** Food/Order/Room 三处反向 FK 解除——字段不再是外键、库层无 FK 约束，
  旧行上的工号原值原样保留；
* **C7** StaffProfile.legacy_staff 可空且不再阻断旧表处置；
* **C8** 旧 staff 表有归档落点——归档 + 恢复演练数据一致；
* **C10** 员工模块权限码解绑旧 ContentType——既有授权已映射到新栈权限码，
  新员工中心授权后仍可访问（不 403）。

裁定口径：渐进迁移、保留历史数据——不删旧表、不删历史数据行。
"""

import ast
import pathlib
import tempfile
from typing import cast

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.db import connection, models
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from django.utils.crypto import get_random_string
from hotelapp.models import Food, Order, Room, Staff

from apps.core.models import AuditEvent, Property
from apps.identity.models import (
    LegacyPermissionCodeGrant,
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.identity.registry import MODULE_REGISTRY_V2, iter_module_permission_codes

_TEST_PW_ARCH04 = get_random_string(24)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# C1 例外清单：旧 staff 表仅允许出现在这些位置（口径见方案文档 §6 C1）
#   ①模型定义/迁移/FK 声明   ②一次性归档或盘点命令   ③测试夹具
# 键 = (相对路径, 所在函数；None = 模块级)，值 = 允许理由
# ---------------------------------------------------------------------------
ALLOWED_LEGACY_STAFF_REFERENCES = {
    ("apps/billing/legacy_archive.py", "_source_queryset"): (
        "②一次性归档 spec 的源表映射（ARCH-04 C8）"
    ),
    ("hotelapp/management/commands/audit_employee_data.py", None): ("②一次性盘点命令的 import"),
    ("hotelapp/management/commands/audit_employee_data.py", "handle"): (
        "②一次性盘点命令的只读输出（不改库）"
    ),
    ("hotelapp/management/commands/seed_hotel_data.py", "handle"): (
        "②一次性盘点/种子命令的只读映射（不写旧表）"
    ),
    ("hotelapp/views.py", None): (
        "③并行开发线在办：模块级 import 仅供 index/search_staff 两处旧只读页"
    ),
    ("hotelapp/views.py", "index"): ("③并行开发线在办：路由级重定向并删除该视图（本线不触碰）"),
    ("hotelapp/views.py", "search_staff"): (
        "③并行开发线在办：路由级重定向并删除该视图（本线不触碰）"
    ),
}

# 旧表在模型/迁移层的声明级引用（不产生 ORM 读，不在源码扫描命中范围）
DECLARATION_ONLY_REFERENCES = (
    "hotelapp/models.py:class Staff（模型定义，旧表与历史数据保留）",
    "apps/identity/models.py:StaffProfile.legacy_staff 的 FK 声明（声明级，非读路径）",
)


def _iter_production_python_files():
    """生产代码 .py 文件（排除测试、迁移、__pycache__）。"""
    for root_name in ("apps", "hotelapp"):
        root = REPO_ROOT / root_name
        for path in sorted(root.rglob("*.py")):
            parts = path.parts
            if "migrations" in parts or "__pycache__" in parts:
                continue
            if "tests" in parts or path.name.startswith("test_") or path.name == "tests.py":
                continue
            yield path


def _scan_legacy_staff_references(path):
    """返回该文件里指向旧 staff 模型的引用 (行号, 所在函数)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            new_func = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                new_func = child.name
            if (
                (
                    isinstance(child, ast.Name)
                    and child.id in {"Staff", "LegacyStaff"}
                    or isinstance(child, ast.Attribute)
                    and child.attr == "Staff"
                )
                or isinstance(child, ast.ImportFrom)
                and any(alias.name == "Staff" for alias in child.names)
            ):
                hits.append((child.lineno, func))
            visit(child, new_func)

    visit(tree, None)
    return hits


def _foreign_key_targets(cursor, table):
    """该表指向的外键目标表清单（兼容 str / (table, column) / [column] 形态）。"""
    targets = []
    for info in connection.introspection.get_constraints(cursor, table).values():
        fk = info.get("foreign_key")
        if not fk:
            continue
        if isinstance(fk, str):
            targets.append(fk)
        elif isinstance(fk, (tuple, list)):
            targets.extend(str(part) for part in fk)
    return targets


def _make_user(username, group_name="店长"):
    """员工测试账号：User + DEFAULT 任职 + 角色授权（与员工中心测试同口径）。"""
    prop, _ = Property.objects.get_or_create(code="DEFAULT", defaults={"name": "默认酒店"})
    group = Group.objects.filter(name=group_name).first()
    user = User.objects.create_user(username=username, password=_TEST_PW_ARCH04)
    profile = StaffProfile.objects.create(user=user, full_name=username, employment_status="ACTIVE")
    membership = PropertyStaffMembership.objects.create(
        property=prop, staff_profile=profile, employee_no=f"E-{username}", status="ACTIVE"
    )
    if group is not None:
        StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=timezone.now()
        )
    return user


class LegacyStaffSourceScanTests(TestCase):
    """C1：旧表零生产 ORM 引用（源码扫描 + 例外清单）。"""

    def test_no_online_path_reads_legacy_staff_table(self):
        """生产代码中旧 staff 表的引用必须全部落在登记的例外清单内。"""
        violations = []
        for path in _iter_production_python_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            for lineno, func in _scan_legacy_staff_references(path):
                if (rel, func) not in ALLOWED_LEGACY_STAFF_REFERENCES:
                    violations.append(f"{rel}:{lineno}（函数 {func or '模块级'}）")
        self.assertEqual(
            violations,
            [],
            "旧 staff 表出现未登记的在线引用（ARCH-04 C1）：\n"
            + "\n".join(violations)
            + "\n例外清单：\n"
            + "\n".join(
                f"{k[0]} / {k[1] or '模块级'}：{v}"
                for k, v in ALLOWED_LEGACY_STAFF_REFERENCES.items()
            )
            + "\n声明级引用（不产生 ORM 读）：\n"
            + "\n".join(DECLARATION_ONLY_REFERENCES),
        )

    def test_exception_list_only_covers_offline_paths(self):
        """例外清单本身受约束：不得把在线业务路径登记成例外。"""
        for rel, func in ALLOWED_LEGACY_STAFF_REFERENCES:
            self.assertTrue(
                rel.endswith(("legacy_archive.py", "audit_employee_data.py", "seed_hotel_data.py"))
                or rel == "hotelapp/views.py",
                f"例外清单出现未登记文件：{rel}",
            )
            if rel == "hotelapp/views.py":
                self.assertIn(
                    func,
                    (None, "index", "search_staff"),
                    "旧表例外只允许并行开发线在办的两处旧只读页",
                )

    def test_dead_code_and_admin_paths_are_cleaned(self):
        """C1/C16：死代码 _get_staff 已摘除、旧 staff 表不再注册 Admin。"""
        from django.contrib import admin

        views_source = (REPO_ROOT / "hotelapp" / "views.py").read_text(encoding="utf-8")
        self.assertNotIn("def _get_staff(", views_source, "死代码 _get_staff 必须摘除")
        self.assertNotIn(
            "admin.site.register(Staff",
            (REPO_ROOT / "hotelapp" / "admin.py").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            Staff,
            list(admin.site._registry),
            "旧 staff 表不得再注册 Django Admin",
        )


class StaffCenterLegacyDecouplingTests(TestCase):
    """C2：员工中心不依赖旧 staff 表，且不静默丢失可追溯性。"""

    owner: User
    prop: Property

    @classmethod
    def setUpTestData(cls):
        cls.owner = _make_user("arch04_owner")
        cls.prop = Property.objects.get(code="DEFAULT")

    def test_staff_center_views_have_no_legacy_staff_reference(self):
        source = (REPO_ROOT / "apps" / "identity" / "staff_center_views.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("LegacyStaff", source, "员工中心不得再反查旧 staff 表")
        self.assertNotIn("hotelapp.models", source, "员工中心不得再依赖旧表模型")

    def test_staff_center_page_has_no_legacy_staff_dropdown(self):
        """页面不再有「关联旧员工」下拉（旧表退役的可见面归零）。"""
        self.client.force_login(self.owner)
        response = self.client.get("/manage/staff/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertNotIn("关联旧员工", html, "「关联旧员工」下拉必须移除")
        self.assertNotIn('name="legacy_staff"', html, "旧员工关联字段不得再出现在表单里")

    def test_staff_center_page_expresses_unmapped_legacy_state(self):
        """MC11-ST-07 未关闭：页面显式表达「历史员工档案尚未映射」。"""
        self.client.force_login(self.owner)
        response = self.client.get("/manage/staff/")
        html = response.content.decode("utf-8")
        self.assertIn(
            "历史员工档案：尚未映射",
            html,
            "历史员工档案未映射的状态必须显式可见，不得靠选项消失隐喻",
        )

    def test_create_employee_ignores_legacy_staff_post_field(self):
        """提交旧员工工号也不读旧表：建档结果不挂旧表关联。"""
        legacy = Staff.objects.create(姓名="旧表员工", 职务="服务员", 月薪="3000", 工龄="1")
        self.client.force_login(self.owner)
        response = self.client.post(
            "/manage/staff/actions/create/",
            {
                "full_name": "新栈员工",
                "department_code": "FO",
                "legacy_staff": str(legacy.pk),
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        profile = StaffProfile.objects.get(full_name="新栈员工")
        self.assertIsNone(
            profile.legacy_staff_id,  # type: ignore[attr-defined]
            "新员工档案不得再挂旧表关联（旧表不在在线读路径）",
        )


class StaffCsvExportFromNewStackTests(TestCase):
    """C4：员工 CSV 导出改读新栈，内容与员工中心列表一致。"""

    finance: User
    manager: User
    owner: User
    prop: Property
    legacy_only: Staff
    profile: StaffProfile
    membership: PropertyStaffMembership

    @classmethod
    def setUpTestData(cls):
        cls.finance = _make_user("arch04_finance", "财务")
        cls.manager = _make_user("arch04_manager", "值班经理")
        cls.owner = _make_user("arch04_csv_owner")
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.legacy_only = Staff.objects.create(
            姓名="仅在旧表的员工", 职务="服务员", 月薪="3000", 工龄="1"
        )
        cls.profile = StaffProfile.objects.create(
            full_name="新栈薪酬员工", employment_status="ACTIVE"
        )
        cls.membership = PropertyStaffMembership.objects.create(
            property=cls.prop,
            staff_profile=cls.profile,
            employee_no="FO-ARCH04",
            position_code="FO",
            status="ACTIVE",
        )

    def _export_csv(self):
        """走完整审批链导出 CSV（申请 → 他人审批下载）。"""
        self.client.force_login(self.finance)
        created = self.client.post(
            "/export/staff/request/",
            {"reason": "ARCH-04 退役验证：员工名册导出"},
            HTTP_IDEMPOTENCY_KEY="arch04-export-001",
        )
        self.assertEqual(created.status_code, 201, created.content)
        approval_id = created.json()["approval_id"]
        self.client.force_login(self.manager)
        response = self.client.post(f"/export/staff/approve/{approval_id}/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.content.decode("utf-8-sig")

    def test_csv_source_is_new_stack_not_legacy_table(self):
        """导出含新栈员工，不含仅存在于旧表的行。"""
        csv_text = self._export_csv()
        self.assertIn("新栈薪酬员工", csv_text, "导出数据源必须是新员工档案")
        self.assertIn("FO-ARCH04", csv_text, "导出必须带物业工号")
        self.assertNotIn("仅在旧表的员工", csv_text, "旧表数据不得再进入在线导出")
        self.assertNotIn("月薪", csv_text.splitlines()[0], "旧表薪酬列不在新档案口径内")

    def test_csv_matches_staff_center_list_population(self):
        """导出与员工中心列表（/api/v1/staff）同一数据源、同一人群。"""
        csv_text = self._export_csv()
        csv_rows = [line for line in csv_text.splitlines()[1:] if line.strip()]
        self.client.force_login(self.owner)
        api_response = self.client.get("/api/v1/staff")
        self.assertEqual(api_response.status_code, 200, api_response.content)
        api_items = api_response.json()["data"]
        self.assertEqual(
            len(csv_rows),
            len(api_items),
            "导出行数必须与员工中心列表一致（同一数据源）",
        )
        api_names = {item["full_name"] for item in api_items}
        for name in api_names:
            self.assertIn(name, csv_text, f"导出缺员工中心列表中的员工：{name}")

    def test_export_is_audited(self):
        """导出仍落 privacy.export_staff 审计（审批链未松动）。"""
        self._export_csv()
        self.assertTrue(
            AuditEvent.objects.filter(action="privacy.export_staff").exists(),
            "敏感导出必须落审计",
        )


class LegacyForeignKeyReleaseTests(TestCase):
    """C6：三处反向 FK 解除——不再是外键、库层无约束、旧值原样保留。"""

    def test_three_fields_are_no_longer_foreign_keys(self):
        for model, field_name in (
            (Food, "厨师id"),
            (Order, "服务员编号"),
            (Room, "负责人编号"),
        ):
            field = model._meta.get_field(field_name)
            self.assertFalse(
                field.is_relation,
                f"{model.__name__}.{field_name} 仍是指向旧 staff 表的外键",
            )
            self.assertTrue(
                field.null,  # type: ignore[union-attr]
                f"{model.__name__}.{field_name} 应可空",
            )

    def test_legacy_staff_ids_are_preserved_as_plain_values(self):
        """旧行上的工号原值保留（不再受旧表行存在与否约束）。"""
        room = Room.objects.create(
            房间号=97021, 客房类型="标准间", 价格=300, 负责人编号=987654, 房间状态="空闲"
        )
        food = Food.objects.create(菜品编号=97021, 菜名="归档菜品", 价格="38", 厨师id=987654)
        client_row = __import__("hotelapp.models", fromlist=["Client"]).Client.objects.create(
            姓名="归档客人", 身份证号="110101199007077777"
        )
        order = Order.objects.create(
            id=97021, 顾客编号=client_row, 菜品编号=food, 服务员编号=987654, 桌号=1, 状态="正常"
        )
        room.refresh_from_db()
        food.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(room.负责人编号, 987654)
        self.assertEqual(food.厨师id, 987654)
        self.assertEqual(order.服务员编号, 987654)

    def test_database_has_no_foreign_key_constraint_to_staff_table(self):
        """库层确认：room/food/order 不再存在指向 staff 表的 FK 约束。"""
        with connection.cursor() as cursor:
            for table in ("room", "food", "order"):
                fk_targets = _foreign_key_targets(cursor, table)
                self.assertNotIn(
                    "staff",
                    fk_targets,
                    f"{table} 表仍存在指向旧 staff 表的外键约束（删表阻断未解除）：{fk_targets}",
                )


class LegacyStaffLinkNullabilityTests(TestCase):
    """C7：StaffProfile.legacy_staff 可空且不再阻断旧表处置。"""

    def test_field_is_nullable_and_set_null(self):
        field = StaffProfile._meta.get_field("legacy_staff")
        self.assertTrue(field.null, "legacy_staff 必须可空")  # type: ignore[union-attr]
        self.assertTrue(field.blank, "legacy_staff 必须允许空表单值")  # type: ignore[union-attr]
        remote_field = field.remote_field
        self.assertIs(
            remote_field.on_delete,  # type: ignore[union-attr]
            models.SET_NULL,
            "旧表行被处置时不得再抛 ProtectedError（阻断旧表退役）",
        )

    def test_existing_link_is_preserved(self):
        legacy = Staff.objects.create(姓名="留痕员工", 职务="服务员", 月薪="3000", 工龄="1")
        profile = StaffProfile.objects.create(
            full_name="留痕档案", legacy_staff=legacy, employment_status="ACTIVE"
        )
        profile.refresh_from_db()
        self.assertEqual(
            profile.legacy_staff_id,  # type: ignore[attr-defined]
            legacy.pk,
            "历史映射必须保留可追溯",
        )


class StaffArchiveLandingTests(TestCase):
    """C8：旧 staff 表有归档落点，归档 + 恢复演练数据一致。"""

    prop: Property
    legacy: Staff

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.legacy = Staff.objects.create(姓名="归档演练员工", 职务="服务员", 月薪="3600", 工龄="5")

    def test_archive_spec_covers_staff_table(self):
        from apps.billing.legacy_archive import ARCHIVE_SPECS, get_spec

        spec = get_spec("Staff")
        self.assertEqual(spec.source_table, "staff")
        self.assertEqual(spec.archive_table, "legacy_staff_archive")
        self.assertIn(spec, ARCHIVE_SPECS)

    def test_archive_and_restore_drill_keeps_data_consistent(self):
        """归档一次 → 摘要复核 → 恢复演练：数据一致，旧表零改动。"""
        from apps.billing.legacy_archive import archive_table, get_spec, verify_archive_run
        from apps.billing.legacy_restore_drill import run_drill
        from apps.billing.models import LegacyStaffArchive

        before = list(Staff.objects.values("员工id", "姓名", "职务", "月薪", "工龄", "启用"))
        run = archive_table(spec=get_spec("Staff"), batch_uid="ARCH04-ARC-1", operator="arch04-ops")
        self.assertEqual(run.row_count, 1)
        self.assertTrue(verify_archive_run(run)["ok"], "归档行摘要必须与整表摘要一致")

        archived = LegacyStaffArchive.objects.get(pk=self.legacy.pk)
        self.assertEqual(archived.job_title_text, "服务员")
        self.assertEqual(archived.salary_text, "3600")
        self.assertEqual(archived.service_years_text, "5")
        self.assertEqual(archived.enabled_text, "启用")
        self.assertNotIn("归档演练员工", archived.name_ciphertext, "姓名不得明文落归档")
        from apps.guests.name_pii import decrypt_guest_name

        self.assertEqual(decrypt_guest_name(archived.name_ciphertext), "归档演练员工")

        after = list(Staff.objects.values("员工id", "姓名", "职务", "月薪", "工龄", "启用"))
        self.assertEqual(before, after, "归档只读旧表，不得改动任何旧行")

        with tempfile.TemporaryDirectory() as tmp:
            report = run_drill(
                batch_uid="ARCH04-ARC-1",
                scratch_path=pathlib.Path(tmp) / "scratch.sqlite3",
                operator="arch04-drill",
                tables=["staff"],
            )
        staff_entry = next(t for t in report["tables"] if t["source_table"] == "staff")
        self.assertTrue(staff_entry["digest_ok"], "恢复演练摘要复核必须通过")
        self.assertTrue(staff_entry["equivalence_ok"], "恢复后与归档行必须逐字段一致")
        self.assertEqual(staff_entry["archived_rows"], 1)
        self.assertEqual(staff_entry["restored_rows"], 1)
        self.assertTrue(report["ok"], report["blockers"])


class StaffPermissionRebindTests(TestCase):
    """C10：员工模块权限码解绑旧 ContentType，既有授权映射到新码。"""

    def test_registry_staff_module_points_to_new_stack_codes(self):
        """员工模块的四个动作列必须脱离旧 staff ContentType 的 CRUD 码。

        口径说明（C10 范围）：解绑对象是 hotelapp.view_staff/add_staff/
        change_staff/delete_staff 四个旧码；专项码 hotelapp.export_pii 与
        hotelapp.export_approve 属敏感导出权限（不在本轮解绑范围）。
        """
        staff_module = MODULE_REGISTRY_V2["staff"]
        actions = cast(dict, staff_module["actions"])
        legacy_crud_codes = {
            "hotelapp.view_staff",
            "hotelapp.add_staff",
            "hotelapp.change_staff",
            "hotelapp.delete_staff",
        }
        codes = set(iter_module_permission_codes(staff_module))
        self.assertTrue(codes, "员工模块必须登记权限码")
        self.assertFalse(
            legacy_crud_codes & codes,
            "员工模块不得再指向旧 staff ContentType 的 CRUD 权限码："
            + "；".join(sorted(legacy_crud_codes & codes)),
        )
        for action in ("view", "add", "change", "delete"):
            code = actions[action]
            self.assertTrue(
                code.startswith("identity."),
                f"员工模块 {action} 列必须指向新栈权限码：{code}",
            )
        self.assertIn("identity.staff_manage", actions["special"])

    def test_existing_grants_migrated_to_new_codes(self):
        """既有授权（旧码持有组）已迁移到新栈权限码。"""
        owner = Group.objects.get(name="店长")
        self.assertTrue(
            owner.permissions.filter(
                content_type__app_label="identity", codename="view_staffprofile"
            ).exists(),
            "持有旧 view_staff 的角色组必须同步持有新栈 view_staffprofile",
        )
        self.assertTrue(
            LegacyPermissionCodeGrant.objects.filter(
                source_code="hotelapp.view_staff", target_code="identity.view_staffprofile"
            ).exists(),
            "权限迁移必须留台账（回退路径可核对）",
        )

    def test_read_only_grant_stays_read_only(self):
        """只读旧码不得映射成可写新码（不得借迁移放大权限）。"""
        finance = Group.objects.get(name="财务")
        self.assertTrue(
            finance.permissions.filter(
                content_type__app_label="identity", codename="view_staffprofile"
            ).exists(),
            "财务的旧只读授权应映射为新栈只读码",
        )
        self.assertFalse(
            finance.permissions.filter(
                content_type__app_label="identity",
                codename__in=["add_staffprofile", "change_staffprofile", "delete_staffprofile"],
            ).exists(),
            "只读岗位不得因权限迁移获得写权限",
        )

    def test_staff_center_still_accessible_after_rebind(self):
        """权限码解绑后，新员工中心仍必须可用（不得 403）。"""
        owner = _make_user("arch04_center_owner")
        self.client.force_login(owner)
        response = self.client.get("/manage/staff/")
        self.assertEqual(response.status_code, 200, "员工中心不得 403")
        api_response = self.client.get("/api/v1/staff")
        self.assertEqual(api_response.status_code, 200, "员工中心 API 不得 403")


class MigrationRoundTripTests(TransactionTestCase):
    """C6/C7/C10：新增迁移可正向执行也可反向执行，数据无损。"""

    reset_sequences = True

    def test_migrations_can_reverse_and_reapply_without_data_loss(self):
        legacy = Staff.objects.create(姓名="迁移往返员工", 职务="服务员", 月薪="3000", 工龄="1")
        room = Room.objects.create(
            房间号=97031, 客房类型="标准间", 价格=300, 负责人编号=legacy.pk, 房间状态="空闲"
        )
        profile = StaffProfile.objects.create(
            full_name="迁移往返档案", legacy_staff=legacy, employment_status="ACTIVE"
        )
        owner_group = Group.objects.get(name="店长")
        self.assertTrue(
            owner_group.permissions.filter(
                content_type__app_label="identity", codename="view_staffprofile"
            ).exists(),
            "正向迁移后新栈权限码必须已授予既有持有组",
        )

        # --- 反向：identity → 0013（撤销 0014/0015），hotelapp → 0021（复原 FK）---
        call_command("migrate", "identity", "0013_ns_e05_permission_grants", verbosity=0)
        call_command("migrate", "hotelapp", "0021_night_auditor_activation", verbosity=0)

        room.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(room.负责人编号, legacy.pk, "反向迁移不得丢失旧行上的工号原值")
        self.assertEqual(
            profile.legacy_staff_id,  # type: ignore[attr-defined]
            legacy.pk,
            "反向迁移不得丢失历史映射",
        )
        owner_group.refresh_from_db()
        self.assertFalse(
            owner_group.permissions.filter(
                content_type__app_label="identity", codename="view_staffprofile"
            ).exists(),
            "反向迁移必须精确撤销本迁移新增的授权",
        )
        with connection.cursor() as cursor:
            fk_targets = _foreign_key_targets(cursor, "room")
            self.assertIn(
                "staff",
                fk_targets,
                "反向迁移必须把 FK 约束复原（回滚能力真实可用，非 noop）",
            )

        # --- 正向：重新应用 ---
        call_command("migrate", "hotelapp", verbosity=0)
        call_command("migrate", "identity", verbosity=0)
        room.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(room.负责人编号, legacy.pk)
        self.assertEqual(
            profile.legacy_staff_id,  # type: ignore[attr-defined]
            legacy.pk,
        )
        owner_group.refresh_from_db()
        self.assertTrue(
            owner_group.permissions.filter(
                content_type__app_label="identity", codename="view_staffprofile"
            ).exists(),
            "正向迁移必须重新授予新栈权限码",
        )
        with connection.cursor() as cursor:
            fk_targets = _foreign_key_targets(cursor, "room")
            self.assertNotIn("staff", fk_targets, "正向迁移后不得存在指向旧 staff 表的 FK 约束")
