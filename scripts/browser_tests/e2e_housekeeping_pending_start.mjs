/**
 * TASK-01 浏览器 E2E（V3.0 任务书第一批 P1）：清洁任务池 PENDING「开始」按钮。
 *
 * 前置：test_housekeeping（客房服务员）账号已 seed；存在 PENDING 清洁任务
 * （验收轮 seed 或退房自动生成）。
 * 链路：
 *   1. 客房服务员登录 → /manage/housekeeping/ → 任务池找到 PENDING（待清洁）行；
 *   2. 该行出现「开始」按钮 → 点击；
 *   3. 行刷新为 IN_PROGRESS（清洁中）+ 行内提示「已开始清洁」；
 *   4. API 只读核对任务状态 IN_PROGRESS、assigned_to=当前账号（服务端自动记人）。
 *
 * 用法：node scripts/browser_tests/e2e_housekeeping_pending_start.mjs \
 *         --base http://127.0.0.1:8100 --hk test_housekeeping --password <口令> \
 *         [--room-number 1410]
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

const BASE = argValue("--base", "http://127.0.0.1:8100");
const HK = argValue("--hk", "test_housekeeping");
const ROOM = argValue("--room-number", "");
const PASSWORD = argValue("--password", "");
if (!PASSWORD) {
  console.error("缺少 --password");
  process.exit(2);
}
const EVIDENCE_DIR = argValue(
  "--evidence-dir",
  join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence", `v3-task01-${new Date().toISOString().slice(0, 10)}`)
);
mkdirSync(EVIDENCE_DIR, { recursive: true });

const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail });
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
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await context.newPage();
  const loggedInWith = await loginWithRotation(page, username);
  if (!loggedInWith) throw new Error(`${username} 登录失败`);
  return { context, page };
}

const browser = await chromium.launch({ channel: "msedge", headless: true });
let taskId = null;
try {
  const hk = await login(browser, HK);
  await hk.page.goto(`${BASE}/manage/housekeeping/`, { waitUntil: "networkidle" });
  await hk.page.waitForTimeout(1000);
  // 截图任务池初始态
  await hk.page.screenshot({ path: join(EVIDENCE_DIR, "01-task-pool.png") });

  // 找 PENDING 行：状态徽标文本「待清洁」；可指定房间号缩小范围。
  // 只选未指派（含「未派工」）的行——并行显示守卫下已指派给他人的行无「开始」。
  const pendingRow = hk.page
    .locator("tr", { hasText: ROOM || "待清洁" })
    .filter({ hasText: "待清洁" })
    .filter({ hasText: "未派工" })
    .first();
  const rowCount = await pendingRow.count().catch(() => 0);
  record("pending_row_found", rowCount > 0, rowCount ? "任务池存在未派工 PENDING 行" : `无未派工 PENDING 任务${ROOM ? "（房号 " + ROOM + "）" : ""}`);
  if (!rowCount) throw new Error("无未派工 PENDING 任务可验收，中止");

  // 先记下目标房号（行点击后会被替换，原 locator 重解析会指到别的行）
  const rowTextBefore = (await pendingRow.textContent().catch(() => "")) || "";
  const roomNo = (rowTextBefore.match(/^\s*(\d+)/) || [null, null])[1] ||
    (rowTextBefore.match(/\b(3\d{3}|2\d{3}|9\d{3})\b/) || [null, null])[1];
  const startBtn = pendingRow.locator('button:has-text("开始")').first();
  const btnVisible = await startBtn.isVisible().catch(() => false);
  record("start_button_visible", btnVisible, btnVisible ? `PENDING 行（房 ${roomNo || "?"}）含「开始」按钮` : "按钮不可见");
  if (!btnVisible) throw new Error("开始按钮不可见，中止");

  await startBtn.click();
  // 行替换后：该房行出现「清洁中」
  const startedRow = hk.page.locator(`tr:has-text("${roomNo}")`).filter({ hasText: "清洁中" }).first();
  await startedRow.waitFor({ timeout: 12000 }).catch(() => {});
  await hk.page.screenshot({ path: join(EVIDENCE_DIR, "02-started.png") });
  const rowTextAfter = (await startedRow.textContent().catch(() => "")) || "";
  record("row_becomes_in_progress", /清洁中/.test(rowTextAfter), rowTextAfter.replace(/\s+/g, " ").slice(0, 80));

  // API 只读核对：该房存在 IN_PROGRESS 任务（payload.data.items）
  const apiCheck = await hk.page.evaluate(async (room) => {
    const res = await fetch("/api/v1/housekeeping/tasks?page_size=50", { credentials: "same-origin" });
    return res.json().catch(() => null);
  }, roomNo);
  const rows = (apiCheck && apiCheck.data && apiCheck.data.items) || [];
  const inProgress = rows.find((t) => t.status === "IN_PROGRESS" && (!roomNo || String(t.room_number) === String(roomNo)));
  record("api_task_in_progress", !!inProgress,
    inProgress ? `任务#${inProgress.id} 房 ${inProgress.room_number} IN_PROGRESS assigned_to=${inProgress.assigned_to}` : `未找到房 ${roomNo} 的 IN_PROGRESS 任务`);
  taskId = inProgress ? inProgress.id : null;

  await hk.context.close();
} catch (error) {
  record("fatal", false, String(error).slice(0, 200));
} finally {
  await browser.close();
  const reportPath = join(EVIDENCE_DIR, "report.json");
  writeFileSync(reportPath, JSON.stringify({ results, task_id: taskId }, null, 2));
  console.log(`报告：${reportPath}`);
  process.exit(results.filter((r) => !r.pass).length ? 1 : 0);
}
