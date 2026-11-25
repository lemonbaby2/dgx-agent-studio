/* ============================================================
   DGX Agent Studio · 文件面板
   工作区管理 / 目录树懒加载 / 文件预览 / 引用到对话
   ============================================================ */
'use strict';

window.Panel = (function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  let workspaces = [];
  let activeRoot = null;
  const expanded = new Set();      // 已展开目录
  const childrenCache = new Map(); // path -> items
  let pickMode = false;

  /* ---------------- 图标 ---------------- */
  const IC = {
    dir: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
    file: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
    img: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
    code: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg>',
    bin: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8v13H3V8M1 3h22l-2 5H3zM10 12v7M14 12v7"/></svg>',
    home: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
    disk: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="2" y="5" width="20" height="14" rx="2"/><circle cx="18" cy="15" r="1.2" fill="currentColor" stroke="none"/><path d="M2 9h20"></path></svg>',
  };
  function iconOf(e) {
    if (e.is_dir) return IC.dir;
    if (e.kind === 'image') return IC.img;
    if (e.lang) return IC.code;
    return IC.file;
  }
  function fmtSize(n) {
    if (!n) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }
  function baseName(p) { return String(p || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop(); }

  /* ---------------- 工作区 ---------------- */
  async function loadWorkspaces() {
    const d = await api('/api/fs/roots');
    workspaces = d.workspaces || [];
    renderWsList();
    if (!workspaces.length) {
      $('tree').innerHTML = '<div class="tree-hint">还没有挂载本地目录。<br>'
        + '点上方「添加目录」，把项目文件夹加进来，'
        + '之后就能浏览文件、@ 引用到对话。</div>';
      return;
    }
    if (!activeRoot || !workspaces.some(w => w.path === activeRoot)) {
      activeRoot = workspaces[0].path;
    }
    renderTree(activeRoot);
  }

  function renderWsList() {
    const box = $('ws-list');
    box.innerHTML = '';
    if (!workspaces.length) {
      box.innerHTML = '<div class="ws-empty">未挂载目录</div>';
      return;
    }
    workspaces.forEach(w => {
      const it = document.createElement('div');
      it.className = 'ws-item' + (w.path === activeRoot ? ' active' : '');
      it.innerHTML = '<span class="ws-ic">' + IC.home + '</span>'
        + '<span class="ws-nm" title="' + esc(w.path) + '">' + esc(w.name) + '</span>'
        + '<button class="ws-del" title="移除">✕</button>';
      it.onclick = (e) => {
        if (e.target.classList.contains('ws-del')) return;
        activeRoot = w.path;
        expanded.clear(); childrenCache.clear();
        renderWsList(); renderTree(w.path);
      };
      it.querySelector('.ws-del').onclick = async (e) => {
        e.stopPropagation();
        if (!confirm('移除「' + w.name + '」？\n只是取消挂载，不会删除磁盘上的文件。')) return;
        const d = await api('/api/ws/remove', { path: w.path });
        workspaces = d.workspaces || [];
        if (activeRoot === w.path) { activeRoot = null; expanded.clear(); childrenCache.clear(); }
        renderWsList();
        if (activeRoot) renderTree(activeRoot);
        else $('tree').innerHTML = '<div class="tree-hint">已移除全部挂载目录</div>';
      };
      box.appendChild(it);
    });
  }

  /* ---------------- 目录树 ---------------- */
  async function renderTree(rootPath) {
    const box = $('tree');
    box.innerHTML = '<div class="tr-load">加载中…</div>';
    const items = await listDir(rootPath);
    box.innerHTML = '';
    if (!items) return;
    const frag = buildNodes(items, rootPath);
    box.appendChild(frag);
  }

  function listDir(path) {
    return api('/api/fs/list?path=' + encodeURIComponent(path))
      .then(d => (d.ok ? d.items : null))
      .catch(() => null);
  }

  function buildNodes(items, parentPath) {
    const frag = document.createDocumentFragment();
    items.forEach(e => frag.appendChild(buildNode(e, parentPath)));
    return frag;
  }

  function buildNode(e, parentPath) {
    const wrap = document.createElement('div');
    wrap.className = 'tr-node';
    const row = document.createElement('div');
    row.className = 'tr-row';
    row.dataset.path = e.path;
    row.innerHTML = '<span class="tr-ic">' + iconOf(e) + '</span>'
      + '<span class="tr-nm" title="' + esc(e.path) + '">' + esc(e.name) + '</span>'
      + (e.is_dir ? '' : '<span class="tr-sz">' + fmtSize(e.size) + '</span>')
      + (e.is_dir ? '' : '<button class="tr-ref">引用</button>');
    row.onclick = (ev) => {
      if (ev.target.classList.contains('tr-ref')) return;
      if (e.is_dir) toggleDir(wrap, row, e);
      else previewFile(e);
    };
    const refBtn = row.querySelector('.tr-ref');
    if (refBtn) refBtn.onclick = (ev) => {
      ev.stopPropagation();
      addRef(e.path, e.name);
    };
    wrap.appendChild(row);
    if (e.is_dir) {
      const kids = document.createElement('div');
      kids.className = 'tr-kids';
      kids.hidden = true;
      wrap.appendChild(kids);
    }
    return wrap;
  }

  async function toggleDir(wrap, row, e) {
    const kids = wrap.querySelector('.tr-kids');
    if (!kids) return;
    if (expanded.has(e.path)) {
      expanded.delete(e.path);
      kids.hidden = true;
      kids.innerHTML = '';
      return;
    }
    expanded.add(e.path);
    kids.hidden = false;
    kids.innerHTML = '<div class="tr-load">加载中…</div>';
    let items = childrenCache.get(e.path);
    if (!items) {
      items = await listDir(e.path);
      childrenCache.set(e.path, items);
    }
    kids.innerHTML = '';
    if (!items) { kids.innerHTML = '<div class="tr-load">无法访问</div>'; return; }
    if (!items.length) { kids.innerHTML = '<div class="tr-load">（空目录）</div>'; return; }
    items.forEach(x => kids.appendChild(buildNode(x, e.path)));
  }

  /* ---------------- 预览 ---------------- */
  async function previewFile(e) {
    document.querySelectorAll('.tr-row.sel').forEach(x => x.classList.remove('sel'));
    document.querySelectorAll('.tr-row').forEach(x => {
      if (x.dataset.path === e.path) x.classList.add('sel');
    });
    switchTab('preview');
    const head = $('pv-head'), body = $('pv-body');
    head.innerHTML = '<span class="pv-name" title="' + esc(e.path) + '">'
      + esc(e.name) + '</span>'
      + '<button class="pv-act" id="pv-ref">引用到对话</button>'
      + '<button class="pv-act" id="pv-download">下载到本机</button>'
      + '<button class="pv-act" id="pv-copy">复制路径</button>';
    body.innerHTML = '<div class="pv-msg">加载中…</div>';
    $('pv-ref').onclick = () => addRef(e.path, e.name);
    $('pv-download').onclick = () => openPath(e.path, 'download');
    $('pv-copy').onclick = () => {
      navigator.clipboard.writeText(e.path).then(
        () => toast('已复制路径'), () => toast('复制失败'));
    };

    if (e.kind === 'image') {
      const d = await api('/api/fs/image?path=' + encodeURIComponent(e.path));
      if (!d.ok) { body.innerHTML = '<div class="pv-msg">' + esc(d.error) + '</div>'; return; }
      body.innerHTML = '<div class="pv-meta">' + esc(d.name) + ' · '
        + fmtSize(d.size) + '</div>'
        + '<div class="pv-img"><img src="' + d.data + '" alt=""></div>';
      return;
    }
    if ((e.ext || '').toLowerCase() === '.pdf') {
      const url = '/api/fs/file?path=' + encodeURIComponent(e.path);
      body.innerHTML = '<div class="pv-meta">PDF · ' + fmtSize(e.size) + '</div>'
        + '<iframe class="pv-pdf" src="' + url + '" title="' + esc(e.name) + '"></iframe>';
      return;
    }
    if (e.kind === 'binary' || e.kind === 'other') {
      body.innerHTML = '<div class="pv-msg">' + esc(e.name)
        + '<br>二进制 / 非文本文件（' + fmtSize(e.size) + '）<br>请用外部编辑器打开</div>';
      return;
    }
    const d = await api('/api/fs/read?path=' + encodeURIComponent(e.path));
    if (!d.ok) {
      body.innerHTML = '<div class="pv-msg">' + esc(d.error) + '</div>'
        + '<div class="pv-code">' + esc((d.error || '')) + '</div>';
      return;
    }
    const lines = d.content.split('\n');
    const show = lines.slice(0, 2000);
    body.innerHTML = '<div class="pv-meta">' + esc(d.lines) + ' 行 · '
      + fmtSize(d.size) + ' · ' + esc(d.encoding)
      + (d.truncated ? ' · <span style="color:var(--err)">已截断</span>' : '')
      + '</div><div class="pv-code" id="pv-code">'
      + show.map((l, i) =>
        '<span class="ln">' + (i + 1) + '</span>' + esc(l) + '\n').join('')
      + (lines.length > show.length
        ? '<span style="color:#9ca3af">… 其余 ' + (lines.length - show.length)
        + ' 行未显示（可引用全文到对话）</span>' : '')
      + '</div>';
  }

  function switchTab(name) {
    document.querySelectorAll('.ptab').forEach(t =>
      t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.ptab-body').forEach(b => {
      b.hidden = b.dataset.tab !== name;
    });
  }

  /* ---------------- 搜索 ---------------- */
  let searchTimer = null;
  function onSearch(v) {
    clearTimeout(searchTimer);
    const kw = (v || '').trim();
    if (!kw) {
      if (activeRoot) renderTree(activeRoot);
      else $('tree').innerHTML = '';
      return;
    }
    searchTimer = setTimeout(async () => {
      const box = $('tree');
      box.innerHTML = '<div class="tr-load">搜索中…</div>';
      const d = await api('/api/fs/search', { keyword: kw, root: activeRoot || null, limit: 200 });
      box.innerHTML = '';
      if (!d.ok) { box.innerHTML = '<div class="tr-load">' + esc(d.error) + '</div>'; return; }
      const h = document.createElement('div');
      h.className = 'tr-load';
      h.textContent = '找到 ' + d.count + ' 个匹配（文件名）';
      box.appendChild(h);
      if (!d.items.length) return;
      const frag = document.createDocumentFragment();
      d.items.forEach(e => {
        const row = document.createElement('div');
        row.className = 'tr-row';
        row.innerHTML = '<span class="tr-ic">' + iconOf(e) + '</span>'
          + '<span class="tr-nm" title="' + esc(e.path) + '">'
          + esc(e.path) + '</span>'
          + (e.is_dir ? '' : '<button class="tr-ref">引用</button>');
        row.onclick = (ev) => {
          if (ev.target.classList.contains('tr-ref')) return;
          if (e.is_dir) {
            activeRoot = e.path; expanded.clear(); childrenCache.clear();
            renderWsList(); renderTree(e.path);
          } else previewFile(e);
        };
        const b = row.querySelector('.tr-ref');
        if (b) b.onclick = (ev) => { ev.stopPropagation(); addRef(e.path, e.name); };
        frag.appendChild(row);
      });
      box.appendChild(frag);
    }, 280);
  }

  /* ---------------- 添加目录弹层 ---------------- */
  async function openWsModal() {
    $('modal-ws').hidden = false;
    $('ws-path').value = '';
    $('ws-path').focus();
    const quick = $('ws-quick');
    quick.innerHTML = '<span class="tr-load">加载快捷目录…</span>';
    try {
      const d = await api('/api/ws/browse');
      quick.innerHTML = '';
      d.items.forEach(it => {
        const b = document.createElement('button');
        b.className = 'mq-item';
        const free = it.total ? '（剩 ' + (it.free / 1073741824).toFixed(0) + ' GB）' : '';
        b.textContent = it.name + '  ' + it.path + free;
        b.title = it.path;
        b.onclick = () => { $('ws-path').value = it.path; };
        quick.appendChild(b);
      });
    } catch (e) {
      quick.innerHTML = '<span class="tr-load">加载失败</span>';
    }
  }

  async function doAddWorkspace() {
    const p = $('ws-path').value.trim();
    if (!p) { toast('请填写目录路径'); return; }
    const d = await api('/api/ws/add', { path: p });
    if (!d.ok) { toast(d.msg || d.error); return; }
    workspaces = d.workspaces || [];
    activeRoot = p;
    expanded.clear(); childrenCache.clear();
    $('modal-ws').hidden = true;
    renderWsList();
    renderTree(p);
    toast('已挂载：' + baseName(p));
  }

  /* ---------------- 对外 ---------------- */
  function init() {
    $('ws-add').onclick = openWsModal;
    $('ws-ok').onclick = doAddWorkspace;
    $('ws-cancel').onclick = () => ($('modal-ws').hidden = true);
    $('ws-path').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doAddWorkspace();
    });
    $('modal-ws').addEventListener('click', (e) => {
      if (e.target === $('modal-ws')) $('modal-ws').hidden = true;
    });
    $('ws-search').addEventListener('input', (e) => onSearch(e.target.value));
    document.querySelectorAll('.ptab').forEach(t =>
      t.onclick = () => switchTab(t.dataset.tab));
    loadWorkspaces();
  }

  function focusPick() {
    const box = $('tree');
    if (box) box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    toast('在右侧目录里点文件的「引用」按钮');
  }

  async function openPath(path, mode) {
    if (!path) return;
    if (mode === 'download') {
      const a = document.createElement('a');
      a.href = '/api/fs/file?download=1&path=' + encodeURIComponent(path);
      a.download = '';
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast('已交给浏览器下载');
      return;
    }
    const e = await api('/api/fs/stat?path=' + encodeURIComponent(path));
    if (!e.ok) { toast(e.error || '文件无法预览'); return; }
    const panel = $('panel');
    if (panel) panel.classList.add('open');
    await previewFile(e);
  }

  return { init, focusPick, refresh: loadWorkspaces, openPath };
})();
