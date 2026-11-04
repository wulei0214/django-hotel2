"""§7 跨 App RBAC 注册表测试（对应 docs/audit/phase0/25-rbac-registry.md）。

覆盖四条验收线：
  1. validate_registry() 无 ERROR——ACTIVE 模块权限码全部解析到真实 Permission，
     并与 Phase 0 审计文档（11-permissions.md / 12-roles.md）交叉核对；
  2. PLANNED 模块（餐饮/停车）权限经伪造 POST 授权：矩阵页白名单若放行，
     注册表校验必须能发现（缺口 F-09 的兜底断言）；
  3. ROLE_REGISTRY 与数据库九组一一对应（5 现役 + 4 预留空组）；
  4. SPECIAL_PERMISSIONS 每项解析到真实 Permission。
另含 module_gate 行为回归：food/order/park 仍隐藏（注册表接管后行为不变）。
"""
import os
import re

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Property
from .models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from .registry import (
    MODULE_ACTIVE,
    MODULE_PLANNED,
    MODULE_REGISTRY_V2,
    NOT_APPLICABLE,
    ROLE_ACTIVE,
    ROLE_PLANNED,
    ROLE_REGISTRY,
    SPECIAL_PERMISSIONS,
    get_special,
    iter_module_permission_codes,
    legacy_module_gate_keys,
    module_gate_open,
    registry_errors,
    validate_registry,
)
from .roles import BUSINESS_ROLE_NAMES

_PHASE0_DOCS = ("11-permissions.md", "12-roles.md")


