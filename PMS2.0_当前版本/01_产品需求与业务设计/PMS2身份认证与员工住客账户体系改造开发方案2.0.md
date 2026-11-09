# PMS2 身份认证与员工、住客账户体系改造开发方案 2.0

> **文档状态：正式开发基线（2.0）**  
> **版本日期：2026-11-04**  
> **适用仓库：`wulei0214/django-hotel2`，主分支 `main`**  
> **替代文件：**《PMS2身份认证与员工住客账户体系改造开发方案.md》（V0.2）  
> **依据：** V0.2 原方案、用户提交的补充审查意见、既有已确认的业务决策。  
> **执行原则：** 本文件是身份认证、员工账户、住客账户及相关公共接口安全改造的唯一开发基线。开发人员不得自行改变账号政策、权限边界、审批规则、财务行为、住客凭据规则或数据处置规则。

---

## 0. 版本说明与效力

### 0.1 本次 2.0 的目的

V0.2 主要描述方向、D1-D12 决策和阶段任务，但存在以下问题：

1. 基线 commit 与实际仓库状态不一致，部分“当前事实”已过期。
2. 将部分安全改造标记为“已实现”，但补充审查发现代码存在反向失效、覆盖不足和测试错误。
3. 对公共接口威胁模型、限流规则、订单访问凭据、邀请状态机、权限码、历史账号盘点和旧路由退役缺少可执行规范。
4. 对财务明细查看权限与退款审批权限、证件揭示权限码等关键权限语义缺少明确边界。
5. 缺少可复现的测试基线、数据库环境要求、回滚策略和逐路由验收清单。

2.0 将原方案和补充审查意见合并为完整实施规范，并对冲突项作如下处理：

- 补充审查中能够由代码事实支持的缺陷，纳入缺陷清单和强制修复任务。
- 原方案中的方向性决策继续保留；已确认的单店运行、员工人工邀请、住客免注册、禁止公开创建员工账户等决策不得被开发人员擅自改变。
- 原方案中与补充审查冲突的“已实现”状态，统一降级为“待修复/待验收”，以实际代码和可复现测试为准。
- 对尚未由业务方确认的产品政策，明确标记为“待业务确认”，开发不得自行补齐或推断。
- 模型存在、代码分支存在、测试单独通过，均不等于功能已交付。

### 0.2 文件效力

自本文件合并进入主分支起：

1. 本文件覆盖 V0.2 的开发基线效力；V0.2 只作为历史决策记录，不再作为开发验收依据。
2. 本文件与旧文档、代码注释、开发人员口头决定不一致时，以本文件为准。
3. 任何偏离本文件的业务规则，必须先提交书面变更，经产品/业务负责人批准后更新本文件，再实施代码变更。
4. 开发人员不得以“技术方便”为由自行放宽权限、改变财务审批、自动合并住客、延长 token 有效期或保留旧业务写入口。
5. 每项任务必须分别记录：代码状态、测试状态、目标数据库验证状态、部署状态；不得合并为一个笼统的“已完成”。

---

## 1. 目标、范围与不可变约束

### 1.1 改造目标

建立统一、可审计、可验证的 PMS2 身份与账户体系：

- 员工使用正式员工身份及岗位授权进入 PMS2。
- 员工账户通过管理员邀请激活，不开放公众自行注册员工账户。
- 员工身份、物业任职、岗位授权、登录凭据分离管理。
- 住客保持免注册预订，不为普通预订创建 Django User。
- 公共预订、订单查询、订单取消和支付意图接口具备限流、防滥用、审计和数据最小化保护。
- 旧系统写入口按路由逐项冻结或退役，避免新旧业务主数据继续双写。
- 权限读取统一走 AuthorizationService，保证改造前后有效授权矩阵不发生未经批准的变化。
- 建立可复现的测试、审计、回滚和运维验收标准。

### 1.2 本期范围

| 范围 | 本期要求 |
|---|---|
| 员工登录 | username + password；全局唯一 username |
| 员工身份 | StaffProfile、PropertyStaffMembership、StaffRoleAssignment 分层 |
| 员工邀请 | 管理员创建一次性邀请链接，复制后人工发送 |
| 员工激活 | 员工通过邀请设置密码并激活 |
| 员工生命周期 | 创建、绑定、启用、停用、重置密码、岗位/任职变更均走 Service |
| 员工中心 | 迁移原有写操作，统一为受权限控制的员工管理工作台 |
| 住客预订 | /reserve/ 免注册直订，不创建 Django User |
| 订单安全 | booking access token、限流、审计、脱敏、统一错误反馈 |
| 公共预订安全 | IP/手机号限流、未支付订单上限、库存占用防滥用、幂等 |
| 旧模块治理 | 逐条清理旧 Client、Accommodation、Staff、FinanceRecord 等写入口 |
| 权限 | AuthorizationService 统一业务授权；财务查看与退款审批分离 |
| 运维 | HTTPS、Cookie、HSTS、密钥、审计保留、备份和恢复验收 |

### 1.3 本期明确不做

- 不开放员工公开注册。
- 不提供住客长期登录账户；不为普通住客预订创建 Django User。
- 本期不实现住客 OTP。
- 本期不实现 MFA；高风险岗位 MFA 另立专项。
- 本期不启用多物业切换、跨物业报表或跨物业业务写入。
- 不强制企业邮箱，不把共享手机号当作唯一登录、邀请或找回凭据。
- 不自动按手机号、姓名或证件号合并 Guest。
- 不接入未经批准的邮件、短信服务商；本期邀请由管理员复制链接并人工发送。
- 不改变现有岗位权限矩阵、敏感字段边界、退款审批链和财务行为。
- 不允许 Django Admin 成为日常业务写入的第二入口。

### 1.4 不可变业务约束

1. **身份不等于授权：** 有 Django User 不代表有 PMS 员工身份；有 StaffProfile 不代表拥有业务权限。
2. **岗位授权唯一来源：** 员工业务授权必须由物业任职与 StaffRoleAssignment 关系计算，经 AuthorizationService 判定。
3. **superuser 不自动获得业务岗位权限：** 平台技术管理权限与店长/财务/前台等业务授权分离。紧急访问只能走受控 break-glass 流程并完整审计。
4. **财务查看不等于财务审批：** 能查看财务明细不能据此审批退款；退款审批必须有专门审批权限。
5. **敏感字段揭示必须使用唯一权限码：** 不得在不同服务中维护同一动作的多个 codename。
6. **住客凭据最小化：** 手机号不得单独成为订单查询和取消的长期访问凭据。
7. **账务和审计不可破坏：** 不得通过账户清理、Guest 处理或旧表退役删除历史预订、Folio、LedgerEntry、Payment、Refund、AuditEvent 等业务凭证。
8. **新旧写入单一权威：** 同一业务事实不得继续由 PMS2 与旧 hotelapp 页面双写。

---

## 2. 代码基线与当前状态口径

### 2.1 基线核对要求

V0.2 曾记录基线为 `610fe3c28bfc43058b69b2d4054c8adbefee70a1`，补充审查记录当时仓库 HEAD 为 `e67f501`，并指出工作区存在未提交文件。两者不是同一个可复现基线。

因此，以上 commit 仅作为历史审查上下文，不作为 2.0 的最终验收基线。实际开发开始前必须：

1. 获取最新 `main` HEAD 完整 SHA。
2. 记录代码工作区是否干净、是否存在未提交变更。
3. 记录迁移状态、Python/Django 版本、依赖锁文件版本。
4. 记录测试数据库类型、版本和连接方式。
5. 将基线信息写入本次实施记录和 CI 构建产物。
6. 任何代码审查结论必须指向具体 commit，不得使用短 SHA 或移动分支名代替验收证据。

### 2.2 状态定义

