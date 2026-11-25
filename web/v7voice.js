/* DGX Agent Studio v7 · 语音交互 + 论文朗读
   依赖 app.js 全局：$, esc, api, md2html, userNode, scrollBottom, setBusy, toast */
(function () {
  'use strict';

  var rec = null, chunks = [], streamRef = null;

  /* ---------- 工具 ---------- */
  function blobToDataURL(blob) {
    return new Promise(function (res, rej) {
      var fr = new FileReader();
      fr.onload = function () { res(fr.result); };
      fr.onerror = rej;
      fr.readAsDataURL(blob);
    });
  }

  function inner() {
    var box = $('chat');
    var el = $('chat-inner');
    if (!el) {
      box.innerHTML = '<div class="chat-inner" id="chat-inner"></div>';
      el = $('chat-inner');
    }
    var empty = $('empty');
    if (empty) empty.style.display = 'none';
    return el;
  }

  function addUser(text) {
    try { inner().appendChild(userNode({ role: 'user', content: text })); }
    catch (e) { }
  }

  function addAI(title, bodyHtml) {
    var node = document.createElement('div');
    node.className = 'msg msg-ai';
    node.innerHTML = '<div class="ai-avatar">D</div><div class="ai-main">'
      + '<div class="ai-meta">' + esc(title) + '</div>'
      + '<div class="ai-body">' + bodyHtml + '</div></div>';
    inner().appendChild(node);
    scrollBottom(true);
    return node;
  }

  /* ---------- 朗读（浏览器 speechSynthesis） ---------- */
  function pickZhVoice() {
    var vs = window.speechSynthesis ? speechSynthesis.getVoices() : [];
    for (var i = 0; i < vs.length; i++) {
      if (/zh|Chinese|中文/i.test(vs[i].lang + ' ' + vs[i].name)) return vs[i];
    }
    return null;
  }

  function speak(text) {
    if (!window.speechSynthesis) { toast('浏览器不支持朗读'); return; }
    speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(String(text || '').slice(0, 3000));
    u.lang = 'zh-CN';
    var v = pickZhVoice();
    if (v) u.voice = v;
    u.rate = 1.0;
    speechSynthesis.speak(u);
    toast('🔊 正在朗读…');
  }

  /* ---------- 麦克风 ---------- */
  async function stopAndSend() {
    if (rec && rec.state === 'running') rec.stop();
  }

  async function startRec() {
    try {
      var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef = stream;
      chunks = [];
      var mime = '';
      if (window.MediaRecorder && MediaRecorder.isTypeSupported) {
        var cands = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
        for (var i = 0; i < cands.length; i++) {
          if (MediaRecorder.isTypeSupported(cands[i])) { mime = cands[i]; break; }
        }
      }
      rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      rec.onstop = async function () {
        try { streamRef.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { }
        var btn = $('btn-mic');
        if (btn) { btn.classList.remove('rec'); btn.querySelector('.ibtn-label').textContent = '语音'; }
        var blob = new Blob(chunks, { type: (chunks[0] && chunks[0].type) || 'audio/webm' });
        if (!blob.size) { toast('没录到声音'); return; }
        await asrUpload(blob);
      };
      rec.start();
      var b = $('btn-mic');
      if (b) { b.classList.add('rec'); b.querySelector('.ibtn-label').textContent = '停止'; }
      toast('🎤 录音中…再点一次结束并识别');
    } catch (e) {
      toast('麦克风不可用：' + e.message + '（需 HTTPS 或 localhost，且浏览器已授权）');
    }
  }

  async function asrUpload(blob) {
    setBusy(true);
    var st = $('status-text'); if (st) st.textContent = '语音识别中…';
    try {
      var b64 = await blobToDataURL(blob);
      var d = await api('/api/speech/asr', { audio: b64, lang: 'zh', size: 'small' });
      if (d && d.ok && d.text) {
        var inp = $('input');
        if (inp) { inp.value = d.text; inp.focus(); }
        toast('识别成功（' + (d.backend || '') + '）：' + d.text.slice(0, 50));
      } else {
        toast('识别失败：' + ((d && d.error) || '空结果'));
      }
    } catch (e) { toast('识别异常：' + e.message); }
    setBusy(false);
    var st2 = $('status-text'); if (st2) st2.textContent = '就绪';
  }

  /* ---------- 论文 PDF（4 种模式：精读 / 导图 / 总结 / 创新点） ---------- */
  var _pdfB64 = '', _pdfName = '';
  var PAPER_MODES = [
    { id: 'paper-close-read', label: '📖 精读' },
    { id: 'paper-mindmap', label: '🧠 思维导图' },
    { id: 'paper-summary', label: '📝 总结' },
    { id: 'paper-novelty', label: '💡 创新点' }
  ];

  async function pickPdf() {
    var f = document.createElement('input');
    f.type = 'file';
    f.accept = 'application/pdf,.pdf';
    f.onchange = async function () {
      var file = f.files && f.files[0];
      if (!file) return;
      setBusy(true);
      var st = $('status-text'); if (st) st.textContent = '读取论文…';
      try {
        _pdfName = file.name;
        _pdfB64 = await blobToDataURL(file);
        addUser('📄 ' + file.name);
        var meta = '已载入：' + file.name + '（' + (file.size / 1048576).toFixed(1) + ' MB）'
          + '，选择模式开始';
        addAI(meta, '<div class="paper-hint">选一个模式：精读（逐节拆解）· 思维导图（结构树）· 总结（要点+数字表）· 创新点（挤水分）</div>');
      } catch (e) { toast('异常：' + e.message); }
      setBusy(false);
      var st2 = $('status-text'); if (st2) st2.textContent = '就绪';
    };
    f.click();
  }

  async function runPaperSkill(id, label) {
    if (!_pdfB64) { toast('请先点「📄 论文」选一篇 PDF'); return; }
    setBusy(true);
    var st = $('status-text');
    if (st) st.textContent = label + ' 生成中（英文长论文可能 2-5 分钟）…';
    try {
      var d = await api('/api/skill/run', { id: id, pdf: _pdfB64 });
      var ans = (d && (d.answer || d.output)) || '';
      if (!ans) { toast('无返回：' + ((d && d.answer_error) || '')); }
      else {
        var meta = label + ' · ' + _pdfName + ' · '
          + (d.pages || 0) + '页 / ' + (d.chars || 0) + '字';
        var node = addAI(meta, md2html(ans));
        var bar = document.createElement('div');
        bar.className = 'paper-modes';
        PAPER_MODES.forEach(function (m) {
          var b = document.createElement('button');
          b.className = 'paper-mode-btn' + (m.id === id ? ' active' : '');
          b.textContent = m.label;
          b.onclick = function () { runPaperSkill(m.id, m.label); };
          bar.appendChild(b);
        });
        node.querySelector('.ai-body').appendChild(bar);
        scrollBottom(true);
        speak(ans);
      }
    } catch (e) { toast('生成失败：' + e.message); }
    setBusy(false);
    var st2 = $('status-text'); if (st2) st2.textContent = '就绪';
  }

  /* ---------- 播放音乐（浏览器播放，DGX 无声卡直连你耳朵） ---------- */
  function pickMusic() {
    var f = document.createElement('input');
    f.type = 'file';
    f.accept = 'audio/*';
    f.onchange = function () {
      var file = f.files && f.files[0];
      if (!file) return;
      var url = URL.createObjectURL(file);
      var node = document.createElement('div');
      node.className = 'msg msg-ai';
      node.innerHTML = '<div class="ai-avatar">D</div><div class="ai-main">'
        + '<div class="ai-meta">🎵 正在播放：' + esc(file.name) + '</div>'
        + '<div class="ai-body"><audio controls autoplay src="' + url + '" '
        + 'style="width:100%;margin-top:6px"></audio></div></div>';
      inner().appendChild(node);
      scrollBottom(true);
      toast('🎵 播放：' + file.name);
    };
    f.click();
  }

  /* ---------- 注入按钮 ---------- */
  function inject() {
    var left = document.querySelector('.foot-left');
    if (!left || $('btn-mic')) return;

    var mic = document.createElement('button');
    mic.className = 'ibtn';
    mic.id = 'btn-mic';
    mic.title = '点麦克风说话（USB 麦克风）→ 自动转成文字';
    mic.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v4"/></svg>'
      + '<span class="ibtn-label">语音</span>';
    mic.onclick = function () {
      if (rec && rec.state === 'running') stopAndSend(); else startRec();
    };
    left.appendChild(mic);

    var pdf = document.createElement('button');
    pdf.className = 'ibtn';
    pdf.id = 'btn-pdf';
    pdf.title = '上传论文 PDF → 读完讲给你听（自动朗读）';
    pdf.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h5"/></svg>'
      + '<span class="ibtn-label">论文</span>';
    pdf.onclick = pickPdf;
    left.appendChild(pdf);

    var mus = document.createElement('button');
    mus.className = 'ibtn';
    mus.id = 'btn-music';
    mus.title = '播放本地音乐（浏览器播放；DGX 无声卡直连你耳朵）';
    mus.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'
      + '<span class="ibtn-label">音乐</span>';
    mus.onclick = pickMusic;
    left.appendChild(mus);

    // 能力徽标
    api('/api/speech/status').then(function (d) {
      var ms = $('model-status');
      if (!ms || !d) return;
      var tag = [];
      tag.push(d.asr ? '🎤ASR' : '🎤ASR✗');
      tag.push(d.pdf ? '📄PDF' : '📄PDF✗');
      if (ms.textContent.indexOf('·') < 0) ms.textContent += ' · ' + tag.join(' ');
    }).catch(function () { });
  }

  function boot() {
    inject();
    if (window.speechSynthesis) speechSynthesis.getVoices(); // 预载音色
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }

  window.v7speak = speak;
  window.v7PickPdf = pickPdf;
  window.v7RunPaper = runPaperSkill;
})();
