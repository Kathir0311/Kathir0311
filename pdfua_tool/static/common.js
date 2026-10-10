/* Shared helpers + settings dialog. */
window.UA = (function () {
  const calm = () => window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  // toasts stack and leave softly (V4.21)
  function toast(msg, ms) {
    const t = document.createElement('div');
    t.className = 'toast'; t.textContent = msg;
    t.setAttribute('role', 'status');
    const others = [...document.querySelectorAll('.toast:not(.out)')];
    t.style.setProperty('--stack', String(others.length));
    document.body.appendChild(t);
    setTimeout(() => {
      t.classList.add('out');
      setTimeout(() => t.remove(), calm() ? 0 : 260);
    }, ms || 2800);
  }
  // a thin bar at the top while the server works (V4.21)
  let busyN = 0, busyT = null;
  function busy(d) {
    busyN = Math.max(0, busyN + d);
    let bar = document.getElementById('uaBusy');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'uaBusy';
      bar.setAttribute('aria-hidden', 'true');
      document.body.appendChild(bar);
    }
    clearTimeout(busyT);
    if (busyN > 0) busyT = setTimeout(() => bar.classList.add('on'), 140);
    else { bar.classList.remove('on'); bar.classList.add('done'); setTimeout(() => bar.classList.remove('done'), 400); }
  }
  async function post(url, body) {
    busy(1);
    try {
      const r = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
      });
      let d = {};
      try { d = await r.json(); } catch (e) { d = { error: 'server error ' + r.status }; }
      if (!r.ok && !d.error) d.error = 'server error ' + r.status;
      return d;
    } finally { busy(-1); }
  }
  async function get(url) {
    busy(1);
    try {
      const r = await fetch(url);
      return r.json();
    } finally { busy(-1); }
  }
  // a soft pulse where a button is pressed (V4.21)
  document.addEventListener('pointerdown', (e) => {
    if (calm() || e.button !== 0) return;
    const b = e.target.closest && e.target.closest('button:not([disabled]), .btn');
    if (!b) return;
    const p = document.createElement('span');
    p.className = 'ua-pulse';
    p.style.left = e.clientX + 'px';
    p.style.top = e.clientY + 'px';
    document.body.appendChild(p);
    setTimeout(() => p.remove(), 520);
  }, { passive: true });
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
    }
    kids.flat().forEach((k) => { if (k != null) e.append(k.nodeType ? k : document.createTextNode(k)); });
    return e;
  }
  // the profile of a file: users and admins change it (V4.20)
  async function pickProfile(jid, filename, onDone) {
    const dlg = document.getElementById('profPick');
    if (!dlg) return;
    const d = await get('/api/job/' + jid + '/profile');
    if (!d || d.error) { toast((d && d.error) || 'could not read the profiles'); return; }
    let chosen = d.profile;
    const go = document.getElementById('ppGo');
    document.getElementById('ppFile').textContent = (filename || '') + ' — now: ' + d.profile;
    const list = document.getElementById('ppList');
    list.replaceChildren(...d.profiles.map((p) => {
      const b = el('button', { type: 'button', class: p === d.profile ? 'cur' : '', text: p });
      b.addEventListener('click', () => {
        chosen = p;
        list.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
        go.disabled = p === d.profile;
      });
      return b;
    }));
    const h = (d.history || []).slice(-3).reverse();
    document.getElementById('ppHist').textContent = h.length ? 'Changed before: ' + h.map((x) => `${x.from} → ${x.to} by ${x.by} ${String(x.at).replace('T', ' ').slice(0, 16)}`).join(' · ') : '';
    go.disabled = true;
    go.onclick = async () => {
      go.disabled = true;
      const r = await post('/api/job/' + jid + '/profile', { profile: chosen, reanalyse: document.getElementById('ppRean').checked });
      if (r.error) { toast(r.error, 4500); go.disabled = false; return; }
      dlg.close();
      toast(`Profile: ${r.profile}` + (r.reanalysed ? ' — analysing the file again' : ' — from the next build on'), 3500);
      if (onDone) onDone(r);
    };
    dlg.showModal();
  }

  // ---- speech for the reading-order checks (V4.21.4): one speed for the app
  const RATE_KEY = 'ua-speech-rate';
  function speechRate(v) {
    if (v != null) { try { localStorage.setItem(RATE_KEY, String(v)); } catch (e) { /* none */ } }
    let r = 0.9;
    try { r = parseFloat(localStorage.getItem(RATE_KEY) || '0.9') || 0.9; } catch (e) { /* none */ }
    return Math.max(0.3, Math.min(3, r));
  }
  /* V4.26 privacy: only voices that run on this computer. Some browser voices
     (the "online" / network ones) send the text they say to a web service —
     a document's words must never leave the computer that way. */
  function localVoices() {
    const S = window.speechSynthesis;
    if (!S) return [];
    return S.getVoices().filter((v) => v.localService === true);
  }
  function localVoice(lang) {
    const vs = localVoices();
    const l = String(lang || '').toLowerCase().slice(0, 2);
    return vs.find((v) => v.default && (!l || v.lang.toLowerCase().startsWith(l)))
      || (l && vs.find((v) => v.lang.toLowerCase().startsWith(l))) || vs.find((v) => v.default) || vs[0] || null;
  }
  let sayToken = 0;
  function hush() {
    sayToken++;
    try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) { /* none */ }
  }
  /* Say `text` and resolve when it has been said (true) or was cut off
     (false). Without a voice the time it would take passes instead. */
  function say(text, opts) {
    const o = opts || {};
    const rate = o.rate || speechRate();
    hush();
    const my = sayToken;
    const words = String(text || '').split(/\s+/).filter(Boolean).length;
    const timed = () => new Promise((res) => setTimeout(() => res(my === sayToken), Math.max(500, words * 60000 / (165 * rate))));
    const S = window.speechSynthesis;
    const voice = localVoice(o.lang);
    if (!S || o.silent || !String(text || '').trim() || !voice) return timed();
    return new Promise((res) => {
      let started = false, finished = false;
      const end = (ok) => { if (!finished) { finished = true; res(ok && my === sayToken); } };
      const u = new SpeechSynthesisUtterance(String(text));
      u.voice = voice;
      u.rate = rate;
      if (o.lang) u.lang = o.lang;
      u.onstart = () => { started = true; };
      u.onend = () => end(true);
      u.onerror = () => end(false);
      S.speak(u);
      // a voice that never starts (audio blocked): the reading time instead
      setTimeout(() => { if (!started && !finished) { S.cancel(); timed().then(end); } }, 2500);
    });
  }
  /* A speed slider bound to the app's speech speed; `onChange(rate)`. */
  function rateControl(id, onChange) {
    const v = el('span', { class: 'tiny ratev' });
    const inp = el('input', { type: 'range', id, min: '0.3', max: '2', step: '0.1', value: String(speechRate()),
      title: 'Reading speed', 'aria-label': 'Reading speed' });
    const show = () => { v.textContent = `${parseFloat(inp.value).toFixed(1)}×`; };
    inp.addEventListener('input', () => { speechRate(parseFloat(inp.value)); show(); if (onChange) onChange(parseFloat(inp.value)); });
    show();
    return el('label', { class: 'ratectl', title: 'Reading speed (slower ← → faster)' }, el('span', { text: '🐢' }), inp, el('span', { text: '🐇' }), v);
  }

  return { toast, post, get, esc, el, pickProfile, busy, say, hush, speechRate, rateControl, localVoices, localVoice };
})();