| 状态 | 定义 |
|---|---|
| 未开始 | 无可用实现，未完成设计或代码 |
| 已实现待验收 | 代码已提交，但缺少完整测试、目标数据库验证或安全验证 |
| 部分实现 | 仅覆盖部分路径、部分角色或部分运行场景 |
| 存在缺陷 | 有实现但存在已知安全、正确性或回归问题 |
| 已验收 | 指定 commit 上测试、目标数据库和验收项均通过 |
| 已部署 | 已在目标环境发布并完成部署后检查 |
| 暂缓 | 依赖决策或外部条件，当前不得擅自实现 |

任何表述“已完成”必须能提供 commit、测试命令、测试结果和目标环境验证证据。

### 2.3 以补充审查为准的现状修正

补充审查指出：

- `/register/` 已停止创建 User，登录页注册链接已移除；但仍需全仓测试保证没有其他公开路径创建员工 User，旧模板也需处理。
- mock 支付生产门禁已有代码，但必须验证所有创建入口和环境配置。
- 订单查询仍以 booking_no + phone 为凭据，缺少 token、限流、审计和字段脱敏。
- 公开预订创建缺少有效防刷、未支付订单上限和占库滥用保护。
- `_public_throttle` 使用 AuditEvent 作为计数器存在维度写入错误、空手机号全局桶、审计污染、并发竞态和清理困难。
- `api_guard` 中 `except JsonResponse` 是非法异常捕获方式，会覆盖原始异常。
- 证件揭示权限码不一致，财务明细和退款审批权限语义混淆。
- `create_employee` 创建主路径没有完整执行 Django 密码验证策略。
- 登录失败没有可靠持久化安全审计和暴力破解防护。
- 多条旧 Client、Accommodation、Staff、财务写路由仍可写旧表。
- 多物业代码处于半启用/半回退状态，与单店运行决策不一致。
- 新增安全测试存在测试夹具错误，且未覆盖若干关键验收点。
- 当时 SQLite 测试 23 项中 2 项 error；另有 apps.identity + apps.api.test_security 26 项通过。MySQL 因本机账号访问拒绝，未能证明目标数据库测试通过。

**结论：不得沿用 V0.2 中 AUTH-08、AUTH-09“已实现”的结论，也不得据此签署 Phase 0 完成。** 以上属于补充审查所记录的当时状态，实施人员必须在最新 main 上重新复核，不得将历史状态直接当作当前 HEAD 状态。

---

## 3. 总体身份模型与职责边界

### 3.1 员工身份关系

员工业务身份由以下关系构成：

- Django User：认证主体，负责用户名、密码、登录状态。
- StaffProfile：自然人/员工档案，不承担岗位授权。
- PropertyStaffMembership：员工与物业的任职关系，包含员工编号、任职状态等。
- StaffRoleAssignment：任职关系与岗位组的授权关系。
- AuthorizationService：根据用户、员工档案、物业任职、岗位分配和权限码统一计算业务权限。

业务代码不得把 `User.groups` 或 `user.has_perm()` 当作员工任职或岗位授权事实。不得在 View 中直接拼装权限判断。

### 3.2 服务层职责

| 服务 | 唯一职责 | 禁止事项 |
|---|---|---|
| EmployeeLifecycleService | User 创建/绑定、密码策略、启停用、重置密码、激活账户 | 不直接处理邀请 token 生命周期 |
| EmployeeInvitationService | 邀请创建、token hash、有效期、撤销、重发、一次性消费和状态 | 不自行创建 User 或绕过生命周期服务设密码 |
| AuthorizationService | 员工业务权限判定、物业范围校验 | 不读取旧 User.groups 作为业务授权兜底 |
| GuestIdentityService（如拆分） | Guest 档案关联、人工链接、历史映射 | 不擅自自动合并 Guest |
| PublicAccessService / RateLimitService | 公共接口访问令牌、限流和反滥用策略 | 不把 AuditEvent 当作计数器 |
| AuditService（如使用） | 统一安全和业务审计事件写入 | 不记录明文密码、token、完整证件号或支付敏感凭据 |

View 层只负责请求解析、调用服务、响应序列化。禁止 View 直接创建 User、消费邀请、写入任职授权或自行决定敏感字段是否展示。

---

## 4. 员工账户政策与 D1-D12 决策

### 4.1 已确认的政策

| 编号 | 决策 | 固定规则 |
|---|---|---|
| D1 | 公开注册 | 不允许公众注册员工账号；`/register/` 不得创建 User，登录页不显示员工公开注册链接 |
| D2a | 主登录标识 | 本期只使用全局唯一 username + password |
| D2b | 联系方式别名 | 本期不启用 email/phone 登录别名；后续独立决策 |
| D3 | 邮箱要求 | 不强制企业邮箱；没有验证的邮箱不得作为身份凭据 |
| D4 | 共享手机号 | 手机号可以作为业务联系信息共享，不得加 unique，不得作为唯一认证/邀请/找回凭据 |
| D5 | 多物业 | 本期单店运行；保留未来扩展模型，但关闭本期多物业切换和跨店操作 |
| D6 | 邀请渠道 | 管理员创建一次性邀请链接并手工复制发送；界面不得假装邮件或短信已发送 |
| D7 | MFA | 本期不做，后续高风险岗位专项评估 |
| D8 | 住客账户 | 住客免注册预订，不创建长期住客 User；旧注册和旧预订写入口逐步退役 |
| D9 | 住客 OTP | 本期不做 OTP；改用 Booking Access Token + 限流 + 审计 + 脱敏 |
| D10 | 历史 User | 先停止新增，再盘点分类；不直接批量删除 |
| D11 | 联系方式模型 | 不在本期增加未经验证的登录别名；联系方式验证、唯一性、共享、失效和恢复规则另立决策 |
| D12 | 状态机 | 采用本文件第 5 节定义的账户与邀请状态语义；不得自行混用多个状态字段 |

### 4.2 D5 单店运行的具体技术要求

本期不提供物业选择页，不接受客户端任意传入物业 ID，不允许通过 Session 切换物业。

- 当前运行物业必须由部署配置确定，使用明确的 DEFAULT_PROPERTY_CODE 或等价配置。
- 对非当前物业的任职/请求必须明确拒绝并记录安全日志，不得静默回退到 DEFAULT 继续执行。
- 禁止使用 `active_property_id` 造成“看似支持切换、实际回退”的半状态行为。
- 保留 Property、Membership 等模型作为未来扩展基础，但相关多物业代码必须标记 dormant，不进入本期功能验收。
- `employment_status` 的语义必须明确为员工全局雇佣状态；物业任职状态由 Membership 独立表达。不得用单店操作误伤其他物业任职数据。
- 未来启用多物业前必须另立专项，覆盖登录门禁、中间件、Membership 服务、全局/物业状态语义、选择页、Session、菜单、API 过滤和跨物业越权测试。

---

## 5. 员工账户与邀请状态机

### 5.1 账户状态

账户业务状态统一表达为：

| 状态 | 含义 | 登录要求 |
|---|---|---|
| 未开通 | 有员工档案/任职记录，但没有可用登录账户 | 不可登录 |
| 待激活 | 已创建邀请，员工尚未完成激活 | 不可登录；密码不可用 |
| 启用 | 邀请已接受、凭据有效、员工任职和岗位授权有效 | 可登录，仍需通过 AuthorizationService |
| 停用 | 管理员停用账户或员工离职等原因导致不可用 | 不可登录 |
| 邀请过期 | 邀请 token 超过 TTL | 不可使用，需管理员重发 |
| 邀请撤销 | 管理员主动撤销邀请 | 不可使用，需重新邀请 |

状态字段不应重复表达同一含义。必须明确以下字段职责：

- `User.is_active`：认证账户是否允许登录。
- `StaffProfile.employment_status`：员工全局雇佣关系状态。
- `PropertyStaffMembership.status`：员工在当前物业的任职状态。
- `StaffRoleAssignment`：岗位授权关系是否有效。
- `must_change_password`：是否必须在登录后完成密码变更。
- Invitation.status：邀请生命周期状态。

授权成功必须同时满足账户可用、员工雇佣关系有效、当前物业任职有效、岗位分配有效和权限码匹配。不得只凭 `User.is_active` 判断员工可操作业务。

### 5.2 邀请状态机

Invitation 至少包含：

