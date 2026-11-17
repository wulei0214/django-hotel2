"""状态机引擎：订单的合法流转集中定义（房态段已于 2026-11-18 M4-1 退役，旧 Room 房态由 PMS2 inventory 三维房态接管），视图层只调用、不自行 if-else。

非法流转由后台直接拒绝（如：房间未清洁完成不可入住），前台捕获错误并提示。
"""

# 订单状态机：当前状态 -> 允许迁移到的状态集合
ORDER_FLOW = {
    "待确认": {"已确认", "已取消", "未到"},
    "已确认": {"已入住", "已取消", "未到"},
    "已入住": {"已退房", "已冲账"},
    "已退房": {"已冲账"},           # 反结账为受控回边，视图层单独校验后放行
    "已冲账": set(),
    "已取消": set(),
    "未到": set(),
}

def order_can(src, dst):
    return dst in ORDER_FLOW.get(src, set())


def require_order_transition(order, dst):
    """校验并执行订单状态迁移；非法流转抛 ValueError（视图转 400 提示）"""
    src = order.状态 or "待确认"
    if not order_can(src, dst):
        raise ValueError(f"订单状态不允许该操作（当前：{src} → 目标：{dst}）")
    order.状态 = dst



