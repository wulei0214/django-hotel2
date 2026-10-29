"""事件订阅器：后台联动动作集中在此，前台视图只发布事件。"""
import logging

from django.db import IntegrityError

from . import events
from .models import Room, FinanceRecord

audit = logging.getLogger("hotel.audit")


def _on_checked_out(order_id, room_no, amount, operator, date):
    """退房事件：房态进入清洁链起点（待清洁），收入写入财务流水"""
    room = Room.objects.get(房间号=room_no)
    room.房间状态 = "待清洁"
    room.save()
    if amount:
        FinanceRecord.objects.create(类型="收入", 金额=amount,
                                     事由=f"客房结算（订单{order_id}）",
                                     关联订单=str(order_id), 日期=date, 经手人=operator)
    audit.info("事件 checked_out：订单%s 结算 %s，房间%s -> 待清洁", order_id, amount, room_no)


events.subscribe("checked_out", _on_checked_out)


def _on_booking_confirmed(order_id, room_no, operator):
    """排房确认事件：锁定房间（房态 -> 已预订）"""
    room = Room.objects.get(房间号=room_no)
    room.房间状态 = "已预订"
    room.save()
    audit.info("事件 booking_confirmed：订单%s 排房 %s（已锁房）", order_id, room_no)


events.subscribe("booking_confirmed", _on_booking_confirmed)


def _on_booking_cancelled(order_id, room_no, prev_status):
    """取消事件：若房间因该订单处于已预订，则释放回空闲"""
    room = Room.objects.get(房间号=room_no)
    if room.房间状态 == "已预订":
        room.房间状态 = "空闲"
        room.save()
    audit.info("事件 booking_cancelled：订单%s 取消（原状态 %s），房态已同步", order_id, prev_status)


events.subscribe("booking_cancelled", _on_booking_cancelled)


def _on_no_show(order_id, room_no):
    """未到事件：释放房间"""
    room = Room.objects.get(房间号=room_no)
    if room.房间状态 == "已预订":
        room.房间状态 = "空闲"
        room.save()
    audit.info("事件 no_show：订单%s 未到处理，房间%s 已释放", order_id, room_no)


events.subscribe("no_show", _on_no_show)
