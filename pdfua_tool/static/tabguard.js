/* Dezquare PDF Accessibility Studio — a sign-in lasts while the app is open (V4.21.4).
 *
 * When every tab of the app has been closed, opening it again asks for a
 * fresh sign-in — even if the browser kept its cookies.
 *
 * Each tab holds a key in sessionStorage (gone when the tab closes; kept on a
 * reload or a link inside the tab). A tab opened with no key asks the other
 * tabs of the app (BroadcastChannel): if one answers, it carries on; if none
 * does, the app was closed, so the session ends and the sign-in page opens.
 * A tab the browser brings back long after it was closed (restore after a
 * restart) is treated the same way. Signing out in one tab signs out all.
 *
 * Loaded in <head> of signed-in pages only; the page stays hidden for the
 * fraction of a second the check takes. */
(function () {
  const KEY = 'ua-tab';
  const CH = 'ua-tabs';
  const html = document.documentElement;
  let ss, ls;
  try { ss = window.sessionStorage; ls = window.localStorage; ss.getItem(KEY); } catch (e) { return; }
  if (!('BroadcastChannel' in window)) return;
  const ch = new BroadcastChannel(CH);
  const newKey = () => Math.random().toString(36).slice(2) + Date.now().toString(36);
  let key = ss.getItem(KEY);
  let ok = false;

  function signOut(why) {
    html.classList.add('tabcheck');
    try { ss.removeItem(KEY); } catch (e) { /* none */ }
    fetch('/logout', { credentials: 'same-origin', redirect: 'manual' }).catch(() => null).finally(() => {
      const next = location.pathname + location.search + location.hash;
      location.replace(why === 'out' ? '/login' : '/login?fresh=1&next=' + encodeURIComponent(next));
    });
  }
  function accept() {
    if (!key) { key = newKey(); ss.setItem(KEY, key); }
    ok = true;
    html.classList.remove('tabcheck');
  }

  // the other tabs: "is the app still open?" — "yes"
  ch.onmessage = (e) => {
    const m = e.data || {};
    if (m.t === 'ping' && ok) ch.postMessage({ t: 'pong', to: m.id });
    if (m.t === 'logout' && ok) signOut('out');
  };
  // the moment this page goes (closed, reloaded or a link followed)
  const leave = () => { try { if (key) ls.setItem('ua-hide-' + key, String(Date.now())); } catch (e) { /* none */ } };
  window.addEventListener('pagehide', leave);
  document.addEventListener('freeze', leave);
  // "Sign out" here signs out every tab
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href="/logout"]');
    if (a) { try { ss.removeItem(KEY); } catch (x) { /* none */ } ch.postMessage({ t: 'logout' }); }
  }, true);

  if (key) {
    // a reload or a link inside the tab comes straight after the page went;
    // a tab brought back by the browser much later (or discarded and
    // reloaded) is checked like a new one
    let last = 0;
    try { last = parseInt(ls.getItem('ua-hide-' + key) || '0', 10); } catch (e) { /* none */ }
    if (document.wasDiscarded || !last || Date.now() - last < 8000) { accept(); return; }
  }
  html.classList.add('tabcheck');
  const id = newKey();
  const done = (alive) => {
    if (ok) return;
    if (alive) accept(); else signOut();
  };
  ch.addEventListener('message', (e) => { const m = e.data || {}; if (m.t === 'pong' && m.to === id) done(true); });
  ch.postMessage({ t: 'ping', id });
  setTimeout(() => done(false), 700);
  // old keys of closed tabs
  try {
    Object.keys(ls).filter((k) => k.startsWith('ua-hide-') && Date.now() - parseInt(ls.getItem(k) || '0', 10) > 7 * 864e5)
      .forEach((k) => ls.removeItem(k));
  } catch (e) { /* none */ }
})();
