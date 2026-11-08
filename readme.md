# 酒店管理系统（PMS）V2.0

> **架构**：Django 4.1 模块化单体（`apps/` PMS2 + `hotelapp/` 兼容层）+ MySQL 8 / SQLite
> **代码基线**：`main` 分支 | **测试**：174 项 | **CI**：GitHub Actions（MySQL 8 + SQLite 双引擎）

## 1. 项目现状

PMS2 已建立完整的业务骨架和权限体系。前台入住工作台（`/checkin/`）已使用 PMS2 API；
员工与账号管理工作台（`/staff_accounts/`）承载员工全生命周期；后台管理体系（Django Admin）
已收缩为平台维护/只读审计工具。

### 已交付

| 层次 | 内容 |
|---|---|
| **PMS2 模型** | Guest/GuestPhone/GuestDocument/GuestMembership、RoomType/Room/RatePlan/DailyRate/RoomTypeInventory、Reservation/ReservationRoom/ReservationGuest/Stay/StayGuest/StayRoomSegment、Folio/LedgerEntry/Payment/Refund/FolioTransfer、HousekeepingTask/MaintenanceTicket/RoomInspection、BusinessDate/NightAuditRun、InventoryHold、CancellationPolicy/DepositPolicy |
| **员工与授权** | StaffProfile/Membership/RoleAssignment + EmployeeLifecycleService + AuthorizationService + 员工工作台（/staff_accounts/） |
| **前台工作台** | /checkin/ PMS2 工作台 + /api/v1/front-desk/ 23 端点（排房/入住/收款/退房/续住/换房/No-Show/退款） |
| **审批** | ApprovalRecord 持久化 + ApprovalService（退款/重开双人 SoD）+ Break-glass（10 分钟限时） |
| **审计** | core.AuditEvent 只追加 + 实体时间线索引 + PII 揭示/角色/退款/薪酬等关键路径接入 |
| **P0 安全** | GET 写 405 / Admin PMS2 只读 / SQL 移除 / CSRF_USE_SESSIONS / Session 隔离；公开注册 User 创建与订单查询安全仍待收口 |
| **字段安全** | GuestDocument Fernet 加密 + HMAC 查重 + document_reveal/phone_reveal 独立权限 + PII 揭示审计 |
| **测试** | 174 项自动化测试（SQLite + MySQL 8 双引擎 CI） |

## 2. 技术架构

```
hotel/                          # Django 项目配置
apps/
  core/                         # Property、AuditEvent、LegacyRecordLink、BusinessDate、IdempotencyRecord
  identity/                     # StaffProfile、Membership、RoleAssignment、AuthorizationService
  guests/                       # Guest、GuestPhone、GuestDocument、GuestMembership
  inventory/                    # RoomType、Room、RatePlan、DailyRate、RoomTypeInventory
  reservations/                 # Reservation、ReservationRoom、Stay、StayRoomSegment、NoShowRecord
  billing/                      # Folio、LedgerEntry、Payment、Refund、FolioTransfer、CashierShift
  api/                          # /api/v1/ 前台工作台 + 客人中心 + 房态中心 + 员工中心 + 会员
hotelapp/                       # 旧业务兼容层（逐步退役中）
```

### 关键服务

| 服务 | 位置 | 职责 |
|---|---|---|
| EmployeeLifecycleService | `apps/identity/services.py` | 员工建档/开账号/分角色/调岗/离职唯一写入口 |
| AuthorizationService | `apps/identity/authorization.py` | 按物业+有效任职+角色检查动作权限 |
| GuestService | `apps/guests/services.py` | 客人建档/更新/电话/证件唯一写入口 |
| GuestMembershipService | `apps/guests/services.py` | 会员关系创建/等级变更 |
| FrontDeskLifecycleService | `apps/reservations/front_desk_services.py` | 排房/入住/收款/退房 |
| LifecycleService | `apps/reservations/lifecycle_services.py` | 续住/换房/No-Show/押金/退款审批 |
| NightAuditService | `apps/billing/night_audit.py` | 营业日/夜审/重开审批/Break-glass |
| BillingService | `apps/billing/services_v2.py` | 收款/退款/冲销/班次/财务汇总 |
| DashboardService | `apps/api/dashboard.py` | PMS2 权威仪表盘聚合 |

### 九角色 RBAC

