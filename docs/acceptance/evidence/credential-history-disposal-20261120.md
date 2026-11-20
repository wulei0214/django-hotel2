# 凭据 Git 历史处置结论（2026-11-20）

> 第二轮任务书 TASK-01 验收标准"Git 历史风险已有明确处置结论"的收口补件；
> 补 V12-06《凭据扫描范围增强报告》§七"本轮未执行历史扫描、不宣称历史对象干净"的遗留开口。
> 裁定：**不改写 Git 历史**（负责人 2026-11-20 确认）。

## 一、取证：按需历史扫描（2026-11-20）

命令：`python scripts/scan_secrets.py --history`（对 `git log -p` 全历史提交内容扫描）。

| 类别 | 命中 | 说明 |
|---|---|---|
| known-redacted-literal | 3 | 已轮换失效的测试口令字面量，规则集见 `scripts/scan_secrets.py` KNOWN_REDACTED（不在本文档复述字面量） |
| django-secret-key | 7 | 两个字面量：真实形态密钥（项目基线提交 45e0fb5 引入，随源码演进出库）与 `dev-only-change-me` 占位符 |

## 二、失效性核实（本会话独立复核）

- 测试口令：42dcc8e 经 EmployeeLifecycleService.reset_password 统一轮换 10 账号（审计留痕）；2026-11-20 独立验证 MySQL `hotel` 库 9 个 `test_*` 账号 `check_password`（新口令）=True、（旧口令）=False。新口令唯一载体 `scripts/r6_env.txt`（gitignored，不入仓）。
- 历史 SECRET_KEY：`hotel/settings.py` 现为 `os.environ.get("DJANGO_SECRET_KEY", "")` 纯环境注入，无字面量回退；当前活跃密钥（44 位）与历史字面量比对为不同值——历史密钥已废弃。

## 三、处置结论与理由

**不改写 Git 历史。**

1. 历史命中均为已失效/已废弃凭据：测试口令已轮换失效并纳入阻断规则防止复用；历史密钥已被环境注入替代且与活跃密钥不同。
2. 仓库为私有仓库，无对外暴露面。
3. 改写历史将使 main 全部 SHA 引用链失效（R6-10 §零 定义的四类 SHA 关系、实施总表与证据均以 SHA 引用），并破坏多会话并行基线，风险显著大于收益。
4. 持续防线：`scripts/scan_secrets.py`（工作树 + 按需历史模式）已接入 CI，新增字面量入库即阻断。

## 四、同期负责人裁定固化（2026-11-20）

1. MySQL `@localhost` 孪生账户持旧口令：无 root 暂无法轮换，**保持登记**（V12-02《MySQL 孪生账户安全复核报告》），由 V1.3 收口轮处理；DB_HOST 已切 127.0.0.1，攻击面限本机。
2. SQLite 休眠走查环境（`hotel_acceptance.sqlite3`，2026-11-14 快照，10 账号新旧统一口令均不匹配）：裁定**废弃**；本机启动脚本 `start_acceptance.ps1` 已改为废弃守卫（gitignored 文件），验收走查统一 MySQL 环境（.env + waitress）。
