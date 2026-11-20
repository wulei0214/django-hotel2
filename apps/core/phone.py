"""手机号统一口径（TASK-02）。

三层职责严格分离，禁止把"规范化"与"有效性校验"混为一个步骤：

1. `normalize_phone(raw)`：**格式规范化**。只清除非数字字符（空格、横线、
   括号、`+`），返回纯数字串；不判断长度、不抛异常（空值返回空串）。
2. `is_valid_phone(raw)` / `validate_phone(raw)`：**有效性校验**。按数字
   位数 `PHONE_MIN_DIGITS ≤ n ≤ PHONE_MAX_DIGITS` 判定；`validate_phone`
   失败抛 `ValueError`，错误文案与原 `reservations.services.normalize_phone`
   完全一致，保证既有 API 400 响应契约不变。
3. `require_phone(raw)`：**业务场景允许性**。预订/入住必须留电话；客户
   档案允许不填。调用方按场景选择 `require_phone` 或直接使用
   `normalize_phone`。

口径裁定（工程基线，变更需评审）：

* **存储与哈希形态为纯数字**，不含 `+`。理由：预订链路（公开下单、walk-in、
  待确认上限 `PendingBookingGate.gate_key`）历来按纯数字计算 `phone_hash`，
  账务/前台检索亦按纯数字；若改为保留 `+`，将与既有 `phone_hash` 行不匹配。
* **国际号码保留国家码数字**（如 `+86 138...` → `86138...`），不额外补 `+`。
* 位数 6–20：沿用预订侧既有规则，用于拦截明显残缺（如 3 位）与超长输入。
* `+` 仅作为可接受的输入分隔符，不进入规范形态。

历史数据影响（不批量改写）：曾以 `+数字` 形态录入的客户档案，其
`Guest.phone_hash` / `GuestPhone.phone_hash` 是 `phone_hash("+数字")`。
本轮不改写历史数据；读侧通过 `apps.guests.services.phone_hash_candidates()`
同时匹配规范形态与历史形态，避免共享号码判定漏判。
"""

from __future__ import annotations

import re

PHONE_MIN_DIGITS = 6
PHONE_MAX_DIGITS = 20

_NON_DIGIT = re.compile(r"\D")


def normalize_phone(raw) -> str:
    """格式规范化：仅保留数字，去除空格/横线/括号/加号等分隔符。"""
    return _NON_DIGIT.sub("", str(raw if raw is not None else ""))


def digit_count(raw) -> int:
    """规范化后的数字位数；用于判定，不抛异常。"""
    return len(normalize_phone(raw))


def is_valid_phone(raw) -> bool:
    """有效性校验：数字位数在 [PHONE_MIN_DIGITS, PHONE_MAX_DIGITS] 内。"""
    return PHONE_MIN_DIGITS <= digit_count(raw) <= PHONE_MAX_DIGITS


def validate_phone(raw, *, message: str = "请输入有效联系电话。") -> str:
    """校验并返回规范形态；失败抛 `ValueError`（API 层映射 400）。"""
    normalized = normalize_phone(raw)
    if not PHONE_MIN_DIGITS <= len(normalized) <= PHONE_MAX_DIGITS:
        raise ValueError(message)
    return normalized


def require_phone(raw, *, message: str = "请输入有效联系电话。") -> str:
    """业务场景允许性校验：预订/入住场景下号码必填且必须有效。"""
    if not normalize_phone(raw):
        raise ValueError(message)
    return validate_phone(raw, message=message)


def legacy_phone_variants(normalized: str) -> tuple[str, ...]:
    """历史兼容形态：返回需要一并参与哈希比对的历史规范形态。

    迁移 0007 之前 Guest 侧 `normalize_phone` 保留 `+`，因此以 `+数字` 录入
    的档案哈希基于 `+数字`。本函数返回去重后的候选形态（规范形态优先），
    仅用于**读取侧**兼容比对，不得用于写入。
    """
    value = normalize_phone(normalized)
    if not value:
        return ()
    return (value, f"+{value}")


__all__ = [
    "PHONE_MIN_DIGITS",
    "PHONE_MAX_DIGITS",
    "digit_count",
    "is_valid_phone",
    "legacy_phone_variants",
    "normalize_phone",
    "require_phone",
    "validate_phone",
]
