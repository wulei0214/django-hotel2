# 酒店管理系统（PMS）

本仓库正在将历史 Django 酒店 CRUD 原型，渐进式重构为以**预订、库存、在住、账务**为核心的可运营级单体酒店 PMS。

> **当前版本：PMS 2.0 Phase 1 + Phase 2 已完成代码交付。**  
> **版本约束：PMS 3.0 仅完善现有功能、修复 Bug、处理安全/性能/兼容问题和回归，不新增业务功能。**

## 1. 项目现状

- 当前按单体酒店落地，通过 `Property` 和核心业务表的 `property_id` 为后续连锁/多物业预留隔离能力。
- 新业务采用模块化单体、Django B/S、API-First；顾客端新页面采用 Django Template + 原生 JavaScript。
- 新预订页为 `/reserve/`，旧版 `/booking/` 保留，员工端旧页面按业务域逐步迁移。
- 本地模拟支付仅验证订单、支付状态和账本流转，不会真实扣款或退款。
- OTA、公安旅业系统、真实支付服务商、POS、停车/车牌识别、电子发票/数电票暂不接入，只保留后续适配边界。
- 不是微服务架构；当前没有独立 Next.js 前端、DRF、Celery、Redis 或消息队列。

## 2. 技术架构

| 层 | 技术/模块 |
|---|---|
| Web 框架 | Django 4.1 |
| 架构 | 模块化单体（Modular Monolith）、B/S、API-First |
| 页面 | Django Template + 原生 JavaScript |
| API | Django View + JsonResponse，API v1 |
| 管理后台 | Django Admin + django-simpleui |
| 认证授权 | Django User / Session / Group / Permission + 员工任职/岗位授权模型 |
| 数据库 | 默认 MySQL；配置支持 PostgreSQL / SQLite |
| 缓存 | 当前默认 LocMemCache |
| 业务模块 | core、identity、guests、inventory、reservations、billing、api |
| 兼容层 | hotelapp（历史页面与尚未迁移的模块） |

详细设计见 [技术架构说明](docs/ARCHITECTURE.md)。

## 3. 当前已交付模块

| 模块 | 已交付内容 |
|---|---|
| 平台底座 | Property 物业上下文、默认物业配置 |
| 员工身份 | StaffProfile、PropertyStaffMembership、StaffRoleAssignment，与 Django User 分离 |
| 住客 | Guest 档案及物业内联系电话去重 |
| 房型与客房 | RoomType、Room，房态拆分为占用/清洁/维修 |
| 房价与库存 | RatePlan、DailyRate、RoomTypeInventory，按住宿日管理库存与价格 |
| 预订 | Reservation、ReservationRoom，幂等键、价格快照、库存 Hold |
| 在住模型 | Stay 数据模型已建立；完整前台入住/退房流程仍需后续接入 |
| 账务 | Folio、Payment、LedgerEntry，不可变账本基础 |
| API | 查房、创建预订、订单查询/取消、Mock 支付、OpenAPI JSON |
| 顾客端 | 新增 `/reserve/`，旧 `/booking/` 保留 |

**注意：** 历史 `hotelapp` 的员工管理、入住/退房、夜审、财务等页面尚未全部迁移到新领域服务。不要把“模型已建立”误认为“整个业务流程已经上线”。

## 4. 当前页面入口

| 页面 | 地址 | 状态 |
|---|---|---|
| 顾客端新预订 | `/reserve/` | PMS 2.0 新页面 |
| 顾客端旧预订 | `/booking/` | 历史兼容页面 |
| 员工登录 | `/login/` | 历史员工端 |
| 员工工作台 | `/home/` | 历史页面 |
| 前台入住管理 | `/checkin/` | 待接入新 Reservation / Stay / Folio 流程 |
| Django Admin | `/admin/` | 管理员后台 |

新页面与截图规则见 [界面说明](docs/UI_GUIDE.md)。仓库旧截图属于历史页面，不作为新预订页的当前效果图。

## 5. 业务核心规则

- 入住日期包含，离店日期不包含；价格与库存按每一个住宿日检查。
- 预订时保存每晚成交价快照，后续调价不改变历史订单金额。
- 同一预订请求使用 Idempotency-Key，防止重复提交。
- 房态拆分为占用状态、清洁状态、维修状态；可售判断不能只看单一字段。
- Reservation（预订合同）、ReservationRoom（房间明细）、Stay（实际入住）分离。
- Folio（账单）与 Payment（支付流水）、LedgerEntry（账本凭证）分离。
- LedgerEntry 不允许常规物理删除或直接修改；财务纠错使用红字冲销并记录原因。
- Mock 支付只允许 dev/test 环境，生产环境不得展示模拟支付或模拟回调。

## 6. 本地开发

