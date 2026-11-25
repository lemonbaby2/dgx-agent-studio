/* ============================================================
   DGX Agent Studio · 核心交互
   状态 / API / SSE 流式对话 / Markdown 渲染 / 会话管理
   ============================================================ */
'use strict';

/* ---------------- 状态 ---------------- */
const S = {
  config: {},
  models: [],
  sessions: [],
  current: null,
  session: null,          // 当前会话对象
  base: '',
  busy: false,
  showThink: true,
  agentMode: localStorage.getItem('dgs_agent') !== '0',   // v12 自主执行，默认开
  refs: [],               // 待发送的引用文件 [{path,name}]
  pendingImages: [],      // 待发送图片 dataURL
  treeState: {},          // 展开的目录 path -> [children]
  view: 'chat',
  panelOpen: true,
  previewPath: null,
  abort: null,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

/* ---------------- 提示 ---------------- */
let toastTimer = null;
function toast(msg, ms = 2400) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), ms);
}

/* ---------------- API ---------------- */
async function api(path, body) {
  const opt = body === undefined
    ? { method: 'GET' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const txt = await r.text();
  let d = {};
  try { d = JSON.parse(txt); } catch (e) { throw new Error('响应解析失败'); }
  if (!r.ok && !d.error) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

/* ---------------- Markdown（轻量，零依赖） ---------------- */
function md2html(md) {
  if (!md) return '';
  let t = String(md).replace(/\r\n/g, '\n');
  const blocks = [];
  // 先抽出代码块，避免内部被转义/替换
  t = t.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push('<pre><code class="lang-' + esc(lang) + '">'
      + esc(code.replace(/\n$/, '')) + '</code></pre>');
    return '\u0000B' + (blocks.length - 1) + '\u0000';
  });
  // 表格
  t = t.replace(/^\|(.+)\|\n\|[ :\-|]+\|\n((?:\|.*\|\n?)*)/gm,
    (m, head, bodyRows) => {
      const th = head.split('|').map(x => x.trim()).map(x => '<th>' + inline(x) + '</th>').join('');
      const trs = bodyRows.trim().split('\n').map(r =>
        '<tr>' + r.replace(/^\||\|$/g, '').split('|')
          .map(x => '<td>' + inline(x.trim()) + '</td>').join('') + '</tr>').join('');
      blocks.push('<table><thead><tr>' + th + '</tr></thead><tbody>'
        + trs + '</tbody></table>');
      return '\u0000B' + (blocks.length - 1) + '\u0000';
    });
  const lines = t.split('\n');
  const out = [];
  let list = null, para = [], quote = [];
  const flushPara = () => {
    if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; }
  };
  const flushList = () => {
    if (list) { out.push('<' + list.tag + '>' + list.items.map(i => '<li>' + inline(i) + '</li>').join('') + '</' + list.tag + '>'); list = null; }
  };
  const flushQuote = () => {
    if (quote.length) { out.push('<blockquote>' + quote.map(inline).join('<br>') + '</blockquote>'); quote = []; }
  };
  const flushAll = () => { flushPara(); flushList(); flushQuote(); };

  for (const ln of lines) {
    if (/^\u0000B\d+\u0000$/.test(ln.trim())) { flushAll(); out.push(ln.trim()); continue; }
    let m;
    if ((m = ln.match(/^(#{1,4})\s+(.*)$/))) {
      flushAll();
      out.push('<h' + m[1].length + '>' + inline(m[2]) + '</h' + m[1].length + '>');
    } else if (/^\s*[-*+]\s+/.test(ln)) {
      flushPara(); flushQuote();
      if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; }
      list.items.push(ln.replace(/^\s*[-*+]\s+/, ''));
    } else if ((m = ln.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
      flushPara(); flushQuote();
      if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; }
      list.items.push(m[2]);
    } else if (/^\s*>\s?/.test(ln)) {
      flushPara(); flushList();
      quote.push(ln.replace(/^\s*>\s?/, ''));
    } else if (/^\s*(---|\*\*\*|___)\s*$/.test(ln)) {
      flushAll(); out.push('<hr>');
    } else if (ln.trim() === '') {
      flushAll();
    } else {
      flushList(); flushQuote();
      para.push(ln);
    }
  }
  flushAll();
  let html = out.join('\n');
  html = html.replace(/\u0000B(\d+)\u0000/g, (m, i) => blocks[+i]);
  return html;
}
function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, (m, c) => '<code>' + c + '</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}

/* ---------------- 会话列表 ---------------- */
function dayGroup(ts) {
  const d = new Date(ts * 1000), n = new Date();
  const dd = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = (dd(n) - dd(d)) / 86400000;
  if (diff <= 0) return '今天';
  if (diff <= 1) return '昨天';
  if (diff <= 7) return '近 7 天';
  if (diff <= 30) return '近 30 天';
  return '更早';
}

function renderSessions() {
  const box = $('sess-list');
  box.innerHTML = '';
  $('sess-count').textContent = S.sessions.length;
  if (!S.sessions.length) {
    box.innerHTML = '<div class="sess-empty">还没有会话<br>点上方「新建对话」开始</div>';
    return;
  }
  const groups = {};
  S.sessions.forEach(s => {
    const g = dayGroup(s.updated || s.created);
    (groups[g] = groups[g] || []).push(s);
  });
  ['今天', '昨天', '近 7 天', '近 30 天', '更早'].forEach(g => {
    if (!groups[g]) return;
    const h = document.createElement('div');
    h.className = 'sess-group';
    h.textContent = g;
    box.appendChild(h);
    groups[g].forEach(s => {
      const it = document.createElement('div');
      it.className = 'sess-item' + (s.id === S.current ? ' active' : '');
      it.dataset.sid = s.id;      // v14：并发生成时靠它打「生成中」标记
      // v10：标出归属。owner 为空的是历史/公共会话，所有人都能看
      const badge = s.shared
        ? ' · <span style="color:#b45309">公共</span>'
        : (s.owner_name ? ' · ' + esc(s.owner_name) : '');
      it.innerHTML = '<div class="sess-title">' + esc(s.title) + '</div>'
        + '<div class="sess-meta">' + esc(s.meta || '') + badge + '</div>';
      it.onclick = () => selectSession(s.id);
      it.oncontextmenu = (e) => {
        e.preventDefault();
        showSessionMenu(e, s);
      };
      box.appendChild(it);
    });
  });
}

function showSessionMenu(e, s) {
  const old = document.querySelector('.sess-menu');
  if (old) old.remove();
  const m = document.createElement('div');
  m.className = 'sess-menu';
  m.style.cssText = 'position:fixed;z-index:120;background:#fff;border:1px solid var(--border);'
    + 'border-radius:9px;box-shadow:var(--shadow-lg);padding:5px;min-width:120px;font-size:12.5px;';
  const mk = (txt, fn, danger) => {
    const b = document.createElement('div');
    b.textContent = txt;
    b.style.cssText = 'padding:7px 11px;border-radius:6px;cursor:pointer;'
      + (danger ? 'color:var(--err)' : 'color:var(--text)');
    b.onmouseenter = () => b.style.background = danger ? '#fef2f2' : '#f1f2f5';
    b.onmouseleave = () => b.style.background = 'none';
    b.onclick = () => { m.remove(); fn(); };
    return b;
  };
  m.appendChild(mk('重命名', () => {
    const t = prompt('会话标题：', s.title);
    if (t && t.trim()) renameSession(s.id, t.trim());
  }));
  m.appendChild(mk('删除', () => {
    if (confirm('删除会话「' + s.title + '」？不可恢复。')) deleteSession(s.id);
  }, true));
  document.body.appendChild(m);
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(e.clientX, innerWidth - r.width - 8) + 'px';
  m.style.top = Math.min(e.clientY, innerHeight - r.height - 8) + 'px';
  const close = () => { m.remove(); document.removeEventListener('click', close); };
  setTimeout(() => document.addEventListener('click', close), 0);
}

/* ---------------- 对话渲染 ---------------- */
function renderChat() {
  const box = $('chat');
  box.innerHTML = '<div class="chat-inner" id="chat-inner"></div>';
  const inner = $('chat-inner');
  if (!S.session || !S.session.messages.length) {
    $('empty').style.display = 'flex';
    return;
  }
  $('empty').style.display = 'none';
  S.session.messages.forEach(m => {
    if (m.role === 'user') inner.appendChild(userNode(m));
    else if (m.role === 'assistant') inner.appendChild(aiNode(m));
  });
  scrollBottom();
}

function userNode(m) {
  const d = document.createElement('div');
  d.className = 'msg msg-user';
  let h = '<div><div class="ububble">' + esc(m.content || '')
    .replace(/\n/g, '<br>') + '</div>';
  if (m.n_images) {
    h += '<div class="uimgs">';
    for (let i = 0; i < m.n_images; i++) h += '<span class="uref">图片 ' + (i + 1) + '</span>';
    h += '</div>';
  }
  if (m.refs && m.refs.length) {
    h += '<div class="urefs">' + m.refs.map(r =>
      '<span class="uref">📎 ' + esc(r.name || r.path) + '</span>').join('') + '</div>';
  }
  h += '</div>';
  d.innerHTML = h;
  return d;
}

function aiNode(m, opts = {}) {
  const d = document.createElement('div');
  d.className = 'msg msg-ai';
  const model = S.session ? S.session.model : '';
  let h = '<div class="ai-avatar">D</div><div class="ai-main">'
    + '<div class="ai-meta">' + esc(model) + '</div>';
  if (m.tool_log && m.tool_log.length) h += toolsNode(m.tool_log, false);
  if (m.think) h += thinkNode(m.think);
  h += '<div class="ai-body">' + md2html(m.content) + '</div></div>';
  d.innerHTML = h;
  const th = d.querySelector('.think-head');
  if (th) th.onclick = () => th.parentElement.classList.toggle('closed');
  return d;
}

function thinkNode(text) {
  return '<div class="think' + (S.showThink ? '' : ' closed') + '">'
    + '<div class="think-head"><span class="caret">▼</span>已思考'
    + ' <span style="font-weight:400;opacity:.75">（点击展开 / 收起）</span></div>'
    + '<div class="think-body">' + esc(text) + '</div></div>';
}

/* v12：执行轨迹渲染。live=true 时带 spinner，false 用 ✓/✗ */
function toolsNode(list, live) {
  if (!list || !list.length) return '';
  const icon = (t) => {
    const name = esc(t.name || '?');
    const argStr = esc(shortArgs(t.args));
    const mark = live && t.ok === undefined
      ? '<span class="tspin">⏳</span>'
      : (t.ok === false ? '<span style="color:var(--err)">✗</span>'
                        : '<span style="color:#16a34a">✓</span>');
    return '<div class="tool-row"><b>' + mark + ' 🛠 ' + name + '</b>'
      + (argStr ? '<span class="tool-args">' + argStr + '</span>' : '')
      + '</div>'
      + (t.result ? '<details class="tool-detail"><summary>结果</summary><pre>'
        + esc(previewResult(t.result)) + '</pre></details>' : '');
  };
  const running = live && list.some(t => t.ok === undefined);
  return '<div class="tools-block" style="margin:6px 0">'
    + '<div class="think-head" style="font-size:12.5px">'
    + (running ? '🤖 <b>正在自主执行</b>（第 ' + list.length + ' 步）…'
               : '🤖 <b>已自主执行 ' + list.length + ' 步</b>')
    + '</div>'
    + '<div style="border-left:2px solid var(--border);margin-left:6px;padding-left:10px">'
    + list.map(icon).join('') + '</div></div>';
}
function shortArgs(a) {
  try {
    const s = JSON.stringify(a);
    return s.length > 90 ? s.slice(0, 90) + '…' : s;
  } catch (e) { return ''; }
}
function previewResult(r) {
  let s = String(r == null ? '' : r);
  try { const o = JSON.parse(s); if (o && typeof o === 'object') s = JSON.stringify(o, null, 1); } catch (e) {}
  return s.length > 800 ? s.slice(0, 800) + '\n…（截断）' : s;
}
function handleAgentFileAction(raw) {
  let data = raw;
  try { if (typeof data === 'string') data = JSON.parse(data); } catch (e) { return; }
  if (!data || !data.action || !data.path || !window.Panel || !Panel.openPath) return;
  if (data.action === 'preview' || data.action === 'download') {
    Panel.openPath(data.path, data.action);
  }
}
function renderToolsLive(el, list, live) {
  if (!el) return;
  el.style.display = '';
  el.innerHTML = toolsNode(list, live);
  const heads = el.querySelectorAll('.tools-block .tool-detail summary');
}

function scrollBottom(force) {
  const c = $('chat');
  if (force || c.scrollTop + c.clientHeight > c.scrollHeight - 140) {
    requestAnimationFrame(() => { c.scrollTop = c.scrollHeight; });
  }
}

/* ---------------- 发送 / SSE ---------------- */
function setBusy(b) {
  S.busy = b;
  $('btn-send').disabled = b;
  $('btn-stop').hidden = !b;
  $('send-hint').textContent = b ? '' : 'Enter 发送';
}

async function send() {
  if (S.busy) return;
  const inp = $('input');
  let text = inp.value.trim();
  if (!text && !S.refs.length && !S.pendingImages.length) return;
  if (!S.session) { await newSession(); }
  inp.value = '';
  autoGrow(inp);

  const refs = S.refs.slice();
  const images = S.pendingImages.slice();
  S.refs = []; S.pendingImages = [];
  renderRefChips();

  setBusy(true);
  $('status-text').textContent = '正在生成…';
  $('empty').style.display = 'none';
  const inner = $('chat-inner');

  // 立即显示用户消息
  const uNode = userNode({
    role: 'user', content: text, n_images: images.length, refs,
  });
  inner.appendChild(uNode);
  const aiBox = document.createElement('div');
  aiBox.className = 'msg msg-ai';
  aiBox.innerHTML = '<div class="ai-avatar">D</div><div class="ai-main">'
    + '<div class="ai-meta">' + esc(S.session.model) + '</div>'
    + '<div class="think" id="think-live" style="display:none">'
    + '<div class="think-head"><span class="caret">▼</span>正在思考…</div>'
    + '<div class="think-body" id="think-body-live"></div></div>'
    + '<div id="tools-live" style="display:none"></div>'
    + '<div class="ai-body" id="ai-body-live"><span class="typing">'
    + '<i></i><i></i><i></i></span></div></div>';
  inner.appendChild(aiBox);
  scrollBottom(true);

  const bodyEl = $('ai-body-live');
  const thinkWrap = $('think-live');
  const thinkBody = $('think-body-live');
  const toolsWrap = $('tools-live');
  const statusEl = $('status-text');
  let acc = '', thinkAcc = '', thinkOpen = false, firstDelta = true;
  const toolAcc = [];   // v12 执行轨迹 [{name,args,ok,result,round}]

  const flusher = setInterval(() => {
    if (thinkOpen) thinkBody.textContent = thinkAcc;
    if (toolAcc.length && toolsWrap) renderToolsLive(toolsWrap, toolAcc, true);
    if (acc || firstDelta) {
      bodyEl.innerHTML = md2html(acc) || '<span class="typing"><i></i><i></i><i></i></span>';
      firstDelta = false;
    }
    scrollBottom();
  }, 90);

  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: S.session.id, text, images, refs,
        temperature: S.config.temperature || 0.7,
        show_think: S.showThink,
        provider: S.session.provider || 'ollama',   // v11：云端模型走统一适配层
        agent_mode: S.agentMode,                    // v12：自主执行（本地+云端通用）
      }),
    });
    if (!r.ok || !r.body) throw new Error('请求失败 HTTP ' + r.status);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          let d;
          try { d = JSON.parse(line.slice(6)); } catch (e) { continue; }
          if (d.type === 'think_start' || d.type === 'think') {
            thinkOpen = true;
            thinkWrap.style.display = '';
            thinkAcc += d.text || '';
          } else if (d.type === 'delta') {
            acc += d.text || '';
          } else if (d.type === 'think_end') {
            thinkOpen = false;
            const h = thinkWrap.querySelector('.think-head');
            if (h && thinkAcc) h.innerHTML = '<span class="caret">▼</span>已完成思考';
          } else if (d.type === 'tool_start') {
            toolAcc.push({ name: d.name, args: d.args || {}, round: (toolAcc.length + 1) });
            statusEl && (statusEl.textContent = '🛠 正在执行：' + d.name + '…');
          } else if (d.type === 'tool_result') {
            handleAgentFileAction(d.result);
            // 与最近的同名 start 配对
            for (let i = toolAcc.length - 1; i >= 0; i--) {
              if (toolAcc[i].name === d.name && toolAcc[i].ok === undefined) {
                toolAcc[i].ok = d.ok;
                toolAcc[i].result = d.result || '';
                break;
              }
            }
          } else if (d.type === 'stopped') {
            acc += '\n\n_（已手动停止）_';
          } else if (d.type === 'error') {
            acc += '\n\n> ⚠️ ' + d.message;
          } else if (d.type === 'end') {
            if (d.session) { S.session = d.session; }
          }
        }
      }
    }
  } catch (e) {
    acc += '\n\n> ⚠️ 连接失败：' + e.message;
  } finally {
    clearInterval(flusher);
  }

  // 收尾：把流式节点换成正式节点
  const finalMsg = { role: 'assistant', content: acc };
  if (thinkAcc) finalMsg.think = thinkAcc;
  if (toolAcc.length) {
    finalMsg.tool_log = toolAcc.map(t => ({
      name: t.name, args: t.args, ok: t.ok,
      result_preview: String(t.result || '').slice(0, 600),
    }));
  }
  aiBox.replaceWith(aiNode(finalMsg));
  scrollBottom(true);
  setBusy(false);
  $('status-text').textContent = '就绪';
  refreshSessions();
}

