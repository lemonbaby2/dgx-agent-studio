/* DGX Agent Studio v16：技能库抽屉
 * 把原先挤在输入框上方的一大片 chip 全部收进右侧抽屉，
 * 侧栏留一个「技能库」按钮，点开才出现，不再占用对话区。
 * 依赖 app.js 全局：$, esc, api, toast, S
 * 依赖 v6ui.js：window.v7PickPdf（论文类技能走 PDF 选择）
 */
(function () {
  'use strict';

  var CACHE = null, CACHE_TS = 0, CUR_CAT = 'all';

  /* ---------- 分类规则（按顺序命中即止） ---------- */
  var RULES = [
    ['论文与文档', /paper|论文|pdf|docs?\b|doc-export|思维导图|精读|总结|创新点|检索/],
    ['开发与代码', /code|代码|dev-|embedded|circuit|algo|quant|distill|边缘|嵌入式|电路|量化|算法|git|编译|调试/],
    ['运维与巡检', /lan-ops|巡检|运维|健康|dgx|docker|服务|guard|funnel|带宽|链路/],
    ['模型与训练', /train|训练|model|模型|标注|cvat|数据集|导出|评测/],
    ['笔记与办公', /note|笔记|email|邮件|brief|会议|周报|整理/],
    ['自主与编排', /agent|managed|自主|编排|harvest|自我|学习|技能/]
  ];

  function catOf(s) {
    var t = ((s.id || '') + ' ' + (s.name || '') + ' ' +
      ((s.trigger || []).join(' ')) + ' ' + (s.desc || '')).toLowerCase();
    for (var i = 0; i < RULES.length; i++) {
      if (RULES[i][1].test(t)) return RULES[i][0];
    }
    return '其它';
  }

  /* ---------- 数据 ---------- */
  async function fetchSkills(force) {
    if (!force && CACHE && Date.now() - CACHE_TS < 60000) return CACHE;
    var d = await api('/api/skills');
    CACHE = (d && d.skills) || [];
    CACHE_TS = Date.now();
    return CACHE;
  }

  /* ---------- 渲染 ---------- */
  function chipHTML(s) {
    var cls = 'sk-chip' + (s.type === 'document' ? ' sk-doc' : '');
    return '<button class="' + cls + '" data-id="' + esc(s.id) + '" title="' +
      esc((s.desc || s.name) + ' · 点击填入 /' + s.id) + '">' +
      '<span class="sk-ico">⚡</span><span class="sk-name">' + esc(s.name) + '</span>' +
      (s.type === 'document' ? '<span class="sk-badge">PDF</span>' : '') +
      '</button>';
  }

  function render(keyword) {
    var body = $('sk-body');
    var tabs = $('sk-tabs');
    if (!body) return;
    var all = CACHE || [];
    var kw = (keyword || '').trim().toLowerCase();

    // 统计分类
    var cats = {}, order = ['all'];
    all.forEach(function (s) {
      var c = catOf(s);
      cats[c] = (cats[c] || 0) + 1;
      if (order.indexOf(c) < 0) order.push(c);
    });

    // tabs
    tabs.innerHTML = '';
    order.forEach(function (c) {
      if (c !== 'all' && !cats[c]) return;
      var b = document.createElement('button');
      b.className = 'sk-tab' + (CUR_CAT === c ? ' on' : '');
      b.textContent = (c === 'all' ? '全部' : c) + ' ' + (c === 'all' ? all.length : cats[c]);
      b.onclick = function () { CUR_CAT = c; render($('sk-search').value); };
      tabs.appendChild(b);
    });

    // 列表
    var hit = all.filter(function (s) {
      if (CUR_CAT !== 'all' && catOf(s) !== CUR_CAT) return false;
      if (!kw) return true;
      var t = ((s.id || '') + ' ' + (s.name || '') + ' ' + (s.desc || '') + ' ' +
        ((s.trigger || []).join(' '))).toLowerCase();
      return t.indexOf(kw) >= 0;
    });

    if (!hit.length) {
      body.innerHTML = '<div class="sk-empty">没有匹配的技能' +
        (kw ? '（关键词：' + esc(keyword) + '）' : '') + '</div>';
      paintCount(all.length, 0);
      return;
    }

    // 按分类聚合（否则同一分类会分散出现多次，标题重复）
    var groups = [], gmap = {};
    hit.forEach(function (s) {
      var c = catOf(s);
      if (!gmap[c]) { gmap[c] = { name: c, items: [] }; groups.push(gmap[c]); }
      gmap[c].items.push(s);
    });
    groups.sort(function (a, b) {
      var ia = order.indexOf(a.name), ib = order.indexOf(b.name);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });

    var html = '';
    groups.forEach(function (g) {
      if (CUR_CAT === 'all') html += '<div class="sk-cat">' + esc(g.name) + '</div>';
      g.items.forEach(function (s) { html += chipHTML(s); });
    });
    body.innerHTML = html;

    body.querySelectorAll('.sk-chip').forEach(function (b) {
      b.onclick = function () { pick(b.dataset.id); };
    });
    paintCount(all.length, hit.length);
  }

  function paintCount(total, shown) {
    var n = $('sk-count');
    if (n) n.textContent = shown === total ? String(total) : shown + ' / ' + total;
  }

  /* ---------- 选中一个技能 ---------- */
  function pick(id) {
    var s = (CACHE || []).filter(function (x) { return x.id === id; })[0];
    if (!s) return;
    var inp = $('input');
    if (!inp) return;
    inp.value = '/' + s.id + ' ';
    close();
    inp.focus();
    if (typeof autoGrow === 'function') autoGrow(inp);
    // 论文/文档类：先留痕在输入框，再弹 PDF 选择，避免"点了没反应"的错觉
    if (s.type === 'document' && typeof window.v7PickPdf === 'function') {
      window.v7PickPdf();
      return;
    }
    if (typeof toast === 'function') toast('已选中技能：' + s.name + '，补一句要求后回车执行');
  }

  /* ---------- 开关 ---------- */
  function open() {
    var d = $('sk-drawer');
    if (!d) return;
    d.hidden = false;
    requestAnimationFrame(function () { d.classList.add('on'); });
    var s = $('sk-search');
    if (s) s.value = '';
    CUR_CAT = 'all';
    fetchSkills(false).then(function () { render(''); }).catch(function () {
      var b = $('sk-body');
      if (b) b.innerHTML = '<div class="sk-empty">技能接口不可用</div>';
    });
    setTimeout(function () { if (s) s.focus(); }, 60);
  }

  function close() {
    var d = $('sk-drawer');
    if (!d) return;
    d.classList.remove('on');
    setTimeout(function () { d.hidden = true; }, 180);
  }

  function toggle() {
    var d = $('sk-drawer');
    if (!d) return;
    if (d.hidden) open(); else close();
  }

  /* ---------- 绑定 ---------- */
  function bind() {
    var nav = $('nav-skills');
    if (nav) {
      nav.onclick = function (e) {
        e.preventDefault();
        e.stopPropagation();
        // 不改视图，只开抽屉；保持原视图高亮
        document.querySelectorAll('.nav-item').forEach(function (n) {
          n.classList.toggle('active', n.dataset.view === (S && S.view || 'chat'));
        });
        toggle();
      };
    }
    var c = $('sk-close');
    if (c) c.onclick = close;
    var m = $('sk-mask');
    if (m) m.onclick = close;
    var s = $('sk-search');
    if (s) {
      s.oninput = function () { render(s.value); };
      s.onkeydown = function (e) {
        if (e.key === 'Escape') { close(); }
        if (e.key === 'Enter') {
          var first = $('sk-body').querySelector('.sk-chip');
          if (first) pick(first.dataset.id);
        }
      };
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        var d = $('sk-drawer');
        if (d && !d.hidden) close();
      }
    });
  }

  function boot() {
    if (!document.getElementById('sk-drawer')) return;   // 老页面没有抽屉就跳过
    bind();
    // 预热：进页面就把技能列表拉回来，点开抽屉不再等
    fetchSkills(false).catch(function () {});
    // 顶部按钮（若有）
    var tb = $('btn-skills');
    if (tb) tb.onclick = toggle;
    window.v16Skills = { open: open, close: close, toggle: toggle, refresh: function () {
      CACHE = null; return fetchSkills(true).then(function () { render(''); });
    } };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