完整步骤见 [开发与本地运行指南](docs/DEVELOPMENT_GUIDE.md)。

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py check
python manage.py migrate
python manage.py runserver
```

顾客端新页面：<http://127.0.0.1:8000/reserve/>

默认数据库为 MySQL；也可通过环境变量切换 PostgreSQL 或 SQLite。运行前需要配置数据库连接、Django Secret、默认物业及 Mock 回调密钥。

## 7. 测试与 CI

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

CI 检查结果以对应 PR 最新提交的 GitHub Actions 为准。SQLite CI 不替代真实 MySQL/PostgreSQL 的事务锁和并发压力测试。

## 8. 目录结构

```text
apps/
  core/          # 物业与上下文
  identity/      # 员工身份、任职和岗位授权
  guests/        # 住客档案
  inventory/     # 房型、客房、房价、日库存
  reservations/  # 预订、预订明细、Stay
  billing/       # Folio、Payment、LedgerEntry
  api/           # API v1 与顾客端页面入口
hotel/           # Django 项目配置与根路由
hotelapp/        # 历史系统与渐进迁移兼容层
templates/       # Django 模板
docs/
  ARCHITECTURE.md
  DEVELOPMENT_GUIDE.md
  CHANGELOG.md
  UI_GUIDE.md
  development/   # 分阶段开发记录
