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
        total: 0
    };

    var $ = function (id) { return document.getElementById(id); };
    var tbody = $("fd-table-body");
    var drawer = $("fd-drawer");
    var toastTimer = null;

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
        var amount = Number(value || 0);
        if (!Number.isFinite(amount)) amount = 0;
        return (currency || "CNY") + " " + amount.toFixed(2);
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

    async function api(path) {
        var response;
        try {
            response = await fetch(apiRoot + path, {
                method: "GET",
                credentials: "same-origin",
                headers: { "Accept": "application/json" },
                cache: "no-store"
            });
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
            card.appendChild(element("span", "", "占用：" + room.occupancy_status + " · 清洁：" + room.housekeeping_status));
            card.appendChild(element("span", "", "维修：" + room.maintenance_status));
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
        drawer.classList.add("is-open");
        drawer.setAttribute("aria-hidden", "false");
        $("fd-drawer-title").textContent = "订单详情";
        $("fd-drawer-booking").textContent = bookingNo;
        ["detail-guest-name", "detail-guest-phone", "detail-source", "detail-status",
         "detail-checkin", "detail-checkout", "detail-room-count", "detail-guests",
         "detail-charges", "detail-credits", "detail-balance", "detail-created", "detail-updated"
        ].forEach(function (id) { $(id).textContent = "加载中…"; });
        $("detail-rooms").replaceChildren();
        $("detail-folio-entries").replaceChildren();
        loadDetail(bookingNo);
    }

    function closeDrawer() {
        drawer.classList.remove("is-open");
        drawer.setAttribute("aria-hidden", "true");
    }

    async function loadDetail(bookingNo) {
        try {
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
            $("detail-balance").textContent = money(summary.balance, summary.currency);
            $("detail-created").textContent = item.timeline && item.timeline.created_at ? new Date(item.timeline.created_at).toLocaleString() : "—";
            $("detail-updated").textContent = item.timeline && item.timeline.updated_at ? new Date(item.timeline.updated_at).toLocaleString() : "—";

            var roomList = $("detail-rooms");
            (item.rooms || []).forEach(function (room) {
                var node = element("div", "fd-detail-room");
                var number = room.assigned_room ? room.assigned_room.number : "待分配";
                node.textContent = (room.room_type ? room.room_type.name : "房型") + " · " + number +
                    " · " + room.check_in + " 至 " + room.check_out +
                    (room.stay ? " · " + room.stay.status : "");
                roomList.appendChild(node);
            });
            if (!roomList.children.length) roomList.appendChild(element("div", "fd-detail-room", "暂无房间明细"));

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
                var left = element("span", "", (entry.entry_type || "") + " · " + (entry.business_date || ""));
                var right = element("b", "", money(entry.amount, entry.currency));
                line.appendChild(left);
                line.appendChild(right);
                box.appendChild(line);
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
                appendCell(row, "当前房态", item.housekeeping_status + " / " + item.maintenance_status);
                appendCell(row, "—", "房间视图");
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
            "reservations": ["订单查询", "检索 PMS 2.0 预订；历史旧订单将在兼容适配阶段接入"],
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
        await loadOverview();
        await loadList();
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
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") closeDrawer();
    });

    state.date = $("fd-date").value || "";
    loadAll();
})();