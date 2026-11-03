# 仓库文档与代码一致性审查报告

> 审查日期：2026-11-04；审查基线：main @ 610fe3c28bfc43058b69b2d4054c8adbefee70a1。范围：docs、根 readme.md，以及身份认证、员工授权、住客预订查询和支付相关代码。静态审查，不代表已运行 Django 测试或生产数据库审计。

## 结论

当前文档有“目标态写成现状”“历史测试结果未标过期”“跨文档入口描述冲突”等问题。不能为消除冲突而批量删除正式决策、migration、审计或账务文件；应保留正式决策，修正事实说明，并给文档加状态、SHA 和核对日期。

## 重点问题

| 等级 | 代码/文档位置 | 问题 | 要求 |
|---|---|---|---|
| P0 | templates/login.html | 仍显示 /register/ 与 /booking/ 链接 | 移除注册入口，顾客入口统一 /reserve/ |
| P0 | hotelapp/views.py | register_view 仍创建 User；next 仅 startswith('/') | 停止创建孤儿 User；改用 url_has_allowed_host_and_scheme |
| P0 | apps/api/views.py reservation_detail | booking_no + phone 可返回姓名、金额、支付列表；需限流、审计、脱敏 | 完成安全控制和负向测试后才可对外开放 |
| P0 | apps/api/views.py payment_intent_create | mock intent 创建必须有生产门禁，不能只保护 callback | 生产拒绝 mock 创建并回归测试 |
| P0 | apps/api/approval.py、apps/api/guest_center.py、apps/guests/services.py | 仍有 User.groups / has_perm 读取路径，可能与 RoleAssignment 授权不一致 | 全仓盘点，业务授权统一 AuthorizationService；平台权限单独定义 |
| P1 | apps/identity/models.py、services.py | phone 是可共享业务联系字段，不是已验证认证凭据；无邀请/验证体系 | 不直接给 phone 加 unique；D11 先决策 |
| P1 | apps/identity/middleware.py、hotelapp/views.py | 当前按 DEFAULT 单店上下文运行 | 本期按单店排期；不宣称多物业切换已支持 |
| P1 | /manage/staff/ 与 /staff_accounts/ | 新员工中心主要只读，旧 URL 仍承担生命周期写操作 | 迁移写操作、权限、CSRF、审计和测试后再退役 |
| P1 | apps/reservations/services.py | 创建预订路径会创建 Guest/GuestPhone；自动复用/合并策略未定义 | 产品确认去重策略，不按手机号自动合并 |
| P1 | settings.py | LocMemCache 不适合多进程共享限流 | 生产限流使用共享缓存或数据库计数 |
| P2 | docs/ARCHITECTURE.md | 仍有部分段落把 PMS2 前台履约描述为未来工作，与当前 /checkin/ 和 API 不完全一致 | 按页面/API/Service/测试四层更新成熟度 |
| P2 | docs/DEVELOPMENT_GUIDE.md | /booking/ 仍列为兼容入口，未说明冻结目标 | 更新为旧入口待退役，正式入口 /reserve/ |
| P2 | docs/角色权限验证指南.md | 2026-11-02 实测、旧菜单和 User.groups 语义不可直接代表当前 main | 重新执行测试后再记录通过；历史结果标记过期 |
| P2 | readme.md | 宣称“注册止血”，但注册视图仍创建 User；仍把 /booking/ 列为顾客预订 | 代码修复后再标完成；入口说明需同步 |

## 代码阅读注解（供开发、测试、产品）

| 文件 | 应理解的职责/边界 | 重点验证 |
|---|---|---|
| hotelapp/views.py | 登录、注册、next 跳转及部分员工生命周期旧入口；区分平台账号与员工任职 | 错误提示、外部 next、注册不建 User、任职门禁 |
| apps/identity/models.py | User 是认证主体；Profile 是员工档案；Membership 是物业任职；Assignment 是岗位授权 | 一对一绑定、任职状态、岗位生效期 |
| apps/identity/services.py | 员工生命周期写入口；邀请激活应扩展此服务 | View 不得直接 create_user；重置/激活密码语义 |
| apps/identity/authorization.py | 运行时结合用户、物业、有效任职和岗位权限判断 | 页面/API 直接访问越权测试 |
| apps/identity/middleware.py | 当前请求物业上下文来源；DEFAULT 是当前单店约束，不是多物业能力 | 上下文缺失、错误回退和跨物业隔离 |
| apps/api/views.py | 公开预订、订单查询/取消和 mock 支付 API | 限流、脱敏、审计、幂等、生产门禁 |
| apps/api/approval.py、guest_center.py | 检查旧 Group 权限读取是否绕过新授权服务 | 新邀请员工权限一致性 |
| apps/guests/services.py | Guest/GuestPhone 写入口与共享号码标记；不等于自然人自动合并 | 共享号码、重复档案和人工合并流程 |
| templates/login.html | 登录页入口必须反映真实产品策略 | 无公开注册链接；预订链接指向 /reserve/ |

## 文档治理规则

1. 每份文档头部标明类型、状态、核对分支/SHA、最后核对日期。
2. 正式决策文件不因与代码不一致而删除；由代码遵循决策或另行形成正式决议。
3. 测试报告注明环境、数据库、日期、命令、账号来源、提交 SHA；旧结果标为历史记录。
4. 方案中的能力标记已实现/部分实现/未实现/待验收；不能用模型存在代替功能交付。
5. 数据库删表删字段前必须有真实 schema、外键、migration graph、数据量、备份恢复和业务签字。
6. 删除文档前检查 README 链接、正式决策引用和 Git 历史；优先更正或归档。
7. 代码注释应解释业务不变量、权限边界、事务/幂等和风险，不要重复代码或承诺未实现能力。

## 本次未执行

- 未运行 Django test、MySQL/SQLite CI 或生产数据审计。
- 未修复上述 P0 代码问题；报告只记录发现和修复要求。
- 未删除历史决策文件、migration、审计或账务数据。
- 需要后续代码 PR 落实修复、测试和源代码注释。