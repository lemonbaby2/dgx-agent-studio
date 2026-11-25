/* v9user.js —— 账号 UI（v12 多用户）
 * 职责：顶栏显示当前账号、退出登录、改昵称/密码、管理员看用户列表。
 * 依赖全局：$ / api / toast（app.js 提供）。自行兜底，缺了也不报错。
 */
(function () {
  'use strict';

  var ME = null;
  var HAS_USERS = false;

  function q(id) { return document.getElementById(id); }
  function toast2(msg) {
    try { if (typeof toast === 'function') return toast(msg); } catch (e) {}
    console.log('[v9user]', msg);
  }
  function getJSON(path) {
    return fetch(path, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); });
  }
  function postJSON(path, data) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(data || {})
    }).then(function (r) { return r.json(); });
  }

  /* ---------------- 样式 ---------------- */
  function injectCSS() {
    if (q('v9user-css')) return;
    var css = document.createElement('style');
    css.id = 'v9user-css';
    css.textContent = [
      '.uc-wrap{position:relative;display:inline-block;margin-right:6px}',
      '.uc-chip{display:flex;align-items:center;gap:6px;padding:5px 10px;border-radius:9px;',
      ' background:#eef3fd;border:1px solid #d8e3f8;cursor:pointer;font-size:12.5px;color:#2b4a7d;',
      ' max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.uc-chip:hover{background:#e2ebfb}',
      '.uc-dot{width:7px;height:7px;border-radius:50%;background:#28a745;flex:none}',
      '.uc-role{font-size:10px;padding:1px 5px;border-radius:5px;background:#3b6ef5;color:#fff;flex:none}',
      '.uc-menu{position:absolute;right:0;top:36px;background:#fff;border:1px solid #dfe6f2;',
      ' border-radius:11px;box-shadow:0 6px 24px rgba(20,40,80,.14);padding:6px;min-width:180px;',
      ' z-index:9999;display:none}',
      '.uc-menu.on{display:block}',
      '.uc-item{padding:9px 12px;border-radius:8px;font-size:13px;color:#2b3a52;cursor:pointer;',
      ' white-space:nowrap}',
      '.uc-item:hover{background:#f2f6fd}',
      '.uc-item.danger{color:#c62828}',
      '.uc-sep{height:1px;background:#eef2f8;margin:5px 4px}',
      '.uc-mask{position:fixed;inset:0;background:rgba(12,20,34,.5);z-index:10000;',
      ' display:flex;align-items:center;justify-content:center}',
      '.uc-modal{background:#fff;border-radius:14px;padding:22px 24px;width:min(460px,92vw);',
      ' box-shadow:0 24px 64px rgba(0,0,0,.35);max-height:82vh;overflow:auto}',
      '.uc-modal h3{margin:0 0 14px;font-size:16px;color:#16233a}',
      '.uc-modal label{display:block;font-size:12px;color:#7a8798;margin:10px 0 4px}',
      '.uc-modal input{width:100%;padding:9px 11px;border:1px solid #d5dce8;border-radius:8px;',
      ' font-size:14px;outline:none;box-sizing:border-box}',
      '.uc-modal input:focus{border-color:#3b6ef5}',
      '.uc-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}',
      '.uc-btn{padding:8px 16px;border-radius:8px;border:1px solid #d5dce8;background:#fff;',
      ' font-size:13px;cursor:pointer;color:#2b3a52}',
      '.uc-btn.primary{background:#3b6ef5;border-color:#3b6ef5;color:#fff}',
      '.uc-btn:hover{filter:brightness(.97)}',
      '.uc-err{color:#d33;font-size:12.5px;margin-top:8px;min-height:16px}',
      '.uc-tbl{width:100%;border-collapse:collapse;font-size:13px}',
      '.uc-tbl th{text-align:left;color:#8494aa;font-weight:600;font-size:11.5px;',
      ' padding:6px 8px;border-bottom:1px solid #eef2f8}',
      '.uc-tbl td{padding:8px;border-bottom:1px solid #f5f8fc;color:#2b3a52}',
      '.uc-tag{font-size:10.5px;padding:1px 6px;border-radius:5px;background:#eef3fd;color:#3b6ef5}'
    ].join('');
    document.head.appendChild(css);
  }

  /* ---------------- 顶栏芯片 ---------------- */
  function paintChip() {
    var host = q('uc-host');
    if (!host) return;
    if (!ME) { host.innerHTML = ''; return; }
    var name = ME.display || ME.name;
    var role = ME.role === 'admin' ? '<span class="uc-role">管理员</span>' : '';
    host.innerHTML =
      '<div class="uc-wrap" id="uc-wrap">' +
      '  <div class="uc-chip" id="uc-chip" title="' + name + '">' +
      '    <span class="uc-dot"></span><span>' + name + '</span>' + role +
      '  </div>' +
      '  <div class="uc-menu" id="uc-menu">' +
      '    <div class="uc-item" data-act="profile">改昵称</div>' +
      '    <div class="uc-item" data-act="pwd">改密码</div>' +
      (ME.role === 'admin' ? '<div class="uc-item" data-act="users">用户管理</div>' : '') +
      '    <div class="uc-sep"></div>' +
      '    <div class="uc-item danger" data-act="logout">退出登录</div>' +
      '  </div>' +
      '</div>';

    var chip = q('uc-chip');
    var menu = q('uc-menu');
    chip.onclick = function (ev) {
      ev.stopPropagation();
      menu.classList.toggle('on');
    };
    document.addEventListener('click', function () { menu.classList.remove('on'); });
    menu.addEventListener('click', function (ev) {
      var it = ev.target.closest && ev.target.closest('.uc-item');
      if (!it) return;
      menu.classList.remove('on');
      var act = it.getAttribute('data-act');
      if (act === 'logout') doLogout();
      else if (act === 'profile') doProfile();
      else if (act === 'pwd') doPwd();
      else if (act === 'users') doUsers();
    });
  }

  /* ---------------- 弹层 ---------------- */
  function modal(title, innerHtml) {
    var m = document.createElement('div');
    m.className = 'uc-mask';
    m.innerHTML = '<div class="uc-modal"><h3>' + title + '</h3>' + innerHtml + '</div>';
    m.addEventListener('click', function (ev) {
      if (ev.target === m) document.body.removeChild(m);
    });
    document.body.appendChild(m);
    return m;
  }
  function close(m) { try { m && m.parentNode && m.parentNode.removeChild(m); } catch (e) {} }

  function doLogout() {
    postJSON('/api/logout').then(function () { location.reload(); });
  }

  function doProfile() {
    var m = modal('改昵称',
      '<label>昵称</label><input id="uc-disp" value="' +
      ((ME.display || ME.name) + '').replace(/"/g, '&quot;') + '">' +
      '<div class="uc-err" id="uc-e1"></div>' +
      '<div class="uc-actions"><button class="uc-btn" id="uc-c1">取消</button>' +
      '<button class="uc-btn primary" id="uc-ok1">保存</button></div>');
    m.querySelector('#uc-c1').onclick = function () { close(m); };
    m.querySelector('#uc-ok1').onclick = function () {
      postJSON('/api/account/display', { display: m.querySelector('#uc-disp').value })
        .then(function (d) {
          if (d.ok) { ME.display = d.display; paintChip(); close(m); toast2('昵称已更新'); }
          else m.querySelector('#uc-e1').textContent = d.error || '失败';
        });
    };
  }

  function doPwd() {
    var m = modal('改密码',
      '<label>原密码</label><input id="uc-old" type="password">' +
      '<label>新密码（至少 6 位）</label><input id="uc-new" type="password">' +
      '<div class="uc-err" id="uc-e2"></div>' +
      '<div class="uc-actions"><button class="uc-btn" id="uc-c2">取消</button>' +
      '<button class="uc-btn primary" id="uc-ok2">保存</button></div>');
    m.querySelector('#uc-c2').onclick = function () { close(m); };
    m.querySelector('#uc-ok2').onclick = function () {
      postJSON('/api/account/password', {
        old: m.querySelector('#uc-old').value,
        new: m.querySelector('#uc-new').value
      }).then(function (d) {
        if (d.ok) { close(m); toast2('密码已更新'); }
        else m.querySelector('#uc-e2').textContent = d.error || '失败';
      });
    };
  }

  function fmtSize(n) {
    if (!n) return '0 B';
    var u = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + u[i];
  }

  function doUsers() {
    var m = modal('用户管理　<span class="uc-tag">管理员</span>', '<div id="uc-ul">读取中…</div>');
    getJSON('/api/users').then(function (d) {
      var box = m.querySelector('#uc-ul');
      if (!d.ok) { box.textContent = d.error || '读取失败'; return; }
      var rows = (d.users || []).map(function (u) {
        var isMe = ME && u.uid === ME.uid;
        var actions = '';
        if (!isMe) {
          actions = '<button class="uc-btn" data-uid="' + u.uid + '" data-role="' +
            (u.role === 'admin' ? 'user' : 'admin') + '" data-op="role">设为' +
            (u.role === 'admin' ? '普通' : '管理员') + '</button> ' +
            '<button class="uc-btn" data-uid="' + u.uid + '" data-op="del">删除</button>';
        }
        return '<tr><td>' + (u.display || u.name) + (isMe ? ' <span class="uc-tag">我</span>' : '') +
          '</td><td>' + u.name + '</td><td>' + (u.role === 'admin' ? '管理员' : '普通') +
          '</td><td>' + fmtSize((u.usage || {}).bytes) + '</td><td>' + actions + '</td></tr>';
      }).join('');
      box.innerHTML = '<table class="uc-tbl"><thead><tr><th>昵称</th><th>用户名</th>' +
        '<th>角色</th><th>占用</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table>' +
        '<div class="uc-err" id="uc-e3"></div>';
      box.addEventListener('click', function (ev) {
        var b = ev.target.closest && ev.target.closest('button[data-uid]');
        if (!b) return;
        var uid = b.getAttribute('data-uid');
        var op = b.getAttribute('data-op');
        var p = op === 'role'
          ? postJSON('/api/users/role', { uid: uid, role: b.getAttribute('data-role') })
          : postJSON('/api/users/delete', { uid: uid });
        p.then(function (r) {
          if (r.ok) { close(m); doUsers(); toast2(op === 'role' ? '角色已更新' : '已删除'); }
          else { var e3 = box.querySelector('#uc-e3'); if (e3) e3.textContent = r.error || '失败'; }
        });
      });
    });
  }

  /* ---------------- 启动 ---------------- */
  function ensureHost() {
    var right = document.querySelector('#topbar .tb-right');
    if (!right) return false;
    if (q('uc-host')) return true;
    var span = document.createElement('span');
    span.id = 'uc-host';
    right.insertBefore(span, right.firstChild);
    return true;
  }

  function boot() {
    injectCSS();
    getJSON('/api/auth/status').then(function (d) {
      HAS_USERS = !!(d && d.has_users);
      ME = (d && d.user) || null;
      if (!ME) return;                 // 未登录：页面本身已被服务端拦到登录页
      var tries = 0;
      var t = setInterval(function () {
        if (ensureHost() || ++tries > 20) {
          clearInterval(t);
          paintChip();
        }
      }, 250);
    }).catch(function () {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
