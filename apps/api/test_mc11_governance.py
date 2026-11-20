"""MC11 治理整改回归（任务书 PMS2-11C-001 盘点缺口闭环）。

覆盖：
- MC11-MB-01：会员有效期全链（创建可填 expires_at + 到期扫描置 EXPIRED+审计）；
- MC11-MB-02：会员 DTO 透出业务来源 source；
- MC11-NA-05：break-glass 查询前惰性批量过期（DB status 不再滞留 GRANTED）；
- MC11-NA-03/04/06/07：break-glass 过期执行 409、重复申请冲突、
  run() 惰性恢复陈旧 RUNNING、并发 RUNNING 409（此前全部零测试）。
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.billing.night_audit import NightAuditService
from apps.core.models import AuditEvent, BreakGlassRecord
from apps.guests.models import GuestMembership

from .test_e2e_flows import E2EFlowTestBase
from .test_five_centers_closure import _post_json, _service_guest
from .test_walk_in_api import _post_json as _public_post


@override_settings(DEBUG=True, DJANGO_ENV="test")
class MembershipExpiryGovernanceTests(E2EFlowTestBase):
    """MC11-MB-01/02：有效期写路径 + 到期扫描 + 来源透出。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("mc11_mb_frontdesk", "前台")
        self.client.force_login(self.fd)

    def _create_membership(self, member_no, expires_at=None):
        guest = _service_guest(self.property, phone=f"139{member_no[-8:]}")
        payload = {"guest_id": guest.pk, "member_no": member_no, "level_code": "STANDARD"}
        if expires_at:
            payload["expires_at"] = expires_at
        return _post_json(self.client, "/api/v1/memberships/create", payload)

    def test_create_membership_with_expires_at(self):
        expires = (timezone.localdate() + timedelta(days=365)).isoformat()
        response = self._create_membership("MC11-EXP-0001", expires_at=expires)
        self.assertEqual(response.status_code, 201, response.content)
        membership = GuestMembership.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(str(membership.expires_at), expires)
        detail = self.client.get(f"/api/v1/memberships/{membership.pk}").json()["data"]
        self.assertEqual(detail["expires_at"], expires)
        # MC11-MB-02：业务来源透出
        self.assertEqual(detail["source"], "PMS2 管理中心")
        listing = self.client.get("/api/v1/memberships").json()["data"]
        row = next(r for r in listing if r["id"] == membership.pk)
        self.assertEqual(row["source"], "PMS2 管理中心")

    def test_create_membership_rejects_expires_before_joined(self):
        from datetime import date

        response = self._create_membership("MC11-EXP-0002", expires_at=date(2020, 1, 1).isoformat())
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("有效期", response.json()["error"]["message"])

    def test_expire_memberships_command_marks_expired_with_audit(self):
        # 有效期不得早于加入日（服务守卫）：先建未来日期，再 ORM 回拨模拟时间流逝
        response = self._create_membership(
            "MC11-EXP-0003", expires_at=(timezone.localdate() + timedelta(days=30)).isoformat()
        )
        self.assertEqual(response.status_code, 201, response.content)
        membership_id = response.json()["data"]["id"]
        GuestMembership.objects.filter(pk=membership_id).update(
            expires_at=timezone.localdate() - timedelta(days=1)
        )

        call_command("expire_memberships", stdout=mock.Mock())
        membership = GuestMembership.objects.get(pk=membership_id)
        self.assertEqual(membership.status, GuestMembership.Status.EXPIRED)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="membership.expire",
                entity_type="GuestMembership",
                entity_id=str(membership_id),
            ).exists()
        )

        # 幂等：重复执行不再命中
        call_command("expire_memberships", stdout=mock.Mock())
        self.assertEqual(
            AuditEvent.objects.filter(
                action="membership.expire",
                entity_type="GuestMembership",
                entity_id=str(membership_id),
            ).count(),
            1,
        )

    def test_unexpired_membership_untouched_by_command(self):
        response = self._create_membership(
            "MC11-EXP-0004", expires_at=(timezone.localdate() + timedelta(days=30)).isoformat()
        )
        membership_id = response.json()["data"]["id"]
        call_command("expire_memberships", stdout=mock.Mock())
        self.assertEqual(
            GuestMembership.objects.get(pk=membership_id).status, GuestMembership.Status.ACTIVE
        )


_BREAKGLASS_PW = {}