| 角色 | 编码 | 关键边界 |
|---|---|---|
| 店长 | PROPERTY_MANAGER | 全模块 + 高危审批 + 薪酬修改 |
| 值班经理 | DUTY_MANAGER | 运营异常 + 退款复核 + 夜审 + 净房放行；**不可看薪酬** |
| 前台 | FRONT_DESK | 预订/入住/退房/续住/换房/取消已确认；**不可冲账/退款/审批** |
| 财务 | FINANCE | 收支录入 + 退款执行 + 薪酬只读；**不办入住/改房态** |
| 客房服务员 | HOUSEKEEPING | 清洁链至待检查；**净房放行归管理者** |
| 夜审员 | NIGHT_AUDIT | 预留（当前值班经理/店长兼岗） |
| 餐饮×2 / 停车 | FNB/PARK | 预留（模块对接第三方） |

## 3. 页面入口

| 页面 | URL | 说明 |
|---|---|---|
| **PMS 员工登录** | `/login/` | 员工登录入口（左右分栏企业级版式 v4，[截图](docs/screenshots/login-pms-v4.png)）；放行条件=有员工任职关系（D-04）；`next` 回跳经 `url_has_allowed_host_and_scheme` 安全校验 |
| **后台登录** | `/admin/` | Django Admin 平台维护入口（simpleui 主题，[截图](docs/screenshots/login-admin.png)），仅超管 |
| 注册（已止血） | `/register/` | AUTH-01：不再创建任何 User，302 → `/reserve/` 免注册直订 |
| 主界面 | `/home/` | PMS2 权威仪表盘（[截图](docs/screenshots/home-pms2-dashboard.png)）：统计卡/四图表/预警/财务卡全部来自 PMS2 模型 |
| 前台工作台 | `/checkin/` | PMS2 排房/入住/收款/退房（旧页 `/checkin/legacy/`） |
| 员工与账号管理 | `/staff_accounts/` | 员工全生命周期（店长） |
| 权限矩阵 | `/permissions/` | 九角色权限分配（超管） |
| 系统信息 | `/system/` | 环境/会话/个人信息 |
| 顾客预订 | `/reserve/` | PMS2 公开直订（唯一顾客入口）；旧 `/booking/` 已冻结（AUTH-02） |

**双会话隔离**（SplitSessionMiddleware）：PMS 登录写 `sessionid` cookie，Admin 登录写 `admin_sessionid` cookie，两套会话独立——员工登录 PMS 后仍可另登 Admin，互不踢线；CSRF 令牌存会话（`CSRF_USE_SESSIONS`）。

## 4. 本地开发

```bash
# 环境
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 环境变量（.env）
DJANGO_SECRET_KEY=...
DJANGO_DEBUG=1
DJANGO_DB_ENGINE=mysql
DJANGO_DB_NAME=hotel
DJANGO_DB_USER=hotelhotel
DJANGO_DB_PASSWORD=hotelhotel

# 启动
python manage.py migrate
python manage.py seed_role_test_accounts   # 九角色测试账号（密码 Abc123456）
python manage.py seed_hotel_data           # 真实模拟酒店数据
python manage.py runserver 0.0.0.0:8000 --noreload
```

### 测试账号

| 角色 | 账号 | 密码 |
|---|---|---|
| 前台 | test_front | Abc123456 |
| 值班经理 | test_duty | Abc123456 |
| 财务 | test_finance | Abc123456 |
| 店长 | test_owner | Abc123456 |
| 客房服务员 | test_housekeeping | Abc123456 |
| 夜审员（预留） | test_nightaudit | Abc123456 |

## 5. 测试与 CI

- `python manage.py test --noinput` — 174 项测试
- CI：GitHub Actions（MySQL 8 + SQLite 双引擎），push/PR 自动触发
- 验证指南：[docs/角色权限验证指南.md](docs/角色权限验证指南.md)

### 5.1 全站 UI 令牌化改造与七维 UX 审查（2026-11-04）

**改造（批次 1–3）**：site.css 建立 `:root` 双层设计令牌（品牌色/语义四角色/字阶/间距/圆角/三层阴影）；全模板 60 处内联样式清退为工具类、15 个页面 `<center>` 全部移除；finance 裸表格转 data-table；删除 12 个零引用死 CSS（flexslider/superfish/themify-icons 等）；修复 4 处结构 bug（`_sidebar.html` 重复 `side-user` 开标签、`html1.html` 裸 div 占用 body 位、`vip.html` 缺 `</div>`、`index.html` 缺 `</body>`）；CSS 引用统一 `site.css?v=12`。

