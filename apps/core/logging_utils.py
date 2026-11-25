"""结构化日志格式化与脱敏（ARCH-05 / Q6）。

输出单行 JSON，固定字段：timestamp/level/logger/message/request_id/property_id。
脱敏规则覆盖手机号、18 位身份证号与常见凭据键值，避免审计与访问日志泄露明文。
"""

import json
import logging
import re
from datetime import datetime, timezone

PHONE_PATTERN = re.compile(r"(?<!\d)1\d{10}(?!\d)")
ID_CARD_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
SECRET_KV_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|credential)\s*[=:]\s*\S+"
)


def redact(text):
    """对敏感明文做不可逆遮盖；非字符串原样返回。"""
    if not isinstance(text, str):
        return text
    text = SECRET_KV_PATTERN.sub(r"\1=<redacted>", text)
    text = ID_CARD_PATTERN.sub("<redacted-id>", text)
    text = PHONE_PATTERN.sub("<redacted-phone>", text)
    return text


class JsonFormatter(logging.Formatter):
    """把日志记录渲染为单行 JSON，并附加请求/物业关联字段。"""

    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for field in ("request_id", "property_id", "actor_id", "event", "error_code"):
            value = getattr(record, field, None)
            if value not in (None, ""):
                payload[field] = value
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else ""
        return json.dumps(payload, ensure_ascii=False)