- 员工档案/任职关联；
- token_hash（不得存储明文 token）；
- status；
- created_at、expires_at、consumed_at、revoked_at；
- created_by、revoked_by；
- resend_count / last_sent_at（人工复制也应记录重发操作）；
- 必要的审计关联和版本/并发控制字段。

状态转换：

1. 未开通 → 待激活：管理员创建邀请。
2. 待激活 → 启用：员工提交有效 token 并成功设置符合策略的密码。
3. 待激活 → 邀请过期：超过 expires_at。
4. 待激活 → 邀请撤销：管理员撤销。
5. 邀请过期/撤销 → 待激活：管理员重新发起新邀请；旧 token 永久失效。
6. 启用 → 停用：管理员停用、员工离职或任职不再有效。
7. 停用 → 启用：仅授权管理员按生命周期服务重新启用，且必须重新校验任职和岗位状态。

### 5.3 邀请安全规则

- token 使用密码学安全随机数生成，至少 256 bit 熵。
- 数据库只存 token 的安全 hash；明文只在创建邀请时返回一次。
- 默认有效期建议 24 小时；如业务需变更，必须由业务负责人批准并更新本文件。
- token 一次性消费；同一 token 并发提交只能有一个成功。
- 激活必须使用数据库事务和行锁/条件更新保证一次性消费。
- token 必须绑定指定员工档案和任职关系，不能通过请求参数改绑其他员工。
- 重发时撤销所有旧 token，生成新 token。
- 撤销、过期、重复消费、错误 token 均不得泄露员工是否存在。
- 激活页面只允许受信任 Host；生成绝对 URL 时使用配置的 canonical host，不信任任意 Host/X-Forwarded-Host。
- 邀请创建、复制/查看、重发、撤销、激活成功和失败均按安全审计策略记录，但不得记录明文 token。
- 邀请创建、验证和激活接口必须限流。
- 不配置邮件/短信供应商时，界面只能显示“邀请链接已生成，请复制并人工发送”，不得显示“发送成功”。

### 5.4 密码策略

- 所有设置/修改密码路径统一调用 Django `validate_password`。
- 覆盖员工创建、邀请激活、绑定既有用户、管理员重置、首次强制改密。
- 禁止自定义“至少 6 位”替代 Django 密码验证器。
- 邀请待激活账户使用 unusable password。
- 员工首次接受邀请自行设置密码后，`must_change_password=False`。
- 管理员重置密码后，`must_change_password=True`。
- 管理员重置不得向操作人员显示或记录新密码明文。
- `last_password_change` 必须明确语义：记录最后一次凭据设置/变更时间；若需要“用户自主改密时间”，另设字段，不能混用。
- 不得把新建账号时设置时间误认为员工已经自主修改密码。

---

## 6. 公共接口威胁模型与访问安全

### 6.1 匿名攻击面

本期匿名或弱凭据访问面至少包括：

- 可用房态/库存查询；
- 公开预订创建；
- 支付意图创建；
- 订单详情查询；
- 订单取消；
- 邀请激活（员工 token 持有者）。

威胁包括：撞库/枚举订单、手机号猜测、token 重放、批量创建占用库存、伪造大量 Guest 档案、支付接口刷请求、取消他人订单、利用空参数污染全局限流桶、通过并发绕过频率限制、日志和审计表膨胀。

### 6.2 RateLimitCounter 设计

**禁止使用 AuditEvent 作为限流计数器。** 审计事件是不可变业务/安全证据，不是高频计数存储。

优先方案：

1. 若部署具备可靠 Redis：使用 Redis 原子 `INCR` + `EXPIRE` 或 Lua 脚本实现窗口计数。
2. 若暂时没有 Redis：建立独立 RateLimitCounter 数据表，采用原子更新/事务锁实现，必须有过期清理任务和索引。
3. LocMemCache 不能作为多进程、多 worker 或多实例生产环境的唯一限流存储。
4. 不得用“先 COUNT 再 INSERT”作为并发安全计数实现。

至少支持以下键维度：

- IP：识别单来源请求洪泛。
- phone_hash：识别同手机号跨 IP 频繁操作；只存带服务端密钥 HMAC 的指纹，不存可逆手机号。
- booking_no / booking_id：限制单订单反复查询、取消和支付请求。
- invitation_id / token_hash 指纹：限制邀请验证和激活尝试。
- username_hash + IP：限制登录暴力尝试。

缺少手机号、订单号或解析失败时，不得使用空字符串 hash 作为共享全局桶。必须仍然按 IP 维度限流，并对格式错误请求使用独立错误请求桶。

### 6.3 初始限流建议值

以下是 2.0 的默认起始策略，必须做配置化，经过压测/运营容量验证后方可调整；不得由开发人员在代码中随意改变：

| 接口/动作 | 维度 | 初始阈值 | 窗口 |
|---|---|---:|---|
| 登录失败 | IP + username_hash | 5 次失败 | 5 分钟 |
| 邀请验证/激活失败 | IP + token_hash 指纹 | 5 次失败 | 15 分钟 |
| 公开预订创建 | IP | 10 次 | 10 分钟 |
| 公开预订创建 | phone_hash | 3 次 | 10 分钟 |
| 公开订单查询 | IP | 30 次 | 5 分钟 |
| 公开订单查询 | booking_no + IP | 10 次 | 5 分钟 |
| 订单取消 | IP | 10 次 | 10 分钟 |
| 订单取消 | booking_id + token | 5 次 | 10 分钟 |
| 支付意图创建 | IP | 10 次 | 5 分钟 |
| 支付意图创建 | booking_id | 5 次 | 5 分钟 |

- 多维度均需通过才放行；任何一个维度超限均返回 429。
- 429 必须返回合理的 `Retry-After` 秒数和稳定 JSON 错误结构。
- 限流阈值应可按环境配置，但生产配置不得缺省为无限制。
- 限流存储异常时的 fail-open/fail-closed 策略必须显式配置。订单取消、支付意图和登录失败等高风险操作默认 fail-closed；只读房态查询可在有监控告警的前提下评估 fail-open。
- 白名单仅允许明确的内部监控/健康检查来源，必须有负责人、范围和审计，不允许按 User-Agent 伪造白名单。
- 需要指标：限流触发次数、维度、接口、存储错误、异常放行数、429 比率和清理任务失败。
- 限流记录必须按 TTL 自动清理，不得无限增长。

### 6.4 公开预订防刷与库存占用保护

公开预订创建至少实施：

1. IP + phone_hash 双维限流。
2. 同一手机号、同一物业的未支付 PENDING 预订数量上限。默认最多 2 笔有效未支付预订；超过返回 429/业务冲突，不新增 Guest 和 Reservation。
3. 订单创建必须幂等；同一个 Idempotency-Key 在有效期内只能产生一个业务结果。
4. 幂等键必须绑定请求主体摘要；同 key 不同请求内容不得返回另一个人的订单结果。
5. 创建预订、占用库存、创建 Folio、保存幂等结果必须在一致的事务边界中完成。
6. 对库存 hold 设置明确 TTL；过期释放任务必须幂等、可重试、可监控。
7. 不得通过反复创建订单无限新增 Guest 档案或长期锁定库存。
8. 创建成功和拦截失败均按审计规范记录必要字段（请求 ID、IP、phone_hash、物业、订单 ID、结果），不得记录完整手机号或证件号。
9. 必须测试并发下库存不会超卖、重复请求不会重复创建、超限请求不会新增 Guest/Reservation/Folio。

Guest 重复档案的业务合并政策仍按第 8 节执行，不允许用“每次预订创建新 Guest”作为长期无治理策略，也不允许开发擅自改成手机号自动合并。

### 6.5 Booking Access Token

订单查询和取消不得继续仅依赖 booking_no + phone。

本期引入 Booking Access Token：

