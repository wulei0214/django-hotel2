"""ARCH-10：模块依赖方向门禁（只读、可重复执行）。

架构宪法 §3 要求调用方向为 View/API → Service → Model，§9 禁止循环依赖。
本脚本不试图一次约束全部跨模块调用（存量耦合广泛，硬拦会大面积误报），
而是强制两条**确定不可违反**的规则，并输出完整依赖图供人工审查：

规则 L1（分层倒置）：`apps.core` 是基础层，不得导入 `apps.api`。
    基础层依赖 HTTP 适配层会造成循环导入与不可测试性。
规则 L2（迁移边界）：业务代码不得从 `*/migrations/` 导入模块。

扫描对象为工作区文件系统（CI 检出后与本地提交前一致），不依赖 git 索引，
因此未提交/暂存文件同样受检。

退出码：0 通过；1 发现违规；2 用法或读取失败（fail-closed，不得当作通过）。

用法：
  python scripts/arch_gate.py check          # 校验规则，违规即非零退出
  python scripts/arch_gate.py graph          # 打印模块依赖边与计数
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "apps"

IMPORT_PATTERN = re.compile(r"^\ufeff?\s*(?:from\s+(apps\.\w+)|import\s+(apps\.\w+))", re.M)
CORE_TO_API_PATTERN = re.compile(r"^\ufeff?\s*(?:from|import)\s+apps\.api\b", re.M)
MIGRATION_IMPORT_PATTERN = re.compile(r"^\ufeff?\s*(?:from|import)\s+\S*migrations\S*", re.M)


def iter_python_files():
    if not APPS_DIR.is_dir():
        raise RuntimeError(f"未找到 apps 目录：{APPS_DIR}")
    for path in sorted(APPS_DIR.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        yield path


def build_graph() -> tuple[collections.Counter, list[str]]:
    edges: collections.Counter = collections.Counter()
    violations: list[str] = []
    for path in iter_python_files():
        relative = path.relative_to(ROOT).as_posix()
        parts = relative.split("/")
        if len(parts) < 3:
            continue
        source_module = parts[1]
        source = path.read_text(encoding="utf-8", errors="replace")
        for match in IMPORT_PATTERN.finditer(source):
            target_module = (match.group(1) or match.group(2)).split(".")[1]
            if target_module != source_module:
                edges[(source_module, target_module)] += 1
        if source_module == "core" and CORE_TO_API_PATTERN.search(source):
            violations.append(f"L1 分层倒置：{relative} 从 apps.core 导入 apps.api")
        if MIGRATION_IMPORT_PATTERN.search(source):
            violations.append(f"L2 迁移边界：{relative} 从 migrations 导入模块")
    return edges, violations


def cmd_graph() -> int:
    try:
        edges, _ = build_graph()
    except RuntimeError as exc:
        print(f"arch gate: 工具层失败（fail-closed）——{exc}")
        return 2
    for (source, target), count in sorted(edges.items(), key=lambda item: (-item[1], item[0])):
        print(f"{source} -> {target}: {count}")
    print(f"edges_total={sum(edges.values())} modules={len({s for s, _ in edges})}")
    return 0


def cmd_check() -> int:
    try:
        _, violations = build_graph()
    except RuntimeError as exc:
        print(f"arch gate: 工具层失败（fail-closed）——{exc}")
        return 2
    if violations:
        print(f"arch gate: 发现 {len(violations)} 项违规")
        for item in violations:
            print(f"  - {item}")
        return 1
    print("arch gate: 通过（L1 分层方向、L2 迁移边界无违规）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="模块依赖方向门禁")
    parser.add_argument("action", choices=("check", "graph"))
    args = parser.parse_args()
    return cmd_check() if args.action == "check" else cmd_graph()


if __name__ == "__main__":
    # 输出编码固定为 UTF-8：Windows 默认代码页（CP936）会把中文结论写成非 UTF-8 字节，
    # 调用方（如 apps.core.test_arch_gate 按 UTF-8 解码的断言）将无法匹配，误判为门禁失败。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
