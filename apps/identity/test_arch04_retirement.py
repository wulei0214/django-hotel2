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
import importlib
import pathlib
import tempfile
from typing import cast

from django.contrib.auth.models import Group, Permission, User
from django.core.management import call_command
from django.db import connection, models
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from django.utils.crypto import get_random_string
from hotelapp.models import Food, Order, Room, Staff

from apps.core.models import AuditEvent, Property
from apps.identity.models import (
    LegacyPermissionCodeGrant,
    LegacyStaffMapping,
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
    ("apps/identity/management/commands/link_legacy_staff.py", None): (
        "②一次性映射台账命令的 import（ARCH-04 B1/C11：离线盘点与登记，" "不在在线业务路径）"
    ),
    ("apps/identity/management/commands/link_legacy_staff.py", "handle"): (
        "②一次性映射台账命令的旧行读取（离线盘点/登记，不是在线路径）"
    ),
    ("apps/identity/management/commands/link_legacy_staff.py", "_report"): (
        "②一次性映射台账命令的只读盘点输出（不改库）"
    ),
    ("apps/identity/management/commands/link_legacy_staff.py", "_link"): (
        "②一次性映射台账命令的映射登记（只写新栈台账，不写旧表）"
    ),
    ("apps/identity/management/commands/link_legacy_staff.py", "_recover"): (
        "②一次性映射台账命令的映射恢复（只写新栈台账，不写旧表）"
    ),
    ("hotelapp/management/commands/audit_employee_data.py", None): ("②一次性盘点命令的 import"),
    ("hotelapp/management/commands/audit_employee_data.py", "handle"): (
        "②一次性盘点命令的只读输出（不改库）"
    ),
    ("hotelapp/management/commands/seed_hotel_data.py", "handle"): (
        "②一次性盘点/种子命令的只读映射（不写旧表）"
    ),
}
# 旧表在在线业务路径上已零引用：ARCH-02/03 把 /index/ 与 /search_staff/ 退役为
# 路由级重定向并删除对应视图后，hotelapp/views.py 的模块级 import 一并摘除，
# 例外清单收敛到「一次性归档/盘点命令」四类。

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


# ---------------------------------------------------------------------------
# C1 / C13 扫描器（ARCH-04 H3：门禁必须防绕过）
#
# 早期版本只认三种静态形态（Name / Attribute / ImportFrom），可被下列写法绕过：
#   ① from hotelapp.models import *          —— 星号导入带入旧模型
#   ② apps.get_model("hotelapp", "staff")    —— 运行期取模型，源码无 Staff 符号
#   ③ getattr(models, "Staff")               —— 运行期取属性
#   ④ importlib.import_module("hotelapp.models") + getattr
# 现在这些形态一律命中；命中即视为「旧表在线引用」。
# ---------------------------------------------------------------------------

# 旧模型符号名（大小写不敏感覆盖 LegacyStaff 别名）。
_LEGACY_MODEL_SYMBOLS = {"Staff", "LegacyStaff"}
# 旧表所在 app_label / 模型名（小写）。
_LEGACY_APP_LABEL = "hotelapp"
_LEGACY_MODEL_LOWER = "staff"
# 旧表所在模块前缀。
_LEGACY_MODULE_PREFIXES = ("hotelapp.models", "hotelapp")
# 写方法集合（C13 零写入防线）：命中即说明旧表被写入。
_LEGACY_WRITE_METHODS = {
    "create",
    "save",
    "update",
    "delete",
    "get_or_create",
    "update_or_create",
    "bulk_create",
    "bulk_update",
    "bulk_update_or_create",
    "raw",
}


