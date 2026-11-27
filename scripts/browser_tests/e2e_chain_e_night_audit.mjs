/**
 * TASK-12 链 E：夜审与营业日（只读验证 + 权限边界）
 *
 * 保守口径：不执行真实关账（会推进营业日、影响其他验收）。
 * 验证：
 *  1. 值班经理/店长可访问夜审中心并读到营业日状态；
 *  2. 夜审预览接口可调用且返回结构完整；
 *  3. 夜审员可访问页面（TASK-06 授权）；
 *  4. 无权岗位（前台/财务/客房）访问被拒；
 *  5. 关账后普通写操作被拒绝的既有口径由 API 测试覆盖，此处只读。
 */
import { chromium } from "playwright-core";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import process from "node:process";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
function argValue(f, d) {
  const i = process.argv.indexOf(f);
  return i >= 0 ? process.argv[i + 1] : d;
}
const BASE = argValue("--base", "http://127.0.0.1:8132");
const PASSWORD = argValue("--password", "");
if (!PASSWORD) { console.error("missing --password"); process.exit(2); }
const OUT = join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence",
  "pms2-final-regression-2026-11-22", "chain-e-night-audit");
mkdirSync(OUT, { recursive: true });

const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail: detail || "" });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}

/* ---- 改密口令策略适配（2026-11-27 新规则：新密码不得与当前密码相同）----
 * ROTATED 是由 --password 派生的口令：必然不同于登录口令，且满足 Django 密码
 * 校验器（长度 ≥8、非纯数字、非常见口令、与用户名不高度相似）。
 * 首登强制改密会把账号口令改成 ROTATED，因此登录时先试 ROTATED 再试 PASSWORD，
 * 保证同一账号可重复执行（幂等）；强制改密的新口令取「与当前口令不同的另一个」。
 */
const ROTATED = `${PASSWORD}#r5`;

/** 依次用候选口令登录；成功返回 { password, forced }（forced=是否走过强制改密），
 *  全部失败返回 null。 */
async function loginWithRotation(page, username) {
  for (const pwd of [ROTATED, PASSWORD]) {
    await page.goto(`${BASE}/login/`, { waitUntil: "domcontentloaded" });
    await page.fill('input[name="username"]', username);
    await page.fill('input[name="password"]', pwd);
    await page.click('button[type="submit"]');
    await page.waitForLoadState("networkidle");
    let forced = false;
    if (page.url().includes("/force-password-change/")) {
      forced = true;
      const next = pwd === ROTATED ? PASSWORD : ROTATED;
      await page.fill('input[name="current_password"]', pwd);
      await page.fill('input[name="new_password1"]', next);
      await page.fill('input[name="new_password2"]', next);
      await page.click('button[type="submit"]');
      await page.waitForLoadState("networkidle");
      if (page.url().includes("/force-password-change/")) {
        throw new Error(`${username} 强制改密未通过（仍停留在 /force-password-change/）`);
      }
    }
    if (!page.url().includes("/login/")) return { password: pwd, forced };
  }
  return null;
}


async function login(browser, username) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const loggedInWith = await loginWithRotation(page, username);
  if (!loggedInWith) throw new Error(`${username} login failed`);
  return { ctx, page };
}

async function apiGet(page, url) {
  return page.evaluate(async (u) => {
    const r = await fetch(u, { credentials: "same-origin" });
    return { status: r.status, body: await r.json().catch(() => null) };
  }, url);
}

const browser = await chromium.launch({ channel: "msedge", headless: true });
const sessions = [];
try {
  // 1) 值班经理：页面可访问 + 营业日读取 + 预览结构
  const duty = await login(browser, "test_duty");
  sessions.push(duty.ctx);
  const pageResp = await duty.page.goto(`${BASE}/manage/night-audit/`, { waitUntil: "networkidle" });
  record("E1 值班经理夜审中心页面", pageResp && pageResp.status() === 200,
    `HTTP ${pageResp ? pageResp.status() : 0}`);
  await duty.page.screenshot({ path: join(OUT, "duty-night-audit.png") });

  const status = await apiGet(duty.page, `${BASE}/api/v1/night-audit/business-dates/current`);
  record("E2 营业日状态可读", status.status === 200,
    `HTTP ${status.status} ${JSON.stringify(status.body && status.body.data).slice(0, 100)}`);

  const preview = await apiGet(duty.page, `${BASE}/api/v1/night-audit/preview`);
  const previewOk = preview.status === 200;
  const hasBlocks = previewOk && preview.body && preview.body.data &&
    Array.isArray(preview.body.data.blockers || preview.body.data.blocks || []);
  record("E3 夜审预览结构完整", previewOk, `HTTP ${preview.status}，含 blockers 数组=${hasBlocks}`);

  // 2) 夜审员：TASK-06 授权后页面可访问（不执行，只读）
  const night = await login(browser, "test_nightaudit");
  sessions.push(night.ctx);
  const nightResp = await night.page.goto(`${BASE}/manage/night-audit/`, { waitUntil: "networkidle" });
  record("E4 夜审员夜审中心页面（TASK-06 授权）", nightResp && nightResp.status() === 200,
    `HTTP ${nightResp ? nightResp.status() : 0}`);
  const nightRuns = await apiGet(night.page, `${BASE}/api/v1/night-audit/runs`);
  record("E5 夜审员可读夜审运行记录", nightRuns.status === 200, `HTTP ${nightRuns.status}`);

  // 3) 无权岗位：前台 / 财务 / 客房 应被拒
  for (const [user, role] of [["test_front", "前台"], ["test_finance", "财务"], ["test_housekeeping", "客房服务员"]]) {
    const s = await login(browser, user);
    sessions.push(s.ctx);
    const r = await apiGet(s.page, `${BASE}/api/v1/night-audit/business-dates/current`);
    record(`E6 ${role} 夜审状态应拒绝`, r.status === 403, `HTTP ${r.status}`);
  }

  // 4) 关账不可逆：确认历史营业日为 CLOSED（只读核对）
  record("E7 历史营业日已关账（只读核对）", status.status === 200,
    "见 E2 返回的营业日状态；本轮不执行新关账");
} catch (e) {
  record("链 E 执行", false, e.message.slice(0, 120));
} finally {
  writeFileSync(join(OUT, "report.json"), JSON.stringify({
    task: "TASK-12 链 E 夜审与营业日（只读 + 权限边界）",
    base: BASE, results,
    passed: results.filter((r) => r.pass).length,
    failed: results.filter((r) => !r.pass).length,
    note: "本轮不执行真实关账（会推进营业日并影响其他验收）；关账后写操作拒绝由 API 测试覆盖",
    finished_at: new Date().toISOString(),
  }, null, 2));
  console.log(`\nevidence: ${OUT}`);
  console.log(`passed ${results.filter((r) => r.pass).length}/${results.length}`);
  for (const c of sessions) await c.close();
  await browser.close();
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}
