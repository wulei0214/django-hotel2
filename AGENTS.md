# AGENTS.md — AI 协作行为规则

> 架构与工程约束的唯一权威是 [ARCHITECTURE.md](ARCHITECTURE.md)（架构宪法）。本文件只补充宪法不覆盖的协作行为规则，两者冲突时以宪法为准；修改宪法属于 L3 架构变更，修改本文件不属于。

## 界面文案（前端，强制）

**Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.**
避免不必要的辅助标签与说明，让布局本身发挥解释作用。

- 禁止给开关/按钮配"开启后系统将…""点击后…""无需手动操作"式的复读说明；控件自身文案（开关标签、按钮动词）必须自解释。
- 提示文字只允许两类：①**不可逆或高危动作**的后果（删除、红字冲销、营业日关账、净房放行）；②**业务规则确实无法从界面推断**的约束（如"押金固定 30%"）。
- 图标、布局、交互反馈优先于文字说明；同一页面内不得出现同义重复提示。
- 新增页面以 `templates/pms/` 现有业务中心为版式基准，不引入新的提示样式；改存量页面时顺手删除冗余小字，不做专项清理提交。

## 会话纪律（代码无法推断的约定）

- Django 测试必须 `DJANGO_ENV=test`；`.env` 不被 settings 自动加载，连 dev MySQL 前 `set -a && source .env && set +a`。
- **取证脚本不得覆盖调用方显式指定的环境变量**：读 `.env` 用 `setdefault` 语义（禁止 `os.environ[k] = v`），否则隔离库名会被 `.env` 里的开发库名悄悄替换；取证服务启动后必须打印实际所连库名并人工核对。**在开发库上跑验收脚本会污染开发数据**（2026-11-26 曾发生：E2E 业务写入落到开发库，需按时间窗口 + 外键闭包清理业务残留，审计事件须保留）。
- **Windows 控制台默认 GBK**：任何以 stdout 镜像写 UTF-8 证据文件的脚本（`scripts/e2e_full_chain.py` 等）都必须固定 `PYTHONIOENCODING=utf-8`，否则遇到 `✓`(U+2713) 等字符会抛 `UnicodeEncodeError` 中断。
- **本机 MySQL `max_connections=20`**：同时只保留一个取证服务，并设 `DJANGO_DB_CONN_MAX_AGE=0`，否则出现 `1040 Too many connections`。
- **本机 MySQL 账号无 SUPER 且开启 binary log**：建触发器报 1419，append-only 触发器行为级验证必须在具备权限的独立 MySQL 上做，不得以「CI 只校验定义存在」冒充行为级通过。
- 涉及锁/约束/并发的交付以 MySQL 结果为准（SQLite 下 `select_for_update` 为 no-op），只跑 SQLite 全绿不算数。
- MySQL 全量回归纪律（负责人 V1.1 §3.3 正式裁定）：同一共享测试环境同一时刻只允许一个 MySQL 全量测试进程执行建库/迁移/销毁，全量测试串行；MySQL 触碰域组合测试与全量错峰；专项并发测试按设计执行但不得争抢同一测试库；SQLite 可并行但不替代 MySQL 权威。不追加 `test_%` 授权（hotelhotel 仅授 `hotel`/`test_hotel`，独立库名 1044 实证）；不得通过删减测试、降低断言或仅跑 SQLite 绕过 MySQL 失败。每次 MySQL 全量须登记执行人/起止时间/测试库名/命令/退出码/报告路径；出现 1213 须区分建库、迁移、夹具准备、真实业务事务四阶段，不笼统归因。新派发模板必须包含本条错峰要求。
- 任何源码不得出现可用凭据字面量，测试密码一律 `get_random_string` 模块级常量（字典 `"password":`、`check_password(...)`、断言中的值与创建处必须同步替换）。
- 同一文件的多次编辑串行执行，批量编辑后 grep 校验落盘。
- 财务/审计事实引用与基线核对：现取现核（`git show <SHA>:<path>`），不凭会话早期读取版本下结论。

## 交付前检查（每轮交付必过）

- 新增测试必须有明确业务目的；不新增重复测试、不保留失效夹具（先确认引用再删）。
- 业务功能退役时同步迁移或退役对应测试与 CI 引用，不留双入口测试体系。
- 临时验证脚本与冒烟库在交付前删除或转为正式工具（`scripts/` 或 `apps/*/management/commands/`）；一次性中间产物放 `_wbi_*` 或 Temp 并在交付前清理。
- 正式验收证据先归档 `docs/acceptance/evidence/` 再入库；`reports/`、`logs/` 只允许 gitignored 的本机运行产物。
- 交付涉及 docs/ 或根目录 Markdown 时，推送前本地运行 `python scripts/docs_link_check.py check`；基线 `docs/共享规范/docs_gate_baseline.json` 只拦新增断链，更新须与修复同提交，禁止清空基线伪造通过。
