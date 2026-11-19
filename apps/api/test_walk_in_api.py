"""A1 Walk-in 旧端点退役行为测试（R2-04，DEC-WI-01 收敛收口）。

原兼容期 HTTP 契约测试（DEC-WALKIN-01：复用 create_walk_in 原子链路、无手机号
占位、幂等重放、认证/权限/物业门禁等 201/400/401/403 断言）已于 2026-11-19
随 410 退役转化为退役行为测试：POST /api/v1/front-desk/walk-in 恒返
410 Gone（error.code=GONE，文案注明已按 DEC-WI-01 退役并指向标准入口
POST /api/v1/front-desk/walk-ins），且不产生任何业务写入——
Reservation/Guest/Stay/Payment/Folio 计数不变、无 frontdesk.legacy_walk_in_called
观测审计新写、无 IdempotencyRecord 幂等记录新写。

每项测试保留原场景入参（匿名 / 仅查看权 / 缺幂等键 / 缺字段 / 带手机号 /
无净房 / 同幂等键重放等），以证明退役对任意调用形态一律生效、不设任何分支。
业务能力由 POST /api/v1/front-desk/walk-ins 承接（服务层事实源
apps.reservations.walkin.create_walk_in 不变，行为覆盖见
apps/reservations/test_whitebox_services.py 及本包 walk-ins 相关用例）；
本文件不触碰服务层实现。
"""

import contextlib
import json
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import Folio, Payment
from apps.core.models import AuditEvent, IdempotencyRecord
from apps.guests.models import Guest
from apps.guests.services import phone_hash
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory
from apps.reservations.models import Reservation, Stay

from .test_whitebox_api import WhiteboxApiTestBase


def _post_json(client, url, payload, **extra):
    return client.post(url, data=json.dumps(payload), content_type="application/json", **extra)


class WalkInApiTestBase(WhiteboxApiTestBase):
    """复用 DEFAULT 物业上下文 + 员工账号工厂，叠加 Walk-in 库存。

    库存/房价夹具保留原契约测试口径：即使库存充足、入参合法，退役端点也
    必须零写入（410 先于任何业务分支）。
    """

    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.room_type = RoomType.objects.create(
            property=self.property, code="WBI-KING", name="散客房型", capacity=2
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="WBI-BAR",
            name="散客门市价",
            valid_from=today - timedelta(days=1),
        )
        self.room = Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="WBI-8101",
            floor="8",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )
        DailyRate.objects.create(
            property=self.property,
            rate_plan=self.rate_plan,
            room_type=self.room_type,
            stay_date=today,
            price=Decimal("300.00"),
        )
        RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type, stay_date=today, total_rooms=3
        )

    def _auth(self, codenames=("view_accommodation", "change_accommodation")):
        user = self._make_staff(codenames=set(codenames))
        self.client.force_login(user)
        # 沿用原契约测试夹具：预开班次（退役路径不读取任何业务上下文，仅保证
        # "已授权 + 班次就绪" 场景下退役行为与其他场景一致）。
        from apps.billing.services_v2 import BillingService

        with contextlib.suppress(Exception):
            # 该用户已有进行中班次则沿用
            BillingService.open_shift(
                property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
            )
        return user

    def _payload(self, **overrides):
        payload = {
            "full_name": "散客张三",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "nights": 1,
            "adults": 1,
            "children": 0,
        }
        payload.update(overrides)
        return payload

    def _snapshot_counts(self):
        """业务写入面快照：退役调用前后必须逐项不变。"""
        return {
            "reservation": Reservation.objects.count(),
            "guest": Guest.objects.count(),
            "stay": Stay.objects.count(),
            "payment": Payment.objects.count(),
            "folio": Folio.objects.count(),
            "audit_total": AuditEvent.objects.count(),
            "legacy_observation_audit": AuditEvent.objects.filter(
                action="frontdesk.legacy_walk_in_called"
            ).count(),
            "idempotency_record": IdempotencyRecord.objects.count(),
        }

    def _assert_retired(self, response, before):
        """统一退役断言：410 + GONE + 指向标准入口 + 零业务写入（含观测审计）。"""
        self.assertEqual(response.status_code, 410, response.content)
        body = response.json()
        self.assertEqual(body["error"]["code"], "GONE")
        self.assertIn("DEC-WI-01", body["error"]["message"])
        self.assertIn("/api/v1/front-desk/walk-ins", body["error"]["message"])
        self.assertEqual(
            self._snapshot_counts(),
            before,
            "退役端点不得产生任何业务/审计/幂等写入",
        )


