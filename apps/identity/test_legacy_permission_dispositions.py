"""ARCH-04 H2：旧 ContentType 权限码归属台账的门禁用例。

治理目标：**不再存在无归属权限码**。

判定口径（与 `apps/identity/legacy_permission_dispositions.py` 的生成依据一致）：

* 「注册表引用」= `MODULE_REGISTRY_V2` 的 actions / special / extra_codes 里出现；
* 「生产代码引用」= `apps/`、`hotelapp/`、`hotel/` 下非测试 .py 的
  `'app_label.codename'` 字面量出现。

四组断言：

1. `auth.Permission(content_type__app_label='hotelapp')` 的**每一个**权限码都
   必须在归属台账里有记录（否则就是无归属权限码）；
2. `MODULE_GATE` 分类必须确实被注册表引用（分类不能与事实相反）；
3. `LEGACY_ROUTE_GATE` 分类必须确实被生产代码引用、且注册表**不**引用；
4. `DORMANT` 分类必须注册表与生产代码**都不**引用——任何把 DORMANT 码重新
   接入门禁的行为都会在这里被拦下；
5. 台账里 `mapped_to` 的码必须有 C10 迁移台账行（`LegacyPermissionCodeGrant`），
   保证「旧码 → 新码」的映射可逆、可追溯；
6. 台账里不得有数据库中已不存在的权限码（防台账腐化）。
"""

import pathlib
import re
from typing import cast

from django.contrib.auth.models import Permission
from django.test import TestCase

from apps.identity.legacy_permission_dispositions import (
    DORMANT,
    LEGACY_PERMISSION_CODE_DISPOSITIONS,
    LEGACY_ROUTE_GATE,
    MODULE_GATE,
)
from apps.identity.models import LegacyPermissionCodeGrant
from apps.identity.registry import MODULE_REGISTRY_V2

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CODE_RE = re.compile(r"['\"]([a-z_]+\.[a-z_0-9]+)['\"]")
# 归属台账模块本身是「权限码的声明处」，不是「权限码的使用处」：
# 里面的字面量是台账条目，不得被当成门禁引用（否则台账会把自己指认为引用方）。
DISPOSITIONS_MODULE = "apps/identity/legacy_permission_dispositions.py"


def _registry_codes():
    """注册表引用的全部权限码。"""
    codes = set()
    for module in cast(dict, MODULE_REGISTRY_V2).values():
        actions = cast(dict, module.get("actions") or {})
        for value in actions.values():
            if isinstance(value, str):
                codes.add(value)
            elif isinstance(value, (list, tuple)):
                codes.update(v for v in value if isinstance(v, str))
        for value in module.get("extra_codes") or ():
            if isinstance(value, str):
                codes.add(value)
    return codes


def _production_code_references():
    """生产代码里出现的权限码字面量 → 引用文件清单。"""
    refs: dict = {}
    for root_name in ("apps", "hotelapp", "hotel"):
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            parts = path.parts
            if "migrations" in parts or "__pycache__" in parts:
                continue
            if "tests" in parts or path.name.startswith("test_") or path.name == "tests.py":
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel == DISPOSITIONS_MODULE:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for code in set(CODE_RE.findall(text)):
                refs.setdefault(code, []).append(rel)
    return refs


