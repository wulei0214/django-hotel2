# V13-01 MySQL 孪生账户取证报告骨架（DBA 移交件）

| 项 | 内容 |
|---|---|
| 编号 | V13-01 |
| 任务书 | 《PMS 2.0 V1.3 未关闭事项专项收口与最终验收补强 第四轮正式执行建议书 V1.3（负责人裁定版）》 |
| 状态 | **BLOCKED**（开发侧骨架已就绪；待 DBA 以特权账户执行 §二 查询并回填） |
| 责任划分 | 开发：建骨架 + 提供精确 SQL 与回填格式（本文件）；DBA：执行查询、处置、回填结论 |
| 关联 | V12-02（上一轮同类项，未关闭）、V1.2 总表遗留项 L1（P0） |

## 一、背景与目标

上一轮 V12-02 因应用账号无权读取 `mysql.user`（仅能以 `ERROR 1045` 间接观察，而该错误无法区分"账户不存在"与"口令不同"），按任务书纪律**不能据此关闭**。本轮目标不变：以 DBA 特权账户在**生产与开发两侧 MySQL** 上取证，证明不存在与应用账号同名/同口令的孪生账户，或对已发现孪生账户完成 LOCK/DROP 处置。

## 二、DBA 待执行清单（逐条可复算）

### 2.1 账户清单取证（开发/生产两侧各执行一次）

```sql
-- ① 全量账户与认证插件（只读）
SELECT User, Host, plugin, account_locked,
       password_last_changed, password_expired
FROM mysql.user ORDER BY User, Host;

-- ② 应用相关账户的孪生筛查（名称包含应用账号词根的账户）
SELECT User, Host FROM mysql.user
WHERE User REGEXP 'hotel|pms|django|app|web';

-- ③ 应用库级授权面（确认授权只落在预期账户上）
SELECT User, Host, Db, Table_name, Privilege_type
FROM mysql.db WHERE Db IN ('hotel', 'test_hotel')
UNION ALL
SELECT User, Host, Db, '*', Privilege_type
FROM mysql.tables_priv WHERE Db IN ('hotel', 'test_hotel');
```

### 2.2 判定口径（与 V12-02 一致）

- `mysql.user` 中**不存在**除 DBA/运维登记账户之外、指向应用库（`hotel`）授权的账户 → 孪生账户**不存在**，V13-01/V12-02/L1 关闭；
- 存在未登记账户且对 `hotel` 库有授权 → 记 **FAIL**，须执行处置：
```sql
ALTER USER '<user>'@'<host>' ACCOUNT LOCK;   -- 先锁后观察
-- 确认无业务依赖后：DROP USER '<user>'@'<host>';
```
- 仅存在同名但 Host 不同的登记账户 → 逐条登记用途与责任人，不算孪生，但须在 §三 回填清单中列明。

### 2.3 回填格式（DBA 直接改写 §三）

| 侧 | 查询 | 执行时间 | 结果行数 | 结论（不存在/已锁定/已删除/待跟进） | 执行人 |
|---|---|---|---|---|---|
| 开发 | ①②③ | _待回填_ | _待回填_ | _待回填_ | _待回填_ |
| 生产 | ①②③ | _待回填_ | _待回填_ | _待回填_ | _待回填_ |

## 三、DBA 回填区（待 DBA 覆写）

> 以下为占位骨架，DBA 执行后请用真实输出（可脱敏 Host/口令哈希，账户名与行数必须保留）替换本节。

```
[DBA 回填区 —— 空]
```

## 四、开发侧已完成工作

- 本骨架与 §二 SQL、判定口径、回填格式；
- 应用侧间接观察记录沿用 V12-02：应用账号尝试读取 `mysql.user` 被拒（权限面正确收窄的旁证）；
- 本轮 dev 环境实测 `max_connections=20` 已由 V13-04 单独出具（与本项目不混同）。

## 五、关闭条件

V13-01 仅在 §三 回填完整（两侧三查询各有真实输出与结论、FAIL 项有处置凭据）且与 §二 判定口径一致时方可置 PASS；在此之前维持 **BLOCKED**，并连带 V1.2 总表 L1、V12-02 维持未关闭状态。整体 GO/NO-GO 判定见 `V1.3_未关闭事项总表与最终验收报告.md`。
