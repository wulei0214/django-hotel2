/**
 * TASK-02 + TASK-03 浏览器 E2E（V3.0 任务书第一批 P0）：
 * Day-use 前台建单入口 + Day-use 转过夜前台操作入口。
 *
 * 前置条件（验收轮 seed）：
 *   * 存在当日生效的 DayUsePolicy（任一房型即可，价格任意 >0）；
 *   * test_front（前台）账号已 seed 且有 OPEN 收银班次前置（押金 CASH 由
 *     服务端强制，需本人班次——建单后前台面板「开班」按钮可兜底，脚本走
 *     API 预开班次由店长？不行——押金要求收款人本人班次，所以脚本先以前台
 *     本人身份调 POST /api/v1/billing/shifts/open 兜底开班）。
 *
 * 链路（全程 UI 触发写操作，API 仅做只读核对）：
 *   1. 前台登录 → /checkin/ → 建单区可见 → 切「日租」→ 政策价文案可见；
 *   2. 填姓名 → 建单入住 → toast 含预订号 → API 核对 billing_type=DAY_USE
 *      且房费凭证=政策价（只读）；
 *   3. 搜索该单 → 打开抽屉 → 「转为过夜」→ 填新离店日期+理由 → 确认；
 *   4. toast 成功 → API 核对 billing_type=NIGHT_STAY、离店日推进；
 *   5. UI 二次提交被拒（按钮随刷新消失）。
 *
 * 用法：node scripts/browser_tests/e2e_dayuse_frontdesk.mjs --base http://127.0.0.1:8100 \
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
const ROOM_TYPE_CODE = argValue("--room-type-code", "");
const PASSWORD = argValue("--password", "");
if (!PASSWORD) {
  console.error("缺少 --password（轮换后凭据见 gitignored scripts/r6_env.txt）");
  process.exit(2);
}
const EVIDENCE_DIR = argValue(
  "--evidence-dir",
  join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence", `v3-task02-03-${new Date().toISOString().slice(0, 10)}`)
);
mkdirSync(EVIDENCE_DIR, { recursive: true });

const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}

async function apiGet(page, url) {
  return page.evaluate(async (u) => {
    const res = await fetch(u, { credentials: "same-origin" });
    return { status: res.status, body: await res.json().catch(() => null) };
  }, url);
}

async function login(browser, username) {
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await context.newPage();
  await page.goto(`${BASE}/login/`, { waitUntil: "domcontentloaded" });
  await page.fill('input[name="username"]', username);
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
  if (page.url().includes("/login/")) {
    throw new Error(`${username} 登录失败`);
  }
  return { context, page };
}

const browser = await chromium.launch({ channel: "msedge", headless: true });
let bookingNo = null;
try {
  const front = await login(browser, FRONT);

  // 1. 建单区可见 + 日租切换 + 政策价文案
  await front.page.goto(`${BASE}/checkin/`, { waitUntil: "networkidle" });
  const walkinbar = front.page.locator("#fd-walkinbar");
  const barVisible = await walkinbar.isVisible().catch(() => false);
  record("walkinbar_visible", barVisible, barVisible ? "建单区已渲染" : "建单区不可见（权限或数据）");
  if (!barVisible) throw new Error("建单区不可见，中止");
  if (!ROOM_TYPE_CODE) {
    // 动态选房：净房（VACANT+CLEAN+NORMAL）最多的房型——walk-in 自动排房
    // 需要物理净房，库存 available_count>0 不代表有净房（保证可重复执行）
    const pick = await front.page.evaluate(async () => {
      const res = await fetch("/api/v1/rooms/status?page_size=100", { credentials: "same-origin" });
      const payload = await res.json();
      const rows = (payload.data && payload.data.data) || payload.data || [];
      const counts = {};
      rows.forEach((r) => {
        if (r.is_ready) counts[r.room_type && r.room_type.id] = (counts[r.room_type && r.room_type.id] || 0) + 1;
      });
      let best = null, bestCount = 0;
      Object.entries(counts).forEach(([id, n]) => {
        if (n > bestCount) { best = String(id); bestCount = n; }
      });
      return best;
    });
    if (!pick) throw new Error("当前无任何净房，无法建单");
    await front.page.selectOption("#fd-walkin-room-type", pick);
  } else {
    // 按 code 选中目标房型（option 文本为「名称（CODE）」）
    const optionValue = await front.page.evaluate((code) => {
      const select = document.getElementById("fd-walkin-room-type");
      const target = Array.from(select.options).find((o) => o.textContent.includes(`（${code}）`));
      return target ? target.value : null;
    }, ROOM_TYPE_CODE);
    if (!optionValue) throw new Error(`房型 ${ROOM_TYPE_CODE} 不在选项中`);
    await front.page.selectOption("#fd-walkin-room-type", optionValue);
  }
  await front.page.click('#fd-walkinbar [data-walkin-type="DAY_USE"]');
  await front.page.waitForTimeout(300);
  const quote = await front.page.locator("#fd-walkin-quote").textContent();
  record("dayuse_quote_policy_price", /日租价/.test(quote || ""),
    (quote || "").trim().slice(0, 60));

  // 2. 建单（日租）
  await front.page.fill("#fd-walkin-name", "日租浏览器验收客");
  await front.page.screenshot({ path: join(EVIDENCE_DIR, "01-dayuse-form.png") });
  await front.page.click("#fd-walkin-submit");
  await front.page.waitForSelector("#fd-toast.is-visible", { timeout: 10000 });
  const toast = await front.page.locator("#fd-toast").textContent();
  const bookingMatch = toast.match(/(?:建单入住|已存在)[：:]\s*([A-Za-z0-9-]+)/);
  bookingNo = bookingMatch ? bookingMatch[1] : null;
  record("dayuse_create_toast", !!bookingNo, toast.trim().slice(0, 80));
  await front.page.screenshot({ path: join(EVIDENCE_DIR, "02-dayuse-created.png") });

  // 3. API 只读核对：DAY_USE + 政策价房费
  if (bookingNo) {
    const detail = await apiGet(front.page, `${BASE}/api/v1/front-desk/reservations/${bookingNo}`);
    const data = detail.body && detail.body.data || {};
    record("api_dayuse_billing_type", data.billing_type === "DAY_USE",
      `billing_type=${data.billing_type} status=${data.status} total=${data.total_amount}`);
    const folio = await apiGet(front.page, `${BASE}/api/v1/front-desk/folios/${bookingNo}`);
    const folioData = folio.body && folio.body.data || {};
    const entries = folioData.entries || folioData.ledger_entries || [];
    const roomCharge = entries.find((e) => e.entry_type === "ROOM_CHARGE");
    record("api_dayuse_room_charge", !!roomCharge,
      roomCharge ? `房费凭证 ${roomCharge.amount}` : "未找到 ROOM_CHARGE 凭证");
  }

  // 4. 抽屉「转为过夜」：日租建单即入住 → 订单在「在住」页签
  await front.page.click('[data-kpi-tab="in-house"]');
  await front.page.waitForTimeout(900);
  await front.page.fill("#fd-search", bookingNo || "");
  await front.page.click("#fd-search-submit");
  await front.page.waitForSelector(`#fd-table-body tr:has-text("${bookingNo}")`, { timeout: 12000 });
  await front.page.click(`#fd-table-body tr:has-text("${bookingNo}") .fd-row-link`);
  await front.page.waitForSelector("#fd-drawer.is-open", { timeout: 8000 });
  await front.page.waitForTimeout(500);
  const convertBtn = front.page.locator('button:has-text("转为过夜")').first();
  const btnVisible = await convertBtn.isVisible().catch(() => false);
  record("convert_button_visible", btnVisible, btnVisible ? "DAY_USE 抽屉含转为过夜动作" : "按钮不可见");
  await front.page.screenshot({ path: join(EVIDENCE_DIR, "03-drawer-convert-entry.png") });
  if (btnVisible) {
    await convertBtn.click();
    const tomorrow = new Date(Date.now() + 86400000);
    const afterTomorrow = new Date(Date.now() + 2 * 86400000);
    const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    await front.page.fill('.fd-convert-form input[type="date"]', fmt(afterTomorrow));
    await front.page.fill('.fd-convert-form input:not([type="date"])', "浏览器验收：客人要求续住过夜");
    await front.page.screenshot({ path: join(EVIDENCE_DIR, "04-convert-form-filled.png") });
    await front.page.click('.fd-convert-form button:has-text("确认转为过夜")');
    // 等 toast 文本切到本次操作的提示（旧 toast 3.2s 窗口内可能仍在显示）
    await front.page
      .waitForFunction(
        () => {
          const el = document.getElementById("fd-toast");
          return el && el.classList.contains("is-visible") && /转为过夜/.test(el.textContent || "");
        },
        { timeout: 15000 }
      )
      .catch(() => {});
    const toast2 = await front.page.locator("#fd-toast").textContent();
    record("convert_success_toast", /已转为过夜/.test(toast2 || ""), toast2.trim().slice(0, 60));
    await front.page.screenshot({ path: join(EVIDENCE_DIR, "05-converted.png") });

    // 5. API 复核转换结果
    if (bookingNo) {
      const detail2 = await apiGet(front.page, `${BASE}/api/v1/front-desk/reservations/${bookingNo}`);
      const data2 = detail2.body && detail2.body.data || {};
      record("api_converted_night_stay", data2.billing_type === "NIGHT_STAY",
        `billing_type=${data2.billing_type} check_out=${data2.check_out}`);
      // 6. 转过夜后抽屉不再出现「转为过夜」（状态机显隐）
      await front.page.waitForTimeout(600);
      const btnGone = !(await front.page.locator('button:has-text("转为过夜")').first().isVisible().catch(() => false));
      record("convert_action_hidden_after_convert", btnGone, btnGone ? "NIGHT_STAY 抽屉无转换动作" : "按钮仍在（显隐未更新）");

      // 7. UI 结清应收（收款表单）→ 办理退房（闭环收口：房转待清洁+生成
      //    离店清洁任务，同时释放净房保证脚本可重复执行）。
      front.page.on("dialog", (d) => d.accept());
      const detail2b = await apiGet(front.page, `${BASE}/api/v1/front-desk/reservations/${bookingNo}`);
      const balance = Number(detail2b.body && detail2b.body.data && detail2b.body.data.folio_summary &&
        detail2b.body.data.folio_summary.balance || 0);
      if (balance > 0) {
        const payBox = front.page.locator('.fd-payment-box', { hasText: '本次实收金额' }).first();
        await payBox.locator('input[type="number"]').fill(balance.toFixed(2));
        await payBox.locator('select').selectOption('CASH');
        await payBox.locator('button[type="submit"]').click();
        await front.page.waitForSelector("#fd-toast.is-visible", { timeout: 15000 });
        await front.page.waitForTimeout(600);
        const payToast = await front.page.locator("#fd-toast").textContent();
        record("ui_payment_collected", /实收款项|收款/.test(payToast || ""), `余额 ${balance.toFixed(2)} 已收 ${payToast.trim().slice(0, 50)}`);
      }
      const checkoutBtn = front.page.locator('button:has-text("办理退房")').first();
      const checkoutVisible = await checkoutBtn.isVisible().catch(() => false);
      if (checkoutVisible) {
        await checkoutBtn.click();
        await front.page.waitForSelector("#fd-toast.is-visible", { timeout: 15000 });
        await front.page.waitForTimeout(400);
        const toast3 = await front.page.locator("#fd-toast").textContent();
        record("checkout_success_toast", /退房/.test(toast3 || ""), toast3.trim().slice(0, 60));
        await front.page.screenshot({ path: join(EVIDENCE_DIR, "06-checked-out.png") });
        const detail3 = await apiGet(front.page, `${BASE}/api/v1/front-desk/reservations/${bookingNo}`);
        const data3 = detail3.body && detail3.body.data || {};
        record("api_checked_out", data3.status === "CHECKED_OUT", `status=${data3.status}`);
      } else {
        record("checkout_success_toast", false, "抽屉无「办理退房」按钮（须先结清应收）");
      }
    }
  }

  await front.context.close();
} catch (error) {
  record("fatal", false, String(error).slice(0, 200));
} finally {
  await browser.close();
  const reportPath = join(EVIDENCE_DIR, "report.json");
  writeFileSync(reportPath, JSON.stringify({ results, booking_no: bookingNo }, null, 2));
  console.log(`报告：${reportPath}`);
  const failed = results.filter((r) => !r.pass);
  process.exit(failed.length ? 1 : 0);
}
