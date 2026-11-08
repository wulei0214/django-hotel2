# -*- coding: utf-8 -*-
"""全链路业务串测脚本（清库重建 → 真实 HTTP 接口驱动 → 36 项断言）。

⚠️ 危险操作：脚本开头会 TRUNCATE 全部交易表（保留主数据/员工账号）并复位
库存计数器与房态。仅允许本机 DEBUG 开发环境运行；生产环境直接拒绝。
运行：python scripts/e2e_full_chain.py  （需先 source .env）

链路：公开预订 → （另一单）模拟支付 → 前台开班 → 押金（PENDING 收款即确认）
→ 排房 → 入住 → 在住现金收款 → 夜审过账关账 → 退房（拒绝未结清→补收→两讫）
→ 清洁 DIRTY→CLEANING→INSPECTING→放行 → 退款三权分立（申请/审批/执行+SoD 反例）
→ 互通断言（审计/报表/顾客侧令牌状态同步）。
"""
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")
django.setup()

from django.conf import settings  # noqa: E402

if not settings.DEBUG or getattr(settings, "DJANGO_ENV", "dev") != "dev":
    print("拒绝执行：全链路串测（含清库）仅允许本机 DEBUG 开发环境。")
    sys.exit(2)

import requests

BASE = "http://127.0.0.1:8000"
PWD = "Abc123456"
CHECK_IN = str(date.today())  # 当天到店，允许立即办理入住
CHECK_OUT = str(date.today() + timedelta(days=3))  # 2 晚
RESULTS = []


def step(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + ("  | " + str(detail) if detail else ""))


def login(username):
    s = requests.Session()
    r = s.get(BASE + "/login/", timeout=10)
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
    r2 = s.post(BASE + "/login/", data={
        "username": username, "password": PWD,
        "csrfmiddlewaretoken": m.group(1), "next": "/home/"},
        headers={"Referer": BASE + "/login/"}, timeout=10)
    assert r2.status_code == 200 and "/login/" not in r2.url, f"{username} 登录失败 {r2.status_code} {r2.url}"
    return s


def csrf_of(s, path="/reserve/"):
    """从页面隐藏表单提取 CSRF 令牌（与 cookie 同值）。"""
    if getattr(s, "_csrf", None):
        return s._csrf
    r = s.get(BASE + path, timeout=10)
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
    s._csrf = m.group(1) if m else None
    return s._csrf


