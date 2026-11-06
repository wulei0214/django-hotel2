# PMS2 MySQL 财务与审计不可变事实部署说明

## 1. 目的

Django 模型的 `save()/delete()` 和自定义 QuerySet 只能防止正常 ORM 路径误改；具有原生 SQL 权限的账号仍可能绕过应用层。

生产环境必须为以下只追加事实表启用 MySQL 8 触发器：

- `core_auditevent`：审计事件；
- `billing_ledgerentry`：账本凭证；
- `billing_cashiershiftmovement`：班次现金补入/支出；
- `billing_cashiershiftmovement` 的 BEFORE INSERT 触发器：禁止向已关班班次或非本人班次插入现金变动。

有状态机的表不能一律禁止 UPDATE：

- `billing_payment`：支付状态由渠道回调/收款领域服务推进；
- `billing_refund`：申请、审批、执行按状态机推进；
- `billing_cashiershift`：OPEN → CLOSED；
- `billing_approvalrecord`：PENDING → 最终审批状态。

## 2. 部署步骤

1. 备份数据库并确认当前连接到目标 PMS 数据库。
2. 使用 DBA/迁移账号执行 `apps/core/sql/audit_immutability.sql`，或在有 `CREATE TRIGGER` 权限的 MySQL 连接下执行 `python manage.py install_mysql_append_only_guards`。
3. 执行 `python manage.py check_mysql_append_only_guards`；必须确认 7 条触发器的名称、表、事件、时机和 `SIGNAL SQLSTATE '45000'` 阻断语句全部匹配。
4. 应用运行账号不得拥有上述三张表的 UPDATE/DELETE 权限；迁移账号单独保管，不写入应用配置。
5. 用应用账号分别尝试 UPDATE/DELETE，必须被权限或触发器拒绝；正常 INSERT 必须成功。

## 3. 权限示例

将下列占位符替换成实际数据库名和应用账号后，由 DBA 单独执行：

```sql
REVOKE UPDATE, DELETE ON `PMS_DATABASE`.`core_auditevent`
FROM 'pms_runtime'@'%';
REVOKE UPDATE, DELETE ON `PMS_DATABASE`.`billing_ledgerentry`
FROM 'pms_runtime'@'%';
REVOKE UPDATE, DELETE ON `PMS_DATABASE`.`billing_cashiershiftmovement`
FROM 'pms_runtime'@'%';
```

不要对迁移账号执行上述 REVOKE，否则 Django migration 无法正常维护 schema。

## 4. 验收

- [ ] 6 条 BEFORE UPDATE/DELETE 只追加触发器 + 1 条 BEFORE INSERT 班次保护触发器全部存在，且关联正确表/事件/时机。
- [ ] 应用账号无法 UPDATE/DELETE 三张只追加事实表。
- [ ] 应用账号可以正常 INSERT 新审计、账本、班次现金事实。
- [ ] Django ORM 的 QuerySet.update/delete/bulk_create/bulk_update 同样被模型管理器拒绝。
- [ ] MySQL CI 的全量迁移、约束和并发测试通过。
- [ ] 备份恢复演练通过。

SQLite 测试不能代替本项；触发器必须在实际生产 MySQL 实例中由 DBA 部署并留存验证结果。

## 5. CI 自动验证

`.github/workflows/ci.yml` 使用独立 MySQL 8 服务执行全量迁移和测试套件；测试数据库清理完成后，在 CI 的迁移数据库上运行：

1. `python manage.py install_mysql_append_only_guards`
2. `python manage.py check_mysql_append_only_guards`

因此 MySQL CI 不仅验证 Django 迁移和约束，还验证七条触发器可创建且定义正确。生产数据库仍需单独部署；CI 的临时数据库触发器不会自动部署到本地或生产环境。

## 6. 退款审批超时任务

退款审批到期后必须把 ApprovalRecord、Refund、Payment 同步关闭，释放原支付可退额度。部署后应通过操作系统任务计划程序定时执行：

```bash
python manage.py expire_pending_refunds --limit 100
```

可选 `--property-id <物业ID>` 限定物业。建议每 5 分钟运行一次；命令每笔退款独立事务、锁顺序为 Payment → Refund → ApprovalRecord，重复运行幂等。任务账号应使用应用服务账号，不得把数据库密码写入命令行参数或日志。

## 7. 本地 MySQL 连接排查

若出现 `Access denied for user ... @localhost`：

- 检查 `.env` / 环境变量中的 `DJANGO_DB_USER`、`DJANGO_DB_PASSWORD`、`DJANGO_DB_HOST`、`DJANGO_DB_PORT` 是否与实际 MySQL 账号一致。
- 环境变量中的账号值不要包含 SQL 引号包裹字符；SQL 中的账号引号写法与 Django 环境变量值不是同一种格式。
- 使用 DBA 账号核对 `mysql.user` 中账号与 Host 授权范围，并确认目标数据库权限。
- 修复连接后先备份，再执行 `python manage.py migrate --plan`、`python manage.py migrate`、`python manage.py check_mysql_append_only_guards`。
- 不要把生产密码、个人密码或迁移账号密码写入仓库或文档。