def _phase0_audit_text():
    """读取 Phase 0 审计文档全文（权限点清单 + 角色快照），供交叉核对。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    parts = []
    for name in _PHASE0_DOCS:
        path = os.path.join(root, "docs", "audit", "phase0", name)
        with open(path, "r", encoding="utf-8") as fh:
            parts.append(fh.read())
    return "\n".join(parts)


class RegistryConsistencyTests(TestCase):
    """注册表 ↔ 数据库一致性：validate_registry 无 ERROR + 文档交叉核对。"""

    def test_validate_registry_has_no_errors(self):
        findings = validate_registry()
        errors = registry_errors(findings)
        self.assertEqual(
            errors, [],
            "注册表存在 ERROR 级不一致：\n" +
            "\n".join(f"[{e['code']}] {e['message']}" for e in errors))

    def test_required_modules_are_declared(self):
        """任务书要求覆盖的 11 个业务/平台模块全部登记。"""
        required = {
            "accommodation": "预订与入住", "guests": "客人", "rooms": "房态与库存",
            "memberships": "会员", "staff": "员工", "finance": "财务",
            "night_audit": "夜审", "fnb": "餐饮", "parking": "停车",
            "legacy": "历史只读", "platform": "权限管理（平台）",
        }
        for code, name_zh in required.items():
            module = MODULE_REGISTRY_V2.get(code)
            self.assertIsNotNone(module, f"缺少模块登记：{code}")
            self.assertEqual(module["name_zh"], name_zh)
            self.assertIn(module["status"], {"ACTIVE", "PLANNED", "RETIRED"})
            for field in ("code", "name_zh", "app_label", "status",
                          "route_prefix", "landing_url", "risk_level",
                          "property_scoped", "actions"):
                self.assertIn(field, module, f"模块 {code} 缺少字段 {field}")

    def test_active_module_codes_resolve_to_real_permissions(self):
        """ACTIVE 模块登记的每个权限码都能解析到 Django Permission 表。"""
        missing = []
        for module in MODULE_REGISTRY_V2.values():
            if module["status"] != MODULE_ACTIVE:
                continue
            for code in iter_module_permission_codes(module):
                app_label, codename = code.split(".", 1)
                if not Permission.objects.filter(
                        content_type__app_label=app_label,
                        codename=codename).exists():
                    missing.append(f"{module['code']}: {code}")
        self.assertEqual(missing, [], "ACTIVE 模块存在未解析权限码：" + "；".join(missing))

    def test_module_codes_traceable_to_phase0_audit_docs(self):
        """注册表权限码与 Phase 0 审计文档（11/12 号报告）逐点交叉核对。"""
        audit_text = _phase0_audit_text()
        untraceable = []
        for module in MODULE_REGISTRY_V2.values():
            for code in iter_module_permission_codes(module):
                codename = code.split(".", 1)[1]
                if codename not in audit_text:
                    untraceable.append(code)
        for item in SPECIAL_PERMISSIONS:
            codename = item["codename"].split(".", 1)[1]
            if codename not in audit_text:
                untraceable.append(item["codename"])
        self.assertEqual(
            untraceable, [],
            "以下权限码在 Phase 0 审计文档中无出处：" + "；".join(untraceable))

    def test_special_tuple_and_special_permissions_are_bidirectionally_consistent(self):
        """模块 special 元组 ⊆ SPECIAL_PERMISSIONS 且 module 双向指回。"""
        for module in MODULE_REGISTRY_V2.values():
            special = module["actions"]["special"]
            if special == NOT_APPLICABLE:
                continue
            for code in special:
                item = get_special(code)
                self.assertIsNotNone(
                    item, f"模块 {module['code']} 的 special 引用了未登记专项权限：{code}")
                self.assertEqual(
                    item["module"], module["code"],
                    f"专项权限 {code} 的 module 归属与模块 {module['code']} 不一致")
        for item in SPECIAL_PERMISSIONS:
            module = MODULE_REGISTRY_V2.get(item["module"])
            self.assertIsNotNone(module, f"专项权限 {item['key']} 模块未登记")
            special = module["actions"]["special"]
            self.assertIn(item["codename"], special,
                          f"专项权限 {item['key']} 未列入模块 {item['module']} 的 special 元组")

    def test_unmapped_report_flags_django_builtin_and_known_orphan(self):
        """未映射权限点盘点必须暴露 Django 内建码与已知孤儿点（F-07）。"""
        findings = validate_registry()
        unmapped = [f for f in findings if f["code"] == "unmapped_permissions"]
        self.assertEqual(len(unmapped), 1, "未映射权限点盘点必须恰好报告一条 INFO")
        message = unmapped[0]["message"]
        self.assertIn("guests.document_reveal", message,
                      "孤儿权限点 guests.document_reveal 应出现在未映射盘点中")
        self.assertIn("auth.add_user", message,
                      "Django 内建权限应出现在未映射盘点中")

    def test_planned_modules_do_not_break_error_free_validation(self):
        """PLANNED/RETIRED 模块的存量授权只产生 WARNING 基线，不产生 ERROR。"""
        findings = validate_registry()
        planned_warnings = [f for f in findings if f["code"] == "planned_module_granted"]
        for warning in planned_warnings:
            self.assertIn("历史授予基线", warning["message"])


class PlannedModuleGrantGuardTests(TestCase):
    """伪造 POST 对 PLANNED 模块（餐饮/停车）权限的授权防线（F-09 兜底）。

    矩阵页 UI 对 reserved 模块置灰，但服务端白名单 ALL_CODENAMES 仍含其
    权限码——伪造 POST 可绕过 UI 直接授权。Phase 2 不改矩阵 UI/白名单，
    因此本测试断言防线二：注册表校验必须能发现这类授权漂移。
    """

    FORGED_PERMS = ["add_food", "delete_park"]  # fnb / parking（均 PLANNED）

    def setUp(self):
        self.admin = User.objects.create_superuser(
            "registry_admin", password="admin-pass123456")
        self.client.force_login(self.admin)
        # 矩阵页属平台会话域（admin_sessionid），与 SplitSession 约定一致
        self.client.cookies["admin_sessionid"] = self.client.cookies["sessionid"].value
        self.group = Group.objects.get(name="客房服务员")

    def _held_hotelapp_codenames(self):
        return set(self.group.permissions.filter(
            content_type__app_label="hotelapp").values_list("codename", flat=True))

    def test_forged_post_is_rejected_or_flagged_by_registry(self):
        response = self.client.post("/platform/permissions/save/", {
            "role": str(self.group.id),
            "perm": self.FORGED_PERMS,
        })
        self.assertEqual(response.status_code, 302)
        held = self._held_hotelapp_codenames()
        slipped = {code for code in self.FORGED_PERMS if code in held}

        findings = validate_registry()
        if slipped:
            # 矩阵白名单放行了 PLANNED 模块权限码 → 注册表必须发现漂移
            flagged = {w["message"] for w in findings
                       if w["code"] == "planned_module_granted"}
            for code in slipped:
                self.assertTrue(
                    any(code in message for message in flagged),
                    f"伪造授权的 PLANNED 权限码 {code} 未被注册表校验发现")
        else:
            # 若白名单拒绝（Phase 3 修复后的形态）→ 直接断言拒绝生效
            self.assertEqual(slipped & set(self.FORGED_PERMS), set())
        # 无论哪个分支，保存动作本身不得制造 ERROR 级不一致
        self.assertEqual(registry_errors(findings), [])

    def test_forged_post_with_out_of_whitelist_code_is_still_blocked(self):
        """白名单外权限码（跨 App 专项码）仍被矩阵保存拒绝。"""
        self.client.post("/platform/permissions/save/", {
            "role": str(self.group.id),
            "perm": ["identity.staff_manage", "billing.refund_approve"],
        })
        held_any_app = set(self.group.permissions.values_list(
            "content_type__app_label", "codename"))
        self.assertNotIn(("identity", "staff_manage"), held_any_app)
        self.assertNotIn(("billing", "refund_approve"), held_any_app)
        self.assertEqual(registry_errors(validate_registry()), [])

    def test_planned_module_grant_baseline_is_pinned(self):
        """干净库上的 PLANNED 授权基线：仅历史 view/管理侧授予，可逐条追溯。"""
        findings = validate_registry()
        flagged_codes = set()
        for warning in findings:
            if warning["code"] == "planned_module_granted":
                match = re.search(r"权限码 (\S+) 仍被组持有", warning["message"])
                self.assertIsNotNone(match, warning["message"])
                flagged_codes.add(match.group(1))
        # 基线内的 PLANNED 授权码必须属于注册表登记的 PLANNED 模块权限面
        planned_codes = set()
        for module in MODULE_REGISTRY_V2.values():
            if module["status"] == MODULE_PLANNED:
                planned_codes.update(iter_module_permission_codes(module))
        self.assertTrue(flagged_codes, "干净库上应存在已记录的 PLANNED 授权基线")
        self.assertFalse(
            flagged_codes - planned_codes,
            "PLANNED 授权基线出现注册表未登记的权限码")


class ModuleGateRegressionTests(TestCase):
    """注册表接管后 module_gate 行为不变：food/order/park 仍隐藏。"""

    def test_views_module_registry_matches_legacy_flags(self):
        from hotelapp.views import MODULE_REGISTRY

        self.assertEqual(
            MODULE_REGISTRY, {"food": False, "order": False, "park": False},
            "hotelapp.views.MODULE_REGISTRY 必须等价于原硬编码开关")

    def test_module_gate_open_semantics(self):
        self.assertFalse(module_gate_open("food"))
        self.assertFalse(module_gate_open("order"))
        self.assertFalse(module_gate_open("park"))
        # 未登记门禁键默认开放（与旧 MODULE_REGISTRY.get(key, True) 一致）
        self.assertTrue(module_gate_open("vip"))
        self.assertEqual(set(legacy_module_gate_keys()), {"food", "order", "park"})

    def test_planned_modules_declared_in_registry(self):
        self.assertEqual(MODULE_REGISTRY_V2["fnb"]["status"], MODULE_PLANNED)
        self.assertEqual(MODULE_REGISTRY_V2["parking"]["status"], MODULE_PLANNED)
        self.assertEqual(MODULE_REGISTRY_V2["accommodation"]["status"], MODULE_ACTIVE)

    def _make_employed_staff(self, username="registry-gate-user"):
        """DEFAULT 物业在职 + 前台授权的员工（过 staff_required 门）。"""
        user = User.objects.create_user(username=username, password="gate-pass-123")
        prop, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "默认测试酒店"})
        profile = StaffProfile.objects.create(
            user=user, full_name="注册表门禁测试员工", employment_status="ACTIVE")
        PropertyStaffMembership.objects.create(
            property=prop, staff_profile=profile,
            employee_no=f"E-{username}", status="ACTIVE")
        StaffRoleAssignment.objects.create(
            membership=PropertyStaffMembership.objects.get(
                property=prop, staff_profile=profile),
            group=Group.objects.get(name="前台"), starts_at=timezone.now())
        return user

    def test_hidden_module_pages_stay_redirecting_for_employed_staff(self):
        user = self._make_employed_staff()
        self.client.force_login(user)
        for url in ("/show_food/", "/show_park/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"{url} 应被 module_gate 隐藏")
            self.assertEqual(response["Location"], "/home/")
        # 对照组：在营模块同一员工可正常进入（前台持 view_room）
        self.assertEqual(self.client.get("/show_room/").status_code, 200)

    def test_hidden_module_export_stay_redirecting_for_employed_staff(self):
        user = self._make_employed_staff(username="registry-gate-export")
        self.client.force_login(user)
        response = self.client.get("/export/food/")
        self.assertEqual(response.status_code, 302, "隐藏模块导出必须被开关拦截")
        self.assertEqual(response["Location"], "/home/")


class RoleRegistryTests(TestCase):
    """ROLE_REGISTRY 与数据库九组一一对应（12-roles.md 快照）。"""

    def test_role_registry_covers_business_role_directory(self):
        self.assertEqual({r["name"] for r in ROLE_REGISTRY},
                         set(BUSINESS_ROLE_NAMES))
        self.assertEqual(len(ROLE_REGISTRY), 9)

    def test_role_registry_matches_database_groups_one_to_one(self):
        names = [r["group_name"] for r in ROLE_REGISTRY]
        found = set(Group.objects.filter(name__in=names).values_list("name", flat=True))
        self.assertEqual(found, set(names),
                         "ROLE_REGISTRY 与数据库业务组必须一一对应")
        self.assertEqual(len(found), 9)

    def test_role_status_split_is_five_active_and_four_planned(self):
        active = [r["name"] for r in ROLE_REGISTRY if r["status"] == ROLE_ACTIVE]
        planned = [r["name"] for r in ROLE_REGISTRY if r["status"] == ROLE_PLANNED]
        self.assertEqual(set(active), {"前台", "值班经理", "财务", "店长", "客房服务员"})
        self.assertEqual(set(planned), {"夜审员", "餐饮服务员", "餐饮主管", "停车安保"})

    def test_active_roles_hold_permissions_and_planned_roles_are_empty(self):
        for role in ROLE_REGISTRY:
            group = Group.objects.get(name=role["group_name"])
            count = group.permissions.count()
            if role["status"] == ROLE_ACTIVE:
                self.assertGreater(count, 0, f"现役角色 {role['name']} 不应为空组")
            else:
                self.assertEqual(count, 0,
                                 f"预留角色 {role['name']} 应为空组（0014 决策 5）")

    def test_role_records_are_well_formed(self):
        for role in ROLE_REGISTRY:
            self.assertEqual(role["name"], role["group_name"],
                             "业务组目录以中文名建组，name 与 group_name 一致")
            self.assertTrue(role["builtin"], "九角色均为内建角色")
            self.assertTrue(role["editable"], "九角色均可经矩阵页编辑")
            self.assertIn(role["risk_level"], {"low", "medium", "high"})


class SpecialPermissionRegistryTests(TestCase):
    """SPECIAL_PERMISSIONS 每项解析到真实 Permission（任务书验收线 4）。"""

    def test_every_special_permission_resolves_to_real_permission(self):
        missing = []
        for item in SPECIAL_PERMISSIONS:
            app_label, codename = item["codename"].split(".", 1)
            if not Permission.objects.filter(
                    content_type__app_label=app_label,
                    codename=codename).exists():
                missing.append(f"{item['key']}（{item['codename']}）")
        self.assertEqual(missing, [], "专项权限未解析到真实 Permission：" + "；".join(missing))

    def test_task_required_special_keys_are_present(self):
        required = {
            "guest_document_reveal", "guest_phone_reveal", "refund_request",
            "refund_approve", "reverse_entry", "night_audit_execute",
            "business_date_reopen", "breakglass_review", "export_pii",
        }
        keys = {item["key"] for item in SPECIAL_PERMISSIONS}
        self.assertTrue(required.issubset(keys),
                        f"缺少任务书要求的专项权限：{required - keys}")

    def test_special_permission_flags_are_well_formed(self):
        for item in SPECIAL_PERMISSIONS:
            self.assertIn(item["risk"], {"low", "medium", "high"})
            self.assertIsInstance(item["requires_reason"], bool)
            self.assertIsInstance(item["requires_confirm"], bool)
            self.assertIn(item["module"], MODULE_REGISTRY_V2)
        # 运行时强制填原因的点必须与登记一致（views.client_reveal_id/
        # vip_reveal_tel 校验原因≥8字符；夜审执行是例行日结，不要求原因）
        reason_enforced = {item["key"] for item in SPECIAL_PERMISSIONS
                           if item["requires_reason"]}
        self.assertTrue({"guest_document_reveal", "guest_phone_reveal",
                         "business_date_reopen", "breakglass_review"}
                        .issubset(reason_enforced))

    def test_reverse_entry_maps_to_reverse_checkout_documenting_f08(self):
        """reverse_entry（红字冲销/反结账）唯一落点是 hotelapp.reverse_checkout。

        F-08：该权限唯一检查点已随 /uncheckout/ 410 退役；注册表登记此处，
        待 Phase 4 front-desk 补反结账 API 时以此授权。
        """
        item = get_special("reverse_entry")
        self.assertIsNotNone(item)
        self.assertEqual(item["codename"], "hotelapp.reverse_checkout")
        self.assertEqual(item["module"], "accommodation")
        self.assertTrue(Permission.objects.filter(
            content_type__app_label="hotelapp",
            codename="reverse_checkout").exists())

    def test_cross_app_special_permissions_cover_f09_scope(self):
        """矩阵外跨 App 专项点（F-09）必须全部登记在 SPECIAL_PERMISSIONS。"""
        codenames = {item["codename"] for item in SPECIAL_PERMISSIONS}
        for code in ("billing.refund_request", "billing.refund_approve",
                     "core.breakglass_review", "identity.staff_manage",
                     "inventory.manage_rates"):
            self.assertIn(code, codenames, f"矩阵外权限 {code} 必须登记入注册表")