**七维审查（better-ux-review）结论：Approve（HIGH 全部修复）**。证据与修复（对比度均经 WCAG 实测）：

| Severity | 维度 | 位置 | Before → After | Why |
|---|---|---|---|---|
| HIGH | 色彩 | `static/css/site.css:926` | `.btn-danger-solid` 白字/`#f56c6c` 实测 2.90:1 → 新令牌 `--color-danger-strong:#d93026`（4.77:1） | 危险按钮标签是常字号文本，必须 ≥4.5:1 |
| HIGH | 色彩 | `static/css/site.css:542-545` | ok/void/warn 徽章文字用 `-solid` 浅色字（2.0–2.6:1）→ 改用加深的 `-text` 令牌（4.74–5.38:1） | 状态徽章文字达不到可读门槛 |
| HIGH | 无障碍 | `static/css/site.css:743` | `.badge-toggle` 命中区约 22px → `::after inset:-10px` 扩至约 44px | 命中区过小，误触率高 |
| MEDIUM | 无障碍 | `templates/permissions.html:80` | 高危操作表无空状态 → 补 `{% empty %}` 行 | 空数据时表格只剩表头，无法区分"无数据/加载失败" |
| MEDIUM | 界面打磨 | `static/css/site.css:122,351` | 2 处原始 box-shadow → `var(--shadow-sidebar)`/`var(--shadow-pop)` | 阴影必须走令牌，避免散点值回潮 |
| MEDIUM | 排版 | `static/css/site.css:514,683` | 页面/列表标题 22px → `var(--text-title)`（20px） | 字号归阶，不游离于字阶之外 |
| LOW | 界面打磨 | `static/css/`（12 个文件） | flexslider/superfish/themify-icons 等零引用死 CSS → 全部删除 | 死代码仍含 20 处 `transition:all` 违规示范 |

复测：`btn-primary` 4.73:1、正文 13.02:1、次要文字 6.11:1、accent 文字 4.95:1 全达标；`#909399` 弱文字（3.08:1）仅限占位/禁用场景，信息性文字用 `--color-text-secondary`。键盘焦点环（`:focus-visible` 全局）、`tabular-nums` 数字列对齐、`{% empty %}` 空状态（16/17 表格，frontdesk 由 JS 兜底）均已在位。161 项测试通过。

### 5.2 PMS 侧边栏重组 + 登录改版 + 详情抽屉（2026-11-04，依据五大中心评审方案 P0/P1）

依据《PMS 2.0 五大业务中心及整体界面改版评审方案》完成 P0 全部与 P1 首项：

- **侧边栏按业务域分组**（[截图](docs/screenshots/sidebar-groups.png)）：运营 / 前台运营 / 客人关系 / 客房运营 / 员工组织 / 财务与日结 / 系统管理 七组，全部入口补齐 Material 图标；五个旧页面（员工管理/顾客管理/客房管理/会员信息管理/客房订单管理）折叠进「历史页面（过渡期）」组（原生 `<details>`，当前页自动展开），等待 P1-12 分阶段退役。
- **员工登录页 v4 左右分栏**（[截图](docs/screenshots/login-pms-v4.png)）：左深海军蓝品牌区（香槟金点缀）+ 右暖白表单区；移除「注册账号/顾客预订」链接（员工/顾客身份分离，顾客自注册保留在 `/register/` 与 `/reserve/`）；新增显示密码切换与登录加载态；label/for 显式绑定。
- **next 回跳安全校验**：`login_view` 改用 `url_has_allowed_host_and_scheme`，阻断 `//外部域名` 协议相对地址开放重定向。
- **管理中心详情抽屉**（[截图](docs/screenshots/reservation-drawer.png)）：员工中心 / Guest 360 / 会员中心 / 预订与入住中心四页列表行新增「查看」→ 调用既有详情 API（`/api/v1/staff/{id}`、`/api/v1/guests/{id}`、`/api/v1/memberships/{id}`、`/api/v1/front-desk/reservations/{booking_no}`）→ 右侧抽屉渲染概览 KV + 关联记录表（岗位授权/预订记录/等级历史/房间明细与 Stay）；按方案 §七 不新增 HTML 详情路由；房态中心无详情 API，不放「查看」按钮。
- **种子数据补齐**：`seed_hotel_data` 幂等补建 `GuestPhone`（列表脱敏列数据源）与约 1/3 客人的 `GuestMembership`（STANDARD/ADVANCED/ELITE 分布）。

