# PMS2 MySQL 财务与审计不可变事实部署说明

## 1. 目的

Django 模型的 `save()/delete()` 和自定义 QuerySet 只能防止正常 ORM 路径误改；具有原生 SQL 权限的账号仍可能绕过应用层。

生产环境必须为以下只追加事实表启用 MySQL 8 触发器：

- `core_auditevent`：审计事件；
- `billing_ledgerentry`：账本凭证；
- `billing_cashiershiftmovement`：班次现金补入/支出。

有状态机的表不能一律禁止 UPDATE：

- `billing_payment`：支付状态由渠道回调/收款领域服务推进；
- `billing_refund`：申请、审批、执行按状态机推进；
- `billing_cashiershift`：OPEN → CLOSED；
- `billing_approvalrecord`：PENDING → 最终审批状态。

## 2. 部署步骤

1. 备份数据库并确认当前连接到目标 PMS 数据库。
2. 使用 DBA/迁移账号执行 `apps/core/sql/audit_immutability.sql`。
3. 运行脚本末尾的 `information_schema.TRIGGERS` 查询，必须返回 6 条触发器记录。
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

- [ ] 6 条触发器全部存在，且关联正确表/事件/时机。
- [ ] 应用账号无法 UPDATE/DELETE 三张只追加事实表。
- [ ] 应用账号可以正常 INSERT 新审计、账本、班次现金事实。
- [ ] Django ORM 的 QuerySet.update/delete/bulk_create/bulk_update 同样被模型管理器拒绝。
- [ ] MySQL CI 的全量迁移、约束和并发测试通过。
- [ ] 备份恢复演练通过。

SQLite 测试不能代替本项；触发器必须在实际生产 MySQL 实例中由 DBA 部署并留存验证结果。
