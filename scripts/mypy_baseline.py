"""mypy 基线门禁（治理 V1.1 第二阶段，问题清单 P-04 的落地机制）。

用法（在仓库根目录、项目虚拟环境中执行；本地 pre-commit 依赖本脚本）：
  python scripts/mypy_baseline.py generate            # 重新生成基线（存量清偿后）
  python scripts/mypy_baseline.py check               # 全量对比基线，"新增"错误则退出码 1（CI 硬门禁）
  python scripts/mypy_baseline.py check-files <py...> # 触碰文件级对比基线（本地 pre-commit 钩子）
  python scripts/mypy_baseline.py selftest            # 负向自测：验证 fail-closed 分类逻辑

退出码约定：
  0 = 通过；1 = 发现新增错误（阻断）；2 = 用法/配置错误；3 = mypy 工具层失败（fail-closed 阻断）。

基线条目格式（mypy-baseline.txt，每行一条）：
  <归一化路径>|<错误码>|<该文件该错误码的数量>

设计要点：
  * 路径统一为正斜杠（Windows 本地与 Linux CI 的 mypy 输出归一）。
  * 行号不计入——代码上下移动不应误报"新增错误"。
  * 数量语义：当前数量 > 基线数量 ⇒ 判定为新增错误，阻断；数量减少 ⇒ 存量清偿，
    提示重新生成基线但不阻断。
  * **fail-closed（验收反馈修正）**：mypy 工具层失败（退出码 ≥2、fatal/traceback、
    解析结果与退出码矛盾）时一律阻断而非放行——"解析不到错误"永远不能解释为"没有错误"。
  * **已知长期风险（登记于《代码审查与质量门禁规范》§2.2 渐进治理项，暂不实施）**：
    若未来 mypy 输出发生部分格式漂移，导致部分错误行被解析、部分被漏掉，当前
    "结构化错误行+退出码"判定无法发现这种部分丢失。后续加固方向：解析错误总数
    与 mypy 汇总行"Found N errors"数量交叉对比，不一致即 fail-closed，并补混合
    格式输出的负向用例。
  * 纪律（V1.1 §10）：禁止清空或手改基线文件伪造通过；基线变更必须随真实清偿
    一起提交，并在提交说明中注明清偿范围。
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "mypy-baseline.txt"
MYPY_TARGETS = ["apps/"]

# 形如: apps\core\rate_limit.py:115: error: Incompatible types ...  [assignment]
ERROR_LINE_RE = re.compile(r"^(.+?):\d+: error: .+\[([a-z0-9_-]+)\]\s*$", re.IGNORECASE)
TOOL_FAILURE_MARKERS = ("mypy: error:", "Traceback (most recent call last)", "Fatal error")
EXIT_OK = 0
EXIT_NEW_ERRORS = 1
EXIT_USAGE = 2
EXIT_TOOL_FAILURE = 3


def _execute(cmd: list[str]) -> tuple[int, str]:
    """运行命令并返回 (退出码, 合并输出)。不因非零退出码抛异常。"""
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout + result.stderr


def classify_mypy_result(returncode: int, output: str) -> tuple[Counter | None, str | None]:
    """把 mypy 运行结果分类为 (错误计数, None) 或 (None, 工具失败原因)。

    fail-closed 规则（任一命中即视为工具层失败，调用方必须阻断）：
      * 退出码不在 {0, 1}（mypy 约定 2+ 为命令行/内部错误）；
      * 输出含 mypy fatal/CLI 错误或 traceback 标记；
      * 退出码与解析结果矛盾（rc==0 却解析到错误；rc==1 却解析不到任何错误——
        后者意味着错误输出格式变化导致解析失效，不能当作"零错误"放行）。
    """
    counter: Counter = Counter()
    for line in output.splitlines():
        match = ERROR_LINE_RE.match(line.strip())
        if not match:
            continue
        path = match.group(1).replace("\\", "/").removeprefix("./")
        code = match.group(2).lower()
        counter[(path, code)] += 1

    if returncode not in (EXIT_OK, EXIT_NEW_ERRORS):
        return None, f"mypy 退出码 {returncode} 不在预期范围 {{0, 1}}"
    for marker in TOOL_FAILURE_MARKERS:
        if marker in output:
            return None, f"输出包含工具失败标记: {marker!r}"
    if returncode == EXIT_OK and counter:
        return None, f"退出码 0 但解析到 {sum(counter.values())} 个错误——解析与退出码矛盾"
    if returncode == EXIT_NEW_ERRORS and not counter:
        return None, "退出码 1（有错误）但未解析到任何结构化错误行——输出格式可能已变化"
    return counter, None


def current_errors() -> tuple[Counter, str | None]:
    """全量运行 mypy apps/，返回分类结果。工具失败时 (None, 原因)。"""
    returncode, output = _execute([sys.executable, "-m", "mypy", *MYPY_TARGETS])
    return classify_mypy_result(returncode, output)


def touched_errors(files: list[str]) -> tuple[Counter, str | None]:
    """对给定文件运行 mypy，仅统计这些文件的错误（过滤 follow-imports 噪声）。"""
    wanted = {f.replace("\\", "/").removeprefix("./") for f in files}
    returncode, output = _execute([sys.executable, "-m", "mypy", *files])
    counter, reason = classify_mypy_result(returncode, output)
    if reason is not None or counter is None:
        return None, reason
    filtered: Counter = Counter()
    for (path, code), count in counter.items():
        if path in wanted:
            filtered[(path, code)] += count
    return filtered, None


def load_baseline() -> Counter:
    baseline: Counter = Counter()
    if not BASELINE.exists():
        return baseline
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, code, count = line.split("|")
        baseline[(path, code)] = int(count)
    return baseline


def _ensure_baseline() -> int | None:
    if not BASELINE.exists():
        print("错误: mypy-baseline.txt 不存在。请先执行 python scripts/mypy_baseline.py generate")
        return EXIT_USAGE
    return None


def _report_tool_failure(reason: str) -> int:
    print(f"mypy 基线门禁: 工具层失败（fail-closed 阻断）——{reason}")
    print("fail-closed 原则: 工具异常时不能把'解析不到错误'当作'没有错误'放行（V1.1 §10）。")
    print(
        "排查: 确认在项目 .venv 中运行（mypy/django-stubs 可用）、pyproject.toml [tool.mypy] 未被破坏。"
    )
    return EXIT_TOOL_FAILURE


def cmd_generate() -> int:
    counter, reason = current_errors()
    if reason is not None:
        return _report_tool_failure(reason)
    lines = [
        "# mypy 基线（治理 V1.1 第二阶段 P-04）——格式: 归一化路径|错误码|数量",
        "# 纪律: 禁止清空或手改本文件伪造通过；重新生成必须随真实存量清偿提交。",
        f"# 生成命令: python scripts/mypy_baseline.py generate；总错误数: {sum(counter.values())}",
        "",
    ]
    lines.extend(f"{path}|{code}|{count}" for (path, code), count in sorted(counter.items()))
    BASELINE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"基线已生成: {BASELINE.name}（{len(counter)} 类条目，{sum(counter.values())} 个错误）")
    return EXIT_OK


def _diff_against_baseline(current: Counter) -> tuple[list, list]:
    baseline = load_baseline()
    new_errors = []
    improved = []
    for key in sorted(set(current) | set(baseline)):
        base_count = baseline.get(key, 0)
        cur_count = current.get(key, 0)
        if cur_count > base_count:
            new_errors.append((key, base_count, cur_count))
        elif cur_count < base_count:
            improved.append((key, base_count, cur_count))
    return new_errors, improved


def cmd_check() -> int:
    missing = _ensure_baseline()
    if missing is not None:
        return missing
    current, reason = current_errors()
    if reason is not None:
        return _report_tool_failure(reason)
    new_errors, improved = _diff_against_baseline(current)

    total = sum(current.values())
    if new_errors:
        print(
            f"mypy 基线门禁: 阻断——发现 {sum(c - b for _, b, c in new_errors)} 个新增错误"
            f"（涉及 {len(new_errors)} 个 路径×错误码 条目）。当前总错误数 {total}。"
        )
        for (path, code), base_count, cur_count in new_errors:
            print(f"  NEW  {path} [{code}] 基线 {base_count} → 当前 {cur_count}")
        print("修复方式: 修复新增错误；不得修改基线文件来放行（V1.1 §10）。")
        return EXIT_NEW_ERRORS
    print(
        f"mypy 基线门禁: 通过——无新增错误。当前总错误数 {total}（基线 {sum(load_baseline().values())}）。"
    )
    if improved:
        print(
            f"存量清偿提示: {len(improved)} 个条目数量下降，可在清偿提交中执行 generate 收紧基线:"
        )
        for (path, code), base_count, cur_count in improved[:20]:
            print(f"  FIX  {path} [{code}] {base_count} → {cur_count}")
    return EXIT_OK


def cmd_check_files(files: list[str]) -> int:
    """本地 pre-commit 钩子入口：触碰文件不得引入超出基线的错误。

    与 CI 全量 check 语义对齐（分文件的子集判定）：
      * 文件不在基线中 ⇒ 该文件任何 mypy 错误都是新增，阻断；
      * 文件在基线中 ⇒ 按 (文件, 错误码) 数量对比，超出基线即阻断。
    工具层失败同样 fail-closed 阻断。
    """
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        print("mypy 基线门禁(触碰文件): 无 .py 文件，跳过。")
        return EXIT_OK
    missing = _ensure_baseline()
    if missing is not None:
        return missing
    current, reason = touched_errors(py_files)
    if reason is not None or current is None:
        return _report_tool_failure(reason or "未知工具失败")
    new_errors, _ = _diff_against_baseline(current)
    if new_errors:
        print(f"mypy 基线门禁(触碰文件): 阻断——触碰文件引入新增错误 {len(new_errors)} 个条目:")
        for (path, code), base_count, cur_count in new_errors:
            print(f"  NEW  {path} [{code}] 基线 {base_count} → 当前 {cur_count}")
        print("修复方式: 修复触碰文件的新增错误；或该文件存量错误按'谁改动谁清理'清偿后 generate。")
        return EXIT_NEW_ERRORS
    print(f"mypy 基线门禁(触碰文件): 通过——{len(py_files)} 个触碰文件无新增错误。")
    return EXIT_OK


def cmd_selftest() -> int:
    """负向自测：不依赖真实 mypy 运行，验证分类器的 fail-closed 逻辑。"""
    cases: list[tuple[str, int, str, bool]] = [
        # (名称, 退出码, 输出, 期望为工具失败)
        ("正常零错误", EXIT_OK, "Success: no issues found in 3 source files\n", False),
        (
            "正常带错误",
            EXIT_NEW_ERRORS,
            "apps/core/a.py:1: error: bad [name-defined]\nFound 1 error\n",
            False,
        ),
        ("退出码 2（CLI/内部错误）", 2, "mypy: error: unrecognized arguments\n", True),
        ("fatal 标记", EXIT_NEW_ERRORS, "Fatal error: can't find files\n", True),
        ("traceback 标记", EXIT_NEW_ERRORS, "Traceback (most recent call last):\n boom\n", True),
        (
            "rc=0 但解析到错误（矛盾）",
            EXIT_OK,
            "apps/core/a.py:1: error: bad [name-defined]\n",
            True,
        ),
        (
            "rc=1 但零解析（格式漂移）",
            EXIT_NEW_ERRORS,
            "Found 500 errors in 10 files (checked 20 source files)\n",
            True,
        ),
        (
            "rc=1 纯文本错误无 [code]",
            EXIT_NEW_ERRORS,
            "apps/core/a.py:1: error: bad\nFound 1 error\n",
            True,
        ),
    ]
    failures = 0
    for name, returncode, output, expect_failure in cases:
        counter, reason = classify_mypy_result(returncode, output)
        got_failure = reason is not None
        ok = got_failure == expect_failure
        if not ok:
            failures += 1
        status = "PASS" if ok else "FAIL"
        detail = reason or f"解析到 {sum((counter or {}).values())} 个错误"
        print(f"  [{status}] {name}: {detail}")
    if failures:
        print(f"selftest: {failures} 个用例失败——fail-closed 逻辑与预期不符，禁止用于门禁。")
        return EXIT_TOOL_FAILURE
    print("selftest: 全部通过（fail-closed 逻辑就绪）。")
    return EXIT_OK


def _reexec_into_venv_if_needed() -> None:
    """当前解释器无 mypy 时，转入项目 .venv 解释器重执行（否则 fail-closed）。

    背景：本机 PATH 上的 python（如 D:\\Python39）是真实 3.9 但未必装了 mypy；
    pre-commit 的 language:system 钩子会用它调用本脚本。无 mypy 时静默退出 0
    会造成门禁旁路，因此必须转入 .venv 重执行或在找不到 .venv 时阻断。
    """
    try:
        import mypy  # noqa: F401
    except ImportError:
        for candidate in (
            ROOT / ".venv" / "Scripts" / "python.exe",
            ROOT / ".venv" / "bin" / "python",
        ):
            if candidate.exists():
                result = subprocess.run(
                    [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]],
                    cwd=ROOT,
                )
                sys.exit(result.returncode)
        print("错误: 当前解释器缺少 mypy，且未找到项目 .venv（.venv/Scripts/python.exe）。")
        print("请在项目 .venv 中安装开发依赖（pip install -r requirements-dev.txt）后重试。")
        sys.exit(EXIT_TOOL_FAILURE)


def main() -> int:
    _reexec_into_venv_if_needed()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "generate":
        return cmd_generate()
    if action == "check":
        return cmd_check()
    if action == "check-files":
        return cmd_check_files(sys.argv[2:])
    if action == "selftest":
        return cmd_selftest()
    print(__doc__)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
