"""权限管理中心测试（方案 §8.6）。

覆盖：
  * 读写 API 四态门禁（未登录 / 仅 PMS 会话 / 非超管平台会话 / 超管）；
  * CSRF 缺失 403、携带令牌成功、GET 打保存端点 405；
  * 未知/PLANNED/注册表外权限码 → 409 整体拒绝 + security 审计（N-1 修复）；
  * 内容 hash 并发保护（旧 version 409 / 同 version 并发第二次 409）；
  * 高危变更确认令牌（缺失 403 / 过期 403 / diff 变化后重放 403）；
  * 正常保存 → Group.permissions 实际变化 + AuthorizationService 一致 +
    AuditEvent before/after 完整；
  * reason 缺失/超长 400、影响范围计数与 StaffRoleAssignment 一致、
    非注册表权限保留、PLANNED 历史基线随保存回收、页面文案与骨架。
"""
import hashlib
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client, TestCase
from django.utils import timezone

from apps.core.models import AuditEvent, Property

from .authorization import AuthorizationService
from .models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from .permission_center import (
    CONFIRM_TOKEN_MAX_AGE,
    allowed_codenames,
    managed_universe_codenames,
    special_codenames,
)
from .registry import validate_registry

User = get_user_model()

SCOPE_NOTICE = "平台超管拥有平台域管理权限；PMS 业务权限仍以员工任职和岗位授权为准"
CSRF_MARKER = 'name="csrfmiddlewaretoken" value="'


def _held(group):
    """组当前权限码全集（"app_label.codename"）。"""
    return {f"{a}.{c}" for a, c in group.permissions.values_list(
        "content_type__app_label", "codename")}


class PermissionCenterTestBase(TestCase):
    """公共夹具：平台超管登录 + 默认角色组。"""

    def setUp(self):
        self.superadmin = User.objects.create_superuser(
            "pc_admin", password="admin-pass123456")
        self.client = Client()
        self._platform_login(self.client, self.superadmin)
        self.group = Group.objects.get(name="客房服务员")

    @staticmethod
    def _platform_login(client, user):
        """平台会域登录：force_login 后把会话键复制到两份平台 Cookie（§B.1）。"""
        client.force_login(user)
        sessionid = client.cookies["sessionid"].value
        client.cookies["platform_sessionid"] = sessionid
        client.cookies["django_admin_sessionid"] = sessionid

    def _read(self, group=None):
        group = group or self.group
        response = self.client.get(f"/platform/api/permissions/{group.id}/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _save(self, group=None, permissions=None, version=None,
              reason="测试矩阵保存", **extra):
        group = group or self.group
        if version is None:
            version = self._read(group)["version"]
        if permissions is None:
            permissions = sorted(_held(group) & allowed_codenames())
        payload = {"permissions": sorted(permissions), "version": version,
                   "reason": reason}
        payload.update(extra)
        return self.client.post(
            f"/platform/api/permissions/{group.id}/save/",
            data=json.dumps(payload), content_type="application/json")

    def _confirm(self, group=None, permissions=None):
        group = group or self.group
        if permissions is None:
            permissions = sorted(_held(group) & allowed_codenames())
        return self.client.post(
            f"/platform/api/permissions/{group.id}/confirm-token/",
            data=json.dumps({"permissions": sorted(permissions)}),
            content_type="application/json")

    @staticmethod
    def _baseline(group_held):
        """历史 PLANNED/RETIRED 授权基线（保存时随声明式矩阵回收的码）。"""
        return sorted((set(group_held) - allowed_codenames())
                      & managed_universe_codenames())