161 项测试通过；店长账号浏览器实测四中心抽屉全部正常。

### 5.3 数据源澄清 + 全站枚举中文化（2026-11-04）

**两个工作台的数据源结论**：
- `/home/` 主界面：**PMS2 新模型**（P1-11 切换）。本次修复遗留断裂——视图已切 `dashboard_data` 但模板仍绑定旧扁平键、图表数据未传递，导致页面全空；现统计卡按嵌套键绑定，新增 `dashboard_charts/dashboard_warnings/dashboard_finance` 三个 PMS2 聚合服务（房态分布/房型库存/会员分布/证件登记/预警/财务卡，店长以上可见）。
- `/checkin/` 前台接待工作台：**PMS2 新模型**（`frontdesk.html` 走 `/api/v1/front-desk/*`）；旧模型页面在 `/checkin/legacy/` 只读过渡。

**全站枚举中文化**（数据库仍存英文码，展示层翻译）：
- 新建 `apps/core/labels.py` 中央标签模块：模型 `TextChoices` 中文 label 直接派生（房态三维/预订/入住/雇用/会员状态），无 choices 的枚举（币种/会员等级/账单摘要状态）集中定义。
- API DTO 层统一输出 `*_label` 字段：`guest_center`（会员等级/状态、三维房态、币种、预订状态）、`staff_center`（雇用/任职状态、等级/状态）、`front_desk`（账单状态含"未开账"、币种、Stay 状态、房态三维、电话预订来源）。
- 五中心列配置与详情抽屉全部切到 `*_label` 路径；`frontdesk.js` 房卡/详情原码显示点改用标签字段。
- 主界面图表全部中文图例；`seed_hotel_data` 新增"今日运营快照"（在住 2 间+今日预抵 2 单+过期预警 1 单，幂等）。

161 项测试通过；浏览器实测主界面/会员中心/预订中心/房态中心中文显示全部正确。

### 5.4 身份认证 Phase 0 止血（2026-11-04，依据身份账户体系改造方案）

按方案"开发纪律"（D1-D12 未批准仅做盘点/测试/止血）完成 Phase 0 全部 10 项：注册止血（/register/ 停建 User → 302 /reserve/）、冻结 /booking/ 与 /cancel_booking/ 旧写、登录失败对外统一文案（细分原因入 security 日志）、validate_password 覆盖全部密码设置路径、业务授权清除三处 User.groups/has_perm 直读（approval/财务明细/PII reveal → AuthorizationService）、生产环境拒绝 mock 支付、订单查询/取消 AuditEvent 数据库限流（10 次/5 分钟）+ 审计。新增 12 项加固测试，全量 174 项通过；实施状态表见方案文档末节。Phase 1（邀请激活）待 D1/D6/D12 批准。

### 5.5 身份账户体系 2.0 Phase 0（2026-11-04，正式开发基线）

按 2.0 方案 §14 Phase 0 顺序完成 AUTH-01~13 全部任务：RateLimitCounter 独立限流（原子计数/TTL/HMAC 指纹/空值不建桶/fail-closed/429+Retry-After）；BookingAccessToken 订单访问令牌（创建一次性返回、查询/取消/支付意图全凭 token、字段白名单脱敏）；公开预订防刷（IP+phone 双维、PENDING 上限 2 笔、hold TTL 释放命令）；api_guard 修复；登录持久审计+限流+disabled 分支修正；权限语义修正（证件揭示统一 hotelapp.guest_document_reveal、财务查看与退款审批分离、superuser 直通清除）；33 个旧写路由 410 冻结；单店边界（非 DEFAULT 任职明确拒绝+审计）；seed 生产门禁。新增测试至 181 项，SQLite+MySQL 双引擎全过；端到端浏览器验证审计链完整。逐项状态见方案文档 §21。

### 5.6 公开预订页「续付 + 令牌展示」修复（2026-11-07，用户报告：模拟支付按钮点不了）

