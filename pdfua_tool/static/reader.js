/* Dezquare PDF Accessibility Studio — Reader: an NVDA-style read-through (V4.21.4).
 *
 * The tags read the way a screen reader in browse mode reads them ("heading
 * level 2, …", "list with 10 items", "row 2, Count column 2, 38", "graphic, …",
 * "out of table"), spoken aloud with the browser's voices, the words shown as
 * they are spoken and the tag being read lit on the page. Say all from this
 * page on (into the next pages) or the whole file; step, quick keys like NVDA
 * (H, L, T, G, K, F …), an elements list, the problems a listener would hear
 * (no alt text, empty link, a skipped heading level, an empty heading …), a
 * transcript to save. Without a voice the words still run at reading speed.
 *
 * Uses the editor through window.ED (static/editor.js). */
(function () {
  if (!window.ED || !document.getElementById('tab-reader')) return;
  const ED = window.ED;
  const { $, el } = ED;
  const pane = $('tab-reader');
  const S = window.speechSynthesis || null;

  const pref = (() => { try { return JSON.parse(localStorage.getItem('ua-reader') || '{}'); } catch (e) { return {}; } })();
  const savePref = () => { try { localStorage.setItem('ua-reader', JSON.stringify(pref)); } catch (e) { /* none */ } };

  let items = [];          // what is read, in order
  let cur = 0;             // the item being read
  let playing = false;
  let token = 0;           // a new play cancels the old one
  let built = false;
  let builtFor = '';

  // ------------------------------------------------------------ what is read
  const HEAD = /^H([1-6])$/;
  const txt = (n) => (n.text || '').replace(/\s+/g, ' ').trim();
  const pageOf = (n) => (n.rects && n.rects.length ? n.rects[0][0] : n.page);
  function blankText(n) {
    if (n.actual_text) return n.actual_text;
    return (n.rects || []).length > 1 ? (window.MULTI_BLANK_LINE || 'Multiple Blank Lines') : (window.BLANK_LINE || 'Blank Line');
  }
  // the words of a tag and everything in it, as a screen reader gets them
  // the words of a tag and everything in it, as a screen reader gets them:
  // /ActualText first, then /Alt, then /E (expansion), else the printed words
  // — also for a Span, a Link … inside a paragraph, in their place (V4.22)
  const GROUPS = ['Document', 'Part', 'Art', 'Sect', 'Div', 'NonStruct', 'Private', 'L', 'LI', 'Table', 'TR',
    'THead', 'TBody', 'TFoot', 'TOC', 'TOCI', 'Index', 'BlockQuote'];
  const INLINE_R = ['Span', 'Quote', 'Lbl', 'Reference', 'Code', 'Note', 'Link', 'BibEntry', 'Figure', 'Formula', 'Form', 'Annot', 'Em', 'Strong'];
  function replacement(n) {
    if (n.actual_text) return n.actual_text;
    if (n.blank_line) return blankText(n);
    if (n.role === 'Figure' || n.role === 'Formula') return n.alt || '';
    if (n.alt && !GROUPS.includes(n.role)) return n.alt;
    if (n.expansion) return n.expansion;
    return null;
  }
  function words(id) {
    const n = ED.model.nodes[id];
    if (!n || n.artifact) return '';
    const rep = replacement(n);
    if (rep != null) return rep;
    const own = txt(n);
    if (!n.children.length) return own;
    const kidsN = n.children.map((c) => ED.model.nodes[c]).filter((k) => k && !k.artifact);
    // a paragraph holding a Span, Quote, Link, Figure … inside its line: its
    // own words, with each inner tag's words in its place
    if (own && kidsN.every((k) => INLINE_R.includes(k.role))) {
      let out = '', at = 0;
      kidsN.forEach((k) => {
        const kt = txt(k);
        const r = replacement(k) ?? (k.children.length ? words(k.id) : null);
        const pos = kt ? own.indexOf(kt, at) : -1;
        if (pos >= 0) {
          out += own.slice(at, pos) + (r != null ? r : kt);
          at = pos + kt.length;
        } else if (r) {
          // no printed words of its own (a figure, a drawn line): read here
          out += `${out && !/\s$/.test(out) ? ' ' : ''}${r} `;
        }
      });
      out += own.slice(at);
      return out.replace(/\s+/g, ' ').trim();
    }
    const kids = n.children.map(words).filter(Boolean).join(' ');
    return kids || own;
  }

  /* The tags → what is spoken. Each item: {id, page, kind, role, text, say,
     warn, lang, depth}. `scope`: 'page' (from page p on), 'doc', 'from' (a tag). */
  function build() {
    const M = ED.model;
    const out = [];
    let lastHead = 0;
    let lastPage = -1;
    const verbose = pref.verbosity !== 'text';
    const push = (o) => {
      o.say = o.say != null ? o.say : (verbose && o.ann ? `${o.ann}, ${o.text}` : o.text);
      if (!o.say || !o.say.trim()) return;
      if (pref.pages && o.page >= 0 && o.page !== lastPage && o.kind !== 'exit') {
        out.push({ id: o.id, page: o.page, kind: 'page', role: 'page', text: `page ${o.page + 1}`, say: `page ${o.page + 1}`, depth: 0 });
      }
      if (o.page >= 0 && o.kind !== 'exit') lastPage = o.page;
      out.push(o);
    };
    const walk = (id, depth, nest) => {
      const n = M.nodes[id];
      if (!n || n.artifact) return;
      const r = n.role;
      const p = pageOf(n);
      const base = { id, page: p, role: r, lang: n.lang || '', depth };
      const h = HEAD.exec(r);
      if (h) {
        const lv = +h[1];
        const t = words(id);
        let warn = '';
        if (!t) warn = 'an empty heading — the listener hears only “heading level ' + lv + '”';
        else if (lastHead && lv > lastHead + 1) warn = `heading level ${lastHead} then ${lv} — a level is skipped`;
        else if (!lastHead && lv > 1 && pref.firstH !== false) warn = `the first heading is level ${lv}, not 1`;
        lastHead = lv;
        push({ ...base, kind: 'heading', nav: 'h', ann: `heading level ${lv}`, text: t || '', say: verbose ? `heading level ${lv}${t ? ', ' + t : ''}` : t, warn });
        return;
      }
      if (r === 'Figure' || r === 'Formula') {
        const alt = n.alt || n.actual_text || '';
        push({ ...base, kind: 'graphic', nav: 'g', ann: r === 'Figure' ? 'graphic' : 'math', text: alt,
          say: verbose ? `${r === 'Figure' ? 'graphic' : 'math'}${alt ? ', ' + alt : ''}` : alt || (r === 'Figure' ? 'graphic' : 'math'),
          warn: alt ? (alt.length > 250 ? 'very long alt text — a listener has to hear it all' : /^(image|picture|graphic|figure|photo)\b/i.test(alt) ? 'alt text starts with “image/picture” — the reader says “graphic” already' : '')
            : 'no alt text — the listener hears only “graphic”' });
        return;
      }
      if (r === 'L') {
        const lis = n.children.filter((c) => M.nodes[c] && M.nodes[c].role === 'LI' && !M.nodes[c].artifact);
        push({ ...base, kind: 'enter', nav: 'l', ann: '', text: `list with ${lis.length} item${lis.length === 1 ? '' : 's'}${nest ? `, nesting level ${nest + 1}` : ''}`,
          say: verbose ? `list with ${lis.length} item${lis.length === 1 ? '' : 's'}${nest ? `, nesting level ${nest + 1}` : ''}` : '',
          warn: lis.length ? '' : 'a list with no items' });
        n.children.forEach((c) => walk(c, depth + 1, nest + 1));
        push({ ...base, kind: 'exit', ann: '', text: 'out of list', say: verbose ? 'out of list' : '' });
        return;
      }
      if (r === 'LI') {
        const kids = n.children.map((c) => M.nodes[c]).filter((k) => k && !k.artifact);
        const lbl = kids.filter((k) => k.role === 'Lbl').map((k) => words(k.id)).join(' ');
        const body = [];
        const sub = [];
        kids.filter((k) => k.role !== 'Lbl').forEach((k) => {
          // a list inside the item is read after the item's words
          const inner = (x) => {
            const y = M.nodes[x];
            if (!y || y.artifact) return;
            if (y.role === 'L') { sub.push(x); return; }
            if (!y.children.length) { const w = words(x); if (w) body.push(w); return; }
            if (y.actual_text) { body.push(y.actual_text); return; }
            // a paragraph with a Span … inside it: read once, as a whole
            if (replacement(y) != null || (txt(y) && y.children.every((c) => !M.nodes[c] || INLINE_R.includes(M.nodes[c].role)))) {
              const w = words(x); if (w) body.push(w); return;
            }
            y.children.forEach(inner);
            if (!y.children.some((c) => M.nodes[c])) { const w = words(x); if (w) body.push(w); }
          };
          if (k.role === 'L') sub.push(k.id); else inner(k.id);
        });
        const t = [lbl, body.join(' ')].filter(Boolean).join(' ');
        push({ ...base, kind: 'item', nav: 'i', ann: lbl ? '' : 'bullet', text: t,
          say: verbose && !lbl ? `bullet ${t}` : t, warn: t ? '' : 'an empty list item' });
        sub.forEach((s) => walk(s, depth + 1, nest));
        return;
      }
      if (r === 'Table') {
        const rows = [];
        const collect = (tid) => M.nodes[tid].children.forEach((c) => {
          const k = M.nodes[c];
          if (!k || k.artifact) return;
          if (k.role === 'TR') rows.push(k); else if (['THead', 'TBody', 'TFoot'].includes(k.role)) collect(c);
        });
        collect(id);
        const grid = [];        // [row][col] = cell
        rows.forEach((rw, ri) => {
          grid[ri] = grid[ri] || [];
          let c = 0;
          rw.children.forEach((cid) => {
            const cell = M.nodes[cid];
            if (!cell || !['TD', 'TH'].includes(cell.role)) return;
            while (grid[ri][c]) c++;
            for (let a = 0; a < (cell.rowspan || 1); a++) for (let b = 0; b < (cell.colspan || 1); b++) {
              grid[ri + a] = grid[ri + a] || [];
              grid[ri + a][c + b] = { cell, r0: ri, c0: c };
            }
            c += cell.colspan || 1;
          });
        });
        const cols = Math.max(0, ...grid.map((g) => g.length));
        const caps = n.children.map((c) => M.nodes[c]).filter((k) => k && k.role === 'Caption');
        const thCount = rows.reduce((s, rw) => s + rw.children.filter((c) => (M.nodes[c] || {}).role === 'TH').length, 0);
        push({ ...base, kind: 'enter', nav: 't', ann: '', text: `table with ${rows.length} rows and ${cols} columns`,
          say: verbose ? `table with ${rows.length} rows and ${cols} columns` : '',
          warn: thCount ? '' : 'no header cells (TH) — the listener hears no column or row names' });
        if (n.summary) push({ ...base, kind: 'text', text: n.summary, say: `summary, ${n.summary}` });
        caps.forEach((cp) => push({ id: cp.id, page: pageOf(cp), role: 'Caption', kind: 'text', text: words(cp.id), depth: depth + 1 }));
        const colHead = (c, ri) => {
          for (let k = 0; k < ri; k++) {
            const g = grid[k] && grid[k][c];
            if (g && g.cell.role === 'TH') return words(g.cell.id);
          }
          return '';
        };
        const rowHead = (ri, c) => {
          for (let k = 0; k < c; k++) {
            const g = grid[ri] && grid[ri][k];
            if (g && g.cell.role === 'TH' && (g.cell.scope === 'Row' || g.cell.scope === 'Both')) return words(g.cell.id);
          }
          return '';
        };
        grid.forEach((g, ri) => {
          let lastRowHead = null;
          (g || []).forEach((slot, c) => {
            if (!slot || slot.r0 !== ri || slot.c0 !== c) return;
            const cell = slot.cell;
            const t = words(cell.id) || (window.BLANK_TEXT || 'blank');
            const ch = cell.role === 'TD' ? colHead(c, ri) : '';
            const rh = cell.role === 'TD' ? rowHead(ri, c) : '';
            const parts = [];
            if (verbose) {
              if (c === 0 || c === (g.findIndex((x) => x && x.r0 === ri))) parts.push(`row ${ri + 1}`);
              if (rh && rh !== lastRowHead) parts.push(rh);
              parts.push(`${ch ? ch + ' ' : ''}column ${c + 1}`);
              const span = [(cell.rowspan || 1) > 1 ? `spans ${cell.rowspan} rows` : '', (cell.colspan || 1) > 1 ? `spans ${cell.colspan} columns` : ''].filter(Boolean).join(' ');
              if (span) parts.push(span);
            }
            lastRowHead = rh;
            push({ id: cell.id, page: pageOf(cell), role: cell.role, lang: cell.lang || '', depth: depth + 1, kind: 'cell', nav: 'c',
              ann: parts.join(', '), text: t, say: verbose ? `${parts.join(', ')}, ${t}` : t,
              warn: cell.role === 'TD' && thCount && !ch && !rh && ri > 0 ? 'a data cell with no header to name it' : '' });
          });
        });
        push({ ...base, kind: 'exit', ann: '', text: 'out of table', say: verbose ? 'out of table' : '' });
        return;
      }
      if (r === 'Link' || r === 'Reference' && n.children.some((c) => (M.nodes[c] || {}).role === 'Link')) {
        const t = words(id);
        push({ ...base, kind: 'link', nav: 'k', ann: 'link', text: t, say: verbose ? `link, ${t || 'unlabelled'}` : t,
          warn: t ? (/^(click here|here|more|read more|link)$/i.test(t) ? '“' + t + '” says nothing about where the link goes' : '') : 'a link with no text — the listener hears only “link”' });
        return;
      }
      if (r === 'Form') {
        const t = words(id) || n.alt || '';
        push({ ...base, kind: 'form', nav: 'f', ann: 'form field', text: t, say: verbose ? `form field, ${t || 'unlabelled'}` : t,
          warn: t ? '' : 'a form field with no name' });
        return;
      }
      if (r === 'Annot') {
        const t = words(id) || n.alt || '';
        push({ ...base, kind: 'text', ann: 'annotation', text: t, say: verbose ? `annotation, ${t}` : t });
        return;
      }
      if (r === 'BlockQuote' || r === 'TOC' || r === 'Index' || r === 'Note' && n.children.length) {
        const name = { BlockQuote: 'block quote', TOC: 'table of contents', Index: 'index', Note: 'note' }[r];
        if (verbose) push({ ...base, kind: 'enter', nav: 'q', text: name, say: name });
        n.children.forEach((c) => walk(c, depth + 1, nest));
        if (verbose) push({ ...base, kind: 'exit', text: `out of ${name}`, say: `out of ${name}` });
        return;
      }
      if (n.blank_line) { push({ ...base, kind: 'text', text: blankText(n) }); return; }
      if (n.actual_text && !['Sect', 'Div', 'Part', 'Art', 'Document', 'NonStruct'].includes(r)) {
        push({ ...base, kind: 'text', text: n.actual_text, ann: '' });
        return;
      }
      const inlineOnly = txt(n) && n.children.every((c) => { const k = M.nodes[c]; return !k || k.artifact || INLINE_R.includes(k.role); });
      const container = !inlineOnly && (!txt(n) || n.children.some((c) => { const k = M.nodes[c]; return k && (k.children.length || ['P', 'L', 'Table', 'Figure'].includes(k.role) || HEAD.test(k.role)); }));
      if (!container || !n.children.length) {
        const t = words(id);
        if (t) push({ ...base, kind: 'text', nav: 'p', ann: r === 'Caption' ? '' : '', text: t,
          warn: /\b\w\s\w\s\w\s\w\b/.test(t) && /^(?:\w\s){4,}/.test(t) ? 'letters spaced out — read one by one' : '' });
        else if (['P', 'Span', 'Caption'].includes(r) && !n.children.length) out.push({ ...base, kind: 'text', text: '', say: '', warn: 'an empty tag — nothing is read' });
        return;
      }
      n.children.forEach((c) => walk(c, depth, nest));
    };
    const scope = $('rdScope').value;
    if (scope === 'from' && ED.focus && M.nodes[ED.focus]) {
      // from the selected tag (or the first thing read inside it) to the end
      M.roots.forEach((r) => walk(r, 0, 0));
      const inside = new Set([ED.focus]);
      const add = (id) => (M.nodes[id] ? M.nodes[id].children : []).forEach((c) => { inside.add(c); add(c); });
      add(ED.focus);
      let idx = out.findIndex((o) => inside.has(o.id));
      if (idx < 0) idx = out.findIndex((o) => ancestorsOf(ED.focus).includes(o.id));
      items = idx >= 0 ? out.slice(idx) : out;
    } else {
      M.roots.forEach((r) => walk(r, 0, 0));
      items = out;
    }
    built = true;
    builtFor = scope;
    cur = 0;
    if (scope === 'page') {
      const k = items.findIndex((o) => o.page >= ED.page && o.kind !== 'exit');
      cur = k >= 0 ? k : 0;
    }
    render();
  }
  function ancestorsOf(id) {
    const out = [];
    let c = ED.model.nodes[id];
    while (c && c.parent) { out.push(c.parent); c = ED.model.nodes[c.parent]; }
    return out;
  }

  // ------------------------------------------------------------ voice and sounds
  let voices = [];
  function loadVoices() {
    if (!S) { $('rdVoice').replaceChildren(el('option', { value: '', text: 'no voice in this browser — words only' })); return; }
    // V4.26 privacy: only the voices on this computer (an online voice sends the words away)
    const all = UA.localVoices();
    voices = all.filter((v) => /^en|^ta|^hi/i.test(v.lang) || voices.length < 1);
    const sel = $('rdVoice');
    sel.replaceChildren(el('option', { value: '', text: all.length ? 'default voice (on this computer)' : 'no voice on this computer — words only' }),
      ...all.map((v) => el('option', { value: v.name, text: `${v.name} (${v.lang})` })));
    if (pref.voice && all.some((v) => v.name === pref.voice)) sel.value = pref.voice;
  }

  let actx = null;
  function tone(kind) {
    if (!$('rdSounds').checked) return;
    const notes = { heading: [880, 0.07], enter: [520, 0.06, 780], exit: [780, 0.06, 520], graphic: [1320, 0.04],
      link: [440, 0.05], warn: [180, 0.14], page: [660, 0.05, 660], form: [990, 0.05] }[kind];
    if (!notes) return;
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      const t0 = actx.currentTime;
      const play = (f, at, d) => {
        const o = actx.createOscillator(), g = actx.createGain();
        o.type = kind === 'warn' ? 'square' : 'sine';
        o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, at);
        g.gain.exponentialRampToValueAtTime(kind === 'warn' ? 0.05 : 0.09, at + 0.01);
        g.gain.exponentialRampToValueAtTime(0.0001, at + d);
        o.connect(g); g.connect(actx.destination);
        o.start(at); o.stop(at + d + 0.02);
      };
      play(notes[0], t0, notes[1]);
      if (notes[2]) play(notes[2], t0 + notes[1] + 0.02, notes[1]);
    } catch (e) { /* no audio */ }
  }

  /* Say one item: words shown as they are spoken (the voice's word events,
     or the reading speed when there is no voice). Resolves when done. */
  function say(it, my) {
    return new Promise((done) => {
      const text = it.say || '';
      const spans = showSpeech(it);
      const rate = parseFloat($('rdRate').value) || 1;
      const mark = (ci) => {
        let k = spans.findIndex((s) => ci >= s.a && ci < s.b);
        if (k < 0) k = spans.findIndex((s) => s.a >= ci);
        spans.forEach((s, j) => {
          [s.el, s.el2].forEach((x) => { if (x) { x.classList.toggle('now', j === k); x.classList.toggle('said', j < k); } });
        });
        // the word being said stays in sight in the speech box
        const w = spans[k] && spans[k].el;
        const bx = $('rdSpeech');
        if (w && bx) {
          const top = w.offsetTop, bot = top + w.offsetHeight;
          if (bot > bx.scrollTop + bx.clientHeight - 8 || top < bx.scrollTop) bx.scrollTop = Math.max(0, top - bx.clientHeight / 3);
        }
      };
      // no voice (or a muted page): run the words at reading speed
      const timed = () => {
        const per = 60000 / (175 * rate);
        let j = 0;
        const step = () => {
          if (my !== token) return done(false);
          if (j >= spans.length) { spans.forEach((s) => [s.el, s.el2].forEach((x) => { if (x) { x.classList.remove('now'); x.classList.add('said'); } })); return setTimeout(() => done(true), 120 / rate); }
          mark(spans[j].a);
          j++;
          setTimeout(step, per * Math.max(0.6, Math.min(2.2, spans[j - 1].w.length / 5)));
        };
        step();
      };
      if (!S || !$('rdVoiceOn').checked || !text.trim() || !UA.localVoices().length) { timed(); return; }
      // a long paragraph in parts (a browser voice stops after ~15 s)
      const chunks = [];
      const re = /[^.!?;:]+[.!?;:]*\s*/g;
      let m, acc = '', start = 0, at = 0;
      while ((m = re.exec(text))) {
        if (acc && acc.length + m[0].length > 220) { chunks.push([start, acc]); start = at; acc = ''; }
        acc += m[0];
        at += m[0].length;
      }
      if (acc) chunks.push([start, acc]);
      if (!chunks.length) chunks.push([0, text]);
      let ci = 0;
      let started = false;
      const next = () => {
        if (my !== token) return done(false);
        if (ci >= chunks.length) { spans.forEach((s) => [s.el, s.el2].forEach((x) => { if (x) { x.classList.remove('now'); x.classList.add('said'); } })); return done(true); }
        const [off, part] = chunks[ci++];
        const u = new SpeechSynthesisUtterance(part);
        const wantLang = (it.lang && $('rdLangs').checked) ? it.lang : (ED.model.lang || 'en-US');
        const v = UA.localVoices().find((x) => x.name === $('rdVoice').value) || UA.localVoice(wantLang);
        if (v) u.voice = v;
        u.lang = (it.lang && $('rdLangs').checked) ? it.lang : (v ? v.lang : (ED.model.lang || 'en-US'));
        u.rate = rate;
        u.pitch = it.kind === 'heading' && $('rdPitch').checked ? 1.15 : 1;
        u.onstart = () => { started = true; mark(off); };
        u.onboundary = (e) => { if (e.name === 'word' || e.charIndex != null) mark(off + e.charIndex); };
        u.onend = () => next();
        u.onerror = () => next();
        S.speak(u);
      };
      next();
      // a voice that never starts (blocked audio): fall back to the words alone
      setTimeout(() => { if (!started && my === token) { S.cancel(); timed(); } }, 2500);
    });
  }

  // ------------------------------------------------------------ playing
  async function play(from) {
    if (!built) build();
    if (!items.length) { UA.toast('Nothing is read here'); return; }
    if (from != null) cur = Math.max(0, Math.min(items.length - 1, from));
    const my = ++token;
    playing = true;
    if (S) S.cancel();
    paintButtons();
    while (playing && my === token && cur < items.length) {
      const it = items[cur];
      focusItem(it);
      if (it.warn) tone('warn');
      tone(it.kind === 'heading' ? 'heading' : it.kind === 'enter' ? 'enter' : it.kind === 'exit' ? 'exit'
        : it.kind === 'graphic' ? 'graphic' : it.kind === 'link' ? 'link' : it.kind === 'page' ? 'page' : it.kind === 'form' ? 'form' : '');
      const ok = await say(it, my);
      if (!ok || my !== token) return;
      if (cur >= items.length - 1) break;
      if (builtFor === 'page' && !$('rdOn').checked && items[cur + 1].page > ED.page) break;
      cur++;
    }
    if (my === token) {
      playing = false;
      paintButtons();
      if (cur >= items.length - 1) { setStatus('End of the document — everything was read.'); tone('page'); }
    }
  }
  function pause() {
    token++;
    playing = false;
    if (S) S.cancel();
    paintButtons();
  }
  function stop() {
    pause();
    clearPage();
  }
  function step(d, speak) {
    if (!built) build();
    if (!items.length) return;
    pause();
    cur = Math.max(0, Math.min(items.length - 1, cur + d));
    const it = items[cur];
    focusItem(it);
    if (speak !== false) { const my = ++token; say(it, my); }
  }
  // NVDA quick keys: the next heading (H), list (L), table (T), graphic (G), link (K), form field (F), item (I), paragraph (P)
  function jump(nav, back, level) {
    if (!built) build();
    const ok = (o) => o.nav === nav && (!level || o.role === 'H' + level);
    let k = -1;
    if (back) { for (let j = cur - 1; j >= 0; j--) if (ok(items[j])) { k = j; break; } }
    else { for (let j = cur + 1; j < items.length; j++) if (ok(items[j])) { k = j; break; } }
    if (k < 0) {
      const name = { h: 'heading', l: 'list', t: 'table', g: 'graphic', k: 'link', f: 'form field', i: 'list item', p: 'paragraph', w: 'problem' }[nav] || 'element';
      tone('warn');
      setStatus(`no ${back ? 'previous' : 'next'} ${level ? 'heading level ' + level : name}`);
      if ($('rdVoiceOn').checked && S) { S.cancel(); S.speak(new SpeechSynthesisUtterance(`no ${back ? 'previous' : 'next'} ${name}`)); }
      return;
    }
    pause();
    cur = k;
    const my = ++token;
    focusItem(items[k]);
    say(items[k], my);
  }
  function nextProblem(back) {
    if (!built) build();
    const n = items.length;
    for (let j = 1; j <= n; j++) {
      const k = (cur + (back ? -j : j) + n) % n;
      if (items[k].warn) { pause(); cur = k; focusItem(items[k]); const my = ++token; say(items[k], my); return; }
    }
    setStatus('No problem a listener would hear 👍');
  }

  // ------------------------------------------------------------ on screen
  function showSpeech(it) {
    const box = $('rdSpeech');
    const text = it.say || '';
    const spans = [];
    const caps = $('rdCaps') && $('rdCaps').checked && pane.offsetParent;
    const frag = document.createDocumentFragment();
    const frag2 = document.createDocumentFragment();
    const re = /\S+/g;
    let m, last = 0;
    while ((m = re.exec(text))) {
      if (m.index > last) { frag.append(document.createTextNode(text.slice(last, m.index))); frag2.append(document.createTextNode(' ')); }
      const s1 = el('span', { class: 'w', text: m[0] });
      const s2 = caps ? el('span', { class: 'w', text: m[0] }) : null;
      spans.push({ a: m.index, b: m.index + m[0].length, el: s1, el2: s2, w: m[0] });
      frag.append(s1);
      if (s2) frag2.append(s2);
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.append(document.createTextNode(text.slice(last)));
    const size = text.length > 320 ? ' xl' : text.length > 160 ? ' lg' : '';
    const nxt = items[cur + 1];
    box.replaceChildren(
      el('div', { class: 'rdprog' }, el('i', { style: `width:${items.length ? ((cur + 1) / items.length) * 100 : 0}%` })),
      el('div', { class: 'rdmeta' }, el('span', { class: 'rdrole ' + (it.kind || ''), text: it.role === 'page' ? 'page' : it.role }),
        it.page >= 0 ? el('span', { class: 'tiny muted', text: `p${it.page + 1}` }) : '',
        it.lang ? el('span', { class: 'tiny muted', text: `[${it.lang}]` }) : '',
        el('span', { class: 'spacer' }),
        el('span', { class: 'tiny muted', text: `${cur + 1} / ${items.length}` })),
      el('div', { class: 'rdtext' + size + (text ? '' : ' empty') }, text ? frag : '(nothing is read)'),
      it.warn ? el('div', { class: 'rdwarn', text: '⚠ ' + it.warn }) : '',
      nxt ? el('div', { class: 'rdnext', title: 'Read next' }, el('span', { class: 'tiny', text: 'next ›' }),
        el('span', { class: 'rdnexttxt', text: (nxt.say || '').slice(0, 140) })) : '');
    box.scrollTop = 0;
    // the words, big, at the foot of the page too (captions)
    let cap = document.getElementById('rdCap');
    if (caps) {
      if (!cap) { cap = el('div', { id: 'rdCap', class: 'rdcap', 'aria-hidden': 'true' }); document.body.append(cap); }
      cap.replaceChildren(el('span', { class: 'rdrole ' + (it.kind || ''), text: it.role === 'page' ? 'page' : it.role }),
        el('span', { class: 'captext' }, text ? frag2 : '…'));
      cap.classList.toggle('show', playing || !!token && false);
      cap.classList.toggle('warn', !!it.warn);
    } else if (cap) cap.classList.remove('show');
    return spans;
  }
  function focusItem(it) {
    $('rdList').querySelectorAll('.rditem.cur').forEach((x) => x.classList.remove('cur'));
    const row = $('rdList').querySelector(`.rditem[data-k="${cur}"]`);
    if (row) { row.classList.add('cur'); row.scrollIntoView({ block: 'nearest' }); }
    setStatus('');
    if ($('rdFollow').checked && it.page >= 0 && it.page !== ED.page) ED.setPage(it.page);
    drawHighlight();
  }
  function setStatus(t) { $('rdStatus').textContent = t; }
  function paintButtons() {
    $('rdPlay').textContent = playing ? '⏸ Pause' : '▶ Play';
    $('rdPlay').classList.toggle('on', playing);
    pane.classList.toggle('reading', playing);
    const cap = document.getElementById('rdCap');
    if (cap) cap.classList.toggle('show', playing && $('rdCaps').checked);
  }
  const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
  function render() {
    const list = $('rdList');
    const only = $('rdOnlyWarn').checked;
    const frag = document.createDocumentFragment();
    items.forEach((it, k) => {
      if (only && !it.warn) return;
      const row = el('div', { class: 'rditem k-' + it.kind + (it.warn ? ' warn' : '') + (k === cur ? ' cur' : ''), 'data-k': k,
        style: `--d:${Math.min(6, it.depth || 0)}`, title: it.warn ? '⚠ ' + it.warn : 'Click: read from here' },
      el('span', { class: 'rdrole ' + (it.kind || ''), text: it.kind === 'page' ? '—' : it.role }),
      el('span', { class: 'rdsay', text: it.say || '(nothing)' }),
      it.warn ? el('span', { class: 'rdw', text: '⚠' }) : null);
      row.addEventListener('click', () => { cur = k; play(k); });
      row.addEventListener('dblclick', () => { pause(); ED.selectTag(it.id); });
      frag.append(row);
    });
    list.replaceChildren(frag);
    const words_ = items.reduce((s, it) => s + (it.say || '').split(/\s+/).filter(Boolean).length, 0);
    const warns = items.filter((it) => it.warn).length;
    const rate = parseFloat($('rdRate').value) || 1;
    $('rdSum').replaceChildren(
      el('span', { text: `${items.length} things read` }), ' · ',
      el('span', { text: `${words_} words` }), ' · ',
      el('span', { text: `≈ ${fmt(words_ / (175 * rate) * 60)} to listen` }), ' · ',
      el('span', { class: warns ? 'bad' : 'good', text: warns ? `⚠ ${warns} problem(s) a listener would hear` : '✓ nothing a listener would trip over' }));
    renderElements();
    if (items[cur]) showSpeech(items[cur]);
  }
  function renderElements() {
    const kind = $('rdElKind').value;
    const sel = $('rdEl');
    const pick = items.map((it, k) => [it, k]).filter(([it]) => kind === 'w' ? it.warn : it.nav === kind && it.kind !== 'exit');
    sel.replaceChildren(el('option', { value: '', text: pick.length ? `${pick.length} — pick one to read it` : 'none' }),
      ...pick.map(([it, k]) => el('option', { value: k, text: `${it.page >= 0 ? 'p' + (it.page + 1) + ' · ' : ''}${kind === 'h' ? it.role + ' ' : ''}${(kind === 'w' ? it.warn : it.text || it.say || '').slice(0, 80)}` })));
  }
  // the tag being read, lit on the page
  let hl = null;
  function drawHighlight(overlay) {
    const ov = overlay || ED.overlay;
    if (!ov) return;
    ov.querySelectorAll('.rdhl').forEach((x) => x.remove());
    const it = items[cur];
    if (!it || !pane.offsetParent && !playing) return;
    const n = ED.model.nodes[it.id];
    if (!n) return;
    (n.rects || []).filter((r) => r[0] === ED.page).forEach((r) => {
      const d = el('div', { class: 'rdhl' + (it.warn ? ' warn' : '') });
      Object.assign(d.style, { left: r[1] * ED.zoom - 3 + 'px', top: r[2] * ED.zoom - 3 + 'px',
        width: (r[3] - r[1]) * ED.zoom + 6 + 'px', height: (r[4] - r[2]) * ED.zoom + 6 + 'px' });
      ov.append(d);
      hl = d;
    });
    if (hl && $('rdFollow').checked && hl.isConnected) hl.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
  }
  function clearPage() { if (ED.overlay) ED.overlay.querySelectorAll('.rdhl').forEach((x) => x.remove()); }

  function transcript() {
    const lines = items.map((it, k) => `${String(k + 1).padStart(4)}  p${it.page + 1}  ${(it.role || '').padEnd(8)} ${it.say || ''}${it.warn ? '   ⚠ ' + it.warn : ''}`);
    return `Reader check — ${window.META && window.META.filename || ''}\n${$('rdSum').textContent}\n\n${lines.join('\n')}\n`;
  }

  // ------------------------------------------------------------ the panel
  pane.replaceChildren(
    el('div', { class: 'rdhead' },
      el('div', {}, el('b', { text: 'Reader' }), el('span', { class: 'tiny muted', text: ' — what NVDA reads, out loud' })),
      el('div', { class: 'spacer' }),
      el('select', { class: 'sm', id: 'rdScope', title: 'What to read' },
        el('option', { value: 'page', text: 'from this page' }), el('option', { value: 'doc', text: 'whole file' }),
        el('option', { value: 'from', text: 'from the selected tag' })),
      el('button', { class: 'sm', id: 'rdRebuild', text: '↻', title: 'Read the tags again (after an edit)' })),
    el('div', { class: 'rdtransport' },
      el('button', { class: 'sm', id: 'rdPrev', text: '⏮', title: 'Previous (↑)' }),
      el('button', { class: 'sm primary', id: 'rdPlay', text: '▶ Play', title: 'Say all — on and on, page after page (Space)' }),
      el('button', { class: 'sm', id: 'rdStop', text: '⏹', title: 'Stop (Esc or Ctrl)' }),
      el('button', { class: 'sm', id: 'rdNext', text: '⏭', title: 'Next (↓)' }),
      el('label', { class: 'rdrate', title: 'Speed' }, '🐢', el('input', { type: 'range', id: 'rdRate', min: '0.3', max: '2.5', step: '0.1', value: String(pref.rate || (window.UA && UA.speechRate ? UA.speechRate() : 1)) }), '🐇',
        el('span', { class: 'tiny', id: 'rdRateV' }))),
    el('div', { class: 'rdspeech', id: 'rdSpeech', 'aria-live': 'off' }, el('div', { class: 'rdtext empty', text: 'Press ▶ Play (or Space here) to hear the page as NVDA reads it.' })),
    el('div', { class: 'tiny muted rdstatus', id: 'rdStatus' }),
    el('div', { class: 'rdnav' },
      ...[['h', 'H', 'Next heading (H, Shift+H back, 1–6 a level)'], ['l', 'L', 'Next list (L)'], ['i', 'I', 'Next list item (I)'],
        ['t', 'T', 'Next table (T)'], ['g', 'G', 'Next graphic (G)'], ['k', 'K', 'Next link (K)'], ['f', 'F', 'Next form field (F)'],
        ['w', '⚠', 'Next problem a listener would hear (W)']]
        .map(([nav, t, title]) => el('button', { class: 'sm', text: t, title, onclick: (e) => (nav === 'w' ? nextProblem(e.shiftKey) : jump(nav, e.shiftKey)) }))),
    el('div', { class: 'rdels' },
      el('select', { class: 'sm', id: 'rdElKind', title: 'Elements list (NVDA+F7)' },
        ...[['h', 'Headings'], ['l', 'Lists'], ['t', 'Tables'], ['g', 'Graphics'], ['k', 'Links'], ['f', 'Form fields'], ['w', '⚠ Problems']]
          .map(([v, t]) => el('option', { value: v, text: t }))),
      el('select', { class: 'sm', id: 'rdEl', title: 'Pick one to read it' })),
    el('details', { class: 'rdopts' }, el('summary', { class: 'small', text: 'Voice and options' }),
      el('div', { class: 'rdoptgrid' },
        el('select', { class: 'sm', id: 'rdVoice' }),
        el('select', { class: 'sm', id: 'rdVerb', title: 'How much is said' },
          el('option', { value: 'full', text: 'say tag types (like NVDA)' }), el('option', { value: 'text', text: 'words only' })),
        el('label', { class: 'row tiny' }, el('input', { type: 'checkbox', id: 'rdVoiceOn' }), 'speak out loud'),
        el('label', { class: 'row tiny' }, el('input', { type: 'checkbox', id: 'rdSounds' }), 'sounds (heading, list, table, link, ⚠)'),
        el('label', { class: 'row tiny' }, el('input', { type: 'checkbox', id: 'rdFollow' }), 'follow on the page'),
        el('label', { class: 'row tiny' }, el('input', { type: 'checkbox', id: 'rdOn' }), 'go on to the next pages'),
        el('label', { class: 'row tiny' }, el('input', { type: 'checkbox', id: 'rdPages' }), 'say page numbers'),
        el('label', { class: 'row tiny' }, el('input', { type: 'checkbox', id: 'rdLangs' }), 'switch voice by language (/Lang)'),
        el('label', { class: 'row tiny' }, el('input', { type: 'checkbox', id: 'rdPitch' }), 'headings in a higher voice'),
        el('label', { class: 'row tiny', title: 'The words being read, big, at the foot of the screen — like captions' }, el('input', { type: 'checkbox', id: 'rdCaps' }), 'captions on the page'))),
    el('div', { class: 'rdsum small', id: 'rdSum' }),
    el('div', { class: 'rdlisthead' }, el('b', { class: 'small', text: 'Transcript' }),
      el('label', { class: 'row tiny' }, el('input', { type: 'checkbox', id: 'rdOnlyWarn' }), 'only problems'),
      el('div', { class: 'spacer' }),
      el('button', { class: 'sm', id: 'rdCopy', text: 'Copy', title: 'Copy the transcript' }),
      el('button', { class: 'sm', id: 'rdSave', text: 'Save .txt', title: 'Save the transcript' })),
    el('div', { class: 'rdlist', id: 'rdList', role: 'list', 'aria-label': 'What is read, in order' }),
    el('p', { class: 'tiny muted', text: 'Keys here: Space play / pause · ↑ ↓ previous / next · H L I T G K F next heading, list, item, table, graphic, link, field (Shift: back) · 1–6 heading level · W next problem · Home / End · Esc stop. Click a line: read from it · double-click: show the tag.' }));
  pane.tabIndex = 0;
  if (S) { loadVoices(); S.onvoiceschanged = loadVoices; } else loadVoices();

  const bools = { rdVoiceOn: true, rdSounds: true, rdFollow: true, rdOn: true, rdPages: false, rdLangs: true, rdPitch: false, rdCaps: true };
  Object.entries(bools).forEach(([id, d]) => {
    $(id).checked = pref[id] != null ? !!pref[id] : d;
    $(id).addEventListener('change', () => { pref[id] = $(id).checked; savePref(); if (id === 'rdPages') { built = false; build(); } });
  });
  pref.pages = $('rdPages').checked;
  $('rdPages').addEventListener('change', () => { pref.pages = $('rdPages').checked; savePref(); build(); });
  $('rdVerb').value = pref.verbosity || 'full';
  $('rdVerb').addEventListener('change', () => { pref.verbosity = $('rdVerb').value; savePref(); build(); });
  $('rdVoice').addEventListener('change', () => { pref.voice = $('rdVoice').value; savePref(); });
  const rateShow = () => { $('rdRateV').textContent = `${parseFloat($('rdRate').value).toFixed(1)}×`; };
  rateShow();
  $('rdRate').addEventListener('input', () => { pref.rate = parseFloat($('rdRate').value); savePref(); if (UA.speechRate) UA.speechRate(pref.rate); rateShow(); render(); });
  $('rdScope').value = pref.scope || 'page';
  $('rdScope').addEventListener('change', () => { pref.scope = $('rdScope').value; savePref(); pause(); build(); });
  $('rdRebuild').addEventListener('click', () => { pause(); build(); UA.toast('Read the tags again'); });
  $('rdPlay').addEventListener('click', () => (playing ? pause() : play()));
  $('rdStop').addEventListener('click', stop);
  $('rdPrev').addEventListener('click', () => step(-1));
  $('rdNext').addEventListener('click', () => step(1));
  $('rdOnlyWarn').addEventListener('change', render);
  $('rdElKind').addEventListener('change', renderElements);
  $('rdEl').addEventListener('change', () => { const k = parseInt($('rdEl').value, 10); if (!isNaN(k)) { pause(); cur = k; focusItem(items[k]); say(items[k], ++token); } });
  $('rdCopy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(transcript()); UA.toast('Transcript copied'); } catch (e) { UA.toast('Copy is blocked here — use Save .txt'); }
  });
  $('rdSave').addEventListener('click', () => {
    const a = el('a', { href: URL.createObjectURL(new Blob([transcript()], { type: 'text/plain' })), download: `reader_${ED.JID}.txt` });
    document.body.append(a); a.click(); a.remove();
  });
  pane.addEventListener('keydown', (e) => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName) && e.key !== 'Escape') return;
    const k = e.key;
    const stopIt = () => { e.preventDefault(); e.stopPropagation(); };
    if (k === ' ' && !e.ctrlKey) { stopIt(); playing ? pause() : play(); return; }
    if (k === 'Escape' || k === 'Control') { stopIt(); pause(); return; }
    if (k === 'ArrowDown') { stopIt(); step(1); return; }
    if (k === 'ArrowUp') { stopIt(); step(-1); return; }
    if ((k === 'Home' || k === 'End') && !items.length) { stopIt(); return; }
    if (k === 'Home') { stopIt(); pause(); cur = 0; focusItem(items[0]); say(items[0], ++token); return; }
    if (k === 'End') { stopIt(); pause(); cur = items.length - 1; focusItem(items[cur]); say(items[cur], ++token); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const low = k.toLowerCase();
    if (/^[1-6]$/.test(k)) { stopIt(); jump('h', e.shiftKey, +k); return; }
    if (low === 'w') { stopIt(); nextProblem(e.shiftKey); return; }
    if ('hltgkfip'.includes(low) && low.length === 1) { stopIt(); jump(low, e.shiftKey); return; }
    if (/^[a-z]$/.test(low)) stopIt();      // no tag changes from the Reader keys
  });

  window.READER = {
    show() {
      const scope = $('rdScope').value;
      if (!built || builtFor !== scope || scope === 'page') {
        const keep = playing;
        if (!keep) build();
      }
      pane.focus({ preventScroll: true });
      drawHighlight();
    },
    hide() { pause(); clearPage(); const c = document.getElementById('rdCap'); if (c) c.classList.remove('show'); },
    draw(ov) { if (pane.offsetParent) drawHighlight(ov); },
    modelLoaded() { if (!playing) built = false; },
    get items() { return items; },
    get cur() { return cur; },
    play, pause, step, jump,
  };
})();
