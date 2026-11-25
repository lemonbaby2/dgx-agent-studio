/* DGX Agent Studio v6 UI 扩展：技能芯片条 / 斜杠技能 / 朗读 */
/* 依赖 app.js 的全局：$, esc, api, md2html, userNode, scrollBottom, setBusy, toast, S */
(function () {
  'use strict';

  /* ---------- 技能入口（v16 起收进侧栏抽屉，不再铺满对话区） ---------- */
  async function loadSkillBar() {
    try {
      const d = await api('/api/skills');
      const n = (d && d.skills) ? d.skills.length : 0;
      const nav = $('nav-skills');
      if (nav && n) {
        const sp = nav.querySelector('span');
        if (sp) sp.textContent = '技能库 (' + n + ')';
      }
      // 抽屉由 v16skills.js 负责渲染；这里只做预热
      if (window.v16Skills) window.v16Skills.refresh();
    } catch (e) { /* 技能接口不可用时静默 */ }
  }

  /* ---------- 技能结果渲染进对话 ---------- */
  function renderSkillResult(text, d, name) {
    const box = $('chat');
    let inner = $('chat-inner');
    if (!inner) {
      box.innerHTML = '<div class="chat-inner" id="chat-inner"></div>';
      inner = $('chat-inner');
    }
    const empty = $('empty');
    if (empty) empty.style.display = 'none';
    try { inner.appendChild(userNode({ role: 'user', content: text })); } catch (e) {}
    const node = document.createElement('div');
    node.className = 'msg msg-ai';
    const model = (typeof S !== 'undefined' && S.session) ? S.session.model : 'skill';
    let body_html;
    if (d.answer && String(d.answer).trim()) {
      body_html = md2html(d.answer);
    } else {
      body_html = md2html(d.output || (d.error ? '**出错：** ' + d.error : '（无返回）'));
    }
    node.innerHTML = '<div class="ai-avatar">D</div><div class="ai-main">'
      + '<div class="ai-meta">' + esc(name || 'Skill') + ' · ' + esc(model) + '</div>'
      + '<div class="ai-body">' + body_html + '</div></div>';
    inner.appendChild(node);
    // 若既有 LLM 结论又有步骤原始数据，可折叠展示
    if (d.answer && d.output && String(d.output).trim()) {
      const det = document.createElement('details');
      det.className = 'skill-raw';
      const sum = document.createElement('summary');
      sum.textContent = '查看执行数据';
      const pre = document.createElement('pre');
      pre.textContent = d.output;
      det.appendChild(sum);
      det.appendChild(pre);
      node.querySelector('.ai-body').appendChild(det);
    }
    scrollBottom(true);
  }

  /* ---------- 自然语言命中提示（不自动执行，避免误触发） ---------- */
  function showSkillHint(skill, text) {
    const bar = $('skill-bar');
    if (!bar) return;
    const old = bar.querySelector('.skill-hint');
    if (old) old.remove();
    const b = document.createElement('button');
    b.className = 'skill-chip skill-hint';
    b.title = '点击执行该技能';
    b.innerHTML = '⚡ 检测到技能「' + esc(skill.name) + '」· 点击执行';
    b.onclick = function () {
      const inp = $('input');
      inp.value = '/' + skill.id + ' ' + text;
      b.remove();
      window.send();
    };
    bar.appendChild(b);
    setTimeout(function () { b.remove(); }, 20000);
  }

  /* ---------- 包装 send()：斜杠命令直接执行技能 ---------- */
  function wrapSend() {
    if (typeof window.send !== 'function') return;
    const orig = window.send;
    window.send = async function () {
      const inp = $('input');
      const text = (inp.value || '').trim();
      if (!text) return orig.apply(this, arguments);

      if (text.charAt(0) === '/') {
        const id = text.slice(1).split(/\s+/)[0];
        const args = text.slice(1 + id.length).trim();
        inp.value = '';
        setBusy(true);
        $('status-text').textContent = '技能执行中…';
        try {
          const d = await api('/api/skill/run', { id: id, args: args });
          renderSkillResult(text, d, d.name || id);
        } catch (e) {
          toast('技能执行失败：' + e.message);
        }
        setBusy(false);
        $('status-text').textContent = '就绪';
        return;
      }

      try {
        const m = await api('/api/skill/match', { text: text });
        if (m && m.hit) showSkillHint(m.skill, text);
      } catch (e) { /* 匹配失败不影响普通对话 */ }

      return orig.apply(this, arguments);
    };
    const bs = $('btn-send');
    if (bs) bs.onclick = window.send;
  }

  /* ---------- 朗读（浏览器 speechSynthesis，零服务端依赖） ---------- */
  function setupTTS() {
    const synth = window.speechSynthesis;
    if (!synth) return;
    document.addEventListener('click', function (e) {
      const b = e.target.closest('.tts-btn');
      if (!b) return;
      const msg = b.closest('.msg-ai');
      const body = msg && msg.querySelector('.ai-body');
      if (!body) return;
      if (synth.speaking) { synth.cancel(); b.classList.remove('speaking'); return; }
      document.querySelectorAll('.tts-btn.speaking').forEach(function (x) {
        x.classList.remove('speaking');
      });
      const u = new SpeechSynthesisUtterance((body.innerText || '').slice(0, 1500));
      u.lang = 'zh-CN';
      u.rate = 1.05;
      u.onend = function () { b.classList.remove('speaking'); };
      b.classList.add('speaking');
      synth.speak(u);
    });
    const mo = new MutationObserver(function () {
      document.querySelectorAll('.ai-meta').forEach(function (m) {
        if (!m.querySelector('.tts-btn')) {
          const b = document.createElement('button');
          b.className = 'tts-btn';
          b.title = '朗读 / 停止（浏览器语音）';
          b.textContent = '🔊';
          m.appendChild(b);
        }
      });
    });
    const c = $('chat');
    if (c) mo.observe(c, { childList: true, subtree: true });
  }

  function boot() {
    wrapSend();
    setupTTS();
    loadSkillBar();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
