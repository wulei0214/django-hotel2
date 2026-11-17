# -*- coding: utf-8 -*-
"""PMS2 调度任务 python 入口（MC-010 残项收口：真实调度器安装证据）。

用法（计划任务动作）：
  <repo>\\.venv\\Scripts\\python.exe <repo>\\scripts\\scheduler_runner.py <manage.py 命令...>

职责：
  1. 载入仓库 .env 到进程环境（setdefault，不覆盖已存在变量）——
     与人工会话 set -a && source .env 等效；
  2. chdir 到仓库根并注入 sys.path；
  3. 以 execute_from_command_line 执行 manage.py 子命令。

调度器证据：每次执行由 install_scheduler_tasks.ps1 注册的动作在
logs/scheduler.log 留痕（stdout/stderr 追加），供"真实调度器安装与
失败重跑"验收核对。
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = REPO / ".env"
    if not env_path.exists():
        print("scheduler_runner: missing .env", file=sys.stderr)
        sys.exit(2)
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # .env 为 shell 风格引号：source 语义会剥掉成对引号，载入器保持一致
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value.strip())
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")


class _Tee:
    """stdout/stderr 同时写原流与调度日志（真实调度器证据落盘）。"""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()


def main() -> int:
    if len(sys.argv) < 2:
        print("scheduler_runner: 用法 scheduler_runner.py <manage.py 命令...>", file=sys.stderr)
        return 2
    _load_env()
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    log_dir = REPO / "logs"
    log_dir.mkdir(exist_ok=True)
    log_handle = open(log_dir / "scheduler.log", "a", encoding="utf-8")
    log_handle.write("\n===== %s | %s =====\n" % (
        sys.argv[1], __import__("datetime").datetime.now().isoformat(timespec="seconds")))
    log_handle.flush()
    sys.stdout = _Tee(sys.stdout, log_handle)
    sys.stderr = _Tee(sys.stderr, log_handle)
    sys.argv = [str(REPO / "manage.py")] + list(sys.argv[1:])
    from django.core.management import execute_from_command_line

    try:
        execute_from_command_line(sys.argv)
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if not exc.code else 1)
    log_handle.write("\n===== exit %s =====\n" % code)
    log_handle.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
