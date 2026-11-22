# Windows 本地部署指南独立复现报告（V3.2 TASK-22，2026-11-22）

> 复现人：本轮开发会话（未参与 TASK-09 原始指南编写，符合任务书复现人要求）。
> 复现对象：`docs/PMS2.0_当前版本/07_部署运维与数据治理/Windows本地部署标准化指南_2026-11-22.md`。
> 复现基线：main `01dad1d`（2026-11-22）。本报告只登记复现结果与指南修订项，不重复指南正文。

## 一、复现结果（按指南章节）

| 步骤 | 指南章节 | 结果 | 备注 |
|---|---|---|---|
| 迁移 | §三 | **PASS**（`manage.py migrate --check` exit 0，No migrations to apply） | dev MySQL 8，`.env` 注入后执行 |
| 九角色账号初始化核查 | §三 | **PASS**（test_front/test_duty/test_finance/test_owner/test_housekeeping/test_nightaudit/test_fnb/test_fnbsup/test_parking 全部 EXISTS active=True） | 本环境为已初始化环境，验证 seed 产物在位；口令经 `scripts/r6_env.txt` 载体纪律未触碰 |
| Waitress 启动 | §四 | **PASS**（`http://127.0.0.1:8017/login/` 返回 200，页面 3187 字节） | 生产形态命令原样可跑 |
| 本地备份 | §六 | **PASS**（mysqldump 全量 1,251,826 字节） | 需注意 `-h127.0.0.1` 在 PowerShell 下须 `cmd /c` 或加空格传递（见 §三 修订项） |
| 本地恢复 | §六 | **PASS（受限域）**（导入 `test_hotel`，102 表恢复成功后清理；`hotel_repro_restore` 独立库名被 1044 拒绝） | `hotelhotel` 账号仅授 `hotel`/`test_hotel`（既有授权事实），恢复演练只能在这两个库内做——与 AGENTS.md「不追加 test_% 授权」一致，指南须写明 |
| 后台任务 | §四/任务书第 10 项 | **PASS**（`expire_reservation_holds` exit 0，输出"已释放 0 笔超时预订的库存"） | 指南未列后台任务命令清单，已补 |
| 关键浏览器测试 | 任务书第 11 项 | **引用既有证据**：res04-05 E2E 6/6 PASS（`docs/acceptance/evidence/res04-05-2026-11-22-10-14/`，本日同环境同栈跑通） | 本轮不重复执行整套 E2E |

## 二、指南修订项（本次复现发现）

1. **`--no-verify` 建议降级为受控例外（安全修订，任务书明确要求）**：§七 第 4 行「本地 ruff+black 后 `--no-verify` 提交」改为——
   pre-commit 已在本环境可用（2026-11-20 起），`--no-verify` 仅允许在网络不可达且**必须当天交付**时使用，且须：①提交前手动跑 `ruff check` + `black --check`（触碰文件）；②推送后等待 CI 全绿方算收口；③在交付记录登记豁免原因。禁止作为常规方案。
2. **备份/恢复库权限边界**：§六 补充——`hotelhotel` 应用账号无 CREATE 权限（仅 `hotel`/`test_hotel`），恢复演练目标库只能选这两者；独立验证库名需 DBA 授权或由管理员执行（登记为 WAITING_FOR_DBA 的生产演练前置）。
3. **PowerShell 调用 mysqldump/mysql 参数**：`-h127.0.0.1` 连写形式在 PowerShell 下会被拆参（Unknown server host '127'），建议统一 `cmd /c` 包裹或写作 `-h 127.0.0.1`；口令一律走 `MYSQL_PWD` 环境变量，**不得放在命令行明文参数**（安全修订，任务书明确要求；指南 §六 原文 `-p` 交互式写法保留，补本条替代方案）。
4. **后台任务命令**：§四 补充调度任务冒烟命令（`manage.py expire_reservation_holds`）；`refresh_operational_todos` 并非管理命令（存在于代码引用中的待办刷新由其他入口承担），复现时勿照搬。
5. **测试口令注入**：§三 已有的 r6_env.txt 注入运行器口径正确，维持；补一句「任何情况下不得把口令写进命令行参数或 shell 历史」。

## 三、结论

指南整体可复现（核心链路迁移→初始化→启动→登录→备份→恢复全通），修订项 5 条均为环境差异与安全口径澄清，不构成部署阻断。修订项已由本报告随附提交落回指南正文（同提交原则）。