function stopGen() {
  if (!S.busy || !S.session) return;
  api('/api/chat/stop', { id: S.session.id }).catch(() => {});
}

/* ---------------- 会话操作 ---------------- */
async function refreshSessions() {
  const d = await api('/api/sessions');
  S.sessions = d.sessions || [];
  S.current = d.current;
  const cur = S.sessions.find(s => s.id === S.current);
  if (cur) S.session = cur;
  renderSessions();
}

async function selectSession(id) {
  if (S.busy) { toast('正在生成中，请先停止'); return; }
  const d = await api('/api/session/select', { id });
  S.session = d.session;
  S.current = id;
  renderSessions();
  renderChat();
  syncModelSelect();
}

async function newSession() {
  if (S.busy) { toast('正在生成中，请先停止'); return; }
  const d = await api('/api/session/new', {
    model: S.config.model, base: S.config.base,
  });
  S.session = d.session;
  await refreshSessions();
  renderChat();
  syncModelSelect();
  $('input').focus();
}

async function renameSession(id, title) {
  await api('/api/session/rename', { id, title });
  await refreshSessions();
}

async function deleteSession(id) {
  const d = await api('/api/session/delete', { id });
  S.sessions = d.sessions;
  S.current = d.current;
  S.session = S.sessions.find(s => s.id === d.current);
  renderSessions();
  renderChat();
}