- 使用密码学安全随机 token，至少 256 bit 熵。
- 数据库存 token hash，不存明文。
- token 绑定单一 Reservation；不得跨订单使用。
- token 只在预订创建成功时向用户展示一次，用户可复制保存；不得在日志、分析事件或普通审计中记录明文。
- token 默认有效期：预订创建后至预计离店日期后 30 天；如订单被取消，取消后的查询只读权限可保留至原预计离店日后 30 天，但不能再执行重复取消。
- 支持撤销/轮换；轮换后旧 token 立即失效。
- 查询和取消必须校验 token、订单状态和限流规则。
- 查询接口只返回完成订单管理所需的最小信息；姓名脱敏（如“张*”），不得返回 provider_ref、内部支付流水标识、完整手机号、证件号或内部审计字段。
- 取消接口必须再次校验取消政策、订单状态、取消权限和幂等键；token 不得绕过原有业务取消规则。
- 对外统一错误反馈，避免区分“订单不存在”“手机号不匹配”“token 不正确”等可被枚举的信息。
- 订单详情查看、取消尝试（成功/失败）均写安全审计。
- 不在 URL query string 中长期暴露 token；优先使用 POST body/header，并设置 Referrer-Policy，避免第三方资源泄漏。若页面必须通过链接携带，采用一次性兑换为短时 HttpOnly Secure SameSite Cookie 的方式。
- 不提供 OTP 不代表允许 phone 继续充当唯一凭据。

### 6.6 API 错误处理契约

修复 `api_guard`：

- 删除 `except JsonResponse`，因为 JsonResponse 不是异常类。
- 正常返回的 Response/JsonResponse 直接返回，不需要异常捕获分支。
- 对预期业务异常使用明确异常类型映射到 4xx JSON。
- 对未预期异常记录完整内部堆栈和 request_id，向客户端返回稳定 JSON 500，不返回 DEBUG 堆栈或 HTML。
- 不得吞掉/覆盖原始异常。
- 对敏感字段和异常内容脱敏，避免把 SQL、token、手机号、证件号、支付 provider_ref 返回给客户端。
- 所有 v1 API 的错误响应结构保持一致；如需调整，必须更新 API 文档和契约测试。

---

## 7. 登录安全、会话与平台访问

### 7.1 登录失败保护

- 对外统一错误文案，不区分用户名不存在、密码错误、员工无任职、账户停用等状态。
- 内部必须记录细分原因，但用户名应脱敏或使用不可逆指纹。
- 登录失败写入持久化 AuditEvent 或专用安全事件表，包含时间、IP、username_hash、user-agent 摘要、失败原因、request_id。
- 不记录明文密码。
- 实施第 6.3 节定义的 IP + username_hash 限流和递增退避。
- 不采用永久锁定，防止攻击者利用锁定机制拒绝服务管理员；锁定/退避必须有自动恢复。
- `authenticate()` 对 inactive User 的行为需要在代码中正确建模；不得保留永远不可达的 disabled 分支并把审计原因写错。
- 登录成功、登出、密码修改、管理员重置、账户停用和异常会话撤销应审计。

### 7.2 next 重定向

所有登录后跳转参数必须使用 Django `url_has_allowed_host_and_scheme` 或等价严格校验：

- 拒绝 `//evil.example`、外部绝对 URL、反斜线变体和不可信 host。
- 仅允许当前站点/明确白名单 host。
- 不得使用简单 `startswith("/")` 作为充分校验。
- 增加合法站内路径、协议相对 URL、外部 URL、反斜线和编码绕过测试。

### 7.3 会话与生产部署

生产验收必须确认：

- 全站 HTTPS；`SECURE_SSL_REDIRECT=True` 或由可信反向代理强制 HTTPS。
- Session Cookie 和 CSRF Cookie 的 Secure、HttpOnly、SameSite 属性符合部署规范。
- HSTS 在 HTTPS 已验证后启用，max-age、includeSubDomains、preload 由运维评估。
- 可信代理头仅信任受控代理；禁止直接信任客户端可伪造的 X-Forwarded-Proto。
- SESSION_COOKIE_AGE、登录后 session rotation、密码修改后旧会话失效策略明确。
- CSRF 对所有有状态写接口有效；不得为方便前端而全局关闭。
- 密钥从环境变量/密钥管理服务读取，不得提交仓库。
- 生产 DEBUG 必须关闭；错误页面不得泄露堆栈。

---

## 8. 住客身份、Guest 档案与历史 Client

### 8.1 Guest 与 Django User 分离

- 普通住客使用 /reserve/ 免注册预订。
- 公开预订不得创建 Django User。
- Guest 是业务档案，不等于登录账户。
- GuestPhone 是联系信息，不是认证凭据。
- 同一手机号可能被家庭成员、公司联系人或多人共享，不得把 phone unique 作为身份主键。
- 不得用 Guest 名称或手机号直接认定两个 Guest 是同一自然人。

### 8.2 Guest 去重与合并

补充审查指出现有 `create_reservation` 每次创建新 Guest，旧 Client 又可能继续形成第二套客人主数据。此问题必须治理，但不能由开发自行确定自动合并规则。

本期固定执行：

1. 不自动按手机号、姓名、证件号合并 Guest。
2. 新预订如需复用 Guest，必须先经过明确的匹配策略和人工确认流程；在该策略批准前，不得擅自复用或合并。
3. 支持人工建立重复档案关联/疑似重复标记，但不物理删除源档案。
4. 合并一旦未来获批，必须保留主档案、源档案、操作者、时间、理由和完整关联映射。
5. 合并必须审查 Reservation、ReservationGuest、Folio、LedgerEntry、Payment、Refund、AuditEvent、LegacyRecordLink 等引用关系；禁止直接改写或删除账务历史。
6. 旧 Client 到 Guest 的映射必须使用 LegacyRecordLink 或经批准的迁移映射表，保留原 ID 和来源系统。
7. Guest 合并策略、匹配置信度、冲突处理、回滚与人工审批流程属于业务待确认项；在批准前只能做只读盘点和候选重复报告。

### 8.3 订单隐私

- 查询接口返回字段按最小必要原则设计。
- 姓名、手机号、证件信息均按敏感级别脱敏。
- 支付 provider_ref 和内部支付流水信息不得暴露给匿名客户端。
- 对 Guest 资料揭示必须走统一权限码和 AuthorizationService。
- 对每次 PII reveal 记录操作者、对象、字段类型、理由、时间和请求来源；不得将揭示后的完整值写入审计日志。

---

## 9. 权限码注册表与权限等价性

### 9.1 唯一权限码

每个业务动作只能有一个正式权限码。不得服务层、API 层和模板层分别使用不同 codename 表达同一动作。

本次审查发现证件揭示存在冲突：

- 服务层检查 `guests.document_reveal`；
- 权限矩阵、API 和旧视图使用 `hotelapp.guest_document_reveal`；
- migration 实际授予的是 `hotelapp.*` 权限。

统一规则：**证件揭示使用 `hotelapp.guest_document_reveal` 作为唯一正式权限码**。服务层必须改用该权限码；旧权限若需兼容，只能在迁移期映射，最终清理，不得并行判断两个权限码。

### 9.2 财务明细与退款审批分离

必须明确：

| 动作 | 权限 |
|---|---|
| 查看财务明细/财务工作台 | `hotelapp.finance_access`（或经正式迁移批准的新 `billing.finance_detail`） |
| 发起退款申请 | `billing.refund_request` |
| 审批退款 | `billing.refund_approve` |
| 执行退款/支付操作 | 按既有财务操作权限与审批状态控制 |

本次实施不得把 `refund_approve` 或 `identity.staff_manage` 当作财务明细查看权限的替代品。补充审查指出这样会导致财务角色失去财务明细查看权。

同时，`apps/api/approval.py` 不得用 `hotelapp.finance_access` 作为退款审批人的兜底条件。财务页入口权限不等于审批权限；审批必须只认专门的 `billing.refund_approve`。

任何权限码变更都必须提供 migration、角色矩阵变更说明、回滚方式和等价性测试。不得在本次身份改造中悄悄改变岗位权限矩阵。

### 9.3 权限等价性测试

建立“角色 × 关键端点/动作”的授权快照测试，至少覆盖：

