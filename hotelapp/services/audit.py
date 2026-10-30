"""结构化审计日志。"""
import datetime
from decimal import Decimal

from ..models import AuditLog


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def log_audit(request, action, entity, object_id="", before=None, after=None):
    actor = getattr(request, "user", None)
    if not getattr(actor, "is_authenticated", False):
        actor = None
    ip = None
    if request is not None:
        ip = request.META.get("REMOTE_ADDR") or None
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        entity=entity,
        object_id=str(object_id) if object_id is not None else None,
        before=_json_safe(before or {}),
        after=_json_safe(after or {}),
        ip=ip,
    )
