# ARCHITECTURE.md — PMS 架构契约（所有 AI 与人类开发者必须遵守）

> 本文件是**唯一权威契约**。任何 AI 派活前必须引用本文件。修改本文件需经代码合并者评审。

## 1. 分层架构（强制）

请求处理流（自上而下，单向依赖）：

```
URL (hotel/urls.py)
  → View (apps/*/views.py 或 apps/api/views.py)   [薄层：仅参数校验 + 调用 service]
    → Service (apps/*/services.py)                 [业务编排、事务边界]
      → Selector (apps/*/selectors.py)             [只读查询，可缓存]
        → Model (apps/*/models.py)                 [数据 + 领域行为]
```

**红线：**
- ❌ View 内禁止写事务、复杂查询、跨聚合逻辑（实测 hotelapp/views.py 的 Fat Views 是反面教材）
- ❌ 禁止在 View/Service 直接裸写 `raw()` SQL，除非经合并者批准并附注释
- ✅ 写操作统一在 Service 内用 `@transaction.atomic` 或显式事务

## 2. API 风格（强制 DRF）

- 所有对外 HTTP API 使用 **Django REST Framework**
- 序列化器放 `apps/api/serializers.py`，视图放 `apps/api/views.py`，路由放 `apps/api/urls.py`
- **统一响应格式**（禁止裸 `Response({"ok":True})`）：

```python
# apps/api/responses.py
from rest_framework.response import Response

def ok(data=None, message="success", code=0):
    return Response({"code": code, "message": message, "data": data})

def fail(message="error", code=1, data=None, status=400):
    return Response({"code": code, "message": message, "data": data}, status=status)
```

- **统一错误处理**：在 `hotel/settings.py` 配置 `EXCEPTION_HANDLER` 指向自定义 handler，捕获 `ValidationError`/业务异常，统一转成上述格式。

## 3. 模块边界（apps/ 是唯一的业务入口）

```
apps/
├── core/          # 基础抽象、审计字段、限流
├── identity/      # 认证、RBAC 权限、会话隔离
├── guests/        # 客人域（PII 加密治理）
├── inventory/     # 房态、库存
├── reservations/  # 预订
├── billing/       # 账务（夜审/退款/对账）
└── api/           # DRF 层（对外暴露）
```

**规则：**
- 新功能一律进 `apps/<domain>/`，禁止在根目录或 `hotelapp/` 新建业务模块
- 跨域交互只通过 **service 接口 / 序列化器**，不共享内部模型细节
- `hotelapp/` 进入**冻结态**：只修 bug、不扩功能，逐步抽离到 `apps/`

## 4. 依赖与运行

- 运行时依赖：`requirements.txt`（含 `djangorestframework`，原文件缺失，已补）
- 开发依赖：`requirements-dev.txt`（ruff/black/isort/mypy/pytest/pre-commit）
- 虚拟环境：`.venv/`（唯一），禁止提交 `venv/`

## 5. 质量门禁（提交前必过）

```bash
ruff check apps/ hotel/          # lint
black --check apps/ hotel/       # 格式
isort --check apps/ hotel/       # import 排序
mypy apps/                       # 类型
pytest                           # 测试
```

上述步骤由 `.pre-commit-config.yaml` 自动执行，CI 复用同一套。

## 6. 测试约定

- 单元测试：`apps/<domain>/test_*.py`
- 跨模块 E2E/冒烟：`tests/`（由 `api_tests/` 迁移而来）
- 没有测试的 PR 一律打回