- 前台：入住/退房、订单查看、证件揭示（按批准矩阵）、不可审批退款。
- 财务：财务明细查看、退款申请（按现有矩阵）、不得仅因 finance_access 获得退款审批。
- 值班经理：按现有授权处理指定审批，不自动获得全部店长权限。
- 店长：按既有矩阵管理员工和审批。
- 普通员工：只能访问被明确授权的业务动作。
- 无任职 User：不得获得业务权限。
- superuser：不自动获得业务岗位授权；平台技术操作必须与业务审批分离。
- 跨物业请求：本期必须拒绝非当前物业数据访问。

测试必须通过正式权限 migration/岗位矩阵建立角色，不得手工向 Group 塞入一个与正式矩阵不一致的权限来让测试变绿。

### 9.4 清理旧授权读取

全仓搜索并逐项审查：

- `user.groups`
- `user.has_perm()`
- `request.user.has_perm()`
- 直接读取 Group/Permission 的业务判断
- `is_superuser` 作为业务授权兜底

补充审查指出 `apps/api/reservation_center.py`、`apps/api/guest_center.py` 仍存在 superuser 直通。必须逐个判断是否属于平台只读运维例外；未经批准不得绕过业务岗位授权。测试命令 `seed_role_test_accounts.py` 不得在生产环境创建可登录且仅靠 User.groups 授权的账号，必须加环境门禁或改成仅测试数据构造。

---

## 10. 旧路由、旧模型与双写退役

### 10.1 退役原则

不能只冻结 `/booking/`。凡是仍可写旧业务表的路由，必须建立完整路由级清单，逐条决定：

- REDIRECT：安全、语义一致时跳转到新入口；
- GONE：旧写能力已退役，返回 410；
- READ_ONLY：保留只读查询；
- MIGRATE：写操作已迁移至 PMS2 服务后再关闭；
- KEEP：确需保留，必须写明业务 owner、数据范围和退役条件。

任何旧写入口在迁移完成后必须通过测试证明不会再写旧表。

### 10.2 必须盘点的旧写路由

补充审查列出的至少包括：

| 路由/入口 | 旧模型/风险 | 目标处理 |
|---|---|---|
| `/register/` | 创建孤儿 User | 继续 redirect/关闭；任何方法不得创建 User |
| `/booking/` | 写 Accommodation 等旧预订模型 | redirect /reserve/，不得写旧表 |
| `/cancel_booking/` | 旧取消入口；重复装饰器导致匿名跳转不正确 | 去除员工门禁，GET/POST 均统一 redirect /reserve/ 或明确 410 |
| `/insert_client/` | 创建旧 Client | 迁移至 Guest Center 后关闭旧写入 |
| `/update_client/` | 更新旧 Client | 迁移后关闭旧写入 |
| `/delete_client/` | 删除旧 Client | 禁止无审计硬删除；迁移后关闭旧写入 |
| `/checkin/legacy/` | 旧入住写入 | 迁移至 PMS2 入住工作台后关闭 |
| `/checkin_confirm/` | 旧 Accommodation/Client 写入 | 迁移后关闭 |
| `/checkin_in/` | 旧入住写入 | 迁移后关闭 |
| `/staff_toggle/` | 写旧 Staff | 迁移至 Employee Center 后关闭 |
| `/update_staff/` | 写旧 Staff | 迁移后关闭 |
| `/delete_staff/` | 删除旧 Staff | 禁止无审计硬删除；迁移后关闭 |
| `/finance_add/` 等 | 旧 FinanceRecord 写入 | 按财务迁移计划逐项关闭，不能绕过 PMS2 Ledger/Folio |

开发必须继续全仓扫描 URLConf、View、表单、模板和 JS 请求，补齐上述清单，不得认为表中列举即完整清单。

### 10.3 旧数据处理

- 不直接删除历史 Client、Accommodation、Staff、FinanceRecord、NightAudit 等数据。
- 只读历史访问和业务写入必须分开。
- 对已迁移记录建立 LegacyRecordLink，保留来源系统、旧主键、新主键、迁移时间和迁移批次。
- 历史账务、审计和预订凭证必须保持引用完整。
- 旧表退役前完成数据对账、备份、回滚演练和只读查询验证。

### 10.4 cancel_booking 特殊要求

`cancel_booking` 作为退役兼容入口，不应再挂员工门禁或重复的 `staff_required`、`pms_permission_required` 装饰器。匿名 GET/POST 请求必须按本文件约定统一跳转到 /reserve/ 或返回 410，且不得触发旧表写入。

---

## 11. 员工中心目标页面与路由

### 11.1 目标

`/manage/staff/` 是最终员工中心入口；`/staff_accounts/` 当前写工作台不能永久作为第二套员工管理入口。

员工中心至少包含：

- 员工档案列表与详情；
- 账号状态与登录标识；
- 当前物业任职状态；
- 岗位/角色分配；
- 邀请创建、复制、重发、撤销和过期状态；
- 密码重置（不显示密码明文）；
- 启用/停用及离职处理；
- 操作审计与历史变更记录。

### 11.2 迁移顺序

1. 盘点 `/staff_accounts/` 所有读写路由、表单、JS、权限和审计行为。
2. 定义 Employee Center API/Service 合约。
3. 将写操作迁移到 EmployeeLifecycleService / EmployeeInvitationService。
4. 在 `/manage/staff/` 实现完整交互和权限控制。
5. 补齐 CSRF、字段校验、并发处理、审计和错误反馈。
6. 完成旧、新入口行为等价性和权限测试。
7. 关闭 `/staff_accounts/` 写路由；必要时只保留兼容 redirect。
8. 删除失效模板、重复 View 和未使用 JS。
9. 确认没有其它入口可以绕过 Employee Center 直接写员工账户和授权。

在以上步骤完成前，不得宣布员工中心交付，也不得提前删除唯一可用的员工管理能力。

---

## 12. 历史 User 盘点与处置

### 12.1 分类标准

所有历史 User 必须分类为：

- `STAFF_LINKED`：已正确关联 StaffProfile、当前物业 Membership 和岗位分配。
- `PROFILE_ONLY`：有关联 StaffProfile，但没有有效物业任职或岗位分配。
- `UNLINKED_USER`：没有员工档案关联。
- `PUBLIC_REGISTERED_USER`：来自旧公开注册入口的用户。
- `SUPERUSER_OR_TECHNICAL`：平台超级用户、技术运维或系统账号。
- `CONFLICT`：重复绑定、多个员工档案、身份冲突或无法自动判定。
- `DISABLED`：已停用账户。

分类可同时保留主分类与异常标记，避免把技术账号错误归入孤儿用户。

### 12.2 盘点命令

必须提供 management command，至少支持：

- `--dry-run`：只统计、不写库。
- `--output`：导出 CSV/JSON 报告。
- 按分类统计数量。
- 输出 User ID、username 脱敏值、关联 StaffProfile ID、Membership ID、岗位、is_active、is_superuser、来源线索和冲突原因。
- 不输出密码 hash、token、完整联系方式或证件信息。
- 支持只处理指定用户集合。
- 正式写入必须显式参数确认，并记录操作者和审计事件。
- 分类规则版本化，重复执行结果稳定可解释。

### 12.3 处置规则

- 不批量删除 User。
- SUPERUSER_OR_TECHNICAL 必须人工核验 owner 和用途。
- CONFLICT 必须人工核验，不自动绑定或合并。
- PUBLIC_REGISTERED_USER 不自动转为 Guest，也不自动删除。
- STAFF_LINKED 仍需核验任职与岗位有效性。
- 任何账户禁用或关联变更都必须审计并具备回滚/恢复记录。
- 住客历史订单和账务凭证不受 User 分类处置影响。

---

## 13. P0/P1/P2 缺陷修复清单

### 13.1 P0：阻断 Phase 0 验收

