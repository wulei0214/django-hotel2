"""TASK-06 九岗位高危权限拒绝矩阵（浏览器会话内取证，可复跑）。

背景：V13-02 Phase 7 的九岗位拒绝矩阵在 `c4c15b6` 基线上得出「夜审员分组无
夜审权限」的结论（当时登记为 NA-13 缺口）。`1b882eb` 按负责人批准给夜审员组
授 `core.night_audit_view` + `hotelapp.night_audit_execute`，因此本脚本复跑
同一矩阵，验证两件事：

1. **授权生效**：夜审员能打开夜审中心页面，且 `POST /api/v1/night-audit/run`
   不再返回 403（用不存在的营业日触发业务校验，确保不产生真实关账）；
2. **无权限外溢**：其余 8 个岗位在原拒绝面上的判定不变（退款执行/审批、电话
   揭示、净房放行、财务视图、审计导出）。

判定口径：拒绝格必须精确 403（`PERMISSION_DENIED`），放行格只要求非 403
（资源不存在/业务校验错误都算通过权限门禁），因此不依赖具体业务数据 ID。
夜审中心页面另做两项目录级断言：放行岗位必须渲染「执行夜审」按钮，且**不得**
渲染「营业日重开申请」表单（0021 迁移未授 `business_date_reopen`）。

隔离库口径（2026-11-27 改名）：
  - 取证隔离库 = **`test_hotel_e2e`**（含 test、不等于 Django 的 MySQL 测试库名
    `test_hotel`、不被 `scripts/e2e_full_chain.py` 的守卫拒绝）；
  - **`test_hotel` 是 Django 的测试库名**，任何一次 `manage.py test` 的收尾阶段
    都会把它删掉，不得再用作取证隔离库；
  - 取证前先跑 `python scripts/acceptance/prepare_isolated_db.py` 建库并 migrate +
    seed（该脚本默认库名即 `test_hotel_e2e`），再起服务、最后跑本脚本。

用法（先由外部起好 dev server，默认 127.0.0.1:8011）：

    NINE_ROLE_BASE=http://127.0.0.1:8011 NINE_ROLE_RUN_ID=20261122-task06 \
        .venv\\Scripts\\python.exe scripts/acceptance/nine_role_matrix.py

或直接用仓库内的包装脚本（自动起停取证服务）：

    powershell -NoProfile -File scripts\\acceptance\\run_nine_role_evidence.ps1 \
        -RunId 20261122-task06 -Port 8011

账号清单（gitignored 的本机文件，缺失时本脚本明确报错而非抛裸异常）：
    .work/v12_accounts.json
    .work/v13/v13_accounts.json

证据：docs/acceptance/evidence/task06-nine-role-<RUN_ID>/
"""

import json
import os
import sys
from datetime import datetime

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)
with open(os.path.join(_ROOT, ".env"), encoding="utf-8") as _env:
    for _line in _env:
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _value = _line.split("=", 1)
            if _key.strip() not in (
                "DJANGO_PLATFORM_ADMIN_USERNAME",
                "DJANGO_PLATFORM_ADMIN_PASSWORD",
            ):
                os.environ.setdefault(_key.strip(), _value.strip().strip("'").strip('"'))
os.chdir(_ROOT)

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = os.environ.get("NINE_ROLE_BASE", "http://127.0.0.1:8011")
RUN_ID = os.environ.get("NINE_ROLE_RUN_ID") or datetime.now().strftime("%Y%m%d-%H%M%S")
EVIDENCE = os.path.join("docs", "acceptance", "evidence", f"task06-nine-role-{RUN_ID}")

ACCOUNTS = {}
ACCOUNT_MANIFESTS = (
    os.path.join(".work", "v12_accounts.json"),
    os.path.join(".work", "v13", "v13_accounts.json"),
)
for _path in ACCOUNT_MANIFESTS:
    if os.path.exists(_path):
        with open(_path, encoding="utf-8") as fh:
            ACCOUNTS.update(json.load(fh))
    else:
        print("[WARN] 账号清单缺失（gitignored 本机文件）：%s" % _path)