class PermissionCenterReadApiTests(PermissionCenterTestBase):
    """读取 API：四态门禁 + 载荷结构。"""

    def test_read_anonymous_redirects_to_admin_login(self):
        client = Client()
        response = client.get(f"/platform/api/permissions/{self.group.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/admin/login/"))

    def test_read_pms_session_only_redirects(self):
        """仅 PMS 会话（员工登录，无平台 Cookie）→ 302，不因员工登录放行。"""
        staff = User.objects.create_user("pc_staff", password="staff-pass123")
        client = Client()
        client.force_login(staff)  # 只写 sessionid
        response = client.get(f"/platform/api/permissions/{self.group.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/admin/login/"))

    def test_read_staff_non_superuser_platform_session_403(self):
        staff = User.objects.create_user("pc_clerk", password="staff-pass123")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        client = Client()
        self._platform_login(client, staff)
        response = client.get(f"/platform/api/permissions/{self.group.id}/")
        self.assertEqual(response.status_code, 403)

    def test_read_superadmin_payload_shape_and_version_hash(self):
        data = self._read()
        current = _held(self.group)
        self.assertEqual(data["role"]["name"], self.group.name)
        self.assertEqual(data["role"]["status"], "ACTIVE")
        self.assertTrue(data["role"]["editable"])
        self.assertEqual(set(data["permissions"]), current)
        expected = hashlib.sha256(
            "\n".join(sorted(current)).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(data["version"], expected)
        self.assertEqual(set(data["impact"]), {"staff_count", "membership_count"})
        self.assertEqual(set(data["special"]), current & special_codenames())
        self.assertEqual(data["reclaimed_on_save"], self._baseline(current))
        self.assertIn("last_save", data)

    def test_read_unregistered_group_404(self):
        stray = Group.objects.create(name="pc-临时组")
        response = self.client.get(f"/platform/api/permissions/{stray.id}/")
        self.assertEqual(response.status_code, 404)
        response = self.client.get("/platform/api/permissions/999999/")
        self.assertEqual(response.status_code, 404)


class PermissionCenterWriteGuardTests(PermissionCenterTestBase):
    """保存 API：门禁四态、CSRF、方法与请求体校验。"""

    def setUp(self):
        super().setUp()
        self.save_url = f"/platform/api/permissions/{self.group.id}/save/"

    def _payload(self, version=None):
        return json.dumps({
            "permissions": sorted(_held(self.group) & allowed_codenames()),
            "version": version or self._read()["version"],
            "reason": "测试矩阵保存",
        })

    def test_save_anonymous_and_pms_and_staff_denied(self):
        held = _held(self.group)
        # 匿名 → 302
        client = Client()
        response = client.post(self.save_url, data=self._payload(),
                               content_type="application/json")
        self.assertEqual(response.status_code, 302)
        # 仅 PMS 会话 → 302
        staff = User.objects.create_user("pc_w1", password="staff-pass123")
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.post(
            self.save_url, data=self._payload(),
            content_type="application/json").status_code, 302)
        # 非超管平台会话 → 403
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        client = Client()
        self._platform_login(client, staff)
        self.assertEqual(client.post(
            self.save_url, data=self._payload(),
            content_type="application/json").status_code, 403)
        # 三态请求均不得变更授权，也不得落 matrix_saved 审计
        self.assertEqual(_held(self.group), held)
        self.assertFalse(AuditEvent.objects.filter(
            action="permissions.matrix_saved").exists())

    def test_save_without_csrf_403(self):
        client = Client(enforce_csrf_checks=True)
        self._platform_login(client, self.superadmin)
        response = client.post(self.save_url, data=self._payload(),
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AuditEvent.objects.filter(
            action="permissions.matrix_saved").exists())

    def test_save_with_csrf_header_succeeds(self):
        """携带页面渲染的 CSRF 令牌（X-CSRFToken 头）→ 保存成功。"""
        client = Client(enforce_csrf_checks=True)
        self._platform_login(client, self.superadmin)
        page = client.get("/platform/permissions/")
        token = page.content.decode("utf-8").split(CSRF_MARKER, 1)[1].split('"', 1)[0]
        response = client.post(
            self.save_url, data=self._payload(),
            content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["saved"])

    def test_save_via_get_405_and_read_via_post_405(self):
        self.assertEqual(self.client.get(self.save_url).status_code, 405)
        self.assertEqual(
            self.client.post(
                f"/platform/api/permissions/{self.group.id}/").status_code, 405)

    def test_save_invalid_json_400(self):
        response = self.client.post(self.save_url, data="{not-json",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "bad_json")

    def test_save_missing_reason_400(self):
        payload = json.dumps({
            "permissions": sorted(_held(self.group) & allowed_codenames()),
            "version": self._read()["version"],
        })
        response = self.client.post(self.save_url, data=payload,
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "reason_required")
        # 空白理由同样 400
        response = self._save(reason="   ")
        self.assertEqual(response.status_code, 400)

    def test_save_too_long_reason_400(self):
        response = self._save(reason="理" * 241)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "reason_too_long")

    def test_save_missing_version_400(self):
        response = self._save(version="")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "version_required")


