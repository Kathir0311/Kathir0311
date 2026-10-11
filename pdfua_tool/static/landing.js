/* The front page (V4.29, more in V4.32): the moving parts — a net of tags in the background, the tagging demo, Dez AI's orb
   and its speech bubble, the zone-learning story, the team lane, numbers that count up, things that come up as
   they are scrolled to — and the sign-in form. Nothing is loaded from the internet. The computer's "less motion"
   setting, and the pause button in the header, stop everything that moves: the pictures then show how they end. */
(function () {
  'use strict';
  window.__dzOK = true;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => [...(r || document).querySelectorAll(s)];
  const root = document.documentElement;
  const mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false, addEventListener() {} };
  const dark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : { matches: false, addEventListener() {} };
  const still = () => mq.matches || root.classList.contains('dz-still');
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { /* none */ } return null; };
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const visible = new Set();                 // the sections on the screen now
  const watchers = [];

  /* ------------------------------------------------------------------ light / dark, and a pause for the motion */
  const effectiveTheme = () => root.dataset.theme || (dark.matches ? 'dark' : 'light');
  const themeBtn = $('#dzTheme');
  function paintTheme() { if (themeBtn) { themeBtn.setAttribute('aria-pressed', String(effectiveTheme() === 'dark')); themeBtn.title = effectiveTheme() === 'dark' ? 'Switch to the light look' : 'Switch to the dark look'; } }
  if (themeBtn) themeBtn.addEventListener('click', () => {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next; store('dz-theme', next); paintTheme(); document.dispatchEvent(new Event('dz-theme'));
  });
  dark.addEventListener && dark.addEventListener('change', () => { paintTheme(); document.dispatchEvent(new Event('dz-theme')); });
  paintTheme();
  const stillBtn = $('#dzStill');
  function paintStill() {
    const s = still();
    if (stillBtn) {
      stillBtn.setAttribute('aria-pressed', String(root.classList.contains('dz-still')));
      const t = root.classList.contains('dz-still') ? 'Play the moving pictures' : 'Pause the moving pictures';
      stillBtn.title = t; stillBtn.setAttribute('aria-label', t);
    }
    return s;
  }
  if (stillBtn) stillBtn.addEventListener('click', () => {
    root.classList.toggle('dz-still');
    store('dz-still', root.classList.contains('dz-still') ? '1' : '0');
    paintStill(); document.dispatchEvent(new Event('dz-motion'));
  });
  mq.addEventListener && mq.addEventListener('change', () => { paintStill(); document.dispatchEvent(new Event('dz-motion')); });
  paintStill();

  /* ------------------------------------------------------------------ things come up as they are scrolled to */
  const SIBLINGS = '.dz-caps,.dz-feats,.dz-flow,.dz-stats,.dz-steps3,.dz-chips,.dz-ticks,.dz-adv,.dz-tiles,.dz-pack,.dz-faq';
  $$(SIBLINGS).forEach((ul) => $$(':scope > .reveal', ul).forEach((li, i) => li.style.setProperty('--rd', (i * 0.07).toFixed(2) + 's')));
  $$('.dz-copy .reveal').forEach((el, i) => el.style.setProperty('--rd', (i * 0.09).toFixed(2) + 's'));
  const reveal = $$('.reveal');
  if (!('IntersectionObserver' in window) || still()) reveal.forEach((el) => el.classList.add('in'));
  else {
    const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } }), { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
    reveal.forEach((el) => io.observe(el));
    setTimeout(() => reveal.forEach((el) => { const r = el.getBoundingClientRect(); if (r.top < innerHeight && r.bottom > 0) el.classList.add('in'); }), 1800);
    document.addEventListener('dz-motion', () => { if (still()) reveal.forEach((el) => el.classList.add('in')); });
  }
  // what starts when a part of the page comes into view (and rests when it leaves)
  function onView(el, fn, leave, threshold) {
    if (!el) return;
    if (!('IntersectionObserver' in window)) { fn(); return; }
    new IntersectionObserver((es) => es.forEach((e) => {
      if (e.isIntersecting) { visible.add(el); fn(); } else { visible.delete(el); if (leave) leave(); }
    }), { threshold: threshold || 0.3 }).observe(el);
  }

  /* ------------------------------------------------------------------ the net of tags in the background */
  (function net() {
    const c = $('#dzNet');
    if (!c || !c.getContext) return;
    const ctx = c.getContext('2d');
    const LABELS = ['H1', 'H2', 'P', 'P', 'P', 'L', 'LI', 'Table', 'TR', 'TD', 'Figure', 'Link', 'Note', 'Caption', 'Sect', 'Span', 'Alt'];
    let w = 0, h = 0, dpr = 1, nodes = [], raf = 0, px = -9999, py = -9999, cols = [];
    const css = (n, d) => (getComputedStyle(document.body).getPropertyValue(n) || d).trim() || d;
    function colours() { cols = [css('--h', '#2862e8'), css('--fig', '#b07b00'), css('--tbl', '#0f7a70'), css('--lst', '#7a3cc2'), css('--lnk', '#c2185b'), css('--accent', '#2862e8')]; }
    function size() {
      dpr = Math.min(2, window.devicePixelRatio || 1);
      w = innerWidth; h = innerHeight;
      c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const want = Math.max(16, Math.min(46, Math.round(w * h / 30000)));
      while (nodes.length < want) nodes.push(make());
      nodes.length = want;
    }
    function make() {
      const a = Math.random() * Math.PI * 2, v = 0.12 + Math.random() * 0.22;
      return { x: Math.random() * (w || 800), y: Math.random() * (h || 600), vx: Math.cos(a) * v, vy: Math.sin(a) * v,
               label: LABELS[(Math.random() * LABELS.length) | 0], k: (Math.random() * 6) | 0, s: 0.8 + Math.random() * 0.5 };
    }
    function chip(n, alpha) {
      const col = cols[n.k] || '#2862e8', t = n.label;
      ctx.font = `600 ${Math.round(10 * n.s)}px ui-monospace,Menlo,Consolas,monospace`;
      const tw = ctx.measureText(t).width + 12, th = 15 * n.s;
      ctx.globalAlpha = alpha; ctx.fillStyle = col;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(n.x - tw / 2, n.y - th / 2, tw, th, 5); else ctx.rect(n.x - tw / 2, n.y - th / 2, tw, th);
      ctx.fill();
      ctx.globalAlpha = Math.min(1, alpha + 0.3); ctx.fillStyle = '#fff';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(t, n.x, n.y + 0.5);
    }
    function draw(move) {
      ctx.clearRect(0, 0, w, h);
      const R = 190;
      ctx.lineWidth = 1;
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        if (move) {
          a.x += a.vx; a.y += a.vy;
          const dx = a.x - px, dy = a.y - py, d2 = dx * dx + dy * dy;
          if (d2 < 150 * 150 && d2 > 1) { const d = Math.sqrt(d2), f = (150 - d) / 150 * 0.9; a.x += dx / d * f; a.y += dy / d * f; }
          if (a.x < -40) a.x = w + 40; else if (a.x > w + 40) a.x = -40;
          if (a.y < -40) a.y = h + 40; else if (a.y > h + 40) a.y = -40;
        }
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j], dx = a.x - b.x, dy = a.y - b.y, d = dx * dx + dy * dy;
          if (d < R * R) {
            ctx.globalAlpha = (1 - Math.sqrt(d) / R) * 0.18;
            ctx.strokeStyle = cols[a.k] || '#2862e8';
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
          }
        }
      }
      for (const n of nodes) chip(n, 0.22);
      ctx.globalAlpha = 1;
    }
    function frame() { raf = 0; if (still() || document.hidden) return; draw(true); raf = requestAnimationFrame(frame); }
    function run() { if (!raf && !still() && !document.hidden) raf = requestAnimationFrame(frame); }
    function paintOnce() { colours(); draw(false); }
    size(); colours(); draw(false); run();
    addEventListener('resize', () => { size(); draw(false); run(); });
    addEventListener('pointermove', (e) => { px = e.clientX; py = e.clientY; }, { passive: true });
    addEventListener('pointerleave', () => { px = py = -9999; });
    document.addEventListener('visibilitychange', run);
    document.addEventListener('dz-motion', () => { if (still()) { if (raf) cancelAnimationFrame(raf); raf = 0; paintOnce(); } else run(); });
    document.addEventListener('dz-theme', () => setTimeout(paintOnce, 30));
  })();

  /* ------------------------------------------------------------------ the words that turn */
  (function rot() {
    const box = $('#dzRot');
    if (!box) return;
    const words = $$('span', box);
    let i = 0, timer = 0;
    function next() {
      timer = 0;
      if (still() || document.hidden) { arm(); return; }
      const cur = words[i]; i = (i + 1) % words.length; const nx = words[i];
      cur.classList.remove('on'); cur.classList.add('out');
      nx.classList.add('on');
      setTimeout(() => { cur.classList.remove('out'); }, 520);
      arm();
    }
    function arm() { if (!timer) timer = setTimeout(next, 2300); }
    arm();
  })();

  /* ------------------------------------------------------------------ the tagging demo */
  const demo = (function () {
    const stage = $('#dzStage');
    if (!stage) return null;
    const page = $('#dzPage'), beam = $('.dz-beam', page), tree = $('#dzTree'), con = $('#dzCon'), score = $('#dzScore');
    const state = $('#dzState'), status = $('#dzStatus');
    const blocks = $$('.blk', page);
    const confidence = { H1: 99, H2: 98, P: 97, Figure: 94, Caption: 96, Table: 95, L: 96 };
    let run = 0, typing = 0, playing = false;
    const set = (el, t) => { if (el) el.textContent = t; };
    const mode = (m, label, st) => { stage.dataset.state = m; set(state, label); set(status, st); };
    const cursor = () => $('.cur', con);
    function line(html, plain) {
      const row = document.createElement('div');
      row.className = 'row';
      con.insertBefore(row, cursor());
      while (con.querySelectorAll('.row').length > 6) con.querySelector('.row').remove();
      if (still() || !plain) { row.innerHTML = html; return Promise.resolve(); }
      const id = ++typing; let k = 0;
      return new Promise((done) => {
        (function step() {
          if (id !== typing) { row.innerHTML = html; return done(); }
          k += 2; row.textContent = plain.slice(0, k);
          if (k < plain.length) setTimeout(step, 16); else { row.innerHTML = html; done(); }
        })();
      });
    }
    function treeRow(b, i) {
      const li = document.createElement('li');
      li.dataset.tag = b.dataset.tag;
      li.style.setProperty('--lv', '1');
      li.innerHTML = `<span class="tg">${b.dataset.tag}</span><span class="nm">${b.dataset.node.replace(/^[^·]*· ?/, '') || b.dataset.node}</span><span class="cf">${confidence[b.dataset.tag] || 95}%</span>`;
      tree.append(li);
    }
    function reset() {
      blocks.forEach((b) => { b.classList.remove('tagged'); b.removeAttribute('data-order'); });
      tree.replaceChildren();
      const root0 = document.createElement('li'); root0.dataset.tag = 'Doc'; root0.innerHTML = '<span class="tg" style="background:#5b6b82">Document</span><span class="nm">research-article.pdf</span>';
      tree.append(root0);
      $$('.row', con).forEach((r) => r.remove());
      score.classList.remove('on');
      beam.style.transition = 'none'; beam.style.opacity = '0'; beam.style.transform = 'translateY(0)';
    }
    function finishedLook() {
      reset();
      blocks.forEach((b, i) => { b.dataset.order = i + 1; b.classList.add('tagged'); treeRow(b, i); });
      con.insertBefore(Object.assign(document.createElement('div'), { className: 'row', innerHTML: '<span class="ok">✓</span> 0 errors · reading order set · ready for review' }), cursor());
      score.classList.add('on');
      mode('done', 'done', 'finished');
    }
    async function play() {
      const my = ++run; typing++;
      if (still()) { finishedLook(); return; }
      playing = true;
      reset();
      mode('working', 'working', 'reading page 1…');
      await line('<b>&gt;</b> reading page 1…', '> reading page 1…');
      if (my !== run) return;
      const H = page.clientHeight, D = 5400;
      beam.style.opacity = '1';
      void beam.offsetWidth;
      beam.style.transition = `transform ${D}ms linear`;
      beam.style.transform = `translateY(${H + 70}px)`;
      set(status, 'tagging…');
      const msgs = { Figure: ['<b>&gt;</b> alt text drafted: “Green hills under a bright sun.”', '> alt text drafted: “Green hills under a bright sun.”'],
                     Table: ['<b>&gt;</b> table: 3 columns, header row found', '> table: 3 columns, header row found'],
                     L: ['<b>&gt;</b> list: 3 items, reading order set', '> list: 3 items, reading order set'] };
      blocks.forEach((b, i) => {
        const t = ((b.offsetTop + b.offsetHeight * 0.55) / H) * D + 180;
        setTimeout(() => {
          if (my !== run) return;
          b.dataset.order = i + 1; b.classList.add('tagged'); treeRow(b, i);
          const m = msgs[b.dataset.tag]; if (m) line(m[0], m[1]);
        }, t);
      });
      await wait(D + 500);
      if (my !== run) return;
      beam.style.opacity = '0';
      mode('working', 'checking', 'checking PDF/UA…');
      await line('<b>&gt;</b> checking PDF/UA-1 and WCAG 2.1 AA…', '> checking PDF/UA-1 and WCAG 2.1 AA…');
      if (my !== run) return;
      await wait(500);
      score.classList.add('on');
      await line('<span class="ok">✓</span> 0 errors · reading order set · ready for review', '✓ 0 errors · reading order set · ready for review');
      mode('done', 'done', 'finished');
      playing = false;
      await wait(5200);
      if (my === run && visible.has(stage) && !still()) play();
    }
    const rep = $('#dzReplay');
    if (rep) rep.addEventListener('click', () => { if (still()) { finishedLook(); return; } play(); });
    onView(stage, () => { if (!playing && stage.dataset.state === 'idle') play(); else if (!playing && !still() && stage.dataset.state === 'done') play(); }, null, 0.4);
    document.addEventListener('dz-motion', () => { if (still()) { run++; finishedLook(); } else if (visible.has(stage)) play(); });
    return { play };
  })();

  /* ------------------------------------------------------------------ Dez AI: the orb and what it says */
  (function orb() {
    const o = $('#dzOrb'), bub = $('#dzBubble .typed'), zone = $('.dz-aihero');
    if (!o || !zone) return;
    zone.addEventListener('pointermove', (e) => {
      if (still()) return;
      const r = zone.getBoundingClientRect();
      const dx = (e.clientX - (r.left + r.width / 2)) / r.width, dy = (e.clientY - (r.top + r.height / 2)) / r.height;
      o.style.transform = `translate(${(dx * 22).toFixed(1)}px,${(dy * 18).toFixed(1)}px)`;
    });
    zone.addEventListener('pointerleave', () => { o.style.transform = ''; });
    o.style.transition = 'transform .25s var(--ease)';
    const say = [
      'I tagged page 1 in 0.4 seconds: 1 heading, 6 paragraphs and 1 table.',
      'This picture needs alt text. My draft: “Bar chart of quarterly sales — Q4 is the highest.”',
      'You cut that zone twice. 9 more places look the same — shall I cut them for you?',
      'I am only 62 % sure this line is a heading. Please look at it first.',
      'Everything stays on your server. I never phone home.'];
    let i = 0, my = 0;
    async function loop() {
      const id = ++my;
      while (id === my) {
        if (still()) { bub.textContent = say[0]; return; }
        if (!visible.has(zone)) { await wait(600); continue; }
        const t = say[i++ % say.length];
        for (let k = 1; k <= t.length && id === my; k++) { bub.textContent = t.slice(0, k); await wait(22); }
        await wait(2600);
        for (let k = t.length; k >= 0 && id === my; k -= 3) { bub.textContent = t.slice(0, Math.max(0, k)); await wait(8); }
      }
    }
    bub.textContent = say[0];
    onView(zone, () => {}, null, 0.25);
    loop();
    document.addEventListener('dz-motion', loop);
  })();

  /* ------------------------------------------------------------------ the pointer lights the cards */
  $$('.spot').forEach((el) => el.addEventListener('pointermove', (e) => {
    const r = el.getBoundingClientRect();
    el.style.setProperty('--mx', (e.clientX - r.left) + 'px'); el.style.setProperty('--my', (e.clientY - r.top) + 'px');
  }, { passive: true }));

  /* ------------------------------------------------------------------ numbers that count up */
  (function counters() {
    const els = $$('.cnt');
    if (!els.length) return;
    const go = (el) => {
      const to = Number(el.dataset.to || el.textContent) || 0;
      if (still() || !to) { el.textContent = String(to); return; }
      const t0 = performance.now(), D = 1500;
      (function tick(t) {
        const p = Math.min(1, (t - t0) / D), e = 1 - Math.pow(1 - p, 3);
        el.textContent = String(Math.round(to * e));
        if (p < 1) requestAnimationFrame(tick);
      })(t0);
    };
    if (!('IntersectionObserver' in window) || still()) return;
    els.forEach((el) => { if (Number(el.dataset.to)) el.textContent = '0'; });
    const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { go(e.target); io.unobserve(e.target); } }), { threshold: 0.6 });
    els.forEach((el) => io.observe(el));
  })();

  /* ------------------------------------------------------------------ "correct it once": the zones story */
  (function zones() {
    const z = $('#dzZones');
    if (!z) return;
    const steps = $$('.dz-steps3 li');
    let my = 0;
    const set = (n) => { z.dataset.step = String(n); steps.forEach((li, i) => li.classList.toggle('hot', (n === 1 || n === 2) ? i === 0 : n === 3 ? i === 1 : n === 4 ? i === 2 : false)); };
    async function loop() {
      const id = ++my;
      if (still()) { set(4); return; }
      while (id === my) {
        if (!visible.has(z)) { await wait(500); continue; }
        set(0); await wait(900); if (id !== my) return;
        set(1); await wait(1700); if (id !== my) return;
        set(2); await wait(1700); if (id !== my) return;
        set(3); await wait(2500); if (id !== my) return;
        set(4); await wait(4200);
      }
    }
    onView(z, () => {}, null, 0.3);
    document.addEventListener('dz-motion', loop);
    loop();
  })();

  /* ------------------------------------------------------------------ many people, one editor at a time */
  (function team() {
    const t = $('#dzTeam');
    if (!t) return;
    const lane = $$('.dz-lane li', t), tag = $('#dzLockTag'), who = $$('.people .p', t);
    const names = ['Anna', 'Raj', 'Mei', null];
    let my = 0;
    function show(i) {
      lane.forEach((l, k) => l.classList.toggle('on', k === i));
      const n = names[i];
      who.forEach((p, k) => {
        const mine = names[k] === n && n;
        p.classList.toggle('a', !!mine);
        const st = $('.st', p); if (st) st.textContent = mine ? 'editing' : (n ? 'view only' : 'done');
      });
      if (n) { tag.textContent = '🔒 ' + n + ' is editing'; tag.classList.remove('free'); }
      else { tag.textContent = '📦 delivered'; tag.classList.add('free'); }
    }
    async function loop() {
      const id = ++my; let i = 0;
      if (still()) { show(0); return; }
      while (id === my) {
        if (!visible.has(t)) { await wait(500); continue; }
        show(i % 4); i++;
        await wait(2700);
        if (id === my && i % 4 !== 0) { tag.textContent = '🔓 free for a moment'; tag.classList.add('free'); await wait(650); }
      }
    }
    onView(t, () => {}, null, 0.3);
    document.addEventListener('dz-motion', loop);
    loop();
  })();

  /* ------------------------------------------------------------------ V4.32: the Online AI teaches Dez AI — the lessons story */
  (function lessons() {
    const box = $('#dzLessons');
    if (!box) return;
    const q = $('#lzQ'), a = $('#lzA'), pct = $('#lzPct'), bar = $('#lzBar'), n = $('#lzN'), st = $('#lzS'), tm = $('#lzT');
    const STEPS = [
      { q: 'Is row 1 of this table a header?', a: 'Yes — I have seen 3 tables like it.', p: 12, n: 60, s: 0, t: 4 },
      { q: 'What should this link say instead of “click here”?', a: 'I remember: “Download the scoring guide (PDF)”.', p: 31, n: 210, s: 1, t: 22 },
      { q: 'Does this table go on over the page break?', a: 'Yes — the header repeats. I am 97 % sure.', p: 49, n: 480, s: 2, t: 61 },
      { q: 'Which line starts a new reference entry?', a: 'The lines that begin “Surname, X.” — 1, 4 and 7.', p: 66, n: 820, s: 4, t: 118 },
      { q: 'What is this picture?', a: 'The publisher’s logo — I described it before.', p: 78, n: 1240, s: 5, t: 190 }];
    const ZERO = { p: 0, n: 0, s: 0, t: 0 };
    const mins = (m) => m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
    let cur = { ...ZERO }, my = 0;
    function paint(v) {
      pct.textContent = Math.round(v.p) + ' %'; bar.style.width = v.p.toFixed(1) + '%';
      n.textContent = Math.round(v.n).toLocaleString('en-US'); st.textContent = String(Math.round(v.s)); tm.textContent = mins(Math.round(v.t));
    }
    function tween(to, ms, id) {
      const from = { ...cur }, t0 = performance.now();
      return new Promise((done) => (function tick(t) {
        if (id !== my) return done();
        const k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3);
        cur = { p: from.p + (to.p - from.p) * e, n: from.n + (to.n - from.n) * e, s: from.s + (to.s - from.s) * e, t: from.t + (to.t - from.t) * e };
        paint(cur);
        if (k < 1) requestAnimationFrame(tick); else done();
      })(t0));
    }
    async function swap(el, text) { el.classList.add('swap'); await wait(260); el.textContent = text; el.classList.remove('swap'); }
    async function loop() {
      const id = ++my;
      if (still()) { const l = STEPS[STEPS.length - 1]; q.textContent = l.q; a.textContent = l.a; q.classList.remove('swap'); a.classList.remove('swap'); cur = { ...l }; paint(cur); return; }
      cur = { ...ZERO }; paint(cur); a.classList.remove('swap');
      let i = 0;
      while (id === my) {
        if (!visible.has(box)) { await wait(500); continue; }
        const k = i % STEPS.length, s = STEPS[k];
        if (k === 0 && i) { cur = { ...ZERO }; paint(cur); }
        a.classList.add('swap');                                  // the last answer goes with its question
        await swap(q, s.q); if (id !== my) return;
        await wait(650); if (id !== my) return;
        await swap(a, s.a); if (id !== my) return;
        await tween(s, 900, id); if (id !== my) return;
        await wait(k === STEPS.length - 1 ? 3600 : 2100);
        i++;
      }
    }
    onView(box, () => {}, null, 0.3);
    document.addEventListener('dz-motion', loop);
    loop();
  })();

  /* ------------------------------------------------------------------ V4.32: the screen-reader preview */
  (function listen() {
    const box = $('#dzSR');
    if (!box) return;
    const lines = $$('.sr-lines li', box), blocks = $$('.sr-page .b', box);
    let my = 0;
    function paint(k) {
      lines.forEach((li, i) => { li.classList.toggle('on', i <= k); li.classList.toggle('now', i === k); });
      blocks.forEach((b) => b.classList.toggle('on', k >= 0 && Number(b.dataset.n) === Number(lines[k].dataset.b)));
    }
    async function loop() {
      const id = ++my;
      if (still()) { lines.forEach((li) => { li.classList.add('on'); li.classList.remove('now'); }); blocks.forEach((b) => b.classList.remove('on')); return; }
      paint(-1);
      while (id === my) {
        if (!visible.has(box)) { await wait(500); continue; }
        for (let k = 0; k < lines.length && id === my; k++) { paint(k); await wait(1500); }
        if (id !== my) return;
        await wait(2400); if (id !== my) return;
        paint(-1); await wait(700);
      }
    }
    onView(box, () => {}, null, 0.3);
    document.addEventListener('dz-motion', loop);
    loop();
  })();

  /* ------------------------------------------------------------------ V4.32: the checks tick, the bars fill */
  (function proof() {
    const list = $('#dzChecks'), ba = $('#dzBA');
    if (list) {
      $$('li', list).forEach((li, i) => li.style.setProperty('--i', i));
      onView(list, () => list.classList.add('go'), null, 0.2);
    }
    if (ba) {
      $$('li', ba).forEach((li, i) => li.style.setProperty('--r', i));
      onView(ba, () => ba.classList.add('go'), null, 0.3);
    }
  })();

  /* ------------------------------------------------------------------ V4.32: the questions — one answer open at a time */
  (function faq() {
    const items = $$('.dz-faq details');
    items.forEach((d) => d.addEventListener('toggle', () => { if (d.open) items.forEach((o) => { if (o !== d && o.open) o.open = false; }); }));
  })();

  /* ------------------------------------------------------------------ V4.32: how far down the page, and the way back up */
  (function progress() {
    const bar = $('#dzProg'), top = $('#dzTop');
    function upd() {
      const h = document.documentElement.scrollHeight - innerHeight;
      const p = h > 0 ? Math.min(1, scrollY / h) : 0;
      if (bar) bar.style.transform = `scaleX(${p.toFixed(4)})`;
      if (top) top.classList.toggle('on', scrollY > innerHeight * 1.2);
    }
    addEventListener('scroll', upd, { passive: true }); addEventListener('resize', upd); upd();
    if (top) top.addEventListener('click', () => { scrollTo({ top: 0, behavior: still() ? 'auto' : 'smooth' }); const b = $('#dzNavSign'); if (b) b.focus({ preventScroll: true }); });
  })();

  /* ------------------------------------------------------------------ small things that wake up when seen */
  onView($('#dzFlow'), () => $('#dzFlow').classList.add('go'), null, 0.4);
  onView($('.dz-shield'), () => $('.dz-shield').classList.add('in'), null, 0.4);
  const nav = $('.dz-nav');
  addEventListener('scroll', () => { if (nav) nav.style.boxShadow = scrollY > 8 ? '0 6px 24px rgba(16,24,40,.10)' : 'none'; }, { passive: true });
  // the link of the part of the page that is on the screen
  (function spy() {
    const links = $$('.dz-links a');
    if (!('IntersectionObserver' in window)) return;
    // the parts that belong to the one in the menu (V4.32)
    const group = { teacher: 'dezai', learn: 'dezai', content: 'features', listen: 'features', delivery: 'standards', how: 'teams', compare: 'teams' };
    const io = new IntersectionObserver((es) => es.forEach((e) => {
      const id = group[e.target.id] || e.target.id;
      if (e.isIntersecting) links.forEach((a) => a.classList.toggle('on', a.getAttribute('href') === '#' + id));
    }), { rootMargin: '-45% 0px -50% 0px' });
    const seen = new Set([...links.map((a) => a.getAttribute('href').slice(1)), ...Object.keys(group)]);
    seen.forEach((id) => { const s = $('#' + id); if (s) io.observe(s); });
  })();

  /* ------------------------------------------------------------------ links to a part of the page */
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href^="#"]');
    if (!a) return;
    const id = a.getAttribute('href');
    const target = id.length > 1 ? $(id) : null;
    if (id === '#top') { e.preventDefault(); scrollTo({ top: 0, behavior: still() ? 'auto' : 'smooth' }); return; }
    if (!target) return;
    e.preventDefault();
    target.scrollIntoView({ behavior: still() ? 'auto' : 'smooth', block: id === '#signin' ? 'center' : 'start' });
    if (history.replaceState) history.replaceState(null, '', id);
    if (id === '#signin') setTimeout(() => { const un = $('#un'); const pw = $('#pw'); ((un && !un.value) ? un : (pw || un)).focus({ preventScroll: true }); }, still() ? 0 : 550);
  });

  /* ------------------------------------------------------------------ the sign-in form */
  (function form() {
    const pw = $('#pw'), eye = $('#pwShow'), un = $('#un'), f = $('#lgForm');
    if (!pw || !f) return;
    if (eye) eye.addEventListener('click', () => {
      const show = pw.type === 'password';
      pw.type = show ? 'text' : 'password';
      eye.setAttribute('aria-pressed', String(show));
      eye.setAttribute('aria-label', show ? 'Hide the password' : 'Show the password');
      eye.title = show ? 'Hide the password' : 'Show the password';
      eye.classList.toggle('on', show);
      pw.focus();
    });
    const caps = $('#caps');
    const capsCheck = (e) => { if (caps && e.getModifierState) caps.hidden = !e.getModifierState('CapsLock'); };
    pw.addEventListener('keydown', capsCheck); pw.addEventListener('keyup', capsCheck);
    pw.addEventListener('blur', () => { if (caps) caps.hidden = true; });
    // the user name is remembered on this computer (never the password)
    try { const last = localStorage.getItem('ua-last-user'); if (last && un && !un.value) { un.value = last; pw.focus({ preventScroll: true }); } } catch (e) { /* none */ }
    f.addEventListener('submit', () => {
      try { localStorage.setItem('ua-last-user', un.value.trim()); } catch (e) { /* none */ }
      // this tab is signed in (a sign-in ends when every tab of the app is closed — static/tabguard.js)
      try { sessionStorage.setItem('ua-tab', Math.random().toString(36).slice(2) + Date.now().toString(36)); } catch (e) { /* none */ }
      const b = $('#lgGo');
      b.classList.add('busy');
      b.querySelector('.lbl').textContent = 'Signing in…';
      setTimeout(() => { b.disabled = true; }, 0);
    });
    if ($('#dzErr')) { f.classList.add('shake'); setTimeout(() => f.classList.remove('shake'), 600); pw.value = ''; pw.focus({ preventScroll: true }); }
  })();
})();