| 编号 | 缺陷 | 强制修复 | 验收证据 |
|---|---|---|---|
| P0-01 | 公共限流 IP 维度查询/写入不一致 | 使用独立 RateLimitCounter/Redis，分别计数 IP 与 phone_hash | IP 超限、手机号超限、空手机号、并发请求测试 |
| P0-02 | 空手机号固定 hash 污染全局桶 | 空值不形成共享手机号桶；错误请求仍按 IP 限流 | 空手机号请求不能影响其他用户 |
| P0-03 | AuditEvent 被当限流计数器且不可清理 | AuditEvent 仅记录审计；计数存储有 TTL 和清理 | 审计行不随每次放行请求无限增长 |
| P0-04 | 限流先查后写并发可绕过 | 原子计数、事务锁或 Redis 原子操作 | 并发压测仍严格限额 |
| P0-05 | reservation_detail 和 reservations_create 无限流 | 补查询与公开预订限流 | 两接口 429/Retry-After 测试 |
| P0-06 | api_guard 非法 except JsonResponse | 删除非法异常捕获，保留原始异常映射 | 未预期异常返回契约 JSON 500 |
| P0-07 | 公开预订可批量占库和新增 Guest | IP/phone 限流、PENDING 上限、幂等、库存 TTL 和清理 | 连续下单占库、超限、幂等、过期释放测试 |
| P0-08 | 订单查询/取消仅靠 booking_no + phone | 引入 Booking Access Token，token hash、有效期、撤销和绑定 | 错 token、跨订单、过期、撤销、重放测试 |
| P0-09 | 订单查询泄露 guest_name/provider_ref | 姓名脱敏、移除 provider_ref 和内部字段 | 响应字段白名单测试 |
| P0-10 | 订单查询/取消无审计 | 查看/取消成功失败均写安全审计 | AuditEvent 记录对象、来源、结果且不含明文凭据 |
| P0-11 | 登录失败无持久化审计和防暴力 | AuditEvent + IP/username_hash 限流/退避 | 失败登录审计和限流测试 |
| P0-12 | 生产 mock payment 入口需完整门禁验证 | 所有入口在非 dev/test 拒绝 mock intent | 生产配置集成测试 |
| P0-13 | 基线与测试不可复现 | 固定 commit、环境、命令、数据库版本 | CI/验收记录可复现 |

### 13.2 P1：权限和治理回归

| 编号 | 缺陷 | 强制修复 | 验收证据 |
|---|---|---|---|
| P1-01 | 证件揭示权限码不一致 | 统一 `hotelapp.guest_document_reveal` | 正式角色矩阵授权测试 |
| P1-02 | 财务明细与退款审批混淆 | finance_access 负责查看；refund_approve 负责审批 | 财务角色查看成功但无审批权测试 |
| P1-03 | create_employee 绕过密码策略 | 创建前调用 validate_password | 弱密码创建失败测试 |
| P1-04 | cancel_booking 装饰器重复且匿名行为错误 | 去除员工门禁，统一 redirect/410 | 匿名 GET/POST 测试 |
| P1-05 | 旧 Client/Accommodation/Staff/Finance 写路由仍开放 | 完整路由清单逐条冻结/迁移 | 每条旧路由不能写旧表 |
| P1-06 | 单店模式与半多物业代码冲突 | 关闭运行时切换，非 DEFAULT 明确拒绝 | 非 DEFAULT 请求拒绝测试 |
| P1-07 | superuser 业务越权残留 | 仅保留批准的平台技术例外 | superuser 业务权限负向测试 |
| P1-08 | 测试账号命令可能生成错误授权账号 | 增加生产环境门禁/仅测试数据 | 生产环境执行被拒绝 |
| P1-09 | 登录 disabled 分支不可达/原因失真 | 调整账户状态检查和审计顺序 | disabled 状态原因测试 |
| P1-10 | /manage/staff 与 /staff_accounts 双工作台 | 写流程迁移后退役旧入口 | 新中心功能和旧入口关闭验收 |

### 13.3 P2：治理与运维

- 审计留存、归档、查询性能和不可变策略。
- audit.log 轮转容量、AuditEvent 数据库归档和分区策略。
- 密码字段时间语义。
- 证件加密密钥轮换、备份和恢复。
- 生产 HTTPS、Cookie、HSTS 和可信代理配置。
- 删除/改造死模板 `templates/register.html`，避免误导后续开发。
- Guest 重复档案报告与旧 Client 映射治理。
- 限流监控、告警、异常 fail-open 监控。
- 目标 MySQL CI job 与迁移兼容性验证。

---

## 14. 分阶段实施计划与依赖顺序

### Phase 0：安全止血与基线恢复（最高优先级）

目标：修复现有安全缺陷，确保公共接口、登录、权限和旧写入口达到可验收状态。

任务顺序：

1. AUTH-00：冻结最新 main 基线、工作区、依赖、数据库和测试命令。
2. AUTH-01：修复 api_guard 和统一 API 错误契约。
3. AUTH-02：实现独立限流存储、原子计数、TTL 和监控。
4. AUTH-03：补齐登录失败审计和暴力破解保护。
5. AUTH-04：公开预订防刷、PENDING 上限、幂等和库存 hold 释放。
6. AUTH-05：Booking Access Token、订单查询/取消审计和脱敏。
7. AUTH-06：mock payment 生产门禁全入口验证。
8. AUTH-07：统一证件揭示权限码、恢复财务查看权限、分离退款审批。
9. AUTH-08：修复 create_employee 密码策略。
10. AUTH-09：next 重定向、会话和生产部署安全检查。
11. AUTH-10：旧写路由逐条冻结并补回归测试。
12. AUTH-11：单店运行边界与 superuser 权限审计。
13. AUTH-12：修复现有测试夹具、补齐覆盖并运行 SQLite + MySQL。
14. AUTH-13：更新状态表、审查报告和可复现验收记录。

**Phase 0 关闭条件：** 所有 P0 修复已提交；P1 中影响权限、旧写入和单店边界的阻断项已完成；自动化测试无 error/failure；目标 MySQL 验证通过；没有未解释的高危安全问题；用户/业务负责人签署验收。否则不得启动 Phase 1 的正式上线。

### Phase 1：员工邀请激活与员工中心

依赖 Phase 0 通过：

1. AUTH-14：Invitation 模型、索引和 migration。
2. AUTH-15：邀请状态机和 token 安全服务。
3. AUTH-16：EmployeeLifecycleService 激活/绑定/密码策略集成。
4. AUTH-17：邀请创建、复制、撤销、重发 API。
5. AUTH-18：邀请激活页面和一次性消费。
6. AUTH-19：员工中心 IA、API 和写操作迁移。
7. AUTH-20：权限、CSRF、审计、并发和回归测试。
8. AUTH-21：旧 `/staff_accounts/` 写入口退役。
9. AUTH-22：历史 User 分类命令、dry-run 报告和人工处置流程。

### Phase 2：联系方式模型（独立决策后）

- 建立 EmployeeContact/AccountContact 等明确模型（最终命名另行确定）。
- 明确 email/phone 验证流程、唯一性作用域、共享联系人规则、失效和更换流程。
- 不得把 StaffProfile.phone 改为 unique。
- 只有完成验证后，联系方式才可作为别名或恢复凭据。
- 接入邮件/短信供应商前完成密钥、限流、退订、发送失败和审计设计。
- 本阶段不改变 Phase 1 的 username 主登录策略，除非另行批准。

### Phase 3：独立专项

- 多物业登录和切换。
- MFA 与高风险岗位强认证。
- 住客 OTP 或长期账户。
- Guest 自动匹配/合并策略。
- 更广泛的历史系统数据迁移。

---

## 15. 测试与验收矩阵

### 15.1 必须覆盖的测试类别

**员工认证**
- 有效 username/password 登录。
- 不存在用户、错误密码、停用用户、无任职用户对外文案一致。
- 内部失败原因正确且审计脱敏。
- 登录失败限流、退避自动恢复。
- next 外部 URL、`//host`、反斜线和编码绕过被拒绝。
- 密码策略覆盖创建、激活、绑定、重置、强制改密。
- 管理员重置后 must_change_password=True。
- 邀请激活后 must_change_password=False。

