# API 契约与测试规范

> 依据：ARCHITECTURE.md §2.3（API 事实边界）/ §8（API 契约要素）/ §10（测试门禁）；治理 V1.1 §3.5/§5；第一阶段报告 §3.2/§7。
> 效力：专项规范，细化宪法而不冲突。API 权威契约 = `apps/api/openapi_schema.py` 运行时导出（/api/v1/openapi.json），`docs/接口文档/openapi.json` 为同步兼容副本。

## 1. API 现行事实（第一阶段核实口径，AI 开发前必读）

- API 实现为**普通 Django 函数视图 + `api_guard` 装饰器 + 领域异常**；全仓 0 处 DRF import。
- 响应为**各端点自定义 JSON**，以 OpenAPI 契约和契约测试为准；**禁止**以"统一格式"为名重写现有响应结构（宪法 §8）。
- 权威契约 = `docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json`（由 export_openapi_schema 命令从 `apps/api/openapi_schema.py` 导出）+ `apps/api/test_openapi_schema.py` 契约测试；`docs/接口文档/openapi.json` 为受控镜像（逐字节一致性守卫）。规模现值以《共享规范/PMS当前状态总览》§2 为准（2026-11-14：116 paths / 118 operations，93 verified / 25 partial；本节原 113/115 为 2026-11-12 时点值）。

## 2. 新增/变更端点流程

1. **契约先行**：先更新 `apps/api/openapi_schema.py`（路径/方法/参数/响应结构/错误码/幂等/限流标注），补 `test_openapi_schema.py` 契约断言；
2. **测试先行**：先写目标端点测试（含权限拒绝/幂等重放/非法输入），确认失败再实现；
3. **实现**：函数视图 + `api_guard`（认证/物业范围/输入校验/调用 Service），业务规则进对应领域 Service；
4. **双库**：涉数据库差异/锁/约束/并发的端点，SQLite+MySQL 都要验证；
5. **同步副本**：影响契约的提交同步 `docs/接口文档/openapi.json`（openapi-snapshot workflow 也会自动回写，注意核对）；
6. **变更记录**：docs/PMS2.0_当前版本/04_开发实施记录 登记。

## 3. 契约要素清单（每个端点必须可回答，宪法 §8 细化）

URL 与方法；请求字段（名/类型/可空/默认/校验）；响应字段（同）；成功/失败状态码与错误码；认证授权（端点门禁权限码）；物业数据范围；敏感字段策略（脱敏/揭示）；分页排序；幂等（键/指纹/重放行为）；并发冲突行为；事务与审计；OpenAPI 状态（verified/partial）；自动化测试；调用方清单。

**现行错误码口径（新增端点必须沿用既有语义，不得另造第二套）**：
- 401 未认证（JSON，AUTHENTICATION_REQUIRED 族）；403 权限拒绝（JSON 或 PermissionDenied 页面）；404 资源不存在/不泄露存在性（如错 token 查询）；409 冲突（幂等键异内容、PLANNED/RETIRED 模块码）；410 旧写入口永久退役（P1-05 族）；425 幂等请求处理中（PROCESSING）；429 限流（fail-closed，RateLimitCounter）；503 物业上下文不可用。
- 领域异常 → HTTP 映射由 api_guard 与服务层既有约定承接；新异常类型属 L2 变更（公共适配层）。

## 4. 测试规范（宪法 §10 细化）

### 4.1 分层与运行入口

| 层 | 位置 | 运行 | 说明 |
| --- | --- | --- | --- |
| Django 单元/服务/API | `apps/<domain>/test_*.py`、`tests.py` | `manage.py test`（SQLite :memory:；CI django-ci 同款环境） | 1201 条（2026-11-13 实测全绿） |
| P0 真实 HTTP 冒烟 | `apps/api/tests_p0/` | `pytest apps/api/tests_p0 -m p0`（需已启动且种子化实例；PMS_BASE_URL/员工账号用环境变量注入） | 9 条；裸跑 `pytest` 默认排除（addopts） |
| pytest 默认收集 | 仓库根 `pytest` | 1124 collected（9 p0 deselected） | 配置在 pyproject [tool.pytest.ini_options] |

### 4.2 用例要求

- 测试函数 `test_snake_case`；文件 `test_*.py`（宪法 §9）；
- 测试先行：先定义用例并确认失败，再实现（V1.1 §5）；
- 业务断言必须验证**最终数据库状态**，不得只断言 HTTP 200；
- 高风险流程（财务/预订/库存并发）覆盖：事务回滚、重复请求（幂等）、并发、不可变约束（V1.1 §5）；
- 涉数据库差异/锁/约束/并发的变更：SQLite + MySQL 双库验证（本地 SQLite + CI ci.yml MySQL，或本地双库）；
- 密码类测试数据用运行时生成（`get_random_string` 模块级常量），不落字面量；
- 不删除测试、弱化断言、跳过关键用例、滥用 ignore 关闭规则制造绿色结果（宪法 §10）。

### 4.3 测试报告格式（随交付）

```
命令: <完整命令>
环境: <SHA / Python / DB 引擎>
结果: <退出状态>，Ran N tests，通过/失败/跳过 = x/y/z
未覆盖: <明确列出>
```

## 5. 兼容策略

- 破坏性契约变更：调用方清单 + 兼容期 + 迁移计划（宪法 §8）；旧入口退役走审批（宪法 §3.1/§4"历史兼容"行）；
- 响应字段新增必须向后兼容（新增可选字段）；删除/改名字段走兼容期；
- 限流/幂等行为变更属契约变更（消费方包括前端 inline JS 与外部调用方）。

## 6. 关联文档

- 环境/运行/提交模板：docs/共享规范/开发指南_2026-11-10.md
- 代码事实全景：docs/PMS2.0_当前版本/02_技术架构与数据模型/PMS2技术功能说明文档_2026-11-12.md
- 派活/审批：《AI派活与架构变更审批规范》；审查/门禁：《代码审查与质量门禁规范》