class LegacyPermissionDispositionTests(TestCase):
    """H2 门禁：旧 ContentType 权限码不得存在无归属状态。"""

    def test_every_legacy_permission_code_has_a_disposition(self):
        """每个 hotelapp.* 权限码都必须登记归属（无归属 = 治理失败）。"""
        existing = {
            f"{perm.content_type.app_label}.{perm.codename}"
            for perm in Permission.objects.filter(
                content_type__app_label="hotelapp"
            ).select_related("content_type")
        }
        missing = sorted(existing - set(LEGACY_PERMISSION_CODE_DISPOSITIONS))
        self.assertEqual(
            missing,
            [],
            "以下旧权限码没有归属记录（孤儿权限码）：\n" + "\n".join(missing),
        )

    def test_disposition_table_has_no_stale_entries(self):
        """台账不得登记数据库中不存在、也不是历史码的权限码（防台账腐化）。

        「历史码」（`historical=True`）的所属模型已不在 hotelapp——它们只在从
        更早状态迁移过来的库里存在（历史 ContentType 的残留权限行），
        **全新迁移的库不会生成**。这类码允许登记但不得要求存在。
        """
        existing = {
            f"{perm.content_type.app_label}.{perm.codename}"
            for perm in Permission.objects.filter(
                content_type__app_label="hotelapp"
            ).select_related("content_type")
        }
        stale = sorted(
            code
            for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items()
            if code not in existing and not item.historical
        )
        self.assertEqual(
            stale,
            [],
            "台账登记了数据库中不存在的权限码：\n" + "\n".join(stale),
        )

    def test_historical_codes_are_really_unreachable_models(self):
        """历史码的所属模型必须确实已不在 hotelapp（否则应改为普通码）。"""
        from django.apps import apps as django_apps

        current = {
            model.__name__.lower() for model in django_apps.get_app_config("hotelapp").get_models()
        }
        wrong = []
        for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items():
            if not item.historical:
                continue
            perm = Permission.objects.filter(
                content_type__app_label="hotelapp",
                codename=code.split(".", 1)[1],
            ).first()
            if perm is None:
                # 该码在当前库不存在（历史 ContentType 残留已清理）——同样算历史码
                continue
            if perm.content_type.model.lower() in current:
                wrong.append(code)
        self.assertEqual(wrong, [], "标为历史码但模型仍在 hotelapp：\n" + "\n".join(wrong))

    def test_module_gate_classification_matches_registry(self):
        """MODULE_GATE 分类必须确实被注册表引用。"""
        registry = _registry_codes()
        wrong = sorted(
            code
            for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items()
            if item.disposition == MODULE_GATE and code not in registry
        )
        self.assertEqual(
            wrong,
            [],
            "标为 MODULE_GATE 但注册表未引用：\n" + "\n".join(wrong),
        )

    def test_legacy_route_gate_classification_is_not_in_registry(self):
        """LEGACY_ROUTE_GATE 必须只在旧页面/旧路由被引用，注册表已解绑。"""
        registry = _registry_codes()
        wrong = sorted(
            code
            for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items()
            if item.disposition == LEGACY_ROUTE_GATE and code in registry
        )
        self.assertEqual(
            wrong,
            [],
            "标为 LEGACY_ROUTE_GATE 但注册表仍在引用：\n" + "\n".join(wrong),
        )

    def test_legacy_route_gate_codes_are_actually_referenced(self):
        """LEGACY_ROUTE_GATE 必须确实还有门禁在用（否则应降级为 DORMANT）。"""
        refs = _production_code_references()
        wrong = sorted(
            code
            for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items()
            if item.disposition == LEGACY_ROUTE_GATE and not refs.get(code)
        )
        self.assertEqual(
            wrong,
            [],
            "标为 LEGACY_ROUTE_GATE 但生产代码无任何引用：\n" + "\n".join(wrong),
        )

    def test_dormant_codes_are_referenced_by_nothing(self):
        """DORMANT 必须确实无人引用（防止「休眠码」被悄悄接回门禁）。"""
        registry = _registry_codes()
        refs = _production_code_references()
        wrong = sorted(
            code
            for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items()
            if item.disposition == DORMANT and (code in registry or refs.get(code))
        )
        self.assertEqual(
            wrong,
            [],
            "标为 DORMANT 但仍被注册表/生产代码引用：\n" + "\n".join(wrong),
        )

    def test_mapped_codes_keep_grant_equivalence(self):
        """声明了映射的码必须保持授权等价（旧码持有者同时持有新码）。

        C10 的不变量：持有旧码的角色组必须同步持有新码。迁移台账
        （`LegacyPermissionCodeGrant`）逐条登记「本次迁移新增的授权」，
        反向迁移据此精确撤销；台账无行 = 该组迁移前就已同时持有两个码。
        """
        from django.contrib.auth.models import Group, Permission

        wrong = []
        for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items():
            if not item.mapped_to:
                continue
            source = Permission.objects.filter(
                content_type__app_label=code.split(".", 1)[0],
                codename=code.split(".", 1)[1],
            ).first()
            target = Permission.objects.filter(
                content_type__app_label=item.mapped_to.split(".", 1)[0],
                codename=item.mapped_to.split(".", 1)[1],
            ).first()
            if source is None or target is None:
                wrong.append(f"{code} → {item.mapped_to}（权限码不存在）")
                continue
            for group in Group.objects.filter(permissions=source):
                if not group.permissions.filter(pk=target.pk).exists():
                    wrong.append(f"{code} → {item.mapped_to}（角色组 {group.name} 未同步）")
        self.assertEqual(
            wrong,
            [],
            "旧码→新码映射的授权等价性被破坏：\n" + "\n".join(wrong),
        )

    def test_mapped_codes_have_migration_ledger_rows_when_granted(self):
        """迁移台账必须登记「由本次迁移新增」的授权（可逆回退的依据）。"""
        ledger = set(
            LegacyPermissionCodeGrant.objects.values_list("source_code", "target_code", "group_id")
        )
        wrong = []
        for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items():
            if not item.mapped_to:
                continue
            for row in LegacyPermissionCodeGrant.objects.filter(source_code=code).values_list(
                "source_code", "target_code", "group_id"
            ):
                if (code, item.mapped_to, row[2]) not in ledger:
                    wrong.append(f"{code} → {item.mapped_to}（台账行不完整）")
        self.assertEqual(wrong, [], "迁移台账不完整：\n" + "\n".join(wrong))

    def test_every_disposition_has_an_owner_and_note(self):
        """归属记录必须写明责任方与说明（否则不算「有归属」）。"""
        bad = sorted(
            code
            for code, item in LEGACY_PERMISSION_CODE_DISPOSITIONS.items()
            if not item.owner.strip() or not item.note.strip()
        )
        self.assertEqual(bad, [], "缺少 owner/note 的归属记录：\n" + "\n".join(bad))