class WalkInCreateApiTests(WalkInApiTestBase):
    """POST /api/v1/front-desk/walk-in 退役行为（R2-04，DEC-WI-01）。

    类名保留原契约测试语义（Walk-in 创建端点 API 行为），全部用例断言
    恒定 410 + 零写入；与原 201/400/401/403 契约测试一一对应。
    """

    url = "/api/v1/front-desk/walk-in"

    def test_walk_in_retired_records_no_observation_audit(self):
        """原 R2-03 观测断言转"无观测审计新写"：410 路径不再记
        frontdesk.legacy_walk_in_called（观察期随退役一并结束）。"""
        self._auth()
        before = self._snapshot_counts()
        response = _post_json(
            self.client, self.url, self._payload(), HTTP_IDEMPOTENCY_KEY="wbi-obs-001"
        )
        self._assert_retired(response, before)
        self.assertEqual(
            AuditEvent.objects.filter(action="frontdesk.legacy_walk_in_called").count(), 0
        )

    def test_walk_in_retired_valid_payload_returns_410_without_writes(self):
        """原"无手机号散客 201"场景：合法入参 + 库存充足仍 410，零业务写入。"""
        self._auth()
        before = self._snapshot_counts()
        response = _post_json(
            self.client, self.url, self._payload(), HTTP_IDEMPOTENCY_KEY="wbi-nophone-001"
        )
        self._assert_retired(response, before)
        self.assertEqual(Reservation.objects.filter(source="WALK_IN").count(), 0)

    def test_walk_in_retired_idempotent_key_replay_returns_410(self):
        """原"同幂等键重放"场景：重放两次均 410，不建档、不写幂等记录。"""
        self._auth()
        before = self._snapshot_counts()
        kw = {"HTTP_IDEMPOTENCY_KEY": "wbi-replay-001"}
        first = _post_json(self.client, self.url, self._payload(), **kw)
        second = _post_json(self.client, self.url, self._payload(), **kw)
        self._assert_retired(first, before)
        self._assert_retired(second, before)
        self.assertEqual(Guest.objects.filter(full_name="散客张三").count(), 0)

    def test_walk_in_retired_missing_idempotency_key_returns_410(self):
        """原"缺幂等键 400"场景：退役端点不设入参分支，缺键也 410。"""
        self._auth()
        before = self._snapshot_counts()
        response = _post_json(self.client, self.url, self._payload())
        self._assert_retired(response, before)

    def test_walk_in_retired_missing_full_name_returns_410(self):
        """原"缺姓名 400"场景：退役端点不校验入参，缺姓名也 410。"""
        payload = self._payload()
        del payload["full_name"]
        self._auth()
        before = self._snapshot_counts()
        response = _post_json(self.client, self.url, payload, HTTP_IDEMPOTENCY_KEY="wbi-noname-001")
        self._assert_retired(response, before)

    def test_walk_in_retired_missing_room_type_returns_410(self):
        """原"缺房型 400"场景：退役端点不校验入参，缺房型也 410。"""
        payload = self._payload()
        del payload["room_type_id"]
        self._auth()
        before = self._snapshot_counts()
        response = _post_json(self.client, self.url, payload, HTTP_IDEMPOTENCY_KEY="wbi-noroom-001")
        self._assert_retired(response, before)

    def test_walk_in_retired_with_phone_does_not_create_or_merge_guest(self):
        """原"同手机号不合并"场景：既有档案保持唯一，无新档案、无合并写入。"""
        existing = Guest.objects.create(
            property=self.property,
            full_name="既有住客",
            phone="",
            phone_hash=phone_hash("13800006699"),
        )
        self._auth()
        before = self._snapshot_counts()
        response = _post_json(
            self.client,
            self.url,
            self._payload(full_name="散客李四", phone="13800006699"),
            HTTP_IDEMPOTENCY_KEY="wbi-merge-001",
        )
        self._assert_retired(response, before)
        # 不合并也不新建：同号档案仍只有既有一条
        self.assertEqual(Guest.objects.filter(phone_hash=existing.phone_hash).count(), 1)

    def test_walk_in_retired_no_vacant_room_returns_410(self):
        """原"无净房 400"场景：房态占用也一律 410，无孤儿记录。"""
        self.room.occupancy_status = Room.OccupancyStatus.OCCUPIED
        self.room.save(update_fields=("occupancy_status",))
        self._auth()
        before = self._snapshot_counts()
        response = _post_json(
            self.client, self.url, self._payload(), HTTP_IDEMPOTENCY_KEY="wbi-novacant-001"
        )
        self._assert_retired(response, before)
        self.assertEqual(Reservation.objects.filter(source="WALK_IN").count(), 0)
        self.assertEqual(Stay.objects.count(), 0)

    def test_walk_in_retired_anonymous_returns_410(self):
        """原"匿名 401"场景：退役端点不设认证分支，匿名调用也 410。

        （Django 测试客户端默认不强制 CSRF，与生产 CSRF 中间件口径无关；
        携带合法会话与 CSRF 的旧调用方同样收到 410。）
        """
        before = self._snapshot_counts()
        response = _post_json(
            self.client, self.url, self._payload(), HTTP_IDEMPOTENCY_KEY="wbi-anon-001"
        )
        self._assert_retired(response, before)

    def test_walk_in_retired_view_only_permission_returns_410(self):
        """原"仅查看权 403"场景：退役端点不设权限分支，仅查看权也 410。"""
        self._auth(codenames=("view_accommodation",))
        before = self._snapshot_counts()
        response = _post_json(
            self.client, self.url, self._payload(), HTTP_IDEMPOTENCY_KEY="wbi-noperm-001"
        )
        self._assert_retired(response, before)
