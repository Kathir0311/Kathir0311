/* Dezquare PDF Accessibility Studio — tag tree + reading-order editor. */
/* The question screen: this PDF already has tags — load them, or analyse it. */
(function askTags() {
  const view = document.getElementById('askView');
  if (!view) return;
  const jid = location.pathname.split('/').filter(Boolean).pop();
  const answer = async (mode, btn) => {
    btn.disabled = true;
    btn.textContent = 'Starting…';
    const r = await UA.post(`/api/job/${jid}/answer`, { mode });
    if (r && r.error) { btn.disabled = false; UA.toast(r.error); return; }
    location.reload();
  };
  document.getElementById('askImport').addEventListener('click', (e) => answer('import', e.target));
  document.getElementById('askRetag').addEventListener('click', (e) => answer('retag', e.target));
  document.getElementById('askAuto').addEventListener('click', (e) => answer('auto', e.target));
})();

(function () {
  const JID = window.JOB;
  const $ = (id) => document.getElementById(id);
  const api = (p) => `/api/job/${JID}${p}`;
  const el = UA.el;

  // ------------------------------------------------ progress / retry views
  function queueText(s) {
    const q = s.queue || {};
    if (!q.position) return '';
    const run = (q.running || []).map((r) => `${r.filename} (${r.message || r.kind})`).join(', ');
    return `Waiting — ${q.position === 1 ? 'next' : '#' + q.position} in the queue.` + (run ? ` Now processing: ${run}` : '');
  }
  if ($('stopBtn')) {
    $('stopBtn').addEventListener('click', async () => {
      if (!confirm('Stop processing this file?')) return;
      $('stopBtn').disabled = true;
      await UA.post(api('/stop'));
    });
  }
  if (window.BUSY) {
    const tick = async () => {
      const s = await UA.get(api('/status'));
      if ($('progMsg')) $('progMsg').textContent = queueText(s) || s.message || s.stage;
      if ($('progSteps')) {
        const m = (s.message || '').toLowerCase(), f = s.progress || 0;
        const at = /build/.test(m) ? 4 : /alt text|contrast|sample/.test(m) || f > 0.88 ? 3 : /page|analys|reading|table|list/.test(m) || f > 0.15 ? 2 : /preflight|ocr|checking/.test(m) || f > 0.02 ? 1 : 0;
        $('progSteps').querySelectorAll('li').forEach((li, i) => { li.classList.toggle('done', i < at); li.classList.toggle('now', i === at); });
      }
      if ($('progBar')) $('progBar').style.width = Math.round((s.progress || 0) * 100) + '%';
      if (['review', 'done', 'error'].includes(s.stage)) location.reload();
      else setTimeout(tick, 1200);
    };
    tick();
    return;
  }
  if ($('retryBtn')) {
    $('retryBtn').addEventListener('click', async () => {
      await fetch(`/job/${JID}/reanalyze`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      location.reload();
    });
    return;
  }
  if (!$('editor')) return;

  // ------------------------------------------------------------ state
  let model = null;
  let issues = [];
  let issuesByNode = {};
  let sel = new Set();
  let focus = null;
  let anchor = null;
  let page = 0;
  let zoom = 1.5;
  let tool = 'select';
  let stage = 'tags';          // tags | links (V4.18: the Links stage, static/links.js)
  let orderClicks = [];
  let flat = [];
  let rowOf = {};
  let collapsed = new Set();
  let contrast = null;         // {available, summary, issues[]}
  let workVer = Date.now() % 100000;   // bumps when colours in the working PDF change
  let cSel = null;
  try { collapsed = new Set(JSON.parse(localStorage.getItem('ua-collapsed-' + JID) || '[]')); } catch (e) { /* private mode */ }
  const saveCollapsed = () => { try { localStorage.setItem('ua-collapsed-' + JID, JSON.stringify([...collapsed])); } catch (e) { /* ignore */ } };

  const treeEl = $('tree'), overlay = $('overlay'), pageimg = $('pageimg');
  const CONTAINERS = new Set(['Document', 'Part', 'Art', 'Sect', 'Div', 'TOC', 'Index', 'NonStruct', 'Private',
    'L', 'LI', 'Table', 'TR', 'THead', 'TBody', 'TFoot', 'Ruby', 'Warichu']);

  // V4.28: a zone cut or joined by hand — the tool looks for the same place elsewhere in the file
  const ZONE_EDITS = /\/(create|geometry|zone\/delete|merge_parts|zones\/split|tags\/remove|move|bulk)$/;
  async function post(path, body) {
    const d = await UA.post(api(path), body);
    if (d.error && !d.confirm) UA.toast(d.error, 4200);
    else if (ZONE_EDITS.test(path)) offerZonesSoon();
    return d;
  }

  // ------------------------------------------------ V4.30: change what changed
  // The answer of a request that changed the tags says exactly what it changed (UA.post keeps it in UA_PATCHES). The
  // editor changes those tags and their rows — not the whole file, the issues, the colours and every row again.
  const PATCHES = (window.UA_PATCHES = window.UA_PATCHES || {});
  function patchChain() {
    if (!model || !rev) return null;
    const chain = [];
    let at = rev;
    for (let i = 0; i < 40 && PATCHES[at]; i++) {
      const p = PATCHES[at];
      delete PATCHES[at];
      if (p.reload || (p.geom && model.geom && p.geom !== model.geom)) return null;      // too much (or the pages changed)
      chain.push(p);
      at = p.rev;
    }
    return chain.length ? chain : null;
  }
  function applyChain(chain) {
    const touched = new Set();
    for (const p of chain) {
      for (const [id, n] of Object.entries(p.nodes || {})) { model.nodes[id] = n; touched.add(id); }
      for (const id of p.removed || []) { delete model.nodes[id]; touched.add(id); }
      for (const [id, v] of Object.entries(p.derived || {})) {
        const n = model.nodes[id];
        if (n) { n.num = v[0]; n.block = v[1]; touched.add(id); }
      }
      Object.assign(model, p.top || {});
      model.can_undo = p.can_undo;
      model.can_redo = p.can_redo;
      model.rev = p.rev;
    }
    rev = model.rev;
    let k = 0;                                   // the reading order is the walk through the tree
    const stack = [...model.roots].reverse();
    while (stack.length) {
      const n = model.nodes[stack.pop()];
      if (!n) continue;
      n.order = ++k;
      for (let i = n.children.length - 1; i >= 0; i--) stack.push(n.children[i]);
    }
    return touched;
  }
  let issT = null, sideT = null;
  function refreshIssuesLater() {              // the issues come a moment after the change, not before the screen shows it
    clearTimeout(issT);
    issT = setTimeout(() => { refreshIssues().then(() => renderTree()); }, 350);
  }
  function refreshSideLater() {
    clearTimeout(sideT);
    sideT = setTimeout(() => { refreshContrast(); refreshXref(); }, 1500);
  }

  async function load(keep) {
    const chain = patchChain();
    const patched = !!chain;
    if (patched) {
      applyChain(chain);
    } else {
      for (const k of Object.keys(PATCHES)) delete PATCHES[k];
      model = await UA.get(api('/model'));
      rev = model.rev || '';
    }
    hideStale();
    $('pageTotal').textContent = model.pages;
    $('undoBtn').disabled = !model.can_undo;
    $('redoBtn').disabled = !model.can_redo;
    $('modeChip').textContent = model.mode === 'import' ? 'existing tags' : 'auto-tagged';
    for (const id of [...sel]) if (!model.nodes[id]) sel.delete(id);
    if (focus && !model.nodes[focus]) focus = null;
    if (keep && model.nodes[keep]) { sel = new Set([keep]); focus = keep; }
    if (patched) {
      refreshIssuesLater();
      refreshSideLater();
    } else {
      await Promise.all([refreshIssues(), refreshContrast()]);
      refreshXref();
    }
    fillRoleFilter();
    renderTree();
    if (patched) drawBoxes(); else renderPage();
    renderInspector();
    if (window.LINKS) window.LINKS.modelLoaded(stage === 'links');
    if (window.READER) window.READER.modelLoaded();
  }

  let fixPolling = false;
  async function refreshContrast() {
    contrast = await UA.get(api('/contrast'));
    if (contrast.task && contrast.task.running && !fixPolling) fixNow(undefined, true);
    const c = $('contrastCount');
    const open = contrast.available ? contrast.issues.filter((i) => i.status !== 'fixed').length : 0;
    c.textContent = open;
    c.classList.toggle('hidden', !open);
    if (!$('tab-contrast').classList.contains('hidden')) renderContrast();
  }

  async function refreshIssues() {
    issues = await UA.get(api('/lint'));
    issuesByNode = {};
    issues.forEach((i) => { if (i.id) (issuesByNode[i.id] = issuesByNode[i.id] || []).push(i); });
    const errs = issues.filter((i) => i.severity === 'error').length;
    const c = $('issueCount');
    c.textContent = errs || issues.length;
    c.classList.toggle('hidden', !issues.length);
    if (!$('tab-issues').classList.contains('hidden')) renderIssues();
  }

  // ------------------------------------------------------------ helpers
  function roleClass(n) {
    if (n.artifact) return 'art';
    if (/^H[1-6]?$/.test(n.role)) return 'h';
    if (['Figure', 'Formula', 'Form'].includes(n.role)) return 'fig';
    if (['Table', 'TR', 'TH', 'TD', 'THead', 'TBody', 'TFoot'].includes(n.role)) return 'tbl';
    if (['L', 'LI', 'Lbl', 'LBody'].includes(n.role)) return 'lst';
    if (['Link', 'Annot', 'Reference'].includes(n.role)) return 'lnk';
    return '';
  }
  const roleName = (n) => n.artifact ? 'Artifact' : n.role;
  // a blank made of several drawn lines reads "Multiple Blank Lines"
  function blankText(n) {
    if (n.actual_text) return n.actual_text;
    const multi = (n.rects || []).length > 1;
    return multi ? (window.MULTI_BLANK_LINE || 'Multiple Blank Lines') : (window.BLANK_LINE || 'Blank Line');
  }
  function labelOf(n) {
    if (n.role === 'Figure' || n.role === 'Formula') return n.alt || '';
    return n.text || '';
  }
  function ancestors(id) {
    const out = [];
    let cur = model.nodes[id] && model.nodes[id].parent;
    while (cur && model.nodes[cur]) { out.push(cur); cur = model.nodes[cur].parent; }
    return out;
  }
  function onPage(n, p) { return (n.rects || []).some((r) => r[0] === p) || n.page === p; }
  const filterFns = {
    all: () => true,
    page: (n) => onPage(n, page),
    issues: (n) => !!issuesByNode[n.id],
    headings: (n) => /^H[1-6]?$/.test(n.role) && !n.artifact,
    figures: (n) => ['Figure', 'Formula'].includes(n.role) && !n.artifact,
    tables: (n) => n.role === 'Table',
    lists: (n) => n.role === 'L',
    links: (n) => ['Link', 'Form', 'Annot'].includes(n.role),
    low: (n) => n.confidence < 0.55 && !n.artifact && n.source !== 'user',
    artifacts: (n) => n.artifact,
  };

  // --------------------------------------------------------------- tree
  function roleOf(n) {
    return n.artifact ? ('Artifact' + (n.pagination_subtype ? ':' + n.pagination_subtype : '')) : n.role;
  }
  function fillRoleFilter() {
    const sel = $('roleFilter');
    if (!sel) return;
    const counts = {};
    Object.values(model.nodes).forEach((n) => { const r = roleOf(n); counts[r] = (counts[r] || 0) + 1; });
    const keep = sel.value;
    const sig = Object.keys(counts).sort().map((r) => r + counts[r]).join('|');
    if (sig === fillRoleFilter.sig) return;                      // the same tag types: the list is as it was
    fillRoleFilter.sig = sig;
    sel.replaceChildren(el('option', { value: '', text: 'Tag type: any' }),
      ...Object.keys(counts).sort().map((r) => el('option', { value: r, text: `${r} (${counts[r]})` })));
    sel.value = [...sel.options].some((o) => o.value === keep) ? keep : '';
  }
  function shownIds() {
    return [...document.querySelectorAll('#tree [role=treeitem]')].map((r) => r.dataset.id)
      .filter((id) => model.nodes[id]);
  }

  // V4.30: a row is made again only when what it shows changed (its signature); the others stay in the page as they are,
  // and a row is moved only when its place changed. A change of one tag touches one row — also in a file of 10 000 tags.
  function rowView(n, depth, hasKids, isCol) {
    let lab = labelOf(n);
    if (lab && n.blank_line) lab = `${lab}  (reads “${blankText(n)}”)`;
    const emptyCell = (n.role === 'TD' || n.role === 'TH') && !lab && !hasKids;
    const text = lab || (emptyCell ? `(empty — reads “${n.actual_text || window.BLANK_TEXT || 'Blank cell'}”)` :
      n.blank_line ? `(${(n.rects || []).length > 1 ? `${n.rects.length} write-in lines` : 'write-in line'} — reads “${blankText(n)}”)` :
      hasKids ? '' : (n.role === 'Figure' ? 'no alt text' : ''));
    const iss = issuesByNode[n.id];
    const v = {
      depth, hasKids, isCol, text, dim: !lab, num: n.num || '', role: roleName(n), rcls: roleClass(n), lang: n.lang || '',
      low: !n.artifact && n.confidence < 0.55 && n.source !== 'user', src: n.source === 'ai' ? 'ai' : n.source === 'user' ? 'user' : '',
      issN: iss ? iss.length : 0, issErr: iss ? iss.some((i) => i.severity === 'error') : false, issCode: iss ? iss[0].code : '',
    };
    v.sig = [depth, hasKids ? 1 : 0, isCol ? 1 : 0, v.low ? 1 : 0, v.num, v.role, v.rcls, v.text, v.dim ? 1 : 0, v.lang, v.src,
      v.issN, v.issErr ? 1 : 0, v.issCode].join('\u0001');
    return v;
  }
  function buildRow(id, v) {
    const row = el('div', {
      class: 'node' + (v.low ? ' low' : ''),
      draggable: 'true', role: 'treeitem', 'aria-level': v.depth + 1,
      'aria-expanded': v.hasKids ? String(!v.isCol) : false,
    });
    row.dataset.id = id;
    row.style.paddingLeft = (4 + v.depth * 14) + 'px';
    row.append(el('span', { class: 'tw', text: v.hasKids ? (v.isCol ? '▸' : '▾') : '' }));
    row.append(el('span', { class: 'ord', text: v.num, title: v.num ? 'Reading position — click again (or double-click) to change' : '' }));
    row.append(el('span', { class: 'role ' + v.rcls, text: v.role }));
    row.append(el('span', { class: 'ntext' + (v.dim ? ' dim' : ''), text: v.text }));
    if (v.lang) row.append(el('span', { class: 'badge ai', text: v.lang }));
    if (v.issN) row.append(el('span', { class: 'badge ' + (v.issErr ? 'err' : ''), text: v.issN > 1 ? `${v.issN} issues` : (v.issCode === 'alt' ? 'alt?' : v.issCode) }));
    if (v.src === 'ai') row.append(el('span', { class: 'badge ai', text: 'AI' }));
    else if (v.src === 'user') row.append(el('span', { class: 'badge user', text: '✓' }));
    row._sig = v.sig;
    return row;
  }
  function renderTree() {
    const f = $('filter').value;
    const rf = ($('roleFilter') || {}).value || '';
    const q = $('search').value.trim().toLowerCase();
    const showArt = $('showArtifacts').checked || f === 'artifacts' || rf.startsWith('Artifact');
    let include = null;
    if (f !== 'all' || q || rf) {
      include = new Set();
      const fn = filterFns[f] || filterFns.all;
      for (const n of Object.values(model.nodes)) {
        if (!fn(n)) continue;
        if (rf && roleOf(n) !== rf) continue;
        if (q && !((n.text || '') + ' ' + (n.alt || '') + ' ' + n.role).toLowerCase().includes(q)) continue;
        include.add(n.id);
        ancestors(n.id).forEach((a) => include.add(a));
        if (['tables', 'lists'].includes(f) && !rf) {
          const stack = [...n.children];
          while (stack.length) {
            const c = stack.pop();
            if (!model.nodes[c]) continue;
            include.add(c);
            stack.push(...model.nodes[c].children);
          }
        }
      }
    }
    const prev = rowOf;
    const next = {};
    const wanted = [];
    flat = [];
    const walk = (id, depth) => {
      const n = model.nodes[id];
      if (!n) return;
      if (n.artifact && !showArt) return;
      if (include && !include.has(id)) return;
      flat.push(id);
      const hasKids = n.children.length > 0;
      const isCol = collapsed.has(id) && !include;
      const v = rowView(n, depth, hasKids, isCol);
      let row = prev[id];
      if (!row || row._sig !== v.sig) row = buildRow(id, v);
      row.classList.toggle('sel', sel.has(id));
      row.classList.toggle('focus', id === focus);
      next[id] = row;
      wanted.push(row);
      if (!isCol) n.children.forEach((c) => walk(c, depth + 1));
    };
    model.roots.forEach((r) => walk(r, 0));
    rowOf = next;
    for (const id in prev) if (next[id] !== prev[id]) prev[id].remove();      // rows that are gone or were made again
    let ref = treeEl.firstChild;
    for (const row of wanted) {
      if (row === ref) ref = ref.nextSibling;
      else treeEl.insertBefore(row, ref);
    }
    while (ref) { const nx = ref.nextSibling; ref.remove(); ref = nx; }        // what is left over (the "nothing matches" line …)
    if (!flat.length) treeEl.append(el('p', { class: 'muted small', style: 'padding:10px', text: 'Nothing matches.' }));
    selMarked = new Set(sel);
    focusMarked = focus;
  }

  // only the rows whose mark changed are touched (was: every row of the file at every click)
  let selMarked = new Set(), focusMarked = null;
  function updateSelectionClasses() {
    for (const id of selMarked) if (!sel.has(id) && rowOf[id]) rowOf[id].classList.remove('sel');
    for (const id of sel) if (rowOf[id]) rowOf[id].classList.add('sel');
    if (focusMarked && focusMarked !== focus && rowOf[focusMarked]) rowOf[focusMarked].classList.remove('focus');
    if (focus && rowOf[focus]) rowOf[focus].classList.add('focus');
    selMarked = new Set(sel);
    focusMarked = focus;
  }

  treeEl.addEventListener('dblclick', (e) => {
    if (!e.target.classList.contains('ord')) return;
    const row = e.target.closest('.node');
    editOrderInline(row.dataset.id, e.target, 'ordin');
  });
  treeEl.addEventListener('click', (e) => {
    const row = e.target.closest('.node');
    if (!row || e.target.tagName === 'INPUT') return;
    if (e.target.classList.contains('ord') && row.dataset.id === focus && sel.size === 1 && model.nodes[focus].num) {
      editOrderInline(focus, e.target, 'ordin');
      return;
    }
    const id = row.dataset.id;
    if (e.target.classList.contains('tw') && model.nodes[id].children.length) {
      collapsed.has(id) ? collapsed.delete(id) : collapsed.add(id);
      saveCollapsed();
      renderTree();
      return;
    }
    clickSelect(id, e, true);
  });

  function clickSelect(id, e, jump) {
    if (window.TBL && window.TBL.marking && window.TBL.treePick(id, e)) return;
    if (e && e.shiftKey && anchor && flat.includes(anchor) && flat.includes(id)) {
      const a = flat.indexOf(anchor), b = flat.indexOf(id);
      sel = new Set(flat.slice(Math.min(a, b), Math.max(a, b) + 1));
    } else if (e && (e.ctrlKey || e.metaKey)) {
      sel.has(id) ? sel.delete(id) : sel.add(id);
      anchor = id;
    } else {
      sel = new Set([id]);
      anchor = id;
    }
    focus = id;
    afterSelect(jump);
  }

  function selectOnly(id, jump) {
    if (!model.nodes[id]) return;
    // make sure it is visible in the tree
    let changed = false;
    ancestors(id).forEach((a) => { if (collapsed.delete(a)) changed = true; });
    if (changed) { saveCollapsed(); renderTree(); }
    sel = new Set([id]);
    focus = id;
    anchor = id;
    afterSelect(jump);
  }

  function afterSelect(jump) {
    const n = model.nodes[focus];
    if (jump && n) {
      const pages = (n.rects || []).map((r) => r[0]);
      const target = pages.length ? (pages.includes(page) ? page : pages[0]) : (n.page >= 0 ? n.page : page);
      if (target !== page) { page = target; renderPage(); }
    }
    if (!rowOf[focus]) renderTree();
    updateSelectionClasses();
    drawBoxes();
    renderInspector();
    const row = rowOf[focus];
    if (row) row.scrollIntoView({ block: 'nearest' });
    const box = overlay.querySelector(`.box[data-id="${CSS.escape(focus || '')}"]`);
    if (box && jump) box.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  // drag & drop in the tree
  let dragIds = [];
  treeEl.addEventListener('dragstart', (e) => {
    const row = e.target.closest('.node');
    if (!row) return;
    const id = row.dataset.id;
    if (!sel.has(id)) { sel = new Set([id]); focus = id; updateSelectionClasses(); }
    dragIds = [...sel].sort((a, b) => model.nodes[a].order - model.nodes[b].order);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', dragIds.join(','));
  });
  function dropMode(e, row) {
    if (e.shiftKey) return 'into';
    const r = row.getBoundingClientRect();
    return (e.clientY - r.top) < r.height / 2 ? 'before' : 'after';
  }
  treeEl.addEventListener('dragover', (e) => {
    const row = e.target.closest('.node');
    if (!row || dragIds.includes(row.dataset.id)) return;
    e.preventDefault();
    treeEl.querySelectorAll('.dropbefore,.dropinto').forEach((x) => x.classList.remove('dropbefore', 'dropinto'));
    const m = dropMode(e, row);
    if (m === 'into') row.classList.add('dropinto');
    else if (m === 'before') row.classList.add('dropbefore');
    else if (row.nextElementSibling) row.nextElementSibling.classList.add('dropbefore');
  });
  treeEl.addEventListener('dragend', () => treeEl.querySelectorAll('.dropbefore,.dropinto').forEach((x) => x.classList.remove('dropbefore', 'dropinto')));
  treeEl.addEventListener('drop', async (e) => {
    const row = e.target.closest('.node');
    treeEl.querySelectorAll('.dropbefore,.dropinto').forEach((x) => x.classList.remove('dropbefore', 'dropinto'));
    if (!row || !dragIds.length || dragIds.includes(row.dataset.id)) return;
    e.preventDefault();
    const res = await post('/move', { action: 'drop', ids: dragIds, target: row.dataset.id, where: dropMode(e, row) });
    dragIds = [];
    if (!res.error) await load(focus);
  });

  // --------------------------------------------------------------- page
  // the page fades in once its picture is there (V4.21)
  if (pageimg) {
    const done = () => { pagewrapEl().classList.remove('loading'); pagewrapEl().classList.add('loaded'); setTimeout(() => pagewrapEl().classList.remove('loaded'), 400); };
    pageimg.addEventListener('load', done);
    pageimg.addEventListener('error', done);
  }
  function pagewrapEl() { return document.getElementById('pagewrap') || pageimg.parentElement; }
  function loadPageImage() {
    const out = $('showOut').checked && window.META.built;
    const next = `/job/${JID}/page/${page}.png?z=${Math.round(zoom * 1000) / 1000}${out ? '&v=out' : ''}&w=${workVer}`;
    if (pageimg.getAttribute('src') !== next) pagewrapEl().classList.add('loading');
    pageimg.src = `/job/${JID}/page/${page}.png?z=${Math.round(zoom * 1000) / 1000}${out ? '&v=out' : ''}&w=${workVer}`;
  }

  function sizePage() {
    const [w, h] = model.page_sizes[page] || [595, 842];
    pageimg.width = Math.round(w * zoom);
    pageimg.height = Math.round(h * zoom);
  }

  function renderPage() {
    if (zoomMode !== 'fixed') zoom = fitZoom(zoomMode);
    loadPageImage();
    sizePage();
    syncZoomSelect();
    $('pageNo').value = page + 1;
    if ($('filter').value === 'page') renderTree();
    drawBoxes();
  }

  // ---------------------------------------------------------------- zoom
  // Fit page / Fit width, Ctrl + / Ctrl − / Ctrl 0, Ctrl + mouse wheel or a
  // touchpad pinch (at the pointer), and dragging with the right mouse button
  // (up: in, down: out); the middle button drags the page around.
  const pageview = $('pageview'), pagewrap = $('pagewrap');
  const ZMIN = 0.25, ZMAX = 8.0;          // up to 800 % (V4.21.1)
  const ZSTEPS = [0.25, 0.33, 0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8];
  let zoomMode = 'fixed';          // fixed | page | width
  let zoomTimer = null;
  try {
    const saved = JSON.parse(localStorage.getItem('ua.zoom') || 'null');
    if (saved && ['page', 'width'].includes(saved.mode)) zoomMode = saved.mode;
    else if (saved && saved.z >= ZMIN && saved.z <= ZMAX) zoom = saved.z;
  } catch (e) { /* no storage: the default zoom */ }

  function fitZoom(mode) {
    const [w, h] = (model && model.page_sizes[page]) || [595, 842];
    const cs = getComputedStyle(pageview);
    const aw = pageview.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - 6;
    const ah = pageview.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - 6;
    const z = mode === 'width' ? aw / w : Math.min(aw / w, ah / h);
    return Math.max(ZMIN, Math.min(ZMAX, Math.floor(z * 1000) / 1000));
  }

  function syncZoomSelect() {
    const zs = $('zoom');
    if (zoomMode === 'page') { zs.value = 'fit'; return; }
    if (zoomMode === 'width') { zs.value = 'width'; return; }
    let opt = [...zs.options].find((o) => !o.dataset.custom && Math.abs(parseFloat(o.value) - zoom) < 0.0005);
    if (!opt) {
      opt = zs.querySelector('option[data-custom]');
      if (!opt) { opt = document.createElement('option'); opt.dataset.custom = '1'; zs.appendChild(opt); }
      opt.value = String(zoom);
      opt.textContent = Math.round(zoom * 100) + '%';
    }
    zs.value = opt.value;
  }

  function setZoom(z, at, mode) {
    zoomMode = mode || 'fixed';
    z = Math.max(ZMIN, Math.min(ZMAX, Math.round(z * 1000) / 1000));
    const old = zoom;
    const vr = pageview.getBoundingClientRect();
    const cx = at ? at.x : vr.left + pageview.clientWidth / 2;
    const cy = at ? at.y : vr.top + pageview.clientHeight / 2;
    const wr = pagewrap.getBoundingClientRect();
    const px = (cx - wr.left) / old, py = (cy - wr.top) / old;   // page point under the pointer
    zoom = z;
    sizePage();
    drawBoxes();
    const nr = pagewrap.getBoundingClientRect();
    if (mode === 'page' || mode === 'width') {
      pageview.scrollTop = 0;
      if (mode === 'page') pageview.scrollLeft = 0;
    } else {
      pageview.scrollLeft += (nr.left + px * z) - cx;
      pageview.scrollTop += (nr.top + py * z) - cy;
    }
    syncZoomSelect();
    try { localStorage.setItem('ua.zoom', JSON.stringify({ mode: zoomMode, z: zoom })); } catch (e) { /* ignore */ }
    clearTimeout(zoomTimer);           // the sharp image once the zoom settles
    zoomTimer = setTimeout(loadPageImage, 180);
  }

  function stepZoom(dir, at) {
    let z = dir > 0 ? ZSTEPS.find((v) => v > zoom + 0.001) : [...ZSTEPS].reverse().find((v) => v < zoom - 0.001);
    if (z === undefined) z = dir > 0 ? ZMAX : ZMIN;
    setZoom(z, at);
  }

  pageview.addEventListener('wheel', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    // a mouse wheel notch is about 100; a touchpad pinch sends small steps
    const k = Math.abs(e.deltaY) < 40 ? 0.012 : 0.0022;
    setZoom(zoom * Math.exp(-e.deltaY * k), { x: e.clientX, y: e.clientY });
  }, { passive: false });

  let zdyn = null, pan = null, noMenu = false;
  pageview.addEventListener('mousedown', (e) => {
    if (e.button === 2) {
      zdyn = { y: e.clientY, z: zoom, at: { x: e.clientX, y: e.clientY }, moved: false };
    } else if (e.button === 1) {
      e.preventDefault();
      pan = { x: e.clientX, y: e.clientY, sl: pageview.scrollLeft, st: pageview.scrollTop };
      pageview.classList.add('panning');
    }
  });
  window.addEventListener('mousemove', (e) => {
    if (zdyn) {
      const dy = zdyn.y - e.clientY;
      if (!zdyn.moved && Math.abs(dy) < 4) return;
      zdyn.moved = true;
      noMenu = true;
      setZoom(zdyn.z * Math.exp(dy / 180), zdyn.at);
    }
    if (pan) {
      pageview.scrollLeft = pan.sl - (e.clientX - pan.x);
      pageview.scrollTop = pan.st - (e.clientY - pan.y);
    }
  });
  window.addEventListener('mouseup', (e) => {
    if (zdyn && e.button === 2) { if (zdyn.moved) noMenu = true; zdyn = null; }
    if (pan && e.button === 1) { pan = null; pageview.classList.remove('panning'); }
  });
  pageview.addEventListener('contextmenu', (e) => {
    // a right-button drag zooms; no menu after it
    if (noMenu || (zdyn && zdyn.moved)) { e.preventDefault(); noMenu = false; }
  });
  window.addEventListener('resize', () => {
    if (model && zoomMode !== 'fixed') setZoom(fitZoom(zoomMode), null, zoomMode);
  });

  let drawnPage = null;
  function staggerBoxes() {
    if (drawnPage === page) return;
    drawnPage = page;
    overlay.classList.add('boxes-in');
    overlay.querySelectorAll('.box').forEach((b, i) => { if (i < 90) b.style.setProperty('--i', String(i)); });
    clearTimeout(staggerBoxes.t);
    staggerBoxes.t = setTimeout(() => overlay.classList.remove('boxes-in'), 1200);
  }
  function drawBoxes() {
    if (stage === 'links' && window.LINKS) {
      overlay.replaceChildren();
      overlay.className = 'overlay links';
      window.LINKS.draw(overlay);
      return;
    }
    overlay.replaceChildren();
    overlay.className = 'overlay' + (tool === 'draw' ? ' draw' : tool === 'order' ? ' order' : '') +
      ($('showLabels').checked ? ' labels' : '');
    if (!$('showBoxes').checked && tool !== 'order') { drawContrast(); return; }
    const showArt = $('showArtifacts').checked;
    const showOrd = $('showOrder').checked;
    const items = [];
    for (const n of Object.values(model.nodes)) {
      if (n.artifact && !showArt) continue;
      for (const r of n.rects || []) {
        if (r[0] !== page) continue;
        const a = (r[3] - r[1]) * (r[4] - r[2]);
        items.push({ n, r, a, ctr: CONTAINERS.has(n.role) && !n.artifact });
      }
    }
    items.sort((x, y) => (y.ctr - x.ctr) || (y.a - x.a));
    const frag = document.createDocumentFragment();
    for (const { n, r, ctr } of items) {
      if (tool === 'order' && ctr) continue;
      const [, x0, y0, x1, y1] = r;
      const d = el('div', { class: 'box ' + roleClass(n) + (ctr ? ' ctr' : '') + (sel.has(n.id) ? ' sel' : '') });
      d.style.left = (x0 * zoom) + 'px';
      d.style.top = (y0 * zoom) + 'px';
      d.style.width = Math.max(3, (x1 - x0) * zoom) + 'px';
      d.style.height = Math.max(3, (y1 - y0) * zoom) + 'px';
      d.dataset.id = n.id;
      d.append(el('span', { class: 'tag', text: roleName(n) }));
      d.title = roleName(n) + (labelOf(n) ? ': ' + labelOf(n).slice(0, 120) : '');
      if (showOrd && n.block && (r[5] == null || r[5] < 0)) d.append(el('span', { class: 'ordn', text: n.block, title: 'Reading position — click to change' }));
      d.dataset.part = r[5] == null ? -1 : r[5];
      if (tool === 'select' && sel.size === 1 && sel.has(n.id)) {
        d.classList.add('editing');
        ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].forEach((h) => d.append(el('span', { class: 'hdl ' + h, 'data-h': h })));
      }
      const ci = orderClicks.indexOf(n.id);
      if (ci >= 0) { d.classList.add('clicked'); d.append(el('span', { class: 'clickn', text: ci + 1 })); }
      frag.append(d);
    }
    overlay.append(frag);
    staggerBoxes();
    drawContrast();
    drawMissed();
    if (window.TBL && window.TBL.active) window.TBL.draw(overlay);
    if (window.LST && window.LST.active) window.LST.draw(overlay);
    if (window.READER) window.READER.draw(overlay);
  }

  // ------------------------------------------- V4.26: the PDF data tab — nothing missed
  const dataView = { on: false, inv: null, drawn: false, sel: -1 };
  function drawMissed() {
    dataView.drawn = false;
    if (!dataView.on || !dataView.inv) return;
    const cov = (dataView.inv.inventory || {}).coverage || {};
    const frag = document.createDocumentFragment();
    (cov.missed || []).forEach((m, k) => {
      if (m.page !== page) return;
      const [x0, y0, x1, y1] = m.bbox;
      const d = el('div', { class: 'mbox' + (dataView.sel === k ? ' flash' : ''), title: `No tag holds this line: ${m.text}` });
      d.style.left = (x0 * zoom - 2) + 'px'; d.style.top = (y0 * zoom - 2) + 'px';
      d.style.width = ((x1 - x0) * zoom + 4) + 'px'; d.style.height = ((y1 - y0) * zoom + 4) + 'px';
      frag.append(d);
    });
    ((cov.pictures || {}).large_decoration || []).forEach((m) => {
      if (m.page !== page) return;
      const [x0, y0, x1, y1] = m.bbox;
      const d = el('div', { class: 'mbox pic', title: 'A large picture left as decoration — is it a Figure?' });
      d.style.left = (x0 * zoom) + 'px'; d.style.top = (y0 * zoom) + 'px';
      d.style.width = ((x1 - x0) * zoom) + 'px'; d.style.height = ((y1 - y0) * zoom) + 'px';
      frag.append(d);
    });
    overlay.append(frag);
    dataView.drawn = true;
  }
  async function renderData(refresh) {
    const box = $('tab-data');
    box.replaceChildren(el('div', { class: 'skel-lines' }, el('i'), el('i'), el('i')));
    const r = refresh ? await post('/inventory', {}) : await UA.get(api('/inventory'));
    if (!r || r.error) { box.replaceChildren(el('p', { class: 'small', text: (r && r.error) || 'Could not read the file.' })); return; }
    dataView.inv = r;
    const inv = r.inventory || {}, sm = r.summary || {}, cov = inv.coverage || {}, doc = inv.document || {}, org = inv.original || {};
    const missed = cov.missed || [];
    const bigPics = (cov.pictures || {}).large_decoration || [];
    $('dataCount').textContent = String(missed.length + bigPics.length);
    $('dataCount').classList.toggle('hidden', !(missed.length + bigPics.length));
    box.replaceChildren();
    const pct = cov.percent != null ? cov.percent : 100;
    box.append(el('div', { class: 'brainbox' },
      el('div', { class: 'brow' },
        el('div', {}, el('div', { class: 'bq', text: `${pct} %` }),
          el('div', { class: 'tiny muted', text: `of the ${cov.lines || 0} printed lines are in a tag or an artifact` })),
        el('div', { class: 'spacer' }),
        el('button', { class: 'sm', text: '↻ Check again', title: 'Read the page again and match every line, picture, link and field with the tags as they are now', onclick: () => renderData(true) })),
      el('div', { class: 'bfacts' },
        el('span', { class: 'chip ' + (missed.length ? 'warn' : 'ok'), text: missed.length ? `${cov.missed_count} line(s) no tag holds` : 'every line is tagged' }),
        el('span', { class: 'chip', text: `pictures: ${(cov.pictures || {}).figure || 0} figure · ${(cov.pictures || {}).artifact || 0} artifact · ${(cov.pictures || {}).decoration || 0} decoration` }),
        el('span', { class: 'chip', text: `links ${(cov.links || {}).tags || 0} tag(s) / ${(cov.links || {}).annotations || 0} in the file` }),
        (cov.fields || {}).widgets ? el('span', { class: 'chip', text: `fields ${(cov.fields || {}).tags || 0} / ${(cov.fields || {}).widgets}` }) : '')));
    if (missed.length) {
      const sec = el('div', { class: 'datasec' }, el('h3', { text: 'Printed but in no tag' }),
        el('div', { class: 'btnrow' },
          el('button', { class: 'sm primary', text: `Tag all as P (${missed.length})`, onclick: () => tagMissed(missed, 'P') }),
          el('button', { class: 'sm', text: `All as artifacts (${missed.length})`, title: 'Decoration, a running head, a page number…', onclick: () => tagMissed(missed, 'Artifact') })));
      missed.slice(0, 120).forEach((m, k) => sec.append(el('div', { class: 'citem' + (dataView.sel === k ? ' sel' : ''), onclick: () => {
        dataView.sel = k; if (page !== m.page) { page = m.page; renderPage(); } else drawBoxes();
      } }, el('div', {}, el('div', { class: 'small', text: m.text || '(no text)' }), el('div', { class: 'cmeta', text: `p${m.page + 1} · ${m.size}pt` })),
        el('div', { class: 'btnrow' },
          el('button', { class: 'sm', text: 'P', title: 'Tag this line as a paragraph', onclick: (e) => { e.stopPropagation(); tagMissed([m], 'P'); } }),
          el('button', { class: 'sm', text: 'Auto', title: 'Let the tool (and the AI, when it is on) decide what it is', onclick: (e) => { e.stopPropagation(); tagMissed([m], 'Auto'); } }),
          el('button', { class: 'sm', text: 'A', title: 'Artifact', onclick: (e) => { e.stopPropagation(); tagMissed([m], 'Artifact'); } })))));
      box.append(sec);
    }
    if (bigPics.length) {
      const sec = el('div', { class: 'datasec' }, el('h3', { text: 'Large pictures left as decoration' }),
        el('p', { class: 'tiny muted', text: 'Read by nobody. If one carries information, make it a Figure (it then needs alt text).' }));
      bigPics.forEach((m) => sec.append(el('div', { class: 'citem', onclick: () => { if (page !== m.page) { page = m.page; renderPage(); } } },
        el('div', {}, el('div', { class: 'small', text: `p${m.page + 1} · ${Math.round(m.share * 100)} % of the page` })),
        el('button', { class: 'sm', text: 'Make Figure', onclick: (e) => { e.stopPropagation(); tagMissed([m], 'Figure'); } }))));
      box.append(sec);
    }
    const F = inv.findings || [];
    if (F.length) {
      const sec = el('div', { class: 'datasec' }, el('h3', { text: 'What the file itself says' }));
      F.forEach((f) => sec.append(el('div', { class: 'small dfind ' + f.level, text: (f.level === 'error' ? '✗ ' : f.level === 'warning' ? '! ' : '• ') + f.text
        + (f.pages && f.pages.length ? ` (p. ${f.pages.slice(0, 8).map((x) => x + 1).join(', ')}${f.pages.length > 8 ? '…' : ''})` : '') })));
      box.append(sec);
    }
    const fact = (k, v) => (v === undefined || v === null || v === '') ? null : el('div', { class: 'fact' }, el('span', { text: k }), el('b', { text: String(v) }));
    const yes = (b) => b ? 'yes' : 'no';
    box.append(el('div', { class: 'datasec' }, el('h3', { text: 'The file' }), el('div', { class: 'facts' }, ...[
      fact('Pages', doc.pages), fact('PDF version', doc.version), fact('Objects', doc.objects),
      fact('Tagged when it came in', org.tagged != null ? yes(org.tagged) + (org.struct && org.struct.elements ? ` (${org.struct.elements} elements)` : '') : null),
      fact('Language when it came in', org.lang || (org.tagged != null ? 'none' : null)),
      fact('Fonts', `${sm.fonts}${sm.fonts_not_embedded ? ` · ${sm.fonts_not_embedded} not embedded` : ''}`),
      fact('Pictures', `${sm.images} different · drawn ${sm.image_uses}×`), fact('Characters', (sm.chars || 0).toLocaleString()),
      fact('Words', (sm.words || 0).toLocaleString()), fact('Invisible text', sm.invisible || 0), fact('Unreadable characters', sm.unreadable || 0),
      fact('Scanned pages', sm.scanned || 0), fact('Vector drawings', (sm.drawings || 0).toLocaleString()),
      fact('Links', Object.entries(sm.links || {}).map(([k, v]) => `${k} ${v}`).join(' · ')),
      fact('Annotations', Object.entries(sm.annots || {}).map(([k, v]) => `${k} ${v}`).join(' · ') || 'none'),
      fact('Form fields', sm.fields || 0), fact('Bookmarks', sm.bookmarks || 0),
      fact('Scripts', Object.entries(sm.scripts || {}).map(([k, v]) => `${k} ${v}`).join(' · ')),
      fact('Embedded files', doc.embedded_files), fact('Layers', doc.optional_content), fact('JavaScript', doc.javascript ? 'yes' : 'no'),
      fact('Read in', `${inv.seconds} s`)].filter(Boolean))));
    const fonts = inv.fonts || [];
    if (fonts.length) {
      box.append(el('div', { class: 'datasec' }, el('h3', { text: `Fonts (${fonts.length})` }),
        el('table', { class: 'list tiny' }, el('thead', {}, el('tr', {}, ...['Font', 'Type', 'Embedded', 'ToUnicode', 'Characters'].map((h) => el('th', { text: h })))),
          el('tbody', {}, ...fonts.slice(0, 60).map((f) => el('tr', {}, el('td', { text: f.name + (f.subset ? ' (subset)' : '') }), el('td', { text: f.type }),
            el('td', { text: yes(f.embedded), class: f.embedded ? '' : 'warn' }), el('td', { text: yes(f.tounicode) }), el('td', { text: (f.chars || 0).toLocaleString() })))))));
    }
    const pics = inv.images || [];
    if (pics.length) {
      box.append(el('div', { class: 'datasec' }, el('h3', { text: `Pictures (${pics.length})` }),
        el('table', { class: 'list tiny' }, el('thead', {}, el('tr', {}, ...['Picture', 'Size', 'Drawn', 'Pages'].map((h) => el('th', { text: h })))),
          el('tbody', {}, ...pics.slice(0, 60).map((p) => el('tr', {}, el('td', { class: 'mono', text: (p.digest || '').slice(0, 10) }),
            el('td', { text: `${p.w}×${p.h}` }), el('td', { text: `${p.uses}×` }), el('td', { text: (p.pages || []).slice(0, 6).map((x) => x + 1).join(', ') + ((p.pages || []).length > 6 ? '…' : '') })))))));
    }
    drawBoxes();
    async function tagMissed(list, role) {
      const res = await post('/coverage/tag', { role, items: list.map((m) => ({ page: m.page, bbox: m.bbox })) });
      if (res.error) return;
      UA.toast(`${res.made} tag(s) made${res.failed ? ` · ${res.failed} could not be made` : ''} — Ctrl+Z takes them back`);
      await load(focus);
      renderData(true);
    }
  }

  function drawContrast() {
    if (!contrast || !contrast.available || !$('showContrast').checked) return;
    const frag = document.createDocumentFragment();
    contrast.issues.filter((i) => i.page === page).forEach((i) => {
      const [x0, y0, x1, y1] = i.rect;
      const fixedNow = i.status === 'fixed';
      const d = el('div', { class: 'cbox ' + (fixedNow ? 'fixed' : i.severity) + (i.apply ? ' ticked' : '') + (cSel === i.id ? ' flash' : ''),
        title: `${i.ratio}:1 — needs ${i.required}:1 · ${i.text}` });
      d.style.left = (x0 * zoom - 2) + 'px';
      d.style.top = (y0 * zoom - 2) + 'px';
      d.style.width = ((x1 - x0) * zoom + 4) + 'px';
      d.style.height = ((y1 - y0) * zoom + 4) + 'px';
      d.dataset.cid = i.id;
      d.append(el('span', { class: 'cr', text: fixedNow ? 'fixed' : `${i.ratio}:1` }));
      frag.append(d);
    });
    overlay.append(frag);
  }

  let swallowClick = false;
  overlay.addEventListener('click', (e) => {
    if (tool === 'draw') return;
    if (swallowClick) { swallowClick = false; return; }
    if (e.target.classList.contains('ordn') && tool === 'select') {
      const b = e.target.closest('.box');
      editOrderInline(b.dataset.id, e.target, 'ordedit');
      return;
    }
    const cb = e.target.closest('.cbox');
    if (cb && tool === 'select') {
      cSel = cb.dataset.cid;
      showTab('contrast');
      const item = document.querySelector(`.citem[data-cid="${CSS.escape(cSel)}"]`);
      if (item) item.scrollIntoView({ block: 'nearest' });
      return;
    }
    const b = e.target.closest('.box');
    if (!b) return;
    const id = b.dataset.id;
    if (tool === 'order') {
      const i = orderClicks.indexOf(id);
      if (i >= 0) orderClicks.splice(i, 1); else orderClicks.push(id);
      $('orderCount').textContent = orderClicks.length;
      drawBoxes();
      return;
    }
    clickSelect(id, e, false);
  });
  overlay.addEventListener('dblclick', (e) => {
    const b = e.target.closest('.box');
    if (b && tool === 'select') {
      // cycle to the parent tag on double click (like Acrobat's "select parent")
      const n = model.nodes[b.dataset.id];
      if (n && n.parent) selectOnly(n.parent, false);
    }
  });

  // draw a new tag
  let drawStart = null, rubber = null, pending = null;
  // ---- zone editing: drag the selected box to move it, drag a handle to resize
  let zdrag = null;
  overlay.addEventListener('mousedown', (e) => {
    if (tool !== 'select' || e.button !== 0) return;
    const box = e.target.closest('.box.editing');
    if (!box || e.target.classList.contains('ordn')) return;
    e.preventDefault();
    zdrag = { box, h: e.target.dataset.h || 'move', sx: e.clientX, sy: e.clientY,
      l: box.offsetLeft, t: box.offsetTop, w: box.offsetWidth, hgt: box.offsetHeight, moved: false };
  });
  window.addEventListener('mousemove', (e) => {
    if (!zdrag) return;
    const dx = e.clientX - zdrag.sx, dy = e.clientY - zdrag.sy;
    if (!zdrag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
    zdrag.moved = true;
    let { l, t, w, hgt } = zdrag;
    const h = zdrag.h;
    if (h === 'move') { l += dx; t += dy; }
    if (h.includes('w')) { l += dx; w -= dx; }
    if (h.includes('e')) { w += dx; }
    if (h.includes('n')) { t += dy; hgt -= dy; }
    if (h.includes('s')) { hgt += dy; }
    if (w < 4 || hgt < 4) return;
    Object.assign(zdrag.box.style, { left: l + 'px', top: t + 'px', width: w + 'px', height: hgt + 'px' });
  });
  window.addEventListener('mouseup', async () => {
    if (!zdrag) return;
    const z = zdrag;
    zdrag = null;
    if (!z.moved) return;
    swallowClick = true;
    const b = z.box;
    const rect = [b.offsetLeft / zoom, b.offsetTop / zoom, (b.offsetLeft + b.offsetWidth) / zoom,
      (b.offsetTop + b.offsetHeight) / zoom];
    const r = await post('/geometry', { id: b.dataset.id, part: parseInt(b.dataset.part, 10), page, rect });
    if (r.error) { drawBoxes(); return; }
    const n = model.nodes[b.dataset.id];
    await load(b.dataset.id);
    if (n && ['Table', 'L'].includes(n.role)) {
      UA.toast('Zone updated — reading the area again…');
      const rb = await post('/rebuild', { id: b.dataset.id });
      if (!rb.error) { await load(rb.id); UA.toast(`${n.role === 'L' ? 'List' : 'Table'} built again from the new zone`); }
    } else {
      UA.toast('Tag zone updated — the tag now owns what is inside the box');
    }
  });

  function editOrderInline(id, anchorEl, cls) {
    const n = model.nodes[id];
    if (!n) return;
    const curNum = n.block || parseInt(String(n.num).split('.').pop(), 10) || 1;
    const inp = el('input', { class: cls, type: 'number', min: 1, value: curNum,
      title: n.block ? 'New reading position among page-level blocks' : 'New position inside its parent' });
    const host = anchorEl.parentElement;
    anchorEl.replaceWith(inp);
    inp.focus();
    inp.select();
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const v = parseInt(inp.value, 10);
      if (commit && v && v !== curNum) {
        const r = await post('/order', { id, order: v });
        if (!r.error) { await load(id); UA.toast(`Reading position is now ${model.nodes[id] ? model.nodes[id].num : v}`); return; }
      }
      renderTree(); drawBoxes();
    };
    inp.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter') finish(true);
      if (e.key === 'Escape') finish(false);
    });
    inp.addEventListener('blur', () => finish(true));
    inp.addEventListener('click', (e) => e.stopPropagation());
    inp.addEventListener('mousedown', (e) => e.stopPropagation());
  }

  overlay.addEventListener('mousedown', (e) => {
    if (tool !== 'draw' || e.button !== 0) return;
    const r = overlay.getBoundingClientRect();
    drawStart = { x: e.clientX - r.left, y: e.clientY - r.top };
    rubber = el('div', { class: 'rubber' });
    overlay.append(rubber);
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!drawStart || !rubber) return;
    const r = overlay.getBoundingClientRect();
    const x = Math.max(0, Math.min(r.width, e.clientX - r.left));
    const y = Math.max(0, Math.min(r.height, e.clientY - r.top));
    Object.assign(rubber.style, {
      left: Math.min(x, drawStart.x) + 'px', top: Math.min(y, drawStart.y) + 'px',
      width: Math.abs(x - drawStart.x) + 'px', height: Math.abs(y - drawStart.y) + 'px'
    });
  });
  window.addEventListener('mouseup', (e) => {
    if (!drawStart || !rubber) return;
    const r = overlay.getBoundingClientRect();
    const x = Math.max(0, Math.min(r.width, e.clientX - r.left));
    const y = Math.max(0, Math.min(r.height, e.clientY - r.top));
    const box = [Math.min(x, drawStart.x) / zoom, Math.min(y, drawStart.y) / zoom,
      Math.max(x, drawStart.x) / zoom, Math.max(y, drawStart.y) / zoom];
    drawStart = null;
    if (box[2] - box[0] < 4 || box[3] - box[1] < 4) { rubber.remove(); rubber = null; return; }
    pending = box;
    const pop = $('rolePop');
    pop.classList.remove('hidden');
    showInside(box);
    // keep the whole popover on the screen (its Delete button too)
    const ph = pop.offsetHeight || 420, pw = pop.offsetWidth || 300;
    pop.style.left = Math.max(4, Math.min(window.innerWidth - pw - 8, e.clientX + 8)) + 'px';
    pop.style.top = Math.max(4, Math.min(window.innerHeight - ph - 8, e.clientY + 8)) + 'px';
  });
  // Draw a zone, then press a key: T = Table, 1–6 = H1–H6, F = Figure … (or click)
  const POP_KEYS = [['1', 'H1'], ['2', 'H2'], ['3', 'H3'], ['4', 'H4'], ['5', 'H5'], ['6', 'H6'],
    ['p', 'P'], ['t', 'Table'], ['l', 'L'], ['f', 'Figure'], ['c', 'Caption'], ['n', 'Note'],
    ['m', 'Formula'], ['q', 'BlockQuote'], ['k', 'Code'], ['a', 'Artifact'], ['i', 'Auto'],
    // read inside a line (V4.21.4): drawn inside a P / heading / cell they become a tag inside it
    ['s', 'Span'], ['o', 'Quote'], ['b', 'Lbl'], ['r', 'Reference']];
  const INLINE = new Set(['Span', 'Quote', 'Lbl', 'Reference', 'Code', 'Note', 'Formula', 'Figure', 'Link']);
  const TEXT_HOLD = /^(H[1-6]?|P|Caption|LBody|Lbl|TD|TH|Note|Span|Link|Quote|Code|Reference|BibEntry|TOCI|BlockQuote)$/;
  // the smallest text tag the zone lies in (as the server picks it): shown in the pop-up
  function inlineHolder(box) {
    const a = Math.max(1e-6, (box[2] - box[0]) * (box[3] - box[1]));
    let best = null, bestA = 0;
    for (const n of Object.values(model.nodes)) {
      if (n.artifact || !TEXT_HOLD.test(n.role)) continue;
      const rs = (n.rects || []).filter((r) => r[0] === page);
      if (!rs.length) continue;
      let cover = 0, na = 0;
      rs.forEach((r) => {
        const w = Math.min(box[2], r[3]) - Math.max(box[0], r[1]), h = Math.min(box[3], r[4]) - Math.max(box[1], r[2]);
        if (w > 0 && h > 0) cover += w * h;
        na += (r[3] - r[1]) * (r[4] - r[2]);
      });
      if (cover / a < 0.85 || na < a * 1.15) continue;
      if (!best || na < bestA) { best = n; bestA = na; }
    }
    return best;
  }
  function showInside(box) {
    const h = inlineHolder(box);
    const row = $('popInsideRow');
    if (!row) return;
    row.classList.toggle('hidden', !h);
    if (h) $('popInsideWhat').textContent = `${roleName(h)} “${(labelOf(h) || '').slice(0, 40)}”`;
  }
  const POP_KEY_ROLE = Object.fromEntries(POP_KEYS);
  const POP_LABEL = { Artifact: 'Artif.', BlockQuote: 'Quote', Auto: 'Auto' };
  const POP_TITLE = { Auto: 'Let the tool decide what this zone is (the AI looks at it when AI is on)',
    L: 'List', BlockQuote: 'Block quote', Artifact: 'Artifact (not read)' };
  const AI_ON = !!($('editor') && $('editor').dataset.ai === '1');
  if ($('popAi')) {
    // off to start with (V4.16): a zone is tagged from the page in a blink, and
    // the model is asked only when you tick it. It stays ticked for the next
    // zone once you do.
    $('popAi').checked = false;
    $('popAi').disabled = !AI_ON;
    if (!AI_ON) $('popAi').parentElement.title = 'The AI is switched off in Settings';
  }
  let zoneBusy = false;
  async function createZone(r) {
    if (!pending || zoneBusy) return;
    $('rolePop').classList.add('hidden');
    const rect = pending;
    const ai = !!($('popAi') && $('popAi').checked && !$('popAi').disabled);
    const deep = ['Table', 'L'].includes(r) && (ai || !!($('popDeep') && $('popDeep').checked));
    const asks = ai && ['Table', 'L', 'Figure', 'Formula', 'Auto'].includes(r);
    let busy = null;
    if (asks || deep) {
      busy = el('div', { class: 'toast', text: r === 'Auto' ? 'Looking at the zone — the AI decides what it is…'
        : asks ? `Reading the ${r} zone with the AI… (the first question also loads the model; `
                 + 'it stays loaded after that)' : 'Reading the area…' });
      document.body.append(busy);
    }
    zoneBusy = true;
    let res;
    try {
      res = await post('/create', { page, rect, role: r, absorb: $('popAbsorb').checked,
        inside: !$('popInside') || $('popInside').checked,
        deep, ai: asks || (ai && deep), split: $('popSplit') ? $('popSplit').value : 'paragraphs' });
    } finally {
      zoneBusy = false;
      if (busy) busy.remove();
    }
    pending = null;
    if (rubber) { rubber.remove(); rubber = null; }
    if (!res.error) {
      await load(res.id);
      const what = res.role || r;
      let msg = res.made > 1 ? `${res.made} ${what} tags created, one per paragraph` : `${what} created`;
      if (r === 'Auto') msg = `Tagged as ${what}`;
      if (res.alt && ['Figure', 'Formula'].includes(what)) msg += ` — “${res.alt.slice(0, 90)}”`;
      const aiBit = (res.note || '').split('; ').filter((s) => /AI/.test(s)).pop();
      if (aiBit) msg += ` · ${aiBit.slice(0, 120)}`;
      UA.toast(msg, aiBit || res.alt ? 6500 : 2800);
    }
  }
  POP_KEYS.forEach(([key, r]) => $('popRoles').append(el('button', {
    type: 'button', class: (r === 'Auto' ? 'auto' : '') + (INLINE.has(r) ? ' inl' : ''),
    title: `${POP_TITLE[r] || r} — key ${key.toUpperCase()}${INLINE.has(r) ? ' · drawn inside a paragraph, heading or cell: a tag inside it' : ''}`,
    onclick: () => createZone(r)
  }, POP_LABEL[r] || r, el('kbd', { text: key.toUpperCase() }))));
  // a zone drawn, then Delete: every tag printed wholly in it goes (V4.19.3)
  async function deleteZone() {
    if (!pending) return;
    const rect = pending;
    $('rolePop').classList.add('hidden');
    pending = null;
    if (rubber) { rubber.classList.add('zonedel'); setTimeout(() => { if (rubber) { rubber.remove(); rubber = null; } }, 260); }
    const r = await post('/zone/delete', { page, rect });
    if (r.error) return;
    sel.clear();
    focus = null;
    await load();
    UA.toast(r.deleted ? `${r.deleted} tag(s) deleted from the zone — Ctrl+Z brings them back` : 'No tag lies wholly inside that zone');
  }
  $('popDelete').addEventListener('click', deleteZone);
  $('popCancel').addEventListener('click', () => {
    $('rolePop').classList.add('hidden');
    pending = null;
    if (rubber) { rubber.remove(); rubber = null; }
  });

  function setTool(t) {
    tool = t;
    ['toolSelect', 'toolDraw', 'toolOrder'].forEach((id) => $(id).classList.remove('on'));
    $({ select: 'toolSelect', draw: 'toolDraw', order: 'toolOrder' }[t]).classList.add('on');
    $('drawBar').classList.toggle('show', t === 'draw');
    $('orderBar').classList.toggle('show', t === 'order');
    if (t !== 'order') orderClicks = [];
    $('orderCount').textContent = orderClicks.length;
    drawBoxes();
  }
  $('toolSelect').addEventListener('click', () => setTool('select'));
  $('toolDraw').addEventListener('click', () => setTool(tool === 'draw' ? 'select' : 'draw'));
  $('toolOrder').addEventListener('click', () => setTool(tool === 'order' ? 'select' : 'order'));
  $('orderCancel').addEventListener('click', () => setTool('select'));
  $('orderApply').addEventListener('click', async () => {
    if (orderClicks.length < 2) { UA.toast('Click at least two boxes'); return; }
    const res = await post('/reorder', { page, ids: orderClicks });
    if (!res.error) { setTool('select'); await load(focus); UA.toast('Reading order updated'); }
  });

  // ---------------------------------------------------------- inspector
  function field(label, input, help) {
    const l = el('label', {}, label, input);
    if (help) l.append(el('span', { class: 'tiny muted', text: help }));
    return l;
  }
  function select(options, value, onchange) {
    const s = el('select', { onchange: () => onchange(s.value) });
    options.forEach((o) => {
      const [v, t] = Array.isArray(o) ? o : [o, o || '(none)'];
      const opt = el('option', { value: v, text: t });
      if (v === value) opt.selected = true;
      s.append(opt);
    });
    return s;
  }
  async function setField(id, fields, reload) {
    const before = model && model.nodes[id] ? (model.nodes[id].artifact ? 'Artifact' : model.nodes[id].role) : '';
    const r = await post(`/node/${id}`, fields);
    if (!r.error && reload !== false) await load(id);
    if (!r.error && fields && fields.role && before && fields.role !== before) offerSimilarLater(id, before, fields.role);
  }

  // ------------------------------------------- V4.26: one fix, the brain offers the rest
  let simCard = null, simT = null;
  function offerSimilarLater(id, from, to) {          // V4.30: after a pause in the changes, not after each one
    clearTimeout(simT);
    simT = setTimeout(() => offerSimilar(id, from, to), 450);
  }
  async function offerSimilar(id, from, to) {
    if (simCard) { simCard.remove(); simCard = null; }
    let r;
    try { r = await UA.get(api(`/brain/similar?id=${encodeURIComponent(id)}&from=${encodeURIComponent(from)}`)); } catch (e) { return; }
    const items = (r && r.items) || [];
    if (!items.length) return;
    const close = () => { if (simCard) { simCard.remove(); simCard = null; } };
    simCard = el('div', { class: 'simcard', role: 'status' },
      el('div', { class: 'small' }, el('b', { text: '🧠 Learned from your fix' }),
        el('div', { text: `${items.length} more tag(s) in this file look like that one (same font, size, weight) and are still ${from}. Make them ${to} too?` })),
      el('div', { class: 'tiny muted simlist', text: items.slice(0, 6).map((x) => `p${x.page + 1} “${x.text}”`).join(' · ') + (items.length > 6 ? ' …' : '') }),
      el('div', { class: 'btnrow' },
        el('button', { class: 'sm primary', text: `Make all ${items.length} ${to}`, onclick: async () => {
          const res = await post('/brain/similar/apply', { ids: items.map((x) => x.id), role: to });
          close();
          if (!res.error) { await load(id); UA.toast(`${res.changed} tag(s) changed like yours — Ctrl+Z puts them back`); }
        } }),
        el('button', { class: 'sm', text: 'Show them', onclick: () => { selectOnly(items[0].id, true); } }),
        el('button', { class: 'sm', text: '✕', title: 'No, only this one', onclick: close })));
    document.body.append(simCard);
    setTimeout(() => { if (simCard && !simCard.matches(':hover')) close(); }, 30000);
  }
  // ------------------------------------------- V4.28: one zone cut by hand, the same cut elsewhere in the file
  let zoneCard = null, zoneTimer = null;
  const zoneSeen = new Set();
  function offerZonesSoon() {
    clearTimeout(zoneTimer);
    zoneTimer = setTimeout(offerZones, 1600);
  }
  async function offerZones() {
    let r;
    try { r = await UA.get(api('/zones/similar')); } catch (e) { return; }
    if (!r || !r.ok) return;
    const kind = r.cut && r.cut.n ? 'cut' : r.join && r.join.n ? 'join' : '';
    if (!kind) { if (zoneCard) { zoneCard.remove(); zoneCard = null; } return; }
    const it = r[kind];
    const sig = kind + ':' + it.n + ':' + (it.pairs[0] ?? '');
    if (zoneSeen.has(sig)) return;
    if (zoneCard) { zoneCard.remove(); zoneCard = null; }
    const close = () => { zoneSeen.add(sig); if (zoneCard) { zoneCard.remove(); zoneCard = null; } };
    const what = kind === 'cut'
      ? `${it.n} more place(s) in this file look like the one you cut — the same space between the lines — and are still one zone. Cut them too?`
      : `${it.n} more pair(s) of zones in this file look like the two you joined and are still apart. Join them too?`;
    zoneCard = el('div', { class: 'simcard', role: 'status' },
      el('div', { class: 'small' }, el('b', { text: '🧠 Learned from your zone fix' }), el('div', { text: what })),
      el('div', { class: 'tiny muted simlist', text: it.nodes.slice(0, 5).map((x) => `p${x.page + 1} \u201c${x.text}\u201d`).join(' · ') + (it.n > 5 ? ' …' : '') }),
      el('div', { class: 'btnrow' },
        el('button', { class: 'sm primary', text: kind === 'cut' ? `Cut all ${it.n}` : `Join all ${it.n}`, onclick: async () => {
          zoneSeen.add(sig);
          const res = await UA.post(api('/zones/similar/apply'), { action: kind, pairs: it.pairs });
          if (zoneCard) { zoneCard.remove(); zoneCard = null; }
          if (res.error) { UA.toast(res.error, 4200); return; }
          await load(focus);
          UA.toast(`${res.changed} zone(s) ${kind === 'cut' ? 'cut' : 'joined'} like yours${res.skipped ? ` (${res.skipped} left as they are)` : ''} — Ctrl+Z puts them back`, 6000);
        } }),
        el('button', { class: 'sm', text: 'Show them', onclick: () => { if (it.nodes[0]) selectOnly(it.nodes[0].id, true); } }),
        el('button', { class: 'sm', text: '✕', title: 'No, only this one', onclick: close })));
    document.body.append(zoneCard);
    setTimeout(() => { if (zoneCard && !zoneCard.matches(':hover')) close(); }, 40000);
  }
  // ------------------------------------------- V4.28: other people in the same file
  // Every few seconds: who else has this file open, and did anybody change the tags since I loaded them?
  let rev = '', staleBar = null, hbBusy = false, lastTouch = Date.now();
  ['keydown', 'pointerdown'].forEach((ev) => document.addEventListener(ev, () => { lastTouch = Date.now(); }, true));
  document.addEventListener('pointermove', (e) => { if (e.buttons) lastTouch = Date.now(); }, true);   // a drag is not idle
  // ------------------------------------------- V4.30: working time — only the time something is done, never a break
  // Every key, click, drag, wheel turn or movement of the mouse is a moment of work. Between two such moments a pause
  // shorter than the idle limit (Admin → Workflow, 2 minutes) is thinking and counts; a longer pause is a break and
  // counts for nothing. The seconds go to the server with the heartbeat, which keeps them per file and per person.
  const WORKCFG = window.WORK || { idle_limit: 120 };
  let lastInput = 0, workMs = 0, workShown = ((WORKCFG.work_s || 0) + (WORKCFG.qc_s || 0)) * 1000, workKind = WORKCFG.kind || 'work',
    workCounts = true, moveAt = 0;
  function noteInput() {
    const now = Date.now();
    if (document.hidden) { lastInput = 0; return; }
    if (lastInput) {
      const gap = now - lastInput;
      const add = gap < (WORKCFG.idle_limit || 120) * 1000 ? gap : 1500;
      workMs += add;
      workShown += add;
    }
    lastInput = now;
  }
  ['keydown', 'pointerdown', 'wheel', 'touchstart'].forEach((ev) => document.addEventListener(ev, noteInput, { capture: true, passive: true }));
  document.addEventListener('pointermove', () => { const t = Date.now(); if (t - moveAt > 1000) { moveAt = t; noteInput(); } }, { capture: true, passive: true });
  document.addEventListener('visibilitychange', () => { if (document.hidden) lastInput = 0; });
  const fmtWork = (ms) => {
    const sec = Math.round(ms / 1000);
    if (sec < 60) return sec + ' s';
    const m = Math.floor(sec / 60);
    return m < 60 ? m + ' min' : Math.floor(m / 60) + ' h ' + String(m % 60).padStart(2, '0') + ' min';
  };
  function renderWorkChip() {
    const c = $('workChip');
    if (!c) return;
    c.classList.toggle('hidden', !workCounts);
    if (!workCounts) return;
    const paused = !lastInput || Date.now() - lastInput > (WORKCFG.idle_limit || 120) * 1000;
    const text = (paused ? '⏸ ' : '⏱ ') + fmtWork(workShown);
    if (c.textContent !== text) c.textContent = text;
    c.classList.toggle('paused', paused);
    c.title = (workKind === 'qc' ? 'QC' : 'Working') + ' time on this file: ' + fmtWork(workShown) + '. Only the time you are active counts — ' +
      'a pause longer than ' + Math.round((WORKCFG.idle_limit || 120) / 60 * 10) / 10 + ' min is a break and is left out.' + (paused ? ' (paused now)' : '');
  }
  setInterval(renderWorkChip, 1000);
  renderWorkChip();
  function hideStale() { if (staleBar) { staleBar.remove(); staleBar = null; } }
  function showHere(list) {
    const chip = $('hereChip');
    if (!chip) return;
    chip.classList.toggle('hidden', !list.length);
    if (!list.length) return;
    chip.textContent = '👥 ' + list.slice(0, 3).map((x) => x.user).join(', ') + (list.length > 3 ? ` +${list.length - 3}` : '');
    chip.title = 'Also in this file: ' + list.map((x) => `${x.user} (page ${x.page})`).join(', ') +
      '. Everybody’s changes are kept; the page shows theirs a moment after they make them.';
    chip.setAttribute('aria-label', 'Also in this file: ' + list.map((x) => `${x.user}, page ${x.page}`).join('; '));
  }
  function showStale(who, at) {
    if (staleBar) return;
    staleBar = el('div', { class: 'stalebar', role: 'status' },
      el('span', { text: `${who || 'Somebody'} changed the tags in this file${at ? ' (' + at.replace('T', ' ').slice(0, 16) + ')' : ''}. ` }),
      el('button', { class: 'sm primary', text: 'Show their changes', onclick: async () => { hideStale(); await load(focus); } }));
    document.body.append(staleBar);
  }
  async function heartbeat() {
    if (document.hidden || hbBusy || !model) return;
    hbBusy = true;
    try {
      const active = Date.now() - lastTouch < 60000;
      const sent = workMs;                                  // the seconds worked since the last beat
      const r = await fetch(api('/presence') + '?rev=' + encodeURIComponent(rev) + '&page=' + page +
        (lock && lock.mine ? '&hold=1' : '') + (active ? '&active=1' : '') + (sent >= 500 ? '&w=' + (sent / 1000).toFixed(1) : ''));
      if (!r.ok) return;
      workMs = Math.max(0, workMs - sent);                  // only now: a beat that failed leaves its seconds for the next one
      const d = await r.json();
      if (d.time) {
        workCounts = !!d.time.counting;
        workKind = d.time.kind || workKind;
        if (d.time.idle_limit) WORKCFG.idle_limit = d.time.idle_limit;
        workShown = d.time.mine_s * 1000 + workMs;           // what the server holds, plus what is still to be sent
        renderWorkChip();
      }
      if (d.lock !== undefined) setLock(d.lock);
      showHere(d.here || []);
      if (!d.changed) { hideStale(); return; }
      const typing = document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      const calm = Date.now() - lastTouch > 4000 && !typing && !busyNow();
      if (calm || d.by === d.me) {
        if (calm) { await load(focus); if (d.by && d.by !== d.me) UA.toast(`${d.by}’s changes are shown`, 2600); }
      } else showStale(d.by, d.at);
    } catch (e) { /* the server is busy or the network blinked: ask again in a moment */ } finally { hbBusy = false; }
  }
  const busyNow = () => !!document.querySelector('#uaBusy.on');
  setInterval(heartbeat, 6000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) heartbeat(); });
  // ------------------------------------------- V4.29: one person at a time in a file
  // Whoever opens a file to work in it has it; the others see it — the tags, the pages, the issues — and cannot change
  // anything (the server refuses every change). The lock is kept while this editor is open, ends when the person
  // leaves or is idle for 10 minutes, and an admin can take a file over.
  let lock = window.LOCK || null;
  const viewOnly = !!(lock && lock.view_only);        // opened with "view only": the person did not ask for the file
  let lockBar = null;
  window.LOCKSTATE = lock;
  function lockedView() { return !!lock && !lock.mine; }
  function setLock(l) {
    const was = lock;
    lock = l && l.enabled !== false ? l : null;
    window.LOCKSTATE = lock;
    document.body.classList.toggle('lockedview', lockedView());
    renderLockChip();
    renderLockBar();
    if (was && lock) {
      if (was.mine && !lock.mine) UA.toast(lock.holder ? `${lock.holder} has the file now — you can look at it, not change it` : 'Your hold on this file ended', 5200);
      if (!was.mine && lock.mine) { UA.toast('The file is yours now — you can change it', 3200); load(focus); }
    }
  }
  function renderLockChip() {
    const c = $('lockChip');
    if (!c) return;
    c.classList.toggle('hidden', !lock);
    if (!lock) return;
    c.classList.toggle('mine', lock.mine);
    c.classList.toggle('view', !lock.mine);
    if (lock.mine) {
      c.textContent = '🔒 You are editing';
      c.title = 'You have this file: everybody else can look at it, not change it. Click to let go of it.';
      c.setAttribute('aria-label', 'You are editing this file. Activate to let go of it.');
    } else {
      c.textContent = lock.holder ? '👁 View only · ' + lock.holder : '👁 View only';
      c.title = lock.reason || 'You are looking at the file. Press “Edit this file” to change it.';
      c.setAttribute('aria-label', c.title);
    }
  }
  function renderLockBar() {
    if (!lock || lock.mine) { if (lockBar) { lockBar.remove(); lockBar = null; } return; }
    const free = !lock.holder;
    const text = free
      ? (viewOnly ? 'You are only looking at this file.' : 'The file is free now.')
      : `${lock.holder} is working on this file now — you can look at it, not change it.`;
    const btns = [];
    if (free) btns.push(el('button', { class: 'sm primary', type: 'button', text: 'Edit this file', onclick: takeFile }));
    else if (lock.can_take_over) btns.push(el('button', { class: 'sm', type: 'button', text: 'Take over', title: 'Admin: take the file from ' + lock.holder, onclick: takeOver }));
    const bar = el('div', { id: 'lockBar', class: 'robanner lockbar', role: 'status' },
      el('span', { class: 'lock', text: free ? '👁' : '🔒' }), el('span', { text }), ...btns);
    if (lockBar) lockBar.replaceWith(bar); else {
      const mid = document.querySelector('.editor > section.panel');
      if (mid) { bar.classList.add('inpanel'); mid.prepend(bar); } else document.body.append(bar);
    }
    lockBar = bar;
  }
  async function takeFile() {
    const r = await UA.post(api('/lock'), { action: 'acquire' });
    if (r.error && !r.ok) UA.toast(r.error, 4600);
    setLock(r);
  }
  async function takeOver() {
    if (!confirm(`${lock.holder} is working in this file. Take it over? Everything they did is already saved; from now on they can look, not change.`)) return;
    const r = await UA.post(api('/lock'), { action: 'takeover' });
    if (r.error) UA.toast(r.error, 4600);
    setLock(r);
  }
  async function letGo() {
    const r = await UA.post(api('/lock'), { action: 'release' });
    setLock(r);
    UA.toast('You let go of the file — others can change it now', 3600);
  }
  const lockChipEl = $('lockChip');
  if (lockChipEl) lockChipEl.addEventListener('click', () => { if (lock && lock.mine) letGo(); else if (lock && !lock.holder) takeFile(); });
  // somebody took the file while this page was open: the next change is refused (423) and the page turns into a view
  document.addEventListener('ua-locked', (e) => {
    const d = e.detail || {};
    setLock({ ...(lock || {}), enabled: true, mine: false, free: false, holder: d.by || '', reason: d.error, since: d.since || '' });
  });
  // leaving: let go (a closed tab that cannot, is let go after 40 seconds)
  window.addEventListener('pagehide', () => {
    if (lock && lock.mine) {
      try { navigator.sendBeacon(api('/lock'), new Blob([JSON.stringify({ action: 'release' })], { type: 'application/json' })); } catch (e) { /* the server lets go by itself */ }
    }
  });
  renderLockChip();
  renderLockBar();
  document.body.classList.toggle('lockedview', lockedView());
  function actionButtons(n) {
    const row = el('div', { class: 'btnrow' });
    [['↑', 'up', 'Move up (Alt+↑)'], ['↓', 'down', 'Move down (Alt+↓)'], ['⇤', 'outdent', 'Out of parent (Alt+←)'],
      ['⇥', 'indent', 'Into previous sibling (Alt+→)'], ['Merge next', 'merge_next', 'Merge with the next sibling'],
      ['Remove tag', 'delete', 'Remove this tag, keep its children'],
      ['Remove with children', 'delete_subtree', 'Remove this tag and everything inside']]
      .forEach(([t, a, title]) => row.append(el('button', {
        class: 'sm' + (a.startsWith('delete') ? ' danger' : ''), text: t, title,
        onclick: async () => {
          const r = await post('/move', { id: n.id, action: a });
          if (!r.error) await load(a.startsWith('delete') ? null : n.id);
        }
      })));
    if (/^H[1-6]$/.test(n.role) || descendantsOf(n.id).some((d) => /^H[1-6]$/.test(model.nodes[d].role))) {
      row.append(el('button', { class: 'sm', text: 'H− up', title: 'Heading one level up: H3 → H2 (Shift+Alt+↑); a Sect: every heading inside it',
        onclick: () => levels([n.id], 'heading_up') }));
      row.append(el('button', { class: 'sm', text: 'H+ down', title: 'Heading one level down: H2 → H3 (Shift+Alt+↓); a Sect: every heading inside it',
        onclick: () => levels([n.id], 'heading_down') }));
    }
    if (['Table', 'L'].includes(n.role)) {
      const what = n.role === 'L' ? 'list' : 'table';
      const again = async (ev, ai) => {
        const btn = ev.target;
        const was = btn.textContent;
        btn.disabled = true;
        btn.textContent = ai ? 'Asking the model…' : 'Reading…';
        const r = await post('/rebuild', { id: n.id, ai });
        btn.disabled = false;
        btn.textContent = was;
        if (!r.error) { await load(r.id); UA.toast(`${what === 'L' ? 'List' : 'Table'} built again from the zone`); }
      };
      row.append(el('button', { class: 'sm', text: `Read ${what} again`,
        title: 'Build it again from its zone: every grid strategy is tried and the one that fits the zone best wins',
        onclick: (ev) => again(ev, false) }));
      row.append(el('button', { class: 'sm', text: '+ AI',
        title: `Read it again and let the vision model check rows, columns and header rows (slow on a CPU)`,
        onclick: (ev) => again(ev, true) }));
    }
    return row;
  }

  const SAY = {};
  // V4.30: passing over tags with the arrow keys asked the server about every one of them (each answer holds the
  // PDF for everybody) — now it is asked when the person stays on a tag, and not at all for one asked before
  let sayT = null;
  function sampleSayLater(n, box) {
    clearTimeout(sayT);
    if (SAY[n.id + '|' + (n.text || '').length]) { sampleSay(n, box); return; }
    sayT = setTimeout(() => { if (focus === n.id && box.isConnected) sampleSay(n, box); }, 320);
  }
  async function sampleSay(n, box) {
    const key = n.id + '|' + (n.text || '').length;
    let r = SAY[key];
    if (!r) {
      r = await UA.get(api('/samples/suggest?id=' + encodeURIComponent(n.id)));
      SAY[key] = r;
    }
    if (!r || !r.files || !(r.votes || []).length) return;
    const top = r.votes[0];
    box.replaceChildren(el('span', { class: 'muted', text: `Samples (${r.client || 'common'}, ${r.files} file${r.files > 1 ? 's' : ''}) say:` }),
      ...r.votes.slice(0, 3).map((v) => el('button', { type: 'button', class: 'sm' + (v.role === n.role ? ' on' : ''),
        title: v.role === n.role ? 'This tag already is ' + v.role : 'Make this tag ' + v.role,
        onclick: () => { if (v.role !== n.role) setField(n.id, { role: v.role }); } },
      `${v.role} ${Math.round(v.share * 100)}%`, el('i', { class: 'sbar', style: `width:${Math.round(v.share * 30)}px` }))),
      r.wording ? el('span', { class: 'tiny muted', text: ` · this wording: ${r.wording.role} (${r.wording.n}×)` }) : '');
    const br = r.brain || {};
    if ((br.brain || []).length) {
      const names = { rules: 'rules', samples: 'samples', model: 'model', style: 'style', wording: 'wording', code: 'learned code', layout: 'same look in the file', ai: 'AI' };
      const src = Object.entries(br.sources || {}).map(([k, v]) => `${names[k] || k} ${v[0]}`).join(' · ');
      box.append(el('span', { class: 'brsrc tiny muted' }, el('b', { text: '🧠 Brain: ' }),
        br.brain.slice(0, 3).map((v) => `${v.role} ${Math.round(v.share * 100)}%`).join(' · ') + (src ? ` — ${src}` : '')));
    }
    box.classList.toggle('hidden', false);
    if (top.role !== n.role && top.share >= 0.7) box.classList.add('differs');
  }
  function renderInspector() {
    const box = $('tab-props');
    box.replaceChildren();
    if (sel.size > 1) return inspMulti(box);
    const n = model.nodes[focus];
    if (!n) return inspNone(box);

    box.append(field('Tag type', select(window.ROLES, roleName(n), (v) => setField(n.id, { role: v }))));
    // what the client's sample files say about this tag (V4.20)
    if (['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'Caption', 'Note', 'BlockQuote'].includes(n.role) && n.text) {
      const say = el('div', { class: 'sampsay hidden' });
      box.append(say);
      sampleSayLater(n, say);
    }

    if (n.artifact) {
      box.append(el('div', { class: 'twocol' },
        field('Artifact type', select(window.ART_TYPES, n.artifact_subtype, (v) => setField(n.id, { artifact_subtype: v }, false))),
        field('Subtype', select(window.PAGINATION, n.pagination_subtype, (v) => setField(n.id, { pagination_subtype: v }, false)))));
    }

    if (n.role === 'Figure' || n.role === 'Formula') {
      const ta = el('textarea', { placeholder: 'What does this image tell the reader?' });
      ta.value = n.alt || '';
      ta.addEventListener('change', () => setField(n.id, { alt: ta.value }));
      box.append(field('Alternative text', ta, `${(n.alt || '').length} characters — describe meaning, not appearance`));
      const img = el('img', { class: 'fig-preview', alt: '' });
      img.src = `/job/${JID}/crop/${n.id}.png?t=${Date.now() % 100000}`;
      box.append(img);
      box.append(el('div', { class: 'btnrow' },
        el('button', {
          class: 'sm', text: 'Draft with AI', onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = 'thinking…';
            const r = await UA.post(api(`/ai/alt/${n.id}`));
            e.target.disabled = false; e.target.textContent = 'Draft with AI';
            if (r.error) { UA.toast(r.error, 5000); return; }
            if (r.decorative) { UA.toast('The model thinks this image is decorative — consider marking it an artifact.', 5000); return; }
            await load(n.id);
          }
        }),
        el('button', { class: 'sm', text: 'Decorative → artifact', onclick: () => setField(n.id, { role: 'Artifact' }) })));
    }

    if (n.role === 'TH' || n.role === 'TD') {
      const row = el('div', { class: 'twocol' });
      if (n.role === 'TH') row.append(field('Scope', select(['', 'Column', 'Row', 'Both'], n.scope, (v) => setField(n.id, { scope: v }, false))));
      const rs = el('input', { type: 'number', min: 1, value: n.rowspan });
      rs.addEventListener('change', () => setField(n.id, { rowspan: rs.value }));
      const cs = el('input', { type: 'number', min: 1, value: n.colspan });
      cs.addEventListener('change', () => setField(n.id, { colspan: cs.value }));
      row.append(field('Row span', rs), field('Column span', cs));
      box.append(row);
      if (n.headers && n.headers.length) {
        box.append(el('p', { class: 'small muted', text: 'Headers: ' + n.headers.map((h) => (model.nodes[h] || {}).text || h).join(' · ') }));
      }
      box.append(el('div', { class: 'btnrow' },
        el('button', { class: 'sm', text: n.role === 'TH' ? 'Make data cell (TD)' : 'Make header cell (TH)', onclick: () => setField(n.id, { role: n.role === 'TH' ? 'TD' : 'TH' }) })));
    }
    if ((!n.text || /^[_\s]+$/.test(n.text)) && !n.children.length && !['TD', 'TH', 'Figure', 'Formula'].includes(n.role) && !n.artifact) {
      const cb = el('input', { type: 'checkbox' });
      cb.checked = !!n.blank_line;
      cb.addEventListener('change', () => setField(n.id, { blank_line: cb.checked }));
      box.append(field('Write-in line', el('label', { class: 'small' }, cb, ' This tag is an answer line (drawn rule)'),
        `Its drawing goes into this tag and screen readers announce “${blankText(n)}” (change it in Actual text or the build setting).`));
    }
    if ((n.role === 'TD' || n.role === 'TH') && !n.text && !n.children.length) {
      const bt = el('input', { value: n.actual_text || '', placeholder: window.BLANK_TEXT || 'Blank cell' });
      bt.addEventListener('change', () => setField(n.id, { actual_text: bt.value }));
      box.append(field('Empty cell — screen readers announce', bt,
        `Leave empty to use the build setting (“${window.BLANK_TEXT || 'Blank cell'}”)`));
    }
    if (n.role === 'L') {
      box.append(field('List numbering', select(window.NUMBERING, n.list_numbering, (v) => setField(n.id, { list_numbering: v }, false))));
    }
    if (n.role === 'Form') {
      const t = el('input', { value: n.text || '' });
      t.addEventListener('change', () => setField(n.id, { text: t.value }));
      box.append(field('Field label / tooltip', t, 'Used as the field tooltip (/TU) when the field has none'));
    }
    if (n.role === 'Link' || n.role === 'Annot') {
      const t = el('input', { value: n.text || '' });
      t.addEventListener('change', () => setField(n.id, { text: t.value }));
      box.append(field(n.role === 'Link' ? 'Link description' : 'Annotation description', t,
        n.role === 'Link' ? 'Written to the link annotation /Contents when it has none' : ''));
    }

    // a tag joined from several pieces: back to one tag per piece
    if ((n.rects || []).length > 1 && !(n.children || []).length
        && ['P', 'Span', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'Caption', 'Note', 'Quote'].includes(n.role)) {
      box.append(el('div', { class: 'btnrow' }, el('button', {
        class: 'sm', text: `Split into one tag per zone (${n.rects.length})`,
        title: 'Each zone of this tag becomes a tag of its own, with the text printed in it, in reading order',
        onclick: () => splitZones([n.id]) })));
    }

    // a cell that holds a list: tag it where it stands
    if ((n.role === 'TD' || n.role === 'TH') && !(n.children || []).length
        && ((' ' + (n.text || '').trim()).match(CELL_LABEL) || []).length >= 2) {
      box.append(el('div', { class: 'btnrow' }, el('button', {
        class: 'sm primary', text: 'Tag the list inside this cell',
        title: 'The lead-in line becomes a P and the items L > LI > Lbl + LBody, inside this cell',
        onclick: () => fixCellLists([n.id]) })));
    }

    // table tools for the table or anything inside it
    const tbl = n.role === 'Table' ? n : ancestors(n.id).map((a) => model.nodes[a]).find((a) => a.role === 'Table');
    if (tbl) box.append(tableSection(tbl));
    // list tools for the list or anything inside it (the outermost list)
    const lists = (n.role === 'L' ? [n] : []).concat(ancestors(n.id).map((a) => model.nodes[a]).filter((a) => a.role === 'L'));
    const lst = lists.length ? lists[lists.length - 1] : null;
    if (lst && !tbl) box.append(listSection(lst));

    if (!CONTAINERS.has(n.role) && n.text && !['Figure', 'Formula', 'Link', 'Form', 'Annot'].includes(n.role)) {
      box.append(el('p', { class: 'small', style: 'white-space:pre-wrap;max-height:9em;overflow:auto;background:var(--panel-2);padding:6px 8px;border-radius:6px', text: n.text.slice(0, 1200) }));
    }

    const lang = el('input', { value: n.lang || '', list: 'langs', placeholder: 'inherit (' + model.lang + ')' });
    lang.addEventListener('change', () => setField(n.id, { lang: lang.value }));
    box.append(field('Language of this part', lang));

    if (!CONTAINERS.has(n.role) || !(n.children || []).length) box.append(actualBox([n.id], true));

    const adv = el('details', {}, el('summary', { class: 'small', text: 'Advanced: actual text, expansion, ID' }));
    const at = el('input', { value: n.actual_text || '' });
    at.addEventListener('change', () => setField(n.id, { actual_text: at.value }));
    const ex = el('input', { value: n.expansion || '' });
    ex.addEventListener('change', () => setField(n.id, { expansion: ex.value }));
    const ida = el('input', { value: n.id_attr || '' });
    ida.addEventListener('change', () => setField(n.id, { id_attr: ida.value }));
    adv.append(field('Actual text (replaces the content for AT)', at), field('Expansion (/E) for abbreviations', ex),
      field('Element ID', ida));
    box.append(adv);

    // a tag for something the page does not print: a missing cell, a label a
    // list item never had, a line left for an answer
    const emptyBox = el('details', {}, el('summary', { class: 'small', text: 'Add an empty tag' }));
    const erole = select([['P', 'P'], ['TD', 'TD (cell)'], ['TH', 'TH (header cell)'], ['TR', 'TR (row)'],
      ['Lbl', 'Lbl'], ['LBody', 'LBody'], ['LI', 'LI'], ['L', 'L'], ['Span', 'Span'],
      ['Caption', 'Caption'], ['Note', 'Note'], ['Sect', 'Sect'], ['Div', 'Div']],
      n.role === 'TR' ? 'TD' : (n.role === 'LI' ? 'Lbl' : 'P'), () => {});
    const ewhere = select([['after', 'after this tag'], ['before', 'before this tag'],
      ['inside', 'inside this tag']], CONTAINERS.has(n.role) ? 'inside' : 'after', () => {});
    const etext = el('input', { placeholder: 'e.g. Blank Line — leave empty for nothing' });
    emptyBox.append(field('Tag type', erole), field('Where', ewhere),
      field('Read as (/ActualText)', etext,
        'Written into the file as invisible text, the way an empty cell is, so the tag is really there. '
        + 'Left empty, a tag with no content is dropped at Build unless it is a cell.'),
      el('div', { class: 'btnrow' }, el('button', {
        class: 'sm primary', text: 'Add empty tag', onclick: async () => {
          const r = await post('/create/empty',
            { role: erole.value, target: n.id, where: ewhere.value, text: etext.value });
          if (r.error) { UA.toast(r.error); return; }
          await load(r.id);
          UA.toast(`Empty ${erole.value} added — drag its box on the page to place it`);
        } })));
    box.append(emptyBox);

    if (n.num) {
      const curNum = n.block || parseInt(String(n.num).split('.').pop(), 10) || 1;
      const ordIn = el('input', { type: 'number', min: 1, value: curNum });
      box.append(el('div', { class: 'orderrow' }, field(n.block ? 'Reading position' : `Position in parent (${n.num})`, ordIn),
        el('button', { class: 'sm', style: 'margin-bottom:8px', text: 'Set', onclick: async () => {
          const r = await post('/order', { id: n.id, order: parseInt(ordIn.value, 10) });
          if (!r.error) { await load(n.id); UA.toast(`Reading position is now ${model.nodes[n.id].num}`); }
        } }),
        el('span', { class: 'tiny muted', style: 'margin-bottom:10px', text: 'or click the number on the page' })));
    }
    box.append(actionButtons(n));

    const iss = issuesByNode[n.id] || [];
    iss.forEach((i) => box.append(el('div', { class: 'issue ' + i.severity }, el('span', { text: i.message }))));

    const kv = el('dl', { class: 'kv' });
    const add = (k, v) => { kv.append(el('dt', { text: k }), el('dd', { text: v })); };
    add('Page', (n.page >= 0 ? n.page + 1 : '–') + ((n.rects || []).length > 1 ? ` (+${n.rects.length - 1} part)` : ''));
    add('Reading position', n.num || '— (artifact)');
    add('Confidence', Math.round(n.confidence * 100) + '%');
    add('Source', n.source);
    if (n.orig_role) add('Original tag', n.orig_role);
    if (n.src_keys) add('Original MCIDs', n.src_keys);
    if (n.annots && n.annots.length) add('Annotation', n.annots.join(', '));
    if (n.note) add('Why', n.note);
    box.append(kv);
  }

  function listSection(L) {
    const wrap = el('div', { class: 'card sectcard', style: 'padding:10px;margin:10px 0;box-shadow:none' });
    const items = Object.values(model.nodes).filter((x) => x.role === 'LI' && ancestors(x.id).includes(L.id)).length;
    wrap.append(el('div', { class: 'lhead' }, el('h3', { text: 'List' }), el('span', { class: 'chip', text: `${items} item(s)` }), el('div', { class: 'spacer' }),
      el('button', { class: 'sm primary', text: '☰ Edit list…',
        title: 'Items: join, split, nest, take out, add from a zone; numbering; templates',
        onclick: () => window.LST && window.LST.open(L.id) })));
    wrap.append(templateRow('list', L.id));
    return wrap;
  }
  // "Template #N → this table / list" right in the inspector
  function templateRow(kind, id) {
    // V4.19.1: the same structure elsewhere is Ctrl/Alt + a number, then a click
    return el('p', { class: 'tiny muted tplrow2' }, 'Same ' + (kind === 'table' ? 'structure' : 'format') + ' for other ' + kind + 's: press ',
      el('kbd', { text: 'Ctrl' }), '+', el('kbd', { text: '4' }), ' (or ', el('kbd', { text: 'Alt' }), '+', el('kbd', { text: '4' }),
      ', any number) to mark this ' + kind + ' #4, then click the next ' + kind + 's on any page. ', el('kbd', { text: 'Esc' }), ' releases the mark.');
  }
  function templateRowOld(kind, id) {
    const row = el('div', { class: 'btnrow tplrow2' });
    const pick = el('select', { class: 'sm', title: `${kind} templates` }, el('option', { value: '', text: 'Template…' }));
    const go = el('button', { class: 'sm', text: 'Apply', disabled: true, onclick: async () => {
      if (!pick.value) return;
      if (!confirm(`Give this ${kind} the structure of template #${pick.value}? (Undo brings it back)`)) return;
      if (window.TBL) await window.TBL.applyTemplate(kind, pick.value, { id });
    } });
    pick.addEventListener('change', () => { go.disabled = !pick.value; });
    row.append(pick, go);
    UA.get('/api/templates').then((t) => {
      (t[kind] || []).forEach((x) => pick.append(el('option', { value: x.number, text: `#${x.number}${x.name ? ' ' + x.name : ''} — ${x.summary}`.slice(0, 70) })));
      if (!(t[kind] || []).length) { pick.firstChild.textContent = 'no template yet'; pick.disabled = true; }
    });
    return row;
  }

  function tableSection(t) {
    const wrap = el('div', { class: 'card', style: 'padding:10px;margin:10px 0;box-shadow:none' });
    wrap.append(el('div', { class: 'lhead' }, el('h3', { text: 'Table' }), el('div', { class: 'spacer' }),
      el('button', { class: 'sm primary', text: '▦ Edit table…',
        title: 'Rows and columns: add, delete, split, merge; merge and split cells; TH / TD; move the lines on the page',
        onclick: () => window.TBL && window.TBL.open(t.id) })));
    wrap.append(templateRow('table', t.id));
    const info = el('p', { class: 'small muted', text: 'loading…' });
    wrap.append(info);
    const hr = el('input', { type: 'number', min: 0, value: 1 });
    const hc = el('input', { type: 'number', min: 0, value: 0 });
    const mode = select([['auto', 'auto'], ['scope', 'Scope only'], ['ids', 'IDs + Headers']], 'auto', () => {});
    wrap.append(el('div', { class: 'twocol' }, field('Header rows', hr), field('Header columns', hc)));
    wrap.append(field('Header association', mode, 'auto: IDs/Headers when the table has merged cells'));
    wrap.append(el('div', { class: 'btnrow' },
      el('button', { class: 'sm primary', text: 'Apply headers', onclick: async () => {
        const r = await post(`/table/${t.id}`, { header_rows: hr.value, header_cols: hc.value, mode: mode.value });
        if (!r.error) { await load(focus); UA.toast('Table headers updated'); }
      } }),
      el('button', { class: 'sm', text: 'Re-read cells from the page',
        title: 'Each cell takes the words printed inside it; a row printed as one line is cut '
             + 'at the cell borders',
        onclick: () => rereadCells([t.id]) }),
      t.id !== focus ? el('button', { class: 'sm', text: 'Select table', onclick: () => selectOnly(t.id, true) }) : null));

    // one heading, then the words printed under it — group after group
    // the last choice is remembered: a workbook is usually tagged the same way all through
    let gpref = 'paragraphs';
    try { gpref = localStorage.getItem('ua-group-mode') || gpref; } catch (e) { /* private mode */ }
    const gmode = select([['paragraphs', 'keep the tags as they are'], ['cells', 'words become cells (TD)']],
      gpref, (v) => { try { localStorage.setItem('ua-group-mode', v); } catch (e) { /* ignore */ } });
    wrap.append(el('div', { class: 'btnrow' },
      el('button', { class: 'sm', text: 'Group by heading',
        title: 'Each heading gets a row of its own, and the tags printed under it follow in one row — '
             + 'read down the first column of the group, then down the next',
        onclick: async () => {
          const r = await post(`/table/${t.id}/columns`, { mode: gmode.value });
          if (r.error) { UA.toast(r.error); return; }
          await load(t.id);
          UA.toast(`${r.groups} heading(s), ${r.cells} tag(s) grouped under them`
            + (r.left_over ? ` · ${r.left_over} not under any heading` : ''));
        } }), gmode));
    wrap.append(el('p', { class: 'tiny muted', text: 'Group by heading: "Initial Position" and its words, '
      + 'then "Medial Position" and its words — the order the card is taught in, instead of reading across '
      + 'the printed rows.' }));
    UA.get(api(`/table/${t.id}`)).then((d) => {
      if (d.error) { info.textContent = d.error; return; }
      hr.value = d.header_rows; hc.value = d.header_cols;
      info.textContent = `${d.rows} rows × ${d.cols} columns` + (d.regular ? '' : ' — rows differ in width, check spans');
    });
    return wrap;
  }

  // ---- Read as (/ActualText): what a screen reader says instead of the print
  async function suggestActual(ids) {
    const r = await post('/actualtext/suggest', { ids });
    return r.error ? null : (r.items || []);
  }
  function actualBox(ids, single) {
    const box = el('div', { class: 'joinbox' });
    box.append(el('div', { class: 'small', text: 'Read as (/ActualText)' }));
    const one = el('input', { placeholder: single ? 'leave empty to read what is printed'
                                                  : 'the same sentence on every selected tag' });
    if (single) one.value = model.nodes[ids[0]].actual_text || '';
    const rows = el('div', { class: 'atrows' });
    const apply = async (mode, text) => {
      const r = await post('/actualtext', { ids, mode, text });
      if (r.error) return;
      await load(focus);
      UA.toast(mode === 'clear' ? `${r.changed} tag(s) read as printed again`
                                : `${r.changed} tag(s) given an /ActualText` +
                                  (r.skipped && r.skipped.length ? ` · ${r.skipped.length} skipped` : ''));
    };
    box.append(el('div', { class: 'btnrow' }, one,
      el('button', { class: 'primary sm', text: single ? 'Apply' : 'Apply to all',
        onclick: () => apply('same', one.value) })));
    box.append(el('div', { class: 'btnrow' },
      el('button', { class: 'sm', text: 'Suggest from the page',
        title: 'Spells the letters out and names the ones printed in colour, from the glyphs on the page',
        onclick: async (ev) => {
          const btn = ev.target;
          btn.disabled = true;
          const items = await suggestActual(ids);
          btn.disabled = false;
          rows.textContent = '';
          if (!items || !items.length) { UA.toast('nothing to read in those tags'); return; }
          if (single) { one.value = items[0].suggestion || ''; return; }
          items.forEach((it) => {
            const inp = el('input', { value: it.suggestion || '' });
            inp.dataset.id = it.id;
            rows.append(el('div', { class: 'atrow' },
              el('span', { class: 'tiny muted', text: `p${it.page + 1} ${it.text.slice(0, 18) || it.role}` }), inp));
          });
          rows.append(el('div', { class: 'btnrow' },
            el('button', { class: 'primary sm', text: `Apply these ${items.length}`, onclick: () => {
              const inputs = [...rows.querySelectorAll('input')];
              apply('each', inputs.map((i) => i.value));
            } }),
            el('button', { class: 'sm', text: 'Cancel', onclick: () => { rows.textContent = ''; } })));
        } }),
      el('button', { class: 'sm', text: 'Clear', onclick: () => apply('clear', '') })));
    box.append(rows);
    box.append(el('p', { class: 'tiny muted', text: single
      ? 'What a screen reader says instead of the printed text of this tag.'
      : 'One sentence for all of them, or “Suggest from the page” for one per tag — the letters spelled out and the ones printed in colour named ("c, a and t, a highlighted in red"). Every line can be edited before it is applied.' }));
    return box;
  }

  // several tags a level in / out, up / down, their heading levels (V4.21.4)
  async function levels(ids, action) {
    const r = await post('/levels', { ids, action });
    if (r.error) return;
    await load();
    const msg = {
      indent: `${r.moved} tag(s) one level in`, outdent: `${r.moved} tag(s) one level out`,
      up: `${r.moved} tag(s) moved up`, down: `${r.moved} tag(s) moved down`,
      heading_up: `${r.changed} heading(s) one level up`, heading_down: `${r.changed} heading(s) one level down`,
    }[action];
    UA.toast(msg + (r.skipped ? ` · ${r.skipped} could not move` : '') +
      (r.headings && r.changed < r.headings ? ` · ${r.headings - r.changed} at H1 / H6 already` : ''));
  }
  function levelBox(ids) {
    const heads = ids.filter((i) => /^H[1-6]$/.test(model.nodes[i].role)).length;
    const inside = ids.some((i) => descendantsOf(i).some((d) => /^H[1-6]$/.test(model.nodes[d].role)));
    const lv = (t, a, title) => el('button', { class: 'sm', text: t, title, onclick: () => levels(ids, a) });
    return el('div', { class: 'lvlbox' },
      el('div', { class: 'lvlhead' }, el('b', { text: 'Tag level' }),
        el('span', { class: 'tiny muted', text: ' — every selected tag at once, one Undo' })),
      el('div', { class: 'btnrow' },
        lv('⇤ Level out', 'outdent', 'Out of their parent, right after it (Alt+←)'),
        lv('⇥ Level in', 'indent', 'Into the tag just before them, together (Alt+→)'),
        lv('↑ Up', 'up', 'Before the tag above them (Alt+↑)'),
        lv('↓ Down', 'down', 'After the tag below them (Alt+↓)')),
      (heads || inside) ? el('div', { class: 'btnrow' },
        el('span', { class: 'small', text: `Headings${heads ? ` (${heads})` : ' inside'}:` }),
        lv('H− level up', 'heading_up', 'H3 → H2, H2 → H1 … (Shift+Alt+↑)'),
        lv('H+ level down', 'heading_down', 'H2 → H3, H3 → H4 … (Shift+Alt+↓)')) : null);
  }
  function descendantsOf(id) {
    const out = [];
    const walk = (x) => (model.nodes[x] ? model.nodes[x].children : []).forEach((c) => { out.push(c); walk(c); });
    walk(id);
    return out;
  }

  function inspMulti(box) {
    const ids = [...sel].sort((a, b) => model.nodes[a].order - model.nodes[b].order);
    box.append(el('h3', { text: `${ids.length} tags selected` }));
    box.append(levelBox(ids));
    box.append(field('Set tag type for all', select([''].concat(window.ROLES), '', async (v) => {
      if (!v) return;
      const r = await post('/bulk', { ids, action: 'role', role: v });
      if (!r.error) await load();
    })));
    const wrapRole = select(['Sect', 'Div', 'Part', 'Art', 'BlockQuote', 'L', 'TOC', 'Table', 'Note', 'Caption', 'Link'], 'Sect', () => {});
    box.append(el('div', { class: 'twocol' }, field('Wrap in', wrapRole),
      el('div', { style: 'align-self:end;margin-bottom:8px' }, el('button', { class: 'sm', text: 'Wrap', onclick: async () => {
        const r = await post('/bulk', { ids, action: 'wrap', role: wrapRole.value });
        if (!r.error) await load(r.id);
      } }))));
    box.append(el('div', { class: 'btnrow' },
      el('button', { class: 'sm', text: 'Make list', title: 'Each selected tag becomes a list item', onclick: async () => {
        const r = await post('/bulk', { ids, action: 'make_list' }); if (!r.error) await load(r.id);
      } }),
      ids.some((i) => model.nodes[i].role === 'Table') ? el('button', { class: 'sm', text: 'Add to table as rows',
        title: 'The selected tags under the table become rows of it, each one cell spanning all columns', onclick: async () => {
          const r = await post('/bulk', { ids, action: 'table_rows' }); if (!r.error) await load(r.id);
        } }) : '',
      el('button', { class: 'sm', text: 'Merge into one', onclick: async () => {
        const r = await post('/bulk', { ids, action: 'merge' }); if (!r.error) await load(r.id);
      } }),
      el('button', { class: 'sm', text: 'Mark as artifacts', onclick: async () => {
        const r = await post('/bulk', { ids, action: 'artifact' }); if (!r.error) await load();
      } }),
      el('button', { class: 'sm', text: 'These are all page numbers',
        title: 'Each one becomes a P read as “Page 5” (its /ActualText); an artifact folio becomes a real tag',
        onclick: async () => {
          const r = await post('/page_numbers', { ids });
          if (r.error) return;
          await load();
          UA.toast(`${r.changed} page number(s) now read as “Page …”` +
            (r.skipped && r.skipped.length ? ` · ${r.skipped.length} skipped (no number in the text)` : ''));
        } }),
      el('button', { class: 'sm danger', text: 'Remove tags', onclick: async () => {
        const r = await post('/bulk', { ids, action: 'delete' }); if (!r.error) { sel.clear(); focus = null; await load(); }
      } })));
    box.append(actualBox(ids, false));
    const roles = new Set(ids.map((i) => model.nodes[i].role));
    const pages = new Set(ids.map((i) => model.nodes[i].page));
    const joinable = roles.size === 1 && ids.length > 1;
    const kind = joinable ? [...roles][0] : '';
    const useAi = el('input', { type: 'checkbox' });
    box.append(el('div', { class: 'joinbox' },
      el('div', { class: 'btnrow' },
        el('button', { class: 'primary sm', disabled: !joinable,
          text: pages.size > 1 ? `Join these ${kind || 'tags'} (pages ${[...pages].map((p) => p + 1).sort((a, b) => a - b).join(', ')})`
                               : `Join these ${kind || 'tags'} into one`,
          title: 'One tag for a list, table or paragraph that a page break split in two',
          onclick: async (ev) => {
            const btn = ev.target;
            const was = btn.textContent;
            btn.disabled = true;
            btn.textContent = useAi.checked ? 'Asking the model…' : 'Joining…';
            const r = await post('/merge_parts', { ids, ai: useAi.checked,
              ask_ai: useAi.checked ? 'always' : 'never' });
            btn.disabled = false;
            btn.textContent = was;
            if (r.error) return;
            await load(r.id);
            const a = r.ai;
            UA.toast(a ? (a.same ? `Joined — AI agrees${a.repeat_header ? ' (repeated header kept as a running head)' : ''}`
                                 : `Joined, but the AI thinks these are different: ${a.why}`)
                       : 'Joined');
          } }),
        el('label', { class: 'row small', title: 'Off by default: the rules decide this on their own, and a model on a CPU takes a while' },
          useAi, 'ask the AI too (slow)')),
      el('p', { class: 'tiny muted', text: joinable
        ? 'A table takes the other one’s rows (a repeated column header becomes a running head), a list takes its items, a paragraph takes its text. The tags keep their own pages.'
        : 'Select two or more tags of the same type — e.g. the list at the end of page 3 and the list at the top of page 4 — to join them.' })));
    box.append(el('p', { class: 'tiny muted', text: 'Wrap / make list / merge into one need tags that share one parent; Join works across pages.' }));
  }

  function inspNone(box) {
    const counts = {};
    let figs = 0, noalt = 0;
    Object.values(model.nodes).forEach((n) => {
      const r = roleName(n);
      counts[r] = (counts[r] || 0) + 1;
      if (n.role === 'Figure' && !n.artifact) { figs++; if (!n.alt) noalt++; }
    });
    box.append(el('h3', { text: 'Document' }));
    const kv = el('dl', { class: 'kv' });
    const add = (k, v) => kv.append(el('dt', { text: k }), el('dd', { text: v }));
    add('Pages', model.pages);
    add('Tags', Object.keys(model.nodes).length);
    add('Mode', model.mode === 'import' ? 'existing tags kept' : 'auto-tagged');
    add('Headings', ['H1', 'H2', 'H3', 'H4', 'H5', 'H6'].map((h) => counts[h] ? `${h}×${counts[h]}` : '').filter(Boolean).join(' ') || '–');
    add('Figures', figs ? `${figs} (${noalt} without alt text)` : '–');
    add('Tables', counts.Table || 0);
    add('Lists', counts.L || 0);
    add('Links / fields', `${counts.Link || 0} / ${counts.Form || 0}`);
    add('Artifacts', counts.Artifact || 0);
    if (model.info && model.info.joined_paragraphs) add('Joined paragraphs', model.info.joined_paragraphs);
    if (model.info && model.info.lang_tagged) add('Other-language passages', model.info.lang_tagged);
    box.append(kv);
    // printed page numbers: some projects read them, most leave them out
    const pn = el('div', { class: 'btnrow' },
      el('span', { class: 'small', text: 'Page numbers:' }),
      el('button', { class: 'sm', text: 'Read as P',
        title: 'Every printed page number becomes a P where it is printed (first on the page at the top, last at the bottom)',
        onclick: () => setPageNumbers('P') }),
      el('button', { class: 'sm', text: 'Leave out (artifact)',
        title: 'Every printed page number is pagination, left out of the reading — the PDF/UA default',
        onclick: () => setPageNumbers('artifact') }));
    box.append(pn);
    UA.get(api('/pagenumbers')).then((r) => {
      if (!r || r.error) return;
      pn.append(el('span', { class: 'tiny muted', text: r.to_p || r.to_artifact
        ? `${r.to_p} left out, ${r.to_artifact} read as P · this file's option: ${r.mode}`
        : `this file's option: ${r.mode}` }));
    }).catch(() => {});
    (model.warnings || []).forEach((w) => box.append(el('div', { class: 'issue warning' }, el('span', { text: w }))));
    box.append(el('p', { class: 'small muted', text: 'Click a tag in the tree or a box on the page. Use Draw tag to tag an area by hand, Reading order to click boxes in order.' }));
  }

  // -------------------------------------------------------------- issues
  function renderIssues() {
    const box = $('tab-issues');
    box.replaceChildren();
    const anyFix = blankLeft || promptLeft || altLeft || cellListLeft || tidyLeft || gridLeft || spanLeft
      || (pageNums && ((pageNums.mode === 'P' && pageNums.to_p) || (pageNums.mode !== 'P' && pageNums.to_artifact)))
      || Object.values(model.nodes || {}).some((n) => n.role === 'TOCI' && !n.artifact
        && !(n.children || []).some((c) => (model.nodes[c] || {}).role === 'Link'));
    if (!issues.length && !anyFix) {
      box.append(el('p', { class: 'muted', text: 'No issues found in the tag tree.' }));
      aiOrderSection(box);
      return;
    }
    const e = issues.filter((i) => i.severity === 'error').length;
    const w = issues.filter((i) => i.severity === 'warning').length;
    box.append(el('p', { class: 'small muted', text: issues.length
      ? `${e} error(s), ${w} warning(s), ${issues.length - e - w} to review`
      : 'Nothing is wrong in the tag tree — these are things the page could still say better.' }));
    box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary', id: 'fixAllBtn',
      text: 'Fix everything the tool can fix',
      title: 'Runs every one-click fix below, in the order that works: structure, lists, the reference '
           + 'list, heading levels, reading order, list labels, empty headers, write-in lines, lists in '
           + 'cells, page numbers, alt text from captions, and the contents links. Undo (Ctrl+Z) takes '
           + 'the steps back one by one',
      onclick: fixEverything })));
    const idxN = issues.filter((i) => i.code === 'index').length;
    if (idxN) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Tag the index (${idxN})`,
      title: 'The index entries become Index > L > LI > LBody, sub-entries as a list inside their entry, '
           + 'a wrapped line kept with its entry; then every page number is linked to its page, the '
           + 'footnote marks to their notes and back, and captions back to where the text mentions them',
      onclick: () => fixBackmatter() })));
    aiOrderSection(box);
    const bib = issues.filter((i) => i.code === 'bibliography').length;
    if (bib) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Tag the reference list (${bib})`,
      title: 'Each entry under References / Bibliography becomes one BibEntry (L > LI > LBody > BibEntry), '
           + 'found from the hanging indent, the numbers and the space between entries — an entry cut into '
           + 'several paragraphs becomes one again. Then the citations (“Hasbrouck, 2010”) are linked to '
           + 'their entry. With the AI on, the text model decides a list with no clear layout',
      onclick: () => fixBibliography() })));
    const ord = issues.filter((i) => i.code === 'order').length;
    if (ord) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary', text: `Fix reading order (${ord})`, onclick: fixOrder })));
    if (spanLeft) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Put /ActualText on a Span (${spanLeft})`,
      title: 'Every client asks for this: a tag that is read through /ActualText (a write-in line, a '
           + 'spelled card, a page number, a letter-spaced line) gets a Span inside it that holds the '
           + 'tag\u2019s content and carries the /ActualText \u2014 the P, TD or H itself carries none. '
           + 'Figure and Formula keep it (it is their description)',
      onclick: fixActualTextSpans })));
    // a client that keeps empty header cells as TH (Kriyadocs) gets no such button
    const HOUSE = !!(window.OPTS && (window.OPTS.house_style || '').toLowerCase() && !['none', 'off'].includes((window.OPTS.house_style || '').toLowerCase()));
    const eth = HOUSE ? 0 : issues.filter((i) => i.code === 'empty_th').length;
    if (eth) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary', text: `Fix empty headers → TD (${eth})`, onclick: fixEmptyHeaders })));
    const lbl = issues.filter((i) => i.code === 'list' && /inside the text/.test(i.message)).length;
    if (lbl) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary', text: `Fix list labels (${lbl})`,
      title: 'Take the number out of the item text and put it in a Lbl', onclick: fixItemLabels })));
    const skips = issues.filter((i) => i.code === 'heading' && /level is skipped/.test(i.message)).length;
    if (skips) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Fix heading levels (${skips})`,
      title: 'A heading may not skip a level (H1 then H3): each one is pulled up to the level after the one before it',
      onclick: fixHeadingLevels })));
    const misplaced = issues.filter((i) => /outside a (TOC|list|table|list item)|outside its row|inside a table row/.test(i.message)).length;
    if (misplaced) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Fix structure (${misplaced})`,
      title: 'A contents entry inside a paragraph, a list item outside a list, a cell outside its row — '
           + 'each one goes back where PDF/UA allows it, keeping its place in the reading order',
      onclick: fixStructure })));
    if (promptLeft) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Split “Name: ____  Date: ____” rows (${promptLeft})`,
      title: 'A row with several labels and write-in lines tagged as one P, or a blank line that took the '
           + 'colon of its label: each label and its line become their own P, only the underscores are '
           + 'read as the blank line — read from the page, in a file tagged by the tool or imported',
      onclick: fixPrompts })));
    if (blankLeft) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Read write-in lines as “${window.BLANK_LINE || 'Blank Line'}” (${blankLeft})`,
      title: 'A rule to write on ("1. A ______ flew out of its nest") is read as the blank-line wording',
      onclick: fixBlankLines })));
    const tociNoLink = Object.values(model.nodes || {}).filter((n) => n.role === 'TOCI'
      && !n.artifact && !(n.children || []).some((c) => (model.nodes[c] || {}).role === 'Link')).length;
    if (tociNoLink) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Link the contents entries (${tociNoLink})`,
      title: 'Each contents line jumps to the page it names. The text is searched as it is now — '
           + 'the file is not re-analysed and no tag is moved',
      onclick: linkContents })));
    if (pageNums && pageNums.mode === 'P' && pageNums.to_p) {
      box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
        text: `Read page numbers as P (${pageNums.to_p})`,
        title: 'This project reads the printed page number: each one becomes a P where it is printed',
        onclick: () => setPageNumbers('P') })));
    } else if (pageNums && pageNums.mode !== 'P' && pageNums.to_artifact) {
      box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm',
        text: `Leave page numbers out — artifacts (${pageNums.to_artifact})`,
        title: 'The option says page numbers are pagination artifacts, but these are tagged P',
        onclick: () => setPageNumbers('artifact') })));
    }
    if (gridLeft) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Separate words joined into one paragraph (${gridLeft})`,
      title: 'A grid of words ("discount", "index", "return") was joined as if a sentence ran on from '
           + 'one column to the next: each word becomes its own P again, read column by column',
      onclick: () => splitZones() })));
    if (tidyLeft) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Tidy the lists (${tidyLeft})`,
      title: 'A wrapped line that became an item of its own goes back into the item it belongs to, '
           + 'a list broken in two by a note is joined back together, points set further in become '
           + 'a list inside the item above them, and tag text loses the characters that are not '
           + 'really printed',
      onclick: fixLists })));
    if (Object.values(model.nodes || {}).some((n) => n.role === 'Table' && !n.artifact)) {
      box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm',
        text: 'Re-read the text in table cells',
        title: 'Each cell takes the words printed inside it. A row printed as one line '
             + '("M N O") is cut at the cell borders, so no cell is read "Blank cell" over a '
             + 'letter that is printed there',
        onclick: () => rereadCells() })));
    }
    if (cellListLeft) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary',
      text: `Tag the list inside a table cell (${cellListLeft})`,
      title: 'A cell holding a numbered or bulleted list (a rubric score key, a set of instructions) '
           + 'gets L > LI > Lbl + LBody inside it instead of one block of text',
      onclick: fixCellLists })));
    if (altLeft) box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm',
      text: `Alt text from the caption (${altLeft})`,
      title: 'Figures with no alt text that have a caption printed with them take that caption',
      onclick: fixAltFromCaptions })));
    issues.forEach((i) => box.append(el('div', {
      class: 'issue ' + i.severity, onclick: () => {
        if (i.id) selectOnly(i.id, true);
        else if (i.code === 'title') $('docTitle').focus();
        else if (i.code === 'lang') $('docLang').focus();
      }
    }, el('span', { class: 'pg', text: i.page != null && i.page >= 0 ? 'p' + (i.page + 1) : 'doc' }), el('span', { text: i.message }))));
  }
  let blankLeft = 0;
  let promptLeft = 0;
  let altLeft = 0;
  let cellListLeft = 0;
  let tidyLeft = 0;
  let pageNums = null;
  let gridLeft = 0;
  let spanLeft = 0;
  const CELL_LABEL = /(?:^|\s)(?:[•●▪■◦○]|\(?\d{1,3}[.)]|\d{1,3}(?:\s*(?:and|&|to|or|[–—-])\s*\d{1,3})?:)(?=\s)/g;
  async function countFixables() {
    try {
      const r = await UA.get(api('/alt/captions'));
      altLeft = (r.items || []).filter((x) => x.caption).length;
    } catch (e) { altLeft = 0; }
    try { pageNums = await UA.get(api('/pagenumbers')); } catch (e) { pageNums = null; }
    try { gridLeft = ((await UA.get(api('/zones/split'))).ids || []).length; } catch (e) { gridLeft = 0; }
    try { promptLeft = ((await UA.get(api('/prompts'))).ids || []).length; } catch (e) { promptLeft = 0; }
    try {
      const t = await UA.get(api('/lists/tidy'));
      tidyLeft = (t.stray || []).length + (t.junk || []).length
        + (t.split || []).length + (t.nested || []).length + (t.values || []).length
        + (t.shape || []).length + (t.short || []).length;
    } catch (e) { tidyLeft = 0; }
    blankLeft = 0;
    cellListLeft = 0;
    try { spanLeft = ((await UA.get(api('/actualtext/tags'))).ids || []).length; } catch (e) { spanLeft = 0; }
    Object.values(model.nodes || {}).forEach((n) => {
      if ((n.role === 'TD' || n.role === 'TH') && !n.artifact && !(n.children || []).length) {
        const t = ' ' + (n.text || '').trim();
        if (t.length > 20 && (t.match(CELL_LABEL) || []).length >= 2) cellListLeft += 1;
      }
    });
    Object.values(model.nodes || {}).forEach((n) => {
      if (n.artifact || !/_{3,}/.test(n.text || '')) return;
      const kids = (n.children || []).map((c) => model.nodes[c]).filter(Boolean);
      if (!kids.some((k) => k.role === 'Span' && k.style && k.style.blank_line)) blankLeft += 1;
    });
  }
  async function fixBibliography() {
    const busy = el('div', { class: 'toast', text: 'Reading the reference list…' });
    document.body.append(busy);
    let r;
    try { r = await post('/bibliography', {}); } finally { busy.remove(); }
    if (r.error) return;
    await load(focus);
    const links = Object.values(r.links || {}).reduce((a, b) => a + b, 0);
    UA.toast(r.entries ? `${r.entries} reference entr${r.entries === 1 ? 'y' : 'ies'} tagged as BibEntry`
      + ` (${(r.methods || []).join(', ')})` + (links ? ` · ${links} link(s) found again` : '')
      : 'No reference list to tag', 5000);
  }
  async function fixEverything() {
    const btn = $('fixAllBtn');
    if (btn) btn.disabled = true;
    const busy = el('div', { class: 'toast', text: 'Fixing… (this can take a while on a big file)' });
    document.body.append(busy);
    const done = [];
    const step = async (path, body, say) => {
      try {
        const r = await UA.post(api(path), body || {});
        if (r && !r.error) { const m = say(r); if (m) done.push(m); }
      } catch (e) { /* the next step still runs */ }
    };
    try {
      await step('/structure/fix', {}, (r) => r.total && `${r.total} tag(s) put back in place`);
      await step('/lists/tidy', {}, (r) => {
        const n = (r.merged || 0) + (r.joined || 0) + (r.nested || 0) + (r.values || 0) + (r.shaped || 0) + (r.reread || 0) + (r.cleaned || 0);
        return n && `${n} list fix(es)`;
      });
      await step('/bibliography', {}, (r) => r.entries && `${r.entries} reference entr${r.entries === 1 ? 'y' : 'ies'} tagged`);
      await step('/headings/fix', {}, (r) => r.changed && `${r.changed} heading level(s)`);
      await step('/order/fix', {}, (r) => r.moved && `${r.moved} tag(s) back on their page`);
      await step('/labels/fix', {}, (r) => r.fixed && `${r.fixed} list label(s)`);
      const house = (window.OPTS && window.OPTS.house_style || '').toLowerCase();
      if (!house || ['none', 'off'].includes(house)) await step('/headers/fix', {}, (r) => r.changed && `${r.changed} empty header(s) → TD`);
      await step('/actualtext/span', {}, (r) => r.changed && `${r.changed} /ActualText onto a Span`);
      await step('/prompts', {}, (r) => r.rows && `${r.rows} label + write-in row(s) split`);
      await step('/blanklines/fix', {}, (r) => r.fixed && `${r.fixed} write-in line(s)`);
      await step('/cells/lists', {}, (r) => r.fixed && `${r.fixed} list(s) in cells`);
      if (pageNums && pageNums.mode === 'P' && pageNums.to_p) await step('/pagenumbers', { as: 'P' }, (r) => r.changed && `${r.changed} page number(s) as P`);
      else if (pageNums && pageNums.mode !== 'P' && pageNums.to_artifact) await step('/pagenumbers', { as: 'artifact' }, (r) => r.changed && `${r.changed} page number(s) as artifacts`);
      await step('/alt/captions', {}, (r) => r.changed && `${r.changed} alt text(s) from captions`);
      await step('/backmatter', {}, (r) => {
        const n = (r.endnotes || 0) + ((r.index || {}).entries || 0);
        return n && `${(r.index || {}).entries || 0} index entr${((r.index || {}).entries || 0) === 1 ? 'y' : 'ies'}, ${r.endnotes || 0} endnote(s)`;
      });
      // every link found again last, once the tags are where they will stay:
      // contents, footnotes both ways, captions both ways, index page numbers …
      await step('/xref', { link_toc: true, link_web: true },
        (r) => (r.stats || {}).linked && `${r.stats.linked} link(s)`);
    } finally {
      busy.remove();
      if (btn) btn.disabled = false;
    }
    await load(focus);
    await countFixables();
    renderIssues();
    UA.toast(done.length ? 'Fixed: ' + done.join(' · ') : 'Nothing left that a one-click fix can change', 7000);
  }
  async function fixPrompts() {
    const r = await post('/prompts', {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.rows ? `${r.rows} row(s) read again: ${r.tags} P tag(s), each label with its own blank line`
                    : 'No row to split');
  }
  async function fixBackmatter() {
    const busy = el('div', { class: 'toast', text: 'Tagging the index and the notes…' });
    document.body.append(busy);
    let r;
    try { r = await post('/backmatter', {}); } finally { busy.remove(); }
    if (r.error) return;
    await load(focus);
    const ix = r.index || {};
    const links = Object.values(r.links || {}).reduce((a, b) => a + b, 0);
    UA.toast(`${ix.entries || 0} index entr${ix.entries === 1 ? 'y' : 'ies'} (${ix.sub_entries || 0} sub-entries)`
      + ` · ${r.endnotes || 0} endnote(s) · ${links} link(s)`, 5000);
  }

  // --------------------------------------------- reading order, checked by the AI
  const orderAI = { running: false, stop: false, done: 0, total: 0, results: {}, error: '' };
  function aiOrderSection(box) {
    const sec = el('div', { class: 'aiorder' });
    sec.append(el('div', { class: 'small', text: 'Reading order — let the AI look at the page:' }));
    const row = el('div', { class: 'btnrow' });
    if (orderAI.running) {
      row.append(el('span', { class: 'small muted', text: `Checking page ${orderAI.done + 1} of ${orderAI.total}…` }),
        el('button', { class: 'sm', text: 'Stop', onclick: () => { orderAI.stop = true; } }));
    } else {
      row.append(el('button', { class: 'sm', text: 'Check this page', disabled: !AI_ON,
        title: AI_ON ? 'The vision model sees the page with every block numbered in reading order and says whether a reader would go through it that way'
                     : 'The AI is switched off in Settings',
        onclick: () => aiOrder([page]) }),
      el('button', { class: 'sm', text: 'Check every page', disabled: !AI_ON,
        title: 'One page after the other (on a CPU about a minute a page); the pages where the AI reads it differently are listed here with Apply',
        onclick: () => aiOrder([...Array(model.pages).keys()]) }));
    }
    sec.append(row);
    if (orderAI.error) sec.append(el('div', { class: 'tiny muted', text: orderAI.error }));
    const res = Object.values(orderAI.results).sort((a, b) => a.page - b.page);
    const diff = res.filter((r) => !r.same);
    if (res.length) {
      sec.append(el('div', { class: 'tiny muted', text: `${res.length} page(s) checked · ${diff.length} where the AI would read it differently` }));
    }
    diff.forEach((r) => sec.append(el('div', { class: 'issue warning' },
      el('span', { class: 'pg', text: 'p' + (r.page + 1) }),
      el('span', { text: `AI reads: ${r.numbers.join(', ')}${r.why ? ' — ' + r.why : ''}` }),
      el('button', { class: 'sm', text: 'Show', onclick: (e) => { e.stopPropagation(); page = r.page; renderPage(); } }),
      el('button', { class: 'sm primary', text: 'Apply', onclick: async (e) => {
        e.stopPropagation();
        const a = await post('/order/apply', { page: r.page, ids: r.order });
        if (a.error) return;
        delete orderAI.results[r.page];
        await load(focus);
        UA.toast(a.moved ? `Page ${r.page + 1}: ${a.moved} block(s) now read in the AI's order` : 'Nothing moved');
      } }))));
    box.append(sec);
  }
  async function aiOrder(pages) {
    if (orderAI.running) return;
    Object.assign(orderAI, { running: true, stop: false, done: 0, total: pages.length, error: '' });
    renderIssues();
    try {
      for (const p of pages) {
        if (orderAI.stop) break;
        const r = await UA.post(api('/order/ai'), { page: p });
        if (r.error) { orderAI.error = `page ${p + 1}: ${r.error}`; if (/switched off|reach Ollama|not installed/.test(r.error)) break; }
        else orderAI.results[p] = r;
        orderAI.done += 1;
        if ($('tab-issues') && !$('tab-issues').classList.contains('hidden')) renderIssues();
      }
    } finally {
      orderAI.running = false;
      renderIssues();
    }
    const diff = Object.values(orderAI.results).filter((r) => !r.same).length;
    UA.toast(pages.length === 1 ? (orderAI.results[pages[0]] ? (orderAI.results[pages[0]].same
      ? 'The AI reads this page in the same order' : 'The AI would read this page differently — see Issues') : 'No answer')
      : `${orderAI.done} page(s) checked, ${diff} to look at`, 5000);
  }
  async function fixHeadingLevels() {
    const r = await post('/headings/fix', {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.changed ? `${r.changed} heading(s) moved to the level after the one before it`
                       : 'No heading skips a level');
  }
  async function fixStructure() {
    const r = await post('/structure/fix', {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.total ? `${r.total} tag(s) put back where they belong`
                     : 'Every tag is already under a parent PDF/UA allows');
  }
  async function fixBlankLines() {
    const r = await post('/blanklines/fix', {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.fixed ? `${r.fixed} write-in line(s) now read as “${window.BLANK_LINE || 'Blank Line'}”`
                     : 'No write-in line could be read off the page');
  }
  async function linkContents() {
    UA.toast('Searching the text…');
    const r = await post('/xref', { ...((xref || {}).options || {}),
      detect_cross_references: true, link_toc: true, link_web: true });
    if (r.error) return;
    await load(focus);
    await refreshXref(false);
    const st = r.stats || {};
    UA.toast(st.linked ? `${st.linked} link(s) created · ${st.unresolved || 0} mention(s) without a target`
                       : 'No contents entry could be matched to a page — see the Links tab');
  }
  async function splitZones(ids) {
    const r = await post('/zones/split', Array.isArray(ids) && ids.length ? { ids } : {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.split ? `${r.split} tag(s) split into one tag per zone` : 'Nothing to split');
  }
  async function setPageNumbers(as) {
    const r = await post('/pagenumbers', { as });
    if (r.error) return;
    await load(focus);
    UA.toast(r.changed ? `${r.changed} page number(s) ${as === 'P' ? 'now read as P' : 'left out as artifacts'}`
                       : 'Every page number is already that way');
  }
  async function fixLists() {
    const r = await post('/lists/tidy', {});
    if (r.error) return;
    await load(focus);
    const done = [];
    if (r.merged) done.push(`${r.merged} wrapped line(s) back in their item`);
    if (r.joined) done.push(`${r.joined} list(s) joined back together`);
    if (r.nested) done.push(`${r.nested} point(s) moved into the item above them`);
    if (r.values) done.push(`${r.values} value(s) put on the item they are printed beside`);
    if (r.shaped) done.push(`${r.shaped} list(s) given items only, items a label and a body`);
    if (r.reread) done.push(`${r.reread} list(s) read again across their page break`);
    if (r.cleaned) done.push(`${r.cleaned} tag(s) cleaned`);
    UA.toast(done.length ? done.join(' · ') : 'The lists are already tidy');
  }
  async function rereadCells(ids) {
    const r = await post('/cells/reread', Array.isArray(ids) && ids.length ? { ids } : {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.changed ? `${r.changed} cell(s) of ${r.cells} re-read from the page`
                       : `Every cell already holds what is printed in it (${r.cells} checked)`);
  }
  async function fixCellLists(ids) {
    const r = await post('/cells/lists', Array.isArray(ids) && ids.length ? { ids } : {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.fixed ? `${r.fixed} cell(s) now hold a tagged list`
                     : 'No cell holds a list that could be read off the page');
  }
  async function fixAltFromCaptions() {
    const r = await post('/alt/captions', {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.changed ? `${r.changed} figure(s) given the caption as their alt text`
                       : 'No figure has a caption to take');
  }
  async function fixItemLabels() {
    const r = await post('/labels/fix', {});
    if (r.error) return;
    await load(focus);
    const left = r.left ? `, ${r.left} could not be read off the page` : '';
    UA.toast(r.fixed ? `${r.fixed} list item(s) now have their number in a Lbl${left}`
      : (r.left ? `${r.left} item(s) still have the number in the text — the number could not be `
        + 'found on the page; draw the label by hand' : 'Nothing to change'));
  }
  async function fixActualTextSpans() {
    const r = await post('/actualtext/span', {});
    if (r.error) return;
    await load(focus);
    await countFixables();
    renderIssues();
    UA.toast(r.changed ? `${r.changed} tag(s) now read through a Span inside them`
                       : 'Every /ActualText already sits on a Span');
    return r;
  }
  async function fixEmptyHeaders() {
    const r = await post('/headers/fix', {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.changed ? `${r.changed} empty header cell(s) changed to TD` : 'No empty header cells');
    return r;
  }
  async function fixOrder() {
    const r = await post('/order/fix', {});
    if (r.error) return;
    await load(focus);
    UA.toast(r.moved ? `${r.moved} tag(s) moved back to their page` : 'Reading order already follows the pages');
    return r;
  }
  let issueCursor = -1;
  function nextIssue() {
    const list = issues.filter((i) => i.id && i.severity !== 'info');
    const pool = list.length ? list : issues.filter((i) => i.id);
    if (!pool.length) { UA.toast('No issues left'); return; }
    issueCursor = (issueCursor + 1) % pool.length;
    selectOnly(pool[issueCursor].id, true);
  }
  $('nextIssueBtn').addEventListener('click', nextIssue);

  // -------------------------------------------------------------- checks
  async function renderChecks() {
    const box = $('tab-checks');
    box.replaceChildren(el('p', { class: 'muted small', text: 'loading…' }));
    const [f, s] = await Promise.all([UA.get(api('/findings')), UA.get(api('/status'))]);
    box.replaceChildren();
    const sb = s.score_before || {}, sa = s.score_after;
    const line = el('div', { class: 'scoreline' });
    line.append(el('span', { class: 'big', text: sa ? `${sb.errors} → ${sa.errors}` : `${sb.errors ?? '?'}` }),
      el('span', { class: 'muted', text: sa ? 'errors before → after build' : 'errors in the original' }));
    box.append(line);
    if (s.verapdf) box.append(el('p', { class: 'chip ' + (/compliant/.test(s.verapdf) && !/failed/.test(s.verapdf) ? 'ok' : 'warn'), text: s.verapdf }));
    if (s.built) {
      box.append(el('div', { class: 'btnrow' },
        el('a', { class: 'btn sm primary', href: `/job/${JID}/download/pdf`, text: 'Download PDF' }),
        el('a', { class: 'btn sm', href: `/job/${JID}/download/report`, text: 'Report (.xlsx)' }),
        el('a', { class: 'btn sm', href: `/job/${JID}/download/model`, text: 'Tags (.json)' })));
      if (s.dirty) box.append(el('p', { class: 'small', style: 'color:var(--warn)', text: 'Tags changed since the last build — build again to include the edits.' }));
    }
    const list = (sa ? f.after : f.before) || [];
    box.append(el('h2', { style: 'margin:10px 0 6px', text: sa ? 'Result' : 'Original file' }));
    const card = (x) => el('div', { class: 'finding sev-' + x.severity },
      el('span', { class: 'code', text: x.code }), el('span', { text: x.title }),
      el('p', { class: 'small muted', text: [x.clause, x.detail, x.pages && x.pages.length ? 'p. ' + x.pages.slice(0, 12).map((p) => p + 1).join(', ') : ''].filter(Boolean).join(' · ') }));
    const bySev = (sv) => list.filter((x) => x.severity === sv);
    const probs = bySev('error').concat(bySev('warning'));
    if (!probs.length) box.append(el('p', { class: 'small', style: 'color:var(--ok)', text: 'No machine-checkable problems.' }));
    probs.forEach((x) => box.append(card(x)));
    [['info', 'Manual checks'], ['pass', 'Passed']].forEach(([sv, title]) => {
      const items = bySev(sv);
      if (!items.length) return;
      const d = el('details', { class: 'group' }, el('summary', { text: `${title} (${items.length})` }));
      items.forEach((x) => d.append(card(x)));
      box.append(d);
    });
    if (s.applied && s.applied.length) {
      box.append(el('h2', { style: 'margin:10px 0 6px', text: 'Automatic fixes' }));
      s.applied.forEach((a) => box.append(el('div', { class: 'small', text: '✓ ' + a })));
    }
    if (s.preflight && (s.preflight.log || []).length) {
      box.append(el('h2', { style: 'margin:10px 0 6px', text: 'Preflight' }));
      (s.preflight.log || []).forEach((a) => box.append(el('div', { class: 'small', text: '• ' + a })));
      (s.preflight.warnings || []).forEach((a) => box.append(el('div', { class: 'small', style: 'color:var(--warn)', text: '! ' + a })));
    }
  }

  function showTab(name) {
    document.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('on', x.dataset.tab === name));
    ['props', 'issues', 'contrast', 'links', 'checks', 'client', 'data', 'reader', 'review'].forEach((t) => { const x = $('tab-' + t); if (x) x.classList.toggle('hidden', t !== name); });
    dataView.on = name === 'data';
    if (name === 'data') renderData(); else if (dataView.drawn) drawBoxes();
    if (window.READER) { if (name === 'reader') window.READER.show(); else window.READER.hide(); }
    if (window.REVIEW) { if (name === 'review') window.REVIEW.show(); else window.REVIEW.hide(); }
    if (name === 'review' && stage !== 'tags') setStage('tags');
    const pane = $('tab-' + name);
    if (pane) { pane.classList.remove('tabin'); void pane.offsetWidth; pane.classList.add('tabin'); }
    if (name === 'client') renderClient();
    if (name === 'issues') countFixables().then(renderIssues, renderIssues);
    if (name === 'contrast') renderContrast();
    if (name === 'links') renderLinks();
    if (name === 'checks') renderChecks();
  }
  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));

  // ------------------------------------------------------------- client tab (V4.21)
  // the self-learning brain's view of this file (V4.25)
  function brainBox(b) {
    const info = b.brain || {};
    const box = el('div', { class: 'brainbox' });
    const q = info.quality;
    const facts = [];
    if (info.doc_type) facts.push(el('span', { class: 'chip', text: info.doc_type.replace(/_/g, ' ') }));
    if (info.changed) facts.push(el('span', { class: 'chip ok', text: `${info.changed} tag(s) decided by the brain` }));
    if (info.ai_asked) facts.push(el('span', { class: 'chip', text: `${info.ai_asked} asked to the AI · ${info.ai_changed || 0} changed` }));
    if (info.unsure) facts.push(el('span', { class: 'chip warn', text: `${info.unsure} to check (amber)` }));
    if (info.model && info.model.accuracy != null) facts.push(el('span', { class: 'chip', text: `model ${Math.round(info.model.accuracy * 100)} % accurate · ${info.model.examples} examples` }));
    if (info.ai_error) facts.push(el('span', { class: 'chip err', title: info.ai_error, text: 'AI not reached' }));
    const sim = (info.similar || []).map((x) => `${x.file} (${x.client}, ${Math.round(x.sim * 100)} %)`).join(' · ');
    const t = b.task || {};
    const btn = el('button', { class: 'sm primary', text: t.running ? 'Thinking…' : '🧠 Think again', disabled: !!t.running,
      title: 'The brain decides every text tag of this file again — with everything it has learned since (Ctrl+Z puts the tags back)',
      onclick: think });
    box.append(el('div', { class: 'brow' },
      el('div', {}, el('div', { class: 'bq', text: q != null ? q + ' %' : '—' }),
        el('div', { class: 'tiny muted', text: 'Dez AI: tagging confidence' })),
      el('div', { class: 'spacer' }), btn));
    if (facts.length) box.append(el('div', { class: 'bfacts' }, ...facts));
    if (sim) box.append(el('div', { class: 'tiny muted', style: 'margin-top:6px', text: 'Learned most from: ' + sim }));
    const rev = (info.review_first || []).filter((x) => x.id);
    if (rev.length) {
      // V4.27: the doubtful tags to look at first — the answer to one settles the others that look the same
      box.append(el('div', { class: 'tiny muted', style: 'margin-top:8px',
        text: 'Check these first — one answer settles the others that look the same:' }));
      box.append(el('div', { class: 'revlist' }, ...rev.map((x) => el('button', { class: 'sm revitem', type: 'button',
        title: `${x.role} · ${Math.round((x.confidence || 0) * 100)} % sure${x.same_look ? ` · ${x.same_look} more look the same` : ''}`,
        text: `p${(x.page || 0) + 1} \u201c${x.text}\u201d${x.same_look ? ` \u00d7${x.same_look + 1}` : ''}`,
        onclick: () => selectOnly(x.id, true) }))));
    }
    if (!info.version) box.append(el('div', { class: 'tiny muted', style: 'margin-top:6px',
      text: b.on ? 'The brain has not looked at this file yet — press Think again.'
        : 'This file\u2019s profile does not use Dez AI\u2019s brain. Press Think again, or pick “Smart AI (all samples)” in the profile chip.' }));
    if (t.running) box.append(el('div', { class: 'tiny muted', text: `${t.message || 'working'} — ${Math.round((t.progress || 0) * 100)} %` }));
    else if (t.result && t.result.error) box.append(el('div', { class: 'tiny', style: 'color:var(--err)', text: t.result.error }));
    return box;
    async function think() {
      btn.disabled = true;
      const r = await post('/brain/think', {});
      if (r.error) { btn.disabled = false; return; }
      UA.toast('The brain is thinking about every tag of this file…');
      const poll = async () => {
        const st = await UA.get(api('/brain/think'));
        if (st.running) { btn.textContent = `Thinking… ${Math.round((st.progress || 0) * 100)} %`; setTimeout(poll, 1500); return; }
        const res = st.result || {};
        UA.toast(res.error ? res.error : `Brain: ${res.changed || 0} tag(s) changed${res.ai_asked ? `, ${res.ai_asked} asked to the AI` : ''} · confidence ${res.quality ?? '—'} %`, 6000);
        await load(focus);
        renderClient();
      };
      setTimeout(poll, 1200);
    }
  }

  // V4.26: the offline rule book — looked up here, never on the internet
  function ruleBook() {
    const out = el('div', { class: 'rbook' });
    const res = el('div', { class: 'rbres' });
    const inp = el('input', { type: 'search', placeholder: '📖 Look up a rule (offline): heading, caption, table header…',
      'aria-label': 'Look up a rule in the offline rule book' });
    let t = null;
    const go = async () => {
      const q = inp.value.trim();
      if (!q) { res.replaceChildren(); return; }
      const r = await UA.get('/api/kb?k=5&job=' + encodeURIComponent(JID) + '&q=' + encodeURIComponent(q));
      res.replaceChildren(...((r && r.results) || []).map((x) => el('div', { class: 'rbitem' },
        el('div', {}, el('b', { text: x.title }), ' ', el('span', { class: 'chip', text: x.source })),
        el('div', { class: 'tiny muted', text: x.refs }), el('div', { class: 'small', text: x.text }))));
      if (!res.children.length) res.append(el('div', { class: 'tiny muted', text: 'Nothing in the rule book for that.' }));
    };
    inp.addEventListener('input', () => { clearTimeout(t); t = setTimeout(go, 250); });
    out.append(inp, res);
    return out;
  }

  async function renderClient() {
    const box = $('tab-client');
    box.replaceChildren(el('div', { class: 'skel-lines' }, el('i'), el('i'), el('i')));
    const [r, b] = await Promise.all([UA.get(api('/samples/compare')), UA.get(api('/brain'))]);
    box.replaceChildren();
    if (b && !b.error) box.append(brainBox(b));
    box.append(ruleBook());
    const head = el('div', { class: 'clienthead' },
      el('div', {}, el('b', { text: r.client || 'Client' }),
        el('div', { class: 'tiny muted', text: !r.files ? 'no sample files yet'
          : r.scope === 'all' ? `${r.files} sample file(s) of every client · the ${r.client_files || 0} most like this file count most`
          : `${r.files} sample file(s)${r.client_files != null ? ` · ${r.client_files} of this client, the rest common` : ''}` })),
      el('button', { class: 'sm', text: '↻', title: 'Compare again', onclick: renderClient }));
    box.append(head);
    if (!r.files) {
      box.append(el('p', { class: 'small muted', text: 'This client has no sample files. An admin adds finished files in Admin → Sample files — and every approved file becomes one by itself.' }));
      $('clientCount').classList.add('hidden');
      return;
    }
    const nodes = r.nodes || [];
    const n = nodes.length + (r.findings || []).length;
    $('clientCount').textContent = String(n);
    $('clientCount').classList.toggle('hidden', !n);
    if (!n) { box.append(el('div', { class: 'okmark' }, el('span', { text: '✓' }), el('span', { text: 'This file is tagged the way the client\'s samples are.' }))); return; }
    (r.findings || []).forEach((f, i) => {
      const row = el('div', { class: 'cfind k-' + f.kind, style: `animation-delay:${i * 30}ms` }, el('span', { class: 'ctext', text: f.text }));
      if (f.kind === 'caption') row.append(el('button', { class: 'sm', text: 'Put them there', onclick: async () => {
        const res = await UA.post(api('/samples/apply'), { captions: true });
        if (!res.error) { UA.toast(`${res.changed} caption(s) moved — Ctrl+Z puts them back`); await load(); renderClient(); }
      } }));
      if (f.kind === 'never') row.append(el('button', { class: 'sm', text: 'Show', onclick: () => {
        const ids = Object.values(model.nodes).filter((x) => x.role === f.role).map((x) => x.id);
        if (ids.length) selectOnly(ids[0], true);
      } }));
      box.append(row);
    });
    if (nodes.length) {
      box.append(el('div', { class: 'btnrow' }, el('button', { class: 'sm primary', text: `Take all ${nodes.length}`,
        title: 'Give every tag below the tag the samples give it (Ctrl+Z takes it back)',
        onclick: () => take(nodes) })));
      nodes.forEach((x, i) => box.append(el('div', { class: 'csug', style: `animation-delay:${Math.min(i, 20) * 25}ms` },
        el('div', { class: 'cwho', onclick: () => selectOnly(x.id, true) },
          el('span', { class: 'role', text: x.role }), el('span', { class: 'carrow', text: '→' }), el('span', { class: 'role h', text: x.says }),
          el('span', { class: 'ctext', text: x.text })),
        el('div', { class: 'tiny muted', text: x.why }),
        el('div', { class: 'sbarwrap' }, el('i', { style: `width:${Math.round((x.share || 0) * 100)}%` })),
        el('button', { class: 'sm', text: 'Take', onclick: () => take([x]) }))));
    }
    async function take(list) {
      const res = await UA.post(api('/samples/apply'), { items: list.map((x) => ({ id: x.id, role: x.says })) });
      if (!res.error) { UA.toast(`${res.changed} tag(s) changed like the samples — Ctrl+Z takes it back`); await load(); renderClient(); }
    }
  }

  // ------------------------------------------------------------- links tab
  let xref = null;
  async function refreshXref(render) {
    xref = await UA.get(api('/xref'));
    const n = (xref.items || []).length;
    const c = $('linkCount');
    c.textContent = String(n);
    c.classList.toggle('hidden', !n);
    if (render || !$('tab-links').classList.contains('hidden')) renderLinks();
  }
  async function renderLinks() {
    const box = $('tab-links');
    if (!xref) { box.replaceChildren(el('p', { class: 'muted small', text: 'Loading…' })); refreshXref(true); return; }
    box.replaceChildren();
    box.append(el('div', { class: 'hintcard' },
      el('div', { class: 'small' }, el('b', { text: 'The Links stage' }),
        el('span', { class: 'muted', text: ' — set links by hand (one-way or two-way), point a link elsewhere, and check that every link lands where its words say.' })),
      el('button', { class: 'sm primary', text: 'Open the Links stage', onclick: () => setStage('links') })));
    const st = xref.stats || {};
    const items = xref.items || [];
    if (!xref.available || !items.length) {
      box.append(el('p', { class: 'muted small', text: xref.available
        ? 'Nothing was found in the text last time this file was searched. If it has a contents '
          + 'list, printed addresses or “see Figure 3” mentions, search again — the search has '
          + 'improved since this file was analysed.'
        : 'This file has not been searched for cross-references yet (a contents entry, a printed '
          + 'address, “see Figure 3”, “[12]”). Searching reads the text as it is now — the file is '
          + 'not re-analysed and no tag is moved.' }));
      const want = { detect_cross_references: true, link_web: true, link_toc: true };
      box.append(el('div', { class: 'btnrow' }, el('button', {
        class: 'primary sm', text: 'Search the text for links now', onclick: async () => {
          UA.toast('Searching the text…');
          const r = await post('/xref', { ...(xref.options || {}), ...want });
          if (r.error) return;
          await load();
          await refreshXref(true);
          const st = r.stats || {};
          UA.toast(st.linked ? `${st.linked} link(s) created, ${st.unresolved || 0} without a target`
                             : 'Nothing in the text points anywhere in this file');
        } })));
      return;
    }
    box.append(el('div', { class: 'scoreline' }, el('span', { class: 'big', text: String(items.length) }),
      el('span', { class: 'muted', text: `cross-reference(s) found · ${st.linked || 0} are links · ${st.unresolved || 0} without a target` })));
    box.append(el('p', { class: 'tiny muted', text: 'Everything here was found in the text of this file (a printed address, a contents line, “see Figure 3”, “[12]”, an index page number) and matched to its target in the same file. Tick a kind to turn those into real links; the look of the page does not change (the link has no border). Items with no target are listed but never linked.' }));
    const kinds = {};
    items.forEach((i) => { (kinds[i.kind] = kinds[i.kind] || []).push(i); });
    const opts = xref.options || {};
    const pend = { ...opts };
    const rows = el('div', { class: 'xkinds' });
    Object.keys(xref.labels).forEach((k) => {
      const list = kinds[k] || [];
      if (!list.length) return;
      const okCount = list.filter((i) => i.target).length;
      const optName = xref.kind_option[k];
      const cb = el('input', { type: 'checkbox' });
      cb.checked = !!opts[optName];
      cb.addEventListener('change', () => { pend[optName] = cb.checked; });
      rows.append(el('label', { class: 'row small' }, cb,
        el('span', { text: `${xref.labels[k]} — ${list.length} found, ${okCount} with a target` })));
    });
    const tagCb = el('input', { type: 'checkbox' });
    tagCb.checked = !!opts.cross_reference_tags;
    tagCb.addEventListener('change', () => { pend.cross_reference_tags = tagCb.checked; });
    rows.append(el('label', { class: 'row small' }, tagCb,
      el('span', { text: 'Also tag every mention as Reference (PDF/UA semantics, adds an inline tag)' })));
    box.append(rows);
    box.append(el('div', { class: 'btnrow' },
      el('button', { class: 'primary sm', text: 'Apply and search again', onclick: async () => {
        UA.toast('Searching the text again…');
        const r = await post('/xref', pend);
        if (r.error) return;
        await load();
        await refreshXref(true);
        UA.toast(`${(r.stats || {}).linked || 0} link(s) in the tags now`);
      } })));
    const order = { web: 0, email: 1, toc: 2, index: 3, section: 4, figure: 5, reference: 6, page: 7,
      footnote: 8, footnote_back: 9, caption_back: 10 };
    [...items].sort((a, b) => (order[a.kind] ?? 9) - (order[b.kind] ?? 9) || a.page - b.page)
      .slice(0, 400).forEach((i) => {
        const t = i.target || {};
        const dest = t.uri ? t.uri : (t.page != null ? `page ${t.page + 1}${t.label ? ' · ' + t.label : ''}` : '');
        box.append(el('div', { class: 'citem', onclick: () => { page = i.page; renderPage(); } },
          el('div', {}, el('div', { class: 'small', text: i.text.slice(0, 90) }),
            el('div', { class: 'cmeta', text: [`p${i.page + 1}`, xref.labels[i.kind] || i.kind,
              dest ? '→ ' + dest.slice(0, 70) : (i.note || 'no target found')].filter(Boolean).join(' · ') }))));
      });
    if (items.length > 400) box.append(el('p', { class: 'tiny muted', text: `… and ${items.length - 400} more` }));
  }

  const METHOD_LABEL = {
    auto: 'Auto (best for each item)',
    background: 'Box colour (background lighter/darker)',
    text: 'Text colour',
    stroke: 'Outline / stroke (PitStop Fill & Stroke)',
  };
  const METHOD_SHORT = { background: 'box colour', text: 'text colour', stroke: 'outline' };
  const METHOD_HELP = {
    auto: 'Auto: light text on a coloured box (page-number badge, table header) → the box gets darker and the text keeps its colour; dark text → the text colour or the box colour, whichever changes less; when no colour works (busy background) → a thin outline.',
    background: 'Box colour: the solid box behind the text is made darker (behind light text) or lighter (behind dark text); the text keeps its colour. Only for text on a solid box.',
    text: 'Text colour: the text colour moves lighter or darker (hue kept) until it reaches the ratio.',
    stroke: 'Outline: every letter gets a thin stroke (text render mode Fill + Stroke) — the same as PitStop Inspector → Fill and Stroke → Stroke, Black, 0.5 pt. Colours stay as they are.',
  };
  function renderContrast() {
    const box = $('tab-contrast');
    box.replaceChildren();
    if (!contrast || !contrast.available) {
      box.append(el('p', { class: 'muted small', text: 'Contrast was not measured for this file (option “Check colour contrast” was off).' }),
        el('button', { class: 'sm', text: 'Measure now', onclick: recheck }));
      return;
    }
    const sm = contrast.summary;
    box.append(el('div', { class: 'scoreline' },
      el('span', { class: 'big', text: String(sm.open) }),
      el('span', { class: 'muted', text: `text run(s) below WCAG ${contrast.level} · ${contrast.checked} measured` + (sm.fixed ? ` · ${sm.fixed} fixed` : '') })));
    const lvl = el('select', { class: 'sm' }, el('option', { value: 'AA', text: 'AA (4.5:1)' }), el('option', { value: 'AAA', text: 'AAA (7:1)' }));
    lvl.value = contrast.level;
    const fix = contrast.fix || { method: 'auto', stroke_width: 0.5, stroke_color: 'black' };
    const canFix = (i) => i.fixable ? !!i.fixable[fix.method] : !!(i.suggested || i.suggested_bg) && i.zone !== 'figure';
    const fixableOpen = contrast.issues.filter((i) => canFix(i) && i.status !== 'fixed');
    const task = contrast.task || {};
    // how to fix: box colour up/down, text colour, or an outline (PitStop Fill & Stroke)
    const mSel = el('select', { class: 'sm', title: 'Used by Fix, Fix all now and Build' },
      Object.entries(METHOD_LABEL).map(([v, t]) => el('option', { value: v, text: t })));
    mSel.value = fix.method;
    const wIn = el('input', { type: 'number', class: 'sm', min: '0.1', max: '3', step: '0.1', value: String(fix.stroke_width),
      style: 'width:4.5em', title: 'Outline width in points (PitStop: 0.5 pt)' });
    const colSel = el('select', { class: 'sm', title: 'Outline colour' },
      el('option', { value: 'black', text: 'Black' }), el('option', { value: 'white', text: 'White' }),
      el('option', { value: 'auto', text: 'Black or white (best)' }));
    colSel.value = fix.stroke_color;
    const saveFix = async () => {
      const r = await post('/contrast/method', { method: mSel.value, stroke_width: wIn.value, stroke_color: colSel.value });
      if (!r.error) await refreshContrast();
      renderContrast();
    };
    [mSel, wIn, colSel].forEach((x) => x.addEventListener('change', saveFix));
    box.append(el('div', { class: 'btnrow' }, el('span', { class: 'small', text: 'Fix method' }), mSel,
      fix.method === 'stroke' ? el('span', { class: 'row small' }, 'width', wIn, 'pt', colSel) : ''));
    const fixRow = el('div', { class: 'btnrow' },
      el('button', { class: 'primary sm', text: `Fix all now (${fixableOpen.length})`, disabled: !fixableOpen.length || task.running,
        title: 'Change the PDF now with the fix method above and re-measure', onclick: () => fixNow(null) }),
      contrast.can_undo_fix ? el('button', { class: 'sm', text: 'Undo colour fixes', disabled: task.running,
        title: 'Put every original colour back (outlines too)', onclick: undoFix }) : '');
    box.append(fixRow);
    if (task.running) box.append(el('p', { class: 'small', id: 'cfixMsg', text: `Fixing… ${task.message || ''} ${Math.round((task.progress || 0) * 100)}%` }));
    box.append(el('div', { class: 'btnrow' },
      lvl, el('button', { class: 'sm', text: 'Re-measure', onclick: () => recheck(lvl.value) }),
      el('button', { class: 'sm', text: 'Tick all', title: 'For Build option “Fix low-contrast colours” = selected', onclick: () => tick(null, true) }),
      el('button', { class: 'sm', text: 'Untick all', onclick: () => tick(null, false) })));
    box.append(el('p', { class: 'tiny muted', text: METHOD_HELP[fix.method] + ' Fix changes the PDF right away, re-measures the page and shows the result here and on the page. Undo puts the originals back. Anything not fixed here is fixed at Build with the same method when “Fix low-contrast colours” = all. Agree design changes with the client.' }));
    if (!contrast.issues.length) { box.append(el('p', { class: 'small', style: 'color:var(--ok)', text: 'All measured text meets the minimum.' })); return; }
    const order = { error: 0, warning: 1, info: 2 };
    [...contrast.issues].sort((a, b) => (a.status === 'fixed') - (b.status === 'fixed') || order[a.severity] - order[b.severity] || a.page - b.page)
      .forEach((i) => {
        const fixable = canFix(i);
        const cb = el('input', { type: 'checkbox', title: fixable ? 'Fix on build' : 'This fix method cannot fix it — choose another', disabled: !fixable || i.status === 'fixed' });
        cb.checked = !!i.apply;
        cb.addEventListener('click', (e) => e.stopPropagation());
        cb.addEventListener('change', () => tick([i.id], cb.checked));
        const sample = (fg, t) => el('span', { class: 'sw', style: `color:${fg};background:${i.bg}`, text: t });
        const sw = el('div', { class: 'swatches' }, sample(i.fg, 'Aa'),
          el('span', { class: 'ratio bad', text: `${i.ratio}:1` }), el('span', { class: 'cmeta', text: `needs ${i.required}:1` }));
        const how = !fixable || i.status === 'fixed' ? '' : fix.method === 'auto' ? (i.auto_method || (i.suggested_bg ? 'background' : 'text')) : fix.method;
        if (how === 'background') sw.append(el('span', { class: 'cmeta', text: '→ box' }),
          el('span', { class: 'sw', style: `color:${i.fg};background:${i.suggested_bg}`, text: 'Aa' }),
          el('span', { class: 'cmeta mono', text: i.suggested_bg }));
        else if (how === 'text') sw.append(el('span', { class: 'cmeta', text: '→' }), sample(i.suggested, 'Aa'),
          el('span', { class: 'cmeta mono', text: i.suggested }));
        else if (how === 'stroke') {
          const o = i.outline || {};
          const want = fix.method === 'stroke' ? fix.stroke_color : 'auto';
          const c = want === 'auto' ? ((o.white || 0) > (o.black || 0) ? 'white' : 'black') : want;
          const px = Math.max(0.5, Math.min(2, (fix.method === 'stroke' ? fix.stroke_width : 0.5) * 1.4));
          sw.append(el('span', { class: 'cmeta', text: '→ outline' }),
            el('span', { class: 'sw', style: `color:${i.fg};background:${i.bg};-webkit-text-stroke:${px}px ${c === 'white' ? '#fff' : '#000'}`, text: 'Aa' }),
            o[c] ? el('span', { class: 'cmeta mono', title: 'expected ratio with the outline', text: `≈${o[c]}:1` }) : '');
        }
        if (fixable && i.status !== 'fixed') {
          sw.append(el('button', { class: 'sm fixone', text: 'Fix', disabled: !!task.running, title: 'Fix this one now',
            onclick: (e) => { e.stopPropagation(); fixNow([i.id]); } }));
        }
        const withTxt = i.fixed_with ? ` (${i.fixed_detail || METHOD_SHORT[i.fixed_with] || i.fixed_with})` : '';
        const status = i.status === 'fixed' ? '✓ fixed' + withTxt : i.status === 'still failing' ? `still ${i.ratio_after}:1` + withTxt : '';
        const where = { content: '', artifact: 'artifact (header/footer)', figure: 'inside a figure' }[i.zone];
        const item = el('div', { class: 'citem' + (cSel === i.id ? ' sel' : ''), onclick: () => {
          cSel = i.id;
          if (page !== i.page) { page = i.page; renderPage(); } else drawBoxes();
          box.querySelectorAll('.citem').forEach((x) => x.classList.toggle('sel', x.dataset.cid === i.id));
          const cbx = overlay.querySelector(`.cbox[data-cid="${CSS.escape(i.id)}"]`);
          if (cbx) cbx.scrollIntoView({ block: 'center', inline: 'nearest' });
        } }, cb,
          el('div', {}, el('div', { class: 'small', text: i.text.slice(0, 140) }),
            el('div', { class: 'cmeta', text: [`p${i.page + 1}`, `${i.size}pt${i.bold ? ' bold' : ''}${i.large ? ' (large)' : ''}`, where,
              i.uniform ? '' : 'varying background', i.has_outline ? 'outlined text' : '', status].filter(Boolean).join(' · ') }),
            (i.fix_note || i.note) ? el('div', { class: 'cmeta', style: 'color:var(--warn)', text: i.fix_note || i.note }) : ''),
          sw);
        item.dataset.cid = i.id;
        box.append(item);
      });
  }
  async function fixNow(ids, resume) {
    if (!resume) {
      const r = await post('/contrast/fix', ids ? { ids } : { all: true });
      if (r.error) return;
      UA.toast(ids ? 'Fixing the colour…' : 'Fixing all colours…');
    }
    fixPolling = true;
    if (!resume) await refreshContrast();
    const poll = async () => {
      const t = await UA.get(api('/contrast/fix'));
      if (t.running) {
        const m = $('cfixMsg');
        if (m) m.textContent = `Fixing… ${t.message || ''} ${Math.round((t.progress || 0) * 100)}%`;
        setTimeout(poll, 1000);
        return;
      }
      const res = t.result || {};
      fixPolling = false;
      workVer = Date.now() % 100000;
      await refreshContrast();
      renderPage();
      if (res.error) UA.toast(res.error);
      else if (res.message) UA.toast(res.message);
      else UA.toast(`${res.fixed || 0} fixed` + (res.still ? `, ${res.still} still below the minimum` : '') + (res.skipped ? `, ${res.skipped} could not be changed` : ''));
    };
    poll();
  }
  async function undoFix() {
    if (!confirm('Put all original colours back?')) return;
    const r = await post('/contrast/undo_fix', {});
    if (r.error) return;
    workVer = Date.now() % 100000;
    await refreshContrast();
    renderPage();
    UA.toast('Original colours restored');
  }
  async function tick(ids, apply) {
    const r = await post('/contrast', ids ? { ids, apply } : { all_fixable: true, apply });
    if (!r.error) { await refreshContrast(); drawBoxes(); UA.toast(`${r.ticked} contrast fix(es) ticked`); }
  }
  async function recheck(level) {
    UA.toast('Measuring contrast…');
    const r = await post('/contrast/recheck', { level: typeof level === 'string' ? level : undefined });
    if (!r.error) { await refreshContrast(); drawBoxes(); renderContrast(); }
  }

  // --------------------------------------------------------------- build
  $('buildBtn').addEventListener('click', async () => {
    await refreshIssues();
    const errs = issues.filter((i) => i.severity === 'error');
    $('buildIssues').replaceChildren();
    const ord = issues.filter((i) => i.code === 'order');
    if (ord.length) {
      // final reading-order check before the PDF is written
      const pages = [...new Set(ord.map((i) => i.page + 1))].slice(0, 8).join(', ');
      const fixBtn = el('button', { class: 'sm primary', text: 'Fix reading order', onclick: async () => {
        fixBtn.disabled = true;
        await fixOrder();
        await refreshIssues();
        const left = issues.filter((i) => i.code === 'order').length;
        box.replaceChildren(el('span', { text: left ? `${left} tag(s) still out of page order — check them in the Issues tab.` : '✓ Reading order follows the pages.' }));
        box.className = 'issue ' + (left ? 'warning' : 'ok');
      } });
      const showBtn = el('button', { class: 'sm', text: 'Show', onclick: () => { $('buildDlg').close(); selectOnly(ord[0].id, true); } });
      const box = el('div', { class: 'issue error' }, el('span', {
        text: `Reading order: ${ord.length} tag(s) are read out of page order (page ${pages}). Fix before building so the screen reader reads the pages in order. `
      }), fixBtn, ' ', showBtn);
      $('buildIssues').append(box);
    } else {
      $('buildIssues').append(el('div', { class: 'issue ok' }, el('span', { text: '✓ Reading order checked: every tag is read in page order.' })));
    }
    if (errs.length) {
      $('buildIssues').append(el('div', { class: 'issue warning' }, el('span', {
        text: `${errs.length} issue(s) in the tag tree still need attention (e.g. ${errs[0].message}). You can build anyway and fix them later.`
      })));
    }
    $('buildResult').replaceChildren();
    $('buildProgress').classList.add('hidden');
    $('buildGo').disabled = false;
    $('buildDlg').showModal();
  });
  $('buildGo').addEventListener('click', async () => {
    const opts = {};
    document.querySelectorAll('#buildOpts [data-opt]').forEach((i) => {
      let v = i.type === 'checkbox' ? i.checked : i.value;
      if (i.dataset.type === 'int') v = parseInt(v, 10);
      opts[i.dataset.opt] = v;
    });
    $('buildGo').disabled = true;
    const r = await post('/build', { options: opts });
    if (r.error) { $('buildGo').disabled = false; return; }
    $('buildProgress').classList.remove('hidden');
    $('buildStop').disabled = false;
    $('buildStop').onclick = async () => { $('buildStop').disabled = true; await UA.post(api('/stop')); };
    const poll = async () => {
      const s = await UA.get(api('/status'));
      $('buildMsg').textContent = queueText(s) || s.message || s.stage;
      $('buildBar').style.width = Math.round((s.progress || 0) * 100) + '%';
      if (s.stage === 'building') { setTimeout(poll, 900); return; }
      $('buildProgress').classList.add('hidden');
      $('buildGo').disabled = false;
      if (s.stage === 'error' || s.build_error) {
        $('buildResult').replaceChildren(
          el('div', { class: 'issue error' }, el('span', { text: 'The build failed. Your tags are unchanged — fix the cause and build again.' })),
          el('pre', { class: 'trace', text: s.build_error || s.error || 'build failed' }));
        return;
      }
      window.META.built = true;
      refreshContrast().then(drawBoxes);
      const b = s.score_before || {}, a = s.score_after || {}, st = s.stats || {};
      $('buildResult').replaceChildren(
        el('div', { class: 'scoreline' }, el('span', { class: 'big', text: `${b.errors} → ${a.errors}` }),
          el('span', { class: 'muted', text: `errors · warnings ${b.warnings} → ${a.warnings} · pass rate ${b.percent}% → ${a.percent}%` })),
        s.verapdf ? el('p', { class: 'small', text: s.verapdf }) : '',
        el('p', { class: 'small muted', text: `${st.elements} elements, ${st.mcids} marked-content IDs, ${st.split_ops} text runs split at tag edges, ` +
          `${st.annots_tagged || 0} annotations tagged, ${st.pruned || 0} empty tags removed, ${st.unmatched || 0} glyphs outside every tag, ` +
          `${st.missing_alt || 0} figure(s) still without alt text.` }),
        el('div', { class: 'btnrow' },
          el('a', { class: 'btn primary', href: `/job/${JID}/download/pdf`, text: 'Download PDF' }),
          el('a', { class: 'btn', href: `/job/${JID}/download/report`, text: 'Report (.xlsx)' })),
        s.report_error ? el('div', { class: 'issue warning' }, el('span', { text: s.report_error })) : '',
        st.contrast_summary ? el('p', { class: 'small', text: `Colour contrast: ${st.contrast_summary.fixed} fixed, ${st.contrast_summary.open} still below the minimum.` }) : '',
        el('p', { class: 'tiny muted', text: 'Confirm final conformance in PAC 2024 or veraPDF.' }));
      if (!$('tab-checks').classList.contains('hidden')) renderChecks();
    };
    poll();
  });

  // ------------------------------------------------------------ preview
  $('previewBtn').addEventListener('click', () => {
    const out = [];
    const txt = (n) => (n.text || '').trim();
    const say = (k, t, lang) => out.push([k, t, lang]);
    const walk = (id) => {
      const n = model.nodes[id];
      if (!n || n.artifact) return;
      const r = n.role;
      if (/^H[1-6]$/.test(r)) say(`heading level ${r[1]}`, txt(n), n.lang);
      else if (r === 'Figure' || r === 'Formula') say(r === 'Figure' ? 'graphic' : 'formula', n.alt || '⚠ no alternative text', n.lang);
      else if (r === 'L') say('list', `with ${n.children.filter((c) => model.nodes[c] && model.nodes[c].role === 'LI').length} items`);
      else if (r === 'LI') {
        // read the item like a screen reader: label + body (ActualText / blank lines included)
        const read = (nid) => {
          const k = model.nodes[nid];
          if (!k || k.artifact || k.role === 'L') return '';
          if (k.actual_text) return k.actual_text;
          if (k.blank_line) return blankText(k);
          // "I can ." + blank Span -> "I can Blank Line ." (the "." follows the line)
          const words = txt(k).split(/\s+/).filter(Boolean);
          const tail = words.length > 1 && /^[.,;:!?…]+$/.test(words[words.length - 1]) ? words.pop() : '';
          return [words.join(' '), ...k.children.map(read), tail].filter(Boolean).join(' ');
        };
        const kids = n.children.map((c) => model.nodes[c]).filter(Boolean);
        const lbl = kids.filter((k) => k.role === 'Lbl').map((k) => read(k.id)).join(' ');
        const body = kids.filter((k) => k.role !== 'Lbl' && k.role !== 'L').map((k) => read(k.id)).join(' ');
        say('list item', [lbl, body].filter(Boolean).join(' ') || '(empty)', n.lang);
        const nested = [];
        const findL = (k) => k.children.forEach((c) => { const x = model.nodes[c]; if (!x) return; if (x.role === 'L') nested.push(c); else findL(x); });
        findL(n);
        nested.forEach(walk);
        return;
      }
      else if (r === 'Lbl') { return; }
      else if (r === 'LBody') say('•', txt(n), n.lang);
      else if (r === 'Table') {
        const rows = [];
        const collect = (tid) => model.nodes[tid].children.forEach((c) => {
          const k = model.nodes[c];
          if (!k) return;
          if (k.role === 'TR') rows.push(k); else if (['THead', 'TBody', 'TFoot'].includes(k.role)) collect(c);
        });
        collect(id);
        const cols = Math.max(0, ...rows.map((rw) => rw.children.reduce((s, c) => s + ((model.nodes[c] || {}).colspan || 1), 0)));
        say('table', `with ${rows.length} rows and ${cols} columns`);
        const blank = window.BLANK_TEXT || 'Blank cell';
        rows.forEach((rw, i) => say(`row ${i + 1}`, rw.children.map((c) => {
          const k = model.nodes[c] || {};
          const t = k.text || (k.children && k.children.length ? '' : (k.actual_text || blank));
          return (k.role === 'TH' ? '[header] ' : '') + (t || '');
        }).join('  |  ')));
        n.children.forEach((c) => { if ((model.nodes[c] || {}).role === 'Caption') say('caption', txt(model.nodes[c])); });
        return;
      } else if (r === 'Link') say('link', txt(n) || '(no text)');
      else if (r === 'Form') say('form field', txt(n) || '(no label)');
      else if (r === 'Annot') say('annotation', txt(n) || '(no description)');
      else if (r === 'TOC') say('table of contents', '');
      else if (r === 'Note') say('note', txt(n), n.lang);
      else if (r === 'Caption') say('caption', txt(n), n.lang);
      else if (n.blank_line) { say('', blankText(n)); return; }
      else if (!CONTAINERS.has(r) && txt(n) && n.children.some((c) => (model.nodes[c] || {}).blank_line)) {
        // "Number Accurate _______ of 26" is read "Number Accurate Blank Line of 26"
        let t = txt(n);
        const x0 = (n.rects && n.rects[0]) ? n.rects[0][1] : null;
        n.children.forEach((c) => {
          const k = model.nodes[c];
          if (k && k.blank_line) {
            const rep = blankText(k);
            if (/_{2,}/.test(t)) t = t.replace(/_{2,}/, rep);       // a line inside the sentence
            else if (x0 != null && k.rects && k.rects[0] && k.rects[0][1] <= x0 + 2) t = `${rep} ${t}`;
            else t = `${t} ${rep}`;                                   // "Name:" + its line
          }
        });
        say('', t, n.lang);
        return;
      }
      else if (!CONTAINERS.has(r) && txt(n)) {
        say('', txt(n), n.lang);
        // a Span, Quote … inside the paragraph is part of its words, read once (V4.21.4)
        if (n.children.every((c) => !model.nodes[c] || INLINE.has(model.nodes[c].role))) return;
      }
      n.children.forEach(walk);
    };
    model.roots.forEach(walk);
    const body = $('srBody');
    body.replaceChildren(...out.map(([k, t, lang]) => el('div', { class: 'u' },
      k ? el('span', { class: 'k', text: k }) : '', lang ? el('span', { class: 'k', text: `[${lang}]` }) : '', t)));
    $('previewDlg').showModal();
  });

  // ------------------------------------------------------------------ AI
  $('aiBtn').addEventListener('click', () => { $('aiOut').replaceChildren(); $('aiDlg').showModal(); });
  $('aiAltAll').addEventListener('click', async (e) => {
    e.target.disabled = true;
    $('aiOut').replaceChildren(el('p', { class: 'small', text: 'Drafting… this can take a while for many figures.' }));
    const q = await UA.post(api('/ai/alt_all'));
    if (q.error) { e.target.disabled = false; $('aiOut').replaceChildren(el('p', { class: 'small', text: q.error })); return; }
    const stop = el('button', { class: 'sm', text: 'Stop', onclick: async () => { stop.disabled = true; await UA.post(api('/stop')); } });
    const msg = el('span', { class: 'small', text: 'Queued…' });
    $('aiOut').replaceChildren(el('p', {}, msg, ' ', stop));
    const pollAi = async () => {
      const t = await UA.get(api('/ai/alt_all'));
      if (t.running) {
        msg.textContent = `${t.message || 'working'} — ${Math.round((t.progress || 0) * 100)}%. You can close this dialog; editing is locked until it finishes.`;
        setTimeout(pollAi, 1500); return;
      }
      e.target.disabled = false;
      const r = t.result || {};
      $('aiOut').replaceChildren(el('p', { class: 'small', text: r.error && !r.drafted ? r.error :
        `${r.drafted || 0} drafted, ${r.decorative || 0} look decorative, ${r.failed || 0} failed.` + (r.error ? ' ' + r.error : '') + ' Review each one in the Figures filter.' }));
      await load(focus);
    };
    pollAi();
  });
  $('aiRoles').addEventListener('click', async (e) => {
    e.target.disabled = true;
    $('aiOut').replaceChildren(el('p', { class: 'small', text: 'Reviewing…' }));
    const r = await UA.post(api('/ai/roles'));
    e.target.disabled = false;
    if (r.error) { $('aiOut').replaceChildren(el('p', { class: 'small', text: r.error })); return; }
    if (!r.suggestions.length) { $('aiOut').replaceChildren(el('p', { class: 'small', text: 'The model agrees with every tag.' })); return; }
    $('aiOut').replaceChildren(...r.suggestions.map((s) => el('div', { class: 'sugg' },
      el('span', { class: 'role', text: s.from }), '→', el('span', { class: 'role h', text: s.to }),
      el('span', { class: 'ntext', text: `p${s.page + 1}: ${s.text}` }), el('span', { class: 'tiny muted', text: s.why }),
      el('button', { class: 'sm', text: 'show', onclick: () => { $('aiDlg').close(); selectOnly(s.id, true); } }),
      el('button', { class: 'sm', text: 'apply', onclick: async (ev) => {
        const x = await post(`/node/${s.id}`, { role: s.to });
        if (!x.error) { ev.target.textContent = 'applied'; ev.target.disabled = true; await load(focus); }
      } }))));
  });

  // ---------------------------------------------------- other AI helpers
  const AI_RUNS = {
    aiHeads: ['/ai/headings', 'Checking the heading levels…', 'The model agrees with every heading level.'],
    aiLinks: ['/ai/links', 'Looking at the links…', 'Every link already describes itself.'],
    aiFields: ['/ai/fields', 'Reading the text around each field…', 'Every form field already has a tooltip.'],
    aiTables: ['/ai/tables', 'Summarising the tables…', 'Every table already has a summary.'],
    aiMath: ['/ai/math', 'Looking at the formulas…', 'Nothing to change on the formulas.'],
  };
  Object.entries(AI_RUNS).forEach(([btn, [path, busy, none]]) => {
    const b = $(btn);
    if (!b) return;
    b.addEventListener('click', async (e) => {
      e.target.disabled = true;
      $('aiOut').replaceChildren(el('p', { class: 'small', text: busy }));
      const r = await UA.post(api(path));
      e.target.disabled = false;
      if (r.error) { $('aiOut').replaceChildren(el('p', { class: 'small', text: r.error })); return; }
      const list = r.suggestions || [];
      if (!list.length) { $('aiOut').replaceChildren(el('p', { class: 'small', text: none })); return; }
      $('aiOut').replaceChildren(...list.map((s) => el('div', { class: 'sugg' },
        el('span', { class: 'role', text: String(s.from).slice(0, 28) }), '→',
        el('span', { class: 'role h', text: String(s.to).slice(0, 90) }),
        el('span', { class: 'ntext', text: `p${s.page + 1}: ${s.text || ''}` }),
        el('span', { class: 'tiny muted', text: s.why || '' }),
        el('button', { class: 'sm', text: 'show', onclick: () => { $('aiDlg').close(); selectOnly(s.id, true); } }),
        el('button', { class: 'sm', text: 'apply', onclick: async (ev) => {
          const x = await post(`/node/${s.id}`, { [s.field || 'alt']: s.to });
          if (!x.error) { ev.target.textContent = 'applied'; ev.target.disabled = true; await load(focus); }
        } }))));
    });
  });

  // ------------------------------------------------------ auto-tag pages
  $('supportBtn').addEventListener('click', () => {
    window.location.href = `/job/${JID}/support/${page}.zip`;
  });
  $('reanBtn').addEventListener('click', () => {
    const o = window.OPTS || {};
    // "ask" is answered at upload; re-analysing is an explicit choice of its own
    $('reanMode').value = (o.existing_tags === 'ask' ? 'auto' : o.existing_tags) || 'auto';
    $('reanAiTable').checked = !!o.ai_table_check;
    $('reanAiAlt').checked = !!o.ai_alt_text_auto;
    $('reanOut').textContent = '';
    $('reanDlg').showModal();
  });
  $('reanGo').addEventListener('click', async () => {
    if (!confirm('Replace ALL tags in this file with a fresh analysis?')) return;
    $('reanGo').disabled = true;
    const r = await fetch(`/job/${JID}/reanalyze`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ options: { existing_tags: $('reanMode').value, ai_table_check: $('reanAiTable').checked,
        ai_alt_text_auto: $('reanAiAlt').checked } }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) { $('reanOut').textContent = d.error || 'could not start'; $('reanGo').disabled = false; return; }
    location.reload();
  });
  // ---- a two-page spread becomes two pages
  let spreadPages = [];
  $('splitBtn').addEventListener('click', async () => {
    $('splitOut').textContent = '';
    $('splitInfo').textContent = 'Looking at the page sizes…';
    $('splitDlg').showModal();
    const r = await UA.get(api('/pages/spreads'));
    spreadPages = r.pages || [];
    const here = spreadPages.includes(page);
    $('splitInfo').textContent = here
      ? `Page ${page + 1} is about twice as wide as the other pages — two pages side by side.`
      : `Page ${page + 1} does not look like a spread (it is not about twice as wide as the other pages). `
        + 'You can still split it.';
    $('splitAll').textContent = `Split every double page (${spreadPages.length})`;
    $('splitAll').classList.toggle('hidden', spreadPages.length < 2);
    $('splitUndo').classList.toggle('hidden', !r.can_unsplit);
  });
  async function doSplit(body) {
    $('splitGo').disabled = $('splitAll').disabled = true;
    $('splitOut').textContent = 'Splitting…';
    const r = await post('/pages/split', { ...body, where: parseFloat($('splitWhere').value) });
    $('splitGo').disabled = $('splitAll').disabled = false;
    if (r.error) { $('splitOut').textContent = r.error; return; }
    workVer = Date.now() % 100000;
    $('splitDlg').close();
    await load(null);
    page = Math.min(page, (model.pages || 1) - 1);
    renderPage();
    UA.toast(`${r.split} page(s) split — the file has ${r.pages} pages now; ${r.moved} tag(s) moved to the right-hand page`);
  }
  $('splitGo').addEventListener('click', () => doSplit({ page }));
  $('splitAll').addEventListener('click', () => {
    if (!confirm(`Split all ${spreadPages.length} double pages?`)) return;
    doSplit({ all: true });
  });
  $('splitUndo').addEventListener('click', async () => {
    const r = await post('/pages/unsplit', {});
    if (r.error) { $('splitOut').textContent = r.error; return; }
    workVer = Date.now() % 100000;
    $('splitDlg').close();
    await load(null);
    page = Math.min(page, (model.pages || 1) - 1);
    renderPage();
    UA.toast(`The last split is put back — ${r.pages} pages`);
  });

  // Tags on pages (V4.21.4): this page / from … to … / a list / the whole file —
  // Auto-tag (the upload proposal), Re-analyse (read again), Remove tags
  function openPages(focusAct) {
    $('pgThis').textContent = ` — page ${page + 1}`;
    $('pgAll').textContent = ` — ${model.pages} page${model.pages === 1 ? '' : 's'}`;
    $('pgFrom').value = page + 1;
    $('pgTo').value = Math.min(model.pages, page + 1);
    $('pgFrom').max = $('pgTo').max = model.pages;
    if (!$('retagPages').value) $('retagPages').value = String(page + 1);
    document.querySelector('input[name=pgScope][value=page]').checked = true;
    $('retagOut').textContent = '';
    $('retagTitle').textContent = focusAct === 'untag' ? 'Remove tags' : 'Tags on pages';
    $('retagDlg').showModal();
    (focusAct === 'untag' ? $('untagGo') : $('retagGo')).focus();
  }
  const scopeOf = () => (document.querySelector('input[name=pgScope]:checked') || {}).value || 'page';
  function pagesSpec() {
    const sc = scopeOf();
    if (sc === 'page') return { spec: String(page + 1), label: `page ${page + 1}` };
    if (sc === 'all') return { spec: `1-${model.pages}`, all: true, label: `all ${model.pages} pages` };
    if (sc === 'range') {
      let a = parseInt($('pgFrom').value, 10), b = parseInt($('pgTo').value, 10);
      if (!a || !b) return null;
      a = Math.max(1, Math.min(model.pages, a)); b = Math.max(1, Math.min(model.pages, b));
      if (a > b) [a, b] = [b, a];
      return { spec: `${a}-${b}`, label: a === b ? `page ${a}` : `pages ${a}–${b}` };
    }
    const v = $('retagPages').value.trim();
    return v ? { spec: v, label: `pages ${v}` } : null;
  }
  ['pgFrom', 'pgTo'].forEach((id) => $(id).addEventListener('focus', () => { document.querySelector('input[name=pgScope][value=range]').checked = true; }));
  $('retagPages').addEventListener('focus', () => { document.querySelector('input[name=pgScope][value=list]').checked = true; });
  $('retagBtn').addEventListener('click', () => openPages('retag'));
  $('untagBtn').addEventListener('click', () => openPages('untag'));
  async function pagesAction(kind) {
    const ps = pagesSpec();
    if (!ps) { $('retagOut').textContent = 'Give the pages first.'; return; }
    if (kind === 'untag' && !confirm(`Remove every tag of ${ps.label}? The printed pages do not change; Undo brings the tags back.`)) return;
    if (kind !== 'untag' && ps.all && !confirm(`${kind === 'fresh' ? 'Re-analyse' : 'Auto-tag'} the whole file? Every tag is replaced (Undo brings them back).`)) return;
    const btns = ['retagGo', 'pgReanGo', 'untagGo'];
    btns.forEach((b) => { $(b).disabled = true; });
    $('retagOut').textContent = kind === 'fresh' ? `Reading ${ps.label} again…` : kind === 'untag' ? 'Removing the tags…' : 'Working…';
    const r = kind === 'untag'
      ? await post('/tags/remove', ps.all ? { all: true } : { pages: ps.spec })
      : await post('/retag', { pages: ps.spec, fresh: kind === 'fresh' });
    btns.forEach((b) => { $(b).disabled = false; });
    if (r.error) { $('retagOut').textContent = r.error; return; }
    $('retagDlg').close();
    sel.clear(); focus = null;
    await load();
    const where = r.pages.length > 6 ? `${r.pages.length} pages` : `Page ${r.pages.map((p) => (typeof p === 'number' && kind !== 'untag' ? p : p)).join(', ')}`;
    UA.toast(kind === 'untag'
      ? `${where}: ${r.removed} tag(s) removed — draw zones or Auto-tag to tag again (Undo to go back)`
      : `${where}: ${r.removed} tags replaced by ${r.added} ${kind === 'fresh' ? 're-analysed' : 'automatic'} tags (Undo to go back)`, 5000);
  }
  $('retagGo').addEventListener('click', () => pagesAction('retag'));
  $('pgReanGo').addEventListener('click', () => pagesAction('fresh'));
  $('untagGo').addEventListener('click', () => pagesAction('untag'));

  // ------------------------------------------------------------ controls
  $('prevPage').addEventListener('click', () => { if (page > 0) { page--; renderPage(); } });
  $('nextPage').addEventListener('click', () => { if (page < model.pages - 1) { page++; renderPage(); } });
  $('pageNo').addEventListener('change', (e) => {
    const v = parseInt(e.target.value, 10) - 1;
    if (v >= 0 && v < model.pages) { page = v; renderPage(); }
  });
  $('zoom').addEventListener('change', (e) => {
    const v = e.target.value;
    if (v === 'fit') setZoom(fitZoom('page'), null, 'page');
    else if (v === 'width') setZoom(fitZoom('width'), null, 'width');
    else setZoom(parseFloat(v));
    e.target.blur();
  });
  ['showBoxes', 'showOrder', 'showLabels', 'showContrast'].forEach((id) => $(id).addEventListener('change', drawBoxes));
  $('showOut').addEventListener('change', () => {
    if ($('showOut').checked && !window.META.built) { UA.toast('Build first to see the result'); $('showOut').checked = false; return; }
    renderPage();
  });
  $('showArtifacts').addEventListener('change', () => { renderTree(); drawBoxes(); });
  $('filter').addEventListener('change', renderTree);
  $('roleFilter').addEventListener('change', renderTree);
  $('selAllShown').addEventListener('click', () => {
    const ids = shownIds();
    if (!ids.length) { UA.toast('Nothing is shown'); return; }
    sel = new Set(ids);
    focus = ids[0];
    renderTree();
    drawBoxes();
    renderInspector();
    UA.toast(`${ids.length} tag(s) selected`);
  });
  let st = null;
  $('search').addEventListener('input', () => { clearTimeout(st); st = setTimeout(renderTree, 200); });
  $('expandAll').addEventListener('click', () => { collapsed.clear(); saveCollapsed(); renderTree(); });
  $('collapseAll').addEventListener('click', () => {
    Object.values(model.nodes).forEach((n) => { if (n.children.length) collapsed.add(n.id); });
    saveCollapsed(); renderTree();
  });
  ['docTitle', 'docLang'].forEach((id) => $(id).addEventListener('change', async () => {
    await post('/doc', { title: $('docTitle').value, lang: $('docLang').value });
    await refreshIssues();
  }));
  // V4.28: the history belongs to the file — when somebody else made the last change a moment ago, ask first
  async function undoRedo(path) {
    let r = await post(path);
    if (r.confirm) r = confirm(r.error) ? await post(path, { force: true }) : { error: 'cancelled' };
    if (!r.error) await load(focus);
    return r;
  }
  $('undoBtn').addEventListener('click', () => undoRedo('/undo'));
  $('redoBtn').addEventListener('click', () => undoRedo('/redo'));

  // ------------------------------------------------------------ keyboard
  const HOTKEYS = { 1: 'H1', 2: 'H2', 3: 'H3', 4: 'H4', 5: 'H5', 6: 'H6', p: 'P', f: 'Figure', a: 'Artifact', c: 'Caption', n: 'Note', t: 'TD', h: 'TH' };
  let gPending = 0;
  // ------------------------------------------------------------ stages
  function setStage(s) {
    if (s === stage) return;
    if (s === 'links' && !window.LINKS) { UA.toast('The Links stage did not load — reload the page'); return; }
    stage = s;
    document.body.classList.toggle('stage-links', s === 'links');
    $('stageTags').classList.toggle('on', s === 'tags');
    $('stageLinks').classList.toggle('on', s === 'links');
    $('stageTags').setAttribute('aria-selected', s === 'tags');
    $('stageLinks').setAttribute('aria-selected', s === 'links');
    $('rolePop').classList.add('hidden');
    if (s === 'links') {
      if (window.TBL && window.TBL.active) window.TBL.stop();
      if (tool !== 'select') setTool('select');
      tool = 'links';
      ['drawBar', 'orderBar'].forEach((id) => $(id).classList.remove('show'));
      window.LINKS.enter();
    } else {
      tool = 'select';
      if (window.LINKS) window.LINKS.leave();
      setTool('select');
    }
    document.querySelector('.editor').classList.remove('stage-anim');
    void document.querySelector('.editor').offsetWidth;
    document.querySelector('.editor').classList.add('stage-anim');
    drawBoxes();
    try { localStorage.setItem('ua-stage-' + JID, s); } catch (e) { /* ignore */ }
    if (history.replaceState) history.replaceState(null, '', s === 'links' ? '#links' : location.pathname + location.search);
  }
  $('stageTags').addEventListener('click', () => setStage('tags'));
  $('stageLinks').addEventListener('click', () => setStage('links'));

  // what static/links.js (and static/tableedit.js) use of the editor
  window.ED = {
    get model() { return model; }, get page() { return page; }, get zoom() { return zoom; },
    get stage() { return stage; }, get focus() { return focus; },
    JID, api, post, el, $, overlay,
    load, drawBoxes, renderPage, roleName, labelOf, roleClass, showTab,
    setPage(p) { if (model && p >= 0 && p < model.pages && p !== page) { page = p; renderPage(); } else drawBoxes(); },
    selectTag(id) { setStage('tags'); selectOnly(id, true); },
    setStage,
    bumpWork() { workVer = Date.now() % 100000; },
  };

  document.addEventListener('keydown', async (e) => {
    if (document.querySelector('dialog[open]')) return;
    const k = e.key;
    // Ctrl + / Ctrl − / Ctrl 0: the page, not the browser, zooms
    if ((e.ctrlKey || e.metaKey) && !e.altKey && ['+', '=', '-', '_', '0'].includes(k)) {
      e.preventDefault();
      if (k === '0') setZoom(fitZoom('page'), null, 'page');
      else stepZoom(k === '-' || k === '_' ? -1 : 1);
      return;
    }
    // a zone was just drawn: the key says what it is (T table, 1–6 headings, F figure …)
    const pop = $('rolePop');
    if (pop && !pop.classList.contains('hidden') && pending && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (k === 'Escape') { e.preventDefault(); $('popCancel').click(); return; }
      if ((k === 'Delete' || k === 'Backspace') && !(e.target.closest && e.target.closest('#rolePop select'))) {
        e.preventDefault();
        e.stopPropagation();
        deleteZone();
        return;
      }
      const zr = k === 'Enter' ? 'Auto' : POP_KEY_ROLE[k.toLowerCase()];
      const inField = e.target.closest && e.target.closest('#rolePop select');
      if (zr && !(inField && k === 'Enter')) {
        e.preventDefault();
        e.stopPropagation();
        if (document.activeElement && pop.contains(document.activeElement)) document.activeElement.blur();
        createZone(zr);
        return;
      }
    }
    const tag = (e.target.tagName || '').toUpperCase();
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
    if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === 'z') {
      e.preventDefault();
      await undoRedo(e.shiftKey ? '/redo' : '/undo');
      return;
    }
    if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === 'y') {
      e.preventDefault();
      await undoRedo('/redo');
      return;
    }
    if (e.ctrlKey || e.metaKey) return;
    if (k.toLowerCase() === 'g' && !e.altKey && !e.shiftKey) {
      // G then T / L: the stage
      gPending = Date.now();
      if (stage !== 'links') return;
    } else if (gPending && Date.now() - gPending < 900 && ['t', 'l'].includes(k.toLowerCase())) {
      gPending = 0;
      setStage(k.toLowerCase() === 'l' ? 'links' : 'tags');
      return;
    }
    if (stage === 'links') {
      if (k === '[') { if (page > 0) { page--; renderPage(); } return; }
      if (k === ']') { if (page < model.pages - 1) { page++; renderPage(); } return; }
      if (window.LINKS) window.LINKS.key(e);
      return;
    }
    if (k === 'Escape') { if (window.TBL && window.TBL.active) window.TBL.stop(); if (window.LST && window.LST.active) window.LST.stop(); setTool('select'); $('rolePop').classList.add('hidden'); return; }
    if (k === '[') { if (page > 0) { page--; renderPage(); } return; }
    if (k === ']') { if (page < model.pages - 1) { page++; renderPage(); } return; }
    if (k === '.') { nextIssue(); return; }
    if (k.toLowerCase() === 'd' && !e.altKey) { setTool(tool === 'draw' ? 'select' : 'draw'); return; }
    if (k.toLowerCase() === 'o' && !e.altKey) { setTool(tool === 'order' ? 'select' : 'order'); return; }
    if (!focus) return;
    const i = flat.indexOf(focus);
    const moves = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'outdent', ArrowRight: 'indent' };
    if (e.altKey && e.shiftKey && (k === 'ArrowUp' || k === 'ArrowDown')) {
      // heading levels of the selected tags (V4.21.4)
      e.preventDefault();
      await levels([...sel].length ? [...sel] : [focus], k === 'ArrowUp' ? 'heading_up' : 'heading_down');
      return;
    }
    if (e.altKey && moves[k] && sel.size > 1) {
      // every selected tag, one change (V4.21.4)
      e.preventDefault();
      await levels([...sel], moves[k]);
      return;
    }
    if (e.altKey && moves[k]) {
      e.preventDefault();
      const r = await post('/move', { id: focus, action: moves[k] });
      if (!r.error) await load(focus);
      return;
    }
    if (k === 'ArrowUp' && i > 0) { e.preventDefault(); clickSelect(flat[i - 1], e, true); return; }
    if (k === 'ArrowDown' && i < flat.length - 1) { e.preventDefault(); clickSelect(flat[i + 1], e, true); return; }
    if (k === 'ArrowLeft' && model.nodes[focus].children.length && !collapsed.has(focus)) { collapsed.add(focus); saveCollapsed(); renderTree(); updateSelectionClasses(); return; }
    if (k === 'ArrowRight' && collapsed.has(focus)) { collapsed.delete(focus); saveCollapsed(); renderTree(); updateSelectionClasses(); return; }
    if (k === 'Delete' || k === 'Backspace') {
      e.preventDefault();
      const ids = [...sel];
      const r = ids.length > 1 ? await post('/bulk', { ids, action: 'delete' }) : await post('/move', { id: focus, action: 'delete' });
      if (!r.error) { const next = flat[i + 1] || flat[i - 1]; sel.clear(); focus = null; await load(next); }
      return;
    }
    const role = HOTKEYS[k.toLowerCase()];
    if (role && !e.altKey) {
      e.preventDefault();
      const ids = [...sel];
      const r = ids.length > 1 ? await post('/bulk', { ids, action: 'role', role }) : await post(`/node/${focus}`, { role });
      if (!r.error) await load(focus);
    }
  });

  load().then(() => {
    let want = location.hash === '#links' ? 'links' : '';
    if (!want) { try { want = localStorage.getItem('ua-stage-' + JID) || ''; } catch (e) { /* ignore */ } }
    if (want === 'links') setStage('links');
  });
})();