async function clearSession() {
  if (!S.session) return;
  if (!confirm('清空当前会话的所有消息？')) return;
  const d = await api('/api/session/clear', { id: S.session.id });
  S.session = d.session;
  renderChat();
  refreshSessions();
}

/* ---------------- 模型 ---------------- */
async function refreshModels(silent) {
  const base = $('sel-base').value || S.config.base;
  if (!silent) $('status-text').textContent = '正在获取模型列表…';
  try {
    const d = await api('/api/models?base=' + encodeURIComponent(base));
    if (!d.ok) throw new Error(d.error);
    S.models = d.models || [];
    fillModelSelect();
    setConn(true, S.models.length + ' 个模型');
    if (!silent) {
      toast('已连接：' + base + '，' + S.models.length + ' 个模型');
      $('status-text').textContent = '就绪';
    }
  } catch (e) {
    setConn(false, '连接失败');
    $('status-text').textContent = 'DGX 未连接';
    if (!silent) toast('连接失败：' + e.message, 3600);
  }
}

function fillModelSelect() {
  const sel = $('sel-model');
  const keep = sel.value;
  sel.innerHTML = '';
  S.models.forEach(m => {
    const o = document.createElement('option');
    o.value = m.name;
    o.textContent = m.name + (m.param ? '  (' + m.param + ')' : '');
    sel.appendChild(o);
  });
  // v11：云端模型（豆包/千问等）追加进下拉，值格式 provider::model
  fillCloudModels(sel);
  if (S.session && S.session.model) {
    sel.value = S.session.provider && S.session.provider !== 'ollama'
      ? S.session.provider + '::' + S.session.model : S.session.model;
  } else if (keep) sel.value = keep;
  syncTags();
}

