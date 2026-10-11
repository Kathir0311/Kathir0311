(function () {
  const $ = (id) => document.getElementById(id);
  const grid = $('optGrid');
  if (grid) uploadForm();
  function uploadForm() {
  const inputs = [...grid.querySelectorAll('[data-opt]')];
  const defaults = {};
  inputs.forEach((i) => { defaults[i.dataset.opt] = i.type === 'checkbox' ? i.checked : i.value; });

  function applyProfile(name) {
    const p = Object.assign({}, window.PROFILES[name] || {});
    inputs.forEach((i) => {
      const v = (i.dataset.opt in p) ? p[i.dataset.opt] : defaults[i.dataset.opt];
      if (i.type === 'checkbox') i.checked = !!v; else i.value = v;
      // an option that belongs to some profiles only (Academain: ActualText on a Span)
      if (i.dataset.showFor) i.closest('label').hidden = !(i.checked || i.dataset.showFor.split(',').includes(name));
    });
  }
  function collect() {
    const o = {};
    inputs.forEach((i) => {
      let v = i.type === 'checkbox' ? i.checked : i.value;
      if (i.dataset.type === 'int') v = parseInt(v, 10);
      if (i.dataset.type === 'float') v = parseFloat(v);
      o[i.dataset.opt] = v;
    });
    return o;
  }
  $('profile').addEventListener('change', (e) => applyProfile(e.target.value));
  applyProfile($('profile').value);

  $('upForm').addEventListener('submit', () => { $('optionsJson').value = JSON.stringify(collect()); });

  const drop = $('drop'), fi = $('fileInput');
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => { fi.files = e.dataTransfer.files; showFiles(); });
  fi.addEventListener('change', showFiles);
  function showFiles() {
    const n = fi.files.length;
    $('fileList').textContent = n ? [...fi.files].map((f) => f.name).join(', ') : '';
    $('batchRow').classList.toggle('hidden', n < 2);
  }

  if ($('saveProfile')) $('saveProfile').addEventListener('click', async () => {
    const name = $('profName').value.trim();
    if (!name) { UA.toast('Give the profile a name'); return; }
    const d = await UA.post('/api/profiles', { name, options: collect() });
    if (d.error) { UA.toast(d.error); return; }
    window.PROFILES = d.profiles;
    const sel = $('profile');
    if (![...sel.options].some((o) => o.value === name)) sel.append(new Option(name, name));
    sel.value = name;
    UA.toast('Profile saved');
  });
  if ($('delProfile')) $('delProfile').addEventListener('click', async () => {
    const name = $('profile').value;
    if (window.BUILTIN.includes(name)) { UA.toast('Built-in profiles cannot be deleted'); return; }
    if (!confirm('Delete profile ' + name + '?')) return;
    const d = await UA.post('/api/profiles', { name, delete: true });
    if (d.error) { UA.toast(d.error); return; }
    window.PROFILES = d.profiles;
    [...$('profile').options].forEach((o) => { if (o.value === name) o.remove(); });
    applyProfile($('profile').value);
  });
  }

  // ---------------------------------------------------------- jobs
  const batch = new URLSearchParams(location.search).get('batch');
  if (batch) {
    $('batchInfo').textContent = 'Batch ' + batch;
    const a = $('batchSummary'); a.href = '/batch/' + batch + '.xlsx'; a.classList.remove('hidden');
  }
  const active = ['queued', 'processing', 'building'];
  const open = new Set();
  let jobsCache = [];
  let tick = null;
  try { const f = JSON.parse(localStorage.getItem('ua-jobfilter') || '{}');
    ['jobStage', 'jobSort'].forEach((k) => { if (f[k]) $(k).value = f[k]; }); } catch (e) { /* ignore */ }
  const saveFilter = () => { try { localStorage.setItem('ua-jobfilter', JSON.stringify({ jobStage: $('jobStage').value, jobSort: $('jobSort').value })); } catch (e) { /* ignore */ } };

  const params = new URLSearchParams(location.search);
  let wfView = params.get('view') === 'queue' ? 'mine' : (params.get('step') || '');
  const W = window.WFUI;
  const STEP_TABS = [['', 'All'], ['mine', 'Waiting for me'], ['production', 'Production'], ['rework', 'Rework'], ['qa', 'QA'],
    ['final_qa', 'Final QA'], ['delivery', 'Delivery'], ['delivered', 'Delivered'], ['hold', 'On hold']];
  function stepOf(j, k) {
    if (k === 'mine') return !!j.todo;
    if (k === 'rework') return j.work === 'rework' || j.work === 'returned';
    return j.wstage === k;
  }
  function wfBar() {
    const bar = $('wfBar');
    if (!bar) return;
    bar.replaceChildren(...[...STEP_TABS.map(([k, label]) => {
      const n = k ? jobsCache.filter((j) => stepOf(j, k)).length : jobsCache.length;
      if (k && !n && k !== 'mine' && k !== wfView) return null;
      return UA.el('button', { type: 'button', role: 'tab', class: 'wftab t-' + (k || 'all') + (wfView === k ? ' on' : ''), 'aria-selected': wfView === k ? 'true' : 'false',
        onclick: () => { wfView = k; const u = new URL(location.href); u.searchParams.delete('view'); if (k) u.searchParams.set('step', k); else u.searchParams.delete('step'); history.replaceState(null, '', u); render(); wfBar(); } },
        label, UA.el('span', { class: 'n', text: String(n) }));
    }), UA.el('div', { class: 'spacer' }),
    !window.IS_ADMIN && window.CAN_UPLOAD && W ? UA.el('button', { type: 'button', class: 'sm', title: 'Your rework first, then what you started, then the files given to you', text: 'Next file ▸', onclick: W.takeNext }) : null].filter(Boolean));
  }
  function errorsOf(j) { return j.score_after ? (j.score_after.errors || 0) : (j.score_before ? (j.score_before.errors || 0) : 0); }
  function shown(jobs) {
    const q = ($('jobSearch').value || '').trim().toLowerCase();
    const st = $('jobStage').value, pf = $('jobProfile').value;
    let list = jobs.filter((j) => {
      if (wfView && !stepOf(j, wfView)) return false;
      if (q && !`${j.filename} ${j.title || ''} ${j.profile || ''} ${j.assigned_to || ''} ${j.qa_user || ''}`.toLowerCase().includes(q)) return false;
      if (pf && (j.profile || 'Default') !== pf) return false;
      if (st === 'active') return active.includes(j.stage);
      if (st === 'review') return j.stage === 'review' || j.stage === 'question';
      if (st === 'done') return j.stage === 'done';
      if (st === 'clean') return j.stage === 'done' && j.score_after && !j.score_after.errors;
      if (st === 'error') return j.stage === 'error';
      return true;
    });
    const by = $('jobSort').value;
    const cmp = { new: (a, b) => (b.created || '').localeCompare(a.created || ''),
      old: (a, b) => (a.created || '').localeCompare(b.created || ''),
      name: (a, b) => (a.filename || '').localeCompare(b.filename || ''),
      pages: (a, b) => (b.pages || 0) - (a.pages || 0),
      errors: (a, b) => errorsOf(b) - errorsOf(a) }[by];
    if (cmp) list = [...list].sort(cmp);
    return list;
  }
  ['jobSearch', 'jobStage', 'jobProfile', 'jobSort'].forEach((id) => $(id).addEventListener(id === 'jobSearch' ? 'input' : 'change', () => { saveFilter(); render(); }));

  async function refresh() {
    clearTimeout(tick);
    jobsCache = await UA.get('/api/jobs' + (batch ? '?batch=' + batch : ''));
    const profs = [...new Set(jobsCache.map((j) => j.profile || 'Default'))].sort();
    const ps = $('jobProfile');
    const cur = ps.value;
    ps.replaceChildren(new Option('Every profile', ''), ...profs.map((p) => new Option(p, p)));
    ps.value = profs.includes(cur) ? cur : '';
    render();
    wfBar();
    dashboard();
    if (jobsCache.some((j) => active.includes(j.stage))) tick = setTimeout(refresh, 1500);
  }

  function render() {
    const jobs = shown(jobsCache);
    const tb = document.querySelector('#jobs tbody');
    tb.innerHTML = '';
    $('emptyJobs').classList.toggle('hidden', jobs.length > 0);
    $('emptyJobs').textContent = jobsCache.length ? 'No file matches.' : 'Nothing yet.';
    jobs.forEach((j, i) => {
      const sb = j.score_before || {}, sa = j.score_after || {};
      const errs = j.score_after ? `${sb.errors} <span class="arrow">→</span> <b class="${sa.errors ? 'bad' : 'good'}">${sa.errors}</b>` :
        (j.score_before ? `${sb.errors}` : '');
      const busy = active.includes(j.stage);
      let msg = j.message || j.stage;
      if (busy && j.position) msg = `waiting — ${j.position === 1 ? 'next' : '#' + j.position} in queue (other files are processing)`;
      const status = busy
        ? `<div class="small">${UA.esc(msg)}</div><div class="bar"><i style="width:${Math.round((j.progress || 0) * 100)}%"></i></div>
           <button class="sm stopbtn" data-id="${j.id}" type="button">Stop</button>`
        : j.stage === 'question'
        ? `<a class="btn sm primary" href="/job/${j.id}">Already tagged — answer</a>`
        : `<span class="chip ${j.stage === 'done' ? 'ok' : j.stage === 'error' ? 'err' : ''}" title="${UA.esc(j.error || '')}">${UA.esc(j.stage === 'done' ? 'built' : j.stage)}</span>` +
          (j.dirty && j.stage === 'done' ? ' <span class="chip warn" title="Edited since the last build">edited</span>' : '') +
          (j.stage === 'error' && j.error ? `<div class="tiny muted">${UA.esc(j.error.slice(0, 140))}</div>` : '');
      const tr = document.createElement('tr');
      tr.className = 'jobrow' + (open.has(j.id) ? ' open' : '');
      tr.style.animationDelay = Math.min(i, 20) * 18 + 'ms';
      const WS = window.WORK_STATES || {};
      const who = window.IS_ADMIN ? (j.assigned_to ? `<div class="tiny">→ ${UA.esc(j.assigned_to)}</div>` : '<div class="tiny muted">pool</div>') : '';
      const canRean = window.CAN_UPLOAD && !busy && (j.can_edit || window.IS_ADMIN);
      const qa = ['qa', 'final_qa'].includes(window.MY_ROLE);
      const todo = { start: 'Start', work: 'Continue', rework: 'Fix the rework', pick: 'Check ▸', check: 'Continue checking', deliver: 'Deliver' }[j.todo];
      tr.innerHTML = `
        <td class="xcell"><button class="sm xbtn" type="button" data-id="${j.id}" aria-expanded="${open.has(j.id)}" title="Details">▸</button></td>
        <td><a href="/job/${j.id}${j.locked_by && !j.lock_mine ? '?view=1' : ''}${qa ? '#review' : ''}"${j.locked_by && !j.lock_mine ? ` title="${UA.esc(j.locked_by)} is working on this file — you can look at it, not change it"` : ''}>${UA.esc(j.filename)}</a>${j.locked_by ? `<span class="lockbadge" title="${j.lock_mine ? 'You have this file open' : UA.esc(j.locked_by) + ' is working on this file now — you can look, not change'}">🔒 ${j.lock_mine ? 'you' : UA.esc(j.locked_by)}</span>` : ''}<div class="tiny muted">${UA.esc((j.created || '').replace('T', ' '))}${j.title ? ' · ' + UA.esc(j.title.slice(0, 60)) : ''}</div></td>
        <td class="stepcell" data-step="${j.id}"></td>
        <td><button type="button" class="pchip pbtn" data-prof="${j.id}" data-fn="${UA.esc(j.filename)}" title="Change the profile of this file"${busy || !window.CAN_UPLOAD ? ' disabled' : ''}>${UA.esc(j.profile || 'Default')}</button>${who}${j.priority && j.priority !== 'normal' ? `<span class="prio p-${UA.esc(j.priority)}">${UA.esc(j.priority)}</span>` : ''}${j.due ? `<div class="tiny muted">due ${UA.esc(j.due)}</div>` : ''}</td>
        <td>${j.pages || ''}</td><td>${UA.esc(j.mode || '')}</td><td>${errs}</td><td>${status}</td>
        <td class="right">
          ${todo && !busy ? `<a class="btn sm primary" href="/job/${j.id}#review" title="It waits for you">${todo}</a>` : ''}
          ${j.can_edit && j.wstage === 'production' && j.stage !== 'question' && !busy && !window.IS_ADMIN ? `<button class="sm qabtn" type="button" data-id="${j.id}" title="Upload the PAC report and send the file to QA">Move to QA…</button>` : ''}
          ${qa ? `<a class="btn sm" href="/job/${j.id}/download/original" title="The PDF as it came in">Input PDF</a>` : ''}
          ${j.built ? `<a class="btn sm" href="/job/${j.id}/download/pdf" title="The accessible PDF">${qa ? 'Output PDF' : 'PDF'}</a> <a class="btn sm" href="/job/${j.id}/download/report">Report</a>` : ''}
          ${canRean ? `<button class="sm reanbtn" type="button" data-id="${j.id}" title="Analyse the whole file again, like at upload (replaces all tags)">Re-analyse</button>` : ''}
          ${window.CAN_UPLOAD && (window.IS_ADMIN || (j.owner || '').toLowerCase() === (window.ME || '').toLowerCase()) ? `<form method="post" action="/job/${j.id}/delete" class="inline"><button class="sm danger">Delete</button></form>` : ''}
        </td>`;
      const sc = tr.querySelector('.stepcell');
      if (W) {
        sc.append(W.strip(j.wstage, j.rework, true));
        sc.append(UA.el('div', {}, W.stateChip(j.work, WS[j.work] || 'not given')));
        const bits = UA.el('div', { class: 'tiny stepbits' });
        if (j.comments_open) bits.append(UA.el('span', { class: 'cbadge', title: 'open review comments', text: `💬 ${j.comments_open}` }));
        if (j.comments_fixed) bits.append(UA.el('span', { class: 'cbadge fx', title: 'marked fixed, waiting for QA to close', text: `✓ ${j.comments_fixed}` }));
        if (j.pac_last) bits.append(UA.el('a', { class: 'cbadge pac ' + (j.pac_last.result || ''), href: `/job/${j.id}/pac/${j.pac - 1}`, title: `Last PAC report: ${j.pac_last.name}${j.pac_last.result ? ' — ' + j.pac_last.result : ''}`, text: 'PAC' + (j.pac_last.result === 'pass' ? ' ✓' : j.pac_last.result === 'fail' ? ' ✗' : '') }));
        if (j.wstage === 'qa' && j.qa_user) bits.append(UA.el('span', { class: 'muted', text: '🔎 ' + j.qa_user }));
        if (j.wstage === 'final_qa' && j.fqa_user) bits.append(UA.el('span', { class: 'muted', text: '🔎 ' + j.fqa_user }));
        if (bits.childNodes.length) sc.append(bits);
      }
      tb.appendChild(tr);
      if (open.has(j.id)) tb.appendChild(detailRow(j));
    });
    tb.querySelectorAll('.xbtn').forEach((b) => b.addEventListener('click', () => {
      const id = b.dataset.id;
      open.has(id) ? open.delete(id) : open.add(id);
      render();
    }));
    tb.querySelectorAll('.reanbtn').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Re-analyse the whole file? All tags and manual edits are replaced (a backup of the current tags is kept).')) return;
      b.disabled = true;
      const r = await fetch('/job/' + b.dataset.id + '/reanalyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.error || 'could not start'); }
      refresh();
    }));
    tb.querySelectorAll('.pbtn').forEach((b) => b.addEventListener('click', () => UA.pickProfile(b.dataset.prof, b.dataset.fn, () => refresh())));
    tb.querySelectorAll('.qabtn').forEach((b) => b.addEventListener('click', () => W && W.submitDialog(b.dataset.id, () => { delete summaries[b.dataset.id]; refresh(); })));
    tb.querySelectorAll('.stopbtn').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Stop this file?')) return;
      b.disabled = true;
      const r = await UA.post('/api/job/' + b.dataset.id + '/stop');
      if (r && r.error) UA.toast(r.error, 4500);
      setTimeout(refresh, 500);
    }));
    // delete: asks first; a file somebody works in is refused (423) with the name — a message, not a page of JSON
    tb.querySelectorAll('form.inline[action$="/delete"]').forEach((f) => f.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!confirm('Delete this job?')) return;
      const r = await fetch(f.action, { method: 'POST', headers: { 'X-Requested-With': 'fetch' } });
      if (r.status === 423 || r.status === 403) { const d = await r.json().catch(() => ({})); alert(d.error || 'This file cannot be deleted now.'); return; }
      refresh();
    }));
  }

  // ---------------------------------------------------------- one file in detail
  const summaries = {};
  const kb = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  function detailRow(j) {
    const tr = document.createElement('tr');
    tr.className = 'detailrow';
    const td = document.createElement('td');
    td.colSpan = 9;
    const box = UA.el('div', { class: 'jdetail' }, UA.el('div', { class: 'small muted', text: 'Loading…' }));
    td.append(box);
    tr.append(td);
    const fill = (s) => box.replaceChildren(...detail(s));
    const have = summaries[j.id];
    if (have && have.stage === j.stage && have.edited === j.edited) { fill(have); return tr; }
    UA.get('/api/job/' + j.id + '/summary').then((s) => { summaries[j.id] = s; fill(s); });
    return tr;
  }
  function detail(s) {
    const el = UA.el;
    const c = s.counts || {};
    const sb = s.score_before || {}, sa = s.score_after || {};
    const pct = (x) => (x && x.percent != null) ? x.percent : null;
    const gauge = (label, x, cls) => el('div', { class: 'gauge ' + cls },
      el('div', { class: 'gring', style: `--p:${pct(x) == null ? 0 : pct(x)}` }, el('span', { text: pct(x) == null ? '—' : pct(x) + '%' })),
      el('div', {}, el('b', { text: label }), el('div', { class: 'tiny muted', text: x && x.checked ? `${x.errors} error(s) · ${x.warnings} warning(s) · ${x.passes}/${x.checked} checks pass` : 'not checked yet' })));
    const stat = (n, label, warn) => el('div', { class: 'mstat' + (warn ? ' warn' : '') }, el('b', { text: String(n == null ? '—' : n) }), el('span', { text: label }));
    const left = el('div', { class: 'jcol' },
      el('div', { class: 'gauges' }, gauge('Before', sb, 'before'), el('span', { class: 'garrow', text: '→' }), gauge('After the build', sa, 'after')),
      el('div', { class: 'mstats' },
        stat(c.tags, 'tags'), stat(c.headings, 'headings'), stat(c.paragraphs, 'paragraphs'),
        stat(c.figures, 'figures'), stat(c.figures_no_alt, 'figures without alt', c.figures_no_alt > 0),
        stat(c.tables, 'tables'), stat(c.lists, 'lists'), stat(c.links, 'links'),
        stat(c.links_by_hand, 'links set by hand'), stat(c.notes, 'notes'), stat(c.formulas, 'formulas'), stat(c.artifacts, 'artifacts')));
    const info = [
      ['Profile', s.profile || 'Default'], ['Mode', s.mode === 'import' ? 'existing tags kept' : 'tagged by the tool'],
      ['Title', s.title || '—'], ['Language', s.lang || '—'], ['Pages', s.pages],
      ['Uploaded', (s.created || '').replace('T', ' ')], ['Last edit', (s.edited || '—').replace('T', ' ')],
      ['Built', (s.built_at || '—').replace('T', ' ')],
      ['Size', `${s.sizes && s.sizes.original ? kb(s.sizes.original) : '—'}${s.sizes && s.sizes.accessible ? ' → ' + kb(s.sizes.accessible) : ''}`],
      ['Contrast', s.contrast ? `${s.contrast.checked || 0} checked · ${s.contrast.open || 0} open · ${s.contrast.fixed || 0} fixed` : '—']];
    const iss = s.issues || {};
    const right = el('div', { class: 'jcol' },
      el('dl', { class: 'kv' }, ...info.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: String(v == null ? '—' : v) })])),
      el('div', { class: 'small' }, el('b', { text: 'Open in the editor: ' }),
        `${iss.error || 0} error(s), ${iss.warning || 0} warning(s)`),
      el('ul', { class: 'jissues' }, ...(iss.top || []).map((i) => el('li', { class: i.severity },
        `${i.page != null ? 'p' + (i.page + 1) + ' · ' : ''}${i.message}`))),
      workBox(s),
      (s.applied || []).length ? el('details', { class: 'small' }, el('summary', { text: `What the build did (${s.applied.length})` }),
        el('ul', { class: 'jissues plain' }, ...s.applied.slice(0, 30).map((a) => el('li', { text: a })))) : null,
      el('div', { class: 'btnrow' },
        el('a', { class: 'btn sm primary', href: '/job/' + s.id, text: 'Open the editor' }),
        el('a', { class: 'btn sm', href: '/job/' + s.id + '#links', text: 'Check its links' }),
        s.built ? el('a', { class: 'btn sm', href: '/job/' + s.id + '/download/pdf', text: 'Accessible PDF' }) : null,
        s.built ? el('a', { class: 'btn sm', href: '/job/' + s.id + '/download/report', text: 'Report (.xlsx)' }) : null,
        el('a', { class: 'btn sm', href: '/job/' + s.id + '/download/original', text: 'Original' })));
    return [left, right];
  }

  function workBox(s) {
    const el = UA.el;
    const WS = s.states || window.WORK_STATES || {};
    const box = el('div', { class: 'workbox' });
    box.append(el('div', { class: 'small wbline' }, el('b', { text: 'Workflow ' }),
      W ? W.strip(s.wstage, s.rework, true) : null,
      el('span', { class: 'wchip w-' + (s.work || 'none'), text: WS[s.work || ''] || '—' }),
      s.assigned_to ? el('span', { class: 'tiny muted', text: ` · production ${s.assigned_to}` }) : null,
      s.qa_user ? el('span', { class: 'tiny muted', text: ` · QA ${s.qa_user}` }) : null,
      s.fqa_user ? el('span', { class: 'tiny muted', text: ` · Final QA ${s.fqa_user}` }) : null,
      s.due ? el('span', { class: 'tiny muted', text: ` · due ${s.due}` }) : null));
    if (s.comments) box.append(el('div', { class: 'tiny' }, `💬 ${s.comments_open} open · ${s.comments_fixed} fixed · ${s.comments} review comment(s)`,
      s.rework ? ` · rework stage-${s.rework}` : ''));
    const act = async (text) => {
      const d = await UA.post('/api/job/' + s.id + '/work', { action: 'note', text });
      if (d.error) { UA.toast(d.error); return; }
      UA.toast('Note saved');
      delete summaries[s.id];
      refresh();
    };
    const row = el('div', { class: 'btnrow' },
      el('a', { class: 'btn sm primary', href: `/job/${s.id}#review`, text: 'Review, comments & history' }),
      el('button', { class: 'sm', type: 'button', text: 'Add a note', onclick: () => { const t = prompt('Note', ''); if (t && t.trim()) act(t); } }));
    box.append(row);
    if ((s.notes || []).length) {
      box.append(el('ul', { class: 'notes' }, ...s.notes.slice(-5).map((n) => el('li', {},
        el('b', { text: n.by }), ` ${(n.at || '').replace('T', ' ').slice(0, 16)}${n.action && n.action !== 'note' ? ' · ' + n.action : ''} — `, n.text))));
    }
    return box;
  }

  // ---------------------------------------------------------- the dashboard
  let dashBusy = false;
  async function dashboard() {
    if (dashBusy) return;
    dashBusy = true;
    let d;
    try { d = await UA.get('/api/dashboard'); } finally { dashBusy = false; }
    if (!d) return;
    const el = UA.el;
    const tile = (cls, big, label, sub) => el('div', { class: 'tile ' + cls }, el('div', { class: 'tbig', 'data-n': big, text: String(big) }),
      el('div', { class: 'tlabel', text: label }), sub ? el('div', { class: 'tsub', text: sub }) : null);
    const rate = d.built ? Math.round(100 * d.clean / d.built) : 0;
    $('tiles').replaceChildren(
      tile('t1', d.files, 'files', `${d.pages.toLocaleString()} pages`),
      tile('t2', d.built, 'built', `${rate}% with 0 errors`),
      tile('t3', d.errors_fixed, 'errors fixed', d.built ? `${d.errors_before} → ${d.errors_after}` : 'nothing built yet'),
      tile('t4', d.review, 'to review', d.active ? `${d.active} working now` : 'none working now'),
      tile('t5' + (d.failed ? ' bad' : ''), d.failed, 'failed', d.failed ? 'open them to see why' : 'all good'));
    $('tiles').querySelectorAll('.tbig').forEach(countUp);
    // two weeks
    const max = Math.max(1, ...d.days.map((x) => Math.max(x.uploaded, x.built)));
    $('spark').replaceChildren(...d.days.map((x, i) => el('div', { class: 'sday', title: `${x.day}: ${x.uploaded} uploaded (${x.pages} pages), ${x.built} built` },
      el('div', { class: 'sbars' },
        el('i', { class: 'up', style: `height:${100 * x.uploaded / max}%;animation-delay:${i * 30}ms` }),
        el('i', { class: 'bu', style: `height:${100 * x.built / max}%;animation-delay:${i * 30 + 15}ms` })),
      el('span', { text: x.day.slice(8) }))));
    const bars = (box, entries, cls) => {
      if (!entries.length) { box.replaceChildren(el('p', { class: 'small muted', text: 'Nothing yet.' })); return; }
      const m = Math.max(1, ...entries.map(([, v]) => v));
      box.replaceChildren(...entries.map(([k, v], i) => el('div', { class: 'hbar' },
        el('span', { class: 'hk', text: k }),
        el('div', { class: 'ht' }, el('i', { class: cls(k), style: `width:${100 * v / m}%;animation-delay:${i * 40}ms` })),
        el('b', { text: String(v) }))));
    };
    const names = { done: 'built', review: 'to review', question: 'tags found — answer', queued: 'waiting', processing: 'analysing', building: 'building', error: 'failed' };
    bars($('stageBars'), Object.entries(d.by_stage).sort((a, b) => b[1] - a[1]).map(([k, v]) => [names[k] || k, v]),
      (k) => ({ built: 'ok', failed: 'err', 'to review': 'warn' }[k] || 'info'));
    bars($('profBars'), Object.entries(d.by_profile), () => 'acc');
  }
  function countUp(node) {
    const n = parseInt(node.dataset.n, 10) || 0;
    if (!n || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const t0 = performance.now(), dur = 700;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur);
      node.textContent = Math.round(n * (1 - Math.pow(1 - k, 3))).toLocaleString();
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  // ---------------------------------------------------------- QA / Final QA: the queue (V4.23)
  if ($('takeNext')) $('takeNext').addEventListener('click', () => W && W.takeNext());
  const fillQ = (q) => document.querySelectorAll('.qtile').forEach((t) => {
    const n = q[t.dataset.k] || 0;
    t.querySelector('b').textContent = String(n);
    t.classList.toggle('hot', n > 0);
  });
  document.addEventListener('wf-queue', (e) => fillQ(e.detail));
  if (window.WFQ) fillQ(window.WFQ);
  document.querySelectorAll('.qtile').forEach((t) => t.addEventListener('click', () => { wfView = 'mine'; render(); wfBar(); }));
  refresh();
})();