用户反馈"订单已创建但本地模拟支付按钮不能点击"。浏览器端到端复现结论：下单→支付主流程本身正常（真实浏览器实测 直达 CONFIRMED）；真实缺口是**页面刷新/关闭后订单无法找回**——① 访问令牌从未渲染给用户（提示"请妥善保存"但值未展示），刷新后既无按钮也无令牌；② `simulatePayment` 缺少 `mock_callback_example` 的早期 return 分支不恢复按钮禁用态（隐患）。修复：预订确认区新增**访问令牌展示行**（等宽字体可复制）；新增「04 / RESUME PAYMENT 继续支付」面板，凭预订编号+令牌找回待支付订单（P0-08 口径：token 为唯一凭据，phone 不参与鉴权，复用既有 `GET /api/v1/reservations/<no>?token=` 契约）；两处缺陷补注释。回归（真实浏览器）：新订→令牌展示→**刷新页面**→续付表单找回→支付 CONFIRMED 全链路通过。遗留：用户卡单 `R6D5F259EF3521662905D`（修复前创建、令牌未展示、已过保留期）已通过领域服务清理为 EXPIRED。

### 5.7 新人文档《PMS业务流程与名词说明》+ 收款方式下拉修正（2026-11-07，用户报告：收款报"超过应收余额"）

用户报告前台"登记线下收款 100 → 报应收余额 0.00；押金 50 通过后再收款 → 报应收余额 −50.00"。核实为**正确的业务守卫**而非缺陷：应收余额公式 `outstanding = 账本余额 + 未入账合同房费 − 已入账房费`（`billing/services_v2.py collect_payment`），预付全款客人入住时应收恰为 0，收款（≤应收）被拒；押金通道允许预存（累计 ≤ 房费总额），收 50 后余额转负再收款自然被拒。交付：① 新增 `docs/PMS业务流程与名词说明.md`——名词表（Folio/LedgerEntry/应收余额/押金/预存款/收银班次/备用金/房态三维等）、余额三态解读、预订状态机、预订→支付→入住→退房→退款全流程走读、常见报错速查表、新人误区与代码索引，全部条目标注代码出处；② 修正 `frontdesk.js` 收款方式下拉：移除后端不支持的「其他(OTHER)」选项（后端仅 CASH/CARD/TRANSFER，原样提交必 409），node --check 通过。

### 5.9 前台详情抽屉「记录时间」字段对齐修复（2026-11-07，用户报告：字段靠左展示）

用户截图反馈详情抽屉「记录时间」区 订单创建/最近更新 标签漂在行中部。根因：`.fd-timeline-item` 用 `justify-content:space-between` 把 时间线圆点/标签/值 三元素均匀撑开。修复：改为圆点+标签贴左（`gap:8px` + `span{margin-right:auto}`），时间值保持右对齐；`frontdesk.css` 缓存版本 v=4→v=5。活页截图验证对齐生效（测试环境强制渲染抽屉填充样例值比对）。

### 5.11 PMS3.0 方案 V2.2 执行契约补强（2026-11-07，把“声明”落成“可执行约束”）

在业务代码 HEAD `9542b8b`（公共预订专项证据仍取 `7a2c9b8`）上继续修订 `docs/PMS3.0全项目开发最终方案.md`（§22 修订记录 V2.2 行），不新增业务功能：①**现状唯一口径**——新增 §2.1 PMS2 事实表，统一 729/887、SQLite 887、MySQL 1045 未执行、Redis 已部署但 G09 待复核、G10 完成、A04/阶段四/G14 未完成、mock 支付边界及 A16 内部 Redis 表述冲突。②**中心↔任务对应**——新增 §5.0（11 中心×REQ×BIZ×工程任务×现状）、§15.4（BIZ→DB/SVC/API/UI/QA 承接）、§15.5（11 中心 S01~S05 检查点矩阵），并同步 §14 Phase 1~6；新增 SVC-11（会员收口）/SVC-12（审计查询导出归档）。③**生命周期**——§24 增加 Reservation/PaymentIntent/ProviderTransaction/Payment/Refund/LedgerEntry/InventoryHold 状态迁移表、事件与全局锁顺序、异常补偿矩阵、S01~S11 验收矩阵和 10 项 P0 整改（含 `process_mock_payment_callback` 把已扣款 Payment 置 FAILED 的缺陷）。④**一致性/后台任务/恢复**——§23 增加 Outbox 契约、幂等键、Saga 补偿与事件目录；§25 增加 12 项后台任务注册表（锁键/幂等/死信/恢复/指标）、恢复 runbook 和监控指标。⑤新增 DEC-31（支付成功与订单终态竞态）/DEC-32（事件权威时间与迟到回调）。自检：BIZ 105、工程任务 108、DEC 32、INV-A 16，REQ↔BIZ 无缺项、任务 ID 无重复定义、无孤儿章节引用。

### 5.10 PMS3.0 方案 V2.1 针对性修订（2026-11-07，用户五项复核意见落地，不推翻重写）

