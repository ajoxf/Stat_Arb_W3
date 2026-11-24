/* MANAGED ACCOUNTS: the mirror's followers, set up from the screen.
 *
 * Each managed account is an OKX Managed Trading Sub-account bound to
 * this desk. Here the trader adds one, sets how much of each Algo trade
 * it takes and its limits, and pastes its API key - which goes to .env
 * and is never shown again. The engine picks a change up within a few
 * seconds, without a restart (a restart would switch the Algo off).
 *
 * Built for ten or fifteen accounts: one line each, opened out to edit.
 * This window redraws only when something is saved, tested or removed -
 * never under a half-typed field.
 */

(function () {
  'use strict';

  var UI = window.MT5Trader;
  var local = {data: null, open: {}, tests: {}, adding: false,
               busy: false, error: null};

  function escape(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function api(path, options) {
    function failed(status, error) {
      return {ok: false, status: status, body: {error: error}};
    }
    return fetch(path, options).then(function (response) {
      return response.json().then(function (body) {
        return {ok: response.ok && body.ok !== false, status: response.status,
                body: body};
      }, function () {
        return failed(response.status, 'the server answered ' +
          response.status + ' with something that is not JSON');
      });
    }, function (e) {
      return failed(0, 'could not reach the server (' +
        ((e && e.message) || 'connection failed') + ')');
    });
  }

  function post(path, body, method) {
    return api(path, {method: method || 'POST',
                      headers: {'Content-Type': 'application/json'},
                      body: body === undefined ? undefined
                        : JSON.stringify(body)});
  }

  // -- the window ---------------------------------------------------------

  function node() {
    var existing = document.querySelector('.window.managed');
    if (existing) { return existing; }
    var panel = document.createElement('section');
    panel.className = 'window managed';
    panel.innerHTML =
      '<div class="titlebar"><span class="swatch"></span>' +
      '<span class="title">Managed accounts — the Algo’s trades, ' +
      'copied</span><span class="winbtns"><button class="winbtn close">' +
      '&times;</button></span></div><div class="ma-body"></div>' +
      '<div class="note">Each account is an OKX Managed Trading ' +
      'Sub-account bound to this desk, traded with its own API key. ' +
      'Saved changes reach the engine in a few seconds — no restart. ' +
      'Your own trades are never touched.</div>';
    panel.querySelector('.close').addEventListener('click', function () {
      UI.closePanel(UI.panelId('managed'));
    });
    panel.addEventListener('click', onClick);
    document.getElementById('desktop').appendChild(panel);
    load();
    return panel;
  }

  function load() {
    return api('/api/mirror/accounts').then(function (result) {
      local.data = result.ok ? result.body : null;
      local.error = result.ok ? null : result.body.error;
      draw();
    });
  }

  function draw() {
    var panel = document.querySelector('.window.managed');
    if (!panel) { return; }
    panel.querySelector('.ma-body').innerHTML = html();
  }

  // -- what it shows ---------------------------------------------------------

  function field(label, input, hint) {
    return '<label class="ma-field"' + (hint ? ' title="' + escape(hint) +
      '"' : '') + '><span>' + label + '</span>' + input + '</label>';
  }

  function number(name, value, step, hint, placeholder) {
    return '<input type="number" min="0" step="' + step + '" data-f="' +
      name + '" value="' + (value === null || value === undefined ? ''
        : escape(value)) + '" placeholder="' + escape(placeholder || 'off') +
      '"' + (hint ? ' title="' + escape(hint) + '"' : '') + '>';
  }

  function keyInput(name, saved, label) {
    return '<input type="password" autocomplete="new-password" data-f="' +
      name + '" placeholder="' + (saved ? '•••• saved — ' +
        'blank keeps it' : 'not set') + '" aria-label="' + label + '">';
  }

  function formHtml(a, isNew) {
    var pairs = (local.data && local.data.pairs) || [];
    var chosen = a.pairs || [];
    return '<div class="ma-form" data-name="' + escape(a.name || '') + '">' +
      (isNew ? field('Name', '<input type="text" data-f="name" ' +
        'maxlength="40" placeholder="e.g. client_a">',
        'letters, digits, spaces, - or _') : '') +
      '<div class="ma-grid">' +
      field('Account', '<select data-f="demo"><option value="1"' +
        (a.demo !== false ? ' selected' : '') + '>Demo</option>' +
        '<option value="0"' + (a.demo === false ? ' selected' : '') +
        '>LIVE</option></select>', 'Demo trades OKX’s demo; LIVE ' +
        'trades real money') +
      field('Multiple ×', number('multiple', a.multiple, '0.01',
        'how much of each Algo trade it takes: 0.5 = half the lots of ' +
        'each leg', 'e.g. 0.5')) +
      field('Capital $', number('capital', a.capital, '100',
        'the client’s capital, for returns on it', 'not set')) +
      field('Execution', '<select data-f="execution"><option value="MIRROR"' +
        (a.execution !== 'TAKER' ? ' selected' : '') + '>As the trade ' +
        '(maker / taker)</option><option value="TAKER"' +
        (a.execution === 'TAKER' ? ' selected' : '') + '>Always taker' +
        '</option></select>', 'As the trade: a maker trade is copied as ' +
        'maker. Always taker: every copy crosses') +
      field('Maker wait (s)', number('maker_timeout_sec',
        a.maker_timeout_sec, '1', 'how long a maker leg rests: on entry ' +
        'the copy is skipped if unfilled; on exit the rest crosses', '30')) +
      field('Profit payout %', number('profit_share_pct', a.profit_share_pct,
        '0.5', 'your payout as fund manager: this % of each month\u2019s ' +
        'profit (a losing month pays none)', 'none')) +
      field('Following', '<select data-f="enabled"><option value="1"' +
        (a.enabled !== false ? ' selected' : '') + '>Yes</option>' +
        '<option value="0"' + (a.enabled === false ? ' selected' : '') +
        '>No — closes only</option></select>') +
      '</div><div class="ma-sub">Limits <small>(blank = off; each stops ' +
      'NEW copies only — a copy on always closes)</small></div>' +
      '<div class="ma-grid">' +
      field('Max margin $', number('max_margin', a.max_margin, '10',
        'margin all its open copies may tie up')) +
      field('Daily loss limit $', number('daily_loss_limit',
        a.daily_loss_limit, '10', 'no new copy today once today’s ' +
        'closed loss reaches it')) +
      field('Max trades / day', number('max_trades_day', a.max_trades_day,
        '1', 'copies a day')) +
      field('Ladders <small>(none chosen = all)</small>',
        '<select data-f="pairs" multiple size="' +
        Math.min(4, Math.max(2, pairs.length)) + '">' +
        pairs.map(function (p) {
          return '<option value="' + escape(p.key) + '"' +
            (chosen.indexOf(p.key) >= 0 ? ' selected' : '') + '>' +
            escape(p.name) + '</option>';
        }).join('') + '</select>', 'none chosen = every ladder') +
      '</div><div class="ma-sub">API key <small>(from OKX: Profile → ' +
      'API → Client accounts → Create API key, with Trade. ' +
      'Saved to .env, never shown again)</small></div><div class="ma-grid">' +
      field('API key', keyInput('api_key', a.key_set, 'API key')) +
      field('Secret', keyInput('secret', a.secret_set, 'Secret')) +
      field('Passphrase', keyInput('passphrase', a.passphrase_set,
        'Passphrase')) + '</div>' +
      (isNew ? '' : loginHtml(a)) +
      '<div class="ma-actions"><button class="ma-btn primary" ' +
      'data-act="save">' + (isNew ? 'Add account' : 'Save') + '</button>' +
      (isNew ? '<button class="ma-btn" data-act="cancel-new">Cancel</button>'
        : '<button class="ma-btn" data-act="test">Test connection</button>' +
          '<a class="ma-btn" target="_blank" rel="noopener" href="' +
          '/mirror/client/' + encodeURIComponent(a.name) + '" title="The ' +
          'dashboard this client sees when they log in">Client dashboard' +
          '</a>' +
          '<button class="ma-btn danger" data-act="remove">Remove</button>') +
      '</div>' + (isNew ? '' : testHtml(a.name) + moneyHtml(a)) +
      '</div>';
  }

  function usd(x) {
    return x === null || x === undefined ? '\u2014' : UI.money(x);
  }

  function loginHtml(a) {
    /* The client's own sign-in to their dashboard. */
    return '<div class="ma-sub">Client login <small>(their own dashboard: ' +
      'balance, money in and out, monthly P&amp;L, profit payout \u2014 ' +
      'never a trade)</small></div><div class="ma-grid ma-login">' +
      field('Username', '<input type="text" class="ma-user" ' +
        'autocomplete="off" value="' + escape(a.login || '') + '" ' +
        'placeholder="e.g. client.a">') +
      field('Password', '<input type="password" class="ma-pass" ' +
        'autocomplete="new-password" placeholder="' + (a.login
          ? 'set \u2014 type a new one to change' : '10 characters or more') +
        '">') +
      '<div class="ma-loginbtns"><button class="ma-btn" data-act="login">' +
      (a.login ? 'Change login' : 'Set login') + '</button>' +
      (a.login ? '<button class="ma-btn" data-act="unlogin">Remove login' +
        '</button>' : '') + '<span class="ma-hint">' + (a.login
          ? '\u2713 can sign in as <b>' + escape(a.login) + '</b>'
          : 'no login yet') + '</span></div></div>';
  }

  function moneyHtml(a) {
    /* The account's money AS OKX HOLDS IT: balance and margin, and every
     * deposit and withdrawal from its own funding history. */
    var b = a.balance || {}, c = a.cash || {};
    var rows = (c.recent || []).map(function (f) {
      return '<tr><td>' + new Date(f.at * 1000).toLocaleDateString(
        undefined, {day: '2-digit', month: 'short', year: 'numeric'}) +
        '</td><td class="l">' + escape(f.kind) + '</td><td class="' +
        (f.amount >= 0 ? 'up' : 'down') + '">' + (f.amount > 0 ? '+' : '') +
        UI.money(f.amount) + '</td></tr>';
    }).join('');
    return '<div class="ma-sub">Account at OKX <small>(read every 10 ' +
      'minutes' + (c.read_at ? ' \u00b7 last ' + new Date(c.read_at * 1000)
        .toLocaleTimeString() : ' \u00b7 not read yet') + ')</small></div>' +
      (c.error ? '<div class="ma-test bad">' + escape(c.error) + '</div>'
        : '') + '<div class="ma-money"><div><span>Balance</span><b>' +
      usd(b.equity) + '</b></div><div><span>Available margin</span><b>' +
      usd(b.available) + '</b></div><div><span>Margin in use</span><b>' +
      usd(b.margin_used) + '</b></div><div><span>Deposited</span><b>' +
      usd(c.deposited) + '</b></div><div><span>Withdrawn</span><b>' +
      usd(c.withdrawn) + '</b></div></div>' + (rows
        ? '<table class="ma-flows"><thead><tr><th>Date</th><th class="l">' +
          'Deposits &amp; withdrawals (OKX)</th><th>Amount</th></tr></thead>' +
          '<tbody>' + rows + '</tbody></table>' +
          (c.count > 12 ? '<div class="ma-hint">latest 12 of ' + c.count +
            ' \u2014 all of them on the client dashboard</div>' : '')
        : '<div class="ma-hint">No deposit or withdrawal at OKX yet.</div>');
  }

  function testHtml(name) {
    var t = local.tests[name];
    if (!t) { return ''; }
    if (t.running) {
      return '<div class="ma-test">testing…</div>';
    }
    var checks = t.checks || [];
    return '<div class="ma-test ' + (t.connected ? 'ok' : 'bad') + '"><b>' +
      (t.connected ? '✓ Connected — it can trade'
        : '✗ Not ready') + '</b>' + (checks.length
        ? '<ul>' + checks.map(function (c) {
            return '<li class="' + (c.status === 'FAIL' ? 'bad'
              : c.status === 'WARN' ? 'warn' : 'ok') + '">' +
              escape(c.message || c.name || '') + '</li>';
          }).join('') + '</ul>'
        : (t.problems || []).map(function (p) {
            return '<div>' + escape(p) + '</div>';
          }).join('')) + '</div>';
  }

  function html() {
    if (local.error) {
      return '<p class="ma-error">' + escape(local.error) + '</p>';
    }
    var d = local.data;
    if (!d) { return '<p class="hint">reading…</p>'; }
    var accounts = (d.followers || []).slice().sort(function (a, b) {
      return String(a.name).localeCompare(b.name);
    });
    var holding = accounts.reduce(function (n, a) {
      return n + (a.holding || 0); }, 0);
    var out = '<div class="ma-top"><label class="ma-switch"><input ' +
      'type="checkbox" data-act="toggle"' + (d.enabled ? ' checked' : '') +
      '> <b>Copy the Algo’s trades into these accounts</b></label>' +
      field('Copy window (s)', '<input type="number" min="1" step="1" ' +
        'class="ma-window" value="' + escape(d.open_window_sec) + '">',
        'a trade first seen later than this after it opened is not copied') +
      '<button class="ma-btn" data-act="window">Save window</button>' +
      '<span class="ma-count">' + accounts.length + ' account(s) · ' +
      holding + ' copy(ies) held · mirror <b class="' +
      (d.enabled ? 'up' : '') + '">' + (d.enabled ? 'ON' : 'OFF') +
      '</b></span>' +
      '<button class="ma-btn primary" data-act="add">+ Add a managed ' +
      'account</button></div>';
    if (local.adding) {
      out += '<div class="ma-card open new"><div class="ma-head"><b>New ' +
        'managed account</b></div>' + formHtml({}, true) + '</div>';
    }
    if (!accounts.length && !local.adding) {
      out += '<p class="hint">No managed account yet. Add one with its ' +
        'multiple and API key, test it, then switch the copying on.</p>';
    }
    accounts.forEach(function (a) {
      var open = !!local.open[a.name];
      var keys = a.key_set && a.secret_set && a.passphrase_set;
      out += '<div class="ma-card' + (open ? ' open' : '') + '">' +
        '<div class="ma-head" data-act="edit" data-name="' + escape(a.name) +
        '"><span class="ma-caret">' + (open ? '▾' : '▸') +
        '</span><b>' + escape(a.name) + '</b>' +
        '<span class="ma-pill ' + (a.demo === false ? 'live' : '') + '">' +
        (a.demo === false ? 'LIVE' : 'demo') + '</span>' +
        '<span>×' + UI.fmt(a.multiple || 0, 2) + '</span>' +
        '<span>' + (a.capital ? '$' + Math.round(a.capital)
          .toLocaleString('en-US') : 'capital not set') + '</span>' +
        '<span>' + (a.execution === 'TAKER' ? 'always taker'
          : 'maker / taker as the trade') + '</span>' +
        '<span class="ma-pill ' + (keys ? 'ok' : 'bad') + '">' +
        (keys ? 'key ✓' : 'key missing') + '</span>' +
        '<span class="ma-pill ' + (a.enabled === false ? '' : 'ok') + '">' +
        (a.enabled === false ? 'not following' : 'following') + '</span>' +
        (a.holding ? '<span class="ma-pill ok">' + a.holding + ' held' +
          '</span>' : '') + ((a.balance || {}).equity !== undefined &&
          (a.balance || {}).equity !== null ? '<span>bal ' +
          UI.money(a.balance.equity) + ' \u00b7 avail ' +
          usd(a.balance.available) + '</span>' : '') +
        (a.login ? '<span class="ma-pill">login \u2713</span>' : '') + '<span class="ma-edit">' + (open ? 'Close'
          : 'Edit') + '</span></div>' + (open ? formHtml(a, false) : '') +
        '</div>';
    });
    return out;
  }

  // -- what it does ---------------------------------------------------------

  function readForm(form) {
    var out = {};
    Array.prototype.forEach.call(form.querySelectorAll('[data-f]'),
      function (input) {
        var f = input.dataset.f;
        if (f === 'pairs') {
          out.pairs = Array.prototype.filter.call(input.options,
            function (o) { return o.selected; }).map(function (o) {
              return o.value; });
        } else if (f === 'demo' || f === 'enabled') {
          out[f] = input.value === '1';
        } else {
          out[f] = input.value;
        }
      });
    return out;
  }

  function onClick(e) {
    var target = e.target.closest('[data-act]');
    if (!target || local.busy) { return; }
    var act = target.dataset.act;
    var form = target.closest('.ma-form');
    var name = form ? form.dataset.name : target.dataset.name;
    if (act === 'toggle') {
      local.busy = true;
      post('/api/mirror/settings', {enabled: target.checked})
        .then(function (r) {
          local.busy = false;
          UI.toast(r.ok ? 'Copying ' + (target.checked ? 'ON' : 'OFF') +
            ' — the engine picks it up in a few seconds'
            : r.body.error, r.ok ? 'ok' : '');
          load();
        });
      return;
    }
    if (act === 'window') {
      var box = target.parentNode.querySelector('.ma-window');
      post('/api/mirror/settings', {open_window_sec: box.value})
        .then(function (r) {
          UI.toast(r.ok ? 'Copy window saved' : r.body.error,
                   r.ok ? 'ok' : '');
          load();
        });
      return;
    }
    if (act === 'add') { local.adding = true; draw(); return; }
    if (act === 'cancel-new') { local.adding = false; draw(); return; }
    if (act === 'edit') {
      local.open[name] = !local.open[name];
      draw();
      return;
    }
    if (act === 'save') {
      var body = readForm(form);
      var isNew = !name;
      var who = isNew ? (body.name || '').trim() : name;
      delete body.name;
      if (!who) { UI.toast('Give the account a name'); return; }
      body['new'] = isNew;
      local.busy = true;
      post('/api/mirror/accounts/' + encodeURIComponent(who), body)
        .then(function (r) {
          local.busy = false;
          if (!r.ok) { UI.toast(r.body.error || 'not saved'); return; }
          UI.toast(who + ' saved — the engine picks it up in a few ' +
                   'seconds', 'ok');
          if (isNew) { local.adding = false; local.open[who] = true; }
          load();
        });
      return;
    }
    if (act === 'test') {
      local.tests[name] = {running: true};
      draw();
      api('/api/mirror/accounts/' + encodeURIComponent(name) + '/test')
        .then(function (r) {
          local.tests[name] = r.body || {};
          draw();
        });
      return;
    }
    if (act === 'login' || act === 'unlogin') {
      var box2 = target.closest('.ma-login');
      var payload = act === 'unlogin' ? {remove: true} : {
        username: box2.querySelector('.ma-user').value,
        password: box2.querySelector('.ma-pass').value};
      post('/api/mirror/accounts/' + encodeURIComponent(name) + '/login',
           payload).then(function (r) {
        UI.toast(r.ok ? (act === 'unlogin' ? name + ': login removed'
          : name + ' can sign in as ' + r.body.login) : r.body.error,
          r.ok ? 'ok' : '');
        if (r.ok) { load(); }
      });
      return;
    }
    if (act === 'remove') {
      UI.ask('Remove ' + name + '?', 'It stops following the Algo and its ' +
        'saved API key is cleared. Refused while it holds a copy — ' +
        'close those first from the Mirror tab. Its past results stay in ' +
        'the record.', 'Remove', function () {
          post('/api/mirror/accounts/' + encodeURIComponent(name),
               undefined, 'DELETE').then(function (r) {
            UI.toast(r.ok ? name + ' removed' : r.body.error,
                     r.ok ? 'ok' : '');
            delete local.open[name];
            load();
          });
        });
    }
  }

  window.MT5Managed = {
    render: function () { node(); },
    html: html, local: local, load: load
  };
})();
