/**
 * TASK-01 companion E2E: REJECTED task "restart" button end-to-end.
 * Fixtures: tasks 17/18 are REJECTED, assigned to test_housekeeping.
 * Verifies: REJECTED row renders restart -> click -> task IN_PROGRESS +
 * room DIRTY->CLEANING (service-layer fix MC11-HK-10 companion defect).
 */
import { chromium } from "playwright-core";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import process from "node:process";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
function argValue(flag, fallback) {
  const idx = process.argv.indexOf(flag);
  return idx >= 0 ? process.argv[idx + 1] : fallback;
}
const BASE = argValue("--base", "http://127.0.0.1:8123");
const PASSWORD = argValue("--password", "");
const TASK_ID = Number(argValue("--task", "17"));
const ROOM_NO = argValue("--room", "3287");
if (!PASSWORD) { console.error("missing --password"); process.exit(2); }
const EVIDENCE_DIR = join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence",
  `task01-hk10-restart-${new Date().toISOString().replace(/[:T]/g, "-").slice(0, 16)}`);
mkdirSync(EVIDENCE_DIR, { recursive: true });

const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail: detail || "" });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}
async function apiGet(page, url) {
  return page.evaluate(async (u) => {
    const res = await fetch(u, { credentials: "same-origin" });
    return { status: res.status, body: await res.json().catch(() => null) };
  }, url);
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
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await ctx.newPage();
  const loggedInWith = await loginWithRotation(page, "test_housekeeping");
  if (!loggedInWith) throw new Error("login failed");

  await page.goto(`${BASE}/manage/housekeeping/`, { waitUntil: "networkidle" });
  await page.selectOption("#hk-task-status", "REJECTED");
  await page.waitForTimeout(800);
  const row = page.locator("#hk-task-body tr").filter({ hasText: ROOM_NO }).first();
  const restartBtn = row.locator("button", { hasText: "重新开始" }).first();
  record("HK10-07 REJECTED row renders restart", (await restartBtn.count()) > 0);
  await page.screenshot({ path: join(EVIDENCE_DIR, "01-rejected-row.png"), fullPage: true });
  await restartBtn.click();
  await page.waitForTimeout(1200);
  const detail = await apiGet(page, `${BASE}/api/v1/housekeeping/tasks/${TASK_ID}`);
  record("HK10-08 restart -> task IN_PROGRESS", detail.body.data.status === "IN_PROGRESS", detail.body.data.status);
  const rooms = await apiGet(page, `${BASE}/api/v1/housekeeping/rooms?page_size=100`);
  const room = ((rooms.body && rooms.body.data) || []).find((r) => r.number === ROOM_NO);
  record("HK10-08b room DIRTY->CLEANING same transaction",
    room && room.housekeeping_status === "CLEANING", room ? room.housekeeping_status : "missing");
  await page.screenshot({ path: join(EVIDENCE_DIR, "02-after-restart.png"), fullPage: true });
} catch (error) {
  record("E2E run", false, error.message);
} finally {
  writeFileSync(join(EVIDENCE_DIR, "report.json"), JSON.stringify({
    task: "TASK-01 companion: REJECTED restart",
    task_id: TASK_ID, room: ROOM_NO, results,
    passed: results.filter((r) => r.pass).length,
    failed: results.filter((r) => !r.pass).length,
    finished_at: new Date().toISOString(),
  }, null, 2));
  console.log(`evidence: ${EVIDENCE_DIR}`);
  console.log(`passed ${results.filter((r) => r.pass).length}/${results.length}`);
  await browser.close();
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}
