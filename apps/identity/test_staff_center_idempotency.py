"""TASK-07 员工中心写路由幂等键契约测试（MC11-ST-02，V3.0 任务书第一批）。

盘点缺口 ST-02：set_status/assign_role/end_assignment/update_profile 四条写
路由此前未挂 @idempotent，网络重试双写审计。本轮补挂装饰器（与会员 API 同
口径：Idempotency-Key + 请求体 hash，同 key 同 body 重放返回首响应）。

锁定契约：
* 重放同 key 同 body → 返回首次响应（响应体逐字节一致），审计不双写；
* 同 key 不同 body → 409 BUSINESS_CONFLICT（装饰器既有语义）；
* 不带 Idempotency-Key → 透传执行（旧行为兼容，装饰器可选语义）。
"""

from django.test import TestCase

from apps.core.models import AuditEvent, IdempotencyRecord

from .test_staff_center import _make_target_employee, _make_user


class StaffCenterIdempotencyTests(TestCase):
    """四条写路由 @idempotent 契约（ST-02 关闭条件）。"""

    @classmethod
    def setUpTestData(cls):
        cls.owner = _make_user("sc_idem_owner")
        profile, _membership, _user = _make_target_employee("idem-target")
        cls.target = profile

    def setUp(self):
        self.client.force_login(self.owner)
        self.profile = self.target

    def test_update_profile_replay_returns_first_response_without_double_audit(self):
        url = f"/manage/staff/actions/{self.profile.pk}/update_profile/"
        payload = {"full_name": "幂等重放员工", "phone": "13811112222", "reason": "幂等契约测试更新"}
        first = self.client.post(
            url, payload, HTTP_IDEMPOTENCY_KEY="sc-idem-up-001"
        )
        self.assertEqual(first.status_code, 200, first.content)
        audits_after_first = AuditEvent.objects.filter(
            entity_id=str(self.profile.pk),
            action__contains="profile_update",
        ).count()

        replay = self.client.post(
            url, payload, HTTP_IDEMPOTENCY_KEY="sc-idem-up-001"
        )
        self.assertEqual(replay.status_code, 200, replay.content)
        # 重放返回首响应（同 key 同 body → 首次响应体）
        self.assertEqual(replay.json(), first.json())
        # 审计不双写
        self.assertEqual(
            AuditEvent.objects.filter(
                entity_id=str(self.profile.pk), action__contains="profile_update"
            ).count(),
            audits_after_first,
        )
        # 幂等记录恰好一条
        self.assertEqual(
            IdempotencyRecord.objects.filter(key="sc-idem-up-001").count(), 1
        )

    def test_set_status_replay_is_idempotent_single_record(self):
        url = f"/manage/staff/actions/{self.profile.pk}/set_status/"
        payload = {"status": "SUSPENDED", "reason": "幂等契约测试暂停"}
        first = self.client.post(url, payload, HTTP_IDEMPOTENCY_KEY="sc-idem-st-001")
        self.assertEqual(first.status_code, 200, first.content)
        replay = self.client.post(url, payload, HTTP_IDEMPOTENCY_KEY="sc-idem-st-001")
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(IdempotencyRecord.objects.filter(key="sc-idem-st-001").count(), 1)

    def test_same_key_different_body_conflicts(self):
        url = f"/manage/staff/actions/{self.profile.pk}/update_profile/"
        first = self.client.post(
            url, {"full_name": "冲突测试A", "reason": "冲突契约测试A"}, HTTP_IDEMPOTENCY_KEY="sc-idem-cx-001"
        )
        self.assertEqual(first.status_code, 200, first.content)
        conflict = self.client.post(
            url, {"full_name": "冲突测试B", "reason": "冲突契约测试B"}, HTTP_IDEMPOTENCY_KEY="sc-idem-cx-001"
        )
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertEqual(conflict.json()["error"]["code"], "BUSINESS_CONFLICT")

    def test_without_key_passthrough_unchanged(self):
        """不带 Idempotency-Key：透传执行（装饰器可选语义，不破坏旧调用方）。"""
        url = f"/manage/staff/actions/{self.profile.pk}/update_profile/"
        response = self.client.post(url, {"full_name": "无键透传员工", "reason": "无键透传测试"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(
            IdempotencyRecord.objects.filter(key="").exists()
        )
