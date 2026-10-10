/* The production workflow on every page (V4.23): Production → QA → Final QA →
   Delivery. The header's queue badge, the step strip, "Move to QA" with the
   PAC report, rework, approve and "take the next file". */
(function () {
  if (!window.UA) return;
  const el = UA.el;
  const STEPS = [['production', 'Production'], ['qa', 'QA'], ['final_qa', 'Final QA'], ['delivery', 'Delivery']];
  const ago = (iso) => {
    if (!iso) return '';
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (!isFinite(s)) return '';
    if (s < 60) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  };
  const dur = (sec) => sec < 60 ? '<1 min' : sec < 3600 ? Math.round(sec / 60) + ' min' : sec < 86400 ? (sec / 3600).toFixed(1).replace('.0', '') + ' h' : (sec / 86400).toFixed(1).replace('.0', '') + ' d';
  const when = (iso) => (iso || '').replace('T', ' ').slice(0, 16);

  /* the four steps as a strip; the file's step lit, the ones behind it done */
  function strip(wstage, rework, small) {
    const at = wstage === 'delivered' ? 4 : STEPS.findIndex(([k]) => k === wstage);
    const box = el('div', { class: 'wfstrip' + (small ? ' sm' : '') + (wstage === 'hold' ? ' hold' : ''), role: 'img',
      'aria-label': wstage === 'hold' ? 'On hold' : `Step: ${(STEPS[at] || ['', 'Delivered'])[1]}` });
    STEPS.forEach(([k, label], i) => {
      if (i) box.append(el('i', { class: 'wfline' + (i <= at ? ' done' : '') }));
      box.append(el('span', { class: 'wfstep' + (i < at ? ' done' : i === at ? ' on' : '') + (k === 'production' && rework && i === at ? ' rw' : ''),
        title: label }, el('b', {}), small ? null : el('em', { text: label })));
    });
    if (rework) box.append(el('span', { class: 'rwbadge', title: `Sent back to production ${rework} time(s)`, text: `Rework ${rework}` }));
    return box;
  }
  function stateChip(work, label) {
    return el('span', { class: 'wchip w-' + (work || 'none'), text: label || work || 'not given' });
  }

  async function step(jid, action, body) {
    const d = await UA.post(`/api/job/${jid}/wf`, Object.assign({ action }, body || {}));
    if (d.error) UA.toast(d.error, 5200);
    return d;
  }

  /* a small modal made once and reused */
  function dialog(id, title) {
    let d = document.getElementById(id);
    if (!d) {
      d = el('dialog', { id, class: 'wfdlg' });
      document.body.append(d);
    }
    d.replaceChildren();
    const body = el('div', { class: 'dlg' }, el('h3', { text: title }));
    const menu = el('menu');
    d.append(body, menu);
    return { d, body, menu };
  }

  /* Production → QA: the PAC report of the built PDF (V4.23) */
  async function submitDialog(jid, onDone) {
    const v = await UA.get(`/api/job/${jid}/wf`);
    if (!v || v.error) { UA.toast((v && v.error) || 'could not load the file'); return; }
    const to = v.rework && v.rework_from === 'final_qa' && v.cfg.return_to_sender && v.cfg.final_qa ? 'Final QA' : 'QA';
    const { d, body, menu } = dialog('wfSubmitDlg', `Move to ${to}`);
    const need = (v.missing || []);
    body.append(
      el('p', { class: 'small muted', text: `${v.filename}${v.rework ? ` · rework stage-${v.rework}` : ''}` }),
      el('ul', { class: 'wfready' },
        el('li', { class: v.built && !v.dirty ? 'ok' : (v.cfg.require_build ? 'no' : 'warn') },
          v.built ? (v.dirty ? 'The tags changed after the last build — build again' : 'Accessible PDF built after the last change') : 'Accessible PDF not built yet'),
        v.rework ? el('li', { class: need.some((m) => /comment/.test(m)) ? 'no' : 'ok',
          text: need.find((m) => /comment/.test(m)) || 'Every QA comment answered (marked fixed)' }) : null,
        v.cfg.require_clean ? el('li', { class: need.some((m) => /tool's check/.test(m)) ? 'no' : 'ok',
          text: need.find((m) => /tool's check/.test(m)) || "The tool's check: 0 errors" }) : null,
        el('li', { class: 'pacline ' + (v.cfg.require_pac ? 'no' : 'warn'), id: 'wfPacLine', text: v.cfg.require_pac ? 'PAC report — needed' : 'PAC report — optional' })));
    const file = el('input', { type: 'file', id: 'wfPac', accept: (v.pac_ext || []).join(',') });
    const res = el('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'PAC result' });
    let result = '';
    [['pass', '✓ Passed'], ['fail', '✗ Failures']].forEach(([k, label]) => {
      const b = el('button', { type: 'button', class: 'sm', 'data-r': k, text: label, onclick: () => {
        result = result === k ? '' : k;
        res.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x.dataset.r === result));
        errs.closest('label').classList.toggle('hidden', result !== 'fail');
      } });
      res.append(b);
    });
    const errs = el('input', { type: 'number', min: '0', id: 'wfPacErr', style: 'width:90px' });
    const note = el('textarea', { rows: '3', id: 'wfNote', placeholder: v.rework ? 'What was fixed in this rework (optional)' : 'Anything the checker should know (optional)' });
    const drop = el('label', { class: 'pacdrop', for: 'wfPac' },
      el('span', { class: 'ic', text: '📄' }),
      el('span', {}, el('b', { text: 'PAC report' }), el('span', { class: 'tiny muted', id: 'wfPacName', text: ' — drop it here or click (PDF, HTML, XML, image, zip)' })));
    drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files.length) { file.files = e.dataTransfer.files; picked(); } });
    function picked() {
      const f = file.files[0];
      document.getElementById('wfPacName').textContent = f ? ` — ${f.name} (${Math.max(1, Math.round(f.size / 1024))} KB)` : '';
      drop.classList.toggle('have', !!f);
      const line = document.getElementById('wfPacLine');
      line.className = 'pacline ' + (f ? 'ok' : (v.cfg.require_pac ? 'no' : 'warn'));
      line.textContent = f ? `PAC report: ${f.name}` : (v.cfg.require_pac ? 'PAC report — needed' : 'PAC report — optional');
      if (f && /pass|ok|compliant/i.test(f.name) && !result) res.querySelector('[data-r=pass]').click();
    }
    file.addEventListener('change', picked);
    const out = el('p', { class: 'small wferr', role: 'alert' });
    body.append(...[drop, file,
      el('div', { class: 'pacres' }, el('span', { class: 'small', text: 'PAC result' }), res,
        el('label', { class: 'inline small hidden' }, 'failures ', errs)),
      v.cfg.pac_must_pass ? el('p', { class: 'tiny muted', text: 'A PAC report with failures does not go to QA — fix them first.' }) : null,
      el('label', {}, 'Note', note),
      (v.pac || []).length ? el('details', { class: 'small' }, el('summary', { text: `Earlier PAC reports (${v.pac.length})` }),
        el('ul', { class: 'paclist' }, ...v.pac.map((p) => el('li', {}, el('a', { href: `/job/${jid}/pac/${p.k}`, text: p.name }),
          ` · round ${p.round + 1} · ${p.by} · ${when(p.at)}`, p.result ? el('span', { class: 'chip ' + (p.result === 'pass' ? 'ok' : 'err'), text: p.result }) : null)))) : null,
      out].filter(Boolean));
    const go = el('button', { class: 'primary', type: 'button', text: `Send to ${to}` });
    go.addEventListener('click', async () => {
      out.textContent = '';
      const fd = new FormData();
      if (file.files[0]) fd.append('pac', file.files[0]);
      fd.append('result', result);
      fd.append('errors', errs.value || '');
      fd.append('note', note.value || '');
      go.disabled = true;
      try {
        const r = await fetch(`/api/job/${jid}/wf/submit`, { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' } });
        const j = await r.json().catch(() => ({ error: 'server error ' + r.status }));
        if (!r.ok || j.error) { out.textContent = j.error || 'could not send'; return; }
        d.close();
        UA.toast(`Sent to ${to} ✓`);
        badge();
        if (onDone) onDone(j);
      } finally { go.disabled = false; }
    });
    menu.append(go, el('button', { type: 'button', text: 'Cancel', onclick: () => d.close() }));
    d.showModal();
  }

  /* QA / Final QA → production: rework stage-N */
  function reworkDialog(jid, v, onDone) {
    const open = (v.comments || []).filter((c) => c.status === 'open').length;
    const { d, body, menu } = dialog('wfReworkDlg', `Send to rework (stage-${(v.rework || 0) + 1})`);
    const note = el('textarea', { rows: '4', placeholder: open ? 'A word for production (optional — the comments say what to fix)' : 'What needs fixing? (needed — or add comments first)' });
    const out = el('p', { class: 'small wferr', role: 'alert' });
    body.append(el('p', { class: 'small', text: open ? `${open} open comment(s) go to production with the file.` : 'No open comment yet.' }),
      el('label', {}, 'Note', note), out);
    const go = el('button', { class: 'primary danger', type: 'button', text: 'Send to rework' });
    go.addEventListener('click', async () => {
      if (!open && !note.value.trim()) { out.textContent = 'Write what needs fixing (or add comments first).'; return; }
      const r = await step(jid, 'rework', { note: note.value });
      if (r.error) { out.textContent = r.error; return; }
      d.close();
      UA.toast(`Rework stage-${r.rework} — back with production`);
      badge();
      if (onDone) onDone(r);
    });
    menu.append(go, el('button', { type: 'button', text: 'Cancel', onclick: () => d.close() }));
    d.showModal();
    note.focus();
  }

  async function approve(jid, v, onDone) {
    const nextName = v.stage === 'qa' && v.cfg.final_qa ? 'Final QA' : 'Delivery';
    const note = prompt(`Approve and send to ${nextName}? A note (optional):`, '');
    if (note === null) return;
    const r = await step(jid, 'approve', { note });
    if (r.error) return;
    UA.toast(`Approved ✓ — now with ${nextName}`);
    badge();
    if (onDone) onDone(r);
  }

  /* take the next file of my queue (and open it) */
  async function takeNext() {
    const r = await UA.post('/api/wf/next', {});
    if (r.error) { UA.toast(r.error); return; }
    if (!r.id) { UA.toast(r.message || 'Nothing waits for you now'); return; }
    location.href = `/job/${r.id}#review`;
  }

  /* the header: what waits for me */
  async function badge() {
    const n = document.getElementById('wfQueueN');
    if (!n) return;
    let q;
    try { q = await UA.get('/api/wf/queue'); } catch (e) { return; }
    if (!q || q.error) return;
    const total = q.total || 0;
    n.textContent = String(total);
    n.classList.toggle('hidden', !total);
    const bits = [];
    if (q.rework) bits.push(`${q.rework} rework`);
    if (q.pick) bits.push(`${q.pick} waiting in the queue`);
    if (q.check) bits.push(`${q.check} you are checking`);
    if (q.start) bits.push(`${q.start} given, not started`);
    if (q.work) bits.push(`${q.work} in progress`);
    if (q.deliver) bits.push(`${q.deliver} ready to deliver`);
    const a = document.getElementById('wfQueueLink');
    if (a) a.title = bits.length ? 'Waiting for you: ' + bits.join(' · ') : 'Nothing waits for you now';
    window.WFQ = q;
    document.dispatchEvent(new CustomEvent('wf-queue', { detail: q }));
  }
  if (document.getElementById('wfQueueN')) {
    badge();
    setInterval(() => { if (!document.hidden) badge(); }, 45000);
  }

  window.WFUI = { strip, stateChip, step, submitDialog, reworkDialog, approve, takeNext, badge, ago, dur, when, STEPS };
})();
