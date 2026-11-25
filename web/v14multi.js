/* ============================================================
   v14multi.js · 多会话并发生成
   ------------------------------------------------------------
   解决的问题：生成中整站被锁死，不能新建对话、不能切会话。
   做法：
   1. 生成状态按会话（sid）分开存，S.runs[sid]；
   2. 流在哪个会话就写哪个会话，切走就后台继续跑，切回接着看；
   3. 同一会话不允许并发两条流（会串内容），不同会话可并行；
   4. 侧栏给运行中的会话打「生成中」标记，「+ 新建对话」全程可用。
   ============================================================ */
(function () {
  'use strict';
  if (window.__v14multi) return;
  window.__v14multi = true;

  /* ---------- 状态 ---------- */
  S.runs = S.runs || {};        // sid -> {acc, think, toolAcc, streaming, queued, started}
  const REMOTE = {};            // sid -> 服务端运行态（别的标签页/设备发起的）

  function runOf(sid) { return S.runs[sid] || null; }
  function busyOf(sid) {
    const r = S.runs[sid];
    if (r && r.streaming) return true;
    const m = REMOTE[sid];
    return !!(m && m.busy);
  }

  /* ---------- 样式 ---------- */
  (function css() {
    const st = document.createElement('style');
    st.textContent = `
.sess-run{display:inline-block;width:7px;height:7px;border-radius:50%;
  background:#22c55e;box-shadow:0 0 0 0 rgba(34,197,94,.65);
  animation:v14pulse 1.6s infinite;margin-right:5px;vertical-align:middle}
.sess-run.q{background:#f59e0b;animation-duration:2.4s}
@keyframes v14pulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.55)}
  70%{box-shadow:0 0 0 7px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}
#v14-bar{position:fixed;right:16px;bottom:76px;z-index:60;display:none;
  background:#0f172a;color:#e2e8f0;border:1px solid #1e293b;border-radius:10px;
  padding:8px 12px;font-size:12px;box-shadow:0 8px 24px rgba(0,0,0,.35);cursor:pointer}
#v14-bar b{color:#38bdf8}
.live-caret{display:inline-block;width:7px;height:15px;background:#38bdf8;
  margin-left:2px;vertical-align:-2px;animation:v14blink 1s steps(2) infinite}
@keyframes v14blink{50%{opacity:0}}
.v14-queued{color:#b45309;font-size:12px;margin:4px 0 8px}`;
    document.head.appendChild(st);
  })();

  /* ---------- 发送按钮状态（只跟当前会话走） ---------- */
  function applyBusy() {
    const sid = S.current || (S.session && S.session.id);
    const b = sid ? busyOf(sid) : false;
    S.busy = b;                       // 兼容老代码里的读取
    const sendBtn = $('btn-send');
    const stopBtn = $('btn-stop');
    if (sendBtn) sendBtn.disabled = b;
    if (stopBtn) stopBtn.hidden = !b;
    const hint = $('send-hint');
    if (hint) hint.textContent = b ? '' : 'Enter 发送';
  }
  window.applyBusy = applyBusy;

  /* ---------- 浮动状态条 ---------- */
  function ensureBar() {
    let b = document.getElementById('v14-bar');
    if (!b) {
      b = document.createElement('div');
      b.id = 'v14-bar';
      b.onclick = () => {
        const sid = Object.keys(S.runs).find(k => S.runs[k].streaming)
          || Object.keys(REMOTE).find(k => REMOTE[k].busy);
        if (sid) selectSession(sid);
      };
      document.body.appendChild(b);
    }
    return b;
  }
  function paintBar() {
    const ids = new Set([].concat(
      Object.keys(S.runs).filter(k => S.runs[k].streaming),
      Object.keys(REMOTE).filter(k => REMOTE[k].busy)));
    const bar = ensureBar();
    if (!ids.size) { bar.style.display = 'none'; return; }
    bar.style.display = 'block';
    bar.innerHTML = '<b>' + ids.size + '</b> 个对话正在生成 · 点击切换';
  }

  /* ---------- 侧栏运行标记 ---------- */
  function paintDots() {
    document.querySelectorAll('#sess-list .sess-item').forEach((el) => {
      const sid = el.dataset.sid;
      if (!sid) return;
      const r = S.runs[sid], m = REMOTE[sid];
      const live = (r && r.streaming) || (m && m.busy);
      const queued = (r && r.queued) || (m && m.queued);
      const t = el.querySelector('.sess-title');
      if (!t) return;
      let d = t.querySelector('.sess-run');
      if (!live) { if (d) d.remove(); return; }
      if (!d) {
        d = document.createElement('i');
        d.className = 'sess-run';
        t.insertBefore(d, t.firstChild);
      }
      d.className = 'sess-run' + (queued ? ' q' : '');
      d.title = queued ? '排队中' : '生成中';
    });
    paintBar();
  }
  window.paintDots = paintDots;

  const _renderSessions = window.renderSessions || renderSessions;
  window.renderSessions = renderSessions = function () {
    _renderSessions.apply(this, arguments);
    // renderSessions 会重建 DOM，先补 data-sid 再打点
    const ids = (S.sessions || []).map(s => s.id);
    document.querySelectorAll('#sess-list .sess-item').forEach((el, i) => {
      if (!el.dataset.sid) el.dataset.sid = ids[i] || '';
    });
    paintDots();
  };

  /* app.js 渲染会话项时没写 data-sid，这里补一次 */
  (function hookSessClick() {
    const orig = document.getElementById('sess-list');
    if (!orig) return;
    const mo = new MutationObserver(() => {
      const ids = S.sessions.map(s => s.id);
      document.querySelectorAll('#sess-list .sess-item').forEach((el, i) => {
        if (el.dataset.sid) return;
        el.dataset.sid = ids[i] || '';
        el.addEventListener('click', paintBar, { passive: true });
      });
    });
    mo.observe(orig, { childList: true });
  })();

  /* ---------- 实时画布：所有会话共用一块画布，按 sid 切换 ---------- */
  function liveBox() {
    const inner = $('chat-inner');
    if (!inner) return null;
    const sid = S.current;
    let box = document.getElementById('v14-live');
    if (box && box.dataset.sid === sid) return box;
    if (box) box.remove();
    box = document.createElement('div');
    box.id = 'v14-live';
    box.className = 'msg msg-ai';
    box.dataset.sid = sid || '';
    box.innerHTML = '<div class="ai-avatar">D</div><div class="ai-main">'
      + '<div class="ai-meta">' + esc((S.session && S.session.model) || '') + '</div>'
      + '<div class="think" style="display:none"><div class="think-head">'
      + '<span class="caret">▼</span>正在思考…</div>'
      + '<div class="think-body"></div></div>'
      + '<div class="tools-holder" style="display:none"></div>'
      + '<div class="ai-body"><span class="typing"><i></i><i></i><i></i></span></div>'
      + '</div>';
    inner.appendChild(box);
    return box;
  }

  function tile(acc, streaming) {
    let h = md2html(acc);
    if (streaming) h += '<span class="live-caret"></span>';
    if (!h) h = '<span class="typing"><i></i><i></i><i></i></span>';
    return h;
  }

  function paintLive() {
    const sid = S.current;
    if (!sid) return;
    const r = S.runs[sid];
    const m = REMOTE[sid];
    if (!(r && r.streaming) && !(m && m.busy)) {
      const old = document.getElementById('v14-live');
      if (old) old.remove();
      return;
    }
    const box = liveBox();
    if (!box) return;
    const acc = (r && r.acc) || (m && m.text) || '';
    const think = (r && r.think) || (m && m.think) || '';
    const tools = (r && r.toolAcc) || (m && m.tools) || [];
    const streaming = (r && r.streaming) || (m && m.busy);
    const queued = (r && r.queued) || (m && m.queued);

    const tw = box.querySelector('.think');
    const tb = box.querySelector('.think-body');
    if (think) { tw.style.display = ''; tb.textContent = think; }
    else tw.style.display = 'none';

    const holder = box.querySelector('.tools-holder');
    if (tools && tools.length) {
      holder.style.display = '';
      holder.innerHTML = toolsNode(tools, true);
    } else holder.style.display = 'none';

    const body = box.querySelector('.ai-body');
    const q = queued ? '<div class="v14-queued">⏳ 已排队，前面还有回答在生成…</div>' : '';
    body.innerHTML = q + (queued && !acc ? '<span class="typing"><i></i><i></i><i></i></span>'
                                          : tile(acc, streaming));
    const st = $('status-text');
    if (st) {
      const sec = r ? ((Date.now() - r.started) / 1000).toFixed(1) + 's' : '';
      st.textContent = queued ? '排队中…' : ('正在生成 ' + sec);
    }
    scrollBottom();
  }

  /* 统一节流重绘 */
  let dirty = false;
  setInterval(() => {
    if (!dirty) return;
    dirty = false;
    paintLive();
    paintDots();
  }, 110);
  function markDirty() { dirty = true; }

  /* ---------- 远端运行态轮询（别的标签页/设备发起的） ---------- */
  const pollTimers = {};
  function pollOnce(sid) {
    if (S.runs[sid] && S.runs[sid].streaming) return;
    api('/api/session/live?id=' + encodeURIComponent(sid)).then(d => {
      if (!d || !d.ok) return;
      if (d.busy || d.done) {
        REMOTE[sid] = { busy: !!d.busy, text: d.text, think: d.think,
                        tools: d.tools || [], queued: !!d.queued };
        if (!d.busy && d.done) delete REMOTE[sid];
        markDirty(); applyBusy();
        if (!d.busy) { refreshSessions(); if (S.current === sid) renderChat(); }
      } else if (REMOTE[sid]) {
        delete REMOTE[sid]; markDirty(); applyBusy();
      }
    }).catch(() => {});
  }
  function startPoll(sid) {
    if (pollTimers[sid]) return;
    pollOnce(sid);
    pollTimers[sid] = setInterval(() => pollOnce(sid), 1200);
  }
  function stopPoll(sid) {
    clearInterval(pollTimers[sid]);
    delete pollTimers[sid];
    delete REMOTE[sid];
  }

  /* ---------- 会话操作：全程不锁 ---------- */
  window.selectSession = selectSession = async function (id) {
    if (!id || id === S.current) return;
    const d = await api('/api/session/select', { id });
    if (!d || !d.session) { toast('会话打不开'); return; }
    S.session = d.session; S.current = id;
    renderSessions(); renderChat(); syncModelSelect();
    applyBusy();
    // 这个会话可能正在别处生成 → 拉一次实时状态
    try {
      const lv = await api('/api/session/live?id=' + encodeURIComponent(id));
      if (lv && lv.ok && (lv.busy || lv.done)) {
        REMOTE[id] = { busy: !!lv.busy, text: lv.text, think: lv.think,
                       tools: lv.tools || [], queued: !!lv.queued };
        startPoll(id);
      }
    } catch (e) {}
    markDirty(); paintLive(); applyBusy();
  };

  window.newSession = newSession = async function () {
    const d = await api('/api/session/new', {
      model: (S.config && S.config.model) || undefined,
      base: (S.config && S.config.base) || undefined,
    });
    if (!d || !d.session) { toast('新建失败'); return; }
    S.session = d.session; S.current = d.session.id;
    await refreshSessions();
    S.session = S.sessions.find(s => s.id === S.current) || S.session;
    renderSessions(); renderChat(); syncModelSelect(); applyBusy();
    const inp = $('input'); if (inp) inp.focus();
  };

  window.stopGen = stopGen = function () {
    const sid = S.current;
    if (!sid) return;
    const r = S.runs[sid];
    if (!r || !r.streaming) {
      // 可能是别的标签页在跑，一样发停止
      toast('已请求停止');
    }
    api('/api/chat/stop', { id: sid }).catch(() => {});
  };

  /* ---------- 发送（核心） ---------- */
  window.send = send = async function () {
    const inp = $('input');
    if (!inp) return;
    let text = inp.value.trim();
    if (!text && !S.refs.length && !S.pendingImages.length) return;
    if (!S.session) { await newSession(); if (!S.session) return; }

    const sid = S.session.id;
    if (S.runs[sid] && S.runs[sid].streaming) {
      toast('这个会话正在生成，等它完成或先点停止');
      return;
    }

    const refs = S.refs.slice();
    const images = S.pendingImages.slice();
    const think = S.showThink, agent = S.agentMode;
    const provider = S.session.provider || 'ollama';
    const model = S.session.model;
    inp.value = ''; autoGrow(inp);
    S.refs = []; S.pendingImages = [];
    renderRefChips();

    const run = S.runs[sid] = {
      acc: '', think: '', toolAcc: [], streaming: true, queued: false,
      started: Date.now(), sid: sid,
    };
    // 本地先把用户消息挂进会话，切走再切回也能看到
    S.session.messages.push({
      role: 'user', content: text, refs: refs, n_images: images.length,
    });

    if (S.current === sid) {
      $('empty').style.display = 'none';
      const inner = $('chat-inner');
      if (inner) inner.appendChild(userNode({
        role: 'user', content: text, n_images: images.length, refs: refs,
      }));
      liveBox();
    }
    applyBusy(); markDirty(); scrollBottom(true);

    try {
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: sid, text: text, images: images, refs: refs,
          temperature: (S.config && S.config.temperature) || 0.7,
          show_think: think, provider: provider, agent_mode: agent,
        }),
      });
      if (!r.ok) {
        let msg = 'HTTP ' + r.status;
        try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
        throw new Error(msg);
      }
      if (!r.body) throw new Error('浏览器不支持流式响应');
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      const handle = (line) => {
        if (!line.startsWith('data: ')) return;
        let d; try { d = JSON.parse(line.slice(6)); } catch (e) { return; }
        if (d.type === 'delta') { run.acc += d.text || ''; run.queued = false; markDirty(); }
        else if (d.type === 'think') { run.think += d.text || ''; markDirty(); }
        else if (d.type === 'think_start' || d.type === 'think_end') { markDirty(); }
        else if (d.type === 'queued') {
          run.queued = true; markDirty();
          if (S.current === sid) toast(d.message || '已排队，稍等');
        } else if (d.type === 'dequeued') { run.queued = false; markDirty(); }
        else if (d.type === 'tool_start') {
          run.toolAcc.push({ name: d.name, args: d.args || {} });
          markDirty();
        } else if (d.type === 'tool_result') {
          if (typeof handleAgentFileAction === 'function') handleAgentFileAction(d.result);
          for (let i = run.toolAcc.length - 1; i >= 0; i--) {
            if (run.toolAcc[i].name === d.name && run.toolAcc[i].ok === undefined) {
              run.toolAcc[i].ok = d.ok; run.toolAcc[i].result = d.result; break;
            }
          }
          markDirty();
        } else if (d.type === 'stopped') { run.acc += '\n\n_（已手动停止）_'; markDirty(); }
        else if (d.type === 'error') { run.acc += '\n\n> ⚠️ ' + d.message; markDirty(); }
        else if (d.type === 'end' && d.session) {
          if (S.current === sid) S.session = d.session;
          else {
            const i = S.sessions.findIndex(x => x.id === sid);
            if (i >= 0 && S.sessions[i] && d.session.updated)
              S.sessions[i].updated = d.session.updated;
          }
        }
      };
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          chunk.split('\n').forEach(handle);
        }
      }
    } catch (e) {
      run.acc += '\n\n> ⚠️ 连接失败：' + e.message;
      markDirty();
    } finally {
      run.streaming = false; run.queued = false;
      markDirty();
      // 只有当前正看着这个会话，才把流式块换成正式消息
      setTimeout(async () => {
        if (S.current === sid) {
          const old = document.getElementById('v14-live');
          if (old) old.remove();
          await refreshSessions();
          S.session = S.sessions.find(s => s.id === S.current) || S.session;
          renderSessions(); renderChat();
        } else {
          refreshSessions();
        }
        delete S.runs[sid];
        applyBusy(); markDirty();
        const st = $('status-text'); if (st) st.textContent = '就绪';
      }, 200);
    }
  };

  /* ---------- 渲染补丁：切回来时把实时框补上 ---------- */
  const _renderChat = window.renderChat || renderChat;
  window.renderChat = renderChat = function () {
    _renderChat.apply(this, arguments);
    const old = document.getElementById('v14-live');
    if (old) old.remove();
    markDirty();
    setTimeout(paintLive, 0);
  };

  /* ---------- 快捷键：Ctrl/Cmd+N 新建，不打断生成 ---------- */
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
      e.preventDefault(); newSession();
    }
  });

  /* 按钮重绑：用间接引用，保证始终调到最新（含 v6ui 的斜杠命令包装） */
  setTimeout(() => {
    const bs = $('btn-send'), bt = $('btn-stop'), bn = $('btn-new');
    if (bs) bs.onclick = () => window.send();
    if (bt) bt.onclick = () => window.stopGen();
    if (bn) bn.onclick = () => window.newSession();
  }, 0);

  /* 启动时把可能残留的运行态补回来（刷新页面也能看到别处还在跑） */
  setTimeout(() => {
    (S.sessions || []).filter(s => s.busy).forEach(s => {
      REMOTE[s.id] = { busy: true, text: '', think: '', tools: [], queued: false };
      startPoll(s.id);
    });
    applyBusy(); markDirty();
  }, 1500);
})();
