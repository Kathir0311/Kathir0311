/* The editor's Review tab (V4.23): where the file is in Production → QA →
   Final QA → Delivery, the next step for you, the PAC reports, the input and
   output PDFs, the QA checklist, review comments (add, edit — marked
   "edited" —, delete, fixed, reply, close, reopen) and the file's history. */
(function () {
  const pane = document.getElementById('tab-review');
  if (!pane || !window.JOB || !window.WFUI) return;
  const el = UA.el;
  const W = window.WFUI;
  const JID = window.JOB;
  const ED = () => window.ED || null;
  let v = null;
  let filter = 'open';
  let editing = null;
  let shown = false;
  const ICON = { started: '▶', 'QA started': '🔎', 'Final QA started': '🔎', 'QA approved': '✅', 'Final QA approved': '🏁',
    delivered: '📦', 'put on hold': '⏸', 'taken off hold': '▶', 'moved by admin': '↪' };
  const lc = (s) => (s || '').toLowerCase();

  async function load() {
    const d = await UA.get(`/api/job/${JID}/wf`);
    if (!d || d.error) { pane.replaceChildren(el('p', { class: 'small muted', text: (d && d.error) || 'Could not load the workflow.' })); return; }
    use(d);
  }
  function use(d) {
    if (!d || d.error || !d.id) return;
    const was = v;
    v = d;
    if (was && !!was.readonly !== !!v.readonly) setTimeout(() => location.reload(), 900);  // the editor's tools follow
    banner();
    chip();
    count();
    if (shown) render();
  }

  // ---------------------------------------------------------- the read-only banner + header chip
  function banner() {
    document.body.classList.toggle('readonly', !!v.readonly);
    let b = document.getElementById('roBanner');
    if (!v.readonly) { if (b) b.remove(); return; }
    if (!b) {
      b = el('div', { id: 'roBanner', class: 'robanner', role: 'status' });
      const mid = document.querySelector('.editor > section.panel');
      if (mid) { b.classList.add('inpanel'); mid.prepend(b); } else document.body.append(b);
    }
    b.replaceChildren(el('span', { class: 'lock', text: '🔒' }), el('span', { text: 'Read only — ' + v.readonly }),
      el('button', { class: 'sm', type: 'button', text: 'Review ▸', onclick: () => openTab() }));
  }
  function chip() {
    const c = document.getElementById('workChip');
    if (!c) return;
    const full = (v.states[v.work] || v.work || 'not given') + (v.holder ? ` · ${v.holder}` : '');
    c.replaceChildren(...[W.strip(v.stage, v.rework, true),
      el('span', { class: 'wclabel', title: full, text: v.work === 'rework' ? (v.holder || 'production') : full }),
      v.comments.filter((x) => x.status === 'open').length
        ? el('span', { class: 'wnotes', title: 'open review comments', text: '💬 ' + v.comments.filter((x) => x.status === 'open').length }) : null].filter(Boolean));
    c.classList.remove('hidden');
    c.classList.toggle('late', !!(v.due && v.due < new Date().toISOString().slice(0, 10) && !['approved', 'delivered'].includes(v.work)));
    c.title = 'Where the file is in the workflow — click for the Review tab';
  }
  function count() {
    const n = document.getElementById('reviewCount');
    if (!n) return;
    const open = v.comments.filter((x) => x.status === 'open').length;
    n.textContent = String(open);
    n.classList.toggle('hidden', !open);
  }
  function openTab() { const e = ED(); if (e && e.showTab) e.showTab('review'); }

  // ---------------------------------------------------------- the pane
  function render() {
    if (!v) { load(); return; }
    const frag = [head(), actions(), files()];
    if (v.stage === 'qa' || v.stage === 'final_qa' || Object.keys(v.checklist || {}).length) frag.push(checklist());
    frag.push(comments(), history());
    pane.replaceChildren(...frag.filter(Boolean));
  }

  function head() {
    const since = (v.history[v.history.length - 1] || {}).at;
    return el('div', { class: 'rvhead' },
      W.strip(v.stage, v.rework),
      el('div', { class: 'rvstate' },
        W.stateChip(v.work, v.states[v.work]),
        v.holder ? el('span', { class: 'small' }, ' with ', el('b', { text: v.holder })) : null,
        since ? el('span', { class: 'tiny muted', text: ' · since ' + W.ago(since) }) : null,
        v.priority && v.priority !== 'normal' ? el('span', { class: 'prio p-' + v.priority, text: v.priority }) : null,
        v.due ? el('span', { class: 'tiny muted', text: ' · due ' + v.due }) : null),
      el('div', { class: 'tiny muted rvpeople' },
        `Production: ${v.assigned_to || v.owner || '—'} · QA: ${v.qa_user || '—'}` + (v.cfg.final_qa ? ` · Final QA: ${v.fqa_user || '—'}` : '')),
      v.readonly ? el('div', { class: 'rvro small', text: '🔒 ' + v.readonly }) : null);
  }

  function btn(label, cls, fn, title) {
    return el('button', { class: 'sm ' + (cls || ''), type: 'button', text: label, title: title || '', onclick: fn });
  }
  function actions() {
    const c = v.can;
    const row = el('div', { class: 'rvacts' });
    const after = (d) => { if (d && !d.error) use(d); };
    if (c.start) {
      const lab = v.stage === 'production' ? '▶ Start working' : '▶ Start checking';
      row.append(btn(lab, 'primary', async () => { const d = await W.step(JID, 'start'); if (!d.error) { UA.toast(v.stage === 'production' ? 'Started' : 'The file is yours to check'); after(d); } }));
    }
    if (c.submit) {
      const back = v.rework && v.rework_from === 'final_qa' && v.cfg.return_to_sender && v.cfg.final_qa;
      row.append(btn(back ? '➜ Back to Final QA…' : '➜ Move to QA…', 'primary', () => W.submitDialog(JID, after),
        'Upload the PAC report and send the file on'));
    }
    if (c.approve) {
      const to = v.stage === 'qa' && v.cfg.final_qa ? 'Final QA' : 'Delivery';
      row.append(btn(`✓ Approve → ${to}`, 'primary ok', () => W.approve(JID, v, after)));
    }
    if (c.rework) row.append(btn(`↩ Rework stage-${v.rework + 1}…`, 'danger', () => W.reworkDialog(JID, v, after), 'Send the file back to production with the comments'));
    if (c.deliver) row.append(btn('📦 Mark delivered', '', async () => { if (confirm('Mark this file delivered to the client?')) after(await W.step(JID, 'deliver')); }));
    if (!c.edit && ['qa', 'final_qa'].includes(v.role)) row.append(btn('Take the next file ▸', '', W.takeNext, 'Your next file: urgent first, then the due date'));
    const blocks = [row];
    if (v.stage === 'production' && (c.submit || v.role === 'admin') && (v.missing || []).length) {
      blocks.push(el('ul', { class: 'wfready rvneed' }, ...v.missing.map((m) => el('li', { class: 'no', text: m })),
        v.cfg.require_pac ? el('li', { class: 'warn', text: 'the PAC report is asked for when you move the file' }) : null));
    }
    if (c.admin) {
      const mv = el('select', { class: 'sm', title: 'Put the file at any step' }, el('option', { value: '__', text: 'Move to…' }),
        ...Object.entries(v.states).filter(([k]) => !['returned', 'submitted'].includes(k)).map(([k, l]) => el('option', { value: k, text: l || 'not given' })));
      mv.addEventListener('change', async () => {
        const to = mv.value;
        mv.value = '__';
        if (to === '__') return;
        if (to === v.work) return;
        after(await W.step(JID, 'move', { to }));
      });
      const who = (which, list, cur) => {
        const s = el('select', { class: 'sm', title: which === 'qa' ? 'QA checker' : 'Final QA checker' },
          el('option', { value: '', text: (which === 'qa' ? 'QA' : 'Final QA') + ': anyone' }), ...(list || []).map((u) => el('option', { value: u, text: (which === 'qa' ? 'QA: ' : 'Final QA: ') + u })));
        s.value = cur || '';
        s.addEventListener('change', async () => after(await W.step(JID, 'checker', { which, who: s.value })));
        return s;
      };
      blocks.push(el('div', { class: 'rvadmin' }, el('span', { class: 'tiny muted', text: 'Admin' }), mv,
        who('qa', (v.users || {}).qa, v.qa_user), v.cfg.final_qa ? who('final_qa', (v.users || {}).final_qa, v.fqa_user) : null,
        v.work === 'on_hold' ? btn('▶ Off hold', '', async () => after(await W.step(JID, 'unhold')))
          : btn('⏸ Hold', '', async () => { const n = prompt('Why is the file on hold? (optional)', ''); if (n !== null) after(await W.step(JID, 'hold', { note: n })); })));
    }
    return el('div', { class: 'rvbox' }, ...blocks);
  }

  function files() {
    const pac = v.pac || [];
    const last = pac[pac.length - 1];
    return el('div', { class: 'rvbox' },
      el('h4', { text: 'Files' }),
      el('div', { class: 'rvfiles' },
        el('a', { class: 'btn sm', href: `/job/${JID}/download/original`, title: 'The PDF as it came in', text: '⬇ Input PDF' }),
        v.output ? el('a', { class: 'btn sm primary', href: `/job/${JID}/download/pdf`, title: 'The accessible PDF as last built', text: '⬇ Output PDF' })
          : el('span', { class: 'btn sm disabled', title: 'Build it first', text: 'Output PDF — not built' }),
        v.built ? el('a', { class: 'btn sm', href: `/job/${JID}/download/report`, text: 'Report (.xlsx)' }) : null,
        v.dirty && v.built ? el('span', { class: 'chip warn', title: 'The tags changed after the last build', text: 'changed since the build' }) : null),
      el('div', { class: 'rvpac' }, el('b', { class: 'small', text: 'PAC reports ' }),
        pac.length ? null : el('span', { class: 'tiny muted', text: 'none yet — production uploads one with “Move to QA”' }),
        last ? el('span', { class: 'chip ' + (last.result === 'pass' ? 'ok' : last.result === 'fail' ? 'err' : ''), text: last.result ? (last.result === 'pass' ? 'last: passed' : `last: failures${last.errors ? ' ' + last.errors : ''}`) : 'last: result not given' }) : null),
      pac.length ? el('ul', { class: 'paclist' }, ...pac.slice().reverse().map((p) => el('li', {},
        el('a', { href: `/job/${JID}/pac/${p.k}`, text: '📄 ' + p.name }),
        el('span', { class: 'tiny muted', text: ` round ${p.round + 1} · ${p.by} · ${W.when(p.at)}` }),
        p.result ? el('span', { class: 'chip ' + (p.result === 'pass' ? 'ok' : 'err'), text: p.result + (p.errors ? ` (${p.errors})` : '') }) : null))) : null);
  }

  function checklist() {
    const ck = v.checklist || {};
    const items = v.checklist_items || [];
    const done = items.filter(([k]) => ck[k] === 'ok' || ck[k] === 'na').length;
    const may = v.can.checklist;
    const box = el('div', { class: 'rvbox' }, el('h4', {}, `Checklist · ${v.stage_labels[v.stage] || ''} `,
      el('span', { class: 'tiny muted', text: `${done}/${items.length}` + (v.cfg.require_checklist ? ' — needed to approve' : '') })),
      el('div', { class: 'ht thin' }, el('i', { class: done === items.length ? 'ok' : 'acc', style: `width:${items.length ? 100 * done / items.length : 0}%` })));
    const ul = el('ul', { class: 'cklist' });
    items.forEach(([k, label]) => {
      const cur = ck[k] || '';
      const li = el('li', { class: 'ck-' + (cur || 'none') }, el('span', { class: 'cklabel', text: label }));
      if (may) {
        const seg = el('span', { class: 'seg ckseg' });
        [['ok', '✓', 'Checked — right'], ['na', 'n/a', 'Not in this file'], ['fail', '✗', 'Wrong — add a comment']].forEach(([val, t, title]) => {
          seg.append(el('button', { type: 'button', class: 'sm' + (cur === val ? ' on' : ''), text: t, title, onclick: async () => {
            const d = await W.step(JID, 'check', { item: k, value: cur === val ? '' : val });
            if (!d.error) { use(d); if (val === 'fail' && cur !== val) startComment(label); }
          } }));
        });
        li.append(seg);
      } else li.append(el('span', { class: 'ckval', text: { ok: '✓', na: 'n/a', fail: '✗' }[cur] || '—' }));
      ul.append(li);
    });
    box.append(ul);
    return box;
  }

  // ---------------------------------------------------------- comments
  const ORDER = { open: 0, fixed: 1, closed: 2 };
  function comments() {
    const all = v.comments || [];
    const n = (s) => all.filter((c) => c.status === s).length;
    const box = el('div', { class: 'rvbox rvcomments' });
    const seg = el('div', { class: 'seg' }, ...[['open', `Open ${n('open')}`], ['fixed', `Fixed ${n('fixed')}`], ['closed', `Closed ${n('closed')}`], ['all', `All ${all.length}`]]
      .map(([k, t]) => el('button', { type: 'button', class: 'sm' + (filter === k ? ' on' : ''), text: t, onclick: () => { filter = k; render(); } })));
    box.append(el('div', { class: 'rvchead' }, el('h4', { text: 'Review comments' }), el('div', { class: 'spacer' }), seg));
    if (v.can.comment) box.append(addForm());
    const list = all.filter((c) => filter === 'all' || c.status === filter)
      .sort((a, b) => (ORDER[a.status] - ORDER[b.status]) || (b.round - a.round) || (a.at || '').localeCompare(b.at || ''));
    if (!list.length) {
      box.append(el('p', { class: 'small muted', text: all.length ? `No ${filter} comment.` : (v.can.comment ? 'No comment yet — write what production should fix.' : 'No review comment on this file.') }));
    }
    list.forEach((c) => box.append(card(c)));
    return box;
  }

  let draftText = '';
  function addForm() {
    const e = ED();
    const ta = el('textarea', { rows: '3', id: 'rvText', placeholder: 'What should production fix? (Ctrl+Enter adds)' });
    ta.value = draftText;
    ta.addEventListener('input', () => { draftText = ta.value; });
    const cat = el('select', { class: 'sm', id: 'rvCat', title: 'Category' }, ...v.categories.map((x) => el('option', { text: x })));
    const sev = el('select', { class: 'sm', id: 'rvSev', title: 'How bad' }, ...v.severities.map((x) => el('option', { text: x })));
    sev.value = 'Major';
    const pg = e && e.model ? e.page + 1 : null;
    const onPage = el('input', { type: 'checkbox', id: 'rvOnPage', checked: true });
    const tagId = e && e.focus && e.model && e.model.nodes[e.focus] ? e.focus : '';
    const onTag = el('input', { type: 'checkbox', id: 'rvOnTag', checked: !!tagId });
    if (!tagId) onTag.disabled = true;
    const add = async () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); return; }
      const body = { op: 'add', text, category: cat.value, severity: sev.value,
        page: onPage.checked && pg ? pg : null, tag: onTag.checked ? tagId : '' };
      const d = await UA.post(`/api/job/${JID}/wf/comment`, body);
      if (d.error) { UA.toast(d.error, 4500); return; }
      draftText = '';
      filter = 'open';
      UA.toast('Comment added');
      use(d);
    };
    ta.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); add(); } });
    return el('div', { class: 'rvadd' }, ta,
      el('div', { class: 'rvaddrow' }, cat, sev,
        pg ? el('label', { class: 'row tiny', title: 'Pin the comment to the page shown' }, onPage, ` page ${pg}`) : null,
        el('label', { class: 'row tiny', title: tagId ? 'Pin it to the selected tag — a click on the comment selects it' : 'Select a tag first to pin the comment to it' }, onTag,
          tagId ? ` the selected ${(e.model.nodes[tagId] || {}).role || 'tag'}` : ' a tag'),
        el('div', { class: 'spacer' }),
        el('button', { type: 'button', class: 'sm primary', text: 'Add comment', onclick: add })));
  }
  function startComment(label) {
    const ta = document.getElementById('rvText');
    if (!ta) return;
    if (!ta.value.trim()) ta.value = label + ' — ';
    draftText = ta.value;
    ta.focus();
    const m = { 'Every content tagged · decorations are artifacts': 'Tagging', 'Reading order follows the page': 'Reading order',
      'Headings and their levels': 'Headings', 'Alt text of every figure': 'Alt text', 'Tables: header cells, scope, no empty rows': 'Tables',
      'Lists: labels, items, nesting': 'Lists', 'Links, bookmarks, cross-references': 'Links', 'Title, language, document properties': 'Metadata',
      'Colour contrast': 'Contrast' }[label];
    if (m) document.getElementById('rvCat').value = m;
  }

  function card(c) {
    const mine = lc(c.by) === lc(v.me) || v.role === 'admin';
    const box = el('div', { class: `cmt sev-${c.severity} st-${c.status}`, id: 'cmt-' + c.id });
    const go = () => {
      const e = ED();
      if (!e) return;
      if (c.tag && e.model && e.model.nodes[c.tag]) { e.selectTag(c.tag); return; }
      if (c.page) e.setPage(c.page - 1);
    };
    const edMark = c.edited ? el('span', { class: 'edited', tabindex: '0',
      title: `edited${c.edited_by ? ' by ' + c.edited_by : ''} ${W.when(c.edited_at)}` + ((c.versions || []).length ? '\nbefore: ' + c.versions[c.versions.length - 1].text : ''),
      text: 'edited' }) : null;
    box.append(el('div', { class: 'chead' },
      el('span', { class: 'avatar sm', text: (c.by || '?')[0].toUpperCase() }),
      el('b', { text: c.by }), el('span', { class: 'rchip r-' + c.role, text: { qa: 'QA', final_qa: 'Final QA', admin: 'Admin', production: 'Production' }[c.role] || c.role }),
      el('span', { class: 'tiny muted', title: W.when(c.at), text: W.ago(c.at) }),
      el('span', { class: 'rnd', title: `written in review round ${c.round}`, text: 'R' + c.round }),
      el('span', { class: 'cat', text: c.category }), el('span', { class: 'sev', text: c.severity }),
      c.page ? el('button', { type: 'button', class: 'linkish', title: 'Go to the page', text: 'p.' + c.page, onclick: go }) : null,
      c.tag ? el('button', { type: 'button', class: 'linkish', title: 'Select the tag', text: '⌖ tag', onclick: go }) : null,
      el('span', { class: 'spacer' }),
      el('span', { class: 'cst', text: c.status })));
    if (editing === c.id) {
      const ta = el('textarea', { rows: '3' });
      ta.value = c.text;
      const cat = el('select', { class: 'sm' }, ...v.categories.map((x) => el('option', { text: x })));
      cat.value = c.category;
      const sev = el('select', { class: 'sm' }, ...v.severities.map((x) => el('option', { text: x })));
      sev.value = c.severity;
      const save = async () => {
        const d = await UA.post(`/api/job/${JID}/wf/comment`, { op: 'edit', id: c.id, text: ta.value, category: cat.value, severity: sev.value });
        if (d.error) { UA.toast(d.error, 4500); return; }
        editing = null;
        UA.toast(d.comment && d.comment.edited ? 'Saved — marked edited' : 'Nothing changed');
        use(d);
      };
      ta.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); save(); }
        if (ev.key === 'Escape') { ev.stopPropagation(); editing = null; render(); }
      });
      box.append(ta, el('div', { class: 'rvaddrow' }, cat, sev, el('div', { class: 'spacer' }),
        btn('Save', 'primary', save), btn('Cancel', '', () => { editing = null; render(); })));
      setTimeout(() => ta.focus(), 0);
      return box;
    }
    box.append(el('div', { class: 'ctext' }, c.text, edMark ? ' ' : null, edMark));
    (c.replies || []).forEach((r) => box.append(el('div', { class: 'creply' },
      el('b', { text: r.by }), el('span', { class: 'rchip r-' + r.role, text: { qa: 'QA', final_qa: 'Final QA', admin: 'Admin', production: 'Production' }[r.role] || r.role }),
      el('span', { class: 'tiny muted', text: ' ' + W.ago(r.at) + ' — ' }), r.text)));
    const meta = [];
    if (c.fixed_by) meta.push(`fixed by ${c.fixed_by} ${W.ago(c.fixed_at)}`);
    if (c.status === 'closed' && c.closed_by) meta.push(`closed by ${c.closed_by}`);
    if (meta.length) box.append(el('div', { class: 'tiny muted', text: meta.join(' · ') }));
    const acts = el('div', { class: 'cacts' });
    const status = async (status, ask) => {
      let reply = '';
      if (ask) { const t = prompt(ask, ''); if (t === null) return; reply = t; if (status === 'reply' && !reply.trim()) return; }
      const d = await UA.post(`/api/job/${JID}/wf/comment`, { op: 'status', id: c.id, status, reply });
      if (d.error) { UA.toast(d.error, 4500); return; }
      UA.toast({ fixed: 'Marked fixed', open: 'Opened again', closed: 'Closed — verified', reply: 'Reply added' }[status]);
      use(d);
    };
    if (v.can.fix && c.status === 'open') acts.append(btn('✓ Mark fixed', 'ok', () => status('fixed', 'What did you change? (optional)')));
    if (v.can.comment && c.status !== 'closed') acts.append(btn('✔ Close (verified)', '', () => status('closed')));
    if (v.can.comment && c.status !== 'open') acts.append(btn('↺ Reopen', '', () => status('open', 'Why is it not right yet? (optional)')));
    if (v.can.fix || v.can.comment) acts.append(btn('Reply', '', () => status('reply', 'Your reply')));
    if (mine && (v.can.comment || v.role === 'admin')) {
      acts.append(btn('Edit', '', () => { editing = c.id; render(); }));
      acts.append(btn('Delete', 'danger', async () => {
        if (!confirm('Delete this comment?')) return;
        const d = await UA.post(`/api/job/${JID}/wf/comment`, { op: 'delete', id: c.id });
        if (d.error) { UA.toast(d.error, 4500); return; }
        UA.toast('Comment deleted');
        use(d);
      }));
    }
    if (acts.childNodes.length) box.append(acts);
    box.addEventListener('dblclick', (ev) => { if (!ev.target.closest('button, textarea, select, a')) go(); });
    return box;
  }

  // ---------------------------------------------------------- history
  function history() {
    const t = v.times || {};
    const tot = Object.values(t).reduce((a, b) => a + b, 0);
    const bar = tot ? el('div', { class: 'tstages', title: 'Time in each step' }, ...['production', 'qa', 'final_qa', 'delivery', 'hold'].filter((k) => t[k])
      .map((k) => el('i', { class: 'ts-' + k, style: `flex:${t[k]}`, title: `${v.stage_labels[k] || k}: ${W.dur(t[k])}` }))) : null;
    const legend = tot ? el('div', { class: 'tiny muted' }, ...['production', 'qa', 'final_qa', 'delivery', 'hold'].filter((k) => t[k])
      .map((k) => el('span', { class: 'tsl' }, el('i', { class: 'ts-' + k }), ` ${v.stage_labels[k] || k} ${W.dur(t[k])}  `))) : null;
    const ev = (v.history || []).slice().reverse();
    return el('details', { class: 'rvbox rvhist', open: ev.length < 8 ? true : null },
      el('summary', {}, el('h4', { text: `History (${ev.length})` })), bar, legend,
      ev.length ? el('ol', { class: 'timeline small' }, ...ev.map((e) => el('li', {},
        el('span', { class: 'hic', text: ICON[e.action] || (/^rework/.test(e.action) ? '↩' : /^sent to/.test(e.action) ? '➜' : '•') }),
        el('div', {}, el('b', { text: e.by }), el('span', { class: 'tiny muted', text: ` (${{ qa: 'QA', final_qa: 'Final QA', admin: 'Admin', production: 'Production' }[e.role] || e.role})` }),
          ` ${e.action}`, e.pac ? el('span', { class: 'tiny muted', text: ` · PAC: ${e.pac}` }) : null,
          e.comments ? el('span', { class: 'tiny muted', text: ` · ${e.comments} comment(s)` }) : null,
          e.note ? el('div', { class: 'hnote', text: e.note }) : null,
          el('div', { class: 'tiny muted', text: W.when(e.at) + (e.from !== e.to ? ` · ${v.states[e.from] || e.from || 'not given'} → ${v.states[e.to] || e.to}` : '') })))))
        : el('p', { class: 'small muted', text: 'Nothing yet.' }));
  }

  window.REVIEW = {
    show() { shown = true; render(); load(); },
    hide() { shown = false; },
    reload: load,
    get view() { return v; },
  };
  const wc = document.getElementById('workChip');
  if (wc) {
    wc.addEventListener('click', openTab);
    wc.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openTab(); } });
  }
  load().then(() => { if (location.hash === '#review') openTab(); });
  setInterval(() => { if (!document.hidden) load(); }, 60000);
})();
