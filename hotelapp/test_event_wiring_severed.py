# -*- coding: utf-8 -*-
"""M3-1 可复燃写面断链守卫（架构治理第三模块提案 G-M3-2①）。

事件链 events.py（publish/subscribe）+ handlers.py（订阅器直写旧 Room/FinanceRecord）
在 2026-11-18 断链（views.py 副作用接线移除，提案 D3-5）。本守卫持续盯防：

1. 全仓无 events.publish / events.subscribe 调用者（定义与测试除外）——
   任何新代码调用 publish 即恢复旧表直写，本测试立即失败；
2. views.py 不再副作用导入 handlers（订阅器注册归零）。
"""
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parent.parent
HOTELAPP = Path(__file__).resolve().parent

ALLOWED_FILES = {"events.py", "handlers.py", "test_event_wiring_severed.py"}


class EventWiringSeveredTests(SimpleTestCase):
    """可复燃写面断链守卫：零发布者 + 零订阅注册。"""

    def _hotelapp_source(self, name):
        return (HOTELAPP / name).read_text(encoding="utf-8")

    def test_no_publish_or_subscribe_callers_outside_definitions(self):
        """除 events.py/handlers.py 定义与守卫自身外，全仓零调用者。"""
        offenders = []
        for py in sorted(HOTELAPP.glob("*.py")):
            if py.name in ALLOWED_FILES:
                continue
            text = py.read_text(encoding="utf-8")
            for needle in ("events.publish", "events.subscribe"):
                if needle in text:
                    offenders.append(f"{py.name}: {needle}")
        self.assertEqual(
            offenders, [],
            "发现事件链调用者——M3-1 断链被复燃（旧表直写风险）：\n" + "\n".join(offenders))

    def test_views_no_longer_registers_handlers(self):
        """views.py 不再副作用导入 events/handlers/state_machine（接线已断）。"""
        text = self._hotelapp_source("views.py")
        for needle in ("from . import events", "from . import handlers",
                       "from . import state_machine"):
            self.assertNotIn(needle, text,
                             f"views.py 恢复了事件链接线（{needle}）——M3-1 断链被复燃")

    def test_events_bus_has_no_registered_subscribers(self):
        """进程内事件总线订阅注册数为零（handlers 未被任何存活代码导入）。"""
        from hotelapp import events

        self.assertEqual(
            events._SUBSCRIBERS, {},
            "事件总线存在已注册订阅器——handlers.py 被某处导入，直写链路复活")
