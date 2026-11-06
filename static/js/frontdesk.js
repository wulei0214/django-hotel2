(function () {
    "use strict";

    var root = document.getElementById("front-desk");
    if (!root) return;

    var apiRoot = root.dataset.apiRoot || "/api/v1/front-desk/";
    var state = {
        tab: "workbench",
        page: 1,
        pageSize: 20,
        query: "",
        date: "",
        assigned: "all",
        readiness: "ALL",
        currentBooking: null,
        paymentKey: null,
        depositKey: null,
        refundKey: null,
        total: 0,
        cashierShift: null
    };

    var $ = function (id) { return document.getElementById(id); };
    var tbody = $("fd-table-body");
    var drawer = $("fd-drawer");
    var toastTimer = null;
    var lastDrawerTrigger = null;

    function escapeText(value) {
        return value === null || value === undefined ? "" : String(value);
    }

    function element(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = escapeText(text);
        return node;
    }

    function money(value, currency) {
        if (value === null || typeof value === "undefined") return "需核对币种";
        var amount = Number(value || 0);
        if (!Number.isFinite(amount)) amount = 0;
        return (currency || "CNY") + " " + amount.toFixed(2);
    }

    function renderFolioBalance(value, currency) {
        var balanceNode = $("detail-balance");
        var labelNode = balanceNode.parentElement.querySelector("span");
        var summaryNode = balanceNode.parentElement;
        if (value === null || typeof value === "undefined") {
            summaryNode.classList.remove("is-credit");
            labelNode.textContent = "币种不一致";
            balanceNode.textContent = "请核对各账单币种";
            return;
        }
        var amount = Number(value || 0);
        if (!Number.isFinite(amount)) amount = 0;
        summaryNode.classList.toggle("is-credit", amount < 0);
        if (amount < 0) {
            labelNode.textContent = "客人预存款";
            balanceNode.textContent = money(Math.abs(amount), currency);
        } else if (amount > 0) {
            labelNode.textContent = "待收余额";
            balanceNode.textContent = money(amount, currency);
        } else {
            labelNode.textContent = "已结清";
            balanceNode.textContent = money(0, currency);
        }
    }

    function showToast(message, isError) {
        var node = $("fd-toast");
        node.textContent = message;
        node.classList.toggle("is-error", !!isError);
        node.classList.add("is-visible");
        window.clearTimeout(toastTimer);
        toastTimer = window.setTimeout(function () {
            node.classList.remove("is-visible");
        }, 3200);
    }

    async function requestApi(path, options) {
        options = options || {};
        var method = options.method || "GET";
        var headers = { "Accept": "application/json" };
        var requestOptions = {
            method: method,
            credentials: "same-origin",
            headers: headers,
            cache: "no-store"
        };
        if (method !== "GET") {
            headers["Content-Type"] = "application/json";
            var tokenInput = document.querySelector("#fd-csrf-token-container input[name='csrfmiddlewaretoken']");
            if (tokenInput && tokenInput.value) headers["X-CSRFToken"] = tokenInput.value;
            if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
            requestOptions.body = JSON.stringify(options.payload || {});
        }
        var response;
        try {
            response = await fetch(path.charAt(0) === "/" ? path : apiRoot + path, requestOptions);
        } catch (error) {
            throw new Error("网络连接失败，请检查服务器后重试。");
        }
        var payload;
        try {
            payload = await response.json();
        } catch (error) {
            throw new Error("服务器返回了无法识别的数据。");
        }
        if (!response.ok) {
            if (response.status === 401) {
                window.location.href = "/login/?next=" + encodeURIComponent(window.location.pathname);
                throw new Error("登录状态已失效，正在跳转登录页。");
            }
            var detail = payload && payload.error;
            if (detail && typeof detail === "object") {
                throw new Error(detail.message || "请求失败。");
            }
            throw new Error(typeof detail === "string" ? detail : "请求失败（" + response.status + "）。");
        }
        return payload;
    }

    function api(path) {
        return requestApi(path, { method: "GET" });
    }

    function postApi(path, payload, idempotencyKey) {
        return requestApi(path, {
            method: "POST",
            payload: payload || {},
            idempotencyKey: idempotencyKey || null
        });
    }

    async function loadCashierShift() {
        var status = $("fd-cashier-shift-status");
        var openButton = $("fd-shift-open");
        var closeButton = $("fd-shift-close");
        if (!status || !openButton || !closeButton) return;
        try {
            var payload = await api("/api/v1/billing/shifts?status=OPEN&mine=1&page=1&page_size=10");
            var rows = payload.data || [];
            state.cashierShift = rows.length ? rows[0] : null;
            if (state.cashierShift) {
                status.textContent = "当前班次 #" + state.cashierShift.id +
                    " · 备用金 " + state.cashierShift.opening_cash;
                openButton.hidden = true;
                closeButton.hidden = false;
            } else {
                status.textContent = "未开班；现金收款/押金登记前必须先开班。";
                openButton.hidden = false;
                closeButton.hidden = true;
            }
        } catch (error) {
            state.cashierShift = null;
            status.textContent = "班次读取失败：" + error.message;
            openButton.disabled = true;
            closeButton.disabled = true;
        }
    }

    function bindCashierShiftControls() {
        var openButton = $("fd-shift-open");
        var closeButton = $("fd-shift-close");
        if (openButton) openButton.addEventListener("click", async function () {
            var openingCash = window.prompt("请输入开班备用金金额", "0.00");
            if (openingCash === null) return;
            openButton.disabled = true;
            try {
                await postApi("/api/v1/billing/shifts/open",
                    {opening_cash: openingCash.trim()}, makeIdempotencyKey("fd-shift-open"));
                await loadCashierShift();
                showToast("收银班次已开立。", false);
            } catch (error) {
                showToast(error.message, true);
            } finally { openButton.disabled = false; }
        });
        if (closeButton) closeButton.addEventListener("click", async function () {
            if (!state.cashierShift) return;
            var actualCash = window.prompt("请输入实盘现金金额");
            if (actualCash === null) return;
            var varianceReason = window.prompt("如有差异，请填写原因；无差异可留空", "");
            if (varianceReason === null) return;
            closeButton.disabled = true;
            try {
                await postApi("/api/v1/billing/shifts/" + state.cashierShift.id + "/close",
                    {actual_cash: actualCash.trim(), variance_reason: varianceReason.trim()},
                    makeIdempotencyKey("fd-shift-close"));
                await loadCashierShift();
                showToast("收银班次已关班并完成现金核对。", false);
            } catch (error) {
                showToast(error.message, true);
            } finally { closeButton.disabled = false; }
        });
    }

    function setLoading(message) {
        tbody.replaceChildren();
        var row = element("tr");
        var cell = element("td", "fd-table-loading", message || "正在加载…");
        cell.colSpan = 6;
        row.appendChild(cell);
        tbody.appendChild(row);
    }

    function showTableError(message) {
        tbody.replaceChildren();
        var row = element("tr");
        var cell = element("td", "fd-table-error", message);
        cell.colSpan = 6;
        row.appendChild(cell);
        tbody.appendChild(row);
        showToast(message, true);
    }

    function badgeFor(item, tab) {
        if (tab === "legacy") return [item.status_label || "历史记录", "gray"];
        if (tab === "rooms") {
            if (item.is_ready) return ["可入住净房", "green"];
            if (item.housekeeping_status === "INSPECTING") return ["待检查放行", "amber"];
            if (item.maintenance_status !== "NORMAL") return ["维修/停售", "red"];
            if (item.occupancy_status === "OCCUPIED") return ["在住", "blue"];
            if (item.housekeeping_status === "DIRTY" || item.housekeeping_status === "CLEANING") return ["待清洁/清洁中", "amber"];
            return ["不可分配", "gray"];
        }
        if (tab === "departures" || tab === "in-house") {
            var balance = Number(item.folio_summary && item.folio_summary.balance || 0);
            if (balance > 0) return ["余额待结", "red"];
            if (item.stay && item.stay.status === "IN_HOUSE") return ["在住", "green"];
        }
        if (!item.assigned_room) return ["待排房", "amber"];
        if (item.reservation_status === "PENDING") return ["待支付/确认", "amber"];
        if (item.reservation_status === "CONFIRMED") return ["待入住", "blue"];
        if (item.status_label) return [item.status_label, "gray"];
        return ["已分配", "green"];
    }

    function appendCell(row, primary, secondary) {
        var cell = element("td");
        if (primary !== undefined && primary !== null) cell.appendChild(element("span", "fd-cell-primary", primary));
        if (secondary) cell.appendChild(element("span", "fd-cell-secondary", secondary));
        row.appendChild(cell);
        return cell;
    }

    function renderRooms(items) {
        tbody.replaceChildren();
        var row = element("tr");
        var cell = element("td");
        cell.colSpan = 6;
        var grid = element("div", "fd-room-grid");
        items.forEach(function (room) {
            var card = element("div", "fd-room-card");
            card.appendChild(element("b", "", room.number));
            card.appendChild(element("span", "", (room.floor || "楼层未设置") + " · " + (room.room_type ? room.room_type.name : "")));
            var badge = badgeFor(room, "rooms");
            card.appendChild(element("span", "fd-status fd-status-" + badge[1], badge[0]));
            card.appendChild(element("span", "", "占用：" + (room.occupancy_label || room.occupancy_status) + " · 清洁：" + (room.housekeeping_label || room.housekeeping_status)));
            card.appendChild(element("span", "", "维修：" + (room.maintenance_label || room.maintenance_status)));
            grid.appendChild(card);
        });
        cell.appendChild(grid);
        row.appendChild(cell);
        if (!items.length) {
            cell.textContent = "当前筛选条件下没有房间。";
            cell.className = "fd-table-empty";
        }
        tbody.appendChild(row);
        $("fd-page-info").textContent = "共 " + items.length + " 间";
        $("fd-page-prev").disabled = true;
        $("fd-page-next").disabled = true;
    }

    function openDrawer(bookingNo) {
        state.currentBooking = bookingNo;
        lastDrawerTrigger = (document.activeElement instanceof HTMLElement) ? document.activeElement : null;
        drawer.classList.add("is-open");
        drawer.setAttribute("aria-hidden", "false");
        var closeBtn = drawer.querySelector(".fd-drawer-close");
        if (closeBtn) closeBtn.focus();
        $("fd-drawer-title").textContent = "订单详情";
        $("fd-drawer-booking").textContent = bookingNo;
        ["detail-guest-name", "detail-guest-phone", "detail-source", "detail-status",
         "detail-checkin", "detail-checkout", "detail-room-count", "detail-guests",
         "detail-charges", "detail-credits", "detail-balance", "detail-created", "detail-updated"
        ].forEach(function (id) { $(id).textContent = "加载中…"; });
        $("detail-balance").parentElement.classList.remove("is-credit");
        $("detail-balance").parentElement.querySelector("span").textContent = "当前余额";
        $("detail-rooms").replaceChildren();
        $("detail-operations").replaceChildren();
        $("fd-operations-section").hidden = true;
        $("detail-folio-entries").replaceChildren();
        state.paymentKey = null;
        state.depositKey = null;
        $("fd-view-folio").hidden = false;
        loadDetail(bookingNo);
    }

    function closeDrawer() {
        if (!drawer.classList.contains("is-open")) return;
        if (lastDrawerTrigger && document.contains(lastDrawerTrigger)) {
            lastDrawerTrigger.focus();
        }
        lastDrawerTrigger = null;
        drawer.classList.remove("is-open");
        drawer.setAttribute("aria-hidden", "true");
    }

    function operationButton(label, className, handler) {
        var button = element("button", "fd-btn " + (className || "fd-btn-secondary"), label);
        button.type = "button";
        button.addEventListener("click", handler);
        return button;
    }

    async function refreshAfterOperation(bookingNo, message) {
        showToast(message, false);
        await loadDetail(bookingNo);
        await loadOverview();
        await loadList();
        await loadCashierShift();
    }

    async function showAssignableRooms(roomLine, roomNode, trigger) {
        if (roomNode.querySelector(".fd-room-assigner")) return;
        trigger.disabled = true;
        try {
            var payload = await api("rooms/assignable?reservation_room_id=" + encodeURIComponent(roomLine.reservation_room_id));
            var rooms = payload.data || [];
            var chooser = element("div", "fd-room-assigner");
            if (!rooms.length) {
                chooser.appendChild(element("p", "fd-operation-note",
                    (payload.meta && payload.meta.reason) || "当前没有符合条件的可分配净房。"));
                roomNode.appendChild(chooser);
                return;
            }
            var label = element("label", "fd-operation-label", "选择可分配房间");
            var select = element("select", "fd-operation-select");
            rooms.forEach(function (room) {
                var option = element("option", "", room.number + " · " + room.floor + "层 · " + room.room_type);
                option.value = room.id;
                select.appendChild(option);
            });
            label.appendChild(select);
            chooser.appendChild(label);
            var confirmButton = operationButton("确认排房", "fd-btn-primary", async function () {
                confirmButton.disabled = true;
                try {
                    await postApi("reservation-rooms/" + roomLine.reservation_room_id + "/assign", {
                        room_id: Number(select.value)
                    });
                    await refreshAfterOperation(roomLine.booking_no, "已完成排房");
                } catch (error) {
                    showToast(error.message, true);
                    confirmButton.disabled = false;
                }
            });
            chooser.appendChild(confirmButton);
            roomNode.appendChild(chooser);
        } catch (error) {
            showToast(error.message, true);
        } finally {
            trigger.disabled = false;
        }
    }

    function renderPaymentForm(item, content) {
        var summary = item.folio_summary || {};
        if (summary.status !== "OPEN" || !["CONFIRMED", "CHECKED_IN"].includes(item.status)) return;
        var box = element("div", "fd-payment-box");
        box.appendChild(element("h4", "", "登记线下收款"));
        box.appendChild(element("p", "fd-operation-note",
            "仅用于记录已经实际收到的现金、刷卡或转账；本操作不会发起扣款，也不会向银行核验到账。"));
        var form = element("form", "fd-payment-form");
        var amountLabel = element("label", "fd-operation-label", "本次实收金额");
        var amount = element("input", "fd-operation-input");
        amount.type = "number";
        amount.min = "0.01";
        amount.step = "0.01";
        amount.required = true;
        amount.placeholder = "例如 500.00";
        amountLabel.appendChild(amount);
        form.appendChild(amountLabel);

        var methodLabel = element("label", "fd-operation-label", "收款方式");
        var method = element("select", "fd-operation-select");
        [
            ["CASH", "现金"],
            ["CARD", "银行卡 / 刷卡"],
            ["BANK_TRANSFER", "银行转账"]
        ].forEach(function (entry) {
            var option = element("option", "", entry[1]);
            option.value = entry[0];
            method.appendChild(option);
        });
        methodLabel.appendChild(method);
        form.appendChild(methodLabel);
        var submit = element("button", "fd-btn fd-btn-primary", "登记已收款");
        submit.type = "submit";
        form.appendChild(submit);
        box.appendChild(form);

        function resetPaymentKey() {
            state.paymentKey = null;
        }
        amount.addEventListener("input", resetPaymentKey);
        method.addEventListener("change", resetPaymentKey);
        form.addEventListener("submit", async function (event) {
            event.preventDefault();
            var enteredAmount = amount.value.trim();
            if (!enteredAmount || !Number.isFinite(Number(enteredAmount)) || Number(enteredAmount) <= 0) {
                showToast("请输入大于 0 的实收金额。", true);
                return;
            }
            if (method.value === "CASH" && !state.cashierShift) {
                showToast("请先开立本人收银班次，再登记现金收款。", true);
                return;
            }
            if (!window.confirm("请确认这笔款项已经实际收到。系统只登记收款，不会代替 POS、刷卡终端或银行完成收款。")) return;
            if (!state.paymentKey) {
                var nonce = (window.crypto && window.crypto.randomUUID)
                    ? window.crypto.randomUUID()
                    : (Date.now().toString(36) + Math.random().toString(36).slice(2));
                state.paymentKey = "fd-manual-" + nonce;
            }
            submit.disabled = true;
            try {
                await postApi(
                    "reservations/" + encodeURIComponent(item.booking_no) + "/payments",
                    { amount: enteredAmount, method: method.value },
                    state.paymentKey
                );
                state.paymentKey = null;
                await refreshAfterOperation(item.booking_no, "已登记本次实收款项");
            } catch (error) {
                showToast(error.message, true);
                submit.disabled = false;
            }
        });
        content.appendChild(box);
    }


    function makeIdempotencyKey(prefix) {
        var nonce = (window.crypto && window.crypto.randomUUID)
            ? window.crypto.randomUUID()
            : (Date.now().toString(36) + Math.random().toString(36).slice(2));
        return prefix + "-" + nonce;
    }

    function renderDepositForm(item, content) {
        var summary = item.folio_summary || {};
        if (summary.status !== "OPEN" || !["CONFIRMED", "CHECKED_IN"].includes(item.status)) return;
        var box = element("div", "fd-payment-box");
        box.appendChild(element("h4", "", "登记入住押金"));
        box.appendChild(element("p", "fd-operation-note",
            "只登记已实际收到的押金；押金作为独立 DEPOSIT 凭证入账，退款需走双人审批。"));
        var form = element("form", "fd-payment-form");
        var amountLabel = element("label", "fd-operation-label", "本次押金金额");
        var amount = element("input", "fd-operation-input");
        amount.type = "number"; amount.min = "0.01"; amount.step = "0.01"; amount.required = true;
        amount.placeholder = "例如 200.00";
        amountLabel.appendChild(amount); form.appendChild(amountLabel);
        var methodLabel = element("label", "fd-operation-label", "押金方式");
        var method = element("select", "fd-operation-select");
        [["CASH","现金"],["CARD","银行卡 / 刷卡"],["BANK_TRANSFER","银行转账"],["OTHER","其他"]].forEach(function (entry) {
            var option = element("option", "", entry[1]); option.value = entry[0]; method.appendChild(option);
        });
        methodLabel.appendChild(method); form.appendChild(methodLabel);
        var submit = element("button", "fd-btn fd-btn-secondary", "登记已收押金");
        submit.type = "submit"; form.appendChild(submit); box.appendChild(form);
        amount.addEventListener("input", function () { state.depositKey = null; });
        method.addEventListener("change", function () { state.depositKey = null; });
        form.addEventListener("submit", async function (event) {
            event.preventDefault();
            if (!amount.value || !Number.isFinite(Number(amount.value)) || Number(amount.value) <= 0) {
                showToast("请输入大于 0 的押金金额。", true); return;
            }
            if (method.value === "CASH" && !state.cashierShift) {
                showToast("请先开立本人收银班次，再登记现金押金。", true);
                return;
            }
            if (!window.confirm("请确认押金已经实际收到。系统只登记账务，不调用支付渠道。")) return;
            state.depositKey = state.depositKey || makeIdempotencyKey("fd-deposit");
            submit.disabled = true;
            try {
                await postApi("reservations/" + encodeURIComponent(item.booking_no) + "/deposits",
                    { amount: amount.value, method: method.value }, state.depositKey);
                state.depositKey = null;
                await refreshAfterOperation(item.booking_no, "押金登记成功");
            } catch (error) {
                showToast(error.message, true); submit.disabled = false;
            }
        });
        content.appendChild(box);
    }

    function renderOperations(item) {
        var section = $("fd-operations-section");
        var content = $("detail-operations");
        section.hidden = false;
        content.replaceChildren();
        if (item.source_system === "LEGACY" || item.read_only) {
            section.hidden = true;
            return;
        }
        var allowed = item.allowed_actions || [];
        var canOperate = allowed.includes("operate");
        var canNoShow = allowed.includes("mark_no_show");
        $("fd-operations-note").textContent = canOperate
            ? "操作按每间房明细执行；服务端会再次校验物业、岗位权限、房态、库存和账单。"
            : "当前岗位只有查看权限；如需办理业务操作，请联系具有相应岗位授权的员工。";

        (item.rooms || []).forEach(function (roomLine) {
            var roomNode = element("div", "fd-operation-room");
            var assigned = roomLine.assigned_room ? roomLine.assigned_room.number : "待分配";
            var stay = roomLine.stay;
            roomNode.appendChild(element(
                "div",
                "fd-detail-room-label",
                (roomLine.room_type ? roomLine.room_type.name : "房间") + " · " + assigned +
                    (roomLine.no_show ? " · 未到店" : (stay ? " · " + stay.status : " · 待入住"))
            ));
            var actionRow = element("div", "fd-operation-actions");
            if (roomLine.no_show) {
                actionRow.appendChild(element("span", "fd-operation-complete", "已登记 No-Show"));
            } else if (canNoShow && !stay &&
                ["CONFIRMED", "CHECKED_IN"].includes(roomLine.reservation_status)) {
                var noShowButton = operationButton("标记未到店", "fd-btn-danger", async function () {
                    var reason = window.prompt("请填写未到店处理原因（必填）");
                    if (!reason || !reason.trim()) return;
                    if (!window.confirm("确认将该房间明细标记为 No-Show？剩余住宿日的已售库存将释放。")) return;
                    noShowButton.disabled = true;
                    try {
                        await postApi("reservation-rooms/" + roomLine.reservation_room_id + "/no-show", { reason: reason.trim() });
                        await refreshAfterOperation(item.booking_no, "已登记该房间 No-Show");
                    } catch (error) {
                        showToast(error.message, true);
                        noShowButton.disabled = false;
                    }
                });
                actionRow.appendChild(noShowButton);
            }
            if (canOperate && !stay && !roomLine.no_show &&
                ["CONFIRMED", "CHECKED_IN"].includes(roomLine.reservation_status)) {
                if (!roomLine.assigned_room) {
                    var assignButton = operationButton("分配房间", "fd-btn-secondary", function () {
                        showAssignableRooms(roomLine, roomNode, assignButton);
                    });
                    actionRow.appendChild(assignButton);
                } else {
                    var checkInButton = operationButton("办理入住", "fd-btn-primary", async function () {
                        if (!window.confirm("确认住客已到店、身份核验和房间交接已完成，并办理入住？")) return;
                        checkInButton.disabled = true;
                        try {
                            await postApi("reservation-rooms/" + roomLine.reservation_room_id + "/check-in", {});
                            await refreshAfterOperation(item.booking_no, "入住办理成功");
                        } catch (error) {
                            showToast(error.message, true);
                            checkInButton.disabled = false;
                        }
                    });
                    actionRow.appendChild(checkInButton);
                }
            } else if (canOperate && stay && stay.status === "IN_HOUSE") {
                var extendButton = operationButton("续住", "fd-btn-secondary", async function () {
                    var newDate = window.prompt("请输入新的离店日期（YYYY-MM-DD）", roomLine.check_out);
                    if (!newDate) return;
                    var reason = window.prompt("请填写续住原因");
                    if (!reason || !reason.trim()) return;
                    extendButton.disabled = true;
                    try {
                        await postApi("stays/" + stay.id + "/extend", { new_check_out: newDate, reason: reason.trim() });
                        await refreshAfterOperation(item.booking_no, "续住成功，已更新价格与库存");
                    } catch (error) {
                        showToast(error.message, true);
                        extendButton.disabled = false;
                    }
                });
                actionRow.appendChild(extendButton);
                var changeRoomButton = operationButton("换房", "fd-btn-secondary", async function () {
                    changeRoomButton.disabled = true;
                    try {
                        var available = await api("stays/" + stay.id + "/available-rooms");
                        var options = available.data || [];
                        if (!options.length) {
                            showToast("当前没有可换入的同房型净房。", true);
                            return;
                        }
                        var chooser = element("div", "fd-room-assigner");
                        var label = element("label", "fd-operation-label", "选择换入房间");
                        var select = element("select", "fd-operation-select");
                        options.forEach(function (room) {
                            var option = element("option", "", room.number + " · " + room.floor + "层");
                            option.value = room.id;
                            select.appendChild(option);
                        });
                        label.appendChild(select);
                        chooser.appendChild(label);
                        var confirmMove = operationButton("确认换房", "fd-btn-primary", async function () {
                            var reason = window.prompt("请填写换房原因");
                            if (!reason || !reason.trim()) return;
                            confirmMove.disabled = true;
                            try {
                                await postApi("stays/" + stay.id + "/change-room", { room_id: Number(select.value), reason: reason.trim() });
                                await refreshAfterOperation(item.booking_no, "换房成功，旧房已转待清洁");
                            } catch (error) {
                                showToast(error.message, true);
                                confirmMove.disabled = false;
                            }
                        });
                        chooser.appendChild(confirmMove);
                        roomNode.appendChild(chooser);
                    } catch (error) {
                        showToast(error.message, true);
                    } finally {
                        changeRoomButton.disabled = false;
                    }
                });
                actionRow.appendChild(changeRoomButton);
                var checkoutButton = operationButton("办理退房", "fd-btn-primary", async function () {
                    if (!window.confirm("确认已完成退房核对？系统将按预订价格快照入账，并按账单余额规则办理退房。")) return;
                    checkoutButton.disabled = true;
                    try {
                        await postApi("stays/" + stay.id + "/check-out", {});
                        await refreshAfterOperation(item.booking_no, "退房办理成功，房间已转为待清洁");
                    } catch (error) {
                        showToast(error.message, true);
                        checkoutButton.disabled = false;
                    }
                });
                actionRow.appendChild(checkoutButton);
            } else if (stay && stay.status === "CHECKED_OUT") {
                actionRow.appendChild(element("span", "fd-operation-complete", "该房间已退房"));
            } else if (roomLine.reservation_status === "PENDING") {
                actionRow.appendChild(element("span", "fd-operation-note", "预订尚未确认，暂不能排房或办理入住"));
            }
            if (actionRow.children.length) roomNode.appendChild(actionRow);
            content.appendChild(roomNode);
        });

        if (!item.rooms || !item.rooms.length) {
            content.appendChild(element("p", "fd-operation-note", "该订单没有房间明细。"));
        }
        if (canOperate) {
            renderPaymentForm(item, content);
            renderDepositForm(item, content);
        }
    }

    async function loadDetail(bookingNo) {
        try {
            if (String(bookingNo).indexOf("LEGACY-") === 0) {
                var legacyId = String(bookingNo).slice(7);
                var legacyPayload = await api("legacy-reservations/" + encodeURIComponent(legacyId));
                var legacy = legacyPayload.data;
                $("fd-drawer-title").textContent = "历史订单（只读）";
                $("detail-guest-name").textContent = legacy.guest.full_name || "—";
                $("detail-guest-phone").textContent = legacy.guest.phone_masked || "—";
                $("detail-source").textContent = "历史 hotelapp";
                $("detail-status").textContent = legacy.status_label || "历史记录";
                $("detail-checkin").textContent = legacy.check_in || "—";
                $("detail-checkout").textContent = legacy.check_out || "—";
                $("detail-room-count").textContent = "1（旧订单）";
                $("detail-guests").textContent = "旧系统未记录";
                $("detail-charges").textContent = "旧账套";
                $("detail-credits").textContent = "已收 " + (legacy.legacy_paid_amount || "未记录") + " / 押金 " + (legacy.legacy_deposit_amount || "未记录");
                $("detail-balance").textContent = "不与 PMS2 合并";
                $("detail-created").textContent = "—";
                $("detail-updated").textContent = "只读历史数据";
                $("fd-operations-section").hidden = true;
                $("fd-view-folio").hidden = true;
                $("fd-view-folio").onclick = null;
                var legacyRooms = $("detail-rooms"); legacyRooms.replaceChildren();
                (legacy.rooms || []).forEach(function (room) {
                    legacyRooms.appendChild(element("div", "fd-detail-room",
                        (room.room_type ? room.room_type.name : "历史房型") + " · " +
                        (room.assigned_room ? room.assigned_room.number : "未记录")));
                });
                var legacyEntries = $("detail-folio-entries"); legacyEntries.replaceChildren();
                (legacy.legacy_entries || []).forEach(function (entry) {
                    var row = element("div", "fd-folio-entry");
                    row.appendChild(element("span", "", (entry.entry_type || "历史凭证") + " · " + (entry.business_date || "")));
                    row.appendChild(element("b", "", (entry.amount_raw || "未记录") + " · " + (entry.description || "")));
                    legacyEntries.appendChild(row);
                });
                if (!legacyEntries.children.length) legacyEntries.appendChild(element("div", "fd-folio-entry", "旧系统没有可读取的独立账单流水。"));
                return;
            }
            $("fd-view-folio").hidden = false;
            var payload = await api("reservations/" + encodeURIComponent(bookingNo));
            var item = payload.data;
            $("detail-guest-name").textContent = item.guest.full_name || "—";
            $("detail-guest-phone").textContent = item.guest.phone_masked || "—";
            $("detail-source").textContent = item.source_label || item.source || "—";
            $("detail-status").textContent = item.status_label || item.status || "—";
            $("detail-checkin").textContent = item.check_in || "—";
            $("detail-checkout").textContent = item.check_out || "—";
            $("detail-room-count").textContent = item.room_count || (item.rooms || []).length;
            $("detail-guests").textContent = (item.adult_count || 0) + " 成人 / " + (item.child_count || 0) + " 儿童";
            var summary = item.folio_summary || {};
            $("detail-charges").textContent = money(summary.charges, summary.currency);
            $("detail-credits").textContent = money(summary.credits, summary.currency);
            renderFolioBalance(summary.balance, summary.currency);
            $("detail-created").textContent = item.timeline && item.timeline.created_at ? new Date(item.timeline.created_at).toLocaleString() : "—";
            $("detail-updated").textContent = item.timeline && item.timeline.updated_at ? new Date(item.timeline.updated_at).toLocaleString() : "—";

            var roomList = $("detail-rooms");
            (item.rooms || []).forEach(function (room) {
                var node = element("div", "fd-detail-room");
                var number = room.assigned_room ? room.assigned_room.number : "待分配";
                node.textContent = (room.room_type ? room.room_type.name : "房型") + " · " + number +
                    " · " + room.check_in + " 至 " + room.check_out +
                    (room.no_show ? " · 未到店" : " · 实际入住：" + (room.stay_status_label || (room.stay ? room.stay.status : "未生成入住记录")));
                if (room.stay && room.stay.room_segments && room.stay.room_segments.length > 1) {
                    room.stay.room_segments.forEach(function (segment) {
                        roomList.appendChild(element("div", "fd-detail-room",
                            "房间历史：" + segment.room_number + " · " + segment.started_at +
                            (segment.ended_at ? " 至 " + segment.ended_at : "（当前房间）") +
                            (segment.reason ? " · " + segment.reason : "")));
                    });
                }
                roomList.appendChild(node);
            });
            if (!roomList.children.length) roomList.appendChild(element("div", "fd-detail-room", "暂无房间明细"));

            renderOperations(item);
            $("fd-view-folio").onclick = function () { loadFolio(bookingNo); };
        } catch (error) {
            showToast(error.message, true);
            $("detail-guest-name").textContent = "暂时无法读取";
        }
    }

    async function loadFolio(bookingNo) {
        try {
            var payload = await api("folios/" + encodeURIComponent(bookingNo));
            var data = payload.data || {};
            var summary = data.summary || {};
            $("detail-charges").textContent = money(summary.charges, summary.currency);
            $("detail-credits").textContent = money(summary.credits, summary.currency);
            $("detail-balance").textContent = money(summary.balance, summary.currency);
            var box = $("detail-folio-entries");
            box.replaceChildren();
            (data.entries || []).forEach(function (entry) {
                var line = element("div", "fd-folio-entry");
                line.appendChild(element("span", "", (entry.entry_type || "") + " · " + (entry.business_date || "")));
                line.appendChild(element("b", "", money(entry.amount, entry.currency)));
                box.appendChild(line);
            });
            var actions = data.allowed_actions || [];
            (data.payments || []).forEach(function (payment) {
                var paymentNode = element("div", "fd-folio-entry");
                paymentNode.appendChild(element("span", "",
                    (payment.provider === "frontdesk_deposit" ? "押金" : "支付") + " · " +
                    payment.method + " · " + (payment.status_label || payment.status)));
                paymentNode.appendChild(element("b", "", money(payment.amount, payment.currency)));
                box.appendChild(paymentNode);
                (payment.refunds || []).forEach(function (refund) {
                    var refundNode = element("div", "fd-folio-entry");
                    refundNode.appendChild(element("span", "", "退款 " + (refund.status_label || refund.status) + " · " + refund.reason));
                    refundNode.appendChild(element("b", "", money(refund.amount, payment.currency)));
                    box.appendChild(refundNode);
                    if (refund.status === "PENDING" && actions.includes("approve_refund")) {
                        var approve = operationButton("确认已实际退款并审批", "fd-btn-primary", async function () {
                            if (!window.confirm("请确认该款项已通过现金/银行等方式实际退还。审批后将写入不可变退款凭证。")) return;
                            approve.disabled = true;
                            try {
                                await postApi("refunds/" + refund.id + "/approve", { confirm_refund_paid: true });
                                showToast("退款审批完成并已入账", false);
                                await loadFolio(bookingNo);
                            } catch (error) {
                                showToast(error.message, true); approve.disabled = false;
                            }
                        });
                        box.appendChild(approve);
                    }
                });
                if (actions.includes("request_refund") && ["SUCCEEDED", "PARTIALLY_REFUNDED"].includes(payment.status)) {
                    var requestButton = operationButton("申请退款", "fd-btn-secondary", async function () {
                        var amount = window.prompt("请输入退款金额（原支付 " + payment.amount + "）", payment.amount);
                        if (!amount) return;
                        var reason = window.prompt("请填写退款原因");
                        if (!reason || !reason.trim()) return;
                        requestButton.disabled = true;
                        try {
                            state.refundKey = state.refundKey || makeIdempotencyKey("fd-refund");
                            await postApi("payments/" + payment.id + "/refunds",
                                { amount: amount, reason: reason.trim() }, state.refundKey);
                            state.refundKey = null;
                            showToast("退款申请已提交，需另一名管理者审批", false);
                            await loadFolio(bookingNo);
                        } catch (error) {
                            showToast(error.message, true); requestButton.disabled = false;
                        }
                    });
                    box.appendChild(requestButton);
                }
            });
            if (!box.children.length) box.appendChild(element("div", "fd-folio-entry", "暂无账单凭证"));
        } catch (error) {
            showToast(error.message, true);
        }
    }

    function renderRows(items, tab) {
        tbody.replaceChildren();
        if (!items.length) {
            var empty = element("tr");
            var emptyCell = element("td", "fd-table-empty", "当前没有符合条件的记录。");
            emptyCell.colSpan = 6;
            empty.appendChild(emptyCell);
            tbody.appendChild(empty);
            return;
        }
        items.forEach(function (item) {
            var bookingNo = item.booking_no || (item.reservation && item.reservation.booking_no);
            var guest = item.guest || {};
            var row = element("tr");
            appendCell(row, bookingNo || "—", guest.full_name || "—");
            if (tab === "rooms") {
                appendCell(row, item.room_type ? item.room_type.name : "—", (item.floor || "楼层未设置") + " · " + item.number);
                appendCell(row, "当前房态", (item.housekeeping_label || item.housekeeping_status) + " / " + (item.maintenance_label || item.maintenance_status));
                appendCell(row, "—", "房间视图");
            } else if (tab === "legacy") {
                appendCell(row, item.room_type ? item.room_type.name : "历史房型",
                    item.assigned_room ? item.assigned_room.number : "房号未记录");
                appendCell(row, (item.check_in || "—") + " — " + (item.check_out || "—"), "历史入住日期");
                appendCell(row, "已收 " + (item.legacy_paid_amount || "未记录"),
                    "押金 " + (item.legacy_deposit_amount || "未记录") + " · 旧账套只读");
            } else {
                var assigned = item.assigned_room ? item.assigned_room.number : "待分配";
                appendCell(row, item.room_type ? item.room_type.name : "—", assigned);
                appendCell(row, (item.check_in || "—") + " — " + (item.check_out || "—"), "共 " + (item.nights || 0) + " 晚");
                var summary = item.folio_summary || {};
                appendCell(row, money(summary.balance, summary.currency), "应收 " + money(summary.charges, summary.currency) + " · 已收/抵扣 " + money(summary.credits, summary.currency));
            }
            var status = badgeFor(item, tab);
            var statusCell = element("td");
            statusCell.appendChild(element("span", "fd-status fd-status-" + status[1], status[0]));
            row.appendChild(statusCell);
            var actionCell = element("td");
            if (bookingNo) {
                var button = element("button", "fd-row-link", "查看详情 →");
                button.type = "button";
                button.addEventListener("click", function () { openDrawer(bookingNo); });
                actionCell.appendChild(button);
            } else {
                actionCell.textContent = "—";
            }
            row.appendChild(actionCell);
            tbody.appendChild(row);
        });
    }

    function updateListHeading() {
        var titles = {
            "workbench": ["今日抵店任务", "优先处理今日预抵、待排房和待入住订单"],
            "arrivals": ["今日抵店", "按营业日期查看抵店预订和房间分配情况"],
            "departures": ["今日离店", "查看当前在住且计划今日离店的住客"],
            "in-house": ["在住客人", "查看实际 Stay、房间和账单摘要"],
            "reservations": ["订单查询", "检索 PMS 2.0 预订与每间房的入住进度"],
            "legacy": ["历史订单", "旧 hotelapp 订单只读兼容；旧金额不参与 PMS2 账单对账"],
            "rooms": ["房间视图", "查看占用、清洁和维修三个状态维度"]
        };
        var pair = titles[state.tab] || titles.workbench;
        $("fd-list-title").textContent = pair[0];
        $("fd-list-subtitle").textContent = pair[1];
        $("fd-assigned-filter").hidden = !(state.tab === "workbench" || state.tab === "arrivals");
    }

    function queryString(params) {
        var q = new URLSearchParams();
        Object.keys(params).forEach(function (key) {
            var value = params[key];
            if (value !== undefined && value !== null && value !== "") q.set(key, value);
        });
        return q.toString();
    }

    async function loadList() {
        updateListHeading();
        setLoading("正在读取前台数据…");
        var params = { page: state.page, page_size: state.pageSize, q: state.query };
        var endpoint = "arrivals";
        if (state.tab === "workbench" || state.tab === "arrivals") {
            endpoint = "arrivals";
            params.date = state.date;
            params.assigned = state.assigned;
        } else if (state.tab === "departures") {
            endpoint = "departures";
            params.date = state.date;
        } else if (state.tab === "in-house") {
            endpoint = "in-house";
        } else if (state.tab === "reservations") {
            endpoint = "reservations";
        } else if (state.tab === "legacy") {
            endpoint = "legacy-reservations";
        } else if (state.tab === "rooms") {
            endpoint = "rooms";
            params = { readiness: state.readiness };
        }

        try {
            var payload = await api(endpoint + (queryString(params) ? "?" + queryString(params) : ""));
            var items = payload.data || [];
            if (state.tab === "rooms") {
                renderRooms(items);
                $("fd-page-info").textContent = "共 " + items.length + " 间房";
                $("fd-page-prev").disabled = true;
                $("fd-page-next").disabled = true;
            } else {
                renderRows(items, state.tab === "workbench" ? "arrivals" : state.tab);
                var meta = payload.meta || {};
                state.total = meta.total || 0;
                $("fd-page-info").textContent = "共 " + state.total + " 条 · 第 " + (meta.page || 1) + " 页";
                $("fd-page-prev").disabled = !meta.has_previous;
                $("fd-page-next").disabled = !meta.has_next;
            }
        } catch (error) {
            showTableError(error.message);
        }
    }

    async function loadOverview() {
        try {
            var overviewUrl = "overview" + (state.date ? "?date=" + encodeURIComponent(state.date) : "");
            var payload = await api(overviewUrl);
            var data = payload.data || {};
            state.date = data.business_date || state.date;
            $("fd-date").value = state.date;
            $("fd-business-date").textContent = "营业日 " + state.date;
            $("kpi-arrivals").textContent = data.arrivals_count || 0;
            $("kpi-unassigned").textContent = data.unassigned_count || 0;
            $("kpi-departures").textContent = data.departures_count || 0;
            $("kpi-in-house").textContent = data.in_house_count || 0;
            $("kpi-ready").textContent = data.clean_available_count || 0;
            $("kpi-inspecting").textContent = data.waiting_release_count || 0;
            $("room-ready-count").textContent = data.clean_available_count || 0;
            $("room-inspecting-count").textContent = data.waiting_release_count || 0;
            $("room-maintenance-count").textContent = data.maintenance_count || 0;
            $("exception-unassigned").textContent = data.unassigned_count || 0;
            $("exception-inspecting").textContent = data.waiting_release_count || 0;
            $("exception-maintenance").textContent = data.maintenance_count || 0;
            $("room-cleaning-count").textContent = "—";
            $("room-readiness-note").textContent = "状态更新时间：" + new Date(data.data_updated_at).toLocaleTimeString();
        } catch (error) {
            showToast(error.message, true);
            $("fd-business-date").textContent = "营业日暂不可用";
        }
    }

    async function loadAll() {
        await Promise.all([loadOverview(), loadList(), loadCashierShift()]);
    }

    function switchTab(tab, filter) {
        state.tab = tab;
        state.page = 1;
        if (filter === "unassigned") state.assigned = "no";
        else if (filter === "READY" || filter === "INSPECTING" || filter === "MAINTENANCE") state.readiness = filter;
        else if (tab === "arrivals" || tab === "workbench") state.assigned = "all";
        document.querySelectorAll(".fd-tab").forEach(function (button) {
            button.classList.toggle("is-active", button.dataset.tab === tab);
        });
        if (tab === "rooms") $("fd-assigned-filter").hidden = true;
        else $("fd-assigned-filter").hidden = !(tab === "arrivals" || tab === "workbench");
        loadList();
    }

    document.querySelectorAll(".fd-tab").forEach(function (button) {
        button.addEventListener("click", function () { switchTab(button.dataset.tab); });
    });
    document.querySelectorAll("[data-kpi-tab]").forEach(function (button) {
        button.addEventListener("click", function () { switchTab(button.dataset.kpiTab, button.dataset.kpiFilter); });
    });
    document.querySelectorAll(".fd-exception-row").forEach(function (button) {
        button.addEventListener("click", function () { switchTab(button.dataset.tab, button.dataset.filter); });
    });
    $("fd-refresh").addEventListener("click", loadAll);
    $("fd-search-submit").addEventListener("click", function () {
        state.query = $("fd-search").value.trim();
        state.page = 1;
        loadList();
    });
    $("fd-search").addEventListener("keydown", function (event) {
        if (event.key === "Enter") $("fd-search-submit").click();
    });
    $("fd-date").addEventListener("change", function () {
        state.date = $("fd-date").value;
        state.page = 1;
        loadAll();
    });
    $("fd-assigned-filter").addEventListener("change", function () {
        state.assigned = $("fd-assigned-filter").value;
        state.page = 1;
        loadList();
    });
    $("fd-page-size").addEventListener("change", function () {
        state.pageSize = Number($("fd-page-size").value) || 20;
        state.page = 1;
        loadList();
    });
    $("fd-page-prev").addEventListener("click", function () {
        if (state.page > 1) { state.page -= 1; loadList(); }
    });
    $("fd-page-next").addEventListener("click", function () {
        state.page += 1;
        loadList();
    });
    drawer.querySelectorAll("[data-drawer-close]").forEach(function (node) {
        node.addEventListener("click", closeDrawer);
    });
    // 抽屉焦点圈闭：Tab/Shift+Tab 在抽屉内循环，避免键盘焦点逃逸到被遮罩的页面
    drawer.addEventListener("keydown", function (event) {
        if (event.key !== "Tab" || !drawer.classList.contains("is-open")) return;
        var focusables = drawer.querySelectorAll(
            "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])");
        var visible = [];
        focusables.forEach(function (node) {
            if (node.offsetParent !== null) visible.push(node);
        });
        if (!visible.length) return;
        var first = visible[0];
        var last = visible[visible.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
    // 窄屏导航开关：与桌面布局无关，仅 ≤820px 由样式显示
    var navToggle = $("fd-nav-toggle");
    function setNavOpen(open) {
        document.body.classList.toggle("fd-nav-open", open);
        if (navToggle) navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    if (navToggle) {
        navToggle.addEventListener("click", function () {
            setNavOpen(!document.body.classList.contains("fd-nav-open"));
        });
        document.querySelectorAll(".sidebar-menu a").forEach(function (link) {
            link.addEventListener("click", function () { setNavOpen(false); });
        });
    }
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        if (drawer.classList.contains("is-open")) closeDrawer();
        else if (document.body.classList.contains("fd-nav-open")) setNavOpen(false);
    });

    state.date = $("fd-date").value || "";
    bindCashierShiftControls();
    loadAll();
})();