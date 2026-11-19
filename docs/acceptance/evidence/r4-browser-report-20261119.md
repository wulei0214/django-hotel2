# R4 浏览器级验收报告（2026-11-19）

- 环境：本地 waitress http://127.0.0.1:8000（Django 4.1 PMS）
- 代码版本：main `8ebf918`（= 简报基线 `e56164d` + 1 commit；工作树另有 15 个未提交改动文件，属其他在途工作，本次验收未触碰）
- 浏览器：ZCode IAB 真实点击（1440x900），登录表单 `form.submit()`，按钮经 `evaluate(el.click())` 原子点击，原生 confirm 用 `getJsDialog`（本次流程未触发 confirm 场景）
- 账号：U-03 重置后 9 个测试账号口令统一为 `（口令已轮换，见受控配置）`（已在 DB 侧 `check_password` 核实；简报中 "test_housekeeping 口令 （历史临时口令，已失效，字面量不入档）" 已过期失效，首次登录实测失败并以此核实）
- 证据目录：`docs/acceptance/evidence/r4-browser-20261119/`（00–18 共 19 张截图）
- 纪律遵守：未 git commit；新增测试数据均带 r4 前缀；未删除既有走查数据（会员/房态/维修操作全部 round-trip 回原状态）

---

## 一、R4-02 十一中心 × 操作矩阵（test_owner 店长）

| # | 中心 | URL | 页面 | 核心区块渲染 | 抽屉/面板 | 真实操作点击（结果） | 截图 | 结论 |
|---|------|-----|------|-------------|-----------|---------------------|------|------|
| 1 | 预订与入住中心 | /manage/reservations/ | 200 | 列表+汇总（前任代理完成） | 预订抽屉 | 查看抽屉操作（前任代理完成） | 01 | PASS* |
| 2 | Guest 360 客人中心 | /manage/guests/ | 200 | 客人表 39 条+分页+搜索 | 详情抽屉（档案/预订记录/4 个操作表单） | 更新资料-备注填 r4 前缀提交 → 「操作成功」 | 02 | PASS |
| 3 | 会员中心 | /manage/memberships/ | 200 | 会员表 13 条+搜索 | 详情抽屉（等级变更历史/变更等级/暂停恢复） | 暂停→「操作成功」，恢复→「操作成功」（状态回到有效） | 03 | PASS（含 2 缺陷，见下） |
| 4 | 房态与库存中心 | /manage/rooms/ | 200 | 房态表 28 间+每日房价 70 行+库存 70 行 | 关房/开房抽屉 | 关房/开房提交 → 「网络错误」（404，缺陷 D-R4-04）；API 直连同请求 200 并 round-trip 恢复 | 04 | PASS（页面）/ FAIL（三个写操作，D-R4-03/04） |
| 5 | 清洁与维修工作台 | /manage/housekeeping/ | 200 | 今日清洁统计+房态网格 28 间+清洁任务池+维修工单（MC-006-R1 新形态已入库） | 维修登记弹层 | 2675 房维修登记（OUT_OF_ORDER）→ 200；维修恢复（NORMAL）→ 200（网格/统计同步刷新） | 05 | PASS |
| 6 | 财务管理 | /manage/finance/ | 200 | 今日汇总/待退款负债/账务调整/待财务核查/账单 2 张/流水/收银班次 4 个 | 账单详情抽屉（账本流水+支付退款+收款表单） | 查看 → 抽屉打开；开班 POST → 200 幂等（created:false，返回既有班次 #8）；关班未执行（避免改变既有班次状态） | 06, 07 | PASS |
| 7 | 营业日与夜审中心 | /manage/night-audit/ | 200 | 当前营业日/夜审两步执行/营业日历/重开申请/夜审历史/break-glass | 营业日详情抽屉（过租核对/异常房态/运行历史/重开审批） | ① 生成夜审预览 → 409 BUSINESS_CONFLICT（缺陷 D-R4-05）；详情 → 抽屉打开 | 08 | PASS（页面）/ FAIL（夜审执行，D-R4-05） |
| 8 | 报表分析中心 | /manage/reports/ | 200 | 7 类报表卡片+KPI+日报表+统计口径说明 | （该页无抽屉形态） | 查询 → 入住率表渲染（11-18/11-19 两行）；导出 CSV 点击 → 触发下载 | 09 | PASS |
| 9 | 审计与合规中心 | /manage/audit/ | 200 | 审计统计（7 天 314/30 天 363/敏感 22）+筛选+事件表 | 事件详情抽屉（before/after/请求链路） | 详情 → 抽屉打开；查询 → 列表刷新（可见本次 r4 操作产生的审计事件） | 10 | PASS |
| 10 | 通知与待办 | /manage/notifications/ | 200 | 类别/状态筛选+通知列表（21 未读） | （列表型页面） | 标记已读 → 未读 21→20（行状态同步「已读」） | 11 | PASS |
| 11 | 员工与账号中心 | /manage/staff/ | 200 | 建档表单+搜索列表+生命周期面板 | 员工操作面板抽屉（档案/任职/账号/授权 31 字段） | 新建员工「r4测试员工20261119」→ 200（工号 FO-0001，授停车安保角色 1 项生效，未开通登录账号）；管理抽屉打开 | 12 | PASS |

