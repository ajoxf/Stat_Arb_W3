/* Settings: the two accounts, and the pairs that route across them.
 *
 * This panel is the one part of the UI that must work with the ENGINE
 * DOWN. The coordinator will not start until the symbols are right, and
 * these are the tools for finding out — so everything here talks to the
 * leg runners directly and never through the coordinator.
 *
 * Two house rules do most of the work in here:
 *   - A warning nobody can act on is not a fix. Where the system can
 *     name the wrong field AND the right value, it offers a one-click
 *     correction — but it stays a click. Nothing is corrected silently.
 *   - Structural fields need a restart and say so; nothing else does.
 */

(function () {
  'use strict';

  var UI = window.MT5Trader;
  var DASH = UI.DASH;

  var local = {
    accounts: [],
    nextPort: '127.0.0.1:9101',
    pairs: {},
    editing: null,          // the pair key being edited, '' for a new one
    draft: {},              // the pair form's current values
    derived: null,          // what MT5 says about the draft's two legs
    symbols: {},            // account -> last search result
    tests: {},              // account -> last connectivity answer
    terminals: [],          // the MetaTrader 5s installed on this PC
    settings: null,         // the engine's tunables, with their defaults
    hot: [],                // ...and which of them apply without a restart
    connection: null,       // is the SYSTEM connected, in one answer
    timer: null
  };

  function el(id) { return document.getElementById(id); }

  function escape(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function api(path, options) {
    // Every caller reads `result.body`, and NO call site has a .catch().
    // So a failure has to arrive AS a body: an unhandled rejection here
    // is a button that does nothing at all — no toast, no error, no
    // clue why. Both ways a request can fail without ever producing
    // JSON land in the two handlers below: an answer that is not JSON
    // (a traceback, a proxy page), and a fetch that never connected.
    function failed(status, error) {
      return {ok: false, status: status, body: {error: error}};
    }
    return fetch(path, options).then(function (response) {
      return response.json().then(function (body) {
        return {ok: response.ok && body.ok !== false, status: response.status,
                body: body};
      }, function () {
        return failed(response.status,
          'the server answered ' + response.status + ' with something that ' +
          'is not JSON — check the launcher console for a traceback');
      });
    }, function (e) {
      return failed(0, 'could not reach the server (' +
        ((e && e.message) || 'connection failed') +
        ') — is the launcher still running?');
    });
  }

  function refresh() {
    return Promise.all([
      api('/api/accounts'), api('/api/pairs'), api('/api/settings'),
      api('/api/lock'), api('/api/terminals')
    ]).then(function (results) {
      local.accounts = results[0].body.accounts || [];
      local.terminals = (results[4].body || {}).terminals || [];
      local.nextPort = results[0].body.next_free_port || local.nextPort;
      local.pairs = results[1].body.pairs || {};
      local.settings = results[2].body.settings || {};
      local.defaults = results[2].body.defaults || {};
      local.hot = results[2].body.hot || [];
      // Whether a PIN EXISTS, never the PIN or its hash. The pane needs
      // it to decide between 'Set PIN' and 'Change PIN', and that is
      // the entire question it is allowed to ask.
      local.settings.__pin_set = !!(results[3].body || {}).pin_set;
      render();
    });
  }

  // -- the panel ---------------------------------------------------------

  function node() {
    var existing = document.querySelector('.window.settings');
    if (existing) { return existing; }
    var panel = document.createElement('section');
    panel.className = 'window settings';
    panel.innerHTML =
      '<div class="titlebar"><span class="swatch"></span>' +
      '<span class="title">Exchanges — accounts, pairs and settings</span>' +
      '<span class="winbtns"><button class="winbtn close">&times;</button>' +
      '</span></div>' +
      '<div class="settings-body">' +
      '<section class="trading"></section>' +
      '<section class="accounts"></section>' +
      '<section class="pairs"></section>' +
      '</div>' +
      '<div class="note">Accounts, symbols and the hedge ratio are ' +
      'STRUCTURAL: the launcher reads them at startup, so a change here ' +
      'takes effect when you restart it. Everything else on the ladder ' +
      '(mode, time in force, overnight, increment, quantity) applies at ' +
      'once.</div>';
    panel.querySelector('.close').addEventListener('click', function () {
      UI.closePanel(UI.panelId('settings'));
    });
    panel.addEventListener('click', onClick);
    panel.addEventListener('change', onChange);
    // A click is mousedown AND mouseup on the SAME element. Replace
    // that element in between — which the connection poll does every
    // five seconds, wholesale, with `innerHTML =` — and the browser
    // fires no click at all: the button the operator pressed no longer
    // exists to receive it. Save pair did nothing, said nothing, and
    // sent nothing (live 2026-08-31).
    //
    // So while a button is held down, nothing repaints. The redraw is
    // not skipped, only deferred to the mouseup.
    panel.addEventListener('mousedown', function () {
      local.pressing = true;
    });
    ['mouseup', 'mouseleave', 'blur'].forEach(function (event) {
      window.addEventListener(event, function () {
        if (!local.pressing) { return; }
        local.pressing = false;
        // AFTER the click has been dispatched, not instead of it.
        window.setTimeout(function () {
          if (local.missed) { local.missed = false; render(); }
        }, 0);
      });
    });
    document.getElementById('desktop').appendChild(panel);
    refresh();
    refreshConnection();
    // The connection state is the one thing on this page that changes
    // without the operator doing anything.
    if (!local.timer) {
      local.timer = window.setInterval(function () {
        if (document.querySelector('.window.settings')) {
          refreshConnection();
        }
      }, 5000);
    }
    return panel;
  }

  function render(fromForm) {
    var panel = document.querySelector('.window.settings');
    if (!panel) { return; }
    // The ladders repaint three times a second. A form that repaints
    // with them loses whatever is half-typed into it — and a field that
    // empties itself under the operator is worse than no form at all.
    // So: keep what is in the pair form (it is the draft), and do not
    // touch a section the cursor is inside.
    //
    // `fromForm === false` says the draft was just changed in code — a
    // one-click correction the operator asked for — and re-reading the
    // form here would immediately undo it.
    if (fromForm !== false) { readDraft(); }
    redraw(panel.querySelector('.trading'), tradingHtml);
    redraw(panel.querySelector('.accounts'), accountsHtml);
    redraw(panel.querySelector('.pairs'), pairsHtml);
  }

  //: The blank "new account" row's fields, in render order.
  var NEW_ACCOUNT_FIELDS = ['f-name', 'f-terminal', 'f-login', 'f-server',
                            'f-endpoint', 'f-password'];

  function newAccountDraft(section) {
    // What is half-typed into the NEW account row, or null.
    //
    // The connection poll repaints this table every 5 seconds and
    // `innerHTML =` throws away anything not yet saved. isTyping()
    // only protects a field that HAS FOCUS, so the moment the operator
    // clicks Save — or leaves the window to copy a terminal path — the
    // row empties under them, and the save then refuses for a missing
    // name while the screen shows no reason at all. The row is unsaved
    // work: carry it across the repaint.
    if (!section) { return null; }
    var row = section.querySelector('tr.new');
    if (!row) { return null; }
    var draft = {};
    NEW_ACCOUNT_FIELDS.forEach(function (field) {
      var input = row.querySelector('.' + field);
      draft[field] = input ? input.value : '';
    });
    // The endpoint arrives PRE-FILLED with the next free port, so it
    // alone is not the operator having started: carrying it would pin
    // the row to a port that may no longer be free.
    var started = NEW_ACCOUNT_FIELDS.some(function (field) {
      return field !== 'f-endpoint' && draft[field];
    });
    return started ? draft : null;
  }

  function accountEdits(section) {
    // What has been CHANGED but not saved on the EXISTING account rows,
    // keyed by account and field, or null.
    //
    // The same hazard as the new row: the connection poll repaints this
    // table every five seconds from the SAVED accounts. A MetaTrader
    // picked from the list - or a path pasted - and then the pointer
    // moved toward Save, and the row went back to the old path under
    // it. So an edit is carried across the repaint until it is saved.
    if (!section) { return null; }
    var edits = null;
    Array.prototype.forEach.call(
      section.querySelectorAll('tr[data-account]:not(.new)'), function (row) {
        Array.prototype.forEach.call(
          row.querySelectorAll('input'), function (input) {
            if (input.dataset.rendered === undefined) { return; }
            if (input.value === input.dataset.rendered) { return; }
            edits = edits || {};
            edits[row.dataset.account + '|' + input.className] = input.value;
          });
      });
    return edits;
  }

  function restoreAccountEdits(section, edits) {
    if (!edits || !section) { return; }
    Object.keys(edits).forEach(function (key) {
      var cut = key.indexOf('|');
      var name = key.slice(0, cut), cls = key.slice(cut + 1);
      Array.prototype.forEach.call(
        section.querySelectorAll('tr[data-account]'), function (row) {
          if (row.dataset.account !== name) { return; }
          var input = row.querySelector('.' + cls.split(' ')[0]);
          if (input) { input.value = edits[key]; }
        });
    });
  }

  function restoreNewAccount(section, draft) {
    if (!draft || !section) { return; }
    var row = section.querySelector('tr.new');
    if (!row) { return; }
    NEW_ACCOUNT_FIELDS.forEach(function (field) {
      var input = row.querySelector('.' + field);
      if (input) { input.value = draft[field]; }
    });
  }

  function stampRendered(section) {
    // What each field was RENDERED with. Comparing against this is how
    // an operator's unapplied edit is told apart from a value that
    // simply came back unchanged from the server.
    // input AND select: the ladder-click convention is a <select>,
    // and a control left out here reverts under the 5s poll and is
    // then SAVED back at its old value by Apply.
    Array.prototype.forEach.call(
      section.querySelectorAll('input, select'), function (input) {
        input.dataset.rendered = input.type === 'checkbox'
          ? String(input.checked) : input.value;
      });
  }

  function editedFields(section) {
    // Settings the operator has CHANGED but not yet applied, or null.
    //
    // The same hazard as the new-account row, one section over: the 5s
    // connection poll repaints this form from the SAVED settings, and
    // isTyping() stops protecting the moment focus leaves the field. So
    // a number typed and then clicked away from silently reverts — and
    // Apply, reading the form, then saves the OLD value back over the
    // new one. Live: the stale-quote limit was set to 15 three times
    // and stayed 5.
    //
    // Keyed by class, which is unique per field in this form. The
    // accounts table repeats classes down its rows and is carried by
    // newAccountDraft() instead.
    if (!section || !section.querySelector('.s-stale')) { return null; }
    var edited = null;
    Array.prototype.forEach.call(
      section.querySelectorAll('input, select'), function (input) {
        if (input.dataset.rendered === undefined) { return; }
        var now = input.type === 'checkbox'
          ? String(input.checked) : input.value;
        if (now === input.dataset.rendered) { return; }
        edited = edited || {};
        edited[input.className] = now;
      });
    return edited;
  }

  function restoreEdited(section, edited) {
    if (!edited) { return; }
    Object.keys(edited).forEach(function (cls) {
      var input = section.querySelector('.' + cls);
      if (!input) { return; }
      if (input.type === 'checkbox') {
        input.checked = edited[cls] === 'true';
      } else {
        input.value = edited[cls];
      }
    });
  }

  function redraw(section, build) {
    if (!section) { return; }
    if (isTyping(section)) { return; }
    // Not with a button held down: see the mousedown listener.
    if (local.pressing) { local.missed = true; return; }
    // Nor while a window is being dragged: these three sections are
    // large tables, and rebuilding them under the pointer is most of
    // why this window moved like treacle.
    if (document.querySelector('.window.dragging')) { return; }
    // Sections without a new-account row or a settings form give null
    // for their half of this, so each is a no-op outside its own table.
    var draft = newAccountDraft(section);
    var edited = editedFields(section);
    var accountEdited = accountEdits(section);
    section.innerHTML = build();
    stampRendered(section);
    restoreNewAccount(section, draft);
    restoreEdited(section, edited);
    restoreAccountEdits(section, accountEdited);
  }

  function isTyping(section) {
    var active = document.activeElement;
    if (!active || !section.contains(active)) { return false; }
    // A BUTTON holding focus is a click that just happened, and the
    // whole point of that click is usually to change what is drawn.
    // Only a field being typed into blocks the repaint.
    var tag = active.tagName;
    return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
  }

  // -- how a click behaves ------------------------------------------------

  function tradingHtml() {
    var settings = local.settings;
    if (!settings) { return '<h3>Trading</h3><p>loading…</p>'; }
    var confirm = settings.CONFIRM_MARKET_CLICKS;
    var html = '<h3>Trading <small>what a click does, and how fast</small>' +
      '</h3><div class="fields trading-fields">';
    html += field('Market clicks',
      '<label class="check"><input type="checkbox" class="s-confirm"' +
      (confirm ? ' checked' : '') + '> ask before crossing</label>' +
      '<div class="hint">' + (confirm
        ? 'ON: every market click asks first. Slower, and deliberate.'
        : 'OFF (default): ONE CLICK IS ONE ORDER — a market click ' +
          'crosses both accounts immediately. The arming carries the ' +
          'weight instead: the mode badge, the tinted columns, the ' +
          'cursor.') + '</div>');
    var tt = (settings.CLICK_CONVENTION || 'TT') !== 'TOUCH';
    html += field('Ladder click',
      '<select class="s-click">' +
      '<option value="TT"' + (tt ? ' selected' : '') + '>' +
      'Bids buy — TT price ladder</option>' +
      '<option value="TOUCH"' + (tt ? '' : ' selected') + '>' +
      'Asks buy — hit and lift</option></select>' +
      '<div class="hint">' + (tt
        ? 'TT: clicking BIDS joins the bid, which is a resting BUY; ' +
          'clicking ASKS joins the offer and sells. What every desk ' +
          'arrives with.'
        : 'HIT/LIFT: clicking ASKS lifts the offer and BUYS; clicking ' +
          'BIDS hits the bid and sells.') +
      ' It moves only which column sends which side — the price is the ' +
      'row you clicked either way, and the BUY and SELL buttons name ' +
      'their own side and do not change.</div>');
    // WHY A CLICK SOMETIMES CLOSES SOMETHING. Surprising the first
    // time it happens on a mixed book, and there was no switch and no
    // sentence about it anywhere on the screen.
    var reduce = settings.CLOSE_FIRST !== false;
    html += field('Opposite clicks',
      '<label class="check"><input type="checkbox" class="s-closefirst"' +
      (reduce ? ' checked' : '') + '> reduce before opening</label>' +
      '<div class="hint">' + (reduce
        ? 'ON (default): a click the other way CLOSES open tickets ' +
          'first, oldest first, taking the last one in part. These ' +
          'accounts are hedging, so MT5 never nets — without this, ' +
          'covering a short leaves you holding a short AND a long, ' +
          'both paying carry. On a mixed book a click that ADDS to ' +
          'the net still clears an opposite ticket: the net moves by ' +
          'what you clicked either way, but that part CROSSES when ' +
          'the level prints instead of earning it.'
        : 'OFF: every click purely OPENS. On a hedging account that ' +
          'stacks a second, opposite ticket beside what you already ' +
          'have — both live, both paying carry.') + '</div>');
    // -- the screen lock --------------------------------------------
    //
    //     Set here rather than at install time on purpose: the PIN
    //     belongs to the TRADER, and one typed by whoever set the
    //     machine up is one the whole office knows.
    html += field('Screen lock PIN',
      '<div class="pinset">' +
      '<input class="s-pin-current" type="password" autocomplete="off" ' +
      'placeholder="current PIN"' +
      (settings.__pin_set ? '' : ' disabled') + '>' +
      '<input class="s-pin-new" type="password" autocomplete="off" ' +
      'placeholder="new PIN">' +
      '<input class="s-pin-again" type="password" autocomplete="off" ' +
      'placeholder="again">' +
      '<button class="btn s-pin-save" type="button">' +
      (settings.__pin_set ? 'Change PIN' : 'Set PIN') + '</button>' +
      '</div>' +
      '<div class="hint pin-note">' + (settings.__pin_set
        ? 'A PIN is set on this PC. The padlock beside NEXUS locks the ' +
          'screen, Ctrl+L does the same, and it locks itself after the ' +
          'idle time below. Changing it needs the current one.'
        : 'No PIN on this PC, so the lock is off. Set one and the ' +
          'screen locks when you ask it to and when it is left alone - ' +
          'nothing on a locked screen can trade.') +
      ' It is stored as a one-way hash in .env, never as the PIN. ' +
      'It does NOT stop somebody at this keyboard opening MetaTrader 5 ' +
      'directly: a Windows account per trader is what does that.' +
      '</div>');
    html += field('Lock itself after (minutes)',
      '<input class="s-autolock" type="number" min="0" step="1" ' +
      'value="' + escape(settings.AUTO_LOCK_MINUTES) + '">' +
      '<div class="hint">Minutes with nobody touching the screen ' +
      'before it locks. 15 is long enough to watch a ladder without ' +
      'clicking. 0 turns it off and leaves only the padlock. It does ' +
      'nothing until a PIN is set.</div>');
    html += field('Slippage protection (ticks)',
      '<input class="s-protection" type="number" min="0" step="0.5" ' +
      'value="' + escape(settings.MARKET_PROTECTION_TICKS) + '">' +
      '<div class="hint">A market click is market-WITH-protection: a ' +
      'fill worse than the clicked spread by more than this many ' +
      'increments is refused, and the ladder says why. 0 turns it off.' +
      '</div>');
    html += field('Ladder row height (px)',
      '<input class="s-rowheight" type="number" min="12" max="40" ' +
      'step="1" value="' + escape(settings.ROW_HEIGHT_PX) + '">' +
      '<div class="hint">17 is the reference screen\'s. A bigger target ' +
      'is a faster and safer click on a large monitor.</div>');
    html += field('Click drain (seconds)',
      '<input class="s-drain" type="number" min="0.005" max="1" ' +
      'step="0.005" value="' + escape(settings.COMMAND_POLL_SEC) + '">' +
      '<div class="hint">How often the engine picks clicks up, on its ' +
      'own thread. This is the click-to-order latency you feel; the ' +
      'price poll is separate and slower.</div>');
    html += field('Re-peg dead band (ticks)',
      '<input class="s-repeg" type="number" min="0" step="0.5" value="' +
      escape(settings.REPEG_DEAD_BAND_TICKS) + '">' +
      '<div class="hint">LIMIT mode only. Every re-peg loses queue ' +
      'position, so a tight band means never being at the front of a ' +
      'queue — which defeats quoting.</div>');
    html += field('Stale quote limit (seconds)',
      '<input class="s-stale" type="number" min="0" step="0.5" value="' +
      escape(settings.MAX_QUOTE_AGE_SEC) + '">' +
      '<div class="hint">A pair is only as good as its worse leg. 0 ' +
      'turns the guard off — it can withhold an order, never a close.' +
      '</div>');
    html += '</div>';

    // What a trade COSTS — and therefore where it gets out — is NOT
    // here. Commission, the slippage allowance, the nights held, the
    // take-profit percentage and the carry rate belong to ONE LADDER:
    // a gold basis and an oil differential are charged differently and
    // held for different lengths of time, and one set of numbers
    // covering both is a set that is wrong for at least one of them.
    // They live behind the cog on each ladder.
    html += '<p class="hint where-exits">Commission, the slippage ' +
      'allowance, nights held, the take-profit % and the carry rate are ' +
      'per LADDER \u2014 open a ladder\u2019s <b>&#9881;</b> for its own. ' +
      'They are not the same trade on every pair.</p>';

    html += '</div><div class="actions">' +
      '<button class="btn save-settings">Apply</button>' +
      '<span class="hint">These apply to the running engine at once — ' +
      'no restart.</span></div>';
    return html;
  }

  // -- accounts -----------------------------------------------------------

  function accountsHtml() {
    var html = '<h3>Exchanges <small>one login, one terminal, one port — ' +
      'the three things that cannot be shared</small></h3>';
    html += connectionHtml();
    html += '<table class="grid-form"><thead><tr><th>Name</th>' +
      '<th>Terminal (terminal64.exe)</th><th>Login</th><th>Server</th>' +
      '<th>Runner endpoint</th><th>Password</th><th></th></tr></thead><tbody>';
    local.accounts.forEach(function (account) {
      html += accountRow(account);
    });
    html += accountRow({name: '', endpoint: local.nextPort, isNew: true});
    html += '</tbody></table>';
    return html;
  }

  function accountRow(account) {
    var clashes = [account.terminal_missing, account.endpoint_clash,
                   account.login_clash,
                   account.terminal_clash].filter(Boolean);
    var test = local.tests[account.name];
    var html = '<tr data-account="' + escape(account.name) + '"' +
      (account.isNew ? ' class="new"' : '') + '>';
    html += '<td>' + (account.isNew
      ? '<input class="f-name" placeholder="e.g. CFI Spot">'
      : escape(account.name)) + '</td>';
    html += '<td><input class="f-terminal" value="' +
      escape(account.terminal_path) + '" placeholder="blank = attach to ' +
      'whichever terminal is open">' + terminalPicker(account) + '</td>';
    html += '<td><input class="f-login" value="' + escape(account.login) +
      '"></td>';
    html += '<td><input class="f-server" value="' + escape(account.server) +
      '"></td>';
    html += '<td><input class="f-endpoint" value="' +
      escape(account.endpoint) + '" placeholder="127.0.0.1:9101"></td>';
    html += '<td><input class="f-password" type="password" placeholder="' +
      (account.password_set ? 'set — type to replace' : 'not set') +
      '"><div class="hint">' + (account.isNew
        ? 'goes to .env under a key derived from the name'
        : '→ ' + escape(account.password_env) + ' in .env') +
      '</div></td>';
    html += '<td class="actions">' +
      '<button class="btn save-account">Save</button>' +
      (account.isNew ? '' :
        // The three questions an operator actually asks, in the order
        // they ask them.
        '<button class="btn connect-account" title="Is the leg runner ' +
        'there, and is its terminal logged in?">Connect</button>' +
        '<button class="btn test-account" title="Can this account ' +
        'trade — Algo Trading, permissions, hedging?">Test</button>' +
        '<button class="btn diagnose-account" title="Everything: the ' +
        'account, its symbols, and whether the two legs fit">Diagnose' +
        '</button>' +
        '<button class="btn danger delete-account">Delete</button>') +
      '</td></tr>';

    if (clashes.length) {
      html += '<tr class="problem"><td colspan="7">' +
        clashes.map(escape).join('<br>') + '</td></tr>';
    }
    if (test) {
      html += '<tr class="checklist"><td colspan="7">' +
        checklistHtml(test) + '</td></tr>';
    }
    return html;
  }

  function terminalPicker(account) {
    // PICK, don't type. The MetaTrader 5s actually installed on this
    // PC, read off the disk by the server. A path typed by hand is how
    // a desk ended up unable to start: pasted with the quotes Windows'
    // "Copy as path" adds, the file was exactly where it said and still
    // could not be found. Choosing from this list fills the box; Save
    // then saves it, and the engine restarts by itself.
    if (!local.terminals.length) { return ''; }
    var current = String(account.terminal_path || '').toLowerCase();
    var html = '<select class="f-terminal-pick" title="The MetaTrader 5 ' +
      'installations found on this PC">' +
      '<option value="">pick an installed MetaTrader 5\u2026</option>';
    local.terminals.forEach(function (terminal) {
      var mine = terminal.path.toLowerCase() === current;
      var other = terminal.used_by && terminal.used_by !== account.name;
      html += '<option value="' + escape(terminal.path) + '"' +
        (mine ? ' selected' : '') + '>' + escape(terminal.folder) +
        (mine ? ' \u2014 this account' :
          (other ? ' \u2014 used by ' + escape(terminal.used_by) : '')) +
        '</option>';
    });
    return html + '</select>';
  }

  function checklistHtml(result) {
    // The answer, as a checklist with a FIX on every failure.
    if (result.error && !result.checks) {
      return '<div class="problem-text">' + escape(result.error) + '</div>';
    }
    var overall = result.overall || (result.ok ? 'PASS' : 'FAIL');
    var html = '<div class="checklist-head ' + overall.toLowerCase() + '">' +
      escape(result.ran || '') + '<b>' + overall + '</b> — ' +
      result.passed + ' passed, ' + result.warnings + ' warning(s), ' +
      result.failed + ' failed' +
      (result.connected ? ' · CONNECTED' : '') + '</div>';
    html += '<table class="checks"><tbody>';
    (result.checks || []).forEach(function (check) {
      html += '<tr class="c-' + check.status.toLowerCase() + '">';
      html += '<td class="status">' + check.status + '</td>';
      html += '<td class="what">' + escape(check.name) + '</td>';
      html += '<td>' + escape(check.message);
      // A warning nobody can act on is not a fix: every failure carries
      // the step that makes it pass.
      if ((check.fix || []).length) {
        html += '<ul class="fix">';
        check.fix.forEach(function (step) {
          html += '<li>' + escape(step) + '</li>';
        });
        html += '</ul>';
      }
      html += '</td></tr>';
    });
    return html + '</tbody></table>';
  }

  function connectionHtml() {
    // Is the system connected — plainly, at the top, in one line.
    var state = local.connection;
    if (!state) { return '<div class="conn checking">checking…</div>'; }
    var html = '<div class="conn ' + (state.connected ? 'up' : 'down') +
      '"><b>' + (state.connected ? 'CONNECTED' : 'NOT READY') + '</b> ' +
      escape(state.summary);
    if ((state.blockers || []).length > 1) {
      html += '<ul>';
      state.blockers.forEach(function (blocker) {
        html += '<li>' + escape(blocker) + '</li>';
      });
      html += '</ul>';
    }
    var clock = state.broker_clock || {};
    if (clock.broker_time) {
      html += '<div class="hint">Broker time ' + escape(clock.broker_time) +
        ' · ' + escape(clock.note) + '</div>';
    } else if (clock.note) {
      html += '<div class="hint problem-text">' + escape(clock.note) +
        '</div>';
    }
    return html + '</div>';
  }

  // -- pairs ---------------------------------------------------------------

  function pairsHtml() {
    var html = '<h3>Pairs <small>each ladder is leg A on one account and ' +
      'leg B on the other; the spread is B − β × A</small></h3>';
    html += '<table class="grid-form"><thead><tr><th>Key</th><th>Name</th>' +
      '<th>Leg A</th><th>Leg B</th><th>β</th><th>Increment</th>' +
      '<th>Enabled</th><th>Status</th><th></th></tr></thead><tbody>';
    Object.keys(local.pairs).forEach(function (key) {
      var pair = local.pairs[key];
      html += '<tr data-pair="' + escape(key) + '">';
      html += '<td>' + escape(key) + '</td>';
      html += '<td>' + escape(pair.name || key) + '</td>';
      html += '<td>' + legText(pair.leg_a) + '</td>';
      html += '<td>' + legText(pair.leg_b) + '</td>';
      html += '<td>' + escape(pair.hedge_ratio) +
        (pair.hedge_ratio_for
          ? '<div class="hint">stamped for ' + escape(pair.hedge_ratio_for) +
            '</div>'
          : '<div class="hint problem-text">not stamped for any pair</div>') +
        '</td>';
      html += '<td>' + escape(pair.increment === null ||
                              pair.increment === undefined
                              ? 'derived' : pair.increment) + '</td>';
      html += '<td>' + (pair.enabled === false ? 'no' : 'yes') + '</td>';
      html += pairStatusCell(key, pair);
      html += '<td class="actions">' +
        '<button class="btn edit-pair">Edit</button>' +
        '<button class="btn danger delete-pair">Delete</button></td></tr>';
    });
    html += '</tbody></table>';
    html += '<button class="btn new-pair">New pair</button>';
    if (local.editing !== null) { html += pairForm(); }
    return html;
  }

  function pairStatus(key, pair) {
    /* Is this ladder actually working? Read from the ENGINE's own
     * snapshot, never from the fact that a row was saved.
     *
     * The distinction that matters to whoever is setting this up: a
     * pair that is configured, a pair the engine has picked up, and a
     * pair that is quoting are three different things, and only the
     * last one can be traded. Saying "connected" for any of the others
     * is how a symbol that does not exist at the broker survives all
     * the way to a click.
     */
    var snapshot = (UI.state && UI.state.snapshot) || {};
    if (pair.enabled === false) {
      return {state: 'off', text: 'disabled'};
    }
    if (snapshot.engine !== 'up') {
      return {state: 'unknown',
              text: 'the engine is not running — nothing to check against'};
    }
    var live = (snapshot.pairs || {})[key];
    if (!live) {
      return {state: 'bad',
              text: 'saved, but the engine has not picked it up yet — ' +
                    'it restarts itself within a few seconds'};
    }
    if ((live.errors || []).length) {
      // The engine's own words, which name the symbol and what the
      // account actually offers.
      return {state: 'bad', text: live.errors[0]};
    }
    if (!live.market || live.short_spread === null ||
        live.short_spread === undefined) {
      return {state: 'bad',
              text: 'both legs resolved, but no two-sided quote has ' +
                    'arrived yet'};
    }
    return {state: 'ok',
            text: 'CONNECTED — quoting at ' +
                  UI.fmt(live.short_spread, 4) + ' / ' +
                  UI.fmt(live.long_spread, 4)};
  }

  function pairStatusCell(key, pair) {
    var status = pairStatus(key, pair);
    var className = status.state === 'ok' ? 'c-pass'
      : status.state === 'bad' ? 'c-fail' : 'c-info';
    return '<td class="pair-status ' + className + '">' +
      escape(status.text) + '</td>';
  }

  function legText(leg) {
    /* The symbol, and the ACCOUNT it trades on — by name, MT5 login and
     * server. Two accounts is the whole architecture of this thing, and
     * which login a leg is actually on is the first question anyone
     * asks of a spread that has gone wrong. Reading it off the account
     * row instead of the pair row means holding two tables in your head
     * at once.
     */
    leg = leg || {};
    if (!leg.account && !leg.symbol) { return DASH; }
    var account = null;
    (local.accounts || []).forEach(function (row) {
      if (row.name === leg.account) { account = row; }
    });
    var detail = escape(leg.account || '?');
    if (account && account.login) { detail += ' · #' + escape(account.login); }
    if (account && account.server) {
      detail += ' · ' + escape(account.server);
    }
    if (!account) {
      // A leg pointing at an account that is no longer configured is a
      // pair that cannot trade, and it says so here rather than at the
      // first click.
      detail += ' — no such account';
    }
    return escape(leg.symbol) + '<div class="hint">on ' + detail + '</div>';
  }

  function pairForm() {
    var draft = local.draft;
    var derived = local.derived;
    var html = '<div class="pair-form"><h4>' +
      (local.editing ? 'Editing ' + escape(local.editing) : 'New pair') +
      '</h4><div class="fields">';
    html += field('Key', '<input class="p-key" value="' +
                  escape(draft.key) + '" placeholder="' +
                  escape(autoKey() || 'built from the two symbols') + '"' +
                  (local.editing ? ' disabled' : '') + '>' +
                  '<div class="hint">Leave it blank and it is built from ' +
                  'the two symbols — the key is an identifier, not a ' +
                  'setting, and typing one by hand is not something this ' +
                  'should ask for.' +
                  (local.editing && autoKey() && autoKey() !== local.editing
                    ? ' <b class="problem-text">Saving will rename this ' +
                      'pair to ' + escape(autoKey()) + ', because its ' +
                      'symbols changed.</b>'
                    : '') + '</div>');
    html += field('Name', '<input class="p-name" value="' +
                  escape(draft.name) + '">');
    html += field('Leg A', accountSelect('p-account-a', draft.leg_a_account) +
                  symbolInput('a', draft.leg_a_symbol));
    html += field('Leg B', accountSelect('p-account-b', draft.leg_b_account) +
                  symbolInput('b', draft.leg_b_symbol));
    html += field('Pair type',
                  '<select class="p-type">' +
                  option('SPOT_FUTURE', draft.pair_type) +
                  option('FUTURE_FUTURE', draft.pair_type) +
                  option('RELATED', draft.pair_type) + '</select>' +
                  '<div class="hint">Same underlying → β is 1 and the ' +
                  'spread IS the basis. Different instruments on the same ' +
                  'price scale → β 1 and the spread is the differential. ' +
                  'Different scales → the price ratio.</div>');
    html += field('β (hedge ratio)',
                  '<input class="p-beta" type="number" step="0.000001" ' +
                  'value="' + escape(draft.hedge_ratio) + '">' +
                  (derived && derived.suggested_beta !== null &&
                   derived.suggested_beta !== undefined
                    ? '<div class="hint">' + escape(derived.beta_reason) +
                      ' <button class="btn tiny use-beta" data-value="' +
                      derived.suggested_beta + '">use ' +
                      derived.suggested_beta + '</button></div>'
                    : ''));
    html += field('Futures expiry',
                  '<input class="p-expiry" type="date" value="' +
                  escape(draft.expiry || '') + '">' +
                  '<div class="hint">The futures leg\'s last trading ' +
                  'day. With the broker\'s own swap it gives the FAIR ' +
                  'spread: a basis converges to zero at expiry, and ' +
                  'until then it is worth its carry. The swaps live on ' +
                  'the ladder\'s own settings, per leg and per side.' +
                  '</div>');
    html += field('Increment',
                  '<input class="p-increment" type="number" step="0.000001" ' +
                  'value="' + escape(draft.increment) +
                  '" placeholder="blank = derived">' +
                  (derived
                    ? '<div class="hint">' +
                      escape(derived.increment_derivation) + ' = ' +
                      escape(derived.increment) +
                      ' <button class="btn tiny use-increment" ' +
                      'data-value="' + derived.increment + '">use it</button>' +
                      '</div>'
                    : ''));
    html += field('Default quantity (spreads)',
                  '<input class="p-quantity" type="number" min="0" ' +
                  'step="0.01" value="' + escape(draft.default_quantity) +
                  '">' + (derived
                    ? '<div class="hint">' + escape(derived.clip_derivation) +
                      ', ' + UI.money(derived.spread_units) +
                      ' per 1.00 of spread</div>' : ''));
    html += field('Quoting leg (LIMIT mode)',
                  '<select class="p-quoting">' +
                  '<option value="">auto — the wider book</option>' +
                  option('a', draft.quoting_leg) +
                  option('b', draft.quoting_leg) + '</select>' +
                  (derived
                    ? '<div class="hint">measured widths: A ' +
                      UI.fmt(derived.widths.a, 4) + ' · B ' +
                      UI.fmt(derived.widths.b, 4) + ' — ' +
                      escape(derived.quoting_note) + '</div>'
                    : ''));
    html += field('Enabled',
                  '<input class="p-enabled" type="checkbox"' +
                  (draft.enabled === false ? '' : ' checked') + '>');
    html += '</div>';

    if (derived) {
      html += '<div class="derived">';
      html += '<div>Spread now: <b>' + UI.fmt(derived.spread_now, 4) +
        '</b></div>';
      html += '<div>Minimum this pair can trade: <b>' +
        (derived.min_notional_usd
          ? UI.money(derived.min_notional_usd) + ' a leg' : DASH) +
        '</b> — state it before anyone tries to trade under it</div>';
      html += '<div>Contract sizes read from MT5: A ' +
        escape((derived.specs.a || {}).contract_size) + ' · B ' +
        escape((derived.specs.b || {}).contract_size) +
        ' (never typed in)</div>';
      html += '</div>';
    }
    html += '<div class="actions">' +
      '<button class="btn derive-pair">Read both legs from MT5</button>' +
      '<button class="btn save-pair">Save pair</button>' +
      '<button class="btn cancel-pair">Cancel</button></div></div>';
    return html;
  }

  function field(label, control) {
    return '<label class="field"><span>' + label + '</span>' + control +
      '</label>';
  }

  function option(value, selected) {
    return '<option value="' + value + '"' +
      (value === selected ? ' selected' : '') + '>' + value + '</option>';
  }

  function accountSelect(cls, selected) {
    var html = '<select class="' + cls + '"><option value="">account…' +
      '</option>';
    local.accounts.forEach(function (account) {
      if (!account.name) { return; }
      html += '<option value="' + escape(account.name) + '"' +
        (account.name === selected ? ' selected' : '') + '>' +
        escape(account.name) + '</option>';
    });
    return html + '</select>';
  }

  function symbolInput(side, value) {
    var found = local.symbols[side] || null;
    var html = '<span class="symbol-row">' +
      '<input class="p-symbol-' + side + '" value="' + escape(value) +
      '" placeholder="symbol">' +
      '<button class="btn tiny find-symbol" data-side="' + side +
      '">Find</button></span>';
    if (found) {
      html += '<div class="hint symbols">';
      if (found.error) {
        html += escape(found.error);
      } else if (!found.symbols.length) {
        html += 'nothing on that account matches — it is probably the ' +
          'wrong account for this leg';
      } else {
        found.symbols.slice(0, 12).forEach(function (symbol) {
          html += '<button class="btn tiny pick-symbol" data-side="' + side +
            '" data-symbol="' + escape(symbol.symbol) + '">' +
            escape(symbol.symbol) + '</button> ';
        });
      }
      html += '</div>';
    }
    return html;
  }

  // -- events ---------------------------------------------------------------

  function onChange(e) {
    if (e.target.closest('.pair-form')) { readDraft(); }
    if (e.target.classList.contains('f-terminal-pick')) {
      var row = e.target.closest('tr');
      var box = row && row.querySelector('.f-terminal');
      if (box && e.target.value) {
        box.value = e.target.value;
        UI.toast('picked ' + e.target.options[e.target.selectedIndex].text +
                 ' \u2014 press Save to use it', 'ok');
      }
    }
  }

  function savePin(panel) {
    /* The PIN goes to the server and NOWHERE else - not into
     * local.settings, not into the settings POST, not into a toast.
     * `.env` holds the hash; this function holds the PIN for as long
     * as one fetch takes and then clears the boxes. */
    var current = panel.querySelector('.s-pin-current');
    var fresh = panel.querySelector('.s-pin-new');
    var again = panel.querySelector('.s-pin-again');
    return api('/api/pin', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({current: current.value, pin: fresh.value,
                            again: again.value})
    }).then(function (result) {
      current.value = fresh.value = again.value = '';
      if (result.body && result.body.ok) {
        UI.toast('PIN set. The screen locks from now on.');
        return refresh();
      }
      UI.toast((result.body && result.body.error) ||
               'the PIN was not changed');
    });
  }

  function onClick(e) {
    /* Every click on this page goes through here, and one that throws
     * used to do nothing and SAY nothing — indistinguishable, from the
     * operator's side, from a button that is not wired up. */
    try {
      return dispatch(e);
    } catch (error) {
      UI.toast('that click failed: ' + (error && error.message));
      throw error;                      // still in the console, in full
    }
  }

  function dispatch(e) {
    var button = e.target.closest('button');
    if (!button) { return; }
    var row = button.closest('tr');

    if (button.classList.contains('s-pin-save')) {
      return savePin(button.closest('.window'));
    }
    if (button.classList.contains('save-settings')) {
      return saveSettings();
    }
    if (button.classList.contains('save-account')) {
      return saveAccount(row);
    }
    if (button.classList.contains('connect-account')) {
      return runCheck(row.dataset.account, 'connect', button);
    }
    if (button.classList.contains('test-account')) {
      return runCheck(row.dataset.account, 'test', button);
    }
    if (button.classList.contains('diagnose-account')) {
      return runCheck(row.dataset.account, 'diagnose', button);
    }
    if (button.classList.contains('delete-account')) {
      var target = row.dataset.account;
      return UI.ask('Delete account ' + target + '?',
        'The password stays in .env until you clear it there. A pair ' +
        'still routing to this account will refuse the deletion.',
        'Delete', function () {
          api('/api/accounts/' + encodeURIComponent(target),
              {method: 'DELETE'}).then(afterWrite);
        });
    }
    if (button.classList.contains('new-pair')) {
      local.editing = '';
      local.draft = {enabled: true, pair_type: 'SPOT_FUTURE',
                     hedge_ratio: 1.0, default_quantity: 1.0};
      local.derived = null;
      local.symbols = {};
      return render(false);
    }
    if (button.classList.contains('edit-pair')) {
      var key = row.dataset.pair;
      var pair = local.pairs[key] || {};
      local.editing = key;
      local.draft = {
        key: key, name: pair.name, pair_type: pair.pair_type || 'SPOT_FUTURE',
        hedge_ratio: pair.hedge_ratio, increment: pair.increment,
        default_quantity: pair.default_quantity,
        quoting_leg: pair.quoting_leg || '',
        enabled: pair.enabled !== false,
        leg_a_account: (pair.leg_a || {}).account,
        leg_a_symbol: (pair.leg_a || {}).symbol,
        leg_b_account: (pair.leg_b || {}).account,
        leg_b_symbol: (pair.leg_b || {}).symbol,
        expiry: pair.expiry || '',
      };
      local.derived = null;
      local.symbols = {};
      return render(false);
    }
    if (button.classList.contains('delete-pair')) {
      var doomed = row.dataset.pair;
      return UI.ask('Delete pair ' + doomed + '?',
        'A pair with an open position refuses this — flatten it first.',
        'Delete', function () {
          api('/api/pairs/' + encodeURIComponent(doomed),
              {method: 'DELETE'}).then(afterWrite);
        });
    }
    if (button.classList.contains('cancel-pair')) {
      local.editing = null;
      return render();
    }
    if (button.classList.contains('find-symbol')) {
      return findSymbols(button.dataset.side);
    }
    if (button.classList.contains('pick-symbol')) {
      readDraft();
      local.draft['leg_' + button.dataset.side + '_symbol'] =
        button.dataset.symbol;
      local.symbols[button.dataset.side] = null;
      return render(false);
    }
    if (button.classList.contains('use-beta')) {
      readDraft();
      local.draft.hedge_ratio = parseFloat(button.dataset.value);
      return render(false);
    }
    if (button.classList.contains('use-increment')) {
      readDraft();
      local.draft.increment = parseFloat(button.dataset.value);
      return render(false);
    }
    if (button.classList.contains('derive-pair')) { return derive(); }
    if (button.classList.contains('save-pair')) { return savePair(); }
  }

  function runCheck(name, kind, button) {
    var label = button.textContent;
    button.textContent = '…';
    button.disabled = true;
    return api('/api/accounts/' + encodeURIComponent(name) + '/' + kind)
      .then(function (result) {
        var body = result.body || {};
        body.ran = kind.charAt(0).toUpperCase() + kind.slice(1) + ': ';
        local.tests[name] = body;
        button.textContent = label;
        button.disabled = false;
        render(false);
        refreshConnection();
      })
      .catch(function (error) {
        button.textContent = label;
        button.disabled = false;
        UI.toast(kind + ' could not run: ' + error.message);
      });
  }

  function refreshConnection() {
    return api('/api/connection').then(function (result) {
      var was = local.connection && local.connection.connected;
      // A real answer always carries `blockers` (possibly empty). Any
      // body without it is api()'s failure shape — render THAT as the
      // reason, rather than an empty banner that reads like a
      // considered "not ready".
      local.connection = result.body && result.body.blockers
        ? result.body
        : {connected: false,
           summary: (result.body && result.body.error) ||
             'the connection check did not answer',
           blockers: [(result.body && result.body.error) ||
             'the connection check did not answer']};
      // Say it ONCE when it becomes true, rather than every poll: a
      // banner that never changes is a banner nobody reads. The words
      // are the SERVER's — a second copy here drifts from it, and this
      // one still said "both accounts" with one account configured.
      if (local.connection.connected && was === false) {
        UI.toast(local.connection.summary, 'ok');
      }
      render(false);
    });
  }

  function saveSettings() {
    var panel = document.querySelector('.window.settings .trading');
    function number(selector) {
      return parseFloat(panel.querySelector(selector).value);
    }
    var fields = {
      CONFIRM_MARKET_CLICKS: panel.querySelector('.s-confirm').checked,
      CLICK_CONVENTION: panel.querySelector('.s-click').value,
      AUTO_LOCK_MINUTES: panel.querySelector('.s-autolock').value,
      CLOSE_FIRST: panel.querySelector('.s-closefirst').checked,
      MARKET_PROTECTION_TICKS: number('.s-protection'),
      ROW_HEIGHT_PX: number('.s-rowheight'),
      COMMAND_POLL_SEC: number('.s-drain'),
      REPEG_DEAD_BAND_TICKS: number('.s-repeg'),
      MAX_QUOTE_AGE_SEC: number('.s-stale')
    };
    return api('/api/settings',
               {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({fields: fields})})
      .then(function (result) {
        if (!result.ok) { UI.toast(result.body.error); return; }
        var cold = result.body.restart_required || [];
        UI.toast(cold.length
          ? 'applied — except ' + cold.join(', ') + ', which the launcher ' +
            'only reads at startup'
          : 'applied to the running engine', 'ok');
        refresh();
      });
  }

  function saveAccount(row) {
    var name = row.dataset.account ||
      (row.querySelector('.f-name') || {}).value;
    if (!name) {
      UI.toast('an account needs a name before it can be saved');
      return;
    }
    var body = {
      terminal_path: row.querySelector('.f-terminal').value,
      login: row.querySelector('.f-login').value,
      server: row.querySelector('.f-server').value,
      endpoint: row.querySelector('.f-endpoint').value,
      password: row.querySelector('.f-password').value
    };
    return api('/api/accounts/' + encodeURIComponent(name),
               {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)}).then(function (result) {
      if (!result.ok) {
        // The refusal's own words — it names the field and, where it
        // can, the value to use instead.
        UI.toast(result.body.error);
        return;
      }
      // Saved: what is in the row is no longer an unsaved edit, so the
      // repaint below shows what the SERVER kept - the path cleaned of
      // any quotes - instead of carrying the pasted text back over it.
      Array.prototype.forEach.call(row.querySelectorAll('input'),
        function (input) { input.dataset.rendered = input.value; });
      if (result.body.terminal_missing) {
        // Saved, and still not launchable: say so plainly, not 'ok'.
        UI.toast('saved ' + name + ' \u2014 but ' +
                 result.body.terminal_missing);
      } else {
        UI.toast('saved ' + name + ' \u2014 the engine restarts by itself ' +
                 'to pick it up', 'ok');
      }
      // This row has BECOME an account. Clear it, or the draft the
      // repaint carries across would reappear in the next blank row.
      if (!row.dataset.account) {
        NEW_ACCOUNT_FIELDS.forEach(function (field) {
          var input = row.querySelector('.' + field);
          if (input) { input.value = ''; }
        });
      }
      refresh();
      // The readiness box too, now: it would otherwise go on saying
      // the old fault for up to one cycle after it was fixed.
      refreshConnection();
    });
  }

  function readDraft() {
    var form = document.querySelector('.pair-form');
    if (!form) { return; }
    function value(selector) {
      var input = form.querySelector(selector);
      return input ? input.value : '';
    }
    function checked(selector, fallback) {
      var input = form.querySelector(selector);
      return input ? input.checked : fallback;
    }
    local.draft = {
      key: value('.p-key') || local.draft.key,
      name: value('.p-name'),
      pair_type: value('.p-type'),
      hedge_ratio: parseFloat(value('.p-beta')) || local.draft.hedge_ratio,
      increment: value('.p-increment') === ''
        ? null : parseFloat(value('.p-increment')),
      default_quantity: parseFloat(value('.p-quantity')) || 1.0,
      quoting_leg: value('.p-quoting'),
      // Never a hard read: this runs on every repaint, and one missing
      // control must not take the whole form down with it.
      enabled: checked('.p-enabled', local.draft.enabled !== false),
      leg_a_account: value('.p-account-a'),
      leg_a_symbol: value('.p-symbol-a'),
      leg_b_account: value('.p-account-b'),
      leg_b_symbol: value('.p-symbol-b'),
      expiry: value('.p-expiry') || null
    };
  }

  function findSymbols(side) {
    readDraft();
    var account = local.draft['leg_' + side + '_account'];
    if (!account) {
      UI.toast('choose the account for leg ' + side.toUpperCase() +
               ' first — symbols are per broker');
      return;
    }
    var query = local.draft['leg_' + side + '_symbol'] || '';
    return api('/api/accounts/' + encodeURIComponent(account) +
               '/symbols?q=' + encodeURIComponent(query))
      .then(function (result) {
        local.symbols[side] = result.ok
          ? {symbols: result.body.symbols || []}
          : {error: result.body.error};
        render();
      });
  }

  function draftPayload() {
    var draft = local.draft;
    return {
      name: draft.name || draft.key,
      leg_a: {account: draft.leg_a_account, symbol: draft.leg_a_symbol},
      leg_b: {account: draft.leg_b_account, symbol: draft.leg_b_symbol},
      pair_type: draft.pair_type,
      hedge_ratio: draft.hedge_ratio,
      increment: draft.increment,
      default_quantity: draft.default_quantity,
      quoting_leg: draft.quoting_leg || null,
      enabled: draft.enabled,
      expiry: draft.expiry || null
    };
  }

  function derive() {
    readDraft();
    var key = local.editing || cleanKey(local.draft.key) || autoKey();
    if (!key) {
      UI.toast('give leg A and leg B a symbol first — the key is built '
               + 'from them');
      return;
    }
    return api('/api/pairs/' + encodeURIComponent(key) + '/derive',
               {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(draftPayload())})
      .then(function (result) {
        if (!result.ok) { UI.toast(result.body.error); return; }
        local.derived = result.body;
        render();
      });
  }

  function cleanKey(key) {
    /* A pair's key is an IDENTIFIER, and it is matched exactly — by the
     * snapshot, by every panel, and in the URL of every call about this
     * pair. Typed by hand it arrives with the spaces a person puts
     * round a separator, and `XAUUSD.f | GCZ6.f` is then a different
     * pair from the `XAUUSD.f|GCZ6.f` everything else writes.
     *
     * So it is tidied here rather than refused: same two symbols, one
     * spelling. */
    return String(key || '').trim().replace(/\s*\|\s*/g, '|');
  }

  function autoKey() {
    /* A pair's key IS its two symbols. Deriving it means the operator
     * never has to know that, and a key can never drift away from the
     * instruments it names. */
    var draft = local.draft || {};
    if (!draft.leg_a_symbol || !draft.leg_b_symbol) { return ''; }
    return draft.leg_a_symbol + '|' + draft.leg_b_symbol;
  }

  function openOn(key) {
    var live = ((UI.state.snapshot || {}).pairs || {})[key] || {};
    return (live.positions || []).length;
  }

  function savePair() {
    readDraft();
    var payload = draftPayload();
    if (!payload.leg_a.symbol || !payload.leg_b.symbol) {
      UI.toast('both legs need a symbol — Find lists what each account '
               + 'actually offers');
      return;
    }
    var key = local.editing || cleanKey(local.draft.key) || autoKey();
    // The symbols were changed on an existing pair: the key names the
    // instruments, so it moves with them rather than being left
    // pointing at something that is no longer traded here.
    var rename = (local.editing && autoKey() && autoKey() !== local.editing)
      ? autoKey() : null;
    if (rename && openOn(local.editing)) {
      UI.toast('this pair has an open position — flatten it before ' +
               'changing its symbols; the position is on the OLD ' +
               'instruments and nothing here can move it');
      return;
    }
    var target = rename || key;
    return api('/api/pairs/' + encodeURIComponent(target),
               {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)})
      .then(function (result) {
        if (!result.ok) { UI.toast(result.body.error); return; }
        if (!rename) { return result; }
        return api('/api/pairs/' + encodeURIComponent(local.editing),
                   {method: 'DELETE'}).then(function (dropped) {
          if (!dropped.ok) {
            UI.toast('saved as ' + target + ', but the old ' +
                     local.editing + ' could not be removed: ' +
                     dropped.body.error);
          }
          return result;
        });
      })
      .then(function (result) {
        if (!result) { return; }
        UI.toast(result.body.restart_required
          ? 'saved ' + target + ' — symbols, accounts and β are structural, ' +
            'so the engine restarts itself to pick them up'
          : 'saved ' + target, 'ok');
        local.editing = null;
        refresh();
      });
  }

  function afterWrite(result) {
    if (!result.ok) { UI.toast(result.body.error); return; }
    refresh();
  }

  window.MT5Settings = {render: function () { node(); render(true); },
                        refresh: refresh, state: local};
})();
