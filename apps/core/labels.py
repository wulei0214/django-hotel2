# -*- coding: utf-8 -*-
"""中央中文标签映射：API DTO 层统一输出 *_label 字段的数据源。

原则：模型 TextChoices 已有中文 label 的直接取（单一事实源）；
模型没有 choices 的业务枚举（币种/会员等级/账单摘要状态）在此集中定义。
数据库仍存英文码，本模块只负责展示层翻译。
"""


def choice_labels(choices_class):
    """把 Django TextChoices 转成 {code: 中文label}。"""
    return {c.value: c.label for c in choices_class}


# ---------- 模型 choices 派生 ----------
def room_label_maps():
    from apps.inventory.models import Room
    return {
        "occupancy": choice_labels(Room.OccupancyStatus),
        "housekeeping": choice_labels(Room.HousekeepingStatus),
        "maintenance": choice_labels(Room.MaintenanceStatus),
    }


def reservation_status_labels():
    from apps.reservations.models import Reservation
    return choice_labels(Reservation.Status)


def stay_status_labels():
    from apps.reservations.models import Stay
    return choice_labels(Stay.Status)


def employment_status_labels():
    from apps.identity.models import StaffProfile
    return choice_labels(StaffProfile.EmploymentStatus)


def gmembership_status_labels():
    from apps.guests.models import GuestMembership
    return choice_labels(GuestMembership.Status)


# ---------- 无 choices 的业务枚举 ----------
CURRENCY_LABELS = {
    "CNY": "人民币",
    "USD": "美元",
    "HKD": "港币",
    "EUR": "欧元",
    "JPY": "日元",
}

# 会员等级（V1.1 M-16：level_code 为 CharField，无 choices）
MEMBER_LEVEL_LABELS = {
    "STANDARD": "标准会员",
    "ADVANCED": "高级会员",
    "ELITE": "精英会员",
}

# Folio 摘要状态：模型 Folio.Status（未结/已结/已作废）+ 未开账哨兵
FOLIO_SUMMARY_STATUS_LABELS = {
    "NOT_OPENED": "未开账",
    "OPEN": "未结",
    "CLOSED": "已结",
    "VOIDED": "已作废",
}


def currency_label(code):
    return CURRENCY_LABELS.get(code, code)


def member_level_label(code):
    return MEMBER_LEVEL_LABELS.get(code, code)


def folio_summary_status_label(code):
    return FOLIO_SUMMARY_STATUS_LABELS.get(code, code)

def payment_status_labels():
    from apps.billing.models import Payment
    return choice_labels(Payment.Status)


def refund_status_labels():
    from apps.billing.models import Refund
    return choice_labels(Refund.Status)
