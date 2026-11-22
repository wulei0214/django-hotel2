"""TASK-06 九岗位矩阵的数据库不变量核对（只读快照 + 前后比对）。

九岗位矩阵只应「读」高危端点：夜审执行用不存在的营业日触发业务校验，退款用
不存在的退款单，房态放行打向已 CLEAN 的房间。因此前后比对的不变量是：

1. 营业日行未推进，`night_audit.run` 审计事件未新增（夜审未被执行）；
2. 目标房间房态未变化；
3. 退款/收款事实未新增；
4. 新增审计事件中无夜审/退款执行/退款审批的成功动作。

用法：
    python scripts/acceptance/nine_role_db_check.py before
    python scripts/acceptance/nine_role_db_check.py after
    python scripts/acceptance/nine_role_db_check.py check

快照目录由 `NINE_ROLE_EVIDENCE` 指定（默认 docs/acceptance/evidence/task06-nine-role）。
"""

import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)
with open(os.path.join(_ROOT, ".env"), encoding="utf-8") as _env:
    for _line in _env:
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _value = _line.split("=", 1)
            if _key.strip() not in (
                "DJANGO_PLATFORM_ADMIN_USERNAME",
                "DJANGO_PLATFORM_ADMIN_PASSWORD",
            ):
                os.environ.setdefault(_key.strip(), _value.strip().strip("'").strip('"'))
os.chdir(_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")

import django  # noqa: E402

django.setup()

from apps.billing.models import Payment, Refund  # noqa: E402
from apps.core.models import AuditEvent, BusinessDate, ExportCredential, Property  # noqa: E402
from apps.inventory.models import Room  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "check"
EVIDENCE = os.environ.get(
    "NINE_ROLE_EVIDENCE", os.path.join("docs", "acceptance", "evidence", "task06-nine-role")
)
BEFORE = os.path.join(EVIDENCE, "db-before.json")
AFTER = os.path.join(EVIDENCE, "db-after.json")
FORBIDDEN_PREFIXES = ("night_audit.run", "refund.execute", "refund.approve")


def snapshot():
    prop = Property.objects.get(code="DEFAULT")
    business_date = BusinessDate.objects.filter(property=prop).order_by("-business_date").first()
    room = Room.objects.filter(number="9936").first()
    return {
        "business_date": {
            "date": str(business_date.business_date) if business_date else None,
            "status": business_date.status if business_date else None,
        },
        "room_9936": {
            "housekeeping": room.housekeeping_status if room else None,
            "maintenance": room.maintenance_status if room else None,
            "occupancy": room.occupancy_status if room else None,
        },
        "counts": {
            "refund": Refund.objects.count(),
            "payment": Payment.objects.count(),
            "export_credential": ExportCredential.objects.count(),
            "night_audit_event": AuditEvent.objects.filter(action="night_audit.run").count(),
            "audit_event_total": AuditEvent.objects.count(),
        },
        "max_audit_event_id": AuditEvent.objects.order_by("-id")
        .values_list("id", flat=True)
        .first()
        or 0,
    }


def main():
    if MODE in ("before", "after"):
        data = snapshot()
        path = BEFORE if MODE == "before" else AFTER
        os.makedirs(EVIDENCE, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if not (os.path.exists(BEFORE) and os.path.exists(AFTER)):
        print("缺少 before/after 快照，先分别执行 before 与 after")
        return 2

    with open(BEFORE, encoding="utf-8") as fh:
        before = json.load(fh)
    with open(AFTER, encoding="utf-8") as fh:
        after = json.load(fh)

    new_events = list(
        AuditEvent.objects.filter(id__gt=before["max_audit_event_id"])
        .order_by("id")
        .values("id", "action", "actor__username")
    )
    forbidden = [
        event
        for event in new_events
        if any(
            event["action"].startswith(prefix) and "den" not in event["action"].lower()
            for prefix in FORBIDDEN_PREFIXES
        )
    ]
    verdicts = [
        {
            "item": "营业日行未推进且无新增夜审成功事件",
            "ok": before["business_date"] == after["business_date"]
            and before["counts"]["night_audit_event"] == after["counts"]["night_audit_event"],
            "detail": f"{before['business_date']} -> {after['business_date']}; "
            f"night_audit.run {before['counts']['night_audit_event']} -> "
            f"{after['counts']['night_audit_event']}",
        },
        {
            "item": "目标房间房态未变化",
            "ok": before["room_9936"] == after["room_9936"],
            "detail": f"{before['room_9936']} -> {after['room_9936']}",
        },
        {
            "item": "退款/收款事实未新增",
            "ok": before["counts"]["refund"] == after["counts"]["refund"]
            and before["counts"]["payment"] == after["counts"]["payment"],
            "detail": f"refund {before['counts']['refund']} -> {after['counts']['refund']}; "
            f"payment {before['counts']['payment']} -> {after['counts']['payment']}",
        },
        {
            "item": "新增审计事件无夜审/退款执行/退款审批成功动作",
            "ok": not forbidden,
            "detail": forbidden or "通过",
        },
    ]
    result = {
        "verdicts": verdicts,
        "new_audit_events": new_events,
        "passed": all(verdict["ok"] for verdict in verdicts),
    }
    with open(os.path.join(EVIDENCE, "db-check.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