\* 预订与入住中心为前任代理中断前完成的 2 张截图（00/01），本轮复核其会话与导航一致。

---

## 二、R4-03 九岗位权限矩阵（浏览器级）

页面状态 = 登录该角色后同源 `fetch` 各中心 URL 的 HTTP 状态（已排除未登录重定向污染：每次探测均在目标角色会话内执行）。

### 5 个 ACTIVE 岗位

| 中心 | owner 店长 | front 前台 | finance 财务 | duty 值班经理 | housekeeping 客房 |
|------|-----------|-----------|-------------|--------------|-------------------|
| /manage/reservations/ | 200 | 200 | 200 | 200 | 403 |
| /checkin/ | 200 | 200 | 200 | 200 | 403 |
| /manage/guests/ | 200 | 200 | 200 | 200 | 403 |
| /manage/memberships/ | 200 | 200 | 200 | 200 | 403 |
| /manage/rooms/ | 200 | 200 | 200 | 200 | 200 |
| /manage/housekeeping/ | 200 | 200 | 200 | 200 | 200 |
| /manage/finance/ | 200 | **403** | 200 | 200 | **403** |
| /manage/night-audit/ | 200 | **403** | **403** | 200 | **403** |
| /manage/reports/ | 200 | 200 | 200 | 200 | **403** |
| /manage/audit/ | 200 | **403** | **403** | 200 | **403** |
| /manage/notifications/ | 200 | 200 | 200 | 200 | 200 |
| /manage/staff/ | 200 | **403** | **403** | **403** | **403** |

- 导航栏与页面权限一致（每个角色登录后截图核对：13=front、14=finance、15=duty、16=housekeeping）。
- 数据脱敏：front/housekeeping 工作台「消费统计」卡片显示占位文案「消费数据仅财务 / 值班经理 / 店长可见」；finance/duty 显示真实金额。符合口径。

### 4 个 PLANNED 岗位（登录成功、业务入口默认拒绝）

| 入口 | nightaudit 夜审员 | fnb 餐饮服务员 | fnbsup 餐饮主管 | parking 停车安保 |
|------|------------------|----------------|-----------------|------------------|
| 导航可见 | 仅 通知/主界面/个人中心 | 同左 | 同左 | 同左 |
| /manage/reservations/、/checkin/ | 403 | 403 | 403 | 403 |
| /manage/housekeeping/、/manage/rooms/ | 403 | 403 | 403 | 403 |
| /manage/finance/、/manage/night-audit/ | 403 | 403 | 403 | 403 |

（nightaudit/fnb 截图 17/18；fnbsup/parking 同构，状态码已逐项抓取。）

### 越权探测（API 级，浏览器会话内发起）

| 探测 | 结果 | 服务端响应 |
|------|------|-----------|
| front → POST /api/v1/night-audit/run | 403 | PERMISSION_DENIED「当前岗位没有该项夜审权限。」 |
| front → GET /api/v1/billing/ledger | 403 | PERMISSION_DENIED「员工任职或财务权限验证失败，无法访问财务中心。」 |
| front → GET /api/v1/staff | 403 | PERMISSION_DENIED「缺少员工管理权限。」 |
| housekeeping → GET /api/v1/billing/ledger | 403 | 同上财务拒绝文案 |
| housekeeping → POST /api/v1/billing/shifts/open | 403 | PERMISSION_DENIED「当前岗位没有收银班次管理权限。」 |
| housekeeping → GET /api/v1/staff | 403 | PERMISSION_DENIED「缺少员工管理权限。」 |

