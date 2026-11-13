"""A1 Walk-in 散客直入 HTTP 端点测试（DEC-WALKIN-01）。

只断言 API 层行为契约：复用 create_walk_in 的原子链路、无手机号占位、
同 Idempotency-Key 幂等重放、不按手机号自动合并住客、认证/权限/物业隔离、
无净房时 400。业务细节由 apps/reservations/test_whitebox_services.py 覆盖，
本文件不触碰服务层实现。
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.guests.models import Guest
from apps.guests.services import phone_hash
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory
from apps.reservations.models import Reservation, Stay

from .test_whitebox_api import WhiteboxApiTestBase


def _post_json(client, url, payload, **extra):
    return client.post(url, data=json.dumps(payload),
                       content_type="application/json", **extra)


class WalkInApiTestBase(WhiteboxApiTestBase):
    """复用 DEFAULT 物业上下文 + 员工账号工厂，叠加 Walk-in 库存。"""

    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.room_type = RoomType.objects.create(
            property=self.property, code="WBI-KING", name="散客房型", capacity=2)
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="WBI-BAR", name="散客门市价",
            valid_from=today - timedelta(days=1))
        self.room = Room.objects.create(
            property=self.property, room_type=self.room_type, number="WBI-8101",
            floor="8", occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL)
        DailyRate.objects.create(
            property=self.property, rate_plan=self.rate_plan,
            room_type=self.room_type, stay_date=today, price=Decimal("300.00"))
        RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type,
            stay_date=today, total_rooms=3)

    def _auth(self, codenames=("view_accommodation", "change_accommodation")):
        user = self._make_staff(codenames=set(codenames))
        self.client.force_login(user)
        return user

    def _payload(self, **overrides):
        payload = {
            "full_name": "散客张三",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "nights": 1, "adults": 1, "children": 0,
        }
        payload.update(overrides)
        return payload


class WalkInCreateApiTests(WalkInApiTestBase):
    url = "/api/v1/front-desk/walk-in"

    def test_walk_in_no_phone_creates_stay_returns_201(self):
        """无手机号散客：占位号建档、生成 Stay、source=WALK_IN。"""
        self._auth()
        response = _post_json(self.client, self.url, self._payload(),
                             HTTP_IDEMPOTENCY_KEY="wbi-nophone-001")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["source"], "WALK_IN")
        self.assertFalse(data["phone_provided"])
        reservation = Reservation.objects.get(booking_no=data["booking_no"])
        self.assertEqual(reservation.source, "WALK_IN")
        self.assertTrue(
            Stay.objects.filter(reservation_room__reservation=reservation).exists())
        # 无手机号：服务层生成确定性占位号，不落明文手机号
        guest = reservation.guest
        self.assertFalse(guest.phone)
        self.assertTrue(guest.phone_hash)

    def test_walk_in_idempotent_replay_returns_same_booking(self):
        """同 Idempotency-Key 重放：返回同一预订号，不重复建档。"""
        self._auth()
        kw = {"HTTP_IDEMPOTENCY_KEY": "wbi-replay-001"}
        first = _post_json(self.client, self.url, self._payload(), **kw)
        second = _post_json(self.client, self.url, self._payload(), **kw)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(first.json()["data"]["booking_no"],
                         second.json()["data"]["booking_no"])
        booking_no = first.json()["data"]["booking_no"]
        self.assertEqual(Reservation.objects.filter(booking_no=booking_no).count(), 1)
        self.assertEqual(Guest.objects.filter(full_name="散客张三").count(), 1)

    def test_walk_in_requires_idempotency_key_returns_400(self):
        """缺少 Idempotency-Key 请求头 → 400。"""
        self._auth()
        response = _post_json(self.client, self.url, self._payload())
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_walk_in_missing_full_name_returns_400(self):
        payload = self._payload()
        del payload["full_name"]
        self._auth()
        response = _post_json(self.client, self.url, payload,
                             HTTP_IDEMPOTENCY_KEY="wbi-noname-001")
        self.assertEqual(response.status_code, 400, response.content)

    def test_walk_in_missing_room_type_returns_400(self):
        payload = self._payload()
        del payload["room_type_id"]
        self._auth()
        response = _post_json(self.client, self.url, payload,
                             HTTP_IDEMPOTENCY_KEY="wbi-noroom-001")
        self.assertEqual(response.status_code, 400, response.content)

    def test_walk_in_with_phone_does_not_merge_existing_guest(self):
        """同手机号散客：新建独立住客档案，不自动合并既有档案。"""
        existing = Guest.objects.create(
            property=self.property, full_name="既有住客", phone="",
            phone_hash=phone_hash("13800006699"))
        self._auth()
        response = _post_json(
            self.client, self.url,
            self._payload(full_name="散客李四", phone="13800006699"),
            HTTP_IDEMPOTENCY_KEY="wbi-merge-001")
        self.assertEqual(response.status_code, 201, response.content)
        reservation = Reservation.objects.get(
            booking_no=response.json()["data"]["booking_no"])
        # 不自动合并：新住客档案独立于既有档案
        self.assertNotEqual(reservation.guest_id, existing.pk)
        self.assertGreaterEqual(
            Guest.objects.filter(phone_hash=existing.phone_hash).count(), 2)

    def test_walk_in_no_vacant_room_returns_400(self):
        """无净房（占用中）时拒绝，不留下孤儿记录。"""
        self.room.occupancy_status = Room.OccupancyStatus.OCCUPIED
        self.room.save(update_fields=("occupancy_status",))
        self._auth()
        response = _post_json(self.client, self.url, self._payload(),
                             HTTP_IDEMPOTENCY_KEY="wbi-novacant-001")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        # 原子回滚：无新增预订/Stay
        self.assertEqual(Reservation.objects.filter(source="WALK_IN").count(), 0)
        self.assertEqual(Stay.objects.count(), 0)

    def test_walk_in_requires_authentication_returns_401(self):
        response = _post_json(self.client, self.url, self._payload(),
                             HTTP_IDEMPOTENCY_KEY="wbi-anon-001")
        self.assertEqual(response.status_code, 401, response.content)

    def test_walk_in_requires_change_permission_returns_403(self):
        """仅有入住查看权、无修改权 → 403。"""
        self._auth(codenames=("view_accommodation",))
        response = _post_json(self.client, self.url, self._payload(),
                             HTTP_IDEMPOTENCY_KEY="wbi-noperm-001")
        self.assertEqual(response.status_code, 403, response.content)
