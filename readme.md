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

## 6. 文档索引

| 文档 | 内容 |
|---|---|
| [docs/五大模块重构需求设计方案.md](docs/五大模块重构需求设计方案.md) | 五模块重构需求基线 |
| [docs/值班经理授权场景设计.md](docs/值班经理授权场景设计.md) | 高危场景权限设计 |
| [docs/敏感字段权限边界设计.md](docs/敏感字段权限边界设计.md) | 字段级权限边界（待 D-09 确认） |
| [docs/角色权限验证指南.md](docs/角色权限验证指南.md) | 当前授权模型与回归场景（历史实测需重跑） |
| [docs/文档索引.md](docs/文档索引.md) | 文档导航与状态规范 |
| [docs/PMS2身份认证与员工住客账户体系改造开发方案.md](docs/PMS2身份认证与员工住客账户体系改造开发方案.md) | 身份认证改造评审方案 |
| [docs/代码与文档审查报告_2026-11-04.md](docs/代码与文档审查报告_2026-11-04.md) | 文档与代码一致性审查 |
| [docs/系统架构说明.md](docs/系统架构说明.md) | 架构说明 |
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
