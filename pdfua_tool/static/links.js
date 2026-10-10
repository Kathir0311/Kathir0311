/* Dezquare PDF Accessibility Studio — the Links stage (V4.18).
 *
 * Every link of the document in one place: where it is, what it says, where
 * it goes, and whether it goes to the right place. Links are set by hand here
 * (a box over the words → a tag, a spot on a page, a page, a web address or an
 * e-mail address; one-way or two-way), pointed elsewhere, made two-way, taken
 * out, found by the search, checked and fixed.
 *
 * Uses the editor through window.ED (static/editor.js). */
(function () {
  if (!window.ED || !document.getElementById('linksSide')) return;
  const ED = window.ED;
  const { $, el } = ED;
  const JID = ED.JID;
  const api = ED.api;

  let data = null;            // GET /links
  let byId = {};
  let selId = null;
  let mode = null;            // null | src | tgt | back | zone | retarget | twoback
  let draft = null;           // the link being made
  let loading = false;
  let stale = true;
  let lastFix = null;
  let pendRetarget = null;    // retarget kind while picking on the page
  const STATUS_TEXT = { ok: 'OK', warning: 'Check', error: 'Error' };
  const ORIGIN = { file: 'From the file', found: 'Found by the search', manual: 'Set by hand', dead: 'Dead link' };

  async function post(path, body) {
    const d = await UA.post(api(path), body);
    if (d.error) UA.toast(d.error, 4500);
    return d;
  }

  // ----------------------------------------------------------- data
  let inflight = null;
  function refresh(quiet, again) {
    // one request at a time: a second call waits for the first one's answer
    // (`again`: the tags changed meanwhile — ask once more after it)
    if (inflight && again) {
      const next = inflight.then(() => refresh(quiet));
      return next;
    }
    if (inflight) return inflight;
    inflight = doRefresh(quiet).finally(() => { inflight = null; });
    return inflight;
  }
  async function doRefresh(quiet) {
    loading = true;
    if (!quiet) $('lkStats').classList.add('loading');
    try {
      data = await UA.get(api('/links'));
    } catch (e) {
      data = null;
    }
    loading = false;
    stale = false;
    $('lkStats').classList.remove('loading');
    byId = {};
    ((data && data.links) || []).forEach((l) => { byId[l.id] = l; });
    if (selId && !byId[selId]) selId = null;
    badge();
    if (ED.stage === 'links') { renderList(); renderRight(); ED.drawBoxes(); }
  }
  function badge() {
    const c = $('stageLinkCount');
    if (!c || !data) return;
    const n = data.counts.error + data.counts.warning;
    c.textContent = String(n);
    c.classList.toggle('hidden', !n);
    c.classList.toggle('warnonly', !data.counts.error);
  }

  // ----------------------------------------------------------- list
  function matches(l) {
    const f = $('lkFilter').value;
    const q = ($('lkSearch').value || '').trim().toLowerCase();
    const t = l.target || {};
    if (q) {
      const hay = `${l.text} ${t.label || ''} ${t.uri || ''} ${l.contents || ''} p${l.page + 1}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    switch (f) {
      case 'problems': return l.status !== 'ok';
      case 'error': return l.status === 'error';
      case 'warning': return l.status === 'warning';
      case 'page': return l.page === ED.page || (l.rects || []).some((r) => r[0] === ED.page);
      case 'internal': return t.type === 'page';
      case 'web': return t.type === 'uri' || t.type === 'email';
      case 'manual': case 'file': case 'found': case 'dead': return l.origin === f;
      default: return true;
    }
  }
  function dest(l) {
    const t = l.target || {};
    if (t.type === 'uri' || t.type === 'email') return t.uri || '';
    if (t.type === 'page') return `${t.label || 'page ' + (t.page + 1)} · p${t.page + 1}`;
    if (t.type === 'none') return 'goes nowhere';
    return t.type + (t.file ? ' ' + t.file : t.name ? ' ' + t.name : '');
  }
  let shown = 400;
  function renderList() {
    const box = $('lkList');
    if (!data) { box.replaceChildren(el('p', { class: 'muted small pad', text: 'Loading the links…' })); return; }
    renderStats();
    const items = data.links.filter(matches);
    const frag = document.createDocumentFragment();
    items.slice(0, shown).forEach((l, i) => {
      const t = l.target || {};
      const row = el('div', { class: 'litem ' + l.status + (l.id === selId ? ' sel' : ''), role: 'option',
        'aria-selected': l.id === selId ? 'true' : 'false', 'data-id': l.id, tabindex: '-1',
        onclick: () => selectLink(l.id, true) },
      el('span', { class: 'ldot ' + l.status, title: STATUS_TEXT[l.status] }),
      el('div', { class: 'lmain' },
        el('div', { class: 'ltext' }, l.text ? l.text.slice(0, 90) : el('i', { class: 'muted', text: '(no words)' })),
        el('div', { class: 'lmeta' },
          el('span', { class: 'lpg', text: 'p' + (l.page + 1) }),
          el('span', { class: 'larrow', text: l.two_way ? '⇄' : '→' }),
          el('span', { class: 'ldest' + (t.type === 'uri' || t.type === 'email' ? ' web' : ''), text: dest(l).slice(0, 80) }))),
      el('span', { class: 'lorig ' + l.origin, title: ORIGIN[l.origin], text: { file: 'file', found: 'found', manual: 'hand', dead: 'dead' }[l.origin] }));
      row.style.animationDelay = Math.min(i, 30) * 12 + 'ms';
      frag.append(row);
    });
    box.replaceChildren(frag);
    if (!items.length) {
      box.append(el('div', { class: 'empty' },
        el('div', { class: 'emptyicon', text: '🔗' }),
        el('p', { class: 'small muted', text: data.links.length ? 'No link matches this filter.'
          : 'This document has no links yet. Press “＋ New link” (N) or let the search find them (right).' })));
    }
    if (items.length > shown) {
      box.append(el('button', { class: 'sm more', text: `Show ${Math.min(400, items.length - shown)} more of ${items.length - shown}`,
        onclick: () => { shown += 400; renderList(); } }));
    }
  }
  function renderStats() {
    const c = data.counts;
    const chip = (cls, n, label, f) => el('button', { class: 'lchip ' + cls + ($('lkFilter').value === f ? ' on' : ''),
      title: 'Show ' + label, onclick: () => { $('lkFilter').value = $('lkFilter').value === f ? 'all' : f; shown = 400; renderList(); } },
    el('b', { text: String(n) }), ' ' + label);
    $('lkStats').replaceChildren(
      chip('all', c.total, 'links', 'all'),
      chip('err', c.error, 'errors', 'error'),
      chip('warn', c.warning, 'to check', 'warning'),
      chip('ok', c.ok, 'OK', 'all'),
      el('div', { class: 'lbar', title: `${c.ok} OK · ${c.warning} to check · ${c.error} errors` },
        el('i', { class: 'ok', style: `width:${c.total ? (100 * c.ok / c.total) : 0}%` }),
        el('i', { class: 'warn', style: `width:${c.total ? (100 * c.warning / c.total) : 0}%` }),
        el('i', { class: 'err', style: `width:${c.total ? (100 * c.error / c.total) : 0}%` })));
  }
  $('lkFilter').addEventListener('change', () => { shown = 400; renderList(); });
  $('lkSearch').addEventListener('input', () => { shown = 400; renderList(); });

  function selectLink(id, jump) {
    selId = id;
    const l = byId[id];
    if (mode && mode !== 'tgt' && mode !== 'retarget') cancel(true);
    if (l && jump) ED.setPage(l.page);
    document.querySelectorAll('#lkList .litem').forEach((r) => {
      const on = r.dataset.id === id;
      r.classList.toggle('sel', on);
      r.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) r.scrollIntoView({ block: 'nearest' });
    });
    renderRight();
    ED.drawBoxes();
  }
  function nextProblem(dir) {
    if (!data) return;
    const list = data.links.filter((l) => l.status !== 'ok');
    if (!list.length) { UA.toast('No link has a problem'); return; }
    let i = list.findIndex((l) => l.id === selId);
    i = i < 0 ? (dir > 0 ? 0 : list.length - 1) : (i + dir + list.length) % list.length;
    selectLink(list[i].id, true);
  }
  $('lkPrev').addEventListener('click', () => nextProblem(-1));
  $('lkNext').addEventListener('click', () => nextProblem(1));

  // ----------------------------------------------------------- page overlay
  function boxDiv(r, cls, title) {
    const [x0, y0, x1, y1] = r;
    const z = ED.zoom;
    const d = el('div', { class: cls, title: title || '' });
    d.style.left = (x0 * z) + 'px';
    d.style.top = (y0 * z) + 'px';
    d.style.width = Math.max(4, (x1 - x0) * z) + 'px';
    d.style.height = Math.max(4, (y1 - y0) * z) + 'px';
    return d;
  }
  function draw(overlay) {
    const page = ED.page;
    const model = ED.model;
    const picking = mode === 'tgt' || mode === 'retarget';
    overlay.classList.toggle('picking', picking);
    overlay.classList.toggle('marking', ['src', 'back', 'zone', 'twoback'].includes(mode));
    const frag = document.createDocumentFragment();
    if (model && (picking || $('lkShowTags').checked)) {
      const items = [];
      for (const n of Object.values(model.nodes)) {
        if (n.artifact || n.role === 'Link' || n.role === 'Reference') continue;
        for (const r of n.rects || []) {
          if (r[0] !== page) continue;
          items.push({ n, r, a: (r[3] - r[1]) * (r[4] - r[2]) });
        }
      }
      items.sort((x, y) => y.a - x.a);
      for (const { n, r } of items) {
        const d = boxDiv(r.slice(1, 5), 'pbox ' + ED.roleClass(n) + (picking ? ' pick' : ''),
          `${ED.roleName(n)}: ${(ED.labelOf(n) || '').slice(0, 100)}`);
        d.dataset.node = n.id;
        if (picking) d.append(el('span', { class: 'tag', text: ED.roleName(n) }));
        frag.append(d);
      }
    }
    if (data) {
      for (const l of data.links) {
        (l.rects || []).forEach((r, k) => {
          if (r[0] !== page) return;
          const d = boxDiv(r.slice(1, 5), `lbox ${l.status}${l.id === selId ? ' sel' : ''}${l.two_way ? ' two' : ''}`,
            `${l.text || '(no words)'} → ${dest(l)}`);
          d.dataset.link = l.id;
          if (k === 0 && (l.two_way || l.id === selId)) d.append(el('span', { class: 'lbadge', text: l.two_way ? '⇄' : '→' }));
          frag.append(d);
        });
      }
      // where the selected link lands, when it is on this page
      const s = selId && byId[selId];
      if (s && s.target && s.target.type === 'page' && s.target.page === page) {
        const t = s.target;
        const land = t.lands_on && model && model.nodes[t.lands_on];
        if (land) (land.rects || []).filter((r) => r[0] === page).forEach((r) => frag.append(boxDiv(r.slice(1, 5), 'landbox')));
        const y = (t.y || 0) * ED.zoom;
        const m = el('div', { class: 'landmark', title: 'The link lands here' }, el('span', { text: '◀ lands here' }));
        m.style.top = y + 'px';
        frag.append(m);
      }
    }
    if (draft) {
      if (draft.src && draft.src.page === page) frag.append(boxDiv(draft.src.rect, 'dmark from', 'the words that link'));
      (draft.more || []).filter((b) => b.page === page).forEach((b) => frag.append(boxDiv(b.rect, 'dmark from', 'the words that link (more)')));
      if (draft.tgt && draft.tgt.page === page && draft.tgt.rect) frag.append(boxDiv(draft.tgt.rect, 'dmark to', 'the link goes here'));
      if (draft.tgt && draft.tgt.page === page && draft.tgt.y != null && !draft.tgt.node) {
        const m = el('div', { class: 'landmark draft' }, el('span', { text: '◀ goes here' }));
        m.style.top = draft.tgt.y * ED.zoom + 'px';
        frag.append(m);
      }
      if (draft.back && draft.back.page === page) frag.append(boxDiv(draft.back.rect, 'dmark back', 'the way back starts here'));
    }
    overlay.append(frag);
  }

  // drag a box / click on the page
  const overlay = ED.overlay;
  let start = null, rubber = null;
  overlay.addEventListener('mousedown', (e) => {
    if (ED.stage !== 'links' || e.button !== 0) return;
    const r = overlay.getBoundingClientRect();
    start = { x: e.clientX - r.left, y: e.clientY - r.top, cx: e.clientX, cy: e.clientY, shift: e.shiftKey };
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!start || ED.stage !== 'links') return;
    if (!rubber && Math.abs(e.clientX - start.cx) + Math.abs(e.clientY - start.cy) < 5) return;
    const r = overlay.getBoundingClientRect();
    const x = Math.max(0, Math.min(r.width, e.clientX - r.left));
    const y = Math.max(0, Math.min(r.height, e.clientY - r.top));
    if (!rubber) { rubber = el('div', { class: 'rubber lrubber' }); overlay.append(rubber); }
    Object.assign(rubber.style, { left: Math.min(x, start.x) + 'px', top: Math.min(y, start.y) + 'px',
      width: Math.abs(x - start.x) + 'px', height: Math.abs(y - start.y) + 'px' });
  });
  window.addEventListener('mouseup', (e) => {
    if (!start || ED.stage !== 'links') { start = null; return; }
    const st = start;
    start = null;
    const r = overlay.getBoundingClientRect();
    const x = Math.max(0, Math.min(r.width, e.clientX - r.left));
    const y = Math.max(0, Math.min(r.height, e.clientY - r.top));
    const z = ED.zoom;
    const dragged = !!rubber;
    if (rubber) { rubber.remove(); rubber = null; }
    if (dragged) {
      const box = [Math.min(x, st.x) / z, Math.min(y, st.y) / z, Math.max(x, st.x) / z, Math.max(y, st.y) / z];
      if (box[2] - box[0] < 2 || box[3] - box[1] < 2) return;
      onBox(box, st.shift || e.shiftKey);
      return;
    }
    onClick(e, x / z, y / z);
  });

  function onBox(box, shift) {
    const page = ED.page;
    if (mode === 'zone') { setZone(box); return; }
    if (mode === 'twoback') { makeTwoWay(box); return; }
    if (mode === 'back') {
      if (!draft || !draft.tgt || draft.tgt.page !== page) { UA.toast('Mark the words on the page the link goes to'); return; }
      draft.back = { page, rect: box };
      mode = 'tgt';
      renderRight(); ED.drawBoxes();
      return;
    }
    if (draft && draft.src && shift) {
      draft.more = draft.more || [];
      draft.more.push({ page, rect: box });
      UA.toast('Another box for the same link (a link over two lines)');
      renderRight(); ED.drawBoxes();
      return;
    }
    if (!draft || mode === 'src' || mode === null || !draft.src) {
      draft = newDraft();
      draft.src = { page, rect: box };
      mode = draft.type === 'place' ? 'tgt' : null;
      selId = null;
      renderRight(); ED.drawBoxes();
      bar();
      return;
    }
    UA.toast('Click where the link goes — or hold Shift and drag to add a second box');
  }

  function onClick(e, x, y) {
    const page = ED.page;
    if (mode === 'tgt' || mode === 'retarget') {
      const hit = document.elementsFromPoint(e.clientX, e.clientY).find((d) => d.classList && d.classList.contains('pbox'));
      const model = ED.model;
      let tgt;
      if (hit && model.nodes[hit.dataset.node]) {
        const n = model.nodes[hit.dataset.node];
        const rr = (n.rects || []).find((q) => q[0] === page);
        tgt = { node: n.id, page, rect: rr ? rr.slice(1, 5) : null,
          label: `${ED.roleName(n)} “${(ED.labelOf(n) || '').slice(0, 50)}”` };
      } else {
        tgt = { page, y, rect: null, label: `a spot on page ${page + 1}` };
      }
      if (mode === 'retarget') { retarget(tgt.node ? { node: tgt.node } : { page: tgt.page, y: tgt.y }); return; }
      draft.tgt = tgt;
      draft.back = null;
      renderRight(); ED.drawBoxes(); bar();
      return;
    }
    if (mode) return;
    const hit = document.elementsFromPoint(e.clientX, e.clientY).find((d) => d.classList && d.classList.contains('lbox'));
    if (hit) { selectLink(hit.dataset.link, false); return; }
    if (selId) { selId = null; renderList(); renderRight(); ED.drawBoxes(); }
  }

  // ----------------------------------------------------------- the mode bar
  function bar() {
    const b = $('linkBar');
    let t = '';
    if (mode === 'src') t = '1 · Drag a box over the words that link.';
    else if (mode === 'tgt' && draft) t = draft.tgt
      ? `→ ${draft.tgt.label} (p${draft.tgt.page + 1}). Click elsewhere to change it, Shift+drag to add a second box — then Create link (right).`
      : `2 · Click where the link goes: a tag or a spot on any page (‹ › or [ ] to turn pages). From “${(draft.src && draft.src.text) || 'the marked words'}” (p${draft.src.page + 1}).`;
    else if (mode === 'back') t = 'Drag a box over the words at the target that link back.';
    else if (mode === 'zone') t = 'Drag a new box over the words of the selected link.';
    else if (mode === 'retarget') t = 'Click the tag or the spot the selected link should go to (any page).';
    else if (mode === 'twoback') t = 'Drag a box over the words where the link lands that should link back.';
    $('linkStep').textContent = t;
    b.classList.toggle('show', !!t);
  }
  $('linkCancel').addEventListener('click', () => cancel());
  function cancel(quiet) {
    const had = mode || draft;
    mode = null;
    draft = null;
    pendRetarget = null;
    bar();
    if (!quiet && had) { renderRight(); ED.drawBoxes(); }
  }

  // ----------------------------------------------------------- new link
  function newDraft() {
    const d = { type: 'place', two: window.META && window.META.link_direction === 'two', more: [], contents: '',
      ref: 'auto', replace: false, uri: '', email: '', pageNo: '' };
    if (draft) { d.type = draft.type; d.two = draft.two; }
    return d;
  }
  function startNew() {
    draft = newDraft();
    mode = 'src';
    selId = null;
    renderList(); renderRight(); ED.drawBoxes(); bar();
  }
  $('lkNewBtn').addEventListener('click', startNew);

  function draftReady() {
    if (!draft || !draft.src) return false;
    if (draft.type === 'place') return !!draft.tgt;
    if (draft.type === 'page') return parseInt(draft.pageNo, 10) >= 1;
    if (draft.type === 'web') return !!draft.uri.trim();
    if (draft.type === 'email') return /@/.test(draft.email);
    return false;
  }
  async function create() {
    if (!draftReady()) return;
    let target;
    if (draft.type === 'place') target = draft.tgt.node ? { node: draft.tgt.node } : { page: draft.tgt.page, y: draft.tgt.y };
    else if (draft.type === 'page') target = { page: parseInt(draft.pageNo, 10) - 1 };
    else if (draft.type === 'web') target = { uri: draft.uri.trim() };
    else target = { email: draft.email.trim() };
    const web = draft.type === 'web' || draft.type === 'email';
    const body = { page: draft.src.page, rect: draft.src.rect, target, two_way: !web && !!draft.two,
      more: (draft.more || []).map((b) => [b.page, ...b.rect]), contents: draft.contents || '',
      replace: !!draft.replace };
    if (draft.ref !== 'auto') body.reference = draft.ref === 'yes';
    if (body.two_way && draft.back) body.back_rect = draft.back.rect;
    const r = await post('/links/set', body);
    if (r.error) return;
    const two = body.two_way;
    cancel(true);
    selId = r.id;
    await ED.load();
    await refresh(true);
    selectLink(r.id, false);
    UA.toast(two ? `Two-way link set — “${(r.text || '').slice(0, 30)}” and the way back` : `Link set on “${(r.text || '').slice(0, 40)}”`);
  }

  // ----------------------------------------------------------- change a link
  async function act(path, body, msg) {
    const r = await post(path, body);
    if (r.error) return r;
    await ED.load();
    await refresh(true);
    if (msg) UA.toast(typeof msg === 'function' ? msg(r) : msg);
    return r;
  }
  async function retarget(target) {
    const id = selId;
    mode = null; bar();
    await act(`/links/${id}/target`, { target }, 'The link goes there now');
    selectLink(id, false);
  }
  async function setZone(box) {
    const id = selId;
    mode = null; bar();
    await act(`/links/${id}/zone`, { page: ED.page, rect: box }, (r) => `Zone drawn again${r.text ? ' — “' + r.text.slice(0, 40) + '”' : ''}`);
    selectLink(id, false);
  }
  async function makeTwoWay(box) {
    const id = selId;
    mode = null; bar();
    const r = await act(`/links/${id}/twoway`, box ? { back_rect: box } : {}, 'Two-way: the place it lands links back now');
    if (!r.error) selectLink(id, false);
  }
  async function removeLink(id, both) {
    const l = byId[id];
    if (!l) return;
    if (l.origin === 'file' && !confirm('This link is in the file itself. Take it out? (Its words stay; the build leaves the link out. Undo brings it back.)')) return;
    const r = await act(`/links/${id}/remove`, { both: !!both }, (x) => x.removed > 1 ? 'The link and its way back are out' : 'Link taken out — its words stay');
    if (!r.error) { selId = null; renderRight(); }
  }
  async function fix(id, f) {
    lastFix = id;
    const r = await act(`/links/${id}/fix`, { fix: f }, 'Fixed');
    if (!r.error && byId[id]) selectLink(id, false);
  }
  function goTarget() {
    const l = selId && byId[selId];
    if (!l) return;
    const t = l.target || {};
    if (t.type === 'page') {
      ED.setPage(t.page);
      setTimeout(() => {
        const m = overlay.querySelector('.landmark');
        if (m) m.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }, 120);
    } else if (t.uri) {
      window.open(t.uri.startsWith('mailto:') ? t.uri : t.uri, '_blank', 'noopener');
    }
  }
  function goLink() {
    const l = selId && byId[selId];
    if (!l) return;
    ED.setPage(l.page);
    setTimeout(() => {
      const m = overlay.querySelector('.lbox.sel');
      if (m) m.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 120);
  }

  // ----------------------------------------------------------- right panel
  function section(title, ...kids) {
    return el('div', { class: 'lsec' }, el('h4', { text: title }), ...kids);
  }
  function seg(options, value, onpick) {
    const s = el('div', { class: 'seg lseg' });
    options.forEach(([v, t]) => s.append(el('button', { class: 'sm' + (v === value ? ' on' : ''), type: 'button', text: t,
      onclick: () => onpick(v) })));
    return s;
  }
  function peekImg(t) {
    if (!t || t.type !== 'page') return null;
    const src = `/job/${JID}/links/peek/${t.page}.png?y=${t.y == null ? '' : Math.round(t.y)}&w=${Date.now() % 1e6}`;
    const img = el('img', { class: 'lpeek', alt: `Where the link lands on page ${t.page + 1}`, src, loading: 'lazy' });
    img.addEventListener('click', goTarget);
    return img;
  }
  function renderRight() {
    const box = $('linksRight');
    if (!box) return;
    box.replaceChildren();
    if (draft) { box.append(wizard()); return; }
    const l = selId && byId[selId];
    if (l) { box.append(details(l)); return; }
    box.append(overview());
  }

  function wizard() {
    const w = el('div', { class: 'lwizard fadein' });
    w.append(el('div', { class: 'lhead' }, el('h3', { text: 'New link' }), el('div', { class: 'spacer' }),
      el('button', { class: 'sm', text: 'Cancel', onclick: () => cancel() })));
    // 1 the words
    const s1 = el('div', { class: 'lstepcard' + (draft.src ? ' done' : ' now') },
      el('div', { class: 'lnum', text: '1' }),
      el('div', {}, el('b', { text: 'The words that link' }),
        el('div', { class: 'small muted', text: draft.src
          ? `Marked on page ${draft.src.page + 1}${draft.more.length ? ` + ${draft.more.length} more box(es)` : ''}. Shift+drag adds a second box (a link over two lines).`
          : 'Drag a box over them on the page.' }),
        draft.src ? el('div', { class: 'btnrow' },
          el('button', { class: 'sm', text: 'Mark again', onclick: () => { draft.src = null; draft.more = []; mode = 'src'; renderRight(); ED.drawBoxes(); bar(); } }),
          draft.more.length ? el('button', { class: 'sm', text: 'Drop the extra boxes', onclick: () => { draft.more = []; renderRight(); ED.drawBoxes(); } }) : null) : null));
    w.append(s1);
    // 2 where it goes
    const kinds = [['place', 'A place here'], ['page', 'A page'], ['web', 'Web address'], ['email', 'E-mail']];
    const s2body = el('div', {});
    s2body.append(seg(kinds, draft.type, (v) => {
      draft.type = v;
      if (v !== 'place') { draft.two = false; if (mode === 'tgt') mode = null; } else if (draft.src) mode = 'tgt';
      renderRight(); ED.drawBoxes(); bar();
    }));
    if (draft.type === 'place') {
      if (draft.tgt) {
        s2body.append(el('div', { class: 'small' }, '→ ', el('b', { text: draft.tgt.label }), ` · page ${draft.tgt.page + 1}`));
        const y = draft.tgt.rect ? draft.tgt.rect[1] : draft.tgt.y;
        s2body.append(peekImg({ type: 'page', page: draft.tgt.page, y }));
        s2body.append(el('div', { class: 'tiny muted', text: 'Click another tag or spot to change it.' }));
      } else {
        s2body.append(el('div', { class: 'small muted', text: draft.src ? 'Click a tag or a spot on any page — turn pages with ‹ › or [ ].' : 'First mark the words.' }));
      }
    } else if (draft.type === 'page') {
      const inp = el('input', { type: 'number', min: 1, max: ED.model.pages, value: draft.pageNo, placeholder: `1–${ED.model.pages}` });
      inp.addEventListener('input', () => { draft.pageNo = inp.value; createBtn.disabled = !draftReady(); });
      s2body.append(el('label', { class: 'small' }, 'Page number (its top)', inp));
    } else if (draft.type === 'web') {
      const inp = el('input', { type: 'url', value: draft.uri, placeholder: 'https://… or www.…' });
      inp.addEventListener('input', () => { draft.uri = inp.value; createBtn.disabled = !draftReady(); });
      s2body.append(el('label', { class: 'small' }, 'Web address', inp));
    } else {
      const inp = el('input', { type: 'email', value: draft.email, placeholder: 'name@example.org' });
      inp.addEventListener('input', () => { draft.email = inp.value; createBtn.disabled = !draftReady(); });
      s2body.append(el('label', { class: 'small' }, 'E-mail address', inp));
    }
    w.append(el('div', { class: 'lstepcard' + (draft.src ? (draftReady() ? ' done' : ' now') : '') },
      el('div', { class: 'lnum', text: '2' }), el('div', {}, el('b', { text: 'Where it goes' }), s2body)));
    // 3 direction
    const web = draft.type === 'web' || draft.type === 'email';
    const s3 = el('div', {});
    s3.append(seg([['one', 'One-way →'], ['two', 'Two-way ⇄']], draft.two && !web ? 'two' : 'one', (v) => {
      if (v === 'two' && web) { UA.toast('A web or e-mail link cannot link back'); return; }
      draft.two = v === 'two'; renderRight();
    }));
    if (draft.two && !web) {
      s3.append(el('div', { class: 'small muted', text: draft.back
        ? `The way back starts from the words you marked on page ${draft.back.page + 1}.`
        : 'The way back starts from the target’s first words (a note: its number) — or mark them yourself.' }));
      s3.append(el('div', { class: 'btnrow' },
        el('button', { class: 'sm', text: draft.back ? 'Mark them again' : 'Mark the words that link back', disabled: !draft.tgt,
          onclick: () => { ED.setPage(draft.tgt.page); mode = 'back'; bar(); ED.drawBoxes(); } }),
        draft.back ? el('button', { class: 'sm', text: 'Let the tool choose', onclick: () => { draft.back = null; renderRight(); ED.drawBoxes(); } }) : null));
    }
    w.append(el('div', { class: 'lstepcard' }, el('div', { class: 'lnum', text: '3' }), el('div', {}, el('b', { text: 'Direction' }), s3)));
    // more options
    const more = el('details', { class: 'lmore' }, el('summary', { text: 'More options' }));
    const desc = el('input', { value: draft.contents, placeholder: 'What a screen reader says (optional)' });
    desc.addEventListener('input', () => { draft.contents = desc.value; });
    more.append(el('label', { class: 'small' }, 'Description', desc));
    const ref = el('select', {}, ...[['auto', `As the profile does (${data && data.reference ? 'Reference > Link' : 'Link'})`], ['yes', 'Reference > Link'], ['no', 'Link only']]
      .map(([v, t]) => { const o = el('option', { value: v, text: t }); if (v === draft.ref) o.selected = true; return o; }));
    ref.addEventListener('change', () => { draft.ref = ref.value; });
    more.append(el('label', { class: 'small' }, 'Tags', ref));
    const rep = el('input', { type: 'checkbox' });
    rep.checked = !!draft.replace;
    rep.addEventListener('change', () => { draft.replace = rep.checked; });
    more.append(el('label', { class: 'row small' }, rep, ' Replace a link of the file that is on these words'));
    w.append(more);
    const createBtn = el('button', { class: 'primary', text: draft.two && !web ? 'Create two-way link' : 'Create link', disabled: !draftReady(), onclick: create });
    w.append(el('div', { class: 'btnrow' }, createBtn));
    return w;
  }

  function details(l) {
    const t = l.target || {};
    const w = el('div', { class: 'ldetails fadein' });
    w.append(el('div', { class: 'lhead' },
      el('span', { class: 'lpill ' + l.status, text: STATUS_TEXT[l.status] }),
      el('span', { class: 'lorig big ' + l.origin, text: ORIGIN[l.origin] }),
      l.two_way ? el('span', { class: 'chip', text: '⇄ two-way' }) : null,
      el('div', { class: 'spacer' }),
      el('button', { class: 'sm', text: '✕', title: 'Close', onclick: () => { selId = null; renderList(); renderRight(); ED.drawBoxes(); } })));
    w.append(section('Words', el('div', { class: 'lquote', text: l.text || '(no words in the zone)' }),
      el('div', { class: 'small muted', text: `Page ${l.page + 1} · ${l.parent_role || '—'} > Link${l.kind ? ' · ' + l.kind : ''}` }),
      el('div', { class: 'btnrow' },
        el('button', { class: 'sm', text: 'Show on the page', onclick: goLink }),
        el('button', { class: 'sm', text: 'Its tag in the Tags stage', onclick: () => ED.selectTag(l.id) }))));
    const tbody = [];
    if (t.type === 'page') {
      tbody.push(el('div', {}, el('b', { text: t.label || `page ${t.page + 1}` }), el('span', { class: 'muted small', text: ` · page ${t.page + 1}` })));
      tbody.push(peekImg(t));
      tbody.push(el('div', { class: 'btnrow' },
        el('button', { class: 'sm primary', text: 'Go there (G)', onclick: goTarget }),
        t.lands_on ? el('button', { class: 'sm', text: 'Select that tag', onclick: () => ED.selectTag(t.lands_on) }) : null));
    } else if (t.type === 'uri' || t.type === 'email') {
      tbody.push(el('div', { class: 'lquote mono', text: t.uri }));
      tbody.push(el('div', { class: 'btnrow' }, el('a', { class: 'btn sm', href: t.uri, target: '_blank', rel: 'noopener noreferrer', text: t.type === 'email' ? 'Write an e-mail' : 'Open it in a new tab' })));
    } else {
      tbody.push(el('div', { class: 'small', text: dest(l) }));
    }
    w.append(section('Goes to', ...tbody));
    if (l.problems.length) {
      w.append(section('What the check found', ...l.problems.map((p) => el('div', { class: 'lprob ' + p.level },
        el('div', { class: 'small', text: p.msg }),
        p.fix ? el('button', { class: 'sm primary', text: p.fix_label || 'Fix', onclick: () => fix(l.id, p.fix) }) : null))));
    } else {
      w.append(el('div', { class: 'lgood' }, el('span', { text: '✓' }), ' Goes where its words say.'));
    }
    // point it elsewhere
    const pe = el('div', {});
    const kinds = [['place', 'A place here'], ['page', 'A page'], ['web', 'Web address'], ['email', 'E-mail']];
    const which = pendRetarget || 'place';
    pe.append(seg(kinds, which, (v) => { pendRetarget = v; if (v !== 'place' && mode === 'retarget') { mode = null; bar(); } renderRight(); }));
    if (which === 'place') {
      pe.append(el('button', { class: 'sm' + (mode === 'retarget' ? ' on' : ''), text: mode === 'retarget' ? 'Click the place on the page…' : 'Pick it on the page',
        onclick: () => { mode = mode === 'retarget' ? null : 'retarget'; bar(); ED.drawBoxes(); renderRight(); } }));
    } else {
      const inp = el('input', which === 'page' ? { type: 'number', min: 1, max: ED.model.pages, placeholder: 'page' }
        : { placeholder: which === 'web' ? 'https://…' : 'name@example.org', value: which === 'web' && t.type === 'uri' ? t.uri : '' });
      const go = () => {
        const v = inp.value.trim();
        if (!v) return;
        if (which === 'page') retarget({ page: parseInt(v, 10) - 1 });
        else if (which === 'web') retarget({ uri: v });
        else retarget({ email: v });
      };
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
      pe.append(el('div', { class: 'lrow' }, inp, el('button', { class: 'sm primary', text: 'Apply', onclick: go })));
    }
    w.append(section('Point it elsewhere', pe));
    // description
    const d = el('input', { value: l.contents || '', placeholder: l.text ? `(read from the words: “${l.text.slice(0, 40)}”)` : 'Say what the link does' });
    const saveD = () => act(`/links/${l.id}/contents`, { contents: d.value }, 'Description saved').then(() => selectLink(l.id, false));
    d.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveD(); });
    w.append(section('Description for screen readers', el('div', { class: 'lrow' }, d, el('button', { class: 'sm', text: 'Save', onclick: saveD }))));
    // more
    const two = (t.type === 'page' && !l.two_way)
      ? el('div', { class: 'btnrow' },
        el('button', { class: 'sm', text: 'Make it two-way ⇄', title: 'The place it lands links back (from its first words, a note: its number)', onclick: () => makeTwoWay(null) }),
        el('button', { class: 'sm', text: 'Two-way, from words I mark', onclick: () => { ED.setPage(t.page); mode = 'twoback'; bar(); ED.drawBoxes(); } }))
      : null;
    const bothCb = el('input', { type: 'checkbox', checked: true });
    w.append(section('More',
      two,
      el('div', { class: 'btnrow' },
        el('button', { class: 'sm' + (mode === 'zone' ? ' on' : ''), text: 'Draw its zone again', onclick: () => { ED.setPage(l.page); mode = mode === 'zone' ? null : 'zone'; bar(); ED.drawBoxes(); renderRight(); } }),
        el('button', { class: 'sm danger', text: 'Take the link out', onclick: () => removeLink(l.id, l.two_way && bothCb.checked) })),
      l.two_way ? el('label', { class: 'row small' }, bothCb, ' and its way back') : null));
    return w;
  }

  function overview() {
    const w = el('div', { class: 'loverview fadein' });
    if (!data) { w.append(el('p', { class: 'muted small', text: 'Loading the links…' })); return w; }
    const c = data.counts;
    const health = c.total ? Math.round(100 * c.ok / c.total) : 100;
    w.append(el('div', { class: 'lhealth' },
      el('div', { class: 'ring', style: `--p:${health}`, title: `${health}% of the links go where their words say` },
        el('span', { text: health + '%' })),
      el('div', {}, el('b', { text: c.total ? `${c.total} link${c.total === 1 ? '' : 's'}` : 'No links yet' }),
        el('div', { class: 'small muted', text: `${c.internal} to this document · ${c.web} web/e-mail` }),
        el('div', { class: 'small muted', text: `${c.file} from the file · ${c.found} found · ${c.manual} set by hand${c.dead ? ` · ${c.dead} dead` : ''}` }))));
    w.append(el('div', { class: 'btnrow' },
      el('button', { class: 'primary', text: '＋ New link', onclick: startNew }),
      el('button', { text: 'Check links', onclick: () => checkNow() }),
      el('button', { text: `Fix all${c.fixable ? ' (' + c.fixable + ')' : ''}`, disabled: !c.fixable, onclick: fixAll })));
    if (c.error + c.warning) {
      w.append(el('button', { class: 'sm', text: `Go through the ${c.error + c.warning} to check ›`, onclick: () => nextProblem(1) }));
    }
    // the search
    const s = data.search || {};
    const opts = { ...(s.options || {}) };
    const box = el('div', { class: 'lsearch' });
    box.append(el('h4', { text: 'Find links automatically' }));
    box.append(el('p', { class: 'tiny muted', text: 'Reads the text of this file for contents lines, “see Figure 3”, “[12]”, printed web and e-mail addresses, note numbers… and links each one to its target. Links set by hand are kept; links you took out stay out.' }));
    const ks = el('div', { class: 'xkinds' });
    const seen = new Set();
    Object.entries(s.kind_option || {}).forEach(([k, opt]) => {
      if (seen.has(opt)) return;
      seen.add(opt);
      const cb = el('input', { type: 'checkbox' });
      cb.checked = !!opts[opt];
      cb.addEventListener('change', () => { opts[opt] = cb.checked; });
      ks.append(el('label', { class: 'row small' }, cb, ' ' + ((s.labels || {})[k] || k)));
    });
    const refCb = el('input', { type: 'checkbox' });
    refCb.checked = !!opts.cross_reference_tags;
    refCb.addEventListener('change', () => { opts.cross_reference_tags = refCb.checked; });
    ks.append(el('label', { class: 'row small' }, refCb, ' Tag every mention as Reference'));
    box.append(ks);
    box.append(el('div', { class: 'btnrow' },
      el('button', { class: 'sm primary', text: 'Search the text now', onclick: async (e) => {
        e.target.disabled = true;
        e.target.textContent = 'Searching…';
        const r = await post('/xref', opts);
        if (!r.error) {
          await ED.load();
          await refresh(true);
          const st = r.stats || {};
          UA.toast(`${st.linked || 0} link(s) made by the search · ${st.unresolved || 0} mention(s) without a target`);
        }
        renderRight();
      } }),
      data.removed_by_hand ? el('button', { class: 'sm', text: `Let it find the ${data.removed_by_hand} taken out again`,
        onclick: () => act('/links/forget_removed', {}, 'The next search may find them again').then(renderRight) }) : null));
    w.append(box);
    w.append(el('div', { class: 'lhelp tiny muted' },
      el('b', { text: 'Setting a link by hand: ' }),
      'drag a box over the words on the page (or press N), click where it goes — a tag or a spot on any page — or choose a page, a web address or an e-mail address, pick one-way or two-way, and Create link. Click a link box on the page or a row on the left to see where it lands, point it elsewhere, make it two-way or take it out.'));
    return w;
  }

  async function checkNow() {
    UA.toast('Checking every link…');
    await refresh();
    const c = data.counts;
    UA.toast(c.error + c.warning ? `${c.error} error(s), ${c.warning} to check — ${c.fixable} can be fixed at once` : `All ${c.total} links go where their words say ✓`, 4000);
  }
  $('lkCheckBtn').addEventListener('click', checkNow);
  async function fixAll() {
    if (!data || !data.counts.fixable) { UA.toast('Nothing the tool can fix by itself'); return; }
    const r = await act('/links/fix_all', {}, (x) => `${x.fixed} fixed${(x.failed || []).length ? ` · ${x.failed.length} could not be` : ''}`);
    if (!r.error) renderRight();
  }
  $('lkFixAllBtn').addEventListener('click', fixAll);
  $('lkShowTags').addEventListener('change', () => ED.drawBoxes());

  // ----------------------------------------------------------- keys
  function key(e) {
    const k = e.key;
    if (k === 'Escape') { if (mode || draft) cancel(); else if (selId) { selId = null; renderList(); renderRight(); ED.drawBoxes(); } return; }
    if (e.altKey) return;
    if (k.toLowerCase() === 'n') { e.preventDefault(); startNew(); return; }
    if (k === '.' || k === '>') { nextProblem(e.shiftKey || k === '>' ? -1 : 1); return; }
    if (k.toLowerCase() === 'g') { goTarget(); return; }
    if ((k === 'Delete' || k === 'Backspace') && selId) { e.preventDefault(); removeLink(selId, true); return; }
    if ((k === 'ArrowDown' || k === 'ArrowUp') && data) {
      e.preventDefault();
      const list = data.links.filter(matches);
      if (!list.length) return;
      let i = list.findIndex((l) => l.id === selId);
      i = k === 'ArrowDown' ? Math.min(list.length - 1, i + 1) : Math.max(0, i - 1);
      selectLink(list[i].id, true);
    }
  }

  window.LINKS = {
    draw,
    key,
    enter() { if (stale || !data) refresh(); else { renderList(); renderRight(); } bar(); },
    leave() { cancel(true); $('linkBar').classList.remove('show'); },
    modelLoaded(inStage) { stale = true; if (inStage) refresh(true, true); },
    refresh,
  };
  // the badge on the stage button, once the editor is up
  setTimeout(() => { if (!data) refresh(true); }, 1500);
})();
