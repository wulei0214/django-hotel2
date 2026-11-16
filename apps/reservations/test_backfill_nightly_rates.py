"""A8 nightly_rates 存量回填专项测试：dry-run 不写 / execute 回填正确 / 缺价日跳过报告。

覆盖（任务书验收点）：
  * dry-run 只扫描报告，不写 nightly_rates、不写审计；
  * --execute 按 rate_plan+room_type+stay 区间从 DailyRate 正确重建逐晚快照；
  * 价格缺失的住宿日跳过并在 --report 输出（部分缺价 / 整段缺价）；
  * 写后明细金额与快照合计核对：不一致行仅列出供人工核对，不强改账务；
  * 重复执行幂等（回填后再次扫描为空）。
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.inventory.models import DailyRate, RatePlan, RoomType
from apps.reservations.models import Reservation, ReservationRoom


@override_settings(DEBUG=True, DJANGO_ENV="test")
class NightlyRatesBackfillTests(TestCase):
    ACTION = "reservation.nightly_rates_backfilled"

    def setUp(self):
        self.prop = Property.objects.create(
            code=f"NRB-{uuid.uuid4().hex[:8]}", name="回填测试物业")
        self.rt = RoomType.objects.create(
            property=self.prop, code="NR-RT", name="回填房型", capacity=2)
        self.check_in = timezone.localdate() - timedelta(days=10)
        self.check_out = self.check_in + timedelta(days=3)  # 3 晚

    def _make_rate_plan(self):
        return RatePlan.objects.create(
            property=self.prop, code=f"NR-RP-{uuid.uuid4().hex[:6]}",
            name="回填房价计划", valid_from=self.check_in)

    def _add_daily_rate(self, rate_plan, stay_date, price="120.00"):
        return DailyRate.objects.create(
            property=self.prop, rate_plan=rate_plan, room_type=self.rt,
            stay_date=stay_date, price=Decimal(price))

    def _make_legacy_room_line(self, rate_plan, amount="360.00"):
        """创建 nightly_rates 为空的历史预订明细（模拟快照引入前的存量行）。"""
        guest = Guest.objects.create(property=self.prop, full_name="回填测试客")
        reservation = Reservation.objects.create(
            property=self.prop, guest=guest,
            booking_no=f"NR-{uuid.uuid4().hex[:12].upper()}",
            status=Reservation.Status.CHECKED_OUT,
            check_in=self.check_in, check_out=self.check_out,
            room_count=1, total_amount=Decimal(amount), currency="CNY")
        return ReservationRoom.objects.create(
            property=self.prop, reservation=reservation, room_type=self.rt,
            rate_plan=rate_plan, check_in=self.check_in,
            check_out=self.check_out, amount=Decimal(amount))

    def _full_rates(self, rate_plan):
        """三晚完整价格 120/130/110 → 合计 360。"""
        return [
            self._add_daily_rate(rate_plan, self.check_in, "120.00"),
            self._add_daily_rate(rate_plan, self.check_in + timedelta(days=1), "130.00"),
            self._add_daily_rate(rate_plan, self.check_in + timedelta(days=2), "110.00"),
        ]

    def _expected_nights(self):
        return [
            {"date": self.check_in.isoformat(), "price": "120.00", "currency": "CNY"},
            {"date": (self.check_in + timedelta(days=1)).isoformat(),
             "price": "130.00", "currency": "CNY"},
            {"date": (self.check_in + timedelta(days=2)).isoformat(),
             "price": "110.00", "currency": "CNY"},
        ]

    def test_dry_run_does_not_write(self):
        rate_plan = self._make_rate_plan()
        self._full_rates(rate_plan)
        room = self._make_legacy_room_line(rate_plan)
        out = StringIO()
        call_command("backfill_nightly_rates", stdout=out)
        room.refresh_from_db()
        self.assertEqual(room.nightly_rates, [])
        self.assertEqual(
            AuditEvent.objects.filter(action=self.ACTION).count(), 0)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertIn("可回填 1 条", out.getvalue())

    def test_dry_run_reports_sample_rows(self):
        rate_plan = self._make_rate_plan()
        self._full_rates(rate_plan)
        room = self._make_legacy_room_line(rate_plan, amount="360.00")
        out = StringIO()
        call_command("backfill_nightly_rates", stdout=out)
        self.assertIn(f"room_line={room.pk}", out.getvalue())
        self.assertIn("snapshot_sum=360.00", out.getvalue())

    def test_execute_backfills_correct_snapshot(self):
        rate_plan = self._make_rate_plan()
        self._full_rates(rate_plan)
        room = self._make_legacy_room_line(rate_plan, amount="360.00")
        out = StringIO()
        call_command("backfill_nightly_rates", execute=True, stdout=out)
        room.refresh_from_db()
        self.assertEqual(room.nightly_rates, self._expected_nights())
        self.assertIn("回填 1 条", out.getvalue())
        # 写后断言：金额一致 → 无人工核对行
        self.assertIn("金额不一致 0 条", out.getvalue())
        # 每行写审计
        events = AuditEvent.objects.filter(action=self.ACTION, entity_id=str(room.pk))
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().after_json["nights"], 3)

    def test_execute_skips_missing_price_dates_and_reports(self):
        """中间一晚缺价：该晚跳过不进快照，--report 列出缺失日。"""
        rate_plan = self._make_rate_plan()
        self._add_daily_rate(rate_plan, self.check_in, "120.00")
        # 第二晚缺价
        self._add_daily_rate(rate_plan, self.check_in + timedelta(days=2), "110.00")
        room = self._make_legacy_room_line(rate_plan, amount="360.00")
        out = StringIO()
        call_command("backfill_nightly_rates", execute=True, report=True, stdout=out)
        room.refresh_from_db()
        # 只有 2 晚进快照，缺失日跳过
        self.assertEqual(room.nightly_rates, [
            {"date": self.check_in.isoformat(), "price": "120.00", "currency": "CNY"},
            {"date": (self.check_in + timedelta(days=2)).isoformat(),
             "price": "110.00", "currency": "CNY"},
        ])
        missing_day = (self.check_in + timedelta(days=1)).isoformat()
        self.assertIn("缺价日", out.getvalue())
        self.assertIn(missing_day, out.getvalue())
        # 快照合计 230 ≠ amount 360：列出供人工核对，不强改账务
        self.assertIn("金额不一致 1 条", out.getvalue())
        self.assertIn(f"amount=360.00 snapshot_sum=230.00", out.getvalue())
        self.assertEqual(Decimal(room.amount), Decimal("360.00"))

    def test_execute_skips_rows_with_no_rates_at_all(self):
        """整段缺价：明细整行跳过，不写快照也不写审计。"""
        rate_plan = self._make_rate_plan()
        room = self._make_legacy_room_line(rate_plan)
        out = StringIO()
        call_command("backfill_nightly_rates", execute=True, report=True, stdout=out)
        room.refresh_from_db()
        self.assertEqual(room.nightly_rates, [])
        self.assertEqual(
            AuditEvent.objects.filter(action=self.ACTION).count(), 0)
        self.assertIn("整段缺价跳过 1 条", out.getvalue())
        self.assertIn(f"room_line={room.pk}", out.getvalue())

    def test_execute_is_idempotent(self):
        rate_plan = self._make_rate_plan()
        self._full_rates(rate_plan)
        room = self._make_legacy_room_line(rate_plan, amount="360.00")
        call_command("backfill_nightly_rates", execute=True, stdout=StringIO())
        out = StringIO()
        call_command("backfill_nightly_rates", execute=True, stdout=out)
        room.refresh_from_db()
        self.assertEqual(room.nightly_rates, self._expected_nights())
        self.assertIn("扫描 0 条", out.getvalue())
        self.assertEqual(
            AuditEvent.objects.filter(action=self.ACTION).count(), 1)

    def test_execute_respects_property_and_limit_scope(self):
        """--property-id / --limit 限定处理范围：范围外存量行不受影响。"""
        rate_plan = self._make_rate_plan()
        self._full_rates(rate_plan)
        room = self._make_legacy_room_line(rate_plan)
        other = Property.objects.create(
            code=f"NRB2-{uuid.uuid4().hex[:8]}", name="回填测试物业2")
        rt2 = RoomType.objects.create(
            property=other, code="NR-RT2", name="回填房型2", capacity=2)
        rp2 = RatePlan.objects.create(
            property=other, code=f"NR-RP2-{uuid.uuid4().hex[:6]}",
            name="回填房价计划2", valid_from=self.check_in)
        guest2 = Guest.objects.create(property=other, full_name="回填测试客2")
        res2 = Reservation.objects.create(
            property=other, guest=guest2,
            booking_no=f"NR-{uuid.uuid4().hex[:12].upper()}",
            status=Reservation.Status.CHECKED_OUT,
            check_in=self.check_in, check_out=self.check_out,
            room_count=1, total_amount=Decimal("360.00"), currency="CNY")
        room2 = ReservationRoom.objects.create(
            property=other, reservation=res2, room_type=rt2, rate_plan=rp2,
            check_in=self.check_in, check_out=self.check_out,
            amount=Decimal("360.00"))
        call_command(
            "backfill_nightly_rates", execute=True,
            property_id=self.prop.pk, stdout=StringIO())
        room.refresh_from_db()
        room2.refresh_from_db()
        self.assertEqual(len(room.nightly_rates), 3)
        self.assertEqual(room2.nightly_rates, [])
        # --limit=0 非法
        with self.assertRaises(Exception):
            call_command("backfill_nightly_rates", limit=0, stdout=StringIO())

    def test_non_default_room_type_rates_are_not_mixed(self):
        """同计划跨房型取价隔离：DailyRate 必须按 room_type 匹配，不串价。"""
        rate_plan = self._make_rate_plan()
        other_rt = RoomType.objects.create(
            property=self.prop, code="NR-RT-OTH", name="其他房型", capacity=2)
        # 价格挂在另一房型上：本房型无价 → 整段跳过
        DailyRate.objects.create(
            property=self.prop, rate_plan=rate_plan, room_type=other_rt,
            stay_date=self.check_in, price=Decimal("999.00"))
        room = self._make_legacy_room_line(rate_plan)
        out = StringIO()
        call_command("backfill_nightly_rates", execute=True, report=True, stdout=out)
        room.refresh_from_db()
        self.assertEqual(room.nightly_rates, [])
        self.assertIn("整段缺价跳过 1 条", out.getvalue())
