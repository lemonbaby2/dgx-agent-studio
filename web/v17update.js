/* v16.3 前端版本自更新
 *
 * 职责（只做三件事，不碰业务逻辑）：
 *   1. 拉 /api/version，右下角显示版本徽标（点开看特性列表）
 *   2. 每 60s 轮询一次；服务端版本变了就弹"有新版本，点此刷新"
 *   3. 客户端版本低于 min_client 时强制提示刷新，不允许继续用旧协议
 *
 * 为什么轮询而不是 WebSocket：后端是长连接 SSE 的，再挂一条长连接会抢并发位；
 * 60s 一次 GET 的代价可以忽略。
 */
(function () {
  'use strict';

  var POLL_MS = 60000;
  var boot = null;         // 首次拿到的版本信息
  var bannerShown = false;

  function $(id) { return document.getElementById(id); }

  function fmtTs(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function ensureBadge() {
    var el = $('v17-ver-badge');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'v17-ver-badge';
    el.title = '点击查看版本详情';
    el.style.cssText = [
      'position:fixed', 'right:10px', 'bottom:10px', 'z-index:99998',
      'font:11px/1.5 ui-monospace,Consolas,monospace',
      'padding:3px 8px', 'border-radius:10px', 'cursor:pointer',
      'background:rgba(24,28,38,.82)', 'color:#9fb3d1',
      'border:1px solid rgba(120,150,200,.35)',
      'user-select:none', 'backdrop-filter:blur(4px)'
    ].join(';');
    el.onclick = function () {
      if (!boot) return;
      var f = (boot.features || []).join('\n');
      alert('DGX Agent Studio\n版本 ' + boot.version +
        '（' + boot.channel + '）\n构建 ' + fmtTs(boot.build_ts) +
        '\n最低客户端 ' + boot.min_client +
        '\n\n特性:\n' + (f || '-'));
    };
    document.body.appendChild(el);
    return el;
  }

  function showBanner(text, onClick) {
    if (bannerShown) return;
    bannerShown = true;
    var b = document.createElement('div');
    b.id = 'v17-update-banner';
    b.style.cssText = [
      'position:fixed', 'left:50%', 'top:0', 'transform:translateX(-50%)',
      'z-index:99999', 'padding:9px 18px', 'border-radius:0 0 10px 10px',
      'background:#2563eb', 'color:#fff', 'font:13px/1.4 system-ui,sans-serif',
      'box-shadow:0 4px 16px rgba(0,0,0,.35)', 'cursor:pointer'
    ].join(';');
    b.textContent = text;
    b.onclick = onClick;
    document.body.appendChild(b);
    setTimeout(function () { if (b.parentNode) b.remove(); bannerShown = false; }, 20000);
  }

  function hardReload() {
    try {
      location.reload(true);
    } catch (e) {
      var u = location.href.split('#')[0];
      location.replace(u + (u.indexOf('?') >= 0 ? '&' : '?') + '_r=' + Date.now());
    }
  }

  function apply(info) {
    var badge = ensureBadge();
    badge.textContent = 'v' + (info.version || '?');
    if (!info.ok) {
      badge.textContent = 'v? 离线';
      badge.style.color = '#f0883e';
      return;
    }
    badge.style.color = '#9fb3d1';
    if (!boot) { boot = info; return; }
    if (info.version !== boot.version) {
      showBanner('服务端已更新到 v' + info.version + '（当前 v' + boot.version +
        '）— 点击刷新', hardReload);
      boot = info;
      return;
    }
    // 客户端被判定过旧
    try {
      var cur = (boot.version || '0.0.0').split('.').map(Number);
      var min = (info.min_client || '0.0.0').split('.').map(Number);
      for (var i = 0; i < 3; i++) {
        if ((cur[i] || 0) < (min[i] || 0)) {
          showBanner('当前版本低于最低要求 v' + info.min_client + '，请刷新升级', hardReload);
          break;
        }
        if ((cur[i] || 0) > (min[i] || 0)) break;
      }
    } catch (e) { /* 版本号畸形就别管 */ }
  }

  function poll() {
    fetch('/api/version', { credentials: 'include', cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(apply)
      .catch(function () {
        var b = ensureBadge();
        b.textContent = 'v? 离线';
        b.style.color = '#f0883e';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { poll(); });
  } else {
    poll();
  }
  setInterval(poll, POLL_MS);
  window.__dgsVersion = function () { return boot; };
})();