/* v11：把已配好密钥的云模型挂进模型下拉 */
let CLOUD_READY = [];
async function fillCloudModels(sel) {
  try {
    const r = await api('/api/providers');
    const ps = (r.providers || []).filter(p => p.has_key && p.id !== 'ollama');
    CLOUD_READY = ps;
    if (!ps.length) return;
    const og = document.createElement('optgroup');
    og.label = '── 云端模型（已配密钥）──';
    ps.forEach(p => {
      (p.models || []).slice(0, 12).forEach(m => {
        const o = document.createElement('option');
        o.value = p.id + '::' + m;
        o.textContent = '☁ ' + p.name.split('·').pop().trim() + ' · ' + m;
        og.appendChild(o);
      });
    });
    if (og.children.length) sel.appendChild(og);
  } catch (e) { /* 拉不到就不显示云模型，不影响本地 */ }
}

function syncModelSelect() {
  if (S.session && S.session.model) {
    const p = S.session.provider || 'ollama';
    $('sel-model').value = p !== 'ollama' ? p + '::' + S.session.model
                                          : S.session.model;
    syncTags();
  }
}

function syncTags() {
  const m = S.models.find(x => x.name === $('sel-model').value);
  const box = $('model-tags');
  box.innerHTML = '';
  if (!m) return;
  (m.tags || []).forEach(t => {
    const s = document.createElement('span');
    s.className = 'mtag' + (t === '视觉' ? ' vision' : '');
    s.textContent = t;
    box.appendChild(s);
  });
  const fm = document.createElement('span');
  fm.className = 'mtag';
  fm.style.cssText = 'background:#f1f2f5;color:var(--dim)';
  fm.textContent = (m.size / 1e9).toFixed(1) + ' GB';
  box.appendChild(fm);
}

