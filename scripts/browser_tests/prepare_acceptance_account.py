# 管理中心 opt-in 能力真实浏览器验收：账号与业务数据准备（隔离库）。
#
# 纪律（AGENTS.md）：
#   1. 本脚本只在显式指定了 DJANGO_DB_ENGINE=sqlite 且库名含 "test" 的隔离库上运行；
#   2. 运行后打印实际所连库名与引擎，人工核对，绝不连开发库；
#   3. 账号按 PMS2 授权模型创建（StaffProfile + PropertyStaffMembership +
#      StaffRoleAssignment + Group），不使用 User.groups 直授。
#
# 由 scripts/browser_tests/run_urlstate_acceptance.ps1 调用，也可单独运行：
#   DJANGO_DB_ENGINE=sqlite DJANGO_DB_NAME=<abs path ending with test>
#   DJANGO_ENV=dev python scripts/browser_tests/prepare_acceptance_account.py
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth.models import Group, Permission, User  # noqa: E402
from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.core.models import Property  # noqa: E402
from apps.identity.models import (  # noqa: E402
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)

DB = connection.settings_dict
ENGINE = str(DB.get("ENGINE"))
NAME = str(DB.get("NAME"))
print("[DB CHECK] engine=%s name=%s" % (ENGINE, NAME))
print("[DB CHECK] DJANGO_DB_NAME=%s" % os.environ.get("DJANGO_DB_NAME"))

if "sqlite" not in ENGINE.lower():
    raise SystemExit("拒绝执行：本脚本只允许在隔离 sqlite 库上运行")
if "test" not in NAME.lower():
    raise SystemExit("拒绝执行：库名必须包含 test，实际为 %s" % NAME)

USERNAME = os.environ.get("UX_ACCEPT_USER", "ux_accept_user")
# 口令由调用方注入：仓库内不得出现可用凭据字面量（AGENTS.md 会话纪律）
PASSWORD = os.environ.get("UX_ACCEPT_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("缺少 UX_ACCEPT_PASSWORD：请由调用方注入本次运行的随机口令")
PERMS = ["hotelapp.view_room", "inventory.manage_rates"]

prop = Property.objects.filter(code="DEFAULT").first()
if prop is None:
    prop = Property.objects.create(code="DEFAULT", name="示例酒店")
print("[PROPERTY] %s / %s" % (prop.code, prop.name))

user, created = User.objects.get_or_create(username=USERNAME)
user.set_password(PASSWORD)
user.save()
print("[ACCOUNT] %s %s" % (USERNAME, "创建" if created else "已存在（口令已重置）"))

profile, _ = StaffProfile.objects.get_or_create(
    user=user,
    defaults={"full_name": "UX 验收员", "employment_status": StaffProfile.EmploymentStatus.ACTIVE},
)
profile.employment_status = StaffProfile.EmploymentStatus.ACTIVE
if hasattr(profile, "must_change_password"):
    profile.must_change_password = False
profile.save()

membership, _ = PropertyStaffMembership.objects.get_or_create(
    staff_profile=profile,
    property=prop,
    defaults={"employee_no": USERNAME, "status": PropertyStaffMembership.Status.ACTIVE},
)
membership.status = PropertyStaffMembership.Status.ACTIVE
membership.save()

group, _ = Group.objects.get_or_create(name="ux-accept-group")
for code in PERMS:
    app_label, codename = code.split(".", 1)
    perm = Permission.objects.filter(
        content_type__app_label=app_label, codename=codename
    ).first()
    if perm is None:
        raise SystemExit("权限码不存在：%s" % code)
    group.permissions.add(perm)
print("[GRANT] 组 %s 持有 %s" % (group.name, PERMS))

StaffRoleAssignment.objects.update_or_create(
    membership=membership,
    group=group,
    defaults={"starts_at": timezone.now() - timedelta(minutes=1), "ends_at": None},
)
print("[GRANT] StaffRoleAssignment 生效：membership=%s group=%s" % (membership.pk, group.name))
print("[READY] user=%s" % USERNAME)
