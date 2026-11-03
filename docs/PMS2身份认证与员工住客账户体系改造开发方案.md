# PMS 2.0 身份认证与员工 / 住客账户体系改造方案（V0.2）

> 2026-11-04；核对基线 main @ 610fe3c28bfc43058b69b2d4054c8adbefee70a1。状态：待评审，D1-D12 尚未正式批准。

## 目标

本期按单店运行，收口员工账号、邀请激活和住客免注册预订；修复认证安全与权限读取不一致。保留 Property / Membership 数据模型，但不承诺多物业登录、切换或跨店报表。不得改变既有权限矩阵、敏感字段、审批和财务决策。

## 当前事实与目标态区分

| 项目 | 当前代码事实 | 目标/待办 |
|---|---|---|
| 员工主登录 | login_view 使用 username/password | 本期继续以全局唯一 username 登录 |
| 邀请激活 | 无邀请模型/token 流程 | 新增一次性邀请，管理员复制链接人工发送 |
| 邮件/短信 | 未配置服务商 | 本期不得显示已发送邮件/短信 |
| 联系方式别名 | phone 未验证且可共享；无 email 验证体系 | 后续 D11 决策后再做，不得在 phone 上直接加 unique |
| 多物业 | 门禁和中间件依赖 DEFAULT；无选择页 | 本期单店运行；多物业另立项 |
| 员工中心 | /manage/staff/ 主要只读；/staff_accounts/ 承载写操作 | 迁移完整写流程后再退役旧入口 |
| 住客预订 | /reserve/ 使用 PMS2，不需 Django User | 继续免注册直订 |
| 旧顾客注册 | /register/ 仍创建 User；登录页仍有注册链接 | 停止创建孤儿 User，移除链接 |
| 旧预订 | /booking/ 仍有旧模型写入 | 冻结写入，最终退役 |
| 订单查询 | booking_no + phone 查询返回个人/金额/支付信息 | 限流、审计、脱敏、统一错误反馈 |
| Mock 支付 | mock intent 创建需有生产环境门禁 | 生产拒绝创建 mock payment |

## 员工账号方案

- 本期唯一主登录标识为稳定且全局唯一的 username。
- 不开放公开注册入口创建可进入 PMS 的账号。现有注册虽不赋予员工权限，但仍会创建没有 Guest/任职关联的孤儿 User；本期必须停止此创建行为。
- 员工 Profile、物业 Membership、RoleAssignment 分离；业务授权必须通过 AuthorizationService，不得把 User.groups 当作业务任职事实。
- 邀请 Service 只管理 token hash、有效期、一次性消费、撤销、重发和邀请状态；User 创建/绑定、密码设置必须调用 EmployeeLifecycleService，View 不得直接 create_user。
- 待激活账号使用不可用密码；员工接受邀请时自行设置密码，成功后 must_change_password=False；管理员重置密码后 must_change_password=True。
- 本期不提供自助找回密码；没有已验证联系方式时由管理员重置。

## 住客方案

- /reserve/ 免注册直订，不创建 Django User。
- /register/ 停止创建 User；登录页移除注册链接。
- /booking/ 冻结旧写入，最终退役并统一指向 /reserve/。
- 本期不做住客 OTP，但订单查询必须完成限流、审计、遮罩和统一错误响应。
- Guest 重复档案策略必须由产品确认，不能由开发自行按手机号自动合并。
- 生产环境禁用 mock payment intent 创建；dev/test 保留模拟支付测试。

## D1-D12 决策表

