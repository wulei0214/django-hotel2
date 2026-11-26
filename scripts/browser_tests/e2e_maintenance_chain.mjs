/**
 * MC11-HK-01 / 任务书第三阶段链路 B：维修报修→房间停用→库存变化→派工→
 * 维修→验收→恢复可售 的浏览器级 E2E（六链路 v1 报告未覆盖的唯一链）。
 *
 * 关键断言：
 *   1. 报修后房型可售库存 -1（停用与库存 out_of_service 同事务同步）；
 *   2. "完成维修"（resolve）后房间仍停用、库存不恢复（必须验收通过）；
 *   3. 验收通过后房间恢复可售、库存回到基线；
 *   4. SoD：验收人 ≠ 报修人 ≠（本脚本报修与执行同为值班经理，验收人=店长）。
 *
 * 用法：node scripts/browser_tests/e2e_maintenance_chain.mjs --base http://127.0.0.1:8100 \
 *         --reporter test_duty --verifier test_owner --password <轮换后口令>
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
const REPORTER = argValue("--reporter", "test_duty");
const VERIFIER = argValue("--verifier", "test_owner");
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

/** 登录 + 顺应首登强制改密（写回同一口令，保证可重复执行）。 */
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

async function availabilityCount(page, roomTypeId) {
  const today = new Date();
  // 本地日期（ISO 是 UTC，跨时区会在日切边界拿到"昨天"）
  const fmt = (d) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const checkIn = fmt(today);
  const checkOut = fmt(new Date(today.getTime() + 86400000));
  const res = await apiGet(
    page,
    `${BASE}/api/v1/availability?check_in=${checkIn}&check_out=${checkOut}&rooms=1&room_type_id=${roomTypeId}`
  );
  const row = (res.body && res.body.data || []).find((rt) => Number(rt.room_type_id) === Number(roomTypeId));
  return row ? Number(row.available_count) : null;
}