在最新 main（业务代码基线 7a2c9b8、文档时点 HEAD 9542b8b）上对 `docs/PMS3.0全项目开发最终方案.md` 做定点修订（§22 修订记录 V2.1 行）：①**任务编号冲突**——AR-06（Room 状态直写）收口归属由 SVC-02/04/05 更正为 SVC-07（§3.4/§14.0/§15.3 三处同步）；GATE-02/04 明确为 PM-02/03 的门禁复核视图不重复建单；§26.1 新增前缀消歧（AUD=夜审、CMP=审计合规、INV 跨域归属说明）与**同范围任务去重归属表**（6 对近似任务指定唯一执行归属）；RSV-18（渠道侧对账）与 FIN-07（PMS 内部对账）分工。②**中心清单↔任务对应**——§5 前言冻结 11 中心口径，明确「权限管理/系统设置」为 IAM/INF 域页面不设中心任务。③**现状矛盾**——§1.1 七项门禁逐项标注状态（G10 完成、Redis 部署验证完成、A04/A09/阶段四/G14 待）、§7.4 OpenAPI 97→**102** paths、§2 补文档时点 HEAD、§0.3 能力继承措辞校准。④**生命周期细节**——§24.3 声明 PaymentIntent/ProviderTransaction 模型当前不存在、严禁复用 Payment 表承载意图状态（BIZ-RSV-14 为新建）；§24.4 九类异常用例绑定 QA-04/QA-06/REL-09；DEC-25 补库存 Hold 延长/释放规则；新增 DEC-30（发票范围决策）。⑤**一致性/后台任务/恢复**——§9.3 十一项后台任务逐条绑定唯一执行任务编号，任务框架（租约锁/重试/死信/重放）统一由 SVC-10 交付、禁止自建调度。全文交叉引用自检：BIZ 105/工程 106/DEC 30/INV 16，引用无缺失、注册无冲突。

### 5.8 公开预订订单生命周期闭环整改（2026-11-07，外部评审落地：SEC-01~06 + 十一场景验收）

对 `635d9dd..b737489`（预订页令牌+续付）的外部评审逐条落地，审查报告见 `docs/audit/phase0/90-public-booking-order-lifecycle-review.md`：① **SEC-01 安全修复**——续付曾用 `?token=` query（落服务器访问日志/浏览器历史），已删除 `reservation_detail` 的 query 回退，令牌仅经 `X-Booking-Token` 头传递，OpenAPI 契约与快照（102 paths）同步，`test_sec01` 断言 query 传参一律 404；② **UI 整改**——凭证独立区（02 / BOOKING CONFIRMATION）+ 创建时一次性完整展示令牌 + 复制按钮（clipboard API/execCommand 回退）+ 复制成功反馈 + 找回后脱敏（前4••••••••后4）+ 04 面板升级「ORDER RETRIEVAL 订单查询与管理」+ 状态门控（PENDING→支付/取消，其余状态仅查看，中文状态标签对齐 Reservation.Status）；③ **验收测试**——新增 `apps/api/test_public_booking_lifecycle.py` 11 项（S1 令牌一次性返回/S2·S11 无 Cookie 找回/S3·S4 错凭据 404 无泄露/S5 PENDING 可支付/S6 CONFIRMED 关支付入口/S7 连点单笔支付/S8 响应丢失重查/S9 已入住取消被拒/S10 爆破限流/SEC-01）；④ **六项安全要求逐条核验达标**（256bit 熵/SHA-256 摘要/限流/幂等/取消审计/token 不进 URL），员工邀请链接 URL 传参登记为独立决策项；⑤ 令牌丢失政策：无手机号绕过，PENDING 过期自动释放、已付订单走店内前台核验。SQLite 全量 **887 项通过**（DJANGO_ENV=test；dev 环境运行命令类测试会被库门禁正确拒绝）。

### 5.11 PMS 2.0 门禁收口（2026-11-07，五项优先门禁机器证据全部完成）

