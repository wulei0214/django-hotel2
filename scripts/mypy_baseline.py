"""mypy 基线门禁（治理 V1.1 第二阶段，问题清单 P-04 的落地机制）。

用法（在仓库根目录、项目虚拟环境中执行）：
  python scripts/mypy_baseline.py generate   # 重新生成基线（存量清偿后）
  python scripts/mypy_baseline.py check      # 当前错误对比基线，出现"新增"则退出码 1

基线条目格式（mypy-baseline.txt，每行一条）：
  <归一化路径>|<错误码>|<该文件该错误码的数量>

设计要点：
  * 路径统一为正斜杠（Windows 本地与 Linux CI 的 mypy 输出归一）。
  * 行号不计入——代码上下移动不应误报"新增错误"。
  * 数量语义：当前数量 > 基线数量 ⇒ 判定为新增错误，阻断；数量减少 ⇒ 存量清偿，
    提示重新生成基线但不阻断。
  * 纪律（V1.1 §10）：禁止清空或手改基线文件伪造通过；基线变更必须随真实清偿
    一起提交，并在提交说明中注明清偿范围。
"""

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


def run_mypy() -> str:
    """运行 mypy 并返回完整输出（不因退出码非零而抛异常）。"""
    result = subprocess.run(
        [sys.executable, "-m", "mypy", *MYPY_TARGETS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout + result.stderr


def current_errors() -> Counter:
    """解析 mypy 输出，聚合为 {(归一化路径, 错误码): 数量}。"""
    counter: Counter = Counter()
    for line in run_mypy().splitlines():
        match = ERROR_LINE_RE.match(line.strip())
        if not match:
            continue
        path = match.group(1).replace("\\", "/").removeprefix("./")
        code = match.group(2).lower()
        counter[(path, code)] += 1
    return counter


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


def cmd_generate() -> int:
    counter = current_errors()
    lines = [
        "# mypy 基线（治理 V1.1 第二阶段 P-04）——格式: 归一化路径|错误码|数量",
        "# 纪律: 禁止清空或手改本文件伪造通过；重新生成必须随真实存量清偿提交。",
        f"# 生成命令: python scripts/mypy_baseline.py generate；总错误数: {sum(counter.values())}",
        "",
    ]
    lines.extend(f"{path}|{code}|{count}" for (path, code), count in sorted(counter.items()))
    BASELINE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"基线已生成: {BASELINE.name}（{len(counter)} 类条目，{sum(counter.values())} 个错误）")
    return 0


def cmd_check() -> int:
    if not BASELINE.exists():
        print("错误: mypy-baseline.txt 不存在。请先执行 python scripts/mypy_baseline.py generate")
        return 2
    current = current_errors()
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

    total = sum(current.values())
    if new_errors:
        print(
            f"mypy 基线门禁: 阻断——发现 {sum(c - b for _, b, c in new_errors)} 个新增错误"
            f"（涉及 {len(new_errors)} 个 路径×错误码 条目）。当前总错误数 {total}。"
        )
        for (path, code), base_count, cur_count in new_errors:
            print(f"  NEW  {path} [{code}] 基线 {base_count} → 当前 {cur_count}")
        print("修复方式: 修复新增错误；不得修改基线文件来放行（V1.1 §10）。")
        return 1
    print(
        f"mypy 基线门禁: 通过——无新增错误。当前总错误数 {total}（基线 {sum(baseline.values())}）。"
    )
    if improved:
        print(
            f"存量清偿提示: {len(improved)} 个条目数量下降，可在清偿提交中执行 generate 收紧基线:"
        )
        for (path, code), base_count, cur_count in improved[:20]:
            print(f"  FIX  {path} [{code}] {base_count} → {cur_count}")
    return 0


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "generate":
        return cmd_generate()
    if action == "check":
        return cmd_check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