async function changeModel() {
  const raw = $('sel-model').value;
  if (!raw || !S.session) return;
  // v11：云模型值格式 provider::model
  let model = raw, provider = 'ollama';
  const k = raw.indexOf('::');
  if (k > 0) { provider = raw.slice(0, k); model = raw.slice(k + 2); }
  if (S.session.model === model &&
      (S.session.provider || 'ollama') === provider) return;
  const d = await api('/api/session/update',
                      { id: S.session.id, model, provider });
  S.session = d.session;
  syncTags();
  renderChat();
  refreshSessions();
}

function setConn(ok, text) {
  const c = $('conn');
  c.className = 'conn ' + (ok ? 'ok' : 'err');
  $('conn-text').textContent = text;
  $('model-status').textContent = ok ? '' : '请检查 DGX 是否在线';
}

/* ---------------- 引用文件 chips ---------------- */
function renderRefChips() {
  const box = $('ref-chips');
  box.innerHTML = '';
  S.refs.forEach((r, i) => {
    const c = document.createElement('div');
    c.className = 'chip';
    c.innerHTML = '<span>📎 ' + esc(r.name) + '</span>';
    const x = document.createElement('button');
    x.textContent = '✕';
    x.onclick = () => { S.refs.splice(i, 1); renderRefChips(); };
    c.appendChild(x);
    box.appendChild(c);
  });
  S.pendingImages.forEach((im, i) => {
    const c = document.createElement('div');
    c.className = 'chip';
    c.innerHTML = '<img src="' + im + '" style="height:18px;border-radius:3px">'
      + '<span>图片 ' + (i + 1) + '</span>';
    const x = document.createElement('button');
    x.textContent = '✕';
    x.onclick = () => { S.pendingImages.splice(i, 1); renderRefChips(); };
    c.appendChild(x);
    box.appendChild(c);
  });
}

