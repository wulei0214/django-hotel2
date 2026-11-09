"""§7 跨 App RBAC 注册表测试（对应 docs/audit/phase0/25-rbac-registry.md）。

覆盖四条验收线：
  1. validate_registry() 无 ERROR——ACTIVE 模块权限码全部解析到真实 Permission，
     并与 Phase 0 审计文档（11-permissions.md / 12-roles.md）交叉核对；
  2. PLANNED 模块（餐饮/停车）权限经伪造 POST 授权：Phase 3 起新保存 API
     整体 409 拒绝（N-1 白名单已移除），旧 form 入口废弃为兼容跳转；
  3. ROLE_REGISTRY 与数据库九组一一对应（5 现役 + 4 预留空组）；
  4. SPECIAL_PERMISSIONS 每项解析到真实 Permission。
另含 module_gate 行为回归：food/order/park 仍隐藏（注册表接管后行为不变）。
"""
import hashlib
import json
import os
import re

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone

from apps.core.models import AuditEvent, Property
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

_PHASE0_DOCS = ("11-permissions.md", "12-roles.md", "20-findings-register.md")


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
    """伪造 POST 对 PLANNED 模块（餐饮/停车）权限的授权防线（F-09/N-1）。

    Phase 3 权限中心重构后的形态：旧 permission_admin.ALL_CODENAMES 白名单
    （含 PLANNED 模块 food/order/park 12 点）已从代码路径上移除，保存唯一
    通道为 POST /platform/api/permissions/<id>/save/，其 allowed 面 =
    注册表 ACTIVE 模块码 ∪ SPECIAL_PERMISSIONS——PLANNED/RETIRED 模块码
    一律整体 409 拒绝 + security.permission_denied 审计，不再存在
    "白名单放行 → 靠注册表告警兜底"的分支。
    """

    # fnb / parking 模块权限码（均 PLANNED，完整 app_label.codename 形态）
    FORGED_PERMS = ["hotelapp.add_food", "hotelapp.delete_park"]

    def setUp(self):
        self.admin = User.objects.create_superuser(
            "registry_admin", password="admin-pass123456")
        self.client.force_login(self.admin)
        # 矩阵页属平台会话域（platform_sessionid @ /platform/），与 SplitSession §B.1 一致
        self.client.cookies["platform_sessionid"] = self.client.cookies["sessionid"].value
        self.group = Group.objects.get(name="客房服务员")

    def _held_hotelapp_codenames(self):
        return set(self.group.permissions.filter(
            content_type__app_label="hotelapp").values_list("codename", flat=True))

    def _current_version(self):
        held = {f"{app}.{code}" for app, code in self.group.permissions.values_list(
            "content_type__app_label", "codename")}
        return hashlib.sha256(
            "\n".join(sorted(held)).encode("utf-8")).hexdigest()[:16]

    def test_forged_planned_codes_rejected_by_save_api(self):
        """伪造提交 PLANNED 模块码 → 保存 API 整体 409，授权与注册表零漂移。"""
        payload = json.dumps({
            "permissions": self.FORGED_PERMS,
            "version": self._current_version(),
            "reason": "伪造授权演练（N-1 防线验证）",
        })
        response = self.client.post(
            f"/platform/api/permissions/{self.group.id}/save/",
            data=payload, content_type="application/json")
        self.assertEqual(response.status_code, 409, response.content)
        data = response.json()
        self.assertEqual(data["code"], "unknown_permissions")
        self.assertEqual(set(data["unknown"]), set(self.FORGED_PERMS))
        held = self._held_hotelapp_codenames()
        for code in self.FORGED_PERMS:
            self.assertNotIn(code.split(".", 1)[1], held,
                             f"PLANNED 权限码 {code} 不得被授予")
        # 越权码拒绝必须落 security 审计（不静默过滤）
        self.assertTrue(AuditEvent.objects.filter(
            action="security.permission_denied",
            entity_type="Group", entity_id=str(self.group.id)).exists())
        # 保存动作本身不得制造 ERROR 级不一致
        self.assertEqual(registry_errors(validate_registry()), [])

    def test_forged_post_with_old_form_save_endpoint_changes_nothing(self):
        """旧 form 保存入口已废弃为兼容跳转：POST 它不再执行任何授权变更。"""
        held_before = set(self.group.permissions.values_list(
            "content_type__app_label", "codename"))
        response = self.client.post("/platform/permissions/save/", {
            "role": str(self.group.id),
            "perm": ["add_food", "delete_park"],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/platform/permissions/")
        held_after = set(self.group.permissions.values_list(
            "content_type__app_label", "codename"))
        self.assertEqual(held_before, held_after, "废弃入口不得变更授权")
        self.assertEqual(registry_errors(validate_registry()), [])

    def test_cross_app_special_codes_now_grantable_via_center(self):
        """注册表内跨 App 专项码（F-09 收敛）：经高危确认流程可正常授予并留审计。"""
        proposed = ["identity.staff_manage", "billing.refund_approve"]
        confirm = self.client.post(
            f"/platform/api/permissions/{self.group.id}/confirm-token/",
            data=json.dumps({"permissions": proposed}),
            content_type="application/json")
        self.assertEqual(confirm.status_code, 200, confirm.content)
        token = confirm.json()["token"]
        payload = json.dumps({
            "permissions": proposed,
            "version": self._current_version(),
            "reason": "F-09 收敛：跨 App 专项权经注册表面授予",
            "confirm_token": token,
        })
        response = self.client.post(
            f"/platform/api/permissions/{self.group.id}/save/",
            data=payload, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.group.permissions.filter(
            content_type__app_label="identity", codename="staff_manage").exists())
        self.assertTrue(self.group.permissions.filter(
            content_type__app_label="billing", codename="refund_approve").exists())
        self.assertTrue(AuditEvent.objects.filter(
            action="permissions.matrix_saved",
            entity_type="Group", entity_id=str(self.group.id)).exists())
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


class MaintenancePermissionGrantTests(TestCase):
    """维修权限不得继承未到处理权限；只允许值班经理与店长持有。"""

    def test_maintenance_manage_grants_are_independent_and_minimal(self):
        permission = Permission.objects.get(
            content_type__app_label="inventory",
            content_type__model="room",
            codename="maintenance_manage",
        )
        holders = set(Group.objects.filter(
            permissions=permission).values_list("name", flat=True))
        self.assertEqual(holders, {"值班经理", "店长"})

        # 维修权限与未到处理是两个独立动作；维修授权不能被复制给其他岗位。
        no_show = Permission.objects.filter(
            content_type__app_label="hotelapp",
            codename="no_show_handle",
        ).first()
        self.assertIsNotNone(no_show)
        self.assertTrue(Group.objects.filter(permissions=no_show).exists())
        self.assertEqual(
            set(Group.objects.filter(permissions=permission)
                .values_list("name", flat=True)),
            {"值班经理", "店长"},
        )


class ReversalPermissionGrantTests(TestCase):
    """冲销执行权与审批权必须分离到财务和管理岗位。"""

    def test_finance_executes_and_managers_only_approve_reversal(self):
        execute_permission = Permission.objects.get(
            content_type__app_label="billing",
            content_type__model="ledgerentry",
            codename="reverse_entry",
        )
        approve_permission = Permission.objects.get(
            content_type__app_label="billing",
            content_type__model="ledgerentry",
            codename="reversal_approve",
        )
        execute_holders = set(Group.objects.filter(
            permissions=execute_permission).values_list("name", flat=True))
        approve_holders = set(Group.objects.filter(
            permissions=approve_permission).values_list("name", flat=True))
        self.assertEqual(execute_holders, {"财务"})
        self.assertEqual(approve_holders, {"值班经理", "店长"})
        self.assertFalse(Group.objects.filter(
            name__in=("值班经理", "店长"),
            permissions=execute_permission).exists())
        self.assertFalse(Group.objects.filter(
            name="财务", permissions=approve_permission).exists())


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
        # 旧客房入口必须经过原权限校验后跳转 PMS2 房态与库存中心。
        room_response = self.client.get("/show_room/")
        self.assertEqual(room_response.status_code, 302)
        self.assertEqual(room_response["Location"], "/manage/rooms/")

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
            "refund_approve", "night_audit_execute",
            "business_date_reopen", "breakglass_review", "export_pii",
            # F-08 补执行点（基线 2.0 §4.3）：room_release_ready 恢复登记
            "room_release_ready",
        }  # P0-04：reverse_entry（红字冲销）仍保持授权面移除
        keys = {item["key"] for item in SPECIAL_PERMISSIONS}
        self.assertTrue(required.issubset(keys),
                        f"缺少专项权限：{required - keys}")

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

    def test_reverse_entry_retired_from_authorization_surface(self):
        """P0-04：reverse_entry（hotelapp.reverse_checkout）保持授权面移除。

        权限点本体与历史组授权保留；Phase 4 补 API 时以新权限码重新登记。
        F-08 补执行点（基线 2.0 §4.3）：room_release_ready 已恢复登记——
        清洁与维修工作台净房放行为其合法执行点，归属 rooms 模块。
        """
        from apps.identity.registry import SPECIAL_PERMISSIONS
        keys = {item["key"] for item in SPECIAL_PERMISSIONS}
        self.assertNotIn("reverse_entry", keys)
        # room_release_ready 恢复登记：module 指回 rooms 且列入模块 special 元组
        self.assertIn("room_release_ready", keys)
        item = next(x for x in SPECIAL_PERMISSIONS
                    if x["key"] == "room_release_ready")
        self.assertEqual(item["codename"], "hotelapp.room_release_ready")
        self.assertEqual(item["module"], "rooms")
        rooms = MODULE_REGISTRY_V2["rooms"]
        self.assertIn("hotelapp.room_release_ready", rooms["actions"]["special"])
        for module in MODULE_REGISTRY_V2.values():
            specials = module.get("actions", {}).get("special") or ()
            if specials == NOT_APPLICABLE:
                continue
            self.assertNotIn(
                "hotelapp.reverse_checkout", specials,
                "reverse_checkout 已按 P0-04 从授权面移除")

    def test_cross_app_special_permissions_cover_f09_scope(self):
        """矩阵外跨 App 专项点（F-09）必须全部登记在 SPECIAL_PERMISSIONS。"""
        codenames = {item["codename"] for item in SPECIAL_PERMISSIONS}
        for code in ("billing.refund_request", "billing.refund_approve",
                     "core.breakglass_review", "identity.staff_manage",
                     "inventory.manage_rates"):
            self.assertIn(code, codenames, f"矩阵外权限 {code} 必须登记入注册表")
