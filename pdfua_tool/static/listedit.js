/* Dezquare PDF Accessibility Studio — Edit list (V4.19).
 *
 * The list beside its picture: items with their label and words, set in by
 * level. Join, split, nest, un-nest, move, take out; numbering; split / join
 * lists; "not a list"; templates. On the page: the lines between items (drag
 * one to move it, double-click to join the two, Shift+click inside an item
 * to split it) and zones that add items or take them out.
 *
 * Uses the editor through window.ED (static/editor.js). */
(function () {
  if (!window.ED || !document.getElementById('lstDlg')) return;
  const ED = window.ED;
  const { $, el } = ED;
  const dlg = $('lstDlg');

  let lid = null;
  let view = null;
  let sel = null;               // the item in focus (the moving end of a selection)
  let multi = new Set();        // every selected item (V4.21.4: Shift / Ctrl)
  let lanchor = null;           // where a Shift selection starts
  let active = null;
  let zoneTool = 'lines';
  let partKey = null;
  let imgZoom = 1;

  async function post(path, body) {
    const d = await UA.post(ED.api(path), body);
    if (d.error) UA.toast(d.error, 4500);
    return d;
  }
  async function fetchView() {
    const d = await UA.get(ED.api(`/list/${lid}/edit`));
    if (d.error) { UA.toast(d.error); return null; }
    view = d;
    lid = d.id;
    return d;
  }
  const parts = () => {
    const out = [];
    if (!view) return out;
    Object.keys(view.pages || {}).map(Number).sort((a, b) => a - b).forEach((p) => {
      (view.pages[String(p)].parts || []).forEach((part, i) => out.push({ page: p, i, part, key: `${p}:${i}` }));
    });
    return out;
  };
  const item = (id) => view && view.items.find((x) => x.id === id);

  async function open(id) {
    lid = id;
    sel = null;
    partKey = null;
    $('lsItems').replaceChildren(el('p', { class: 'muted small', text: 'Reading the list…' }));
    if (!dlg.open) dlg.showModal();
    if (window.TBL) window.TBL.loadTemplates();
    if (await fetchView()) {
      const ps = parts();
      partKey = ((ps.find((x) => x.page === ED.page)) || ps[0] || {}).key || null;
      sel = view.items.length ? view.items[0].id : null;
      multi = new Set(sel ? [sel] : []);
      lanchor = sel;
      render();
    }
  }

  async function op(body, msg, keepMulti) {
    if (typeof readStop === 'function') readStop();
    const r = await post(`/list/${lid}/edit`, body);
    if (r.error) return false;
    if (r.gone) { await ED.load(ED.focus); dlg.close(); stop(); UA.toast(msg || 'Done'); return true; }
    if (r.view) view = r.view; else await fetchView();
    if (r.touched && r.touched.length && item(r.touched[r.touched.length - 1])) sel = r.touched[r.touched.length - 1];
    if (sel && !item(sel)) sel = view.items.length ? view.items[0].id : null;
    // a bulk change keeps its items selected
    const still = (keepMulti || []).filter((id) => item(id));
    multi = new Set(still.length > 1 ? still : sel ? [sel] : []);
    if (!multi.has(lanchor)) lanchor = sel;
    if (!parts().some((p) => p.key === partKey)) partKey = (parts()[0] || {}).key || null;
    await ED.load(ED.focus);
    if (dlg.open) render(true);
    if (active) ED.drawBoxes();
    if (msg) UA.toast(typeof msg === 'function' ? msg(r) : msg);
    return true;
  }

  // ------------------------------------------------------------ items
  function render(keep) {
    const box = $('lsItems');
    const st = box.scrollTop;
    const levels = view.lists.length;
    $('lsInfo').textContent = `${view.count} item(s)${levels > 1 ? ` · ${levels} lists (nested)` : ''}`;
    const frag = document.createDocumentFragment();
    view.items.forEach((it, i) => {
      const lab = el('span', { class: 'llbl' + (it.lbl ? '' : ' none'), text: it.lbl || '—', title: 'Double-click to change the label' });
      const body = el('span', { class: 'lbody', text: it.body || '(empty)', title: 'Double-click to change the words' });
      const row = el('div', { class: 'lsitem' + (multi.has(it.id) ? ' sel' : '') + (it.id === sel ? ' focus' : ''), role: 'option', tabindex: -1, 'data-id': it.id,
        'aria-selected': multi.has(it.id) ? 'true' : 'false',
        style: `--lv:${it.level};animation-delay:${Math.min(i, 30) * 10}ms` },
      el('span', { class: 'lvl', text: it.level ? '↳'.padStart(1) : '' }), lab, body,
      el('span', { class: 'tiny muted lpg', text: 'p' + (it.page + 1) }));
      row.addEventListener('click', (e) => pick(it.id, e));
      row.addEventListener('mousedown', (e) => { if (e.shiftKey) e.preventDefault(); });
      lab.addEventListener('dblclick', (e) => { e.stopPropagation(); inline(lab, it.lbl, (v) => op({ op: 'set_label', id: it.id, text: v }, 'Label changed')); });
      body.addEventListener('dblclick', (e) => { e.stopPropagation(); inline(body, it.body, (v) => op({ op: 'set_body', id: it.id, text: v }, 'Words changed'), true); });
      frag.append(row);
    });
    const ae = document.activeElement;
    const refocus = !ae || ae === document.body || box.contains(ae);
    box.replaceChildren(frag);
    if (keep) box.scrollTop = st;
    if (refocus && dlg.open && sel) {
      const r = box.querySelector(`.lsitem[data-id="${CSS.escape(sel)}"]`);
      if (r) r.focus({ preventScroll: true });
    }
    tools();
    renderImage();
  }
  function inline(span, value, save, multi) {
    const inp = el(multi ? 'textarea' : 'input', { class: 'cedit' });
    inp.value = value || '';
    span.replaceChildren(inp);
    inp.focus();
    inp.select();
    let done = false;
    const fin = (ok) => { if (done) return; done = true; if (ok && inp.value !== (value || '')) save(inp.value); else render(true); };
    inp.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); fin(true); }
      if (e.key === 'Escape') { e.preventDefault(); fin(false); }
    });
    inp.addEventListener('blur', () => fin(true));
  }
  const idx = (id) => view.items.findIndex((x) => x.id === id);
  function range(a, b) {
    const i = idx(a), j = idx(b);
    if (i < 0 || j < 0) return new Set([b]);
    const [lo, hi] = i <= j ? [i, j] : [j, i];
    return new Set(view.items.slice(lo, hi + 1).map((x) => x.id));
  }
  // click: one item · Shift+click: a run from the anchor · Ctrl+click: add / drop one
  function pick(id, e) {
    if (e && e.keep) { /* the selection stays as it is */ }
    else if (e && e.shiftKey && lanchor && item(lanchor)) multi = range(lanchor, id);
    else if (e && (e.ctrlKey || e.metaKey)) {
      if (multi.has(id) && multi.size > 1) multi.delete(id); else multi.add(id);
      lanchor = id;
    } else { multi = new Set([id]); lanchor = id; }
    sel = id;
    const it = item(id);
    if (it && it.rects.length) {
      const p = parts().find((x) => x.page === it.rects[0][0] && x.part.items.includes(id));
      if (p && p.key !== partKey) partKey = p.key;
    }
    $('lsItems').querySelectorAll('.lsitem').forEach((r) => {
      r.classList.toggle('sel', multi.has(r.dataset.id));
      r.classList.toggle('focus', r.dataset.id === id);
      r.setAttribute('aria-selected', multi.has(r.dataset.id) ? 'true' : 'false');
    });
    const row = $('lsItems').querySelector(`.lsitem[data-id="${CSS.escape(id)}"]`);
    if (row) {
      row.scrollIntoView({ block: 'nearest' });
      const ae = document.activeElement;
      if (!ae || ae === document.body || $('lsItems').contains(ae)) row.focus({ preventScroll: true });
    }
    tools();
    renderImage();
  }
  function tools() {
    const it = sel && item(sel);
    const many = chosen().length > 1;
    ['lsIndent', 'lsOutdent', 'lsUp', 'lsDown', 'lsMerge', 'lsRelabel', 'lsRemove', 'lsArt', 'lsSplitList'].forEach((id) => { $(id).disabled = !it; });
    $('lsSplitList').disabled = !it || many;
    $('lsSelNone').disabled = !many;
    $('lsMerge').textContent = many ? 'Join selected' : 'Join with next';
    $('lsMerge').disabled = !it || (many && !contiguous());
    const ns = $('lsNumbering');
    if (!ns.options.length) view.numberings.forEach((n) => ns.append(el('option', { value: n, text: n })));
    const lst = it ? view.lists.find((l) => l.id === it.list) : view.lists[0];
    if (lst) ns.value = lst.numbering;
    if (many) {
      const c = chosen();
      $('lsSel').textContent = `${c.length} items selected · ${c[0].lbl || '—'} … ${c[c.length - 1].lbl || '—'}${contiguous() ? '' : ' (not side by side)'} — the buttons work on all of them`;
    } else $('lsSel').textContent = it ? `${it.lbl ? it.lbl + ' ' : ''}${(it.body || '').slice(0, 70)}${it.level ? ' · level ' + (it.level + 1) : ''}` : 'Click an item (Shift+click for more)';
  }
  // the selected items, top to bottom
  function chosen() {
    if (!view) return [];
    const c = view.items.filter((x) => multi.has(x.id));
    return c.length ? c : (sel && item(sel) ? [item(sel)] : []);
  }
  function contiguous() {
    const ix = chosen().map((x) => idx(x.id));
    return ix.every((v, k) => k === 0 || v === ix[k - 1] + 1);
  }

  // ------------------------------------------------------------ picture
  const M = 8;
  function renderImage() {
    const ps = parts();
    $('lsParts').replaceChildren(...ps.map((p) => el('button', { class: 'sm' + (p.key === partKey ? ' on' : ''), type: 'button',
      text: `p${p.page + 1}` + ((view.pages[String(p.page)].parts || []).length > 1 ? ` · ${p.i + 1}` : ''),
      onclick: () => { partKey = p.key; renderImage(); } })));
    const p = ps.find((x) => x.key === partKey);
    const img = $('lsImg'), ov = $('lsImgOv');
    ov.replaceChildren();
    if (!p) { img.removeAttribute('src'); return; }
    const b = p.part.box;
    const box = [b[0] - M, b[1] - M, b[2] + M, b[3] + M];
    const w = $('lsImgWrap').clientWidth - 16;
    const z = Math.max(0.5, Math.min(3, w / Math.max(40, box[2] - box[0]))) * imgZoom;
    const q = Math.min(4, Math.max(1, Math.round(z * 2) / 2 * (window.devicePixelRatio > 1 ? 1.5 : 1)));
    img.src = `/job/${ED.JID}/region/${p.page}.png?box=${box.map((v) => v.toFixed(1)).join(',')}&z=${q}&v=${Date.now() % 1e5}`;
    img.style.width = ((box[2] - box[0]) * z) + 'px';
    img.style.height = ((box[3] - box[1]) * z) + 'px';
    $('lsImgBox').style.width = img.style.width;
    $('lsImgBox').style.height = img.style.height;
    drawPart(ov, p.page, p.part, z, [box[0], box[1]], true);
  }
  $('lsZoomIn').addEventListener('click', () => { imgZoom = Math.min(3, imgZoom * 1.25); renderImage(); });
  $('lsZoomOut').addEventListener('click', () => { imgZoom = Math.max(0.4, imgZoom / 1.25); renderImage(); });

  function drawPart(cont, page, part, z, o, inDialog) {
    const X = (x) => (x - o[0]) * z, Y = (y) => (y - o[1]) * z;
    const frag = document.createDocumentFragment();
    for (const id of part.items) {
      const it = item(id);
      if (!it) continue;
      it.rects.filter((r) => r[0] === page).forEach((r) => {
        const d = el('div', { class: `litembox lv${Math.min(it.level, 3)}${(inDialog ? multi.has(it.id) : it.id === sel) ? ' sel' : ''}`, title: `${it.lbl} ${it.body}`.slice(0, 120) });
        Object.assign(d.style, { left: X(r[1]) + 'px', top: Y(r[2]) + 'px', width: (r[3] - r[1]) * z + 'px', height: (r[4] - r[2]) * z + 'px' });
        if (it.lbl) d.append(el('span', { class: 'ltag', text: it.lbl }));
        d.addEventListener('mousedown', (e) => { if (!inDialog) e.stopPropagation(); });
        d.addEventListener('click', (e) => {
          e.stopPropagation();
          if (e.shiftKey) {
            const rr = cont.getBoundingClientRect();
            const y = (e.clientY - rr.top) / z + o[1];
            op({ op: 'split', id: it.id, page, y }, 'Item split in two');
            return;
          }
          if (inDialog) pick(it.id, e);
          else { sel = it.id; ED.drawBoxes(); }
        });
        frag.append(d);
      });
    }
    (part.lines || []).forEach((ln) => {
      const a = item(ln.a);
      const b = item(ln.b);
      if (!a || !b) return;
      const ra = a.rects.find((r) => r[0] === page), rb = b.rects.find((r) => r[0] === page);
      const x0 = Math.min(ra ? ra[1] : part.box[0], rb ? rb[1] : part.box[0]);
      const x1 = Math.max(ra ? ra[3] : part.box[2], rb ? rb[3] : part.box[2]);
      const d = el('div', { class: 'tline row lline', title: 'Drag to move the line between these two items · double-click: join them' });
      Object.assign(d.style, { left: X(x0) + 'px', width: (x1 - x0) * z + 'px', top: Y(ln.y) - 4 + 'px' });
      d.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        e.preventDefault();
        drag = { d, a: ln.a, b: ln.b, y: ln.y, z, page, start: e.clientY, base: Y(ln.y) - 4, moved: false };
        d.classList.add('dragging');
      });
      d.addEventListener('dblclick', (e) => { e.stopPropagation(); op({ op: 'merge_next', id: ln.a }, 'The two items are one now'); });
      frag.append(d);
    });
    cont.append(frag);
  }
  let drag = null;
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    const dy = e.clientY - drag.start;
    if (Math.abs(dy) > 2) drag.moved = true;
    drag.d.style.top = (drag.base + dy) + 'px';
  });
  window.addEventListener('mouseup', (e) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    d.d.classList.remove('dragging');
    if (!d.moved) return;
    const y = d.y + (e.clientY - d.start) / d.z;
    op({ op: 'move_line', id: d.a, b: d.b, page: d.page, y }, 'Line moved — both items read their words again');
  });

  // ------------------------------------------------------------ buttons
  const on = (id, fn) => $(id).addEventListener('click', fn);
  const need = () => { if (!sel) UA.toast('Click an item first'); return sel; };
  // one change for every selected item, in one step (one Undo)
  function bulk(kind, extra, msg, order) {
    const c = chosen();
    if (!c.length) { UA.toast('Click an item first'); return; }
    if (c.length === 1) return op({ op: kind, id: c[0].id, ...(extra || {}) }, msg);
    const ids = c.map((x) => x.id);
    const seq = order === 'up' ? ids.slice().reverse() : ids;
    return op({ op: 'many', ops: seq.map((id) => ({ op: kind, id, ...(extra || {}) })) }, typeof msg === 'function' ? msg : `${ids.length} items: ${msg}`, ids);
  }
  on('lsIndent', () => bulk('indent', null, 'Nested under the item above'));
  on('lsOutdent', () => bulk('outdent', null, 'Out of the nested list'));
  on('lsUp', () => bulk('move', { dir: -1 }, 'Moved up'));
  on('lsDown', () => bulk('move', { dir: 1 }, 'Moved down', 'up'));
  on('lsMerge', () => {
    const c = chosen();
    if (c.length < 2) return need() && op({ op: 'merge_next', id: sel }, 'Joined with the next item');
    if (!contiguous()) { UA.toast('Pick items side by side to join them (Shift+click)'); return; }
    op({ op: 'many', ops: c.slice(1).map(() => ({ op: 'merge_next', id: c[0].id })) }, `${c.length} items are one now`);
  });
  on('lsRelabel', () => bulk('relabel', null, 'Label and words read again'));
  on('lsRemove', () => {
    const c = chosen();
    if (c.length && c.length === view.items.length) { if (confirm('Every item is selected — the whole list becomes paragraphs?')) op({ op: 'not_a_list' }, 'Paragraphs now'); return; }
    bulk('remove', null, 'Out of the list — a paragraph now');
  });
  on('lsArt', () => {
    const c = chosen();
    if (!c.length) return need();
    if (c.length === view.items.length) { UA.toast('Every item is selected — use “Not a list”, or leave one item out'); return; }
    if (confirm(c.length > 1 ? `Take the ${c.length} selected items out of the list and make them artifacts (not read)?` : 'Take the item out of the list and make it an artifact (not read)?')) bulk('remove', { artifact: true }, 'An artifact now');
  });
  on('lsSelNone', () => { if (sel) { multi = new Set([sel]); lanchor = sel; pick(sel); } });
  // read the list aloud, item by item, at the chosen speed (V4.21.4)
  let reading = null;
  $('lsRateBox').append(UA.rateControl('lsRate'));
  function readStop() {
    reading = null;
    UA.hush();
    $('lsRead').textContent = '🔊 Read the list';
    $('lsRead').classList.remove('on');
  }
  async function readList() {
    if (reading) { readStop(); return; }
    if (!view || !view.items.length) return;
    const my = {};
    reading = my;
    $('lsRead').textContent = '⏸ Stop reading';
    $('lsRead').classList.add('on');
    let i = Math.max(0, idx(sel));
    if (i >= view.items.length - 1) i = 0;
    let level = -1;
    if ($('lsSpeak').checked) await UA.say(`list with ${view.items.filter((x) => !x.level).length} items`);
    while (reading === my && i < view.items.length) {
      const it = view.items[i];
      pick(it.id);
      const pre = it.level !== level && level >= 0 ? (it.level > level ? `nested list, level ${it.level + 1}. ` : 'out of nested list. ') : '';
      level = it.level;
      const ok = await UA.say(`${pre}${it.lbl ? it.lbl + ' ' : 'bullet '}${it.body || ''}`, { silent: !$('lsSpeak').checked });
      if (reading !== my || !ok) return;
      await new Promise((r) => setTimeout(r, 250 / UA.speechRate()));
      i++;
    }
    if (reading === my) { if ($('lsSpeak').checked) await UA.say('out of list'); readStop(); }
  }
  on('lsRead', readList);
  dlg.addEventListener('close', readStop);
  on('lsSplitList', () => need() && op({ op: 'split_list', id: sel }, 'Split into two lists'));
  on('lsJoinNext', () => op({ op: 'merge_next_list' }, 'The next list joined this one'));
  // V4.21: the whole list at once
  on('lsJoinLoose', () => op({ op: 'join_unlabelled' }, (r) => r.joined ? `${r.joined} line(s) joined to the item above` : 'Every item has its label'));
  on('lsByLabels', () => op({ op: 'by_labels' }, (r) => `${r.items} item(s), one at each label`));
  on('lsPerLine', () => op({ op: 'one_per_line' }, (r) => `${r.items} item(s), one per line`));
  on('lsAutoNest', () => op({ op: 'auto_nest' }, (r) => `Nested by the labels: ${r.levels} level(s)`));
  on('lsFlatten', () => op({ op: 'flatten' }, (r) => `${r.items} item(s) in one list`));
  on('lsNumAuto', () => op({ op: 'numbering_auto' }, (r) => `${r.changed} list(s) got their numbering from the labels`));
  on('lsNotList', () => confirm('Not a list? Every item becomes a paragraph.') && op({ op: 'not_a_list' }, 'Paragraphs now'));
  $('lsNumbering').addEventListener('change', () => {
    const it = sel && item(sel);
    op({ op: 'numbering', list: it ? it.list : lid, value: $('lsNumbering').value }, `Numbering: ${$('lsNumbering').value}`);
  });
  on('lsTplSave', async () => {
    const r = await UA.post('/api/templates', { kind: 'list', job: ED.JID, id: lid, number: $('lsTplNum').value, name: $('lsTplName').value });
    if (r.error) { UA.toast(r.error, 4500); return; }
    UA.toast(`Saved as list template #${r.number}: ${r.summary}`, 4500);
    $('lsTplNum').value = '';
    if (window.TBL) { await window.TBL.loadTemplates(); $('lsTplPick').value = String(r.number); }
  });
  on('lsTplApply', async () => {
    const n = $('lsTplPick').value;
    if (!n) { UA.toast('Save a template first'); return; }
    if (!confirm(`Give this list the format of template #${n}? (Undo brings it back)`)) return;
    const r = await window.TBL.applyTemplate('list', n, { id: lid });
    if (r && !r.error) { lid = r.id || lid; if (await fetchView()) render(); }
  });
  on('lsTplMode', () => {
    const n = $('lsTplPick').value;
    if (!n) { UA.toast('Save a template first'); return; }
    dlg.close();
    window.TBL.startTpl('list', n);
  });
  async function undo() {
    const r = await UA.post(ED.api('/undo'));
    if (r.error) { UA.toast(r.error); return; }
    await ED.load(ED.focus);
    if (await fetchView().catch(() => null)) { if (dlg.open) render(true); if (active) ED.drawBoxes(); }
    else { dlg.close(); stop(); }
  }
  on('lsUndo', undo);
  on('lsDone', () => dlg.close());
  on('lsClose2', () => dlg.close());
  on('lsPage', () => startPage());
  dlg.addEventListener('keydown', (e) => {
    if (['TEXTAREA', 'INPUT', 'SELECT'].includes(e.target.tagName) || !view) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
    keyList(e);
  });
  dlg.addEventListener('cancel', (e) => { if (multi.size > 1) { e.preventDefault(); $('lsSelNone').click(); } });
  // the keys (V4.21.4): arrows move, Shift extends, Ctrl jumps to the ends,
  // Ctrl+Shift selects to the ends, Ctrl+A all, Ctrl+Space adds / drops one
  function keyList(e) {
    const n = view.items.length;
    if (!n) return;
    const ctrl = e.ctrlKey || e.metaKey;
    const i = Math.max(0, idx(sel));
    if (ctrl && !e.shiftKey && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      multi = new Set(view.items.map((x) => x.id));
      lanchor = view.items[0].id;
      pick(sel || view.items[0].id, { keep: true });
      UA.toast(`All ${n} items selected`, 1500);
      return;
    }
    if (ctrl && e.key === ' ' && sel) { e.preventDefault(); pick(sel, { ctrlKey: true }); return; }
    let j = null;
    if (e.key === 'ArrowDown') j = ctrl ? n - 1 : Math.min(n - 1, i + 1);
    else if (e.key === 'ArrowUp') j = ctrl ? 0 : Math.max(0, i - 1);
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = n - 1;
    else if (e.key === 'PageDown') j = Math.min(n - 1, i + 8);
    else if (e.key === 'PageUp') j = Math.max(0, i - 8);
    if (j != null) {
      e.preventDefault();
      const id = view.items[j].id;
      if (e.shiftKey) { if (!lanchor || !item(lanchor)) lanchor = sel || id; pick(id, { shiftKey: true }); }
      else pick(id);
      return;
    }
    if (e.key === 'Tab' && sel) { e.preventDefault(); $(e.shiftKey ? 'lsOutdent' : 'lsIndent').click(); return; }
    if (ctrl || e.altKey) return;
    if (e.key === 'Delete' && sel) { e.preventDefault(); $('lsRemove').click(); return; }
    if (e.key.toLowerCase() === 'j' && sel) { e.preventDefault(); $('lsMerge').click(); }
  }
  window.addEventListener('resize', () => { if (dlg.open && view) renderImage(); });

  // ------------------------------------------------------------ on the page
  function startPage() {
    if (!view || !parts().length) { UA.toast('This list has no zones on a page'); return; }
    if (window.TBL) window.TBL.stop();
    active = lid;
    dlg.close();
    const pages = parts().map((p) => p.page);
    if (!pages.includes(ED.page)) ED.setPage(pages[0]); else ED.drawBoxes();
    $('lstBar').classList.add('show');
    setZone('lines');
  }
  function stop() {
    if (!active) return;
    active = null;
    $('lstBar').classList.remove('show');
    ED.drawBoxes();
  }
  function setZone(z) {
    zoneTool = z;
    $('lstZoneSeg').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.z === z));
    $('lstHint').textContent = {
      lines: 'Shift+click inside an item: split it · drag the line between two items · double-click it: join them',
      add: 'drag a box over printed lines: they join the list — a new item at each label',
      add1: 'drag a box: it joins the list as one item',
      remove: 'drag a box over items: they leave the list as paragraphs',
      label: 'drag a box over the label of an item (1., a), •) — or down a column of labels: each becomes its item\'s Lbl',
    }[z];
    ED.drawBoxes();
  }
  $('lstZoneSeg').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => setZone(b.dataset.z)));
  on('lstDone', stop);
  on('lstReopen', () => { const id = active; stop(); open(id); });

  let zs = null, zr = null;
  function draw(overlay) {
    if (!active || !view) return;
    overlay.classList.add('lstadjust');
    if (zoneTool !== 'lines') {
      const layer = el('div', { class: 'tzone' });
      layer.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        e.preventDefault();
        const r = overlay.getBoundingClientRect();
        zs = { x: e.clientX - r.left, y: e.clientY - r.top, cx: e.clientX, cy: e.clientY, overlay };
      });
      overlay.append(layer);
    }
    const P = view.pages[String(ED.page)];
    if (!P) return;
    P.parts.forEach((part) => {
      const lay = el('div', { class: 'tpart' + (zoneTool !== 'lines' ? ' dim' : '') });
      Object.assign(lay.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
      overlay.append(lay);
      drawPart(lay, ED.page, part, ED.zoom, [0, 0], false);
      lay.querySelectorAll('.tline, .litembox').forEach((x) => { x.style.pointerEvents = zoneTool === 'lines' ? 'auto' : 'none'; });
    });
  }
  window.addEventListener('mousemove', (e) => {
    if (!zs) return;
    if (!zr && Math.abs(e.clientX - zs.cx) + Math.abs(e.clientY - zs.cy) < 5) return;
    const r = zs.overlay.getBoundingClientRect();
    const x = Math.max(0, Math.min(r.width, e.clientX - r.left)), y = Math.max(0, Math.min(r.height, e.clientY - r.top));
    if (!zr) { zr = el('div', { class: 'rubber tzr' }); zs.overlay.append(zr); }
    Object.assign(zr.style, { left: Math.min(x, zs.x) + 'px', top: Math.min(y, zs.y) + 'px', width: Math.abs(x - zs.x) + 'px', height: Math.abs(y - zs.y) + 'px' });
  });
  window.addEventListener('mouseup', (e) => {
    if (!zs) return;
    const s = zs;
    zs = null;
    if (!zr) return;
    zr.remove();
    zr = null;
    const r = s.overlay.getBoundingClientRect();
    const z = ED.zoom;
    const x = Math.max(0, Math.min(r.width, e.clientX - r.left)), y = Math.max(0, Math.min(r.height, e.clientY - r.top));
    const box = [Math.min(x, s.x) / z, Math.min(y, s.y) / z, Math.max(x, s.x) / z, Math.max(y, s.y) / z];
    if (box[2] - box[0] < 3 || box[3] - box[1] < 3) return;
    if (zoneTool === 'label') {
      op({ op: 'label_zone', page: ED.page, rect: box }, (r2) => `${(r2.touched || []).length} item(s) got their label from the box`);
      return;
    }
    if (zoneTool === 'remove') {
      if (confirm('The items in the zone leave the list as paragraphs?')) op({ op: 'remove_zone', page: ED.page, rect: box }, (r2) => `${(r2.made || []).length} item(s) out of the list`);
    } else {
      op({ op: 'add_zone', page: ED.page, rect: box, mode: zoneTool === 'add1' ? 'one' : 'labels' }, (r2) => `${(r2.touched || []).length} item(s) added`);
    }
  });

  window.LST = { open, draw, stop, get active() { return active; } };
})();