class PermissionCenterForbiddenCodeTests(PermissionCenterTestBase):
    """越权码整体拒绝（N-1 修复验证）：PLANNED/未知/注册表外码一律 409。"""

    def _assert_rejected(self, codes, held_before, expected_unknown=None):
        response = self._save(permissions=codes)
        self.assertEqual(response.status_code, 409, response.content)
        data = response.json()
        self.assertEqual(data["code"], "unknown_permissions")
        self.assertEqual(set(data["unknown"]),
                         set(expected_unknown if expected_unknown is not None
                             else codes))
        # 整体拒绝：不静默过滤，合法码也不得被授予
        self.assertEqual(_held(self.group), held_before)
        # security 审计必须留痕
        event = AuditEvent.objects.filter(
            action="security.permission_denied",
            entity_type="Group", entity_id=str(self.group.id)).first()
        self.assertIsNotNone(event, "越权码拒绝必须落 security.permission_denied")
        self.assertIsNotNone(event.actor)

    def test_planned_module_codes_rejected_409(self):
        """N-1 修复：PLANNED 模块（fnb/parking）权限码不可再经保存端点授予。"""
        self._assert_rejected(
            ["hotelapp.add_food", "hotelapp.delete_park"], _held(self.group))

    def test_bare_codename_without_app_label_rejected_409(self):
        """旧矩阵接受裸 codename；新 API 要求完整 app_label.codename。"""
        self._assert_rejected(["add_food", "delete_park"], _held(self.group))

    def test_mixed_known_and_unknown_rejected_without_partial_grant(self):
        """合法码与未知码混提 → 整体 409，合法码不得落库（不静默过滤）。"""
        # add_vip 合法且该组未持有；no_such_perm 未知
        self._assert_rejected(
            ["hotelapp.add_vip", "hotelapp.no_such_perm"],
            _held(self.group),
            expected_unknown={"hotelapp.no_such_perm"})

    def test_registry_outside_cross_app_code_rejected_409(self):
        """注册表未登记的跨 App 码（如 Django 内建 auth.*）→ 409。"""
        self._assert_rejected(["auth.add_user"], _held(self.group))


class PermissionCenterVersionTests(PermissionCenterTestBase):
    """内容 hash 并发保护。"""

    def test_stale_version_conflict_409(self):
        version = self._read()["version"]
        # 模拟并发：他人先移除了组持有的权限（版本内容随之变化）
        perm = Permission.objects.get(
            content_type__app_label="hotelapp", codename="view_room")
        self.assertIn("hotelapp.view_room", _held(self.group))
        self.group.permissions.remove(perm)
        response = self._save(version=version)
        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertEqual(data["code"], "version_conflict")
        self.assertNotEqual(data["current_version"], version)
        # 拒绝后授权保持并发修改后的状态（不被旧快照覆盖）
        self.assertNotIn("hotelapp.view_room", _held(self.group))

    def test_same_version_second_save_conflict_409(self):
        """同一 version 两次保存：第一次成功，第二次 409。"""
        version = self._read()["version"]
        non_special = allowed_codenames() - special_codenames()
        add_code = sorted(non_special - _held(self.group))[0]
        first = self._save(version=version,
                           permissions=sorted(
                               (_held(self.group) & allowed_codenames())
                               | {add_code}))
        self.assertEqual(first.status_code, 200, first.content)
        self.assertNotEqual(first.json()["version"], version,
                            "首次保存真实变更后版本必须推进")
        second = self._save(version=version)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "version_conflict")


