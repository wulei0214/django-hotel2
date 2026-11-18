# -*- coding: utf-8 -*-
"""可复燃写面退役守卫（M3-3/D3-5 甲，2026-11-19）。

事件链 events.py（publish/subscribe）+ handlers.py（订阅器直写旧 Room/FinanceRecord）
+ state_machine.py（房态流转表）三文件已于 M3-1 断链（views.py 副作用接线移除，
2026-11-18）、M3-3 删除（D3-5 甲裁定，2026-11-19）。本守卫持续盯防：

1. 三文件保持删除态（不得恢复）；
2. hotelapp 全域零事件链引用（源码级扫描）；
3. views.py 保持无接线导入。
"""
from pathlib import Path

from django.test import SimpleTestCase

HOTELAPP = Path(__file__).resolve().parent

REMOVED = ("events.py", "handlers.py", "state_machine.py")


class EventWiringSeveredTests(SimpleTestCase):
    """可复燃写面退役守卫：文件不存在 + 全域零引用双断言。"""

    def test_removed_files_stay_removed(self):
        """M3-3/D3-5 甲：三文件必须保持删除态（不得恢复）。"""
        for name in REMOVED:
            self.assertFalse(
                (HOTELAPP / name).exists(),
                f"{name} 被恢复——违反 D3-5 甲删除裁定（旧表直写风险复活）")

    def test_no_publish_or_subscribe_callers_anywhere(self):
        """hotelapp 全域零事件链代码引用（import/调用形态；docstring 与方法名不误伤）。"""
        offenders = []
        code_needles = ("import events", "import handlers", "import state_machine",
                        "events.publish(", "events.subscribe(")
        for py in sorted(HOTELAPP.glob("*.py")):
            if py.name == "test_event_wiring_severed.py":
                continue  # 守卫自身源码含检索词，自排除
            text = py.read_text(encoding="utf-8")
            for needle in code_needles:
                if needle in text:
                    offenders.append(f"{py.name}: {needle}")
        self.assertEqual(
            offenders, [],
            "发现事件链代码引用残留——D3-5 甲删除裁定被违反：\n" + "\n".join(offenders))

    def test_views_has_no_event_wiring(self):
        """views.py 保持无事件链接线（M3-1 断链持久）。"""
        text = (HOTELAPP / "views.py").read_text(encoding="utf-8")
        for needle in ("from . import events", "from . import handlers",
                       "from . import state_machine"):
            self.assertNotIn(needle, text,
                             f"views.py 恢复了事件链接线（{needle}）——M3-1 断链被复燃")
