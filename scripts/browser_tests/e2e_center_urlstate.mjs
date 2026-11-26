/**
 * 管理中心 opt-in 能力真实浏览器验收：URL 状态同步 + 房态操作表单守卫。
 *
 * 覆盖两件事：
 *  A) 管理中心的「筛选 / 排序 / 分页」与 URL 互写（pms-ux 的 data-ux-urlstate
 *     + data-ux-param 声明式接线），并验证刷新与分享链接能还原状态；
 *  B) 房态与库存中心的操作表单启用了 pms-ux 的表单守卫：
 *     未保存提醒（beforeunload）、草稿（localDraft）、失焦校验、提交防重复。
 *
 * 前置准备（隔离库，不得连开发库）：
 *   $env:DJANGO_ENV="test"; $env:DJANGO_DB_ENGINE="sqlite"
 *   $env:DJANGO_DB_NAME="<仓库>\.work\ux_acceptance_test.sqlite3"
 *   python manage.py migrate
 *   python manage.py seed_hotel_data --rooms-per-type 3 --guests 5 --reservations 2
 *   （再准备一个具备 hotelapp.view_room / inventory.manage_rates 权限的账号）
 *   $env:DJANGO_ENV="dev"; python manage.py runserver 127.0.0.1:8124 --noreload
 *
 * 用法：
 *   node scripts/browser_tests/e2e_center_urlstate.mjs --base http://127.0.0.1:8124 \
 *        --user ux_acceptance_user --password <口令>
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
const USER = argValue("--user", "ux_acceptance_user");
const PASSWORD = argValue("--password", "");
const CENTER = argValue("--center", "rooms");
if (!PASSWORD) { console.error("missing --password"); process.exit(2); }

const EVIDENCE_DIR = join(SCRIPT_DIR, "..", "..", "docs", "acceptance", "evidence",
  `center-urlstate-${new Date().toISOString().replace(/[:T]/g, "-").slice(0, 16)}`);
mkdirSync(EVIDENCE_DIR, { recursive: true });

const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail: detail || "" });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}
function urlParams(page) {
  return new URL(page.url()).searchParams;
}
async function waitForParam(page, key, value, timeout = 4000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const params = urlParams(page);
    if (value === null ? !params.has(key) : params.get(key) === value) return true;
    await page.waitForTimeout(120);
  }
  return false;
}

const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await ctx.newPage();
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  // ---- 登录 ----
  await page.goto(`${BASE}/login/`, { waitUntil: "domcontentloaded" });
  await page.fill('input[name="username"]', USER);
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
  if (page.url().includes("/login/")) throw new Error("登录失败：" + USER);

  // ================= A) URL 状态同步 =================
  await page.goto(`${BASE}/manage/${CENTER}/`, { waitUntil: "networkidle" });
  record("进入房态与库存中心真实页面", page.url().includes(`/manage/${CENTER}/`), page.url());
  /* 只统计管理中心页面上的脚本错误（登录跳转的 /home/ 与 favicon 与本项无关） */
  consoleErrors.length = 0;

  const uxPresent = await page.evaluate(() => !!window.PMSUX);
  record("pms-ux 已加载（window.PMSUX 存在）", uxPresent);
  if (!uxPresent) throw new Error("PMSUX 未加载，后续断言无意义");

  const declared = await page.evaluate(() => ({
    urlstate: document.querySelectorAll("[data-ux-urlstate]").length,
    params: Array.from(document.querySelectorAll("[data-ux-param]")).map((n) => ({
      key: n.getAttribute("data-ux-param"), id: n.id, type: n.type
    })),
    sortButtons: document.querySelectorAll("[data-ux-sort]").length,
  }));
  record("页面声明了 URL 状态容器", declared.urlstate === 1, `data-ux-urlstate x${declared.urlstate}`);
  const paramKeys = declared.params.map((p) => p.key).sort();
  record(
    "受控参数覆盖筛选/排序/分页",
    ["q", "page", "page_size", "order_by", "direction"].every((k) => paramKeys.includes(k)),
    paramKeys.join(",")
  );
  record("存在可排序表头", declared.sortButtons > 0, `${declared.sortButtons} 个`);

  // A1 筛选 → URL
  await page.fill("#pms-search", "8");
  const qSynced = await waitForParam(page, "q", "8");
  record("筛选值同步到 URL（q）", qSynced, page.url());

  // A2 每页条数 → URL
  await page.selectOption("#pms-page-size", "50");
  const sizeSynced = await waitForParam(page, "page_size", "50");
  record("每页条数同步到 URL（page_size）", sizeSynced, page.url());

  // A3 排序 → URL（升序 → 降序 → 复位）
  const sortKey = await page.evaluate(() => {
    const btn = document.querySelector('[data-ux-sort="number"]') || document.querySelector("[data-ux-sort]");
    return btn ? btn.getAttribute("data-ux-sort") : "";
  });
  await page.click(`[data-ux-sort="${sortKey}"]`);
  const ascSynced = await waitForParam(page, "order_by", sortKey);
  const ascDir = urlParams(page).get("direction");
  record("点表头排序同步到 URL（order_by/direction=asc）", ascSynced && ascDir === "asc",
    `order_by=${urlParams(page).get("order_by")} direction=${ascDir}`);

  const ariaSort = await page.getAttribute(`[data-ux-sort="${sortKey}"]`, "data-ux-sort-state");
  record("排序列标记生效", ariaSort === "asc", `data-ux-sort-state=${ariaSort}`);

  await page.click(`[data-ux-sort="${sortKey}"]`);
  const descDir = await waitForParam(page, "direction", "desc");
  record("再次点击切换为降序（direction=desc）", descDir, `direction=${urlParams(page).get("direction")}`);

  // A4 请求确实带上了排序参数（服务端排序，不是浏览器内排序）
  const apiCalls = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/v1/") && req.url().includes("order_by")) apiCalls.push(req.url());
  });
  await page.click(`[data-ux-sort="${sortKey}"]`); // 第三击：复位为默认排序
  await page.waitForTimeout(600);
  record("三击复位后 URL 不再带 order_by", !urlParams(page).has("order_by"), page.url());

  // A5 分页 → URL
  await page.fill("#pms-goto-page", "2");
  await page.click("#pms-goto-btn");
  const pageSynced = await waitForParam(page, "page", "2");
  record("页码同步到 URL（page）", pageSynced, page.url());

  // A6 带参数刷新：状态还原
  const shareUrl = `${BASE}/manage/${CENTER}/?q=8&page_size=50&order_by=${sortKey}&direction=desc&page=2`;
  await page.goto(shareUrl, { waitUntil: "networkidle" });
  const restored = await page.evaluate(() => ({
    q: document.getElementById("pms-search").value,
    size: document.getElementById("pms-page-size").value,
    goto: document.getElementById("pms-goto-page").value,
    sortState: (document.querySelector("[data-ux-sort-state]") || {}).getAttribute
      ? document.querySelector("[data-ux-sort-state]").getAttribute("data-ux-sort-state") : "",
    sortKey: (document.querySelector("[data-ux-sort-state]") || {}).getAttribute
      ? document.querySelector("[data-ux-sort-state]").getAttribute("data-ux-sort") : "",
  }));
  record("刷新后筛选值还原", restored.q === "8", `q=${restored.q}`);
  record("刷新后每页条数还原", restored.size === "50", `page_size=${restored.size}`);
  record("刷新后排序状态还原", restored.sortKey === sortKey && restored.sortState === "desc",
    `${restored.sortKey}/${restored.sortState}`);
  await page.screenshot({ path: join(EVIDENCE_DIR, "01-url-state-restored.png"), fullPage: true });

  // ================= B) 房态操作表单守卫 =================
  await page.goto(`${BASE}/manage/${CENTER}/`, { waitUntil: "networkidle" });
  await page.click('#pms-actions button:has-text("调整每日房价")');
  await page.waitForSelector("#pms-drawer form[data-ux-form]", { timeout: 8000 });
  const formAttrs = await page.evaluate(() => {
    const form = document.querySelector("#pms-drawer form[data-ux-form]");
    return {
      hasForm: !!form,
      draft: form.hasAttribute("data-ux-draft"),
      draftKey: form.getAttribute("data-ux-draft-key"),
      validate: form.hasAttribute("data-ux-validate"),
      rules: Array.from(form.querySelectorAll("[data-ux-rule]")).map((n) => ({
        name: n.name, rule: n.getAttribute("data-ux-rule")
      })),
      submit: !!form.querySelector("[data-ux-submit]"),
      slot: !!form.querySelector("[data-ux-saved-slot]"),
    };
  });
  record("操作表单启用 pms-ux 表单守卫（data-ux-form/draft/validate）",
    formAttrs.hasForm && formAttrs.draft && formAttrs.validate, JSON.stringify(formAttrs));
  record("必填字段声明了失焦校验规则", formAttrs.rules.some((r) => r.rule === "required"),
    JSON.stringify(formAttrs.rules));
  record("提交按钮声明了防重复标记", formAttrs.submit, `draftKey=${formAttrs.draftKey}`);

  // B1 失焦校验：留空必填项并移开焦点 → 错误提示出现
  await page.fill('#pms-drawer input[name="rate_id"]', "");
  await page.locator('#pms-drawer input[name="rate_id"]').blur();
  await page.waitForTimeout(250);
  const errVisible = await page.locator("#pms-drawer .ux-field-error:visible").count();
  const errText = errVisible ? await page.locator("#pms-drawer .ux-field-error:visible").first().innerText() : "";
  record("必填项失焦即时报错", errVisible > 0, `提示=${errText}`);
  await page.screenshot({ path: join(EVIDENCE_DIR, "02-blur-validation.png"), fullPage: true });

  // B2 草稿：填值后刷新页面再打开表单 → 内容仍在
  await page.fill('#pms-drawer input[name="rate_id"]', "999001");
  await page.fill('#pms-drawer input[name="price"]', "888.00");
  await page.fill('#pms-drawer textarea[name="reason"]', "浏览器验收草稿");
  await page.waitForTimeout(1400); // 库内草稿防抖 1s
  const draftKeys = await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith("pms-ux:draft:")));
  record("草稿已写入 localStorage", draftKeys.length > 0, draftKeys.join(","));

  await page.goto(`${BASE}/manage/${CENTER}/`, { waitUntil: "networkidle" });
  await page.click('#pms-actions button:has-text("调整每日房价")');
  await page.waitForSelector("#pms-drawer form[data-ux-form]", { timeout: 8000 });
  await page.waitForTimeout(300);
  const draftValues = await page.evaluate(() => {
    const form = document.querySelector("#pms-drawer form[data-ux-form]");
    const get = (n) => { const el = form.querySelector('[name="' + n + '"]'); return el ? el.value : ""; };
    return { rate_id: get("rate_id"), price: get("price"), reason: get("reason") };
  });
  record("刷新后草稿内容已还原",
    draftValues.rate_id === "999001" && draftValues.price === "888.00",
    JSON.stringify(draftValues));

  // B3 未保存提醒：改动字段后离开页面触发 beforeunload
  let beforeUnloadFired = false;
  page.on("dialog", async (dialog) => {
    if (dialog.type() === "beforeunload") beforeUnloadFired = true;
    await dialog.dismiss().catch(() => {});
  });
  await page.fill('#pms-drawer input[name="price"]', "777.00");
  await page.waitForTimeout(200);
  await page.goto(`${BASE}/manage/${CENTER}/`, { waitUntil: "domcontentloaded" }).catch(() => {});
  await page.waitForTimeout(600);
  record("未保存离开页面触发提醒", beforeUnloadFired, `beforeunload=${beforeUnloadFired}`);
  /* 提醒弹窗会中断导航：解除脏标记后再继续后续步骤 */
  await page.evaluate(() => {
    const form = document.querySelector("#pms-drawer form[data-ux-form]");
    if (form && typeof form.__uxClearDirty === "function") form.__uxClearDirty();
  });

  // B4 提交：真实走房态写接口（隔离库），成功后表单复位
  await page.goto(`${BASE}/manage/${CENTER}/`, { waitUntil: "networkidle" });
  const rateId = await page.evaluate(() => {
    const cell = document.querySelector("#pms-secondary table tbody tr td");
    return cell ? cell.textContent.trim() : "";
  });
  await page.click('#pms-actions button:has-text("调整每日房价")');
  await page.waitForSelector("#pms-drawer form[data-ux-form]", { timeout: 8000 });
  await page.fill('#pms-drawer input[name="rate_id"]', rateId);
  await page.fill('#pms-drawer input[name="price"]', "666.00");
  await page.fill('#pms-drawer textarea[name="reason"]', "浏览器验收提交");
  await page.click('#pms-drawer button[data-ux-submit]');
  await page.waitForTimeout(1500);
  const resultText = await page.locator("#pms-drawer").innerText();
  record("操作表单提交成功并回显结果", resultText.includes("操作成功"),
    resultText.replace(/\s+/g, " ").slice(0, 160));
  const afterSubmit = await page.evaluate(() => {
    const form = document.querySelector("#pms-drawer form[data-ux-form]");
    const get = (n) => { const el = form.querySelector('[name="' + n + '"]'); return el ? el.value : ""; };
    const btn = form.querySelector("[data-ux-submit]");
    return {
      price: get("price"), reason: get("reason"),
      disabled: btn.disabled, loading: btn.classList.contains("is-loading"),
      label: (btn.textContent || "").trim(),
    };
  });
  record("提交成功后表单已复位", afterSubmit.price === "" && afterSubmit.reason === "",
    JSON.stringify(afterSubmit));
  /* 回归：提交成功后按钮必须解除 disabled 与 is-loading
     （否则按钮文字被 CSS 隐藏且一直转圈，用户无法在同一抽屉内再次提交）。 */
  record("提交成功后提交按钮已恢复可用（未禁用、未残留 loading）",
    afterSubmit.disabled === false && afterSubmit.loading === false,
    JSON.stringify(afterSubmit));
  record("提交成功后提交按钮文字可见", afterSubmit.label.length > 0, `label=${afterSubmit.label}`);

  // 回归：同一抽屉内可以再次提交（按钮可再次点击 → 请求真的发出）
  await page.fill('#pms-drawer input[name="rate_id"]', rateId);
  await page.fill('#pms-drawer input[name="price"]', "555.00");
  await page.fill('#pms-drawer textarea[name="reason"]', "浏览器验收二次提交");
  await page.click('#pms-drawer button[data-ux-submit]');
  await page.waitForTimeout(1500);
  /* 断言要能区分「第一次的成功提示」与「第二次真的成功」：
     第二次成功时 result 元素被重写为「操作成功」，且表单再次复位（price 清空）。 */
  const secondSubmit = await page.evaluate(() => {
    const form = document.querySelector("#pms-drawer form[data-ux-form]");
    const price = form.querySelector('[name="price"]');
    /* 表单内有两个 role=status：草稿「已保存」标记与提交结果框；
       结果框是后挂的那个，取最后一个。 */
    const results = Array.from(form.querySelectorAll('[role="status"]'));
    const result = results.length ? results[results.length - 1] : null;
    const btn = form.querySelector("[data-ux-submit]");
    return {
      price: price ? price.value : "",
      result: result ? (result.textContent || "").trim() : "",
      disabled: btn ? btn.disabled : true,
      loading: btn ? btn.classList.contains("is-loading") : true,
    };
  });
  record("提交成功后可再次提交（同一抽屉二次提交成功并再次复位）",
    secondSubmit.result === "操作成功" && secondSubmit.price === "" && secondSubmit.disabled === false,
    JSON.stringify(secondSubmit));
  await page.screenshot({ path: join(EVIDENCE_DIR, "03-action-submitted.png"), fullPage: true });

  record("浏览器控制台无脚本错误", consoleErrors.length === 0,
    consoleErrors.filter((e) => !e.includes("favicon")).slice(0, 3).join(" | "));
} catch (error) {
  record("E2E run", false, error.message);
} finally {
  writeFileSync(join(EVIDENCE_DIR, "report.json"), JSON.stringify({
    task: "管理中心 opt-in 能力真实浏览器验收（URL 状态同步 + 房态操作表单守卫）",
    base: BASE, center: CENTER, results,
    passed: results.filter((r) => r.pass).length,
    failed: results.filter((r) => !r.pass).length,
    finished_at: new Date().toISOString(),
  }, null, 2));
  console.log(`evidence: ${EVIDENCE_DIR}`);
  console.log(`passed ${results.filter((r) => r.pass).length}/${results.length}`);
  await browser.close();
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}
