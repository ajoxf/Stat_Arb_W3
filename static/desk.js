/* The desk — Arrow Trader's window frame over THIS engine (OKX edition).
 *
 * Every figure on these windows comes from the same endpoints the dashboard
 * reads (/api/signal, /api/ladder, /api/margin, /api/execution,
 * /api/exchange-orders, /api/desk/trades, /api/spread-series, /api/candles),
 * and every order goes through the same server paths — so the rules (the
 * MANUAL / ALGO lock, one position at a time, a close must match what is
 * open) are enforced by the server, and the buttons here only reflect them.
 *
 * House rules, from Arrow Trader: one shared modal, errors that stay until
 * dismissed, and an em dash for anything unmeasured — never a zero.
 */
(function () {
  'use strict';

  var DASH = '—';
  var LAYOUT_KEY = 'nexus-desk-layout-v2';
  var PREF_KEY = 'nexus-desk-prefs-v1';

  // ── tiny helpers ───────────────────────────────────────────────────────
  function $(sel, root) { return (root || document).querySelector(sel); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function num(v, d) {
    if (v == null || !isFinite(v)) { return DASH; }
    return Number(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
  }
  function usd(v, d) {
    if (v == null || !isFinite(v)) { return DASH; }
    var n = Number(v);
    return (n < 0 ? '−$' : '$') + Math.abs(n).toLocaleString('en-US',
      {minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d});
  }
  function signed(v, d) {
    if (v == null || !isFinite(v)) { return DASH; }
    return (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(Number(v)).toFixed(d);
  }
  function ms(v) { return v == null ? DASH : (v >= 1000 ? (v / 1000).toFixed(2) + 's' : v + 'ms'); }
  function clock(t) {
    return t ? new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : DASH;
  }
  // Background polling must never starve the page: a poll whose previous
  // request is still out is skipped, polls use at most 4 connections, and a
  // request is cut off at 10 s.
  var inflight = {}, inflightN = 0, POLL_MAX = 4, slowUntil = 0;
  function getJSON(url, userAsked) {
    var key = url.split('?')[0];
    if (!userAsked && (inflight[key] || inflightN >= POLL_MAX)) {
      return Promise.reject(new Error('skipped: a request is still pending'));
    }
    var counted = !userAsked;
    inflight[key] = true; if (counted) { inflightN++; }
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) { ctl.abort(); } slowUntil = Date.now() + 15000; }, 10000);
    return fetch(url, {cache: 'no-store', signal: ctl ? ctl.signal : undefined}).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    }).finally(function () {
      clearTimeout(timer); inflight[key] = false; if (counted) { inflightN--; }
    });
  }
  function postJSON(url, body) {
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                       body: JSON.stringify(body || {})}).then(function (r) { return r.json(); });
  }
  function load(key, dflt) {
    try { return JSON.parse(localStorage.getItem(key)) || dflt; } catch (e) { return dflt; }
  }
  function save(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) {} }

  // ── toasts + the one modal (also used by the shared ALGO control) ─────
  function toast(msg, kind) {
    var box = $('#toasts');
    var t = document.createElement('div');
    t.className = 'toast ' + (kind === 'danger' ? 'error' : kind || 'info');
    t.innerHTML = '<span>' + esc(msg) + '</span><button class="toast-x" title="Dismiss">×</button>';
    t.querySelector('button').onclick = function () { t.remove(); };
    box.appendChild(t);
    if (kind !== 'danger' && kind !== 'error') { setTimeout(function () { t.remove(); }, 6000); }
  }
  window.showToast = toast;

  window.addEventListener('error', function (e) {
    toast('Page error: ' + (e.message || e) + ' — please screenshot this.', 'danger');
  });

  var algoModal = document.getElementById('algoCtlModal');
  if (algoModal && algoModal.parentNode !== document.body) { document.body.appendChild(algoModal); }

  function dismissDialogs() {
    var m = $('#modal');
    if (m && !m.classList.contains('hidden')) { $('#modal-cancel').click(); }
    if (algoModal && algoModal.classList.contains('show')) {
      var x = algoModal.querySelector('[data-bs-dismiss]');
      if (x) { x.click(); }
    }
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { dismissDialogs(); } });
  [$('#modal'), algoModal].forEach(function (m) {
    if (m) { m.addEventListener('mousedown', function (e) { if (e.target === m) { dismissDialogs(); } }); }
  });

  function confirmBox(title, body, confirmText) {
    var m = $('#modal');
    $('#modal-title').textContent = title;
    $('#modal-body').textContent = body;
    $('#modal-confirm').textContent = confirmText || 'Confirm';
    m.classList.remove('hidden');
    return new Promise(function (resolve) {
      function done(v) {
        m.classList.add('hidden');
        $('#modal-confirm').onclick = $('#modal-cancel').onclick = null;
        resolve(v);
      }
      $('#modal-confirm').onclick = function () { done(true); };
      $('#modal-cancel').onclick = function () { done(false); };
    });
  }

  // ── windows ────────────────────────────────────────────────────────────
  var W = {};
  var layout = load(LAYOUT_KEY, {});
  var prefs = load(PREF_KEY, {confirmClicks: true});
  var topZ = 20;

  var DEFS = [
    {id: 'ladder', title: 'Ladder · Leg A − Leg B', x: 8, y: 8, w: 360, h: 640, flush: true},
    {id: 'signal', title: 'Signal & Position', x: 376, y: 8, w: 470, h: 360},
    {id: 'stats', title: 'Statistics & Filters', x: 854, y: 8, w: 330, h: 360},
    {id: 'bands', title: 'Spread', x: 376, y: 376, w: 470, h: 272},
    {id: 'charts', title: 'Z-Score & Spread', x: 376, y: 376, w: 470, h: 272, closed: true},
    {id: 'margin', title: 'Margin (OKX)', x: 1192, y: 8, w: 300, h: 300},
    {id: 'fills', title: 'Fills & Slippage', x: 8, y: 656, w: 838, h: 250, flush: true},
    {id: 'trades', title: 'Trades', x: 854, y: 376, w: 638, h: 272, flush: true},
    {id: 'orders', title: 'Order Log (OKX)', x: 854, y: 656, w: 638, h: 250, flush: true},
    {id: 'settings', title: 'Settings', x: 120, y: 60, w: 900, h: 640, flush: true, iframe: '/settings', closed: true},
    {id: 'analysis', title: 'Analysis', x: 200, y: 100, w: 1000, h: 640, flush: true, iframe: '/analysis', closed: true},
  ];

  function defOf(id) { return DEFS.filter(function (d) { return d.id === id; })[0]; }

  function fit(d) {
    var dk = $('#desktop');
    var sx = Math.min(1, (dk.clientWidth - 8) / 1500), sy = Math.min(1, (dk.clientHeight - 8) / 910);
    return {x: Math.round(d.x * sx), y: Math.round(d.y * sy), w: Math.round(d.w * sx), h: Math.round(d.h * sy)};
  }

  function place(node, x, y, w, h) {
    node.style.left = Math.max(0, x) + 'px';
    node.style.top = Math.max(0, y) + 'px';
    if (w) { node.style.width = Math.max(220, w) + 'px'; }
    if (h) { node.style.height = Math.max(90, h) + 'px'; }
  }

  function openWindow(id) {
    var d = defOf(id);
    if (!d) { return; }
    if (W[id]) { raise(W[id].node); return; }
    var node = document.createElement('section');
    node.className = 'window floating sized';
    node.dataset.id = id;
    node.innerHTML =
      '<div class="titlebar"><span class="title">' + esc(d.title) + '</span>' +
      (d.iframe ? '<a class="winbtn" href="' + d.iframe + '" target="_blank" title="Open as a full page">↗</a>' : '') +
      '<span class="winbtns"><button class="winbtn close" title="Close">×</button></span></div>' +
      '<div class="wbody' + (d.flush ? ' flush' : '') + '"></div><div class="grip"></div>';
    $('#desktop').appendChild(node);
    var L = layout[id] || {};
    var F = fit(d);
    place(node, L.x != null ? L.x : F.x, L.y != null ? L.y : F.y, L.w || F.w, L.h || F.h);
    node.style.zIndex = L.z || ++topZ;
    topZ = Math.max(topZ, L.z || 0);
    var body = node.querySelector('.wbody');
    W[id] = {def: d, node: node, body: body};
    node.querySelector('.close').onclick = function () { closeWindow(id); };
    node.addEventListener('pointerdown', function (e) { raise(node); startDrag(e, node); });
    if (d.iframe) {
      body.innerHTML = '<iframe class="embed" src="' + d.iframe + '"></iframe>';
    } else {
      BUILD[id](body);
      REFRESH[id] && REFRESH[id]();
    }
    layout[id] = Object.assign({}, layout[id], {open: true});
    save(LAYOUT_KEY, layout);
    renderTabs();
  }

  function closeWindow(id) {
    if (!W[id]) { return; }
    W[id].node.remove();
    delete W[id];
    layout[id] = Object.assign({}, layout[id], {open: false});
    save(LAYOUT_KEY, layout);
    renderTabs();
  }

  function raise(node) {
    node.style.zIndex = ++topZ;
    var id = node.dataset.id;
    layout[id] = Object.assign({}, layout[id], {z: topZ});
  }

  function startDrag(e, node) {
    if (e.button !== 0) { return; }
    var grip = e.target.closest('.grip');
    var bar = e.target.closest('.titlebar');
    if (!grip && !bar) { return; }
    if (!grip && e.target.closest('button, select, input, a')) { return; }
    var id = node.dataset.id;
    var box = node.getBoundingClientRect();
    var origin = $('#desktop').getBoundingClientRect();
    var sx = e.clientX, sy = e.clientY;
    node.classList.add('dragging');
    function move(ev) {
      if (grip) {
        place(node, box.left - origin.left, box.top - origin.top,
              box.width + ev.clientX - sx, box.height + ev.clientY - sy);
      } else {
        place(node, box.left - origin.left + ev.clientX - sx, box.top - origin.top + ev.clientY - sy);
      }
    }
    function drop() {
      node.classList.remove('dragging');
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', drop);
      layout[id] = Object.assign({}, layout[id], {
        x: parseInt(node.style.left, 10), y: parseInt(node.style.top, 10),
        w: node.offsetWidth, h: node.offsetHeight, z: parseInt(node.style.zIndex, 10) || topZ});
      save(LAYOUT_KEY, layout);
      if (id === 'charts') { resizeCharts(); }
    }
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', drop);
    e.preventDefault();
  }

  function renderTabs() {
    var tabs = $('#tabs');
    tabs.innerHTML = '';
    DEFS.forEach(function (d) {
      if (!W[d.id]) { return; }
      var b = document.createElement('button');
      b.className = 'tab';
      b.textContent = d.title;
      b.onclick = function () { raise(W[d.id].node); };
      tabs.appendChild(b);
    });
    var menu = $('#add-menu');
    menu.innerHTML = '<div class="menu-title">Open a window</div>' + DEFS.map(function (d) {
      return '<button data-id="' + d.id + '">' + esc(d.title) + (W[d.id] ? ' <small>open</small>' : '') + '</button>';
    }).join('');
    Array.prototype.forEach.call(menu.querySelectorAll('button[data-id]'), function (b) {
      b.onclick = function () { menu.classList.add('hidden'); openWindow(b.dataset.id); };
    });
  }

  $('#add-panel').onclick = function () { $('#add-menu').classList.toggle('hidden'); };
  $('#tidy').onclick = function () {
    layout = {};
    save(LAYOUT_KEY, layout);
    Object.keys(W).forEach(function (id) {
      var F = fit(W[id].def);
      place(W[id].node, F.x, F.y, F.w, F.h);
    });
    resizeCharts();
  };

  // ── shared state from the server ──────────────────────────────────────
  // S.status mirrors the arrow shape: {signal: {...}, open_trade: {...}}
  // assembled from /api/signal (which carries the algo snapshot + edge).
  var S = {status: null, algo: null, ladder: null, statusAt: 0};

  function pollStatus() {
    return getJSON('/api/signal').then(function (d) {
      var a = d.algo || {};
      var op = d.open_position;
      var lv = a.spread_levels || {};
      var apos = a.position || {};
      S.status = {
        signal: {
          sell_spread: d.sell_spread, buy_spread: d.buy_spread,
          z_sell: d.z_sell, z_buy: d.z_buy,
          entry_threshold: d.entry_zscore,
          trade_direction: d.trade_direction || 'both',
          history_sec: d.history_sec, min_history_sec: d.min_history_sec,
          data_ready: d.ready, sample_status: d.sample_status,
          spread_mean: d.mean, spread_std: d.std,
          half_life_sec: d.half_life_sec,
          measured_interval_sec: d.measured_interval_sec,
          quote_rate_per_min: d.quote_rate_per_min,
          regime: d.regime, book: d.book,
          band_source: d.band_source, band_timeframe: d.band_timeframe,
          edge: d.edge || {},
        },
        algo_snap: a,
        open_trade: op ? {
          owner: op.owner,
          position_type: String(op.direction || '').replace('_SPREAD', ''),
          quantity: op.qty,
          entry_spread: (a.entry_spread != null ? a.entry_spread
                         : (op.journal ? op.journal.spread : null)),
          unrealized_pnl: a.net_pnl,
          spread_levels: lv,
          exit_target_usd: a.profit_target, exit_stop_usd: a.dollar_stop,
          entry_zscore: (apos.entry_z != null ? apos.entry_z
                         : (op.journal ? op.journal.z : null)),
        } : null,
      };
      S.statusAt = Date.now();
      $('#engine-banner').classList.add('hidden');
      REFRESH.signal && W.signal && REFRESH.signal();
      REFRESH.stats && W.stats && REFRESH.stats();
      renderBanners();
    }).catch(function (e) {
      if (S.statusAt && Date.now() - S.statusAt < 5000) { return; }
      if (/^skipped/.test(String(e && e.message)) && S.statusAt) { return; }
      var b = $('#engine-banner');
      b.textContent = 'The engine is not answering — prices and positions on this screen are NOT live.';
      b.classList.remove('hidden');
    });
  }

  function pollAlgo() {
    return getJSON('/api/algo/state').then(function (d) { S.algo = d; renderBanners(); applyLock(); })
      .catch(function () {});
  }

  function renderBanners() {
    var a = S.algo || {};
    var b = $('#lock-banner');
    var msg = '';
    if (a.running) {
      msg = 'ALGO ON — manual orders are off on every window. Turn the algo OFF to trade by hand.';
    } else if (a.algo_block) {
      msg = a.algo_block;
    }
    b.textContent = msg;
    b.classList.toggle('hidden', !msg);
    var lb = $('#link-badge');
    var st = S.status;
    var fresh = S.statusAt && Date.now() - S.statusAt < 5000;
    var feed = st && st.signal && st.signal.book;
    var slow = Date.now() < slowUntil;
    lb.className = 'link ' + (!fresh ? 'bad' : slow ? 'warn' : feed ? 'ok' : 'warn');
    lb.textContent = !fresh ? 'engine down' : slow ? 'server slow' : feed ? 'book live' : 'no book';
    lb.title = slow ? 'A request took over 10 s and was cut off — the server is slow to answer.'
                    : 'OKX connection and price feed';
    $('#loop-stat').textContent = S.statusAt ? ((Date.now() - S.statusAt) / 1000).toFixed(1) + 's' : DASH;
  }

  // Manual controls follow the lock the server reports.
  function applyLock() {
    var a = S.algo || {};
    var blocked = !!a.manual_block;
    var flat = !(S.status && S.status.open_trade);
    Array.prototype.forEach.call(document.querySelectorAll('[data-manual]'), function (el) {
      var closer = el.classList.contains('flatten') || el.classList.contains('pos-close');
      el.disabled = blocked || (closer && flat);
      el.title = blocked ? a.manual_block : (closer && flat) ? 'No open position' : (el.dataset.tip || '');
    });
    if (W.ladder) {
      W.ladder.node.classList.toggle('locked-manual', blocked);
      var note = $('.lock-note', W.ladder.body);
      if (note) { note.classList.toggle('hidden', !blocked); }
    }
  }

  // ── manual orders (server enforces the lock and one-position rule) ────
  function manualOrder(direction) {
    var clips = parseInt(($('.qty-in', W.ladder && W.ladder.body) || {}).value, 10) || 1;
    var lad = S.ladder || {};
    var qtyBtc = (lad.qty || 0) * clips;
    var px = direction === 'LONG_SPREAD' ? lad.buy_spread : lad.sell_spread;
    var words = (direction === 'LONG_SPREAD' ? 'BUY' : 'SELL') + ' the spread · ' + clips +
      ' clip(s) = ' + num(qtyBtc, 6) + ' leg-B units · at ~' + num(px, 2) +
      ' (' + (direction === 'LONG_SPREAD' ? 'Ask A − Bid B' : 'Bid A − Ask B') + ')';
    var go = prefs.confirmClicks !== false
      ? confirmBox('Manual order', words + '\n\nTagged MANUAL. The algo cannot start while it is open.',
                   direction === 'LONG_SPREAD' ? 'BUY' : 'SELL')
      : Promise.resolve(true);
    go.then(function (ok) {
      if (!ok) { return; }
      postJSON('/api/execute', {direction: direction, qty: qtyBtc || undefined}).then(function (r) {
        if (r.success) { toast('MANUAL ' + words + ' — ' + (r.message || 'sent'), 'success'); }
        else { toast('Not sent: ' + (r.error || 'refused'), 'danger'); }
        refreshAll();
      }).catch(function (e) { toast('Order request failed: ' + e, 'danger'); });
    });
  }

  function closePosition() {
    var t = S.status && S.status.open_trade;
    if (!t) { toast('No open position.', 'info'); return; }
    confirmBox('Close position',
      'Close the ' + (t.owner || '') + ' ' + t.position_type + ' position (qty ' +
      num(t.quantity, 6) + ') now, at the ' + (t.position_type === 'LONG' ? 'Sell' : 'Buy') + ' spread.', 'Close')
      .then(function (ok) {
        if (!ok) { return; }
        postJSON('/api/close', {}).then(function (r) {
          toast(r.success ? 'Closed.' : 'Not closed: ' + (r.error || 'refused'), r.success ? 'success' : 'danger');
          refreshAll();
        });
      });
  }

  // ── builders + refreshers per window ──────────────────────────────────
  var BUILD = {}, REFRESH = {};

  // LADDER ------------------------------------------------------------------
  var ladderAnchor = null;
  BUILD.ladder = function (body) {
    body.innerHTML =
      '<div class="body">' +
      '<div class="rail">' +
      '  <div class="lock-note hidden">ALGO ON — manual orders off</div>' +
      '  <div class="route-box row"><div class="route-leg route-a">A <b>—</b></div><div class="route-leg route-b">B <b>—</b></div></div>' +
      '  <button class="quick buy-touch" data-manual data-tip="Buy the spread at the Buy spread (Ask A − Bid B)">BUY</button>' +
      '  <button class="quick sell-touch" data-manual data-tip="Sell the spread at the Sell spread (Bid A − Ask B)">SELL</button>' +
      '  <button class="quick flatten" data-manual data-tip="Close the open position at market">CLOSE POSITION</button>' +
      '  <label class="inline-field qty-row"><span>Qty (clips)</span><input class="qty-in" type="number" min="1" step="1" value="1"></label>' +
      '  <label class="inline-field"><span>Increment</span><input class="inc-in" type="number" min="0" step="0.5" placeholder="1.0"></label>' +
      '  <div class="centre-row"><label class="check"><input type="checkbox" class="lock-scroll"> Lock</label>' +
      '    <label class="check" title="Ask before every manual order"><input type="checkbox" class="confirm-clicks"> Confirm</label></div>' +
      '  <div class="rail-label">Position</div><div class="lad-pos muted">flat</div>' +
      '  <div class="rail-label">Sell / Buy</div><div class="lad-sb">—</div>' +
      '  <div class="lad-err muted"></div>' +
      '</div>' +
      '<div class="grid"><table><thead><tr>' +
      '  <th class="c-work" title="Your position\'s levels: ENTRY, BE (break-even), TP and SL">Mark</th>' +
      '  <th class="c-bid" title="SELL the spread: Bid A − Ask B. Size = what both books can do, in clips">Bids</th>' +
      '  <th class="c-price" title="The spread, Leg A − Leg B, one row per increment">Price</th>' +
      '  <th class="c-ask" title="BUY the spread: Ask A − Bid B. Size = what both books can do, in clips">Asks</th>' +
      '</tr></thead><tbody></tbody></table></div>' +
      '</div>';
    $('.buy-touch', body).onclick = function () { manualOrder('LONG_SPREAD'); };
    $('.sell-touch', body).onclick = function () { manualOrder('SHORT_SPREAD'); };
    $('.flatten', body).onclick = closePosition;
    var cc = $('.confirm-clicks', body);
    cc.checked = prefs.confirmClicks !== false;
    cc.onchange = function () { prefs.confirmClicks = cc.checked; save(PREF_KEY, prefs); };
    $('.lock-scroll', body).onchange = function (e) {
      ladderAnchor = e.target.checked && S.ladder && S.ladder.rows && S.ladder.rows.length
        ? S.ladder.rows[Math.floor(S.ladder.rows.length / 2)].level : null;
    };
    $('tbody', body).onclick = function (e) {
      var td = e.target.closest('td.bid, td.ask');
      if (!td) { return; }
      if ((S.algo || {}).manual_block) { toast(S.algo.manual_block, 'danger'); return; }
      if (!td.classList.contains('clickable')) {
        toast('Only the inside level trades now (the outlined cell). Resting orders at other levels come later.', 'info');
        return;
      }
      manualOrder(td.classList.contains('ask') ? 'LONG_SPREAD' : 'SHORT_SPREAD');
    };
    applyLock();
  };

  REFRESH.ladder = function () {
    if (!W.ladder) { return Promise.resolve(); }
    var body = W.ladder.body;
    var inc = $('.inc-in', body).value;
    var q = '/api/ladder?count=29' + (inc ? '&increment=' + encodeURIComponent(inc) : '') +
            (ladderAnchor != null ? '&anchor=' + ladderAnchor : '');
    return getJSON(q).then(function (d) {
      S.ladder = d;
      if (d.legs) {
        $('.route-a b', body).textContent = d.legs[0];
        $('.route-b b', body).textContent = d.legs[1];
      }
      $('.lad-err', body).textContent = d.error || '';
      $('.lad-sb', body).innerHTML = '<span class="down">' + num(d.sell_spread, 2) + '</span> / <span style="color:var(--bid-strong)">' +
        num(d.buy_spread, 2) + '</span>';
      var pos = d.position;
      $('.lad-pos', body).innerHTML = pos
        ? '<span class="owner ' + pos.owner + '">' + pos.owner.toUpperCase() + '</span> ' +
          (pos.direction === 'LONG_SPREAD' ? 'LONG' : 'SHORT') + ' ' + num(pos.qty, 6)
        : 'flat';
      drawLadder(body, d);
      applyLock();
    }).catch(function () {});
  };

  function nearestRow(rows, v) {
    if (v == null || !rows.length) { return null; }
    var best = null, dist = Infinity;
    rows.forEach(function (r) { var x = Math.abs(r.level - v); if (x < dist) { dist = x; best = r.level; } });
    return best;
  }

  function drawLadder(body, d) {
    var rows = d.rows || [];
    var tb = $('tbody', body);
    var dig = d.increment && d.increment < 1 ? 2 : 0;
    var marks = {}, markTips = {};
    var m = d.markers;
    if (m && rows.length) {
      // A level off the visible ladder is NOT drawn at the edge price as if
      // it were there: it goes on the edge row with an arrow (↑ / ↓).
      var top = rows[0].level, bottom = rows[rows.length - 1].level;
      var half = (d.increment || 0) / 2;
      [['entry', 'ENTRY'], ['break_even', 'BE'], ['take_profit', 'TP'], ['stop', 'SL']].forEach(function (p) {
        var v = m[p[0]];
        if (v == null) { return; }
        var lv, tag = p[1];
        if (v > top + half) { lv = top; tag += '↑'; }
        else if (v < bottom - half) { lv = bottom; tag += '↓'; }
        else { lv = nearestRow(rows, v); }
        marks[lv] = (marks[lv] ? marks[lv] + ' ' : '') + tag;
        markTips[lv] = (markTips[lv] ? markTips[lv] + ' · ' : '') + p[1] + ' ' + num(v, 2);
      });
    }
    var sell = d.sell_spread, buy = d.buy_spread;
    var direction = d.trade_direction || 'both';
    tb.innerHTML = rows.map(function (r) {
      var cls = [];
      if (sell != null && r.level <= sell + 1e-9) { cls.push('in-bid'); }
      if (buy != null && r.level >= buy - 1e-9) { cls.push('in-ask'); }
      if (r.is_mid) { cls.push('mid-line'); }
      var mk = marks[r.level] || '';
      var mkCls = /SL/.test(mk) ? 'sl' : /TP/.test(mk) ? 'tp' : /BE/.test(mk) ? 'be' : mk ? 'entry' : '';
      var bidCell = r.level <= (sell == null ? -Infinity : sell + 1e-9);
      var askCell = r.level >= (buy == null ? Infinity : buy - 1e-9);
      var bidTxt = r.bid_size != null ? r.bid_size : (r.is_best_bid ? '▲' : '');
      var askTxt = r.ask_size != null ? r.ask_size : (r.is_best_ask ? '▼' : '');
      return '<tr class="' + cls.join(' ') + '">' +
        '<td class="work' + (mk ? ' mark ' + mkCls : '') + '"' + (mk ? ' title="' + esc(markTips[r.level]) + '"' : '') + '>' + esc(mk) + '</td>' +
        '<td class="' + (bidCell ? 'bid' : '') + (r.is_best_bid ? ' has-qty clickable' : '') + '"' +
          (r.is_best_bid ? ' title="SELL the spread here (Bid A − Ask B)' + (direction === 'buy_only' ? ' — note: the ALGO only buys; this is manual' : '') + '"' : '') + '>' +
          (bidCell ? bidTxt : '') + '</td>' +
        '<td class="price">' + num(r.level, dig) + '</td>' +
        '<td class="' + (askCell ? 'ask' : '') + (r.is_best_ask ? ' has-qty clickable' : '') + '"' +
          (r.is_best_ask ? ' title="BUY the spread here (Ask A − Bid B)"' : '') + '>' +
          (askCell ? askTxt : '') + '</td></tr>';
    }).join('');
  }

  // SIGNAL & POSITION ----------------------------------------------------------
  BUILD.signal = function (body) {
    body.innerHTML =
      '<div class="sides">' +
      ' <div class="side sell"><div class="lbl">SELL SPREAD <small>Bid A − Ask B</small></div>' +
      '   <div class="px s-px">—</div><div class="zl">Z-SCORE</div><div class="z s-z">—</div>' +
      '   <div class="en s-en"></div><div class="en">short entry · long exit</div></div>' +
      ' <div class="side buy"><div class="lbl">BUY SPREAD <small>Ask A − Bid B</small></div>' +
      '   <div class="px b-px">—</div><div class="zl">Z-SCORE</div><div class="z b-z">—</div>' +
      '   <div class="en b-en"></div><div class="en">long entry · short exit</div></div>' +
      '</div>' +
      '<div class="sec">Position</div><div class="pos-box muted">flat</div>' +
      '<div style="margin-top:6px"><button class="btn pos-close" data-manual data-tip="Close the open position at market">Close position</button>' +
      ' <span class="muted algo-status"></span></div>';
    $('.pos-close', body).onclick = closePosition;
    applyLock();
  };

  REFRESH.signal = function () {
    if (!W.signal || !S.status) { return; }
    var body = W.signal.body, g = S.status.signal || {};
    var entry = Number(g.entry_threshold || 2.5);
    var td = g.trade_direction || 'both';
    function side(cls, px, z, armed, on, enTxt) {
      var box = $('.side.' + cls, body);
      $('.' + cls[0] + '-px', body).textContent = num(px, 2);
      var zEl = $('.' + cls[0] + '-z', body);
      zEl.textContent = z == null ? DASH : signed(z, 2);
      zEl.className = 'z ' + cls[0] + '-z' + (armed && on ? ' hot ' + cls : '');
      var en = $('.' + cls[0] + '-en', body);
      en.textContent = on ? enTxt : 'entries OFF (' + (cls === 'sell' ? 'Buy' : 'Sell') + ' spread only)';
      en.className = 'en ' + cls[0] + '-en' + (on ? '' : ' offline');
      box.className = 'side ' + cls + (armed && on ? ' armed' : '') + (on ? '' : ' off');
    }
    side('sell', g.sell_spread, g.z_sell, g.z_sell != null && g.z_sell >= entry, td !== 'buy_only',
         'short at ≥ +' + entry.toFixed(2));
    side('buy', g.buy_spread, g.z_buy, g.z_buy != null && g.z_buy <= -entry, td !== 'sell_only',
         'long at ≤ −' + entry.toFixed(2));
    var t = S.status.open_trade;
    var pb = $('.pos-box', body);
    if (!t) {
      pb.className = 'pos-box muted'; pb.textContent = 'flat';
    } else {
      var lv = t.spread_levels || {};
      var closePx = t.position_type === 'LONG' ? g.sell_spread : g.buy_spread;
      var dlt = (closePx != null && t.entry_spread != null) ? closePx - t.entry_spread : null;
      var good = dlt == null ? '' : ((t.position_type === 'LONG') === (dlt >= 0) ? 'up' : 'down');
      pb.className = 'pos-box';
      pb.innerHTML =
        '<div style="margin-bottom:4px"><span class="owner ' + String(t.owner || '').toLowerCase() + '">' +
        esc(t.owner || '') + '</span> <b>' + esc(t.position_type) + '</b> qty ' + num(t.quantity, 6) +
        ' · closes on the <b>' + (t.position_type === 'LONG' ? 'Sell' : 'Buy') + '</b> spread</div>' +
        '<div class="kv">' +
        '<span>Entry spread</span><span>' + num(t.entry_spread, 2) + '</span>' +
        '<span>Closing price now</span><span>' + num(closePx, 2) + '</span>' +
        '<span>Δ spread</span><span class="' + good + '">' + signed(dlt, 2) + '</span>' +
        '<span>Net P&amp;L</span><span class="' + (t.unrealized_pnl >= 0 ? 'up' : 'down') + '">' + usd(t.unrealized_pnl) + '</span>' +
        '<span>Levels</span><span>BE ' + num(lv.break_even, 2) + ' · TP ' + num(lv.take_profit, 2) + ' · SL ' + num(lv.stop, 2) + '</span>' +
        '<span>Target / Stop ($)</span><span>' + usd(t.exit_target_usd) + ' / ' + usd(t.exit_stop_usd) + '</span>' +
        '<span>Entry z</span><span>' + (t.entry_zscore == null ? DASH : signed(t.entry_zscore, 2)) + '</span>' +
        '</div>';
    }
    var a = S.algo || {};
    $('.algo-status', body).textContent = a.running ? ('algo: ' + (a.status || '')) : '';
  };

  // STATISTICS & FILTERS -------------------------------------------------------
  BUILD.stats = function (body) {
    body.innerHTML =
      '<div class="sec" style="margin-top:0">Lookback (warm-up)</div>' +
      '<div class="bar st-bar"><i></i></div><div class="muted st-warm" style="margin:3px 0 6px"></div>' +
      '<div class="kv st-kv"></div>' +
      '<div class="sec">Edge filter</div><div class="kv st-edge"></div>' +
      '<div class="sec">Algo</div><div class="st-algo muted"></div>';
  };

  REFRESH.stats = function () {
    if (!W.stats || !S.status) { return; }
    var body = W.stats.body, g = S.status.signal || {};
    var have = g.history_sec || 0, need = g.min_history_sec || 0;
    var pct = need ? Math.min(100, have / need * 100) : 100;
    var bar = $('.st-bar', body);
    bar.classList.toggle('ready', !!g.data_ready);
    bar.firstChild.style.width = (g.data_ready ? 100 : pct) + '%';
    $('.st-warm', body).textContent = g.data_ready
      ? 'Ready — ' + Math.floor(have / 60) + ' min sampled'
      : Math.floor(have / 60) + ' of ' + Math.round(need / 60) + ' min sampled' +
        (g.sample_status ? ' · not sampling: ' + g.sample_status : '');
    $('.st-kv', body).innerHTML =
      '<span>Mean</span><span>' + num(g.spread_mean, 2) + '</span>' +
      '<span>Std dev (σ)</span><span>' + num(g.spread_std, 3) + '</span>' +
      '<span>Half-life</span><span>' + (g.half_life_sec == null ? DASH : Number(g.half_life_sec).toFixed(0) + ' s') + '</span>' +
      '<span>Tick (measured)</span><span>' + (g.measured_interval_sec == null ? DASH : Number(g.measured_interval_sec).toFixed(2) + 's') + '</span>' +
      '<span>Quotes / min</span><span>' + (g.quote_rate_per_min == null ? DASH : g.quote_rate_per_min) + '</span>' +
      '<span>Regime</span><span>' + esc(g.regime || DASH) + '</span>' +
      '<span>Bands</span><span>' + esc(g.band_source === 'candles' ? 'candles · ' + (g.band_timeframe || '') : 'ticks') + '</span>' +
      '<span>Trade direction</span><span>' + esc({both: 'Both', sell_only: 'High → Low only', buy_only: 'Low → High only'}[g.trade_direction] || DASH) + '</span>';
    var ed = g.edge || {};
    var ok = ed.edge_ok;
    $('.st-edge', body).innerHTML =
      '<span>Capture ÷ cost</span><span>' + (ed.edge_multiple == null ? DASH : ed.edge_multiple.toFixed(2) + '×') +
        ' / req ' + (ed.min_edge_multiple == null ? DASH : ed.min_edge_multiple + '×') + '</span>' +
      '<span>Round-trip cost</span><span>' + usd(ed.round_trip_cost_usd) + '</span>' +
      '<span>Expected capture</span><span>' + usd(ed.expected_capture_usd) + '</span>' +
      '<span>Margin / trade</span><span>' + usd(ed.margin_usd, 0) + '</span>' +
      '<span>Status</span><span><span class="pill ' + (ok === true ? 'ok' : ok === false ? 'bad' : '') + '">' +
        (ok === true ? 'OK' : ok === false ? 'BLOCKED' : DASH) + '</span></span>';
    var a = S.algo || {};
    $('.st-algo', body).textContent = (a.running ? 'ON · ' : 'OFF · ') + (a.status || DASH);
  };

  // SPREAD CANDLES + BANDS -------------------------------------------------------
  // The spread's candles with the TradingView BB (EMA basis): candles, EMA(N),
  // EMA ± entry·σ — the current candle included and marked. The timeframe
  // picker is a VIEW; the badge says which bands the algo actually trades.
  var bandsChart = null;
  BUILD.bands = function (body) {
    body.innerHTML =
      '<div class="bb-bar" style="display:flex;gap:6px;align-items:center;font-size:10px;height:20px">' +
      '  <select class="bb-tf" style="font-size:10px"><option value="">algo timeframe</option>' +
      '    <option value="5m">5 min</option><option value="15m">15 min</option>' +
      '    <option value="1h">1 hour</option><option value="4h">4 hours</option></select>' +
      '  <select class="bb-last" style="font-size:10px"><option value="40">40 candles</option>' +
      '    <option value="80">80 candles</option><option value="150">150 candles</option>' +
      '    <option value="300">300 candles</option></select>' +
      '  <span class="pill bb-src" style="margin-left:auto">—</span></div>' +
      '<div style="height:calc(100% - 40px)"><canvas class="bb-c"></canvas></div>' +
      '<div class="bb-note muted" style="font-size:10px;height:18px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis"></div>';
    var tf = $('.bb-tf', body), last = $('.bb-last', body);
    tf.value = prefs.bbTf || '';
    last.value = String(prefs.bbLast || 80);
    tf.onchange = function () { prefs.bbTf = tf.value; save(PREF_KEY, prefs); REFRESH.bands(); };
    last.onchange = function () { prefs.bbLast = Number(last.value); save(PREF_KEY, prefs); REFRESH.bands(); };
    if (!window.Chart) { $('.bb-c', body).parentNode.innerHTML = '<div class="muted">Charts unavailable.</div>'; return; }
    bandsChart = window.SpreadCandlesChart
      ? new Chart($('.bb-c', body), (function () {
          var c = SpreadCandlesChart.config({fontSize: 9, xTicks: 5, maintainAspectRatio: false});
          return c;
        })())
      : null;
  };
  REFRESH.bands = function () {
    if (!W.bands || !bandsChart) { return Promise.resolve(); }
    var body = W.bands.body;
    var q = '/api/candles?last=' + (prefs.bbLast || 80) + (prefs.bbTf ? '&tf=' + prefs.bbTf : '');
    return getJSON(q).then(function (d) {
      var pts = d.points || [], e = Number(d.entry_zscore || 2.5);
      var fmt = function (t) {
        return new Date(t * 1000).toLocaleString([], {day: '2-digit', month: 'short',
          hour: '2-digit', minute: '2-digit', hour12: false});
      };
      var entry = null, t = S.status && S.status.open_trade;
      if (t) { entry = Number(t.entry_spread); }
      SpreadCandlesChart.set(bandsChart, pts, e, (entry != null && isFinite(entry)) ? entry : null, fmt);
      var algoTf = d.algo_timeframe || d.timeframe;
      var src = $('.bb-src', body), onCandles = d.band_source === 'candles';
      src.textContent = onCandles ? 'ALGO USES CANDLES · ' + algoTf : 'ALGO USES TICKS';
      src.className = 'pill bb-src ' + (onCandles && d.timeframe === algoTf ? 'ok' : '');
      src.title = onCandles ? (d.timeframe === algoTf ? 'These are the bands the algo trades on.'
                                                     : 'The algo trades the ' + algoTf + ' bands — this is a view of ' + d.label + '.')
                            : 'The algo uses the tick window. Settings → Signal Parameters → Band source.';
      var b = d.bands || {}, st = (d.status || {})[d.timeframe] || {}, lp = pts[pts.length - 1];
      $('.bb-note', body).textContent = d.label + ' · BB(' + d.length + ', EMA) ±' + e + 'σ · ' +
        (b.ready ? 'EMA ' + num(b.mean, 2) + ' · σ ' + num(b.std, 2) : 'need ' + d.length + ' candles (' + (b.count || 0) + ')') +
        (lp ? ' · now ' + num(lp.close, 2) + ' @ ' + fmt(lp.t) : '') +
        ' · history ' + (st.state || 'pending') + ' · ' + new Date().toLocaleTimeString([], {hour12: false});
      $('.bb-note', body).title = st.detail || '';
    }).catch(function () {});
  };

  // CHARTS ---------------------------------------------------------------------
  var charts = {};
  BUILD.charts = function (body) {
    body.innerHTML = '<div class="ch-bar muted" style="font-size:10px;height:18px">Show last ' +
                     '<select class="ch-range" style="font-size:10px">' +
                     '<option value="300">5 min</option><option value="900">15 min</option>' +
                     '<option value="3600">1 hour</option><option value="0">whole window</option></select>' +
                     ' <span class="ch-note"></span></div>' +
                     '<div style="height:calc(48% - 9px)"><canvas class="c-z"></canvas></div>' +
                     '<div style="height:calc(48% - 9px);margin-top:1%"><canvas class="c-s"></canvas></div>';
    var rng = $('.ch-range', body);
    rng.value = String(prefs.chartSec != null ? prefs.chartSec : 900);
    rng.onchange = function () { prefs.chartSec = Number(rng.value); save(PREF_KEY, prefs); REFRESH.charts(); };
    if (!window.Chart) { body.innerHTML = '<div class="muted">Charts unavailable.</div>'; return; }
    function base() {
      return {animation: false, responsive: true, maintainAspectRatio: false,
              plugins: {legend: {display: false}}, elements: {point: {radius: 0}},
              scales: {x: {display: false}, y: {ticks: {font: {size: 10}}}}};
    }
    charts.z = new Chart($('.c-z', body), {type: 'line', data: {labels: [], datasets: [
      {data: [], borderColor: '#1f7ac2', borderWidth: 1.5},
      {data: [], borderColor: '#b83232', borderDash: [4, 3], borderWidth: 1},
      {data: [], borderColor: '#b83232', borderDash: [4, 3], borderWidth: 1}]}, options: base()});
    charts.s = new Chart($('.c-s', body), {type: 'line', data: {labels: [], datasets: [
      {data: [], borderColor: '#6a4fa3', borderWidth: 1.5}]}, options: base()});
  };
  REFRESH.charts = function () {
    if (!W.charts || !charts.z) { return Promise.resolve(); }
    var sec = prefs.chartSec != null ? prefs.chartSec : 900;
    return getJSON('/api/spread-series?n=600' + (sec ? '&sec=' + sec : '')).then(function (d) {
      var note = $('.ch-note', W.charts.body);
      if (note) { note.textContent = '· updated ' + new Date().toLocaleTimeString(); }
      var z = d.zscores || [], s = d.spreads || [];
      var e = Number(d.entry_zscore || ((S.status || {}).signal || {}).entry_threshold || 2.5);
      var lab = z.map(function (_, i) { return i; });
      charts.z.data.labels = lab;
      charts.z.data.datasets[0].data = z;
      charts.z.data.datasets[1].data = z.map(function () { return e; });
      charts.z.data.datasets[2].data = z.map(function () { return -e; });
      var lim = e + 2.5;
      charts.z.options.scales.y.min = -lim;
      charts.z.options.scales.y.max = lim;
      charts.z.update('none');
      charts.s.data.labels = s.map(function (_, i) { return i; });
      charts.s.data.datasets[0].data = s;
      charts.s.update('none');
    }).catch(function () {});
  };
  function resizeCharts() {
    Object.keys(charts).forEach(function (k) { try { charts[k].resize(); } catch (e) {} });
    if (bandsChart) { try { bandsChart.resize(); } catch (e) {} }
  }

  // MARGIN ---------------------------------------------------------------------
  BUILD.margin = function (body) { body.innerHTML = '<div class="kv mg-acc"></div><div class="sec">Pair margin</div><div class="kv mg-pair"></div><div class="muted mg-err" style="margin-top:4px"></div>'; };
  REFRESH.margin = function () {
    if (!W.margin) { return Promise.resolve(); }
    var body = W.margin.body;
    return getJSON('/api/margin').then(function (m) {
      $('.mg-acc', body).innerHTML =
        '<span>Total equity</span><span>' + usd(m.cash, 2) + '</span>' +
        '<span>Available margin</span><span>' + usd(m.available, 2) + '</span>' +
        '<span>Margin used</span><span>' + usd(m.used, 2) + (m.utilisation_pct == null ? '' : ' (' + m.utilisation_pct.toFixed(1) + '%)') + '</span>';
      var p = m.pair || {};
      $('.mg-pair', body).innerHTML =
        '<span>Qty per clip (leg B units)</span><span>' + (p.qty == null ? DASH : num(p.qty, 6)) + '</span>' +
        '<span>Notional per trade</span><span>' + usd(p.notional_usd, 0) + '</span>' +
        '<span>Margin per trade</span><span>' + usd(p.per_trade_usd, 0) + '</span>' +
        '<span>Leverage A / B</span><span>' + (p.spot_leverage || DASH) + '× / ' + (p.futures_leverage || DASH) + '×</span>' +
        '<span>Trades covered</span><span>' + (p.headroom_trades == null ? DASH : p.headroom_trades) + '</span>';
      $('.mg-err', body).textContent = m.error || '';
    }).catch(function () {});
  };

  // FILLS & SLIPPAGE -----------------------------------------------------------
  BUILD.fills = function (body) {
    body.innerHTML = '<div class="kv fl-sum" style="padding:4px 8px;grid-template-columns:repeat(5,auto 1fr)"></div>' +
      '<table class="dtable"><thead><tr><th>Date</th><th>Source</th><th>Trade</th><th>Symbol</th><th>Side</th><th class="num">Qty</th>' +
      '<th>Fired</th><th>Filled</th><th class="num">Took</th><th class="num">Touch</th><th class="num">Fill</th>' +
      '<th class="num">Slip pts</th><th class="num">Slip $</th><th>How</th></tr></thead><tbody></tbody></table>';
  };
  REFRESH.fills = function () {
    if (!W.fills) { return Promise.resolve(); }
    var body = W.fills.body;
    return getJSON('/api/execution').then(function (d) {
      var s = d.stats || {};
      $('.fl-sum', body).innerHTML =
        '<span>Avg slip / leg</span><span class="' + (s.avg_slippage_usd_per_leg > 0 ? 'down' : 'up') + '">' + usd(s.avg_slippage_usd_per_leg) + '</span>' +
        '<span>Setting (bps/leg)</span><span>' + (d.configured_slippage_bps == null ? DASH : d.configured_slippage_bps) + '</span>' +
        '<span>Avg to fill</span><span>' + ms(s.avg_fill_ms) + '</span>' +
        '<span>Leg gap avg/max</span><span>' + ms(s.avg_leg_gap_ms) + ' / ' + ms(s.max_leg_gap_ms) + '</span>' +
        '<span>Orders</span><span>' + (s.orders == null ? DASH : s.orders) + '</span>';
      var rows = [];
      (d.events || []).forEach(function (e) { (e.legs || []).forEach(function (l) { rows.push([e, l]); }); });
      $('tbody', body).innerHTML = rows.slice(0, 300).map(function (x) {
        var e = x[0], l = x[1], sp = l.slippage, c = sp > 0 ? 'down' : sp < 0 ? 'up' : '';
        var how = [String(l.order_type || '').toUpperCase(), l.status ? l.status : '',
                   l.error ? '⚠ ' + l.error : ''].filter(Boolean).join(' · ');
        return '<tr><td>' + esc(e.date || '') + '</td><td><span class="owner ' + (e.source || 'manual') + '">' +
          esc((e.source || 'manual').toUpperCase()) + '</span></td><td>' + (e.label === 'Close' ? 'Exit' : 'Entry') +
          ' <span class="muted">' + esc(e.mode || '') + '</span></td><td>' + esc(l.symbol || DASH) + '</td>' +
          '<td class="' + (String(l.side).toLowerCase() === 'buy' ? '' : 'down') + '"><b>' + esc(String(l.side || '').toUpperCase()) + '</b></td>' +
          '<td class="num">' + (l.qty == null ? DASH : num(l.qty, 6)) + '</td><td>' + clock(l.sent_at) + '</td><td>' + clock(l.filled_at) + '</td>' +
          '<td class="num">' + ms(l.fill_ms) + '</td><td class="num">' + num(l.ref_price, 2) + '</td><td class="num">' + num(l.fill_price, 2) + '</td>' +
          '<td class="num ' + c + '">' + signed(sp, 2) + '</td><td class="num ' + c + '">' + usd(l.slippage_usd) + '</td>' +
          '<td class="muted">' + esc(how) + '</td></tr>';
      }).join('') || '<tr><td colspan="14" class="muted" style="text-align:center;padding:10px">No executed orders yet</td></tr>';
    }).catch(function () {});
  };

  // TRADES ---------------------------------------------------------------------
  BUILD.trades = function (body) {
    body.innerHTML = '<div style="padding:4px 8px" class="tr-sum muted"></div>' +
      '<table class="dtable"><thead><tr><th>Closed</th><th>Opened by</th><th>Closed by</th><th>Side</th><th class="num">Qty</th>' +
      '<th class="num">Entry</th><th class="num">Exit</th><th class="num">Net $</th><th>Reason</th></tr></thead><tbody></tbody></table>';
  };
  REFRESH.trades = function () {
    if (!W.trades) { return Promise.resolve(); }
    var body = W.trades.body;
    return getJSON('/api/desk/trades').then(function (d) {
      $('.tr-sum', body).innerHTML = (d.count || 0) + ' closed · total ' +
        '<b class="' + (d.total_pnl >= 0 ? 'up' : 'down') + '">' + usd(d.total_pnl) + '</b>' +
        (d.open ? ' · <span class="owner ' + (d.open.source || 'manual') + '">' + esc((d.open.source || 'manual').toUpperCase()) +
                  '</span> ' + esc(String(d.open.direction || '').replace('_SPREAD', '')) + ' open' : '');
      $('tbody', body).innerHTML = (d.trips || []).map(function (r) {
        var np = Number(r.net_pnl || 0);
        var es = r.entry_source || 'manual', xs = r.exit_source || 'manual';
        return '<tr><td>' + esc(r.time || DASH) + '</td>' +
          '<td><span class="owner ' + es + '">' + es.toUpperCase() + '</span></td>' +
          '<td><span class="owner ' + xs + '">' + xs.toUpperCase() + '</span></td>' +
          '<td>' + esc(String(r.direction || '').replace('_SPREAD', '')) + '</td><td class="num">' + (r.qty == null ? DASH : num(r.qty, 6)) + '</td>' +
          '<td class="num">' + num(r.entry_spread, 2) + '</td><td class="num">' + num(r.exit_spread, 2) + '</td>' +
          '<td class="num ' + (np >= 0 ? 'up' : 'down') + '">' + usd(np) + '</td><td class="muted">' + esc(r.exit_reason || DASH) + '</td></tr>';
      }).join('') || '<tr><td colspan="9" class="muted" style="text-align:center;padding:10px">No completed trades yet</td></tr>';
    }).catch(function () {});
  };

  // ORDER LOG ------------------------------------------------------------------
  BUILD.orders = function (body) {
    body.innerHTML = '<div style="padding:4px 8px"><button class="btn ol-refresh">Refresh</button> <span class="muted ol-note"></span></div>' +
      '<table class="dtable"><thead><tr><th>Time</th><th>Symbol</th><th>Type</th><th>Side</th><th>Pos</th><th>Ord</th>' +
      '<th class="num">Qty</th><th class="num">Filled</th><th class="num">Avg</th><th class="num">Fee</th><th>Status</th><th>Order ID</th></tr></thead><tbody></tbody></table>';
    $('.ol-refresh', body).onclick = function () { REFRESH.orders(true); };
  };
  REFRESH.orders = function (force) {
    if (!W.orders || !force) { return Promise.resolve(); }
    var body = W.orders.body;
    return getJSON('/api/exchange-orders?limit=60', true).then(function (d) {
      $('.ol-note', body).textContent = (d.orders || []).length + ' order(s)';
      $('tbody', body).innerHTML = (d.orders || []).map(function (o) {
        var side = String(o.side || '').toUpperCase();
        var t = o.created_at ? new Date(Number(o.created_at)).toLocaleTimeString() : (o.time || DASH);
        return '<tr><td>' + esc(t) + '</td><td>' + esc(o.symbol || DASH) + '</td><td>' + esc(o.inst_type || DASH) + '</td>' +
          '<td class="' + (side.indexOf('B') === 0 ? '' : 'down') + '"><b>' + esc(side) + '</b></td><td>' + esc(o.pos_side || DASH) + '</td>' +
          '<td>' + esc(o.order_type || DASH) + '</td><td class="num">' + (o.quantity == null ? DASH : o.quantity) + '</td>' +
          '<td class="num">' + (o.fill_qty || DASH) + '</td><td class="num">' + num(o.fill_price || null, 2) + '</td>' +
          '<td class="num">' + (o.fee == null ? DASH : Number(o.fee).toFixed(4)) + '</td><td>' + esc(o.state || o.status || DASH) + '</td>' +
          '<td class="muted">' + esc(String(o.order_id || '').slice(-10)) + '</td></tr>';
      }).join('') || '<tr><td colspan="12" class="muted" style="text-align:center;padding:10px">No orders</td></tr>';
    }).catch(function (e) { toast('Order log: ' + e, 'danger'); });
  };

  // ── polling ────────────────────────────────────────────────────────────
  function refreshAll() {
    pollAlgo();
    pollStatus();
    REFRESH.ladder();
    setTimeout(function () { REFRESH.margin(); REFRESH.fills(); REFRESH.trades(); }, 1200);
  }
  setInterval(pollStatus, 1000);
  setInterval(pollAlgo, 2000);
  setInterval(function () { REFRESH.ladder(); }, 700);
  setInterval(function () { REFRESH.charts(); }, 2000);
  setInterval(function () { REFRESH.bands(); }, 3000);
  setInterval(function () { REFRESH.margin(); }, 15000);
  setInterval(function () { REFRESH.fills(); REFRESH.trades(); }, 10000);
  setInterval(renderBanners, 1000);

  // ── start: reopen what was open last time, else the default set ────────
  DEFS.forEach(function (d) {
    var L = layout[d.id];
    var open = L && L.open != null ? L.open : !d.closed;
    if (open) { openWindow(d.id); }
  });
  renderTabs();
  refreshAll();
  REFRESH.charts();
  REFRESH.bands();
  REFRESH.orders(true);
})();
