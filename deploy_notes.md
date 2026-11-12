# 性能优化与安全保障 — 实施说明与升级路线

本文档说明项目中已落地的部分，以及生产环境（Redis / Nginx / 异步任务）的升级路径。

## 一、已落地

### 性能

| 项 | 实现 | 位置 |
|---|---|---|
| 缓存策略 | 开发默认 LocMemCache；生产必须 Redis（Django 内置 RedisCache）；查重缓存 60 秒，缓存不可用时回退数据库，不影响权威查询 | `hotel/settings.py` CACHES、`hotelapp/views.py` check_unique |
| 数据库长连接 | `CONN_MAX_AGE = 60`，避免每个请求重建 MySQL 连接 | `hotel/settings.py` DATABASES |
| 模板渲染 | 开发环境关闭模板缓存（即改即生效）；上生产改回默认缓存即可 | `hotel/settings.py` TEMPLATES |

> **Redis 配置（当前代码口径）**：项目使用 Django 内置
> `django.core.cache.backends.redis.RedisCache`，不需要 `django-redis`。
> `requirements.txt` 已声明 `redis>=4.3,<6`。开发环境默认 LocMemCache；
> 生产环境必须配置 `DJANGO_CACHE_BACKEND=redis` 和
> `DJANGO_REDIS_URL=redis://<Redis主机>:6379/1`，否则 Django 启动配置校验失败。
> `.env.example` 提供本地示例值；生产密码/凭据应由部署环境注入，不应提交到仓库。
>
> 查重与摘要缓存均为非权威数据。Redis 短暂不可用时，查重直接回退数据库；
> 缓存失效/写入失败只记录日志，不得把已经完成的数据库业务操作伪装成 500。

### 安全

| 项 | 实现 | 位置 |
|---|---|---|
| 用户身份验证 | 前台全部管理页面要求登录（`@staff_member_required`），未登录自动跳转 `/admin/login/`，登录后跳回原页面 | `hotelapp/views.py`、`hotel/settings.py` LOGIN_URL |
| 权限管理（RBAC） | 基于 Django 内置权限体系：只有 `is_staff`（后台员工）账号可进入前台；进一步细分可在后台「认证和授权 → 组」里按模块建组配权限，代码层用 `user.has_perm('hotelapp.xxx')` 扩展 | Django auth |
| 日志记录与审计 | ① 操作审计：所有 POST 写操作与删除请求记录「时间 / 方法 / 路径 / 结果状态 / 操作人」到 `logs/audit.log`（1MB 轮转，保留 5 份）；② 请求错误日志 `logs/request.log`；③ 后台 CRUD 自带 django_admin_log 审计 | `hotelapp/audit_middleware.py`、`hotel/settings.py` LOGGING |
| 输入安全 | 全字段白名单正则校验、查询字段白名单、CSRF 保护、模板自动转义 | `hotelapp/views.py` |

## 一点五、MySQL 不可变审计/账务事实保护（生产必做）

MySQL 触发器是数据库层保护，不是 Django 模型约定，也不是 Migration 自动安装。
当前仓库提供两个管理命令：

- `python manage.py install_mysql_append_only_guards`：在当前连接的 MySQL 数据库安装 6 条只追加保护触发器，以及 1 条现金流水必须属于本人 OPEN 收银班次的 INSERT 保护触发器。
- `python manage.py check_mysql_append_only_guards`：读取当前数据库的 `information_schema.TRIGGERS`，核对触发器名称、表、事件、时机和关键 SQL 定义。

**每个环境的首次部署、数据库重建或迁移到新 MySQL 实例后，都必须显式执行并留存结果：**

```bash
python manage.py migrate --noinput
python manage.py install_mysql_append_only_guards
python manage.py check_mysql_append_only_guards
```

注意事项：

1. 以上命令必须连接目标环境实际使用的 MySQL 数据库；SQLite 检查不能替代生产验收。
2. 安装账号必须拥有目标 schema 的 TRIGGER 权限；应用运行账号不应因此获得任意 DDL 权限。
3. 安装命令会按固定名称重建本项目管理的触发器；应在受控部署窗口执行。
4. CI 的 MySQL 工作流会在测试数据库迁移后安装并校验触发器，但 CI 通过不代表开发、预发布或生产数据库已安装。
5. 发布验收必须保存 `check_mysql_append_only_guards` 的成功输出，并在目标库再次执行校验；不能仅凭代码文件或 CI 记录认定目标环境已受保护。

## 二、生产部署升级路线（按需实施）

### 负载均衡（Nginx）

本项目为单进程 Django 开发服务器，上生产时建议 `gunicorn`/`waitress`（Windows 用 waitress）+ Nginx：