**邀请**
- token 随机性、hash 存储、过期、撤销、重发。
- 一次性消费、重复提交、并发消费只有一个成功。
- token 不能跨员工/任职使用。
- 不可信 Host 不能污染邀请 URL。
- 激活失败统一反馈并限流。

**公共预订**
- IP/phone_hash 限流。
- 空手机号请求不会污染全局桶。
- 同手机号 PENDING 上限。
- 幂等键相同请求只创建一次。
- 同 key 不同请求主体被拒绝。
- 并发库存不超卖。
- hold 超时自动释放且可重试。
- 超限请求不新增 Guest、Reservation、Folio。
- 创建成功/拦截事件审计正确。

**订单查询/取消**
- 只有正确 Booking Access Token 能访问对应订单。
- token 过期、撤销、跨订单、重放均拒绝。
- token 不能被日志或审计明文记录。
- 响应不包含完整姓名、手机号、证件号、provider_ref。
- 查询和取消均限流并写审计。
- 取消仍遵守既有取消政策和状态机。

**权限**
- 证件揭示只使用正式权限码。
- 财务明细查看和退款审批分离。
- 各角色 × 关键端点快照符合既有矩阵。
- 无任职 User、superuser、跨物业请求均按规则拒绝。
- 任何关键动作都不依赖 User.groups 兜底。

**旧路由**
- 路由清单每一项都具备匿名/员工/管理员行为测试。
- 旧路由不再写 Client、Accommodation、Staff、FinanceRecord 等旧表。
- redirect/410 不会通过 GET/POST 副作用写库。
- 旧数据只读和 LegacyRecordLink 映射正确。

### 15.2 测试环境要求

- SQLite：快速单元/基础集成测试。
- MySQL：目标数据库集成测试，必须执行 migrations、约束、事务、锁、并发相关测试。
- 生产配置安全测试：DEBUG=False、HTTPS、Cookie、HSTS、可信代理、mock payment 门禁。
- CI 必须保留测试命令、数据库版本、commit SHA、测试总数、失败数和错误日志摘要。
- 本地因数据库账号或服务不可用而无法运行目标库测试时，必须标记“未验证”，不得用 SQLite 结果替代 MySQL 通过结论。

### 15.3 最终验收记录模板

每次阶段验收必须填写：

| 项目 | 记录 |
|---|---|
| 阶段 | Phase 0 / Phase 1 / Phase 2 |
| main commit SHA | 完整 SHA |
| 工作区 | clean / dirty |
| Python/Django | 实际版本 |
| 数据库 | SQLite/MySQL + 版本 |
| migrations | 命令与结果 |
| 自动化测试 | 命令、总数、通过、失败、错误 |
| 安全专项 | 对应测试报告 |
| 目标数据库验证 | 通过/未通过/未运行及原因 |
| 已知问题 | 编号、风险、负责人 |
| 回滚方案 | migration/code/config 回滚步骤 |
| 验收人 | 业务、技术、安全负责人 |
| 结论 | 通过 / 有条件通过 / 不通过 |

---

## 16. 审计、数据保护与运维要求

### 16.1 审计事件

审计事件至少覆盖：

- `auth.login_success`、`auth.login_failure`、`auth.logout`、`auth.password_changed`、`auth.password_reset`；
- `identity.employee_created`、`identity.employee_bound`、`identity.employee_disabled`、`identity.role_assigned`；
- `identity.invitation_created`、`identity.invitation_resent`、`identity.invitation_revoked`、`identity.invitation_accepted`；
- `guest.booking_access_viewed`、`guest.booking_access_denied`、`guest.booking_cancelled`；
- `public.reservation_created`、`public.reservation_blocked`、`public.rate_limited`；
- `security.permission_denied`、`security.break_glass_used`。

事件名统一、稳定，不得一个动作在不同模块使用不同名称。

### 16.2 审计内容最小化

- 记录操作者/匿名来源、对象 ID、动作、结果、时间、request_id、IP（按隐私策略处理）和必要理由。
- 不记录明文密码、邀请 token、Booking Access Token、完整手机号、完整证件号、支付密钥或完整 provider_ref。
- 对敏感字段揭示只记录字段类型和理由，不把揭示后的明文值写入日志。
- 安全审计与限流计数完全分离。
- AuditEvent 不可变特性必须保留；归档不能破坏审计链和关联性。

### 16.3 留存、归档和恢复

2.0 要求制定正式留存政策，不能仅依赖 `audit.log` 的 1MB × 5 轮转：

- 明确不同审计类型的保留期限和法定/业务依据。
- 定义数据库 AuditEvent 的归档、分区、索引和查询性能策略。
- 归档需校验完整性、可检索性和恢复能力。
- 对加密证件数据定义密钥轮换、备份、灾难恢复和访问审批。
- 定期执行备份恢复演练；记录 RPO/RTO。
- 统一服务器时区、数据库时区和应用时区，审计统一存 UTC，展示按物业时区转换。

---

## 17. 回滚与变更控制

### 17.1 变更要求

每个 PR/提交必须说明：

- 对应 AUTH/P0/P1/P2 编号；
- 影响的模型、API、路由、模板和权限码；
- 数据迁移、数据回填和向后兼容方案；
- 可能的隐私/安全影响；
- 测试命令和实际结果；
- 生产配置变更；
- 回滚步骤；
- 是否影响历史数据和旧入口。

### 17.2 回滚原则

- 新增 token/Invitation/RateLimitCounter 等表应采用可回滚 migration。
- 不能通过删除审计、支付、账务和预订记录回滚业务错误。
- 对无法安全逆向的数据迁移，必须先备份、提供补偿脚本和人工恢复流程。
- 功能开关只能用于暂停新功能，不得让旧的不安全写入口自动恢复。
- 生产事故期间使用 break-glass 必须有审批、限定时长、最小权限和事后复核。

### 17.3 禁止行为

- 禁止开发人员自行修改权限矩阵。
- 禁止把 finance_access 当退款审批权限。
- 禁止把 superuser 当作所有业务动作的默认放行条件。
- 禁止用 AuditEvent 充当限流计数器。
- 禁止仅凭 booking_no + phone 查询/取消订单。
- 禁止在生产环境创建 mock payment intent。
- 禁止通过硬删除方式清理历史 Guest、User、Client、账务或审计数据。
- 禁止旧路由继续写旧业务表而不进入迁移清单。
- 禁止把未运行的 MySQL 测试写成通过。
- 禁止把模型、页面、API 壳或注释存在写成业务功能已交付。

---

## 18. 未决业务事项与批准边界

以下事项必须由业务负责人明确批准，技术人员不得代替业务决策：

1. Guest 重复档案的正式匹配、人工合并审批和合并后主档案规则。
2. Guest/旧 Client 历史映射的业务 owner 和冲突处理人。
3. 审计各类别的法定/业务保留期限。
4. Booking Access Token 的最终有效期若需偏离本文件默认规则。
5. 公开预订 PENDING 上限和限流阈值在容量测试后的调整。
6. 邮件/短信供应商与联系方式验证流程。
7. 多物业、MFA、住客 OTP 的后续专项范围。

未决事项不阻塞本文件已明确的安全止血与基础实施；但任何未决事项不得被开发人员以默认实现方式“悄悄决定”。

---

## 19. 交付物清单

本项目最终必须交付：

1. 员工账户与邀请模型、迁移和服务层。
2. 统一的 EmployeeLifecycleService 和 EmployeeInvitationService。
3. 统一 AuthorizationService 权限读取和权限码注册表。
4. RateLimitCounter/Redis 实现、清理任务、监控和配置。
5. Booking Access Token 模型/服务/API/前端使用流程。
6. 公开预订防刷、幂等、PENDING 上限和库存 hold 释放。
7. 登录失败审计、限流和安全会话配置。
8. 财务明细/退款审批权限分离与证件揭示权限修复。
9. 完整旧写路由清单和冻结/退役代码。
10. Employee Center 页面、API、权限和审计。
11. 历史 User 分类命令与报告。
12. Guest/Client 历史映射与重复档案治理报告。
13. SQLite + MySQL 自动化测试结果。
14. 部署、回滚、监控、审计留存和运维说明。
15. 阶段验收报告和最终基线 commit。

