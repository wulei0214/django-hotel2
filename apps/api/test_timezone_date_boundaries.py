"""财务摘要与房价日历物业本地日期回归测试。"""
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import mock

from django.test import TestCase

from apps.api.guest_center import _property_today
from apps.core.models import Property


class GuestCenterPropertyDateTests(TestCase):
    def test_financial_summary_and_rate_calendar_date_uses_property_timezone(self):
        prop = Property.objects.create(
            code="API-TZ", name="API 时区测试酒店", timezone="Asia/Singapore")
        # UTC 2026-11-09 16:30 = 新加坡 2026-11-10 00:30。
        instant = datetime(2026, 11, 9, 16, 30, tzinfo=dt_timezone.utc)
        with mock.patch("apps.inventory.services.timezone.now", return_value=instant):
            self.assertEqual(str(_property_today(prop)), "2026-11-10")
