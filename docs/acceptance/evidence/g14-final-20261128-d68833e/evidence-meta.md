# 证据轮次元信息（g14-final-20261128-d68833e）

> 类型：ACCEPTANCE 证据轮次元信息 ｜ 执行人：AI 开发会话（ZCode 主控）｜ 执行日期：2026-11-28
> 被测树：`d68833ee9b9c67118ed8dc5166bbc486b447002f`（`main` HEAD，含业务代码 `ed50dbf` pms-ux 与 `a97098f` 三写点收口；其上仅文档提交）
> 执行依据：《PMS2.0收尾执行任务书_2026-11-28》（DOC-100）A-01～A-06；R-01 签署（通过·有保留）授权原文「如果上述都没有问题，全部签字和做出裁定，我给你全部权限」

## 一、六项证据登记（执行人/起止/命令/退出码）

| # | 任务卡 | 证据文件 | 开始 | 结束 | 退出码 | 结果 |
|---|---|---|---|---|---|---|
| A-04 | E2E 全链路 56 项 | `e2e-56-d68833e.log`（服务：`serve_isolated_db.ps1 -Port 8001 -DbName test_hotel`） | 2026-11-28 21:27 | 2026-11-28 21:28 | 0 | 56 项 / 失败 0 |
| A-05 | 九岗位矩阵 90 格 | `../task06-nine-role-20261128-d68833e/report.json`（`run_nine_role_evidence.ps1 -RunId 20261128-d68833e -Port 8011 -DbName test_hotel`） | 2026-11-28 21:29 前后 | 同 | 0 | 失败 0，`passed=true` |
| A-01 | SQLite 全量 | `sqlite-d68833e.log`（`scripts/test_evidence.py --engine sqlite`） | 2026-11-28 21:29:16（证据头） | 2026-11-28 21:38 | 0 | Ran 2244 OK（skipped=32，568.813s） |
| A-02 | MySQL 全量 | `mysql-d68833e.log`（`scripts/test_evidence.py --engine mysql`） | 2026-11-28 21:39:26（证据头） | 2026-11-28 21:50 | 0 | Ran 2244 OK（skipped=5，687.339s） |
| A-03 | MySQL 并发仲裁 | `mysql-concurrency-d68833e.log`（`run_concurrency_evidence.ps1`） | 2026-11-28 21:52 前后 | 同 | 0 | Ran 55 OK（24.398s） |
| A-06 | 浏览器真实页面 | `../center-urlstate-2026-11-28-13-53/`（`run_urlstate_acceptance.ps1 -Port 8141 -Center rooms`） | 2026-11-28 21:53 前后 | 同 | 0 | 28/28，failed 0 |

> 顺序说明：R7-a 未落地（`information_schema.SCHEMATA` 现取无 `test_hotel_e2e`），按 DOC-100 §A-04 依赖条款先跑 A-04/A-05（用 `test_hotel`），A-02 MySQL 全量在其后串行（收尾会重建 `test_hotel`，与 A-04/A-05 不同时运行）。

## 二、R7-a 例外声明（A-04/A-05）

本轮 A-04（E2E）与 A-05（九岗位）**未使用目标隔离库名 `test_hotel_e2e`，实际使用 `test_hotel`**。原因：R7-a DBA 授权仍未落地（执行前现取 `information_schema.SCHEMATA` 无 `test_hotel_e2e`，仅 `test_hotel`）。DOC-100 §A-04 已预设此例外路径：「若未落地，必须在 A-01/A-02 之前用 `test_hotel` 完成 A-04/A-05（全量测试收尾会删除 `test_hotel`），并在证据头写明例外」。例外不影响断言正确性——两轮证据的业务口径与 `test_hotel_e2e` 完全一致，仅库名不同；R7-a 落地后（C-07）后续轮次恢复目标库名。

## 三、MySQL 全量错峰登记（A-02）

| 项 | 值 |
|---|---|
| 执行人 | AI 开发会话（ZCode 主控，受 DOC-100 授权） |
| 起止时间 | 2026-11-28 21:39:26 → 21:50 前后（687.339s） |
| 测试库名 | `test_hotel`（Django 自动建删） |
| 命令 | `.venv/Scripts/python.exe scripts/test_evidence.py --engine mysql --output docs/acceptance/evidence/g14-final-20261128-d68833e/mysql-d68833e.log`（DJANGO_ENV=test；`.env` 注入；`DJANGO_PLATFORM_ADMIN_USERNAME/PASSWORD` 已剥离） |
| 退出码 | 0 |
| 错峰确认 | 执行前后 `test_evidence.py` 内建检查无其它 `manage.py test` 进程；本轮为当时唯一 MySQL 全量 |

## 四、证据头「未提交变更 2 项」说明

A-01/A-02 证据头记「工作区含未提交变更 2 项」——逐项为：本轮证据目录自身（本文件与进行中的日志文件，未跟踪）+ 浏览器/服务运行产物；**均无业务代码改动**（A-01 起跑前 `git status --porcelain` 为 0 行，被测代码态 = HEAD `d68833e`）。
