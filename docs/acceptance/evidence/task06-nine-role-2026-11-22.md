# TASK-06 九岗位权限矩阵复跑报告（2026-11-22）

> 性质：验收证据 ｜ 触发：`1b882eb`（NA-13 夜审员岗位激活）后 V13-02 九岗位拒绝矩阵需按新权限面复跑 ｜ 结论：**PASS（failures=0）**

## 一、目的与范围

V13-02 Phase 7 在 `c4c15b6` 基线得出「夜审员分组无夜审权限」的实测结论，登记为 NA-13 缺口。`1b882eb` 按负责人 2026-11-22 批准，向「夜审员」组追加 `core.night_audit_view` + `hotelapp.night_audit_execute`（迁移 `hotelapp.0021`，add-only，不含 `business_date_reopen` / break-glass / 退款审批）。本次复跑验证两件事：

1. **授权生效**：夜审员能进夜审中心页面，夜审执行不再 403，且页面提供「执行夜审」入口；
2. **无权限外溢**：其余 8 个岗位在原拒绝面上的判定不变，夜审员也没有拿到营业日重开申请入口。

## 二、环境与账号

| 项 | 值 |
|---|---|
| 代码基线 | `0a3d3ce`（含 `1b882eb` 授权与 `a04ade5` TASK-01 落库） |
| 服务 | `manage.py runserver 127.0.0.1:8011 --noreload --nothreading`，dev MySQL（`DJANGO_DB_CONN_MAX_AGE=0`，避开开发库 `max_connections=20` 的连接争抢） |
| 浏览器 | Playwright Chromium headless，每账号独立 context（隔离 session，防串号） |
| 账号 | `v12_gm` 店长 / `v12_fd` 前台接待 / `v12_fin` 财务 / `v13_duty` 值班经理 / `v13_hk` 客房服务员 / `v13_night` 夜审员 / `v13_rest` 餐饮服务员 / `v13_chef` 餐饮主管 / `v13_sec` 停车安保，均 ACTIVE 且岗位归属与命名一致 |
| 脚本 | `scripts/acceptance/nine_role_matrix.py`（矩阵）+ `scripts/acceptance/nine_role_db_check.py`（DB 不变量） |

判定口径：拒绝格必须精确 403（结构化 `PERMISSION_DENIED`）；放行格只要求非 403（资源不存在或业务校验错误都算通过权限门禁）。因此矩阵不依赖具体业务数据 ID，也不产生高危写。

## 三、实测矩阵

| 场景 | 端点 | gm | fd | fin | duty | hk | **night** | rest | chef | sec |
|---|---|---|---|---|---|---|---|---|---|---|
| S1 退款执行 | POST `/api/v1/billing/refunds/999999/execute` | 403 | 403 | 400† | 403 | 403 | **403** | 403 | 403 | 403 |
| S2 退款审批 | POST `/api/v1/billing/refunds/999999/approve` | 400‡ | 403 | 403 | 400‡ | 403 | **403** | 403 | 403 | 403 |
| S3 夜审执行 | POST `/api/v1/night-audit/run`（date=1900-01-01） | SKIP | 403 | 403 | SKIP | 403 | **409★** | 403 | 403 | 403 |
| S4 电话揭示 | POST `/api/v1/guests/51/phones/47/reveal` | 200 | 200 | 403 | 200 | 403 | **403** | 403 | 403 | 403 |
| S5 房态放行 | POST `/api/v1/housekeeping/rooms/999999/status` | 404 | 403 | 403 | 404 | 403 | **403** | 403 | 403 | 403 |
| S6 财务视图 | GET `/api/v1/billing/refund-liabilities` | 200 | 403 | 200 | 200 | 403 | **403** | 403 | 403 | 403 |
| S7 报表导出（非法码） | GET `/api/v1/reports/export?report=__invalid__` | 400 | 400 | 400 | 400 | 400 | **400** | 400 | 400 | 400 |
| S7b 报表导出（occupancy） | GET `/api/v1/reports/export?report=occupancy` | — | 200 | — | — | — | **403** | 403 | 403 | 403 |
| S8 审计导出 | GET `/api/v1/audit/export` | 200 | 403 | 403 | 200 | 403 | **403** | 403 | 403 | 403 |
| PAGE 夜审中心 | GET `/manage/night-audit/` | 可访问 | 403 | 403 | 可访问 | 403 | **可访问★** | 403 | 403 | 403 |