---

## 20. 最终开发执行顺序

开发人员必须严格按以下顺序推进，不得跳过前置条件：

1. **先核对当前 main**：确认代码基线、工作区和真实测试状态。
2. **先修 P0**：API 错误契约、限流、公开预订防刷、订单 token、审计、脱敏和登录安全。
3. **再修权限回归**：证件揭示权限码、财务查看与退款审批、密码策略、superuser 边界。
4. **再封闭旧写入**：建立全量路由清单，逐条冻结旧业务表写入。
5. **完成单店边界**：非 DEFAULT 明确拒绝，不保留静默回退。
6. **通过 SQLite + MySQL 验收**：修复现有测试夹具和测试错误，目标库未通过不得签 Phase 0。
7. **再实现邀请激活**：Invitation 状态机、EmployeeLifecycleService、员工中心迁移。
8. **最后盘点历史账户和数据**：dry-run、人工核验、可回滚处置。
9. **联系方式、多物业、MFA、住客 OTP** 均按后续专项推进。

**最终原则：先保证安全、权限、数据一致性和验收可复现，再扩展功能。任何未通过验收的项目必须保持“未完成”状态，不得为了进度将其标记为已实现。**

---

## 21. Phase 0 实施记录与验收（2026-11-04）

### 21.1 验收记录（§15.3 模板）

| 项目 | 记录 |
|---|---|
| 阶段 | Phase 0（安全止血与基线恢复） |
| main commit SHA | 见提交记录（工作区干净后推送的 HEAD） |
| 工作区 | clean |
| Python/Django | 3.9.0 / 4.1 |
| 数据库 | SQLite（单测）+ MySQL 8（目标库，hotel@localhost） |
| migrations | `makemigrations core add_rate_limit_counter`、`makemigrations reservations add_booking_access_token`；`migrate` 已应用（MySQL 表 core_ratelimitcounter / reservations_bookingaccesstoken 已建） |
| 自动化测试 | `manage.py test --noinput`；SQLite 181 项 + MySQL 181 项，全部通过、0 failure 0 error |
| 安全专项 | test_auth_hardening 20 项（注册止血/冻结/登录统一文案×4/登录限流/审计指纹/密码策略×2/mock 门禁×4/token 生命周期/PENDING 上限/429+Retry-After/原子计数/空值桶） |
| 目标数据库验证 | 通过（MySQL 8 全量 181 项） |
| 已知问题 | P1-10 员工中心写迁移（Phase 1）；audit 归档策略（P2）；`docs/UI_GUIDE.md` 已由并行会话更名重写 |
| 回滚方案 | 两个新 migration 可 `migrate <app> <previous>` 逆向；旧路由 410 guard 为纯代码可 revert；token 表为新增不触历史数据 |
| 结论 | 待业务/安全负责人签署 |

### 21.2 P0/P1 逐项状态（§13）

| 编号 | 状态 | 实施说明 |
|---|---|---|
| P0-01~04 | 已实现待验收 | `RateLimitCounter` 表 + `apps/core/rate_limit.py`：F() 原子自增、窗口唯一约束、TTL 惰性清理（cleanup_expired）、HMAC 指纹、空维度不建共享桶、fail-closed、429 带 Retry-After |
| P0-05 | 已实现待验收 | reservations_create（IP+phone）/detail（IP+booking）/cancel（IP+booking）/payment_intent（IP+booking）四端点全部接入 |
| P0-06 | 已实现待验收 | api_guard 删除 `except JsonResponse`；未预期异常统一 JSON 500 |
| P0-07 | 已实现待验收 | IP+phone 双维限流、PENDING 上限（默认 2，PUBLIC_PENDING_RESERVATION_CAP 可配）、幂等绑定 fingerprint（既有）、`release_expired_reservations` 释放命令（幂等+审计） |
| P0-08 | 已实现待验收 | `BookingAccessToken`（SHA-256 hash、256bit token、离店+30 天有效、可撤销轮换）；创建一次性返回；查询/取消/支付意图全部凭 token（header X-Booking-Token / body token）；`issue_booking_tokens` 补签命令 |
| P0-09 | 已实现待验收 | 查询白名单：姓名脱敏（张*）、移除 provider_ref/完整手机号/内部字段 |
| P0-10 | 已实现待验收 | guest.booking_access_viewed/denied、guest.booking_cancelled、public.reservation_created/blocked/rate_limited 全部落 AuditEvent；不含明文凭据 |
| P0-11 | 已实现待验收 | auth.login_failure 持久审计（username HMAC 指纹）+ IP+username_hash 限流 5/5min |
| P0-12 | 已实现待验收 | `_is_dev_env()` 门禁唯一入口；测试覆盖生产 403 |
| P0-13 | 已实现待验收 | 本节记录 + 双引擎测试证据 |
| P1-01 | 已实现待验收 | 证件/电话揭示统一 `hotelapp.guest_document_reveal/phone_reveal`（AuthorizationService） |
| P1-02 | 已实现待验收 | 财务明细=finance_access；审批只认 billing.refund_approve；superuser 不再兜底 |
| P1-03 | 已实现待验收 | create_employee 主路径 `_enforce_password_policy` |
| P1-04 | 已实现待验收 | cancel_booking 去全部装饰器，匿名/员工统一 302 → /reserve/ |
| P1-05 | 已实现待验收 | 33 个旧写路由 410（insert/update/delete 全家、checkin_confirm/in、finance_add、walkin_create、checkout/uncheckout/extend_stay/change_room/express_checkout/room_status_change、staff_toggle）；`/booking/` `/register/` redirect；测试断言不写旧表 |
| P1-06 | 已实现待验收 | PropertyContextMiddleware 重写：无 DEFAULT 任职 → 无物业上下文+security.property_context_denied 审计；移除 active_property_id 半状态 |
| P1-07 | 已实现待验收 | reservation_center/guest_center superuser 直通清除 |
| P1-08 | 已实现待验收 | seed_role_test_accounts 生产环境 CommandError 拒绝 |
| P1-09 | 已实现待验收 | 登录 disabled 分支修正：先查账号再 authenticate，停用原因正确审计 |
| P1-10 | 暂缓 | Phase 1（AUTH-19~21 员工中心迁移） |
| P2 | 部分完成 | templates/register.html 已删除；审计归档/HTTPS/密钥轮换为运维事项 |

### 21.3 浏览器端到端验证

/reserve/ 页面全流程：创建订单（CSRF+token 返回）→ 错 token 查询 404（guest.booking_access_denied 落审计）→ 页面取消按钮 token 取消成功（guest.booking_cancelled 落审计）。AuditEvent 数据库取证确认。


---

## 2026-11-09 最新 main 实施与测试状态补充

> 本文件继续作为身份认证、员工账号和住客公共访问的正式规则基线；本节仅更新代码/测试现状，不改变原有账号政策。

- 最新业务代码核对基线：main@8e310b5a8491f31dd91b5461160fd6dfc1f22e06。
- 九角色测试账号由 seed_role_test_accounts 命令准备；每个账号独立 User、StaffProfile、PropertyStaffMembership、StaffRoleAssignment，不通过 User.groups 直接授权。
- 当前五个 ACTIVE 角色为前台、值班经理、财务、店长、客房服务员；夜审员、餐饮服务员、餐饮主管、停车安保四个角色为 PLANNED 空组、0 权限。四个 PLANNED 角色不得被临时扩权以满足业务测试。
- 初始化命令使用 getpass 隐藏输入和 Django 密码校验器；密码不写入仓库、日志或文档。
- 最新 SQLite 与 MySQL 8.0.46 CI 各 887 项通过；但九角色真实口令登录、真实浏览器 E2E 和 G14 独立验收仍待完成。
- 全链路脚本 scripts/e2e_full_chain.py 的白盒缺陷和安全限制见《PMS2最新代码单元测试与白盒全链路审查报告_2026-11-09.md》。
