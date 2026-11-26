/* THE ECONOMIC CALENDAR: the high-impact US releases on the desk.
 *
 * CPI, the jobs report and PPI from BLS; jobless claims from DOL; crude
 * inventories from EIA - each figure the moment the agency publishes
 * it, beside the last one and a forecast the trader may type (no agency
 * publishes forecasts). A countdown chip on the taskbar, a box and a
 * sound when a figure lands, and what the spreads did in the five
 * minutes after.
 *
 * Display only: nothing here changes how the Algo trades.
 */

(function () {
  'use strict';

  var UI = window.MT5Trader;
  var DASH = '—';
  var local = {seen: null, warned: {}, keys: null, error: null};

  function escape(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function calendar() {
    return (UI.state.snapshot || {}).calendar || null;
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  var DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug',
                'Sep', 'Oct', 'Nov', 'Dec'];

  function when(at) {
    var d = new Date(at * 1000);
    return DAYS[d.getUTCDay()] + ' ' + d.getUTCDate() + ' ' +
      MONTHS[d.getUTCMonth()] + ' ' + pad(d.getUTCHours()) + ':' +
      pad(d.getUTCMinutes());
  }

  function hhmm(at) {
    var d = new Date(at * 1000);
    return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
  }

  function countdown(seconds) {
    if (seconds <= 0) { return 'now'; }
    var m = Math.ceil(seconds / 60);
    if (m < 60) { return m + ' min'; }
    var h = Math.floor(m / 60);
    if (h < 24) { return h + 'h ' + (m % 60) + 'm'; }
    return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
  }

  function nowSec(cal) {
    // The engine's clock as of the snapshot - a few hundred ms old.
    return cal && cal.now ? cal.now : Date.now() / 1000;
  }

  function status(event, now) {
    if (event.status === 'released') { return 'out'; }
    if (event.status === 'due') { return 'awaiting'; }
    if (event.status === 'missed') { return 'not found'; }
    return countdown(event.at - now);
  }

  // -- the taskbar chip, and the box when a figure lands ------------------

  function tick() {
    var cal = calendar();
    if (!cal) { return; }
    // A release ABOUT TO LAND: one box, once, when it is within the
    // alert time (15 min) - nothing kept on the taskbar.
    var now = nowSec(cal);
    var lead = (cal.alert_min || 15) * 60;
    (cal.events || []).forEach(function (e) {
      if (e.status !== 'upcoming' || local.warned[e.id]) { return; }
      var ahead = e.at - now;
      if (ahead <= 0 || ahead > lead) { return; }
      local.warned[e.id] = true;
      UI.toast('\u2605\u2605\u2605 US ' + e.name + ' (' + e.agency + ') in ' +
        countdown(ahead) + ', at ' + hhmm(e.at) + ' UTC \u00b7 ' +
        (e.figures || []).map(function (f) {
          return f.name + ' previous ' + (f.previous || DASH) +
            (f.forecast ? ', forecast ' + f.forecast : '');
        }).join(' \u00b7 '), 'ok');
      if (UI.sound) { UI.sound('placed'); }
    });
    if (!cal) { return; }
    // A NEW release: a box and a sound. The first look only takes its
    // bearings - figures already out when the page opened are not news.
    var released = (cal.events || []).filter(function (e) {
      return e.status === 'released';
    });
    if (local.seen === null) {
      local.seen = {};
      released.forEach(function (e) { local.seen[e.id] = true; });
      return;
    }
    released.forEach(function (e) {
      if (local.seen[e.id]) { return; }
      local.seen[e.id] = true;
      var f = (e.figures || [])[0] || {};
      UI.toast('RELEASED · US ' + ((e.figures || []).length === 1
        ? '' : e.name + ': ') + (e.figures || [])
        .map(function (x) {
          return x.name + ' ' + (x.actual || DASH) + ' (previous ' +
            (x.previous || DASH) + (x.forecast ? ', forecast ' + x.forecast
              : '') + ')';
        }).join(' · ') + (f.surprise ? ' · ' + f.surprise : ''),
        'ok');
      if (UI.sound) { UI.sound('filled'); }
    });
  }

  // -- the window -----------------------------------------------------------

  function node() {
    var existing = document.querySelector('.window.econcal');
    if (existing) { return existing; }
    var panel = document.createElement('section');
    panel.className = 'window econcal';
    panel.innerHTML =
      '<div class="titlebar"><span class="swatch"></span>' +
      '<span class="title">Economic calendar</span><span class="winbtns"><button class="winbtn close">' +
      '&times;</button></span></div><div class="ec-body"></div>' +
      '<div class="note">Official sources only: BLS (CPI, jobs, PPI), DOL ' +
      '(jobless claims), EIA (crude inventories). Times in UTC. A figure ' +
      'is shown the moment the agency publishes it. Display only: ' +
      'nothing here changes how the Algo trades.</div>';
    panel.querySelector('.close').addEventListener('click', function () {
      UI.closePanel(UI.panelId('econcal'));
    });
    panel.addEventListener('change', onChange);
    panel.addEventListener('click', onClick);
    document.getElementById('desktop').appendChild(panel);
    loadKeys();
    return panel;
  }

  function loadKeys() {
    fetch('/api/calendar/keys').then(function (r) { return r.json(); })
      .then(function (body) { local.keys = body.keys || {}; draw(true); })
      .catch(function () {});
  }

  function draw(force) {
    var panel = document.querySelector('.window.econcal');
    if (!panel) { return; }
    // Never redraw under a half-typed forecast or key.
    var active = document.activeElement;
    if (!force && active && panel.contains(active) &&
        active.tagName === 'INPUT') { return; }
    panel.querySelector('.ec-body').innerHTML = html();
  }

  function figureName(e, f, i) {
    // One figure: its own name says it. Several: the release, then each.
    if ((e.figures || []).length === 1) {
      return '<b>US ' + escape(f.name) + '</b>';
    }
    return (i ? '' : '<b>US ' + escape(e.name) + '</b> · ') +
      escape(f.name);
  }

  function card(kind, label, big, small) {
    return '<div class="ec-card ' + kind + '"><div class="ec-k">' + label +
      '</div><div class="ec-big">' + big + '</div><div>' + small +
      '</div></div>';
  }

  function figuresLine(e) {
    return (e.figures || []).map(function (f) {
      return escape(f.name) + ' ' + (f.forecast ? 'forecast ' +
        escape(f.forecast) + ' · ' : '') + 'previous ' +
        escape(f.previous || DASH);
    }).join('<br>');
  }

  function spreadMoves(e) {
    var rows = [];
    Object.keys(e.spreads || {}).forEach(function (key) {
      var s = e.spreads[key];
      if (s.at === undefined) { return; }
      rows.push(escape(s.name) + ' <b>' + UI.fmt(s.at, 3) + '</b>' +
        (s.after !== undefined ? ' → <b>' + UI.fmt(s.after, 3) +
          '</b> (' + (s.move > 0 ? '+' : '') + UI.fmt(s.move, 3) + ')'
          : ' → measuring 5 min'));
    });
    return rows.join('<br>') || DASH;
  }

  function html() {
    var cal = calendar();
    if (!cal) {
      return '<p class="hint">The calendar is off, or the engine has not ' +
        'read it yet (CALENDAR_ON in the settings).</p>';
    }
    var now = nowSec(cal);
    var next = cal.next;
    var last = cal.last;
    var out = '<div class="ec-cards">';
    out += next ? card('next', 'Next · ' + escape(next.agency),
      'US ' + escape(next.name), hhmm(next.at) + ' UTC · <b class="' +
      'down">' + (next.status === 'due' ? 'awaiting figures'
        : 'in ' + countdown(next.at - now)) + '</b><br>' +
      figuresLine(next))
      : card('next', 'Next', DASH, 'nothing in the next week');
    out += last ? card('out', 'Just released · ' + hhmm(last.at) +
      ' UTC', 'US ' + escape(last.name), (last.figures || []).map(
        function (f) {
          return escape(f.name) + ' <b class="up">' + escape(f.actual ||
            DASH) + '</b> (previous ' + escape(f.previous || DASH) +
            (f.forecast ? ', forecast ' + escape(f.forecast) : '') + ')' +
            (f.surprise ? ' · <b>' + escape(f.surprise) + '</b>' : '');
        }).join('<br>'))
      : card('out', 'Just released', DASH, 'nothing this week yet');
    out += card('move', 'What the spreads did · 5 min after',
      last ? 'US ' + escape(last.name) : DASH,
      last ? spreadMoves(last) : DASH);
    out += '</div>';

    out += '<table class="an-table ec-table"><thead><tr>' +
      '<th>Time (UTC)</th><th>In</th><th class="l">Source</th>' +
      '<th class="l">Release</th><th>Impact</th><th>Actual</th>' +
      '<th title="Typed by you (from investing.com, say): no agency ' +
      'publishes a forecast. The surprise is worked out against it.">' +
      'Forecast</th><th>Previous</th><th class="l">Surprise</th>' +
      '<th title="The first ladder’s mid spread, from the release to ' +
      'five minutes after">Spread 5 min after</th></tr></thead><tbody>';
    var events = (cal.events || []).filter(function (e) {
      return e.at >= now - 3 * 86400;
    });
    if (!events.length) {
      out += '<tr><td colspan="10" class="hint">No release in the window ' +
        '— the schedule is read from BLS and the weekly rules for ' +
        'DOL and EIA.</td></tr>';
    }
    events.forEach(function (e) {
      var cls = e.status === 'released' && last && e.id === last.id
        ? 'ec-new' : (e.status === 'upcoming' || e.status === 'due') &&
        e.at - now <= (cal.alert_min || 15) * 60 ? 'ec-soon'
          : e.status === 'released' ? 'ec-done' : '';
      var moveKey = Object.keys(e.spreads || {})[0];
      var move = moveKey ? e.spreads[moveKey].move : undefined;
      (e.figures || []).forEach(function (f, i) {
        out += '<tr class="' + cls + '">' +
          '<td>' + (i ? '' : when(e.at)) + '</td>' +
          '<td>' + (i ? '' : (e.status === 'upcoming' && e.at - now <=
            (cal.alert_min || 15) * 60 ? '<b class="down">' +
            status(e, now) + '</b>' : status(e, now))) + '</td>' +
          '<td class="l">' + (i ? '' : escape(e.agency)) + '</td>' +
          '<td class="l">' + figureName(e, f, i) + '</td>' +
          '<td class="ec-star">★★★</td>' +
          '<td><b>' + escape(f.actual || DASH) + '</b></td>' +
          '<td><input class="ec-fc" data-event="' + escape(e.id) +
            '" data-figure="' + escape(f.key) + '" value="' +
            escape(f.forecast || '') + '" placeholder="' + (e.status ===
            'released' ? DASH : 'type') + '" title="Your forecast for ' +
            escape(f.name) + ', e.g. 0.3%, 221K or -0.6M"></td>' +
          '<td>' + escape(f.previous || DASH) + '</td>' +
          '<td class="l">' + escape(f.surprise || DASH) + '</td>' +
          '<td>' + (i || move === undefined ? '' : (move > 0 ? '+' : '') +
            UI.fmt(move, 3)) + '</td></tr>';
      });
    });
    out += '</tbody></table>';

    // Where the figures come from, and the two free keys.
    var sources = cal.sources || {};
    var errors = cal.errors || {};
    var keys = local.keys || {};
    out += '<div class="ec-sources">' + ['BLS', 'DOL', 'EIA'].map(
      function (name) {
        var bad = errors[name] || errors[name + ' calendar'];
        return '<span class="' + (bad ? 'down' : '') + '"><b>' + name +
          '</b> ' + escape(bad || sources[name] || DASH) + '</span>';
      }).join(' · ') + '</div>';
    out += '<div class="ec-keys">' +
      '<label title="Free from bls.gov/developers: 500 reads a day, so a ' +
      'release is read every 10 seconds">BLS key <input type="password" ' +
      'class="ec-key" data-key="BLS_API_KEY" autocomplete="new-password" ' +
      'placeholder="' + (keys.BLS_API_KEY ? '•••• ' +
        'saved' : 'optional, free') + '"></label> ' +
      '<label title="Free from eia.gov/opendata: needed for crude ' +
      'inventories">EIA key <input type="password" class="ec-key" ' +
      'data-key="EIA_API_KEY" autocomplete="new-password" placeholder="' +
      (keys.EIA_API_KEY ? '•••• saved'
        : 'needed for crude') + '"></label> ' +
      '<button class="ec-save-keys">Save keys</button>' +
      (local.error ? ' <span class="down">' + escape(local.error) +
        '</span>' : '') +
      ' <span class="hint">Telegram warns ' + (cal.alert_min || 15) +
      ' min before each release and again when it lands.</span></div>';
    return out;
  }

  function onChange(event) {
    var input = event.target;
    if (!input.classList || !input.classList.contains('ec-fc')) { return; }
    fetch('/api/calendar/forecast', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event: input.dataset.event,
                            figure: input.dataset.figure,
                            value: input.value})
    }).then(function (r) { return r.json(); }).then(function (body) {
      if (body.ok === false) { UI.toast(body.error); }
      input.blur();
    });
  }

  function onClick(event) {
    if (!event.target.classList.contains('ec-save-keys')) { return; }
    var panel = document.querySelector('.window.econcal');
    var payload = {};
    Array.prototype.forEach.call(panel.querySelectorAll('.ec-key'),
      function (box) {
        if (box.value.trim()) { payload[box.dataset.key] = box.value.trim(); }
      });
    if (!Object.keys(payload).length) { return; }
    fetch('/api/calendar/keys', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); }).then(function (body) {
      local.error = body.ok === false ? body.error : null;
      if (body.ok !== false) {
        UI.toast('Saved: ' + (body.saved || []).join(', ') +
                 ' — used from the next read', 'ok');
      }
      loadKeys();
    });
  }

  window.MT5Calendar = {
    render: function () { node(); draw(false); },
    tick: tick, html: html, local: local
  };
})();