```nginx
# nginx_hotel.conf —— Nginx 反向代理 + 静态文件
upstream hotel_app {
    server 127.0.0.1:8001;
    server 127.0.0.1:8002;   # 多实例即实现负载均衡
}
server {
    listen 80;
    location /static/ {
        alias /path/to/django-hotel/static/;
    }
    location / {
        proxy_pass http://hotel_app;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### 异步处理（Celery）

项目暂无邮件发送、报表生成类任务。后续加入时：

1. `pip install celery redis`，新增 `hotel/celery.py` 配置 broker 为 Redis；
2. 耗时任务写成 `@shared_task`，视图里 `task.delay()` 异步执行；
3. Windows 本地调试可用 `celery -A hotel worker -P solo`。

### 多因素登录 / 第三方登录

当前为用户名密码登录（Django 内置哈希存储）。接入验证码/第三方登录建议使用
`django-allauth` 或 `django-otp`，在 `/admin/login/` 的登录流程上叠加。

## 三、定时任务（基线 2.0 §4.2 自动调度：Windows Task Scheduler / cron）

现有维护类管理命令共 8 个（本节只做文档化，不新增任务队列依赖）。
所有命令支持重复执行（幂等），可安全挂调度；失败重试=按原计划再次执行。

| 命令 | 作用 | 建议频率 | 幂等性 |
|---|---|---|---|
| `python manage.py expire_reservation_holds` | 释放超 15 分钟未支付预订的库存 hold（PENDING + expires_at 已过） | 每 5 分钟 | 是（仅处理过期 PENDING） |
| `python manage.py sync_inventory_calendar --property-code DEFAULT --room-type-code XXX --start-date YYYY-MM-DD --end-date YYYY-MM-DD` | 按物理房间主数据初始化/同步房型日库存 | 每日凌晨 + 房型变更后手动 | 是（按日期区间重算） |
| `python manage.py expire_invitations` | 员工邀请超期扫描（PENDING → EXPIRED，AUTH-20） | 每 15 分钟 | 是 |
| `python manage.py bind_staff_user ...` | 员工账号绑定修复（按需手动执行） | 按需 | 按入参 |
| `python manage.py platform_superadmin_check ...` | 平台超管账号核查（按需手动执行） | 按需 | 按入参 |
| `python manage.py run_billing_maintenance` | 支付意图到期过期、卡住的渠道成功回调重试、账务对账差异转补偿任务 | 每 10 分钟 | 是（意图只处理到期项；任务按稳定幂等键去重） |
| `python manage.py payment_compensation_worker` | 消费持久化补偿任务（渠道查询/回调确认/账本修复/Hold 协调/对账差异） | 每 5 分钟 | 是（数据库租约 + 退避 + 死信） |
| `python manage.py recover_stale_night_audits` | 把超时残留的夜审 RUNNING 标记 FAILED，恢复该营业日可重跑 | 每 15 分钟 | 是（仅处理超阈值记录） |

> 调度闭环：`run_billing_maintenance` 是补偿任务的**创建方**，
> `payment_compensation_worker` 是**消费方**。此前两者都未列入调度清单，
> 导致意图过期、账务对账扫描与四类补偿执行器在生产中完全不可达。

### 夜审（Night Audit）调度口径

- 夜审执行入口是 `POST /api/v1/night-audit/run`（Session 认证 +
  `hotelapp.night_audit_execute` 权限 + CSRF），**不存在也不允许匿名/公开 URL
  触发**（§4.2：任务执行必须校验物业、当前营业日、幂等键和系统身份）。
- 首期由值班经理在「营业日与夜审中心」`/manage/night-audit/` 人工执行
  （预览 → 确认 → 执行两步）；`NightAuditService.run` 自带并发锁、同营业日
  幂等重放与失败记录（NightAuditRun.FAILED + error_message），重复触发安全。
- 自动夜审触发属二期（需系统身份凭证设计），本期不新增管理命令。

### cron（Linux 部署）示例

```cron
# crontab -e（应用目录 /srv/django-hotel，虚拟环境 .venv）
*/5 * * * * cd /srv/django-hotel && .venv/bin/python manage.py expire_reservation_holds >> logs/scheduler.log 2>&1
*/15 * * * * cd /srv/django-hotel && .venv/bin/python manage.py expire_invitations >> logs/scheduler.log 2>&1
30 2 * * * cd /srv/django-hotel && .venv/bin/python manage.py sync_inventory_calendar --property-code DEFAULT --room-type-code STD --start-date $(date +\%F) --end-date $(date -d "+30 days" +\%F) >> logs/scheduler.log 2>&1
*/10 * * * * cd /srv/django-hotel && .venv/bin/python manage.py run_billing_maintenance >> logs/scheduler.log 2>&1
*/5 * * * * cd /srv/django-hotel && .venv/bin/python manage.py payment_compensation_worker >> logs/scheduler.log 2>&1
*/15 * * * * cd /srv/django-hotel && .venv/bin/python manage.py recover_stale_night_audits >> logs/scheduler.log 2>&1
```

### Windows Task Scheduler（开发/单机部署）示例

```bat
:: taskschd.msc 创建基本任务，或管理员命令行：
schtasks /Create /TN "PMS2 expire_reservation_holds" /SC MINUTE /MO 5 ^
  /TR "cmd /c cd /d F:\django-hotel && .venv\Scripts\python.exe manage.py expire_reservation_holds >> logs\scheduler.log 2>&1"
schtasks /Create /TN "PMS2 expire_invitations" /SC MINUTE /MO 15 ^
  /TR "cmd /c cd /d F:\django-hotel && .venv\Scripts\python.exe manage.py expire_invitations >> logs\scheduler.log 2>&1"
schtasks /Create /TN "PMS2 billing_maintenance" /SC MINUTE /MO 10 ^
  /TR "cmd /c cd /d F:\django-hotel && .venv\Scripts\python.exe manage.py run_billing_maintenance >> logs\scheduler.log 2>&1"
schtasks /Create /TN "PMS2 payment_compensation_worker" /SC MINUTE /MO 5 ^
  /TR "cmd /c cd /d F:\django-hotel && .venv\Scripts\python.exe manage.py payment_compensation_worker >> logs\scheduler.log 2>&1"
schtasks /Create /TN "PMS2 recover_stale_night_audits" /SC MINUTE /MO 15 ^
  /TR "cmd /c cd /d F:\django-hotel && .venv\Scripts\python.exe manage.py recover_stale_night_audits >> logs\scheduler.log 2>&1"
```

> 注意：命令重复执行安全（幂等）；并发场景由 `select_for_update` 串行化。
> 营业日切换时间由 `Property.timezone`（IANA 时区）驱动营业日本地日期，
> 调度器的服务器自然日不得直接当作营业日。