function addRef(path, name) {
  if (S.refs.some(r => r.path === path)) { toast('已引用过该文件'); return; }
  if (S.refs.length >= 8) { toast('单次最多引用 8 个文件'); return; }
  S.refs.push({ path, name: name || path.split(/[\\/]/).pop() });
  renderRefChips();
  toast('已引用：' + (name || path));
}

/* ---------------- 输入框 ---------------- */
function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 190) + 'px';
}

/* ---------------- 视图切换 ---------------- */
function switchView(v) {
  S.view = v;
  document.querySelectorAll('.nav-item').forEach(n =>
    n.classList.toggle('active', n.dataset.view === v));
  if (v === 'files') openPanel();
  if (v === 'models') {
    const sel = $('sel-model');
    if (sel) sel.focus();
    toast('顶部下拉即可切换模型，共 ' + S.models.length + ' 个');
  }
  if (v === 'settings') openSettings();
}

/* ---------------- 设置 ---------------- */
function openSettings() {
  const cfg = S.config;
  const b = $('cfg-base');
  b.innerHTML = '';
  (cfg.bases || []).forEach(x => {
    const o = document.createElement('option');
    o.value = x; o.textContent = x;
    b.appendChild(o);
  });
  b.value = cfg.base;
  const m = $('cfg-model');
  m.innerHTML = '';
  S.models.forEach(x => {
    const o = document.createElement('option');
    o.value = x.name; o.textContent = x.name;
    m.appendChild(o);
  });
  m.value = cfg.model;
  $('cfg-temp').value = cfg.temperature || 0.7;
  $('cfg-temp-v').textContent = cfg.temperature || 0.7;
  $('cfg-system').value = cfg.system || '';
  $('modal-cfg').hidden = false;
}

