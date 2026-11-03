# -*- coding: utf-8 -*-
"""P1-11：/home/ 统计切 PMS2 权威 + P1-12 旧写入口冻结 + P1-10 LegacyRecordLink 填充"""
import ast
import io

# ---------- 1. /home/ 切 PMS2 权威数据 ----------
p = "F:/Desktop/django-hotel/hotelapp/views.py"
with open(p, encoding="utf-8") as f:
    v = f.read()

# 替换 home 视图主体
old_home_start = "def home(request):"
idx = v.find(old_home_start)
if idx < 0:
    print("!! home 视图未找到")
else:
    # 找下一个顶级 def
    next_def = v.find("\ndef ", idx + 10)
    if next_def < 0:
        next_def = len(v)
    old_home = v[idx:next_def]
    new_home = '''def home(request):
    """主界面：PMS2 权威数据仪表盘（P1-11：不再读旧 hotelapp 统计）。"""
    from apps.api.dashboard import dashboard_data
    from apps.core.models import Property
    user = request.user
    prop = getattr(request, "property", None)
    if prop is None:
        prop = Property.objects.filter(code="DEFAULT").first()
    stats = dashboard_data(prop)
    ctx = {
        "stats": stats,
        "role": _role_name(user),
        "today": str(timezone.localdate()),
        "cards": [],
    }
    return render(request, "home.html", ctx)

'''
    v = v[:idx] + new_home + v[next_def:]
    print("  /home/ 切 PMS2 权威 OK")

# ---------- 2. 旧写入口冻结（P1-12：G0 冻结写） ----------
# 旧 insert_client/update_client/insert_room/update_room/insert_vip/update_vip
# 旧 insert_staff/update_staff/insert_accomodation
# 加 G0 冻结标记：返回 410 并提示使用新入口
FREEZE_ROUTES = [
    "insert_client", "update_client", "insert_vip", "update_vip",
    "insert_room", "update_room", "insert_staff", "update_staff",
    "insert_accomodation",
]
frozen = 0
for func_name in FREEZE_ROUTES:
    pattern = f"def {func_name}(request):"
    idx = v.find(pattern)
    if idx < 0:
        continue
    body_start = v.find("\n", idx) + 1
    # 检查是否已有 G0 冻结标记
    check_region = v[idx:idx+500]
    if "G0" in check_region:
        continue
    guard = ('    # G0 冻结写（P1-12）：PMS2 新入口已覆盖，旧写入口冻结\\n'
             '    return HttpResponse("旧写入口已冻结（410 Gone），请使用 PMS2 管理中心", status=410)\\n')
    guard = guard.replace("\\n", "\n")
    v = v[:idx] + guard + v[idx:]
    frozen += 1

print(f"  {frozen} 个旧写入口已冻结（G0）")

with open(p, "w", encoding="utf-8", newline="") as f:
    f.write(v)
ast.parse(v)
print("views.py P1-11/P1-12 OK")