负向结论：页面 403 与 API 403 双层一致，无越权路径发现。

---

## 三、缺陷清单（记录不修，按级别排序）

| 编号 | 级别 | 位置 | 描述 | 复现证据 |
|------|------|------|------|---------|
| D-R4-04 | 高（功能不可用） | `templates/pms/management_center.html` L261（page 级 action 传 `a.endpoint` 原文，仅 row 级 L357 做 `.replace("{id}",...)`）；配置 `apps/api/pages.py` rooms actions | 房态与库存中心三个页面级写操作（调整每日房价 `/api/v1/daily-rates/{rate_id}/adjust`、关房/开房 `/api/v1/inventory/daily-rates/{rate_id}/close`、停售/开售 `/api/v1/inventory/stock/{inventory_id}/stop-sell`）的 URL 占位符不替换，提交打到字面 `{rate_id}` 路径 → 404 HTML → 前端统一显示「网络错误，请稍后重试。」误导排障。同构影响所有带路径参数的 page 级 action | 04-rooms-drawer.png；fetch 捕获 `POST /api/v1/inventory/daily-rates/{rate_id}/close → 404`；同体直连正确 URL 返回 200 changed:true |
| D-R4-05 | 高（流程阻断） | `apps/api/night_audit.py::night_audit_business_date_current`（L423-441，按 `property_date` 查当日行，无视 OPEN/REOPENED 行）vs `apps/billing/night_audit.py::_resolve_date`（L103-111，取最新 OPEN/REOPENED 行为当前营业日） | 营业日「当前」双口径不一致：物业存在 2026-11-18 REOPENED 行（DB 唯一 BusinessDate 行）时，夜审中心页面头显示「2026-11-19 营业中」（/business-dates/current 返回 exists:false, OPEN），而夜审守卫认定当前=2026-11-18 → ①生成夜审预览/②执行夜审必然 409 BUSINESS_CONFLICT，夜审在重开状态下完全无法执行 | 08-night-audit-drawer.png（页面 11-19 + 红色 409 文案 + 详情抽屉显示 11-18 已重开并存） |
| D-R4-01 | 中（功能不可用） | `apps/api/pages.py` membership_create.fields（只有 guest_id、level）vs `apps/api/staff_center.py::membership_create`（要求 member_no）+ `apps/guests/services.py::create_membership` | 会员中心「新增会员」抽屉缺「会员编号」输入框，payload 只有 `{guest_id, level}`，服务端必回 400「会员编号必须为 1 至 32 个字符。」→ UI 上永远无法新增会员 | fetch 捕获 body `{"guest_id":89,"level":"r4-MEM-20261119"}` → 400 |
| D-R4-02 | 中（功能不可用） | `apps/api/pages.py` membership_level action（发 `level`）vs `apps/api/staff_center.py::membership_change_level` L317（读 `body.get("level_code")`） | 会员「变更等级」字段名不匹配：前端发 `{"level":"GOLD","reason":...}`，后端读 `level_code` 得 None → 必回 400「会员等级代码必须为 1 至 32 个字符。」→ UI 上永远无法变更等级 | fetch 捕获 `POST /api/v1/memberships/23/level {"level":"GOLD",...} → 400`（03-memberships-drawer.png 可见错误文案） |
| D-R4-03 | 中（可用性） | `apps/api/guest_center.py::rate_calendar`（L614-624 rate_data 序列化不含行 id） | 房价日历 API 房价行/库存行均不返回 id，而房态中心操作表单要求填写「房价行 ID/库存行 ID」→ 用户无从得知合法 ID（本次以 DB 直查取得 218 才能测试后端）。与 D-R4-04 叠加使这三个操作在 UI 上双重不可用 | /api/v1/rate-calendar 响应 rate 行无 id 字段 |