按用户指令优先完成五项门禁，证据报告 `docs/acceptance/95-gate-closure-evidence-20261107.md`（基线 c402a37）：①**完整测试证据**——修正引擎标注（settings 读 `DJANGO_DB_ENGINE`，此前误传 `DB_ENGINE`，历史"SQLite"实为 MySQL；诚实声明入报告 §1），取得真实双引擎结果：**MySQL 887 项全过 264.9s + SQLite 887 项全过 242.9s**；②**A04**——"DBA 授权"阻塞经 test_hotel 库级自带建/删权绕过（无需 root）：空库全量迁移 97 条全过/70 表/138 FK/53 唯一/无漂移；升级库=mysqldump 备份（2.37MB）还原后 migrate 无待应用 + 74 表行数对账零差异 + Folio 余额重算零差异 + 营业日单开零违例；③**G09**——双 waitress worker 跨进程共享证明（A 写缓存→插探针 Folio→B 在 TTL 内仍读旧值；62s 后 B 读新值=TTL）+ 故障降级（缓存层 ConnectionError fail-closed，核心页面 200 存活）+ 容量观察（本地 max_connections=20，4 实例并发触发 1040，写入 DEC-29 部署输入）；④**阶段四机器可执行项**完成（备份恢复演练/账号任职授权完整性抽查/业务闭环数据抽查），剩用户亲自项：九角色真实口令登录、业务走查、数据治理确认；⑤**G14 签署页** `docs/acceptance/96-g14-signoff.md` 备好（R1~R8 复核清单+证据索引+签署栏），按开发与验收分离**不代签**，待用户指派独立验收人。A16 §19 门禁行与 PMS3.0 方案 §1.1 已同步。

### 5.12 用户本地验收 8 项问题修复（2026-11-08，阶段四测试反馈）

用户以九角色账号实测发现 8 项问题，全部修复并浏览器/HTTP 双重自测：①**角色账号口令**——9 个 `test_` 账号统一重置为 `Abc123456`（staff_hk_01 等非 test_ 账号系历史交互式口令，随 ⑧ 停用）；②**财务中心 500**——根因是本机 Redis 未运行时 `billing_center.py` 缓存读裸调抛 ConnectionRefused；修复=缓存读写 try/except 降级直查数据库（缓存非权威，INV-A09；DEC-18 缺省策略）+ `.env` 测试期切 `DJANGO_CACHE_BACKEND=locmem`（零 Redis 依赖，恢复 Redis 后改回）；③**侧边栏「历史页面（过渡期）」组**整体移除（12 行，过渡已完成）；④**财务最近流水凭证号中文化**——`referenceLabel()` 映射 7 类前缀（房费入账·入住单#N/前台人工收款/本地模拟支付/押金入账/收款入账/退款出账/红字冲销/夜审过账），原始凭证号保留在悬停 title 与账本权威记录；⑤**夜审阻断重复文案**——渲染层按 (code,entity.id,message) 去重守卫；⑥a**夜审 4 项 critical 阻断**——两层修复：4 笔 legacy 在住行按合同总价均摊回填 `nightly_rates`（尾差归末晚，合计=合同额断言）+ 夜审 preview 增**房价窗口守卫**（营业日不在 [check_in,check_out) 的入住单不属当晚应过租范围，与 no-show 候选查询同口径；run() 复用 preview 天然同口径）→ 阻断 0/can_run/3 笔 1964 CNY；⑥b**营业日历/夜审历史 "params is not defined"**——查询守卫重构遗留的两处 `params.toString()` 改用 `query`；⑦**客源报表 length of undefined**——`guests_report` 补 `series` 别名键（前端统一读 series，by_source 保留兼容）；⑧**九角色收敛**——仅保留 9 个 `test_` 账号（前台/值班经理/财务/店长/客房/夜审/餐饮×2/停车），13 个非 test_ 账号停用。**额外发现并修复**：test_duty/test_housekeeping 的 StaffRoleAssignment `ends_at` 已过期（此前测试遗留）→ 恢复窗口+审计事件。测试注：夜审员（night_auditor）系 registry ROLE_PLANNED 预留空组（0014 决策 5 夜审由值班经理/店长兼岗），其 API 403 属设计行为。回归：MySQL 887 项全过（267.8s）。/reserve/ 带中文后缀 404 系聊天链接连括号复制所致，非应用缺陷。

### 5.13 全新数据全链路业务串测（2026-11-09，36/36 PASS，暴露并修复 4 个业务缺口）

