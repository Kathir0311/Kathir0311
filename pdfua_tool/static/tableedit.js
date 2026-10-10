/* Dezquare PDF Accessibility Studio — Edit table (V4.18, V4.19).
 *
 * The table as a grid beside its picture: rows and columns added, deleted,
 * split, merged, moved; cells merged and split; TH / TD; header rows and
 * columns; row groups; split / join tables; "not a table"; read again with
 * N columns; templates. On the page: the lines of each printed part of the
 * table (only over its own cells), zones that add rows or a column or take
 * rows out, and "apply template to the tables I click".
 *
 * Uses the editor through window.ED (static/editor.js). */
(function () {
  if (!window.ED || !document.getElementById('tblDlg')) return;
  const ED = window.ED;
  const { $, el } = ED;
  const dlg = $('tblDlg');

  let tid = null;
  let grid = null;
  let sel = new Set();
  let anchor = null;
  let active = null;           // table id while it is edited on the page
  let zoneTool = 'lines';
  let partKey = null;          // "page:i" shown in the picture
  let imgZoom = 1;
  let tplMode = null;          // {kind, number} while applying a template by clicking
  let cur = null;              // {r, c}: the moving end of a keyboard selection (V4.21.4)
  let ro = null;               // the reading order check: {mode: 'check'|'click', i, seq, timer}

  async function post(path, body) {
    const d = await UA.post(ED.api(path), body);
    if (d.error) UA.toast(d.error, 4500);
    return d;
  }
  async function fetchGrid() {
    const d = await UA.get(ED.api(`/table/${tid}/grid`));
    if (d.error) { UA.toast(d.error); return null; }
    grid = d;
    return d;
  }
  const parts = () => {
    const out = [];
    if (!grid) return out;
    Object.keys(grid.lines || {}).map(Number).sort((a, b) => a - b).forEach((p) => {
      (grid.lines[String(p)].parts || []).forEach((part, i) => out.push({ page: p, i, part, key: `${p}:${i}` }));
    });
    return out;
  };

  async function open(id) {
    tid = id;
    sel = new Set();
    anchor = null;
    cur = null;
    roQuit();
    partKey = null;
    $('tbGrid').replaceChildren(el('p', { class: 'muted small', text: 'Reading the table…' }));
    if (!dlg.open) dlg.showModal();
    loadTemplates();
    if (await fetchGrid()) { const ps = parts(); const onPage = ps.find((x) => x.page === ED.page); partKey = (onPage || ps[0] || {}).key || null; render(); }
  }

  async function op(body, msg) {
    const r = await post(`/table/${tid}/grid`, body);
    if (r.error) return false;
    if (r.gone || r.new_table || r.joined) {
      await ED.load(ED.focus);
      if (r.gone) { dlg.close(); stop(); UA.toast(msg || 'Done'); return true; }
    }
    if (r.grid) grid = r.grid; else await fetchGrid();
    const keep = new Set((r.touched || []).filter((x) => grid.cells.some((c) => c.id === x)));
    sel = keep.size ? new Set([...keep].slice(0, 1)) : new Set([...sel].filter((x) => grid.cells.some((c) => c.id === x)));
    anchor = [...sel][0] || null;
    if (!parts().some((p) => p.key === partKey)) partKey = (parts()[0] || {}).key || null;
    await ED.load(ED.focus);
    if (dlg.open) render(true);
    if (active) ED.drawBoxes();
    if (msg) UA.toast(typeof msg === 'function' ? msg(r) : msg);
    return true;
  }

  // ------------------------------------------------------------ the grid
  const colName = (c) => {
    let s = '';
    c += 1;
    while (c > 0) { const m = (c - 1) % 26; s = String.fromCharCode(65 + m) + s; c = Math.floor((c - 1) / 26); }
    return s;
  };
  function cellAt(r, c) {
    return grid.cells.find((x) => x.r <= r && r < x.r + x.rs && x.c <= c && c < x.c + x.cs);
  }
  function selected() { return grid.cells.filter((c) => sel.has(c.id)); }
  function selBox() {
    const s = selected();
    if (!s.length) return null;
    return { r0: Math.min(...s.map((c) => c.r)), c0: Math.min(...s.map((c) => c.c)),
      r1: Math.max(...s.map((c) => c.r + c.rs - 1)), c1: Math.max(...s.map((c) => c.c + c.cs - 1)) };
  }
  function pickBlock(a, b) {
    const r0 = Math.min(a.r, b.r), c0 = Math.min(a.c, b.c);
    const r1 = Math.max(a.r + a.rs - 1, b.r + b.rs - 1), c1 = Math.max(a.c + a.cs - 1, b.c + b.cs - 1);
    sel = new Set(grid.cells.filter((x) => x.r <= r1 && x.r + x.rs - 1 >= r0 && x.c <= c1 && x.c + x.cs - 1 >= c0).map((x) => x.id));
  }
  function clickCell(cell, e) {
    if (ro && ro.mode === 'click') { roPick(cell); return; }
    if (ro && ro.mode === 'check' && !(e && (e.shiftKey || e.ctrlKey || e.metaKey))) { roStop(); roGo(cell.order - 1); return; }
    if (e && e.shiftKey && anchor) {
      const a = grid.cells.find((x) => x.id === anchor);
      if (a) pickBlock(a, cell);
    } else if (e && (e.ctrlKey || e.metaKey)) {
      sel.has(cell.id) ? sel.delete(cell.id) : sel.add(cell.id);
      anchor = cell.id;
    } else {
      sel = new Set([cell.id]);
      anchor = cell.id;
    }
    cur = { r: cell.r, c: cell.c };
    // the picture follows the cell
    if (cell.rect) {
      const p = parts().find((x) => x.page === cell.page && x.part.cells.includes(cell.id));
      if (p && p.key !== partKey) { partKey = p.key; renderImage(); }
    }
    render(true);
  }

  function render(keepScroll) {
    const wrap = $('tbGrid');
    const st = wrap.scrollTop, sl = wrap.scrollLeft;
    const ae = document.activeElement;
    const refocus = !ae || ae === document.body || wrap.contains(ae);
    $('tbInfo').textContent = `${grid.rows} rows × ${grid.cols} columns · ${grid.cells.length} cells`;
    if (document.activeElement !== $('tbSummary')) $('tbSummary').value = grid.summary || '';
    $('tbEmptyN').textContent = grid.empty_cells ? `${grid.empty_cells} empty cell(s)` : 'no empty cell';
    $('tbNote').textContent = grid.note ? '· ' + grid.note.slice(0, 90) : '';
    const table = el('table', { class: 'tbgrid' + ($('tbShowOrder').checked ? ' withord' : '') });
    const head = el('tr', {}, el('th', { class: 'corner', text: '' }));
    for (let c = 0; c < grid.cols; c++) {
      head.append(el('th', { class: 'colh', text: colName(c), title: 'Select the whole column',
        onclick: (e) => {
          const cells = grid.cells.filter((x) => x.c <= c && c < x.c + x.cs);
          if (e.shiftKey && anchor) cells.forEach((x) => sel.add(x.id)); else sel = new Set(cells.map((x) => x.id));
          anchor = cells[0] ? cells[0].id : null;
          render(true);
        } }));
    }
    table.append(el('thead', {}, head));
    const body = el('tbody');
    for (let r = 0; r < grid.rows; r++) {
      const info = grid.row_info[r] || {};
      const g = info.group && info.group !== 'Table' ? info.group : '';
      const tr = el('tr', { class: g === 'THead' ? 'ghead' : g === 'TFoot' ? 'gfoot' : '' });
      tr.append(el('th', { class: 'rowh', title: `Select the whole row${g ? ' (' + g + ')' : ''}`,
        onclick: (e) => {
          const cells = grid.cells.filter((x) => x.r <= r && r < x.r + x.rs);
          if (e.shiftKey && anchor) cells.forEach((x) => sel.add(x.id)); else sel = new Set(cells.map((x) => x.id));
          anchor = cells[0] ? cells[0].id : null;
          render(true);
        } }, String(r + 1), g ? el('span', { class: 'rg', text: g === 'THead' ? 'H' : g === 'TFoot' ? 'F' : '' }) : null));
      for (let c = 0; c < grid.cols; c++) {
        const cell = cellAt(r, c);
        if (!cell) { tr.append(el('td', { class: 'hole', title: 'No cell here — rows differ in width', text: '·' })); continue; }
        if (cell.r !== r || cell.c !== c) continue;
        const td = el('td', { class: `cell ${cell.role.toLowerCase()}${sel.has(cell.id) ? ' sel' : ''}${cell.rect ? '' : ' nozone'}${cell.kids ? ' kids' : ''}`,
          rowspan: cell.rs, colspan: cell.cs, tabindex: 0,
          title: `${cell.role}${cell.scope ? ' · scope ' + cell.scope : ''} · ${colName(cell.c)}${cell.r + 1}${cell.rs > 1 || cell.cs > 1 ? ` · spans ${cell.rs}×${cell.cs}` : ''}${cell.rect ? ' · p' + (cell.page + 1) : ' · no zone on the page (empty cell)'}`,
          'data-id': cell.id });
        td.append(el('span', { class: 'ctext', text: cell.text || (cell.kids ? '(tags inside)' : '—') }));
        if (cell.role === 'TH') td.append(el('span', { class: 'crole', text: 'TH' + (cell.scope ? ' ' + cell.scope[0] : '') }));
        if ($('tbShowOrder').checked) td.append(el('span', { class: 'cord', title: 'Read as number ' + cell.order, text: String(cell.order) }));
        td.addEventListener('click', (e) => clickCell(cell, e));
        td.addEventListener('dblclick', () => editText(td, cell));
        tr.append(td);
      }
      body.append(tr);
    }
    table.append(body);
    wrap.replaceChildren(table);
    if (keepScroll) { wrap.scrollTop = st; wrap.scrollLeft = sl; }
    // the keys keep working after a click (the grid is drawn anew)
    if (refocus && dlg.open) {
      const fc = (cur && cellAt(cur.r, cur.c)) || grid.cells.find((x) => x.id === anchor);
      const td = fc && wrap.querySelector(`td[data-id="${CSS.escape(fc.id)}"]`);
      if (td) td.focus({ preventScroll: true });
    }
    $('tbHr').value = grid.header_rows;
    $('tbHc').value = grid.header_cols;
    if (!$('tbRbCols').value) $('tbRbCols').value = grid.cols;
    $('tbChecks').replaceChildren(...(grid.checks || []).map((c) => el('div', { class: 'tbcheck ' + c.level, text: c.msg })));
    $('tbJoinNext').disabled = !grid.has_next;
    tools();
    if (keepScroll && !$('tbShowOrder').checked) markImage(); else renderImage();
    if (ro) { roMark(); roShow(); }
  }

  // the tags inside one cell, in the order they are read (V4.21.3)
  function orderPanel(one) {
    const box = $('tbOrder');
    const kids = one && one.kid_list ? one.kid_list : [];
    if (!one || kids.length < 2) { box.classList.add('hidden'); box.replaceChildren(); return; }
    box.classList.remove('hidden');
    const ids = kids.map((k) => k.id);
    const send = (order, msg) => op({ op: 'cell_order', id: one.id, order }, msg);
    box.replaceChildren(
      el('div', { class: 'tbordhead' }, el('b', { text: `Read inside ${colName(one.c)}${one.r + 1}` }),
        el('span', { class: 'tiny muted', text: ' — drag or use the arrows' }), el('div', { class: 'spacer' }),
        el('button', { class: 'sm', text: 'By position', title: 'Top to bottom, left to right as printed',
          onclick: () => op({ op: 'cell_order_auto', ids: [one.id] }, (r) => (r.touched || []).length ? 'Ordered as printed' : 'Already in printed order') })),
      ...kids.map((k, i) => {
        const row = el('div', { class: 'tbordrow', draggable: 'true' },
          el('span', { class: 'ordn2', text: String(i + 1) }), el('span', { class: 'role', text: k.role }),
          el('span', { class: 'ctext', text: k.text || '—' }),
          el('button', { class: 'sm', text: '↑', disabled: i === 0, title: 'Read earlier',
            onclick: () => { const o = [...ids]; [o[i - 1], o[i]] = [o[i], o[i - 1]]; send(o, 'Moved up'); } }),
          el('button', { class: 'sm', text: '↓', disabled: i === kids.length - 1, title: 'Read later',
            onclick: () => { const o = [...ids]; [o[i + 1], o[i]] = [o[i], o[i + 1]]; send(o, 'Moved down'); } }));
        row.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', String(i)); row.classList.add('dragging'); });
        row.addEventListener('dragend', () => row.classList.remove('dragging'));
        row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('over'); });
        row.addEventListener('dragleave', () => row.classList.remove('over'));
        row.addEventListener('drop', (e) => {
          e.preventDefault();
          const from = parseInt(e.dataTransfer.getData('text/plain'), 10);
          if (isNaN(from) || from === i) return;
          const o = [...ids];
          const [m] = o.splice(from, 1);
          o.splice(i, 0, m);
          send(o, 'Order changed');
        });
        return row;
      }));
  }
  function editText(td, cell) {
    if (cell.kids) { UA.toast('This cell holds tags of its own — edit them in the tag tree'); return; }
    const inp = el('textarea', { class: 'cedit', rows: 2 });
    inp.value = cell.text || '';
    td.replaceChildren(inp);
    inp.focus();
    inp.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      if (save && inp.value !== (cell.text || '')) await op({ op: 'set_text', id: cell.id, text: inp.value }, 'Cell text saved');
      else render(true);
    };
    inp.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); finish(true); }
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    inp.addEventListener('blur', () => finish(true));
  }

  function tools() {
    const b = selBox();
    const s = selected();
    const one = s.length === 1 ? s[0] : null;
    const set = (id, on) => { const x = $(id); if (x) x.disabled = !on; };
    ['tbRowAbove', 'tbRowBelow', 'tbRowDel', 'tbRowArt', 'tbColLeft', 'tbColRight', 'tbColDel', 'tbTH', 'tbTD'].forEach((id) => set(id, !!b));
    set('tbRowMerge', !!b && b.r1 < grid.rows - 1);
    set('tbColL', !!b && b.c0 > 0);
    set('tbColR', !!b && b.c1 < grid.cols - 1);
    orderPanel(one);
    set('tbColMerge', !!b && b.c1 < grid.cols - 1);
    set('tbRowUp', !!b && b.r0 > 0);
    set('tbRowDown', !!b && b.r1 < grid.rows - 1);
    set('tbMerge', s.length > 1);
    set('tbSplit', !!one && (one.rs > 1 || one.cs > 1));
    set('tbSplitTable', !!b && b.r0 > 0);
    $('tbSel').textContent = b ? (s.length === 1 ? `${colName(b.c0)}${b.r0 + 1}: “${(one.text || '').slice(0, 50)}”` : `${s.length} cells · ${colName(b.c0)}${b.r0 + 1}:${colName(b.c1)}${b.r1 + 1}`) : 'Click a cell (Shift+click for a block)';
  }

  // ------------------------------------------------------------ the picture
  const M = 8;          // margin around the part, points
  function renderImage() {
    const ps = parts();
    $('tbParts').replaceChildren(...ps.map((p) => el('button', { class: 'sm' + (p.key === partKey ? ' on' : ''), type: 'button',
      text: `p${p.page + 1}` + ((grid.lines[String(p.page)].parts || []).length > 1 ? ` · ${p.i + 1}` : ''),
      title: `Page ${p.page + 1}, part ${p.i + 1} of the table`, onclick: () => { partKey = p.key; renderImage(); } })));
    const p = ps.find((x) => x.key === partKey);
    const img = $('tbImg'), ov = $('tbImgOv');
    ov.replaceChildren();
    if (!p) { img.removeAttribute('src'); img.alt = 'The table has no zone on a page'; return; }
    const b = p.part.box;
    const box = [b[0] - M, b[1] - M, b[2] + M, b[3] + M];
    const w = $('tbImgWrap').clientWidth - 16;
    const fit = Math.max(0.5, Math.min(3, w / Math.max(40, box[2] - box[0])));
    const z = fit * imgZoom;
    const q = Math.min(4, Math.max(1, Math.round(z * 2) / 2 * (window.devicePixelRatio > 1 ? 1.5 : 1)));
    img.src = `/job/${ED.JID}/region/${p.page}.png?box=${box.map((v) => v.toFixed(1)).join(',')}&z=${q}&v=${Date.now() % 1e5}`;
    img.style.width = ((box[2] - box[0]) * z) + 'px';
    img.style.height = ((box[3] - box[1]) * z) + 'px';
    $('tbImgBox').style.width = img.style.width;
    $('tbImgBox').style.height = img.style.height;
    $('tbImgBox').dataset.z = z;
    drawPart(ov, p.page, p.i, p.part, z, [box[0], box[1]], true);
    if (ro) roMark();
  }
  function markImage() {
    $('tbImgOv').querySelectorAll('.ccell').forEach((d) => d.classList.toggle('sel', sel.has(d.dataset.id)));
    const ps = parts();
    $('tbParts').querySelectorAll('button').forEach((bt, i) => bt.classList.toggle('on', ps[i] && ps[i].key === partKey));
  }
  $('tbZoomIn').addEventListener('click', () => { imgZoom = Math.min(3, imgZoom * 1.25); renderImage(); });
  $('tbZoomOut').addEventListener('click', () => { imgZoom = Math.max(0.4, imgZoom / 1.25); renderImage(); });
  $('tbShowCells').addEventListener('change', () => renderImage());

  /* The lines (and, in the picture, the cells) of one printed part of the
     table: only as long as the cells along them. `o`: the display point at
     the container's corner; `z`: display points → pixels. */
  function drawPart(cont, page, pi, part, z, o, inDialog) {
    const frag = document.createDocumentFragment();
    const X = (x) => (x - o[0]) * z, Y = (y) => (y - o[1]) * z;
    const [bx0, by0, bx1, by1] = part.box;
    const split = (e) => {
      const r = cont.getBoundingClientRect();
      const x = (e.clientX - r.left) / z + o[0], y = (e.clientY - r.top) / z + o[1];
      if (e.shiftKey) {
        const k = rowAt(part, y);
        if (k != null) { e.stopPropagation(); op({ op: 'split_row', index: k, page, pos: y, part: pi }, 'A new row line'); }
      } else if (e.altKey) {
        const k = colAt(part, x);
        if (k != null) { e.stopPropagation(); op({ op: 'split_col', index: k, page, pos: x, part: pi }, 'A new column line'); }
      }
    };
    if (inDialog && $('tbShowCells').checked) {
      for (const cid of part.cells) {
        const c = grid.cells.find((x) => x.id === cid);
        if (!c || !c.rect) continue;
        const d = el('div', { class: `ccell ${c.role.toLowerCase()}${sel.has(c.id) ? ' sel' : ''}`, title: `${colName(c.c)}${c.r + 1}: ${(c.text || '—').slice(0, 80)}` });
        d.dataset.id = c.id;
        if ($('tbShowOrder').checked) d.append(el('span', { class: 'cord', text: String(c.order) }));
        Object.assign(d.style, { left: X(c.rect[0]) + 'px', top: Y(c.rect[1]) + 'px', width: (c.rect[2] - c.rect[0]) * z + 'px', height: (c.rect[3] - c.rect[1]) * z + 'px' });
        d.addEventListener('click', (e) => {
          e.stopPropagation();
          if (e.shiftKey || e.altKey) { split(e); return; }
          clickCell(c, e);
        });
        frag.append(d);
      }
    }
    const catcher = el('div', { class: 'tcatch' + (inDialog ? ' indlg' : ''), title: 'Shift+click: a new row line here · Alt+click: a new column line here' });
    Object.assign(catcher.style, { left: X(bx0) + 'px', top: Y(by0) + 'px', width: (bx1 - bx0) * z + 'px', height: (by1 - by0) * z + 'px' });
    catcher.addEventListener('mousedown', (e) => { if (!inDialog) e.stopPropagation(); });
    catcher.addEventListener('click', (e) => { if (e.shiftKey || e.altKey) split(e); });
    frag.prepend(catcher);
    const line = (kind, k, pos, span) => {
      const d = el('div', { class: `tline ${kind}`, title: kind === 'row'
        ? `Row line ${k} — drag to move it, double-click to take it out (rows ${k} and ${k + 1} become one)`
        : `Column line ${colName(k - 1)}|${colName(k)} — drag to move it, double-click to take it out` });
      if (kind === 'row') Object.assign(d.style, { left: X(span[0]) + 'px', width: (span[1] - span[0]) * z + 'px', top: Y(pos) - 4 + 'px' });
      else Object.assign(d.style, { top: Y(span[0]) + 'px', height: (span[1] - span[0]) * z + 'px', left: X(pos) - 4 + 'px' });
      d.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        e.preventDefault();
        drag = { d, kind, k, pos, z, page, pi, start: kind === 'row' ? e.clientY : e.clientX, moved: false };
        d.classList.add('dragging');
      });
      d.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        op({ op: kind === 'row' ? 'merge_rows' : 'merge_cols', index: k - 1 }, kind === 'row' ? 'Rows merged' : 'Columns merged');
      });
      frag.append(d);
    };
    Object.entries(part.rows).forEach(([k, y]) => { k = +k; if (k > 0 && k < grid.rows && part.rows[k - 1] != null) line('row', k, y, part.row_span[k] || [bx0, bx1]); });
    Object.entries(part.cols).forEach(([k, x]) => { k = +k; if (k > 0 && k < grid.cols && part.cols[k - 1] != null) line('col', k, x, part.col_span[k] || [by0, by1]); });
    cont.append(frag);
  }
  function rowAt(part, y) {
    let best = null;
    Object.entries(part.rows).forEach(([k, v]) => { if (v <= y && (best == null || v > part.rows[best])) best = k; });
    return best == null ? null : Math.min(+best, grid.rows - 1);
  }
  function colAt(part, x) {
    let best = null;
    Object.entries(part.cols).forEach(([k, v]) => { if (v <= x && (best == null || v > part.cols[best])) best = k; });
    return best == null ? null : Math.min(+best, grid.cols - 1);
  }
  let drag = null;
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    const delta = ((drag.kind === 'row' ? e.clientY : e.clientX) - drag.start);
    if (Math.abs(delta) > 2) drag.moved = true;
    const cur = parseFloat(drag.d.style[drag.kind === 'row' ? 'top' : 'left']);
    if (drag.base == null) drag.base = cur;
    drag.d.style[drag.kind === 'row' ? 'top' : 'left'] = (drag.base + delta) + 'px';
  });
  window.addEventListener('mouseup', (e) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    d.d.classList.remove('dragging');
    if (!d.moved) return;
    const pos = d.pos + ((d.kind === 'row' ? e.clientY : e.clientX) - d.start) / d.z;
    op({ op: 'move_line', kind: d.kind, index: d.k, page: d.page, pos, part: d.pi }, 'Line moved — the cells read their words again');
  });
  window.addEventListener('resize', () => { if (dlg.open && grid) renderImage(); });

  // ------------------------------------------------------------ buttons
  const need = () => { const b = selBox(); if (!b) UA.toast('Click a cell first'); return b; };
  const on = (id, fn) => $(id).addEventListener('click', fn);
  on('tbRowAbove', () => { const b = need(); if (b) op({ op: 'insert_row', at: b.r0 }, 'Row added above'); });
  on('tbRowBelow', () => { const b = need(); if (b) op({ op: 'insert_row', at: b.r1 + 1 }, 'Row added below'); });
  on('tbRowDel', () => { const b = need(); if (b) op({ op: 'delete_row', index: b.r0 }, 'Row deleted'); });
  on('tbRowArt', () => { const b = need(); if (b && confirm(`Row ${b.r0 + 1} leaves the table and is not read (an artifact). Undo brings it back.`)) op({ op: 'delete_row', index: b.r0, artifact: true }, 'The row is an artifact now'); });
  on('tbRowMerge', () => { const b = need(); if (b) op({ op: 'merge_rows', index: b.r1 }, `Rows ${b.r1 + 1} and ${b.r1 + 2} are one row now`); });
  on('tbRowUp', () => { const b = need(); if (b) op({ op: 'move_row', index: b.r0, dir: -1 }, 'Row moved up'); });
  on('tbRowDown', () => { const b = need(); if (b) op({ op: 'move_row', index: b.r0, dir: 1 }, 'Row moved down'); });
  on('tbColLeft', () => { const b = need(); if (b) op({ op: 'insert_col', at: b.c0 }, 'Column added on the left'); });
  on('tbColRight', () => { const b = need(); if (b) op({ op: 'insert_col', at: b.c1 + 1 }, 'Column added on the right'); });
  on('tbColDel', () => { const b = need(); if (b) op({ op: 'delete_col', index: b.c0 }, 'Column deleted'); });
  on('tbColMerge', () => { const b = need(); if (b) op({ op: 'merge_cols', index: b.c1 }, `Columns ${colName(b.c1)} and ${colName(b.c1 + 1)} are one column now`); });
  on('tbMerge', () => op({ op: 'merge_cells', ids: [...sel] }, 'Cells merged'));
  on('tbSplit', () => op({ op: 'split_cell', id: [...sel][0] }, 'Cell split'));
  on('tbTH', () => op({ op: 'set_role', ids: [...sel], role: 'TH', scope: $('tbScope').value }, 'Header cells (TH)'));
  on('tbTD', () => op({ op: 'set_role', ids: [...sel], role: 'TD' }, 'Data cells (TD)'));
  on('tbReread', () => op({ op: 'reread', ids: sel.size ? [...sel] : null }, (r) => `${(r.touched || []).length} cell(s) read again`));
  on('tbHeaders', () => op({ op: 'headers', header_rows: $('tbHr').value, header_cols: $('tbHc').value, mode: $('tbHmode').value }, 'Header rows and columns set'));
  document.querySelectorAll('#tbMore [data-group]').forEach((b) => b.addEventListener('click', () => {
    const bx = need();
    if (!bx) return;
    const rows = [];
    for (let r = bx.r0; r <= bx.r1; r++) rows.push(r);
    op({ op: 'set_group', rows, group: b.dataset.group }, `Row(s) in ${b.dataset.group}`);
  }));
  on('tbSplitTable', () => { const b = need(); if (b && confirm(`Rows ${b.r0 + 1} to ${grid.rows} become a table of their own?`)) op({ op: 'split_table', index: b.r0 }, 'Split into two tables'); });
  on('tbJoinNext', () => op({ op: 'merge_next_table', drop: parseInt($('tbDrop').value || '0', 10) }, 'The next table joined this one'));
  on('tbUntable', () => { if (confirm('Not a table? Every ' + ($('tbUntableHow').value === 'cells' ? 'cell' : 'row') + ' becomes a paragraph.')) op({ op: 'untable', how: $('tbUntableHow').value }, 'The table is paragraphs now'); });
  on('tbRebuild', () => {
    const cols = parseInt($('tbRbCols').value || '0', 10);
    if (!confirm(`Read the table again with ${cols || grid.cols} column(s)? Its rows and cells are made anew (Undo brings them back).`)) return;
    op({ op: 'rebuild', cols, row_mode: $('tbRbMode').value || undefined }, (r) => `Read again: ${r.rows} rows × ${r.cols} columns`);
  });
  on('tbCsv', () => {
    const rows = [];
    for (let r = 0; r < grid.rows; r++) {
      const row = [];
      for (let c = 0; c < grid.cols; c++) {
        const cell = cellAt(r, c);
        row.push(cell && cell.r === r && cell.c === c ? cell.text || '' : '');
      }
      rows.push(row.map((v) => /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v).join(','));
    }
    const a = el('a', { href: URL.createObjectURL(new Blob(['﻿' + rows.join('\r\n')], { type: 'text/csv' })), download: `table_${tid}.csv` });
    document.body.append(a); a.click(); a.remove();
  });
  // V4.21: tidy, header rows / columns, empty cells, summary, paste
  on('tbColL', () => { const b = need(); if (b) op({ op: 'move_col', index: b.c0, dir: -1 }, `Column ${colName(b.c0)} moved left`); });
  on('tbColR', () => { const b = need(); if (b) op({ op: 'move_col', index: b.c0, dir: 1 }, `Column ${colName(b.c0)} moved right`); });
  on('tbOrderAll', () => op({ op: 'cell_order_auto' }, (r) => `${(r.touched || []).length} cell(s) now read their tags as printed`));
  $('tbShowOrder').addEventListener('change', () => { render(true); renderImage(); });
  on('tbTidy', () => op({ op: 'tidy' }, (r) => `Tidied: ${r.filled || 0} empty cell(s) added, ${r.rows_dropped || 0} empty row(s) and ${r.cols_dropped || 0} column(s) out, ${r.th_to_td || 0} empty header(s) → TD`));
  on('tbRowTH', () => { const b = need(); if (b) op({ op: 'row_th', index: b.r0 }, `Row ${b.r0 + 1}: header cells`); });
  on('tbColTH', () => { const b = need(); if (b) op({ op: 'col_th', index: b.c0 }, `Column ${colName(b.c0)}: header cells`); });
  on('tbRowTD', () => { const b = need(); if (b) op({ op: 'row_td', index: b.r0 }, `Row ${b.r0 + 1}: data cells`); });
  on('tbColTD', () => { const b = need(); if (b) op({ op: 'col_td', index: b.c0 }, `Column ${colName(b.c0)}: data cells`); });
  on('tbEmptyGo', () => op({ op: 'empty_cells', text: $('tbEmptyText').value }, (r) => `${r.cells} empty cell(s) read as “${$('tbEmptyText').value || 'the build setting'}”`));
  on('tbSummarySave', () => op({ op: 'summary', text: $('tbSummary').value }, $('tbSummary').value.trim() ? 'Summary saved' : 'Summary taken out'));
  on('tbSummaryAi', async () => {
    const b = $('tbSummaryAi');
    b.disabled = true; b.textContent = 'thinking…';
    const r = await UA.post(ED.api(`/table/${tid}/summary_ai`), {});
    b.disabled = false; b.textContent = 'Draft with AI';
    if (r.error) { UA.toast(r.error, 5000); return; }
    $('tbSummary').value = r.summary || '';
    $('tbSummary').classList.add('flashin');
    setTimeout(() => $('tbSummary').classList.remove('flashin'), 900);
  });
  function selectedTSV() {
    const b = selBox();
    if (!b) return '';
    const rows = [];
    for (let r = b.r0; r <= b.r1; r++) {
      const row = [];
      for (let c = b.c0; c <= b.c1; c++) {
        const cell = cellAt(r, c);
        row.push(cell && cell.r === r && cell.c === c ? (cell.text || '').replace(/[\t\n]+/g, ' ') : '');
      }
      rows.push(row.join('\t'));
    }
    return rows.join('\n');
  }
  dlg.addEventListener('copy', (e) => {
    if (['TEXTAREA', 'INPUT'].includes(e.target.tagName) || !grid || !sel.size) return;
    e.preventDefault();
    e.clipboardData.setData('text/plain', selectedTSV());
    UA.toast(`${sel.size} cell(s) copied`);
  });
  dlg.addEventListener('paste', (e) => {
    if (['TEXTAREA', 'INPUT'].includes(e.target.tagName) || !grid) return;
    const b = selBox();
    if (!b) return;
    const txt = (e.clipboardData || window.clipboardData).getData('text');
    if (!txt) return;
    e.preventDefault();
    const rows = txt.replace(/\r/g, '').replace(/\n$/, '').split('\n').map((r) => r.split('\t'));
    op({ op: 'set_texts', r: b.r0, c: b.c0, rows }, (r) => `${(r.touched || []).length} cell(s) got the pasted words`);
  });
  async function undo() {
    const r = await UA.post(ED.api('/undo'));
    if (r.error) { UA.toast(r.error); return; }
    await ED.load(ED.focus);
    if (dlg.open && await fetchGrid().catch(() => null)) render(true);
    else if (dlg.open) dlg.close();
    if (active) { await fetchGrid(); ED.drawBoxes(); }
  }
  on('tbUndo', undo);
  on('tbDone', () => dlg.close());
  on('tbClose2', () => dlg.close());
  on('tbAdjust', () => startAdjust());
  dlg.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (!grid) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
    if (roKey(e)) return;
    if (!e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'r') { e.preventDefault(); ro ? roClose() : roOpen(); return; }
    const s = selected();
    if (e.key === 'Delete' && s.length === 1 && !s[0].kids) { e.preventDefault(); op({ op: 'set_text', id: s[0].id, text: '' }, 'Cell emptied'); }
    if (!e.ctrlKey && !e.metaKey && !e.altKey && s.length) {
      const k = e.key.toLowerCase();
      if (k === 'm' && s.length > 1) { e.preventDefault(); $('tbMerge').click(); return; }
      if (k === 's' && s.length === 1 && (s[0].rs > 1 || s[0].cs > 1)) { e.preventDefault(); $('tbSplit').click(); return; }
      if (k === 'h') { e.preventDefault(); $('tbTH').click(); return; }
      if (k === 'd') { e.preventDefault(); $('tbTD').click(); return; }
    }
    if (e.key === 'Enter' && s.length === 1) {
      const td = $('tbGrid').querySelector(`td[data-id="${CSS.escape(s[0].id)}"]`);
      if (td) { e.preventDefault(); editText(td, s[0]); }
    }
    keyMove(e, s);
  });
  // Shift / Ctrl with the arrows (V4.21.4): the anchor stays, the other end
  // (`cur`) moves — one cell, or to the edge with Ctrl.
  function keyMove(e, s) {
    const ctrl = e.ctrlKey || e.metaKey;
    const all = () => grid.cells.slice().sort((a, b) => a.r - b.r || a.c - b.c);
    if (ctrl && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      const L = all();
      sel = new Set(L.map((x) => x.id));
      anchor = L.length ? L[0].id : null;
      cur = { r: grid.rows - 1, c: grid.cols - 1 };
      render(true);
      UA.toast(`All ${L.length} cells selected`, 1500);
      return;
    }
    if (e.key === ' ' && (e.shiftKey || ctrl) && !(e.shiftKey && ctrl)) {
      const b = selBox();
      if (!b) return;
      e.preventDefault();
      const rowWise = e.shiftKey;
      const cells = grid.cells.filter((x) => rowWise ? x.r <= b.r1 && x.r + x.rs - 1 >= b.r0 : x.c <= b.c1 && x.c + x.cs - 1 >= b.c0);
      sel = new Set(cells.map((x) => x.id));
      render(true);
      return;
    }
    const mv = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[e.key];
    if (!mv && e.key !== 'Home' && e.key !== 'End') return;
    if (e.altKey || !grid.cells.length) return;
    e.preventDefault();
    const a0 = grid.cells.find((x) => x.id === anchor) || s[0] || cellAt(0, 0) || all()[0];
    const p = cur && cellAt(cur.r, cur.c) ? cur : { r: a0.r, c: a0.c };
    const here = cellAt(p.r, p.c) || a0;
    let r = p.r, c = p.c;
    if (!s.length) { r = here.r; c = here.c; }
    else if (mv) {
      if (ctrl) { if (mv[0]) r = mv[0] > 0 ? grid.rows - 1 : 0; if (mv[1]) c = mv[1] > 0 ? grid.cols - 1 : 0; }
      else {
        if (mv[0] > 0) r = here.r + here.rs; else if (mv[0] < 0) r = here.r - 1;
        if (mv[1] > 0) c = here.c + here.cs; else if (mv[1] < 0) c = here.c - 1;
      }
    } else if (e.key === 'Home') { c = 0; if (ctrl) r = 0; } else { c = grid.cols - 1; if (ctrl) r = grid.rows - 1; }
    r = Math.max(0, Math.min(grid.rows - 1, r));
    c = Math.max(0, Math.min(grid.cols - 1, c));
    let n = cellAt(r, c);
    // a hole (rows of different widths): the nearest cell back towards where we were
    while (!n && mv && (r !== here.r || c !== here.c)) { r -= Math.sign(r - here.r); c -= Math.sign(c - here.c); n = cellAt(r, c); }
    if (!n) return;
    cur = { r, c };
    if (e.shiftKey) pickBlock(grid.cells.find((x) => x.id === anchor) || here, n);
    else { sel = new Set([n.id]); anchor = n.id; }
    render(true);
    const td = $('tbGrid').querySelector(`td[data-id="${CSS.escape(n.id)}"]`);
    if (td) { td.focus({ preventScroll: true }); td.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }
  }

  // ------------------------------------------------------------ reading order: check & adjust (V4.21.4)
  const roList = () => grid.cells.slice().sort((a, b) => a.order - b.order);
  const canReorder = () => !grid.cells.some((c) => c.rs > 1 || c.cs > 1) && grid.cells.length === grid.rows * grid.cols;
  const cname = (c) => `${colName(c.c)}${c.r + 1}`;
  function roOpen() {
    if (!grid || !grid.cells.length) return;
    const s = selected();
    ro = { mode: 'check', i: s.length === 1 ? s[0].order - 1 : 0, seq: [], timer: null };
    $('tbShowOrder').checked = true;
    $('tbShowCells').checked = true;
    $('tbRO').classList.remove('hidden');
    if (!$('roRate')) $('roSpeak').closest('label').after(UA.rateControl('roRate'));
    $('tbReadOrder').classList.add('on');
    render(true);
    roGo(ro.i);
  }
  function roQuit() {
    if (!ro) return;
    roStop();
    ro = null;
    UA.hush();
    $('tbRO').classList.add('hidden');
    $('tbReadOrder').classList.remove('on');
  }
  function roClose() {
    if (!ro) return;
    roQuit();
    if (grid) render(true);
  }
  function roMode(m) {
    if (!ro) return;
    if (m === 'click' && !canReorder()) { UA.toast('Merged cells (or rows of different widths): split them or use “Tidy the table” first', 4500); return; }
    roStop();
    ro.mode = m;
    ro.seq = [];
    if (m === 'click') { sel = new Set(); render(true); } else roGo(ro.i);
    roMark();
    roShow();
  }
  function roStop() {
    if (ro) ro.timer = null;
    UA.hush();
    if ($('roPlay')) $('roPlay').textContent = '▶ Play';
  }
  const roWords = (c) => `${c.role === 'TH' ? 'Header. ' : ''}${c.text || (c.kids ? c.kid_list.map((k) => k.text).join(' ') : '') || 'blank'}`;
  // play: each cell is said to the end (at the chosen speed), then the next one
  async function roPlay() {
    if (!ro) return;
    if (ro.mode !== 'check') roMode('check');
    if (ro.timer) { roStop(); return; }
    if (ro.i >= roList().length - 1) roGo(0, true);
    const my = {};
    ro.timer = my;
    $('roPlay').textContent = '⏸ Pause';
    while (ro && ro.timer === my) {
      const c = roList()[ro.i];
      const rate = UA.speechRate();
      const ok = await UA.say(roWords(c), { rate, silent: !$('roSpeak').checked });
      if (!ro || ro.timer !== my || !ok) return;
      await new Promise((r) => setTimeout(r, 250 / rate));
      if (!ro || ro.timer !== my) return;
      if (ro.i >= roList().length - 1) { roStop(); return; }
      roGo(ro.i + 1, true);
    }
  }
  function roGo(i, quiet) {
    if (!ro) return;
    const L = roList();
    if (!L.length) return;
    ro.i = Math.max(0, Math.min(L.length - 1, i));
    const c = L[ro.i];
    sel = new Set([c.id]);
    anchor = c.id;
    cur = { r: c.r, c: c.c };
    if (c.rect) {
      const p = parts().find((x) => x.page === c.page && x.part.cells.includes(c.id));
      if (p && p.key !== partKey) { partKey = p.key; renderImage(); }
    }
    $('tbGrid').querySelectorAll('td[data-id]').forEach((d) => d.classList.toggle('sel', sel.has(d.dataset.id)));
    markImage();
    tools();
    roMark();
    roShow();
    const td = $('tbGrid').querySelector(`td[data-id="${CSS.escape(c.id)}"]`);
    if (td) td.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    keepKeys(td);
    const cc = $('tbImgOv').querySelector(`.ccell[data-id="${CSS.escape(c.id)}"]`);
    if (cc) cc.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    if ($('roSpeak').checked && !quiet) UA.say(roWords(c));
  }
  // the dialog keeps the keys when a cell on the picture (not focusable) was clicked
  function keepKeys(td) {
    const ae = document.activeElement;
    if (td && (!ae || ae === document.body || !dlg.contains(ae))) td.focus({ preventScroll: true });
  }
  function roPick(cell) {
    keepKeys($('tbGrid').querySelector(`td[data-id="${CSS.escape(cell.id)}"]`));
    const k = ro.seq.indexOf(cell.id);
    if (k >= 0) ro.seq.splice(k, 1); else ro.seq.push(cell.id);
    roMark();
    roShow();
  }
  // the numbers and colours on the grid and the picture
  function roMark() {
    if (!grid) return;
    const curId = ro && ro.mode === 'check' ? (roList()[ro.i] || {}).id : null;
    const off = new Set(ro && ro.mode === 'check' ? grid.order_off || [] : []);
    const byId = new Map(grid.cells.map((c) => [c.id, c]));
    if (ro) ro.seq = ro.seq.filter((id) => byId.has(id));
    const picked = new Map(ro && ro.mode === 'click' ? ro.seq.map((id, k) => [id, k + 1]) : []);
    const clicking = !!ro && ro.mode === 'click';
    [...$('tbGrid').querySelectorAll('td[data-id]'), ...$('tbImgOv').querySelectorAll('.ccell')].forEach((d) => {
      const id = d.dataset.id;
      d.classList.toggle('rocur', id === curId);
      d.classList.toggle('rooff', off.has(id));
      d.classList.toggle('ropick', picked.has(id));
      d.classList.toggle('rowait', clicking && !picked.has(id));
      if (clicking) d.classList.remove('sel');
      const b = d.querySelector('.cord');
      if (b) b.textContent = clicking ? (picked.has(id) ? String(picked.get(id)) : '') : String((byId.get(id) || {}).order || '');
    });
    const t = $('tbGrid').querySelector('table');
    if (t) t.classList.toggle('romode', !!ro);
    $('tbImgOv').classList.toggle('romode', !!ro);
  }
  function roShow() {
    if (!ro || !grid) return;
    const L = roList();
    if (!L.length) return;
    ro.i = Math.max(0, Math.min(L.length - 1, ro.i));
    const off = grid.order_off || [];
    const printed = grid.printed_order || [];
    const can = canReorder();
    const stat = $('roStat');
    if (!printed.length) { stat.className = 'rostat'; stat.textContent = 'printed order not known — a cell has no zone on the page'; }
    else if (off.length) { stat.className = 'rostat warn'; stat.textContent = `⚠ ${off.length} of ${L.length} cell(s) read out of their printed place`; }
    else { stat.className = 'rostat ok'; stat.textContent = `✓ all ${L.length} cells read as printed`; }
    $('roMode').querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b.dataset.m === ro.mode);
      if (b.dataset.m === 'click') { b.disabled = !can; b.title = can ? 'Click the cells one after another in the order they should be read' : 'Merged cells or rows of different widths — split them or use Tidy first'; }
    });
    $('roCheck').classList.toggle('hidden', ro.mode !== 'check');
    $('roClick').classList.toggle('hidden', ro.mode !== 'click');
    if (ro.mode === 'check') {
      const c = L[ro.i];
      $('roStep').textContent = `${ro.i + 1} / ${L.length}`;
      $('roFirst').disabled = $('roPrev').disabled = ro.i === 0;
      $('roNext').disabled = $('roLast').disabled = ro.i >= L.length - 1;
      const pi = printed.length ? printed.indexOf(c.id) : -1;
      $('roCur').replaceChildren(
        el('span', { class: 'ordn2', text: String(c.order) }),
        el('b', { text: cname(c) }),
        el('span', { class: 'role', text: c.role + (c.scope ? ' · ' + c.scope : '') }),
        el('span', { class: 'ctext', text: c.text || (c.kids ? '(tags inside)' : '(empty)') }),
        pi < 0 ? null : el('span', { class: 'roflag ' + (pi === ro.i ? 'ok' : 'warn'), text: pi === ro.i ? '✓ as printed' : `⚠ printed as #${pi + 1}` }));
      $('roPrinted').disabled = !can || !off.length;
      $('roPrinted').title = !can ? 'Merged cells or rows of different widths — split them or use Tidy first' : off.length ? 'Read the cells as they are printed: by rows on the page, left to right' : 'The cells are read as printed already';
      $('roOffNext').disabled = !off.length;
      $('roHint').textContent = 'Arrows or ◀ ▶ walk through the cells in the order a screen reader reads them · click a cell to jump to it · Space plays · Esc closes. To change the order: “Order as printed”, “Set by clicking”, or move rows (↑ ↓) and columns (← →).';
    } else {
      const n = ro.seq.length;
      $('roPicked').replaceChildren(el('b', { text: `${n} of ${L.length}` }), document.createTextNode(n < L.length ? ` cells clicked — click the cell to be read as number ${n + 1}` : ' cells clicked — Apply'));
      $('roBack').disabled = $('roClear').disabled = $('roApply').disabled = !n;
      $('roHint').textContent = 'Click the cells (here or on the picture) in the order they should be read. Click a numbered cell again to take it out. Cells not clicked are read after them, in their present order. Backspace takes back the last click, Enter applies.';
    }
  }
  async function roApply() {
    if (!ro || !ro.seq.length) return;
    const order = ro.seq.slice();
    const ok = await op({ op: 'reorder_cells', order }, (r) => `Reading order set: ${(r.touched || []).length} cell(s) moved`);
    if (ok && ro) { ro.seq = []; ro.mode = 'check'; roGo(0); }
  }
  on('tbReadOrder', () => (ro ? roClose() : roOpen()));
  on('roClose', roClose);
  $('roMode').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => roMode(b.dataset.m)));
  on('roFirst', () => { roStop(); roGo(0); });
  on('roPrev', () => { roStop(); roGo(ro.i - 1); });
  on('roNext', () => { roStop(); roGo(ro.i + 1); });
  on('roLast', () => { roStop(); roGo(roList().length - 1); });
  on('roPlay', roPlay);
  on('roOffNext', () => {
    const L = roList(), off = new Set(grid.order_off || []);
    for (let k = 1; k <= L.length; k++) { const j = (ro.i + k) % L.length; if (off.has(L[j].id)) { roStop(); roGo(j); return; } }
  });
  on('roPrinted', async () => {
    const ok = await op({ op: 'cells_as_printed' }, (r) => (r.touched || []).length ? `${r.touched.length} cell(s) moved — the cells are read as printed` : 'The cells are read as printed already');
    if (ok && ro) roGo(ro.i);
  });
  on('roBack', () => { if (ro && ro.seq.length) { ro.seq.pop(); roMark(); roShow(); } });
  on('roClear', () => { if (ro) { ro.seq = []; roMark(); roShow(); } });
  on('roApply', roApply);
  dlg.addEventListener('cancel', (e) => { if (ro) { e.preventDefault(); roClose(); } });
  dlg.addEventListener('close', () => roQuit());
  // the keys of the reading order check; true when the key was used
  function roKey(e) {
    if (!ro) return false;
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    if (ro.mode === 'check' && plain && !e.shiftKey) {
      const st = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1, PageDown: 5, PageUp: -5 }[e.key];
      if (st) { e.preventDefault(); roStop(); roGo(ro.i + st); return true; }
      if (e.key === 'Home') { e.preventDefault(); roStop(); roGo(0); return true; }
      if (e.key === 'End') { e.preventDefault(); roStop(); roGo(roList().length - 1); return true; }
      if (e.key === ' ') { e.preventDefault(); roPlay(); return true; }
    }
    if (ro.mode === 'click' && plain) {
      if (e.key === 'Backspace') { e.preventDefault(); $('roBack').click(); return true; }
      if (e.key === 'Enter') { e.preventDefault(); roApply(); return true; }
      if (e.key.startsWith('Arrow') || e.key === ' ') { e.preventDefault(); return true; }
    }
    return false;
  }

  // ------------------------------------------------------------ templates
  let templates = { table: [], list: [] };
  async function loadTemplates() {
    templates = await UA.get('/api/templates');
    const fill = (sel2, list) => {
      const v = sel2.value;
      sel2.replaceChildren(...(list.length ? list.map((t) => el('option', { value: t.number, text: `#${t.number}${t.name ? ' ' + t.name : ''} — ${t.summary}`.slice(0, 90) }))
        : [el('option', { value: '', text: 'no template yet' })]));
      if (v) sel2.value = v;
    };
    fill($('tbTplPick'), templates.table);
    if ($('lsTplPick')) fill($('lsTplPick'), templates.list);
    return templates;
  }
  on('tbTplSave', async () => {
    const r = await UA.post('/api/templates', { kind: 'table', job: ED.JID, id: tid, number: $('tbTplNum').value, name: $('tbTplName').value });
    if (r.error) { UA.toast(r.error, 4500); return; }
    UA.toast(`Saved as table template #${r.number}: ${r.summary}`, 4500);
    $('tbTplNum').value = '';
    await loadTemplates();
    $('tbTplPick').value = String(r.number);
  });
  on('tbTplApply', () => {
    const n = $('tbTplPick').value;
    if (!n) { UA.toast('Save a template first'); return; }
    if (!confirm(`Give this table the structure of template #${n}? Its rows and cells are made anew (Undo brings them back).`)) return;
    applyTemplate('table', n, { id: tid }).then((r) => { if (r && !r.error) { fetchGrid().then(() => render()); } });
  });
  on('tbTplMode', () => {
    const n = $('tbTplPick').value;
    if (!n) { UA.toast('Save a template first'); return; }
    dlg.close();
    startTpl('table', n);
  });
  async function applyTemplate(kind, number, where, mark) {
    const r = await UA.post(ED.api('/templates/apply'), { kind, number, mark: !!mark, ...where });
    if (r.error) { UA.toast(r.error, 5000); return r; }
    if (tplMode) (tplMode.done = tplMode.done || []).push(r.id);
    await ED.load(r.id || ED.focus);
    const what = mark ? `#${number}` : `Template #${number}`;
    UA.toast(kind === 'table' ? `${what}: ${r.rows} rows × ${r.cols} columns${r.repeated_headers ? ` · ${r.repeated_headers} repeated header row(s) made artifacts` : ''}`
      : `${what}: the list is tagged the same way`);
    return r;
  }
  function startTpl(kind, number, mark) {
    stop();
    if (window.LST) window.LST.stop();
    tplMode = { kind, number, mark: mark || null, done: [] };
    const name = kind === 'table' ? 'table' : 'list';
    if (mark) {
      $('tplTitle').textContent = `${name === 'table' ? 'Table' : 'List'} #${number} marked`;
      $('tplHint').textContent = `Click the next ${name}s (any page — [ ] to turn) to tag them the same way · drag a box over an untagged ${name} · Esc lets the mark go`;
    } else {
      $('tplTitle').textContent = `${name === 'table' ? 'Table' : 'List'} template #${number}`;
      $('tplHint').textContent = `Click a ${name} on the page to give it the template — or drag a box to make a new ${name} there. Turn pages with [ ].`;
    }
    $('tplBar').classList.add('show');
    $('tplBar').classList.toggle('markbar', !!mark);
    ED.drawBoxes();
  }
  function stopTpl() {
    if (!tplMode) return;
    const m = tplMode;
    tplMode = null;
    $('tplBar').classList.remove('show', 'markbar');
    if (m.mark) {
      // Esc: the mark is let go
      fetch(ED.api('/marks'), { method: 'DELETE', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ number: m.number }) }).catch(() => {});
      UA.toast(`Mark #${m.number} released`);
    }
    ED.drawBoxes();
  }

  // ---- Ctrl+N (or Alt+N): mark the selected table / list #N, then click the
  // next ones to tag them the same way; Esc releases the mark (V4.19.1)
  let digits = '', digitTimer = null;
  async function markOrArm(n) {
    const model = ED.model;
    const f = ED.focus && model.nodes[ED.focus];
    const inside = (id, role) => { let c = model.nodes[id]; while (c) { if (c.role === role) return c; c = model.nodes[c.parent]; } return null; };
    const target = f && (inside(f.id, 'Table') || inside(f.id, 'L'));
    if (target && !(tplMode && tplMode.mark && tplMode.number === n && tplMode.mark.source === target.id)) {
      const r = await UA.post(ED.api('/marks'), { id: target.id, number: n });
      if (r.error) { UA.toast(r.error, 4500); return; }
      if (tplMode && tplMode.mark && tplMode.number !== n) { tplMode = null; }
      startTpl(r.kind, n, { source: r.source, page: r.page, summary: r.summary });
      UA.toast(`${r.kind === 'table' ? 'Table' : 'List'} marked #${n} — ${r.summary}. Click the next ${r.kind}s.`, 4500);
      return;
    }
    // nothing selected: take up a mark made before
    const marks = await UA.get(ED.api('/marks'));
    const kind = (marks.table || {})[n] ? 'table' : (marks.list || {})[n] ? 'list' : null;
    if (!kind) { UA.toast(`Select a table or a list (or a cell / an item in it), then press Ctrl+${n} (or Alt+${n})`, 4500); return; }
    const m = marks[kind][n];
    startTpl(kind, n, { source: m.source, page: m.page, summary: m.summary });
    UA.toast(`#${n} again — click the next ${kind}s`);
  }
  document.addEventListener('keydown', (e) => {
    if (ED.stage !== 'tags' || document.querySelector('dialog[open]')) return;
    const m = /^(?:Digit|Numpad)(\d)$/.exec(e.code || '');
    if (!m || !(e.ctrlKey || e.altKey || e.metaKey)) return;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target.tagName || '').toUpperCase())) return;
    e.preventDefault();
    e.stopPropagation();
    digits += m[1];
    clearTimeout(digitTimer);
    // Ctrl+1 then Ctrl+2 quickly is #12
    digitTimer = setTimeout(() => { const n = parseInt(digits, 10); digits = ''; if (n > 0) markOrArm(n); }, 450);
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && tplMode && !document.querySelector('dialog[open]')) { e.preventDefault(); stopTpl(); }
  }, true);
  // a table or list clicked in the tag tree while a mark is on
  function treePick(id, e) {
    if (!tplMode || !e || e.type !== 'click') return false;
    const want = tplMode.kind === 'table' ? 'Table' : 'L';
    let c = ED.model.nodes[id];
    let hit = null;
    while (c) { if (c.role === want) hit = c; if (hit && want === 'Table') break; c = ED.model.nodes[c.parent]; }
    if (!hit) return false;
    if (tplMode.mark && hit.id === tplMode.mark.source) { UA.toast('That is the marked one itself'); return true; }
    applyTemplate(tplMode.kind, tplMode.number, { id: hit.id }, !!tplMode.mark);
    return true;
  }
  on('tplDone', stopTpl);

  // ------------------------------------------------------------ on the page
  function startAdjust() {
    if (!grid || !parts().length) { UA.toast('This table has no zones on a page'); return; }
    stopTpl();
    if (window.LST) window.LST.stop();
    active = tid;
    dlg.close();
    const pages = parts().map((p) => p.page);
    if (!pages.includes(ED.page)) ED.setPage(pages[0]); else ED.drawBoxes();
    $('tblBar').classList.add('show');
    setZone('lines');
  }
  function stop() {
    if (!active) return;
    active = null;
    $('tblBar').classList.remove('show');
    ED.drawBoxes();
  }
  function setZone(z) {
    zoneTool = z;
    $('tblZoneSeg').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.z === z));
    $('tblHint').textContent = {
      lines: 'drag a line · Shift+click: new row line · Alt+click: new column line · double-click a line: take it out',
      rows: 'drag a box over printed lines: each line becomes a row of the table',
      row1: 'drag a box: it becomes one row of the table',
      col: 'drag a box beside the table: it becomes a column',
      remove: 'drag a box over whole rows (or columns): they leave the table as paragraphs',
    }[z];
    ED.drawBoxes();
  }
  $('tblZoneSeg').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => setZone(b.dataset.z)));
  on('tblDone', stop);
  on('tblReopen', () => { const id = active; stop(); open(id); });

  // a zone drawn on the page (the table's zone tools, or a template)
  let zs = null, zr = null;
  function zoneLayer(overlay, onBox, onClick) {
    const layer = el('div', { class: 'tzone' });
    layer.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.stopPropagation();
      e.preventDefault();
      const r = overlay.getBoundingClientRect();
      zs = { x: e.clientX - r.left, y: e.clientY - r.top, cx: e.clientX, cy: e.clientY, onBox, onClick, overlay };
    });
    overlay.append(layer);
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
    const r = s.overlay.getBoundingClientRect();
    const x = Math.max(0, Math.min(r.width, e.clientX - r.left)), y = Math.max(0, Math.min(r.height, e.clientY - r.top));
    const z = ED.zoom;
    if (zr) {
      zr.remove();
      zr = null;
      const box = [Math.min(x, s.x) / z, Math.min(y, s.y) / z, Math.max(x, s.x) / z, Math.max(y, s.y) / z];
      if (box[2] - box[0] > 3 && box[3] - box[1] > 3) s.onBox(box);
      return;
    }
    if (s.onClick) s.onClick(e);
  });

  function draw(overlay) {
    if (tplMode) {
      overlay.classList.add('tplmode');
      // the marked one, and the ones given its structure, on this page
      const badge = (id, text, cls) => {
        const n = ED.model.nodes[id];
        if (!n) return;
        (n.rects || []).filter((r) => r[0] === ED.page).forEach((r) => {
          const d = el('div', { class: 'markbox ' + cls });
          const z = ED.zoom;
          Object.assign(d.style, { left: r[1] * z + 'px', top: r[2] * z + 'px', width: (r[3] - r[1]) * z + 'px', height: (r[4] - r[2]) * z + 'px' });
          d.append(el('span', { text }));
          overlay.append(d);
        });
      };
      if (tplMode.mark) badge(tplMode.mark.source, `#${tplMode.number}`, 'src');
      (tplMode.done || []).forEach((id) => badge(id, `✓ #${tplMode.number}`, 'done'));
      zoneLayer(overlay, async (box) => {
        const r = await applyTemplate(tplMode.kind, tplMode.number, { page: ED.page, rect: box }, !!tplMode.mark);
        void r;
      }, async (e) => {
        const hit = document.elementsFromPoint(e.clientX, e.clientY).find((d) => d.classList && d.classList.contains('box'));
        if (!hit) { UA.toast(`Click a ${tplMode.kind} (or drag a box)`); return; }
        const n = ED.model.nodes[hit.dataset.id];
        const want = tplMode.kind === 'table' ? 'Table' : 'L';
        let id = n && n.id;
        let cur = n;
        while (cur && cur.role !== want) cur = ED.model.nodes[cur.parent];
        if (cur && want === 'L') {
          // the outermost list
          let up = ED.model.nodes[cur.parent];
          while (up) { if (up.role === 'L') cur = up; up = ED.model.nodes[up.parent]; }
        }
        if (cur) id = cur.id;
        if (!cur) { UA.toast(`That is not inside a ${tplMode.kind} — drag a box to make one`); return; }
        if (tplMode.mark && id === tplMode.mark.source) { UA.toast('That is the marked one itself — click the next one'); return; }
        hit.classList.add('flash');
        await applyTemplate(tplMode.kind, tplMode.number, { id }, !!tplMode.mark);
      });
      return;
    }
    if (!active || !grid) return;
    overlay.classList.add('tbladjust');
    const L = grid.lines[String(ED.page)];
    if (zoneTool !== 'lines') {
      zoneLayer(overlay, (box) => {
        if (zoneTool === 'remove') {
          if (confirm('The rows (or columns) in the zone leave the table as paragraphs?')) op({ op: 'remove_zone', page: ED.page, rect: box }, (r) => `${(r.made || []).length} row(s) out of the table`);
        } else {
          op({ op: 'add_zone', page: ED.page, rect: box, kind: zoneTool === 'col' ? 'col' : 'rows', mode: zoneTool === 'row1' ? 'one' : 'lines' },
            (r) => `${(r.touched || []).length} cell(s) added`);
        }
      }, null);
    }
    if (!L) return;
    (L.parts || []).forEach((part, i) => {
      const lay = el('div', { class: 'tpart' + (zoneTool !== 'lines' ? ' dim' : '') });
      overlay.append(lay);
      Object.assign(lay.style, { position: 'absolute', inset: '0', pointerEvents: zoneTool === 'lines' ? 'none' : 'none' });
      drawPart(lay, ED.page, i, part, ED.zoom, [0, 0], false);
      lay.querySelectorAll('.tline, .tcatch').forEach((x) => { x.style.pointerEvents = zoneTool === 'lines' ? 'auto' : 'none'; });
    });
  }

  window.TBL = {
    open,
    draw,
    stop() { stop(); stopTpl(); },
    get active() { return active || tplMode; },
    loadTemplates,
    applyTemplate,
    startTpl,
    stopTpl,
    treePick,
    get marking() { return !!tplMode; },
  };
})();
