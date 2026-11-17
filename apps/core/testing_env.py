"""测试进程环境变量净化的单一来源。

本机按 `set -a && source .env` 注入环境变量时，`.env` 中的
`DJANGO_PLATFORM_ADMIN_USERNAME` / `DJANGO_PLATFORM_ADMIN_PASSWORD` 会激活
`settings.PLATFORM_ADMIN_USERNAME` 的平台后台登录白名单，使身份会话分离、权限矩阵
与酒店侧用例出现 403 != 200 / 200 != 302 的假失败。CI 不注入这两个变量，
故所有本地测试证据生成器必须沿同一口径清理子进程环境。
"""

import os
from collections.abc import Mapping
from typing import Optional

# 与 CI 测试口径对齐：仅这两个变量会改变被测业务行为，其余 .env 变量照常透传
# （MySQL 连接参数仍由环境变量提供，禁止在脚本内写凭据字面量）。
STRIP_KEYS = ("DJANGO_PLATFORM_ADMIN_USERNAME", "DJANGO_PLATFORM_ADMIN_PASSWORD")


def test_process_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """返回测试子进程环境：剔除平台后台白名单变量并强制 DJANGO_ENV=test。

    `base=None` 时取当前 `os.environ` 的快照；传入映射时为纯函数，便于测试。
    原环境不被修改。
    """
    env = dict(os.environ if base is None else base)
    for key in STRIP_KEYS:
        env.pop(key, None)
    env["DJANGO_ENV"] = "test"
    return env