备注（非缺陷）：
1. 简报口令过期：test_housekeeping 实际口令为统一口令 `（口令已轮换，见受控配置）`（（历史临时口令，已失效，字面量不入档） 已失效），已在 DB 核实并按统一口令完成验收。
2. 已知守卫按预期复现，不计缺陷：PENDING 订单直接入住 409（本轮未复演，前任走查已覆盖）、front/housekeeping 各 API 403 文案明确、PLANNED 默认拒绝。
3. 报表分析中心无抽屉形态（只读聚合页），以「查询+导出 CSV」替代抽屉操作要求。

## 四、测试数据与状态回归说明

- 新增：会员暂停/恢复历史 2 条（SELFTEST-MEM-001，原因 r4-browser-accept-20261119，最终状态=原状态「有效」）；审计事件若干（均为上述操作的审计痕迹，含 r4 原因）；员工 1 名「r4测试员工20261119」（工号 FO-0001，停车安保角色，未开通登录账号）；通知已读 1 条（21→20）。
- round-trip 回原状态：房 2675 维修登记→恢复（终态 NORMAL/当前可售）；房价行 218 关房→开房（终态 closed:false）。
- 未触碰：班次 #8 保持开班中（关班会对既有走查状态产生不可逆变更，故只验证了幂等开班）；账单/流水/预订数据零写入。

## 五、截图清单（docs/acceptance/evidence/r4-browser-20261119/）

| 文件 | 内容 |
|------|------|
| 00-login.png | 登录页（前任代理） |
| 01-reservations-drawer.png | 预订与入住中心抽屉（前任代理） |
| 02-guest360-drawer.png | Guest 360 详情抽屉 + 更新资料「操作成功」 |
| 03-memberships-drawer.png | 会员详情抽屉（等级变更历史/暂停恢复成功；变更等级 400 文案为 D-R4-02 证据） |
| 04-rooms-drawer.png | 房态中心关房/开房抽屉（「网络错误」= D-R4-04 证据） |
| 05-housekeeping-workbench.png | 清洁与维修工作台 MC-006-R1 形态 + 2675 维修恢复成功 |
| 06-finance-bill-drawer.png | 账单 #20 详情抽屉（账本流水/支付退款/收款表单） |
| 07-finance-shifts.png | 财务中心收银班次区（4 班次，#8 开班中） |
| 08-night-audit-drawer.png | 夜审中心（页面 11-19 vs 409 文案 vs 详情抽屉 11-18 已重开，D-R4-05 证据） |
| 09-reports-occupancy.png | 报表分析中心入住率报表（KPI+日报表+口径说明） |
| 10-audit-center.png | 审计与合规中心（统计+事件表含 r4 操作痕迹） |
| 11-notifications.png | 通知与待办中心（未读 20，首条已读） |
| 12-staff-center.png | 员工中心（r4测试员工20261119 建档成功横幅+操作面板抽屉） |
| 13-role-front-home.png | test_front 工作台（受限导航+消费统计脱敏） |
| 14-role-finance-home.png | test_finance 工作台 |
| 15-role-duty-home.png | test_duty 工作台 |
| 16-role-housekeeping-home.png | test_housekeeping 工作台（最窄权限） |
| 17-role-nightaudit-home.png | test_nightaudit（PLANNED，仅通知/主界面/个人中心） |
| 18-role-fnb-home.png | test_fnb（PLANNED） |

## 六、遗留项

1. D-R4-01/02/04 三处 UI 写操作缺陷需代码修复（均在管理中心的配置/前端胶水层，后端服务与 API 本身经直连验证可用），建议下一轮修复后回归：新增会员、变更等级、房态中心三操作、以及排查其他 center 的 page 级带参 action 是否同构受影响。
2. D-R4-05 需统一「当前营业日」解析口径（建议 `business-dates/current` 与 `NightAuditService._resolve_date` 共用同一解析函数），修复后在营业日重开状态下回归夜审两步执行。
3. D-R4-03 建议 rate-calendar 序列化补充行 id（或 UI 改为行内操作自动携带 id）。
4. 关账日拒写（WB-079）、PENDING 入住 409 引导文案本轮未复演（前任/白盒轮已覆盖），如需浏览器级证据可在修复回归时补拍。
5. PLANNED 岗位转正（激活授权）后的权限扩展矩阵未在本次范围。
