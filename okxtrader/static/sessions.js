/* Market sessions: when to expect the market to move.
 *
 * Crypto never closes, but it does not move evenly: volume and
 * volatility come with the big equity centres' opens - New York's
 * above all - and every perpetual on OKX settles FUNDING at 00:00,
 * 08:00 and 16:00 UTC, every day including weekends, when the basis
 * between a perp and its spot or future visibly snaps. The strip on
 * the top bar counts down to each, coloured by how close it is;
 * "Sessions" opens a 24-hour timeline; ten minutes before each event a
 * chime and a pop-up say so.
 *
 * DISPLAY ONLY. Nothing here reads the engine or sends anything. The
 * hours are each exchange's own, in its own city's time zone, so a
 * daylight-saving change in New York or London moves the countdown by
 * itself. The centres are weekdays only (exchange holidays are not
 * known here); funding is every day.
 */
(function () {
  'use strict';

  //: [name, time zone, open, close, which ends to alert on]
  //: An `events` entry has no session: instants, every day, in UTC.
  var MARKETS = [
    {name: 'Tokyo', zone: 'Asia/Tokyo', open: '09:00', close: '15:30',
     alert: ['open']},
    {name: 'Hong Kong', zone: 'Asia/Hong_Kong', open: '09:30',
     close: '16:00', alert: ['open']},
    {name: 'London', zone: 'Europe/London', open: '08:00', close: '16:30',
     alert: ['open']},
    {name: 'New York', zone: 'America/New_York', open: '09:30',
     close: '16:00', alert: ['open', 'close']},
    {name: 'OKX funding', zone: 'UTC', events: ['00:00', '08:00', '16:00'],
     alert: ['event']}
  ];
  var SOON_MIN = 30;        // amber: opens or closes within this
  var HOT_MIN = 30;         // red: opened within this - the volatile part
  var ALERT_MIN = 10;       // chime + pop-up this long before

  var DAYS = {Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6};

  function minutes(hhmm) {
    var parts = hhmm.split(':');
    return Number(parts[0]) * 60 + Number(parts[1]);
  }

  function wall(now, zone) {
    /* The city's own weekday and minutes since its midnight, now. */
    var parts = {};
    new Intl.DateTimeFormat('en-GB', {
      timeZone: zone, weekday: 'short', hour: '2-digit', minute: '2-digit',
      second: '2-digit', hourCycle: 'h23'
    }).formatToParts(now).forEach(function (p) { parts[p.type] = p.value; });
    return {day: DAYS[parts.weekday],
            min: Number(parts.hour) * 60 + Number(parts.minute) +
              Number(parts.second) / 60,
            text: parts.weekday + ' ' + parts.hour + ':' + parts.minute};
  }

  function untilNext(at, target) {
    /* Minutes from now to the next weekday `target` (minutes) in the
     * city - today if it is still ahead, else the next trading day. */
    for (var off = 0; off < 8; off++) {
      var day = (at.day + off) % 7;
      if (day === 0 || day === 6) { continue; }
      var delta = off * 1440 + target - at.min;
      if (delta > 0) { return delta; }
    }
    return null;
  }

  function nextEvent(market, now) {
    /* Minutes to the next of an `events` market's instants, any day,
     * and which one it is. */
    var at = wall(now, market.zone);
    var best = null;
    market.events.forEach(function (hhmm) {
      var delta = minutes(hhmm) - at.min;
      if (delta <= 0) { delta += 1440; }
      if (best === null || delta < best.at) { best = {at: delta, hhmm: hhmm}; }
    });
    return best;
  }

  function status(market, now) {
    /* One market, now: open or not, and what comes next, in words. */
    if (market.events) {
      var next = nextEvent(market, now);
      return {name: market.name, local: next.hhmm + ' UTC', open: false,
              event: true, toOpen: next.at, at: next.hhmm,
              tone: next.at <= SOON_MIN ? 'soon' : 'off'};
    }
    var at = wall(now, market.zone);
    var open = minutes(market.open), close = minutes(market.close);
    var weekday = at.day >= 1 && at.day <= 5;
    var isOpen = weekday && at.min >= open && at.min < close;
    var row = {name: market.name, local: at.text, open: isOpen};
    if (isOpen) {
      row.sinceOpen = at.min - open;
      row.toClose = close - at.min;
      row.toOpen = untilNext(at, open);
      row.tone = row.sinceOpen < HOT_MIN ? 'hot'
        : row.toClose <= SOON_MIN ? 'soon' : 'open';
    } else {
      row.toOpen = untilNext(at, open);
      row.tone = row.toOpen !== null && row.toOpen <= SOON_MIN ? 'soon'
        : 'off';
    }
    return row;
  }

  function span(min) {
    if (min === null || min === undefined) { return '—'; }
    var m = Math.max(0, Math.ceil(min - 1e-9));
    var h = Math.floor(m / 60);
    return h ? h + 'h ' + (m % 60) + 'm' : (m % 60) + 'm';
  }

  function words(row, market) {
    if (row.event) { return 'in ' + span(row.toOpen); }
    if (!row.open) { return 'opens in ' + span(row.toOpen); }
    if (row.tone === 'hot') {
      return 'JUST OPENED · ' + (market.closeWord || 'closes') + ' in ' +
        span(row.toClose);
    }
    return 'OPEN · ' + (market.closeWord || 'closes') + ' in ' +
      span(row.toClose);
  }

  function headline(rows) {
    /* The one thing to know right now. */
    var hot = rows.filter(function (r) { return r.tone === 'hot'; });
    if (hot.length) {
      var h = hot[0];
      return h.name + ' opened ' + span(h.sinceOpen) +
        ' ago — expect volatility';
    }
    var events = [];
    rows.forEach(function (r, i) {
      if (r.event) {
        events.push({at: r.toOpen, text: r.name + ' (' + r.at + ' UTC) in ' +
                     span(r.toOpen)});
      } else if (!r.open) {
        events.push({at: r.toOpen, text: r.name + ' opens in ' +
                     span(r.toOpen)});
      } else if (MARKETS[i].alert.indexOf('close') >= 0) {
        events.push({at: r.toClose, text: r.name + ' ' +
                     (MARKETS[i].closeWord || 'closes') + ' in ' +
                     span(r.toClose)});
      }
    });
    events = events.filter(function (e) { return e.at !== null; });
    events.sort(function (a, b) { return a.at - b.at; });
    return events.length ? 'Next: ' + events[0].text : '';
  }

  function snapshot(now) {
    var rows = MARKETS.map(function (m) { return status(m, now); });
    return {rows: rows, headline: headline(rows)};
  }

  // -- alerts ---------------------------------------------------------------

  var said = {};

  function alertKey(name, what, now) {
    return name + '|' + what + '|' + now.toISOString().slice(0, 10);
  }

  function remembered(key) {
    if (said[key]) { return true; }
    try { return window.localStorage.getItem('mkt:' + key) === '1'; }
    catch (e) { return false; }
  }

  function remember(key) {
    said[key] = true;
    try { window.localStorage.setItem('mkt:' + key, '1'); } catch (e) { /* */ }
  }

  function due(now) {
    /* The alerts to say now: within ALERT_MIN of an open, or of a close
     * a market alerts on - each once a day, never again on a reload. */
    var out = [];
    MARKETS.forEach(function (market) {
      var row = status(market, now);
      if (row.event) {
        if (row.toOpen <= ALERT_MIN) {
          out.push({key: alertKey(market.name, row.at, now),
                    text: market.name + ' in ' + span(row.toOpen) + ' (' +
                      row.at + ' UTC) — perps pay or receive funding, and ' +
                      'the basis can snap'});
        }
        return;
      }
      if (market.alert.indexOf('open') >= 0 && !row.open &&
          row.toOpen !== null && row.toOpen <= ALERT_MIN) {
        out.push({key: alertKey(market.name, 'open', now),
                  text: market.name + ' opens in ' + span(row.toOpen) +
                    ' — expect volatility'});
      }
      if (market.alert.indexOf('close') >= 0 && row.open &&
          row.toClose <= ALERT_MIN) {
        out.push({key: alertKey(market.name, 'close', now),
                  text: market.name + ' ' + (market.closeWord || 'closes') +
                    ' in ' + span(row.toClose) + ' (' + market.close +
                    ' local) — expect volatility'});
      }
    });
    return out.filter(function (a) { return !remembered(a.key); });
  }

  // -- the screen -------------------------------------------------------------

  function esc(text) {
    return String(text).replace(/[&<>"]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
    });
  }

  function clockLine(now) {
    function hm(date, zone) {
      return new Intl.DateTimeFormat('en-GB', {
        timeZone: zone, hour: '2-digit', minute: '2-digit',
        hourCycle: 'h23'}).format(date);
    }
    var local = new Intl.DateTimeFormat('en-GB', {
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      weekday: 'short'}).format(now);
    return 'This PC ' + local + ' · UTC ' + hm(now, 'UTC');
  }

  function renderStrip(now) {
    var node = document.getElementById('mkt-strip');
    if (!node) { return; }
    var snap = snapshot(now);
    var head = snap.headline;
    var hot = snap.rows.some(function (r) { return r.tone === 'hot'; });
    node.innerHTML =
      (head ? '<span class="mkt-head' + (hot ? ' hot' : '') + '">' +
        esc(head) + '</span>' : '') +
      snap.rows.map(function (r, i) {
        return '<span class="mkt-chip ' + r.tone + '" title="' +
          esc(MARKETS[i].events
            ? MARKETS[i].name + ': ' + MARKETS[i].events.join(', ') +
              ' UTC, every day'
            : MARKETS[i].name + ': ' + MARKETS[i].open + '–' +
              MARKETS[i].close + ' local, weekdays') + '"><b>' +
          esc(r.name) + '</b> ' + esc(r.local) + ' · ' +
          esc(words(r, MARKETS[i])) + '</span>';
      }).join('') +
      '<button type="button" class="mkt-more">Sessions ▸</button>';
  }

  function renderPanel(now) {
    var panel = document.getElementById('mkt-panel');
    if (!panel || panel.hidden) { return; }
    var utcMin = now.getUTCHours() * 60 + now.getUTCMinutes();
    var rows = MARKETS.map(function (market) {
      if (market.events) {
        var marks = market.events.map(function (hhmm) {
          return '<div class="mkt-bar" style="left:' +
            (minutes(hhmm) / 14.4) + '%;width:0.6%"></div>';
        }).join('');
        return '<div class="mkt-row"><div class="mkt-name"><b>' +
          esc(market.name) + '</b><br><small>' +
          market.events.join(' · ') + ' UTC, daily</small></div>' +
          '<div class="mkt-track">' + marks + '</div></div>';
      }
      // Where the session sits on TODAY's UTC clock: the local hours,
      // moved by the city's offset now.
      var at = wall(now, market.zone);
      var shift = ((at.min - utcMin) % 1440 + 1440 + 720) % 1440 - 720;
      var start = ((minutes(market.open) - shift) % 1440 + 1440) % 1440;
      var length = minutes(market.close) - minutes(market.open);
      var parts = start + length <= 1440 ? [[start, length]]
        : [[start, 1440 - start], [0, start + length - 1440]];
      var bars = parts.map(function (p) {
        return '<div class="mkt-bar" style="left:' + (p[0] / 14.4) +
          '%;width:' + (p[1] / 14.4) + '%"></div>';
      }).join('');
      return '<div class="mkt-row"><div class="mkt-name"><b>' +
        esc(market.name) + '</b><br><small>' + market.open + '–' +
        market.close + ' local</small></div><div class="mkt-track">' +
        bars + '</div></div>';
    }).join('');
    var ticks = '';
    for (var h = 0; h <= 24; h += 3) {
      ticks += '<span style="left:' + (h / 24 * 100) + '%">' +
        (h < 10 ? '0' : '') + h + ':00</span>';
    }
    panel.querySelector('.mkt-body').innerHTML =
      '<div class="mkt-clock">' + esc(clockLine(now)) +
        ' · bars on UTC</div>' +
      '<div class="mkt-ticks">' + ticks + '</div>' +
      '<div class="mkt-rows">' + rows +
        '<div class="mkt-now" style="left:calc(150px + (100% - 150px) * ' +
        (utcMin / 1440) + ')"><span>NOW</span></div></div>' +
      '<div class="mkt-note">Alerts: a chime and a pop-up ' + ALERT_MIN +
        ' minutes before each open, before New York’s 16:00 close, and ' +
        'before every OKX funding settlement (00:00, 08:00, 16:00 UTC, ' +
        'every day). The centres are weekdays only - exchange holidays ' +
        'are not known here.</div>';
  }

  function tick() {
    var now = api.now();
    renderStrip(now);
    renderPanel(now);
    var trader = window.MT5Trader || {};
    due(now).forEach(function (alert) {
      remember(alert.key);
      if (trader.toast) { trader.toast(alert.text); }
      if (trader.sound) { trader.sound('session'); }
    });
  }

  function start() {
    document.addEventListener('click', function (event) {
      var target = event.target;
      if (target && target.classList &&
          target.classList.contains('mkt-more')) {
        var panel = document.getElementById('mkt-panel');
        if (panel) { panel.hidden = !panel.hidden; tick(); }
      }
      if (target && target.classList &&
          target.classList.contains('mkt-close')) {
        document.getElementById('mkt-panel').hidden = true;
      }
    });
    tick();
    window.setInterval(tick, 1000);
  }

  var api = {
    MARKETS: MARKETS, snapshot: snapshot, status: status, due: due,
    tick: tick, span: span,
    // The clock, replaceable so a test can stand at any moment.
    now: function () { return new Date(); }
  };
  window.MT5Sessions = api;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
