"""ARCH-10：架构门禁脚本的可执行性测试。

目的不是重复脚本内部逻辑，而是固定三条对外契约：
1. 规则在真实仓库上通过（当前无违规）；
2. 分层倒置（core 导入 api）必须被拦截且退出码非零；
3. 从 migrations 导入必须被拦截且退出码非零。
第 2/3 条用临时文件构造，结束后清理，不污染仓库。
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # 仓库根
SCRIPT = ROOT / "scripts" / "arch_gate.py"


def run_gate() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class ArchGateTests(unittest.TestCase):
    def test_current_repository_passes(self):
        result = run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("通过", result.stdout)

    def test_core_importing_api_is_blocked(self):
        probe = ROOT / "apps" / "core" / "zz_arch_gate_probe_l1.py"
        probe.write_text("from apps.api import views\n", encoding="utf-8")
        try:
            result = run_gate()
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("L1", result.stdout)
        finally:
            probe.unlink(missing_ok=True)

    def test_migration_import_is_blocked(self):
        probe = ROOT / "apps" / "billing" / "zz_arch_gate_probe_l2.py"
        probe.write_text("from apps.core.migrations import x\n", encoding="utf-8")
        try:
            result = run_gate()
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("L2", result.stdout)
        finally:
            probe.unlink(missing_ok=True)

    def test_cleanup_restores_pass(self):
        self.assertEqual(run_gate().returncode, 0)


if __name__ == "__main__":
    unittest.main()
