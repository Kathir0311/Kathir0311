/* Dezquare PDF Accessibility Studio — the admin page (V4.19; overview, profiles and sample files V4.20). */
(function () {
  const $ = (id) => document.getElementById(id);
  const el = UA.el;
  const STATES = window.STATES || {};
  const PRIO = { urgent: 0, high: 1, normal: 2, low: 3 };
  const ROLE = { admin: 'Admin', production: 'Production', user: 'Production', qa: 'QA', final_qa: 'Final QA' };
  const fmt = (s) => (s || '').replace('T', ' ').slice(0, 16);
  const mb = (b) => b >= 1073741824 ? (b / 1073741824).toFixed(1) + ' GB' : b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round((b || 0) / 1024) + ' KB';
  let files = [];
  let sel = new Set();
  let openRow = null;
  let users = [];

  async function post(url, body) {
    const d = await UA.post(url, body);
    if (d.error) UA.toast(d.error, 4500);
    return d;
  }

  // ------------------------------------------------------------ tabs
  function show(tab) {
    document.querySelectorAll('.atabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
    document.querySelectorAll('.apane').forEach((p) => p.classList.toggle('hidden', p.id !== 'pane-' + tab));
    try { localStorage.setItem('ua-admin-tab', tab); } catch (e) { /* ignore */ }
    if (tab === 'overview') overview();
    if (tab === 'files') loadFiles();
    if (tab === 'users') loadUsers();
    if (tab === 'activity') loadLog();
    if (tab === 'templates') loadTemplates();
    if (tab === 'samples') { loadSamples(); loadLearning(); }
    if (tab === 'brain') { loadBrain(); kbSearch(); }
    if (tab === 'system') loadSystem();
    if (tab === 'workflow') loadWorkflow();
    if (tab === 'time') loadTime();
    const pane = $('pane-' + tab);
    if (pane) { pane.classList.remove('tabin'); void pane.offsetWidth; pane.classList.add('tabin'); }
    revealCards(pane);
  }
  document.querySelectorAll('.atabs button').forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
  document.querySelectorAll('[data-goto]').forEach((b) => b.addEventListener('click', () => show(b.dataset.goto)));

  // ------------------------------------------------------------ overview
  function countUp(node) {
    const n = parseInt(node.dataset.n, 10) || 0;
    if (!n || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 650);
      node.textContent = Math.round(n * (1 - Math.pow(1 - k, 3))).toLocaleString();
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  const day = (iso) => new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short' }).slice(0, 2);
  function hello() {
    const h = new Date().getHours();
    return (h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening') + (window.ME ? ', ' + window.ME : '');
  }
  async function overview() {
    const [d, today] = await Promise.all([UA.get('/api/admin/overview'), UA.get('/api/admin/time?days=1').catch(() => null)]);
    if (!d || d.error) return;
    const tile = (big, label, sub, cls, go) => {
      const t = el('div', { class: 'tile ' + (cls || '') + (go ? ' link' : ''), title: go ? 'Show these files' : '' },
        el('div', { class: 'tbig', 'data-n': typeof big === 'number' ? big : '', text: String(big) }), el('div', { class: 'tlabel', text: label }),
        sub ? el('div', { class: 'tsub', text: sub }) : null);
      if (go) t.addEventListener('click', go);
      return t;
    };
    const filt = (o) => () => {
      clearFilters();
      Object.entries(o).forEach(([k, v]) => { $(k).value = v; });
      shownLimit = PAGE;
      show('files');
    };
    const open = d.users.reduce((a, u) => a + u.open, 0);
    const fx = d.fixed || {};
    const pct = fx.before ? Math.round(100 * (fx.before - fx.after) / fx.before) : 0;
    $('ovHello').textContent = hello();
    $('ovSub').textContent = `${d.files.toLocaleString()} files · ${d.pages.toLocaleString()} pages · ${d.users.filter((u) => u.active).length} users · version ${d.version || ''}`;
    $('aTiles').replaceChildren(
      tile(d.files, 'files', `${d.pages.toLocaleString()} pages`, 't-files', filt({})),
      tile(d.pool.files, 'not given yet', `${d.pool.pages} pages in the pool`, 't-pool', filt({ fUser: '-' })),
      tile(open, 'open with users', `${(d.work.in_progress || 0)} in progress`, 't-open'),
      tile((d.work.qa || 0) + (d.work.submitted || 0) + (d.work.qa_progress || 0) + (d.work.final_qa || 0) + (d.work.fqa_progress || 0), 'in QA / Final QA',
        `${(d.work.rework || 0) + (d.work.returned || 0)} in rework`, 't-check', () => show('workflow')),
      tile((d.work.approved || 0) + (d.work.delivered || 0), 'ready / delivered', `${(d.approved_pages || 0).toLocaleString()} pages done`, 't-done', filt({ fWork: 'approved' })),
      tile(d.overdue || 0, 'overdue', `${d.urgent || 0} urgent open`, d.overdue ? 'bad' : 't-due', filt({ fSort: 'due' })),
      tile(d.busy, 'working now', d.failed ? `${d.failed} failed` : 'none failed', d.failed ? 'bad' : 't-busy', d.failed ? filt({ fStage: 'error' }) : null),
      tile(pct, '% errors fixed', `${fx.before || 0} → ${fx.after || 0} checker errors`, 't-fix'),
      today && !today.error ? tile(dur((today.work_s || 0) + (today.qc_s || 0)), 'worked today',
        `${today.users.length} people · ${dur(today.work_s || 0)} working · ${dur(today.qc_s || 0)} QC`, 't-time', () => show('time')) : null);
    $('aTiles').querySelectorAll('.tbig').forEach(countUp);
    revealCards($('pane-overview'));
    $('poolCount').textContent = d.pool.files ? String(d.pool.files) : '';
    // last 14 days
    const tmax = Math.max(1, ...d.trend.map((t) => Math.max(t.uploaded, t.built, t.approved)));
    $('aTrend').replaceChildren(...d.trend.map((t, i) => el('div', { class: 'sday', title: `${t.day}: ${t.uploaded} uploaded, ${t.built} built, ${t.approved} approved` },
      el('div', { class: 'sbars' },
        el('i', { class: 'up', style: `height:${100 * t.uploaded / tmax}%;animation-delay:${i * 25}ms` }),
        el('i', { class: 'bu', style: `height:${100 * t.built / tmax}%;animation-delay:${i * 25 + 60}ms` }),
        el('i', { class: 'ap', style: `height:${100 * t.approved / tmax}%;animation-delay:${i * 25 + 120}ms` })),
      el('span', { text: day(t.day) }))));
    // work status + ring
    const wmax = Math.max(1, ...Object.values(d.work));
    $('aWork').replaceChildren(...Object.entries(d.states).map(([k, label], i) => el('div', { class: 'hbar link', title: 'Show these files', onclick: filt({ fWork: k || '' }) },
      el('span', { class: 'hk', text: label }),
      el('div', { class: 'ht' }, el('i', { class: ['approved', 'delivered'].includes(k) ? 'ok' : ['returned', 'rework', 'on_hold'].includes(k) ? 'err' : ['submitted', 'qa', 'qa_progress', 'final_qa', 'fqa_progress'].includes(k) ? 'warn' : 'info',
        style: `width:${100 * (d.work[k] || 0) / wmax}%;animation-delay:${i * 40}ms` })),
      el('b', { text: String(d.work[k] || 0) }))));
    const done = (d.work.approved || 0) + (d.work.delivered || 0);
    const share = d.files ? Math.round(100 * done / d.files) : 0;
    $('aRing').replaceChildren(el('div', { class: 'ringwrap' },
      el('div', { class: 'donut', style: `--p:${share}` }, el('b', { text: share + '%' })),
      el('div', { class: 'small' }, el('b', { text: 'approved' }), el('div', { class: 'tiny muted', text: `${done} of ${d.files} files` }))));
    // due soon
    $('aOverdue').classList.toggle('hidden', !d.overdue);
    $('aOverdue').textContent = `${d.overdue} overdue`;
    $('aDue').replaceChildren(...(d.due.length ? d.due.map((x, i) => el('li', { class: x.late ? 'late' : '', style: `animation-delay:${i * 30}ms` },
      el('span', { class: 'ddate', text: x.due.slice(5) }),
      el('div', {}, el('a', { href: '/job/' + x.id, text: x.filename }),
        el('div', { class: 'tiny muted', text: [x.assigned_to || 'pool', STATES[x.work] || x.work, x.priority !== 'normal' ? x.priority : ''].filter(Boolean).join(' · ') }))))
      : [el('li', { class: 'muted small', text: 'Nothing due in the next three days.' })]));
    // users
    const max = Math.max(1, ...d.users.map((u) => u.pages));
    $('aUsers').replaceChildren(...d.users.map((u, i) => el('div', { class: 'ucard' + (u.active ? '' : ' off'), style: `animation-delay:${i * 40}ms` },
      el('div', { class: 'uhead' }, el('span', { class: 'avatar', text: (u.name || u.username)[0].toUpperCase() }),
        el('div', {}, el('b', { text: u.name || u.username }), el('div', { class: 'tiny muted', text: `${u.username} · ${ROLE[u.role] || u.role}${u.active ? '' : ' · switched off'}` }))),
      el('div', { class: 'ustats' },
        el('span', {}, el('b', { text: String(u.files) }), ' files'), el('span', {}, el('b', { text: String(u.pages) }), ' pages'),
        el('span', {}, el('b', { text: String(u.open) }), ' open'), el('span', {}, el('b', { text: String(u.done) }), ' approved')),
      el('div', { class: 'ht', title: `${u.pages} pages` }, el('i', { class: 'acc', style: `width:${100 * u.pages / max}%` })),
      el('div', { class: 'wchips' }, ...Object.entries(u.by_work).map(([k, v]) => el('span', { class: 'wchip w-' + (k || 'none'), text: `${STATES[k] || k} ${v}` }))),
      el('div', { class: 'tiny muted', text: (u.errors_left ? `${u.errors_left} checker error(s) left · ` : '') + (u.last ? 'last activity ' + fmt(u.last) : 'no activity yet') }),
      el('div', { class: 'btnrow' }, el('button', { class: 'sm', text: 'Their files', onclick: filt({ fUser: u.username }) })))));
    // profiles / clients
    const learned = {};
    ((d.samples || {}).clients || []).forEach((c) => { learned[c.client] = c; });
    const pmax = Math.max(1, ...Object.values(d.profiles).map((p) => p.files));
    $('aProfiles').querySelector('tbody').replaceChildren(...Object.entries(d.profiles).sort((a, b) => b[1].files - a[1].files).map(([name, p], i) => {
      const L = learned[name];
      return el('tr', { class: 'frow link', style: `animation-delay:${i * 30}ms`, title: 'Show the files of this profile', onclick: filt({ fProfile: name }) },
        el('td', {}, el('span', { class: 'pchip', text: name }), el('div', { class: 'ht thin' }, el('i', { class: 'acc', style: `width:${100 * p.files / pmax}%` }))),
        el('td', { text: String(p.files) }), el('td', { text: p.pages.toLocaleString() }), el('td', { text: String(p.open) }),
        el('td', { text: String(p.approved) }), el('td', { class: p.errors_left ? 'late' : '', text: String(p.errors_left) }),
        el('td', {}, L ? el('span', { class: 'chip ok', text: `${L.ready || 0} file(s)` }) : el('span', { class: 'tiny muted', text: '—' })));
    }));
    if (!Object.keys(d.profiles).length) $('aProfiles').querySelector('tbody').append(el('tr', {}, el('td', { colspan: 7, class: 'muted small', text: 'No files yet.' })));
    const common = learned[''];
    $('sampCount').textContent = (d.samples || {}).queued ? String(d.samples.queued) : '';
    // latest activity
    $('aRecent').replaceChildren(...((d.recent || []).length ? d.recent.map((e, i) => el('li', { class: 'lg-' + (e.action || '').split(' ')[0], style: `animation-delay:${i * 25}ms` },
      el('span', { class: 'avatar sm', text: (e.by || '?')[0].toUpperCase() }),
      el('div', {}, el('b', { text: e.by }), ` ${e.action} `, e.file ? el('a', { href: '/job/' + e.job, text: e.file }) : '',
        e.detail ? el('span', { class: 'muted', text: ' — ' + e.detail }) : '', el('div', { class: 'tiny muted', text: fmt(e.at) }))))
      : [el('li', { class: 'muted', text: 'Nothing yet.' })]));
    const q = d.queue || {};
    const run = (q.running || []).map((r) => el('div', { class: 'qrow' }, el('span', { class: 'spinner' }), el('span', { text: `${r.filename} — ${r.message || r.kind}` })));
    $('aQueue').replaceChildren(...(run.length ? run : [el('span', { text: 'Nothing is being analysed or built.' })]),
      q.waiting ? el('div', { class: 'tiny muted', text: `${q.waiting} waiting` }) : '');
    const st = d.stages || {};
    $('aQuality').replaceChildren(
      el('div', {}, `To review: `, el('b', { text: String(st.review || 0) }), ` · built: `, el('b', { text: String(st.done || 0) }), ` · failed: `, el('b', { class: st.error ? 'bad' : '', text: String(st.error || 0) })),
      el('div', {}, `Checker errors: `, el('b', { text: String(fx.before || 0) }), ' before → ', el('b', { text: String(fx.after || 0) }), ' after build'),
      el('div', { class: 'tiny muted' }, `Sample files: ${Object.values(learned).reduce((a, c) => a + (c.ready || 0), 0)} learned for ${Object.keys(learned).filter(Boolean).length} client(s)` + (common ? `, ${common.ready || 0} common` : '')));
  }
  let ovTimer = null;
  function ovLoop() {
    clearInterval(ovTimer);
    ovTimer = setInterval(() => { if ($('ovAuto').checked && !$('pane-overview').classList.contains('hidden') && !document.hidden) overview(); }, 20000);
  }
  $('ovRefresh').addEventListener('click', overview);
  ovLoop();

  // ------------------------------------------------------------ files
  // V4.30: more filters (a panel of its own), the filters in use as chips, a Reset, views to keep, the working time of
  // every file, and a list that draws 200 rows at a time (a long list used to be drawn whole at every key typed)
  const PAGE = 200;
  let shownLimit = PAGE;
  const dur = (s) => s < 60 ? `${Math.round(s || 0)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${String(Math.round(s % 3600 / 60)).padStart(2, '0')} min`;
  const BASIC = ['fSearch', 'fUser', 'fWork', 'fStage', 'fFolder', 'fProfile', 'fPrio', 'fSort'];
  const ADV = ['fUpFrom', 'fUpTo', 'fDueFrom', 'fDueTo', 'fOverdue', 'fPgMin', 'fPgMax', 'fErrMin', 'fHasErr', 'fWorkMin', 'fQcMin',
    'fWorkedBy', 'fNoTime', 'fChecker', 'fLocked', 'fComments', 'fRework', 'fBuilt'];
  const STAGE_NAME = { active: 'working now', review: 'to review', done: 'built', error: 'failed', question: 'tags found' };
  const CHIPS = {
    fSearch: (v) => `Search “${v}”`, fUser: (v) => v === '-' ? 'Not given (pool)' : `Given to ${v}`, fWork: (v) => `Work: ${STATES[v] || v}`,
    fStage: (v) => `Tool: ${STAGE_NAME[v] || v}`, fFolder: (v) => `Folder ${v}`, fProfile: (v) => `Profile ${v}`, fPrio: (v) => `Priority ${v}`,
    fUpFrom: (v) => `Uploaded from ${v}`, fUpTo: (v) => `Uploaded until ${v}`, fDueFrom: (v) => `Due from ${v}`, fDueTo: (v) => `Due until ${v}`,
    fOverdue: () => 'Overdue', fPgMin: (v) => `≥ ${v} pages`, fPgMax: (v) => `≤ ${v} pages`, fErrMin: (v) => `≥ ${v} errors left`,
    fHasErr: () => 'Errors left', fWorkMin: (v) => `Working ≥ ${v} min`, fQcMin: (v) => `QC ≥ ${v} min`, fWorkedBy: (v) => `Worked on by ${v}`,
    fNoTime: () => 'Nobody worked on it', fChecker: (v) => `QA / Final QA ${v}`, fLocked: () => 'Being edited now',
    fComments: () => 'Open comments', fRework: () => 'In rework', fBuilt: (v) => ({ yes: 'Built', no: 'Not built', stale: 'Changed since the build' }[v] || v),
  };
  const isCheck = (id) => $(id).type === 'checkbox';
  const readFilters = () => Object.fromEntries([...BASIC, ...ADV].map((id) => [id, isCheck(id) ? $(id).checked : $(id).value]));
  function writeFilters(o) {
    [...BASIC, ...ADV].forEach((id) => {
      const v = o ? o[id] : undefined;
      if (isCheck(id)) $(id).checked = !!v; else $(id).value = v === undefined || v === null ? (id === 'fSort' ? 'new' : '') : v;
    });
  }
  function clearFilters() { writeFilters(null); }
  const activeFilters = () => { const o = readFilters(); return Object.keys(CHIPS).filter((id) => (isCheck(id) ? o[id] : o[id] !== '' && o[id] != null)).map((id) => ({ id, text: CHIPS[id](o[id]) })); };
  const lsGet = (k, d) => { try { const v = JSON.parse(localStorage.getItem(k)); return v === null || v === undefined ? d : v; } catch (e) { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* no storage: the filters are not kept */ } };
  let restored = false;
  async function loadFiles() {
    files = await UA.get('/api/admin/files');
    const folders = [...new Set(files.map((f) => f.batch_name || '').filter(Boolean))].sort();
    const fs = $('fFolder');
    const cur = fs.value;
    fs.replaceChildren(new Option('Every folder', ''), ...folders.map((f) => new Option(f, f)));
    fs.value = folders.includes(cur) ? cur : '';
    if (!restored) {                                  // the filters the admin had the last time
      restored = true;
      const saved = lsGet('ua-admin-filters', null);
      if (saved && !activeFilters().length) { writeFilters(saved); if (activeFilters().length) UA.toast('Your filters from last time are on — ↺ Reset filters clears them', 3600); }
      fillViews();
    }
    for (const id of [...sel]) if (!files.some((f) => f.id === id)) sel.delete(id);
    renderFiles();
    if (files.some((f) => ['queued', 'processing', 'building'].includes(f.stage))) {
      clearTimeout(loadFiles.t);
      loadFiles.t = setTimeout(() => { if (!$('pane-files').classList.contains('hidden')) loadFiles(); }, 2500);
    }
  }
  function matcher() {
    const o = readFilters();
    const q = o.fSearch.toLowerCase().trim();
    const today = new Date().toISOString().slice(0, 10);
    const num = (v) => (v === '' || v === null || v === undefined ? null : Number(v));
    const wmin = num(o.fWorkMin), qmin = num(o.fQcMin), pmin = num(o.fPgMin), pmax = num(o.fPgMax), emin = num(o.fErrMin);
    const wb = (o.fWorkedBy || '').toLowerCase(), ck = (o.fChecker || '').toLowerCase();
    const day = (iso) => (iso || '').slice(0, 10);
    return (f) => {
      if (q && !`${f.filename} ${f.batch_name} ${f.rel_path} ${f.title} ${f.assigned_to} ${f.owner} ${f.profile} ${f.qa_user} ${f.fqa_user}`.toLowerCase().includes(q)) return false;
      if (o.fUser === '-' && f.assigned_to) return false;
      if (o.fUser && o.fUser !== '-' && (f.assigned_to || '').toLowerCase() !== o.fUser.toLowerCase()) return false;
      if (o.fWork && f.work !== o.fWork) return false;
      if (o.fStage === 'active' && !['queued', 'processing', 'building'].includes(f.stage)) return false;
      if (o.fStage && o.fStage !== 'active' && f.stage !== o.fStage) return false;
      if (o.fFolder && f.batch_name !== o.fFolder) return false;
      if (o.fPrio && f.priority !== o.fPrio) return false;
      if (o.fProfile && (f.profile || 'Default') !== o.fProfile) return false;
      if (o.fUpFrom && day(f.created) < o.fUpFrom) return false;
      if (o.fUpTo && day(f.created) > o.fUpTo) return false;
      if (o.fDueFrom && (!f.due || f.due < o.fDueFrom)) return false;
      if (o.fDueTo && (!f.due || f.due > o.fDueTo)) return false;
      if (o.fOverdue && !(f.due && f.due < today && !['approved', 'delivered'].includes(f.work))) return false;
      if (pmin !== null && (f.pages || 0) < pmin) return false;
      if (pmax !== null && (f.pages || 0) > pmax) return false;
      const err = f.errors_after != null ? f.errors_after : (f.errors_before || 0);
      if (emin !== null && err < emin) return false;
      if (o.fHasErr && !(err > 0)) return false;
      if (wmin !== null && (f.work_s || 0) < wmin * 60) return false;
      if (qmin !== null && (f.qc_s || 0) < qmin * 60) return false;
      if (wb && !(f.time_by || []).some((x) => x.user.toLowerCase() === wb)) return false;
      if (o.fNoTime && ((f.work_s || 0) + (f.qc_s || 0)) > 0) return false;
      if (ck && (f.qa_user || '').toLowerCase() !== ck && (f.fqa_user || '').toLowerCase() !== ck) return false;
      if (o.fLocked && !f.locked_by) return false;
      if (o.fComments && !(f.comments_open > 0)) return false;
      if (o.fRework && !(f.rework > 0)) return false;
      if (o.fBuilt === 'yes' && !f.built) return false;
      if (o.fBuilt === 'no' && f.built) return false;
      if (o.fBuilt === 'stale' && !(f.built && f.dirty)) return false;
      return true;
    };
  }
  function shownFiles() {
    let out = files.filter(matcher());
    const by = $('fSort').value;
    const tot = (f) => (f.work_s || 0) + (f.qc_s || 0);
    const cmp = {
      new: (a, b) => (b.created || '').localeCompare(a.created || ''),
      old: (a, b) => (a.created || '').localeCompare(b.created || ''),
      name: (a, b) => (a.filename || '').localeCompare(b.filename || ''),
      pages: (a, b) => (b.pages || 0) - (a.pages || 0),
      due: (a, b) => (a.due || '9999').localeCompare(b.due || '9999'),
      prio: (a, b) => (PRIO[a.priority] ?? 2) - (PRIO[b.priority] ?? 2),
      user: (a, b) => (a.assigned_to || '~').localeCompare(b.assigned_to || '~'),
      edited: (a, b) => (b.edited || '').localeCompare(a.edited || ''),
      time: (a, b) => tot(b) - tot(a),
      timeasc: (a, b) => tot(a) - tot(b),
      errors: (a, b) => ((b.errors_after ?? b.errors_before) || 0) - ((a.errors_after ?? a.errors_before) || 0),
    }[by];
    if (cmp) out = [...out].sort(cmp);
    return out;
  }
  function stageChip(f) {
    if (['queued', 'processing', 'building'].includes(f.stage)) {
      return el('div', {}, el('div', { class: 'tiny', text: f.message || f.stage }),
        el('div', { class: 'bar' }, el('i', { style: `width:${Math.round((f.progress || 0) * 100)}%` })));
    }
    const name = { done: 'built', review: 'to review', error: 'failed', question: 'tags found' }[f.stage] || f.stage;
    return el('span', { class: 'chip ' + (f.stage === 'done' ? 'ok' : f.stage === 'error' ? 'err' : ''), title: f.error || '', text: name });
  }
  function timeCell(f) {
    const w = f.work_s || 0, q = f.qc_s || 0;
    if (!w && !q) return el('td', { class: 'timecell muted', text: '—' });
    const who = (f.time_by || []).map((x) => `${x.user}: ${dur(x.work_s)} working${x.qc_s ? ' · ' + dur(x.qc_s) + ' QC' : ''}`).join('\n');
    return el('td', { class: 'timecell', title: (who ? who + '\n' : '') + 'Only the time people were active — breaks are left out' },
      el('b', { text: dur(w + q) }),
      el('div', { class: 'tiny muted', text: [w ? `working ${dur(w)}` : '', q ? `QC ${dur(q)}` : ''].filter(Boolean).join(' · ') }));
  }
  function renderChips() {
    const act = activeFilters();
    const box = $('fChips');
    box.replaceChildren(...act.map((c) => el('span', { class: 'fchip' }, c.text,
      el('button', { type: 'button', 'aria-label': 'Remove: ' + c.text, title: 'Remove this filter', text: '✕', onclick: () => { if (isCheck(c.id)) $(c.id).checked = false; else $(c.id).value = ''; shownLimit = PAGE; renderFiles(); } }))));
    const advN = act.filter((c) => ADV.includes(c.id)).length;
    $('fAdvN').textContent = String(advN);
    $('fAdvN').classList.toggle('hidden', !advN);
    const moved = $('fSort').value !== 'new';
    $('fReset').disabled = !act.length && !moved;
    $('filterBar').classList.toggle('filtered', act.length > 0);
    return act;
  }
  function renderFiles() {
    const list = shownFiles();
    renderChips();
    lsSet('ua-admin-filters', readFilters());
    const tb = $('fTable').querySelector('tbody');
    const frag = document.createDocumentFragment();
    const today = new Date().toISOString().slice(0, 10);
    list.slice(0, shownLimit).forEach((f, i) => {
      const ck = el('input', { type: 'checkbox', 'aria-label': 'select ' + f.filename });
      ck.checked = sel.has(f.id);
      ck.addEventListener('change', () => { ck.checked ? sel.add(f.id) : sel.delete(f.id); bulkState(); tr.classList.toggle('picked', ck.checked); });
      const errs = f.errors_after != null ? `${f.errors_before ?? ''} → ${f.errors_after}` : (f.errors_before ?? '');
      const tr = el('tr', { class: 'frow' + (sel.has(f.id) ? ' picked' : '') + (openRow === f.id ? ' open' : ''), style: `animation-delay:${Math.min(i, 20) * 12}ms` },
        el('td', { class: 'ck' }, ck),
        el('td', {}, el('a', { href: '/job/' + f.id + (f.locked_by ? '?view=1' : ''), text: f.filename }),
          f.locked_by ? el('button', { class: 'lockbadge', type: 'button', title: `${f.locked_by} has this file open${f.lock_idle_s > 60 ? ' (idle ' + Math.round(f.lock_idle_s / 60) + ' min)' : ''} — click to unlock it`,
            text: '🔒 ' + f.locked_by, onclick: async (e) => {
              e.stopPropagation();
              if (!confirm(`${f.locked_by} has this file open. Unlock it? They can then only look until they take it again.`)) return;
              const r = await post('/api/admin/files/unlock', { ids: [f.id] });
              if (!r.error) { UA.toast('Unlocked'); loadFiles(); }
            } }) : null,
          el('div', { class: 'tiny muted', text: [f.batch_name ? '📁 ' + (f.rel_path || f.batch_name) : '', fmt(f.created)].filter(Boolean).join(' · ') })),
        el('td', {}, f.assigned_to ? el('span', { class: 'who' }, el('span', { class: 'avatar sm', text: f.assigned_to[0].toUpperCase() }), f.assigned_to)
          : el('span', { class: 'muted small', text: 'pool' })),
        el('td', {}, window.WFUI ? window.WFUI.strip(f.wstage, f.rework, true) : null,
          el('span', { class: 'wchip w-' + (f.work || 'none'), text: STATES[f.work] || f.work || '—' }),
          f.comments_open ? el('span', { class: 'cbadge', title: 'open review comments', text: '💬 ' + f.comments_open }) : null,
          (f.qa_user || f.fqa_user) ? el('div', { class: 'tiny muted', text: [f.qa_user ? 'QA ' + f.qa_user : '', f.fqa_user ? 'FQA ' + f.fqa_user : ''].filter(Boolean).join(' · ') }) : null),
        el('td', {}, el('button', { class: 'pchip pbtn', title: 'Change the profile of this file', text: f.profile || 'Default',
          onclick: () => { sel.clear(); sel.add(f.id); renderFiles(); askProfile(); } })),
        el('td', {}, el('span', { class: 'prio p-' + f.priority, text: f.priority })),
        el('td', { class: f.due && f.due < today && f.work !== 'approved' ? 'late' : '', text: f.due || '' }),
        el('td', { text: String(f.pages || '') }),
        timeCell(f),
        el('td', {}, stageChip(f)),
        el('td', { text: String(errs) }),
        el('td', { class: 'small', text: f.owner || '—' }),
        el('td', { class: 'right' }, el('button', { class: 'sm xbtn', 'aria-expanded': openRow === f.id ? 'true' : 'false', title: 'Details', text: '▸',
          onclick: () => { openRow = openRow === f.id ? null : f.id; renderFiles(); } })));
      frag.append(tr);
      if (openRow === f.id) frag.append(detailRow(f));
    });
    tb.replaceChildren(frag);
    $('fEmpty').classList.toggle('hidden', list.length > 0);
    $('fTable').classList.toggle('hidden', list.length === 0);
    const left = list.length - shownLimit;
    $('fMoreRow').classList.toggle('hidden', left <= 0);
    $('fMore').textContent = `Show ${Math.min(PAGE, left)} more (${left} left)`;
    $('fCount').textContent = `${list.length} of ${files.length} file(s)`;
    $('ckAll').checked = list.length > 0 && list.every((f) => sel.has(f.id));
    bulkState(list);
  }
  function detailRow(f) {
    const fact = (k, v) => (v === undefined || v === null || v === '') ? null : el('div', { class: 'fact' }, el('span', { text: k }), el('b', { text: String(v) }));
    const facts = el('div', { class: 'facts' },
      fact('Profile', f.profile), fact('Mode', f.mode === 'import' ? 'existing tags' : f.mode ? 'auto-tagged' : ''), fact('Tags', f.nodes || ''),
      fact('Language', f.lang), fact('Pages', f.pages), fact('Checker errors', f.errors_after != null ? `${f.errors_before} → ${f.errors_after}` : f.errors_before),
      fact('Changed by samples', f.samples_changed ? `${f.samples_changed} tag(s)` : ''), fact('Approved', fmt(f.approved_at)),
      fact('Working time', f.work_s ? dur(f.work_s) : ''), fact('QC time', f.qc_s ? dur(f.qc_s) : ''),
      f.sample_id ? el('div', { class: 'fact' }, el('span', { text: 'Sample' }), el('a', { href: '#', text: 'this file teaches the tool', onclick: (e) => { e.preventDefault(); showSample(f.sample_id); } })) : null,
      (f.profile_history || []).length ? fact('Profile changes', f.profile_history.map((h) => `${h.from} → ${h.to}`).join(', ')) : null);
    const acts = el('div', { class: 'btnrow' },
      el('button', { class: 'sm', text: 'Change profile…', onclick: () => { sel.clear(); sel.add(f.id); renderFiles(); askProfile(); } }),
      el('button', { class: 'sm', text: 'Add to samples', onclick: async () => {
        const r = await post('/api/admin/samples/from_jobs', { ids: [f.id] });
        if (!r.error) UA.toast(r.added ? 'Added to the samples — learning now' : 'Not analysed yet');
      } }),
      el('button', { class: 'sm', text: 'Build', onclick: async () => after(await post('/api/admin/files/build', { ids: [f.id] }), 'Build started') }));
    const tmBox = el('div', { class: 'tmbox' });
    if (f.time_by && f.time_by.length) {
      tmBox.append(el('h4', { text: 'Who spent the time' }),
        el('table', { class: 'list mini' }, el('thead', {}, el('tr', {}, el('th', { text: 'Person' }), el('th', { text: 'Working' }), el('th', { text: 'QC' }), el('th', { text: 'Last' }))),
          el('tbody', {}, ...f.time_by.map((x) => el('tr', {}, el('td', { text: x.user }), el('td', { text: dur(x.work_s) }), el('td', { text: x.qc_s ? dur(x.qc_s) : '—' }), el('td', { class: 'tiny muted', text: fmt(x.last) }))))));
    }
    const box = el('div', { class: 'fdetail' }, facts, acts, tmBox,
      el('div', {}, el('h4', { text: 'Notes' }),
        f.notes && f.notes.length ? el('ul', { class: 'notes' }, ...f.notes.map((n) => el('li', {}, el('b', { text: n.by }), ` ${fmt(n.at)}${n.action && n.action !== 'note' ? ' · ' + n.action : ''} — `, n.text)))
          : el('p', { class: 'small muted', text: 'No notes.' })),
      el('div', {}, el('h4', { text: 'History' }), el('ol', { class: 'timeline small', id: 'hist-' + f.id }, el('li', { class: 'muted', text: 'Loading…' }))),
      el('div', { class: 'btnrow' },
        el('a', { class: 'btn sm primary', href: '/job/' + f.id, text: 'Open in the editor' }),
        el('a', { class: 'btn sm', href: '/job/' + f.id + '#links', text: 'Its links' }),
        f.built ? el('a', { class: 'btn sm', href: `/job/${f.id}/download/pdf`, text: 'Accessible PDF' }) : null,
        el('a', { class: 'btn sm', href: `/job/${f.id}/download/original`, text: 'Original' })));
    UA.get('/api/admin/log?job=' + encodeURIComponent(f.id) + '&limit=30').then((log) => {
      const ol = document.getElementById('hist-' + f.id);
      if (!ol) return;
      ol.replaceChildren(...(log.length ? log.map((e) => el('li', {}, el('b', { text: e.by }), ` ${e.action}${e.detail ? ' — ' + e.detail : ''}`, el('span', { class: 'tiny muted', text: ' ' + fmt(e.at) })))
        : [el('li', { class: 'muted', text: 'Nothing recorded yet.' })]));
    });
    const td = el('td', { colspan: 13 }, box);
    return el('tr', { class: 'detailrow' }, td);
  }
  function bulkState(list) {
    const n = sel.size;
    $('selCount').textContent = n ? `${n} selected` : 'Select files to act on them';
    $('bulkBar').classList.toggle('active', n > 0);
    $('bulkBar').querySelectorAll('button, select, input').forEach((b) => { b.disabled = !n; });
    // all that match are selected, and not all of them are drawn
    const ban = $('fSelBanner');
    const all = list || shownFiles();
    const hidden = all.length - shownLimit;
    const allSel = all.length > 0 && all.every((f) => sel.has(f.id));
    ban.classList.toggle('hidden', !(allSel && hidden > 0));
    if (allSel && hidden > 0) ban.textContent = `All ${all.length} files that match are selected — ${hidden} of them are not drawn yet.`;
  }
  // the filters
  let filterT = null;
  const onFilter = (id) => () => {
    shownLimit = PAGE;
    if (id === 'fSearch') { clearTimeout(filterT); filterT = setTimeout(renderFiles, 130); } else renderFiles();
  };
  [...BASIC, ...ADV].forEach((id) => $(id).addEventListener(id === 'fSearch' || $(id).type === 'number' || $(id).type === 'date' ? 'input' : 'change', onFilter(id)));
  function resetFilters() { clearFilters(); shownLimit = PAGE; $('fViews').value = ''; $('fDelView').hidden = true; renderFiles(); UA.toast('Filters cleared', 1600); }
  $('fReset').addEventListener('click', resetFilters);
  $('fEmptyReset').addEventListener('click', resetFilters);
  $('fMore').addEventListener('click', () => { shownLimit += PAGE; renderFiles(); });
  $('fAdv').addEventListener('click', () => {
    const open = $('fAdvPanel').classList.toggle('hidden') === false;
    $('fAdv').setAttribute('aria-expanded', String(open));
  });
  $('fSearch').addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('fSearch').value) { $('fSearch').value = ''; onFilter('fSearch')(); } });
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !$('pane-files').classList.contains('hidden') && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || ''))) {
      e.preventDefault();
      $('fSearch').focus();
    }
  });
  // views (a set of filters kept under a name)
  function fillViews() {
    const vs = lsGet('ua-admin-views', []);
    $('fViews').replaceChildren(new Option('Saved views…', ''), ...vs.map((v, i) => new Option(v.name, String(i))));
  }
  $('fViews').addEventListener('change', () => {
    const vs = lsGet('ua-admin-views', []);
    const v = vs[Number($('fViews').value)];
    $('fDelView').hidden = !v;
    if (v) { writeFilters(v.state); shownLimit = PAGE; renderFiles(); }
  });
  $('fSave').addEventListener('click', () => {
    const name = (prompt('Name this view (the filters and the sort as they are now):', '') || '').trim().slice(0, 40);
    if (!name) return;
    const vs = lsGet('ua-admin-views', []).filter((v) => v.name !== name);
    vs.push({ name, state: readFilters() });
    lsSet('ua-admin-views', vs.slice(-30));
    fillViews();
    $('fViews').value = String(vs.length - 1);
    $('fDelView').hidden = false;
    UA.toast(`View “${name}” kept`);
  });
  $('fDelView').addEventListener('click', () => {
    const vs = lsGet('ua-admin-views', []);
    const i = Number($('fViews').value);
    if (!vs[i] || !confirm(`Delete the view “${vs[i].name}”?`)) return;
    vs.splice(i, 1);
    lsSet('ua-admin-views', vs);
    fillViews();
    $('fDelView').hidden = true;
  });
  $('fExport').addEventListener('click', () => {
    const rows = shownFiles();
    const q = (v) => '"' + String(v ?? '').replace(/"/g, '""') + '"';
    const head = ['file', 'folder', 'uploaded by', 'given to', 'work', 'step', 'profile', 'priority', 'due', 'pages', 'status', 'errors left', 'working time (min)', 'QC time (min)', 'time by'];
    const lines = [head.map(q).join(',')].concat(rows.map((f) => [f.filename, f.batch_name, f.owner, f.assigned_to, STATES[f.work] || f.work, f.wstage_label, f.profile, f.priority, f.due, f.pages, f.stage,
      f.errors_after != null ? f.errors_after : f.errors_before, Math.round((f.work_s || 0) / 6) / 10, Math.round((f.qc_s || 0) / 6) / 10,
      (f.time_by || []).map((x) => `${x.user} ${Math.round(x.work_s / 60)}+${Math.round(x.qc_s / 60)} min`).join('; ')].map(q).join(',')));
    const a = el('a', { href: URL.createObjectURL(new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv' })), download: `files_${new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')}.csv` });
    document.body.append(a);
    a.click();
    a.remove();
    UA.toast(`${rows.length} file(s) exported`);
  });
  $('ckAll').addEventListener('change', () => {
    const list = shownFiles();
    if ($('ckAll').checked) list.forEach((f) => sel.add(f.id)); else list.forEach((f) => sel.delete(f.id));
    renderFiles();
  });
  const ids = () => [...sel];
  async function after(r, msg) {
    if (r && !r.error) { UA.toast(typeof msg === 'function' ? msg(r) : msg, (r.skipped || []).length ? 7000 : undefined); await loadFiles(); overview(); }
  }
  // V4.29: a file somebody works in is left alone by the steps for many files — say which
  const skippedNote = (r) => (r.skipped || []).length ? ` · left alone: ${r.skipped.slice(0, 3).join(', ')}${r.skipped.length > 3 ? ' …' : ''}` : '';
  $('bGive').addEventListener('click', async () => after(await post('/api/admin/files/assign', { ids: ids(), to: $('bTo').value }), `Given to ${$('bTo').value}`));
  $('bSpread').addEventListener('click', () => $('spreadDlg').showModal());
  $('spreadGo').addEventListener('click', async () => {
    const us = [...$('spreadUsers').querySelectorAll('input:checked')].map((c) => c.value);
    if (!us.length) { UA.toast('Tick the users'); return; }
    $('spreadDlg').close();
    after(await post('/api/admin/files/assign', { ids: ids(), users: us, mode: $('spreadMode').value }),
      (r) => `Shared out: ${Object.entries(Object.values(r.plan || {}).reduce((a, u) => { a[u] = (a[u] || 0) + 1; return a; }, {})).map(([u, n]) => `${u} ${n}`).join(', ')}`);
  });
  $('bUnassign').addEventListener('click', async () => after(await post('/api/admin/files/unassign', { ids: ids() }), 'Back in the pool'));
  $('bRevert').addEventListener('click', async () => after(await post('/api/admin/files/revert', { ids: ids() }),
    (r) => `${r.reverted} put back` + (r.skipped && r.skipped.length ? ` · ${r.skipped.length} had nothing to put back` : '')));
  $('bApprove').addEventListener('click', async () => {
    if (!confirm(`Mark ${sel.size} file(s) ready for delivery, skipping QA and Final QA?`)) return;
    after(await post('/api/admin/files/update', { ids: ids(), work: 'approved' }), 'Ready for delivery');
  });
  $('bStep').addEventListener('change', async () => {
    const v = $('bStep').value;
    $('bStep').value = '';
    if (!v) return;
    const to = v === '-' ? '' : v;
    after(await post('/api/admin/files/update', { ids: ids(), work: to }), `Moved to ${STATES[to] || 'not given'}`);
  });
  let noteMode = 'note';
  $('bReturn').addEventListener('click', () => { noteMode = 'return'; $('noteTitle').textContent = 'Rework — what needs fixing?'; $('noteText').value = ''; $('noteDlg').showModal(); });
  $('bNote').addEventListener('click', () => { noteMode = 'note'; $('noteTitle').textContent = 'A note on the selected files'; $('noteText').value = ''; $('noteDlg').showModal(); });
  $('noteGo').addEventListener('click', async () => {
    const t = $('noteText').value.trim();
    if (!t) { UA.toast('Write the note'); return; }
    $('noteDlg').close();
    const body = { ids: ids(), note: t };
    if (noteMode === 'return') body.work = 'rework';
    after(await post('/api/admin/files/update', body), noteMode === 'return' ? 'Sent to rework with the note' : 'Note saved');
  });
  $('bPrio').addEventListener('change', async () => {
    if (!$('bPrio').value) return;
    after(await post('/api/admin/files/update', { ids: ids(), priority: $('bPrio').value }), 'Priority set');
    $('bPrio').value = '';
  });
  $('bDue').addEventListener('change', async () => after(await post('/api/admin/files/update', { ids: ids(), due: $('bDue').value }), 'Due date set'));
  function askProfile() {
    $('profN').textContent = String(sel.size);
    $('profTo').textContent = $('bProfile').value;
    const one = files.find((f) => sel.has(f.id));
    if (sel.size === 1 && one) $('bProfile').value = one.profile || 'Default';
    $('profTo').textContent = $('bProfile').value;
    $('profDlg').showModal();
  }
  $('bProfile').addEventListener('change', () => { $('profTo').textContent = $('bProfile').value; });
  $('bProfileGo').addEventListener('click', askProfile);
  $('profGo').addEventListener('click', async () => {
    $('profDlg').close();
    after(await post('/api/admin/files/profile', { ids: ids(), profile: $('bProfile').value, reanalyse: $('profRean').checked }),
      (r) => `${r.changed} file(s) now ${$('bProfile').value}` + (r.busy ? ` · ${r.busy} busy` : '') + ($('profRean').checked ? ' — analysing again' : '') + skippedNote(r));
  });
  $('bLearn').addEventListener('click', async () => {
    const notOk = files.filter((f) => sel.has(f.id) && f.work !== 'approved').length;
    if (notOk && !confirm(`${notOk} of the ${sel.size} file(s) are not approved yet. Their tags teach the tool as they are now — add them anyway?`)) return;
    const r = await post('/api/admin/samples/from_jobs', { ids: ids() });
    if (!r.error) UA.toast(`${r.added} file(s) added to the samples — learning now` + (r.skipped ? ` · ${r.skipped} not analysed yet` : ''));
  });
  $('bBuild').addEventListener('click', async () => after(await post('/api/admin/files/build', { ids: ids() }), (r) => `${r.started} build(s) started` + skippedNote(r)));
  $('bRean').addEventListener('click', async () => {
    if (!confirm(`Analyse ${sel.size} file(s) again from scratch? Their tags and hand edits are replaced.`)) return;
    after(await post('/api/admin/files/reanalyze', { ids: ids() }), (r) => `${r.started} started` + (r.busy ? `, ${r.busy} busy` : '') + skippedNote(r));
  });
  $('bZip').addEventListener('click', () => {
    const built = files.filter((f) => sel.has(f.id) && f.built).map((f) => f.id);
    if (!built.length) { UA.toast('None of the selected files is built yet'); return; }
    location.href = '/api/admin/files/download.zip?reports=1&ids=' + built.join(',');
  });
  $('bDelete').addEventListener('click', async () => {
    if (!confirm(`Delete ${sel.size} file(s) for good? This cannot be undone.`)) return;
    after(await post('/api/admin/files/delete', { ids: ids() }), (r) => `${r.count} deleted` + skippedNote(r));
    sel.clear();
  });

  // ------------------------------------------------------------ workflow (V4.23)
  const COLS = [['production', 'Production'], ['qa', 'QA'], ['final_qa', 'Final QA'], ['delivery', 'Delivery'], ['delivered', 'Delivered'], ['hold', 'On hold']];
  const DROP_TO = { qa: 'qa', final_qa: 'final_qa', delivery: 'approved', delivered: 'delivered', hold: 'on_hold' };
  async function loadWorkflow() {
    const d = await UA.get('/api/admin/workflow');
    if (!d || d.error) return;
    const st = d.stages || {};
    const tile = (big, label, sub, cls) => el('div', { class: 'tile ' + (cls || '') }, el('div', { class: 'tbig', 'data-n': big, text: String(big) }),
      el('div', { class: 'tlabel', text: label }), sub ? el('div', { class: 'tsub', text: sub }) : null);
    const inQa = (st.qa || 0) + (st.final_qa || 0);
    $('wfCount').textContent = inQa ? String(inQa) : '';
    $('wfTiles').replaceChildren(
      tile(st.production || 0, 'in production', `${(d.states.rework || 0) + (d.states.returned || 0)} in rework`, 't-open'),
      tile(st.qa || 0, 'in QA', `${d.states.qa_progress || 0} being checked · ${(d.states.qa || 0) + (d.states.submitted || 0)} waiting`, 't-check'),
      tile(st.final_qa || 0, 'in Final QA', `${d.states.fqa_progress || 0} being checked · ${d.states.final_qa || 0} waiting`, 't-check'),
      tile(st.delivery || 0, 'ready for delivery', `${st.delivered || 0} delivered`, 't-done'),
      tile(d.rework_files || 0, 'files with a rework', `${(d.roles || {}).qa || 0} QA · ${(d.roles || {}).final_qa || 0} Final QA user(s)`, d.rework_files ? 't-due' : 't-files'),
      tile(d.first_pass_rate == null ? 0 : d.first_pass_rate, '% first pass', d.done ? `${d.first_pass} of ${d.done} done without a rework` : 'nothing done yet', 't-fix'));
    $('wfTiles').querySelectorAll('.tbig').forEach(countUp);
    // the board
    const today = new Date().toISOString().slice(0, 10);
    $('wfBoard').replaceChildren(...COLS.filter(([k]) => k !== 'hold' || st.hold).map(([k, label]) => {
      const cards = (d.board[k] || []);
      const col = el('div', { class: 'kcol k-' + k, 'data-col': k },
        el('div', { class: 'khead' }, el('b', { text: label }), el('span', { class: 'n', text: String(st[k] || 0) })),
        el('div', { class: 'kcards' }, ...(cards.length ? cards.map((c, i) => {
          const card = el('a', { class: 'kcard' + (c.due && c.due < today && !['approved', 'delivered'].includes(c.work) ? ' late' : ''), href: `/job/${c.id}#review`,
            draggable: 'true', 'data-id': c.id, 'data-stage': c.wstage, style: `animation-delay:${Math.min(i, 20) * 18}ms` },
            el('div', { class: 'kname', text: c.filename }),
            el('div', { class: 'kmeta' }, el('span', { class: 'wchip w-' + (c.work || 'none'), text: STATES[c.work] || c.work || 'not given' }),
              c.rework ? el('span', { class: 'rwbadge', text: 'R' + c.rework }) : null,
              c.comments_open ? el('span', { class: 'cbadge', text: '💬 ' + c.comments_open }) : null,
              c.priority !== 'normal' ? el('span', { class: 'prio p-' + c.priority, text: c.priority }) : null),
            el('div', { class: 'tiny muted', text: [k === 'qa' ? (c.qa_user ? '🔎 ' + c.qa_user : 'nobody yet') : k === 'final_qa' ? (c.fqa_user ? '🔎 ' + c.fqa_user : 'nobody yet') : (c.assigned_to || 'pool'),
              c.pages ? c.pages + ' p' : '', c.due ? 'due ' + c.due : '', c.stage_since ? (window.WFUI ? window.WFUI.ago(c.stage_since) : '') : ''].filter(Boolean).join(' · ') }));
          card.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', c.id); card.classList.add('dragging'); });
          card.addEventListener('dragend', () => card.classList.remove('dragging'));
          return card;
        }) : [el('div', { class: 'kempty', text: 'nothing here' })])),
        (st[k] || 0) > cards.length ? el('div', { class: 'tiny muted', text: `+${st[k] - cards.length} more — Files tab` }) : null);
      col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('over'); });
      col.addEventListener('dragleave', () => col.classList.remove('over'));
      col.addEventListener('drop', async (e) => {
        e.preventDefault();
        col.classList.remove('over');
        const id = e.dataTransfer.getData('text/plain');
        const from = (document.querySelector(`.kcard[data-id="${id}"]`) || {}).dataset || {};
        if (!id || from.stage === k) return;
        const work = k === 'production' ? (['qa', 'final_qa', 'delivery'].includes(from.stage) ? 'rework' : 'assigned') : DROP_TO[k];
        let note = '';
        if (work === 'rework') { note = prompt('Rework — what needs fixing?', ''); if (note === null) return; }
        const r = await post('/api/admin/files/update', { ids: [id], work, note });
        if (!r.error) { UA.toast(`Moved to ${label}`); loadWorkflow(); }
      });
      return col;
    }));
    // people
    const ppl = d.people || [];
    $('wfPeople').querySelector('tbody').replaceChildren(...(ppl.length ? ppl.map((p) => el('tr', {},
      el('td', {}, el('b', { text: p.user })), el('td', { text: String(p.files) }),
      el('td', { class: p.rework_files ? 'late' : '', text: String(p.rework_files) }), el('td', { text: String(p.reworks) }),
      el('td', { text: String(p.comments) }), el('td', { text: String(p.delivered) }),
      el('td', { text: p.delivered ? Math.round(100 * Math.max(0, p.delivered - Math.min(p.delivered, p.rework_files)) / p.delivered) + '%' : '—' })))
      : [el('tr', {}, el('td', { colspan: 7, class: 'muted small', text: 'No file given yet.' }))]));
    const ck = d.checkers || [];
    $('wfCheckers').querySelector('tbody').replaceChildren(...(ck.length ? ck.map((c) => el('tr', {},
      el('td', {}, el('b', { text: c.user })), el('td', {}, el('span', { class: 'rolechip r-' + c.role, text: ROLE[c.role] || c.role })),
      el('td', { text: String(c.approved) }), el('td', { text: String(c.rework) })))
      : [el('tr', {}, el('td', { colspan: 4, class: 'muted small', text: 'No check finished yet.' }))]));
    const cats = Object.entries(d.categories || {});
    const cmax = Math.max(1, ...cats.map(([, n]) => n));
    $('wfCats').replaceChildren(...(cats.length ? cats.map(([c, n], i) => el('div', { class: 'hbar' }, el('span', { class: 'hk', text: c }),
      el('div', { class: 'ht' }, el('i', { class: 'warn', style: `width:${100 * n / cmax}%;animation-delay:${i * 40}ms` })), el('b', { text: String(n) })))
      : [el('p', { class: 'small muted', text: 'No review comment yet.' })]));
    // the rules
    const L = d.setting_labels || {};
    $('wfRules').replaceChildren(...(d.setting_order || Object.keys(L)).map((k) => {
      const cb = el('input', { type: 'checkbox', 'data-k': k });
      cb.checked = !!d.cfg[k];
      cb.addEventListener('change', async () => {
        const r = await post('/api/admin/workflow', { [k]: cb.checked });
        if (r.error) cb.checked = !cb.checked; else UA.toast(`${L[k][0]}: ${cb.checked ? 'on' : 'off'}`);
      });
      return el('label', { class: 'wfrule' }, cb, el('span', {}, el('b', { text: L[k][0] }), el('span', { class: 'tiny muted', text: L[k][1] })));
    }));
    $('wfIdle').value = d.cfg.work_idle_s || 120;
  }
  $('wfIdleSave').addEventListener('click', async () => {
    const r = await post('/api/admin/workflow', { work_idle_s: Number($('wfIdle').value) });
    if (!r.error) { $('wfIdle').value = r.cfg.work_idle_s; UA.toast(`A pause of more than ${r.cfg.work_idle_s} seconds is a break`); }
  });
  $('wfRefresh').addEventListener('click', loadWorkflow);

  // ------------------------------------------------------------ users
  async function loadUsers() {
    let ov, tm;
    [users, ov, tm] = await Promise.all([UA.get('/api/admin/users'), UA.get('/api/admin/overview'), UA.get('/api/admin/time?days=7').catch(() => null)]);
    const count = {};
    ov.users.forEach((u) => { count[u.username.toLowerCase()] = u.files; });
    const spent = {};
    ((tm && tm.users) || []).forEach((u) => { spent[u.user.toLowerCase()] = u; });
    const tb = $('uTable').querySelector('tbody');
    tb.replaceChildren(...users.map((u) => {
      const act = el('input', { type: 'checkbox', title: u.active ? 'Switch the account off' : 'Switch it on' });
      act.checked = !!u.active;
      act.addEventListener('change', async () => {
        const r = await post('/api/admin/users/' + encodeURIComponent(u.username), { active: act.checked });
        if (r.error) act.checked = !act.checked; else UA.toast(act.checked ? 'Switched on' : 'Switched off — they cannot sign in');
      });
      return el('tr', { class: 'frow' },
        el('td', { class: 'link', title: 'Everything about this user', onclick: () => userDetail(u.username) }, el('span', { class: 'who' }, el('span', { class: 'avatar sm', text: (u.name || u.username)[0].toUpperCase() }),
          el('span', {}, el('b', { text: u.username }), el('div', { class: 'tiny muted', text: [u.name, u.email, u.note].filter(Boolean).join(' · ') })))),
        el('td', {}, el('span', { class: 'rolechip r-' + (u.role === 'user' ? 'production' : u.role), text: ROLE[u.role] || u.role })),
        el('td', { text: String(count[u.username.toLowerCase()] || 0) }),
        (() => {
          const t = spent[u.username.toLowerCase()];
          return t ? el('td', { class: 'timecell', title: `${t.files} file(s), ${Object.keys(t.days).length} active day(s)` }, el('b', { text: dur(t.work_s + t.qc_s) }),
            el('div', { class: 'tiny muted', text: `working ${dur(t.work_s)}${t.qc_s ? ' · QC ' + dur(t.qc_s) : ''}` })) : el('td', { class: 'muted small', text: '—' });
        })(),
        el('td', {}, (() => {
          const n = Object.keys(u.rights || {}).length;
          return u.role === 'admin' ? el('span', { class: 'tiny muted', text: 'everything' })
            : el('button', { class: 'sm rightsbtn' + (n ? ' custom' : ''), type: 'button', title: 'What this person may do on top of the role', text: n ? `${n} custom` : 'role defaults', onclick: () => openRights(u) });
        })()),
        el('td', { class: 'small', text: fmt(u.last_login) || 'never' }),
        el('td', {}, act),
        el('td', { class: 'right' },
          el('button', { class: 'sm', text: 'Details', onclick: () => userDetail(u.username) }),
          el('button', { class: 'sm', text: 'Edit', onclick: () => editUser(u) }),
          el('button', { class: 'sm danger', text: 'Delete', onclick: () => delUser(u) })));
    }));
  }
  let editing = null;
  function editUser(u) {
    editing = u.username;
    $('udName').textContent = u.username;
    $('udFull').value = u.name || '';
    $('udMail').value = u.email || '';
    $('udRole').value = u.role === 'user' ? 'production' : u.role;
    $('udNote').value = u.note || '';
    $('udPw').value = '';
    $('udOut').textContent = '';
    $('userDlg').showModal();
  }
  $('udSave').addEventListener('click', async () => {
    const body = { name: $('udFull').value, email: $('udMail').value, role: $('udRole').value, note: $('udNote').value };
    if ($('udPw').value) body.password = $('udPw').value;
    const r = await post('/api/admin/users/' + encodeURIComponent(editing), body);
    if (r.error) { $('udOut').textContent = r.error; return; }
    $('userDlg').close();
    UA.toast('Saved' + (body.password ? ' — new password set' : ''));
    loadUsers();
  });
  async function delUser(u) {
    const others = users.filter((x) => x.username !== u.username && x.active).map((x) => x.username);
    const to = prompt(`Delete ${u.username}? Their files go to (type a user name, or leave empty for the pool):\n${others.join(', ')}`, '');
    if (to === null) return;
    const res = await fetch('/api/admin/users/' + encodeURIComponent(u.username), {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ give_to: to.trim() }) });
    const d = await res.json();
    if (d.error) { UA.toast(d.error, 4500); return; }
    UA.toast(`${u.username} deleted · ${d.moved} file(s) ${to.trim() ? 'to ' + to.trim() : 'back in the pool'}`);
    loadUsers();
  }
  $('uForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const r = await post('/api/admin/users', Object.fromEntries(f.entries()));
    if (r.error) { $('uOut').textContent = r.error; return; }
    $('uOut').textContent = `${r.user.username} can sign in now.`;
    e.target.reset();
    loadUsers();
  });

  // ------------------------------------------------------------ rights (V4.30)
  let rgCat = null, rgUser = null, rgState = {};
  async function openRights(u) {
    if (!rgCat) { const c = await UA.get('/api/admin/rights'); if (!c || c.error) return; rgCat = c.rights; }
    const all = await UA.get('/api/admin/users');
    rgUser = all.find((x) => x.username === u.username) || u;
    rgState = { ...(rgUser.rights || {}) };
    $('rgName').textContent = rgUser.username;
    $('rgRole').textContent = ROLE[rgUser.role] || rgUser.role;
    $('rgRole').className = 'rolechip r-' + (rgUser.role === 'user' ? 'production' : rgUser.role);
    $('rgOut').textContent = '';
    drawRights();
    $('rightsDlg').showModal();
  }
  function drawRights() {
    const role = rgUser.role === 'user' ? 'production' : rgUser.role;
    const admin = role === 'admin';
    const groups = {};
    rgCat.forEach((r) => { (groups[r.group] = groups[r.group] || []).push(r); });
    $('rgList').replaceChildren(...Object.entries(groups).map(([g, rs]) => el('fieldset', { class: 'rggroup' }, el('legend', { text: g }),
      ...rs.map((r) => {
        const mine = rgState[r.key];                                     // true / false / undefined (the role's default)
        const def = !!(r.default[role]);
        const eff = admin ? true : (mine === undefined ? def : mine);
        const opt = (val, text, title) => el('button', { type: 'button', role: 'radio', 'aria-checked': String(mine === val), class: 'rgopt' + (mine === val ? ' on' : '') + (val === true ? ' allow' : val === false ? ' deny' : ''),
          title, disabled: admin, text, onclick: () => { if (val === undefined) delete rgState[r.key]; else rgState[r.key] = val; drawRights(); } });
        return el('div', { class: 'rgrow' + (mine !== undefined ? ' set' : '') },
          el('div', { class: 'rgtext' }, el('b', { text: r.label }), el('span', { class: 'tiny muted', text: r.help })),
          el('div', { class: 'rgseg', role: 'radiogroup', 'aria-label': r.label },
            opt(undefined, `Role default (${def ? 'yes' : 'no'})`, 'Follow the role'), opt(true, 'Allow', 'This person may'), opt(false, 'Deny', 'This person may not')),
          el('span', { class: 'chip ' + (eff ? 'ok' : 'off'), text: eff ? 'may' : 'may not' }));
      }))));
    if (admin) $('rgOut').textContent = 'An admin can always do everything — rights narrow or widen the others only.';
  }
  $('rgSave').addEventListener('click', async () => {
    const body = {};
    rgCat.forEach((r) => { body[r.key] = rgState[r.key] === undefined ? null : rgState[r.key]; });
    const r = await post('/api/admin/users/' + encodeURIComponent(rgUser.username), { rights: body });
    if (r.error) { $('rgOut').textContent = r.error; return; }
    $('rightsDlg').close();
    UA.toast(`Rights of ${rgUser.username} saved`);
    loadUsers();
  });
  $('rgReset').addEventListener('click', () => { rgState = {}; drawRights(); });

  // ------------------------------------------------------------ working time (V4.30)
  let tmDays = 7, tmData = null;
  async function loadTime() {
    const user = $('tmUser').value;
    $('tmReset').disabled = !user && tmDays === 7;
    const d = await UA.get(`/api/admin/time?days=${tmDays}` + (user ? '&user=' + encodeURIComponent(user) : ''));
    if (!d || d.error) return;
    tmData = d;
    const total = d.work_s + d.qc_s;
    const nfiles = d.users.reduce((a, u) => a + u.files, 0);
    const active = d.users.reduce((a, u) => a + Object.keys(u.days).length, 0);
    const tile = (big, label, sub, cls) => el('div', { class: 'tile ' + cls }, el('div', { class: 'tbig', text: String(big) }), el('div', { class: 'tlabel', text: label }), sub ? el('div', { class: 'tsub', text: sub }) : null);
    $('tmSub').textContent = `Only the time people are active in a file — a pause of more than ${Math.round((d.idle_limit || 120) / 6) / 10} minutes is a break and is left out. From ${d.since}.`;
    $('tmTiles').replaceChildren(
      tile(dur(d.work_s), 'working', 'production and rework', 't-open'),
      tile(dur(d.qc_s), 'QC', 'QA and Final QA', 't-check'),
      tile(d.users.length, 'people', active ? `${dur(total / Math.max(1, active))} per person and active day` : 'nobody worked yet', 't-files'),
      tile(nfiles, 'files worked on', 'summed over the people', 't-done'));
    // day by day (every day of the range, also the empty ones)
    const days = [];
    for (let i = d.days - 1; i >= 0; i--) { const t = new Date(); t.setDate(t.getDate() - i); days.push(t.toISOString().slice(0, 10)); }
    const by = Object.fromEntries(d.per_day.map((x) => [x.day, x]));
    const max = Math.max(1, ...days.map((k) => (by[k] ? by[k].work_s + by[k].qc_s : 0)));
    $('tmDays').replaceChildren(...days.map((k, i) => {
      const x = by[k] || { work_s: 0, qc_s: 0 };
      return el('div', { class: 'sday', title: `${k}: working ${dur(x.work_s)}, QC ${dur(x.qc_s)}` },
        el('div', { class: 'sbars stack' },
          el('i', { class: 'up', style: `height:${100 * x.work_s / max}%;animation-delay:${i * 18}ms` }),
          el('i', { class: 'ap', style: `height:${100 * x.qc_s / max}%;animation-delay:${i * 18 + 50}ms` })),
        el('span', { text: d.days <= 14 ? day(k) : (i % 7 === 0 ? k.slice(5) : '') }));
    }));
    const umax = Math.max(1, ...d.users.map((u) => u.work_s + u.qc_s));
    $('tmBars').replaceChildren(...(d.users.length ? d.users.slice(0, 12).map((u, i) => el('div', { class: 'hbar link', title: 'Their files', onclick: () => { clearFilters(); $('fWorkedBy').value = u.user; shownLimit = PAGE; show('files'); $('fAdvPanel').classList.remove('hidden'); } },
      el('span', { class: 'hk', text: u.user }),
      el('div', { class: 'ht' }, el('i', { class: 'info', style: `width:${100 * u.work_s / umax}%;animation-delay:${i * 40}ms` }), el('i', { class: 'warn', style: `width:${100 * u.qc_s / umax}%;animation-delay:${i * 40 + 60}ms` })),
      el('b', { text: dur(u.work_s + u.qc_s) }))) : [el('p', { class: 'small muted', text: 'Nobody worked in this period.' })]));
    $('tmTable').querySelector('tbody').replaceChildren(...(d.users.length ? d.users.map((u) => el('tr', { class: 'frow' },
      el('td', {}, el('span', { class: 'who' }, el('span', { class: 'avatar sm', text: u.user[0].toUpperCase() }), u.user)),
      el('td', { text: dur(u.work_s) }), el('td', { text: u.qc_s ? dur(u.qc_s) : '—' }), el('td', {}, el('b', { text: dur(u.work_s + u.qc_s) })),
      el('td', { text: String(u.files) }), el('td', { text: dur((u.work_s + u.qc_s) / Math.max(1, Object.keys(u.days).length)) }),
      el('td', { class: 'small muted', text: String(Object.keys(u.days).length) })))
      : [el('tr', {}, el('td', { colspan: 7, class: 'muted small', text: 'No working time recorded in this period yet — it is counted while people work in the editor.' }))]));
    $('tmTiles').querySelectorAll('.tbig').forEach(() => {});
    revealCards($('pane-time'));
  }
  $('tmRange').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    tmDays = Number(b.dataset.d);
    $('tmRange').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    loadTime();
  }));
  $('tmUser').addEventListener('change', loadTime);
  $('tmRefresh').addEventListener('click', loadTime);
  $('tmReset').addEventListener('click', () => {
    tmDays = 7;
    $('tmUser').value = '';
    $('tmRange').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x.dataset.d === '7'));
    loadTime();
  });
  $('tmCsv').addEventListener('click', () => {
    if (!tmData) return;
    const q = (v) => '"' + String(v ?? '').replace(/"/g, '""') + '"';
    const rows = [['person', 'day', 'working (min)', 'QC (min)']];
    tmData.users.forEach((u) => Object.entries(u.days).forEach(([k, v]) => rows.push([u.user, k, Math.round(v[0] / 6) / 10, Math.round(v[1] / 6) / 10])));
    const a = el('a', { href: URL.createObjectURL(new Blob(['﻿' + rows.map((r) => r.map(q).join(',')).join('\r\n')], { type: 'text/csv' })), download: `working_time_${tmData.since}.csv` });
    document.body.append(a);
    a.click();
    a.remove();
  });

  // ------------------------------------------------------------ little animations (nothing runs when motion is reduced)
  const still = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  let revealIO = null;
  function revealCards(root) {
    if (!root || still() || !('IntersectionObserver' in window)) return;
    revealIO = revealIO || new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('in'); revealIO.unobserve(e.target); } }), { rootMargin: '0px 0px -8% 0px' });
    root.querySelectorAll('.card, .ucard').forEach((c, i) => {
      if (c.classList.contains('in') || c.classList.contains('reveal')) return;
      c.classList.add('reveal');
      c.style.setProperty('--ri', String(Math.min(i, 12)));
      revealIO.observe(c);
    });
    setTimeout(() => root.querySelectorAll('.reveal:not(.in)').forEach((c) => c.classList.add('in')), 1600);   // whatever happens, nothing stays hidden
  }
  // the filter bar gets a shadow when the page is scrolled under it
  (() => {
    const bar = $('filterBar');
    if (!bar || !('IntersectionObserver' in window)) return;
    const mark = el('div', { class: 'stickmark', 'aria-hidden': 'true' });
    bar.before(mark);
    new IntersectionObserver(([e]) => bar.classList.toggle('stuck', !e.isIntersecting), { rootMargin: '-60px 0px 0px 0px' }).observe(mark);
  })();

  // ------------------------------------------------------------ bulk upload
  let picked = [];
  function showFolder(list) {
    picked = list.filter((f) => /\.pdf$/i.test(f.name));
    const size = picked.reduce((a, f) => a + f.size, 0);
    const root = picked.length ? (picked[0].webkitRelativePath || picked[0].name).split('/')[0] : '';
    $('folderInfo').textContent = picked.length
      ? `${root}: ${picked.length} PDF(s), ${(size / 1048576).toFixed(1)} MB` + (list.length > picked.length ? ` (${list.length - picked.length} other file(s) left out)` : '')
      : (list.length ? 'No PDF in that folder.' : 'No folder chosen.');
    $('bulkGo').disabled = !picked.length;
    $('folderList').replaceChildren(...picked.slice(0, 300).map((f) => el('div', { class: 'fitem' },
      el('span', { text: '📄 ' + (f.webkitRelativePath || f.name) }), el('span', { class: 'tiny muted', text: (f.size / 1024).toFixed(0) + ' KB' }))),
    picked.length > 300 ? el('div', { class: 'tiny muted', text: `… and ${picked.length - 300} more` }) : '');
  }
  $('folderInput').addEventListener('change', () => showFolder([...$('folderInput').files]));
  const fdrop = $('fdrop');
  ['dragenter', 'dragover'].forEach((ev) => fdrop.addEventListener(ev, (e) => { e.preventDefault(); fdrop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => fdrop.addEventListener(ev, (e) => { e.preventDefault(); fdrop.classList.remove('over'); }));
  fdrop.addEventListener('drop', async (e) => {
    // a folder dropped: walk it
    const items = [...(e.dataTransfer.items || [])].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    const out = [];
    const walk = (entry, path) => new Promise((res) => {
      if (entry.isFile) {
        entry.file((f) => { Object.defineProperty(f, 'webkitRelativePath', { value: path + f.name }); out.push(f); res(); }, () => res());
      } else if (entry.isDirectory) {
        const r = entry.createReader();
        const all = [];
        const more = () => r.readEntries(async (ents) => {
          if (!ents.length) { for (const en of all) await walk(en, path + entry.name + '/'); res(); return; }
          all.push(...ents); more();
        }, () => res());
        more();
      } else res();
    });
    for (const it of items) await walk(it, '');
    if (out.length) showFolder(out);
  });
  $('bulkGive').addEventListener('change', () => {
    const v = $('bulkGive').value;
    $('bulkOneRow').classList.toggle('hidden', v !== 'one');
    $('bulkUsers').classList.toggle('hidden', !['round_robin', 'balance'].includes(v));
  });
  $('bulkGo').addEventListener('click', async () => {
    if (!picked.length) return;
    const root = (picked[0].webkitRelativePath || picked[0].name).split('/')[0] || 'folder';
    const give = $('bulkGive').value;
    const many = [...$('bulkUsers').querySelectorAll('input:checked')].map((c) => c.value);
    if (['round_robin', 'balance'].includes(give) && !many.length) { UA.toast('Tick the users to share the files among'); return; }
    $('bulkGo').disabled = true;
    $('bulkProg').classList.remove('hidden');
    let batch = '';
    const jobs = [];
    const CH = 8;
    for (let i = 0; i < picked.length; i += CH) {
      const part = picked.slice(i, i + CH);
      const fd = new FormData();
      part.forEach((f) => fd.append('pdf', f, f.name));
      fd.append('paths_json', JSON.stringify(part.map((f) => f.webkitRelativePath || f.name)));
      fd.append('profile', $('bulkProfile').value);
      fd.append('folder', root);
      if (batch) fd.append('batch', batch);
      if (give === 'one') fd.append('assign_to', $('bulkOne').value);
      $('bulkMsg').textContent = `Uploading ${Math.min(i + CH, picked.length)} of ${picked.length}…`;
      const r = await fetch('/upload', { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' } });
      const d = await r.json().catch(() => ({ error: 'upload failed (' + r.status + ')' }));
      if (d.error) { UA.toast(d.error, 6000); break; }
      batch = d.batch || batch;
      jobs.push(...(d.jobs || []));
      $('bulkBar').style.width = Math.round(100 * Math.min(i + CH, picked.length) / picked.length) + '%';
    }
    if (jobs.length && ['round_robin', 'balance'].includes(give)) {
      await post('/api/admin/files/assign', { ids: jobs, users: many, mode: give });
    }
    $('bulkMsg').textContent = `${jobs.length} file(s) uploaded from “${root}” — they are being analysed now.`;
    UA.toast(`${jobs.length} file(s) uploaded`);
    $('bulkGo').disabled = false;
    $('fFolder').value = '';
    setTimeout(() => { show('files'); setTimeout(() => { $('fFolder').value = root; renderFiles(); }, 400); }, 900);
  });

  // ------------------------------------------------------------ activity
  async function loadLog() {
    const who = $('logUser').value;
    const act = $('logAction').value;
    const log = await UA.get('/api/admin/log?limit=600' + (who ? '&user=' + encodeURIComponent(who) : ''));
    const q = ($('logSearch').value || '').toLowerCase();
    const list = log.filter((e) => (!q || `${e.by} ${e.action} ${e.file} ${e.detail}`.toLowerCase().includes(q))
      && (!act || (e.action || '').startsWith(act)));
    $('logCsv').href = '/api/admin/log.csv?' + new URLSearchParams({ user: who, action: act }).toString();
    const byAct = {};
    list.forEach((e) => { const k = (e.action || '').split(' ')[0]; byAct[k] = (byAct[k] || 0) + 1; });
    $('logStats').replaceChildren(...Object.entries(byAct).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([k, v]) =>
      el('button', { class: 'sm lchip' + (act === k ? ' on' : ''), text: `${k} ${v}`, onclick: () => { $('logAction').value = act === k ? '' : k; loadLog(); } })));
    $('logList').replaceChildren(...list.map((e, i) => el('li', { class: 'lg-' + e.action.split(' ')[0], style: `animation-delay:${Math.min(i, 30) * 10}ms` },
      el('span', { class: 'avatar sm', text: (e.by || '?')[0].toUpperCase() }),
      el('div', {}, el('b', { text: e.by }), ` ${e.action} `, e.file ? el('a', { href: '/job/' + e.job, text: e.file }) : '',
        e.detail ? el('span', { class: 'muted', text: ' — ' + e.detail }) : '',
        el('div', { class: 'tiny muted', text: fmt(e.at) })))));
    if (!list.length) $('logList').append(el('li', { class: 'muted', text: 'Nothing yet.' }));
  }
  const logFiltered = () => !!($('logUser').value || $('logAction').value || $('logSearch').value);
  const logChanged = () => { $('logReset').disabled = !logFiltered(); loadLog(); };
  $('logUser').addEventListener('change', logChanged);
  $('logAction').addEventListener('change', logChanged);
  $('logSearch').addEventListener('input', logChanged);
  $('logReset').addEventListener('click', () => { $('logUser').value = ''; $('logAction').value = ''; $('logSearch').value = ''; logChanged(); });

  // ------------------------------------------------------------ sample files (V4.20)
  let samples = [];
  let kClient = null;
  const SSTAT = { queued: ['waiting', ''], learning: ['learning…', 'warn'], ready: ['learned', 'ok'], error: ['failed', 'err'] };
  async function loadSamples() {
    const d = await UA.get('/api/admin/samples');
    samples = d.samples || [];
    const names = d.names || [];
    const fsel = $('sFilter');
    const cur = fsel.value;
    const used = [...new Set(samples.map((x) => x.client))];
    fsel.replaceChildren(new Option('Every client', '*'), ...used.map((c) => new Option(c || 'Common', c)));
    fsel.value = used.includes(cur) || cur === '*' ? cur : '*';
    // the client list of the upload form: profiles + clients with samples
    const cs = $('sClient');
    const keep = cs.value;
    cs.replaceChildren(new Option('Common — every client', ''), ...names.map((n) => new Option(n, n)), new Option('New client…', '__new__'));
    cs.value = [...cs.options].some((o) => o.value === keep) ? keep : '';
    renderSamples();
    const clients = d.clients || [];
    if (clients.length && (kClient === null || !clients.some((c) => c.client === kClient))) kClient = clients[0].client;
    $('kClients').replaceChildren(...clients.map((c) => el('button', { class: kClient === c.client ? 'on' : '', text: `${c.client || 'Common'} (${c.ready || 0})`,
      onclick: () => { kClient = c.client; loadSamples(); } })));
    if (clients.length) loadKnowledge(kClient); else $('kView').replaceChildren(el('p', { class: 'small muted', text: 'No sample files yet. Add a few finished files of a client above.' }));
    const busy = samples.filter((x) => x.status === 'queued' || x.status === 'learning').length;
    $('sampCount').textContent = busy ? String(busy) : '';
    clearTimeout(loadSamples.t);
    if (busy) loadSamples.t = setTimeout(() => { if (!$('pane-samples').classList.contains('hidden')) loadSamples(); }, 2500);
  }
  function renderSamples() {
    const f = $('sFilter').value;
    const list = samples.filter((x) => f === '*' || x.client === f);
    $('sCount').textContent = `${list.length} file(s)`;
    $('sTable').querySelector('tbody').replaceChildren(...(list.length ? list.map((x, i) => {
      const on = el('input', { type: 'checkbox', title: 'Use this sample' });
      on.checked = !!x.enabled;
      on.addEventListener('change', async () => { await post('/api/admin/samples/' + x.id, { enabled: on.checked }); UA.toast(on.checked ? 'Used again' : 'Not used any more'); loadSamples(); });
      const st = SSTAT[x.status] || [x.status, ''];
      return el('tr', { class: 'frow', style: `animation-delay:${Math.min(i, 20) * 15}ms` },
        el('td', {}, el('a', { href: '#', text: x.filename, onclick: (e) => { e.preventDefault(); showSample(x.id); } }),
          el('div', { class: 'tiny muted' }, x.kind === 'approved' ? el('span', { class: 'chip ok', text: 'approved file' }) : x.kind === 'file' ? 'a file from the tool' : 'tagged PDF',
            x.job ? el('a', { href: '/job/' + x.job, text: ' · open the file' }) : '')),
        el('td', {}, el('span', { class: 'pchip', text: x.client || 'Common' })),
        el('td', { text: String(x.pages || '') }), el('td', { text: String(x.tags || '') }),
        el('td', {}, el('span', { class: 'chip ' + st[1], title: x.error || '', text: st[0] }), x.status === 'learning' ? el('span', { class: 'spinner' }) : '',
          x.error ? el('div', { class: 'tiny bad', text: x.error.slice(0, 120) }) : ''),
        el('td', {}, on), el('td', { class: 'small', text: `${fmt(x.created)}${x.uploaded_by ? ' · ' + x.uploaded_by : ''}` }),
        el('td', { class: 'right' },
          el('button', { class: 'sm', text: 'Details', onclick: () => showSample(x.id) }),
          el('button', { class: 'sm', text: 'Learn again', title: 'Read the file again', onclick: async () => { await post('/api/admin/samples/' + x.id, { relearn: true }); loadSamples(); } }),
          el('button', { class: 'sm danger', text: 'Delete', onclick: async () => {
            if (!confirm(`Delete the sample ${x.filename}? What the tool learned from it goes too.`)) return;
            await fetch('/api/admin/samples/' + x.id, { method: 'DELETE' });
            loadSamples();
          } })));
    }) : [el('tr', {}, el('td', { colspan: 8, class: 'muted small', text: 'No sample files.' }))]));
  }
  $('sFilter').addEventListener('change', renderSamples);
  async function loadKnowledge(client) {
    const k = await UA.get('/api/admin/samples/knowledge?client=' + encodeURIComponent(client || ''));
    if (!k.files) { $('kView').replaceChildren(el('p', { class: 'small muted', text: 'Nothing learned for this client yet (the files are still being read, failed, or are switched off).' })); return; }
    const rmax = Math.max(1, ...Object.values(k.roles || {}));
    const box = (title, ...kids) => el('div', { class: 'kbox' }, el('h4', { text: title }), ...kids);
    $('kView').replaceChildren(
      box(`Tags in ${k.files} sample file(s)`, el('div', { class: 'hbars' }, ...Object.entries(k.roles || {}).slice(0, 12).map(([r, n], i) => el('div', { class: 'hbar' },
        el('span', { class: 'hk', text: r }), el('div', { class: 'ht' }, el('i', { class: r.startsWith('H') ? 'acc' : r === 'P' ? 'info' : 'warn', style: `width:${100 * n / rmax}%;animation-delay:${i * 30}ms` })),
        el('b', { text: String(n) }))))),
      box('How each tag looks', el('table', { class: 'list mini' }, el('thead', {}, el('tr', {}, el('th', { text: 'Tag' }), el('th', { text: 'Size × body' }), el('th', { text: 'Bold' }), el('th', { text: 'Seen' }))),
        el('tbody', {}, ...Object.entries(k.styles || {}).sort().map(([r, v]) => el('tr', {}, el('td', {}, el('b', { text: r })), el('td', { text: '×' + v.size }), el('td', { text: v.bold + '%' }), el('td', { text: String(v.n) })))))),
      box('Wording always tagged one way', (k.words || []).length ? el('div', { class: 'kwords' }, ...k.words.slice(0, 40).map((w) => el('span', { class: 'kword', title: `${w.n}× in the samples` }, w.text, el('i', { text: ' → ' + w.role }))))
        : el('p', { class: 'tiny muted', text: 'No wording seen twice yet.' })),
      box('Running heads and feet (artifacts)', (k.artifacts || []).length ? el('ul', { class: 'klist' }, ...k.artifacts.slice(0, 12).map((a) => el('li', {}, el('code', { text: a.pattern }), el('span', { class: 'tiny muted', text: ` ${a.band === 'top' ? 'top' : 'foot'} · ${a.n}×` }))))
        : el('p', { class: 'tiny muted', text: 'None seen.' })),
      box('Which tag holds which', el('ul', { class: 'klist' }, ...(k.pairs || []).slice(0, 16).map((p) => el('li', {}, el('code', { text: `${p.parent} > ${p.child}` }), el('span', { class: 'tiny muted', text: ` ${p.n}×` }))))),
      (k.fixes || []).length ? box('What people fix most', el('ul', { class: 'klist' }, ...k.fixes.slice(0, 12).map((f) => el('li', {},
        el('code', { text: `${f.from} → ${f.to}` }), el('span', { class: 'tiny muted', text: ` ${f.n}× · “${f.example}”` }))))) : '',
      (k.captions || []).length ? box('Where captions go', el('ul', { class: 'klist' }, ...k.captions.map((c) => el('li', {},
        el('code', { text: `${c.kind}: ${c.place.replace('_', ' ')}` }), el('span', { class: 'tiny muted', text: ` ${c.n}×` }))))) : '',
      box('Where the samples come from', el('div', { class: 'kwords' }, ...Object.entries(k.kinds || {}).map(([kd, v]) =>
        el('span', { class: 'kword', text: `${kd === 'approved' ? 'approved in the tool' : kd === 'file' ? 'added from Files' : 'uploaded tagged PDFs'}: ${v}` })))),
      Object.keys(k.rolemap || {}).length ? box("The client's own tag names", el('ul', { class: 'klist' }, ...Object.entries(k.rolemap).slice(0, 16).map(([m, n]) => el('li', {}, el('code', { text: m }), el('span', { class: 'tiny muted', text: ` ${n}×` }))))) : '');
  }
  // upload
  $('sClient').addEventListener('change', () => {
    const nw = $('sClient').value === '__new__';
    $('sNewRow').classList.toggle('hidden', !nw);
    $('sMakeRow').classList.toggle('hidden', !nw);
    if (nw) $('sNew').focus();
  });
  const sPick = () => {
    const fs = [...$('sFiles').files].filter((f) => /\.pdf$/i.test(f.name));
    $('sInfo').textContent = fs.length ? `${fs.length} PDF(s): ${fs.slice(0, 4).map((f) => f.name).join(', ')}${fs.length > 4 ? '…' : ''}` : 'No file chosen.';
    $('sGo').disabled = !fs.length;
  };
  $('sFiles').addEventListener('change', sPick);
  const sDrop = $('sDrop');
  ['dragenter', 'dragover'].forEach((ev) => sDrop.addEventListener(ev, (e) => { e.preventDefault(); sDrop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => sDrop.addEventListener(ev, (e) => { e.preventDefault(); sDrop.classList.remove('over'); }));
  sDrop.addEventListener('drop', (e) => {
    const dt = new DataTransfer();
    [...e.dataTransfer.files].filter((f) => /\.pdf$/i.test(f.name)).forEach((f) => dt.items.add(f));
    $('sFiles').files = dt.files;
    sPick();
  });
  $('sForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    if ($('sClient').value === '__new__' && !$('sNew').value.trim()) { UA.toast('Give the new client a name'); return; }
    $('sGo').disabled = true;
    $('sInfo').textContent = 'Uploading…';
    const r = await fetch('/api/admin/samples/upload', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({ error: 'upload failed (' + r.status + ')' }));
    if (d.error) { UA.toast(d.error, 5000); $('sGo').disabled = false; sPick(); return; }
    UA.toast(`${d.ids.length} sample file(s) for ${d.client || 'every client'} — learning now` + (d.profile_made ? ` · profile “${d.profile_made}” made` : ''), 4500);
    e.target.reset();
    $('sNewRow').classList.add('hidden');
    $('sMakeRow').classList.add('hidden');
    sPick();
    kClient = d.client;
    if (d.profile_made) setTimeout(() => location.reload(), 1500); else loadSamples();
  });

  // ------------------------------------------------------------ learning settings (V4.21)
  async function loadLearning() {
    const d = await UA.get('/api/admin/learning');
    if (!d || d.error) return;
    document.querySelectorAll('[data-learn]').forEach((cb) => { cb.checked = !!d.learning[cb.dataset.learn]; });
    $('lAi').textContent = d.ai ? 'AI is on' : 'AI is off in Settings';
    $('lAi').className = 'chip ' + (d.ai ? 'ok' : 'warn');
    const db = d.db || {};
    $('lDb').replaceChildren(...(db.exists ? [
      ['sample files', db.samples], ['tags learned', db.blocks], ['fixes by people', db.corrections], ['file models stored', db.models],
      ['running heads', db.artifacts], ['database', mb(db.size)], ['sample PDFs', mb(db.files_size)]] : [['database', 'empty']])
      .map(([k, v], i) => el('div', { class: 'dbstat', style: `animation-delay:${i * 40}ms` }, el('b', { text: String(v ?? 0) }), el('span', { text: k }))));
  }
  document.querySelectorAll('[data-learn]').forEach((cb) => cb.addEventListener('change', async () => {
    const r = await post('/api/admin/learning', { [cb.dataset.learn]: cb.checked });
    if (!r.error) UA.toast(cb.closest('label').querySelector('b').textContent + (cb.checked ? ' — on' : ' — off'));
  }));
  $('lRelearn').addEventListener('click', async () => {
    if (!confirm('Read every sample file again? It runs in the background.')) return;
    const r = await post('/api/admin/samples/relearn_all', {});
    if (!r.error) { UA.toast(`${r.queued} sample file(s) are being learned again`); loadSamples(); }
  });

  // ------------------------------------------------------------ the self-learning brain (V4.25)
  const SRC = { rules: 'The analysis\u2019 rules', samples: 'Nearest sample tags', model: 'Its own trained model',
    style: 'The client\u2019s style map', wording: 'Wording memory', code: 'Learned code (its own rules)',
    layout: 'The same look in this file', ai: 'The local AI (Ollama)' };
  const pct = (v) => (v === null || v === undefined) ? '—' : Math.round(v * (v <= 1 ? 100 : 1)) + ' %';
  function svgEl(tag, attrs) {
    const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
    return n;
  }
  function curve(series) {
    // series: [{name, cls, points: [{x: index, y: 0..100, label}]}] — one shared 0–100 % axis
    const W = 640, H = 210, L = 34, R = 10, T = 10, B = 26;
    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'brsvg', role: 'img',
      'aria-label': 'Learning curve: ' + series.map((s) => `${s.name} ${s.points.map((p) => Math.round(p.y) + '%').join(', ')}`).join('; ') });
    const n = Math.max(2, ...series.map((s) => s.points.length));
    const x = (i, len) => L + (W - L - R) * (len <= 1 ? 0.5 : i / (len - 1));
    const y = (v) => T + (H - T - B) * (1 - Math.max(0, Math.min(100, v)) / 100);
    [0, 25, 50, 75, 100].forEach((g) => {
      svg.append(svgEl('line', { x1: L, x2: W - R, y1: y(g), y2: y(g), class: 'grid' }));
      const t = svgEl('text', { x: L - 6, y: y(g) + 4, 'text-anchor': 'end', class: 'axis' });
      t.textContent = g + '%';
      svg.append(t);
    });
    series.forEach((s) => {
      const len = s.points.length;
      if (!len) return;
      const d = s.points.map((p, i) => `${i ? 'L' : 'M'}${x(i, len).toFixed(1)},${y(p.y).toFixed(1)}`).join(' ');
      svg.append(svgEl('path', { d, class: 'ln ' + s.cls }));
      s.points.forEach((p, i) => {
        const c = svgEl('circle', { cx: x(i, len), cy: y(p.y), r: 3.5, class: 'pt ' + s.cls });
        const tt = svgEl('title', {});
        tt.textContent = `${s.name}: ${Math.round(p.y)} % — ${p.label || ''}`;
        c.append(tt);
        svg.append(c);
      });
      const last = s.points[len - 1];
      const ly = y(last.y) < T + 14 ? y(last.y) + 16 : y(last.y) - 7;       // never above the frame
      const lt = svgEl('text', { x: Math.min(W - R - 2, x(len - 1, len) + 2), y: ly, 'text-anchor': 'end', class: 'lbl ' + s.cls });
      lt.textContent = Math.round(last.y) + ' %';
      svg.append(lt);
    });
    const xl = svgEl('text', { x: W - R, y: H - 6, 'text-anchor': 'end', class: 'axis' });
    xl.textContent = 'older → newer';
    svg.append(xl);
    void n;
    return svg;
  }
  async function loadBrain() {
    const d = await UA.get('/api/admin/brain');
    if (!d || d.error) { $('brSub').textContent = (d && d.error) || 'The brain could not be read.'; return; }
    const m = d.model;
    const state = d.running ? ['learning…', 'warn'] : !m ? ['not trained yet', 'warn'] : d.stale ? ['new data — trains soon', 'warn'] : ['up to date', 'ok'];
    $('brState').textContent = state[0];
    $('brState').className = 'chip ' + state[1];
    $('brainCount').textContent = d.running ? '•' : '';
    $('brStudy').disabled = !d.ai || d.running;
    $('brTrain').disabled = d.running || !d.numpy;
    $('brDefault').textContent = 'Upload default: ' + (d.default_profile || '—');
    document.querySelectorAll('#pane-brain [data-learn]').forEach((cb) => { cb.checked = !!(d.learning || {})[cb.dataset.learn]; });
    const job = d.job || {};
    if (job.result && job.result.error) $('brSub').textContent = 'Last run: ' + job.result.error;
    else if (job.result && job.what === 'train' && job.result.ok && !job.result.skipped) $('brSub').textContent = `Trained on ${job.result.examples} examples from ${job.result.samples} file(s) in ${job.result.seconds} s.`;
    const c = d.curve || {};
    const tile = (big, label, sub, cls) => el('div', { class: 'tile ' + cls },
      el('div', { class: 'tbig', text: String(big) }), el('div', { class: 'tlabel', text: label }),
      sub ? el('div', { class: 'tsub tiny', text: sub }) : '');
    $('brTiles').replaceChildren(
      tile(m && m.accuracy != null ? pct(m.accuracy) : '—', 'Model accuracy',
        m ? `held-out files${m.folds > 1 ? ` (${m.folds} rounds)` : ''} · chance ${pct(m.baseline)}${m.ece != null ? ` · calibration error ${(m.ece * 100).toFixed(1)} %` : ''}` : 'needs a few sample files', 't-open'),
      tile(c.files ? c.last + ' %' : '—', 'Right first time', c.files ? `${c.files} approved file(s) · first ${c.first} %` : 'after the first approved file', 't-done'),
      tile(m ? (m.n_train || 0).toLocaleString() : 0, 'Examples learned', m ? `${m.n_samples} file(s) · ${(m.classes || []).length} kinds of tag` : '', 't-files'),
      tile((d.memory || 0).toLocaleString(), 'Memory (RAG)', d.embedded ? `${d.embedded.toLocaleString()} with AI vectors` : 'hashed vectors', 't-fix'),
      tile(d.styles || 0, 'Style patterns', 'font · size · weight → tag', 't-due'),
      tile(((d.corrections || 0) + (d.feedback || 0)).toLocaleString(), 'Fixes learned',
        `${d.corrections || 0} approved · ${d.feedback || 0} from ${d.feedback_files || 0} built file(s)`, 't-check'));
    // learning curve
    const appr = (d.history || []).filter((h) => h.kind === 'approved' && h.quality != null).reverse();
    const runs = (d.trainings || []).filter((t) => t.accuracy != null);
    const series = [
      { name: 'Right first time', cls: 'ap', points: appr.map((h) => ({ y: h.quality, label: (h.created || '').slice(0, 10) + ' ' + (h.client || '') })) },
      { name: 'Model accuracy', cls: 'up', points: runs.map((t) => ({ y: t.accuracy * 100, label: `${(t.created || '').slice(0, 10)} · ${t.n_train} examples` })) }];
    $('brCurve').replaceChildren(series.some((s) => s.points.length) ? curve(series)
      : el('p', { class: 'small muted', text: 'The curve starts with the first training and the first approved file.' }));
    // trust
    $('brTrust').replaceChildren(...(d.trust || []).map((t) => el('div', { class: 'hbar brbar', title: t.n ? `${t.ok} of ${t.n} decisions right · counts ×${t.weight}` : `not measured yet · counts ×${t.weight}` },
      el('span', { class: 'hk', text: SRC[t.source] || t.source }),
      el('span', { class: 'ht' }, el('i', { class: t.right == null ? 'info' : t.right >= 0.8 ? 'ok' : t.right >= 0.6 ? 'warn' : 'err',
        style: `width:${t.right != null ? Math.round(t.right * 100) : 0}%` })),
      el('b', { text: t.right != null ? `${Math.round(t.right * 100)} %` : '—' }),
      el('span', { class: 'tiny muted', text: '×' + t.weight }))));
    // style map
    const sb = $('brStyles').querySelector('tbody');
    sb.replaceChildren(...(d.style_map || []).map((r) => el('tr', {},
      el('td', { text: r.font }), el('td', { text: r.size.toFixed(1) + '×' }),
      el('td', { text: [r.bold ? 'bold' : '', r.italic ? 'italic' : '', r.caps ? 'CAPS' : ''].filter(Boolean).join(' ') || 'regular' }),
      el('td', {}, el('span', { class: 'chip', text: r.role })), el('td', { text: Math.round(r.share * 100) + ' %' }), el('td', { text: String(r.n) }))));
    if (!(d.style_map || []).length) sb.replaceChildren(el('tr', {}, el('td', { colspan: 6, class: 'muted small', text: 'No style learned yet (sample files learned with V4.25 or later).' })));
    const rb = $('brRoles').querySelector('tbody');
    const per = Object.entries((m && m.per_role) || {}).sort((a, b) => b[1].n - a[1].n);
    rb.replaceChildren(...per.map(([role, v]) => el('tr', {}, el('td', {}, el('span', { class: 'chip', text: role })),
      el('td', { text: pct(v.precision) }), el('td', { text: pct(v.recall) }), el('td', { text: pct(v.f1) }), el('td', { text: String(v.n) }))));
    if (!per.length) rb.replaceChildren(el('tr', {}, el('td', { colspan: 5, class: 'muted small', text: 'Measured at the next training (it needs at least 40 examples).' })));
    const types = Object.entries(d.doc_types || {});
    $('brDocs').textContent = types.length ? 'Document types in the samples: ' + types.map(([k, v]) => `${k.replace(/_/g, ' ')} ×${v}`).join(' · ') : '';
    // insights
    const ins = (d.insights || []).filter((x) => (x.rules || []).length || x.summary || x.error);
    $('brInsCount').textContent = ins.length ? `${ins.length} sample file(s) studied` : '';
    $('brInsights').replaceChildren(...(ins.length ? ins.slice(0, 30).map((x) => el('div', { class: 'brin' },
      el('div', { class: 'brinhead' }, el('b', { text: x.filename || x.sample_id }), el('span', { class: 'chip', text: x.client || 'common' }),
        x.doc_type ? el('span', { class: 'chip ok', text: x.doc_type.replace(/_/g, ' ') }) : '', el('span', { class: 'tiny muted', text: (x.model || '') + ' · ' + fmt(x.created) })),
      x.error ? el('div', { class: 'tiny', style: 'color:var(--err)', text: x.error }) : '',
      x.summary ? el('div', { class: 'small', text: x.summary }) : '',
      (x.rules || []).length ? el('ul', { class: 'small' }, ...(x.rules || []).map((r) => el('li', {}, r.tag ? el('span', { class: 'chip', text: r.tag }) : '', ' ' + (r.rule || '')))) : ''))
      : [el('p', { class: 'small muted', text: d.ai ? 'The AI studies each new sample by itself — or press “AI study of every sample”.' : 'Switch the AI on in Settings: the local AI then studies every sample and writes each client\u2019s tagging rules.' })]));
    // zones learned from people (V4.28)
    const zn = d.zones || {};
    $('znCount').textContent = zn.rows ? `${zn.corrections} correction(s) in ${zn.refs} file(s) · ${zn.rows.toLocaleString()} pairs` +
      (d.live && d.live.runs ? ` · learned live ${d.live.runs}×` : '') : '';
    const zb = $('znTable').querySelector('tbody');
    const ZS = { active: 'ok', waiting: 'warn', candidate: 'warn', off: '' };
    zb.replaceChildren(...(zn.models || []).map((m) => el('tr', {},
      el('td', { text: m.scope || 'everybody' }), el('td', { text: String(m.n) }), el('td', { text: String(m.explicit) }),
      el('td', { text: String(m.refs) }), el('td', { text: m.acc_model != null ? `${m.fixed} / ${m.broken}` : '—' }),
      el('td', { text: m.acc_model != null ? `${pct(m.acc_rule)} → ${pct(m.acc_model)}` : '—' }),
      el('td', {}, el('span', { class: 'chip ' + (ZS[m.status] || ''), title: m.note || '', text: m.status })),
      el('td', { class: 'small' }, m.convention ? el('div', { text: m.convention }) : '',
        el('div', { class: 'tiny muted', text: (m.why || []).map((x) => `${x.feature} ${x.weight > 0 ? '+' : ''}${x.weight}`).join(' · ') || (m.note || '') })))));
    if (!(zn.models || []).length) zb.replaceChildren(el('tr', {}, el('td', { colspan: 8, class: 'muted small',
      text: 'No zone model yet: it starts when people have cut or joined zones in a few places.' })));
    $('znWho').textContent = Object.keys(zn.people || {}).length ? 'Who taught it (corrections): ' +
      Object.entries(zn.people).map(([k, v]) => `${k} ${v}`).join(' · ') : '';
    // learned code (V4.26)
    const rules = d.code || [];
    $('cdCount').textContent = rules.length ? `${rules.filter((r) => r.status === 'active').length} active of ${rules.length}` : '';
    const cb = $('cdTable').querySelector('tbody');
    const ST = { active: 'ok', candidate: 'warn', retired: 'err', disabled: '' };
    cb.replaceChildren(...rules.map((r) => el('tr', {},
      el('td', { class: 'mono', text: `${r.id} v${r.version}` }),
      el('td', {}, el('span', { class: 'chip', text: `${(r.from_roles || []).join('/')} → ${r.target}` }), ' ', el('span', { class: 'small', text: r.description || '' })),
      el('td', { text: r.client || 'every client' }),
      el('td', { text: `${r.positives} fix(es)${r.verified_pos ? ` · ${r.verified_pos} approved` : ''}${r.files ? ` · ${r.files} file(s)` : ''}${r.origin === 'ai' ? ' · AI' : ''}` }),
      el('td', { text: `${Math.round((r.precision || 0) * 1000) / 10} % of ${r.matched}` }),
      el('td', { text: r.right + r.wrong ? `${r.right} right · ${r.wrong} wrong` : '—' }),
      el('td', {}, el('span', { class: 'chip ' + (ST[r.status] || ''), title: r.note || '', text: r.status })),
      el('td', {}, el('div', { class: 'btnrow', style: 'margin:0' },
        el('button', { class: 'sm', text: 'Code', onclick: () => { const v = $('cdCode'); v.textContent = r.source; v.classList.remove('hidden'); v.scrollIntoView({ block: 'nearest' }); } }),
        r.status === 'active' ? el('button', { class: 'sm', text: 'Switch off', onclick: () => ruleSet(r.id, 'disabled') })
          : el('button', { class: 'sm', text: 'Switch on', title: 'Use it from the next file', onclick: () => ruleSet(r.id, 'active') }),
        el('button', { class: 'sm danger', text: '✕', title: 'Delete the rule', onclick: async () => {
          if (!confirm(`Delete ${r.id}? The engine may write it again from the same fixes.`)) return;
          await fetch(`/api/admin/code/${r.id}`, { method: 'DELETE' }); loadBrain();
        } }))))));
    if (!rules.length) cb.replaceChildren(el('tr', {}, el('td', { colspan: 8, class: 'muted small', text: 'No rule yet: rules are written once people have made the same kind of fix three times or more.' })));
    // memory
    const mm = d.memories || {};
    $('memView').replaceChildren(...(mm.pictures || mm.readings ? [
      el('div', { class: 'facts' }, el('div', { class: 'fact' }, el('span', { text: 'Pictures with alt text' }), el('b', { text: `${mm.pictures} (${mm.pictures_confirmed || 0} confirmed)` })),
        el('div', { class: 'fact' }, el('span', { text: 'Readings (/ActualText)' }), el('b', { text: String(mm.readings) }))),
      ...(mm.examples || []).slice(0, 6).map((x) => el('div', { class: 'tiny', text: `“${x.text}” → “${x.said}” (${x.n}×${x.v ? ', confirmed' : ''})` })),
      ...(mm.alts || []).slice(0, 6).map((x) => el('div', { class: 'tiny', text: `picture ${x.digest}… → “${x.alt}” (${x.n}×${x.v ? ', confirmed' : ''})` }))]
      : [el('span', { text: 'Nothing remembered yet.' })]));
    // privacy
    const pv = d.privacy || {};
    $('pvState').textContent = pv.on ? 'guard on' : 'guard OFF';
    $('pvState').className = 'chip ' + (pv.on ? 'ok' : 'err');
    $('pvGuard').checked = !!pv.on;
    $('pvLog').replaceChildren(...((pv.recent || []).length ? pv.recent.slice(0, 12).map((x) => el('div', { text: `${x.time} · ${x.process || ''} · ${x.kind} ${x.target}${x.where ? ' · ' + x.where : ''}` })) : [el('span', { text: 'none' })]));
    privacyLoad();
    const hb = $('brHist').querySelector('tbody');
    hb.replaceChildren(...(d.history || []).slice(0, 60).map((h) => el('tr', {},
      el('td', { text: fmt(h.created) }), el('td', {}, h.file ? el('a', { href: '/job/' + h.job, text: h.file }) : el('span', { class: 'muted', text: h.job })),
      el('td', { text: h.client || '' }), el('td', {}, el('span', { class: 'chip ' + (h.kind === 'approved' ? 'ok' : ''), text: h.kind })),
      el('td', { text: String(h.tags ?? '') }), el('td', { text: h.quality != null ? h.quality + ' %' : '—' }),
      el('td', { text: h.brain_quality != null ? h.brain_quality + ' %' : '—' }))));
    if (!(d.history || []).length) hb.replaceChildren(el('tr', {}, el('td', { colspan: 7, class: 'muted small', text: 'Nothing yet: built and approved files show up here.' })));
    clearTimeout(loadBrain.t);
    if (d.running) loadBrain.t = setTimeout(() => { if (!$('pane-brain').classList.contains('hidden')) loadBrain(); }, 3000);
  }
  async function ruleSet(id, status) {
    const r = await post(`/api/admin/code/${id}`, { status });
    if (!r.error) { UA.toast(`${id}: ${status === 'active' ? 'on' : 'off'}`); loadBrain(); }
  }
  $('znTrain').addEventListener('click', async () => {
    const r = await post('/api/admin/zones/train', {});
    if (!r.error) { UA.toast(`Zone models: ${(r.models || []).length} trained`); setTimeout(loadBrain, 500); }
  });
  $('cdWrite').addEventListener('click', async () => {
    const r = await post('/api/admin/code/write', {});
    if (!r.error) { UA.toast(r.started ? 'The learning engine is writing its rules…' : 'The brain is busy — try again in a moment'); setTimeout(loadBrain, 1500); }
  });
  async function privacyLoad() {
    const p = await UA.get('/api/admin/privacy');
    if (!p || p.error) return;
    $('pvAllow').value = ((p.settings || {}).allow || []).join('\n');
    $('pvAi').textContent = p.ai_host ? (p.ai_host_local ? `Ollama at ${p.ai_host}: on this network ✓` : `Ollama at ${p.ai_host}: outside this network — allowed because Settings names it`) : '';
  }
  $('pvSave').addEventListener('click', async () => {
    const allow = $('pvAllow').value.split(/\s+/).filter(Boolean);
    const r = await post('/api/admin/privacy', { guard: $('pvGuard').checked, allow });
    if (!r.error) { UA.toast(r.on ? 'Privacy guard on' : 'Privacy guard OFF — documents could reach the internet'); loadBrain(); }
  });
  $('pvGuard').addEventListener('change', () => { if (!$('pvGuard').checked && !confirm('Switch the privacy guard off? The tool could then connect outside this server.')) $('pvGuard').checked = true; });
  let kbT = null;
  async function kbSearch() {
    const q = $('kbQ').value.trim();
    const r = await UA.get('/api/kb?k=8&q=' + encodeURIComponent(q || 'heading'));
    $('kbRes').replaceChildren(...((r && r.results) || []).map((x) => el('div', { class: 'brin' },
      el('div', { class: 'brinhead' }, el('b', { text: x.title }), el('span', { class: 'chip', text: x.source }),
        x.source === 'house rule' ? el('button', { class: 'sm danger', text: '✕', onclick: async () => { await fetch('/api/admin/kb/' + x.id, { method: 'DELETE' }); kbSearch(); } }) : ''),
      el('div', { class: 'tiny muted', text: x.refs }), el('div', { class: 'small', text: x.text }))));
  }
  $('kbQ').addEventListener('input', () => { clearTimeout(kbT); kbT = setTimeout(kbSearch, 250); });
  $('kbAdd').addEventListener('click', async () => {
    const r = await post('/api/admin/kb', { title: $('kbT').value, text: $('kbX').value, roles: $('kbR').value, client: $('kbC').value });
    if (!r.error) { UA.toast('House rule added — the AI reads it with every question'); $('kbT').value = $('kbX').value = ''; $('kbQ').value = ''; kbSearch(); }
  });
  $('brRefresh').addEventListener('click', loadBrain);
  $('brTrain').addEventListener('click', async () => {
    const r = await post('/api/admin/brain/train', {});
    if (!r.error) { UA.toast(r.started ? 'Training the brain — a few seconds to a minute' : 'The brain is already busy'); setTimeout(loadBrain, 800); }
  });
  $('brStudy').addEventListener('click', async () => {
    if (!confirm('Let the local AI study every sample file again? On a CPU this takes about a minute per file; it runs in the background.')) return;
    const r = await post('/api/admin/brain/study', {});
    if (!r.error) { UA.toast(r.started ? 'The AI is studying the samples' : 'The brain is already busy'); setTimeout(loadBrain, 800); }
  });

  async function showSample(id) {
    const d = await UA.get(`/api/admin/samples/${id}/detail`);
    if (!d || d.error) return;
    const det = d.details || {};
    const st = d.stats || {};
    $('sdTitle').textContent = d.filename;
    const fact = (k, v) => (v === undefined || v === null || v === '') ? null : el('div', { class: 'fact' }, el('span', { text: k }), el('b', { text: String(v) }));
    const roles = Object.entries(st.roles || {}).slice(0, 14);
    const rmax = Math.max(1, ...roles.map((r) => r[1]));
    $('sdBody').replaceChildren(
      el('div', { class: 'facts' }, fact('Client', d.client || 'Common'), fact('From', d.kind === 'approved' ? 'approved in the tool' : d.kind === 'file' ? 'added from Files' : 'uploaded tagged PDF'),
        fact('Status', d.status), fact('Pages', d.pages), fact('Tags learned', d.tags), fact('Fixes by people', (d.corrections || []).length),
        fact('Added', fmt(d.created) + (d.uploaded_by ? ' · ' + d.uploaded_by : '')), fact('Learned', fmt(d.learned)),
        fact('Approved by', det.approved_by), fact('Worked by', det.worked_by), fact('Profile', det.profile),
        fact('Checker errors', det.errors_before != null ? `${det.errors_before} → ${det.errors_after ?? '—'}` : ''),
        fact('Hand edits', (det.model || {}).hand_edits), fact('Model', d.model ? `${mb(d.model.size)} (stored ${mb(d.model.stored)})` : '')),
      el('div', { class: 'btnrow' },
        el('a', { class: 'btn sm', href: `/admin/samples/${id}.pdf`, target: '_blank', text: 'Open the PDF' }),
        d.model ? el('a', { class: 'btn sm', href: `/admin/samples/${id}/model.json`, text: '⤓ Tag model (.json)' }) : '',
        d.model && d.model.auto ? el('a', { class: 'btn sm', href: `/admin/samples/${id}/model.json?which=auto`, text: '⤓ What the tool proposed' }) : '',
        d.job ? el('a', { class: 'btn sm', href: '/job/' + d.job, text: 'Open the file' }) : ''),
      el('h4', { text: 'Tags in the file' }),
      el('div', { class: 'hbars' }, ...roles.map(([r, n], i) => el('div', { class: 'hbar' }, el('span', { class: 'hk', text: r }),
        el('div', { class: 'ht' }, el('i', { class: r.startsWith('H') ? 'acc' : 'info', style: `width:${100 * n / rmax}%;animation-delay:${i * 25}ms` })), el('b', { text: String(n) })))),
      (d.corrections || []).length ? el('h4', { text: 'What people changed' }) : '',
      (d.corrections || []).length ? el('ul', { class: 'klist' }, ...d.corrections.slice(0, 40).map((c) => el('li', {},
        el('code', { text: `${c.from_role} → ${c.role}` }), el('span', { class: 'small', text: ' ' + (c.text || '').slice(0, 90) })))) : '',
      (d.artifacts || []).length ? el('h4', { text: 'Running heads and feet' }) : '',
      (d.artifacts || []).length ? el('ul', { class: 'klist' }, ...d.artifacts.slice(0, 12).map((a) => el('li', {}, el('code', { text: a.pattern }), el('span', { class: 'tiny muted', text: ` ${a.band} · ${a.n}×` })))) : '',
      el('h4', { text: 'Some tags learned' }),
      el('table', { class: 'list mini' }, el('tbody', {}, ...(d.blocks || []).slice(0, 30).map((b) => el('tr', {},
        el('td', {}, el('b', { text: b.role })), el('td', { class: 'small', text: (b.text || '').slice(0, 90) }),
        el('td', { class: 'tiny muted', text: `×${(b.size_rel || 0).toFixed(2)}${b.bold ? ' bold' : ''}` }))))),
      d.error ? el('p', { class: 'small bad', text: d.error }) : '');
    $('sampDlg').showModal();
  }

  // ------------------------------------------------------------ one user (V4.21)
  async function userDetail(name) {
    const d = await UA.get('/api/admin/users/' + encodeURIComponent(name) + '/detail');
    if (!d || d.error) return;
    const u = d.user;
    $('udTitle').textContent = u.name ? `${u.name} (${u.username})` : u.username;
    const open = d.files.filter((f) => f.work !== 'approved' && (f.assigned_to || '').toLowerCase() === u.username.toLowerCase());
    const others = (window.ALL_USERS || []).filter((x) => x !== u.username);
    const to = el('select', { class: 'sm' }, new Option('the pool', ''), ...others.map((x) => new Option(x, x)));
    const tile = (n, l) => el('div', { class: 'dbstat' }, el('b', { text: String(n) }), el('span', { text: l }));
    $('udBody').replaceChildren(
      el('div', { class: 'dbstats' }, tile(d.files.length, 'files'), tile(open.length, 'open'), tile(d.work.approved || 0, 'approved'),
        tile(d.pages, 'pages'), tile(d.pages_done, 'pages approved'), tile(d.errors_left, 'errors left'), tile(d.active_days, 'active days')),
      el('div', { class: 'facts' },
        el('div', { class: 'fact' }, el('span', { text: 'Role' }), el('b', { text: u.role + (u.active ? '' : ' · switched off') })),
        el('div', { class: 'fact' }, el('span', { text: 'Last sign-in' }), el('b', { text: fmt(u.last_login) || 'never' })),
        u.email ? el('div', { class: 'fact' }, el('span', { text: 'E-mail' }), el('b', { text: u.email })) : null,
        u.note ? el('div', { class: 'fact' }, el('span', { text: 'Note' }), el('b', { text: u.note })) : null),
      el('div', { class: 'btnrow' },
        el('button', { class: 'sm', text: 'Their files', onclick: () => { $('userDetailDlg').close(); $('fUser').value = u.username; show('files'); } }),
        open.length ? el('span', { class: 'small muted', text: `Give their ${open.length} open file(s) to` }) : '', open.length ? to : '',
        open.length ? el('button', { class: 'sm', text: 'Move', onclick: async () => {
          const ids_ = open.map((f) => f.id);
          const r = to.value ? await post('/api/admin/files/assign', { ids: ids_, to: to.value }) : await post('/api/admin/files/unassign', { ids: ids_ });
          if (!r.error) { UA.toast(`${ids_.length} file(s) moved`); userDetail(name); }
        } }) : ''),
      el('h4', { text: 'Files' }),
      el('div', { class: 'ulist' }, ...(d.files.length ? d.files.slice(0, 60).map((f) => el('div', { class: 'urow' },
        el('a', { href: '/job/' + f.id, text: f.filename }), el('span', { class: 'wchip w-' + (f.work || 'none'), text: STATES[f.work] || f.work || '—' }),
        el('span', { class: 'pchip', text: f.profile }), el('span', { class: 'tiny muted', text: `${f.pages} p.` })))
        : [el('p', { class: 'small muted', text: 'No files.' })])),
      el('h4', { text: 'What they did lately' }),
      el('ol', { class: 'timeline small' }, ...(d.log.length ? d.log.slice(0, 30).map((e) => el('li', {}, el('div', {}, ` ${e.action} `,
        e.file ? el('a', { href: '/job/' + e.job, text: e.file }) : '', e.detail ? el('span', { class: 'muted', text: ' — ' + e.detail }) : '',
        el('div', { class: 'tiny muted', text: fmt(e.at) })))) : [el('li', { class: 'muted', text: 'Nothing yet.' })])));
    $('userDetailDlg').showModal();
  }

  // ------------------------------------------------------------ system (V4.21)
  async function loadSystem() {
    const d = await UA.get('/api/admin/system');
    if (!d || d.error) return;
    const tile = (big, label, sub, cls) => el('div', { class: 'tile ' + cls }, el('div', { class: 'tbig', text: String(big) }),
      el('div', { class: 'tlabel', text: label }), sub ? el('div', { class: 'tsub', text: sub }) : null);
    const disk = d.disk || {};
    $('sysTiles').replaceChildren(
      tile(d.jobs, 'files on the server', mb(d.jobs_size), 't-files'),
      tile(disk.total ? Math.round(100 * (1 - disk.free / disk.total)) + '%' : '—', 'disk used', disk.free ? mb(disk.free) + ' free' : '', 't-check'),
      tile((d.queue.running || []).length, 'working now', `${d.queue.waiting} waiting · ${d.queue.workers} at a time`, 't-busy'),
      tile((d.samples || {}).samples || 0, 'sample files', (d.samples || {}).exists ? mb((d.samples || {}).size) + ' database' : 'none yet', 't-done'));
    const row = (k, v) => el('tr', {}, el('td', { class: 'muted', text: k }), el('td', {}, typeof v === 'string' || typeof v === 'number' ? el('b', { text: String(v) }) : v));
    const st = d.stages || {};
    $('sysInfo').querySelector('tbody').replaceChildren(
      row('Version', d.version), row('Python / PyMuPDF', `${d.python} / ${d.pymupdf}`),
      row('Users / profiles', `${d.users} / ${d.profiles}`),
      row('Files by stage', `${st.review || 0} to review · ${st.done || 0} built · ${st.error || 0} failed · ${(st.queued || 0) + (st.processing || 0)} waiting/working`),
      row('At the same time', `${(d.parallel || {}).files || '?'} files, ${(d.parallel || {}).page_procs || '?'} cores each`),
      row('Dez AI', el('span', {}, el('span', { class: 'chip ' + (d.ai.enabled ? 'ok' : 'warn'), text: d.ai.enabled ? 'on' : 'off' }), ' ', el('span', { class: 'small', text: `${d.ai.base_url || ''} ${d.ai.text_model ? '· ' + d.ai.text_model : ''}` }))),
      row('Learning', Object.entries(d.learning || {}).map(([k, v]) => `${k.replace('_', ' ')} ${v ? 'on' : 'off'}`).join(' · ')),
      ...loadRows(d, row));
  }
  // V4.28: how busy the server is — is the one-at-a-time lock, the work queue or the learner a bottleneck?
  function loadRows(d, row) {
    const ld = d.load || {}, mu = ld.mupdf || {}, q = ld.queue || {}, lv = d.live || {};
    const editing = Object.entries(ld.editing || {});
    const people = new Set(editing.flatMap(([, who]) => who));
    const waiting = Object.entries(ld.people_waiting || {}).map(([w, n]) => `${w} ${n}`).join(', ');
    return [
      row('In the editor now', people.size ? `${people.size} ${people.size === 1 ? 'person' : 'people'} in ${editing.length} file(s)` : 'nobody'),
      row('Files held by an editor', (() => {
        const fl = ld.file_locks || {}, held = Object.values(fl.held || {});
        if (fl.on === false) return 'the lock is off (Workflow → One person at a time in a file)';
        return held.length ? `${held.length}: ${[...new Set(held.map((h) => h.user))].join(', ')} · ${fl.refused || 0} change(s) refused so far` : `none · ${fl.refused || 0} change(s) refused so far`;
      })()),
      row('Files waiting', q.waiting ? `${q.waiting} (${waiting}) · waited ${q.avg_wait_s}s on average, ${q.max_wait_s}s at most` : 'none'),
      row('One PDF at a time', `${mu.requests || 0} requests · ${mu.waited || 0} had to wait (${mu.avg_wait_ms || 0} ms on average, ${mu.max_wait_ms || 0} ms at most) · ${mu.waiting_now || 0} waiting now`),
      row('Learning from edits', `${lv.runs || 0} runs · ${lv.running || 0} now · ${lv.waiting || 0} waiting${lv.failed ? ' · ' + lv.failed + ' failed' : ''}`)];
  }
  $('cuPreview').addEventListener('click', async () => {
    const r = await post('/api/admin/cleanup', { which: $('cuWhich').value, days: $('cuDays').value, preview: true });
    if (r.error) return;
    $('cuOut').textContent = r.count ? `${r.count} file(s), ${mb(r.size)}: ${r.files.join(', ')}${r.count > r.files.length ? '…' : ''}` : 'No file matches.';
    $('cuGo').disabled = !r.count;
  });
  $('cuGo').addEventListener('click', async () => {
    if (!confirm('Delete these files for good? This cannot be undone.')) return;
    const r = await post('/api/admin/cleanup', { which: $('cuWhich').value, days: $('cuDays').value, preview: false });
    if (!r.error) { $('cuOut').textContent = `${r.count} file(s) deleted, ${mb(r.size)} freed.`; $('cuGo').disabled = true; loadSystem(); }
  });

  // ------------------------------------------------------------ templates
  async function loadTemplates() {
    const t = await UA.get('/api/templates');
    const row = (kind) => (x) => el('div', { class: 'tplrow' },
      el('span', { class: 'tplnum', text: '#' + x.number }),
      el('div', {}, el('b', { text: x.name || `${kind} template ${x.number}` }), el('div', { class: 'tiny muted', text: `${x.summary} · by ${x.by || '?'} ${fmt(x.made)}` })),
      el('button', { class: 'sm danger', text: 'Delete', onclick: async () => {
        if (!confirm(`Delete ${kind} template #${x.number}?`)) return;
        await post('/api/templates', { kind, number: x.number, delete: true });
        loadTemplates();
      } }));
    $('tplTable').replaceChildren(...(t.table.length ? t.table.map(row('table')) : [el('p', { class: 'small muted', text: 'None yet.' })]));
    $('tplList').replaceChildren(...(t.list.length ? t.list.map(row('list')) : [el('p', { class: 'small muted', text: 'None yet.' })]));
  }

  let start = 'overview';
  try { start = localStorage.getItem('ua-admin-tab') || start; } catch (e) { /* ignore */ }
  if (location.hash && document.querySelector(`.atabs button[data-tab="${location.hash.slice(1)}"]`)) start = location.hash.slice(1);
  show(start);
  overview();
  window.addEventListener('hashchange', () => {
    const t = location.hash.slice(1);
    if (document.querySelector(`.atabs button[data-tab="${t}"]`)) show(t);
  });
})();