async function saveSettings() {
  const base = $('cfg-base').value.trim();
  await api('/api/config', { config: {
    base, model: $('cfg-model').value,
    temperature: parseFloat($('cfg-temp').value),
    system: $('cfg-system').value,
  }});
  await boot(false);
  $('modal-cfg').hidden = true;
  toast('已保存');
}

/* ---------------- 面板 ---------------- */
function openPanel() {
  S.panelOpen = true;
  $('panel').classList.add('open');
  $('panel').classList.remove('closed');
}
function closePanel() {
  S.panelOpen = false;
  $('panel').classList.remove('open');
  $('panel').classList.add('closed');
}

/* ---------------- 初始化 ---------------- */
async function boot(first = true) {
  const d = await api('/api/config');
  S.config = d.config || {};
  S.showThink = S.config.show_think !== false;
  $('ver-text').textContent = 'v' + (d.ver || '5.0');
  $('brand-sub').textContent = S.config.base || '';

  const bs = $('sel-base');
  bs.innerHTML = '';
  (S.config.bases || []).forEach(x => {
    const o = document.createElement('option');
    o.value = x; o.textContent = x.replace(/^https?:\/\//, '');
    bs.appendChild(o);
  });
  bs.value = S.config.base;

  $('btn-think2').classList.toggle('on', S.showThink);
  $('btn-toggle-think').classList.toggle('on', S.showThink);

  await refreshModels(first ? false : true);
  await refreshSessions();
  renderChat();
  syncModelSelect();
  if (window.Panel) Panel.init();
}

document.addEventListener('DOMContentLoaded', () => {
  // 输入框
  const inp = $('input');
  inp.addEventListener('input', () => autoGrow(inp));
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $('btn-send').onclick = send;
  $('btn-stop').onclick = stopGen;
  // v12：自主执行开关
  $('btn-agent').classList.toggle('on', S.agentMode);
  $('btn-agent').onclick = () => {
    S.agentMode = !S.agentMode;
    localStorage.setItem('dgs_agent', S.agentMode ? '1' : '0');
    $('btn-agent').classList.toggle('on', S.agentMode);
    toast(S.agentMode ? '自主执行已开启：我会自动调用工具干活，不只是回答'
                      : '自主执行已关闭：只回答，不动手');
  };
  $('btn-new').onclick = newSession;
  $('btn-clear').onclick = clearSession;
  $('btn-refresh').onclick = () => refreshModels(false);
  $('sel-model').onchange = changeModel;
  $('sel-base').onchange = async () => {
    await api('/api/config', { config: { base: $('sel-base').value } });
    await boot(false);
  };
  $('btn-toggle-think').onclick = () => {
    S.showThink = !S.showThink;
    $('btn-think2').classList.toggle('on', S.showThink);
    $('btn-toggle-think').classList.toggle('on', S.showThink);
    api('/api/config', { config: { show_think: S.showThink } }).catch(() => {});
    renderChat();
  };
  $('btn-think2').onclick = $('btn-toggle-think').onclick;

  $('btn-panel').onclick = () => (S.panelOpen ? closePanel() : openPanel());
  $('panel-close').onclick = closePanel;

  document.querySelectorAll('.nav-item').forEach(n =>
    n.onclick = () => switchView(n.dataset.view));

  // 图片上传
  $('btn-img').onclick = () => $('file-img').click();
  $('file-img').onchange = (e) => {
    const fs = Array.from(e.target.files || []);
    if (!fs.length) return;
    let left = fs.length;
    fs.forEach(f => {
      const r = new FileReader();
      r.onload = () => {
        S.pendingImages.push(r.result);
        if (--left === 0) { renderRefChips(); toast('已添加 ' + fs.length + ' 张图片'); }
      };
      r.readAsDataURL(f);
    });
    e.target.value = '';
  };

  // 引用文件（从面板选择）
  $('btn-file').onclick = () => { openPanel(); Panel.focusPick(); };

  // 设置弹层
  $('nav-settings') || document.querySelector('[data-view="settings"]');
  document.querySelector('[data-view="settings"]').onclick = openSettings;
  $('cfg-cancel').onclick = () => ($('modal-cfg').hidden = true);
  $('cfg-save').onclick = saveSettings;
  $('cfg-temp').oninput = (e) => { $('cfg-temp-v').textContent = e.target.value; };

  // 建议问题
  const suggs = [
    '帮我看看这条产线的良率怎么算',
    '写一个 Python 读取 CSV 的脚本',
    '解释一下红外焊接的关键参数',
    '这段报错是什么意思',
  ];
  const sb = $('sugg');
  suggs.forEach(t => {
    const b = document.createElement('button');
    b.className = 'sugg-item';
    b.textContent = t;
    b.onclick = () => { inp.value = t; autoGrow(inp); inp.focus(); };
    sb.appendChild(b);
  });

  boot(true).catch(e => {
    toast('初始化失败：' + e.message, 6000);
    console.error(e);
  });
});