| 决策 | 推荐结论 | 依赖 |
|---|---|---|
| D1 公开注册 | 不允许公开入口创建 PMS 员工账号；停止 register_view 新建 User | D8、D10 |
| D2a 主登录 | 本期只用 username | 无 |
| D2b 联系方式别名 | 后续，完成 D11 后再决策 | D11 |
| D3 邮箱要求 | 不强制企业邮箱；本期人工邀请，无自助找回 | D6 |
| D4 共享手机号 | 业务号码可共享，不可作为唯一认证凭据 | D2b、D11 |
| D5 多物业 | 本期单店；保留模型，不做切换/跨店报表 | 决定 AUTH-10 范围 |
| D6 邀请渠道 | 管理员复制一次性链接并人工发送，不伪造发送成功 | D3 |
| D7 MFA | 本期不做，后续评估高风险岗位 | 独立评估 |
| D8 住客长期账号 | 不需要；免注册直订，关闭旧注册/旧预订写入 | D1 |
| D9 住客 OTP | 后续；本期先做查询安全和 Mock 支付门禁 | D8 |
| D10 历史 User | 先停止新增，再分类；不直接删除 | D1 |
| D11 联系方式模型 | 待定独立模型/字段、唯一性、验证、共享和失效规则 | D2b-D4 |
| D12 状态机 | 定义未开通/待激活/启用/停用优先级及密码语义 | D6 |

若决定本期支持多物业，必须另行覆盖登录门禁、中间件、Membership 服务、全局/物业状态语义、选择页、Session、菜单、API 过滤和跨物业越权测试，不能只做选择页。

## P0 任务

1. 关闭 /register/ 的 User 创建，移除登录页注册链接。
2. 冻结 /booking/ 旧写入。
3. next 使用 Django url_has_allowed_host_and_scheme，拒绝 //host 和外域跳转。
4. 登录失败对外统一文案，内部审计保留细分原因。
5. 统一调用 validate_password，覆盖创建、激活、重置和强制改密。
6. 全仓审计 user.groups / has_perm；业务授权统一 AuthorizationService，平台权限单独处理。
7. 生产环境拒绝 mock payment intent 创建。
8. 订单查询/取消加入共享缓存或数据库限流、审计、脱敏；LocMemCache 不得作为多进程限流唯一机制。
9. 公开预订创建加入防刷评估、幂等和库存占用滥用测试。

## 阶段任务

**Phase 0 止血：** AUTH-00 基线与调用点盘点；AUTH-01 注册止血；AUTH-02 冻结旧 booking；AUTH-03 next 安全；AUTH-04 登录错误统一；AUTH-05 密码验证；AUTH-06 权限读取审计；AUTH-07 mock 支付门禁；AUTH-08 订单查询安全；AUTH-09 公开预订防刷。

**Phase 1 邀请激活：** AUTH-10 Invitation 状态机；AUTH-11 EmployeeLifecycleService 激活/绑定；AUTH-12 邀请 API 和激活页；AUTH-13 员工中心写操作迁移；AUTH-14 staff_accounts 退役门禁；AUTH-15 历史账号分类。

**Phase 2 联系方式：** D11 决策后实施验证、唯一性、变更失效、审计与恢复；依赖真实邮件/短信服务商和共享限流后端。

**Phase 3 后续专项：** 多物业、MFA、住客 OTP 各自独立立项。

## 验收

- 公开注册不创建 User；旧 booking 不写旧业务表。
- 邀请一次性、过期、撤销、重放、并发消费均有测试。
- User 创建/绑定只能经 EmployeeLifecycleService；密码规则使用 validate_password。
- 新邀请员工的业务权限与现有员工一致，不依赖未维护的 User.groups。
- 无开放重定向；登录错误不泄露账号状态。
- 订单查询有限流、审计、遮罩；生产不能创建 mock payment。
- 员工中心覆盖档案、账号、任职、岗位、邀请和审计。
- Django check、migration check、自动化测试及目标数据库测试通过。

## 历史账号处置

停止新增后，分类为 STAFF_LINKED、PROFILE_ONLY、UNLINKED_USER、PUBLIC_REGISTERED_USER、SUPERUSER_OR_TECHNICAL、CONFLICT、DISABLED。admin 单列技术身份，不按孤儿删除；冲突人工核验。不得删除历史预订、审计和账务凭证。

## 开发纪律

D1-D12 未确认前，只能做不改变业务政策的盘点、测试补充和安全止血。每项功能标注已实现/部分实现/未实现/待验收；模型存在不等于功能交付。每个 PR 说明权限、数据、迁移、回滚、测试和已知限制。