注：
- ★ 本次与 V13-02 基线的**唯一实质差异**。409 的含义是「权限门禁已通过，业务校验拒绝不存在的营业日」，因此没有触发关账；`night_audit.run` 审计事件在运行前后均为 9 条。
- † `v12_fin` 持 `billing.refund_execute`，通过门禁后因退款单 999999 不存在而报业务错误，无真实退款。
- ‡ 店长/值班经理持 `billing.refund_approve`，同理无真实写。
- SKIP：店长/值班经理为夜审执行权限持有者，真实执行会关账推进营业日，沿用 V13-02 的跳过口径（正控已在 Phase 6/7 取证）；夜审员本次改用不存在的营业日，既能证明权限通过又不关账。

## 四、页面级断言（NA-13 直接验收点）

| 岗位 | 执行夜审按钮 | 营业日重开申请表单 | break-glass 记录面板 |
|---|---|---|---|
| 店长 / 值班经理 | 有 | 有（正控，本就持 `business_date_reopen`） | 有 |
| **夜审员** | **有** | **无**（未授重开权限，符合裁定） | 有（只读列表） |
| 其余 6 岗位 | 页面 403，元素均不存在 | — | — |

夜审员页面首屏文本实测包含「营业日与夜审中心」「财务与日结」「个人设置」等菜单与「营业日历、夜审预览与执行…」说明，确认是真实页面而非错误页；重开申请表单不存在，说明 `0021` 只授两码未溢权。

## 五、数据库不变量

| 判定 | 结果 |
|---|---|
| 营业日行未推进、`night_audit.run` 事件数未增（9→9） | 通过 |
| 目标房间房态未变化 | 通过 |
| 退款/收款事实未新增 | 通过 |
| 新增审计事件无夜审执行/退款执行/退款审批成功动作 | 通过 |

新增审计事件仅为 9 条 `auth.login_success` 与允许岗位的 `guest.pii_reveal.phone` / `audit.export` / `report.export`，均属正控取证范围，无被拒岗位留下的成功痕迹。

## 六、观察项（不阻塞）

break-glass「记录列表」面板在模板里未按权限隐藏，夜审员能看到只读列表；面板内的复核动作由 `can_review_break_glass` 与 API 权限双重把关，实测夜审员无复核权。是否对非复核岗位隐藏该面板属产品展示口径，登记待负责人裁定。

## 七、结论与复跑方法

- **PASS**：9 账号 × 9 端点 + 页面断言全部符合预期，failures=0；NA-13 授权生效且无权限外溢。
- 证据：本目录 `report.json`（逐格状态与页面元素布尔值）、`db-before.json` / `db-after.json` / `db-check.json`、`landing-*.png` ×9、`night-audit-*.png` ×9。
- 复跑：

```bash
# 1) 起 dev 服务（单线程、无连接池，避免开发库 max_connections=20 争抢）
DJANGO_ENV=dev DJANGO_DB_CONN_MAX_AGE=0 \
  .venv/Scripts/python.exe manage.py runserver 127.0.0.1:8011 --noreload --nothreading
# 2) 快照 → 矩阵 → 快照 → 核对
python scripts/acceptance/nine_role_db_check.py before
NINE_ROLE_BASE=http://127.0.0.1:8011 NINE_ROLE_RUN_ID=20261122 \
  python scripts/acceptance/nine_role_matrix.py
python scripts/acceptance/nine_role_db_check.py after
python scripts/acceptance/nine_role_db_check.py check
```

脚本依赖 `scripts/r6_env.txt`（gitignored）中的九岗位口令与 `.work/v12_accounts.json` / `.work/v13/v13_accounts.json`（本机账号索引，不入库）。