(function settings() {
  const dlg = document.getElementById('settingsDlg');
  const btn = document.getElementById('settingsBtn');
  if (!dlg || !btn) return;
  const $ = (id) => document.getElementById(id);
  async function envLine() {
    const s = await UA.get('/api/settings');
    const e = s.env || {};
    $('envLine').textContent = 'Tesseract: ' + (e.tesseract ? 'yes (' + (e.tess_langs || []).join(', ') + ')' : 'not found') +
      ' · veraPDF: ' + (e.verapdf || 'not found') + ' · version ' + e.version;
    const p = e.parallel || {};
    if ($('cpuInfo')) $('cpuInfo').textContent = `This server has ${p.cores} CPU core(s); now: ${p.files} file(s) at the same time × ${p.page_procs} core(s) per file.`;
  }
  let taskInfo = null;
  async function renderTasks() {
    const box = $('taskRows');
    if (!box) return;
    taskInfo = await UA.get('/api/ai/tasks');
    const models = taskInfo.models || [];
    box.replaceChildren(...Object.entries(taskInfo.tasks).map(([key, t]) => {
      const sel = UA.el('select', { class: 'sm', 'data-task': key },
        UA.el('option', { value: '', text: `General ${t.kind} model` }),
        ...models.filter((m) => (t.kind === 'vision' ? m.vision : m.chat))
          .map((m) => UA.el('option', { value: m.name, text: m.name + (m.size_gb ? ` · ${m.size_gb} GB` : '') })));
      if (t.set && !models.some((m) => m.name === t.set)) sel.append(UA.el('option', { value: t.set, text: t.set + ' (not installed)' }));
      sel.value = t.set || '';
      return UA.el('label', { class: 'taskrow small' },
        UA.el('span', { text: t.label }), UA.el('span', { class: 'tiny muted', text: t.kind }), sel);
    }));
  }
  function taskModels() {
    const out = {};
    document.querySelectorAll('#taskRows select[data-task]').forEach((s) => { out[s.dataset.task] = s.value; });
    return out;
  }
  btn.addEventListener('click', () => { dlg.showModal(); envLine(); renderTasks(); });
  async function testConnection() {
    $('pingOut').textContent = 'contacting…';
    const d = await UA.post('/api/ai/ping', {
      base_url: $('cfgUrl').value, text_model: $('cfgText').value, vision_model: $('cfgVision').value,
      task_models: taskModels()
    });
    if (!d.ok) { $('pingOut').textContent = d.error; return; }
    const det = d.details || [];
    const fill = (id, pred) => {
      const dl = $(id);
      dl.replaceChildren(...det.filter(pred).map((m) => UA.el('option', { value: m.name })));
    };
    fill('ollamaVision', (m) => m.vision);
    fill('ollamaText', (m) => m.chat);
    const lines = ['connected — ' + det.length + ' model(s) installed'];
    det.forEach((m) => lines.push('  • ' + m.name + (m.vision ? '  [vision]' : '') + (m.chat ? '' : '  [not a chat model]') + (m.size_gb ? '  ' + m.size_gb + ' GB' : '')));
    lines.push('');
    lines.push('General vision model: ' + (d.vision_model || '— none —') + (d.vision_note ? '   (' + d.vision_note + ')' : ''));
    lines.push('General text model:   ' + (d.text_model || '— none —') + (d.text_note ? '   (' + d.text_note + ')' : ''));
    if (d.tasks) {
      lines.push('');
      lines.push('Each job will use:');
      Object.values(d.tasks).forEach((t) => lines.push('  • ' + (t.label + ':').padEnd(46) + (t.uses || '— none —')));
    }
    $('pingOut').textContent = lines.join('\n');
  }
  $('pingBtn').addEventListener('click', testConnection);
  if ($('cfgRecommended')) $('cfgRecommended').addEventListener('click', () => {
    $('cfgEnabled').checked = true;
    $('cfgTimeout').value = 600;
    if ($('cfgKeep')) $('cfgKeep').value = '30m';
    if ($('cfgImgPx')) $('cfgImgPx').value = 1100;
    $('cfgFiles').value = 5;
    $('cfgProcs').value = 10;
    UA.toast('AI on · 600 s · model kept 30 min · pictures 1100 px · 5 files × 10 cores — press Save to keep them');
  });
  $('saveCfg').addEventListener('click', async () => {
    const d = await UA.post('/api/settings', {
      ai: {
        base_url: $('cfgUrl').value, text_model: $('cfgText').value,
        vision_model: $('cfgVision').value, enabled: $('cfgEnabled').checked,
        timeout: $('cfgTimeout').value, task_models: taskModels(),
        keep_alive: $('cfgKeep') ? $('cfgKeep').value : undefined,
        max_image_px: $('cfgImgPx') ? $('cfgImgPx').value : undefined
      },
      verapdf: $('cfgVera').value,
      parallel: { files: parseInt($('cfgFiles').value || '0', 10), page_procs: parseInt($('cfgProcs').value || '0', 10) }
    });
    UA.toast(d.ok ? 'Settings saved' : (d.error || 'failed'));
    envLine();
  });
})();

