/* 权限管理中心（§8.1）：四区页面逻辑。
 * 数据源：#pc-bootstrap（注册表派生 JSON）+ /platform/api/permissions/ JSON API。
 * 关键约定：
 *   - CSRF_USE_SESSIONS=True → 令牌在 DOM 隐藏域，经 X-CSRFToken 头提交；
 *   - 保存携带 version（内容 hash）做并发保护，409 时自动刷新；
 *   - 触及专项权限的变更走两步流程：先 confirm-token 取令牌，再携带保存。
 */
(function () {
    'use strict';

    var bootstrap = JSON.parse(
        document.getElementById('pc-bootstrap').textContent);
    var SPECIAL_CODES = new Set(bootstrap.special_codenames || []);

    var state = {
        groupId: null,
        version: '',
        permsSet: new Set(),     // 服务端当前全集（含注册表外保留码）
        reclaimed: [],           // 保存时将被回收的历史 PLANNED 授权
        confirmToken: null,      // 高危变更确认令牌（两步流程中间态）
        busy: false
    };

    // ------------------------------------------------------------------
    // 工具
    // ------------------------------------------------------------------

    function $(id) { return document.getElementById(id); }

    function getCookie(name) {
        var rows = document.cookie ? document.cookie.split('; ') : [];
        for (var i = 0; i < rows.length; i++) {
            var pair = rows[i].split('=');
            if (pair[0] === name) { return decodeURIComponent(pair[1]); }
        }
        return null;
    }

    function csrfToken() {
        var input = document.querySelector(
            '#pc-csrf input[name="csrfmiddlewaretoken"]');
        if (input && input.value) { return input.value; }
        return getCookie('csrftoken') || '';
    }

    function api(method, url, payload) {
        var headers = { 'X-CSRFToken': csrfToken(), 'Accept': 'application/json' };
        var opts = { method: method, headers: headers };
        if (payload !== undefined) {
            headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(payload);
        }
        return fetch(url, opts).then(function (resp) {
            return resp.json().catch(function () { return {}; })
                .then(function (data) {
                    if (!resp.ok) {
                        var err = new Error(data.error || ('请求失败（' + resp.status + '）'));
                        err.data = data;
                        err.status = resp.status;
                        throw err;
                    }
                    return data;
                });
        });
    }

    function esc(text) {
        var div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    function badge(status) {
        var map = {
            ACTIVE: ['ok', '现役'], PLANNED: ['warn', '预留'],
            RETIRED: ['off', '退役']
        };
        var item = map[status] || ['off', status];
        return '<span class="pc-badge pc-badge-' + item[0] + '">' + item[1] + '</span>';
    }

    function moduleBadge(status) {
        var map = {
            ACTIVE: ['ok', '在营'], PLANNED: ['warn', '预留'],
            RETIRED: ['off', '退役']
        };
        var item = map[status] || ['off', status];
        return '<span class="pc-badge pc-badge-' + item[0] + '">' + item[1] + '</span>';
    }

    function riskBadge(level) {
        var map = { high: 'high', medium: 'warn', low: 'off' };
        var names = { high: '高危', medium: '中危', low: '低危' };
        return '<span class="pc-badge pc-risk-' + (map[level] || 'off') + '">'
            + (names[level] || level || '') + '</span>';
    }

    // codename → 展示信息（专项 label / 所属模块），由 bootstrap 派生
    var CODE_META = {};
    bootstrap.modules.forEach(function (m) {
        m.columns.forEach(function (col) {
            if (col.code) {
                CODE_META[col.code] = { label: col.label, module: m, special: SPECIAL_CODES.has(col.code) };
            }
        });
        m.specials.forEach(function (s) {
            CODE_META[s.codename] = { label: s.label, module: m, special: true, note: s.note };
        });
    });

    function codeLabel(code) {
        return CODE_META[code] ? CODE_META[code].label : code;
    }

    // ------------------------------------------------------------------
    // 左区：角色列表
    // ------------------------------------------------------------------

    function renderRoles() {
        var ul = $('pc-role-list');
        ul.replaceChildren();
        bootstrap.roles.forEach(function (role) {
            var li = document.createElement('li');
            li.className = 'pc-role-item';
            li.dataset.groupId = role.group_id == null ? '' : role.group_id;
            li.innerHTML =
                '<div class="pc-role-line"><span class="pc-role-name">' + esc(role.name) + '</span>'
                + badge(role.status) + '</div>'
                + '<div class="pc-role-sub">在岗 <b>' + role.staff_count + '</b> 人 · 任职 <b>'
                + role.membership_count + '</b> 条</div>';
            if (role.group_id == null) {
                li.classList.add('pc-role-unavailable');
                li.title = '角色组尚未初始化';
            } else {
                li.setAttribute('role', 'button');
                li.addEventListener('click', function () { selectRole(role.group_id); });
            }
            ul.appendChild(li);
        });
    }

    function markSelectedRole(groupId) {
        document.querySelectorAll('.pc-role-item').forEach(function (li) {
            li.classList.toggle('pc-role-selected', li.dataset.groupId === String(groupId));
        });
    }

    // ------------------------------------------------------------------
    // 中区：模块×动作矩阵 + 专项权限
    // ------------------------------------------------------------------

    function renderMatrix() {
        var headCells = bootstrap.modules.length
            ? '<tr><th class="pc-mx-module">业务模块</th>'
                + ['查看', '新增', '修改', '删除', '审批']
                    .map(function (l) { return '<th class="pc-mx-action">' + l + '</th>'; }).join('')
                + '</tr>'
            : '';
        var rows = bootstrap.modules.map(function (m) {
            var muted = m.status !== 'ACTIVE';
            var cells = m.columns.map(function (col) {
                if (!col.code) {
                    return '<td class="pc-mx-cell pc-na">—</td>';
                }
                var specialMark = SPECIAL_CODES.has(col.code)
                    ? ' <span class="pc-mini-tag" title="专项权限：变更需二次确认">专项</span>' : '';
                return '<td class="pc-mx-cell">'
                    + '<input type="checkbox" class="pc-check" data-codename="' + esc(col.code) + '"'
                    + ' title="' + esc(col.code) + '"'
                    + (muted ? ' disabled' : '') + '> ' + specialMark + '</td>';
            }).join('');
            return '<tr class="' + (muted ? 'pc-row-muted' : '') + '" title="'
                + esc(m.code) + '（' + esc(m.status) + '，风险 ' + esc(m.risk_level) + '）">'
                + '<td class="pc-mx-module"><span class="pc-module-name">' + esc(m.name)
                + '</span>' + moduleBadge(m.status) + '</td>' + cells + '</tr>';
        }).join('');
        $('pc-matrix-box').innerHTML =
            '<table class="pc-matrix-table"><thead>' + headCells + '</thead><tbody>'
            + rows + '</tbody></table>';

        var specialGroups = bootstrap.modules.filter(function (m) {
            return m.specials.length > 0;
        }).map(function (m) {
            var items = m.specials.map(function (s) {
                return '<label class="pc-special-item" title="' + esc(s.note) + '">'
                    + '<input type="checkbox" class="pc-check pc-special-check" data-codename="'
                    + esc(s.codename) + '"' + (m.status !== 'ACTIVE' ? ' disabled' : '') + '>'
                    + '<span class="pc-special-label">' + esc(s.label) + '</span>'
                    + riskBadge(s.risk)
                    + '<span class="pc-special-note">' + esc(s.note) + '</span></label>';
            }).join('');
            return '<div class="pc-special-group"><h4>' + esc(m.name) + ' '
                + moduleBadge(m.status) + '</h4>' + items + '</div>';
        }).join('');
        $('pc-special-box').innerHTML =
            specialGroups || '<p class="pc-empty">注册表未登记专项权限。</p>';

        document.querySelectorAll('.pc-check').forEach(function (box) {
            box.addEventListener('change', function () {
                state.confirmToken = null;   // 变更后旧令牌必然 diff 不匹配
                updateConfirm();
            });
        });
    }

    function allChecks() {
        return Array.prototype.slice.call(
            document.querySelectorAll('.pc-check'));
    }

    function enabledCodes() {
        return new Set(allChecks()
            .filter(function (b) { return !b.disabled; })
            .map(function (b) { return b.dataset.codename; }));
    }

    function requestedSet() {
        return new Set(allChecks()
            .filter(function (b) { return !b.disabled && b.checked; })
            .map(function (b) { return b.dataset.codename; }));
    }

    // ------------------------------------------------------------------
    // 右区：影响范围 / 持有专项 / 最近保存审计
    // ------------------------------------------------------------------

    function renderImpact(data) {
        $('pc-impact-box').innerHTML =
            '<div class="pc-impact-grid">'
            + '<div class="pc-impact-item"><b>' + data.impact.staff_count + '</b><span>在岗员工</span></div>'
            + '<div class="pc-impact-item"><b>' + data.impact.membership_count + '</b><span>生效任职</span></div>'
            + '<div class="pc-impact-item"><b>' + data.permissions.length + '</b><span>权限项</span></div>'
            + '</div>'
            + '<p class="pc-zone-hint">' + esc(data.role.description) + '</p>';

        var held = data.special || [];
        $('pc-held-box').innerHTML = held.length
            ? held.map(function (code) {
                return '<span class="pc-held-tag" title="' + esc(code) + '">'
                    + esc(codeLabel(code)) + '</span>';
            }).join(' ')
            : '<p class="pc-empty">未持有专项权限。</p>';

        var last = data.last_save;
        $('pc-audit-box').innerHTML = last
            ? '<p class="pc-audit-line"><b>' + esc((last.at || '').replace('T', ' ').slice(0, 19))
                + '</b> 操作人 ' + esc(last.actor || '—') + '<br>'
                + '<span class="pc-audit-reason">理由：' + esc(last.reason || '—') + '</span></p>'
            : '<p class="pc-empty">该角色尚无矩阵保存审计记录。</p>';
    }

    // ------------------------------------------------------------------
    // 底部：变更摘要 + 理由 + 保存
    // ------------------------------------------------------------------

    function currentDiff() {
        var requested = requestedSet();
        var enabled = enabledCodes();
        var added = [];
        var removed = [];
        requested.forEach(function (code) {
            if (!state.permsSet.has(code)) { added.push(code); }
        });
        state.permsSet.forEach(function (code) {
            if (enabled.has(code) && !requested.has(code)) { removed.push(code); }
        });
        added.sort();
        removed.sort();
        return { added: added, removed: removed,
                 reclaimed: (state.reclaimed || []).slice().sort() };
    }

    function diffTouchesSpecial(diff) {
        return diff.added.concat(diff.removed).some(function (code) {
            return SPECIAL_CODES.has(code);
        });
    }

    function chipList(codes) {
        return codes.map(function (code) {
            return '<span class="pc-chip' + (SPECIAL_CODES.has(code) ? ' pc-chip-risk' : '')
                + '" title="' + esc(code) + '">' + esc(codeLabel(code)) + '</span>';
        }).join(' ');
    }

    function updateConfirm() {
        var diff = currentDiff();
        var total = diff.added.length + diff.removed.length + diff.reclaimed.length;
        $('pc-count').textContent = '变更 ' + total + ' 项';
        var summary = $('pc-diff-summary');
        if (!total) {
            summary.innerHTML = '<p class="pc-empty">暂无变更。</p>';
            setStepButton('idle');
            return;
        }
        summary.innerHTML =
            (diff.added.length
                ? '<p class="pc-diff-line pc-diff-add">新增 ' + diff.added.length + '：'
                    + chipList(diff.added) + '</p>' : '')
            + (diff.removed.length
                ? '<p class="pc-diff-line pc-diff-remove">移除 ' + diff.removed.length + '：'
                    + chipList(diff.removed) + '</p>' : '')
            + (diff.reclaimed.length
                ? '<p class="pc-diff-line pc-diff-reclaim">自动回收（历史预留授权，保存时随矩阵声明式回收） '
                    + diff.reclaimed.length + '：' + chipList(diff.reclaimed) + '</p>' : '');

        var highRisk = diffTouchesSpecial(diff);
        if (state.confirmToken && !highRisk) { state.confirmToken = null; }
        setStepButton(highRisk ? 'need_token' : 'ready');
    }

    function setStepButton(mode) {
        var btn = $('pc-step-btn');
        var hint = $('pc-step-hint');
        btn.disabled = state.busy;
        if (mode === 'idle') {
            btn.textContent = '保存权限';
            hint.textContent = '无实际变更，无需保存。';
        } else if (mode === 'need_token' && !state.confirmToken) {
            btn.textContent = '第一步：获取高危变更确认令牌';
            hint.textContent = '本次变更触及专项权限，需两步确认（令牌 10 分钟内有效）。';
        } else if (mode === 'need_token') {
            btn.textContent = '第二步：确认保存（高危变更）';
            hint.textContent = '令牌已获取，点击确认保存；变更内容再变动需重新获取。';
        } else {
            btn.textContent = '保存权限';
            hint.textContent = '保存后立即生效，并写入平台审计。';
        }
    }

    function reasonOrError() {
        var input = $('pc-reason');
        var reason = (input.value || '').trim();
        if (!reason) { return { error: '变更理由必填（1-240 字符）。' }; }
        if (reason.length > 240) { return { error: '变更理由不能超过 240 字符。' }; }
        return { reason: reason };
    }

    function showResult(kind, html) {
        var box = $('pc-result');
        box.hidden = false;
        box.className = 'pc-result pc-result-' + kind;
        box.innerHTML = html;
    }

    function saveFlow() {
        if (state.busy || !state.groupId) { return; }
        var reasonCheck = reasonOrError();
        if (reasonCheck.error) {
            showResult('error', esc(reasonCheck.error));
            return;
        }
        var diff = currentDiff();
        var highRisk = diffTouchesSpecial(diff);
        var requested = Array.from(requestedSet()).sort();

        if (highRisk && !state.confirmToken) {
            // 第一步：取高危确认令牌（服务端计算真实 diff，一并回显）
            state.busy = true;
            setStepButton('need_token');
            api('POST', '/platform/api/permissions/' + state.groupId + '/confirm-token/',
                { permissions: requested })
                .then(function (data) {
                    state.confirmToken = data.token;
                    state.reclaimed = data.removed.filter(function (code) {
                        return !enabledCodes().has(code);
                    });
                    updateConfirm();
                    showResult('info', '已获取确认令牌（'
                        + Math.round((data.expires_in || 600) / 60) + ' 分钟内有效）。'
                        + '服务端 diff：新增 ' + data.added.length + ' / 移除 '
                        + data.removed.length + '。请再次点击确认保存。');
                })
                .catch(function (err) {
                    showResult('error', esc(err.message));
                })
                .finally(function () {
                    state.busy = false;
                    updateConfirm();
                });
            return;
        }

        // 第二步（或非高危单步）：保存
        var payload = {
            permissions: requested,
            version: state.version,
            reason: reasonCheck.reason
        };
        if (state.confirmToken) { payload.confirm_token = state.confirmToken; }
        state.busy = true;
        setStepButton(highRisk ? 'need_token' : 'ready');
        api('POST', '/platform/api/permissions/' + state.groupId + '/save/', payload)
            .then(function (data) {
                state.confirmToken = null;
                showResult('success',
                    '保存成功（request_id ' + esc(data.request_id || '—') + '）。'
                    + '新增 ' + data.added.length + ' 项、移除 ' + data.removed.length
                    + ' 项，新版本 <code>' + esc(data.version) + '</code>。'
                    + (data.added.length
                        ? '<br>新增：' + chipList(data.added) : '')
                    + (data.removed.length
                        ? '<br>移除：' + chipList(data.removed) : ''));
                // 静默刷新服务端真值（版本/影响面/审计时间），不覆盖结果展示
                return loadRoleData(state.groupId);
            })
            .catch(function (err) {
                showResult('error', esc(err.message));
                state.confirmToken = null;
                if (err.status === 409) {
                    // 版本冲突 / 越权码：静默刷新服务端真值
                    loadRoleData(state.groupId).catch(function () {});
                }
            })
            .finally(function () {
                state.busy = false;
                updateConfirm();
            });
    }

    // ------------------------------------------------------------------
    // 角色加载
    // ------------------------------------------------------------------

    function loadRoleData(groupId) {
        return api('GET', '/platform/api/permissions/' + groupId + '/')
            .then(function (data) {
                state.groupId = groupId;
                state.version = data.version;
                state.permsSet = new Set(data.permissions);
                state.reclaimed = data.reclaimed_on_save || [];
                state.confirmToken = null;
                markSelectedRole(groupId);
                allChecks().forEach(function (box) {
                    if (!box.disabled) {
                        box.checked = state.permsSet.has(box.dataset.codename);
                    }
                });
                renderImpact(data);
                updateConfirm();
                return data;
            });
    }

    function selectRole(groupId) {
        if (state.busy) { return; }
        state.busy = true;
        loadRoleData(groupId)
            .then(function (data) {
                showResult('info', '已加载角色「' + esc(data.role.name) + '」，'
                    + '当前版本 <code>' + esc(data.version) + '</code>。');
            })
            .catch(function (err) {
                showResult('error', '加载角色失败：' + esc(err.message));
            })
            .finally(function () {
                state.busy = false;
                updateConfirm();
            });
    }

    // ------------------------------------------------------------------
    // 初始化
    // ------------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', function () {
        renderRoles();
        renderMatrix();
        $('pc-step-btn').addEventListener('click', saveFlow);
        var first = bootstrap.roles.find(function (role) {
            return role.group_id != null;
        });
        if (first) { selectRole(first.group_id); }
    });
})();
