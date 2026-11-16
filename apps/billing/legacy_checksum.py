"""checksum-v2 归档校验和算法（迭代-2 归档门禁 §4.2 定稿算法的实现）。

设计约束（原文口径，不得自行更改）：
1. 逐字段编码：``len(字段名) + 字段名 + len(类型标识) + 类型标识 + NULL 标志 + len(值) + 值``，
   所有长度前缀为 4 字节大端整数——长度前缀使任意字节内容（含控制字符/分隔符样字符）无歧义；
2. NULL 与空串严格区分（NULL 标志=1 且值字节为空；空串 NULL 标志=0 且值字节为空）；
3. 字符串统一 UTF-8 原样；**金额保原字符串，不做类型转换**；
4. 时间字段：原始值 + UTC ISO8601（``YYYY-MM-DDTHH:MM:SSZ``）转换值**两者都纳入编码**；
5. Decimal/整数：标准十进制文本（无科学计数法、无千分位）；
6. 行摘要 = SHA-256(该行全部字段编码拼接)；
7. 行摘要按主键升序排列，以同样长度前缀编码（``len(行摘要字节) + 行摘要字节``）拼接为表规范化串；
8. 整表摘要 = SHA-256(表规范化串)；
9. 元数据登记算法版本、源表行数、min/max 主键、计算时间、执行人。

实现说明：校验和全部由 Python 侧计算，不依赖数据库 SHA2 函数——避免 SQLite 与 MySQL
方言差异导致同一归档数据算出不同摘要（§4.2「实现约束」）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

ALGORITHM_VERSION = "checksum-v2"

# 类型标识统一用小写 ASCII 字面量，作为编码的一部分参与摘要——
# 同一值以不同类型传入（如 Decimal("1.00") 与 "1.00"）必须产生不同摘要。
_TYPE_NULL = b"null"
_TYPE_BOOL = b"bool"
_TYPE_INT = b"int"
_TYPE_DECIMAL = b"dec"
_TYPE_STR = b"str"
_TYPE_DATE = b"date"
_TYPE_DATETIME = b"dt"

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _len_prefixed(data: bytes) -> bytes:
    """4 字节大端长度前缀 + 原始字节（§4.2 第 1 条）。"""
    return len(data).to_bytes(4, "big") + data


def _type_tag(value: Any) -> bytes:
    # bool 必须在 int 之前判断：Python 中 bool 是 int 的子类。
    if isinstance(value, bool):
        return _TYPE_BOOL
    if isinstance(value, int):
        return _TYPE_INT
    if isinstance(value, Decimal):
        return _TYPE_DECIMAL
    if isinstance(value, datetime):
        return _TYPE_DATETIME
    if isinstance(value, date):
        return _TYPE_DATE
    return _TYPE_STR


def _utc_iso8601(value: datetime) -> str:
    """统一转换值：UTC ISO8601，精确到秒，``Z`` 结尾（§4.2 第 4 条）。"""
    if value.tzinfo is None:
        # 无时区信息按 UTC 处理：归档只保存数据库原始事实，不猜测本地时区。
        normalized = value.replace(tzinfo=timezone.utc)
    else:
        normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _value_bytes(value: Any) -> bytes:
    """把非 NULL 值规范化为字节（§4.2 第 3~5 条）。"""
    if isinstance(value, bool):
        return b"1" if value else b"0"
    if isinstance(value, int):
        return str(value).encode("utf-8")
    if isinstance(value, Decimal):
        # format(Decimal, "f") 不含科学计数法、不含千分位，且保留原始小数位。
        return format(value, "f").encode("utf-8")
    if isinstance(value, datetime):
        # 原始值 + 转换值两者都纳入编码。原始值取 isoformat 的原始挂钟时间，
        # 转换值取 UTC ISO8601，两个子串各自长度前缀化后再拼接，避免拼接歧义。
        original = value.isoformat(sep=" ").encode("utf-8")
        converted = _utc_iso8601(value).encode("utf-8")
        return _len_prefixed(original) + _len_prefixed(converted)
    if isinstance(value, date):
        return value.isoformat().encode("utf-8")
    if isinstance(value, str):
        return value.encode("utf-8")
    # 未知类型（bytes / UUID / 其他）按稳定文本处理，避免静默混入不可复现的 repr。
    raise TypeError(f"checksum-v2 不支持的字段类型：{type(value)!r}")


def encode_field(name: str, value: Any) -> bytes:
    """按 §4.2 第 1~2 条编码单个字段。"""
    name_bytes = name.encode("utf-8")
    if value is None:
        return _len_prefixed(name_bytes) + _len_prefixed(_TYPE_NULL) + b"\x01" + _len_prefixed(b"")
    return (
        _len_prefixed(name_bytes)
        + _len_prefixed(_type_tag(value))
        + b"\x00"
        + _len_prefixed(_value_bytes(value))
    )


def row_digest(row: Mapping[str, Any], columns: Sequence[str]) -> bytes:
    """行摘要 = SHA-256(该行全部字段编码拼接)（§4.2 第 6 条）。

    ``columns`` 必须是归档表定义的**列序**——列序参与摘要，顺序不同摘要必然不同。
    缺列直接抛 ``KeyError``：静默按 NULL 处理会让"漏导出字段"算出一致的假校验和。
    """
    parts = []
    for column in columns:
        if column not in row:
            raise KeyError(f"行数据缺少列 {column!r}：列序须与归档表定义一致")
        parts.append(encode_field(column, row[column]))
    return hashlib.sha256(b"".join(parts)).digest()


def _pk_sort_key(pk: Any) -> tuple[int, int, str]:
    """主键升序：数字主键按数值排序（避免 "10" < "9" 的字典序错误）。"""
    text = str(pk)
    stripped = text[1:] if text.startswith("-") else text
    if stripped.isdigit():
        return (0, int(text), "")
    return (1, 0, text)


def table_digest(
    rows: Iterable[Mapping[str, Any]],
    columns: Sequence[str],
    pk_field: str,
) -> str:
    """整表摘要（§4.2 第 7~8 条），返回小写十六进制串。"""
    digests = []
    for row in rows:
        if pk_field not in row:
            raise KeyError(f"行数据缺少主键列 {pk_field!r}")
        digests.append((_pk_sort_key(row[pk_field]), row_digest(row, columns)))
    digests.sort(key=lambda item: item[0])
    canonical = b"".join(_len_prefixed(digest) for _, digest in digests)
    return hashlib.sha256(canonical).hexdigest()


def build_metadata(
    *,
    source_table: str,
    rows: Sequence[Mapping[str, Any]],
    pk_field: str,
    table_summary: str,
    computed_at: datetime,
    operator: str,
) -> dict[str, Any]:
    """归档批次元数据（§4.2 第 9 条）：算法版本/行数/min-max 主键/计算时间/执行人。"""
    pks = [row[pk_field] for row in rows]
    numeric = [pk for pk in pks if isinstance(pk, int) and not isinstance(pk, bool)]
    min_pk: Any = (
        min(numeric)
        if numeric and len(numeric) == len(pks)
        else (min(map(str, pks)) if pks else None)
    )
    max_pk: Any = (
        max(numeric)
        if numeric and len(numeric) == len(pks)
        else (max(map(str, pks)) if pks else None)
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "source_table": source_table,
        "pk_field": pk_field,
        "row_count": len(rows),
        "min_pk": min_pk,
        "max_pk": max_pk,
        "table_digest": table_summary,
        "computed_at": computed_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "operator": operator,
    }
