# -*- coding: utf-8 -*-
"""seed_role_test_accounts 的安全环境变量注入运行器（基线 2.4 sanctioned 路径）。

getpass 在 Windows 管道/无 TTY 下会阻塞（msvcrt 控制台读取），本运行器
按基线 2.4 允许的"安全环境变量注入"方式：从环境变量 ACCEPT_PASSWORD
读取统一口令，monkey-patch getpass 两次调用后调用原命令；口令不写入
命令行参数、日志或本脚本。
"""
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")

_env = {}
try:
    for line in io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "r6_env.txt"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            _env[k.strip()] = v.strip()
except FileNotFoundError:
    pass
PASSWORD = os.environ.get("ACCEPT_PASSWORD") or _env.get("ACCEPT_PASSWORD")
if not PASSWORD:
    print("缺少 ACCEPT_PASSWORD（r6_env.txt 或环境变量）")
    sys.exit(2)

import getpass

_real_getpass = getpass.getpass
_calls = {"n": 0}


def _injected_getpass(prompt="Password: ", stream=None):
    _calls["n"] += 1
    return PASSWORD


getpass.getpass = _injected_getpass

import django

django.setup()

from django.core.management import call_command
from io import StringIO

out = StringIO()
try:
    call_command("seed_role_test_accounts", stdout=out)
    rc = 0
except Exception as exc:
    out.write(f"\n[ERROR] {type(exc).__name__}: {exc}\n")
    rc = 1
getpass.getpass = _real_getpass
sys.stdout.write(out.getvalue() + f"\n[getpass 注入调用次数: {_calls['n']}]\n")
sys.exit(rc)
