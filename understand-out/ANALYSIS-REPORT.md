# django-hotel 知识图谱分析报告

- 仓库：`F:\Desktop\django-hotel`（Django 4.1 酒店 PMS，中文代码库）
- 分析提交：`788d486fdfae050b8084ddd3e63355f788f5349b`
- 分析时间：2026-11-10
- 工具：Egonex-AI / Understand-Anything v2.9.8（`/understand` 技能流程）

## 1. 规模统计

| 项 | 数量 |
| --- | --- |
| 仓库受 Git 跟踪文件 | 582 |
| 本次分析文件 | 214（其中代码 167、模板 35、配置 3、文档 5、CI 3、数据 1） |
| 因 `--exclude` 跳过 | 298 |
| 节点总数 | 298 |
| 边总数 | 722 |
| 架构分层 | 13 |
| 导览步骤 | 9 |
| Louvain 社区数 | 12（模块度 0.513） |

节点类型：`file` 202、`class` 84、`pipeline` 3、`config` 3、`document` 5、`table` 1。

边类型：`imports` 425、`depends_on` 91、`contains` 84、`exports` 84、`tested_by` 16、`related` 10、`triggers` 4、`documents` 4、`configures` 3、`migrates` 1。

校验结果：**issues 0 / warnings 0 / 自环边 0 / 孤立节点 0**。

## 2. 架构分层

| 层 | 节点数 | 说明 |
| --- | --- | --- |
| HTTP API 与路由 | 19 | 路由注册、五大中心 API、公开预订与支付接口、OpenAPI 契约 |
| 身份与权限 | 24 | 员工生命周期、九角色权限注册表、会话/Cookie 隔离、邀请入职 |
| 账务与财务 | 15 | 不可变财务事实、审批编排、夜审、对账 |
| 预订与前台 | 11 | 预订创建/取消、排房/入住/续住/换房/离店、散客入住 |
| 库存与客房 | 9 | 房量库存、房态流转、维修单、库存对账 |
| 客人域 | 6 | 客人主档、PII 脱敏与按原因解密、会员会籍 |
| 核心基础能力 | 13 | 物业/审计/营业日/幂等/限流/通知、API 契约工具 |
| 旧版单体业务 | 18 | `hotelapp` 旧版视图与模型 |
| 前端模板 | 35 | PMS2 五大中心页、平台页、公开预订页、旧版页面 |
| 测试与端到端 | 49 | Django 测试、契约测试、E2E 脚本 |
| 文档与交付配置 | 5 | README、部署说明、依赖清单、PR 模板 |
| CI/CD 自动化 | 3 | GitHub Actions 工作流 |
| 项目配置与入口 | 7 | settings、根路由、WSGI、manage.py、环境变量样例 |

## 3. 关键枢纽（入度最高）

| 节点 | 入度 |
| --- | --- |
| `apps/core/models.py` | 77 |
| `apps/reservations/models.py` | 42 |
| `apps/billing/models.py` | 40 |
| `apps/identity/models.py` | 37 |
| `apps/inventory/models.py` | 30 |
| `templates/_sidebar.html` | 24 |
| `apps/guests/models.py` | 23 |
| `apps/identity/authorization.py` | 22 |

结论：领域模型文件是全局耦合中心（各服务层普遍 `imports` 到 models），`_sidebar.html` 被 20+ 模板依赖；权限判定集中在 `authorization.py`。

## 4. 产物清单

| 文件 | 大小 |
| --- | --- |
| `understand-out\.ua\knowledge-graph.json` | 269,802 B（263.5 KB） |
| `understand-out\.ua\fingerprints.json` | 511,894 B（499.9 KB） |
| `understand-out\.ua\meta.json` | 159 B |
| `understand-out\.ua\config.json` | 65 B |
| `understand-out\.ua\.understandignore` | 1,528 B |
| `understand-out\.ua\intermediate\scan-result.json` | 54,591 B |
| `understand-out\.ua\.trash-20261110192009\` | 64 个文件 / 4.53 MB（中间产物与脚本归档） |
| `understand-out\review-summary.json` | 549 B |

## 5. 查看方式

开发服务器（当前已启动）：

```
http://127.0.0.1:5178/?token=9f3c1a7d2b8e4c6a90f1d3e5b7a9c2f4
```

如需重启：

```powershell
cd "C:\Users\wu'lei\.understand-anything-plugin\packages\dashboard"
$env:GRAPH_DIR="F:\Desktop\django-hotel"
$env:UNDERSTAND_ACCESS_TOKEN="9f3c1a7d2b8e4c6a90f1d3e5b7a9c2f4"
node node_modules\vite\bin\vite.js --host 127.0.0.1 --port 5178
```

## 6. 本次分析的取舍与偏差（如实记录）

1. 当前会话无子代理调度权限，`project-scanner` / `file-analyzer` / `architecture-analyzer` / `tour-builder` 均未作为子代理派发，各阶段由主代理内联完成；语义摘要由模型在确定性结构提取结果之上撰写，未逐函数生成摘要。
2. 符号级节点仅覆盖 84 个核心领域/服务类，未生成 function 节点。
3. 排除了 `docs/**`、`static/**`、`apps/*/migrations/**`、`hotelapp/migrations/**`、`.archify/**`、`.deepseek-harness/**`、`understand-out/**`。
4. 工具硬编码数据目录为 `<项目根>/.ua`，因此在仓库根建立了目录联接 `F:\Desktop\django-hotel\.ua` → `understand-out\.ua`，实际文件全部落在 `understand-out`。
5. 未使用 `--review`（LLM 复审），走默认确定性校验。
