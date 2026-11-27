"""在隔离库上重建九岗位拒绝矩阵所需的 v12 / v13 测试账号。

背景：`scripts/acceptance/nine_role_matrix.py` 从账号清单读取九岗位的账号与
口令（`.work/v12_accounts.json` 与 `.work/v13/v13_accounts.json`）。这两份清单是
历史取证期在隔离库上建好的账号快照；隔离库一旦重建（如
`scripts/acceptance/prepare_isolated_db.py`），账号随之消失，矩阵脚本会因缺账号
而无法开跑。本脚本按同一口径把账号重建出来。

口径（与 PMS2 授权模型一致）：
    User(username) -> StaffProfile -> PropertyStaffMembership -> StaffRoleAssignment

纪律：
  - 只在库名含 test 的隔离库上运行，绝不触碰开发库；
  - 岗位授权只走 StaffRoleAssignment，不直授 `User.groups`；
  - 口令只从账号清单读取，不在本文件内出现任何凭据字面量。

用法（仓库根）：
    python scripts/acceptance/prepare_nine_role_accounts.py
"""

import argparse
import json
import os
import sys
from datetime import timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")
import django  # noqa: E402

django.setup()

from django.contrib.auth.models import Group, User  # noqa: E402
from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.core.models import Property  # noqa: E402
from apps.identity.models import (  # noqa: E402
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)

DEFAULT_ACCOUNT_FILES = (
    os.path.join(".work", "v12_accounts.json"),
    os.path.join(".work", "v13", "v13_accounts.json"),
)


def main():
    parser = argparse.ArgumentParser(description="重建九岗位矩阵测试账号（隔离库）")
    parser.add_argument(
        "--accounts",
        action="append",
        default=None,
        help="账号清单 JSON（可重复）；默认读 .work/v12_accounts.json 与 .work/v13/v13_accounts.json",
    )
    args = parser.parse_args()

    db_name = str(connection.settings_dict.get("NAME"))
    if "test" not in db_name.lower():
        raise SystemExit("拒绝执行：只允许在库名含 test 的隔离库上运行，实际 %s" % db_name)
    print("[DB] %s" % db_name)

    files = args.accounts or list(DEFAULT_ACCOUNT_FILES)
    accounts = {}
    for rel in files:
        path = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print("[SKIP] 账号清单不存在：%s" % rel)
            continue
        with open(path, encoding="utf-8") as handle:
            accounts.update(json.load(handle))
    if not accounts:
        raise SystemExit("没有可用的账号清单：请用 --accounts 指定 JSON 路径")

    prop = Property.objects.filter(status="ACTIVE").order_by("id").first()
    if prop is None:
        prop = Property.objects.filter(code="DEFAULT").first()
    if prop is None:
        raise SystemExit("隔离库里没有可用物业")

    now = timezone.now()
    prepared = []
    for username, info in accounts.items():
        group_name = info["group"]
        group = Group.objects.filter(name=group_name).first()
        if group is None:
            raise SystemExit("角色组不存在（迁移未种出？）: %s" % group_name)

        user, _ = User.objects.get_or_create(username=username)
        user.is_active = True
        user.is_staff = False
        user.set_password(info["password"])
        user.save()
        user.groups.clear()

        profile, _ = StaffProfile.objects.get_or_create(
            user=user,
            defaults={
                "full_name": "取证-%s" % group_name,
                "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
            },
        )
        profile.employment_status = StaffProfile.EmploymentStatus.ACTIVE
        if hasattr(profile, "must_change_password"):
            profile.must_change_password = False
        profile.save()

        membership, _ = PropertyStaffMembership.objects.get_or_create(
            property=prop,
            staff_profile=profile,
            defaults={
                "employee_no": "E-%s" % username[:20].upper(),
                "position_code": group_name,
                "status": PropertyStaffMembership.Status.ACTIVE,
            },
        )
        membership.status = PropertyStaffMembership.Status.ACTIVE
        membership.save()

        StaffRoleAssignment.objects.update_or_create(
            membership=membership,
            group=group,
            defaults={"starts_at": now - timedelta(minutes=1), "ends_at": None},
        )
        prepared.append("%s(%s)" % (username, group_name))

    print("[READY] %d 个账号已按 StaffRoleAssignment 口径重建：%s"
          % (len(prepared), ", ".join(prepared)))


if __name__ == "__main__":
    main()
