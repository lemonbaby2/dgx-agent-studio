/* ============================================================
   DGX Embodied Studio · v8 前端
   三个工作台：具身仿真 / 论文朗读 / 笔记仓库
   ------------------------------------------------------------
   设计要点：
   · 不改 app.js 的既有逻辑，只「包装」全局 switchView 做视图切换
     （app.js 是普通 script，switchView 是全局函数，可安全覆盖）
   · 所有请求走同一套 api() 封装，统一错误提示，避免静默失败
   · 论文朗读按「块」按需合成 + 缓存，不在浏览器里存整篇音频
   · 断网/后端 500 时必须有可见反馈 —— 静默是最难查的 bug
   ============================================================ */
(function () {
  'use strict';

  const V8 = {
    view: '',
    caps: null,
    status: null,
    topics: [],
    plan: null,
    paper: null,          // {doc, chunks, sections}
    chunkState: {},       // i -> 'ok' | 'bad' | 'loading'
    playing: false,
    cur: 0,
    audio: null,
    notes: [],
    gitst: null,
    busy: false,
  };

  /* ---------------- v10：多人身份 ---------------- */
  // client_id 是"弱隔离"的钥匙：只为了各人只看自己的会话，内网同事之间没有对抗。
  // 存在 localStorage，换浏览器/清缓存会变成新身份（旧的会话就变成公共的）。
  function getClientId() {
    let v = '';
    try { v = localStorage.getItem('dgx_cid') || ''; } catch (e) {}
    if (!/^[A-Za-z0-9_\-]{4,40}$/.test(v)) {
      v = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      try { localStorage.setItem('dgx_cid', v); } catch (e) {}
    }
    return v;
  }

  function getClientName() {
    try { return localStorage.getItem('dgx_cname') || ''; } catch (e) { return ''; }
  }

  function setClientName(n) {
    try {
      localStorage.setItem('dgx_cname', (n || '').slice(0, 24));
    } catch (e) {}
    paintWhoami();
  }

  /* 给**所有** /api/ 请求自动加身份头。
     用包装 window.fetch 的方式，而不是改每个调用点 ——
     这样 app.js / panel.js / v7voice.js 的请求全都自动带上，不用一个个改。 */
  function installFetchHook() {
    if (window.__v10fetchHook) return;
    window.__v10fetchHook = true;
    const orig = window.fetch;
    if (typeof orig !== 'function') return;
    window.fetch = function (input, init) {
      try {
        const url = (typeof input === 'string') ? input : (input && input.url) || '';
        if (url.indexOf('/api/') >= 0) {
          init = init || {};
          let h = init.headers;
          if (h && typeof h.forEach === 'function' && !Array.isArray(h)) {
            const o = {};
            h.forEach((v, k) => { o[k] = v; });   // Headers 实例 -> 普通对象
            h = o;
          }
          h = Object.assign({}, h || {});
          h['X-Client-Id'] = getClientId();
          const nm = getClientName();
          // ⚠️ HTTP 头只能是 latin-1，中文必须编码，服务端 unquote 还原
          if (nm) h['X-Client-Name'] = encodeURIComponent(nm);
          init.headers = h;
        }
      } catch (e) { /* 头加不上也不能让请求失败 */ }
      return orig.call(this, input, init);
    };
  }

  function paintWhoami() {
    const box = $id('whoami');
    if (!box) return;
    const nm = getClientName();
    box.innerHTML = '<span class="dot on" style="flex:0 0 8px"></span>' +
      '<span style="overflow:hidden;text-overflow:ellipsis">' +
      '使用者 <b>' + esc(nm || '访客') + '</b>' +
      '<span style="color:var(--hint)"> · 改名</span></span>';
    box.title = 'client_id: ' + getClientId() + '（本浏览器专属，点此改名）';
  }

  function ensureWhoamiEl() {
    if ($id('whoami')) return;
    const foot = document.querySelector('.side-foot');
    if (!foot) return;
    const d = document.createElement('div');
    d.id = 'whoami';
    // white-space:nowrap 必须有：侧栏只有 264px，中文会一个字一行地竖排下去
    d.style.cssText = 'font-size:11px;color:var(--side-dim);padding:6px 2px;' +
      'cursor:pointer;display:flex;align-items:center;gap:4px;' +
      'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    d.onclick = () => {
      const cur = getClientName();
      const n = prompt('你的名字（同事之间用来区分会话；留空=访客）', cur || '');
      if (n === null) return;
      setClientName(n.trim());
      toast('已保存，刷新后生效');
      setTimeout(() => location.reload(), 400);
    };
    foot.insertBefore(d, foot.firstChild);
    paintWhoami();
  }

  /* POST 的语法糖：等价于 api(path, {body}) */
  function post(path, body) {
    return api(path, { body: body || {} });
  }

  /* ---------------- 基础工具 ---------------- */
  const $id = (x) => document.getElementById(x);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  let toastTimer = null;
  function toast(msg, ms) {
    let el = $id('v8-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'v8-toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), ms || 2600);
  }

  async function api(path, opt) {
    opt = opt || {};
    const init = { method: opt.body ? 'POST' : 'GET', headers: {} };
    if (opt.body) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opt.body);
    }
    let r, j;
    try {
      r = await fetch(path, init);
    } catch (e) {
      throw new Error('连不上后端：' + e.message);
    }
    try {
      j = await r.json();
    } catch (e) {
      throw new Error('返回不是 JSON（HTTP ' + r.status + '）');
    }
    if (!r.ok && j && j.error) throw new Error(j.error);
    if (!r.ok && !j.error) throw new Error('HTTP ' + r.status);
    return j;
  }

  const body = () => $id('v8-body');

  /* pip 版分级文案：0=未装 1=torch就位 2=API就位缺extscache 3=可启动 */
  function pipText(st) {
    const p = st.isaac_pip || {};
    const lv = p.level || 0;
    const ext = p.exts ? '（exts=' + p.exts + '）' : '';
    if (lv === 3) return '可启动' + ext + ' · ~/isaacsim-venv';
    if (lv === 2) return 'API 就位，缺 extscache 运行时' + ext + '（起不来 Kit，装包中）';
    if (lv === 1) return 'torch/kernel 就位，缺 app/gui 子包' + ext;
    return '未安装' + (p.reason ? '：' + shortReason(p.reason) : '');
  }

  function shortReason(r) {
    r = (r || '') + '';
    if (/No module named 'torch'/i.test(r)) return '缺 torch';
    if (/No module named 'isaacsim'/i.test(r)) return 'isaacsim 未装';
    const lines = r.split('\n').map(x => x.trim()).filter(Boolean);
    return (lines[lines.length - 1] || r).slice(0, 70);
  }

  /* ---------------- 视图切换（包装 app.js 的 switchView） ---------------- */
  const V8_VIEWS = { embodied: '具身仿真', paper: '论文朗读', notes: '笔记仓库',
                     learn: '自我进化', hub: '模型中心' };

  function showV8(v) {
    V8.view = v;
    const el = $id('v8-view');
    el.hidden = false;
    $id('v8-title').textContent = V8_VIEWS[v];
    document.querySelectorAll('.nav-item').forEach(n =>
      n.classList.toggle('active', n.dataset.view === v));
    render(v);
  }

  function hideV8() {
    const el = $id('v8-view');
    if (el) el.hidden = true;
    V8.view = '';
    pauseAudio();
  }

  function installSwitchHook() {
    if (typeof window.switchView !== 'function' || window.switchView.__v8) return;
    const orig = window.switchView;
    const wrapped = function (v) {
      if (V8_VIEWS[v]) { showV8(v); return; }
      hideV8();
      return orig.apply(this, arguments);
    };
    wrapped.__v8 = true;
    window.switchView = wrapped;
  }

  /* ---------------- 路由渲染 ---------------- */
  async function render(v) {
    if (v === 'embodied') return renderEmbodied();
    if (v === 'paper') return renderPaper();
    if (v === 'notes') return renderNotes();
    if (v === 'learn') return renderLearn();
    if (v === 'hub') return renderHub();
  }

  /* ============================================================
     一、具身仿真
     ============================================================ */
  async function renderEmbodied() {
    body().className = 'v8-body';
    body().innerHTML = '<div class="v8-card span2"><div class="v8-empty">读取 DGX 状态…</div></div>';
    let st;
    try {
      st = await api('/api/embodied/status');
    } catch (e) {
      body().innerHTML = '<div class="v8-card span2"><h3>状态读取失败</h3>' +
        '<div class="v8-empty">' + esc(e.message) + '</div></div>';
      return;
    }
    V8.status = st;
    const isaac = st.isaac || {};
    const gpu = st.gpu || {};
    const ros = st.ros2 || {};
    const dsp = st.display || {};
    const cont = isaac.container;

    const running = isaac.running;
    const imgs = st.isaac_images || [];

    body().innerHTML = `
    <div class="v8-card">
      <h3>运行环境 <span class="tag ${ros.ready ? 'ok' : 'err'}">
        ${ros.ready ? 'ROS2 就绪' : 'ROS2 未就绪'}</span></h3>
      <div class="v8-kv">
        <div class="row"><span class="k">ROS2</span><span class="v">
          <span class="dot ${ros.ready ? 'on' : 'off'}"></span>${esc(ros.distro || '')}
          ${esc(ros.version || '')}</span></div>
        <div class="row"><span class="k">Isaac Sim</span><span class="v">
          <span class="dot ${running ? 'on' : 'off'}"></span>
          ${running ? '运行中' : '未运行'}
          ${cont ? '· ' + esc(cont.status) : ''}</span></div>
        <div class="row"><span class="k">显示 :1</span><span class="v">
          <span class="dot ${dsp.ready ? 'on' : 'off'}"></span>
          ${dsp.ready ? '可截图' : '不可用'}</span></div>
        <div class="row"><span class="k">GPU</span><span class="v">
          ${esc(gpu.name || 'N/A')} ${esc(gpu.mem_used || '')}/${esc(gpu.mem_total || '')}
          · ${esc(gpu.util || '')}</span></div>
        <div class="row"><span class="k">pip 版</span><span class="v">
          <span class="dot ${(st.isaac_pip || {}).level >= 3 ? 'on' :
            ((st.isaac_pip || {}).level >= 1 ? 'err' : 'off')}"></span>
          ${esc(pipText(st))}</span></div>
        <div class="row"><span class="k">镜像</span><span class="v">
          ${imgs.length ? imgs.map(esc).join('<br>') : '无 isaac 镜像（pip 版可用则不需要）'}</span></div>
        <div class="row"><span class="k">数据卷</span><span class="v">
          ${esc(st.isaac_data_dir || '')} ${st.isaac_data_exists ? '✓' : '✗'}</span></div>
      </div>
      <div class="v8-row">
        ${running
          ? '<button class="v8-btn danger" data-act="isaac-stop">停止仿真</button>'
          : '<button class="v8-btn primary" data-act="isaac-start">启动仿真</button>'}
        <button class="v8-btn" data-act="isaac-pull">拉取镜像</button>
        <button class="v8-btn" data-act="shot">抓一帧屏幕</button>
        <button class="v8-btn" data-act="isaac-logs">看仿真日志</button>
      </div>
      <div id="v8-isaac-out"></div>
    </div>

    <div class="v8-card">
      <h3>ROS2 话题
        <span class="tag">${(V8.topics || []).length || '?'}</span>
        <span class="spacer"></span>
        <button class="v8-btn" data-act="ros-topics">刷新</button>
      </h3>
      <div id="v8-topics" class="v8-topics">
        <div class="v8-empty">点「刷新」拉取话题列表</div>
      </div>
    </div>

    <div class="v8-card span2">
      <h3>VLA 任务 <span class="tag warn">自然语言 → 仿真动作</span>
        <span class="spacer"></span>
        规划模型
        <select class="v8-sel" id="v8-vla-model">
          <option value="deepseek-r1:70b">deepseek-r1:70b（深推理，约 3-5 分钟）</option>
          <option value="qwen3-coder:30b">qwen3-coder:30b（快，约 15s）</option>
          <option value="qwen3-vl:30b-a3b-instruct">qwen3-vl:30b（视觉/OCR/检测）</option>
        </select>
      </h3>
      <div class="v8-field">
        <label>告诉这个具身智能体你要做什么（会先规划再执行，最多 6 步）</label>
        <input class="v8-input" id="v8-task"
          placeholder="例如：看一下仿真里现在有哪些话题，然后抓一帧画面确认机械臂位置">
      </div>
      <div class="v8-row">
        <button class="v8-btn primary" data-act="vla-plan">规划</button>
        <button class="v8-btn" data-act="vla-run" id="v8-run-btn" disabled>执行计划</button>
      </div>
      <div id="v8-steps" class="v8-steps"></div>
    </div>

    <div class="v8-card span2" id="v8-shot-card" hidden>
      <h3>画面 <span class="spacer"></span>
        <button class="v8-btn" data-act="shot-close">关闭</button></h3>
      <div id="v8-shot"></div>
    </div>`;

    bindEmbodied();
  }

  function bindEmbodied() {
    const b = body();
    b.onclick = async (ev) => {
      const btn = ev.target.closest('[data-act]');
      const tp = ev.target.closest('.v8-topics .t');
      if (tp && tp.dataset.topic) {
        echoTopic(tp.dataset.topic);
        return;
      }
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === 'isaac-start') return doIsaac('start', btn);
      if (act === 'isaac-stop') return doIsaac('stop', btn);
      if (act === 'isaac-pull') return doIsaac('pull', btn);
      if (act === 'isaac-logs') return doIsaac('logs', btn);
      if (act === 'ros-topics') return loadTopics(btn);
      if (act === 'shot') return doShot(btn);
      if (act === 'shot-close') { $id('v8-shot-card').hidden = true; return; }
      if (act === 'vla-plan') return doPlan(btn);
      if (act === 'vla-run') return doRunPlan(btn);
    };
  }

  async function doIsaac(kind, btn) {
    const out = $id('v8-isaac-out');
    btn.disabled = true;
    try {
      if (kind === 'logs') {
        const r = await api('/api/embodied/isaac/logs?n=60');
        out.innerHTML = '<div class="v8-log">' + esc(r.output || '(无日志)') + '</div>';
        return;
      }
      const map = { start: '/api/embodied/isaac/start', stop: '/api/embodied/isaac/stop', pull: '/api/embodied/isaac/pull' };
      const r = await api(map[kind], kind === 'stop' ? { body: {} } : { body: {} });
      if (r.job_id) {
        toast('已在后台启动，正在轮询进度…');
        out.innerHTML = '<div class="v8-log" id="v8-job">' + esc(JSON.stringify(r, null, 1)) + '</div>';
        pollJob(r.job_id);
      } else if (r.ok === false) {
        toast(r.error || '操作失败');
        out.innerHTML = '<div class="v8-log bad">' + esc(r.error || '') +
          (r.hint ? '\n可用镜像：' + esc((r.hint || []).join(', ')) : '') + '</div>';
      } else {
        toast('已停止');
        setTimeout(renderEmbodied, 600);
      }
    } catch (e) {
      toast(e.message);
      out.innerHTML = '<div class="v8-log bad">' + esc(e.message) + '</div>';
    } finally {
      btn.disabled = false;
    }
  }

  async function pollJob(jid) {
    const box = () => $id('v8-job');
    for (let n = 0; n < 120; n++) {
      await new Promise(r => setTimeout(r, 2500));
      let j;
      try { j = await api('/api/embodied/job?id=' + encodeURIComponent(jid)); }
      catch (e) { return; }
      const jb = j.job || {};
      const el = box();
      if (el) {
        el.textContent = '[' + (jb.status || '?') + ']\n' + (jb.tail || []).join('\n');
        el.className = 'v8-log' + (jb.status === 'done' ? ' ok' : (jb.status === 'failed' ? ' bad' : ''));
      }
      if (jb.status === 'done' || jb.status === 'failed') {
        toast('任务' + (jb.status === 'done' ? '完成' : '失败'));
        if (V8.view === 'embodied') setTimeout(loadTopics, 800);
        return;
      }
    }
  }

  async function loadTopics(btn) {
    if (btn) btn.disabled = true;
    try {
      const r = await api('/api/embodied/ros/topics');
      V8.topics = r.topics || [];
      const box = $id('v8-topics');
      if (!V8.topics.length) {
        box.innerHTML = '<div class="v8-empty">没有检测到话题。仿真可能没启动，' +
          '或 ROS2 域不一致（ROS_DOMAIN_ID）。</div>';
      } else {
        box.innerHTML = V8.topics.map(t =>
          `<div class="t" data-topic="${esc(t)}"><span class="nm">${esc(t)}</span>
           <span class="ty">点我读一条</span></div>`).join('');
      }
      toast('共 ' + V8.topics.length + ' 个话题');
    } catch (e) {
      toast(e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function echoTopic(topic) {
    toast('正在读 ' + topic + ' …');
    try {
      const r = await api('/api/embodied/ros/echo?topic=' + encodeURIComponent(topic) + '&n=1');
      const card = $id('v8-shot-card');
      card.hidden = false;
      $id('v8-shot').innerHTML = '<div class="v8-card" style="box-shadow:none;margin:0">' +
        '<h3>' + esc(topic) + '</h3><div class="v8-log">' +
        esc(r.output || '(无数据)') + '</div></div>';
    } catch (e) {
      toast(e.message);
    }
  }

  async function doShot(btn) {
    btn.disabled = true;
    try {
      const r = await api('/api/embodied/screenshot');
      if (!r.ok) throw new Error(r.error || '截图失败');
      const card = $id('v8-shot-card');
      card.hidden = false;
      $id('v8-shot').innerHTML =
        '<img src="' + r.image + '" style="width:100%;border-radius:9px;border:1px solid var(--border)">';
      toast('已抓屏 ' + Math.round((r.bytes || 0) / 1365) + ' KB');
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function doPlan(btn) {
    const task = ($id('v8-task').value || '').trim();
    if (!task) return toast('先写一句你想让仿真做什么');
    const model = (($id('v8-vla-model') || {}).value) || 'deepseek-r1:70b';
    btn.disabled = true;
    const slow = model.indexOf('r1') >= 0;
    $id('v8-steps').innerHTML = '<div class="v8-empty">规划中（' + esc(model) +
      (slow ? '，R1-70B 深推理约 3-5 分钟' : '，约 15 秒') + '，DGX 忙时会更久）…</div>';
    try {
      const r = await api('/api/embodied/vla/plan', { body: { text: task, model: model } });
      if (!r.ok) throw new Error(r.error || '规划失败');
      V8.plan = r.steps || [];
      $id('v8-steps').innerHTML = V8.plan.length
        ? V8.plan.map((s, i) => `
          <div class="v8-step">
            <div class="h"><span class="idx">${i + 1}</span>
              <span class="act">${esc(s.action)}</span>
              <span class="why">${esc(s.why || '')}</span></div>
            ${s.topic ? '<div class="out">topic: ' + esc(s.topic) +
              (s.type ? '  type: ' + esc(s.type) : '') + '</div>' : ''}
          </div>`).join('')
        : '<div class="v8-empty">模型没给出可执行步骤，换个说法试试</div>';
      $id('v8-run-btn').disabled = !V8.plan.length;
      toast('已生成 ' + V8.plan.length + ' 步计划');
    } catch (e) {
      $id('v8-steps').innerHTML = '<div class="v8-empty">' + esc(e.message) + '</div>';
      toast(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function doRunPlan(btn) {
    if (!V8.plan || !V8.plan.length) return toast('先规划');
    btn.disabled = true;
    $id('v8-steps').innerHTML = '<div class="v8-empty">执行中…</div>';
    try {
      const r = await api('/api/embodied/vla/run', { body: { steps: V8.plan } });
      const res = r.results || [];
      $id('v8-steps').innerHTML = res.map(x => {
        const ok = (x.result || {}).ok;
        let out = '';
        const rr = x.result || {};
        if (rr.topics) out = rr.topics.join('\n');
        else if (rr.output) out = rr.output;
        else if (rr.image) out = '[截图 ' + Math.round((rr.bytes || 0) / 1365) + ' KB]';
        else out = JSON.stringify(rr).slice(0, 800);
        return `<div class="v8-step ${ok ? 'ok' : 'bad'}">
          <div class="h"><span class="idx">${x.step}</span>
            <span class="act">${esc(x.action)}</span>
            <span class="why">${esc(x.why || '')} · ${x.elapsed}s
              ${ok ? '' : '· 失败'}</span></div>
          ${out ? '<div class="out">' + esc(out.slice(0, 1200)) + '</div>' : ''}
          ${rr.image ? '<img src="' + rr.image +
            '" style="width:100%;border-radius:7px;margin-top:7px;border:1px solid var(--border)">' : ''}
        </div>`;
      }).join('') || '<div class="v8-empty">没有执行结果</div>';
      toast('执行完成：' + res.length + ' 步');
      // 把最后一张截图也放到画面卡
      const withImg = res.filter(x => (x.result || {}).image).pop();
      if (withImg) {
        $id('v8-shot-card').hidden = false;
        $id('v8-shot').innerHTML = '<img src="' + withImg.result.image +
          '" style="width:100%;border-radius:9px;border:1px solid var(--border)">';
      }
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  /* ============================================================
     二、论文朗读
     ============================================================ */
  function renderPaper() {
    body().className = 'v8-body';
    body().innerHTML = `
    <div class="v8-card span2">
      <h3>载入论文 <span class="tag">PDF / 文本</span></h3>
      <div class="v8-row" style="margin-top:0">
        <button class="v8-btn primary" data-act="pick-pdf">选择 PDF</button>
        <button class="v8-btn" data-act="narrate">生成口播稿（总结 idea）</button>
        <button class="v8-btn" data-act="demo-text">用示例文本试跑</button>
      </div>
      <div class="v8-field" style="margin-top:12px">
        <label>或者直接粘贴文本</label>
        <textarea class="v8-textarea" id="v8-paper-text"
          placeholder="把论文正文、摘要或任意长文本粘进来…"></textarea>
      </div>
      <div class="v8-row">
        <button class="v8-btn" data-act="from-text">用这段文本生成朗读</button>
      </div>
      <div id="v8-paper-info" style="margin-top:10px"></div>
    </div>

    <div class="v8-card span2" id="v8-player-card" hidden>
      <div class="v8-player">
        <div class="now">
          <span class="sec" id="v8-cur-sec">—</span>
          <span id="v8-cur-title">未开始</span>
        </div>
        <div class="txt" id="v8-cur-text"></div>
        <div class="bar">
          <button class="v8-play" data-act="toggle" id="v8-play-btn" title="播放/暂停">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
          </button>
          <button class="v8-btn" data-act="prev">上一段</button>
          <button class="v8-btn" data-act="next">下一段</button>
          <div class="prog"><i id="v8-prog"></i></div>
          <span class="cnt" id="v8-cnt">0 / 0</span>
          <select class="v8-sel" id="v8-rate">
            <option value="-20">0.8×</option>
            <option value="0" selected>1.0×</option>
            <option value="15">1.15×</option>
            <option value="30">1.3×</option>
          </select>
        </div>
        <div class="v8-sec-list" id="v8-sec-list"></div>
      </div>
    </div>

    <div class="v8-card span2" id="v8-chunks-card" hidden>
      <h3>朗读块 <span class="tag" id="v8-chunk-tag">0</span>
        <span class="spacer"></span>
        <button class="v8-btn" data-act="prefetch">预合成前 8 段</button>
      </h3>
      <div class="v8-chunks" id="v8-chunks"></div>
    </div>

    <input type="file" id="v8-pdf-file" accept="application/pdf" hidden>`;

    bindPaper();
    if (V8.paper) paintPaper();
  }

  function bindPaper() {
    body().onclick = async (ev) => {
      const btn = ev.target.closest('[data-act]');
      const ck = ev.target.closest('.v8-chunk');
      const sc = ev.target.closest('.v8-sec');
      if (sc && sc.dataset.si != null) { jumpSection(+sc.dataset.si); return; }
      if (ck && ck.dataset.i != null) { play(+ck.dataset.i); return; }
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === 'pick-pdf') return $id('v8-pdf-file').click();
      if (act === 'from-text') return fromText();
      if (act === 'demo-text') return demotext();
      if (act === 'narrate') return narrate();
      if (act === 'toggle') return togglePlay();
      if (act === 'prev') return play(Math.max(0, V8.cur - 1));
      if (act === 'next') return play(Math.min((V8.paper.chunks.length - 1), V8.cur + 1));
      if (act === 'prefetch') return prefetch(btn);
    };
    const f = $id('v8-pdf-file');
    if (f) f.onchange = () => pickPdf(f.files[0]);
  }

  function readFileB64(file) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result);
      r.onerror = () => rej(new Error('读取文件失败'));
      r.readAsDataURL(file);
    });
  }

  async function pickPdf(file) {
    if (!file) return;
    if (file.size > 40 * 1024 * 1024) return toast('PDF 太大（>40MB），先拆一下');
    toast('解析 PDF 中…');
    try {
      const b64 = await readFileB64(file);
      V8._pdfB64 = b64;                     // 口播稿要复用同一份 PDF
      const r = await api('/api/paper/prepare', {
        body: { pdf: b64, title: file.name, max_chunks: 400 }
      });
      if (!r.ok) throw new Error(r.error || '解析失败');
      V8.paper = r; V8.chunkState = {}; V8.cur = 0;
      paintPaper();
      toast('已切分 ' + r.total_chunks + ' 段 / ' + (r.sections || []).length + ' 节');
    } catch (e) { toast(e.message); }
  }

  async function fromText() {
    const t = ($id('v8-paper-text').value || '').trim();
    if (!t) return toast('先粘一段文本');
    await prepareText(t, '粘贴文本');
  }

  async function demotext() {
    const t = '本文提出一种面向工业产线的视觉-语言-动作一体化智能体。' +
      '它把摄像头画面、工序文本描述与机械臂动作序列映射到同一个表示空间，' +
      '从而在少样本条件下完成任务泛化。实验在三个工位上验证，' +
      '平均成功率达到百分之八十二，比基线提升十一点三个百分点。' +
      '局限在于对光照变化仍然敏感，强反光场景下成功率下降约两成。';
    await prepareText(t, '示例文本');
  }

  async function prepareText(text, title) {
    toast('切分中…');
    try {
      const r = await api('/api/paper/prepare', {
        body: { text: text, title: title, max_chunks: 400 }
      });
      if (!r.ok) throw new Error(r.error || '切分失败');
      V8.paper = r; V8.chunkState = {}; V8.cur = 0;
      paintPaper();
      toast('已切成 ' + r.total_chunks + ' 段');
    } catch (e) { toast(e.message); }
  }

  async function narrate() {
    const b64 = V8._pdfB64;
    if (!b64) return toast('先选一个 PDF');
    toast('生成口播稿中（R1 推理，约 30-90 秒）…');
    try {
      const r = await api('/api/paper/narrate', { body: { pdf: b64 } });
      if (!r.ok) throw new Error(r.error || '生成失败');
      V8.paper = r; V8.chunkState = {}; V8.cur = 0;
      paintPaper();
      toast('口播稿就绪：' + r.total_chunks + ' 段，点播放即可朗读');
      play(0);
    } catch (e) { toast(e.message); }
  }

  function paintPaper() {
    const p = V8.paper;
    if (!p) return;
    $id('v8-paper-info').innerHTML =
      `<div class="v8-kv">
        <div class="row"><span class="k">文档</span><span class="v">${esc(p.pdf || p.title || '—')}</span></div>
        <div class="row"><span class="k">页数 / 字数</span><span class="v">${p.pages || '-'} 页 · ${p.chars || 0} 字</span></div>
        <div class="row"><span class="k">朗读块</span><span class="v">${p.total_chunks} 段
          ${p.truncated ? '（已截断）' : ''}</span></div>
        <div class="row"><span class="k">TTS</span><span class="v">
          <span class="dot ${p.tts_ready ? 'on' : 'off'}"></span>
          ${p.tts_ready ? 'edge-tts 可用' : '不可用，将退回浏览器朗读'}</span></div>
      </div>`;
    $id('v8-player-card').hidden = false;
    $id('v8-chunks-card').hidden = false;
    $id('v8-chunk-tag').textContent = p.total_chunks;

    // 分节快捷跳转
    const secs = p.sections || [];
    $id('v8-sec-list').innerHTML = secs.slice(0, 12).map((s, i) =>
      `<span class="v8-sec" data-si="${i}">${esc(s.title)}</span>`).join('');

    $id('v8-chunks').innerHTML = (p.chunks || []).map(c => `
      <div class="v8-chunk" data-i="${c.i}">
        <span class="no">${String(c.i + 1).padStart(2, '0')}</span>
        <span class="tx">${esc(c.text)}</span>
        <span class="st" data-st="${c.i}"></span>
      </div>`).join('');
    markCur();
  }

  function markCur() {
    const p = V8.paper;
    if (!p) return;
    document.querySelectorAll('.v8-chunk').forEach(el => {
      el.classList.toggle('active', +el.dataset.i === V8.cur);
    });
    const c = p.chunks[V8.cur] || {};
    const sec = $id('v8-cur-sec');
    if (sec) sec.textContent = c.section || '—';
    const ti = $id('v8-cur-title');
    if (ti) ti.textContent = '第 ' + (V8.cur + 1) + ' / ' + p.chunks.length + ' 段';
    const tx = $id('v8-cur-text');
    if (tx) tx.textContent = c.text || '';
    const cnt = $id('v8-cnt');
    if (cnt) cnt.textContent = (V8.cur + 1) + ' / ' + p.chunks.length;
    const pr = $id('v8-prog');
    if (pr) pr.style.width = (p.chunks.length ? (V8.cur + 1) / p.chunks.length * 100 : 0) + '%';
    const active = document.querySelector('.v8-chunk.active');
    if (active) active.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function jumpSection(si) {
    const p = V8.paper;
    if (!p) return;
    const idx = (p.chunks || []).findIndex(c => c.section_idx === si);
    if (idx >= 0) play(idx);
    document.querySelectorAll('.v8-sec').forEach(el =>
      el.classList.toggle('active', +el.dataset.si === si));
  }

  async function ensureAudio(i) {
    const p = V8.paper;
    const c = p.chunks[i];
    if (!c) return null;
    const key = i;
    if (V8.chunkState[key] === 'ok') {
      return '/api/tts/audio?doc=' + encodeURIComponent(p.doc) + '&i=' + i;
    }
    if (V8.chunkState[key] === 'bad') return null;
    V8.chunkState[key] = 'loading';
    const st = document.querySelector('[data-st="' + i + '"]');
    if (st) st.textContent = '⋯';
    try {
      const r = await api('/api/tts/synth', {
        body: {
          doc: p.doc, i: i, text: c.text, voice: c.voice,
          rate: parseInt(($id('v8-rate') || {}).value || 0, 10)
        }
      });
      if (r.ok) {
        V8.chunkState[key] = 'ok';
        if (st) st.textContent = '♪';
        return r.url + '&t=' + Date.now();
      }
      V8.chunkState[key] = 'bad';
      if (st) st.textContent = '×';
      return null;
    } catch (e) {
      V8.chunkState[key] = 'bad';
      if (st) st.textContent = '×';
      return null;
    }
  }

  function pauseAudio() {
    if (V8.audio) { try { V8.audio.pause(); } catch (e) {} }
    V8.playing = false;
    setPlayIcon(false);
  }

  function setPlayIcon(playing) {
    const b = $id('v8-play-btn');
    if (!b) return;
    b.innerHTML = playing
      ? '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>'
      : '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  }

  function browserSpeak(text) {
    if (!('speechSynthesis' in window)) { toast('浏览器不支持朗读'); return; }
    const u = new SpeechSynthesisUtterance(text);
    u.lang = /[\u4e00-\u9fff]/.test(text) ? 'zh-CN' : 'en-US';
    u.rate = 1.0 + (parseInt(($id('v8-rate') || {}).value || 0, 10) / 100);
    u.onend = () => {
      if (V8.playing && V8.cur < V8.paper.chunks.length - 1) play(V8.cur + 1);
      else { V8.playing = false; setPlayIcon(false); }
    };
    window.speechSynthesis.speak(u);
  }

  async function play(i) {
    const p = V8.paper;
    if (!p || !p.chunks || !p.chunks.length) return;
    pauseAudio();
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    V8.cur = Math.max(0, Math.min(i, p.chunks.length - 1));
    markCur();
    V8.playing = true;
    setPlayIcon(true);

    const url = await ensureAudio(V8.cur);
    const text = p.chunks[V8.cur].text || '';
    if (!url) {
      browserSpeak(text);       // TTS 失败 → 浏览器兜底
      return;
    }
    if (!V8.audio) {
      V8.audio = new Audio();
      V8.audio.onended = () => {
        if (V8.playing && V8.cur < p.chunks.length - 1) play(V8.cur + 1);
        else { V8.playing = false; setPlayIcon(false); }
      };
      V8.audio.onerror = () => browserSpeak(text);
    }
    V8.audio.src = url;
    try { await V8.audio.play(); } catch (e) { browserSpeak(text); }
  }

  function togglePlay() {
    if (!V8.paper) return toast('先载入文本');
    if (V8.playing) { pauseAudio(); return; }
    play(V8.cur);
  }

  async function prefetch(btn) {
    const p = V8.paper;
    if (!p) return;
    btn.disabled = true;
    const items = p.chunks.slice(0, 8).map(c => ({ i: c.i, text: c.text, voice: c.voice }));
    toast('批量合成 8 段…');
    try {
      const r = await api('/api/tts/many', { body: { doc: p.doc, items: items } });
      (r.items || []).forEach(x => {
        if (x.ok) {
          V8.chunkState[x.i] = 'ok';
          const st = document.querySelector('[data-st="' + x.i + '"]');
          if (st) st.textContent = '♪';
        }
      });
      toast('合成完成 ' + (r.done || 0) + '/' + (r.total || 0));
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; }
  }

  /* ============================================================
     三、笔记仓库
     ============================================================ */
  async function renderNotes() {
    body().className = 'v8-body';
    body().innerHTML = '<div class="v8-card span2"><div class="v8-empty">读取仓库状态…</div></div>';
    let st, nt;
    try {
      st = await api('/api/git/status');
      nt = await api('/api/git/notes');
    } catch (e) {
      body().innerHTML = '<div class="v8-card span2"><h3>读取失败</h3><div class="v8-empty">' +
        esc(e.message) + '</div></div>';
      return;
    }
    V8.gitst = st;
    V8.notes = nt.notes || [];

    body().innerHTML = `
    <div class="v8-card">
      <h3>仓库 <span class="tag ${st.is_repo ? 'ok' : 'warn'}">
        ${st.is_repo ? '已初始化' : '未初始化'}</span></h3>
      <div class="v8-kv">
        <div class="row"><span class="k">笔记目录</span><span class="v">${esc(st.notes_dir)}</span></div>
        <div class="row"><span class="k">远端</span><span class="v">${esc(st.remote || '未配置')}</span></div>
        <div class="row"><span class="k">分支</span><span class="v">${esc(st.branch || '-')}</span></div>
        <div class="row"><span class="k">待提交</span><span class="v">${st.dirty} 个文件</span></div>
        <div class="row"><span class="k">Token</span><span class="v">
          <span class="dot ${st.has_token ? 'on' : 'off'}"></span>
          ${st.has_token ? '已配置' : '未配置（push 会失败）'}</span></div>
        <div class="row"><span class="k">最近提交</span><span class="v">${esc(st.last_commit || '—')}</span></div>
      </div>
      <div class="v8-row">
        <button class="v8-btn" data-act="init">绑定仓库</button>
        <button class="v8-btn" data-act="token">设置 Token</button>
        <button class="v8-btn primary" data-act="push">推送</button>
        <button class="v8-btn" data-act="pull">拉取</button>
      </div>
      <div id="v8-git-out" style="margin-top:10px"></div>
    </div>

    <div class="v8-card">
      <h3>笔记 <span class="tag">${V8.notes.length}</span>
        <span class="spacer"></span>
        <button class="v8-btn" data-act="reload-notes">刷新</button></h3>
      <div class="v8-notes" id="v8-note-list">
        ${V8.notes.length ? V8.notes.map(n => `
          <div class="v8-note" data-name="${esc(n.name)}">
            <span class="nm">${esc(n.name)}</span>
            <span class="mt">${esc(n.mtime)}</span>
          </div>`).join('') : '<div class="v8-empty">还没有笔记</div>'}
      </div>
    </div>

    <div class="v8-card span2">
      <h3>写笔记 <span class="tag">Markdown · 提交后可直接推送</span></h3>
      <div class="v8-field">
        <label>标题</label>
        <input class="v8-input" id="v8-note-title" placeholder="例如：VLA 论文阅读笔记 - Diffusion Policy">
      </div>
      <div class="v8-field">
        <label>正文（支持 Markdown）</label>
        <textarea class="v8-textarea" id="v8-note-body" rows="10"
          placeholder="把对话结论、论文要点、实验记录写在这里…"></textarea>
      </div>
      <div class="v8-row">
        <button class="v8-btn primary" data-act="save-note">保存并提交</button>
        <button class="v8-btn" data-act="save-push">保存并推送</button>
        <button class="v8-btn" data-act="from-chat">把当前对话存成笔记</button>
      </div>
    </div>`;

    bindNotes();
  }

  function bindNotes() {
    body().onclick = async (ev) => {
      const nb = ev.target.closest('.v8-note');
      if (nb) { loadNote(nb.dataset.name); return; }
      const btn = ev.target.closest('[data-act]');
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === 'init') return doInit(btn);
      if (act === 'token') return doToken(btn);
      if (act === 'push') return doPush(btn);
      if (act === 'pull') return doPull(btn);
      if (act === 'reload-notes') return renderNotes();
      if (act === 'save-note') return saveNote(btn, false);
      if (act === 'save-push') return saveNote(btn, true);
      if (act === 'from-chat') return fromChat();
    };
  }

  async function loadNote(name) {
    try {
      const r = await api('/api/git/note?name=' + encodeURIComponent(name));
      if (!r.ok) throw new Error(r.error || '读取失败');
      $id('v8-note-title').value = name.replace(/^\d{4}-\d{2}-\d{2}_/, '').replace(/\.md$/, '');
      $id('v8-note-body').value = r.content || '';
      document.querySelectorAll('.v8-note').forEach(el =>
        el.classList.toggle('active', el.dataset.name === name));
      toast('已载入 ' + name);
    } catch (e) { toast(e.message); }
  }

  function gitOut(msg, cls) {
    const o = $id('v8-git-out');
    if (o) o.innerHTML = '<div class="v8-log ' + (cls || '') + '">' + esc(msg) + '</div>';
  }

  async function doInit(btn) {
    const url = prompt('GitHub 仓库地址（https://github.com/用户名/仓库.git 或 git@github.com:用户名/仓库.git）：');
    if (!url) return;
    btn.disabled = true;
    try {
      const r = await api('/api/git/init', { body: { repo_url: url.trim(), branch: 'main' } });
      gitOut((r.msgs || []).join('\n'));
      toast('仓库已绑定');
      setTimeout(renderNotes, 700);
    } catch (e) { gitOut(e.message, 'bad'); toast(e.message); }
    finally { btn.disabled = false; }
  }

  async function doToken(btn) {
    const t = prompt('GitHub Personal Access Token（需要 repo 权限；只存在 DGX 本地，权限 600）：');
    if (t == null) return;
    btn.disabled = true;
    try {
      const r = await api('/api/git/token', { body: { token: t.trim() } });
      toast(r.has_token ? 'Token 已保存' : 'Token 已清除');
      setTimeout(renderNotes, 500);
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; }
  }

  async function doPush(btn) {
    btn.disabled = true;
    gitOut('推送中…');
    try {
      const title = ($id('v8-note-title') || {}).value || '';
      const r = await api('/api/git/push', {
        body: { message: title ? ('note: ' + title) : 'notes: 批量更新' }
      });
      gitOut((r.output || '') + (r.hint ? '\n\n提示：' + r.hint : ''),
        r.ok ? 'ok' : 'bad');
      toast(r.ok ? '推送成功' : '推送失败');
      if (r.ok) setTimeout(renderNotes, 900);
    } catch (e) { gitOut(e.message, 'bad'); toast(e.message); }
    finally { btn.disabled = false; }
  }

  async function doPull(btn) {
    btn.disabled = true;
    gitOut('拉取中…');
    try {
      const r = await api('/api/git/pull', { body: {} });
      gitOut(r.output || '', r.ok ? 'ok' : 'bad');
    } catch (e) { gitOut(e.message, 'bad'); }
    finally { btn.disabled = false; }
  }

  async function saveNote(btn, andPush) {
    const title = ($id('v8-note-title').value || '').trim();
    const content = ($id('v8-note-body').value || '').trim();
    if (!title) return toast('写个标题');
    if (!content) return toast('正文还是空的');
    btn.disabled = true;
    try {
      const r = await api('/api/git/save', {
        body: { title: title, content: content, tags: ['dgx', 'agent'] }
      });
      if (!r.ok) throw new Error(r.error || '保存失败');
      toast('已保存 ' + r.name);
      if (andPush) {
        const p = await api('/api/git/push', { body: { message: 'note: ' + title } });
        gitOut((p.output || '') + (p.hint ? '\n提示：' + p.hint : ''), p.ok ? 'ok' : 'bad');
        toast(p.ok ? '已推送' : '推送失败：' + (p.hint || ''));
      }
      setTimeout(renderNotes, 600);
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; }
  }

  function currentChatText() {
    // 从 app.js 的 S.sessions 里取当前会话消息（存在则用，不存在则退回 DOM）
    let text = '';
    try {
      const S = window.S;
      if (S && S.sessions && S.cur) {
        const s = S.sessions.find(x => x.id === S.cur) || S.sessions[0];
        if (s && s.messages) {
          text = s.messages.map(m =>
            (m.role === 'user' ? '**问：** ' : '**答：** ') + (m.content || '')).join('\n\n');
        }
      }
    } catch (e) { /* ignore */ }
    if (!text) {
      const nodes = document.querySelectorAll('#chat .msg');
      text = Array.from(nodes).map(n => n.innerText).join('\n\n');
    }
    return text;
  }

  function fromChat() {
    const t = currentChatText();
    if (!t || t.length < 10) return toast('当前对话是空的');
    $id('v8-note-title').value = '对话记录 ' + new Date().toLocaleString('zh-CN');
    $id('v8-note-body').value = t.slice(0, 60000);
    toast('已填入当前对话（可编辑后再保存）');
  }

  /* ============================================================
     四、自我进化（自学习）
     ============================================================ */
  let LEARN_TIMER = null;

  function fmtTs(sec) {
    if (!sec) return '—';
    const d = new Date(sec * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
           `${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function fmtAgo(sec) {
    if (!sec) return '从未';
    const dt = Math.floor(Date.now() / 1000 - sec);
    if (dt < 60) return dt + ' 秒前';
    if (dt < 3600) return Math.floor(dt / 60) + ' 分钟前';
    if (dt < 86400) return Math.floor(dt / 3600) + ' 小时前';
    return Math.floor(dt / 86400) + ' 天前';
  }

  async function renderLearn() {
    body().className = 'v8-body';
    body().innerHTML = '<div class="v8-card span2"><div class="v8-empty">读取学习状态…</div></div>';
    let st, hist, kb, dep;
    try {
      st = await api('/api/learn/status');
      hist = await api('/api/learn/history?limit=14');
      kb = await api('/api/learn/kb?limit=20');
    } catch (e) {
      body().innerHTML = '<div class="v8-card span2"><h3>读取失败</h3>' +
        '<div class="v8-empty">' + esc(e.message) + '</div></div>';
      return;
    }
    // 部署状态是附加信息，拿不到不能让整页失败
    try { dep = await api('/api/deploy/status'); } catch (e) { dep = {}; }
    V8.learn = st;
    V8.deploy = dep;
    const last = st.last || {};
    const job = st.job || {};
    const running = !!st.running;
    const stats = last.status || 'never';
    const rt = st.realtime || {};
    const rtCfg = rt.config || {};
    const dl = dep.last || {};

    const statusTag = {
      ok: ['ok', '成功'],
      nothing_new: ['', '无新内容'],
      llm_failed: ['err', '模型调用失败'],
      bad_json: ['warn', '输出非 JSON'],
      crash: ['err', '崩溃'],
      dry_no_llm: ['', '干跑'],
    }[stats] || ['', stats];

    body().innerHTML = `
    <div class="v8-card">
      <h3>自学习状态
        <span class="tag ${statusTag[0]}">${running ? '运行中…' : statusTag[1]}</span></h3>
      <div class="v8-kv">
        <div class="row"><span class="k">上次批量学习</span><span class="v">
          ${esc(last.date || '—')} · ${fmtAgo(last.ts)}
          ${last.elapsed ? '（' + last.elapsed + 's）' : ''}</span></div>
        <div class="row"><span class="k">已消化对话</span><span class="v">
          ${st.learned_messages} 轮（去重指纹）</span></div>
        <div class="row"><span class="k">知识库</span><span class="v">
          ${st.kb_files} 个文件 · ${(st.kb_bytes / 1024).toFixed(1)} KB</span></div>
        <div class="row"><span class="k">技能</span><span class="v">
          内置 ${st.skills_builtin} + <b style="color:var(--accent)">自动 ${st.skills_auto}</b></span></div>
        <div class="row"><span class="k">下次批量</span><span class="v">
          ${fmtTs(st.next_run)}（每天 00:00）</span></div>
      </div>
      <div class="v8-row">
        <button class="v8-btn primary" data-act="learn-run" ${running ? 'disabled' : ''}>
          ${running ? '学习中…' : '立即全量学习'}</button>
        <button class="v8-btn" data-act="learn-dry" ${running ? 'disabled' : ''}>干跑</button>
        <button class="v8-btn" data-act="learn-refresh">刷新</button>
      </div>
    </div>

    <div class="v8-card">
      <h3>实时学习 <span class="tag ${rt.running ? 'ok' : 'warn'}">
          ${rt.running ? '线程运行中' : '未运行'}</span></h3>
      <div class="v8-kv">
        <div class="row"><span class="k">待消化</span><span class="v">
          <b style="color:${(rt.pending || 0) > 0 ? 'var(--warn)' : 'var(--ok)'}">${rt.pending || 0}</b> 条</span></div>
        <div class="row"><span class="k">上次消化</span><span class="v">
          ${fmtAgo(rt.last_ok)} ${rt.busy ? '· <b>正在消化…</b>' : ''}</span></div>
        <div class="row"><span class="k">消化次数</span><span class="v">
          成功 ${rt.ok_runs || 0} / 失败 ${rt.fail_runs || 0}</span></div>
        <div class="row"><span class="k">累计产出</span><span class="v">
          知识 ${rt.insights_total || 0} 条 · 技能 ${rt.skills_total || 0} 个</span></div>
        <div class="row"><span class="k">贡献者</span><span class="v">
          ${(rt.pending_owners || []).length ? esc(rt.pending_owners.join('、')) : '—'}</span></div>
        <div class="row"><span class="k">触发条件</span><span class="v">
          静默 ${rtCfg.quiet_sec || 25}s 或满 ${rtCfg.batch_max || 8} 条
          · 限流 ${rtCfg.min_interval || 30}s</span></div>
        ${rt.last_error ? '<div class="row"><span class="k">最近错误</span><span class="v" style="color:var(--err)">' +
          esc(rt.last_error) + '</span></div>' : ''}
      </div>
      <div class="v8-row">
        <button class="v8-btn primary" data-act="rt-poke">催一次（立即消化）</button>
        <button class="v8-btn" data-act="learn-refresh">刷新</button>
      </div>
      <div class="v8-log" style="margin-top:10px;max-height:150px">${esc((rt.log || []).slice(-10).join('\n') || '（暂无实时日志）')}</div>
    </div>

    <div class="v8-card">
      <h3>自动生成的技能 <span class="tag">${st.skills_auto}</span></h3>
      <div class="v8-notes">
        ${(st.auto_skills || []).length
          ? st.auto_skills.map(s => `
            <div class="v8-note">
              <span class="nm" title="${esc(s.file)}">
                <b>${esc(s.name || s.id)}</b>
                <span style="color:var(--hint);font-size:11px"> ${esc(s.id)}</span>
                ${(s.trigger || []).length ? '<br><span style="color:var(--dim);font-size:11px">触发：' +
                  esc(s.trigger.join(' / ')) + '</span>' : ''}
              </span>
              <span class="mt">${esc(s.mtime || '')}</span>
            </div>`).join('')
          : '<div class="v8-empty">还没有自动生成的技能。多聊几轮，夜里会自动长出来。</div>'}
      </div>
    </div>

    <div class="v8-card">
      <h3>自动部署
        <span class="tag ${dl.result === 'ok' ? 'ok' : (dl.result ? 'warn' : '')}">
          ${dl.result === 'ok' ? '上次成功' : (dl.result || '从未部署')}</span></h3>
      <div class="v8-kv">
        <div class="row"><span class="k">上次部署</span><span class="v">
          ${dl.ts ? fmtTs(dl.ts) + '（' + fmtAgo(dl.ts) + '）' : '—'}
          ${dl.elapsed ? '· ' + dl.elapsed + 's' : ''}</span></div>
        <div class="row"><span class="k">部署后版本</span><span class="v">
          ${esc((dl.health || {}).ver || '—')}</span></div>
        <div class="row"><span class="k">下次定时</span><span class="v">
          ${fmtTs(dep.next_run)}（每天 00:05）</span></div>
        <div class="row"><span class="k">待投递代码</span><span class="v">
          ${dep.has_staged ? '<b style="color:var(--warn)">' + (dep.staged_files || 0) + ' 个文件待应用</b>' : '无'}</span></div>
        <div class="row"><span class="k">模式</span><span class="v">
          有 staged 则应用新代码，否则自检式重启</span></div>
      </div>
      ${dep.alert ? '<div class="v8-log" style="margin-top:10px;background:#fef2f2;color:var(--err);border:1px solid #fbd5d5">' +
        esc(dep.alert) + '</div>' : ''}
      <div class="v8-row">
        <button class="v8-btn" data-act="learn-refresh">刷新</button>
      </div>
    </div>

    <div class="v8-card span2" ${running || (job.tail && job.tail.length) ? '' : 'hidden'} id="v8-learn-log-card">
      <h3>批量学习日志 ${running ? '<span class="tag warn">实时</span>' : ''}</h3>
      <div class="v8-log" id="v8-learn-log">${esc((job.tail || []).join('\n') || '（暂无日志）')}</div>
    </div>

    <div class="v8-card">
      <h3>学习历史 <span class="tag">${(hist.history || []).length} 天</span></h3>
      <div class="v8-notes">
        ${(hist.history || []).length
          ? hist.history.map(h => `
            <div class="v8-note">
              <span class="nm">${esc(h.date)}
                <span style="color:var(--hint);font-size:11px">
                  · 知识 ${h.insights} · 技能 ${h.skills} · ${esc(h.status)}</span>
              </span>
              <span class="mt">${esc(h.mtime)}</span>
            </div>`).join('')
          : '<div class="v8-empty">还没有学习记录</div>'}
      </div>
    </div>

    <div class="v8-card">
      <h3>知识库文件 <span class="tag">${(kb.items || []).length}</span></h3>
      <div class="v8-notes" id="v8-kb-list">
        ${(kb.items || []).length
          ? kb.items.map(k => `
            <div class="v8-note" data-kb="${esc(k.path)}">
              <span class="nm">${esc(k.name)}</span>
              <span class="mt">${(k.bytes / 1024).toFixed(1)} KB</span>
            </div>`).join('')
          : '<div class="v8-empty">知识库还是空的</div>'}
      </div>
      <div id="v8-kb-view"></div>
    </div>`;

    bindLearn();

    // 运行中：每 3 秒自动刷新日志
    clearTimeout(LEARN_TIMER);
    if (running && V8.view === 'learn') {
      LEARN_TIMER = setTimeout(() => {
        if (V8.view === 'learn') pollLearnLog();
      }, 3000);
    }
  }

  async function pollLearnLog() {
    try {
      const st = await api('/api/learn/status');
      const el = $id('v8-learn-log');
      const card = $id('v8-learn-log-card');
      const job = st.job || {};
      if (card) card.hidden = false;
      if (el) el.textContent = (job.tail || []).join('\n') || '（等待输出…）';
      if (!st.running) {
        toast('学习任务已结束');
        renderLearn();      // 跑完整体刷新一次
        return;
      }
      LEARN_TIMER = setTimeout(() => {
        if (V8.view === 'learn') pollLearnLog();
      }, 3000);
    } catch (e) {
      LEARN_TIMER = setTimeout(() => {
        if (V8.view === 'learn') pollLearnLog();
      }, 5000);
    }
  }

  function bindLearn() {
    body().onclick = async (ev) => {
      const kbn = ev.target.closest('[data-kb]');
      if (kbn) { showKb(kbn.dataset.kb); return; }
      const btn = ev.target.closest('[data-act]');
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === 'learn-refresh') { clearTimeout(LEARN_TIMER); return renderLearn(); }
      if (act === 'learn-kb-close') {
        const box = $id('v8-kb-view');
        if (box) box.innerHTML = '';
        return;
      }
      if (act === 'learn-run') return startLearn({}, btn);
      if (act === 'rt-poke') return pokeRealtime(btn);
      if (act === 'learn-force') {
        if (!confirm('强制全量会重新消化所有历史对话，耗时更久，确定？')) return;
        return startLearn({ force: true }, btn);
      }
      if (act === 'learn-dry') return startLearn({ dry_run: true }, btn);
    };
  }

  async function startLearn(opts, btn) {
    btn.disabled = true;
    try {
      const r = await api('/api/learn/run', { body: opts || {} });
      if (!r.ok) throw new Error(r.error || '启动失败');
      toast('已启动，日志会实时刷新');
      await renderLearn();
      pollLearnLog();
    } catch (e) {
      toast(e.message);
      btn.disabled = false;
    }
  }

  async function pokeRealtime(btn) {
    btn.disabled = true;
    try {
      const r = await api('/api/learn/poke', { body: {} });
      if (!r.ok) throw new Error(r.error || '催失败');
      toast('已催，稍候刷新看结果');
      setTimeout(() => { if (V8.view === 'learn') renderLearn(); }, 6000);
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; }
  }

  async function showKb(path) {
    try {
      const name = String(path).split('/').pop();
      const r = await api('/api/learn/kb/read?name=' + encodeURIComponent(name));
      if (!r.ok) throw new Error(r.error || '读取失败');
      const box = $id('v8-kb-view');
      box.innerHTML = '<div class="v8-card" style="box-shadow:none;margin-top:10px">' +
        '<h3>' + esc(r.name) + ' <span class="spacer"></span>' +
        '<button class="v8-btn" data-act="learn-kb-close">关闭</button></h3>' +
        '<div class="v8-log" style="max-height:320px;background:#fff;color:var(--text);' +
        'border:1px solid var(--border)">' + esc(r.content || '') + '</div></div>';
      box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } catch (e) { toast(e.message); }
  }

  /* ============================================================
     六、模型中心（v11）——豆包 / 千问 / 云端模型统一管理
     ============================================================ */
  let HUB = { providers: [], tools: [], auth: null };

  async function renderHub() {
    body().className = 'v8-body';
    body().innerHTML = '<div class="v8-card span2"><div class="v8-empty">读取供应商状态…</div></div>';
    try {
      const r = await api('/api/providers');
      HUB.providers = r.providers || [];
    } catch (e) {
      body().innerHTML = '<div class="v8-card span2"><h3>读取失败</h3>' +
        '<div class="v8-empty">' + esc(e.message) + '</div></div>';
      return;
    }
    try { HUB.tools = (await api('/api/providers/tools')).names || []; } catch (e) { HUB.tools = []; }
    try { HUB.auth = await api('/api/auth/status'); } catch (e) { HUB.auth = null; }
    paintHub();
  }

  function paintHub() {
    const ps = HUB.providers;
    const ready = ps.filter(p => p.has_key || !p.needs_key).length;
    const authed = HUB.auth || {};
    const warnHtml = (!authed.enabled)
      ? '<div class="v8-card span2" style="border-color:#e0a800">' +
        '<h3><span class="tag warn">安全提示</span></h3>' +
        '<div class="v8-empty" style="text-align:left;line-height:1.8">' +
        '当前<b>没有设置访问口令</b>。如果这个页面已经能通过公网访问，' +
        '任何人都能打开它并调用 Agent 的文件读写 / 命令执行能力。<br>' +
        '建议设置：在 DGX 上给服务加环境变量 <code>DGX_STUDIO_ACCESS_CODE=你的口令</code>，' +
        '或在下面「访问控制」里查看当前状态。</div></div>'
      : '';

    body().innerHTML = `
    <div class="v8-card span2">
      <h3>模型中心
        <span class="tag ${ready ? 'ok' : 'warn'}">${ready}/${ps.length} 可用</span>
        <span class="spacer"></span>
        <button class="v8-btn" data-act="hub-refresh">刷新</button></h3>
      <div class="v8-empty" style="text-align:left;line-height:1.85">
        本地 Ollama 不出网不要钱；豆包（火山方舟）、千问（阿里百炼）等云端模型需要先填密钥。
        <b>密钥只存在 DGX 本机</b>（<code>~/.dgx_studio/providers.json</code>，600 权限），
        页面永远只回显掩码，不会明文回传。<br>
        <b>火山方舟特别注意</b>：对话模型要在控制台「在线推理」里创建接入点，
        模型名填 <code>ep-xxxxxx</code> 这种接入点 ID，直接填 doubao-pro 会报模型不存在。
      </div>
      ${warnHtml}
    </div>

    ${ps.map(p => hubCard(p)).join('')}

    <div class="v8-card">
      <h3>Agent 工具层 <span class="tag">${HUB.tools.length}</span>
        <span class="spacer"></span>
        <button class="v8-btn" data-act="hub-refresh">刷新</button></h3>
      <div class="v8-empty" style="text-align:left;line-height:1.85">
        云模型（豆包/千问/GPT）支持 function calling，可以在对话里<b>真正动手</b>：
        读文件、跑命令、看 GPU、查知识库、联网搜索。<br>
        在聊天输入框下方打开「Agent 模式」即可启用。
      </div>
      <div class="v8-notes">
        ${HUB.tools.length
          ? HUB.tools.map(t => '<div class="v8-note"><span class="nm">' +
              '<b>' + esc(t) + '</b></span></div>').join('')
          : '<div class="v8-empty">工具层未加载</div>'}
      </div>
    </div>

    <div class="v8-card">
      <h3>访问控制
        <span class="tag ${authed.enabled ? 'ok' : 'warn'}">
          ${authed.enabled ? '已启用口令' : '未启用'}</span></h3>
      <div class="v8-kv">
        <div class="row"><span class="k">来源 IP</span><span class="v">${esc(authed.peer_ip || '—')}</span></div>
        <div class="row"><span class="k">识别为内网</span><span class="v">${authed.is_lan ? '是（免密）' : '否（需口令）'}</span></div>
        <div class="row"><span class="k">当前登录态</span><span class="v">${authed.logged_in ? '已登录' : '未登录'}</span></div>
        <div class="row"><span class="k">内网免密</span><span class="v">${authed.lan_free ? '开' : '关'}</span></div>
      </div>
      <div class="v8-empty" style="text-align:left;line-height:1.8;font-size:12px;margin-top:10px">
        密钥读写接口强制要求「已登录 或 内网来源」，公网匿名用户无法回填 API Key。
      </div>
    </div>`;
    bindHub();
  }

  function hubCard(p) {
    const okMs = p.models || [];
    return `
    <div class="v8-card" data-pid="${esc(p.id)}">
      <h3>${esc(p.name)}
        <span class="tag ${p.has_key || !p.needs_key ? 'ok' : 'err'}">
          ${p.needs_key ? (p.has_key ? '已配密钥' : '未配密钥') : '无需密钥'}</span>
        <span class="spacer"></span>
        <span style="font-size:11px;color:var(--hint)">${esc(p.vendor)}</span></h3>
      <div class="v8-kv">
        <div class="row"><span class="k">服务地址</span><span class="v">${esc(p.base)}</span></div>
        <div class="row"><span class="k">密钥</span><span class="v">${p.key_mask ? esc(p.key_mask) : '—'}</span></div>
        ${p.last_test ? '<div class="row"><span class="k">上次体检</span><span class="v">' +
          (p.last_test.ok ? '<span style="color:var(--ok)">通过</span>'
                          : '<span style="color:var(--err)">失败</span>') +
          ' · ' + esc(p.last_test.ts) + '</span></div>' : ''}
      </div>
      <div class="v8-empty" style="text-align:left;font-size:12px;line-height:1.7;margin:8px 0">
        ${esc(p.hint)}</div>
      <div class="v8-field">
        <label>API Key（填了只存在本机，页面不再明文显示）</label>
        <input class="v8-input" type="password" id="key-${esc(p.id)}"
          placeholder="${p.needs_key ? '粘贴密钥，留空表示不改' : '可选'}"
          autocomplete="off">
      </div>
      <div class="v8-field">
        <label>测试用模型名（火山方舟填接入点 ID）</label>
        <input class="v8-input" id="m-${esc(p.id)}"
          list="ml-${esc(p.id)}" placeholder="${esc(okMs[0] || '模型名')}">
        <datalist id="ml-${esc(p.id)}">
          ${okMs.map(m => '<option value="' + esc(m) + '">').join('')}
        </datalist>
      </div>
      <div class="v8-row">
        <button class="v8-btn primary" data-act="hub-save" data-id="${esc(p.id)}">保存密钥</button>
        <button class="v8-btn" data-act="hub-test" data-id="${esc(p.id)}">连通测试</button>
        <button class="v8-btn" data-act="hub-models" data-id="${esc(p.id)}">拉模型列表</button>
        ${p.has_key ? '<button class="v8-btn danger" data-act="hub-del" data-id="' + esc(p.id) + '">清除</button>' : ''}
      </div>
      <div class="v8-out" id="out-${esc(p.id)}" hidden></div>
    </div>`;
  }

  function bindHub() {
    body().querySelectorAll('[data-act]').forEach(btn => {
      btn.onclick = async () => {
        const a = btn.dataset.act;
        const id = btn.dataset.id;
        if (a === 'hub-refresh') return renderHub();
        if (a === 'hub-save') return hubSave(id, btn);
        if (a === 'hub-test') return hubTest(id, btn);
        if (a === 'hub-models') return hubModels(id, btn);
        if (a === 'hub-del') return hubDel(id, btn);
      };
    });
  }

  function hubOut(id, html, isErr) {
    const el = $id('out-' + id);
    if (!el) return;
    el.hidden = false;
    el.innerHTML = '<div class="v8-log" style="max-height:220px;' +
      (isErr ? 'color:var(--err);' : '') + '">' + html + '</div>';
  }

  async function hubSave(id, btn) {
    const key = ($id('key-' + id).value || '').trim();
    if (!key) return toast('请输入密钥');
    btn.disabled = true; btn.textContent = '保存中…';
    try {
      const r = await post('/api/providers/keys', { id, key });
      if (!r.ok) throw new Error(r.error || '保存失败');
      toast('已保存：' + (r.key_mask || ''));
      await renderHub();
    } catch (e) {
      hubOut(id, esc(e.message), true); toast(e.message);
    } finally { btn.disabled = false; btn.textContent = '保存密钥'; }
  }

  async function hubTest(id, btn) {
    const model = ($id('m-' + id).value || '').trim();
    btn.disabled = true; btn.textContent = '测试中…';
    hubOut(id, '正在连通测试，可能需要十几秒…');
    try {
      const r = await post('/api/providers/test', { id, model: model || undefined });
      if (r.ok) {
        hubOut(id, '<b style="color:var(--ok)">✓ 连通成功</b>\n' +
          '模型：' + esc(r.model || '') + '\n' +
          '耗时：' + r.elapsed + 's\n' +
          '思维链：' + (r.thinking ? '支持' : '不支持') + '\n' +
          '回复：' + esc(r.reply || ''));
        toast('连通成功');
      } else {
        hubOut(id, '<b style="color:var(--err)">✗ 失败</b>\n' +
          esc(r.error || '') + (r.tip ? '\n\n建议：' + esc(r.tip) : ''), true);
        toast('连通失败');
      }
      await renderHub();
    } catch (e) {
      hubOut(id, esc(e.message), true);
    } finally { btn.disabled = false; btn.textContent = '连通测试'; }
  }

  async function hubModels(id, btn) {
    btn.disabled = true; btn.textContent = '拉取中…';
    try {
      const r = await post('/api/providers/models/refresh', { id });
      if (!r.ok) throw new Error(r.error || '拉取失败');
      const ms = r.models || [];
      hubOut(id, '<b>共 ' + ms.length + ' 个模型</b>（已填入下方输入框的下拉）\n\n' +
        ms.slice(0, 60).map(esc).join('\n'));
      const inp = $id('m-' + id), dl = $id('ml-' + id);
      if (dl) dl.innerHTML = ms.map(m => '<option value="' + esc(m) + '">').join('');
      if (inp && ms.length) inp.value = ms[0];
      toast('拉取到 ' + ms.length + ' 个模型');
    } catch (e) {
      hubOut(id, esc(e.message), true); toast(e.message);
    } finally { btn.disabled = false; btn.textContent = '拉模型列表'; }
  }

  async function hubDel(id, btn) {
    if (!confirm('确定清除 ' + id + ' 的密钥？')) return;
    try {
      await post('/api/providers/keys/delete', { id });
      toast('已清除'); await renderHub();
    } catch (e) { toast(e.message); }
  }

  /* ---------------- 初始化 ---------------- */
  function init() {
    installSwitchHook();
    ensureWhoamiEl();
    // hash 直达：/#embodied  /#paper  /#notes  /#learn（便于分享和截图核验）
    const h = (location.hash || '').replace('#', '');
    if (V8_VIEWS[h]) setTimeout(() => showV8(h), 60);
    // 页面加载后探测能力，给侧栏加小圆点
    api('/api/capabilities').then(c => {
      V8.caps = c;
      const v = $id('ver-text');
      if (v) v.textContent = 'v' + (c.ver || '8.0');
      const d = c.detail || {};
      const mark = (view, ok) => {
        const n = document.querySelector('[data-view="' + view + '"]');
        if (!n) return;
        if (!n.querySelector('.v8-cap')) {
          const s = document.createElement('span');
          s.className = 'v8-cap';
          s.style.cssText = 'margin-left:auto;width:6px;height:6px;border-radius:50%;';
          n.appendChild(s);
        }
        n.querySelector('.v8-cap').style.background = ok ? '#2ecc71' : '#6b7280';
      };
      mark('embodied', d.ros2);
      mark('paper', d.tts_ready);
      mark('notes', d.git_repo);
      mark('learn', (d.learned_messages || 0) > 0 || d.skills_auto > 0);
    }).catch(() => {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // ⚠️ 必须在这里（IIFE 顶层）就装，不能等 DOMContentLoaded ——
  // app.js 的初始化一旦先发请求，那批请求就不带身份头了。
  installFetchHook();

  window.V8 = V8;
})();
