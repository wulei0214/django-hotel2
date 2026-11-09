# PMS 2.0 — Phase 1 平台底座开发记录

- 分支：`pms2/phase1-platform-foundation`
- 目标：建立物业上下文、员工账号关系、岗位授权和安全配置基线。
- 版本边界：本轮属于 PMS 2.0 架构重构；3.0 仅安排现有功能完善、缺陷修复和回归，不新增业务功能。

## 本阶段实际改动

1. 新增 `apps.core.Property`，迁移时初始化 `DEFAULT` 单店物业。
2. 新增 `StaffProfile`、`PropertyStaffMembership`、`StaffRoleAssignment`。
3. 把旧版 `hotelapp.Staff` 回填为员工档案和默认物业任职关系；不推断旧 Django User 与员工的对应关系。
4. 新增物业范围授权服务。岗位 Group 通过任职关系分配，不写入 `User.groups`；因此不会把某家酒店的角色权限自动变成全局权限。
5. 新增 `bind_staff_user` 管理命令，管理员必须显式选择员工工号、账号、物业和岗位。
6. 新增物业上下文中间件和 PMS 专用权限装饰器。旧视图仍暂时保留原门禁，避免员工账号尚未绑定时把现有后台全部锁死。
7. 把 Django 密钥、数据库连接、Allowed Hosts 和 HTTPS 安全选项改为环境变量；增加 SQLite 测试配置。
8. 新增物业和授权服务测试、GitHub Actions CI 工作流。

## 数据迁移注意

- 先备份现有数据库，再部署代码。
- 运行 `python manage.py migrate` 后会创建 `DEFAULT` 物业，并把旧 Staff 档案迁移到新表。
- 旧员工工号以旧 `Staff.员工id` 字符串形式写入该物业的 `employee_no`。
- 账号绑定需在确认员工与账号身份后逐个执行，例如：

```bash
python manage.py bind_staff_user --employee-no 12 --username front --role FRONT_DESK
```

- 命令不会设置 `User.is_staff`、`is_superuser` 或写入 `User.groups`。
- 当前旧业务视图仍使用旧权限装饰器；必须在完成员工账号核对与绑定、补齐各业务域权限点后，分模块切换到 `pms_employee_required` 和 `pms_permission_required`。

## 验证状态

CI 将执行：

- `python manage.py check`
- `python manage.py makemigrations --check --dry-run`
- `python manage.py test --verbosity 2`

本地/CI 的实际运行结果以 GitHub Actions 工作流为准。本文件创建时尚未把“测试通过”作为既成事实。

## 本阶段未做

- 未升级 Django 主版本，避免在没有完整回归测试的情况下同时进行框架与数据模型迁移。
- 未接入真实支付、公安、OTA、数电票、POS 或停车系统。
- 未开始房价、库存、预订、账务、夜审业务域的第二轮拆分。