class PermissionCenterConfirmTokenTests(PermissionCenterTestBase):
    """高危变更（触及 SPECIAL_PERMISSIONS）二次确认。"""

    SPECIAL_CODE = "billing.refund_approve"  # finance 模块高危审批权

    def _proposed_with_special(self):
        return sorted((_held(self.group) & allowed_codenames())
                      | {self.SPECIAL_CODE})

    def test_high_risk_change_without_token_403(self):
        response = self._save(permissions=self._proposed_with_special())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "token_required")
        self.assertNotIn(self.SPECIAL_CODE, _held(self.group))
        self.assertFalse(AuditEvent.objects.filter(
            action="permissions.matrix_saved").exists())

    def test_confirm_token_endpoint_returns_diff_and_token(self):
        held = _held(self.group)
        response = self._confirm(permissions=self._proposed_with_special())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["high_risk"])
        self.assertEqual(data["added"], [self.SPECIAL_CODE])
        self.assertEqual(data["removed"], self._baseline(held))
        self.assertEqual(data["expires_in"], CONFIRM_TOKEN_MAX_AGE)
        self.assertIn("token", data)
        self.assertIn("diff_hash", data)

    def test_confirm_token_no_changes_400(self):
        held = sorted(_held(self.group) & allowed_codenames())
        response = self._confirm(permissions=held)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "no_changes")

    def test_high_risk_change_with_token_saves_and_audits(self):
        held_before = _held(self.group)
        token_payload = self._confirm(
            permissions=self._proposed_with_special()).json()
        response = self._save(permissions=self._proposed_with_special(),
                              confirm_token=token_payload["token"])
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["saved"])
        self.assertEqual(data["added"], [self.SPECIAL_CODE])
        self.assertEqual(data["removed"], self._baseline(held_before))
        held_after = _held(self.group)
        self.assertIn(self.SPECIAL_CODE, held_after)
        event = AuditEvent.objects.filter(
            action="permissions.matrix_saved",
            entity_type="Group", entity_id=str(self.group.id)).first()
        self.assertIsNotNone(event, "保存必须与审计同事务落库")
        self.assertEqual(set(event.before_json), held_before)
        self.assertEqual(set(event.after_json), held_after)
        self.assertEqual(event.reason, "测试矩阵保存")
        self.assertEqual(event.actor, self.superadmin)

    def test_expired_token_403(self):
        """过期 token（超 10 分钟有效期）→ 403 token_expired，且不落任何变更。"""
        token = self._confirm(
            permissions=self._proposed_with_special()).json()["token"]
        held = _held(self.group)
        with mock.patch("apps.identity.permission_center.CONFIRM_TOKEN_MAX_AGE",
                        -1):
            response = self._save(permissions=self._proposed_with_special(),
                                  confirm_token=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "token_expired")
        self.assertEqual(_held(self.group), held)

    def test_replayed_token_after_diff_change_403(self):
        """重放：token 绑定 diff hash——保存成功后再以同 token 提交反向变更
        （移除同一专项码）→ diff 变化后 hash 不匹配 → 403 token_invalid。"""
        token = self._confirm(
            permissions=self._proposed_with_special()).json()["token"]
        first = self._save(permissions=self._proposed_with_special(),
                           confirm_token=token)
        self.assertEqual(first.status_code, 200)
        held_now = _held(self.group)
        # 反向变更：移除刚授予的专项码（again 高危），重放旧 token
        without_special = sorted(held_now - {self.SPECIAL_CODE})
        replay = self._save(permissions=without_special, confirm_token=token)
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(replay.json()["code"], "token_invalid")
        self.assertEqual(_held(self.group), held_now, "重放请求不得变更授权")

    def test_token_issued_for_other_group_rejected_403(self):
        """token 的 salt 绑定 group_id：跨组重用 → 403。"""
        token = self._confirm(
            permissions=self._proposed_with_special()).json()["token"]
        other = Group.objects.get(name="前台")
        other_proposed = sorted((_held(other) & allowed_codenames())
                                | {self.SPECIAL_CODE})
        response = self._save(group=other, permissions=other_proposed,
                              confirm_token=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "token_invalid")
        self.assertNotIn(self.SPECIAL_CODE, _held(other))


