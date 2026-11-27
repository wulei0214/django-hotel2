"""以隔离库配置启动开发服务器（供 E2E / 九岗位矩阵取证）。

与直接 `manage.py runserver` 的差别：
  - 读 `.env` 用 **setdefault 语义**（不覆盖调用方显式指定的变量，例如隔离库名
    `test_hotel_e2e`），否则隔离库会被 `.env` 里的开发库名悄悄替换掉；
  - 跳过平台管理员白名单变量（`DJANGO_PLATFORM_ADMIN_USERNAME/PASSWORD`），
    否则身份/权限判定失真；
  - 启动即打印实际所连库（`[DEV_SERVER] db=... host=... env=...`），人工核对，
    防止取证服务连到开发库 `hotel`。

用法（仓库根）：
    python scripts/acceptance/isolated_devserver.py 127.0.0.1:8001

或经 PowerShell 包装（推荐：附带取证期设置 DJANGO_DB_CONN_MAX_AGE=0）：
    powershell -NoProfile -File scripts\\acceptance\\serve_isolated_db.ps1 `
        -Port 8001 -DbName test_hotel_e2e

纪律：不打印任何凭据；`.env` 缺失时只依赖调用方已设置的环境变量。
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

DEFAULT_ADDR = "127.0.0.1:8123"


def load_env(path):
    """读取 .env 到进程环境（去引号）。

    使用 setdefault：不得覆盖调用方显式指定的变量（例如隔离库名 test_hotel_e2e），
    否则 E2E 隔离库会被 .env 中的开发库名悄悄替换掉。
    """
    if not os.path.exists(path):
        print("[DEV_SERVER] 未找到 %s：只使用调用方已设置的环境变量" % path, flush=True)
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in {"DJANGO_PLATFORM_ADMIN_USERNAME", "DJANGO_PLATFORM_ADMIN_PASSWORD"}:
                continue
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


load_env(os.path.join(ROOT, ".env"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")

# 启动即打印所连数据库（防止隔离库被静默替换成开发库）
try:
    import django

    django.setup()
    from django.db import connection

    print("[DEV_SERVER] db=%s host=%s env=%s"
          % (connection.settings_dict.get("NAME"), connection.settings_dict.get("HOST"),
             os.environ.get("DJANGO_ENV")), flush=True)
except Exception as exc:  # pragma: no cover - 仅用于启动诊断
    print("[DEV_SERVER] 数据库探测失败: %s" % exc, flush=True)

from django.core.management import execute_from_command_line  # noqa: E402

addr = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ADDR
execute_from_command_line(["manage.py", "runserver", addr, "--noreload"])
