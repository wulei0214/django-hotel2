# V12-02 MySQL `@localhost` 孪生账户旧口令安全复核报告

| 项目 | 内容 |
|---|---|
| 任务编号 | V12-02（P0，安全） |
| 执行环境 | 本机 MySQL 8.0.33（`127.0.0.1:3306`），业务库 `hotel`，账号 `hotelhotel` |
| 执行人 | 开发（本轮实施）；账户枚举/轮换需 DBA 权限，**未完成** |
| 执行时间 | 2026-11-20（UTC+8） |
| 结论 | **BLOCKED（无法关闭）**：应用侧证据齐备，但"账户是否存在 / 旧口令是否失效"仍需 DBA 权限确认 |

## 一、已取得的客观证据（不含任何口令）

| # | 检查项 | 命令（口令经 `MYSQL_PWD` 注入，不落命令行/日志） | 实测结果 |
|---|---|---|---|
| 1 | 应用连接实际命中的账户 | `mysql -h 127.0.0.1 -u hotelhotel -e "SELECT CURRENT_USER(), USER(), @@skip_name_resolve"` | `CURRENT_USER()=hotelhotel@127.0.0.1`；`USER()=hotelhotel@localhost`；`skip_name_resolve=0` |
| 2 | `@localhost` 路径是否接受当前应用口令 | `mysql -h localhost -u hotelhotel ...`（同一口令） | **`ERROR 1045 (28000): Access denied for user 'hotelhotel'@'localhost' (using password: YES)`** |
| 3 | 应用账户权限面 | `SHOW GRANTS FOR CURRENT_USER()` | `GRANT USAGE ON *.* TO 'hotelhotel'@'127.0.0.1'`；`GRANT ALL ON hotel.*`；`GRANT ALL ON test_hotel.*`（均只对 `@127.0.0.1` 授予） |
| 4 | 是否可枚举账户 | `SELECT user,host FROM mysql.user` | **`ERROR 1142 (42000): SELECT command denied ... for table 'user'`**（最小权限，符合预期） |
| 5 | 可见的授权主体 | `SELECT DISTINCT GRANTEE FROM information_schema.USER_PRIVILEGES` | 仅 `'hotelhotel'@'127.0.0.1'` |
| 6 | 应用配置引用 | `.env`（gitignore）中 `DJANGO_DB_HOST=127.0.0.1` | 应用只走 `127.0.0.1`，不引用 `localhost` |

## 二、能证明什么、不能证明什么

**可以证明：**

1. 应用实际认证命中的是 `hotelhotel@127.0.0.1`，权限仅限 `hotel` / `test_hotel`，无全局权限。
2. 当前应用口令在 `@localhost` 认证路径上**认证失败**——即"应用在用口令"与
   任何 `@localhost` 孪生账户的口令**不相同**（若存在孪生账户，它保存的是另一套口令）。
3. 业务账号无法读取 `mysql.user`，因此凭据泄露面不含账户表。

**不能证明（因此不能关闭）：**

- `hotelhotel@localhost` 是否仍然存在；
- 若存在，其口令是否为历史旧口令、是否仍可认证、是否仍具业务库权限；
- 是否存在其它 `hotel%` 前缀的重复账户（`test_*`、只读账号等）。

`ERROR 1045` 既可能来自"账户不存在"，也可能来自"账户存在但口令不同"，
MySQL 不区分二者，**不能据此判定孪生账户已删除或已失效**。

## 三、需 DBA 执行的收口动作（未执行）

以具备 `SELECT ON mysql.*` 与 `CREATE USER` 权限的账户执行：

```sql
-- 1) 账户维度盘点（含锁定状态与口令最后修改时间）
SELECT user, host, account_locked, password_last_changed
FROM mysql.user WHERE user LIKE 'hotel%' ORDER BY user, host;

-- 2) 若存在 @localhost 孪生账户：先锁定并保留证据，再决定轮换或删除
ALTER USER 'hotelhotel'@'localhost' ACCOUNT LOCK;
-- 或（确认无调用方后）DROP USER 'hotelhotel'@'localhost';

-- 3) 轮换后验证：旧口令必须认证失败，新口令可用应用连接成功
```

验收证据要求（DBA 侧补齐后本任务方可关闭）：
账户清单（User/Host/锁定状态/口令最后修改时间）、锁定或删除的执行记录、
旧口令认证失败的失败码、新口令应用连接成功结果、执行人与时间。

## 四、结论与纪律说明

- 本任务**不因下列任一事由关闭**：凭据扫描器命中 0、文档已脱敏、代码中无明文口令。
- 当前状态：**BLOCKED**（依赖 DBA 权限），风险面已通过"应用仅用 `@127.0.0.1` +
  该口令在 `@localhost` 路径认证失败"收窄，但**未消除**。
- 临时控制措施：应用连接串固定 `127.0.0.1`；业务账号无 `mysql` 库权限；
  `.env` 已在 gitignore 且经 V12-06 扫描确认未入库。
- 负责人 / 期限：需具备 MySQL 管理权限的负责人在下一运维窗口执行 §三 动作。
