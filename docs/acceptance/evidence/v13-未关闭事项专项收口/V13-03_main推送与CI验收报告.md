# V13-03 main 推送与 CI 验收报告

**日期**: 2026-11-21

## 一、推送事实（如实记录）

| 项 | 值 |
|---|---|
| 任务书基线 | 4bcd7ad |
| 五笔审查提交（e40889b/4177439/4e8dbb7/f5aeea1/108bd6c） | **已在 origin/main**——经本会话 V1.2/V13 批量推送连带入库（推送先于审查的流程偏差已在 V13-02 §〇如实记录，追补审查通过） |
| 本会话推送序列 | f2c07fe（GAP-DU-11/14）→ c1848fa（V13-01/02 整改）→ 本报告随附提交（V13-06 场景测试+V13 报告集） |
| 并行会话推送 | ab3faf9/f104488/a90b9d6（MC11 阶段二B+浏览器回归修正）+ 8150a17（SQLite 全量收口——G360-04 旧测试理由对齐，与本会话 CI 诊断同因同修）+ 90dcbc6（TASK-07 溯源） |
| 推送前 main SHA | 4bcd7ad（任务书基线） |
| 推送后 main SHA | 见下方回读 |
| 远程新提交合并 | 并行会话推送与本会话推送交错发生；均为 fast-forward，无强制覆盖 |

## 二、CI 验收（GitHub Actions 实测）

### c1848fa 运行（44776226571 / 44776226663）

| Job | 结论 | 说明 |
|---|---|---|
| lint (ruff) | ✅ success | |
| secret-scan (scan_secrets) | ✅ success | TASK-04 门禁在 CI 生效 |
| type-check (mypy 基线) | ✅ success | c1848fa 清偿后通过（f2c07fe 为 failure——即 V13-01 修复对象） |
| OpenAPI snapshot (44776226537) | ✅ success | |
| docs-lint (docs_link_check) | ❌ failure | 0f07c22 治理记录 3 条断链（docs/architecture 引用 ×2 + hotel-backup sql 历史路径 ×1）——**已修复**（治理记录路径改写为自引用/无斜杠表述），本地复扫新增违规=0 |
| test | ❌ failure | 干净 worktree（c1848fa 精确检出）复现：1844 例仅 **1 错误**=hotelapp G360-04 旧测试理由未对齐 8 字符校验——**并行会话 8150a17 已修复入库**；与本会话 CI 诊断同因同修 |

### 修复后状态

- 本地 main 含 8150a17（G360-04 对齐）+ 90dcbc6 + 本会话增量 → 推送后 CI 应全绿
- docs_link_check 本地复扫：新增违规数=0 ✓
- mypy 基线：除并行在途未提交文件（verify_report_consistency 未跟踪/settings.py 未提交）外 0 新增 ✓
- scan_secrets：通过 ✓

## 三、推送后回读

见本报告提交后的 push 输出与 GitHub Actions（本报告提交 SHA 记录于 V13-08 汇总）。

## 四、终态（2026-11-21 更新）

后继提交序列：a0b875f（V13-07/V13-06/报告集）→ 3b5d9cc（CI lint 清偿——MC11 三文件 ruff 回归）。

**CI 终态（GitHub Actions 实测，head=3b5d9cc）**：
- Django CI：**completed / success**（lint ✅ / docs-lint ✅ / secret-scan ✅ / type-check ✅ / test ✅ 五 job 全绿）
- tests workflow：**completed / success**（全量 1844 例）

main 已恢复可信质量门禁状态。lint 失败根因说明：a0b875f 推送的变更文件集含 MC11 接线三文件（audit_center/staff_center/inventory_admin_center），其 ruff 回归（15 重复字典键 + 9 B904 + 1 FK ignore）由本会话清偿（3b5d9cc）。
