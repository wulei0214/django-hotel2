/**
 * TASK-01 companion E2E #2: guard — PENDING rows assigned to someone else
 * show no start button for the housekeeping operator.
 * Fixtures (dev DB, live-checked): task 20 room 3287 (assigned duty #9),
 * task 19 room 4612 (assigned hk self #12), task 7 room 4812 (assigned #32).
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
if (!PASSWORD) { console.error("missing --password"); process.exit(2); }
const EVIDENCE_DIR = join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence",
  `task01-hk10-guard-${new Date().toISOString().replace(/[:T]/g, "-").slice(0, 16)}`);
mkdirSync(EVIDENCE_DIR, { recursive: true });

const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail: detail || "" });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}
const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/login/`, { waitUntil: "domcontentloaded" });
  await page.fill('input[name="username"]', "test_housekeeping");
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForLoadState("networkidle");
  if (page.url().includes("/force-password-change/")) {
    await page.fill('input[name="current_password"]', PASSWORD);
    await page.fill('input[name="new_password1"]', PASSWORD);
    await page.fill('input[name="new_password2"]', PASSWORD);
    await page.click('button[type="submit"]');
    await page.waitForLoadState("networkidle");
  }
  if (page.url().includes("/login/")) throw new Error("login failed");

  await page.goto(`${BASE}/manage/housekeeping/`, { waitUntil: "networkidle" });
  await page.selectOption("#hk-task-status", "PENDING");
  await page.waitForTimeout(1000);

  // Row assigned to self (4612, hk #12): start button visible.
  const selfRow = page.locator("#hk-task-body tr").filter({ hasText: "4612" }).first();
  const selfStart = await selfRow.locator("button", { hasText: "开始" }).count();
  record("HK10-01b self-assigned PENDING row shows start", selfStart === 1, `${selfStart} button(s)`);

  // Rows assigned to others (3287 duty #9; 4812 user #32): no start button.
  const dutyRow = page.locator("#hk-task-body tr").filter({ hasText: "3287" }).first();
  const dutyStart = await dutyRow.locator("button", { hasText: "开始" }).count();
  record("HK10-09 row assigned to duty manager hides start", dutyStart === 0, `${dutyStart} button(s)`);
  const otherRow = page.locator("#hk-task-body tr").filter({ hasText: "4812" }).first();
  const otherStart = await otherRow.locator("button", { hasText: "开始" }).count();
  record("HK10-09b row assigned to other staff hides start", otherStart === 0, `${otherStart} button(s)`);
  await page.screenshot({ path: join(EVIDENCE_DIR, "01-guard-rows.png"), fullPage: true });
} catch (error) {
  record("E2E run", false, error.message);
} finally {
  writeFileSync(join(EVIDENCE_DIR, "report.json"), JSON.stringify({
    task: "TASK-01 companion: assigned-to-others guard",
    results,
    passed: results.filter((r) => r.pass).length,
    failed: results.filter((r) => !r.pass).length,
    finished_at: new Date().toISOString(),
  }, null, 2));
  console.log(`evidence: ${EVIDENCE_DIR}`);
  console.log(`passed ${results.filter((r) => r.pass).length}/${results.length}`);
  await browser.close();
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}
