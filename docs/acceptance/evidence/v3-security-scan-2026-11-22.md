# V3.0 第一批凭据扫描取证（2026-11-22）

> 性质：验收证据。**本记录结论为 NOT PASS，不构成安全审计完成声明。**

## 一、基线与工具

| 项 | 值 |
|---|---|
| 代码基线 | `0d865eb28b1cafd0b113dc9194a661979d70b806`（远端 main） |
| 扫描器 | `scripts/scan_secrets.py`（V12-06 增强版；git blob `1b745053ae72d45dcb9cc61f53f2192da3ee6d0f`） |
| Python | 3.9.0（本机 `.venv`） |
| 白名单 | 0 条（`allowlist_size: 0`） |
| 执行方式 | 独立 detached worktree 检出 `0d865eb`，不夹带任何并行会话未提交文件 |
| 命令 | `python scripts/scan_secrets.py --json` |

## 二、范围

- 全仓受控文本：源码、模板/前端、配置、CI 工作流、部署脚本、文档与脚本。
- 排除目录及理由由扫描器固定维护（`.git`、`.venv`、依赖/构建产物、本机备份、日志等）；本次未改排除清单、未添加文件豁免。
- 未执行 `--history`（CI 默认口径亦不含历史模式）；本结论仅覆盖工作树。

## 三、结果

| 项 | 值 |
|---|---|
| 退出码 | **1** |
| `hit_count` | **1** |
| 命中规则 | `generic-assignment` |
| 命中位置 | `apps/api/test_verify_report_consistency.py` |
| 内容类型 | TASK-05 冒烟测试中的测试辅助口令字面量（不在本证据复述具体值） |

结论：扫描器本身工作正常且阻断了提交；当前基线未达到凭据扫描 PASS。

## 四、CI secret-scan 假绿问题

| 项 | 值 |
|---|---|
| Django CI run | `44988780824` |
| secret-scan job | `137105066609` |
| job 报告结论 | success |
| job 日志实际值 | `allowlist_size: 0`、`hit_count: 1`、同一文件同一规则 |

根因：`.github/workflows/django-ci.yml` 使用 `python scripts/scan_secrets.py --json | tee secret-scan-report.json`，未启用 `pipefail`，扫描器退出码 1 被 `tee` 的退出码 0 覆盖。**该 job 的 success 不能记为凭据扫描 PASS，也不能作为 G14 安全门禁依据。**

## 五、待收口动作（保留项）

1. 将测试口令改为 `get_random_string` 模块级常量，清除仓库中的凭据字面量。
2. 在 secret-scan 步骤启用 `set -o pipefail`（或改为先落文件、保留原退出码），并增加“命中必须阻断”的工作流级验证。
3. 修复后重跑全仓扫描，归档命令、范围、退出码、结果与代码 SHA。
4. 取得 owner 对最新基线的补充确认前，安全扫描维持 **NOT PASS**。