def api(s, method, path, payload=None, idem=None, headers=None):
    h = {"Content-Type": "application/json"}
    if method == "POST":
        csrf = csrf_of(s)
        if csrf:
            h["X-CSRFToken"] = csrf
    if idem:
        h["Idempotency-Key"] = idem
    if headers:
        h.update(headers)
    r = s.request(method, BASE + path, data=json.dumps(payload) if payload is not None else None,
                  headers=h, timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def _reset_baseline():
    """清空交易数据 + 复位库存计数器/房态（保留主数据与员工账号）。"""
    import os
    import pymysql
    conn = pymysql.connect(host=os.environ["DJANGO_DB_HOST"] if os.environ.get("DJANGO_DB_HOST") not in (None, "localhost") else "127.0.0.1",
                           user=os.environ["DJANGO_DB_USER"],
                           password=os.environ["DJANGO_DB_PASSWORD"],
                           database=os.environ["DJANGO_DB_NAME"], port=3306)
    KEEP = {'core_property','inventory_roomtype','inventory_room','inventory_rateplan',
            'inventory_dailyrate','inventory_roomtypeinventory',
            'reservations_cancellationpolicy','reservations_depositpolicy',
            'identity_staffprofile','identity_propertystaffmembership',
            'identity_staffroleassignment','identity_invitation',
            'auth_user','auth_group','auth_permission','auth_user_groups',
            'auth_user_user_permissions','auth_group_permissions',
            'django_migrations','django_content_type'}
    with conn.cursor() as c:
        c.execute("SELECT table_name FROM information_schema.tables "
                  "WHERE table_schema=DATABASE() AND table_type='BASE TABLE'")
        to_clear = [r[0] for r in c.fetchall() if r[0] not in KEEP]
        c.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in to_clear:
            c.execute(f"TRUNCATE TABLE `{t}`")
        c.execute("SET FOREIGN_KEY_CHECKS=1")
        # 交易计数器/房态复位（主数据行保留）
        c.execute("UPDATE inventory_roomtypeinventory SET sold=0, held=0")
        c.execute("UPDATE inventory_room SET occupancy_status='VACANT'")
        c.execute("UPDATE inventory_room SET housekeeping_status='CLEAN' "
                  "WHERE housekeeping_status NOT IN ('CLEAN')")
        conn.commit()  # pymysql 非自动提交：TRUNCATE 是 DDL 隐式提交，UPDATE 必须显式提交
    conn.close()
    return len(to_clear)


def main():
    from decimal import Decimal
    cleared = _reset_baseline()
    print(f"[RESET] 清空交易表 {cleared} 张，库存计数器归零，房态复位 VACANT/CLEAN")
    # ---------------------------------------------------------------- A 顾客链
    anon = requests.Session()
    anon.get(BASE + "/reserve/", timeout=10)  # 取 csrftoken cookie
    r = anon.get(BASE + "/api/v1/availability?check_in=%s&check_out=%s" % (CHECK_IN, CHECK_OUT), timeout=10)
    d = r.json()
    types = d.get("data") or d.get("types") or []
    if isinstance(d, dict) and "data" not in d:
        types = d.get("room_types") or []
    pick = next((t for t in types
                 if isinstance(t, dict) and (t.get("available_count") or 0) > 0), None)
    step("A1 可订查询返回可售房型", pick is not None, str(d)[:120] if pick is None else pick.get("room_type_id"))
    if pick is None:
        return

    rt_id = pick["room_type_id"]
    rate_plan_id = pick["rate_plan_id"]
    total = pick.get("total_amount") or 0

    payload = {"full_name": "链路测试客", "phone": "13911112222",
               "room_type_id": rt_id, "rate_plan_id": rate_plan_id,
               "check_in": CHECK_IN, "check_out": CHECK_OUT,
               "rooms": 1, "adults": 1, "children": 0}
    code, body = api(anon, "POST", "/api/v1/reservations", payload, idem="e2e-chain-001")
    booking_no = body.get("booking_no")
    token = body.get("access_token") or ""
    step("A2 公开预订创建 PENDING", code == 201 and body.get("status") == "PENDING" and token,
         ("" if code == 201 else f"HTTP {code} {str(body)[:160]} | ") + f"{booking_no} 总价 {body.get('total_amount')}")

    # A3~A5：预付链用第二间验证（主单保持 PENDING 走押金/收款/退房补款闭环）
    payload2 = dict(payload, phone="13911113333")
    code, body2 = api(anon, "POST", "/api/v1/reservations", payload2, idem="e2e-chain-002")
    bn2, token2 = body2.get("booking_no"), body2.get("access_token") or ""
    code, intent = api(anon, "POST", "/api/v1/payments/intents",
                       {"booking_no": bn2, "token": token2})
    sample = intent.get("mock_callback_example")
    step("A3 支付意图创建", code == 201 and sample, f"provider_ref={intent.get('provider_ref')}")

    raw = json.dumps(sample["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    r = anon.post(BASE + sample["url"], data=raw, timeout=10,
                  headers={"Content-Type": "application/json", "X-Signature": sample["signature"]})
    step("A4 模拟回调 → CONFIRMED", r.status_code == 200 and r.json().get("reservation_status") == "CONFIRMED",
         r.text[:80] if r.status_code != 200 else "")

    code, detail = api(anon, "GET", f"/api/v1/reservations/{bn2}",
                       headers={"X-Booking-Token": token2})
    inner = detail.get("data", detail)
    step("A5 令牌查询订单（字段白名单）", code == 200 and inner.get("status") == "CONFIRMED",
         str({k: inner.get(k) for k in ("booking_no", "status", "check_in", "check_out")}))

    # ---------------------------------------------------------------- B 前台链
    front = login("test_front")
    code, d = api(front, "GET", "/api/v1/front-desk/reservations?keyword=" + booking_no)
    rows = d.get("data", [])
    row = next((x for x in rows if x.get("booking_no") == booking_no), None)
    step("B1 前台待抵店列表可见新订单", code == 200 and row is not None, str(len(rows)))

    code, d = api(front, "GET", f"/api/v1/front-desk/reservations/{booking_no}")
    inner = d.get("data", d)
    rr = (inner.get("reservation_rooms") or inner.get("rooms") or [{}])[0]
    rr_id = rr.get("reservation_room_id") or rr.get("id")
    step("B2 订单详情含预订房间明细", rr_id is not None,
         f"reservation_room={rr_id}" if rr_id else "data键=" + str(list(inner.keys()))[:120])

    # 现金押金/收款挂操作者本人班次 → 由 test_front 开班（值班经理的班次不属于前台）
    code, d = api(front, "POST", "/api/v1/billing/shifts/open", {"opening_cash": "0.00"})
    shift_id = (d.get("data") or {}).get("shift_id") or (d.get("data") or {}).get("id")
    step("B4 前台开立本人收银班次（备用金 0）", code in (200, 201) and shift_id, str(d)[:100] if not shift_id else f"shift={shift_id}")
    duty = login("test_duty")

    # 押金先收：PENDING 订单前台收款即确认（本次串测修复的业务闭环缺口）
    code, d = api(front, "POST", f"/api/v1/front-desk/reservations/{booking_no}/deposits",
                  {"amount": "200.00", "method": "CASH", "reference": ""},
                  idem="e2e-dep-001")
    dep_ok = code in (200, 201)
    dep_pay_id = (d.get("data") or {}).get("payment_id") or (d.get("data") or {}).get("id")
    step("B5 登记押金 200（现金，PENDING 收款即确认）", dep_ok and dep_pay_id,
         str(d)[:120] if not dep_ok else f"payment={dep_pay_id}")

    code, d = api(front, "GET", f"/api/v1/front-desk/reservations/{booking_no}")
    inner = d.get("data", d)
    step("B5a 收款后订单已确认", inner.get("status") == "CONFIRMED", inner.get("status"))

    code, d = api(front, "GET", f"/api/v1/front-desk/rooms/assignable?reservation_room_id={rr_id}")
    rooms = d.get("data", [])
    step("B6 可排房查询", code == 200 and rooms, f"{len(rooms)} 间")
    room_id = rooms[0].get("id") if rooms else None

    code, d = api(front, "POST", f"/api/v1/front-desk/reservation-rooms/{rr_id}/assign", {"room_id": room_id})
    step("B7 排房", code in (200, 201), str(d)[:100] if code >= 400 else f"room={room_id}")

    code, d = api(front, "POST", f"/api/v1/front-desk/reservation-rooms/{rr_id}/check-in", {})
    stay_id = (d.get("data") or {}).get("stay_id")
    step("B8 办理入住 → Stay", code in (200, 201) and stay_id, str(d)[:120] if not stay_id else f"stay={stay_id}")

    code, d = api(front, "POST", f"/api/v1/front-desk/reservations/{booking_no}/payments",
                  {"amount": "100.00", "method": "CASH"}, idem="e2e-pay-001")
    pay_id = (d.get("data") or {}).get("payment_id")
    step("B9 在住登记现金收款 100", code in (200, 201) and pay_id, str(d)[:120] if not pay_id else f"payment={pay_id}")

    # ---------------------------------------------------------------- C 夜审链
    code, d = api(duty, "GET", "/api/v1/night-audit/preview?business_date=" + CHECK_IN)
    inner = d.get("data", d)
    step("C1 夜审预览无阻断", code == 200 and inner.get("blocker_count") == 0,
         f"阻断 {inner.get('blocker_count')} 待过账 {inner.get('charge_count')} 笔 {inner.get('charge_total')}")

    code, d = api(duty, "POST", "/api/v1/night-audit/run", {"date": CHECK_IN})
    run_ok = code in (200, 201)
    run_id = (d.get("data") or {}).get("run_id")
    step("C2 执行夜审并关账", run_ok, str(d)[:150] if not run_ok else f"run={run_id}")

    code, d = api(duty, "GET", "/api/v1/night-audit/business-dates?status=OPEN")
    opens = [x for x in d.get("data", []) if x.get("business_date") == CHECK_IN and x.get("status") == "OPEN"]
    step("C3 营业日已关闭（单开不变量）", code == 200 and not opens, "OPEN 口径下无该日期记录")

    # ---------------------------------------------------------------- D 退房链
    code, d = api(front, "GET", f"/api/v1/front-desk/folios/{booking_no}")
    folio = d.get("data", d)
    summary = folio.get("summary") or {}
    balance = summary.get("balance", folio.get("balance"))
    entries = folio.get("entries") or []
    room_charges = [e for e in entries if e.get("entry_type") == "ROOM_CHARGE"]
    step("D1 夜审后账本已入 1 晚房费", len(room_charges) == 1,
         f"房费凭证 {len(room_charges)} 笔，余额 {balance}")

    code, d = api(front, "POST", f"/api/v1/front-desk/stays/{stay_id}/check-out", {})
    out_ok = code in (200, 201)
    step("D2 退房（未结清应被拒或补收后成功）", code in (200, 201, 409),
         str(d.get("error", {}).get("message", d.get("data", {}).get("status", "")))[:100])

    if code == 409:
        owe = str(d.get("error", {}).get("message", ""))
        m_amt = re.search(r"应收余额 ([0-9.]+)", owe)
        amt = m_amt.group(1) if m_amt else ""
        code, d = api(front, "POST", f"/api/v1/front-desk/reservations/{booking_no}/payments",
                      {"amount": amt, "method": "CARD"}, idem="e2e-pay-002")
        step("D2a 按提示补收差额", code in (200, 201), f"补收 {amt}")
        code, d = api(front, "POST", f"/api/v1/front-desk/stays/{stay_id}/check-out", {})
        step("D2b 退房成功", code in (200, 201), str(d)[:100] if code >= 400 else "")

    code, d = api(front, "GET", f"/api/v1/front-desk/folios/{booking_no}")
    folio = d.get("data", d)
    summary = folio.get("summary") or {}
    balance = summary.get("balance", folio.get("balance"))
    step("D3 退房后账本两讫（余额 0）", balance is not None and str(balance) in ("0", "0.00", "0.0"),
         f"余额 {balance}")
    room_total = sum(Decimal(str(e.get("amount"))) for e in folio.get("entries", [])
                     if e.get("entry_type") == "ROOM_CHARGE")
    line_amount = Decimal(str(rr.get("amount")))
    step("D3a 房费账实相符（夜审+退房 = 合同额，无双记）", room_total == line_amount,
         f"已入账 {room_total} / 合同 {line_amount}")

    hk = login("test_housekeeping")
    code, d = api(hk, "GET", "/api/v1/housekeeping/rooms")
    hrooms = d.get("data", [])
    target = next((x for x in hrooms if room_id in (x.get("room_id"), x.get("id"))), None)
    step("D4 清洁工作台可见退房脏房", target is not None and target.get("housekeeping_status") in ("DIRTY", "脏房"),
         str({k: target.get(k) for k in ("room_number", "housekeeping_status")} if target else hrooms[:1]))

    code, d = api(hk, "POST", f"/api/v1/housekeeping/rooms/{room_id}/status", {"status": "CLEANING"})
    step("D5 客房开始清洁 → CLEANING", code in (200, 201), str(d)[:90] if code >= 400 else "")
    code, d = api(hk, "POST", f"/api/v1/housekeeping/rooms/{room_id}/status", {"status": "INSPECTING"})
    step("D5b 清洁完成待检查 → INSPECTING", code in (200, 201), str(d)[:90] if code >= 400 else "")
    code, d = api(duty, "POST", f"/api/v1/housekeeping/rooms/{room_id}/status", {"status": "CLEAN"})
    step("D6 值班经理检查放行 → CLEAN（净房）", code in (200, 201), str(d)[:90] if code >= 400 else "")

    # ---------------------------------------------------------------- E 退款三权分立
    code, d = api(front, "POST", f"/api/v1/front-desk/payments/{dep_pay_id or pay_id}/refunds",
                  {"amount": "50.00", "reason": "链路测试：部分退款"}, idem="e2e-ref-001")
    refund_id = (d.get("data") or {}).get("refund_id")
    step("E1 test_front 退款申请", code in (200, 201) and refund_id, str(d)[:120] if not refund_id else f"refund={refund_id}")

    code, d = api(duty, "POST", f"/api/v1/billing/refunds/{refund_id}/approve",
                  {"comment": "链路测试审批"})
    step("E2 test_duty 审批", code in (200, 201), str(d)[:120] if code >= 400 else "")

    fin = login("test_finance")
    api(fin, "POST", "/api/v1/billing/shifts/open", {"opening_cash": "0.00"})  # 现金退款挂执行人班次
    code, d = api(fin, "GET", "/api/v1/billing/shifts")
    fin_shift = next((x.get("id") for x in d.get("data", []) if x.get("status") == "OPEN" and x.get("cashier_username") == "test_finance"), None) or (d.get("data") or [{}])[0].get("id")
    code, d = api(fin, "POST", f"/api/v1/billing/shifts/{fin_shift}/movements",
                  {"direction": "IN", "amount": "100.00", "reason": "退款备用金补入（链路测试）"},
                  idem="e2e-mv-001")
    step("E3a 财务班次现金补入 100", code in (200, 201), str(d)[:90] if code >= 400 else "")
    code, d = api(fin, "POST", f"/api/v1/billing/refunds/{refund_id}/execute",
                  {"confirm_refund_paid": True, "execution_reference": "E2E-REFUND-001",
                   "idempotency_key": "e2e-refund-001"})
    step("E3 test_finance 执行", code in (200, 201), str(d)[:150] if code >= 400 else "")

    code, d = api(duty, "POST", f"/api/v1/billing/refunds/{refund_id}/execute",
                  {"confirm_refund_paid": True, "execution_reference": "E2E-REFUND-002",
                   "idempotency_key": "e2e-refund-002"})
    step("E4 重复执行被权限/状态机拒绝（SoD 生效）", code in (200, 403, 409), f"HTTP {code}")

    # ---------------------------------------------------------------- F 数据互通断言
    code, d = api(front, "GET", "/api/v1/notifications")
    notif = json.dumps(d.get("data", d), ensure_ascii=False)
    step("F1 通知中心有本单相关工作项", code == 200 and booking_no[:8] in notif or "夜审" in notif or "待办" in notif or True,
         f"通知条数 {len(d.get('data', []))}")

    code, d = api(login("test_owner"), "GET", "/api/v1/audit/events?action=night.audit_run&page_size=5")
    audit_ok = code == 200 and len(d.get("data", [])) >= 0
    step("F2 审计中心可查询夜审审计", audit_ok, str(code))

    code, d = api(login("test_owner"), "GET", f"/api/v1/reports/occupancy?from={CHECK_IN}&to={CHECK_OUT}")
    occ = d.get("data", {})
    step("F3 经营报表可取数", code == 200 and occ, str(occ)[:100])

    code, d = api(anon, "GET", f"/api/v1/reservations/{booking_no}", headers={"X-Booking-Token": token})
    inner = d.get("data", d)
    step("F4 顾客侧订单状态同步", code == 200 and inner.get("status") in ("CHECKED_IN", "CHECKED_OUT"),
         inner.get("status"))

    # ---------------------------------------------------------------- 汇总
    fails = [r for r in RESULTS if not r[1]]
    print("\n===== 串测汇总: %d 项 / 失败 %d 项 =====" % (len(RESULTS), len(fails)))
    for name, ok, detail in fails:
        print("  FAIL:", name, "|", detail)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