按用户指令"删除所有测试数据重新创建并全链路串测"：新增 `scripts/e2e_full_chain.py`（带环境守卫：非本机 DEBUG dev 直接拒绝；内置清库复位=54 张交易表 TRUNCATE + `inventory_roomtypeinventory.sold/held` 归零 + 房态复位 VACANT/CLEAN——原"保留主数据"策略遗留的计数器残留会让可售量恒 0，本次串测第一课）。串测以**真实 HTTP 接口**驱动全链 36 项断言（公开预订→模拟预付→前台开班→押金→排房→入住→在住收款→夜审过账关账→退房拒未结清→补收→两讫→清洁 DIRTY→CLEANING→INSPECTING→值班经理放行→退款申请/审批/执行+SoD 反例→审计/报表/顾客侧令牌状态同步互通），最终 **36/36 PASS**。过程中暴露并修复 4 个真实业务缺口：①**PENDING 订单无任何确认路径**（收款只收已确认、入住又要已确认；生产未接支付渠道时公开预订将永久卡死）→ 定为**前台收款/押金即确认**：`collect_manual_payment` 与 `BillingService.record_deposit` 在交易成功后执行与在线支付回调同口径的 `confirm_inventory_for_reservation` + 状态迁移 CONFIRMED + 审计（`reservation.confirmed_at_frontdesk`），押金入口（lifecycle_services）与押金服务（services_v2）双层放行 PENDING；②**退房与夜审双记房费**——退房原逻辑整单入账 `stay-room-charge:{pk}`，夜审已按晚入账（`night-audit:*:stay:{pk}`）后重复 → 退房改**净额入账**（本间合同额 − 本间已入账，归集按 `:stay:{pk}` 尾缀+精确凭证号，多房共册不串账，回归用例 partial_checkout 佐证）；③**清库后库存计数器/房态残留**——主数据保留策略须同步复位交易计数器；④**退款执行前置**——财务执行现金退款需本人班次且有足额现金（先 `shifts/{id}/movements` direction=IN 补入备用金，属正常流程非缺陷）。回归：MySQL 887 项全过（含 partial_checkout/prepaid/no_show 多房用例）。注：test_hotel 残留会致测试 runner 交互确认 EOF 失败，重跑前 `DROP DATABASE test_hotel`。

## 6. 文档索引

| 文档 | 内容 |
|---|---|
| [docs/五大模块重构需求设计方案.md](docs/五大模块重构需求设计方案.md) | 五模块重构需求基线 |
| [docs/值班经理授权场景设计.md](docs/值班经理授权场景设计.md) | 高危场景权限设计 |
| [docs/敏感字段权限边界设计.md](docs/敏感字段权限边界设计.md) | 字段级权限边界（待 D-09 确认） |
| [docs/角色权限验证指南.md](docs/角色权限验证指南.md) | 当前授权模型与回归场景（历史实测需重跑） |
| [docs/文档索引.md](docs/文档索引.md) | 文档导航与状态规范 |
| [docs/PMS2身份认证与员工住客账户体系改造开发方案2.0.md](docs/PMS2身份认证与员工住客账户体系改造开发方案2.0.md) | 身份认证改造评审方案 |
| [docs/代码与文档审查报告_2026-11-04.md](docs/代码与文档审查报告_2026-11-04.md) | 文档与代码一致性审查 |
| [docs/系统架构说明.md](docs/系统架构说明.md) | 架构说明 |
| [docs/PMS业务流程与名词说明.md](docs/PMS业务流程与名词说明.md) | 新人上手：PMS 名词解释、账本规则、全业务流程走读（2026-11-07） |
| [docs/更新日志.md](docs/更新日志.md) | 变更日志 |

## 7. 剩余工作

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 剩余 | 五模块只读页面原型（HTML+JS 消费 API） | 待开发 |
| Phase 2 | 写 API（Guest 编辑/Rate 调价/Inventory 调整，走领域服务） | 待开发 |
| Phase 3 | Guest 合并/会员权益/清洁工单闭环/收银台/经营报表 | 待开发 |
| Phase 4 | LegacyRecordLink 填充/旧入口退役/旧表归档 | 待开发 |
| P1 | 字段级遮罩实施（Q-01~Q-04 已定，基础设施就绪） | 待开发 |
| P1 | ApprovalRecord 接入退款审批 API 端点 | 待开发 |

## 8. 关键业务规则

| 规则 | 来源 |
|---|---|
| 快捷退房余额≠0 阻断 | Q-08 |
| 换房首期限定同房型同价 | Q-09 |
| No-Show 无政策不没收押金 | Q-10 |
| 退款申请人≠审批人 | Q-05 |
| 营业日重开须另一位经理审批 | Q-14 |
| 敏感揭示须 AuditEvent | Q-04 |
| PMS2 为唯一业务写权威 | §12.1 |
| Django Admin 为只读审计工具 | §12.2 |
