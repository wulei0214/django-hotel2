/* ============================================================
 * app.js — 全站交互基座（所有后台页面共用，勿在页面内重复定义）
 *
 * 提供：CSRF 注入 / 校验规则 VPAT / 消息提示 showToast /
 *       样式化确认框 askConfirm / 按钮加载态 btnLoading /
 *       表单防重复提交 / 侧边栏高亮
 * 依赖：无（jQuery 存在时自动增强 AJAX CSRF）
 * ============================================================ */
(function () {
    "use strict";

    /* ---------- CSRF ---------- */
    function getCookie(name) {
        var v = document.cookie.match('(^|;) ?' + name + '=([^;]*)(;|$)');
        return v ? v[2] : null;
    }
    function getCsrfToken() {
        // With CSRF_USE_SESSIONS the token is intentionally not exposed as a
        // shared cookie. Read the request-local token rendered into the page.
        var inputs = document.querySelectorAll('input[name="csrfmiddlewaretoken"]');
        for (var i = 0; i < inputs.length; i += 1) {
            if (inputs[i].value) return inputs[i].value;
        }
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content) return meta.content;
        // Backward-compatible fallback for external pages not yet using the
        // session-backed CSRF context.
        return getCookie('csrftoken');
    }
    if (window.jQuery) {
        window.jQuery.ajaxSetup({
            beforeSend: function (xhr) {
                var token = getCsrfToken();
                if (token) xhr.setRequestHeader('X-CSRFToken', token);
            }
        });
        // 全局 AJAX 进度条：任何异步请求期间顶部显示细进度条
        document.addEventListener('DOMContentLoaded', function () {
            var bar = document.createElement('div');
            bar.className = 'ajax-bar';
            document.body.appendChild(bar);
            var pending = 0;
            window.jQuery(document)
                .ajaxStart(function () { pending += 1; bar.classList.add('ajax-active'); })
                .ajaxStop(function () { pending = Math.max(0, pending - 1); if (!pending) { bar.classList.remove('ajax-active'); } });
        });
    }

    /* ---------- 校验规则（与后端白名单一致） ---------- */
    var VPAT = {
        id10: /\d{1,10}/.source,
        digits4: /\d{1,4}/.source,
        price5: /\d{1,5}/.source,
        name: /[\u4e00-\u9fa5A-Za-z·]{2,10}/.source,
        name20: /[\u4e00-\u9fa5A-Za-z0-9·]{2,20}/.source,
        type10: /[\u4e00-\u9fa5A-Za-z0-9]{2,10}/.source,
        money: /(0|[1-9]\d{0,5})(\.\d{1,2})?/.source,
        idcard: /\d{17}[\dXx]/.source,
        tel: /1\d{10}/.source,
        plate: /[\u4e00-\u9fa5][A-Z][A-Z0-9]{4,6}/.source,
        job: '总经理|大堂经理|厨师|服务员|保安',
        viprank: '普通会员|高级会员',
        parkstatus: '空闲|有车'
    };
    function vbad(v, p) {
        return !(new RegExp('^(?:' + p + ')$')).test((v || '').trim());
    }

    /* ---------- 消息提示（4.5 秒自动消失） ---------- */
    function showToast(text, tag) {
        var box = document.querySelector('.toast-box');
        if (!box) {
            box = document.createElement('div');
            box.className = 'toast-box';
            document.body.appendChild(box);
        }
        var t = document.createElement('div');
        t.className = 'toast ' + (tag || 'success');
        t.textContent = text;
        box.appendChild(t);
        setTimeout(function () {
            t.style.transition = 'opacity .4s';
            t.style.opacity = '0';
        }, 4200);
        setTimeout(function () { t.remove(); }, 4700);
    }

    /* ---------- 样式化确认框（替代原生 confirm） ---------- */
    function askConfirm(msg, onOk) {
        var mask = document.createElement('div');
        mask.className = 'ask-mask';
        mask.innerHTML =
            '<div class="ask-box">' +
            '  <div class="ask-msg"></div>' +
            '  <div class="ask-actions">' +
            '    <button type="button" class="ask-ok"></button>' +
            '    <button type="button" class="ask-cancel">取消</button>' +
            '  </div>' +
            '</div>';
        mask.querySelector('.ask-msg').textContent = msg;
        mask.querySelector('.ask-ok').textContent = '确定';
        var close = function () { mask.remove(); };
        mask.querySelector('.ask-ok').addEventListener('click', function () { close(); onOk(); });
        mask.querySelector('.ask-cancel').addEventListener('click', close);
        mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
        document.body.appendChild(mask);
        mask.querySelector('.ask-ok').focus();
    }

    /* ---------- 样式化输入对话框（替代原生 prompt，用于冲账原因等高危操作留痕） ----------
       askPrompt(msg, onOk(value), opts) —— opts: { placeholder, okText, required } */
    function askPrompt(msg, onOk, opts) {
        opts = opts || {};
        var mask = document.createElement('div');
        mask.className = 'ask-mask';
        mask.innerHTML =
            '<div class="ask-box">' +
            '  <div class="ask-msg"></div>' +
            '  <input type="text" class="ask-input" />' +
            '  <div class="ask-actions">' +
            '    <button type="button" class="ask-ok ask-ok-primary"></button>' +
            '    <button type="button" class="ask-cancel">取消</button>' +
            '  </div>' +
            '</div>';
        mask.querySelector('.ask-msg').textContent = msg;
        var okBtn = mask.querySelector('.ask-ok');
        okBtn.textContent = opts.okText || '确定';
        var input = mask.querySelector('.ask-input');
        input.placeholder = opts.placeholder || '';
        var close = function () { mask.remove(); };
        okBtn.addEventListener('click', function () {
            var v = (input.value || '').trim();
            if (opts.required && !v) {
                input.style.borderColor = 'var(--color-danger-solid)';
                input.focus();
                return;
            }
            close();
            onOk(v);
        });
        mask.querySelector('.ask-cancel').addEventListener('click', close);
        // 输入框内回车直接提交
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); okBtn.click(); }
        });
        mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
        document.body.appendChild(mask);
        input.focus();
    }

    /* ---------- 按钮加载态（防重复点击） ---------- */
    function btnLoading(btn) {
        if (!btn || btn.dataset.loading) { return function () {}; }
        btn.dataset.loading = '1';
        btn.classList.add('btn-is-loading');
        btn.disabled = true;
        return function () {
            btn.classList.remove('btn-is-loading');
            btn.disabled = false;
            delete btn.dataset.loading;
        };
    }

    /* ---------- 表单防重复提交（原生提交统一守护） ---------- */
    document.addEventListener('submit', function (e) {
        var f = e.target;
        if (f.dataset.guarded) { return; }
        f.dataset.guarded = '1';
        var btn = f.querySelector('[type="submit"]');
        if (btn) {
            setTimeout(function () {
                btn.classList.add('btn-is-loading');
                btn.disabled = true;
            }, 0);
            setTimeout(function () {
                btn.classList.remove('btn-is-loading');
                btn.disabled = false;
            }, 4000);
        }
    }, true);

    /* ---------- 侧边栏高亮 + 服务端消息淡出 ---------- */
    document.addEventListener('DOMContentLoaded', function () {
        var path = location.pathname;
        document.querySelectorAll('.sidebar-menu a').forEach(function (a) {
            if (a.getAttribute('href') === path) { a.classList.add('active'); }
        });
        setTimeout(function () {
            document.querySelectorAll('.toast').forEach(function (t) { t.style.opacity = '0'; });
        }, 4200);
    });

    /* ---------- 暴露给页面脚本 ---------- */
    window.getCookie = getCookie;
    window.VPAT = VPAT;
    window.vbad = vbad;
    window.showToast = showToast;
    window.askConfirm = askConfirm;
    window.askPrompt = askPrompt;
    window.btnLoading = btnLoading;
})();
