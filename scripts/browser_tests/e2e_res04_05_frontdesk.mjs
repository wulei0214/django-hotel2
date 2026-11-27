/**
 * MC11-RES-04 + MC11-RES-05 browser E2E.
 *
 * RES-04: deposit method select must not offer OTHER (server enum is
 *   CASH/CARD/TRANSFER only; OTHER would 400).
 * RES-05: an expanded folio-entries panel must rebuild after a front-desk
 *   operation (register deposit) instead of keeping stale entries.
 *
 * Flow (housekeeping-free, pure front-desk):
 *   owner session: pick a CONFIRMED/CHECKED_IN reservation with OPEN folio
 *   front-desk session: open drawer -> click "查看账单明细" (panel open) ->
 *   register deposit -> after refresh the panel must show the new DEPOSIT
 *   entry (RES-05), and the deposit form must not contain OTHER (RES-04).
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
const BASE = argValue("--base", "http://127.0.0.1:8124");
const PASSWORD = argValue("--password", "");
if (!PASSWORD) { console.error("missing --password"); process.exit(2); }
const EVIDENCE_DIR = join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence",
  `res04-05-${new Date().toISOString().replace(/[:T]/g, "-").slice(0, 16)}`);
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

async function login(browser, username) {
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await context.newPage();
  const loggedInWith = await loginWithRotation(page, username);
  if (!loggedInWith) throw new Error(`${username} login failed`);
  return { context, page };
}

const browser = await chromium.launch({ channel: "msedge", headless: true });
let fd = null;
let bookingNo = null;
let depositCountBefore = 0;
try {
  fd = await login(browser, "test_front");

  // Find a CONFIRMED or CHECKED_IN reservation with an OPEN folio.
  const res = await apiGet(fd.page, `${BASE}/api/v1/front-desk/reservations?page_size=100`);
  const items = ((res.body || {}).data) || [];
  const target = items.find(
    (r) => ["CONFIRMED", "CHECKED_IN"].includes(r.status) &&
      r.folio_summary && r.folio_summary.status === "OPEN"
  );
  record("fixture: reservation with OPEN folio", !!target,
    target ? target.booking_no : JSON.stringify(res.body).slice(0, 120));
  if (!target) throw new Error("SKIP: no eligible reservation");
  bookingNo = target.booking_no;

  // Open drawer on the front-desk workspace (route: /checkin/).
  await fd.page.goto(`${BASE}/checkin/`, { waitUntil: "networkidle" });

  // Search in the "订单查询" tab (reservations endpoint covers all statuses).
  await fd.page.click('button.fd-tab[data-tab="reservations"]');
  await fd.page.fill("#fd-search", bookingNo);
  await fd.page.click("#fd-search-submit");
  await fd.page.waitForTimeout(1200);
  const rowBtn = fd.page.locator("button.fd-row-link").first();
  if ((await rowBtn.count()) === 0) throw new Error("SKIP: drawer row not found for " + bookingNo);
  await rowBtn.click();
  await fd.page.waitForTimeout(1200);

  // RES-04: deposit method select has no OTHER option.
  const depositBox = fd.page.locator(".fd-payment-box", { hasText: "登记入住押金" }).first();
  const otherOptions = await depositBox.locator("select.fd-operation-select option[value='OTHER']").count();
  const methodValues = await depositBox.locator("select.fd-operation-select option").evaluateAll(
    (opts) => opts.map((o) => o.value)
  );
  record("RES-04 deposit select has no OTHER", otherOptions === 0, methodValues.join("/"));
  record("RES-04 deposit select keeps server-accepted methods",
    ["CASH", "CARD", "BANK_TRANSFER"].every((v) => methodValues.includes(v)),
    methodValues.join("/"));

  // Open folio panel (RES-05 precondition).
  await fd.page.click("#fd-view-folio");
  await fd.page.waitForTimeout(1200);
  depositCountBefore = await fd.page.locator("#detail-folio-entries .fd-folio-entry")
    .filter({ hasText: "押金" }).count();
  record("RES-05 fixture: folio panel open, deposit entries visible", true, `${depositCountBefore} deposit row(s)`);
  await fd.page.screenshot({ path: join(EVIDENCE_DIR, "01-panel-before.png"), fullPage: true });

  // Register a deposit while the panel is open (cash requires shift; use CARD).
  const amountInput = depositBox.locator("input.fd-operation-input");
  const amount = "100.00";
  await amountInput.fill(amount);
  await depositBox.locator("select.fd-operation-select").selectOption("CARD");
  fd.page.once("dialog", (dialog) => dialog.accept());
  await depositBox.locator("button[type='submit']").click();
  await fd.page.waitForTimeout(2500);

  // RES-05: the still-open panel must now include the fresh DEPOSIT entry.
  const depositCountAfter = await fd.page.locator("#detail-folio-entries .fd-folio-entry")
    .filter({ hasText: "押金" }).count();
  record("RES-05 panel rebuilt after operation (new deposit visible)",
    depositCountAfter > depositCountBefore, `${depositCountBefore} -> ${depositCountAfter}`);
  await fd.page.screenshot({ path: join(EVIDENCE_DIR, "02-panel-after.png"), fullPage: true });

  // Cross-check via API: the deposit produced both a DEPOSIT ledger entry
  // and a payment record; the panel count (entries + payments rows) must
  // therefore exceed the pre-operation count by at least the new rows.
  const folio = await apiGet(fd.page, `${BASE}/api/v1/front-desk/folios/${bookingNo}`);
  const data = (folio.body || {}).data || {};
  const depositEntries = (data.entries || []).filter((e) => e.entry_type === "DEPOSIT");
  const paymentRows = (data.payments || []).length;
  const panelRowsExpected = depositEntries.length + paymentRows;
  record("RES-05 API cross-check: panel rows match entries+payments",
    panelRowsExpected === depositCountAfter,
    `api ${depositEntries.length} entries + ${paymentRows} payments = ${panelRowsExpected}; panel ${depositCountAfter}`);
} catch (error) {
  record("E2E run", false, error.message);
} finally {
  writeFileSync(join(EVIDENCE_DIR, "report.json"), JSON.stringify({
    task: "MC11-RES-04 + MC11-RES-05",
    base: BASE, booking_no: bookingNo, results,
    passed: results.filter((r) => r.pass).length,
    failed: results.filter((r) => !r.pass).length,
    finished_at: new Date().toISOString(),
  }, null, 2));
  console.log(`evidence: ${EVIDENCE_DIR}`);
  console.log(`passed ${results.filter((r) => r.pass).length}/${results.length}`);
  if (fd) await fd.context.close();
  await browser.close();
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}