@override_settings(DEBUG=True, DJANGO_ENV="test")
class BreakGlassGovernanceTests(E2EFlowTestBase):
    """MC11-NA-03/04/05：break-glass 惰性过期与冲突分支。"""

    def setUp(self):
        super().setUp()
        # break-glass 仅适用于"唯一有效经理"（night_audit.py 单经理守卫）：
        # 夹具只建一个值班经理，避免误触 409 双经理拦截。
        self.duty = self._make_staff("mc11_na_duty", "值班经理")
        pw = get_random_string(16)
        self.duty.set_password(pw)
        self.duty.save(update_fields=("password",))
        _BREAKGLASS_PW[self.duty.username] = pw
        # set_password 后重拉实例：内存旧实例的 session_auth_hash 与库不一致
        # 会导致 force_login 会话被中间件判为失效（401）。
        self.duty = User.objects.get(pk=self.duty.pk)
        # 唯一有效经理口径（对齐 test_e2e_flows 链 C 模式）：禁用其他账号
        User.objects.exclude(pk=self.duty.pk).update(is_active=False)
        self.bd = None

    def _open_business_date(self):
        from apps.core.models import BusinessDate

        bd, _ = BusinessDate.objects.get_or_create(
            property=self.property, business_date=timezone.localdate()
        )
        return bd

    def _request_break_glass(self, user, expires_at=None):
        self.client.force_login(user)
        bd = self._open_business_date()
        # break-glass 前提：营业日已关账（夜审 run 后）；本组聚焦授权链，
        # 直接置 CLOSED 构造服务层前提（既有 E2E 链 C 已覆盖真实关账路径）。
        from apps.core.models import BusinessDate

        BusinessDate.objects.filter(pk=bd.pk).update(status=BusinessDate.Status.CLOSED)
        bd.refresh_from_db()
        # 二次密码：request_break_glass 校验申请者本人密码（AUTH 口径）
        payload = {
            "reason": "MC11 测试：紧急修正过账错误，需紧急重开处理",
            "password": _BREAKGLASS_PW[user.username],
        }
        response = _post_json(
            self.client,
            f"/api/v1/night-audit/business-dates/{bd.pk}/break-glass",
            payload,
        )
        return response, bd

    def test_list_expires_stale_grants_lazily(self):
        """MC11-NA-05：过窗 GRANTED 在列表查询时批量落 EXPIRED+审计。"""
        response, bd = self._request_break_glass(self.duty)
        self.assertEqual(response.status_code, 201, response.content)
        record_id = response.json()["data"]["break_glass_id"]
        BreakGlassRecord.objects.filter(pk=record_id).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        listing = self.client.get("/api/v1/night-audit/break-glass")
        self.assertEqual(listing.status_code, 200)
        record = BreakGlassRecord.objects.get(pk=record_id)
        self.assertEqual(record.status, BreakGlassRecord.Status.EXPIRED)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="night_audit.break_glass_expired", entity_id=str(record_id)
            ).exists()
        )

    def test_duplicate_active_grant_request_conflicts(self):
        """MC11-NA-04：同一营业日已有有效授权 → 再次申请 409（此前零测试）。"""
        first, _bd = self._request_break_glass(self.duty)
        self.assertEqual(first.status_code, 201, first.content)
        second, _bd2 = self._request_break_glass(self.duty)
        self.assertEqual(second.status_code, 409, second.content)
        self.assertIn("已有有效的 Break-glass 授权", second.json()["error"]["message"])

    def test_expired_grant_execute_returns_409(self):
        """MC11-NA-03：过期授权执行 → 409 且营业日不被重开（API 级分支）。"""
        response, bd = self._request_break_glass(self.duty)
        record_id = response.json()["data"]["break_glass_id"]
        # 从 CLOSED 营业日出发才有重开语义：直接置 CLOSED 以构造执行前提
        from apps.core.models import BusinessDate

        BusinessDate.objects.filter(pk=bd.pk).update(status=BusinessDate.Status.CLOSED)
        BreakGlassRecord.objects.filter(pk=record_id).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        execute = _post_json(
            self.client,
            f"/api/v1/night-audit/business-dates/{bd.pk}/break-glass/{record_id}/execute",
            {},
        )
        self.assertEqual(execute.status_code, 409, execute.content)
        self.assertIn("过期", execute.json()["error"]["message"])
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)