```

## 9. 文档索引

- [技术架构说明](docs/ARCHITECTURE.md)
- [本地开发指南](docs/DEVELOPMENT_GUIDE.md)
- [界面与页面说明](docs/UI_GUIDE.md)
- [开发变更记录](docs/CHANGELOG.md)
- [Phase 2 预订与库存开发记录](docs/development/PMS2_PHASE2_RESERVATION_INVENTORY.md)

## 10. 推送与文档更新规范

每次推送必须使用中文说明：
1. 本次新增/开发了什么；
2. 修复了哪些 Bug 或风险；
3. 影响哪些模块、API、数据库和配置；
4. 测试执行结果；
5. 界面是否变化，是否更新最新截图；
6. 已知限制与回滚说明。

文档必须与代码同一 PR 更新。不得只推送代码而不留下可追溯的中文变更说明。详见开发指南。

## 11. 版本路线

- PMS 2.0：完成已确定的核心架构和业务模块闭环，按阶段迁移。
- PMS 3.0：只做现有功能完善、Bug 修复、权限/安全/性能/兼容优化与自动化回归，不增加新业务模块。

## 权限分配管理（超管专属，`/permissions/`）

参照主流 RBAC 管理台（若依、Mews Role-based Permissions 等）的通用交互实现的权限分配页：

- **角色中心制**：以角色（前台 / 值班经理 / 财务 / 店长）为分配对象，不逐用户授权；角色编号 `R-<组ID>` 展示
- **左栏 菜单管理**：模块勾选 = `view` 权限 = 侧边栏菜单可见性，父子联动
- **右栏 功能权限卡片**：每模块 查看/新增/修改/删除 四个权限点 + 单卡全选，实时统计已选数量
- **保存**：整体覆盖该角色 `hotelapp` 权限集合；服务端按白名单过滤防伪造提权；变更写入审计日志
- **护栏**：仅超管可访问；前端隐藏按钮不是权限控制，接口层 `permission_required` 双重强制

> 参考：[WorkOS – RBAC Best Practices](https://workos.com/blog/rbac-best-practices)、
> [Pathlock – RBAC Comprehensive Guide](https://pathlock.com/blog/role-based-access-control-rbac)、
> [Mews Role-based permissions](https://help.mews.com/s/article/Role-based-permissions-FAQ?language=en_US)

## 系统环境配置信息（`/system/`）

参照商用 PMS 的「个人中心 / 系统信息」页设计的登录后信息页（左侧菜单 → 系统信息）：

- **欢迎横幅**：姓名 + 角色徽章 + 账号 + 登录时间
- **当前账号信息卡**：账号编号（`AD-<用户ID>` 补零格式）、姓名、登录账号、角色、超管标志、账号状态
- **系统环境配置信息卡**：服务器版本、API 基址（`/api/v1/` 绝对地址）、运行模式（development/production）、时区、浏览器语言、系统语言、会话生效/过期时间与剩余有效期（基于 `SESSION_COOKIE_AGE` 与 `user.last_login` 计算）

## 图5 列表页改版（全部业务模块统一）

全部业务模块的管理页面已按统一规范改版：

| 组件 | 说明 |
|---|---|
| 工具栏 | 刷新数据 / 导出数据（CSV）/ 添加（按权限显示）|
| 筛选条件 | 可折叠面板，含属性查询与状态筛选 |
| 数据表格 | 复选框选择行、可排序编号列、行悬停高亮 |
| 分页 | 每页 15 条，`共 N 条记录` + 页码导航 |
| 状态徽章 | 员工管理新增「是否启用」列，点击徽章即可切换（免删除） |
| 导出 | `/export/<模块>/` CSV（UTF-8 BOM，Excel 直接打开），按 view 权限校验 |

员工管理额外支持：勾选单行后点「编辑员工」直接进入该行行内编辑。

## 变更记录（图5 批次）

- 员工管理：新增 `Staff.启用` 字段（0013 迁移）、启用/停用切换接口、CSV 导出、分页与排序
- 其余 7 个模块：统一接入工具栏 / 筛选折叠面板 / 分页 / CSV 导出
- 技术细节：分页使用 `django.core.paginator.Paginator`；导出按模块白名单映射字段并校验 view 权限

## 权限分配管理：矩阵式重设计

应运营反馈完全重做：放弃“左树 + 右卡片”双栏结构，改为**一张权限矩阵表**（与 PMS 2.0 说明书 §5.4.1 岗位权限矩阵同构）——行=业务模块，列=查看/新增/修改/删除，一眼看清每个角色能干什么。

- **角色切换**：页头角色标签（前台 / 值班经理 / 财务 / 店长）点击即切，当前角色高亮
- **联动全选**：整行全选（一个模块全部动作）、整列全选（所有模块同一动作）、全局全选，三粒度联动
- **全中文**：界面无英文权限代码；「查看」列同时控制模块菜单可见性
- **保存即生效**：原生复选框多值提交（无拼接歧义），服务端白名单过滤后整体覆盖，立即生效
- **超管说明**：页面明确标注超级管理员拥有全部权限、不受此页控制

### 缺陷修复：全选后权限被清空

- **根因**：前端把勾选的权限点以逗号串放入单个字段，后端 `getlist` 按单值处理，与白名单交集为空——保存后角色权限整体清零
- **修复**：改为原生复选框多值提交（`getlist` 天然支持），彻底消除拼接歧义；实测 空/全选/矩阵 三种提交均正确入库
- **矩阵重梳**：前台 17 项 / 值班经理 20 项 / 财务 7 项（只读）/ 店长 24 项（无删除），经保存链路写入并逐组校验一致


- **修复严重缺陷**：全选/批量保存时，前端把权限点以逗号串放入单个字段，后端按单值处理导致与白名单交集为空——**保存后角色权限被整体清空**。已修复为兼容逗号串与多值两种提交（`apps/identity/permission_admin.py`），实测全选 32 项保存后完整入库
- **全中文化**：权限点不再显示英文代码（如 `view_client`），改为「查看 / 新增 / 修改 / 删除」纯中文动作名（所属模块由卡片标题表达）；角色编号展示改为 `角色编号：1 · 角色名称：前台`
- **角色权限矩阵重梳**（经权限页保存链路写入并校验一致）：

| 角色 | 权限集 | 项数 |
|---|---|---|
| 前台 | 顾客/会员/餐饮订单/客房订单/车位的 查看+新增+修改；餐厅/客房 查看 | 17 |
| 值班经理 | 前台全部 + 客房修改 + 餐饮订单/客房订单删除（反结账冲账权） | 20 |
| 财务 | 7 个业务模块只读 | 7 |
| 店长 | 全部模块 查看+新增+修改（含员工管理），无删除 | 24 |

- **界面英文术语清理**：散客直入页去掉「Walk-in」、夜审页「No-Show」改为「未到订单」，全站已无冗余英文术语

## 信息展示页布局重设计（全部模块）

参照主流 PMS 列表页布局，重设计全部信息展示模块，消除留白与浮动卡片：

- **单卡片集成式结构**：查询筛选行内嵌于列表卡片顶部（属性下拉 + 输入框 + 查询/显示全部），数据表格满宽展示，分页条贴底——不再有漂浮的独立查询卡片和大段空白
- **查询结果直显主表**：查询时主表直接切换为结果集（附"显示全部"一键还原），空结果时表格内提示"未查询到相关记录，请重新核对信息"
- **表格样式**：取消 border=1 旧式边框，统一为悬停高亮、细分隔线的现代表格；文本列左对齐
- 餐饮订单/客房订单的专属查询（消费明细/入住明细）保留为独立结果卡片，仅在查询后出现

## 行内编辑与列表体验修复批次

- **客房管理行内编辑边界修复**：编辑列与房间状态列错位——把状态徽章文本（如「已入住」）填进服务员字段提交，必然校验失败。现改为行内编辑只修改 客房类型/价格（房间状态统一走「状态操作」按钮与工作流），后端 `update_room` 的负责人改为可选（不传则保持不变）
- **客房订单管理表头补齐**：表头只有 5 列而数据行有 9 列，已补齐 联系电话/状态/退房时间/收款金额 列
- **筛选控件尺寸规范**：下拉选择框固定紧凑宽度（170px），文本输入框加宽（320px）；餐饮订单/客房订单移除空的属性下拉，输入框按查询字段提示
- **入住管理统一样式**：5 张表格统一 `data-table` 样式，散客直入表单输入框宽度统一
- **表格满宽**：数据表格 100% 填充卡片宽度，消除右侧留白
