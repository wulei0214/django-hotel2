"""PMS2 调度任务 python 入口（MC-010 残项收口：真实调度器安装证据）。

用法（计划任务动作）：
  <repo>\\.venv\\Scripts\\pythonw.exe <repo>\\scripts\\scheduler_runner.py \
      --log=logs/scheduler/<任务名>.log <manage.py 命令...>

参数支持日期占位符 {today} / {today+N}（展开为本地 ISO 日期），等价 Linux
cron 里的 $(date +%F) / $(date -d "+N days" +%F)；Windows 计划任务无 shell
日期运算，滚动窗口只能在这一层展开，业务命令的入参契约保持不变。

职责：
  1. 先把 stdout/stderr 绑定到调度日志（`pythonw.exe` 无控制台，
     sys.stdout/sys.stderr 为 None，必须先兜住再执行业务，
     否则空转任务会因写入 None 抛错）；
  2. 载入仓库 .env 到进程环境（setdefault，不覆盖已存在变量）——
     与人工会话 set -a && source .env 等效；
  3. chdir 到仓库根并注入 sys.path；
  4. 以 execute_from_command_line 执行 manage.py 子命令。

为什么用 pythonw.exe：计划任务以交互会话运行时，控制台子系统解释器
（python.exe）会由 Windows Terminal 托管出一个可见窗口；GUI 子系统
（pythonw.exe）不分配控制台，任务彻底静默。Windows 11 上后者还能避免
「操作员或管理员拒绝了请求」（0x800710E0）这类控制台创建失败。

调度器证据：每次执行在 --log 指定文件追加一段带完整命令、时间戳与退出码的
记录（未指定时为 logs/scheduler.log），供"真实调度器安装与失败重跑"验收核对；
每个任务独立文件，避免并发追加互相穿插。
"""

import datetime
import os
import re
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG_ARG_PREFIX = "--log="
# 日期占位符：{today} / {today+N}，等价 Linux cron 的 $(date +%F) / $(date -d "+N days")。
# Windows 计划任务没有 shell 日期运算，只有调度入口这一层能补，业务命令契约不变。
DATE_TOKEN = re.compile(r"\{today(?:\+(\d{1,4}))?\}")


class _Tee:
    """同时写原流与调度日志（真实调度器证据落盘）。

    任一底层流为 None（pythonw 下的控制台流）时自动跳过，
    不因缺少控制台而中断业务执行。
    """

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for stream in self._streams:
            stream.write(data)

    def flush(self):
        for stream in self._streams:
            stream.flush()

    def isatty(self):
        return False


def _split_log_arg(argv):
    """剥离前置的 --log= 参数，剩余参数原样交给 manage.py。"""
    log_target = None
    rest = list(argv)
    while rest and rest[0].startswith(LOG_ARG_PREFIX):
        log_target = rest.pop(0)[len(LOG_ARG_PREFIX) :]
    return log_target, rest


def _expand_date_tokens(argv):
    """展开 {today} / {today+N} 为 ISO 日期（本地日，与 cron 的 date +%F 同口径）。"""

    def _replace(match):
        offset = int(match.group(1) or 0)
        return (date.today() + timedelta(days=offset)).isoformat()

    return [DATE_TOKEN.sub(_replace, item) for item in argv]


def _bind_streams(log_handle):
    """把 stdout/stderr 指向「原流 + 日志文件」；无控制台时只写日志。"""
    sys.stdout = _Tee(sys.stdout, log_handle)
    sys.stderr = _Tee(sys.stderr, log_handle)


def _resolve_log_path(log_target):
    target = Path(log_target) if log_target else Path("logs") / "scheduler.log"
    if not target.is_absolute():
        target = REPO / target
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


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


def main() -> int:
    log_target, args = _split_log_arg(sys.argv[1:])
    args = _expand_date_tokens(args)
    log_path = _resolve_log_path(log_target)
    with open(log_path, "a", encoding="utf-8", errors="replace") as log_handle:
        _bind_streams(log_handle)
        if not args:
            print("scheduler_runner: 用法 scheduler_runner.py [--log=路径] <manage.py 命令...>")
            return 2

        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        log_handle.write(f"\n===== {' '.join(args)} | {stamp} =====\n")
        log_handle.flush()

        _load_env()
        sys.path.insert(0, str(REPO))
        os.chdir(REPO)
        sys.argv = [str(REPO / "manage.py")] + list(args)
        from django.core.management import execute_from_command_line

        try:
            execute_from_command_line(sys.argv)
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if not exc.code else 1)
        except Exception as exc:  # 兜底：异常必须落日志并置非零退出码，供任务历史定位
            traceback.print_exc()
            print(f"scheduler_runner: 未捕获异常：{exc}", file=sys.stderr)
            code = 1
        log_handle.write(f"\n===== exit {code} =====\n")
        sys.stdout.flush()
        sys.stderr.flush()
        return code


if __name__ == "__main__":
    sys.exit(main())