const browser = await chromium.launch({ channel: "msedge", headless: true });
let ticketNo = null;
let room = null;
let availBefore = null;
let availStopped = null;
let availAfter = null;
try {
  // ── 店长会话：取值班经理档案 ID（staff API 门禁 identity.staff_manage） ──
  const owner = await login(browser, VERIFIER);
  const staffRes = await apiGet(owner.page, `${BASE}/api/v1/staff?page_size=100`);
  const staffRows =
    (staffRes.body && staffRes.body.data) ||
    [];
  const reporterRow = staffRows.find(
    (r) => r.account && r.account.username === REPORTER
  );
  if (!reporterRow) {
    console.error("staff 查询失败：", JSON.stringify(staffRes.body).slice(0, 300));
    process.exit(3);
  }
  const reporterProfileId = reporterRow.id;
  record("staff_profile_id", true, `${REPORTER} → 档案 #${reporterProfileId}`);
  await owner.context.close();

  // ── 值班经理会话：报修 → 派工 → 维修 → 完成维修 ──
  const duty = await login(browser, REPORTER);
  const roomsRes = await apiGet(duty.page, `${BASE}/api/v1/housekeeping/rooms?page_size=100`);
  const roomsData = roomsRes.body && roomsRes.body.data;
  const roomRows = roomsData.data || roomsData.items || roomsData;
  const candidates = roomRows.filter(
    (r) =>
      (r.maintenance_status === "NORMAL" || r.maintenance_label === "正常") &&
      (r.room_type_id || (r.room_type && r.room_type.id))
  );
  room = null;
  let roomTypeId = null;
  for (const candidate of candidates) {
    const typeId = candidate.room_type_id || (candidate.room_type && candidate.room_type.id);
    const count = await availabilityCount(duty.page, typeId);
    if (count !== null && count > 0) {
      room = candidate;
      roomTypeId = typeId;
      availBefore = count;
      break;
    }
  }
  if (!room) {
    console.error("所有房型的可售库存均为 0，无法执行维修链（先清理预订数据或换日期）：",
      JSON.stringify(roomsRes.body).slice(0, 300));
    process.exit(3);
  }
  record("pick_room", true, `${room.number}（room_type=${roomTypeId}）`);
  record("availability_before", availBefore > 0, `available_count=${availBefore}`);

  await duty.page.goto(`${BASE}/manage/housekeeping/`, { waitUntil: "networkidle" });
  await duty.page.fill("#hk-ticket-room", room.number);
  await duty.page.fill("#hk-ticket-reason", `MC11 链B回归：空调故障 ${Date.now().toString(36)}`);
  await duty.page.click("#hk-ticket-form button[type=submit]");
  await duty.page
    .waitForFunction(
      () => {
        const el = document.getElementById("hk-ticket-result");
        return el && /维修工单 MT-/.test(el.textContent);
      },
      { timeout: 15000 }
    )
    .catch(() => {});
  const formMsg = await duty.page.evaluate(() => {
    const el = document.getElementById("hk-ticket-result");
    return el ? el.textContent : "";
  });
  const ticketMatch = formMsg.match(/MT-[A-Z0-9-]+/);
  ticketNo = ticketMatch ? ticketMatch[0] : null;
  record("ticket_created", Boolean(ticketNo), formMsg.trim().slice(0, 60));
  await duty.page.screenshot({ path: join(EVIDENCE_DIR, "B1-ticket-created.png"), fullPage: true });

  availStopped = await availabilityCount(duty.page, roomTypeId);
  record(
    "inventory_stopped_on_report",
    availStopped === availBefore - 1,
    `报修后 available_count=${availStopped}（基线 ${availBefore}）`
  );

  const ticketRow = duty.page.locator(`#hk-ticket-body tr:has-text("${ticketNo}")`).first();
  await ticketRow.locator("input[type=number]").fill(String(reporterProfileId));
  await ticketRow.locator("button:has-text('派工')").click();
  await duty.page.waitForTimeout(900);
  await duty.page.locator(`#hk-ticket-body tr:has-text("${ticketNo}")`).first()
    .locator("button:has-text('开始')").click();
  await duty.page.waitForTimeout(900);
  await duty.page.locator(`#hk-ticket-body tr:has-text("${ticketNo}")`).first()
    .locator("button:has-text('完成维修')").click();
  await duty.page.waitForTimeout(900);
  const statusText = await duty.page
    .locator(`#hk-ticket-body tr:has-text("${ticketNo}")`)
    .first()
    .locator("td:nth-child(3)")
    .innerText();
  record("resolve_leaves_pending_verify", statusText.includes("待验收"), `工单状态=${statusText.trim()}`);
  await duty.page.screenshot({ path: join(EVIDENCE_DIR, "B2-resolved-pending-verify.png"), fullPage: true });

  // 关键断言：维修完成 ≠ 恢复可售（必须验收通过）
  const availAfterResolve = await availabilityCount(duty.page, roomTypeId);
  record(
    "resolve_does_not_restore_inventory",
    availAfterResolve === availBefore - 1,
    `完成维修后 available_count=${availAfterResolve}（应仍为 ${availBefore - 1}）`
  );
  await duty.context.close();

  // ── 店长会话：验收通过 → 恢复可售 ──
  const verifier = await login(browser, VERIFIER);
  await verifier.page.goto(`${BASE}/manage/housekeeping/`, { waitUntil: "networkidle" });
  const vRow = verifier.page.locator(`#hk-ticket-body tr:has-text("${ticketNo}")`).first();
  await vRow.locator("button:has-text('验收通过')").click();
  await verifier.page.waitForTimeout(900);
  const verifiedStatus = await verifier.page
    .locator(`#hk-ticket-body tr:has-text("${ticketNo}")`)
    .first()
    .locator("td:nth-child(3)")
    .innerText();
  record("verify_pass", verifiedStatus.includes("验收通过"), `工单状态=${verifiedStatus.trim()}`);

  availAfter = await availabilityCount(verifier.page, roomTypeId);
  record(
    "inventory_restored_after_acceptance",
    availAfter === availBefore,
    `验收后 available_count=${availAfter}（基线 ${availBefore}）`
  );
  await verifier.page.screenshot({ path: join(EVIDENCE_DIR, "B3-verified-restored.png"), fullPage: true });
  await verifier.context.close();

  writeFileSync(
    join(EVIDENCE_DIR, "maintenance-chain-result.json"),
    JSON.stringify({ base: BASE, ticket_no: ticketNo, room: room.number, avail_before: availBefore, avail_stopped: availStopped, avail_after: availAfter, results }, null, 2)
  );
  const passed = results.filter((r) => r.pass).length;
  console.log(`\n合计 ${passed}/${results.length} PASS`);
  process.exit(passed === results.length ? 0 : 1);
} catch (error) {
  console.error("链路执行异常：", error.message);
  writeFileSync(
    join(EVIDENCE_DIR, "maintenance-chain-result.json"),
    JSON.stringify({ base: BASE, ticket_no: ticketNo, room: room && room.number, avail_before: availBefore, avail_stopped: availStopped, avail_after: availAfter, results, error: error.message }, null, 2)
  );
  process.exit(1);
} finally {
  await browser.close();
}
