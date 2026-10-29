"""轻量事件总线（同步发布/订阅）。

前台操作只负责发布事件（如 checked_out），后台联动（房态流转、账务流水、审计）
由订阅器统一执行——保证前台操作即时响应，后台流程集中可靠。
"""

_SUBSCRIBERS = {}


def subscribe(event, fn):
    _SUBSCRIBERS.setdefault(event, []).append(fn)


def publish(event, **payload):
    for fn in _SUBSCRIBERS.get(event, []):
        fn(**payload)
