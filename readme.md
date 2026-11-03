# 酒店管理系统（PMS）V2.0

> **架构**：Django 4.1 模块化单体（`apps/` PMS2 + `hotelapp/` 兼容层）+ MySQL 8 / SQLite
> **代码基线**：`main` 分支 | **测试**：162 项 | **CI**：GitHub Actions（MySQL 8 + SQLite 双引擎）

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
| **P0 安全** | 注册止血 / GET 写 405 / Admin PMS2 只读 / SQL 移除 / CSRF_USE_SESSIONS / Session 隔离 |
| **字段安全** | GuestDocument Fernet 加密 + HMAC 查重 + document_reveal/phone_reveal 独立权限 + PII 揭示审计 |
| **测试** | 162 项自动化测试（SQLite + MySQL 8 双引擎 CI） |

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
| 主界面 | `/home/` | PMS2 权威仪表盘 |
| 前台工作台 | `/checkin/` | PMS2 排房/入住/收款/退房（旧页 `/checkin/legacy/`） |
| 员工与账号管理 | `/staff_accounts/` | 员工全生命周期（店长） |
| 权限矩阵 | `/permissions/` | 九角色权限分配（超管） |
| 系统信息 | `/system/` | 环境/会话/个人信息 |
| 顾客预订 | `/booking/` | 公开预订 |
| Django Admin | `/admin/` | 平台维护（独立 session） |

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

- `python manage.py test --noinput` — 162 项测试
- CI：GitHub Actions（MySQL 8 + SQLite 双引擎），push/PR 自动触发
- 验证指南：[docs/角色权限验证指南.md](docs/角色权限验证指南.md)

## 6. 文档索引

| 文档 | 内容 |
|---|---|
| [docs/五大模块重构需求设计方案.md](docs/五大模块重构需求设计方案.md) | 五模块重构需求基线 |
| [docs/值班经理授权场景设计.md](docs/值班经理授权场景设计.md) | 高危场景权限设计 |
| [docs/敏感字段权限边界设计.md](docs/敏感字段权限边界设计.md) | 字段级权限边界（待 D-09 确认） |
| [docs/角色权限验证指南.md](docs/角色权限验证指南.md) | 九角色验证清单 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构说明 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 变更日志 |

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