class PermissionCenterSaveEffectTests(PermissionCenterTestBase):
    """正常保存：授权实际变化 + 授权服务一致 + 审计完整 + 基线回收。"""

    def setUp(self):
        super().setUp()
        self.prop, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "测试酒店"})

    def _make_employed_staff(self, username="pc_holder"):
        user = User.objects.create_user(username, password=f"{username}-pass123")
        profile = StaffProfile.objects.create(
            user=user, full_name="权限中心测试员工", employment_status="ACTIVE")
        membership = PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=profile,
            employee_no=f"E-{username}", status="ACTIVE")
        StaffRoleAssignment.objects.create(
            membership=membership, group=self.group, starts_at=timezone.now())
        return user

    def test_save_updates_permissions_and_authorization_service(self):
        user = self._make_employed_staff()
        held = _held(self.group)
        non_special = allowed_codenames() - special_codenames()
        add_code = sorted(non_special - held)[0]
        self.assertFalse(
            AuthorizationService.has_permission(user, self.prop, add_code))
        proposed = sorted((held & allowed_codenames()) | {add_code})
        response = self._save(permissions=proposed)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["added"], [add_code])
        self.assertEqual(data["removed"], self._baseline(held))
        self.group.refresh_from_db()
        app_label, codename = add_code.split(".", 1)
        self.assertTrue(self.group.permissions.filter(
            content_type__app_label=app_label, codename=codename).exists())
        # 授权服务判定与矩阵一致（运行时逻辑未被改动，授权经 StaffRoleAssignment 生效）
        self.assertTrue(AuthorizationService.has_permission(user, self.prop, add_code))
        # 审计 before/after 为完整码表
        event = AuditEvent.objects.get(
            action="permissions.matrix_saved", entity_type="Group",
            entity_id=str(self.group.id))
        self.assertEqual(set(event.before_json), held)
        self.assertEqual(set(event.after_json), held | {add_code})

    def test_save_removes_permission_and_service_stops_allowing(self):
        user = self._make_employed_staff()
        held = _held(self.group)
        held_allowed = held & allowed_codenames()
        self.assertTrue(held_allowed, "客房服务员应持有在营模块权限")
        victim = sorted(held_allowed)[0]
        self.assertTrue(AuthorizationService.has_permission(user, self.prop, victim))
        response = self._save(permissions=sorted(held_allowed - {victim}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"],
                         sorted({victim} | set(self._baseline(held))))
        self.assertFalse(
            AuthorizationService.has_permission(user, self.prop, victim))
        event = AuditEvent.objects.get(
            action="permissions.matrix_saved", entity_type="Group",
            entity_id=str(self.group.id))
        self.assertIn(victim, set(event.before_json))
        self.assertNotIn(victim, set(event.after_json))

    def test_save_preserves_non_registry_permissions(self):
        """注册表管辖外权限（Django Admin 手工授予的 auth.*）原样保留。"""
        builtin = Permission.objects.get(
            content_type__app_label="auth", codename="add_user")
        self.group.permissions.add(builtin)
        held = _held(self.group)
        response = self._save(
            permissions=sorted(held & allowed_codenames()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_held(self.group), held, "管辖外权限不得被矩阵保存清空")
        self.assertEqual(response.json()["removed"], self._baseline(held))

    def test_save_reclaims_planned_baseline_and_registry_warning_clears(self):
        """历史 PLANNED 授权基线随保存显式回收（出现在 removed 与审计 after）。"""
        planned_perm = Permission.objects.get(
            content_type__app_label="hotelapp", codename="add_food")
        self.group.permissions.add(planned_perm)  # 模拟历史授予基线
        held = _held(self.group)
        self.assertIn("hotelapp.add_food", held)
        # 读取 API 必须预告回收清单（不静默）
        data = self._read()
        self.assertIn("hotelapp.add_food", data["reclaimed_on_save"])
        response = self._save(permissions=sorted(held & allowed_codenames()))
        self.assertEqual(response.status_code, 200)
        self.assertIn("hotelapp.add_food", response.json()["removed"])
        self.assertNotIn("hotelapp.add_food", _held(self.group))
        # 注册表校验不再对该码产生 PLANNED 授权告警
        warnings = [w["message"] for w in validate_registry()
                    if w["code"] == "planned_module_granted"]
        self.assertFalse(
            any("add_food" in message and self.group.name in message
                for message in warnings),
            "回收后 PLANNED 基线告警应消失：\n" + "\n".join(warnings))

    def test_impact_counts_match_staff_role_assignment(self):
        data = self._read()
        self.assertEqual(data["impact"]["staff_count"], 0)
        self.assertEqual(data["impact"]["membership_count"], 0)

        user = self._make_employed_staff("pc_impact1")
        data = self._read()
        self.assertEqual(data["impact"]["staff_count"], 1)
        self.assertEqual(data["impact"]["membership_count"], 1)

        # 同一员工第二家酒店任职（同角色）→ 任职数 2、员工数仍 1
        prop2 = Property.objects.create(code="P2", name="第二酒店")
        profile = StaffProfile.objects.get(user=user)
        membership2 = PropertyStaffMembership.objects.create(
            property=prop2, staff_profile=profile,
            employee_no="E-P2-1", status="ACTIVE")
        StaffRoleAssignment.objects.create(
            membership=membership2, group=self.group, starts_at=timezone.now())
        data = self._read()
        self.assertEqual(data["impact"]["staff_count"], 1)
        self.assertEqual(data["impact"]["membership_count"], 2)

        # 授权过期（ends_at 已过）→ 不再计入影响范围
        assignment = StaffRoleAssignment.objects.get(membership=membership2)
        assignment.ends_at = timezone.now() - timezone.timedelta(hours=1)
        assignment.starts_at = timezone.now() - timezone.timedelta(hours=2)
        assignment.save()
        data = self._read()
        self.assertEqual(data["impact"]["membership_count"], 1)


class PermissionCenterPageTests(PermissionCenterTestBase):
    """四区页面：§8.1 固定文案 + 注册表 bootstrap + CSRF 令牌渲染。"""

    def test_page_renders_scope_notice_and_bootstrap(self):
        response = self.client.get("/platform/permissions/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn(SCOPE_NOTICE, content, "页面必须呈现 §8.1 边界原文")
        self.assertIn('id="pc-bootstrap"', content, "必须内嵌注册表 bootstrap JSON")
        self.assertIn('id="pc-role-list"', content)
        self.assertIn('id="pc-matrix-box"', content)
        self.assertIn('id="pc-impact-box"', content)
        self.assertIn('id="pc-diff-summary"', content)
        self.assertIn(CSRF_MARKER, content, "CSRF_USE_SESSIONS 下 JS 依赖 DOM 令牌")
        self.assertIn("permission_center.js", content)

    def test_page_bootstrap_carries_registry_matrix(self):
        """bootstrap JSON 必须携带注册表矩阵结构（11 模块 + 17 专项 + 9 角色）。"""
        import json as jsonlib
        import re

        response = self.client.get("/platform/permissions/")
        content = response.content.decode("utf-8")
        match = re.search(
            r'<script id="pc-bootstrap" type="application/json">(.*?)</script>',
            content, re.S)
        self.assertIsNotNone(match, "必须内嵌 pc-bootstrap JSON")
        data = jsonlib.loads(match.group(1))
        self.assertEqual(len(data["modules"]), 11)
        self.assertEqual(len(data["roles"]), 9)
            # 既有 14 个专项权限 + 夜审查看、清洁查看/流转、维修管理 3 个专项权限
        self.assertEqual(len(data["special_codenames"]), 17)
        module_codes = {m["code"] for m in data["modules"]}
        self.assertEqual(
            module_codes,
            {"accommodation", "guests", "rooms", "memberships", "staff",
             "finance", "night_audit", "fnb", "parking", "legacy", "platform"})
        # PLANNED/RETIRED 模块在 bootstrap 中原样标记（前端置灰依据）
        self.assertEqual({m["code"]: m["status"] for m in data["modules"]}["fnb"],
                         "PLANNED")
        # 矩阵列与专项码均来自注册表 ACTIVE 面 + 专项清单
        cell_codes = {col["code"] for m in data["modules"]
                      for col in m["columns"] if col["code"]}
        # 夜审只读入口使用 core.night_audit_view；高危执行权仍单独列在专项权限，
        # 不得把“查看夜审”与“执行夜审”合并成同一个矩阵单元格权限。
        self.assertIn("core.night_audit_view", cell_codes)
        self.assertIn("hotelapp.night_audit_execute", data["special_codenames"])
        self.assertIn("billing.refund_approve", data["special_codenames"])

    def test_legacy_form_save_endpoint_redirects_without_saving(self):
        """旧 form 保存入口已废弃：302 跳回矩阵页且不做任何授权变更。"""
        held = _held(self.group)
        response = self.client.post("/platform/permissions/save/", {
            "role": str(self.group.id), "perm": ["view_room", "add_food"]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/platform/permissions/")
        self.assertEqual(_held(self.group), held, "旧入口不得再变更授权")