def _root_name(node):
    """取属性/调用链的根符号名（Staff.objects.create → "Staff"）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root_name(node.value)
    if isinstance(node, ast.Call):
        return _root_name(node.func)
    if isinstance(node, ast.Subscript):
        return _root_name(node.value)
    return None


def _callee_name(node):
    """取调用目标的方法名（apps.get_model → "get_model"）。"""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _chain_contains_legacy(node):
    """属性/调用链里是否出现旧模型符号（models.Staff.objects.get → True）。"""
    if isinstance(node, ast.Name):
        return node.id in _LEGACY_MODEL_SYMBOLS
    if isinstance(node, ast.Attribute):
        return node.attr in _LEGACY_MODEL_SYMBOLS or _chain_contains_legacy(node.value)
    if isinstance(node, ast.Call):
        return _chain_contains_legacy(node.func)
    if isinstance(node, ast.Subscript):
        return _chain_contains_legacy(node.value)
    return False


def _legacy_bound_names(tree):
    """本文件内由旧表表达式赋值出来的局部变量名。

    静态类型推导不做，但「`row = Staff.objects.get(...)` 之后 `row.save()`」
    这种最常见的旧表写入形态必须能被发现，否则 C13 零写入防线形同虚设。
    """
    names = set()
    for node in ast.walk(tree):
        targets: list = []
        value = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None or not _chain_contains_legacy(value):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _scan_legacy_staff_references(path):
    """返回该文件里指向旧 staff 模型的引用 (行号, 所在函数)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []

    def add(node, func):
        hits.append((node.lineno, func))

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            new_func = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                new_func = child.name
            if (
                isinstance(child, ast.Name)
                and child.id in _LEGACY_MODEL_SYMBOLS
                or isinstance(child, ast.Attribute)
                and child.attr in _LEGACY_MODEL_SYMBOLS
            ):
                add(child, new_func)
            elif isinstance(child, ast.ImportFrom):
                module = child.module or ""
                # 相对导入（from .models import Staff）解析到本包内，也算旧表引用
                relative = child.level and child.level > 0
                absolute_legacy = module == _LEGACY_APP_LABEL or module.startswith(
                    _LEGACY_APP_LABEL + "."
                )
                if (relative or absolute_legacy) and any(
                    alias.name in _LEGACY_MODEL_SYMBOLS or alias.name == "*"
                    for alias in child.names
                ):
                    # ① 直接导入旧模型；② 星号导入（可把旧模型带入命名空间）
                    add(child, new_func)
            elif isinstance(child, ast.Call):
                callee = _callee_name(child.func)
                # ② apps.get_model("hotelapp", "staff") / get_model("hotelapp.models", "Staff")
                if callee == "get_model":
                    strings = [
                        a.value
                        for a in child.args
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    ]
                    if any(
                        s.lower() == _LEGACY_MODEL_LOWER
                        or s == _LEGACY_APP_LABEL
                        or s.lower().startswith(_LEGACY_APP_LABEL + ".")
                        for s in strings
                    ):
                        add(child, new_func)
                # ③ getattr(models, "Staff")
                elif callee == "getattr":
                    if len(child.args) >= 2:
                        second = child.args[1]
                        if (
                            isinstance(second, ast.Constant)
                            and isinstance(second.value, str)
                            and second.value.lower() == _LEGACY_MODEL_LOWER
                        ):
                            add(child, new_func)
                # ④ importlib.import_module("hotelapp.models")
                elif callee == "import_module":
                    strings = [
                        a.value
                        for a in child.args
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    ]
                    if any(s.lower().startswith(_LEGACY_APP_LABEL) for s in strings):
                        add(child, new_func)
            elif isinstance(child, ast.Subscript):
                # ⑤ models.__dict__["Staff"] / vars(models)["Staff"]
                slice_node = child.slice
                if (
                    isinstance(slice_node, ast.Constant)
                    and isinstance(slice_node.value, str)
                    and slice_node.value.lower() == _LEGACY_MODEL_LOWER
                ):
                    add(child, new_func)
            visit(child, new_func)

    visit(tree, None)
    return hits


