/**
 * MC11-RES-03：PENDING 收款即确认的浏览器级回归（任务书 PMS2-11C-001 点名项）。
 *
 * 背景：615e976 只改了 frontdesk.js 状态门禁数组（零自动化测试）；9004155
 * 修复 D-R4-07 房行重复渲染同样是纯 JS。本脚本以真实浏览器（Edge，channel
 * 无需下载）固化以下断言：
 *   1. PENDING 订单在前台工作台可见且抽屉提供收款入口（PENDING 门禁）；
 *   2. 通过页面表单登记收款后：订单状态翻已确认、抽屉房行恰 1 条（D-R4-07）、
 *      排房入口出现（收款成功后页面/状态/库存/入口同步）；
 *   3. 库存占用同步（held→sold：排房入口背后的 rooms/assignable 可用性）。
 *
 * 用法：node scripts/browser_tests/e2e_frontdesk_pending.mjs --base http://127.0.0.1:8100
 * 退出码 0=全 PASS；截图存 docs/acceptance/evidence/mc11-browser-<date>/。
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
const USERNAME = argValue("--username", "test_front");
const PASSWORD = argValue("--password", "");
if (!PASSWORD) {
  console.error("缺少 --password（轮换后凭据见 gitignored scripts/r6_env.txt）");
  process.exit(2);
}
const EVIDENCE_DIR = join(
  SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence", `mc11-browser-${new Date().toISOString().slice(0, 10)}`
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

async function apiPost(page, url, payload, idempotencyKey) {
  return page.evaluate(
    async ({ u, payload, key }) => {
      // CSRF_USE_SESSIONS=True：令牌在会话中，取页面已渲染的表单隐藏域
      const csrfInput = document.querySelector("input[name=csrfmiddlewaretoken]");
      const csrf = csrfInput ? csrfInput.value : null;
      const res = await fetch(u, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": key,
          ...(csrf ? { "X-CSRFToken": csrf } : {}),
        },
        body: JSON.stringify(payload),
      });
      return { status: res.status, body: await res.json().catch(() => null) };
    },
    { u: url, payload, key: idempotencyKey }
  );
}

const run = await chromium.launch({
  channel: "msedge",
  headless: true,
});
try {
  const context = await run.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await context.newPage();
  const runTag = Date.now().toString(36);

  // 先进入同源页面（about:blank 的 evaluate 无法 fetch 应用域）
  await page.goto(`${BASE}/login/`, { waitUntil: "domcontentloaded" });

  // ── 步骤 0：公开可用性查询（匿名）→ 建单参数 ──
  // 用本地日期（ISO 字符串是 UTC，跨时区会在日切边界拿到"昨天"）
  const today = new Date();
  const fmt = (d) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const checkIn = fmt(today);
  const checkOut = fmt(new Date(today.getTime() + 86400000));
  const avail = await apiGet(
    page,
    `${BASE}/api/v1/availability?check_in=${checkIn}&check_out=${checkOut}&rooms=1`
  );
  if (avail.status !== 200 || !(avail.body.data || []).length) {
    console.error("可用性查询失败或无可售房型：", avail.status, JSON.stringify(avail.body).slice(0, 300));
    process.exit(3);
  }
  const plan = (avail.body.data || []).find((rt) => Number(rt.available_count) > 0);
  if (!plan) {
    console.error("availability 无可售房型：", JSON.stringify(avail.body).slice(0, 400));
    process.exit(3);
  }
  const roomTypeId = plan.room_type_id;
  const ratePlanId = plan.rate_plan_id;
  record("availability", true, `room_type=${roomTypeId} rate_plan=${ratePlanId}`);

  // ── 步骤 1：公开预订 API 建 PENDING 单（匿名，幂等键） ──
  const idemKey = `mc11-e2e-${runTag}`;
  const created = await apiPost(
    page,
    `${BASE}/api/v1/reservations`,
    {
      full_name: `MC11回归客人${runTag}`,
      phone: `138${(Date.now() % 100000000).toString().padStart(8, "0")}`,
      room_type_id: roomTypeId,
      rate_plan_id: ratePlanId,
      check_in: checkIn,
      check_out: checkOut,
      rooms: 1,
      adults: 1,
      children: 0,
    },
    idemKey
  );
  if (created.status !== 201) {
    console.error("公开建单失败：", created.status, JSON.stringify(created.body).slice(0, 300));
    process.exit(3);
  }
  const bookingNo = created.body.booking_no;
  record("public_create_pending", true, bookingNo);

  // ── 步骤 2：浏览器登录（真实登录表单） ──
  await page.goto(`${BASE}/login/`, { waitUntil: "networkidle" });
  await page.fill('input[name="username"]', USERNAME);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForLoadState("networkidle");
  record("login", !page.url().includes("/login/"), page.url());

  // 首登强制改密（must_change_password=True 账号）：顺应自助改密流程，
  // 新密码取同一 --password 值，保证脚本对同一账号可重复执行。
  if (page.url().includes("/force-password-change/")) {
    await page.fill('input[name="new_password1"]', PASSWORD);
    await page.fill('input[name="new_password2"]', PASSWORD);
    await page.click('button[type="submit"]');
    await page.waitForLoadState("networkidle");
    record("force_password_change", true, "首登改密完成（写回同一口令）");
  }

  // ── 步骤 3：前台工作台搜索订单并打开抽屉 ──
  await page.goto(`${BASE}/checkin/`, { waitUntil: "networkidle" });
  await page.fill("#fd-search", bookingNo);
  await page.click("#fd-search-submit");
  await page.waitForTimeout(800);
  const row = page.locator(`#fd-table-body tr:has-text("${bookingNo}")`).first();
  if ((await row.count()) === 0) {
    record("drawer_row_visible", false, `列表未找到 ${bookingNo}（PENDING 单应出现在订单 tab）`);
  } else {
    record("drawer_row_visible", true, bookingNo);
    await row.locator("button:has-text('查看详情')").first().click();
    await page.waitForTimeout(1000);
    const drawerOpen = await page.locator("#fd-drawer.is-open, #fd-drawer[aria-hidden='false']").count();
    record("drawer_open", drawerOpen > 0, "抽屉已打开");

    // ── 断言 1：PENDING 门禁下收款表单可见（615e976 修复的浏览器回归） ──
    const payForm = page.locator(".fd-payment-form").first();
    const payVisible = await payForm.count();
    record("pending_payment_form_visible", payVisible > 0, "PENDING 单抽屉含收款表单");
    if (payVisible > 0) {
      // ── 步骤 4：经页面表单登记收款（真实提交，confirm 对话框自动应答） ──
      page.on("dialog", (dialog) => dialog.accept());
      await payForm.locator("input[type='number']").fill("100.00");
      await payForm.locator("select").selectOption("CARD");
      await payForm.locator("button[type='submit']").click();
      await page
        .waitForFunction(
          (no) => {
            const el = document.querySelector("#detail-status");
            return el && el.textContent && el.textContent.includes("已确认");
          },
          bookingNo,
          { timeout: 15000 }
        )
        .then(() => record("payment_confirms_pending", true, "#detail-status=已确认"))
        .catch(() => record("payment_confirms_pending", false, "超时：状态未翻已确认"));

      // ── 断言 2：房行不翻倍（D-R4-07 回归：DB 1 条 → UI 恰 1 条房行） ──
      const roomRowCounts = await page.evaluate(() => {
        const ops = document.querySelector("#detail-operations");
        if (!ops) return -1;
        return ops.querySelectorAll(".fd-operation-room").length;
      });
      record("room_rows_not_duplicated", roomRowCounts === 1, `房行数=${roomRowCounts}`);

      // ── 断言 3：排房入口出现（收款成功 → 可排房同步） ──
      const assignVisible = await page.locator("button:has-text('分配房间'), select.fd-room-select, [data-action='assign']").count();
      record("assign_entry_available", assignVisible > 0, "确认后排房入口可见");
    }
  }
  await page.screenshot({ path: join(EVIDENCE_DIR, "A1-pending-pay-confirm.png"), fullPage: true });

  // ── 证据落盘 ──
  const passed = results.filter((r) => r.pass).length;
  writeFileSync(
    join(EVIDENCE_DIR, "frontdesk-pending-result.json"),
    JSON.stringify({ base: BASE, booking_no: bookingNo, results }, null, 2)
  );
  console.log(`\n合计 ${passed}/${results.length} PASS`);
  process.exit(passed === results.length ? 0 : 1);
} finally {
  await run.close();
}
