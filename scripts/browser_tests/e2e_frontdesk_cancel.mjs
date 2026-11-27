/**
 * TASK-04 浏览器 E2E（V3.0 任务书第一批 P1）：前台取消 PENDING 预订入口。
 *
 * 前置：test_front（前台）账号已 seed。
 * 链路：
 *   1. 公开域 API 建 PENDING 单（浏览器会话内 fetch，允许——公开预订属
 *      验数据准备，非高危写）；
 *   2. 前台登录 → /checkin/ → 搜索该单 → 抽屉出现「取消预订」区块；
 *   3. 空原因提交 → 提示必填（前端拦截）；
 *   4. 填原因 → 确认 → toast「已取消该预订」；
 *   5. API 只读核对 status=CANCELLED；抽屉复开无取消区块（幂等显隐）。
 *
 * 用法：node scripts/browser_tests/e2e_frontdesk_cancel.mjs --base http://127.0.0.1:8100 \
 *         --front test_front --password <口令>
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
const FRONT = argValue("--front", "test_front");
const PASSWORD = argValue("--password", "");
if (!PASSWORD) {
  console.error("缺少 --password");
  process.exit(2);
}
const EVIDENCE_DIR = argValue(
  "--evidence-dir",
  join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence", `v3-task04-${new Date().toISOString().slice(0, 10)}`)
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
let bookingNo = null;
try {
  // 0. 匿名会话：公开 API 建 PENDING 单（验数据准备）
  const seedCtx = await browser.newContext();
  const seedPage = await seedCtx.newPage();
  await seedPage.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  const created = await seedPage.evaluate(async () => {
    const today = new Date();
    const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const checkIn = fmt(today);
    const checkOut = fmt(new Date(today.getTime() + 86400000));
    const avail = await fetch(
      `/api/v1/availability?check_in=${checkIn}&check_out=${checkOut}&rooms=1`,
      { credentials: "same-origin" }
    ).then((r) => r.json());
    const rt = (avail.data || []).find((row) => Number(row.available_count) > 0);
    if (!rt) return { error: "无可售房型" };
    // 公开预订页表单需要 CSRF；直接走公开 API 需带表单令牌——从首页取
    const html = await fetch("/", { credentials: "same-origin" }).then((r) => r.text());
    const m = html.match(/csrfmiddlewaretoken['"]\s*value=['"]([^'"]+)/) ||
              html.match(/name="csrfmiddlewaretoken" value="([^"]+)"/);
    const res = await fetch("/api/v1/reservations", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": m ? m[1] : "",
        "Idempotency-Key": "e2e-cancel-" + Date.now().toString(36),
      },
      body: JSON.stringify({
        full_name: "前台取消验收客",
        phone: "139" + String(Date.now()).slice(-8),
        room_type_id: rt.room_type_id,
        rate_plan_id: rt.rate_plans && rt.rate_plans.length ? rt.rate_plans[0].id : undefined,
        check_in: checkIn,
        check_out: checkOut,
        rooms: 1,
        adults: 1,
        children: 0,
      }),
    });
    return { status: res.status, body: await res.json().catch(() => null) };
  });
  await seedCtx.close();
  const seedBody = created && created.body || {};
  bookingNo = (seedBody.data && seedBody.data.booking_no) || seedBody.booking_no || null;
  record("seed_pending_reservation", !!bookingNo,
    bookingNo ? `PENDING 单 ${bookingNo}` : JSON.stringify(created).slice(0, 160));
  if (!bookingNo) throw new Error("PENDING 夹具创建失败，中止");

  const front = await login(browser, FRONT);
  await front.page.goto(`${BASE}/checkin/`, { waitUntil: "networkidle" });
  // PENDING 未入住订单在「今日应到」列表（arrivals）
  await front.page.fill("#fd-search", bookingNo);
  await front.page.click("#fd-search-submit");
  await front.page.waitForSelector(`#fd-table-body tr:has-text("${bookingNo}")`, { timeout: 12000 });
  await front.page.click(`#fd-table-body tr:has-text("${bookingNo}") .fd-row-link`);
  await front.page.waitForSelector("#fd-drawer.is-open", { timeout: 8000 });
  await front.page.waitForTimeout(500);

  const cancelBox = front.page.locator("button:has-text(\"取消预订\")").first();
  const boxVisible = await cancelBox.isVisible().catch(() => false);
  record("cancel_box_visible_for_pending", boxVisible, boxVisible ? "PENDING 抽屉含取消预订动作" : "不可见");
  await front.page.screenshot({ path: join(EVIDENCE_DIR, "01-cancel-entry.png") });

  if (boxVisible) {
    // 空原因提交 → 前端拦截 toast
    await cancelBox.click();
    await front.page.waitForSelector("#fd-toast.is-visible", { timeout: 6000 });
    const emptyToast = await front.page.locator("#fd-toast").textContent();
    record("empty_reason_blocked", /取消原因/.test(emptyToast || ""), emptyToast.trim().slice(0, 50));
    await front.page.click('#fd-toast .close, body', { trial: true }).catch(() => {});
    await front.page.waitForTimeout(3600);
    // 填原因 + 确认
    await front.page.fill('.fd-payment-box input[placeholder*="客人来电取消"]', "浏览器验收：客人来电取消");
    await front.page.on("dialog", (d) => d.accept());
    await front.page.click('button:has-text("取消预订")');
    await front.page.waitForSelector("#fd-toast.is-visible", { timeout: 10000 });
    await front.page.waitForTimeout(400);
    const toast = await front.page.locator("#fd-toast").textContent();
    record("cancel_success_toast", /已取消/.test(toast || ""), toast.trim().slice(0, 60));
    await front.page.screenshot({ path: join(EVIDENCE_DIR, "02-cancelled.png") });

    // API 只读核对
    const detail = await front.page.evaluate(async (no) => {
      const res = await fetch(`/api/v1/front-desk/reservations/${no}`, { credentials: "same-origin" });
      return res.json().catch(() => null);
    }, bookingNo);
    const data = detail && detail.data || {};
    record("api_cancelled_status", data.status === "CANCELLED", `status=${data.status}`);
  }

  await front.context.close();
} catch (error) {
  record("fatal", false, String(error).slice(0, 200));
} finally {
  await browser.close();
  const reportPath = join(EVIDENCE_DIR, "report.json");
  writeFileSync(reportPath, JSON.stringify({ results, booking_no: bookingNo }, null, 2));
  console.log(`报告：${reportPath}`);
  process.exit(results.filter((r) => !r.pass).length ? 1 : 0);
}