// V4.29 — Dez AI on the file list: a tip at a time (never while the pointer is on it, never with "less motion")
(function () {
  const box = document.getElementById('dzSay');
  if (!box) return;
  const tips = [
    'Draw a zone, press a key — the tag is made. Press <kbd>D</kbd>, drag over the text, then <kbd>P</kbd>.',
    'Cut a zone once. I offer the same cut at every place in the file that looks alike — one click, one undo.',
    'Start with the doubtful tags: the Client tab lists them first, the ones my answer would teach most on top.',
    'I learn from every correction a few seconds after you make it — not only when you build.',
    'While somebody works in a file, everybody else can look at it but not change it. Nobody is overwritten.',
    '<kbd>Ctrl</kbd>+<kbd>Z</kbd> takes back the last change — even a whole “Cut all”.',
    'A picture I described once is remembered: the next file gets the same alt text, before I am asked.',
    'Everything runs on this server. I never phone home.'];
  const calm = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  let i = 0, held = false;
  const strip = document.getElementById('dzStrip');
  strip.addEventListener('mouseenter', () => { held = true; });
  strip.addEventListener('mouseleave', () => { held = false; });
  strip.addEventListener('focusin', () => { held = true; });
  strip.addEventListener('focusout', () => { held = false; });
  i = Math.floor(Math.random() * tips.length);
  box.innerHTML = tips[i];
  if (calm) return;
  setInterval(() => {
    if (held || document.hidden) return;
    box.classList.add('out');
    setTimeout(() => { i = (i + 1) % tips.length; box.innerHTML = tips[i]; box.classList.remove('out'); }, 360);
  }, 9000);
})();
