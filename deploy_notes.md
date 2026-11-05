# 性能优化与安全保障 — 实施说明与升级路线

本文档说明项目中已落地的部分，以及生产环境（Redis / Nginx / 异步任务）的升级路径。

## 一、已落地

### 性能

| 项 | 实现 | 位置 |
|---|---|---|
| 缓存策略 | Django 缓存框架（LocMemCache），身份证/手机号/菜名/车牌查重结果缓存 60 秒，减少重复查库 | `hotel/settings.py` CACHES、`hotelapp/views.py` check_unique |
| 数据库长连接 | `CONN_MAX_AGE = 60`，避免每个请求重建 MySQL 连接 | `hotel/settings.py` DATABASES |
| 模板渲染 | 开发环境关闭模板缓存（即改即生效）；上生产改回默认缓存即可 | `hotel/settings.py` TEMPLATES |

> **升级到 Redis**：`pip install django-redis` 并安装 Redis 服务后，把 CACHES 的
> `BACKEND` 改为 `django_redis.cache.RedisCache`、`LOCATION` 改为
> `redis://127.0.0.1:6379/1` 即可，业务代码零改动（已按 Django 缓存 API 编写）。

### 安全

| 项 | 实现 | 位置 |
|---|---|---|
| 用户身份验证 | 前台全部管理页面要求登录（`@staff_member_required`），未登录自动跳转 `/admin/login/`，登录后跳回原页面 | `hotelapp/views.py`、`hotel/settings.py` LOGIN_URL |
| 权限管理（RBAC） | 基于 Django 内置权限体系：只有 `is_staff`（后台员工）账号可进入前台；进一步细分可在后台「认证和授权 → 组」里按模块建组配权限，代码层用 `user.has_perm('hotelapp.xxx')` 扩展 | Django auth |
| 日志记录与审计 | ① 操作审计：所有 POST 写操作与删除请求记录「时间 / 方法 / 路径 / 结果状态 / 操作人」到 `logs/audit.log`（1MB 轮转，保留 5 份）；② 请求错误日志 `logs/request.log`；③ 后台 CRUD 自带 django_admin_log 审计 | `hotelapp/audit_middleware.py`、`hotel/settings.py` LOGGING |
| 输入安全 | 全字段白名单正则校验、查询字段白名单、CSRF 保护、模板自动转义 | `hotelapp/views.py` |

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

现有维护类管理命令共 5 个（本节只做文档化，不新增任务队列依赖）。
所有命令支持重复执行（幂等），可安全挂调度；失败重试=按原计划再次执行。

| 命令 | 作用 | 建议频率 | 幂等性 |
|---|---|---|---|
| `python manage.py expire_reservation_holds` | 释放超 15 分钟未支付预订的库存 hold（PENDING + expires_at 已过） | 每 5 分钟 | 是（仅处理过期 PENDING） |
| `python manage.py sync_inventory_calendar --property-code DEFAULT --room-type-code XXX --start-date YYYY-MM-DD --end-date YYYY-MM-DD` | 按物理房间主数据初始化/同步房型日库存 | 每日凌晨 + 房型变更后手动 | 是（按日期区间重算） |
| `python manage.py expire_invitations` | 员工邀请超期扫描（PENDING → EXPIRED，AUTH-20） | 每 15 分钟 | 是 |
| `python manage.py bind_staff_user ...` | 员工账号绑定修复（按需手动执行） | 按需 | 按入参 |
| `python manage.py platform_superadmin_check ...` | 平台超管账号核查（按需手动执行） | 按需 | 按入参 |

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
```

### Windows Task Scheduler（开发/单机部署）示例

```bat
:: taskschd.msc 创建基本任务，或管理员命令行：
schtasks /Create /TN "PMS2 expire_reservation_holds" /SC MINUTE /MO 5 ^
  /TR "cmd /c cd /d F:\django-hotel && .venv\Scripts\python.exe manage.py expire_reservation_holds >> logs\scheduler.log 2>&1"
schtasks /Create /TN "PMS2 expire_invitations" /SC MINUTE /MO 15 ^
  /TR "cmd /c cd /d F:\django-hotel && .venv\Scripts\python.exe manage.py expire_invitations >> logs\scheduler.log 2>&1"
```

> 注意：命令重复执行安全（幂等）；并发场景由 `select_for_update` 串行化。
> 营业日切换时间由 `Property.timezone`（IANA 时区）驱动营业日本地日期，
> 调度器的服务器自然日不得直接当作营业日。
