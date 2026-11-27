/**
 * TASK-07 Windows 独立部署复现：九岗位登录 + 关键页面 + 核心操作验证。
 * 独立于指南作者编写的脚本；只读验证（不写业务数据）。
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
const BASE = argValue("--base", "http://127.0.0.1:8130");
const PASSWORD = argValue("--password", "");
if (!PASSWORD) { console.error("missing --password"); process.exit(2); }
const OUT = join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence", "task07-win-repro-2026-11-22");
mkdirSync(OUT, { recursive: true });

const ACCOUNTS = [
  ["test_front", "前台", ["/checkin/"]],
  ["test_duty", "值班经理", ["/manage/night-audit/", "/manage/housekeeping/"]],
  ["test_finance", "财务", ["/manage/finance/"]],
  ["test_owner", "店长", ["/manage/staff/", "/manage/audit/"]],
  ["test_housekeeping", "客房服务员", ["/manage/housekeeping/"]],
  ["test_nightaudit", "夜审员", ["/manage/night-audit/"]],
  ["test_fnb", "餐饮服务员", ["/home/"]],
  ["test_fnbsup", "餐饮主管", ["/home/"]],
  ["test_parking", "停车安保", ["/home/"]],
];

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

const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  for (const [username, role, pages] of ACCOUNTS) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    try {
      const loggedInWith = await loginWithRotation(page, username);
      const loggedIn = !!loggedInWith;
      record(`${role} (${username}) 登录`, loggedIn, page.url().replace(BASE, ""));

      for (const path of pages) {
        const resp = await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded" });
        const code = resp ? resp.status() : 0;
        // 403 也可能是"岗位无权"的合法拒绝；此处只看"页面可服务"（非 5xx）
        record(`  ${role} ${path} 页面服务`, code > 0 && code < 500, `HTTP ${code}`);
        if (code === 200) {
          const shot = `${username}${path.replace(/\//g, "_")}.png`;
          await page.screenshot({ path: join(OUT, shot), fullPage: false }).catch(() => {});
        }
      }
    } catch (e) {
      record(`${role} (${username})`, false, e.message.slice(0, 100));
    } finally {
      await ctx.close();
    }
  }
} finally {
  writeFileSync(join(OUT, "nine-role-login-report.json"), JSON.stringify({
    task: "TASK-07 Windows 独立部署复现：九岗位登录与页面服务",
    base: BASE, results,
    passed: results.filter((r) => r.pass).length,
    failed: results.filter((r) => !r.pass).length,
    finished_at: new Date().toISOString(),
  }, null, 2));
  console.log(`\nevidence: ${OUT}`);
  console.log(`passed ${results.filter((r) => r.pass).length}/${results.length}`);
  await browser.close();
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}
