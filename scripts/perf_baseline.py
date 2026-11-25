"""ARCH-14：关键列表接口的延迟基准测量（本机可复现）。

用途：为「优化前后可比较」提供量化基线。**不是**生产容量证明：
- 在本机 + 测试数据库上测量，数值只用于同环境回归对比；
- 生产容量、并发压测、真实数据量下的表现需在生产或压测环境另行验证。

用法（务必指定引擎，MySQL 为权威）：
  DJANGO_ENV=test DJANGO_DB_ENGINE=mysql python scripts/perf_baseline.py
  DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite DJANGO_DB_NAME=:memory: \
      python scripts/perf_baseline.py

输出：每条接口的样本数、中位数、p95、查询数，JSON 可存档对比。
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext, setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.core.models import Property  # noqa: E402
from apps.identity.models import (  # noqa: E402
    Group,
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)

ENDPOINTS = (
    "/api/v1/front-desk/overview",
    "/api/v1/front-desk/reservations",
    "/api/v1/front-desk/arrivals",
    "/api/v1/front-desk/rooms",
)
SAMPLES = int(os.environ.get("PERF_SAMPLES", "12"))


def percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def ensure_user(property_obj):
    user, _ = User.objects.get_or_create(username="perf-probe")
    if not user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=["password"])
    profile, _ = StaffProfile.objects.get_or_create(
        user=user,
        defaults={
            "full_name": "性能探针",
            "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
        },
    )
    PropertyStaffMembership.objects.get_or_create(
        staff_profile=profile,
        property=property_obj,
        defaults={"employee_no": "PERF-0001", "status": PropertyStaffMembership.Status.ACTIVE},
    )
    membership = PropertyStaffMembership.objects.get(staff_profile=profile, property=property_obj)
    StaffRoleAssignment.objects.get_or_create(
        membership=membership,
        group=Group.objects.get(name="前台"),
        defaults={"starts_at": timezone.now()},
    )
    return user


def main() -> int:
    setup_test_environment()
    property_obj = Property.objects.filter(
        code=os.environ.get("DEFAULT_PROPERTY_CODE", "DEFAULT")
    ).first()
    if property_obj is None:
        print("未找到 DEFAULT 物业；请先在目标库播种基础数据。")
        return 2
    user = ensure_user(property_obj)
    client = Client()
    client.force_login(user)

    results = []
    for path in ENDPOINTS:
        durations = []
        query_counts = []
        status = None
        for _ in range(SAMPLES):
            with CaptureQueriesContext(connection) as ctx:
                started = time.perf_counter()
                response = client.get(path)
                durations.append((time.perf_counter() - started) * 1000.0)
            status = response.status_code
            query_counts.append(len(ctx.captured_queries))
        results.append(
            {
                "path": path,
                "status": status,
                "samples": SAMPLES,
                "median_ms": round(statistics.median(durations), 2),
                "p95_ms": round(percentile(durations, 95), 2),
                "queries_median": int(statistics.median(query_counts)),
                "queries_max": max(query_counts),
            }
        )

    payload = {
        "engine": connection.vendor,
        "django": django.get_version(),
        "samples_per_endpoint": SAMPLES,
        "note": "本机测量，仅供同环境回归对比；非生产容量证明。",
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