def _scan_legacy_staff_writes(path):
    """返回该文件里对旧 staff 表的**写入**调用 (行号, 所在函数, 写方法)。

    C13 零写入防线：生产代码里对旧表的 create/save/update/delete 等写调用
    必须为零。只读调用（filter/get/first/values 等）不算写。

    识别两种形态：
      ① 链上直接出现旧模型符号：`Staff.objects.create(...)`；
      ② 由旧表表达式赋值出的局部变量再写：`row = Staff.objects.get(...)`
         之后 `row.save()` / `row.delete()`（局部数据流，文件内可判定）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    legacy_names = _LEGACY_MODEL_SYMBOLS | _legacy_bound_names(tree)
    hits = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            new_func = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                new_func = child.name
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                method = child.func.attr
                if method in _LEGACY_WRITE_METHODS:
                    receiver = child.func.value
                    root = _root_name(receiver)
                    if _chain_contains_legacy(receiver) or root in legacy_names:
                        hits.append((child.lineno, new_func, method))
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
        for rel, _func in ALLOWED_LEGACY_STAFF_REFERENCES:
            self.assertTrue(
                rel.endswith(
                    (
                        "legacy_archive.py",
                        "audit_employee_data.py",
                        "seed_hotel_data.py",
                        "link_legacy_staff.py",
                    )
                ),
                f"例外清单出现未登记文件：{rel}",
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


class LegacyStaffGateAntiBypassTests(TestCase):
    """ARCH-04 H3 / C13：C1 AST 门禁必须**防绕过**，且旧表零写入。

    本组用例是门禁本身的负向测试：把各种绕过写法写成临时 .py 文件喂给扫描
    器，断言**每一份都被命中**。没有这组用例，扫描器可能被"优化"成只认某
    一种写法而重新打开绕过口子，而 CI 仍然是绿的。
    """

    # 绕过写法 → 必须命中（每个片段都刻意用不同形态取到旧表）
    BYPASS_SNIPPETS = {
        "直接导入": "from hotelapp.models import Staff\n\ndef f():\n    return Staff.objects.all()\n",
        "别名导入": "from hotelapp.models import Staff as Old\n\ndef f():\n    return Old.objects.all()\n",
        "属性访问": "import hotelapp.models\n\ndef f():\n    return hotelapp.models.Staff.objects.all()\n",
        "模块别名": "import hotelapp.models as m\n\ndef f():\n    return m.Staff.objects.all()\n",
        "from 导入模块": "from hotelapp import models\n\ndef f():\n    return models.Staff.objects.all()\n",
        "星号导入": "from hotelapp.models import *\n\ndef f():\n    return Staff.objects.all()\n",
        "get_model": (
            "from django.apps import apps\n\n"
            "def f():\n"
            "    return apps.get_model('hotelapp', 'staff').objects.all()\n"
        ),
        "getattr": (
            "from hotelapp import models\n\n"
            "def f():\n"
            "    return getattr(models, 'Staff').objects.all()\n"
        ),
        "import_module": (
            "import importlib\n\n"
            "def f():\n"
            "    return importlib.import_module('hotelapp.models').Staff.objects.all()\n"
        ),
        "字典下标": (
            "from hotelapp import models\n\n"
            "def f():\n"
            "    return models.__dict__['Staff'].objects.all()\n"
        ),
    }

    def _scan_snippet(self, source):
        """把源码片段写成临时文件并跑扫描器，返回命中列表。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "snippet.py"
            path.write_text(source, encoding="utf-8")
            return _scan_legacy_staff_references(path)

    def test_every_bypass_shape_is_detected(self):
        """逐个绕过写法都必须被扫描器命中。"""
        missed = []
        for label, source in self.BYPASS_SNIPPETS.items():
            if not self._scan_snippet(source):
                missed.append(label)
        self.assertEqual(
            missed,
            [],
            "C1 AST 门禁被绕过（下列写法未被命中）：\n" + "\n".join(missed),
        )

    def test_clean_production_code_is_not_flagged(self):
        """反向校验：正常的新栈代码不得被误报（门禁不得变成噪音源）。"""
        clean = (
            "from apps.identity.models import StaffProfile\n\n"
            "def f(profile):\n"
            "    return profile.full_name\n"
        )
        self.assertEqual(self._scan_snippet(clean), [])

    def test_write_calls_to_legacy_table_are_detected(self):
        """C13：旧表写调用必须被识别（含链式、queryset 写与局部数据流）。"""
        snippets = {
            "objects.create": (
                "from hotelapp.models import Staff\n\n"
                "def f():\n"
                "    Staff.objects.create(姓名='x')\n"
            ),
            "instance.save": (
                "from hotelapp.models import Staff\n\n"
                "def f():\n"
                "    row = Staff.objects.get(pk=1)\n"
                "    row.save()\n"
            ),
            "instance.delete": (
                "from hotelapp.models import Staff\n\n"
                "def f():\n"
                "    row = Staff.objects.get(pk=1)\n"
                "    row.delete()\n"
            ),
            "queryset.update": (
                "from hotelapp.models import Staff\n\n"
                "def f():\n"
                "    Staff.objects.filter(启用=True).update(姓名='y')\n"
            ),
            "queryset.delete": (
                "from hotelapp.models import Staff\n\n"
                "def f():\n"
                "    Staff.objects.filter(启用=False).delete()\n"
            ),
            "get_or_create": (
                "from hotelapp.models import Staff\n\n"
                "def f():\n"
                "    Staff.objects.get_or_create(员工id=1)\n"
            ),
        }
        missed = []
        for label, source in snippets.items():
            with tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "snippet.py"
                path.write_text(source, encoding="utf-8")
                if not _scan_legacy_staff_writes(path):
                    missed.append(label)
        self.assertEqual(
            missed,
            [],
            "C13 旧表写调用未被识别（下列写法漏检）：\n" + "\n".join(missed),
        )

    def test_opaque_parameter_write_is_not_statically_decidable(self):
        """边界登记：通过不透明参数拿到旧表实例再写入，静态扫描无法判定。

        静态扫描能做的是「本文件内出现旧表符号」与「本文件内的局部数据流」。
        「旧表实例从别的模块传进来」属于跨模块数据流，静态不可判定——
        但**取得该实例的那一侧**必然出现旧表符号，会被 C1 引用扫描拦下；
        运行期还有 `apps/identity/test_module_gates.py` 的旧表计数零变化探针
        兜底。本用例把这条边界写进用例，避免后人误以为写扫描是完备的。
        """
        source = "def f(row):\n" "    row.save()\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "snippet.py"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(_scan_legacy_staff_writes(path), [])

    def test_read_only_calls_are_not_write_violations(self):
        """反向校验：只读调用不算写（旧表只读路径是允许的例外）。"""
        source = (
            "from hotelapp.models import Staff\n\n"
            "def f():\n"
            "    return list(Staff.objects.filter(启用=True).values('员工id'))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "snippet.py"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(_scan_legacy_staff_writes(path), [])

    def test_repository_has_no_legacy_staff_write_path(self):
        """C13：全仓生产代码对旧表的写调用必须为零（例外清单也不豁免写）。"""
        violations = []
        for path in _iter_production_python_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            for lineno, func, method in _scan_legacy_staff_writes(path):
                violations.append(f"{rel}:{lineno}（函数 {func or '模块级'}）.{method}")
        self.assertEqual(
            violations,
            [],
            "旧 staff 表出现生产写路径（ARCH-04 C13 零写入防线）：\n" + "\n".join(violations),
        )


class LegacyStaffMappingLedgerTests(TestCase):
    """ARCH-04 B1：旧员工 ↔ 新档案的持久映射台账。

    验收口径（映射唯一 / 可追溯 / 异常可恢复 / 删除保护）：
      * 映射唯一——旧行与档案各一对一，重复映射被拒；
      * 可追溯——记录匹配方式、状态、核验人与核验时间、旧主键原值；
      * 异常可恢复——台账行存在但旧行指向失效时可恢复；
      * 删除保护——台账行不可删除；删除已映射的新档案被库层 PROTECT 拒绝。
    """

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        self.profile = StaffProfile.objects.create(
            full_name="B1 映射档案", employment_status="ACTIVE"
        )
        self.legacy = Staff.objects.create(姓名="B1 旧员工", 职务="前台", 启用=True)

    # ------------------------------------------------------------------
    def test_binding_creates_a_traceable_mapping(self):
        """登记映射 → 台账行存在，且记录匹配方式/核验人/时间/旧主键。"""
        from apps.identity.services import EmployeeLifecycleService

        mapping = EmployeeLifecycleService.bind_legacy_staff_mapping(
            self.profile,
            legacy_staff=self.legacy,
            property_obj=self.prop,
            match_method="工号映射",
            reason="B1 用例",
        )
        self.assertEqual(mapping.legacy_pk, self.legacy.pk)
        self.assertEqual(mapping.staff_profile_id, self.profile.pk)
        self.assertEqual(mapping.legacy_staff_id, self.legacy.pk)
        self.assertEqual(mapping.mapping_status, "LINKED")
        self.assertEqual(mapping.match_method, "工号映射")
        self.assertIsNotNone(mapping.reviewed_at)
        # 映射必须同时落到 StaffProfile.legacy_staff（「当前指向」与「映射事实」一致）
        self.profile.refresh_from_db()
        self.assertEqual(cast(Staff, self.profile.legacy_staff).pk, self.legacy.pk)

    def test_binding_is_idempotent_and_rejects_conflicts(self):
        """重复绑定幂等；同一旧行不得映射到两个档案。"""
        from apps.identity.services import EmployeeLifecycleError, EmployeeLifecycleService

        first = EmployeeLifecycleService.bind_legacy_staff_mapping(
            self.profile,
            legacy_staff=self.legacy,
            property_obj=self.prop,
            reason="首次",
        )
        again = EmployeeLifecycleService.bind_legacy_staff_mapping(
            self.profile,
            legacy_staff=self.legacy,
            property_obj=self.prop,
            reason="重复",
        )
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(LegacyStaffMapping.objects.count(), 1)

        other = StaffProfile.objects.create(full_name="B1 另一个档案", employment_status="ACTIVE")
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.bind_legacy_staff_mapping(
                other,
                legacy_staff=self.legacy,
                property_obj=self.prop,
                reason="冲突",
            )

    def test_mapping_row_cannot_be_deleted(self):
        """删除保护①：映射台账行不可删除（映射事实只追加）。"""
        from apps.identity.services import EmployeeLifecycleService

        mapping = EmployeeLifecycleService.bind_legacy_staff_mapping(
            self.profile,
            legacy_staff=self.legacy,
            property_obj=self.prop,
        )
        with self.assertRaises(RuntimeError):
            mapping.delete()

    def test_deleting_a_mapped_profile_is_blocked(self):
        """删除保护②：删除已映射的员工档案被库层拒绝（映射不得悬空）。"""
        from django.db.utils import IntegrityError

        from apps.identity.services import EmployeeLifecycleService

        EmployeeLifecycleService.bind_legacy_staff_mapping(
            self.profile,
            legacy_staff=self.legacy,
            property_obj=self.prop,
        )
        with self.assertRaises(IntegrityError):
            self.profile.delete()

    def test_legacy_pk_must_match_legacy_row(self):
        """一致性守卫：台账行的旧主键必须与旧行一致（防错配/防篡改）。"""
        mapping = LegacyStaffMapping(
            property=self.prop,
            staff_profile=self.profile,
            legacy_staff=self.legacy,
            legacy_pk=self.legacy.pk + 999,
        )
        with self.assertRaises(ValueError):
            mapping.save()

    def test_mapping_is_recoverable_after_legacy_pointer_lost(self):
        """异常可恢复：旧行指向失效后，可按台账恢复并重新指向。"""
        from apps.identity.services import EmployeeLifecycleService

        mapping = EmployeeLifecycleService.bind_legacy_staff_mapping(
            self.profile,
            legacy_staff=self.legacy,
            property_obj=self.prop,
        )
        # 模拟旧行被清理：指向置空（台账行保留，旧主键原值仍在）
        LegacyStaffMapping.objects.filter(pk=mapping.pk).update(legacy_staff=None)
        mapping.refresh_from_db()
        self.assertIsNone(mapping.legacy_staff_id)
        self.assertEqual(mapping.legacy_pk, self.legacy.pk)

        recovered = EmployeeLifecycleService.recover_legacy_staff_mapping(
            mapping,
            legacy_staff=self.legacy,
            reason="B1 恢复用例",
        )
        recovered.refresh_from_db()
        self.assertEqual(recovered.legacy_staff_id, self.legacy.pk)
        self.assertEqual(recovered.mapping_status, "LINKED")
        self.profile.refresh_from_db()
        self.assertEqual(cast(Staff, self.profile.legacy_staff).pk, self.legacy.pk)

    def test_mapping_is_audited(self):
        """映射登记与恢复都要写审计（操作者/前后值）。"""
        from apps.identity.services import EmployeeLifecycleService

        mapping = EmployeeLifecycleService.bind_legacy_staff_mapping(
            self.profile,
            legacy_staff=self.legacy,
            property_obj=self.prop,
            reason="B1 审计用例",
        )
        event = cast(
            AuditEvent,
            AuditEvent.objects.filter(
                action="identity.legacy_staff_mapped", entity_id=str(self.profile.pk)
            ).first(),
        )
        self.assertIsNotNone(event, "映射登记必须写审计")
        self.assertEqual(event.after_json["legacy_staff_id"], self.legacy.pk)
        self.assertEqual(event.after_json["mapping_id"], mapping.pk)

    def test_existing_legacy_link_is_backfilled_into_ledger(self):
        """迁移回填：历史 `StaffProfile.legacy_staff` 外键必须已进入台账。

        用迁移的 RunPython 逻辑直接验证（不依赖迁移执行顺序）：把外键指向建好
        后调用回填函数，台账必须出现对应行。
        """
        from django.apps import apps as django_apps

        self.profile.legacy_staff = self.legacy
        self.profile.save(update_fields=("legacy_staff", "updated_at"))
        migration = importlib.import_module("apps.identity.migrations.0016_legacy_staff_mapping")
        migration._backfill(django_apps, None)
        mapping = cast(
            LegacyStaffMapping,
            LegacyStaffMapping.objects.filter(staff_profile=self.profile).first(),
        )
        self.assertIsNotNone(mapping, "历史外键映射未被回填进台账")
        self.assertEqual(mapping.legacy_pk, self.legacy.pk)
        self.assertEqual(mapping.mapping_status, "LINKED")
        self.assertEqual(mapping.match_method, "历史外键回填")


class StaffPermissionChangeAuditTests(TestCase):
    """ARCH-04 H1：运行期授权变更审计必须带**操作者 + 时间 + 前后值**。

    历史实现里 `identity.role_assign` 只写 after（授权后是什么岗位），
    `identity.role_end` 只写 before（结束前是什么岗位）——两头各缺一半，
    无法据此复原「这个岗位是什么时候、被谁、加上还是撤掉的」。
    """

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        self.profile = StaffProfile.objects.create(
            full_name="H1 授权档案", employment_status="ACTIVE"
        )
        self.membership = PropertyStaffMembership.objects.create(
            property=self.prop,
            staff_profile=self.profile,
            employee_no="H1-0001",
            status="ACTIVE",
        )
        self.group, _ = Group.objects.get_or_create(name="客房服务员")
        self.actor = User.objects.create_user(username="h1_actor", password="x")

    def _assign(self):
        from apps.identity.services import EmployeeLifecycleService

        return EmployeeLifecycleService.assign_role(
            membership=self.membership,
            group=self.group,
            granted_by=self.actor,
            reason="H1 用例",
        )

    def test_role_assign_audit_has_operator_time_and_before_after(self):
        """授权审计：操作者 + 时间 + before + after 齐备。"""
        self._assign()
        event = cast(
            AuditEvent,
            AuditEvent.objects.filter(
                action="identity.role_assign", entity_id=str(self.profile.pk)
            ).first(),
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.actor, self.actor)
        self.assertIsNotNone(event.created_at)
        self.assertIsNotNone(event.before_json)
        self.assertIsNotNone(event.after_json)
        self.assertEqual(event.after_json["role"], self.group.name)
        self.assertIn(self.group.name, event.after_json["active_roles"])
        self.assertNotIn(self.group.name, event.before_json["active_roles"])
        self.assertEqual(event.before_json["employee_no"], self.membership.employee_no)

    def test_role_end_audit_has_operator_time_and_before_after(self):
        """结束授权审计：before 含该岗位，after 反映该岗位已失效。"""
        from apps.identity.services import EmployeeLifecycleService

        assignment = self._assign()
        EmployeeLifecycleService.end_assignment(assignment, ended_by=self.actor, reason="H1 结束")
        event = cast(
            AuditEvent,
            AuditEvent.objects.filter(
                action="identity.role_end", entity_id=str(self.profile.pk)
            ).first(),
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.actor, self.actor)
        self.assertIsNotNone(event.created_at)
        self.assertIsNotNone(event.before_json)
        self.assertIsNotNone(event.after_json)
        self.assertEqual(event.before_json["role"], self.group.name)
        self.assertIn(self.group.name, event.before_json["active_roles"])
        self.assertNotIn(self.group.name, event.after_json["active_roles"])
        self.assertIn("ended_at", event.after_json)

    def test_authorization_change_chain_is_reconstructable(self):
        """授权链可复原：assign 的 after 集合 == end 的 before 集合。"""
        from apps.identity.services import EmployeeLifecycleService

        assignment = self._assign()
        EmployeeLifecycleService.end_assignment(assignment, ended_by=self.actor, reason="H1 链")
        assign_event = cast(
            AuditEvent,
            AuditEvent.objects.filter(
                action="identity.role_assign", entity_id=str(self.profile.pk)
            ).first(),
        )
        end_event = cast(
            AuditEvent,
            AuditEvent.objects.filter(
                action="identity.role_end", entity_id=str(self.profile.pk)
            ).first(),
        )
        self.assertEqual(
            assign_event.after_json["active_roles"],
            end_event.before_json["active_roles"],
            "授权变更链断裂：assign 的 after 与 end 的 before 必须一致",
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
        # 自建持有旧权限码的角色组：数据迁移种出的角色组会被 TransactionTestCase 的
        # flush 清空（flush 只重建 ContentType/Permission，不重建 Group），
        # 断言不得依赖迁移种子数据——否则用例结果取决于同批次其它 TransactionTestCase
        # 的执行顺序与引擎跳过项（MySQL 跳过项少→flush 更多→种子组先消失）。
        group = Group.objects.create(name="ARCH04-ROUNDTRIP")
        old_view = Permission.objects.get(content_type__app_label="hotelapp", codename="view_staff")
        group.permissions.add(old_view)

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
        group.refresh_from_db()
        self.assertTrue(
            group.permissions.filter(pk=old_view.pk).exists(),
            "反向迁移不得误删迁移前就持有的旧权限码",
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
        group.refresh_from_db()
        self.assertTrue(
            group.permissions.filter(
                content_type__app_label="identity", codename="view_staffprofile"
            ).exists(),
            "正向迁移必须给持有旧码的角色组补上新栈权限码",
        )
        self.assertFalse(
            group.permissions.filter(
                content_type__app_label="identity",
                codename__in=[
                    "add_staffprofile",
                    "change_staffprofile",
                    "delete_staffprofile",
                ],
            ).exists(),
            "只读旧码不得被迁移放大成写权限",
        )
        self.assertTrue(
            LegacyPermissionCodeGrant.objects.filter(
                group=group,
                source_code="hotelapp.view_staff",
                target_code="identity.view_staffprofile",
            ).exists(),
            "权限迁移必须留台账（回退路径可核对）",
        )
        with connection.cursor() as cursor:
            fk_targets = _foreign_key_targets(cursor, "room")
            self.assertNotIn("staff", fk_targets, "正向迁移后不得存在指向旧 staff 表的 FK 约束")

        # --- 再次反向：必须精确撤销本迁移新增的授权，且不误删旧码 ---
        call_command("migrate", "identity", "0013_ns_e05_permission_grants", verbosity=0)
        group.refresh_from_db()
        self.assertFalse(
            group.permissions.filter(
                content_type__app_label="identity", codename="view_staffprofile"
            ).exists(),
            "反向迁移必须精确撤销本迁移新增的授权",
        )
        self.assertTrue(
            group.permissions.filter(pk=old_view.pk).exists(),
            "反向迁移不得误删迁移前就持有的旧权限码",
        )
        with connection.cursor() as cursor:
            tables = set(connection.introspection.table_names(cursor))
        self.assertNotIn(
            LegacyPermissionCodeGrant._meta.db_table,
            tables,
            "反向迁移必须把台账表一并撤下（表随模型删除，无残留）",
        )
        # --- 末态：重新正向，状态与首次正向一致 ---
        call_command("migrate", "identity", verbosity=0)
        group.refresh_from_db()
        self.assertTrue(
            group.permissions.filter(
                content_type__app_label="identity", codename="view_staffprofile"
            ).exists(),
            "再次正向迁移后必须重新授予新栈权限码",
        )


class C6ReverseWithNullAndDanglingDataTests(TransactionTestCase):
    """C6 反向迁移在「NULL 工号 + 悬空工号」数据上真实可用。

    实证缺陷（2026-11-26 复核）：0022 把三处旧 staff 外键改成可空整数后，同批用例
    又把「悬空工号（指向不存在的 staff 行）」与「NULL 工号」断言为合法数据；而反向
    迁移在 MySQL 上先 MODIFY ... NOT NULL（遇 NULL 报 1138）、再 ADD CONSTRAINT
    FOREIGN KEY（遇悬空工号报 1452）——反向在真实数据上必失败，违反方案
    §5.2「可用反向迁移（不可 noop）」的硬约束。
    修复：0022 的反向路径先补建占位员工行并回填 NULL，使 FK 约束可被真实重建。
    """

    reset_sequences = True

    def test_reverse_migration_succeeds_with_null_and_dangling_staff_refs(self):
        legacy = Staff.objects.create(姓名="C6 反向员工", 职务="服务员", 月薪="3000", 工龄="1")
        Room.objects.create(
            房间号=97061, 客房类型="标准间", 价格=300, 负责人编号=legacy.pk, 房间状态="空闲"
        )
        Room.objects.create(
            房间号=97062, 客房类型="标准间", 价格=300, 负责人编号=987655, 房间状态="空闲"
        )  # 悬空工号
        Room.objects.create(
            房间号=97063, 客房类型="标准间", 价格=300, 负责人编号=None, 房间状态="空闲"
        )  # NULL 工号

        # 反向：不得抛 1138 / 1452
        call_command("migrate", "hotelapp", "0021_night_auditor_activation", verbosity=0)

        with connection.cursor() as cursor:
            self.assertIn(
                "staff",
                _foreign_key_targets(cursor, "room"),
                "反向迁移必须把 FK 约束复原（回滚能力真实可用，非 noop）",
            )
        self.assertEqual(
            Room.objects.get(房间号=97061).负责人编号,
            legacy.pk,
            "真实工号原值不得丢失",
        )
        self.assertEqual(
            Room.objects.get(房间号=97062).负责人编号,
            987655,
            "悬空工号不得被改写（须补建同 id 占位行而不是改值）",
        )
        self.assertTrue(
            Staff.objects.filter(员工id=987655).exists(),
            "悬空工号必须补建同 id 的占位员工行，否则 FK 约束重建失败",
        )
        nulled = Room.objects.get(房间号=97063).负责人编号
        self.assertIsNotNone(nulled, "NULL 工号必须在反向前被回填（否则 NOT NULL 还原失败）")
        self.assertTrue(
            Staff.objects.filter(员工id=nulled).exists(),
            "回填目标必须是真实存在的占位员工行",
        )

        # --- 末态：重新正向，状态与首次正向一致 ---
        call_command("migrate", "hotelapp", verbosity=0)
        call_command("migrate", "identity", verbosity=0)
        self.assertEqual(Room.objects.get(房间号=97062).负责人编号, 987655)