/* the user menu and "Change my password" (V4.19) */
(function userMenu() {
  const btn = document.getElementById('userBtn');
  const pop = document.getElementById('userPop');
  if (!btn || !pop) return;
  const close = () => { pop.classList.add('hidden'); btn.setAttribute('aria-expanded', 'false'); };
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = pop.classList.toggle('hidden') === false;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  document.addEventListener('click', (e) => { if (!pop.contains(e.target)) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  const dlg = document.getElementById('pwDlg');
  const pw = document.getElementById('pwBtn');
  if (!dlg || !pw) return;
  pw.addEventListener('click', () => { close(); dlg.showModal(); document.getElementById('pwOld').focus(); });
  document.getElementById('pwGo').addEventListener('click', async () => {
    const o = document.getElementById('pwOld').value, n = document.getElementById('pwNew').value,
      n2 = document.getElementById('pwNew2').value, out = document.getElementById('pwOut');
    if (n !== n2) { out.textContent = 'The two new passwords are not the same.'; return; }
    const d = await UA.post('/api/me', { old: o, new: n });
    out.textContent = d.error || 'Password changed.';
    if (!d.error) setTimeout(() => dlg.close(), 900);
  });
})();

/* a request made after the sign-in ran out: back to the sign-in page */
(function signedOut() {
  const orig = window.fetch;
  window.fetch = async function (...a) {
    const r = await orig.apply(this, a);
    if (r.status === 401) {
      try {
        const d = await r.clone().json();
        if (d && d.login) location.href = '/login?next=' + encodeURIComponent(location.pathname);
      } catch (e) { /* not JSON */ }
    }
    return r;
  };
})();