NINE = [
    "v12_gm",
    "v12_fd",
    "v12_fin",
    "v13_duty",
    "v13_hk",
    "v13_night",
    "v13_rest",
    "v13_chef",
    "v13_sec",
]
ROLE_NAMES = {
    "v12_gm": "店长",
    "v12_fd": "前台接待",
    "v12_fin": "财务",
    "v13_duty": "值班经理",
    "v13_hk": "客房服务员",
    "v13_night": "夜审员",
    "v13_rest": "餐饮服务员",
    "v13_chef": "餐饮主管",
    "v13_sec": "停车安保",
}
NIGHT_AUDIT_PAGE = "/manage/night-audit/"

# 拒绝面（expect="deny"）：精确 403；放行面（expect="allow"）：非 403 即可。
# 夜审执行对店长/值班经理为 hold（真实执行会关账推进营业日），沿用 Phase 7 的跳过口径。
EXPECT = {
    "S1_refund_execute": {
        "v12_gm": "deny",
        "v12_fd": "deny",
        "v12_fin": "allow",
        "v13_duty": "deny",
        "v13_hk": "deny",
        "v13_night": "deny",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
    "S2_refund_approve": {
        "v12_gm": "allow",
        "v12_fd": "deny",
        "v12_fin": "deny",
        "v13_duty": "allow",
        "v13_hk": "deny",
        "v13_night": "deny",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
    "S3_night_audit_run": {
        "v12_gm": "hold",
        "v12_fd": "deny",
        "v12_fin": "deny",
        "v13_duty": "hold",
        "v13_hk": "deny",
        "v13_night": "allow",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
    "S4_phone_reveal": {
        "v12_gm": "allow",
        "v12_fd": "allow",
        "v12_fin": "deny",
        "v13_duty": "allow",
        "v13_hk": "deny",
        "v13_night": "deny",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
    "S5_room_release": {
        "v12_gm": "allow",
        "v12_fd": "deny",
        "v12_fin": "deny",
        "v13_duty": "allow",
        "v13_hk": "deny",
        "v13_night": "deny",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
    "S6_finance_liabilities": {
        "v12_gm": "allow",
        "v12_fd": "deny",
        "v12_fin": "allow",
        "v13_duty": "allow",
        "v13_hk": "deny",
        "v13_night": "deny",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
    "S7_report_export_invalid": {
        user: "allow" for user in NINE  # 非法报表码在权限门禁之后/之前均返回 400，不得为 403
    },
    "S7b_report_export_occupancy": {
        "v12_fd": "allow",
        "v13_night": "deny",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
    "S8_audit_export": {
        "v12_gm": "allow",
        "v12_fd": "deny",
        "v12_fin": "deny",
        "v13_duty": "allow",
        "v13_hk": "deny",
        "v13_night": "deny",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
    "PAGE_night_audit": {
        "v12_gm": "allow",
        "v12_fd": "deny",
        "v12_fin": "deny",
        "v13_duty": "allow",
        "v13_hk": "deny",
        "v13_night": "allow",
        "v13_rest": "deny",
        "v13_chef": "deny",
        "v13_sec": "deny",
    },
}

# 夜审执行一律用不存在的营业日：即便权限通过也只会在业务校验处失败，不关账。
# 房态放行同样打向不存在的房间：持权者拿 404、无权者拿 403，绝不改动真实房态。
SCENARIOS = [
    (
        "S1_refund_execute",
        "POST",
        "/api/v1/billing/refunds/999999/execute",
        lambda u: {"idempotency_key": f"task06-refund-exec-{u}"},
    ),
    ("S2_refund_approve", "POST", "/api/v1/billing/refunds/999999/approve", lambda u: {}),
    ("S3_night_audit_run", "POST", "/api/v1/night-audit/run", lambda u: {"date": "1900-01-01"}),
    (
        "S4_phone_reveal",
        "POST",
        "/api/v1/guests/51/phones/47/reveal",
        lambda u: {"reason": "TASK-06 九岗位权限矩阵复跑：电话揭示授权面核对"},
    ),
    (
        "S5_room_release",
        "POST",
        "/api/v1/housekeeping/rooms/999999/status",
        lambda u: {"status": "CLEAN", "reason": "task06-matrix-evidence"},
    ),
    ("S6_finance_liabilities", "GET", "/api/v1/billing/refund-liabilities", lambda u: None),
    (
        "S7_report_export_invalid",
        "GET",
        "/api/v1/reports/export?report=__invalid__&reason=task06-matrix",
        lambda u: None,
    ),
    (
        "S7b_report_export_occupancy",
        "GET",
        "/api/v1/reports/export?report=occupancy&reason=task06-matrix",
        lambda u: None,
    ),
    ("S8_audit_export", "GET", "/api/v1/audit/export?reason=task06-matrix", lambda u: None),
]


def page_fetch(page, method, path, body=None, csrf=""):
    return page.evaluate(
        """async ([method, path, body, csrf]) => {
            const resp = await fetch(path, {
                method: method,
                headers: Object.assign(
                    {"Content-Type": "application/json"},
                    csrf ? {"X-CSRFToken": csrf} : {}
                ),
                credentials: "same-origin",
                body: body === null ? undefined : JSON.stringify(body),
            });
            let data = null;
            try { data = await resp.json(); } catch (e) {}
            return {status: resp.status, data: data};
        }""",
        [method, path, body, csrf],
    )


def obtain_csrf(page):
    """CSRF_USE_SESSIONS=True：token 只能取自页面渲染的隐藏 input（通知中心全员可达）。"""
    page.goto(f"{BASE}/manage/notifications/", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    loc = page.locator("#pms-csrf-token input[name='csrfmiddlewaretoken']")
    if loc.count() == 0:
        loc = page.locator("input[name='csrfmiddlewaretoken']")
    return loc.first.input_value() if loc.count() else ""


def login(context, username):
    page = context.new_page()
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{BASE}/login/", wait_until="domcontentloaded")
    page.wait_for_timeout(900)
    if page.locator("input[name=username]").count() == 0:
        head = page.evaluate("() => document.body.innerText.slice(0, 200)")
        raise AssertionError(f"登录页不可用（{username}）：{head}")
    page.fill("input[name=username]", username)
    page.fill("input[name=password]", ACCOUNTS[username]["password"])
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    if "/login" in page.url:
        raise AssertionError(f"登录失败：{username}")
    return page


def classify(expect, status):
    if status is None:
        return False
    if expect == "deny":
        return status == 403
    return status != 403


def main():
    missing = [user for user in NINE if user not in ACCOUNTS]
    if missing:
        print("[FAIL] 账号清单不完整，缺少：%s" % ", ".join(missing))
        print("[FAIL] 本矩阵需要下列 gitignored 本机账号清单文件：")
        for path in ACCOUNT_MANIFESTS:
            print("[FAIL]   %s" % path)
        print("[FAIL] 缺失清单时不做任何取证（不写证据、不启动浏览器）。")
        return 2
    os.makedirs(EVIDENCE, exist_ok=True)
    results = []
    failures = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for user in NINE:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = login(context, user)
                page.screenshot(path=os.path.join(EVIDENCE, f"landing-{user}.png"))
                csrf = obtain_csrf(page)
                entry = {
                    "account": user,
                    "role": ROLE_NAMES[user],
                    "csrf_obtained": bool(csrf),
                    "cells": [],
                }
                print(f"[task06] {user}({ROLE_NAMES[user]}) csrf={bool(csrf)}")

                for name, method, path, body_fn in SCENARIOS:
                    expect = EXPECT[name].get(user)
                    if expect is None:
                        entry["cells"].append(
                            {
                                "scenario": name,
                                "skipped": True,
                                "note": "本轮不比对该岗位（沿用 Phase 7 的矩阵覆盖口径）",
                            }
                        )
                        continue
                    if expect == "hold":
                        entry["cells"].append(
                            {
                                "scenario": name,
                                "skipped": True,
                                "note": "店长/值班经理为夜审执行权限持有者；真实执行会关账推进营业日，"
                                "正控在 Phase 6/7 已取证，此处不重复发起",
                            }
                        )
                        print(f"[task06] {user} {name} SKIP(holder)")
                        continue
                    resp = page_fetch(page, method, path, body_fn(user), csrf)
                    ok = classify(expect, resp["status"])
                    cell = {
                        "scenario": name,
                        "expect": expect,
                        "status": resp["status"],
                        "code": (resp["data"] or {}).get("code"),
                        "message": str((resp["data"] or {}).get("message", ""))[:120],
                        "ok": ok,
                    }
                    entry["cells"].append(cell)
                    if not ok:
                        failures.append(f"{user}/{name}: expect={expect} got={resp['status']}")
                    print(f"[task06] {user} {name} -> {resp['status']} ok={ok}")

                # 页面级：夜审中心门禁（NA-13 的直接验收点）
                page.goto(f"{BASE}{NIGHT_AUDIT_PAGE}", wait_until="domcontentloaded")
                page.wait_for_timeout(800)
                page_status = page.evaluate(
                    "() => document.body.innerText.slice(0, 120).replace(/\\s+/g, ' ')"
                )
                page_denied = "无权" in page_status or "403" in page_status
                ui = page.evaluate(
                    """() => ({
                        run_button: !!document.querySelector('#na-run-btn'),
                        preview_button: !!document.querySelector('#na-preview-btn'),
                        reopen_form: !!document.querySelector('#na-reopen-form'),
                        breakglass_panel: !!document.querySelector(
                            'section[aria-label="紧急重开记录"]'),
                    })"""
                )
                expect = EXPECT["PAGE_night_audit"][user]
                page_ok = (not page_denied) if expect == "allow" else page_denied
                # 越权外溢检查：夜审执行按钮必须有；夜审员**不得**出现营业日重开申请
                # 表单（0021 迁移只授 night_audit_view + night_audit_execute）。
                # 店长/值班经理本就持 business_date_reopen，其重开表单属正控，不做否定。
                ui_ok = True
                if expect == "allow":
                    ui_ok = ui["run_button"] and (user != "v13_night" or not ui["reopen_form"])
                entry["cells"].append(
                    {
                        "scenario": "PAGE_night_audit",
                        "expect": expect,
                        "denied_text": page_status,
                        "ui": ui,
                        "ok": page_ok and ui_ok,
                    }
                )
                if not page_ok:
                    failures.append(
                        f"{user}/PAGE_night_audit: expect={expect} denied={page_denied}"
                    )
                if not ui_ok:
                    failures.append(f"{user}/PAGE_night_audit UI: {ui}")
                page.screenshot(path=os.path.join(EVIDENCE, f"night-audit-{user}.png"))
                print(
                    f"[task06] {user} PAGE_night_audit expect={expect} denied={page_denied} ok={page_ok}"
                )

                results.append(entry)
                page.close()
                context.close()
            browser.close()
    finally:
        report = {
            "run_id": RUN_ID,
            "phase": "TASK-06 nine-role matrix rerun after night-auditor activation (1b882eb)",
            "base": BASE,
            "accounts": NINE,
            "role_names": ROLE_NAMES,
            "failures": failures,
            "passed": not failures,
            "results": results,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(os.path.join(EVIDENCE, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"failures={len(failures)}")
        print(f"report -> {os.path.join(EVIDENCE, 'report.json')}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
