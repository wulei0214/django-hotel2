# V3.2 TASK-21 安全扫描补证：仓库历史扫描与 Mimosa 扫描器根因（2026-11-22）

> 性质：验收证据。本文是 [v3-security-scan-2026-11-22.md](v3-security-scan-2026-11-22.md) §六（工作树 PASS）的补充：
> 覆盖任务书 V3.2 TASK-21 第 6 项（仓库历史核对）与第 1 项（Mimosa scanner 缓冲/超时根因）。
> 历史扫描结论：**命中 10 处，均为早期提交残留或扫描器自命中，当前树无可用凭据；不需要历史重写**。

## 一、扫描器版本与执行记录

| 项 | 值 |
|---|---|
| 扫描器 | `scripts/scan_secrets.py`（V12-06 增强版） |
| 模式 | `--history --json`（工作树 + Git 全历史提交内容） |
| 执行时间 | 2026-11-22 20:05（历史扫描时点，HEAD 见随附 JSON） |
| Python | 3.9.0（本机 `.venv`） |
| 白名单 | 0 条（`allowlist_size: 0`） |
| 工作树结果 | 退出码 0、`hit_count: 0`（§六 PASS 口径） |
| **历史结果** | **退出码 1、`hit_count: 10`**（命中即阻断，如实报告） |

## 二、历史命中分类（10 处）

| 规则 | 数量 | 内容 | 当前树状态 | 评估 |
|---|---|---|---|---|
| `django-secret-key` | 7 | 早期 `hotel/settings.py` 硬编码的 dev SECRET_KEY（两个值：随机 dev 键与 `dev-only-change-me-…` 占位键），仅存在于基线提交 `45e0fb5`/`c37d051` 及一次保护快照 | **已清零**：现行 `settings.py` 改为 `DJANGO_SECRET_KEY` 环境变量注入 + dev 随机回退，生产启动校验强制 ≥32 位强随机并拒绝 `django-insecure` | 值已失效且不再被任何代码引用；不构成可用凭据泄露 |
| `known-redacted-literal` | 3 | 扫描器 `KNOWN_REDACTED` 清单内的三个历史测试口令（**值一律不回抄**，见 `scripts/scan_secrets.py:29-33`；命中位置即该清单本身，属**自命中**） | 2 处仅存在于扫描器清单内（防止失效口令被复用的哨兵值），1 处仅在历史整改提交（`42dcc8e` 轮换+脱敏）的上下文中出现 | 设计内自命中，非泄露 |

**处置结论**：无需 Git 历史重写（filter-repo/BFG）。理由：① dev 密钥早已轮换失效且现行配置不读取任何字面量；② 本仓库为私有仓库，无公开暴露窗口；③ 历史重写的破坏性（强推、提交链断裂）大于收益。若未来仓库转公开，需先执行历史重写并全量轮换密钥（登记为转公开前置条件）。

## 三、Mimosa scanner 缓冲/超时根因（scanner_enobufs 同族）

### 现象

`.mimosa/history/run-*.json` 最近 6 次运行全部 `inconclusive`，`errors` 字段为逐文件错误：

```text
scan:apps/api/front_desk.py:        scanner_failed: spawnSync D:\nodejs\node.exe ETIMEDOUT
scan:apps/api/test_walkin_options.py: scanner_failed: spawnSync D:\nodejs\node.exe ETIMEDOUT
```

### 根因

Mimosa hook 对每个目标文件用 Node `spawnSync` 起独立扫描子进程，Windows 管道缓冲 + 默认同步超时对**大文件**（`front_desk.py` 2262 行、多份千行级测试文件）不够用：子进程输出超过管道缓冲或耗时超过同步等待上限即报 `ETIMEDOUT`（与 ENOBUFS 同族——`spawnSync` stdio 管道的缓冲/等待上限）。受影响文件退出后该 run 被标 `inconclusive`（`completeness: partial`），**不阻断、不放行**，只是覆盖度缺口如实登记。

### 复核结论（深度扫描已补齐的部分）

2026-11-20 deep scan（job `scan-job-mwp4jik4`，`scan-2026-11-20T15-19-09.821Z`）结果已人工复核：

- 覆盖：522 个源文件全部解析成功（0 read/parse failures，`limit=26206` 行），pathAnalysis 完成（5560 函数 / 6546 调用边）；
- 22 个去重 finding 逐条核对：**4 个为 vendored 第三方库**（`staticfiles/admin/simpleui-x/` 下 vue.js/elementui/particles 的 exec/动态执行特征，门禁规范 §80 已登记为确认误报）；**18 个为自研代码静态特征误报**——迁移文件 SQL 由 `schema_editor.quote_name` 引名（无外部输入拼接）、`openapi_schema.py` 的 "硬编码凭据" 实为 OpenAPI 安全方案 header 字段名、`seed_hotel_data.py` 的 `random` 为演示数据播种（非安全令牌）、演练/审计脚本的路径穿越提示均来自受控命令行参数（管理命令仅本机运维使用）；
- 无一为真实可利用漏洞；scanner 失败文件集中在 API/测试大文件，其凭据面已由 `scan_secrets.py`（工作树 exit 0）与本次历史扫描独立覆盖。

### 处置

- 不修改 Mimosa hook 配置（属 DSH 插件层，无项目侧超时旋钮；`security-policy.json` 无相关字段）；
- 以 `scan_secrets.py` 工作树 + `--history` 双模式作为权威凭据门禁（已入 CI 且 pipefail 真绿）；
- 本根因登记为**已知覆盖度缺口**（Mimosa inconclusive 不计入 PASS/FAIL 证据链），不把兼容放行记为 PASS。

## 四、CI 与提交纪律核对

| 项 | 状态 |
|---|---|
| CI secret-scan（`0c61398` run 45005686602） | 真绿：`-o pipefail` + `hit_count: 0`（§六） |
| 本批提交 | 全部经 pre-commit（ruff/ruff-format/black/mypy）通过，**未使用 `--no-verify`** |
| 兼容放行 | 无（未改动扫描器排除清单、未添加 allowlist 条目） |
| 本机代码差异扫描 | 本批 `apps/api/` 改动（复算命令+测试）均为测试/工具代码，工作树扫描 `hit_count: 0` 覆盖 |
