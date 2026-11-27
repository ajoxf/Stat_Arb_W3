/* The ladder, the Market Grid and the positions monitor.
 *
 * Everything on screen comes from ONE snapshot (/api/status), which the
 * coordinator publishes each poll — so the ladder cannot disagree with
 * the grid beside it, and the grid costs no extra MT5 round trips.
 * Everything the trader does goes out as a command (/api/command) and
 * is executed once by the coordinator.
 *
 * House rules, from the spec:
 *   - No native confirm() / alert() / prompt(). One shared modal.
 *   - Errors do not auto-hide. A failure that vanishes in 3s is missed.
 *   - Unmeasured is not zero: render an em dash.
 *   - Never send the operator to a log for a decision already made.
 */

(function () {
  'use strict';

  var REFRESH_MS = 300;
  var DASH = '—';

  var state = {
    snapshot: {pairs: {}, accounts: {}},
    open: [],            // panel ids in taskbar order
    closed: {},          // panels the TRADER closed — they stay closed
    reveal: null,        // a panel just opened: scroll it into view once
    active: null,
    // pair key -> {buy, sell}: a size per SIDE. They are usually the
    // same — the keypad sets both — but a desk that wants to lift 1 and
    // offer 5 can type each one, and the box each size sits in is
    // beside the button that will send it.
    armed: {},
    locked: {},          // pair key -> scroll is locked
    scrolledAt: {},      // pair key -> when the TRADER last scrolled it
    busyAt: {},          // pair key -> when the trader last TOUCHED it
    hovering: {},        // pair key -> the pointer is over its ladder
    centredAt: {},       // pair key -> when we last centred it on the mid
    centring: {},        // pair key -> that scroll event was ours
    centringAgain: {},   // pair key -> a centring is waiting on layout
    filtered: {},        // pair key -> hide rows with nothing on them
    monitorTab: 'positions',
    // The Analysis tab: the last report, when it was read, and what it
    // is filtered to. The backtest's answer is kept here too, because
    // the pane is redrawn on every snapshot.
    analysis: null, analysisAt: 0, analysisDays: 7, analysisPair: '',
    // NET P&L on the Analysis tab and its report: 'show', 'mask' (the
    // column is there, the dollars are dots) or 'hide' (left out). The
    // percentages stay. Per browser.
    analysisPnl: (function () {
      try {
        return window.localStorage.getItem('okxtrader.analysisPnl') || 'show';
      } catch (err) { return 'show'; }
    }()),
    // The SIGMA ZONE track's own range. A day by default: the rows are
    // a tick-by-tick record, and a day of them is the usual read.
    zoneDays: 1, zoneDate: '', deriving: false, deriveNote: null,
    showUnconfirmed: false,
    //: The notional table's own picked range of the broker's days,
    //: both inclusive; either alone is that one day.
    volumeFrom: '', volumeTo: '',
    //: The pair charts: the last series read, when, and how much of
    //: the session each one draws.
    charts: {}, chartFetch: {}, chartTf: {}, chartHist: {},
    chartHistFetch: {}, chartHistNote: {},
    analysisBt: null, analysisBtDays: 5,
    // What the Reconciler tab's adopt form has selected. IN STATE, not
    // in the DOM: that pane is re-rendered from innerHTML on every
    // snapshot, so a half-filled form left in the markup would be
    // cleared under the operator's hands twice a second.
    adopt: {pair: '', a: '', b: ''},
    // Clicks that have been sent but are not in a snapshot yet. The
    // round trip is ~25ms plus the broker, but the ladder must show the
    // order the INSTANT it is clicked: a trader who cannot see their
    // click clicks again.
    pending: [],
    help: false,
    // Banners the trader has put away, by what they were ABOUT: the
    // same banner returns when the situation behind it changes.
    dismissed: {},
    // Dead orders already said out loud, by id: a refusal is said once
    // and then stays in the Working Orders tab.
    reported: {},
    // The one-key shortcuts, which a desk may not want at all: B and S
    // are orders, and a keyboard nobody meant to touch is a click
    // nobody meant to make.
    keysOff: false,
    // Sound on placed / filled / cancelled, and how many positions the
    // last snapshot had — a resting order that fills is a fill nobody
    // clicked for.
    soundOff: false,
    positionCount: null,
    // The journal, from the database. Not in the snapshot: it is the
    // BROKER's record, it outlives the process, and it is read on its
    // own slow clock rather than three times a second.
    fills: null,
    fillsAt: 0,
    fillsFilter: {ours: false},
    // The slippage report, over the session that is actually running.
    // Same treatment as the journal: recorded, not live, so it is read
    // on its own slow clock.
    // The coordinator's own publish clock, from the last snapshot: the
    // panels are redrawn when this MOVES, not on every poll.
    lastAt: null,
    slippage: null,
    slippageAt: 0,
    slippageAll: false
  };

  // -- plumbing -------------------------------------------------------

  function el(id) { return document.getElementById(id); }

  function fmt(value, digits) {
    if (value === null || value === undefined || value === '') return DASH;
    if (typeof value !== 'number') return String(value);
    if (!isFinite(value)) return DASH;
    return value.toFixed(digits === undefined ? 4 : digits);
  }

  // A SIZE, printed the way it was typed.
  //
  // Quantities are added and subtracted — a click walking down a stack
  // of positions is a chain of subtractions — and binary floats do not
  // survive that cleanly. `0.15 - 0.1` is 0.04999999999999999, and the
  // Working Orders panel printed exactly that beside a clean 0.1, which
  // reads as the size having been changed on the way to the broker. The
  // engine tidies the arithmetic now; this makes sure nothing on the
  // way to the screen can put the dust back.
  //: Below this, a net position is FLAT. Three 0.01 buys covered by
  //: three 0.01 sells leaves -3.4e-18 in binary floating point, and
  //: `if (!net)` is false for that - so the ladder offered to close a
  //: position that does not exist and printed a seventeen-digit
  //: nothing where it should have said "flat". The engine sweeps this
  //: too; the screen must not depend on that being perfect.
  var FLAT_EPSILON = 1e-9;

  function isFlat(net) {
    return !net || Math.abs(net) < FLAT_EPSILON;
  }

  function qty(value) {
    if (value === null || value === undefined || value === '') return DASH;
    var number = Number(value);
    if (!isFinite(number)) return DASH;
    return String(parseFloat(number.toFixed(6)));
  }

  function escapeHtml(value) {
    // Symbols, comments and broker refusals are the broker's text, not
    // ours, and every one of them lands in innerHTML.
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function money(value) {
    if (value === null || value === undefined || !isFinite(value)) return DASH;
    var sign = value < 0 ? '-' : '';
    // Grouped: an account equity of 100000.00 is a number nobody reads
    // at a glance, and this column is read at a glance.
    return sign + '$' + Math.abs(value).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2});
  }

  function toastStamp(at) {
    /* WHEN, at the head of every box: a wall of alerts is a history,
     * and a history with no times cannot be read in order. This PC's
     * clock, date and time to the second. `at` is
     * the engine's epoch seconds - the moment it happened, not the
     * moment the page drew it. */
    var when = typeof at === 'number' ? new Date(at * 1000) : new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var time = pad(when.getHours()) + ':' + pad(when.getMinutes()) + ':' +
      pad(when.getSeconds());
    time = when.getFullYear() + '-' + pad(when.getMonth() + 1) + '-' +
      pad(when.getDate()) + ' ' + time;
    var stamp = document.createElement('span');
    stamp.className = 'toast-time';
    stamp.textContent = time;
    return stamp;
  }

  function toast(message, kind) {
    var box = document.createElement('div');
    box.className = 'toast' + (kind === 'ok' ? ' ok' : '');
    box.innerHTML = '<span class="dismiss">&times;</span>';
    box.appendChild(toastStamp());
    box.appendChild(document.createTextNode(message));
    // Errors used to stay until dismissed, on purpose — so that a
    // refusal could not scroll past unread. But a refusal that REPEATS
    // (a broker refusing every order, a leg that will not quote) built
    // a wall of identical boxes over the ladder, each needing its own
    // click. That buries the market behind the message about it.
    //
    // So errors clear too, just slower: long enough to read twice,
    // and still dismissable on click. Anything that must not be missed
    // belongs in the checklist or the banner, which persist properly,
    // rather than in a box that was always going to disappear.
    box.addEventListener('click', function () { box.remove(); });
    el('toasts').appendChild(box);
    window.setTimeout(function () { box.remove(); },
                      kind === 'ok' ? 4000 : 10000);
  }

  function ask(title, body, confirmLabel, onConfirm, alt) {
    /* `alt` is an optional second answer, {label, run}: the one
     * question with two — close the Algo's position, or hand it over. */
    el('modal-title').textContent = title;
    el('modal-body').textContent = body;
    el('modal-confirm').textContent = confirmLabel;
    var other = el('modal-alt');
    if (other) {
      other.hidden = !alt;
      other.textContent = alt ? alt.label : '';
    }
    el('modal').classList.remove('hidden');
    el('modal').dataset.pending = '1';
    el('modal')._onConfirm = onConfirm;
    el('modal')._onAlt = alt ? alt.run : null;
  }

  function closeModal() {
    el('modal').classList.add('hidden');
    el('modal')._onConfirm = null;
    el('modal')._onAlt = null;
    if (el('modal-alt')) { el('modal-alt').hidden = true; }
  }

  // -- sound -------------------------------------------------------------
  //
  // Generated here, with WebAudio: no files, nothing fetched, nothing
  // that a blocked CDN or a missing asset can silence. Three events, and
  // they are deliberately different in shape rather than in pitch alone
  // — a desk hears these across a room and must not have to work out
  // which one it was.
  //
  //   placed     one short blip
  //   filled     two notes, rising — the one that means money moved
  //   cancelled  one low, flat note
  //
  // Off is a first-class setting: some desks want silence, and a sound
  // nobody asked for is worse than none.

  var TONES = {
    placed: [{hz: 880, ms: 70, gain: 0.05}],
    filled: [{hz: 660, ms: 80, gain: 0.07}, {hz: 1040, ms: 130, gain: 0.07}],
    cancelled: [{hz: 320, ms: 110, gain: 0.05}],
    refused: [{hz: 180, ms: 200, gain: 0.06}],
    // SIGNALS mode: rising for an entry, falling for an exit - each its
    // own, so the trader knows which without looking.
    signal_entry: [{hz: 523, ms: 110, gain: 0.08}, {hz: 659, ms: 110, gain: 0.08},
                   {hz: 784, ms: 180, gain: 0.08}],
    signal_exit: [{hz: 784, ms: 110, gain: 0.08}, {hz: 659, ms: 110, gain: 0.08},
                  {hz: 523, ms: 180, gain: 0.08}],
    // A market about to open: a bell, two even strokes - nothing like
    // a fill or a signal, so it is never mistaken for one.
    session: [{hz: 988, ms: 220, gain: 0.07}, {hz: 988, ms: 220, gain: 0.07}]
  };
  var audio = null;

  function sound(name) {
    if (state.soundOff) { return; }
    var tones = TONES[name];
    if (!tones) { return; }
    try {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) { return; }
      audio = audio || new Ctor();
      // Browsers start the context suspended until a gesture. Every
      // sound here follows a click, so this resolves on the first one.
      if (audio.state === 'suspended') { audio.resume(); }
      var at = audio.currentTime;
      tones.forEach(function (tone) {
        var osc = audio.createOscillator();
        var gain = audio.createGain();
        osc.type = 'sine';
        osc.frequency.value = tone.hz;
        gain.gain.setValueAtTime(tone.gain, at);
        // Ramped down rather than cut: a square edge clicks.
        gain.gain.exponentialRampToValueAtTime(0.0001, at + tone.ms / 1000);
        osc.connect(gain).connect(audio.destination);
        osc.start(at);
        osc.stop(at + tone.ms / 1000);
        at += tone.ms / 1000;
      });
    } catch (e) {
      // No audio device, or a policy that forbids it. Never a reason
      // for anything on this screen to stop working.
    }
  }

  function send(kind, payload, then) {
    return fetch('/api/command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: kind, payload: payload || {}})
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data.ok) {
          // The refusal's OWN words, on screen. Never "check the log".
          toast(data.error || 'the command was refused');
          return null;
        }
        return pollResult(data.id, then);
      });
    }).catch(function (error) {
      toast('could not reach the web process: ' + error.message);
    });
  }

  function soundFor(result) {
    /* What the ENGINE says happened, not what the click intended: a
     * market click that was refused must not sound like a fill. */
    if (!result) { return; }
    var data = result.data || {};
    if (result.ok === false || data.refused) { return sound('refused'); }
    if (data.position) { return sound('filled'); }
    if (data.order) { return sound('placed'); }
    if (data.cancelled !== undefined || data.closed !== undefined) {
      return sound('cancelled');
    }
  }

  function stickyToast(message, at) {
    /* A SIGNALS alert: it stays until the trader dismisses it. The
     * trade it asks for is theirs to make, so it must not scroll away
     * unread the way an ordinary outcome does. */
    var box = document.createElement('div');
    box.className = 'toast signal';
    box.innerHTML = '<span class="dismiss">&times;</span>';
    box.appendChild(toastStamp(at));
    box.appendChild(document.createTextNode(message));
    box.addEventListener('click', function () { box.remove(); });
    el('toasts').appendChild(box);
  }

  function announceSignals() {
    /* Each ladder's SIGNALS alert, said ONCE: by its id, never by its
     * text. On a page that has just loaded, one already showing is
     * taken as seen unless it is fresh - a reload never replays the
     * morning's alerts. */
    var snapshot = state.snapshot;
    state.signalSeen = state.signalSeen || {};
    Object.keys(snapshot.pairs || {}).forEach(function (key) {
      var row = snapshot.pairs[key];
      var alert = (row.algo_block || {}).signal_alert;
      if (!alert || !alert.id) { return; }
      var seen = state.signalSeen[key];
      state.signalSeen[key] = alert.id;
      if (seen === alert.id) { return; }
      if (seen === undefined && typeof alert.at === 'number' &&
          typeof snapshot.at === 'number' && snapshot.at - alert.at > 30) {
        return;                       // on load, and not fresh: seen
      }
      stickyToast((row.name || key) + ': ' + alert.text, alert.at);
      sound(alert.kind === 'ENTRY' ? 'signal_entry' : 'signal_exit');
      // The ladder's frame flashes - only if its window is open.
      var node = document.querySelector(
        '.ladder[data-pair="' + cssEscape(key) + '"]');
      if (node) {
        node.classList.add('signal-flash');
        window.setTimeout(function () {
          node.classList.remove('signal-flash');
        }, 3000);
      }
    });
  }

  function toastOutcome(result) {
    /* What the engine said, in the style that says which it was.
     *
     * A REASON IS NOT A REFUSAL. An error toast stays on the screen
     * until it is dismissed, on purpose — and an ordinary outcome
     * ("resting at 7.69 to CLOSE 2 position(s), then open 93") was
     * going out in exactly that style, so a click that did precisely
     * what it was asked read as a fault and sat there until somebody
     * clicked it away.
     */
    var data = (result && result.data) || {};
    if (result && result.ok === false) {
      return toast(result.error || 'the engine refused that');
    }
    /* THE ENGINE'S OWN `ok`, WHICH IS NOT THE ENVELOPE'S.
     *
     * The envelope says whether the command RAN; the payload says
     * whether the broker did the thing. A close that the broker
     * refused came back {ok: false, error: '3677: ...'} inside an
     * envelope that ran fine, and this function had no branch for it:
     * no toast, no style, nothing. The operator clicked "Close it" on
     * an unclaimed position, the broker said no, and the screen said
     * NOTHING — which is what "there is no way to close it" looks
     * like from the desk. A refusal carries the broker's own words
     * (spec §11), and it has to reach the screen to do that.
     */
    if (data.ok === false) {
      return toast(data.error || data.reason || 'the broker refused that');
    }
    if (data.refused) { return toast(data.reason || 'refused'); }
    if (data.reason) { return toast(data.reason, 'ok'); }
  }

  function pollResult(id, then, tries) {
    tries = tries || 0;
    return fetch('/api/result/' + id).then(function (r) { return r.json(); })
      .then(function (result) {
        if (result && result.pending) {
          if (tries > 20) { return null; }
          return new Promise(function (resolve) {
            window.setTimeout(function () {
              resolve(pollResult(id, then, tries + 1));
            }, 150);
          });
        }
        soundFor(result);
        toastOutcome(result);
        if (then) { then(result); }
        return result;
      });
  }

  // -- panels ---------------------------------------------------------

  function panelId(kind, key) { return kind + ':' + (key || ''); }

  function openPanel(id) {
    delete state.closed[id];
    if (state.open.indexOf(id) < 0) { state.open.push(id); }
    state.active = id;
    // ...and SHOW it. The desktop is a row that scrolls: with two
    // ladders and the grid already on it, a window opened at the end
    // sits off the right-hand edge, and "Positions opens into nothing"
    // is what that looks like from the outside.
    state.reveal = id;
    writeLayout();
    render();
  }

  function closePanel(id) {
    state.open = state.open.filter(function (other) { return other !== id; });
    // Closing is a decision, and it is remembered: a ladder that came
    // back on the next poll would be a window that cannot be closed.
    state.closed[id] = true;
    writeLayout();
    render();
  }

  function panelIdOf(node) {
    /* Which panel a window element IS. One definition, because three
     * copies of this drifting apart is how a window ends up impossible
     * to close, focus or move. */
    if (!node) { return null; }
    return node.classList.contains('market-grid') ? panelId('grid')
      : node.classList.contains('monitor') ? panelId('monitor')
        : node.classList.contains('settings') ? panelId('settings')
        : node.classList.contains('managed') ? panelId('managed')
        : node.classList.contains('econcal') ? panelId('econcal')
          : node.classList.contains('chartwin')
            ? panelId('chart', node.dataset.pair)
          : node.classList.contains('calcwin')
            ? panelId('calc', node.dataset.pair)
          : node.classList.contains('fairwin')
            ? panelId('fair', node.dataset.pair)
            : node.classList.contains('algowin')
              ? panelId('algo', node.dataset.pair)
              : panelId('ladder', node.dataset.pair);
  }

  // -- moving the windows ------------------------------------------------
  //
  // Drag a title bar and the window goes where it is put, over the
  // others, and stays there across a reload. Two rules the reference
  // screen has and this needs for the same reason:
  //
  //   - A window can NEVER be dropped where it cannot be got back. The
  //     title bar is kept on screen at both edges, so there is always
  //     something left to grab.
  //   - Dragging is not clicking. A drag that started on a title bar
  //     never reaches the ladder underneath, and the ladder is where a
  //     click places an order.
  //
  // The layout is per browser, in localStorage: it is a preference
  // about this screen, not state the engine should ever be asked about.

  var LAYOUT_KEY = 'okxtrader.windows.v1';
  //: How much of a window must stay on the desktop, in pixels.
  var KEEP_VISIBLE_PX = 90;
  var layout = readLayout();
  //: Whether this browser came with a layout of its own. Decided at
  //: load: the first redraw writes entries of its own, and reading
  //: 'is the layout empty?' after it made a fresh browser ignore the
  //: desk's saved layout whenever the redraw beat the answer.
  var layoutWasLocal = Object.keys(layout).length > 0;
  var topZ = 10;
  //: Windows stack inside this band; the modal, the menu and the toasts
  //: live above it in ladder.css. topZ climbed with every click and
  //: never came back down, so after enough clicks a window passed the
  //: dialog and a Delete confirmation opened BEHIND the window that
  //: asked for it — a question nobody can answer and nothing to
  //: dismiss.
  var MAX_WINDOW_Z = 900;

  function nextZ() {
    if (topZ < MAX_WINDOW_Z) {
      topZ += 1;
      return topZ;
    }
    // At the ceiling: renumber from the bottom instead of climbing into
    // the dialog's band. The ORDER windows are stacked in is what the
    // operator arranged; only the numbers shrink.
    var nodes = Array.prototype.slice.call(
      document.querySelectorAll('.window')).filter(function (node) {
        return node.style.zIndex;
      });
    nodes.sort(function (a, b) {
      return (parseInt(a.style.zIndex, 10) || 0) -
             (parseInt(b.style.zIndex, 10) || 0);
    });
    topZ = 10;
    nodes.forEach(function (node) {
      topZ += 1;
      node.style.zIndex = topZ;
      var id = panelIdOf(node);
      if (layout[id]) { layout[id].z = topZ; }
    });
    topZ += 1;
    return topZ;
  }

  function readLayout() {
    try {
      return JSON.parse(window.localStorage.getItem(LAYOUT_KEY) || '{}') || {};
    } catch (e) {
      return {};                     // private mode, or a corrupt value
    }
  }

  //: The windows whose being OPEN is part of the layout: the tools a
  //: trader arranges (ladders open by themselves, the Algo window with
  //: its Algo).
  var SAVED_KINDS = ['chart', 'calc', 'monitor', 'grid'];

  function writeLayout() {
    var tools = state.open.filter(function (id) {
      return SAVED_KINDS.indexOf(id.split(':')[0]) >= 0;
    });
    if (tools.length) { layout._open = tools; } else { delete layout._open; }
    try {
      window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    } catch (e) {
      // A layout that cannot be saved is a layout that does not survive
      // a reload. Nothing else about the screen depends on it.
    }
    saveLayoutToDesk();
  }

  // ...and on the desk itself, a second after the last change: it
  // survives a cleared browser and follows the desk to another screen.
  var layoutSaveTimer = null;
  var layoutSaved = null;
  function saveLayoutToDesk() {
    // At most once a second, and only what changed: some windows write
    // their layout on every redraw (three a second), which would keep a
    // 'wait for a quiet second' from ever saving at all.
    if (layoutSaveTimer) { return; }
    layoutSaveTimer = window.setTimeout(function () {
      var body = JSON.stringify({layout: layout});
      if (body === layoutSaved) { layoutSaveTimer = null; return; }
      layoutSaved = body;
      layoutSaveTimer = null;
      try {
        fetch('/api/layout', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: body}).catch(function () {});
      } catch (e) { /* a preference: never in the way */ }
    }, 1000);
  }

  function loadLayoutFromDesk() {
    /* This browser has no layout of its own (a new browser, a cleared
     * cache): take the desk's. */
    if (layoutWasLocal) { return; }
    fetch('/api/layout').then(function (r) { return r.json(); })
      .then(function (body) {
        if (!body || !body.layout || !Object.keys(body.layout).length) {
          return;
        }
        // The desk's places win over the defaults the first redraws
        // gave; anything only those redraws know is kept.
        layout = Object.assign({}, layout, body.layout);
        layoutWasLocal = true;
        try {
          window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
        } catch (e) { /* ignore */ }
        state.layoutRestored = false;
        render();
      }).catch(function () {});
  }

  function restoreOpenTools(snapshot) {
    /* Once, on the first snapshot: the tools that were open when the
     * layout was last saved come back, where the ladder they belong to
     * is still on the desk. */
    if (state.layoutRestored) { return; }
    state.layoutRestored = true;
    (layout._open || []).forEach(function (id) {
      var kind = id.split(':')[0], key = id.split(':').slice(1).join(':');
      if (SAVED_KINDS.indexOf(kind) < 0) { return; }
      if (key && !(snapshot.pairs || {})[key]) { return; }
      if (state.open.indexOf(id) < 0 && !state.closed[id]) {
        state.open.push(id);
      }
    });
  }

  function desktopBox() {
    return el('desktop').getBoundingClientRect();
  }

  function desktopOrigin() {
    /* Where a floating window's `left: 0` actually SITS on the screen.
     *
     * The desktop scrolls sideways (`overflow-x: auto`) and is the
     * containing block for every floating window, so `style.left` is
     * measured from the scrolled CONTENT origin. A bounding rect is
     * measured from the VISIBLE edge. Those two differ by exactly
     * `scrollLeft`, and a drag that reads one and writes the other
     * moves the window by the scroll distance in the wrong direction
     * — which reads as a window that runs away from the cursor.
     */
    var node = el('desktop');
    var rect = node.getBoundingClientRect();
    return {left: rect.left - node.scrollLeft,
            top: rect.top - node.scrollTop};
  }

  function clampTo(node, left, top) {
    var desktop = el('desktop');
    var width = node.offsetWidth || 200;
    // The right-hand stop is the edge of what the trader can SEE right
    // now, which is the scrolled viewport rather than the desktop's
    // own width. Clamping to the width alone pinned every window into
    // the first screenful: drag one past the fold and it sprang back.
    // Deliberately NOT scrollWidth — a floating window contributes to
    // that, so the ceiling would grow as the window approached it and
    // the window would never stop.
    var rightStop = desktop.scrollLeft + desktop.clientWidth
      - KEEP_VISIBLE_PX;
    var bottomStop = desktop.scrollTop + desktop.clientHeight - 24;
    return {
      left: Math.min(Math.max(left, KEEP_VISIBLE_PX - width),
                     Math.max(rightStop, 0)),
      // Never above the desktop: a title bar under the banner cannot be
      // grabbed at all.
      top: Math.min(Math.max(top, 0), Math.max(bottomStop, 0))
    };
  }

  // -- snapping ------------------------------------------------------------
  //
  // A window dragged within SNAP_PX of another window's edge, or of the
  // desk's, lands exactly on it: side by side, edge to edge, tops and
  // bottoms in line - the tiled desk, without placing each by hand.

  //: How close an edge must come to be caught, in pixels.
  var SNAP_PX = 10;
  //: The gap left between two windows snapped side by side.
  var SNAP_GAP = 2;

  function otherBoxes(node) {
    /* Every other visible window, in the desk's own (scrolled) frame. */
    var origin = desktopOrigin();
    var out = [];
    Array.prototype.forEach.call(document.querySelectorAll('#desktop > .window'),
      function (other) {
        if (other === node || other.classList.contains('maximized') ||
            !other.offsetWidth) { return; }
        var r = other.getBoundingClientRect();
        out.push({left: r.left - origin.left, top: r.top - origin.top,
                  right: r.right - origin.left, bottom: r.bottom - origin.top});
      });
    return out;
  }

  function nearest(value, candidates) {
    var best = null;
    candidates.forEach(function (c) {
      var d = Math.abs(c - value);
      if (d <= SNAP_PX && (best === null || d < Math.abs(best - value))) {
        best = c;
      }
    });
    return best === null ? value : best;
  }

  function snapTo(node, left, top) {
    var w = node.offsetWidth, h = node.offsetHeight;
    var xs = [0], ys = [0];
    otherBoxes(node).forEach(function (b) {
      // Beside it (either side), or edges in line with it.
      xs.push(b.right + SNAP_GAP, b.left - w - SNAP_GAP, b.left,
              b.right - w);
      // Under or over it, or tops / bottoms in line.
      ys.push(b.bottom + SNAP_GAP, b.top - h - SNAP_GAP, b.top,
              b.bottom - h);
    });
    return {left: nearest(left, xs), top: nearest(top, ys)};
  }

  function snapEdges(node, width, height) {
    /* Resizing: the right and bottom edges catch on the others'. */
    var origin = desktopOrigin();
    var r = node.getBoundingClientRect();
    var left = r.left - origin.left, top = r.top - origin.top;
    var rights = [], bottoms = [];
    otherBoxes(node).forEach(function (b) {
      rights.push(b.left - SNAP_GAP, b.right);
      bottoms.push(b.top - SNAP_GAP, b.bottom);
    });
    return {w: nearest(left + width, rights) - left,
            h: nearest(top + height, bottoms) - top};
  }

  function place(node, left, top) {
    var at = clampTo(node, left, top);
    node.classList.add('floating');
    node.style.left = at.left + 'px';
    node.style.top = at.top + 'px';
    return at;
  }

  function ensureGrip(node) {
    /* The corner every window is resized by. Added here rather than in
     * the template because the monitor, the grid and the settings page
     * build their own markup — one grip, added the same way to all of
     * them, is one behaviour to get right. */
    if (node.querySelector(':scope > .grip')) { return; }
    var grip = document.createElement('div');
    grip.className = 'grip';
    grip.title = 'Drag to resize this window';
    node.appendChild(grip);
  }

  function size(node, width, height) {
    var desktop = desktopBox();
    var w = Math.max(240, Math.min(width, desktop.width));
    var h = Math.max(120, Math.min(height, desktop.height));
    node.classList.add('sized');
    node.style.width = w + 'px';
    node.style.height = h + 'px';
    return {w: w, h: h};
  }

  //: The windows that are TOOLS rather than ladders. They are wide,
  //: they are opened and closed all day, and putting them at the end of
  //: the desktop row pushes everything else off the screen: the trader
  //: then scrolls sideways past every ladder to reach the one they
  //: wanted. They float over the ladders instead, in view, and can be
  //: dragged and resized like anything else.
  var FLOATING_BY_DEFAULT = ['grid:', 'monitor:', 'settings:', 'managed:',
                             'econcal:'];

  function floatByDefault(node, id) {
    var desktop = desktopBox();
    // Sizes come from the VISIBLE desktop; positions are written in
    // content coordinates, so they are measured from the scrolled
    // origin. Mixing the two puts a tool window off-screen the moment
    // the desk has been scrolled sideways.
    var origin = desktopOrigin();
    if (!desktop.width) { return; }
    // THE SIZE IS THE ROOM THERE IS, not a flat 1180 x 560.
    //
    // A tool window opened at 560px on a 900px screen left a third of
    // the display as bare desktop below it while the Analysis tab -
    // which is several screens long - scrolled inside a short box.
    // The width was capped at 1180 on the reasoning that a wider
    // table is read by scrolling anyway; the journals put the levels
    // in columns and are 17 wide, so on a 1800px desk that cap threw
    // away 500px of screen the trader had. Both ceilings are now the
    // desk's own size, less a margin.
    var wanted = size(node, Math.max(desktop.width - 80, 320),
                      Math.max(desktop.height - 80, 200));
    // Stepped down and across so a second and third one do not land
    // exactly on top of the first.
    // Beside the tiled ladders where there is room for it, so opening
    // Positions does not cover the market. Over them when there is not
    // — it is a window, and it can be dragged.
    var index = FLOATING_BY_DEFAULT.indexOf(id);
    var free = 0;
    Array.prototype.forEach.call(document.querySelectorAll('.window.ladder'),
      function (ladder) {
        if (ladder.classList.contains('floating')) { return; }
        var box = ladder.getBoundingClientRect();
        free = Math.max(free, box.right - origin.left + 8);
      });
    var left = Math.max(Math.min(free + index * 24,
                                 el('desktop').scrollLeft + desktop.width
                                 - wanted.w - 8), 8);
    var top = Math.min(30 + index * 24,
                       Math.max(desktop.height - wanted.h - 8, 0));
    var at = place(node, left, top);
    node.style.zIndex = nextZ();
    // `auto`: this size was CHOSEN BY US, not by the trader. A size
    // the trader dragged is theirs and is restored exactly; one we
    // picked is recomputed on every open, so a saved default from a
    // smaller screen - or from before this default grew - does not
    // leave a strip of bare desktop under the window for good.
    layout[id] = {left: at.left, top: at.top, z: topZ,
                  w: wanted.w, h: wanted.h, auto: true};
    writeLayout();
  }

  function fairFloatByDefault(node, id) {
    /* A small window, landing clear of the ladders rather than over the
     * prices. It is dragged and resized like anything else once the
     * trader has put it somewhere. */
    var desktop = desktopBox();
    if (!desktop.width) { return; }
    // NEITHER dimension is set. The window is as wide as its widest row
    // and as tall as its figures — fix either one and the difference
    // comes back as empty grey, beside a ladder, where screen is not
    // spare. It is still dragged and resized like anything else; that
    // is when a size gets remembered.
    node.classList.add('sized');
    node.style.width = '';
    node.style.height = 'auto';
    var open = document.querySelectorAll('.window.fairwin').length;
    var width = node.getBoundingClientRect().width || 240;
    var left = Math.max(desktop.width - width - 12 - (open - 1) * 18, 8);
    var top = Math.min(30 + (open - 1) * 20, Math.max(desktop.height - 260, 0));
    var at = place(node, left, top);
    node.style.zIndex = nextZ();
    layout[id] = {left: at.left, top: at.top, z: topZ};
    writeLayout();
  }

  function chartFloatByDefault(node, id) {
    /* A chart is wide and short: two plots one above the other, beside
     * the ladders where there is room, stepped so a second one does
     * not land on the first. Dragged and resized like anything else. */
    var desktop = desktopBox();
    if (!desktop.width) { return; }
    var wanted = size(node, Math.min(660, desktop.width - 16),
                      Math.min(520, desktop.height - 16));
    var open = document.querySelectorAll('.window.chartwin').length;
    var left = Math.max(el('desktop').scrollLeft + desktop.width -
                        wanted.w - 12 - (open - 1) * 24, 8);
    var top = Math.min(30 + (open - 1) * 24,
                       Math.max(desktop.height - wanted.h - 8, 0));
    var at = place(node, left, top);
    node.style.zIndex = nextZ();
    layout[id] = {left: at.left, top: at.top, z: topZ,
                  w: wanted.w, h: wanted.h, auto: false};
    writeLayout();
  }

  function applyLayout(node) {
    /* Put a window back where it was left. Called for every window on
     * every render, because panels are created lazily — the monitor
     * opened an hour later must still come back to its own corner. */
    var id = panelIdOf(node);
    var saved = layout[id];
    if (!saved) {
      // TIDY put this window in the row, and there it stays until the
      // trader moves it. Floating it off to its default corner on the
      // next render shifted the whole row under the pointer: a ladder
      // grabbed just after Tidy jumped sideways by the fair window's
      // width. Marked on the ELEMENT, so a window closed and opened
      // again is a new one and lands where new ones land.
      if (node.dataset.tidied) { return; }
      if (FLOATING_BY_DEFAULT.indexOf(id) >= 0) { floatByDefault(node, id); }
      else if (node.classList.contains('fairwin') ||
               node.classList.contains('algowin') ||
               node.classList.contains('calcwin')) {
        fairFloatByDefault(node, id);
      }
      else if (node.classList.contains('chartwin')) {
        chartFloatByDefault(node, id);
      }
      return;
    }
    // `!== false`, not `=== true`: a layout saved before this flag
    // existed has neither, and it is far more likely to be the old
    // 560px default than a size somebody chose. Those windows take
    // the room there is once, and a drag after that is remembered.
    if (saved.auto !== false && FLOATING_BY_DEFAULT.indexOf(id) >= 0) {
      // Ours, not theirs: take the room there is now.
      var desktop = desktopBox();
      if (desktop.width) {
        var fresh = size(node, Math.max(desktop.width - 80, 320),
                         Math.max(desktop.height - 80, 200));
        layout[id] = Object.assign({}, saved, {w: fresh.w, h: fresh.h});
        writeLayout();
      }
    }
    else if (saved.w && saved.h) { size(node, saved.w, saved.h); }
    else if (saved.w) {
      // Width remembered, height left to the content — what the fair
      // window is given until the trader drags it themselves.
      node.classList.add('sized');
      node.style.width = Math.max(200, saved.w) + 'px';
      node.style.height = 'auto';
    }
    if (saved.left === undefined) { return; }   // resized, never moved
    var at = place(node, saved.left, saved.top);
    node.style.zIndex = saved.z || 10;
    // Clamped: a layout saved before the ceiling existed can carry a z
    // from the dialog's band, and reading it back would put us straight
    // over the modal again.
    topZ = Math.min(MAX_WINDOW_Z, Math.max(topZ, saved.z || 10));
    // Clamped on the way in as well as on the way out: a layout saved
    // on a big monitor must not hide a window on a laptop.
    if (at.left !== saved.left || at.top !== saved.top) {
      // Moved back onto the screen, its SIZE kept: dropping it here
      // shrank a tiled window to its default the first time it was
      // pulled in from a bigger monitor's edge.
      layout[id] = Object.assign({}, saved,
                                 {left: at.left, top: at.top,
                                  z: saved.z || 10});
      writeLayout();
    }
  }

  function raise(node) {
    var id = panelIdOf(node);
    if (!layout[id] || layout[id].left === undefined) {
      return;                           // still in the row; nothing to raise
    }
    node.style.zIndex = nextZ();
    layout[id].z = topZ;
    writeLayout();
  }

  // -- Tidy: the desk tiled -------------------------------------------------
  //
  // One click lays every open window out edge to edge, filling the desk:
  //
  //   | ladder | ladder | Algo | Algo |  chart   |
  //   |        |        |-------------|----------|
  //   |        |        |   Monitor   |  chart   |
  //
  // Ladders full height on the left; the Algo windows beside them with
  // the Trading Monitor under them across the same width; charts, the
  // calculators and any other tool stacked down the right, sharing the
  // height. Every window gets its place and size in the layout, so the
  // arrangement is saved like one made by hand.

  //: The width a ladder and an Algo window are given.
  var TILE_LADDER_W = 380;
  var TILE_ALGO_W = 380;
  //: The Monitor's width when there is no Algo window over it.
  var TILE_MONITOR_W = 760;
  //: The least the right-hand column is given; past the screen's edge
  //: the desk scrolls to it.
  var TILE_RIGHT_MIN_W = 480;
  //: The share of the height the Algo windows take over the Monitor.
  var TILE_ALGO_SHARE = 0.62;
  //: The most Tidy narrows the ladders and the middle column to fit
  //: the charts on the screen: never below 85% of their width.
  var TILE_MIN_SCALE = 0.85;
  //: The least the middle column is given while the Monitor is in it:
  //: its tables need the room, and the Algo windows widen to match.
  var TILE_MIDDLE_MIN_W = 560;
  //: How the right-hand column's height is shared: a chart is two plots
  //: and takes twice a calculator's share.
  var TILE_WEIGHT = {chart: 2, calc: 1, fair: 1, grid: 1.5, settings: 1.5};

  function tileWindows() {
    var desktop = el('desktop');
    var W = desktop.clientWidth - 4, H = desktop.clientHeight - 4;
    if (W <= 0 || H <= 0) { return tidyWindows(); }
    var gap = SNAP_GAP;
    var groups = {ladder: [], algo: [], monitor: [], right: []};
    Array.prototype.forEach.call(
      document.querySelectorAll('#desktop > .window'), function (node) {
        var kind = panelIdOf(node).split(':')[0];
        if (kind === 'ladder') { groups.ladder.push(node); }
        else if (kind === 'algo') { groups.algo.push(node); }
        else if (kind === 'monitor') { groups.monitor.push(node); }
        else { groups.right.push(node); }
      });
    // In the taskbar's order, so Tidy is the same every time.
    function byTab(a, b) {
      return state.open.indexOf(panelIdOf(a)) -
        state.open.indexOf(panelIdOf(b));
    }
    Object.keys(groups).forEach(function (k) { groups[k].sort(byTab); });
    state.maximized = {};
    var placed = [];
    function put(node, left, top, w, h) {
      placed.push({node: node, left: Math.round(left), top: Math.round(top),
                   w: Math.max(240, Math.round(w)),
                   h: Math.max(120, Math.round(h))});
    }
    // EVERYTHING ON ONE SCREEN where it can be: with two ladders, two
    // Algo windows and the charts, the right-hand column would start
    // past the edge - so the ladders and the middle column give up to
    // 15% of their width first.
    var algos = groups.algo.length;
    var middleW = algos ? algos * TILE_ALGO_W + (algos - 1) * gap
      : (groups.monitor.length ? TILE_MONITOR_W : 0);
    if (groups.monitor.length) {
      middleW = Math.max(middleW, TILE_MIDDLE_MIN_W);
    }
    var ladderW = TILE_LADDER_W;
    var leftW = groups.ladder.length * (ladderW + gap) +
      (middleW ? middleW + gap : 0);
    var wantRight = groups.right.length ? TILE_RIGHT_MIN_W : 0;
    if (leftW + wantRight > W && leftW > 0) {
      var scale = Math.max(TILE_MIN_SCALE, (W - wantRight) / leftW);
      ladderW = Math.floor(ladderW * scale);
      middleW = Math.floor(middleW * scale);
    }
    var x = 0;
    groups.ladder.forEach(function (node) {
      put(node, x, 0, ladderW, H);
      x += ladderW + gap;
    });
    var algoW = algos ? (middleW - (algos - 1) * gap) / algos : 0;
    var algoH = groups.monitor.length && algos
      ? Math.round(H * TILE_ALGO_SHARE) : H;
    groups.algo.forEach(function (node, i) {
      put(node, x + i * (algoW + gap), 0, algoW, algoH);
    });
    groups.monitor.forEach(function (node) {
      var top = algos ? algoH + gap : 0;
      put(node, x, top, middleW, H - top);
    });
    if (middleW) { x += middleW + gap; }
    var n = groups.right.length;
    if (n) {
      var rightW = Math.max(W - x, TILE_RIGHT_MIN_W);
      var weights = groups.right.map(function (node) {
        return TILE_WEIGHT[panelIdOf(node).split(':')[0]] || 1;
      });
      var total = weights.reduce(function (a, b) { return a + b; }, 0);
      var room = H - (n - 1) * gap, top = 0;
      groups.right.forEach(function (node, i) {
        var h = room * weights[i] / total;
        put(node, x, top, rightW, h);
        top += h + gap;
      });
    }
    layout = {};
    placed.forEach(function (p) {
      var id = panelIdOf(p.node);
      delete p.node.dataset.tidied;
      p.node.classList.remove('maximized');
      layout[id] = {left: p.left, top: p.top, w: p.w, h: p.h,
                    z: 10, auto: false};
    });
    writeLayout();
    desktop.scrollLeft = 0;
    desktop.scrollTop = 0;
    placed.forEach(function (p) {
      size(p.node, p.w, p.h);
      place(p.node, p.left, p.top);
      p.node.style.zIndex = 10;
      // A chart draws at its box's size: redraw at the new one.
      if (p.node.classList.contains('chartwin')) {
        p.node.dataset.drawn = '';
      }
    });
    render();
  }

  function tidyWindows() {
    /* Everything back to the row it started in. The way out of a mess,
     * and the way back from a window dragged somewhere useless. */
    layout = {};
    writeLayout();
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (node) {
        node.dataset.tidied = '1';
        node.classList.remove('floating', 'sized');
        node.style.left = node.style.top = node.style.zIndex = '';
        node.style.width = node.style.height = '';
      });
  }

  function startDrag(e) {
    if (e.button !== 0) { return; }
    var node = e.target.closest('.window');
    if (!node) { return; }
    if (e.target.closest('.grip')) { return startResize(e, node); }
    var bar = e.target.closest('.titlebar');
    if (!bar) { return; }
    // The buttons and selects that live IN the title bar keep working.
    if (e.target.closest('button, select, input, a')) { return; }
    // A maximized window stays put: restore it to move it.
    if (node.classList.contains('maximized')) { return; }

    var box = node.getBoundingClientRect();
    var desktop = desktopOrigin();
    var grabX = e.clientX - box.left;
    var grabY = e.clientY - box.top;
    var startX = e.clientX;
    var startY = e.clientY;
    var moved = false;
    state.active = panelIdOf(node);

    function move(event) {
      if (!moved) {
        // A CLICK on a title bar must not rearrange the desk: the
        // window only leaves the row once the pointer has actually
        // travelled. Pinned first at exactly where it already is, so
        // it does not jump out from under the cursor.
        if (Math.abs(event.clientX - startX) < 4 &&
            Math.abs(event.clientY - startY) < 4) { return; }
        moved = true;
        node.classList.add('dragging');
        layout[panelIdOf(node)] = Object.assign(
          {}, layout[panelIdOf(node)],
          {left: box.left - desktop.left, top: box.top - desktop.top,
           z: Math.min(MAX_WINDOW_Z, topZ + 1)});
        place(node, box.left - desktop.left, box.top - desktop.top);
        raise(node);
      }
      // Re-read every move: a drag that reaches the edge scrolls the
      // desktop under the window, and an origin captured at grab time
      // would then be stale by exactly that scroll.
      var frame = desktopOrigin();
      var snapped = snapTo(node, event.clientX - grabX - frame.left,
                           event.clientY - grabY - frame.top);
      var at = place(node, snapped.left, snapped.top);
      layout[panelIdOf(node)].left = at.left;
      layout[panelIdOf(node)].top = at.top;
      // Kept as it moves, not only on the drop: a release the page never
      // hears (the pointer let go outside the browser) left the desk
      // with a place from the middle of the drag. The desk's copy is
      // throttled to once a second, so the last place always follows.
      writeLayout();
    }

    function drop() {
      node.classList.remove('dragging');
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', drop);
      if (moved) {
        writeLayout();
        render();
      }
    }

    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', drop);
    // The drag has the pointer now: the ladder underneath must not also
    // see this as a click.
    e.preventDefault();
  }

  function startResize(e, node) {
    /* Windows are freely expandable, in both directions, from the
     * corner. The ladder gets longer — more price rows on screen at
     * once, which is the whole point of a ladder — and the monitor and
     * the grid get wider without a horizontal scrollbar. */
    var box = node.getBoundingClientRect();
    var startX = e.clientX;
    var startY = e.clientY;
    var id = panelIdOf(node);
    node.classList.add('dragging');

    function move(event) {
      var edge = snapEdges(node, box.width + (event.clientX - startX),
                           box.height + (event.clientY - startY));
      var to = size(node, edge.w, edge.h);
      // Dragged by hand: the size is the trader's from here on, and
      // is restored exactly rather than recomputed.
      layout[id] = Object.assign({}, layout[id],
                                 {w: to.w, h: to.h, auto: false});
    }

    function drop() {
      node.classList.remove('dragging');
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', drop);
      writeLayout();
      render();
    }

    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', drop);
    e.preventDefault();
  }

  // -- the ladder ------------------------------------------------------

  function ladderNode(key) {
    var existing = document.querySelector('.ladder[data-pair="' + cssEscape(key) + '"]');
    if (existing) { return existing; }
    var node = el('ladder-template').content.firstElementChild.cloneNode(true);
    node.dataset.pair = key;
    wireLadder(node, key);
    el('desktop').appendChild(node);
    return node;
  }

  function cssEscape(value) { return String(value).replace(/"/g, '\\"'); }

  function fairNode(key) {
    /* One pair's fair value and exit, in a window of their own. */
    var existing = document.querySelector(
      '.fairwin[data-pair="' + cssEscape(key) + '"]');
    if (existing) { return existing; }
    var node = el('fair-template').content.firstElementChild.cloneNode(true);
    node.dataset.pair = key;
    node.querySelector('.close').addEventListener('click', function () {
      // Closing it IS turning it off — for this screen and in the
      // pair's settings. A window that comes back on the next poll
      // because the setting still says so is a window the trader
      // cannot get rid of.
      showFairWindow(key, false);
    });
    node.querySelectorAll('.fw-cog, .open-exits').forEach(function (cog) {
      cog.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var ladder = document.querySelector(
          '.ladder[data-pair="' + cssEscape(key) + '"]');
        if (ladder) { openLadderSettings(ladder, key); }
        else { toast('open the ' + key + ' ladder to reach its settings'); }
      });
    });
    el('desktop').appendChild(node);
    return node;
  }

  function showFairWindow(key, on) {
    /* Whether this window is open is a decision about THIS SCREEN.
     *
     * The pair's setting is where it starts — tick the box and it
     * opens, here and on the next browser to load the page — but it
     * does not own the window. Owned by the snapshot, a poll arriving
     * before the save has landed, or with the engine down, or
     * restarted, takes the window away under the trader's hands a
     * third of a second after they opened it. Which is what it did.
     *
     * So the panel is opened and closed like every other panel, and
     * the setting is saved beside it.
     */
    var id = panelId('fair', key);
    if (on) { openPanel(id); } else { closePanel(id); }
    setPair(key, {algo_window: !!on});
    var row = (state.snapshot.pairs || {})[key];
    if (row) { row.algo_window = !!on; }           // before the next poll
    fetch('/api/pairs/' + encodeURIComponent(key), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({algo_window: !!on})
    }).catch(function () { /* the engine already has it */ });
  }

  function renderFairWindow(key, row) {
    var node = fairNode(key);
    var name = row.name || key;
    var title = node.querySelector('.fw-pair');
    // Named across the top: two of these open at once are otherwise two
    // identical tables of numbers with nothing to say which instrument
    // either belongs to.
    title.textContent = name;
    title.title = key + ' — leg A ' + (row.symbol_a || '?') + ', leg B '
      + (row.symbol_b || '?');
    // WHICH algo, first: the window shows one reading, not two.
    renderFair(node, row, renderAlgo(node, row));
  }

  // -- the pair chart -------------------------------------------------
  //
  // One ladder's spread as 15m, 1h or 4h candles with its standard-
  // deviation bands, and the Algo's z against them. The history is
  // MT5's own bars, fetched by the engine; the newest candle moves with
  // the live 10-second candles the engine writes every two seconds.
  // The bands are the Algo's own arithmetic - an EMA middle, the
  // population sigma of the last `length` closes - so on the Algo's own
  // timeframe they ARE its bands. Drawn in SVG: nothing is loaded from
  // outside.

  //: Candles drawn. The bands are worked out over the history before
  //: them too, so the first one drawn already has its band.
  //: Candles on screen before any zoom: wide enough to read each one.
  var CHART_SHOWN = 60;
  //: How old a history may be before the engine is asked again. The
  //: engine reuses its own for five minutes as well.
  var CHART_HISTORY_MS = 300000;
  //: The longest a history request is waited for before it is asked
  //: again: the engine's answer is polled for 30 s.
  var CHART_BUSY_MS = 45000;
  //: A history this old is out of date, and the window says so: the
  //: candles stop there and only the live ones carry on.
  var CHART_STALE_MS = 2 * CHART_HISTORY_MS;

  function chartOpen(key) {
    return state.open.indexOf(panelId('chart', key)) >= 0;
  }

  function toggleChart(key) {
    var id = panelId('chart', key);
    if (chartOpen(key)) { closePanel(id); } else { openPanel(id); }
  }

  function chartTf(key) { return state.chartTf[key] || 15; }

  function chartNode(key) {
    var existing = document.querySelector(
      '.chartwin[data-pair="' + cssEscape(key) + '"]');
    if (existing) { return existing; }
    var node = el('chart-template').content.firstElementChild.cloneNode(true);
    node.dataset.pair = key;
    node.querySelector('.close').addEventListener('click', function () {
      closePanel(panelId('chart', key));
    });
    // The hover: a crosshair on both charts and a readout of the candle.
    [['.cw-z', 'z'], ['.cw-spread', 's']].forEach(function (pair) {
      var box = node.querySelector(pair[0]);
      box.addEventListener('mousemove', function (e) {
        chartHover(node, key, pair[1], e);
      });
      box.addEventListener('mouseleave', function () { chartUnhover(node); });
    });
    // ZOOM AND PAN, per chart window: the wheel zooms (about the
    // candle under the pointer's side of the chart), a drag scrolls
    // back in time, a double-click comes back to now. Both charts move
    // together - the z and the spread are always the same candles.
    [node.querySelector('.cw-z'), node.querySelector('.cw-spread')]
      .forEach(function (box) {
        box.addEventListener('wheel', function (e) {
          e.preventDefault();
          chartZoom(node, key, e.deltaY > 0 ? 1.25 : 0.8);
        }, {passive: false});
        box.addEventListener('mousedown', function (e) {
          if (e.button !== 0) { return; }
          var view = chartView(key);
          var startX = e.clientX, startBack = view.back;
          var width = Math.max(box.clientWidth - 60, 50);
          box.classList.add('dragging');
          function move(ev) {
            var candles = (ev.clientX - startX) / width * view.shown;
            chartPan(node, key, startBack + Math.round(candles));
          }
          function up() {
            box.classList.remove('dragging');
            document.removeEventListener('mousemove', move);
            document.removeEventListener('mouseup', up);
          }
          document.addEventListener('mousemove', move);
          document.addEventListener('mouseup', up);
          e.preventDefault();
        });
        box.addEventListener('dblclick', function () {
          chartResetView(node, key);
        });
      });
    node.querySelector('.cw-zoom-in').addEventListener('click', function () {
      chartZoom(node, key, 0.8);
    });
    node.querySelector('.cw-zoom-out').addEventListener('click', function () {
      chartZoom(node, key, 1.25);
    });
    node.querySelector('.cw-zoom-reset').addEventListener('click',
      function () { chartResetView(node, key); });
    node.querySelector('.cw-vp-toggle').addEventListener('click',
      function () {
        state.chartVp = !chartVpOn();
        try {
          window.localStorage.setItem('okxtrader.chartVp',
                                      state.chartVp ? '1' : '0');
        } catch (err) { /* a preference */ }
        document.querySelectorAll('.chartwin').forEach(function (w) {
          w.dataset.drawn = '';
        });
        render();
      });
    node.querySelectorAll('.cw-tfs button').forEach(function (button) {
      button.addEventListener('click', function () {
        state.chartTf[key] = parseInt(button.dataset.tf, 10) || 15;
        node.dataset.drawn = '';
        fetchHistory(key, true);
        drawChart(node, key);
      });
    });
    el('desktop').appendChild(node);
    return node;
  }

  //: The fewest candles a zoom goes in to.
  var CHART_MIN_SHOWN = 10;

  function chartView(key) {
    /* How many candles are shown, and how far back from the latest the
     * right-hand edge is. Per chart window, kept while it is open. */
    state.chartView = state.chartView || {};
    if (!state.chartView[key]) {
      state.chartView[key] = {shown: CHART_SHOWN, back: 0, total: 0};
    }
    return state.chartView[key];
  }

  function chartZoom(node, key, factor) {
    var view = chartView(key);
    var most = Math.max(view.total || CHART_SHOWN, CHART_MIN_SHOWN);
    view.shown = Math.max(CHART_MIN_SHOWN,
                          Math.min(most, Math.round(view.shown * factor)));
    view.back = Math.max(0, Math.min(view.back, most - view.shown));
    node.dataset.drawn = '';
    drawChart(node, key);
  }

  function chartPan(node, key, back) {
    var view = chartView(key);
    var most = Math.max((view.total || 0) - view.shown, 0);
    var next = Math.max(0, Math.min(back, most));
    if (next === view.back) { return; }
    view.back = next;
    node.dataset.drawn = '';
    drawChart(node, key);
  }

  function chartResetView(node, key) {
    var view = chartView(key);
    view.shown = CHART_SHOWN;
    view.back = 0;
    node.dataset.drawn = '';
    drawChart(node, key);
  }

  function chartRedraw(key) {
    var node = document.querySelector(
      '.chartwin[data-pair="' + cssEscape(key) + '"]');
    if (node) { drawChart(node, key); }
  }

  function fetchLive(key) {
    // The live 10-second candles: the right-hand end of every timeframe.
    var held = state.chartFetch[key] || {};
    var at = Date.now();
    if (held.busy || (held.at && at - held.at < 2000)) { return; }
    state.chartFetch[key] = {busy: true, at: at};
    fetch('/api/chart/' + encodeURIComponent(key) + '?minutes=120')
      .then(function (r) { return r.json(); })
      .then(function (body) {
        state.charts[key] = body;
        state.chartFetch[key] = {busy: false, at: Date.now()};
        chartRedraw(key);
      })
      .catch(function () {
        state.chartFetch[key] = {busy: false, at: Date.now()};
      });
  }

  function fetchHistory(key, now) {
    /* The candles from MT5. Read what the engine last wrote; if there is
     * none, or it is five minutes old, ask the engine for it - through
     * the same command log a backtest goes through - and read it again
     * once it answers. */
    var tf = chartTf(key);
    var id = key + '#' + tf;
    var held = state.chartHistFetch[id] || {};
    var at = Date.now();
    // Busy, unless for longer than any answer takes - a request that
    // never came back must not hold the chart on an old history for
    // good (it did, until the page was reloaded) - or the trader pressed
    // the timeframe button, which always asks again.
    if (held.busy && !now && at - held.at < CHART_BUSY_MS) { return; }
    if (!now && held.at && at - held.at < 15000) { return; }
    state.chartHistFetch[id] = {busy: true, at: at};
    function finish(body) {
      if (body) { state.chartHist[id] = body; }
      state.chartHistFetch[id] = {busy: false, at: Date.now(),
                                  asked: held.asked};
      chartRedraw(key);
    }
    function read() {
      return fetch('/api/chart_history/' + encodeURIComponent(key) +
                   '?tf=' + tf).then(function (r) { return r.json(); });
    }
    read().then(function (body) {
      var fresh = body && body.ok && body.at &&
        Date.now() - body.at * 1000 < CHART_HISTORY_MS;
      if (fresh) {
        delete state.chartHistNote[id];
        return finish(body);
      }
      if (body && body.ok) { state.chartHist[id] = body; }
      // Asked already, a moment ago: wait for that answer - unless the
      // trader pressed the timeframe button, which asks again now.
      if (!now && held.asked && at - held.asked < 20000) {
        return finish(null);
      }
      held.asked = at;
      return fetch('/api/command', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({kind: 'chart_history',
                              payload: {pair: key, timeframe_min: tf}})
      }).then(function (r) { return r.json(); }).then(function (sent) {
        if (!sent.ok) {
          state.chartHistNote[id] = sent.error || 'the engine refused';
          return finish(null);
        }
        var tries = 0;
        (function poll() {
          fetch('/api/result/' + sent.id).then(function (r) {
            return r.json();
          }).then(function (result) {
            if (result && result.pending && ++tries < 120) {
              return window.setTimeout(poll, 250);
            }
            if (result && result.pending) {
              state.chartHistNote[id] = 'the engine did not answer in ' +
                '30 s - it may be busy; asking again shortly';
              return finish(null);
            }
            var data = (result && result.data) || {};
            if (!result || result.ok === false || data.ok === false) {
              state.chartHistNote[id] = (result && result.error) ||
                data.reason || 'no answer from the engine';
              return finish(null);
            }
            read().then(function (body) {
              // The engine says it wrote a history at `data.at`; the
              // file must now hold that one, not an older one.
              if (body && body.ok && data.at && body.at &&
                  body.at + 1 < data.at) {
                state.chartHistNote[id] = 'the engine read the candles ' +
                  'at ' + chartClock(data.at, true) + ' but the file ' +
                  'still holds ' + chartClock(body.at, true);
              } else {
                delete state.chartHistNote[id];
              }
              finish(body);
            }).catch(function () { finish(null); });
          }).catch(function () {
            state.chartHistNote[id] = 'no answer from the web screen';
            finish(null);
          });
        })();
      });
    }).catch(function () { finish(null); });
  }

  function renderChartWindow(key, row) {
    var node = chartNode(key);
    var title = node.querySelector('.cw-pair');
    title.textContent = (row.name || key) + ' · Chart';
    title.title = key + ' — leg A ' + (row.symbol_a || '?') +
      ', leg B ' + (row.symbol_b || '?');
    var tf = chartTf(key);
    node.querySelectorAll('.cw-tfs button').forEach(function (button) {
      button.classList.toggle('on', parseInt(button.dataset.tf, 10) === tf);
    });
    fetchLive(key);
    fetchHistory(key, false);
    drawChart(node, key);
  }

  function chartClock(t, withDay) {
    var d = new Date(t * 1000);
    function two(n) { return (n < 10 ? '0' : '') + n; }
    var clock = two(d.getHours()) + ':' + two(d.getMinutes());
    return withDay ? d.getDate() + ' ' +
      ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep',
       'Oct', 'Nov', 'Dec'][d.getMonth()] + ' ' + clock : clock;
  }

  //: Candle colours: up green, down red, the house style of every
  //: chart a trader already reads; the entry lines keep Arrow-statarb's.
  var CHART = {up: '#198754', down: '#dc3545', stop: '#6c757d',
               mean: '#fd7e14', sd: 'rgba(13,110,253,.55)',
               grid: 'rgba(128,128,128,.25)', text: '#6c757d',
               axis: '#343a40'};

  function mergeLive(history, live, seconds, anchor, since) {
    /* MT5's candles with the live 10-second ones folded into them: a
     * live price makes a candle's high, low and close; a bucket the
     * history has not reached yet starts a new candle. */
    var byT = {};
    var out = history.map(function (c) {
      var copy = {t: c.t, o: c.o, h: c.h, l: c.l, c: c.c};
      // Volume rides along where OKX gave it: the profile reads it.
      if (c.f) { copy.f = c.f; copy.v = c.v; }
      byT[c.t] = copy;
      return copy;
    });
    (live || []).forEach(function (lc) {
      if (lc.o === null || lc.o === undefined) { return; }
      var b = Math.floor((lc.t + anchor) / seconds) * seconds - anchor;
      var candle = byT[b];
      if (!candle) {
        if (out.length && b < out[out.length - 1].t) { return; }
        candle = byT[b] = {t: b, o: lc.o, h: lc.h, l: lc.l, c: lc.c};
        out.push(candle);
        return;
      }
      candle.h = Math.max(candle.h, lc.h);
      candle.l = Math.min(candle.l, lc.l);
      // The close only from a live candle at or after the history's own
      // reading: an older one would move the close backwards.
      if (lc.t + 10 >= since) { candle.c = lc.c; }
    });
    return out;
  }

  function withBands(candles, length) {
    /* The Algo's band on every candle: Pine's EMA middle (seeded with
     * the simple average of the first `length` closes) and the
     * POPULATION sigma of the last `length` closes. None until there
     * are `length` closes behind it - unmeasured is not zero. */
    var alpha = 2 / (length + 1), mean = null, sum = 0;
    candles.forEach(function (c, i) {
      if (i < length) { sum += c.c; }
      if (i === length - 1) { mean = sum / length; }
      else if (i >= length) { mean = alpha * c.c + (1 - alpha) * mean; }
      if (i < length - 1) { c.m = null; c.sd = null; return; }
      var last = candles.slice(i - length + 1, i + 1);
      var avg = last.reduce(function (a, w) { return a + w.c; }, 0) /
        length;
      var sd = Math.sqrt(last.reduce(function (a, w) {
        return a + (w.c - avg) * (w.c - avg);
      }, 0) / length);
      c.m = mean;
      c.sd = sd > 1e-12 ? sd : null;
    });
    return candles;
  }

  //: The right margin a chart with a price tag keeps for it: room for
  //: "-1234.56" in the tag's bold 11 px.
  var CHART_TAG_ROOM = 62;

  // -- the position calculator -------------------------------------------

  //: OKX's lowest-tier maintenance margin on USDT perpetuals (0.4%):
  //: where cross margin starts liquidating, for the estimate below.
  var CALC_MMR = 0.004;
  //: OKX's standard (Lv1) USDT-perpetual fees in bp, for when the
  //: account's own have not been read.
  var CALC_STANDARD_FEES = {maker: {a: 2.0, b: 2.0},
                            taker: {a: 5.0, b: 5.0}};

  function calcPosition(inp, ctx) {
    /* Capital at risk for one spread trade. Pure arithmetic, so it can
     * be checked with a calculator:
     *   one Qty = perA + perB of position (each leg's lots x contract x
     *     mid), and `point` dollars per point of spread (leg B's units);
     *   margin = each leg over its own leverage;
     *   fees = both legs, in AND out, at the chosen rate;
     *   loss at the stop and profit at the target are the spread's move
     *     times the dollars per point, fees on top.
     * Returns null for anything that cannot be worked out. */
    var out = {error: null};
    var perA = ctx.perA, perB = ctx.perB, point = ctx.point;
    var levA = inp.levA || ctx.levA, levB = inp.levB || ctx.levB;
    if (!perA || !perB || !point) {
      out.error = 'this ladder has no live price or lots yet';
      return out;
    }
    if (!levA || !levB) {
      out.error = 'OKX\u2019s leverage on ' + (!levA && !levB ? 'both legs'
        : (!levA ? 'leg A' : 'leg B')) + ' is not known yet - type it ' +
        'in Leverage A / B';
      return out;
    }
    var perQty = perA + perB;
    var marginPerQty = perA / levA + perB / levB;
    var feeKind = inp.fees === 'taker' ? 'taker' : 'maker';
    var feeRate = (ctx.fees || {})[feeKind];
    // The account's own rates not read yet: OKX's standard USDT-perp
    // rates, and said - never a silent zero.
    out.feesStandard = !feeRate;
    feeRate = feeRate || CALC_STANDARD_FEES[feeKind];
    var feesPerQty = 2 * (perA * feeRate.a + perB * feeRate.b) / 1e4;
    var size = Number(inp.size) || 0;
    var qty = inp.sizeBy === 'MARGIN' ? size / marginPerQty
      : inp.sizeBy === 'NOTIONAL' ? size / perQty : size;
    out.qty = qty;
    out.notionalA = perA * qty;
    out.notionalB = perB * qty;
    out.notional = perQty * qty;
    out.marginA = perA / levA * qty;
    out.marginB = perB / levB * qty;
    out.margin = marginPerQty * qty;
    out.levA = levA;
    out.levB = levB;
    out.point = point * qty;
    out.fees = feesPerQty * qty;
    out.breakEvenPts = out.point ? out.fees / out.point : null;
    var capital = inp.capital || ctx.equity || null;
    out.capital = capital;
    out.marginPct = capital ? out.margin / capital * 100 : null;
    var sign = inp.side === 'SELL' ? -1 : 1;
    var entry = (inp.entry === null || inp.entry === undefined ||
                 inp.entry === '' || isNaN(inp.entry))
      ? (inp.side === 'SELL' ? ctx.bid : ctx.ask) : Number(inp.entry);
    out.entry = entry;
    function level(x) {
      return x === null || x === undefined || x === '' || isNaN(x)
        ? null : Number(x);
    }
    var sl = level(inp.sl), tp = level(inp.tp);
    if (sl !== null && entry !== null && entry !== undefined) {
      var dist = sign * (entry - sl);
      out.stopPts = dist;
      if (dist <= 0) {
        out.stopError = 'the stop is on the wrong side of the entry';
      } else {
        out.lossGross = dist * out.point;
        out.loss = out.lossGross + out.fees;
        out.lossPct = capital ? out.loss / capital * 100 : null;
        out.lossOfMargin = out.margin ? out.loss / out.margin * 100 : null;
        // The size that loses exactly the risk % at this stop, fees in.
        var risk = capital && inp.riskPct ? capital * inp.riskPct / 100
          : null;
        var lossPerQty = dist * point + feesPerQty;
        out.riskMoney = risk;
        out.riskQty = risk ? risk / lossPerQty : null;
        out.riskNotional = out.riskQty === null ? null
          : out.riskQty * perQty;
        out.riskMargin = out.riskQty === null ? null
          : out.riskQty * marginPerQty;
      }
    }
    if (tp !== null && entry !== null && entry !== undefined) {
      var gain = sign * (tp - entry);
      out.targetPts = gain;
      if (gain <= 0) {
        out.targetError = 'the target is on the wrong side of the entry';
      } else {
        out.profit = gain * out.point - out.fees;
        out.profitPct = capital ? out.profit / capital * 100 : null;
      }
    }
    if (out.loss && out.profit !== undefined) {
      out.rr = out.profit / out.loss;
    }
    // Cross margin: the account goes to liquidation when its equity
    // falls to the maintenance margin. With nothing else open, that is
    // this many points against the trade.
    if (capital && out.point) {
      var room = capital - CALC_MMR * out.notional;
      out.liqPts = room > 0 ? room / out.point : 0;
      out.liqLevel = entry === null || entry === undefined ? null
        : entry - sign * out.liqPts;
      // A spread is leg B less beta x leg A: a move bigger than leg B's
      // own price needs a coin below zero. Out of reach, said so.
      out.liqOutOfReach = !!(ctx.priceB && out.liqPts >= ctx.priceB);
    }
    if (inp.stressPct) {
      out.stressLoss = out.notionalB * inp.stressPct / 100;
      out.stressPct = capital ? out.stressLoss / capital * 100 : null;
    }
    return out;
  }

  function calcNode(key) {
    var existing = document.querySelector(
      '.calcwin[data-pair="' + cssEscape(key) + '"]');
    if (existing) { return existing; }
    var node = el('calc-template').content.firstElementChild.cloneNode(true);
    node.dataset.pair = key;
    node.querySelector('.close').addEventListener('click', function () {
      closePanel(panelId('calc', key));
    });
    // Every input recomputes at once; the figures are redrawn on every
    // snapshot as well, so they follow the live price.
    node.querySelector('.pc-form').addEventListener('input', function () {
      renderCalcWindow(key, (state.snapshot.pairs || {})[key] || {});
    });
    node.querySelector('.pc-form').addEventListener('change', function () {
      renderCalcWindow(key, (state.snapshot.pairs || {})[key] || {});
    });
    // A first size: the ladder's own Qty.
    var row = (state.snapshot.pairs || {})[key] || {};
    node.querySelector('.pc-size').value = row.default_quantity || 1;
    el('desktop').appendChild(node);
    return node;
  }

  function renderCalcWindow(key, row) {
    var node = calcNode(key);
    node.querySelector('.pc-pair').textContent =
      (row.name || key) + ' · Calc';
    function val(cls) { return node.querySelector(cls).value; }
    function num(cls) {
      var v = val(cls);
      return v === '' ? null : Number(v);
    }
    var sizeBy = val('.pc-size-by');
    node.querySelector('.pc-size-label').textContent = sizeBy === 'MARGIN'
      ? 'Total margin $' : sizeBy === 'NOTIONAL' ? 'Position $' : 'Qty';
    var market = row.market || {};
    var legs = row.leg_notional || {};
    var midB = mid(market.leg_b_bid, market.leg_b_ask);
    var fees = row.fee_bps || {};
    function rate(kind) {
      var a = (fees.a || {})[kind], b = (fees.b || {})[kind];
      return a === undefined || b === undefined ? null : {a: a, b: b};
    }
    var accounts = (state.snapshot || {}).accounts || {};
    var ctx = {perA: legs.a, perB: legs.b,
               point: legs.b && midB ? legs.b / midB : null,
               priceB: midB,
               levA: row.leverage_a, levB: row.leverage_b,
               fees: {maker: rate('maker'), taker: rate('taker')},
               bid: market.short_spread, ask: market.long_spread,
               equity: (accounts[row.account_a] || {}).equity};
    // What blank means, said in the boxes themselves.
    node.querySelector('.pc-lev-a').placeholder =
      row.leverage_a ? fmt(row.leverage_a, 0) + 'x' : 'OKX';
    node.querySelector('.pc-lev-b').placeholder =
      row.leverage_b ? fmt(row.leverage_b, 0) + 'x' : 'OKX';
    node.querySelector('.pc-capital').placeholder =
      ctx.equity ? Math.round(ctx.equity) : 'equity';
    var side = val('.pc-side');
    var now = side === 'SELL' ? ctx.bid : ctx.ask;
    node.querySelector('.pc-entry').placeholder =
      now === null || now === undefined ? 'now' : 'now ' + fmt(now, 2);
    var r = calcPosition({
      side: side, sizeBy: sizeBy, size: num('.pc-size'),
      entry: num('.pc-entry'), sl: num('.pc-sl'), tp: num('.pc-tp'),
      levA: num('.pc-lev-a'), levB: num('.pc-lev-b'),
      fees: val('.pc-fees'), capital: num('.pc-capital'),
      riskPct: num('.pc-risk'), stressPct: num('.pc-stress')}, ctx);
    var out = node.querySelector('.pc-out');
    if (r.error) {
      out.innerHTML = '<div class="hint">' + escapeHtml(r.error) + '</div>';
      return;
    }
    function k(label, value, cls, title, wide) {
      return '<div class="pc-kv' + (wide ? ' wide' : '') + '"' +
        (title ? ' title="' + escapeHtml(title) + '"' : '') + '><span>' +
        label + '</span><b' + (cls ? ' class="' + cls + '"' : '') + '>' +
        value + '</b></div>';
    }
    function pct(x, d) {
      return x === null || x === undefined || !isFinite(x) ? DASH
        : fmt(x, d === undefined ? 1 : d) + '%';
    }
    function pts(x) {
      return x === null || x === undefined || !isFinite(x) ? DASH
        : fmt(x, 2) + ' pts';
    }
    var html = '<div class="pc-head">Position</div><div class="pc-grid">' +
      k('Qty', fmt(r.qty, 2)) +
      k('Position', money(r.notional), '',
        'leg A ' + money(r.notionalA) + ' + leg B ' + money(r.notionalB)) +
      k('Margin', money(r.margin), '',
        'leg A ' + money(r.marginA) + ' at ' + fmt(r.levA, 0) +
        'x + leg B ' + money(r.marginB) + ' at ' + fmt(r.levB, 0) + 'x') +
      k('Of capital', pct(r.marginPct), r.marginPct > 50 ? 'down' : '',
        'margin against ' + money(r.capital)) +
      k('$ per point', money(r.point)) +
      k('Fees round trip', money(r.fees) +
        (r.feesStandard ? ' <small>std</small>' : ''), '',
        'both legs, in and out, ' + val('.pc-fees') +
        (r.feesStandard ? ' - OKX’s standard rate (maker 2 / taker ' +
          '5 bp): this account’s own has not been read' : '')) +
      k('Break-even', pts(r.breakEvenPts), '',
        'how far the spread must move to pay the fees') +
      k('Entry', r.entry === null || r.entry === undefined ? DASH
        : fmt(r.entry, 2)) +
      '</div><div class="pc-head">At the stop and the target</div>' +
      '<div class="pc-grid">';
    if (r.stopError) {
      html += k('Stop', r.stopError, 'down', '', true);
    } else {
      html += k('Stop distance', pts(r.stopPts)) +
        k('Loss at stop', r.loss === undefined ? DASH : money(-r.loss),
          r.loss === undefined ? '' : 'down', 'the move times $ per point, ' +
          'plus fees') +
        k('Of capital', pct(r.lossPct, 2), r.lossPct > 5 ? 'down' : '') +
        k('Of margin', pct(r.lossOfMargin));
    }
    if (r.targetError) {
      html += k('Target', r.targetError, 'down', '', true);
    } else {
      html += k('Target distance', pts(r.targetPts)) +
        k('Profit at target', r.profit === undefined ? DASH
          : money(r.profit), r.profit > 0 ? 'up' : '',
          'the move times $ per point, less fees') +
        k('Of capital', pct(r.profitPct, 2)) +
        k('Reward : risk', r.rr === undefined ? DASH
          : fmt(r.rr, 2) + ' : 1', r.rr >= 1 ? 'up' : (r.rr ? 'down' : ''));
    }
    html += '</div><div class="pc-head">Sizing and limits</div>' +
      '<div class="pc-grid">' +
      k('Size for ' + pct(num('.pc-risk'), 1) + ' risk',
        r.riskQty === null || r.riskQty === undefined ? DASH
          : fmt(r.riskQty, 2) + ' Qty', '',
        'the size whose loss at this stop, fees in, is ' +
        money(r.riskMoney) + ' - position ' + money(r.riskNotional) +
        ', margin ' + money(r.riskMargin), true) +
      k('Liquidation, est.', r.liqPts === undefined ? DASH
        : r.liqOutOfReach ? 'out of reach at this size'
        : pts(r.liqPts) + ' · at ' + fmt(r.liqLevel, 2),
        r.liqOutOfReach ? 'up' : 'down',
        'cross margin: how far against the trade the account’s ' +
        'equity reaches OKX’s maintenance margin (0.4%), with nothing ' +
        'else open. An estimate - other positions and funding move it',
        true) +
      k('Stress: leg B ' + pct(num('.pc-stress'), 0) + ' vs A',
        r.stressLoss === undefined ? DASH : money(-r.stressLoss) +
          ' · ' + pct(r.stressPct, 0) + ' of capital', 'down',
        'what the hedge loses if leg B moves this far against leg A', true) +
      '</div>';
    out.innerHTML = html;
  }

  // -- the Sizing tab ------------------------------------------------------
  //
  // The sizing sheet, live: capital in hand down the side, what it
  // means per trade, a month and in a run of losses. Worked out here
  // from the ETH vs BTC ladder's own prices, beta and leverage, and the
  // fees of THIS account's own OKX tier. A planning table only.

  //: Before the engine has read the account's own rates: OKX's regular
  //: USDT-margined perpetual rates (0.020% maker, 0.050% taker).
  var SZ_REGULAR = {level: 'Regular', maker: 2.0, taker: 5.0, live: false};
  //: OKX's FUTURES fee tiers for this account, as its own fee page shows
  //: them (Futures tab, AED): [tier, assets from (AED), 30-day volume
  //: from (AED), maker bp, taker bp]. A tier is reached on assets OR on
  //: volume - whichever is higher. A negative maker fee is a rebate.
  var SZ_TIERS = [
    ['Regular', 0, 0, 2.0, 5.0],
    ['VIP 1', 400001, 18000001, 1.6, 4.5],
    ['VIP 2', 750001, 36000001, 1.5, 3.6],
    ['VIP 3', 7500001, 180000001, 1.0, 2.8],
    ['VIP 4', 20000001, 720000001, 0.8, 2.7],
    ['VIP 5', 75000001, 2160000001, 0.5, 2.6],
    ['VIP 6', 180000001, 3600000001, 0.0, 2.5],
    ['VIP 7', 360000001, 5400000001, -0.1, 2.3],
    ['VIP 8', 900000001, 7200000001, -0.25, 2.0],
    ['VIP 9', 1800000001, 72000000001, -0.5, 1.5]];
  var SZ_AED_PER_USD = 3.6725;

  function sizingTier(capital, volume) {
    /* The tier a row reaches: on its capital held as assets, or on its
     * 30-day volume - the higher of the two, as OKX ranks it. */
    var assets = capital * SZ_AED_PER_USD, traded = volume * SZ_AED_PER_USD;
    var tier = SZ_TIERS[0], on = null;
    SZ_TIERS.forEach(function (t) {
      if (assets >= t[1] || traded >= t[2]) {
        tier = t;
        on = traded >= t[2] ? 'volume' : 'assets';
      }
    });
    return [tier[0], 0, tier[3], tier[4], on];
  }
  var SZ_WINS = [55, 60, 62.5, 65, 70, 75, 80];
  var SZ_LOSS_SHARES = [5, 10, 15, 20, 25, 30, 35];

  function sizingSettings() {
    /* Every assumption on the Sizing tab, the trader's to change. */
    var d = {win: 65, perDay: 3, days: 30, freePct: 40, lev: 0,
             target: 1.5, targetBase: 'margin',
             stop: 1.5, stopBase: 'capital', makerPct: 100};
    return Object.assign(d, state.sizing || {});
  }

  function sizingMarket() {
    /* ETH + beta x BTC and the leverage, from the ETH vs BTC ladder;
     * fixed figures until one is live. */
    var pairs = (state.snapshot || {}).pairs || {};
    var found = null;
    Object.keys(pairs).forEach(function (key) {
      var r = pairs[key];
      if (!found && /BTC-USDT-SWAP/.test(r.symbol_a || '') &&
          /ETH-USDT-SWAP/.test(r.symbol_b || '')) { found = r; }
    });
    var m = (found || {}).market || {};
    var a = mid(m.leg_a_bid, m.leg_a_ask), b = mid(m.leg_b_bid, m.leg_b_ask);
    var beta = (found || {}).hedge_ratio;
    var lev = Math.min((found || {}).leverage_a || 20,
                       (found || {}).leverage_b || 20) || 20;
    var fee = accountFees(found);
    if (a && b && beta) {
      return {per: b + beta * a, lev: lev, live: true, eth: b, btc: a,
              fee: fee};
    }
    return {per: 1720 + 0.028402 * 61400, lev: 20, live: false,
            eth: 1720, btc: 61400, fee: fee};
  }

  function accountFees(row) {
    /* THE ACCOUNT'S OWN TIER: maker and taker in bp, as OKX reports them
     * for this account (both legs averaged), and its level. They follow
     * the tier: when OKX moves the account up a level, the next read
     * has the new rates. Before the first read: the regular rates. */
    var rates = [];
    Object.keys((row || {}).fee_bps || {}).forEach(function (leg) {
      var r = row.fee_bps[leg];
      if (r && isFinite(r.maker) && isFinite(r.taker)) { rates.push(r); }
    });
    if (!rates.length) { return SZ_REGULAR; }
    var avg = function (k) {
      return rates.reduce(function (t, r) { return t + Number(r[k]); }, 0) /
        rates.length;
    };
    return {level: feeLevelName((row || {}).fee_level), maker: avg('maker'),
            taker: avg('taker'), live: true};
  }

  function feeLevelName(level) {
    /* OKX calls its regular levels Lv1, Lv2... and its VIP tiers VIP 1,
     * VIP 2...: a regular level is said as one, never as a VIP tier. */
    if (!level) { return DASH; }
    var text = String(level);
    return /^lv/i.test(text) ? text + ' (regular)' : text;
  }

  function sizingRow(capital, cfg, mk) {
    /* One capital level, worked through. The target is the profit AFTER
     * every cost and the stop the whole loss, costs included, so the P&L
     * is just wins x target - losses x stop. The maker / taker mix sets
     * the fees, and so how far the spread must move: target + fees to
     * win, stop - fees before the stop. */
    var lev = cfg.lev || mk.lev;
    var margin = capital * (100 - cfg.freePct) / 100;
    var position = margin * lev;
    var point = position / mk.per;
    // Trading DAYS a month: OKX's tier is on 30 calendar days, and a
    // month of 25 trading days trades 25 days' volume in them.
    var perMonth = cfg.perDay * (cfg.days || 30);
    var volume = position * 2 * perMonth;
    // The tier THIS row reaches - its volume or its capital - from the
    // account's own OKX futures fee table.
    var tier = sizingTier(capital, volume);
    var takerShare = (100 - cfg.makerPct) / 100;
    var perTradeVolume = position * 2;
    var fees = perTradeVolume * ((1 - takerShare) * tier[2] +
                                 takerShare * tier[3]) / 1e4;
    var target = cfg.target / 100 *
      (cfg.targetBase === 'capital' ? capital : margin);
    var stop = cfg.stop / 100 * (cfg.stopBase === 'margin' ? margin : capital);
    var w = cfg.win / 100;
    var perTrade = w * target - (1 - w) * stop;
    var breakEven = (target + stop) > 0 ? stop / (target + stop) * 100 : null;
    return {capital: capital, margin: margin, free: capital - margin,
            position: position, point: point, volume: volume, tier: tier,
            fees: fees, target: target, stop: stop,
            lossPerStop: stop, perTrade: perTrade,
            month: perTrade * perMonth, breakEven: breakEven,
            targetPts: point ? (target + fees) / point : null,
            stopPts: point ? Math.max(stop - fees, 0) / point : null,
            feePts: point ? fees / point : null};
  }

  //: How each status reads, and its colour on the Mirror tab.
  var MIRROR_STATUS = {
    OPEN: ['held', 'ok'], CLOSED: ['closed', 'done'],
    PENDING: ['sending', 'busy'], CLOSING: ['closing', 'busy'],
    FAILED: ['did not open', 'bad'], CLOSE_FAILED: ['CLOSE FAILED', 'alarm'],
    SKIPPED: ['skipped', 'skip']
  };

  function mirrorAgo(at) {
    if (!at) { return DASH; }
    var s = Math.max(0, Date.now() / 1000 - at);
    return s < 60 ? Math.round(s) + 's ago' : s < 3600
      ? Math.round(s / 60) + 'm ago' : s < 86400
      ? Math.round(s / 3600) + 'h ago' : anClock(at);
  }

  function mirrorLegs(legs) {
    return (legs || []).map(function (l) {
      return escapeHtml(l.side + ' ' + fmt(l.volume, 2) + ' ' +
                        String(l.symbol).replace('-USDT-SWAP', ''));
    }).join(' · ');
  }

  function mirrorFilter() {
    /* The Mirror tab's filters: one client (or all), which trades, over
     * what period. Per browser session. */
    state.mirrorFilter = state.mirrorFilter ||
      {client: '', show: 'all', period: 'all'};
    return state.mirrorFilter;
  }

  function mirrorMoney(x) {
    if (x === null || x === undefined) { return DASH; }
    return '<span class="' + (x > 0 ? 'up' : x < 0 ? 'down' : '') + '">' +
      (x > 0 ? '+' : '') + money(x) + '</span>';
  }

  function mirrorReportLink(name, view, label, days) {
    return '<a class="mr-btn" target="_blank" rel="noopener" href="' +
      '/mirror/report?follower=' + encodeURIComponent(name) +
      (view === 'client' ? '&view=client' : '') + '&days=' + (days || 0) +
      '" title="' + escapeHtml(label + ' for ' + name) + '">' + label +
      '</a>';
  }

  function mirrorLimit(used, max, fmtUsed) {
    /* "105 / 150" - red at 90% of the limit or over; DASH when off. */
    if (!max) { return '<span class="hint">off</span>'; }
    var near = used >= 0.9 * max;
    return '<span class="' + (near ? 'down' : '') + '">' + fmtUsed(used) +
      ' / ' + fmtUsed(max) + '</span>';
  }

  function mirrorPane(mr) {
    /* THE MIRROR, built for ten or fifteen accounts: what needs a person
     * first, then one row per follower with its money and its limits,
     * then the trades - filtered by client, by status, by period. */
    var sum = mr.summary || {};
    var flt = mirrorFilter();
    var followers = (mr.followers || []).slice();
    var attention = mr.attention || [];
    var high = attention.filter(function (a) { return a.severity === 'high'; });
    var low = attention.filter(function (a) { return a.severity !== 'high'; });
    var html = '<div class="mr">';
    // 1. THE TILES.
    function tile(label, value, cls, title) {
      return '<div class="mr-tile ' + (cls || '') + '"' + (title
        ? ' title="' + escapeHtml(title) + '"' : '') + '><b>' + value +
        '</b><span class="mr-lbl">' + label + '</span></div>';
    }
    var today = followers.reduce(function (a, f) {
      return a + (f.pnl_today || 0); }, 0);
    var month = followers.reduce(function (a, f) {
      return a + (f.pnl_month || 0); }, 0);
    var halted = followers.filter(function (f) { return f.halted; }).length;
    html += '<div class="mr-tiles">' +
      tile('followers', (sum.following || 0) + ' / ' + (sum.followers || 0),
           '', 'following now / in the config') +
      tile('copies held', sum.holding || 0, sum.holding ? 'ok' : '',
           'copies open in follower accounts now') +
      tile('P&amp;L today, all clients', mirrorMoney(today)) +
      tile('P&amp;L this month', mirrorMoney(month)) +
      tile('failed today', sum.failed_today || 0,
           sum.failed_today ? 'bad' : '',
           'copies that did not open (or close) today') +
      tile('halted', halted, halted ? 'bad' : '') +
      tile('need attention', high.length, high.length ? 'alarm' : 'ok',
           'an account still holding a copy whose trade has closed') +
      tile('updated', mirrorAgo(mr.at)) + '</div>';
    if (mr.paused) {
      html += '<div class="mr-attn"><div class="bad"><b>Copying is ' +
        'switched OFF</b> (Managed accounts) \u2014 no new copies; every ' +
        'copy on still closes with its trade.</div></div>';
    }
    // 2. WHAT NEEDS A PERSON.
    if (high.length || low.length) {
      html += '<div class="mr-attn">' + high.concat(low).slice(0, 12)
        .map(function (a) {
          return '<div class="' + (a.severity === 'high' ? 'alarm' : 'bad') +
            '"><b>' + escapeHtml(a.follower) + '</b> — ' +
            (a.severity === 'high'
              ? 'STILL HOLDS a copy of a closed trade: '
              : 'copy did not open: ') + escapeHtml(a.why || '') +
            ' <small>(' + escapeHtml(a.position_id) + ')</small></div>';
        }).join('') + (high.length + low.length > 12
          ? '<div class="hint">…and ' + (high.length + low.length - 12) +
            ' more</div>' : '') + '</div>';
    }
    // 3. THE FILTERS.
    function opt(value, label, on) {
      return '<option value="' + escapeHtml(value) + '"' +
        (on ? ' selected' : '') + '>' + escapeHtml(label) + '</option>';
    }
    var names = followers.map(function (f) { return f.name; }).sort();
    if (flt.client && names.indexOf(flt.client) < 0) { flt.client = ''; }
    html += '<div class="mr-filters"><label>Client <select class="mr-filter"' +
      ' data-key="client">' + opt('', 'All clients', !flt.client) +
      names.map(function (n) { return opt(n, n, flt.client === n); })
        .join('') + '</select></label>' +
      '<label>Show <select class="mr-filter" data-key="show">' +
      [['all', 'All trades'], ['open', 'Open'],
       ['attention', 'Needs attention'], ['failed', 'Failed / skipped'],
       ['closed', 'Closed']].map(function (o) {
        return opt(o[0], o[1], flt.show === o[0]);
      }).join('') + '</select></label>' +
      '<label>Period <select class="mr-filter" data-key="period">' +
      [['today', 'Today'], ['7', 'Last 7 days'], ['30', 'Last 30 days'],
       ['all', 'All']].map(function (o) {
        return opt(o[0], o[1], flt.period === o[0]);
      }).join('') + '</select></label>' +
      (flt.client ? '<span class="mr-reports">Reports for <b>' +
        escapeHtml(flt.client) + '</b>: ' + mirrorReportLink(flt.client,
          'client', 'Client report', flt.period === 'all' ? 0 :
          flt.period === 'today' ? 1 : flt.period) + ' ' +
        mirrorReportLink(flt.client, 'trader', 'Trader report',
          flt.period === 'all' ? 0 : flt.period === 'today' ? 1 :
          flt.period) + '</span>' : '') + '</div>';
    // 4. ONE ROW PER FOLLOWER - trouble first.
    function rank(f) {
      return (high.some(function (a) { return a.follower === f.name; }) ? 0
        : f.halted ? 1 : f.last_error ? 2 : f.enabled ? 3 : 4);
    }
    followers.sort(function (a, b) {
      return rank(a) - rank(b) || String(a.name).localeCompare(b.name);
    });
    var shown = flt.client ? followers.filter(function (f) {
      return f.name === flt.client; }) : followers;
    function usd0(x) {
      return '$' + Math.round(x).toLocaleString('en-US');
    }
    html += '<h4>Followers <small>— each takes a multiple of every ' +
      'Algo trade, in its own account. Limits stop NEW copies only; a ' +
      'copy on always closes.</small></h4>' +
      '<table class="mr-table"><thead><tr><th class="l">Follower</th>' +
      '<th>Multiple</th><th>Status</th><th>Account</th><th>Execution</th>' +
      '<th>Capital</th><th>Holding</th><th>P&amp;L today</th>' +
      '<th>P&amp;L month</th><th title="margin its open copies tie up / ' +
      'its max">Margin</th><th title="today’s closed P&amp;L / its ' +
      'daily loss limit">Day loss</th><th title="copies today / its max">' +
      'Trades</th><th class="l">Last error</th><th>Actions</th></tr>' +
      '</thead><tbody>' + shown.map(function (f) {
        var alarm = high.some(function (a) { return a.follower === f.name; });
        var status = f.removed
          ? '<span class="mr-pill skip">removed \u2014 closes only</span>'
          : f.halted
          ? '<span class="mr-pill alarm" title="' + escapeHtml(
              f.halted.why || '') + '">HALTED</span>'
          : !f.enabled ? '<span class="mr-pill skip">off — closes only' +
            '</span>'
          : f.blocked ? '<span class="mr-pill bad" title="' +
            escapeHtml(f.blocked) + '">limit reached</span>'
          : '<span class="mr-pill ok">following</span>';
        var dayLoss = Math.max(0, -(f.pnl_today || 0));
        return '<tr class="' + (alarm ? 'alarm-row' : '') +
          (flt.client === f.name ? ' mr-picked' : '') + '">' +
          '<td class="l"><b>' + escapeHtml(f.name) + '</b></td>' +
          '<td>×' + fmt(f.multiple, 2) + '</td><td>' + status +
          '</td><td>' + (f.demo ? 'demo'
            : '<span class="mr-pill alarm">LIVE</span>') + '</td>' +
          '<td>' + (f.execution === 'TAKER' ? 'always taker'
            : 'as the trade') + '</td>' +
          '<td>' + (f.capital ? usd0(f.capital) : DASH) + '</td>' +
          '<td>' + (f.holding ? '<b class="up">' + f.holding + '</b>' : '0') +
          '</td><td>' + mirrorMoney(f.pnl_today) + '</td><td>' +
          mirrorMoney(f.pnl_month) + (f.return_month_pct === null ||
            f.return_month_pct === undefined ? '' : ' <small>(' +
            pctSigned(f.return_month_pct, 2) + ')</small>') + '</td>' +
          '<td>' + mirrorLimit(f.margin_held || 0, f.max_margin, usd0) +
          '</td><td>' + mirrorLimit(dayLoss, f.daily_loss_limit,
            function (x) { return money(x); }) + '</td><td>' +
          mirrorLimit(f.trades_today || 0, f.max_trades_day,
            function (x) { return String(x); }) + '</td>' +
          '<td class="l down mr-why">' + escapeHtml(f.last_error || '') +
          '</td><td class="mr-acts">' +
          (f.halted
            ? '<button class="mr-btn mr-act" data-act="RESUME" data-name="' +
              escapeHtml(f.name) + '">Resume</button>'
            : '<button class="mr-btn mr-act" data-act="HALT" data-name="' +
              escapeHtml(f.name) + '" title="No new copies; what it holds ' +
              'still closes with the trade">Halt</button>') +
          (f.holding ? ' <button class="mr-btn mr-act danger" ' +
            'data-act="CLOSE_COPIES" data-name="' + escapeHtml(f.name) +
            '" title="Close every copy it holds NOW, at market, and halt it' +
            '">Close copies</button>' : '') + ' ' +
          mirrorReportLink(f.name, 'client', 'Client') + ' ' +
          mirrorReportLink(f.name, 'trader', 'Trader') + '</td></tr>';
      }).join('') + '</tbody></table>';
    // 5. THE TRADES, filtered.
    var since = flt.period === 'all' ? 0 : Date.now() / 1000 -
      (flt.period === 'today' ? (Date.now() / 1000) % 86400
        : Number(flt.period) * 86400);
    var live = ['OPEN', 'PENDING', 'CLOSING', 'CLOSE_FAILED'];
    var trades = (mr.trades || []).filter(function (t) {
      if ((t.opened_at || 0) < since) { return false; }
      var copies = Object.keys(t.followers || {}).filter(function (n) {
        return !flt.client || n === flt.client;
      }).map(function (n) { return t.followers[n]; });
      if (flt.client && !copies.length) { return false; }
      if (flt.show === 'open') {
        return copies.some(function (f) { return live.indexOf(f.status) >= 0; });
      }
      if (flt.show === 'attention') {
        return copies.some(function (f) {
          return f.status === 'CLOSE_FAILED' || f.status === 'FAILED'; });
      }
      if (flt.show === 'failed') {
        return copies.some(function (f) {
          return ['FAILED', 'CLOSE_FAILED', 'SKIPPED']
            .indexOf(f.status) >= 0; });
      }
      if (flt.show === 'closed') {
        return t.gone && copies.every(function (f) {
          return live.indexOf(f.status) < 0; });
      }
      return true;
    });
    state.mirrorOpen = state.mirrorOpen || {};
    html += '<h4>Copied trades <small>— ' + trades.length + ' shown' +
      (flt.client ? ' for ' + escapeHtml(flt.client) : '') + ', newest ' +
      'first. Click a trade for each account’s legs and reason. A ' +
      'trade first seen more than ' + fmt(mr.open_window_sec, 0) + 's ' +
      'after it opened is not copied.</small></h4>' +
      '<table class="mr-table mr-trades"><thead><tr><th></th>' +
      '<th>Opened</th><th class="l">Ladder</th><th>Trade</th>' +
      '<th>In / out</th><th class="l">Legs</th><th class="l">' +
      (flt.client ? escapeHtml(flt.client) + '’s copy' : 'Copies') +
      '</th>' + (flt.client ? '<th>Its P&amp;L</th>' : '') +
      '</tr></thead><tbody>';
    if (!trades.length) {
      html += '<tr><td colspan="8" class="hint">No trade matches these ' +
        'filters.</td></tr>';
    }
    trades.forEach(function (t) {
      var open = !!state.mirrorOpen[t.position_id];
      var counts = t.counts || {};
      var order = ['CLOSE_FAILED', 'FAILED', 'PENDING', 'CLOSING', 'OPEN',
                   'CLOSED', 'SKIPPED'];
      var tally = Object.keys(counts).sort(function (a, b) {
        return order.indexOf(a) - order.indexOf(b);
      }).map(function (k) {
        var st = MIRROR_STATUS[k] || [k, ''];
        return '<span class="mr-pill ' + st[1] + '">' + counts[k] + ' ' +
          st[0] + '</span>';
      }).join(' ');
      var names = Object.keys(t.followers || {}).sort().filter(function (n) {
        return !flt.client || n === flt.client;
      });
      var mine = flt.client ? t.followers[flt.client] : null;
      var styleIn = t.style === 'MAKER' ? 'maker' : 'taker';
      var styleOut = !t.gone ? '…' :
        (t.close_style === 'MAKER' ? 'maker' : 'taker');
      html += '<tr class="mr-trade" data-pid="' + escapeHtml(t.position_id) +
        '"><td class="mr-caret">' + (open ? '▾' : '▸') + '</td>' +
        '<td>' + (t.opened_at ? anClock(t.opened_at) : DASH) + '</td>' +
        '<td class="l">' + escapeHtml(t.pair_key || '') + '</td><td>' +
        (t.gone ? 'closed' : '<b class="up">open</b>') + '</td>' +
        '<td>' + styleIn + ' / ' + styleOut + '</td>' +
        '<td class="l">' + mirrorLegs(t.legs) + '</td><td class="l">' +
        (mine ? (function () {
            var st = MIRROR_STATUS[mine.status] || [mine.status, ''];
            return '<span class="mr-pill ' + st[1] + '">' + st[0] +
              '</span> ' + (mirrorLegs(mine.legs) || '') +
              (mine.why ? '<div class="mr-why hint">' +
                escapeHtml(mine.why) + '</div>' : '');
          }()) :
          (tally || '<span class="hint">no follower on this ladder</span>') +
          '<div class="mr-chips">' + names.map(function (n) {
            var f = t.followers[n], st = MIRROR_STATUS[f.status] ||
              [f.status, ''];
            return '<span class="mr-chip ' + st[1] + '" title="' +
              escapeHtml(n + ': ' + st[0] + (f.pnl !== undefined &&
                f.pnl !== null ? ' · P&L ' + money(f.pnl) : '') +
                (f.why ? ' — ' + f.why : '')) + '">' + escapeHtml(n) +
              '</span>';
          }).join('') + '</div>') + '</td>' +
        (flt.client ? '<td>' + mirrorMoney(mine && mine.pnl) + '</td>' : '') +
        '</tr>';
      if (open) {
        html += '<tr class="mr-detail"><td></td><td colspan="7">' +
          '<table class="mr-table mr-inner"><thead><tr>' +
          '<th class="l">Account</th><th>Status</th><th>In</th><th>Out</th>' +
          '<th class="l">Its legs</th><th>P&amp;L</th><th>Fees</th>' +
          '<th class="l">Reason</th></tr></thead><tbody>' +
          names.map(function (n) {
            var f = t.followers[n], st = MIRROR_STATUS[f.status] ||
              [f.status, ''];
            return '<tr><td class="l"><b>' + escapeHtml(n) + '</b></td>' +
              '<td><span class="mr-pill ' + st[1] + '">' + st[0] +
              '</span></td><td>' + (f.style ? f.style.toLowerCase() : DASH) +
              '</td><td>' + (f.style_out ? f.style_out.toLowerCase() : DASH) +
              '</td><td class="l">' + (mirrorLegs(f.legs) || DASH) +
              '</td><td>' + mirrorMoney(f.pnl) + '</td><td>' +
              (f.fees === undefined || f.fees === null ? DASH
                : money(f.fees)) + '</td><td class="l mr-why">' +
              escapeHtml(f.why || '') + '</td></tr>';
          }).join('') + '</tbody></table></td></tr>';
      }
    });
    return html + '</tbody></table></div>';
  }

  function sizingSheetHtml() {
    /* THE PRINTABLE SIZING SHEET: the table as the Sizing tab shows it
     * now, with the settings it was worked out on, as a page of its own
     * - A4 landscape, scaled to ONE page. */
    var cfg = sizingSettings();
    var mk = sizingMarket();
    var holder = document.createElement('div');
    holder.innerHTML = sizingPane();
    var table = holder.querySelector('.sz-table');
    var notes = holder.querySelector('.sz-notes');
    var lev = cfg.lev || mk.lev;
    var settings = [
      ['Win rate', cfg.win + '%'],
      ['Free capital', cfg.freePct + '%'],
      ['Target', cfg.target + '% of ' + cfg.targetBase],
      ['Stop', cfg.stop + '% of ' + cfg.stopBase],
      ['Maker / taker fills', cfg.makerPct + '% / ' + (100 - cfg.makerPct) +
        '%'],
      ['Trades a day', cfg.perDay + ' on ' + (cfg.days || 30) +
        ' trading days (' + cfg.perDay * (cfg.days || 30) + ' a month)'],
      ['Leverage', lev + 'x'],
      ['Prices', mk.live ? 'ETH ' + mk.eth.toFixed(2) + ', BTC ' +
        Math.round(mk.btc).toLocaleString('en-US') : 'no live price: ETH ' +
        '1,720, BTC 61,400']];
    var now = new Date();
    var mark = '<svg class="mark" viewBox="0 0 16 16"><rect x="4" y="7.2" ' +
      'width="8" height="1.6" fill="#1a1a1a"/><circle cx="4.6" cy="8" ' +
      'r="2.6" fill="#2f6fb0"/><circle cx="11.4" cy="8" r="2.6" ' +
      'fill="#c0342f"/></svg>';
    var css = '@page{size:A4 landscape;margin:6mm}' +
      '*{box-sizing:border-box}body{margin:0;font:11px "Segoe UI",Arial,' +
      'sans-serif;color:#1f2933;background:#e9ecf0;' +
      '-webkit-print-color-adjust:exact;print-color-adjust:exact}' +
      '.bar{position:sticky;top:0;background:#16324f;color:#dbe4ee;' +
      'padding:8px 14px;display:flex;gap:12px;align-items:center}' +
      '.bar button{font:600 12px inherit;padding:5px 14px;border:0;' +
      'border-radius:4px;background:#fff;color:#16324f;cursor:pointer}' +
      '.sheet{background:#fff;margin:12px auto;padding:10px 12px;' +
      'width:max-content}' +
      '.head{display:flex;justify-content:space-between;align-items:flex-end;' +
      'border-bottom:3px solid #16324f;padding-bottom:6px;margin-bottom:6px;' +
      'gap:24px}.brand{display:flex;gap:10px;align-items:center}' +
      '.mark{width:40px;height:40px}.desk{font-weight:700;letter-spacing:' +
      '.18em;font-size:10px;color:#6b7280}h1{margin:0;font-size:20px;' +
      'color:#16324f}.set{display:grid;grid-template-columns:repeat(4,auto);' +
      'gap:1px 16px;font-size:10px}.set span{color:#6b7280;margin-right:4px}' +
      // The summary and the notes wrap to the TABLE's width rather than
      // widening the sheet with one long line (width 0, min 100%).
      '.sum{font-size:10px;margin:4px 0 6px;color:#374151;width:0;' +
      'min-width:100%}' +
      // As narrow as the numbers allow: the headings wrap, the numbers
      // never do - the narrower the sheet, the bigger it prints.
      'table{border-collapse:collapse;font-variant-numeric:tabular-nums;' +
      'width:1px}' +
      'th,td{border:1px solid #c9cfd8;padding:1px 4px;text-align:center;' +
      'white-space:nowrap;font-size:10px}th{background:#16324f;color:#fff;' +
      'font-weight:600;white-space:normal;line-height:1.15}td small,th small{font-size:8px;opacity:.75}' +
      'tbody tr:nth-child(even) td{background:#f3f5f8}' +
      '.up{color:#127a3a}.down{color:#b42318}' +
      '.notes{font-size:9px;color:#6b7280;margin-top:5px;width:0;' +
      'min-width:100%;white-space:normal}' +
      '.head{width:0;min-width:100%}' +
      '@media print{body{background:#fff}.bar{display:none}' +
      '.sheet{margin:0;padding:0}}';
    // ONE PAGE: the sheet is scaled to the printable area of A4
    // landscape (6 mm margins: about 1077 x 748 px at 96 dpi).
    var fit = 'function fit(){var s=document.querySelector(".sheet");' +
      's.style.zoom=1;var k=Math.min(1,1060/s.offsetWidth,' +
      '735/s.offsetHeight);s.style.zoom=k;}' +
      'fit();window.addEventListener("load",fit);' +
      'window.addEventListener("beforeprint",fit);' +
      'if(document.fonts){document.fonts.ready.then(fit);}';
    return '<!doctype html><html><head><meta charset="utf-8">' +
      '<title>Nexus \u2014 Position Sizing Sheet</title><style>' + css +
      '</style></head><body><div class="bar"><button onclick="' +
      'window.print()">Print / Save as PDF</button><span>One A4 landscape ' +
      'page. In the print dialog, turn on \u201cBackground graphics\u201d ' +
      'for the colours.</span></div><div class="sheet"><div class="head">' +
      '<div class="brand">' + mark + '<div><div class="desk">NEXUS</div>' +
      '<h1>Position Sizing Sheet</h1></div></div><div class="set">' +
      settings.map(function (kv) {
        return '<div><span>' + kv[0] + '</span>' + escapeHtml(kv[1]) +
          '</div>';
      }).join('') + '<div><span>Prepared</span>' +
      escapeHtml(now.toLocaleString()) + '</div></div></div>' +
      '<div class="sum">' + (holder.querySelector('.sz-sum') || {}).innerHTML +
      '</div>' + (table ? table.outerHTML : '') +
      '<div class="notes">' + (notes ? notes.innerHTML : '') + '</div>' +
      '</div><script>' + fit + '<\/script></body></html>';
  }

  function printSizingSheet() {
    var page = window.open('', '_blank');
    if (!page) {
      toast('The sheet could not open - allow pop-ups for this page');
      return;
    }
    page.document.open();
    page.document.write(sizingSheetHtml());
    page.document.close();
  }

  function sizingPane() {
    var cfg = sizingSettings();
    var mk = sizingMarket();
    var perMonth = cfg.perDay * (cfg.days || 30);
    function k(x) {
      var a = Math.abs(x);
      // A million and up in M: "$1.25M", not "$1,250k".
      if (a >= 1e6) {
        return '$' + String(parseFloat((a / 1e6).toFixed(2))) + 'M';
      }
      return (a < 100e3 && Math.round(a) % 1000 ? '$' + (a / 1e3).toFixed(1)
        : '$' + Math.round(a / 1e3).toLocaleString('en-US')) + 'k';
    }
    function usd(x) {
      return '$' + Math.round(Math.abs(x)).toLocaleString('en-US');
    }
    function pct(x, d) {
      return (x >= 0 ? '+' : '−') + Math.abs(x).toFixed(d === undefined
        ? 1 : d) + '%';
    }
    var wins = SZ_WINS.map(function (v) {
      return '<button type="button" class="sz-win' +
        (v === cfg.win ? ' on' : '') + '" data-win="' + v + '">' + v +
        '%</button>';
    }).join('');
    function input(key, label, step, unit) {
      return '<label class="sz-field"><span>' + label + '</span><input ' +
        'class="sz-input" id="sz-' + key + '" type="number" min="0" step="' +
        step + '" data-key="' + key + '" value="' + cfg[key] + '">' +
        (unit || '') + '</label>';
    }
    function base(key) {
      return '<select class="sz-input" id="sz-' + key + '" data-key="' + key +
        '" data-kind="text"><option value="margin"' +
        (cfg[key] === 'margin' ? ' selected' : '') + '>of margin</option>' +
        '<option value="capital"' + (cfg[key] === 'capital' ? ' selected'
          : '') + '>of capital</option></select>';
    }
    var lev = cfg.lev || mk.lev;
    var html = '<div class="sz">' +
      '<div class="sz-bar"><span class="sz-label">Win rate</span>' + wins +
      input('win', 'or', 0.5, '%') + '</div>' +
      '<div class="sz-bar sz-fields">' +
      input('freePct', 'Free capital', 5, '%') +
      '<label class="sz-field"><span>Profit (target)</span><input ' +
        'class="sz-input" id="sz-target" type="number" min="0" step="0.1" ' +
        'data-key="target" value="' + cfg.target + '">%' + base('targetBase') +
        '</label>' +
      '<label class="sz-field"><span>Loss (stop)</span><input ' +
        'class="sz-input" id="sz-stop" type="number" min="0" step="0.1" ' +
        'data-key="stop" value="' + cfg.stop + '">%' + base('stopBase') +
        '</label>' +
      input('makerPct', 'Maker fills', 5, '% (taker ' +
        (100 - cfg.makerPct) + '%)') +
      input('perDay', 'Trades / day', 1) +
      input('days', 'Trading days / month', 1) +
      '<label class="sz-field"><span>Leverage</span><input class="sz-input" ' +
        'id="sz-lev" type="number" min="1" step="1" data-key="lev" value="' +
        (cfg.lev || '') + '" placeholder="' + mk.lev + '">x</label>' +
      '<button type="button" class="sz-reset">Reset</button>' +
      '<button type="button" class="sz-print" title="This sheet, with the ' +
      'settings above, on one A4 landscape page - print it or save it as ' +
      'a PDF">Print sheet</button></div>';
    // The summary, at $100k so every percentage reads against one
    // example; each row has its own figures below.
    var ex = sizingRow(100e3, cfg, mk);
    html += '<div class="sz-sum">At $100k capital: target <b>+' +
      usd(ex.target) + '</b> net, stop <b>−' + usd(ex.stop) +
      '</b> all in, fees <b>' + (ex.fees < 0 ? '\u2212' + usd(-ex.fees) +
        ' (a rebate)' : usd(ex.fees)) + '</b> a trade (' +
      (ex.feePts || 0).toFixed(1) + ' pts) → the spread moves <b>' +
      (ex.targetPts || 0).toFixed(1) + ' pts</b> to the target, <b>' +
      (ex.stopPts || 0).toFixed(1) + ' pts</b> to the stop' +
      ' · break-even win rate <b>' +
      (ex.breakEven === null ? DASH : ex.breakEven.toFixed(1) + '%') +
      '</b> · at ' + cfg.win + '%: <b class="' +
      (ex.month >= 0 ? 'up' : 'down') + '">' + pct(ex.month / 1e3, 1)
        .replace('%', '') + 'k a month (' + pct(ex.month / ex.capital * 100) +
      ' of capital)</b> · ' + perMonth + ' trades a month at ' + lev +
      'x · ' + (mk.live ? 'live: ETH ' + mk.eth.toFixed(2) + ', BTC ' +
        Math.round(mk.btc).toLocaleString('en-US')
        : 'no live price yet: ETH 1,720, BTC 61,400') + '</div>';
    var losses = SZ_LOSS_SHARES.map(function (p) {
      return Math.ceil(perMonth * p / 100);
    });
    html += '<div class="sz-wrap"><table class="sz-table"><thead>' +
      '<tr><th rowspan="2">Sr</th><th colspan="5">Size</th>' +
      '<th colspan="4">Per trade (after all costs)</th><th colspan="5">Volume and ' +
      'fees</th><th colspan="3">Month at ' + cfg.win + '% wins</th>' +
      '<th colspan="7">Drawdown if this share of the ' + perMonth +
      ' trades lose, back to back</th></tr><tr>' +
      '<th>Capital in hand</th><th>Margin / trade</th><th>Free</th>' +
      '<th>Position (both legs)</th><th>$ / point</th>' +
      '<th>Target</th><th>Stop</th><th>Target move (pts)</th>' +
      '<th>Stop move (pts)</th>' +
      '<th>30-day volume</th><th>Fee tier</th><th>Maker / Taker (bp)</th>' +
      '<th>Fees / trade</th><th>Break-even win</th>' +
      '<th>Net P&amp;L</th><th>Return on capital</th>' +
      '<th>Return on margin</th>' +
      SZ_LOSS_SHARES.map(function (p, i) {
        return '<th>' + p + '%<br><small>' + losses[i] + ' losses</small></th>';
      }).join('') + '</tr></thead><tbody>';
    // Capital in hand: $5k, $10k, then $25k steps to $200k, $100k steps
    // to $500k and $250k steps to $10M.
    var caps = [5e3, 10e3];
    for (var c = 25e3; c <= 200e3; c += 25e3) { caps.push(c); }
    for (c = 300e3; c <= 500e3; c += 100e3) { caps.push(c); }
    for (c = 750e3; c <= 10e6; c += 250e3) { caps.push(c); }
    caps.forEach(function (capital, i) {
      var r = sizingRow(capital, cfg, mk);
      var cls = r.month >= 0 ? 'up' : 'down';
      var beCls = r.breakEven !== null && r.breakEven > cfg.win ? 'down' : '';
      html += '<tr><td>' + (i + 1) + '</td><td>' + k(capital) + '</td><td>' +
        k(r.margin) + '</td><td>' + k(r.free) + '</td><td>$' +
        (r.position / 1e6).toFixed(2) + 'M</td><td>' + usd(r.point) +
        '</td><td>+' + usd(r.target) + '</td><td>−' + usd(r.stop) +
        '</td><td>' + (r.targetPts || 0).toFixed(1) + '</td><td class="' +
        (r.fees >= r.stop ? 'down' : '') + '">' +
        (r.stopPts || 0).toFixed(1) + '</td><td>' + (r.volume >= 1e9
          ? '$' + (r.volume / 1e9).toFixed(2) + 'B'
          : '$' + Math.round(r.volume / 1e6).toLocaleString('en-US') + 'M') +
        '</td><td title="' + escapeHtml(r.tier[4]
          ? 'reached on its ' + r.tier[4] : 'below VIP 1') + '">' +
        escapeHtml(r.tier[0]) + (r.tier[4] ? ' <small>(' +
          escapeHtml(r.tier[4]) + ')</small>' : '') + '</td><td>' +
        fmt(r.tier[2], 2).replace(/(\.\d)0$/, '$1') + ' / ' +
        fmt(r.tier[3], 1) + '</td><td>' +
        (r.fees < 0 ? '\u2212' + usd(-r.fees) + ' <small>rebate</small>'
         : usd(r.fees)) + '</td><td class="' + beCls +
        '">' + (r.breakEven === null ? DASH : r.breakEven.toFixed(1) + '%') +
        '</td><td class="' + cls + '">' + (r.month >= 0 ? '+' : '−') +
        k(r.month) + '</td><td class="' + cls + '">' +
        pct(r.month / capital * 100) + '</td><td class="' + cls + '">' +
        pct(r.margin ? r.month / r.margin * 100 : 0) + '</td>' +
        losses.map(function (n) {
          return '<td>−' + k(n * r.lossPerStop) + ' <small>(' +
            (n * r.lossPerStop / capital * 100).toFixed(1) + '%)</small></td>';
        }).join('') + '</tr>';
    });
    html += '</tbody></table></div><div class="sz-notes">' +
      '<b>Target</b> is the profit after every cost; <b>Stop</b> is the ' +
      'whole loss, costs included. <b>Fees</b> follow the maker / taker ' +
      'mix at the tier each row reaches on OKX’s futures fee table for ' +
      'this account: Regular 2.0 / 5.0 bp below 18M AED of 30-day volume ' +
      'and 400k AED of assets, down to VIP 9 (−0.5 / 1.5 bp) - on volume ' +
      'or assets, whichever is higher, at 3.6725 AED a dollar. A ' +
      'negative maker fee is a rebate. The volume tier comes once a ' +
      'month at that volume is behind it; the first month pays the tier ' +
      'the account is on. ' +
      'They set how far the spread must ' +
      'move: target + fees to win, stop − fees before the stop. ' +
      '<b>Break-even win</b> = stop ÷ (target + stop). <b>Stop move ' +
      'in red:</b> the fees alone use up the stop. ' +
      '<b>$ / point:</b> P&amp;L ' +
      'for one point of spread (−20 → −19). <b>Volume</b> = ' +
      'position × 2 (open and close) × trades a day × trading days a ' +
      'month. <b>Not included:</b> slippage on stops, ' +
      'funding.' +
      '</div></div>';
    return html;
  }

  function sizeDollars(row, qty) {
    /* ' · $3,563': a size in spreads as money, both legs together at
     * the mid now. Nothing when a leg's price or size is not known. */
    var legs = (row || {}).leg_notional || {};
    if (legs.a === null || legs.a === undefined || legs.b === null ||
        legs.b === undefined || !qty) { return ''; }
    var total = (legs.a + legs.b) * qty;
    return ' <small>\u00b7 $' + Math.round(total).toLocaleString('en-US') +
      '</small>';
  }

  function algoMarginHtml(row, qty) {
    /* MARGIN & LEVERAGE, in the Algo window: OKX's leverage on each leg,
     * what one Qty and one Algo trade tie up and are worth, that margin
     * against the account's equity. */
    var exit = row.exit || {};
    var legs = row.leg_notional || {};
    var perQty = exit.margin_per_spread;
    var la = row.leverage_a, lb = row.leverage_b;
    function lev(x) { return x ? fmt(x, 0) + 'x' : DASH; }
    var levCls = la && lb && la !== lb ? 'down' : '';
    var notional = (legs.a !== null && legs.a !== undefined &&
                    legs.b !== null && legs.b !== undefined)
      ? legs.a + legs.b : null;
    var trade = perQty ? perQty * qty : null;
    if (trade === null && la && lb && legs.a !== null &&
        legs.a !== undefined && legs.b !== null && legs.b !== undefined) {
      // Not priced by OKX yet: each leg's value over its own leverage.
      trade = (legs.a / la + legs.b / lb) * qty;
    }
    var accounts = (state.snapshot || {}).accounts || {};
    var equity = ((accounts[row.account_a] || {}).equity);
    var share = trade && equity ? trade / equity * 100 : null;
    // The TRADE first - what one Algo trade is worth and ties up - and
    // one Qty after it, labelled as such: 'Notional / Qty' beside a
    // size of 30 read as the trade's own value.
    // ONE ROW PER LINE, the label left and the figures right on one
    // line: two columns wrapped the longer figures ('$699.95 A $353.00 ·
    // B $346.85') onto three lines under the wrong label.
    var qtyWords = ' · ' + fmt(qty, 2) + ' Qty';
    // Sized by total margin: what it was given, and what it came to.
    var sizing = row.algo_sizing;
    function legMargin(leg) {
      var value = legs[leg], lever = leg === 'a' ? la : lb;
      return value === null || value === undefined || !lever ? null
        : value * qty / lever;
    }
    return '<div class="aw-head">Margin &amp; leverage</div>' +
      '<div class="aw-grid aw-margin">' +
      kv('Leverage', 'A ' + lev(la) + ' · B ' + lev(lb), levCls,
         'the leverage OKX holds on each leg (cross). ' +
         (levCls ? 'The two legs differ: the lower one ties up more ' +
           'margin - set both in the OKX app.' : ''), true) +
      (sizing ? kv('Sized by', 'Total margin ' + money(sizing.margin),
         sizing.note ? 'down' : '',
         sizing.note ? sizing.note : 'the Algo’s Qty is worked out ' +
           'from this total on every poll: ' + fmt(sizing.qty, 2) +
           ' Qty now, rounded down', true) : '') +
      kv('Trade size', notional === null ? DASH
         : money(notional * qty) + qtyWords, '',
         'one Algo trade: both legs’ value at the mid now - leg A ' +
         money(legs.a === null || legs.a === undefined ? null : legs.a * qty) +
         ', leg B ' +
         money(legs.b === null || legs.b === undefined ? null : legs.b * qty),
         true) +
      kv('Trade margin', trade === null ? DASH : money(trade) +
         (legMargin('a') === null || legMargin('b') === null ? ''
           : ' · A ' + money(legMargin('a')) + ' · B ' +
             money(legMargin('b'))), '',
         'what one Algo trade ties up at OKX, both legs, and each leg’s ' +
         'share (its value over its leverage)' +
         (exit.margin_source ? ' (' + exit.margin_source + ')' : ''), true) +
      kv('Of equity', share === null ? DASH : fmt(share, 1) + '%',
         share !== null && share > 50 ? 'down' : '',
         'one Algo trade’s margin against the account’s equity' +
         (equity ? ' (' + money(equity) + ')' : ''), true) +
      kv('Per 1 Qty', (notional === null ? DASH : money(notional)) +
         ' · margin ' + (perQty ? money(perQty) : DASH), '',
         'ONE spread (one Qty): leg A ' + money(legs.a) + ' + leg B ' +
         money(legs.b) + ', and the margin it ties up', true) +
      '</div>';
  }

  function btcDominanceKv() {
    /* BTC.D: Bitcoin's share of the crypto market, from CoinGecko, and
     * its move over 24 h in percentage points. Context for ETH vs BTC -
     * nothing is decided from it. Hidden when it is switched off. */
    var d = (state.snapshot || {}).btc_dominance;
    if (!d) { return ''; }
    var text = DASH, cls = '';
    if (d.pct !== null && d.pct !== undefined) {
      text = d.pct.toFixed(2) + '%';
      if (d.change_24h !== null && d.change_24h !== undefined) {
        text += ' <small>' + (d.change_24h >= 0 ? '\u25b2 +' : '\u25bc ') +
          d.change_24h.toFixed(2) + '</small>';
      }
      // The last good reading, kept while the source does not answer.
      if (d.error) { text += ' <small>(old)</small>'; }
    }
    var title = 'Bitcoin\u2019s share of the whole crypto market\u2019s ' +
      'capitalisation (' + (d.source || 'CoinGecko') + '), and its move ' +
      'over 24 h in percentage points. Rising: money moving into ' +
      'Bitcoin. A reading only - the Algo decides nothing from it.' +
      (d.age_sec !== null && d.age_sec !== undefined
        ? ' Read ' + Math.round(d.age_sec / 60) + ' min ago.' : '') +
      (d.error ? ' Last try: ' + d.error : '');
    return kv('BTC.D', text, cls, title);
  }

  function chartVpOn() {
    /* The volume profile, on unless the trader turned it off (per
     * browser). */
    if (state.chartVp === undefined) {
      try {
        state.chartVp = window.localStorage.getItem('okxtrader.chartVp')
          !== '0';
      } catch (err) { state.chartVp = true; }
    }
    return state.chartVp;
  }

  function volumeProfile(candles, lo, hi, rows) {
    /* [{lo, hi, v}] - the volume traded in each of `rows` even price
     * bands from lo to hi, over the candles given. Each candle's volume
     * sits at the prices its FINER bars closed at (`f`: [price, volume]),
     * not all at its close. The point of control is the fullest row. */
    var step = (hi - lo) / rows, out = [];
    for (var r = 0; r < rows; r++) {
      out.push({lo: lo + r * step, hi: lo + (r + 1) * step, v: 0});
    }
    var total = 0;
    candles.forEach(function (c) {
      (c && c.f || []).forEach(function (pv) {
        var i = Math.floor((pv[0] - lo) / step);
        if (i < 0 || i >= rows || !(pv[1] > 0)) { return; }
        out[i].v += pv[1];
        total += pv[1];
      });
    });
    return total > 0 ? out : [];
  }

  function svgCandles(box, candles, opts) {
    /* One candlestick plot at the box's own pixel size, so the labels
     * are crisp. Candles sit on the CLOCK: a stretch the broker sent
     * nothing for is left as a gap, not closed up. `candles` may hold
     * nulls - a slot with nothing to draw (a z before the band has its
     * length) - and `opts.times` gives every slot its time, so the z
     * and the spread line up candle for candle.
     *
     * `opts.lines` [{y, colour, dash, cls}] go straight across;
     * `opts.curves` [{value(c), colour, dash, cls}] follow the candles,
     * like a band. Returns the SVG and the geometry the hover reads. */
    // The room the box actually has: a window dragged small draws a
    // small chart, rather than a full-size one cropped by its frame.
    var w = Math.max(box.clientWidth, 120);
    var h = Math.max(box.clientHeight, 50);
    // With a price tag, the right margin is the TAG's: it sits beside
    // the newest candle, never on it. The same width on the z and the
    // spread chart, so the two keep the same candle slots.
    var left = 52, right = opts.nowTag ? CHART_TAG_ROOM : 8, top = 8,
        bottom = 20;
    var lines = opts.lines || [], curves = opts.curves || [];
    var n = candles.length;
    var ys = [];
    candles.forEach(function (c) { if (c) { ys.push(c.h, c.l); } });
    lines.forEach(function (line) { ys.push(line.y); });
    curves.forEach(function (curve) {
      candles.forEach(function (c) {
        var v = c ? curve.value(c) : null;
        if (v !== null && v !== undefined && isFinite(v)) { ys.push(v); }
      });
    });
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    if (!isFinite(lo) || !isFinite(hi)) { lo = -1; hi = 1; }
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    var pad = (hi - lo) * 0.06;
    lo -= pad; hi += pad;
    var times = opts.times || [];
    var seconds = opts.seconds || 60;
    var t0 = times.length ? times[0] : 0;
    var t1 = times.length ? times[times.length - 1] + seconds : 1;
    var plotW = w - left - right;
    var slot = seconds / (t1 - t0) * plotW;
    function X(i) {
      return left + (times[i] + seconds / 2 - t0) / (t1 - t0) * plotW;
    }
    function Y(v) { return top + (hi - v) / (hi - lo) * (h - top - bottom); }
    var body = Math.max(2, Math.min(14, slot * 0.7));
    var svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' +
      w + ' ' + h + '" xmlns="http://www.w3.org/2000/svg">';
    // Ticks the caller names (z: whole sigmas), or five even ones.
    // ROUND numbers on the axis - 1, 2, 2.5 or 5 times a power of ten -
    // not five even slices of whatever the range happens to be (which
    // read 146.1903, 67.4153, -11.4542...).
    var digits = opts.digits;
    var ticks = opts.ticks;
    if (!ticks) {
      var want = Math.max(2, Math.min(6, Math.floor((h - top - bottom) / 28)));
      var raw = (hi - lo) / want;
      var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
      var step = [1, 2, 2.5, 5, 10].map(function (k) { return k * mag; })
        .filter(function (v) { return v >= raw; })[0] || 10 * mag;
      ticks = [];
      for (var v0 = Math.ceil(lo / step) * step; v0 <= hi + 1e-9;
           v0 += step) {
        ticks.push(Math.abs(v0) < step * 1e-9 ? 0 : v0);
      }
      digits = Math.max(0, -Math.floor(Math.log(step) / Math.LN10 + 1e-9)) +
        (Math.abs(step / mag - 2.5) < 1e-9 ? 1 : 0);
    }
    ticks = ticks.filter(function (v) { return v >= lo && v <= hi; });
    // Thinned to the room there is: a label every 14 px at least, so a
    // small window shows fewer numbers rather than numbers on numbers.
    var room = h - top - bottom;
    var most = Math.max(2, Math.floor(room / 14) + 1);
    if (ticks.length > most) {
      var every = Math.ceil((ticks.length - 1) / (most - 1));
      ticks = ticks.filter(function (_v, i) {
        return i % every === 0 || i === ticks.length - 1;
      });
    }
    ticks.forEach(function (v) {
      svg += '<line x1="' + left + '" x2="' + (w - right) + '" y1="' +
        Y(v).toFixed(1) + '" y2="' + Y(v).toFixed(1) + '" stroke="' +
        CHART.grid + '" stroke-width="1"/>' +
        '<text x="' + (left - 4) + '" y="' + (Y(v) + 4).toFixed(1) +
        '" text-anchor="end" font-size="11" fill="' + CHART.axis + '">' +
        v.toFixed(digits) + '</text>';
    });
    // THE VOLUME PROFILE, behind everything priced: a bar per price
    // band from the right edge of the plot, as long as the volume
    // traded there. The fullest one (the point of control) darker,
    // with a dotted line across at its middle.
    if (opts.profile) {
      var rows = Math.max(8, Math.min(48, Math.floor((h - top - bottom) / 5)));
      var vp = volumeProfile(candles, lo, hi, rows);
      if (vp.length) {
        var vpMost = 0, poc = null;
        vp.forEach(function (b) { if (b.v > vpMost) { vpMost = b.v; poc = b; } });
        var widest = plotW * 0.28;
        svg += '<g class="cw-vp">';
        vp.forEach(function (b) {
          if (!b.v) { return; }
          var len = b.v / vpMost * widest;
          var y1 = Y(b.hi), y2 = Y(b.lo);
          svg += '<rect class="cw-vp-bar' + (b === poc ? ' poc' : '') +
            '" x="' + (w - right - len).toFixed(1) + '" y="' +
            (y1 + 0.5).toFixed(1) + '" width="' + len.toFixed(1) +
            '" height="' + Math.max(1, y2 - y1 - 1).toFixed(1) +
            '" fill="' + (b === poc ? '#2f6fb0' : '#7fa7cf') +
            '" opacity="' + (b === poc ? '.45' : '.28') + '"/>';
        });
        var pocY = Y((poc.lo + poc.hi) / 2);
        svg += '<line class="cw-vp-poc" x1="' + left + '" x2="' +
          (w - right) + '" y1="' + pocY.toFixed(1) + '" y2="' +
          pocY.toFixed(1) + '" stroke="#2f6fb0" stroke-width="1" ' +
          'stroke-dasharray="2 3" opacity=".7"/>' +
          // ITS PRICE as a tag on the price axis - solid, like the live
          // price's tag on the right - where no candle can sit on it.
          (function () {
            var text = 'POC ' + ((poc.lo + poc.hi) / 2).toFixed(2);
            var tw = text.length * 6.2 + 8;
            return '<rect class="cw-vp-tag" x="1" y="' +
              (pocY - 8).toFixed(1) + '" width="' + tw.toFixed(1) +
              '" height="16" rx="2" fill="#2f6fb0"/>' +
              '<text x="5" y="' + (pocY + 4).toFixed(1) +
              '" font-size="11" font-weight="700" fill="#fff">' + text +
              '</text>';
          }()) + '</g>';
      }
    }
    lines.forEach(function (line) {
      svg += '<line class="' + (line.cls || '') + '" x1="' + left +
        '" x2="' + (w - right) + '" y1="' + Y(line.y).toFixed(1) +
        '" y2="' + Y(line.y).toFixed(1) + '" stroke="' + line.colour +
        '" stroke-width="1" stroke-dasharray="' + line.dash + '"/>';
    });
    curves.forEach(function (curve) {
      var path = '', pen = 'M';
      candles.forEach(function (c, i) {
        var v = c ? curve.value(c) : null;
        if (v === null || v === undefined || !isFinite(v)) {
          pen = 'M';
          return;
        }
        path += pen + X(i).toFixed(1) + ' ' + Y(v).toFixed(1) + ' ';
        pen = 'L';
      });
      if (path) {
        svg += '<path class="' + (curve.cls || '') + '" d="' + path +
          '" fill="none" stroke="' + curve.colour + '" stroke-width="' +
          (curve.width || 1) + '" stroke-dasharray="' + (curve.dash || '0') +
          '"/>';
      }
    });
    candles.forEach(function (c, i) {
      if (!c) { return; }
      var up = c.c >= c.o;
      var colour = up ? CHART.up : CHART.down;
      var x = X(i);
      var yTop = Y(Math.max(c.o, c.c)), yBot = Y(Math.min(c.o, c.c));
      svg += '<g class="cw-candle ' + (up ? 'up' : 'down') +
        (c.beyond ? ' beyond' : '') + '">' +
        '<line shape-rendering="crispEdges" x1="' + x.toFixed(1) +
        '" x2="' + x.toFixed(1) + '" y1="' +
        Y(c.h).toFixed(1) + '" y2="' + Y(c.l).toFixed(1) + '" stroke="' +
        colour + '" stroke-width="1"/>' +
        '<rect shape-rendering="crispEdges" x="' +
        (x - body / 2).toFixed(1) + '" y="' + yTop.toFixed(1) +
        '" width="' + body.toFixed(1) + '" height="' +
        Math.max(1, yBot - yTop).toFixed(1) + '" fill="' + colour +
        '" stroke="' + (c.beyond ? '#000' : colour) + '"/></g>';
    });
    // WHERE IT IS NOW: the last close, as a dotted line across and its
    // value in a tag at the right-hand end.
    var lastC = null;
    for (var li = candles.length - 1; li >= 0; li--) {
      if (candles[li]) { lastC = candles[li].c; break; }
    }
    if (opts.nowTag && lastC !== null && isFinite(lastC)) {
      var yl = Y(lastC);
      var tag = lastC.toFixed(Math.max(digits, opts.nowDigits || 0));
      var tw = tag.length * 6.6 + 8;
      // In the right margin, just past the plot: the newest candle and
      // its wick stay in view (the tag used to sit on top of them).
      var tx = w - right + 2;
      svg += '<line x1="' + left + '" x2="' + (w - right) + '" y1="' +
        yl.toFixed(1) + '" y2="' + yl.toFixed(1) + '" stroke="#212529" ' +
        'stroke-width="1" stroke-dasharray="1 3" opacity=".6"/>' +
        '<rect class="cw-now-tag" x="' + tx.toFixed(1) + '" y="' +
        (yl - 8).toFixed(1) + '" width="' + tw.toFixed(1) +
        '" height="16" rx="2" fill="#212529"/>' +
        '<text x="' + (tx + 4).toFixed(1) + '" y="' +
        (yl + 4).toFixed(1) + '" text-anchor="start" font-size="11" ' +
        'font-weight="700" fill="#fff">' + tag + '</text>';
    }
    // The time along the bottom: five labels evenly across the clock,
    // the day added where the chart spans more than one.
    if (times.length) {
      var withDay = t1 - t0 > 20 * 3600;
      // As many labels as fit: about one per 95 px with the day, 60
      // without, between two and five.
      var gap = withDay ? 95 : 60;
      var count = Math.max(2, Math.min(5, Math.floor(plotW / gap) + 1));
      var marks = [];
      for (var q = 0; q < count; q++) { marks.push(q / (count - 1)); }
      marks.forEach(function (f, k) {
        var anchor = k === 0 ? 'start' : k === count - 1 ? 'end'
          : 'middle';
        svg += '<text x="' + (left + f * plotW).toFixed(1) + '" y="' +
          (h - 4) + '" font-size="11" text-anchor="' + anchor + '" fill="' +
          CHART.axis + '">' + chartClock(t0 + f * (t1 - t0), withDay) +
          '</text>';
      });
    }
    return {svg: svg + '</svg>',
            geom: {left: left, right: right, top: top, bottom: bottom,
                   w: w, h: h, lo: lo, hi: hi, n: n, slot: slot,
                   t0: t0, t1: t1, seconds: seconds, times: times,
                   x: X}};
  }

  // -- the hover -------------------------------------------------------
  //
  // A crosshair across BOTH charts on the same candle, and a readout of
  // everything about it: its open, high, low and close, the band's
  // middle and every band line in price, sigma, and the z of each. The
  // price under the pointer is read off the axis it is over.

  function chartHover(node, key, plot, event) {
    var drawn = node._drawn;
    if (!drawn) { return; }
    var geom = drawn.geom[plot === 'z' ? 'z' : 's'];
    if (!geom) { return; }
    var box = node.querySelector(plot === 'z' ? '.cw-z' : '.cw-spread');
    var rect = box.getBoundingClientRect();
    var x = event.clientX - rect.left, y = event.clientY - rect.top;
    if (x < geom.left || x > geom.w - geom.right) {
      return chartUnhover(node);
    }
    // The candle under the pointer, by time: in a gap there is none.
    var at = geom.t0 + (x - geom.left) / (geom.w - geom.left -
      geom.right) * (geom.t1 - geom.t0);
    var i = -1;
    for (var k = 0; k < geom.times.length; k++) {
      if (geom.times[k] <= at && at < geom.times[k] + geom.seconds) {
        i = k;
        break;
      }
    }
    if (i < 0) { return chartUnhover(node); }
    var c = drawn.shown[i], z = drawn.zc[i];
    if (!c) { return chartUnhover(node); }
    // The vertical line, on both plots, through the candle.
    ['.cw-z', '.cw-spread'].forEach(function (selector) {
      var other = node.querySelector(selector);
      var g = drawn.geom[selector === '.cw-z' ? 'z' : 's'];
      var line = crossNode(other, 'cw-xline');
      if (!g) { line.hidden = true; return; }
      line.hidden = false;
      line.style.left = g.x(i) + 'px';
      line.style.top = g.top + 'px';
      line.style.height = (g.h - g.top - g.bottom) + 'px';
    });
    // The level under the pointer, on the plot it is over.
    var value = geom.hi - (y - geom.top) / (geom.h - geom.top -
      geom.bottom) * (geom.hi - geom.lo);
    node.querySelectorAll('.cw-yline, .cw-ylabel').forEach(function (el_) {
      el_.hidden = true;
    });
    if (y >= geom.top && y <= geom.h - geom.bottom) {
      var yline = crossNode(box, 'cw-yline');
      yline.hidden = false;
      yline.style.top = y + 'px';
      yline.style.left = geom.left + 'px';
      yline.style.width = (geom.w - geom.left - geom.right) + 'px';
      var ylabel = crossNode(box, 'cw-ylabel');
      ylabel.hidden = false;
      ylabel.style.top = (y - 8) + 'px';
      ylabel.textContent = plot === 'z'
        ? (value > 0 ? '+' : '') + value.toFixed(2) + 'σ'
        : value.toFixed(4);
    }
    var tip = crossNode(node.querySelector('.cw-body'), 'cw-tip');
    tip.hidden = false;
    tip.innerHTML = chartTipHtml(drawn, c, z);
    // Beside the pointer, flipped to the other side near the edge.
    var bodyRect = node.querySelector('.cw-body').getBoundingClientRect();
    var tx = event.clientX - bodyRect.left + 14;
    var ty = event.clientY - bodyRect.top + 14;
    if (tx + tip.offsetWidth > bodyRect.width - 4) {
      tx = event.clientX - bodyRect.left - tip.offsetWidth - 14;
    }
    if (ty + tip.offsetHeight > bodyRect.height - 4) {
      ty = Math.max(4, event.clientY - bodyRect.top - tip.offsetHeight - 14);
    }
    tip.style.left = Math.max(4, tx) + 'px';
    tip.style.top = ty + 'px';
    node._hover = {plot: plot, clientX: event.clientX,
                   clientY: event.clientY};
  }

  function chartUnhover(node) {
    node.querySelectorAll('.cw-xline, .cw-yline, .cw-ylabel, .cw-tip')
      .forEach(function (el_) { el_.hidden = true; });
    node._hover = null;
  }

  function crossNode(parent, cls) {
    var found = parent.querySelector(':scope > .' + cls);
    if (found) { return found; }
    var made = document.createElement('div');
    made.className = cls;
    made.hidden = true;
    parent.appendChild(made);
    return made;
  }

  function chartTipHtml(drawn, c, z) {
    /* Everything about one candle, in price and in sigma. */
    var entry = drawn.entry;
    function p(v) { return v === null || v === undefined ? DASH : fmt(v, 4); }
    function band(k) { return c.m === null || !c.sd ? null : c.m + k * c.sd; }
    var when = new Date(c.t * 1000);
    var head = when.toLocaleString([], {weekday: 'short', day: 'numeric',
                                        month: 'short', hour: '2-digit',
                                        minute: '2-digit'}) +
      ' · ' + drawn.label;
    // Each price with its SCORE: how many of the band's sigmas it
    // stands from the mean - the z, as a plain number.
    function score(v) {
      return v === null || v === undefined || !isFinite(v) ? ''
        : (v > 0 ? '+' : '') + v.toFixed(2);
    }
    var rows = [
      ['', 'Price', 'Score', 'cols'],
      ['Open', p(c.o), z ? score(z.o) : ''],
      ['High', p(c.h), z ? score(z.h) : ''],
      ['Low', p(c.l), z ? score(z.l) : ''],
      ['Close', p(c.c), z ? score(z.c) : '', c.c >= c.o ? 'up' : 'down'],
      ['Range', p(c.h - c.l), c.sd ? ((c.h - c.l) / c.sd).toFixed(2) : '']
    ];
    var bands = [];
    if (entry !== null && entry !== undefined) {
      bands.push(['+ entry', p(band(entry)), score(entry), 'entry-up']);
    }
    // Only what the trader acts on: the entry either side, and the mean.
    bands.push(['Mean', p(c.m), score(0) || '0.00', 'mean']);
    if (entry !== null && entry !== undefined) {
      bands.push(['\u2212 entry', p(band(-entry)), score(-entry),
                  'entry-down']);
    }
    function table(list) {
      return '<table>' + list.map(function (r) {
        return '<tr class="' + (r[3] || '') + '"><th>' + r[0] +
          '</th><td>' + r[1] + '</td><td class="z">' + (r[2] || '') +
          '</td></tr>';
      }).join('') + '</table>';
    }
    var verdict = '';
    if (z && entry !== null && entry !== undefined) {
      verdict = Math.abs(z.c) >= entry
        ? '<div class="beyond">closed beyond the entry band</div>'
        : Math.abs(z.h) >= entry || Math.abs(z.l) >= entry
          ? '<div class="touched">touched the entry band, closed inside</div>'
          : '';
    }
    if (!z) {
      verdict = '<div class="hint">no band yet — fewer than ' +
        drawn.length + ' candles behind it</div>';
    }
    // ONE table, so the band prices line up under the candle's.
    var html = table(rows.concat([['', '', '', 'sep']]).concat(bands));
    html = html.replace('<tr class="sep"><th></th><td></td>' +
                        '<td class="z"></td></tr>',
                        '<tr class="sep"><th colspan="3">Bands</th></tr>');
    return '<div class="head">' + escapeHtml(head) + '</div>' + html +
      verdict;
  }

  function drawChart(node, key) {
    var tf = chartTf(key);
    var id = key + '#' + tf;
    var hist = state.chartHist[id];
    var live = state.charts[key] || {};
    var zBox = node.querySelector('.cw-z');
    var sBox = node.querySelector('.cw-spread');
    // Only when the data or the room it is drawn in has changed.
    var view = chartView(key);
    var vpButton = node.querySelector('.cw-vp-toggle');
    if (vpButton) { vpButton.classList.toggle('on', chartVpOn()); }
    var stale = !!(hist && hist.ok && hist.at &&
                   Date.now() - hist.at * 1000 > CHART_STALE_MS);
    var stamp = [tf, view.shown, view.back, hist ? hist.at : '',
                 stale, state.chartHistNote[id] || '',
                 live.at || '',
                 (live.candles || []).length, zBox.clientWidth,
                 zBox.clientHeight, sBox.clientWidth,
                 sBox.clientHeight].join('|');
    if (node.dataset.drawn === stamp) { return; }
    node.dataset.drawn = stamp;
    var note = node.querySelector('.cw-note');
    var nowBox = node.querySelector('.cw-now');
    var label = tf === 15 ? '15-minute candles' : tf === 60
      ? '1-hour candles' : '4-hour candles';
    node.querySelector('.cw-size').textContent = label;
    var seconds = tf * 60;
    if (!hist || !hist.ok || !(hist.candles || []).length) {
      var why = state.chartHistNote[id];
      var waiting = '<div class="cw-empty">' + escapeHtml(why
        ? 'no candles: ' + why : 'reading OKX’s candles…') + '</div>';
      zBox.innerHTML = waiting;
      sBox.innerHTML = waiting;
      nowBox.textContent = '';
      note.textContent = '';
      node._drawn = null;
      node.querySelector('.cw-min').textContent = 'Min: —';
      node.querySelector('.cw-max').textContent = 'Max: —';
      return;
    }
    var length = hist.length || 20;
    var entry = hist.entry_z, stop = hist.stop_z;
    var all = withBands(mergeLive(hist.candles, live.candles, seconds,
                                  hist.anchor || 0, hist.at || 0), length);
    // The window the trader has zoomed and scrolled to: `shown`
    // candles, ending `back` candles before the latest.
    view.total = all.length;
    view.shown = Math.min(Math.max(view.shown, CHART_MIN_SHOWN),
                          Math.max(all.length, CHART_MIN_SHOWN));
    view.back = Math.max(0, Math.min(view.back, all.length - view.shown));
    var end = all.length - view.back;
    var shown = all.slice(Math.max(0, end - view.shown), end);
    var size = node.querySelector('.cw-size');
    size.textContent = label + ' \u00b7 ' +
      shown.length + ' shown' + (view.back ? ' \u00b7 ' + view.back +
      ' back from now' : '');
    // An out-of-date history, said in the window: OKX's candles stop at
    // `hist.at` and only the live ones carry on, so the gap between is
    // empty - and why the refresh failed, where the engine said.
    size.classList.toggle('cw-stale', stale);
    size.title = '';
    if (stale) {
      var why = state.chartHistNote[id];
      size.textContent += ' \u00b7 \u26a0 OKX history only to ' +
        chartClock(hist.at, true) + (why ? ' - ' + why
                                         : ' - refreshing');
      size.title = size.textContent;
    }
    // z of each candle against ITS OWN band: where it stood when it
    // was drawn, not against today's. Null where there is no band yet,
    // so the two charts keep the same slots.
    var zc = shown.map(function (c) {
      if (c.sd === null || c.m === null) { return null; }
      var z = {t: c.t, o: (c.o - c.m) / c.sd, h: (c.h - c.m) / c.sd,
               l: (c.l - c.m) / c.sd, c: (c.c - c.m) / c.sd};
      z.beyond = entry !== null && entry !== undefined &&
        Math.abs(z.c) >= entry;
      c.beyond = z.beyond;
      return z;
    });
    var times = shown.map(function (c) { return c.t; });
    var zLines = [{y: 0, colour: CHART.grid, dash: '0'}];
    if (entry !== null && entry !== undefined) {
      zLines.push({y: entry, colour: '#dc3545', dash: '5 5',
                   cls: 'cw-entry-up'});
      zLines.push({y: -entry, colour: '#198754', dash: '5 5',
                   cls: 'cw-entry-down'});
    }
    if (stop !== null && stop !== undefined) {
      zLines.push({y: stop, colour: CHART.stop, dash: '1 3',
                   cls: 'cw-stop'});
      zLines.push({y: -stop, colour: CHART.stop, dash: '1 3',
                   cls: 'cw-stop'});
    }
    node.querySelector('.cw-key-stop').hidden =
      stop === null || stop === undefined;
    var geom = {z: null, s: null};
    if (zc.some(function (z) { return z; })) {
      var zPlot = svgCandles(zBox, zc, {lines: zLines, digits: 0,
                                        nowTag: true, nowDigits: 2,
                                        ticks: [-4, -3, -2, -1, 0, 1, 2, 3,
                                                4], times: times,
                                        seconds: seconds});
      zBox.innerHTML = zPlot.svg;
      geom.z = zPlot.geom;
    } else {
      zBox.innerHTML = '<div class="cw-empty">no band yet — fewer ' +
        'than ' + length + ' candles</div>';
    }
    function band(k) {
      return function (c) {
        return c.m === null || c.sd === null ? null : c.m + k * c.sd;
      };
    }
    var curves = [{value: band(0), colour: CHART.mean, cls: 'cw-mean',
                   width: 1.5}];
    if (entry !== null && entry !== undefined) {
      curves.push({value: band(entry), colour: '#dc3545', dash: '5 5',
                   cls: 'cw-band-entry'});
      curves.push({value: band(-entry), colour: '#198754', dash: '5 5',
                   cls: 'cw-band-entry'});
    }
    var sPlot = svgCandles(sBox, shown, {curves: curves, digits: 4,
                                         nowTag: true, nowDigits: 2,
                                         times: times, seconds: seconds,
                                         profile: chartVpOn()});
    sBox.innerHTML = sPlot.svg;
    geom.s = sPlot.geom;
    node._drawn = {shown: shown, zc: zc, geom: geom, entry: entry,
                   length: length, label: label};
    // A redraw under a resting pointer keeps its readout, on the
    // candle now there.
    if (node._hover) {
      chartHover(node, key, node._hover.plot,
                 {clientX: node._hover.clientX,
                  clientY: node._hover.clientY});
    }
    var lows = shown.map(function (c) { return c.l; });
    var highs = shown.map(function (c) { return c.h; });
    node.querySelector('.cw-min').textContent = 'Min: ' +
      fmt(Math.min.apply(null, lows), 4);
    node.querySelector('.cw-max').textContent = 'Max: ' +
      fmt(Math.max.apply(null, highs), 4);
    var last = shown[shown.length - 1];
    var lastZ = zc[zc.length - 1] ? zc[zc.length - 1].c : null;
    nowBox.textContent = 'now: z ' + (lastZ === null ? '—'
      : (lastZ > 0 ? '+' : '') + lastZ.toFixed(2)) +
      ' · spread ' + fmt(last.c, 4) +
      (last.sd ? ' · 1σ ' + fmt(last.sd, 4) : '');
    var own = hist.algo_timeframe_min === tf;
    note.textContent = 'Hover a candle for its prices, the mean and the entry bands. ' +
      'Bands: EMA(' + length + ') middle, the entry ±' + fmt(entry, 2) + 'σ dashed — the ' +
      'Algo’s own arithmetic' +
      (own ? ', on its own ' + tf + '-minute candles: these are its bands.'
        : '; the Algo itself reads ' + hist.algo_timeframe_min +
          '-minute candles.') +
      ' Outlined: closed beyond the entry band. Candles from OKX’s ' +
      'bars, the newest moving live. Wheel to zoom, drag to scroll back, ' +
      'double-click for now.';
    // One line on the window, all of it on hover: in a small window the
    // charts keep the room.
    note.title = note.textContent;
  }

  function wireLadder(node, key) {
    node.querySelector('.close').addEventListener('click', function () {
      closePanel(panelId('ladder', key));
    });
    // The chart: one click opens it, the next closes it.
    node.querySelector('.chart-btn').addEventListener('click', function (e) {
      e.stopPropagation();
      toggleChart(key);
    });
    node.querySelector('.calc-btn').addEventListener('click', function (e) {
      e.stopPropagation();
      var id = panelId('calc', key);
      if (state.open.indexOf(id) >= 0) { closePanel(id); }
      else { openPanel(id); }
    });
    wireAlgoSwitch(node, key);
    node.querySelector('.order-type').addEventListener('change', function (e) {
      setPair(key, {order_type: e.target.value});
    });
    node.querySelector('.tif').addEventListener('change', function (e) {
      setPair(key, {time_in_force: e.target.value});
    });
    node.querySelector('.increment').addEventListener('change', function (e) {
      setPair(key, {increment: parseFloat(e.target.value)});
    });
    // "Where do I change these?" — answered by the panel showing the
    // numbers, and answered with THIS ladder's own settings: the next
    // ladder is a different instrument with different costs.
    node.querySelectorAll('.open-exits, .ladder-cog').forEach(function (cog) {
      cog.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        openLadderSettings(node, key);
      });
    });
    node.querySelector('.ls-close').addEventListener('click', function () {
      node.querySelector('.ladder-settings').hidden = true;
    });
    node.querySelector('.ls-save').addEventListener('click', function () {
      saveLadderSettings(node, key);
    });
    // The pair type decides which legs have an expiry, so the form
    // follows it as it is changed.
    ['input', 'change'].forEach(function (event) {
      node.querySelector('.ladder-settings').addEventListener(
        event, function (e) {
          /* THIS CONTROL NOW BELONGS TO THE OPERATOR.
           *
           * The pane is shown at once and FILLED when two reads come
           * back, and that fill used to write over every field
           * unconditionally. An operator quick enough to change
           * something in between had it silently reverted - and the
           * form then saved the OLD value, because the box no longer
           * said what they had chosen. On a desk running four
           * terminals that gap is not small.
           *
           * Marked here, honoured in the fill. Both events, because a
           * checkbox reports `change` where a select reports both.
           */
          if (e.target && e.target.dataset) {
            e.target.dataset.touched = '1';
          }
          // Which legs have an expiry follows the pair type, and it
          // must follow it as it is CHANGED — not only when the pane
          // opens.
          if (e.target.classList.contains('ls-pair-type')) {
            var pane = node.querySelector('.ladder-settings');
            fairKindFields(pane, {pair_type: e.target.value});
          }
        });
    });

    node.querySelector('.lock-scroll').addEventListener('change', function (e) {
      setLocked(key, e.target.checked);
    });
    node.querySelector('.recentre').addEventListener('click', function () {
      // Both halves: the ENGINE re-anchors the price window (the rows
      // themselves), and the browser scrolls to the mid. Doing only the
      // second would scroll to a market that had walked out of the
      // window the engine is still building.
      send('recentre_ladder', {pair: key});
      state.scrolledAt[key] = 0;
      state.busyAt[key] = 0;
      state.hovering[key] = false;
      state.centredAt[key] = 0;
      var market = (state.snapshot.pairs[key] || {}).market || {};
      if (market.spread !== undefined) { centreOnMid(node, key, market); }
    });
    node.querySelector('.filter').addEventListener('change', function (e) {
      state.filtered[key] = e.target.checked;
      render();
    });
    node.querySelector('.armed').addEventListener('input', function (e) {
      // Typed sizes are armed the moment they are typed: no Enter, no
      // second gesture. Blank goes back to the ladder's default rather
      // than arming zero, which would be a click that sends nothing.
      setArmed(key, 'buy', e.target.value);
      if (state.armedUsd) { delete state.armedUsd[key]; }
      // Redraw THIS ladder as it is typed, so the button beside the box
      // carries the size before the next poll. The box being typed into
      // is never written back over — it has focus.
      if (state.snapshot.pairs[key]) {
        renderLadder(key, state.snapshot.pairs[key]);
      }
    });
    node.querySelector('.sell-qty').addEventListener('input', function (e) {
      setArmed(key, 'sell', e.target.value);
      if (state.armedUsd) { delete state.armedUsd[key]; }
      if (state.snapshot.pairs[key]) {
        renderLadder(key, state.snapshot.pairs[key]);
      }
    });
    node.querySelector('.keypad').addEventListener('click', function (e) {
      var button = e.target.closest('.qty');
      if (!button) { return; }
      // The keypad arms BOTH sides: they are the same size in almost
      // every case, and the two boxes are there for the exception.
      var size = button.dataset.qty || '';
      setArmed(key, 'buy', size);
      setArmed(key, 'sell', size);
      if (state.armedUsd) { delete state.armedUsd[key]; }
      render();
    });
    // MARGIN TO QTY: the dollars are the TOTAL MARGIN the trade ties
    // up, both legs together - what one Qty ties up now (live prices,
    // OKX's leverage) into the amount, rounded DOWN so a click never
    // ties up more than it was given.
    function sizeInDollars(dollars) {
      var row = state.snapshot.pairs[key] || {};
      var per = qtyMargin(row);
      var box = node.querySelector('.usd-qty');
      if (!per) {
        toast('The margin for one Qty is not known yet (no live price, or ' +
              'OKX\u2019s leverage not read) - size in Qty for now.');
        return;
      }
      var qty = Math.floor(dollars / per * 100 + 1e-9) / 100;
      if (!(qty >= 0.01)) {
        toast('$' + Number(dollars).toLocaleString() + ' of margin is under ' +
              'one order step here (1 Qty ties up $' + per.toFixed(2) + ').');
        return;
      }
      setArmed(key, 'buy', qty);
      setArmed(key, 'sell', qty);
      state.armedUsd = state.armedUsd || {};
      state.armedUsd[key] = dollars;
      if (box) {
        box.textContent = '= Qty ' + qty.toFixed(2);
      }
      render();
    }
    node.querySelector('.usd-keypad').addEventListener('click', function (e) {
      var button = e.target.closest('.usd');
      if (!button) { return; }
      sizeInDollars(parseFloat(button.dataset.usd));
    });
    node.querySelector('.usd-box').addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') { return; }
      var dollars = parseFloat(e.target.value);
      if (isFinite(dollars) && dollars > 0) { sizeInDollars(dollars); }
    });
    node.querySelector('.buy-touch').addEventListener('click', function () {
      atTouch(key, 'BUY');
    });
    node.querySelector('.sell-touch').addEventListener('click', function () {
      atTouch(key, 'SELL');
    });
    node.querySelector('.flatten').addEventListener('click', function () {
      flatten(key);
    });
    node.querySelector('.close-limit-go').addEventListener('click', function () {
      closeAtLimit(key, node.querySelector('.close-limit').value);
    });
    node.querySelector('.close-limit').addEventListener('keydown',
      function (e) {
        if (e.key === 'Enter') { closeAtLimit(key, e.target.value); }
      });
    node.querySelector('.refresh-feed').addEventListener('click', function () {
      // Re-subscribe both legs. The answer to "it is moving in MT5 and
      // stale here" that the trader can act on themselves.
      send('refresh_feed', {pair: key}, function (result) {
        var data = (result || {}).data || {};
        if (data.reason) { toast(data.reason, data.ok ? 'ok' : ''); }
      });
    });
    node.querySelector('.cxl-b').addEventListener('click', function () {
      send('cancel_where', {pair: key, side: 'BUY'});
    });
    node.querySelector('.cxl-s').addEventListener('click', function () {
      send('cancel_where', {pair: key, side: 'SELL'});
    });
    node.querySelector('.cxl-all').addEventListener('click', function () {
      send('cancel_where', {pair: key});
    });
    node.querySelector('.grid').addEventListener('scroll', function () {
      // A scroll we did not make is the trader looking somewhere:
      // leave the window where they put it (SCROLL_GRACE_MS).
      if (state.centring[key]) { return; }
      state.scrolledAt[key] = Date.now();
    });
    // Any of these means hands on: hold the ladder still (ladderIsBusy).
    var grid = node.querySelector('.grid');
    grid.addEventListener('pointerenter', function () {
      state.hovering[key] = true;
    });
    grid.addEventListener('pointerleave', function () {
      state.hovering[key] = false;
      state.busyAt[key] = Date.now();          // start the grace period
    });
    grid.addEventListener('pointerdown', function () {
      state.busyAt[key] = Date.now();
    });
    grid.addEventListener('pointermove', function () {
      state.busyAt[key] = Date.now();
    });
    node.querySelector('.grid tbody').addEventListener('click', function (e) {
      var cell = e.target.closest('td');
      if (!cell) { return; }
      var row = cell.closest('tr');
      var level = parseFloat(row.dataset.level);
      if (cell.classList.contains('ask')) {
        clickLevel(key, sideForColumn('ask'), level);
      } else if (cell.classList.contains('bid')) {
        clickLevel(key, sideForColumn('bid'), level);
      } else if (cell.classList.contains('work')) {
        // ADD one more at this level, on the side already resting
        // there. Clicking your own working order to DESTROY it is the
        // wrong default on a ladder: the ordinary thing a trader does
        // to a level they are working is want more of it, and pulling
        // has three other ways to happen (right-click here, the S / B
        // / CXL All buttons, and Cancel in the Working orders panel).
        if (!cell.dataset.side) { return; }
        clickLevel(key, cell.dataset.side, level);
      }
    });
    node.querySelector('.grid tbody').addEventListener(
      'contextmenu', function (e) {
        var cell = e.target.closest('td.work');
        if (!cell || !cell.dataset.orderId) { return; }
        e.preventDefault();
        send('cancel_order', {order_id: cell.dataset.orderId});
      });
  }

  function kDollars(x) {
    /* $25K, $2.5K, $850 - a dollar size as the keypad names it. */
    var n = Math.abs(x);
    if (n >= 1000) {
      var k = n / 1000;
      return '$' + (k >= 100 ? Math.round(k) : Math.round(k * 10) / 10) + 'K';
    }
    return '$' + Math.round(n);
  }

  function qtyMargin(row) {
    /* What ONE Qty ties up at OKX, both legs: the engine's own priced
     * figure, else each leg's value over its leverage. Null when
     * neither is known. */
    var priced = ((row || {}).exit || {}).margin_per_spread;
    if (priced) { return priced; }
    var legs = (row || {}).leg_notional || {};
    var la = (row || {}).leverage_a, lb = (row || {}).leverage_b;
    if (!la || !lb || !legs.a || !legs.b) { return null; }
    return legs.a / la + legs.b / lb;
  }

  function qtyDollars(row) {
    /* What ONE Qty of this ladder is worth now: both legs' lots x
     * contract x mid. Null without a price. */
    var market = (row || {}).market || {};
    function mid(l) {
      var b = market['leg_' + l + '_bid'], a = market['leg_' + l + '_ask'];
      return b && a ? (b + a) / 2 : null;
    }
    var ma = mid('a'), mb = mid('b');
    if (!ma || !mb || !row.contract_a || !row.contract_b) { return null; }
    return (row.clip_lots_a || 1) * row.contract_a * ma +
      (row.clip_lots_b || 1) * row.contract_b * mb;
  }

  function setArmed(key, side, value) {
    var size = parseFloat(value);
    var armed = state.armed[key] || (state.armed[key] = {});
    // Blank goes back to the ladder's default rather than arming zero,
    // which would be a click that sends nothing.
    armed[side] = (isFinite(size) && size > 0) ? size : null;
  }

  function armedFor(key, side) {
    var armed = state.armed[key] || {};
    return armed[String(side).toLowerCase()] || null;
  }

  function restingOn(quote) {
    /* WHERE the real pending is. In LIMIT mode one leg rests a pending
     * and the OTHER is crossed at market when it fills — so leg A
     * having no order at the broker is not a fault, it is the mode.
     * Said in words, because "leg a" on one panel was the only place
     * this appeared and it read as leg A having nothing. */
    if (!quote) { return ''; }
    if (quote.held) {
      // A working order held HERE by design: nothing at the broker
      // until the spread reaches it, then both legs cross together.
      return ', held by the desk - both legs cross at market when the ' +
        'spread reaches it' + (quote.reason ? ' (' + quote.reason + ')' : '') +
        flickerWords(quote);
    }
    if (!quote.ticket) {
      return ', not at the broker yet' +
        (quote.reason ? ' (' + quote.reason + ')' : '');
    }
    var leg = String(quote.leg || '').toUpperCase();
    return ', resting on leg ' + leg +
      (quote.symbol ? ' (' + quote.symbol + ')' : '') +
      ' at ' + fmt(quote.price, 5) + ' #' + quote.ticket +
      (quote.crosses_leg
        ? ' — leg ' + quote.crosses_leg + ' crosses at market when it fills'
        : '');
  }

  function flickerWords(quote) {
    /* The re-check before sending: how many times the spread only
     * flickered to this level and the order was NOT sent. */
    var n = (quote && quote.rechecks_missed) || 0;
    return n ? ' · ' + n + ' flicker' + (n > 1 ? 's' : '') +
      ' to the level not traded' : '';
  }

  function sideForColumn(column) {
    /* Which SIDE a click in the bids or asks column sends.
     *
     * TT (default): the price-ladder convention every desk arrives
     * with — clicking BIDS joins the bid, which is a resting BUY, and
     * clicking ASKS joins the offer, which is a resting SELL.
     * TOUCH: the hit/lift reading — clicking ASKS lifts the offer.
     *
     * This is the ONLY place the mapping exists. It changes nothing
     * else: the level comes from the row that was clicked, and the
     * side reaches the engine already decided, so sizing, hedging and
     * execution are identical either way. The BUY and SELL buttons and
     * the B/S keys name their side outright and never come through
     * here.
     */
    // Absent means TT, matching the engine's own default, the
    // snapshot's fallback and the Settings pane. This line said TOUCH
    // while all three of those said TT, so a snapshot arriving without
    // the key produced a ladder that CROSSED THE OPPOSITE WAY to the
    // convention its own Settings pane was displaying.
    //
    // The fallback's job is not to be timid, it is to AGREE. A default
    // that differs from the one the rest of the system uses is not
    // conservative - it is a second convention nobody chose, reachable
    // whenever a snapshot is missing the field.
    var tt = state.snapshot.click_convention !== 'TOUCH';
    if (column === 'ask') { return tt ? 'SELL' : 'BUY'; }
    return tt ? 'BUY' : 'SELL';
  }

  function clickHint(column, level) {
    /* What this cell will actually DO, in the words of the convention
     * in force. A tooltip that says BUY while the click sends SELL is
     * worse than no tooltip at all, so it is built from the same
     * mapping the click uses rather than written out twice. */
    var side = sideForColumn(column);
    var legs = side === 'BUY' ? 'buy leg B, sell leg A'
                              : 'sell leg B, buy leg A';
    return 'Click: ' + side + ' the spread at ' + fmt(level, 4) +
      ' (' + legs + ')';
  }

  function algoTrades(row) {
    /* This ladder's Algo is LIVE: it, and only it, places orders here. */
    return !!(row && row.algo_on && row.algo_mode === 'LIVE');
  }

  function clickLevel(key, side, level) {
    var pair = state.snapshot.pairs[key] || {};
    // Every manual order on the screen comes through here — the grid,
    // BUY/SELL, the B and S keys and the Market Grid. The ENGINE refuses
    // it too; this only saves sending what will be refused.
    if (algoTrades(pair)) {
      toast((pair.name || key) + ': the Algo is trading this ladder ' +
            '(LIVE) — manual orders are off. CLOSE ALL still closes; ' +
            'turn the Algo off in the title bar to trade by hand.');
      return;
    }
    var armed = armedFor(key, side);
    var quantity = armed || pair.default_quantity;
    var payload = {pair: key, side: side, level: level};
    if (armed) { payload.quantity = armed; }

    // ONE CLICK IS ONE ORDER. A market click crosses both accounts
    // immediately — that is the product. The arming is what carries the
    // weight instead: the mode badge, the tinted columns, the cursor.
    // A desk that wants the extra gesture turns CONFIRM_MARKET_CLICKS on.
    if (pair.order_type === 'MARKET' && state.snapshot.confirm_market_clicks) {
      ask('Cross both legs now?',
          key + '\n' + side + ' ' + quantity + ' spread(s) at ' +
          fmt(level, 4) + '.\n\n' +
          'MARKET crosses both legs immediately. The clicked price is the ' +
          'slippage guard: a fill worse than it by more than the ' +
          'protection is refused.',
          'Send it', function () { fire(key, side, level, payload); });
      return;
    }
    fire(key, side, level, payload);
  }

  function fire(key, side, level, payload) {
    var pair = state.snapshot.pairs[key] || {};
    var ghost = {
      pair_key: key, side: side, level: level,
      quantity: payload.quantity || pair.default_quantity,
      market: pair.order_type === 'MARKET', at: Date.now()
    };
    state.pending.push(ghost);
    flash(key, level, side);
    render();
    send('click', payload, function (result) {
      ghost.done = true;
      if (result && result.ok === false) { ghost.failed = true; }
      render();
    });
  }

  function flash(key, level, side) {
    // The row lights up under the finger, before any round trip. It is
    // the difference between "did that register?" and knowing it did.
    var selector = '.ladder[data-pair="' + cssEscape(key) + '"] tr[data-level="'
      + level + '"]';
    var row = document.querySelector(selector);
    if (!row) { return; }
    row.classList.add(side === 'BUY' ? 'flash-buy' : 'flash-sell');
    window.setTimeout(function () {
      row.classList.remove('flash-buy');
      row.classList.remove('flash-sell');
    }, 350);
  }

  function prunePending() {
    var now = Date.now();
    state.pending = state.pending.filter(function (ghost) {
      // A ghost lives until the engine's own answer arrives, and never
      // more than a second: a stale ghost is a phantom order.
      if (ghost.failed) { return false; }
      if (ghost.done && now - ghost.at > 400) { return false; }
      return now - ghost.at < 1500;
    });
  }

  function pendingAt(key, level, increment) {
    var half = (increment || 0.0001) / 2;
    return state.pending.filter(function (ghost) {
      return ghost.pair_key === key && !ghost.failed &&
        Math.abs(ghost.level - level) < half;
    });
  }

  function setPair(key, fields) {
    send('set_pair', {pair: key, fields: fields});
  }

  //: The overnight rule in the trader's own words, for the read-only
  //: line on the Exit panel.
  var OVERNIGHT_WORDS = {
    ALLOW: 'hold', EXIT_IF_PROFIT: 'exit if profit', EXIT_ALWAYS: 'exit'
  };

  //: Every field in this ladder's settings: the class on the control,
  //: the name the engine and the config both know it by, and how to
  //: read it back. One list, because a form that saves a field it does
  //: not load — or loads one it cannot save — is a form that silently
  //: drops what was typed into it.
  //: Every field in this ladder's settings: the control, the name the
  //: engine and the config both know it by, how to read it, and — for
  //: the ones with a system-wide default — which setting that is.
  //:
  //: Three kinds, and the difference is the whole point of the form:
  //:
  //:   live    what the ladder is RUNNING. Never blank: a blank Mode
  //:           is not "no mode", it is a form that cannot tell you
  //:           what the ladder is doing — and one that would send
  //:           `order_type: null` to an engine that raises on it.
  //:   number  a number this ladder OWNS. There is no defaults page:
  //:           every ladder carries its own, so the field shows a
  //:           real value and saving writes it to this pair. A pair
  //:           that has never been configured starts from the
  //:           built-in value, shown plainly — nothing inherits, and
  //:           nothing is hidden behind a word.
  //:   text    a fact about this pair with no default at all — an
  //:           expiry, a swap. Blank is a real state here.
  var LADDER_FIELDS = [
    ['.ls-order-type', 'order_type', 'live'],
    ['.ls-exit-type', 'exit_type', 'live'],
    ['.ls-tif', 'time_in_force', 'live'],
    ['.ls-overnight', 'overnight', 'live'],
    ['.ls-increment', 'increment', 'live-number'],
    // NOT 'rows' on the snapshot — that is the ladder's PRICE ROWS,
    // an array of forty objects. A number input handed one shows
    // blank, which is how this field was empty on every ladder.
    ['.ls-rows', 'row_count', 'live-number'],
    ['.ls-qty', 'default_quantity', 'live-number'],
    // What ONE spread means in leg A lots. Blank is a real state and
    // the useful default: it means "the smallest size both legs can
    // actually clear", which is what the engine derives. Leg B is
    // never typed — it is the hedge of this one.
    // LIVE, never blank. A blank box now MEANS 1, so leaving it empty
    // while the engine runs 0.10 would read as "derived" and send 1 on
    // the next Apply — a ten-fold resize nobody typed.
    ['.ls-usd', 'usd_per_qty', 'blank-number'],
    ['.ls-clip-a', 'clip_lots_a', 'live-number'],
    ['.ls-clip-b', 'clip_lots_b', 'live-number'],
    // Blank is a REAL state here and the normal one: it means MT5's.
    ['.ls-contract-a', 'contract_size_a', 'blank-number'],
    ['.ls-contract-b', 'contract_size_b', 'blank-number'],
    ['.ls-quoting', 'quoting_leg', 'live'],
    ['.ls-fill', 'fill_style', 'live'],
    // What the window reads: None, Fair spread or Algo. ONE control
    // for one decision. Fair spread is the saved `algo_window`; Algo is
    // a switch on the running engine, never saved, off after a restart.
    ['.ls-algo', 'algo_window', 'algo-choice'],
    // Blank is the normal state on OKX: the commission is priced from
    // the account's own VIP fees, shown in the box as a hint. Only a
    // number typed here overrides it.
    ['.ls-comm-a', 'commission_per_lot_a', 'blank-number',
     'COMMISSION_PER_LOT_A', 'vip'],
    ['.ls-comm-b', 'commission_per_lot_b', 'blank-number',
     'COMMISSION_PER_LOT_B', 'vip'],
    // Blank is a REAL state: it means the desk-wide threshold.
    ['.ls-stale', 'max_quote_age_sec', 'blank-number'],
    ['.ls-maxpos', 'max_positions', 'blank-number'],
    ['.ls-slip', 'slippage_allowance', 'number', 'SLIPPAGE_ALLOWANCE'],
    ['.ls-nights', 'break_even_nights', 'number', 'BREAK_EVEN_NIGHTS'],
    ['.ls-tp', 'tp_target_pct_of_margin', 'number', 'TP_TARGET_PCT_OF_MARGIN'],
    // Blank here is a real state: no second estimate, so no
    // cross-check. It is the one number with nothing to fall back to.
    ['.ls-carry-rate', 'carry_rate_pct', 'blank-number', 'CARRY_RATE_PCT'],
    ['.ls-pair-type', 'pair_type', 'live'],
    ['.ls-expiry-a', 'expiry_a', 'text'],
    ['.ls-expiry', 'expiry', 'text'],
    ['.ls-swap-a-long', 'swap_a_long_per_lot', 'text'],
    ['.ls-swap-a-short', 'swap_a_short_per_lot', 'text'],
    ['.ls-swap-b-long', 'swap_b_long_per_lot', 'text'],
    ['.ls-swap-b-short', 'swap_b_short_per_lot', 'text'],
    // This ladder's hours, HH:MM broker time. Blank is a real state:
    // the desk-wide cutoff, any time before it, no break.
    ['.ls-session-open', 'session_open', 'text'],
    ['.ls-session-close', 'session_close', 'text'],
    ['.ls-break-start', 'break_start', 'text'],
    ['.ls-break-end', 'break_end', 'text']

  ];

  //: The Algo's own numbers: the control, the name inside the pair's
  //: `algo_params`, and how to read it. Blank means the default, which
  //: the box shows — the engine's effective value, from the snapshot.
  var ALGO_FIELDS = [
    ['.ls-az-entry', 'entry_z', 'number'],
    ['.ls-az-direction', 'direction', 'text'],
    ['.ls-az-tf', 'timeframe_min', 'number'],
    ['.ls-az-length', 'length', 'number'],
    ['.ls-az-confirm', 'confirm_ticks', 'number'],
    ['.ls-az-max', 'max_entry_z', 'number'],
    ['.ls-az-buffer', 'cutoff_buffer_min', 'number'],
    ['.ls-az-cooldown', 'cooldown_min', 'number'],
    ['.ls-az-warmup', 'warmup_min', 'number'],
    ['.ls-az-sl-on', 'stop_loss_on', 'check'],
    ['.ls-az-sl', 'stop_loss_pct', 'number'],
    ['.ls-az-stop-mode', 'stop_mode', 'text'],
    ['.ls-az-target-mode', 'target_mode', 'text'],
    ['.ls-az-atr-period', 'atr_period', 'number'],
    ['.ls-az-atr-stop', 'atr_stop_mult', 'number'],
    // Each side's own exits: blank is the ladder's own value.
    ['.ls-az-stop-mode-sell', 'stop_mode_sell', 'text'],
    ['.ls-az-stop-mode-buy', 'stop_mode_buy', 'text'],
    ['.ls-az-target-mode-sell', 'target_mode_sell', 'text'],
    ['.ls-az-target-mode-buy', 'target_mode_buy', 'text'],
    ['.ls-az-sl-sell', 'stop_loss_pct_sell', 'number'],
    ['.ls-az-sl-buy', 'stop_loss_pct_buy', 'number'],
    ['.ls-az-tp-sell', 'tp_pct_sell', 'number'],
    ['.ls-az-tp-buy', 'tp_pct_buy', 'number'],
    ['.ls-az-atr-stop-sell', 'atr_stop_mult_sell', 'number'],
    ['.ls-az-atr-stop-buy', 'atr_stop_mult_buy', 'number'],
    ['.ls-az-atr-target-sell', 'atr_target_mult_sell', 'number'],
    ['.ls-az-atr-target-buy', 'atr_target_mult_buy', 'number'],
    ['.ls-az-atr-target', 'atr_target_mult', 'number'],
    ['.ls-az-progress', 'progress_bar', 'check'],
    ['.ls-az-qty', 'algo_qty', 'number'],
    ['.ls-az-size-by', 'algo_size_by', 'text'],
    ['.ls-az-margin', 'algo_margin', 'number'],
    ['.ls-az-maxtrades', 'max_trades_day', 'number'],
    ['.ls-az-loss', 'daily_loss_limit', 'number'],
    ['.ls-az-losses', 'max_losses_row', 'number'],
    ['.ls-az-edge-on', 'edge_on', 'check'],
    ['.ls-az-edge', 'edge_multiple', 'number'],
    ['.ls-az-capture', 'edge_capture_frac', 'number'],
    ['.ls-az-regime-on', 'regime_on', 'check'],
    ['.ls-az-reentry-on', 'reentry_on', 'check'],
    ['.ls-az-reentry', 'reentry_back', 'number'],
    ['.ls-az-reentry-window', 'reentry_window_pct', 'number'],
    ['.ls-az-trend-on', 'trend_on', 'check'],
    ['.ls-az-trend', 'trend_sigma', 'number'],
    ['.ls-az-trend-look', 'trend_lookback_min', 'number'],
    ['.ls-az-hl-min', 'half_life_min_min', 'number'],
    ['.ls-az-hl-max', 'half_life_max_min', 'number'],
    ['.ls-az-stop-on', 'stop_z_on', 'check'],
    ['.ls-az-stop', 'stop_z', 'number'],
    ['.ls-az-revert-on', 'reversion_on', 'check'],
    ['.ls-az-add-on', 'add_on', 'check'],
    ['.ls-az-add-sigma', 'add_sigma', 'number'],
    ['.ls-az-max-adds', 'max_adds', 'number'],
    ['.ls-az-add-size-by', 'add_size_by', 'text'],
    ['.ls-az-add-qty', 'add_qty', 'number'],
    ['.ls-az-add-margin', 'add_margin', 'number'],
    ['.ls-az-time-on', 'time_stop_on', 'check'],
    ['.ls-az-time', 'time_stop_candles', 'number']
  ];

  function algoChoiceOf(row) {
    /* What the ladder's window reads NOW, as the dropdown names it. */
    if (row && row.algo_on) {
      return row.algo_mode === 'LIVE' ? 'ALGO_LIVE' : 'ALGO';
    }
    return row && row.algo_window ? 'FAIR_SPREAD' : 'NONE';
  }

  function warmupHtml(warmup) {
    /* The live warm-up: minutes of live prices watched since the Algo
     * was turned on, against the minutes it needs. No entry until full —
     * a band loaded from MT5's history in a second is not a feed that
     * has been watched. */
    if (!warmup || !warmup.need_sec) { return ''; }
    var done = !!warmup.done;
    var pct = done ? 100 : Math.floor(100 * warmup.sec / warmup.need_sec);
    return '<div class="aw-data aw-warmup" title="No entry until the Algo '
      + 'has watched this long of live prices. Time with no price does not '
      + 'count.">'
      + '<span>Live</span><div class="aw-bar"><div class="'
      + (done ? 'ok' : 'wait') + '" style="width:' + pct + '%"></div></div>'
      + '<span>' + (done ? 'warmed up'
        : 'warming up ' + Math.floor(warmup.sec / 60) + '/'
          + Math.round(warmup.need_sec / 60) + ' min') + '</span></div>';
  }

  function renderAlgoSwitch(node, key, row) {
    /* The ladder's Algo switch: OFF / DRY RUN / LIVE, in the title bar.
     * While LIVE a click on this ladder is refused, and the trader must
     * be able to see why without opening anything. */
    var button = node.querySelector('.algo-btn');
    if (!button) { return; }
    var live = !!(row.algo_on && row.algo_mode === 'LIVE');
    var dry = !!(row.algo_on && !live);
    button.textContent = live ? 'ALGO LIVE'
      : (dry ? 'ALGO SIGNALS' : 'ALGO OFF');
    button.className = 'algo-btn ' + (live ? 'live' : (dry ? 'dry' : 'off'));
    button.title = live
      ? 'The Algo is TRADING this ladder — manual orders here are off; '
        + 'CLOSE ALL still closes. Click to change.'
      : dry ? 'Signals: the Algo signals and watches your position\u2019s '
          + 'TP and SL; you trade on this ladder, nothing is sent. '
          + 'Click to change.'
        : 'The Algo is off on this ladder. Click to turn it on.';
  }

  function wireAlgoSwitch(node, key) {
    var button = node.querySelector('.algo-btn');
    var menu = node.querySelector('.algo-menu');
    if (!button || !menu) { return; }
    button.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      menu.hidden = !menu.hidden;
    });
    menu.querySelectorAll('button[data-algo]').forEach(function (item) {
      item.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        menu.hidden = true;
        var choice = item.dataset.algo;
        var row = (state.snapshot.pairs || {})[key] || {};
        if (choice === algoChoiceOf(row)) { return; }   // already there
        // Off goes back to whatever the window tick says, never to a
        // choice the trader did not make.
        if (choice === 'NONE' && row.algo_window) { choice = 'FAIR_SPREAD'; }
        setAlgo(key, choice);
      });
    });
    document.addEventListener('click', function () { menu.hidden = true; });
  }

  function setAlgo(key, choice, extra) {
    /* One ladder's Algo: NONE / FAIR_SPREAD / ALGO (dry run) /
     * ALGO_LIVE. LIVE is confirmed here, every time — it sends real
     * orders and takes the ladder from the trader. The engine refuses
     * anything it cannot do in its own words, and when the Algo is
     * leaving LIVE holding a position it asks what to do with it. */
    var live = choice === 'ALGO_LIVE';
    var payload = Object.assign({pair: key,
                                 algo: live ? 'ALGO' : choice,
                                 mode: live ? 'LIVE' : 'DRY_RUN'},
                                extra || {});
    if (live && !payload.confirmed) {
      ask('Algo LIVE on ' + key + '?',
          'The Algo will send REAL orders to both accounts on this ladder: '
          + 'MARKET in when the spread stretches past its band, out at the '
          + 'take-profit or the stop loss. Manual orders on this ladder are '
          + 'OFF while it runs; CLOSE ALL and the positions list still close.',
          'Go LIVE', function () {
            setAlgo(key, choice, Object.assign({}, extra, {confirmed: true}));
          });
      return;
    }
    send('set_algo', payload, function (result) {
      var data = (result && result.data) || {};
      if (data.choose) {
        var count = (data.positions || []).length || 1;
        ask('The Algo holds a position on ' + key,
            'It holds ' + count + ' position(s) on this ladder. Close '
            + (count > 1 ? 'them' : 'it') + ' now at market, by ticket — '
            + 'or leave ' + (count > 1 ? 'them' : 'it') + ' open and '
            + 'manage by hand.',
            'Close it now', function () {
              setAlgo(key, choice, Object.assign({}, extra,
                                                 {off_action: 'close'}));
            },
            {label: 'Hand it to manual', run: function () {
              setAlgo(key, choice, Object.assign({}, extra,
                                                 {off_action: 'manual'}));
            }});
      }
    });
  }

  function openLadderSettings(node, key) {
    /* THIS ladder's settings, showing what is ACTUALLY IN FORCE.
     *
     * Three sources, because there are three kinds of field. What the
     * ladder is RUNNING comes from the snapshot — it is the only place
     * that knows, and a form that shows Mode blank while the title bar
     * says LIMIT is a form nobody can trust. A number with a
     * A cost or a target is this pair's OWN number: there is no
     * defaults page, so the box shows a real value — seeded from the
     * built-in one for a pair nobody has configured yet — and saving
     * writes it to this pair. And a fact about this pair alone — an
     * expiry, a swap — comes from the config, where blank is a real
     * state.
     */
    var pane = node.querySelector('.ladder-settings');
    if (!pane) { return; }
    pane.hidden = false;
    node.querySelector('.ls-pair').textContent = key;
    // A FRESH OPEN OWES NOTHING TO THE LAST ONE. Cleared here, before
    // the reads go out, so that only an edit made during THIS open can
    // hold off the fill below.
    Array.prototype.forEach.call(
      pane.querySelectorAll('input, select, textarea'),
      function (input) { input.dataset.touched = ''; });
    Promise.all([
      fetch('/api/pairs').then(function (r) { return r.json(); }),
      fetch('/api/settings').then(function (r) { return r.json(); })
    ]).then(function (answers) {
      var saved = ((answers[0] || {}).pairs || {})[key] || {};
      var settings = (answers[1] || {}).settings || {};
      var live = (state.snapshot.pairs || {})[key] || {};
      LADDER_FIELDS.forEach(function (entry) {
        var input = pane.querySelector(entry[0]);
        if (!input) { return; }
        var field = entry[1];
        var kind = entry[2];
        var own = saved[field];
        if (field === 'algo_window' && own === undefined) {
          own = saved.show_fair_window;              // the old name
        }
        // Edited since this pane opened: leave it alone. What the
        // operator chose outranks what the file said a moment ago.
        if (input.dataset.touched) { return; }
        if (kind === 'algo-choice') {
          // The ENGINE says whether the Algo is on — it is never in the
          // file. The window tick is the file's.
          input.value = live.algo_on
            ? (live.algo_mode === 'LIVE' ? 'ALGO_LIVE' : 'ALGO')
            : ((own === undefined || own === null ? live.algo_window : own)
                ? 'FAIR_SPREAD' : 'NONE');
          return;
        }
        if (kind === 'check') {
          input.checked = !!(own === undefined || own === null
            ? live[entry[4] || field] : own);
          return;
        }
        if (kind === 'live' || kind === 'live-number') {
          // What the ladder is running, whatever the file does or does
          // not say. Never blank.
          var running = live[entry[4] || field];
          if (own === undefined || own === null || own === '') {
            input.value = running === undefined || running === null
              ? '' : running;
          } else {
            input.value = own;
          }
          if (field === 'quoting_leg' && !input.value) { input.value = ''; }
          return;
        }
        if (kind === 'number' || kind === 'blank-number') {
          // This ladder's own number. A pair that has never been
          // configured starts from the built-in value, shown plainly:
          // there is no defaults page, so what is in the box is what
          // this pair uses.
          var seed = settings[entry[3]];
          if (entry[4] === 'vip') {
            // The OKX VIP-priced figure is never copied into the box:
            // Apply would save it as this ladder's own and freeze it.
            var vip = (live.fee_commission || {})[entry[3]];
            seed = undefined;
            input.placeholder = vip === undefined || vip === null
              ? 'OKX fees' : Number(vip).toFixed(4);
            input.title = 'Blank: the commission per lot per side at your ' +
              'own OKX fee tier' + (vip === undefined || vip === null ? ''
                : ' (' + Number(vip).toFixed(4) + ' USDT)') +
              ', re-read hourly. Type a number only to override it.';
          }
          var value = (own === undefined || own === null || own === '')
            ? seed : own;
          input.value = (value === undefined || value === null)
            ? '' : value;
          input.title = kind === 'blank-number'
            ? 'this pair’s own. Leave it empty for no cross-check.'
            : 'this pair’s own. Every ladder carries its own numbers.';
          return;
        }
        input.value = (own === null || own === undefined) ? '' : own;
      });
      // A blank close is the desk-wide cutoff: say which time that is.
      var closeBox = pane.querySelector('.ls-session-close');
      var deskCutoff = (state.snapshot.broker_clock || {}).cutoff;
      if (closeBox && deskCutoff) { closeBox.placeholder = 'desk ' + deskCutoff; }
      // ...and the expiry rows follow whatever the pair-type control
      // NOW says. Reading `saved` here would put the note and the
      // enabled/disabled rows back to the file's answer even though
      // the control beside them is left showing the operator's.
      var typed = saved.algo_params || {};
      var effective = live.algo_params || {};
      ALGO_FIELDS.forEach(function (entry) {
        var input = pane.querySelector(entry[0]);
        if (!input || input.dataset.touched) { return; }
        var value = typed[entry[1]];
        if (value === undefined || value === null || value === '') {
          value = effective[entry[1]];
        }
        if (entry[2] === 'check') { input.checked = !!value; return; }
        input.value = (value === undefined || value === null) ? '' : value;
      });
      var algoState = pane.querySelector('.ls-algo-state');
      if (algoState) {
        algoState.textContent = !live.algo_on ? ''
          : (live.algo_mode === 'LIVE' ? 'LIVE — trading this ladder'
                                       : 'on — Signals: the Algo signals, '
                                         + 'you trade');
      }
      var typeBox = pane.querySelector('.ls-pair-type');
      fairKindFields(pane, {pair_type:
        (typeBox && typeBox.dataset.touched && typeBox.value)
          || saved.pair_type || live.pair_type || 'SPOT_FUTURE'});
      clipNote(pane, live);
      measuredSlippageInto(pane);
    }).catch(function (e) {
      toast('could not read this pair: ' + e);
    });
  }

  function clipNote(pane, live) {
    /* What one spread IS, right now, in both legs and in money.
     *
     * Leg B is not a field: it is the hedge — leg A x contract A /
     * (beta x contract B), rounded down — and typing a number into it
     * independently is how a "hedged" pair ends up net long one leg.
     * So it is reported instead, beside the leg A box that decides it.
     */
    var note = pane.querySelector('.ls-clip-note');
    if (!note) { return; }
    var lotsA = live.clip_lots_a || 1;
    var lotsB = live.clip_lots_b || 1;
    var qty = live.default_quantity || 1;
    // PER QTY AND FOR THE QTY IN THE BOX, side by side. The first is
    // the figure the exit levels are priced per and does not move when
    // the keypad is touched; the second is the real money on the click
    // about to be made.
    // `spread_units` on the snapshot is ALREADY per one Qty — it is
    // clip_lots_b x contract B — so it is the per-unit figure, not the
    // one for the click.
    var perQty = live.spread_units || 0;
    // ...and what a lot IS, in the instrument's own units. The desk
    // thinks in ounces and barrels, not lots: "when a trader places 1,
    // in the backend it places 100 oz". The contract size comes from
    // MT5 and is never typed, so this is the one place the two
    // readings meet.
    function overridden(used, fromMt5, symbol) {
      if (!used || !fromMt5 || Math.abs(used - fromMt5) < 1e-9) { return ''; }
      return (symbol || '') + ' set to ' + fmt(used, 2) +
        ', OKX says ' + fmt(fromMt5, 2);
    }
    function inUnits(lots, contract, symbol) {
      // Coins, not lots: 0.1 lot of BTC is 0.001 BTC, and two decimals
      // printed that as 0.00. Four significant figures, whatever size.
      if (!contract) { return ''; }
      var units = lots * contract;
      return ' = ' + (units ? String(Number(units.toPrecision(4))) : '0') +
        ' ' + (String(symbol || '').indexOf('-') > 0
               ? String(symbol).split('-')[0] : 'units');
    }
    // Sized in dollars: the lots are the engine's, worked out from the
    // prices - typed ones would be overwritten on the next pass.
    var usd = live.usd_per_qty;
    ['.ls-clip-a', '.ls-clip-b'].forEach(function (sel) {
      var box = pane.querySelector(sel);
      if (box) {
        box.readOnly = !!usd;
        if (usd) {
          // The lots in force, not the ones once typed.
          var now = sel === '.ls-clip-a' ? live.clip_lots_a
                                         : live.clip_lots_b;
          if (now) { box.value = Number(now).toFixed(4); }
        }
        box.title = usd ? 'Sized in dollars: worked out from the live ' +
          'prices and beta. Clear "$ per Qty" to type lots.' : box.title;
      }
    });
    note.textContent = (usd ? 'Sized in dollars: 1 Qty = $' +
      Number(usd).toLocaleString() + ' (both legs), lots following the ' +
      'prices. ' : '') +
      'In force: 1 Qty = ' + fmt(lotsA, 2) + ' lots ' +
      (live.symbol_a || 'A') + inUnits(lotsA, live.contract_a, live.symbol_a) +
      ' / ' + fmt(lotsB, 2) + ' lots ' +
      (live.symbol_b || 'B') + inUnits(lotsB, live.contract_b, live.symbol_b) +
      (perQty ? ' — ' + money(perQty) + ' per 1.00 of spread' : '') +
      '. Qty ' + fmt(qty, 2) + ' sends ' + fmt(lotsA * qty, 2) + ' / ' +
      fmt(lotsB * qty, 2) + ' lots' +
      (perQty ? ', ' + money(perQty * qty) + ' per 1.00' : '') +
      // WHERE THE CONTRACT SIZE CAME FROM. Normally MT5's, and said so.
      // An override is stated as an override, with MT5's own number
      // beside it: every money figure on this ladder runs through it.
      (overridden(live.contract_a, live.contract_a_mt5, live.symbol_a) ||
       overridden(live.contract_b, live.contract_b_mt5, live.symbol_b)
        ? ' CONTRACT SIZE OVERRIDDEN — ' +
          [overridden(live.contract_a, live.contract_a_mt5, live.symbol_a),
           overridden(live.contract_b, live.contract_b_mt5, live.symbol_b)]
            .filter(Boolean).join('; ') + '.'
        : ' Contract sizes (ctVal) come from OKX and are never typed in.');
  }

  function fairKindFields(pane, saved) {
    /* An expiry belongs to a CONTRACT, and a fair spread to a pair
     * whose legs are the same underlying. Spot vs a future: only leg B
     * expires. Two futures: both do. Two different instruments — WTI
     * against Brent — have no carry between them and no fair spread at
     * all, and asking for a date there is asking for something that
     * does not exist. */
    var type = (saved.pair_type || 'SPOT_FUTURE').toUpperCase();
    var shape = {
      SPOT_FUTURE: {a: false, b: true,
                    note: 'spot vs a future — only leg B expires'},
      FUTURE_FUTURE: {a: true, b: true,
                      note: 'two futures — both legs expire, and the '
                        + 'carry runs to the NEAR one'},
      RELATED: {a: false, b: false,
                note: 'two different instruments — no carry ties them '
                  + 'together, and there is no fair spread to quote'}
    }[type] || {a: true, b: true, note: ''};
    var note = pane.querySelector('.ls-pairtype');
    if (note) { note.textContent = shape.note; }
    [['.row-expiry-a', shape.a, 'Leg A is spot — spot does not expire'],
     ['.row-expiry-b', shape.b, 'this pair has no expiry']].forEach(
      function (entry) {
        var row = pane.querySelector(entry[0]);
        if (!row) { return; }
        var input = row.querySelector('input');
        row.classList.toggle('not-applicable', !entry[1]);
        input.disabled = !entry[1];
        input.placeholder = entry[1] ? 'from OKX' : entry[2];
      });
  }

  function measuredSlippageInto(pane) {
    /* The allowance is a BUDGET; this is what slippage actually cost.
     * Beside it, so the number is corrected from data. */
    fetch('/api/slippage').then(function (r) { return r.json(); })
      .then(function (body) {
        var stats = body && body.ok && body.overall && body.overall.round_trip;
        var row = pane.querySelector('.ls-slip');
        if (!row) { return; }
        row.title = (stats && stats.money_mean !== null &&
                     stats.money_mean !== undefined)
          ? row.title + ' MEASURED this session: ' +
            stats.money_mean.toFixed(2) + ' a round turn over ' +
            stats.measured + ' position(s).'
          : row.title;
      }).catch(function () { /* no engine: the row says nothing extra */ });
  }

  function saveLadderSettings(node, key) {
    /* What the form sends, and what it must never send.
     *
     * A LIVE field — Mode, Time in force, Overnight — is never sent as
     * null. `OrderType(null)` raises on the engine, so a blank Mode
     * used to make Apply fail silently: the save reported success and
     * the ladder went on running whatever it had.
     *
     * An INHERITED number that has not been touched is sent as null,
     * which means "keep using the system default" — not as the number
     * it happens to be showing, which would silently freeze today's
     * default into this pair for ever.
     */
    var pane = node.querySelector('.ladder-settings');
    var payload = {};
    var live = {};
    var before = algoChoiceOf((state.snapshot.pairs || {})[key]);
    var choice = before;
    LADDER_FIELDS.forEach(function (entry) {
      var input = pane.querySelector(entry[0]);
      if (!input) { return; }
      var field = entry[1];
      var kind = entry[2];
      var raw = (input.value || '').trim();
      var value;
      if (kind === 'algo-choice') {
        choice = raw || 'NONE';
        // Algo leaves the saved window tick as it was: the Algo is the
        // engine's switch, and a restart goes back to what is saved.
        if (choice === 'ALGO' || choice === 'ALGO_LIVE') { return; }
        value = choice === 'FAIR_SPREAD';
      } else if (kind === 'check') {
        value = input.checked;
      } else if (kind === 'live') {
        if (!raw && field !== 'quoting_leg') { return; }   // never a null
        value = raw || null;              // '' on quoting_leg = wider book
      } else if (kind === 'live-number') {
        if (!raw) { return; }
        value = parseFloat(raw);
        if (isNaN(value)) { return; }
      } else if (kind === 'number') {
        // Emptied, it goes back to the built-in value rather than to a
        // null the engine would read as "nothing set" — with no
        // defaults page there is nothing for a null to mean.
        value = raw === '' ? null : parseFloat(raw);
        if (value !== null && isNaN(value)) { return; }
      } else if (kind === 'blank-number') {
        value = raw === '' ? null : parseFloat(raw);
        if (value !== null && isNaN(value)) { return; }
      } else {
        value = raw === '' ? null : raw;
      }
      payload[field] = value;
      if (value !== null) { live[field] = value; }
    });
    // The Algo's numbers, typed ones only: an empty box is the default.
    var params = {};
    ALGO_FIELDS.forEach(function (entry) {
      var input = pane.querySelector(entry[0]);
      if (!input) { return; }
      if (entry[2] === 'check') { params[entry[1]] = !!input.checked; return; }
      var raw = (input.value || '').trim();
      if (raw === '') { return; }
      if (entry[2] === 'text') { params[entry[1]] = raw; return; }
      var number = parseFloat(raw);
      if (!isNaN(number)) { params[entry[1]] = number; }
    });
    payload.algo_params = params;
    live.algo_params = params;
    fetch('/api/pairs/' + encodeURIComponent(key), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); }).then(function (answer) {
      if (!answer || !answer.ok) {
        toast('could not save: ' + ((answer && answer.error) || 'unknown'));
        return;
      }
      // Saved AND applied: the engine re-reads the file on its next
      // poll, and the live send changes the ladder in front of the
      // trader now rather than a poll later.
      setPair(key, live);
      if (choice !== before) {
        // ON, OFF or LIVE in the running engine. Its refusal, if any,
        // comes back in its own words; the next snapshot shows what it
        // actually did.
        setAlgo(key, choice);
      }
      var row = (state.snapshot.pairs || {})[key];
      if (row && choice !== 'ALGO' && choice !== 'ALGO_LIVE') {
        row.algo_window = !!payload.algo_window;
      }
      (answer.notes || []).forEach(function (note) { toast(note); });
      if (!(answer.notes || []).length) { toast('applied to ' + key, 'ok'); }
      pane.hidden = true;
      // One door for the window, so the pane and the window's own X
      // cannot disagree about whether it is open.
      var fairId = panelId('fair', key);
      var algoChosen = choice === 'ALGO' || choice === 'ALGO_LIVE';
      // The Algo opens its own window (render, on the next snapshot);
      // this one is the fair spread's, and follows its own tick.
      var wanted = algoChosen ? (state.open.indexOf(fairId) >= 0)
        : !!payload.algo_window;
      if (wanted) { delete state.closed[fairId]; }
      if (algoChosen) { delete state.closed[panelId('algo', key)]; }
      if (wanted !== (state.open.indexOf(fairId) >= 0)) {
        if (wanted) { openPanel(fairId); } else { closePanel(fairId); }
      } else {
        render();
      }
    }).catch(function (e) { toast('could not save: ' + e); });
  }

  function atTouch(key, side) {
    // Hit the touch without hunting for the row: the fastest
    // possible "I want in, now" on the ladder already in front of you.
    var row = state.snapshot.pairs[key];
    if (!row) { return; }
    // BUY lifts the long spread, SELL hits the short one — the price the
    // market is actually offering for that direction, never the mid.
    var level = side === 'BUY' ? row.long_spread : row.short_spread;
    if (level === null || level === undefined) {
      toast(key + ' has no price yet — nothing to hit');
      return;
    }
    clickLevel(key, side, level);
  }

  function closeAtLimit(key, value) {
    /* Close everything on this ladder AT A PRICE, instead of crossing
     * now. One working order per position, carrying that position's
     * ticket so it CLOSES — on a hedging account an opposite order
     * without it opens a second position. A target, and no stop. */
    var row = state.snapshot.pairs[key] || {};
    var level = parseFloat(value);
    if (!isFinite(level)) {
      toast('type the spread level to close at, beside the button');
      return;
    }
    if (isFlat(row.net_position)) {
      toast((row.name || key) + ' is already flat — nothing to close');
      return;
    }
    ask('Rest a closing order at ' + fmt(level, 4) + '?',
        (row.name || key) + '\n' +
        (row.net_position > 0 ? '+' : '') + qty(row.net_position) +
        ' spreads open.\n\nOne working order per position, at that ' +
        'level, by ticket. It WAITS there — nothing crosses now, and ' +
        'there is no stop. CLOSE ALL is the one that crosses.',
        'Rest it', function () {
          send('close_at_limit', {pair: key, level: level},
               function (result) {
                 var data = (result || {}).data || {};
                 if (data.reason) { toast(data.reason, data.ok ? 'ok' : ''); }
                 else if (data.ok) {
                   toast('closing order(s) resting at ' + fmt(level, 4), 'ok');
                 }
               });
        });
  }

  function exitPair(key) {
    /* The ladder's DEFAULT way out — what F does.
     *
     * MARKET crosses now; LIMIT rests at the price on the rail. Both
     * buttons stay on the screen whichever is selected: this decides
     * which one is the default, never which one exists.
     */
    var node = document.querySelector(
      '.ladder[data-pair="' + cssEscape(key) + '"]');
    var row = state.snapshot.pairs[key] || {};
    if ((row.exit_type || 'MARKET') !== 'LIMIT') { return flatten(key); }
    var box = node && node.querySelector('.close-limit');
    return closeAtLimit(key, box ? box.value : '');
  }

  function flatten(key) {
    var row = state.snapshot.pairs[key] || {};
    if (isFlat(row.net_position)) {
      toast((row.name || key) + ' is already flat');
      return;
    }
    // Flattening is irreversible and it is the button pressed in a
    // hurry, so it asks — but with one key, and only once.
    ask('Flatten ' + (row.name || key) + '?',
        (row.net_position > 0 ? '+' : '') + qty(row.net_position) +
        ' spreads, at market, by ticket. This cannot be undone.',
        'Flatten now', function () { send('flatten_pair', {pair: key}); });
  }

  function money_(value, currency) {
    /* Compact money for the rail: 15,764 rather than 15,764.37. The
     * rail is 120px wide and the cents are never the question there. */
    if (value === undefined || value === null || !isFinite(value)) {
      return DASH;
    }
    return Math.round(value).toLocaleString('en-US') +
      (currency ? ' ' + currency : '');
  }

  function fitLadderFloor(node) {
    /* The ladder's floor, measured on THIS machine.
     *
     * A number typed into the stylesheet is a number measured against
     * one font on one box. Segoe UI on Windows sets taller than the
     * font this was designed against, so a rail that fits by four
     * pixels here goes behind a scrollbar there — which is exactly
     * what happened, twice.
     *
     * So it is measured: the rail's own children, the chrome above and
     * below, and nothing else. Written only when it CHANGES, because
     * this runs on every render.
     */
    var rail = node.querySelector('.rail');
    var footer = node.querySelector('.footer');
    if (!rail || !footer || !rail.children.length) { return; }
    // Measured from the first child's top to the last one's bottom, so
    // the gaps AND the margins are in it. Adding up offsetHeight
    // misses margins, and the four pixels that costs is the difference
    // between fitting and a scrollbar.
    var first = rail.firstElementChild.getBoundingClientRect();
    var last = rail.lastElementChild.getBoundingClientRect();
    var need = (last.bottom - first.top) + 8;     // + the rail's padding
    var chrome = 0;
    ['.titlebar', '.quotestrip'].forEach(function (selector) {
      var part = node.querySelector(selector);
      if (part) { chrome += part.offsetHeight; }
    });
    chrome += footer.scrollHeight + 2;
    // Never taller than the desktop it is on. `min-height` beats
    // `max-height` in CSS, so a floor measured larger than the screen
    // would push the footer off the bottom of a desktop that does not
    // scroll vertically — the books gone, and no way to get them back.
    var desktop = desktopBox();
    var floor = Math.ceil(need + chrome);
    if (desktop.height) { floor = Math.min(floor, Math.floor(desktop.height)); }
    if (!floor || node._floor === floor) { return; }
    node._floor = floor;
    node.style.minHeight = floor + 'px';
  }

  function renderLadder(key, row) {
    var node = ladderNode(key);
    var market = row.market || {};
    // The ENGINE holds the lock; the browser only shows it. On the
    // first draw of a pair — a reload, or a second screen — adopt what
    // the engine reports rather than starting unticked over a window
    // the engine is still holding still.
    if (state.locked[key] === undefined) {
      state.locked[key] = !!row.ladder_locked;
      var first = node.querySelector('.lock-scroll');
      if (first) { first.checked = state.locked[key]; }
    }
    node.classList.toggle('mode-market', row.order_type === 'MARKET');
    node.classList.toggle('inactive', state.active !== panelId('ladder', key));
    // Algo LIVE: the ways IN are off and say why; the ways OUT stay.
    var algoLive = algoTrades(row);
    node.classList.toggle('algo-live', algoLive);
    var lockNote = node.querySelector('.algo-lock');
    if (lockNote) { lockNote.hidden = !algoLive; }
    node.querySelectorAll('.buy-touch, .sell-touch, .keypad button, ' +
                          '.qty-box, .usd-keypad button, .usd-box')
      .forEach(function (control) {
      control.disabled = algoLive;
    });
    node.querySelector('.title').textContent = row.name || key;
    node.querySelector('.route').textContent =
      (row.account_a || '?') + ' → ' + (row.account_b || '?');
    // Which leg rests the real pending, and which one crosses. Read
    // from the ENGINE's own groups where there is one working, and
    // from the pair's setting before that.
    var note = node.querySelector('.quoting-note');
    if (note) {
      var live = (row.quotes || [])[0];
      var leg = (live && live.leg) ||
        (row.quoting_leg_effective
          ? row.quoting_leg_effective.toUpperCase() : '');
      var symbol = (live && live.symbol) ||
        (leg === 'A' ? row.symbol_a : leg === 'B' ? row.symbol_b : '');
      // WHY AN ORDER IS NOT AT THE BROKER, IN WORDS, WITHOUT HOVERING.
      //
      // An entry whose pending was never placed says why — but only in
      // the Work cell's tooltip, which nobody hovers when the thing
      // they are looking at is an order that appears to be working.
      // "W:1 (broker 0)" in 9px in the footer was the only other sign.
      // A trader reading the ladder must be able to see that their
      // order is in this process and NOWHERE ELSE.
      var held = null;
      (row.quotes || []).forEach(function (quote) {
        if (held || quote.intent === 'CLOSE' || quote.ticket) { return; }
        if (quote.held && !quote.reason) { return; }    // held by design
        held = (quote.held ? 'ORDER HELD: ' : 'NOT AT THE BROKER: ') +
          (quote.reason || 'not at the broker yet');
      });
      var maker = (row.maker_fills || [])[0];
      var makerStyle = row.fill_style === 'maker';
      note.textContent = held
        ? held
        : maker
        ? makerWords(maker)
        : (row.order_type === 'MARKET'
            ? (makerStyle ? 'MARKET: both legs rest at the touch as maker'
                          : 'MARKET: both legs cross at once')
            : row.working_orders === 'TRIGGER'
            ? (makerStyle
                ? 'LIMIT: held here · both legs rest as maker when the ' +
                  'spread reaches it'
                : 'LIMIT: held here · both legs cross when the spread ' +
                  'reaches it')
            : (leg
                ? 'LIMIT: quotes leg ' + leg + (symbol ? ' · ' + symbol : '') +
                  ' · leg ' + (leg === 'A' ? 'B' : 'A') + ' crosses on fill'
                : 'LIMIT: quoting leg picked from the wider book'));
      note.classList.toggle('live', (!!(live && live.ticket) || !!maker) &&
                            !held);
      note.classList.toggle('held', !!held);
      note.title = held
        ? 'This ladder has a working order that exists in THIS process '
          + 'and nowhere else — there is nothing of yours at either '
          + 'broker for it. It rests as soon as the reason above clears.'
        : 'LIMIT mode rests a real pending on ONE leg and crosses the '
          + 'other at market when it fills. Leg A showing no PENDING in '
          + 'MT5 is the mode working, not a fault: it takes a position '
          + 'the instant the quoting leg fills, and never an order '
          + 'before that.';
    }
    var badge = node.querySelector('.mode-badge');
    // The ladder says whose it is: while the Algo is LIVE, a click here
    // is refused, and the trader must be able to see why.
    badge.textContent = row.order_type + ' · ' + row.time_in_force;
    badge.title = 'the mode a click sends, and how long a working order lives';
    renderAlgoSwitch(node, key, row);

    var change = market.net_change;
    var netchg = node.querySelector('.netchg');
    netchg.textContent = change === null || change === undefined
      ? DASH : (change > 0 ? '+' : '') + fmt(change, 4);
    netchg.classList.toggle('up', change > 0);

    var session = market.session || {};
    node.querySelector('.stat-h').textContent = fmt(session.high, 4);
    node.querySelector('.stat-l').textContent = fmt(session.low, 4);
    node.querySelector('.stat-o').textContent = fmt(session.open, 4);
    // Per LEG, never added: one lot of spot and one of the future are
    // not two lots of anything.
    node.querySelector('.stat-v1').textContent = fmt(session.volume_a, 0);
    node.querySelector('.stat-v2').textContent = fmt(session.volume_b, 0);
    // Whose numbers these are — the terminals', or ours since this
    // process started. A borrowed range read as the exchange's is how a
    // spread gets judged against prices nobody traded.
    var ours = node.querySelector('.ours');
    ours.textContent = session.source === '24h' ? '24h'
      : (session.ours === false ? 'legs' : 'ours');
    ours.title = session.source === '24h'
      ? 'the spread\u2019s own last 24 hours: both legs\u2019 5-minute ' +
        'closes paired, B \u2212 \u03b2 \u00d7 A each bar. The big ' +
        'number is the change since 24 h ago'
      : 'since this desk started';
    ours.classList.toggle('legs', session.ours === false);
    netchg.title = session.source === '24h'
      ? 'change in the spread over the last 24 hours'
      : 'change in the spread since this desk started';

    setValue(node.querySelector('.order-type'), row.order_type);
    // WHICH way out is this ladder's default — said on the buttons
    // themselves, because F fires one of them and a trader must not
    // have to remember which. CLOSE ALL still crosses now either way:
    // the way out does not change meaning under the button pressed in
    // a hurry, it only stops being the DEFAULT one.
    var atLimit = (row.exit_type || 'MARKET') === 'LIMIT';
    var flat = node.querySelector('.flatten');
    var lmt = node.querySelector('.close-limit-go');
    if (flat && lmt) {
      flat.classList.toggle('primary-exit', !atLimit);
      lmt.classList.toggle('primary-exit', atLimit);
      flat.title = flat.title.replace(/ \(F\)$/, '') + (atLimit ? '' : ' (F)');
      lmt.title = lmt.title.replace(/ \(F\)$/, '') + (atLimit ? ' (F)' : '');
    }
    setValue(node.querySelector('.tif'), row.time_in_force);
    setValue(node.querySelector('.increment'), row.increment);

    // One box per SIDE, each beside the button that sends it. Never
    // blank — a box shows the size that click WILL send, which is the
    // armed one if there is one and the ladder's default otherwise —
    // and never written while it is being typed in, or the poll would
    // eat the size as it is entered.
    // THE KEYPAD OFFERS ONLY WHAT CAN BE TRADED.
    //
    // A ladder whose broker caps leg A at 10 lots was showing 50 and
    // 100 buttons, and pressing one got a refusal back — "Qty 100 x 1
    // wants 100 lots on leg A, over this broker's 10-lot maximum". The
    // arithmetic was right and the button should not have been there.
    var cap = (row.max_qty === null || row.max_qty === undefined)
      ? null : Number(row.max_qty);
    node.querySelectorAll('.keypad .qty').forEach(function (button) {
      var size = Number(button.dataset.qty);
      var beyond = cap !== null && button.dataset.qty && size > cap;
      button.disabled = !!beyond || algoTrades(row);
      button.title = beyond
        ? 'Qty ' + size + ' is over what this pair can trade — it tops '
          + 'out at ' + cap
        : '';
    });

    ['buy', 'sell'].forEach(function (side) {
      var box = node.querySelector(side === 'buy' ? '.armed' : '.sell-qty');
      var armed = armedFor(key, side);
      if (document.activeElement !== box) {
        box.value = armed ? String(armed)
          : (row.default_quantity === null ||
             row.default_quantity === undefined
              ? '' : String(row.default_quantity));
      }
      box.classList.toggle('on', !!armed);
      var size = armed || row.default_quantity;
      // OVER WHAT THE BROKER WILL TAKE. The refusal already says so
      // once the click is made; this says it before, on the box the
      // size was typed into.
      box.classList.toggle('over', cap !== null && size > cap);
      box.title = cap === null ? ''
        : 'this pair tops out at Qty ' + cap + ' — the smaller of what '
          + 'the two brokers will take';
      var sized = (size === null || size === undefined) ? '' : ' ' + size;
      var button = node.querySelector(
        side === 'buy' ? '.buy-touch' : '.sell-touch');
      // SAID IN THE UNIT IT WAS SET IN: "BUY $500" when the size came
      // from the margin keypad or box (the total margin, both legs
      // together), "BUY 30" when it came from a Qty. The other figures
      // on hover.
      var usdArmed = (state.armedUsd || {})[key];
      var margin = usdArmed || null;
      var per = qtyDollars(row);
      button.textContent = (side === 'buy' ? 'BUY' : 'SELL') +
        (margin ? ' ' + kDollars(margin) : sized);
      button.title = (side === 'buy' ? 'Buy' : 'Sell') + ' the spread ' +
        (side === 'buy' ? 'at the offer (B)' : 'at the bid (S)') +
        (size ? ' - Qty ' + size +
          (margin ? ', ' + kDollars(margin) + ' of margin, both legs ' +
            'together' : '') +
          (per ? ', about ' + kDollars(size * per) + ' of position'
            : '') : '');
    });
    Array.prototype.forEach.call(node.querySelectorAll('.keypad .qty'),
      function (button) {
        var size = parseFloat(button.dataset.qty);
        button.classList.toggle('on',
          armedFor(key, 'buy') === size && armedFor(key, 'sell') === size);
      });
    // Which accounts this ladder is routed across, and their logins.
    var accounts = state.snapshot.accounts || {};
    function accountText(account) {
      var info = accounts[account] || {};
      var uid = String(info.login || '');
      var money = info.equity === undefined || info.equity === null
        ? DASH
        : money_(info.equity, info.currency);
      return escapeHtml(account || '?') + (uid
        ? ' <span title="OKX account UID ' + escapeHtml(uid) + '">#' +
          escapeHtml(uid.length > 6 ? '\u2026' + uid.slice(-4) : uid) +
          '</span>' : '') +
        '<div class="route-eq" title="Equity: balance plus the ' +
        'P&L of everything open on this account">' + money + '</div>';
    }
    function routeText(account, symbol) {
      var info = accounts[account] || {};
      // OKX UIDs run to 18 digits - wider than half the rail. The last
      // four identify it; the whole one is on hover.
      var uid = String(info.login || '');
      var login = uid ? ' <span title="OKX account UID ' + escapeHtml(uid) +
        '">#' + escapeHtml(uid.length > 6 ? '\u2026' + uid.slice(-4) : uid) +
        '</span>' : '';
      // EQUITY, not balance: balance ignores what is open, and the
      // number a trader sizes the next spread against is the one that
      // already carries the running P&L. Unmeasured stays a dash —
      // a leg that could not be read must never read as zero money.
      var money = info.equity === undefined || info.equity === null
        ? DASH
        : money_(info.equity, info.currency);
      return (symbol || '?') + '<div class="hint">' + (account || '?') +
        login + '</div><div class="hint route-eq" title="Equity: balance ' +
        'plus the P&L of everything open on this account">' + money +
        '</div>';
    }
    var shared = node.querySelector('.route-shared');
    var oneAccount = row.account_a && row.account_a === row.account_b;
    if (oneAccount && shared) {
      node.querySelector('.route-a b').textContent = row.symbol_a || '?';
      node.querySelector('.route-b b').textContent = row.symbol_b || '?';
      shared.innerHTML = accountText(row.account_a);
      shared.classList.remove('hidden');
    } else {
      node.querySelector('.route-a b').innerHTML =
        routeText(row.account_a, row.symbol_a);
      node.querySelector('.route-b b').innerHTML =
        routeText(row.account_b, row.symbol_b);
      if (shared) { shared.classList.add('hidden'); }
    }
    // The three cancel buttons, with what they would pull ON them —
    // and disabled when that is nothing, so a button that cannot do
    // anything cannot be pressed and wondered about.
    var buys = row.working_buys || 0;
    var sells = row.working_sells || 0;
    node.querySelector('.count-b').textContent = buys || '';
    node.querySelector('.count-s').textContent = sells || '';
    node.querySelector('.count-all').textContent = (buys + sells) || '';
    node.querySelector('.cxl-b').disabled = !buys;
    node.querySelector('.cxl-s').disabled = !sells;
    node.querySelector('.cxl-all').disabled = !(buys + sells);

    renderRows(node, key, row);

    var counts = node.querySelector('.counts');
    counts.querySelector('.cnt-b').textContent = 'B:' + bought(row);
    counts.querySelector('.cnt-s').textContent = 'S:' + sold(row);
    var working = row.working_buys + row.working_sells;
    var resting = row.broker_pendings;
    // How many of ours SHOULD be at the broker. A resting close is
    // held here and fired by ticket — it can never have a pending —
    // so counting it against the broker's number made every reducing
    // click read as two orders that had failed to place, in red.
    var closes = row.resting_closes || 0;
    // A working ENTRY held here (the spread trigger) has no pending
    // either, by design.
    var expected = working - closes - (row.held_entries || 0);
    var disagree = resting !== undefined && resting !== expected;
    counts.querySelector('.cnt-w').textContent =
      'W:' + working +
      (closes ? ' (' + closes + ' resting close' + (closes > 1 ? 's' : '')
                + ')' : '') +
      (disagree ? ' (broker ' + resting + ')' : '');
    counts.querySelector('.cnt-w').classList.toggle('mismatch', disagree);
    counts.querySelector('.cnt-w').title = closes
      ? closes + ' of these are resting CLOSES: the level is held here '
        + 'and both legs are closed by ticket when the market reaches '
        + 'it, so nothing of theirs is at the broker and nothing should be.'
      : 'Our working orders, and the broker\u2019s own count when they '
        + 'disagree.';

    var feed = node.querySelector('.feed');
    feed.textContent = market.feed_badge || DASH;
    feed.className = 'feed ' + badgeClass(market.feed_badge);
    node.querySelector('.pos').textContent = !isFlat(row.net_position)
      ? (row.net_position > 0 ? '+' : '') + qty(row.net_position) + ' @ ' +
        fmt(row.avg_entry, 4)
      : 'flat';
    var pnl = node.querySelector('.pnl');
    pnl.textContent = row.open_pnl === null || row.open_pnl === undefined
      ? DASH : money(row.open_pnl) + shareWords(row.open_pnl_pct_notional,
                                                row.open_pnl_pct_margin);
    pnl.title = 'open P&L, and as a share of notional (N) and of margin '
      + '(M) over every position on this ladder';
    pnl.classList.toggle('up', row.open_pnl > 0);
    pnl.classList.toggle('down', row.open_pnl < 0);
    // HIDDEN ON REQUEST, not removed. The numbers are correct — Qty is
    // in SPREADS and 0.01 lots is what one spread IS, from
    // L_B = L_A x C_A / (beta x C_B) — but reading "Qty 1" and
    // "0.01 lots" side by side reads as a contradiction on the desk.
    // The derivation still lives in the element's tooltip. To put it
    // back, restore this line.
    node.querySelector('.clip').textContent = '';
    // node.querySelector('.clip').textContent =
    //   '1 spread = ' + fmt(row.clip_lots_a, 2) + ' A / ' +
    //   fmt(row.clip_lots_b, 2) + ' B, ' + money(row.spread_units) +
    //   ' per 1.00';
    node.querySelector('.errors').textContent = (row.errors || []).join(' ');
    renderLegBook(node, row, market);
    // After the books are in: the floor includes them, and they are the
    // part whose height depends on the symbols' names.
    fitLadderFloor(node);
    return node;
  }

  function depthText(size, isTouch, mark) {
    if (size === null || size === undefined) {
      return isTouch ? mark : '';
    }
    // Rounded to two, because a spread's implied size is a ratio of two
    // lot sizes and 3.3333333 is not a number anyone can act on.
    var text = size >= 10 ? String(Math.round(size)) : size.toFixed(2);
    return text.replace(/\.00$/, '');
  }

  function renderLegBook(node, row, market) {
    /* The two legs' books, at the BOTTOM of the window.
     *
     * Moved there rather than merely placed there: a page served from
     * a template an older process cached can have this panel somewhere
     * else entirely, and where it sits is not a detail — it is beside
     * the ladder's own prices, where it would be read as part of them.
     * One line here makes the position true of whatever markup the
     * browser was given.
     */
    var legs = node.querySelector('.legs');
    var footer = node.querySelector('.footer');
    if (!legs || !footer) { return; }
    if (legs.parentNode !== footer) { footer.appendChild(legs); }
    legs.innerHTML = legFeed(row, market);
    var okxacct = node.querySelector('.okxacct');
    if (okxacct) { okxacct.textContent = okxAccountWords(row); }

  }

  function okxAccountWords(row) {
    /* 'OKX Lv1 (regular) · fees A 2.0/5.0 B 2.0/5.0 bp · lev A 10x B 10x' */
    var parts = ['OKX ' + feeLevelName(row.fee_level)];
    var fees = row.fee_bps || {};
    function rate(leg) {
      var f = fees[leg];
      return f ? fmt(f.maker, 1) + '/' + fmt(f.taker, 1) : DASH;
    }
    if (fees.a || fees.b) {
      parts.push('maker/taker A ' + rate('a') + ' B ' + rate('b') + ' bp');
    }
    function lev(x) { return x ? fmt(x, 0) + 'x' : DASH; }
    parts.push('leverage A ' + lev(row.leverage_a) + ' B ' +
               lev(row.leverage_b));
    return parts.join(' \u00b7 ');
  }

  //: What the Algo is doing, in the trader's words. The engine's
  //: state names are for the code.
  var ALGO_EXIT_WORDS = {
    STOP_LOSS: 'stop loss',
    PROFIT_TARGET: 'profit target', Z_STOP: 'z-stop',
    MEAN_REVERSION: 'back to the mean', TIME_STOP: 'time stop'
  };

  function algoLine(block, digits) {
    /* One short line: what the Algo says right now. */
    var state = block.state;
    if (state === 'SIGNAL') {
      var z = block.signal === 'SELL' ? block.z_sell : block.z_buy;
      return (block.signal === 'SELL' ? 'H to L' : 'L to H') + ' signal  z ' +
        signed(z);
    }
    if (state === 'EXIT') {
      // WHICH position, by what it is: the side and the price it went
      // on at. With two on the ladder, "exit" alone is half a sentence.
      var rows = (block.positions || []).filter(function (p) {
        return p.exit;
      });
      return rows.map(function (p) {
        return 'EXIT ' + positionWords(p, digits) + ': ' +
          (ALGO_EXIT_WORDS[p.exit] || p.exit);
      }).join('; ');
    }
    if (state === 'IN_POSITION') {
      var held = block.positions || [];
      var first = held[0] || {};
      var add = block.add || {};
      return 'in ' + positionWords(first, digits) +
        (held.length > 1 ? ' (+' + (held.length - 1) + ' add)' : '') +
        ' — TP ' + levelWords(first.tp, first.tp_money, digits) + ' · SL ' +
        (first.sl === null || first.sl === undefined
          ? (((block.params || {}).stop_loss_on) ? '—' : 'off')
          : levelWords(first.sl, first.sl_money, digits)) +
        // Where an ADD would go on: the spread it must reach first.
        (add.beyond === null || add.beyond === undefined ? ''
          // The add waits for the spread to have GONE past the line (the
          // furthest so far, beside it), then takes the re-entry.
          : ' · add ' + addSizeWords(block.params || {}) +
            (add.reached ? 'on re-entry (went ' +
              fmt(add.extreme, digits) + ', past ' + fmt(add.beyond, digits) +
              ')'
              : 'once past ' + (add.side === 'SELL' ? '\u2265 ' : '\u2264 ') +
                fmt(add.beyond, digits) +
                (add.extreme === null || add.extreme === undefined ? ''
                  : ' (furthest ' + fmt(add.extreme, digits) + ')') +
                ', then on re-entry'));
    }
    if (state === 'BLOCKED') { return 'held: ' + (block.blocked || ''); }
    if (state === 'CONFIRMING') {
      var streak = block.streak || {};
      return 'confirming ' + Math.max(streak.BUY || 0, streak.SELL || 0) +
        '/' + ((block.params || {}).confirm_ticks || '?');
    }
    if (state === 'STARTING') { return 'starting…'; }
    return 'watching';
  }

  function levelWords(level, worth, digits) {
    /* "59.2000 (+$4.00)": the price to compare with the closing spread,
     * and what closing there is worth to the whole position, net. */
    return fmt(level, digits) + (worth === null || worth === undefined ? ''
      : ' (' + (worth > 0 ? '+' : '') + money(worth) + ')');
  }

  function positionWords(position, digits) {
    /* "BUY @ 59.11": the side and the spread it was ENTERED at. Fills
     * read as one position say their size and that it is an average:
     * "SELL 2 @ 9.76 avg". */
    if (position.combined) {
      return (position.side || '?') + ' ' + fmt(position.quantity, 2)
        .replace(/\.?0+$/, '') + ' @ ' +
        fmt(position.entry_spread, digits) + ' avg';
    }
    return (position.side || '?') + ' @ ' + fmt(position.entry_spread, digits);
  }

  function progressHtml(first, digits) {
    /* SL <- entry -> TP, with the closing price on it. The two halves
     * are drawn to their own scale, as the engine measures them: the
     * stop and the target are rarely the same distance from the entry. */
    var entry = first.entry_spread;
    var toTp = first.tp === null || first.tp === undefined
      ? null : Math.abs(first.tp - entry);
    var toSl = first.sl === null || first.sl === undefined
      ? null : Math.abs(entry - first.sl);
    var split = (toTp && toSl) ? toSl / (toSl + toTp) : (toSl ? 0.5 : 0);
    var p = first.progress;
    var at = (p === null || p === undefined) ? null
      : (p >= 0 ? split + p * (1 - split) : split + p * split);
    var fill = at === null ? '' :
      '<div class="ap-fill ' + (p >= 0 ? 'up' : 'down') + '" style="left:' +
      (Math.min(at, split) * 100) + '%;width:' +
      (Math.abs(at - split) * 100) + '%"></div>' +
      '<div class="ap-mark" style="left:calc(' + (at * 100) +
      '% - 1px)"></div>';
    return '<div class="algo-progress" title="' + escapeHtml(positionWords(first,
        digits) + ', closing at ' + fmt(first.closing_spread, digits)) + '">' +
      '<div class="ap-pct">' + (p === null || p === undefined ? DASH
        : (p >= 0 ? Math.round(p * 100) + '% to TP'
                  : Math.round(-p * 100) + '% to SL')) + '</div>' +
      '<div class="ap-track">' + fill +
      '<div class="ap-entry" style="left:' + (split * 100) + '%"></div>' +
      '</div><div class="ap-ends"><span>' +
      (toSl === null ? 'no SL'
        : 'SL ' + levelWords(first.sl, first.sl_money, digits)) +
      '</span><span>' +
      (toTp === null ? 'no TP'
        : 'TP ' + levelWords(first.tp, first.tp_money, digits)) +
      '</span></div></div>';
  }

  function signed(value) {
    if (value === null || value === undefined || isNaN(value)) { return '—'; }
    return (value > 0 ? '+' : '') + Number(value).toFixed(2);
  }

  function renderAlgo(node, row) {
    /* The Fair Spread window's one line about which reading it is.
     *
     * The Algo has a window of its own now (renderAlgoWindow); this one
     * is the carry's, and says so. Returns false: nothing here draws
     * over the fair reading. */
    var block = row.algo_block || {};
    var kind = node.querySelector('.fair-kind');
    if (!kind) { return false; }
    kind.className = 'fair-kind hint';
    kind.textContent = row.algo_window ? (block.kind_note ||
      ((row.fair || {}).kind_note) || '') : '';
    var stray = row.algo_unmanaged || [];
    if (stray.length) {
      // The Algo's own position with nobody managing it: after a
      // restart, or once it went to dry run. Said until LIVE adopts it
      // or a trader closes it.
      kind.textContent = stray.length + ' Algo position(s) not managed — '
        + 'Algo LIVE adopts them, or close by hand';
    }
    return false;
  }

  // -- the Algo window ----------------------------------------------------

  function algoNode(key) {
    /* One ladder's Algo, in a window of its own while it is on. */
    var existing = document.querySelector(
      '.algowin[data-pair="' + cssEscape(key) + '"]');
    if (existing) { return existing; }
    var node = el('algo-template').content.firstElementChild.cloneNode(true);
    node.dataset.pair = key;
    node.querySelector('.close').addEventListener('click', function () {
      // The window IS the Algo's display. Closed, the Algo goes off with
      // it — a signal nobody can see is a signal nobody watches. LIVE
      // holding a position asks first what to do with it, and the
      // window stays until the engine says the Algo is off.
      var row = (state.snapshot.pairs || {})[key] || {};
      setAlgo(key, row.algo_window ? 'FAIR_SPREAD' : 'NONE');
    });
    node.querySelector('.aw-cog').addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      var ladder = document.querySelector(
        '.ladder[data-pair="' + cssEscape(key) + '"]');
      if (ladder) { openLadderSettings(ladder, key); }
      else { toast('open the ' + key + ' ladder to reach its settings'); }
    });
    node.querySelector('.aw-bt-run').addEventListener('click', function (e) {
      e.preventDefault();
      runBacktest(node, key,
                  parseInt(node.querySelector('.aw-bt-days').value, 10) || 5);
    });
    el('desktop').appendChild(node);
    return node;
  }

  function runBacktest(node, key, days) {
    /* Replay MT5's history through this ladder's Algo, on the engine.
     * A few seconds, not a click's fraction of one: it is polled for
     * longer than an order is, and says it is running meanwhile. */
    var out = node.querySelector('.aw-bt-out');
    var button = node.querySelector('.aw-bt-run');
    out.className = 'aw-bt-out hint';
    out.textContent = 'running on the last ' + days + ' days\u2026';
    button.disabled = true;
    function done(html, cls) {
      button.disabled = false;
      out.className = 'aw-bt-out' + (cls ? ' ' + cls : '');
      out.innerHTML = html;
    }
    fetch('/api/command', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: 'algo_backtest',
                            payload: {pair: key, days: days}})
    }).then(function (r) { return r.json(); }).then(function (sent) {
      if (!sent.ok) { return done(escapeHtml(sent.error || 'refused'), 'down'); }
      var tries = 0;
      (function poll() {
        fetch('/api/result/' + sent.id).then(function (r) { return r.json(); })
          .then(function (result) {
            if (result && result.pending) {
              if (++tries > 120) {
                return done('no answer from the engine yet \u2014 try again',
                            'down');
              }
              return window.setTimeout(poll, 250);
            }
            var data = (result && result.data) || {};
            if (!result || result.ok === false || data.ok === false) {
              return done('could not run: ' + escapeHtml(
                (result && result.error) || data.reason || 'unknown'), 'down');
            }
            done(backtestHtml(data));
          });
      })();
    }).catch(function (error) { done(escapeHtml(error.message), 'down'); });
  }

  function backtestHtml(data) {
    var s = data.summary || {};
    var plain = data.without_protections || {};
    function line(sum) {
      return (sum.trades || 0) + ' trade(s) \u00b7 ' + (sum.wins || 0) +
        ' won, ' + (sum.losses || 0) + ' lost \u00b7 net ' + moneyOr(sum.net) +
        ' \u00b7 worst run ' + moneyOr(sum.max_drawdown);
    }
    function when(at) {
      return at ? new Date(at * 1000).toLocaleString([], {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})
        : DASH;
    }
    var held = Object.keys(data.held || {}).map(function (reason) {
      return [reason, data.held[reason]];
    }).sort(function (a, b) { return b[1] - a[1]; }).slice(0, 3);
    var rows = (data.trades || []).slice(-8).reverse().map(function (t) {
      return '<tr><td>' + when(t.opened_at) + '</td><td>' +
        sideWords(t.side) + '</td><td>' + signed(t.entry_z) + '</td><td>' +
        escapeHtml(algoExitWords(t.reason)) + '</td><td class="' +
        (t.pnl > 0 ? 'up' : (t.pnl < 0 ? 'down' : '')) + '">' +
        moneyOr(t.pnl) + '</td></tr>';
    }).join('');
    return '<div><b>These settings:</b> ' + line(s) + '</div>' +
      '<div class="hint">Without re-entry and the trend filter: ' +
      line(plain) + '</div>' +
      (held.length ? '<div class="hint">Held back most by: ' + held.map(
        function (h) { return escapeHtml(h[0]) + ' (' + h[1] + ')'; })
        .join('; ') + '</div>' : '') +
      (rows ? '<table class="aw-bt-trades"><tr><th>Entered</th><th>Side' +
        '</th><th>z</th><th>Exit</th><th>P&amp;L</th></tr>' + rows +
        '</table>' : '') +
      '<div class="hint" title="' + escapeHtml((data.caveats || []).join('; ')) +
      '">' + (s.candles || 0) + ' candles of OKX history, ' + when(s.from) +
      ' \u2013 ' + when(s.to) + ' \u00b7 hover for what a backtest cannot see' +
      '</div>';
  }

  function algoExitWords(reason) {
    return {STOP_LOSS: 'stop loss', PROFIT_TARGET: 'target',
            Z_STOP: 'z-stop', MEAN_REVERSION: 'mean', TIME_STOP: 'time stop'
           }[reason] || reason || '';
  }

  function moneyOr(value) {
    return value === null || value === undefined ? DASH : money(value);
  }

  function smallMoney(x) {
    if (x === null || x === undefined) { return DASH; }
    var n = Number(x);
    return (n < 0 ? '\u2212$' : '$') + (Math.abs(n) < 1
      ? Math.abs(n).toFixed(4) : Math.abs(n).toFixed(2));
  }

  function kv(label, value, cls, title, wide) {
    return '<div class="aw-kv' + (wide ? ' wide' : '') + '"' +
      (title ? ' title="' + escapeHtml(title) + '"' : '')
      + '><span>' + label + '</span><b' + (cls ? ' class="' + cls + '"' : '')
      + '>' + value + '</b></div>';
  }

  function badge(text, tone, title) {
    return '<span class="aw-badge ' + tone + '"' +
      (title ? ' title="' + escapeHtml(title) + '"' : '') + '>' + text + '</span>';
  }

  function algoLegsHtml(row) {
    /* "A USOIL_F7 · #19514405 · CFI Financial Group", and B's. The
     * login and broker are the terminal's own, read live; a leg that
     * could not be read says so rather than showing nothing. */
    var accounts = (state.snapshot && state.snapshot.accounts) || {};
    function leg(letter, account, symbol) {
      var info = accounts[account];
      var who = info
        ? '#' + escapeHtml(info.login || DASH) + ' \u00b7 ' +
          escapeHtml(info.company || info.server || DASH)
        : '<span class="down">not read</span>';
      return '<div class="aw-leg" title="Leg ' + letter + ': account \u2018' +
        escapeHtml(account || '?') + '\u2019' +
        (info && info.server ? ', server ' + escapeHtml(info.server) : '') +
        '"><b>' + letter + '</b> ' + escapeHtml(symbol || '?') + ' \u00b7 ' +
        who + '</div>';
    }
    return leg('A', row.account_a, row.symbol_a) +
      leg('B', row.account_b, row.symbol_b);
  }

  function marginShare(position) {
    /* " (-8.05% margin)": the P&L over what BOTH legs tied up. */
    var back = positionReturns(position, position.net_pnl);
    if (back.pct_margin === null) { return ''; }
    return ' <small>(' + pctSigned(back.pct_margin, 2) + ' margin' +
      (back.estimated ? ', est.' : '') + ')</small>';
  }

  function limitLine(row) {
    /* The position limit, said where the position is read: how many
     * are on, how many working orders would open another, and the most
     * this ladder may hold. Only shown once it matters. */
    var limit = row.max_positions;
    if (!limit) { return ''; }
    var held = (row.positions || []).length;
    var openers = (row.orders || []).filter(function (o) {
      return !o.position_id &&
        (o.state === 'WORKING' || o.state === 'FILLING');
    }).length;
    if (held + openers < limit) { return ''; }
    var what = openers && held
      ? held + ' on + ' + openers + ' working add' + (openers > 1 ? 's' : '') +
        ' = ' + (held + openers) + ' of ' + limit + '.'
      : openers ? openers + ' working order' + (openers > 1 ? 's' : '') +
        ' that would open = ' + openers + ' of ' + limit + '.'
        : held + ' of ' + limit + ' positions on \u2014 the limit.';
    return '<div class="aw-limit">' + '<b>' + what + '</b> A new order that ' +
      'would OPEN is refused. Closes always work.</div>';
  }

  function fillsHtml(key, first, digits) {
    /* The fills behind a combined position: folded, one click away. */
    if (!first.combined) { return ''; }
    state.fillsOpen = state.fillsOpen || {};
    var fills = first.fills || [];
    var now = Date.now() / 1000;
    var rows = fills.map(function (f, i) {
      var age = f.opened_at ? Math.max(0, Math.floor((now - f.opened_at) / 60))
        + 'm ago' : DASH;
      var pnl = f.net_pnl;
      return '<span>#' + (i + 1) + ' ' + escapeHtml(first.side || '') + ' ' +
        fmt(f.quantity, 2).replace(/\.?0+$/, '') + '</span><span>@ ' +
        fmt(f.entry_spread, digits) + '</span><span>' + age + '</span>' +
        '<span class="' + (pnl === null || pnl === undefined ? ''
          : pnl >= 0 ? 'up' : 'down') + '">' + moneyOr(pnl) + '</span>';
    }).join('');
    return '<details class="aw-fills" data-key="' + escapeHtml(key) + '"' +
      (state.fillsOpen[key] ? ' open' : '') + '><summary>Fills (' +
      fills.length + ') \u2014 combined above</summary>' +
      '<div class="aw-fill-rows">' + rows + '</div></details>';
  }

  function coverageHtml(first) {
    /* A working close that covers only part of the whole position. */
    if (!first.combined) { return ''; }
    var covered = first.close_covered || 0;
    var total = first.quantity || 0;
    if (!(covered > 1e-9) || covered >= total - 1e-9) { return ''; }
    var rest = total - covered;
    return '<div class="aw-cover"><b>Working close covers ' +
      fmt(covered, 2).replace(/\.?0+$/, '') + ' of ' +
      fmt(total, 2).replace(/\.?0+$/, '') + '.</b> The other ' +
      fmt(rest, 2).replace(/\.?0+$/, '') + ' has no close order.</div>';
  }

  function addSizeWords(params) {
    /* "17.25 " - the Qty an add would go on at, from its own size or the
     * trade's; "(size not set) " when it has one of its own that is 0. */
    var qty = (params.add_size_by || 'SAME') === 'SAME' ? params.algo_qty
      : params.add_qty;
    if (qty === undefined) { return ''; }
    return qty > 0 ? fmt(qty, 2) + ' ' : '(size not set) ';
  }

  function addsHtml(adds, block, params, digits) {
    /* The ADDS: where each went on and what it is worth now. Averaged
     * with the trade (the usual case), its levels are the shared ones
     * above, so only its fill is said here. */
    return adds.map(function (p, i) {
      if (p.averaged) {
        return kv('Add ' + (i + 1), 'in @ ' + fmt(p.entry_spread, digits) +
          ' \u00b7 ' + fmt(p.quantity, 2) + ' \u00b7 net ' +
          '<span class="' + (p.net_pnl === null || p.net_pnl === undefined
            ? '' : (p.net_pnl >= 0 ? 'up' : 'down')) + '">' +
          moneyOr(p.net_pnl) + '</span> \u00b7 averaged: shares the ' +
          'levels above', '', 'an ADD, averaged with the trade');
      }
      return kv('Add ' + (i + 1), 'in @ ' + fmt(p.entry_spread, digits) +
        ' \u00b7 ' + fmt(p.quantity, 2) + ' \u00b7 BE ' +
        fmt(p.break_even, digits) + ' \u00b7 TP ' +
        (p.tp === null || p.tp === undefined ? DASH
          : levelWords(p.tp, p.tp_money, digits)) + ' \u00b7 SL ' +
        (p.sl === null || p.sl === undefined
          ? (params.stop_loss_on ? DASH : 'off')
          : levelWords(p.sl, p.sl_money, digits)) +
        meanExitWords(p, block.mean, params, digits) + ' \u00b7 net ' +
        '<span class="' + (p.net_pnl === null || p.net_pnl === undefined
          ? '' : (p.net_pnl >= 0 ? 'up' : 'down')) + '">' +
        moneyOr(p.net_pnl) + '</span>', '',
        'an ADD: its own trade, with its own break-even, target, stop ' +
        'and mean exit, from its own fill');
    }).join('');
  }

  function meanExitWords(pos, mean, params, digits) {
    /* BACK TO MEAN, where it is on: the mean as a fourth level, and
     * which of the exits it is. The Algo takes it when the CLOSING price
     * reaches the mean at or past break-even - so a mean short of the TP
     * is where the trade comes off first, and a mean not yet past
     * break-even is not an exit at all (the trade holds for TP or SL). */
    if (!params.reversion_on) { return ''; }
    if (mean === null || mean === undefined) { return ' \u00b7 Mean ' + DASH; }
    var sign = pos.side === 'SELL' ? -1 : 1;
    var be = pos.break_even, tp = pos.tp;
    var note;
    if (be !== null && be !== undefined && sign * (mean - be) < 0) {
      note = 'not past BE: holds for TP / SL';
    } else if (tp !== null && tp !== undefined && sign * (mean - tp) >= 0) {
      note = 'past TP: TP first';
    } else {
      note = 'exits here first';
    }
    return ' \u00b7 <b>Mean ' + fmt(mean, digits) + '</b> <small>(' + note +
      ')</small>';
  }

  function renderAlgoWindow(key, row) {
    /* SIGNAL & POSITION, STATISTICS, FILTERS — the stat-arb dashboard's
     * three panels, for one ladder, from the engine's Algo block. */
    var node = algoNode(key);
    var block = row.algo_block || {};
    var params = block.params || {};
    var filters = block.filters || {};
    var digits = digitsFor(row.increment);
    var live = block.mode === 'LIVE';
    node.querySelector('.aw-pair').textContent = (row.name || key) + ' · Algo';
    var mode = node.querySelector('.aw-mode');
    mode.textContent = live ? 'LIVE' : 'SIGNALS';
    mode.className = 'aw-mode ' + (live ? 'live' : 'dry');
    mode.title = live
      ? 'the Algo TRADES this ladder — manual orders on it are off'
      : 'Signals: the Algo signals, you trade — nothing is sent';
    var market = row.market || {};
    var entryZ = params.entry_z;

    // Each leg's account number and broker, small, across the top.
    node.querySelector('.aw-legs').innerHTML = algoLegsHtml(row);

    // -- SIGNAL & POSITION ------------------------------------------------
    var direction = params.direction || 'BOTH';
    function tile(side) {
      var sell = side === 'SELL';
      var z = sell ? block.z_sell : block.z_buy;
      var hit = z !== null && z !== undefined && entryZ &&
        (sell ? z >= entryZ : z <= -entryZ);
      // This ladder may be set to enter one way only. The other tile is
      // still drawn — it is how the spread is read — but it says so.
      var off = (sell && direction === 'L_TO_H') ||
        (!sell && direction === 'H_TO_L');
      return '<div class="aw-tile ' + (sell ? 'sell' : 'buy') +
        (hit ? ' hit' : '') + (off ? ' off' : '') + '" title="' + (sell
          ? 'What SELLING the spread gets now (the bid side). The Algo '
            + 'sells when this z reaches +' + entryZ + ', and closes a long here.'
          : 'What BUYING the spread costs now (the offer side). The Algo '
            + 'buys when this z reaches \u2212' + entryZ + ', and closes a short here.')
        + '"><div class="aw-tile-head">' + (sell ? 'H to L' : 'L to H') +
        (off ? ' <small>(entries off)</small>' : '') +
        '</div><div class="aw-tile-price">' +
        fmt(sell ? market.short_spread : market.long_spread, digits) +
        '</div><div class="aw-tile-z" title="How stretched the spread is: ' +
        'entry at \u00b1' + (entryZ || '?') + '">' + signed(z) + '</div>' +
        entryLine(sell) + '</div>';
    }
    function entryLine(sell) {
      /* Where this side enters. On the TOUCH of the band; or — with
       * re-entry — armed at the band and entered on the way back in,
       * which a spread riding the band in a trend never gives. */
      if (!params.reentry_on) {
        return '<div class="aw-tile-entry">' + (sell ? 'short' : 'long') +
          ' at ' + (sell ? '\u2265 +' : '\u2264 \u2212') + (entryZ || '?') +
          ' (' + fmt(sell ? block.upper : block.lower, digits) + ')</div>';
      }
      var inZ = Math.max(0, (entryZ || 0) - (params.reentry_back || 0));
      // The window ends this far back: past it the side disarms.
      var windowPct = params.reentry_window_pct === undefined ||
        params.reentry_window_pct === null ? 50 : params.reentry_window_pct;
      var outZ = inZ * (1 - windowPct / 100);
      // Each end of the window as a PRICE, beside its own z.
      var priced = !(block.mean === null || block.mean === undefined ||
                     !block.sigma);
      var level = priced ? block.mean + (sell ? 1 : -1) * inZ * block.sigma
        : null;
      var outLevel = priced
        ? block.mean + (sell ? 1 : -1) * outZ * block.sigma : null;
      var armed = (block.armed || {})[sell ? 'SELL' : 'BUY'];
      return '<div class="aw-tile-entry" title="Armed when the stretch ' +
        'reaches ' + (sell ? '+' : '\u2212') + entryZ + '; entered on the ' +
        'way back in, between ' + (sell ? '+' : '\u2212') + inZ.toFixed(2) +
        ' and ' + (sell ? '+' : '\u2212') + outZ.toFixed(2) + '. Past ' +
        outZ.toFixed(2) + ' it disarms - too close to the mean to trade.">' +
        (armed ? '<b class="aw-armed">ARMED</b> ' : 'arm ' +
         (sell ? '\u2265 +' : '\u2264 \u2212') + entryZ + ' \u00b7 ') +
        (sell ? 'short' : 'long') + ' back at ' + (sell ? '+' : '\u2212') +
        inZ.toFixed(2) + ' (' + fmt(level, digits) + ') to ' +
        (sell ? '+' : '\u2212') + outZ.toFixed(2) +
        ' (' + fmt(outLevel, digits) + ')</div>';
    }
    var held = block.positions || [];
    var first = held[0] || null;
    var position = first
      ? (first.side === 'BUY' ? 'LONG' : 'SHORT') + ' ' +
        held.map(function (p) { return fmt(p.quantity, 2); }).join(' + ')
      : 'FLAT';
    var html = '<div class="aw-head">Signal &amp; Position</div>' +
      '<div class="aw-tiles">' + tile('SELL') + tile('BUY') +
      '<div class="aw-pos ' + (first ? (first.side === 'BUY' ? 'long' : 'short')
        : 'flat') + '">' + position + '</div></div>' +
      '<div class="aw-line" title="' + escapeHtml(block.blocked || '') + '">' +
      escapeHtml(algoLine(block, digits)) +
      (block.health && block.state !== 'BLOCKED' ? ' — ' + escapeHtml(block.health)
        : '') + '</div>' + limitLine(row);
    if (first) {
      var delta = (first.closing_spread === null ||
                   first.closing_spread === undefined ||
                   first.entry_spread === null)
        ? null : first.closing_spread - first.entry_spread;
      var good = delta === null ? '' :
        ((first.side === 'BUY' ? delta >= 0 : delta <= 0) ? 'up' : 'down');
      function legRow(leg) {
        var entry = first['leg_' + leg + '_entry'];
        var now = first['leg_' + leg + '_now'];
        var change = (entry === null || entry === undefined ||
                      now === null || now === undefined) ? null : now - entry;
        var lots = first['leg_' + leg + '_volume'];
        return kv('Leg ' + leg.toUpperCase() + ' (' +
                  (first['leg_' + leg + '_side'] || '?') +
                  (first.combined && lots ? ' ' + fmt(lots, 2)
                    .replace(/\.?0+$/, '') : '') + ')',
                  fmt(entry, 2) + (first.combined ? ' avg' : '') +
                  ' \u2192 ' + fmt(now, 2) +
                  (change === null ? '' : ' <small>(' +
                    (change > 0 ? '+' : '') + fmt(change, 2) + ')</small>'),
                  '', 'the leg\u2019s fill \u2192 the price it would close at now',
                  true);
      }
      html += '<div class="aw-grid">' +
        (first.combined
          ? kv('Avg entry', fmt(first.entry_spread, digits) + ' <small>(' +
               (first.fills || []).length + ' fills)</small>', '',
               'the fills read as ONE position: their average, weighted '
               + 'by size; each fill is listed under Fills')
          : kv('Entry spread', fmt(first.entry_spread, digits))) +
        kv('\u0394 spread', delta === null ? DASH
          : (delta > 0 ? '+' : '') + fmt(delta, digits), good,
          'closing price now \u2212 entry: a LONG makes money when this is '
          + 'positive, a SHORT when it is negative') +
        kv('Entry z', signed(first.entry_z)) +
        // Mirror's "(-8.05% margin)" where the entry margin is known;
        // otherwise the engine's own shares of notional and margin.
        kv('Net P&amp;L', moneyOr(first.net_pnl) + (marginShare(first) ||
             shareWords(first.pnl_pct_notional, first.pnl_pct_margin)),
           first.net_pnl === null || first.net_pnl === undefined ? ''
             : (first.net_pnl >= 0 ? 'up' : 'down'),
           'net P&L as a share of both legs\u2019 notional at entry, and '
           + 'of the margin the position ties up (the base TP % and SL % '
           + 'are set on)') +
        legRow('a') + legRow('b') +
        kv('Age', first.age_sec === null || first.age_sec === undefined ? DASH
          : Math.floor(first.age_sec / 60) + 'm ' +
            Math.floor(first.age_sec % 60) + 's' +
            (first.combined ? ' <small>(oldest)</small>' : '')) +
        kv('Size', fmt(first.quantity, 2) + ' spread(s)' +
           sizeDollars(row, first.quantity), '',
           'both legs\u2019 value at the mid now, for this size') +
        '</div>' +
        // AVERAGED with an add: one break-even, target and stop for
        // the whole, and what each is worth to the whole.
        (first.averaged ? kv('Average', fmt(first.averaged.entry, digits) +
           ' \u00b7 ' + fmt(first.averaged.quantity, 2) + ' spread(s) in ' +
           first.averaged.fills + ' fills \u00b7 net ' +
           moneyOr(first.averaged.net_pnl), '',
           'the trade and its add, averaged: they share one break-even, ' +
           'target and stop, and come off together') : '') +
        kv('Levels', 'BE ' + fmt(first.break_even, digits) + ' · TP ' +
           (first.tp === null || first.tp === undefined ? DASH
             : levelWords(first.tp, first.averaged
                 ? first.averaged.tp_money : first.tp_money, digits)) +
           ' · SL ' +
           (first.sl === null || first.sl === undefined
             ? (params.stop_loss_on ? DASH : 'off')
             : levelWords(first.sl, first.averaged
                 ? first.averaged.sl_money : first.sl_money, digits)) +
           meanExitWords(first, block.mean, params, digits) +
           (first.stop_mode === 'ATR' || first.target_mode === 'ATR'
             ? ' <small>(ATR ' + fmt(first.entry_atr, digits) + ' at entry: ' +
               (first.target_mode === 'ATR' ? 'TP ' + (first.atr_target_mult || params.atr_target_mult) + '\u00d7' : 'TP %') +
               ', ' + (first.stop_mode === 'ATR' ? 'SL ' + (first.atr_stop_mult || params.atr_stop_mult) + '\u00d7' : 'SL %') +
               ')</small>' : ''),
           '', 'compare with the CLOSING price: the Sell spread for a LONG, '
           + 'the Buy spread for a SHORT. In brackets: the net P&L of the '
           + 'whole position closing there, after every cost') +
        addsHtml(held.slice(1), block, params, digits) +
        fillsHtml(key, first, digits) + coverageHtml(first) +
        (params.progress_bar === false ? '' : progressHtml(first, digits));
    }
    node.querySelector('.aw-signal').innerHTML = html;

    // -- STATISTICS ---------------------------------------------------------
    var history = block.history || {};
    var regime = filters.regime || {};
    var count = block.count || 0;
    var needed = block.needed || params.length || 0;
    var pct = needed ? Math.min(100, Math.round(count * 100 / needed)) : 0;
    var regimeText = regime.state === 'TRENDING'
      ? 'Trend ' + ((regime.slope || 0) > 0 ? '\u2191' : '\u2193')
      : regime.state === 'RANGE' ? 'Mean-rev'
        : regime.state === 'COLLECTING' ? 'Collect' : DASH;
    node.querySelector('.aw-stats').innerHTML =
      '<div class="aw-head">Statistics</div><div class="aw-grid">' +
      kv('Mean (EMA)', fmt(block.mean, digits)) +
      kv('Std dev', fmt(block.sigma, digits)) +
      kv('ATR(' + (block.atr_period || params.atr_period || 14) + ')',
         fmt(block.atr, digits), '',
         'the average size of one candle\u2019s move in the spread, close ' +
         'to close (Wilder). In ATR mode the stop and target are ' +
         'multiples of it, frozen when the trade opens') +
      kv('Half-life', filters.half_life_minutes === null ||
         filters.half_life_minutes === undefined ? DASH
         : Math.round(filters.half_life_minutes) + ' min', '',
         'how long a stretch takes to halve, from the candles (AR(1))') +
      kv('Regime', regimeText, regime.state === 'TRENDING' ? 'down'
         : (regime.state === 'RANGE' ? 'up' : ''),
         regime.efficiency_ratio === null || regime.efficiency_ratio === undefined
           ? '' : 'efficiency ' + regime.efficiency_ratio.toFixed(2) + ', ' +
             regime.crossings + ' mean crossings') +
      btcDominanceKv() +
      kv('Candles', (block.timeframe_min || params.timeframe_min) + 'm \u00d7 ' +
         needed) +
      kv('Band', fmt(block.lower, digits) + ' \u2026 ' + fmt(block.upper, digits)) +
      '</div><div class="aw-data" title="' + escapeHtml(history.note || '') + '">' +
      '<span>Data</span><div class="aw-bar"><div class="' +
      (block.ready ? 'ok' : 'wait') + '" style="width:' +
      (block.ready ? 100 : pct) + '%"></div></div><span>' +
      (block.ready ? 'candles ready' : count + '/' + needed + ' candles') +
      '</span></div>' + warmupHtml(block.warmup) +
      algoMarginHtml(row, filters.qty || params.algo_qty || 1);

    // -- FILTERS -----------------------------------------------------------
    var edge = filters.edge || {};
    var cost = filters.cost || {};
    var band = filters.half_life_band || [0, 0];
    function onOff(on, ok, yes, no, why) {
      if (!on) { return badge('OFF', 'off', why); }
      if (ok === null || ok === undefined) { return badge('\u2014', 'wait', why); }
      return badge(ok ? yes : no, ok ? 'ok' : 'bad', why);
    }
    var trending = regime.state === 'TRENDING';
    var day = block.day || {};
    var last = block.last_blocked;
    // The round trip's parts: the size, a point of spread in money at
    // it, and OKX's own fee rate on each leg (maker under the W3 fill).
    var algoQty = filters.qty || params.algo_qty || 1;
    var legsNow = row.leg_notional || {};
    var marketNow = row.market || {};
    var midB = mid(marketNow.leg_b_bid, marketNow.leg_b_ask);
    var pointMoney = legsNow.b && midB ? legsNow.b / midB * algoQty : null;
    var tripPts = pointMoney && cost.total !== null && cost.total !== undefined
      ? cost.total / pointMoney : null;
    var feeKind = row.fill_style === 'taker' ? 'taker' : 'maker';
    var feeRate = (function () {
      var f = row.fee_bps || {};
      var a = (f.a || {})[feeKind], b = (f.b || {})[feeKind];
      return a === undefined || b === undefined ? null : {a: a, b: b};
    })();
    function legFee(leg) {
      var value = legsNow[leg];
      return !feeRate || value === null || value === undefined ? null
        : value * algoQty * feeRate[leg] / 1e4 * 2;
    }
    node.querySelector('.aw-filters').innerHTML =
      '<div class="aw-head">Filters</div><div class="aw-badges">' +
      '<div>' + onOff(edge.on, edge.ok, '\u2713', '\u2717',
        'expected capture against the round-trip cost, at the entry z') +
      '<small>Edge</small></div>' +
      '<div>' + (regime.on ? badge(trending ? 'TR' : (regime.state === 'RANGE'
          ? 'MR' : 'WAIT'), trending ? 'bad' : (regime.state === 'RANGE'
          ? 'ok' : 'wait')) : badge('OFF', 'off')) +
      '<small>Regime</small></div>' +
      '<div>' + trendBadge(filters.trend || {}) + '<small>Trend</small></div>' +
      '<div>' + badge(filters.ready ? 'YES' : 'NO', filters.ready ? 'ok' : 'wait')
      + '<small>Ready</small></div>' +
      ((band[0] || band[1]) ? '<div>' + badge(
        Math.round(band[0] || 0) + '\u2013' + (band[1] ? Math.round(band[1]) : '\u221e'),
        'off') + '<small>HL min</small></div>' : '') +
      '</div><div class="aw-grid aw-margin">' +
      // ONE ROW PER LINE, as Margin & leverage: the size and what the
      // trade costs, the round trip broken into its parts under it.
      kv('Size', fmt(algoQty, 2) + ' spread(s)' +
         sizeDollars(row, algoQty), '',
         'one Algo trade: both legs’ value at the mid now', true) +
      kv('Capture / cost', (edge.ratio === null || edge.ratio === undefined
          ? DASH : edge.ratio.toFixed(2) + '×') + ' / req ' +
          (edge.required === undefined ? DASH : edge.required + '×'),
         edge.ok === true ? 'up' : (edge.ok === false ? 'down' : ''),
         'expected capture ' + moneyOr(edge.capture) +
         ' (' + (params.edge_capture_frac || 0.5) + ' × entry z × '
         + 'σ) against the round trip, at the entry z', true) +
      kv('Round trip', moneyOr(cost.total) +
         (tripPts === null ? '' : ' · ' + fmt(tripPts, 2) + ' pts'),
         '', 'what one Algo trade costs in and out: the fees below plus ' +
         'the bid-ask it crosses' + (tripPts === null ? ''
           : ', ' + fmt(tripPts, 2) + ' points of spread at ' +
             money(pointMoney) + ' a point'), true) +
      kv('  Fees', smallMoney(cost.commission) +
         (feeRate ? ' · ' + fmt(feeRate.a, 1) + ' bp × 2 legs ' +
           '× in + out' : ''), '',
         'OKX’s ' + feeKind + ' fee on BOTH legs, on the way in AND ' +
         'the way out' + (legFee('a') === null ? '' : ': leg A ' +
           smallMoney(legFee('a')) + ' + leg B ' + smallMoney(legFee('b'))) +
         '. It grows with the size; only a lower VIP rate makes it ' +
         'smaller per dollar', true) +
      kv('  Bid-ask', smallMoney(cost.crossing), '',
         'crossing the spread’s own bid-ask once, at this size', true) +
      (cost.slippage ? kv('  Slippage allowance',
         smallMoney(cost.slippage), '', 'the allowance set on this ' +
         'ladder, per spread x size: a buffer, not an OKX charge', true) : '') +
      kv('Today', (day.trades || 0) + (params.max_trades_day
          ? '/' + params.max_trades_day : '') + ' trades · ' +
          (day.losses_row || 0) + ' in a row · ' + moneyOr(day.pnl),
         '', 'the day’s limits stop entries, never exits', true) +
      '</div><div class="aw-blocked"><div class="aw-head2">Last signal blocked'
      + '</div>' + (last
        ? '<div><b>' + escapeHtml(sideWords(last.side)) + '</b>' +
          (last.spread === null || last.spread === undefined ? ''
            : ' @ ' + fmt(last.spread, 4)) + ' z ' +
          signed(last.z) + ' · ' +
          new Date(last.at * 1000).toLocaleTimeString() + '</div><div>' +
          escapeHtml(last.reason || '') + '</div>'
        : '<div class="hint">none yet</div>') + '</div>' +
      lastOrderHtml(block);
  }

  function trendBadge(trend) {
    /* Which way the band's middle has moved over the lookback: up
     * blocks H to L, down blocks L to H, flat blocks neither. */
    if (!trend.on) { return badge('OFF', 'off'); }
    var drift = trend.drift_sigma;
    var why = drift === null || drift === undefined
      ? 'not enough candles to measure the trend yet'
      : 'the middle moved ' + (drift >= 0 ? '+' : '') + drift.toFixed(2) +
        '\u03c3 in ' + trend.lookback_min + ' min (limit ' + trend.limit +
        '\u03c3)';
    if (trend.state === 'UP') { return badge('\u2191', 'bad', why + ' \u2014 no H to L'); }
    if (trend.state === 'DOWN') { return badge('\u2193', 'bad', why + ' \u2014 no L to H'); }
    if (trend.state === 'FLAT') { return badge('\u2013', 'ok', why); }
    return badge('WAIT', 'wait', why);
  }

  function sideWords(side) {
    return side === 'SELL' ? 'H to L' : (side === 'BUY' ? 'L to H' : side);
  }

  function lastOrderHtml(block) {
    /* What the Algo last DID, and what became of it. A refused order
     * used to leave no trace on the screen at all — "Last signal
     * blocked: none yet" over an Algo that had tried, and been refused,
     * every time. The refusal is shown in its own words. */
    var last = (block.recent || [])[0];
    var html = '<div class="aw-blocked aw-last-order"><div class="aw-head2">'
      + 'Last order</div>';
    if (!last) { return html + '<div class="hint">none yet</div></div>'; }
    var what = escapeHtml(last.action || '') + ' ' +
      escapeHtml(sideWords(last.side)) +
      (last.spread === null || last.spread === undefined ? ''
        : ' @ ' + fmt(last.spread, 4)) +
      (last.z === null || last.z === undefined ? '' : ' z ' + signed(last.z)) +
      ' · ' + new Date(last.at * 1000).toLocaleTimeString();
    var outcome;
    if (last.mode !== 'LIVE') {
      outcome = '<div class="hint">signal — nothing sent</div>';
    } else if (last.done) {
      outcome = '<div class="up">sent — done</div>';
    } else {
      outcome = '<div class="down">REFUSED — ' +
        escapeHtml(last.result || 'no reason given') + '</div>';
    }
    return html + '<div><b>' + what + '</b></div>' + outcome + '</div>';
  }

  function renderFair(node, row, algoShown) {
    /* What the CARRY says this basis should be — both directions.
     *
     * Two columns, because buying the spread pays the offer and
     * selling it receives the bid, and those are charged different
     * swaps on different legs. One number here would be right half the
     * time, and the gap would be measured against a price nobody
     * fills at.
     *
     * Never an instruction. Nothing here places or withholds an order.
     */
    var fair = row.fair || {};
    var buy = node.querySelector('.fair-buy');
    if (!buy) { return; }          // a page from an older template
    var digits = digitsFor(row.increment);
    if (!algoShown) {
      node.querySelector('.fair-buy').textContent = fmt(fair.fair_buy, digits);
      node.querySelector('.fair-sell').textContent = fmt(fair.fair_sell,
                                                         digits);
      node.querySelector('.fair-buy').className = 'fair-buy';
      node.querySelector('.fair-sell').className = 'fair-sell';
      node.querySelector('.fair').title = 'What the carry says this basis '
        + 'should be: the broker\'s own swap over the nights to expiry, '
        + 'divided by k. A reference reading — it feeds nothing.';
    }
    // Rich = the market is above what the carry justifies, which is the
    // side a trader sells. Said in colour as well as sign: the
    // direction of a basis is the thing everyone gets backwards once.
    // THE KEYPAD OFFERS ONLY WHAT CAN BE TRADED.
    //
    // A ladder whose broker caps leg A at 10 lots was showing 50 and
    // 100 buttons, and pressing one got a refusal back — "Qty 100 x 1
    // wants 100 lots on leg A, over this broker's 10-lot maximum". The
    // arithmetic was right and the button should not have been there.
    var cap = (row.max_qty === null || row.max_qty === undefined)
      ? null : Number(row.max_qty);
    node.querySelectorAll('.keypad .qty').forEach(function (button) {
      var size = Number(button.dataset.qty);
      var beyond = cap !== null && button.dataset.qty && size > cap;
      button.disabled = !!beyond || algoTrades(row);
      button.title = beyond
        ? 'Qty ' + size + ' is over what this pair can trade — it tops '
          + 'out at ' + cap
        : '';
    });

    // With the Algo on, these rows are its band — drawn by renderAlgo.
    (algoShown ? [] : ['buy', 'sell']).forEach(function (side) {
      var cell = node.querySelector('.gap-' + side);
      var gap = fair['gap_' + side];
      if (gap === null || gap === undefined) {
        cell.textContent = '—';
        cell.className = 'gap-' + side;
        cell.title = '';
        return;
      }
      cell.textContent = (gap > 0 ? '+' : '') + fmt(gap, digits);
      cell.className = 'gap-' + side + ' ' + (gap > 0 ? 'down' : 'up');
      cell.title = gap > 0
        ? 'the market is RICH to its own carry here'
        : 'the market is CHEAP to its own carry here';
    });
    if (!algoShown) {
      // The rail is 100px wide: a sentence does not fit in it. The short
      // form is shown; the engine's full wording is the tooltip.
      var note = node.querySelector('.fair-note');
      var expiring = fair.days_to_expiry;
      note.textContent = fair.fair_buy === null || fair.fair_buy === undefined
        ? (fair.expects_expiry === false ? '' : 'set expiry + swap')
        : (expiring === null || expiring === undefined
            ? '' : expiring + 'd to expiry');
      note.title = fair.note || '';

      // A swap that disagrees with an annual rate — or a long leg showing
      // a credit — REPLACES the reading rather than printing beneath it.
      var warn = node.querySelector('.fair-warn');
      var fix = node.querySelector('.fair-fix');
      if (!warn) { return; }
      warn.hidden = !fair.warning;
      node.querySelector('.fair-warn-text').textContent = fair.warning || '';
      fix.hidden = !fair.fix;
      if (fair.fix) {
        // Named field, named value, ONE click — and still an explicit
        // action. A sign the engine flipped by itself is a sign nobody
        // would ever notice was wrong.
        fix.textContent = 'set ' + fair.fix.field.replace(/_/g, ' ') +
          ' = ' + fmt(fair.fix.value, 2);
        fix.onclick = function () { applyCarryFix(row.key, fair.fix); };
      }
    }
    // The other two things that decide where a position ends, beside
    // the two figures that price it: read-only here, because both are
    // set in the ladder's own settings.
    var overnight = node.querySelector('.x-overnight');
    if (overnight) {
      // Read-only here: the overnight rule is EXIT logic and lives in
      // this ladder's settings, beside the other two things that decide
      // where a position ends.
      overnight.textContent = OVERNIGHT_WORDS[row.overnight] || row.overnight;
      overnight.title = 'what happens to an OPEN POSITION at the session '
        + 'cutoff — change it in this ladder\u2019s settings';
    }
    renderExit(node, row);
  }

  function applyCarryFix(key, fix) {
    /* Correct one swap field, on the operator's click. */
    var body = {};
    body[fix.field] = fix.value;
    fetch('/api/pairs/' + encodeURIComponent(key), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (answer) {
      if (answer && answer.ok) {
        toast(fix.field.replace(/_/g, ' ') + ' set to ' + fmt(fix.value, 2),
          'ok');
      } else {
        toast('could not save: ' + ((answer && answer.error) || 'unknown'));
      }
    }).catch(function (e) { toast('could not save: ' + e); });
  }

  function renderExit(node, row) {
    /* Where a trade entered NOW gets out: break-even first, then
     * break-even plus the target.
     *
     * Two columns because a spread has two directions and their exits
     * move opposite ways: a long leaves on the bid ABOVE what it paid,
     * a short buys back BELOW what it sold. One number here would be
     * right half the time.
     *
     * A position that is already on gets its own line, anchored on the
     * price it was ENTERED at — a take-profit that moves with the
     * market is not a take-profit.
     */
    var exit = row.exit || {};
    if (!node.querySelector('.be-buy')) { return; }   // older template
    // The LADDER's own precision, not four decimals: the rail is 100px
    // wide and two seven-character numbers side by side collide into
    // one unreadable string. A price is shown here the way it is shown
    // in the price column.
    var digits = digitsFor(row.increment);
    // What the exit is BUILT from, in the order it is built: the round
    // turn the market charges, the commission the broker charges, the
    // profit the target asks for — then break-even and the target
    // price themselves.
    var width = node.querySelector('.x-width');
    width.textContent = fmt(exit.spread_width, digits);
    // One column, one basis: the spread figures are in spread points
    // and the money is in money, and `k` is what converts them. Both
    // units are on the row, because a bare dollar total cannot be
    // checked against a ladder quoted in spread.
    width.title = exit.spread_width_money === null ||
      exit.spread_width_money === undefined
      ? 'one round turn of both legs\u2019 bid-ask, measured live'
      : 'one round turn of both legs\u2019 bid-ask = ' +
        money(exit.spread_width_money) + ' at this size. Already inside ' +
        'the two prices, so it is not added again.';
    node.querySelector('.x-comm').textContent =
      exit.commission === null || exit.commission === undefined
        ? DASH : money(exit.commission);
    // Two terms that are ZERO by default and shown only when they are
    // in play: an allowance nobody set and a swap over no nights are
    // rows that say nothing, and the rail is 100px wide.
    showTerm(node, '.x-slip', exit.slippage_allowance,
      'a BUDGET for slippage, not a measurement — the realised figure ' +
      'is in the slippage report');
    showTerm(node, '.x-swap', exit.swap_money,
      exit.nights ? exit.nights + ' night(s) of swap, both legs, signed: ' +
        'a credit reduces what has to be recovered'
        : 'set BREAK_EVEN_NIGHTS to price a holding period');
    // Borrow interest belongs to ONE direction - the one that sells a
    // spot leg - so it is shown with which.
    var borrowB = exit.borrow_money, borrowS = exit.borrow_money_sell;
    var borrowInfo = exit.borrow || exit.borrow_sell || {};
    showTerm(node, '.x-borrow', borrowB || borrowS || null,
      'interest on ' + (borrowInfo.ccy || 'the coin') + ' borrowed to sell ' +
      'the spot leg, ' + (borrowInfo.hours || 1) + ' hour(s) at OKX\u2019s ' +
      'hourly rate' + (borrowInfo.rate_hourly
        ? ' (' + (borrowInfo.rate_hourly * 100).toFixed(5) + '%/h)' : '') +
      ' \u2014 only ' + (borrowB ? 'buying (B)' : 'selling (S)') +
      ' the spread pays it');
    if (!borrowB && !borrowS) {
      var bcell = node.querySelector('.x-borrow');
      if (bcell) { bcell.parentNode.hidden = true; }
    }
    var target = node.querySelector('.x-target');
    target.textContent = exit.target_money === null ||
      exit.target_money === undefined ? DASH : money(exit.target_money);
    target.title = exit.target_pct
      ? exit.target_pct + '% of ' + money(exit.margin_per_spread) +
        ' margin per spread' +
        (exit.margin_source ? ' (from the ' + exit.margin_source + ')' : '')
      : 'set TP_TARGET_PCT_OF_MARGIN in Settings';
    // How far the target is, in spread, against what the pair actually
    // travels in a session. At small size a percentage of margin can be
    // far outside anything this spread moves — the number is honest,
    // but whether it is REACHABLE is the trader's call.
    var reach = node.querySelector('.x-reach');
    if (reach) {
      var tr = reach.parentNode;
      tr.hidden = exit.target_points === null ||
        exit.target_points === undefined;
      reach.textContent = fmt(exit.target_points, digits) +
        (exit.session_range ? ' / ' + fmt(exit.session_range, digits) : '');
      reach.className = 'x-reach' +
        (exit.target_reachable === false ? ' out-of-range' : '');
      reach.title = exit.target_reachable === false
        ? 'the target is FURTHER than this pair has travelled all ' +
          'session — honest arithmetic, but reaching it is another matter'
        : 'target distance in spread, against the session range';
    }
    // Break-even is quoted on the CLOSING side, which is the opposite
    // side to the one you entered on: a long's is a BID level, a
    // short's is an ASK level. Named, because unlabelled the number
    // reads as "the price now" rather than "the bid you need" — and
    // that is a fault that has already shown +$0.02 on a trade that
    // would have booked -$0.58.
    var beBuy = node.querySelector('.be-buy');
    var beSell = node.querySelector('.be-sell');
    beBuy.textContent = fmt(exit.break_even_buy, digits);
    beSell.textContent = fmt(exit.break_even_sell, digits);
    beBuy.title = 'bid \u2014 a long leaves on the bid, so this is the bid ' +
      'you need back';
    beSell.title = 'ask \u2014 a short buys back at the offer, so this is ' +
      'the ask you need';
    node.querySelector('.tp-buy').textContent = fmt(exit.tp_buy, digits);
    node.querySelector('.tp-sell').textContent = fmt(exit.tp_sell, digits);
    var short = '';
    if (exit.target_pct && exit.margin_per_spread) {
      short = exit.target_pct + '% of ' + money(exit.margin_per_spread);
    } else if (exit.break_even_buy !== null &&
               exit.break_even_buy !== undefined) {
      short = 'B/E only';
    }
    var open = (row.positions || [])[0];
    if (open && open.exit) {
      short = open.side[0] + ' out ' + fmt(open.exit.tp, 4);
    }
    var line = node.querySelector('.exit-note');
    line.textContent = short;
    line.title = (open && open.exit
      ? open.side + ' on: out at ' + fmt(open.exit.tp, 4) + ', flat at ' +
        fmt(open.exit.break_even, 4) + '. '
      : '') + (exit.note || '');
  }

  function showTerm(node, selector, value, why) {
    /* A break-even term that is zero by default: shown only when it is
     * actually in play, and hidden — not zeroed — when it is not. */
    var cell = node.querySelector(selector);
    if (!cell) { return; }
    var tr = cell.parentNode;
    var missing = value === null || value === undefined;
    tr.hidden = missing || value === 0;
    cell.textContent = missing ? DASH : money(value);
    cell.title = why;
    if (missing) { tr.hidden = false; }   // unmeasured is not zero: say so
  }

  function legFeed(row, market) {
    /* The two books, and the spread they make, as a small aligned
     * panel: bid, ask, and the WIDTH each leg is charging.
     *
     * Aligned in fixed columns because these numbers are read by
     * comparing them down the column — a leg whose width has doubled is
     * the leg about to cost money, and that is invisible in a ragged
     * row of figures. The spread's own width is the round turn, which
     * is what the whole ladder is about, so it sits under a rule.
     *
     * Staleness marks the AGE cell only. Painting the whole line red
     * made a quiet market look like a fault.
     */
    if (!market || market.leg_a_bid === undefined) {
      return '<span class="bad">no quote from either leg yet</span>';
    }
    function cell(value, klass, digits) {
      return '<td class="' + klass + '">' +
        fmt(value, digits === undefined ? 4 : digits) + '</td>';
    }
    function width(bid, ask) {
      if (bid === null || bid === undefined ||
          ask === null || ask === undefined) {
        return '<td class="c-width">' + DASH + '</td>';
      }
      return '<td class="c-width">' + fmt(ask - bid, 4) + '</td>';
    }
    function age(seconds) {
      if (seconds === null || seconds === undefined) {
        return '<td class="age c-age">' + DASH + '</td>';
      }
      return '<td class="age c-age' + (seconds > 5 ? ' bad' : '') + '">' +
        seconds.toFixed(1) + 's</td>';
    }
    function line(label, symbol, bid, ask, seconds, visible, stamp) {
      // The tooltip carries what a stale age actually means: this is
      // how long since the QUOTE CHANGED, not since we last asked.
      var note = (symbol || '?') +
        (seconds === null || seconds === undefined ? ''
          : ' — last change ' + seconds.toFixed(1) + 's ago') +
        (visible === undefined || visible === null ? ''
          : (visible ? '. In Market Watch: the terminal is subscribed, so '
                     + 'a frozen age here means it is receiving nothing — '
                     + 'look at this instrument on OKX'
                     : '. NOT in Market Watch — not subscribed. Press '
                     + 'Feed, and add it in the terminal')) +
        (stamp ? '. Broker stamp ' + stamp : '');
      return '<tr><th>' + label + '</th><td class="sym" title="' + note +
        '">' + (symbol || '?') + '</td>' +
        cell(bid, 'c-bid') + cell(ask, 'c-ask') +
        width(bid, ask) + age(seconds) + '</tr>';
    }
    var html = '<table class="legbook"><thead><tr>' +
      '<th></th><th class="sym">leg</th><th class="c-bid">Bid</th>' +
      '<th class="c-ask">Ask</th>' +
      // Each leg's own bid-ask, and on the last line the SPREAD's —
      // which is the two legs' summed, and is one round turn of both
      // books. That sum is the check that makes the panel readable at
      // a glance: 0.0460 + 0.0480 = 0.0940.
      '<th class="c-width" title="the bid-ask each leg is charging — and on the spread row, one round turn of both books">Spread</th>' +
      '<th class="c-age">Age</th></tr></thead><tbody>';
    html += line('A', row.symbol_a, market.leg_a_bid, market.leg_a_ask,
                 market.leg_a_quote_age_sec, market.leg_a_visible,
                 market.leg_a_tick_time);
    html += line('B', row.symbol_b, market.leg_b_bid, market.leg_b_ask,
                 market.leg_b_quote_age_sec, market.leg_b_visible,
                 market.leg_b_tick_time);
    /*
        WHICH WAY EACH SIDE WANTS THE SPREAD TO GO, under the price it
        would be entered at.
    
        The two columns are not two prices of the same thing. The Bid is
        where the spread can be SOLD, and a short makes money as the
        spread falls - High to Low. The Ask is where it can be BOUGHT,
        and a long makes money as it rises - Low to High.
    
        Directly ABOVE the spread row and in the same two columns, so
        the label and the number it is about are read together. It is
        not in the header: that is shared with the two LEG rows, where
        bid and ask are just a leg's own book and mean nothing about
        direction.
    */
    html += '<tr class="spread-hint"><th></th><td class="sym"></td>' +
      '<td class="c-bid hint-down" title="The BID is where a sale ' +
      'executes, whichever column your desk clicks to get there. A ' +
      'short is in profit as the spread falls: High to Low.">' +
      'H &rarr; L</td>' +
      '<td class="c-ask hint-up" title="The ASK is where a purchase ' +
      'executes, whichever column your desk clicks to get there. A ' +
      'long is in profit as the spread rises: Low to High.">' +
      'L &rarr; H</td>' +
      '<td class="c-width"></td><td class="c-age"></td></tr>';
    html += '<tr class="spread"><th></th><td class="sym">spread</td>' +
      cell(market.short_spread, 'c-bid') +
      cell(market.long_spread, 'c-ask') +
      width(market.short_spread, market.long_spread) +
      '<td class="age c-age"></td></tr>';
    return html + '</tbody></table>' + spreadMathHtml(row, market);
  }

  function spreadMathHtml(row, market) {
    /* How the spread is made, with today's numbers in it, so a trader
     * can check it with a calculator: B - beta x A at the mids. Then
     * the two drifts that move it without anyone trading:
     *   beta drift - the legs' price ratio now against the beta the
     *     spread uses. Large, and the spread starts to track one
     *     leg's own price instead of the gap between them.
     *   24 h drift - where the spread was a day ago. */
    var beta = row.hedge_ratio;
    var a = mid(market.leg_a_bid, market.leg_a_ask);
    var b = mid(market.leg_b_bid, market.leg_b_ask);
    if (beta === null || beta === undefined || a === null || b === null) {
      return '';
    }
    var spread = b - beta * a;
    // Coins, not letters: ETH-USDT-SWAP -> ETH.
    function coin(symbol) { return String(symbol || '?').split('-')[0]; }
    function two(x) {
      return x.toLocaleString(undefined, {minimumFractionDigits: 2,
                                          maximumFractionDigits: 2});
    }
    function signed2(x) { return (x >= 0 ? '+' : '\u2212') + two(Math.abs(x)); }
    // Line 1: the sum, with today's prices, that a calculator agrees with.
    var html = '<div class="spread-math" title="The spread is leg B minus ' +
      '\u03b2 times leg A, at the middle of each leg\u2019s bid and ask">' +
      '<div>Spread <b>' + signed2(spread) + '</b> = ' + coin(row.symbol_b) +
      ' ' + two(b) + ' \u2212 ' + beta + ' \u00d7 ' + coin(row.symbol_a) +
      ' ' + two(a) + '</div>';
    // Line 2: how far it has moved without anyone trading.
    var bits = [];
    var betaDrift = beta ? ((b / a) / beta - 1) * 100 : null;
    var big = betaDrift !== null && isFinite(betaDrift) &&
      Math.abs(betaDrift) >= 5;
    if (betaDrift !== null && isFinite(betaDrift)) {
      bits.push('<span' + (big ? ' class="sm-warn"' : '') + ' title="How ' +
        'far ' + coin(row.symbol_b) + '\u00f7' + coin(row.symbol_a) +
        ' is now (' + (b / a).toFixed(6) + ') from \u03b2. Past 5% the ' +
        'spread starts to follow one coin\u2019s own price: fix \u03b2 in ' +
        'Pairs > Edit.">\u03b2 drift ' + (betaDrift >= 0 ? '+' : '\u2212') +
        Math.abs(betaDrift).toFixed(1) + '%' +
        (big ? ' \u2014 fix \u03b2 in Pairs' : '') + '</span>');
    }
    var session = market.session || {};
    if (session.source === '24h' && session.open !== null &&
        session.open !== undefined) {
      bits.push('<span title="The spread 24 hours ago was ' +
        two(session.open) + '">24h ' + signed2(spread - session.open) +
        '</span>');
    }
    if (bits.length) { html += '<div>' + bits.join(' \u00b7 ') + '</div>'; }
    return html + '</div>';
  }

  function mid(bid, ask) {
    if (bid === null || bid === undefined || ask === null ||
        ask === undefined) { return null; }
    return (bid + ask) / 2;
  }

  function badgeClass(badge) {
    if (!badge) { return ''; }
    if (badge.indexOf('OK') === 0) { return 'ok'; }
    if (badge === 'warming up') { return 'warn'; }
    if (badge.indexOf('stale') === 0) { return 'warn'; }
    return 'bad';
  }

  function bought(row) {
    return (row.positions || []).filter(function (p) {
      return p.side === 'BUY';
    }).length;
  }

  function sold(row) {
    return (row.positions || []).filter(function (p) {
      return p.side === 'SELL';
    }).length;
  }

  function flashChanged(tr, side) {
    var cell = tr.querySelector('td.' + side);
    if (!cell) { return; }
    var now = cell.textContent;
    var was = tr['_' + side];
    tr['_' + side] = now;
    // Nothing to announce on the first draw, or when only some other
    // cell in the row changed.
    if (was === undefined || was === now) { return; }
    // Only the TOUCH flashes. With OKX's full book every row's size
    // moves every second, and a whole column flashing pale is a column
    // nobody can read (it was: white figures on a pale band).
    if (!cell.classList.contains('has-qty')) { return; }
    cell.classList.remove('ticked');
    void cell.offsetWidth;                    // restart the animation
    cell.classList.add('ticked');
  }

  function commitRows(body, rows) {
    /* Update the ladder IN PLACE, keyed by PRICE.
     *
     * The whole tbody used to be rebuilt three times a second with
     * `innerHTML =`. That threw away the very row the pointer was over
     * — losing :hover and the pressed state — and a click landing while
     * the rows were being replaced hit a detached element, or whatever
     * had just slid into that position. The element for a price is now
     * the SAME element from tick to tick, and only the cells whose
     * markup actually changed are written.
     *
     * The identity is the price, never the index: an index moves when
     * the window does, and an order must never be sent at whatever
     * happened to slide under the cursor.
     */
    var existing = {};
    Array.prototype.forEach.call(body.children, function (tr) {
      existing[tr.dataset.level] = tr;
    });
    var previous = null;
    rows.forEach(function (row) {
      var keyed = String(row.level);
      var tr = existing[keyed];
      if (tr) {
        delete existing[keyed];
        if (tr.className !== row.cls) { tr.className = row.cls; }
        // Held on the element, not in an attribute: this is a
        // comparison key, not something the page should carry.
        if (tr._cells !== row.cells) {
          tr.innerHTML = row.cells;
          tr._cells = row.cells;
          // Flash only the size that actually moved. On a ladder that
          // no longer crawls, this is the only thing left saying the
          // book is live — and it is a background colour, so nothing
          // reflows and the click target never shifts.
          flashChanged(tr, 'bid');
          flashChanged(tr, 'ask');
        }
      } else {
        tr = document.createElement('tr');
        tr.className = row.cls;
        tr.dataset.level = keyed;
        tr.innerHTML = row.cells;
        tr._cells = row.cells;
      }
      // Into place without disturbing rows already sitting correctly.
      var want = previous ? previous.nextSibling : body.firstChild;
      if (tr !== want) { body.insertBefore(tr, want); }
      previous = tr;
    });
    Object.keys(existing).forEach(function (level) {
      body.removeChild(existing[level]);
    });
  }

  function renderRows(node, key, row) {
    // '.grid tbody', not 'tbody': the rail has small tables of its own
    // now, and the first tbody in the window is not necessarily the
    // ladder's. Writing the rows into the wrong one destroys the rail.
    var body = node.querySelector('.grid tbody');
    if (state.snapshot.row_height_px) {
      // A bigger target is a faster and safer click. 17px is the
      // reference screen's; a large monitor wants more.
      node.style.setProperty('--row-h',
                             state.snapshot.row_height_px + 'px');
    }
    var rows = row.rows || [];
    var market = row.market || {};
    var lastPrint = row.last_print || {};
    // Which of our working orders are NOT actually resting at the
    // broker. A synthetic order joins the book the instant it is
    // clicked, but the real pending on the quoting leg is only placed
    // once the guards are clear (quoter._rest_or_repeg) — so while the
    // feed reads stale or desynced, the order exists here and NOWHERE
    // else. It looked exactly like one resting at the broker, and the
    // only hint was W:8 (broker 0) in small text in the footer.
    var heldOff = {};
    var quoteFor = {};
    (row.quotes || []).forEach(function (quote) {
      (quote.orders || []).forEach(function (id) { quoteFor[id] = quote; });
      if (quote.ticket) { return; }             // really is at the broker
      // A CLOSING order rests NOTHING at the broker, by design: MT5
      // ignores `position` on a pending, so a closing limit is an
      // ordinary one and opens a second position on a hedging account.
      // The level is held here and fired by ticket. Marking it "not at
      // the broker" flagged the mode working as a fault — every
      // resting close came up amber, permanently.
      if (quote.intent === 'CLOSE') { return; }
      // Nor does a held entry: it is the mode, not a fault - unless
      // something is holding it (a stale price), which IS said.
      if (quote.held && !quote.reason) { return; }
      (quote.orders || []).forEach(function (id) {
        heldOff[id] = quote.reason ||
          'not resting at the broker yet';
      });
    });

    var ordersByLevel = {};
    (row.orders || []).forEach(function (order) {
      var bucket = ordersByLevel[order.level.toFixed(6)] ||
        (ordersByLevel[order.level.toFixed(6)] = []);
      bucket.push(order);
    });

    // The Algo's levels for each position it is watching - its own in
    // LIVE, YOURS in Signals: ENTRY, BE, TP and SL marked on the rows
    // they fall on, so the ladder shows where the trade comes off.
    var levelMarks = [];
    ((row.algo_block || {}).positions || []).forEach(function (held) {
      [[held.combined ? 'AVG' : 'ENTRY', held.entry_spread, 'entry'],
       ['BE', held.break_even, 'be'],
       ['TP', held.tp, 'tp'], ['SL', held.sl, 'sl']].forEach(function (m) {
        if (m[1] !== null && m[1] !== undefined && isFinite(m[1])) {
          levelMarks.push({tag: m[0], at: m[1], cls: m[2]});
        }
      });
    });
    // M: the Algo's mean (its band's middle) while a position is on -
    // where the spread is expected to revert to. Visual only.
    var heldNow = ((row.algo_block || {}).positions || []).length ||
      (row.positions || []).length;
    var meanNow = (row.algo_block || {}).mean;
    if (heldNow && meanNow !== null && meanNow !== undefined &&
        isFinite(meanNow)) {
      levelMarks.push({tag: 'M', at: meanNow, cls: 'mean'});
    }
    var half = (row.increment || 0.01) / 2;

    var out = [];
    rows.forEach(function (line) {
      var level = line.level;
      var inBid = market.short_spread !== undefined &&
        level <= market.short_spread + 1e-12;
      var inAsk = market.long_spread !== undefined &&
        level >= market.long_spread - 1e-12;
      var orders = ordersByLevel[level.toFixed(6)] || [];
      if (state.filtered[key] && !orders.length && !line.is_best_bid &&
          !line.is_best_ask && !inBid && !inAsk) {
        return;
      }
      var work = orders.length ? orders[0] : null;
      var workQty = orders.reduce(function (sum, order) {
        return sum + (order.quantity - order.filled_quantity);
      }, 0);
      // Clicks that are on their way. Shown as their own thing, never
      // added into the working total: a ghost is not an order yet.
      var ghosts = pendingAt(key, level, row.increment);
      var ghostQty = ghosts.reduce(function (sum, ghost) {
        return sum + ghost.quantity;
      }, 0);
      var classes = [];
      if (inBid) { classes.push('in-bid'); }
      if (inAsk) { classes.push('in-ask'); }
      if (line.is_best_bid) { classes.push('market-line'); }
      // The heavy rule marks where the market is; on a LOCKED ladder
      // it marks the anchor instead, because the anchor is the one
      // price on a locked ladder that is guaranteed not to move. With
      // the rows frozen and the bands solid, a rule chasing the mid
      // was the only thing left hopping about.
      if (state.locked[key] ? line.is_anchor : line.is_mid) {
        classes.push('mid-line');
      }
      if (line.is_best_ask) { classes.push('best-ask'); }
      var isLast = lastPrint.level !== undefined &&
        Math.abs(lastPrint.level - level) < (row.increment || 1) / 2;

      var cells = '';
      var ghostSide = ghosts.length ? ghosts[0].side.toLowerCase() : '';
      // ANY order here not at the broker marks the cell: a level that
      // is half resting is still a level the trader must not read as
      // fully working.
      var heldReason = null;
      orders.forEach(function (order) {
        heldReason = heldReason || heldOff[order.order_id] || null;
      });
      var workQuote = work ? quoteFor[work.order_id] : null;
      var afterQty = (workQuote && workQuote.open_after) || 0;
      cells += '<td class="work' +
        (work ? ' ' + work.side.toLowerCase() : '') +
        (heldReason ? ' held' : '') +
        (ghosts.length ? ' pending ' + ghostSide : '') + '"' +
        (work ? ' data-order-id="' + work.order_id + '"' +
          ' data-side="' + work.side + '" title="' +
          escapeHtml(heldReason
            ? 'NOT at the broker: ' + heldReason +
              '. Click to add another here; right-click to pull one.'
            : orders.length + ' ' + work.side + ' order(s) here' +
              restingOn(workQuote) +
              '. Click to add another; right-click to pull one.') +
          '"' : '') + '>' +
        (workQty ? workQty : '') +
        // WHAT THIS CLICK STILL OWES. A reducing click bigger than the
        // position it covers holds the rest back until the close has
        // gone through — one instruction, in order. The held part was
        // in the engine and NOWHERE on the screen: a trader who
        // clicked 100 over a 93 saw '93' and no sign that 7 more were
        // coming, waiting, or quietly dropped.
        (afterQty ? '<span class="after" title="' +
          escapeHtml(afterQty + ' more to OPEN here, held back until this '
                     + 'close goes through. One click is one instruction, '
                     + 'in order: resting it now would put the new '
                     + 'position on before the old one came off.') +
          '">+' + afterQty + '</span>' : '') +
        (ghostQty ? '<span class="ghost" title="sent — waiting for the ' +
          'engine">' + ghostQty + '</span>' : '') + '</td>';
      // MT5 publishes no depth for a spread, so only the touch is real.
      // The size is the two ORDER BOOKS', in spreads: whatever the
      // worse of the two legs can fill at the prices this level
      // implies. Nothing where the brokers publish no depth — an
      // invented size is one a trader would click on.
      cells += '<td class="bid' + (line.is_best_bid ? ' has-qty' : '') +
        '" title="' + clickHint('bid', level) + '">' +
        depthText(line.bid_size, line.is_best_bid, '▲') + '</td>';
      var tags = levelMarks.filter(function (m) {
        return Math.abs(m.at - level) < half;
      }).map(function (m) {
        return '<span class="lvl-tag ' + m.cls + '" title="' + m.tag + ' ' +
          fmt(m.at, digitsFor(row.increment) + 2) + '">' + m.tag + '</span>';
      }).join('');
      cells += '<td class="price' + (isLast ? ' last-trade' : '') + '">' +
        fmt(level, digitsFor(row.increment)) + tags + '</td>';
      cells += '<td class="ask' + (line.is_best_ask ? ' has-qty' : '') +
        '" title="' + clickHint('ask', level) + '">' +
        depthText(line.ask_size, line.is_best_ask, '▼') + '</td>';
      cells += '<td class="ltq' + (isLast ? ' print' : '') + '">' +
        (isLast ? fmt(lastPrint.quantity, 2) : '') + '</td>';
      out.push({level: level, cls: classes.join(' '), cells: cells});
    });
    commitRows(body, out);

    if (shouldRecentre(key, node, market)) { centreOnMid(node, key, market); }
  }

  //: How long the ladder leaves a hand-scrolled window alone before it
  //: re-centres. A ladder that snaps back while the trader is reading
  //: a level twenty rows away is a ladder they cannot use.
  var SCROLL_GRACE_MS = 4000;

  //: How long after the trader's last touch the ladder stays put. A
  //: window that re-centres between a mousedown and the mouseup is a
  //: window that moved the order.
  var BUSY_GRACE_MS = 1200;

  function setLocked(key, locked) {
    /* Lock, in one place, because it has to reach THREE things.
     *
     * The browser stops recentring its scroll; the engine stops
     * re-anchoring the price window; and the engine stops widening
     * that window to follow the touches. Only the first of those is
     * local, and a Lock that did only the first is what left the
     * ladder crawling with the box ticked.
     */
    locked = !!locked;
    state.locked[key] = locked;
    var node = document.querySelector('.ladder[data-pair="' +
                                      cssEscape(key) + '"]');
    var box = node && node.querySelector('.lock-scroll');
    if (box) { box.checked = locked; }
    send('lock_ladder', {pair: key, locked: locked});
  }

  function ladderIsBusy(key) {
    /* The trader is working this ladder RIGHT NOW: the pointer is over
     * it, a button is down on it, or they touched it a moment ago. Any
     * of those and it does not move under them — recentring while a
     * click is being aimed is how the wrong price gets sent. */
    if (state.hovering[key]) { return true; }
    return Date.now() - (state.busyAt[key] || 0) < BUSY_GRACE_MS;
  }

  function shouldRecentre(key, node, market) {
    if (state.locked[key]) { return false; }        // Lock means LOCKED
    if (ladderIsBusy(key)) { return false; }
    if (market.spread === undefined || market.spread === null) {
      return false;
    }
    var grid = node.querySelector('.grid');
    var now = Date.now();
    if (now - (state.scrolledAt[key] || 0) < SCROLL_GRACE_MS) {
      // The trader is working somewhere else on the ladder. Free
      // scrolling is the point: orders get placed away from the touch.
      return false;
    }
    if (!state.centredAt[key]) { return true; }     // first draw
    var row = midRow(node, market);
    if (row) {
      // Out of sight is always a reason: a market that has left the
      // window is a ladder showing prices nobody is trading.
      var top = row.offsetTop - grid.scrollTop;
      if (top < 0 || top > grid.clientHeight - row.offsetHeight) {
        return true;
      }
    }
    var every = state.snapshot.recentre_sec;
    if (every === undefined || every === null) { every = 5; }
    if (!every) { return false; }                   // 0 = only when lost
    return now - state.centredAt[key] >= every * 1000;
  }

  function midRow(node, market) {
    var marked = node.querySelector('tbody tr.mid-line');
    if (marked) { return marked; }
    /* The row the CURRENT MID sits on — the middle of the book, which
     * is where the ladder centres. Centring on a touch puts the other
     * side against an edge, and a side you cannot see is a side you
     * cannot trade. */
    var best = null;
    var closest = Infinity;
    Array.prototype.forEach.call(node.querySelectorAll('tbody tr'),
      function (row) {
        var away = Math.abs(parseFloat(row.dataset.level) - market.spread);
        if (away < closest) { closest = away; best = row; }
      });
    return best;
  }

  function centreOnMid(node, key, market) {
    var row = midRow(node, market);
    var grid = node.querySelector('.grid');
    if (!row || !grid.clientHeight) {
      // The window has not been laid out yet, so there is nothing to
      // centre IN. Ask again on the next frame rather than dropping it:
      // dropped, the ladder sits wherever the rows happened to land
      // until the re-centring timer next fires — seconds of a market
      // the trader is looking straight at, off centre.
      if (row && !state.centringAgain[key]) {
        state.centringAgain[key] = true;
        window.requestAnimationFrame(function () {
          state.centringAgain[key] = false;
          var live = (state.snapshot.pairs[key] || {}).market;
          if (live) { centreOnMid(node, key, live); }
        });
      }
      return;
    }
    state.centring[key] = true;                     // our scroll, not theirs
    grid.scrollTop = row.offsetTop -
      (grid.clientHeight - row.offsetHeight) / 2;
    state.centredAt[key] = Date.now();
    window.setTimeout(function () { state.centring[key] = false; }, 0);
  }


  function digitsFor(increment) {
    if (!increment) { return 4; }
    var digits = Math.max(0, Math.ceil(-Math.log10(increment)));
    return Math.min(6, digits);
  }

  function setValue(input, value) {
    if (document.activeElement === input) { return; }   // do not fight typing
    var text = value === null || value === undefined ? '' : String(value);
    if (input.value !== text) { input.value = text; }
  }

  // -- the Market Grid --------------------------------------------------

  function renderGrid() {
    var node = document.querySelector('.market-grid');
    if (!node) {
      node = document.createElement('section');
      node.className = 'window market-grid';
      node.innerHTML =
        '<div class="titlebar"><span class="swatch"></span>' +
        '<span class="title">Market Grid</span>' +
        '<span class="winbtns"><button class="winbtn close">&times;</button></span></div>' +
        '<div class="grid"><table><thead></thead><tbody></tbody></table></div>';
      node.querySelector('.close').addEventListener('click', function () {
        closePanel(panelId('grid'));
      });
      node.querySelector('tbody').addEventListener('click', onGridClick);
      node.querySelector('tbody').addEventListener('change', onGridChange);
      el('desktop').appendChild(node);
    }
    node.querySelector('thead').innerHTML =
      '<tr><th>Contract</th><th>Bid</th><th>Ask</th><th>Last</th>' +
      '<th>Chg</th><th>Incr</th><th>Qty</th><th>k $</th><th>Net</th>' +
      '<th>Work</th><th>Avg</th><th>Open P&amp;L</th><th>Mode</th>' +
      '<th>TIF</th><th>O/N</th><th>Feed</th><th>&beta;</th><th></th></tr>';

    var html = '';
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      var market = row.market || {};
      var broken = (row.errors || []).length > 0;
      html += '<tr class="' + (broken ? 'broken' : '') + '" data-pair="' +
        key + '">';
      html += '<td class="contract" title="Open this ladder">' +
        (row.name || key) + '</td>';
      // Bid = the SHORT spread (where it can be sold); Ask = the LONG.
      //
      // The TOOLTIPS come from the same mapping the click uses, and
      // they did not before: they were written out by hand as 'Click:
      // SELL' on the bid and 'Click: BUY' on the ask, which is the
      // hit-and-lift reading. On a TT desk both were exactly backwards
      // - the cell said SELL and the click bought. The click itself
      // was right the whole time, which is what made it dangerous: the
      // only thing disagreeing was the words the trader reads.
      html += '<td class="bid" title="' + clickHint('bid', row.short_spread) +
        '">' + fmt(row.short_spread, digitsFor(row.increment)) + '</td>';
      html += '<td class="ask" title="' + clickHint('ask', row.long_spread) +
        '">' + fmt(row.long_spread, digitsFor(row.increment)) + '</td>';
      html += '<td>' + fmt((row.last_print || {}).level,
                           digitsFor(row.increment)) + '</td>';
      html += '<td>' + fmt(market.net_change, 4) + '</td>';
      html += '<td><input class="incr" type="number" step="0.0001" value="' +
        (row.increment === null ? '' : row.increment) + '" title="' +
        'Derived: max(tick B, beta x tick A) = ' +
        fmt(row.increment_derived, 6) + '"></td>';
      html += '<td><input class="qty" type="number" step="0.01" value="' +
        row.default_quantity + '"></td>';
      html += '<td>' + money(row.spread_units) + '</td>';
      html += '<td>' + (row.net_position || 0) + '</td>';
      html += '<td>' + row.working_buys + '/' + row.working_sells + '</td>';
      html += '<td>' + fmt(row.avg_entry, 4) + '</td>';
      html += '<td>' + (row.open_pnl === null || row.open_pnl === undefined
                        ? DASH : money(row.open_pnl)) + '</td>';
      html += '<td>' + select('mode', ['LIMIT', 'MARKET'], row.order_type) + '</td>';
      html += '<td>' + select('tif', ['DAY', 'GTC'], row.time_in_force) + '</td>';
      html += '<td>' + select('on', ['ALLOW', 'EXIT_IF_PROFIT', 'EXIT_ALWAYS'],
                              row.overnight) + '</td>';
      html += '<td>' + (market.feed_badge || DASH) + '</td>';
      html += '<td title="Stamped for ' + (row.hedge_ratio_for || '?') + '">' +
        fmt(row.hedge_ratio, 4) + '</td>';
      html += '<td class="reason">' + (row.errors || []).join(' ') + '</td>';
      html += '</tr>';
    });
    node.querySelector('tbody').innerHTML = html;
    node.classList.toggle('inactive', state.active !== panelId('grid'));
    return node;
  }

  function select(name, options, value) {
    var html = '<select class="' + name + '">';
    options.forEach(function (option) {
      html += '<option value="' + option + '"' +
        (option === value ? ' selected' : '') + '>' + option + '</option>';
    });
    return html + '</select>';
  }

  function onGridClick(e) {
    var cell = e.target.closest('td');
    if (!cell) { return; }
    var key = cell.closest('tr').dataset.pair;
    var row = state.snapshot.pairs[key] || {};
    if (cell.classList.contains('contract')) {
      openPanel(panelId('ladder', key));
    } else if (cell.classList.contains('bid')) {
      // Identical semantics to a ladder click, through the identical
      // code path and the identical mapping — a test asserts it. The
      // PRICE is still the one that cell displays; only the side moves
      // with the convention.
      clickLevel(key, sideForColumn('bid'), row.short_spread);
    } else if (cell.classList.contains('ask')) {
      clickLevel(key, sideForColumn('ask'), row.long_spread);
    }
  }

  function onGridChange(e) {
    var input = e.target;
    var key = input.closest('tr').dataset.pair;
    if (input.classList.contains('incr')) {
      setPair(key, {increment: parseFloat(input.value)});
    } else if (input.classList.contains('qty')) {
      setPair(key, {default_quantity: parseFloat(input.value)});
    } else if (input.classList.contains('mode')) {
      setPair(key, {order_type: input.value});
    } else if (input.classList.contains('tif')) {
      setPair(key, {time_in_force: input.value});
    } else if (input.classList.contains('on')) {
      setPair(key, {overnight: input.value});
    }
  }

  // -- the positions monitor ---------------------------------------------

  function renderMonitor() {
    var node = document.querySelector('.monitor');
    if (!node) {
      node = document.createElement('section');
      node.className = 'window monitor';
      node.innerHTML =
        '<div class="titlebar"><span class="swatch"></span>' +
        '<span class="title">Trading Monitor</span>' +
        '<span class="winbtns"><button class="winbtn close">&times;</button></span></div>' +
        '<div class="tabs">' +
        '<button data-tab="positions">Positions</button>' +
        '<button data-tab="orders">Working Orders</button>' +
        '<button data-tab="fills">Fills</button>' +
        '<button data-tab="slippage">Slippage</button>' +
        '<button data-tab="accounts">Accounts</button>' +
        '<button data-tab="reconcile">Reconciler</button>' +
        '<button data-tab="analysis">Analysis</button>' +
        '<button data-tab="sizing">Sizing</button>' +
        // Only while the mirror is switched on in config.json.
        '<button data-tab="mirror" hidden>Mirror</button></div>' +
        // `monitor-note`, not `note`: the panes have notes of their own,
        // and a bare `.note` selector reaches into them and overwrites
        // the first one it finds.
        '<div class="pane"></div><div class="note monitor-note"></div>';
      node.querySelector('.close').addEventListener('click', function () {
        closePanel(panelId('monitor'));
      });
      node.querySelector('.tabs').addEventListener('click', function (e) {
        var button = e.target.closest('button');
        if (!button) { return; }
        state.monitorTab = button.dataset.tab;
        render();
      });
      node.querySelector('.pane').addEventListener('click', onMonitorClick);
      node.querySelector('.pane').addEventListener('change', function (e) {
        if (e.target.classList.contains('ours-only')) {
          state.fillsFilter.ours = e.target.checked;
          loadFills(true);
        }
        if (e.target.classList.contains('all-sessions')) {
          state.slippageAll = e.target.checked;
          loadSlippage(true);
        }
        if (e.target.classList.contains('an-days')) {
          state.analysisDays = Number(e.target.value);
          loadAnalysis(true);
        }
        if (e.target.classList.contains('an-pair')) {
          state.analysisPair = e.target.value;
          loadAnalysis(true);
        }
        if (e.target.classList.contains('mr-filter')) {
          mirrorFilter()[e.target.dataset.key] = e.target.value;
          render();
        }
        if (e.target.classList.contains('an-pnl')) {
          state.analysisPnl = e.target.value;
          try {
            window.localStorage.setItem('okxtrader.analysisPnl',
                                        state.analysisPnl);
          } catch (err) { /* a preference, not the money */ }
          render();
        }
        if (e.target.classList.contains('an-capital')) {
          saveCapital(e.target.value);
        }
        if (e.target.classList.contains('an-zone-days')) {
          state.zoneDays = Number(e.target.value);
          state.zoneDate = '';          // a range and a day are exclusive
          loadAnalysis(true);
        }
        if (e.target.classList.contains('an-zone-date')) {
          state.zoneDate = e.target.value || '';
          loadAnalysis(true);
        }
        if (e.target.classList.contains('an-volume-from')) {
          state.volumeFrom = e.target.value || '';
          loadAnalysis(true);
        }
        if (e.target.classList.contains('an-volume-to')) {
          state.volumeTo = e.target.value || '';
          loadAnalysis(true);
        }
        if (e.target.classList.contains('an-bt-days')) {
          state.analysisBtDays = Number(e.target.value);
        }
        if (e.target.classList.contains('sz-input')) {
          var next = sizingSettings();
          var key = e.target.dataset.key;
          if (e.target.dataset.kind === 'text') {
            next[key] = e.target.value;
          } else {
            var v = parseFloat(e.target.value);
            // Blank leverage = the ladder's own; anything else must read.
            if (key === 'lev' && e.target.value === '') { v = 0; }
            else if (!isFinite(v) || v < 0) { return; }
            if ((key === 'win' || key === 'freePct' || key === 'makerPct') &&
                v > 100) { v = 100; }
            // A month has 1 to 31 trading days.
            if (key === 'days') { v = Math.min(31, Math.max(1, Math.round(v))); }
            next[key] = v;
          }
          state.sizing = next;
          render();
        }
        // Changing the PAIR clears the two tickets: they belong to that
        // pair's accounts, and carrying them over would leave a choice
        // on screen that the engine is about to refuse.
        if (e.target.classList.contains('adopt-pair')) {
          state.adopt = {pair: e.target.value, a: '', b: ''};
          render();
        }
        // NO render() here. Picking a ticket changes only whether the
        // Adopt button is enabled, and re-rendering the pane from under
        // a <select> the operator has just used is what made this form
        // unusable in the first place.
        if (e.target.classList.contains('adopt-a')) {
          state.adopt.a = e.target.value;
          syncAdoptButton(e.target);
        }
        if (e.target.classList.contains('adopt-b')) {
          state.adopt.b = e.target.value;
          syncAdoptButton(e.target);
        }
      });
      el('desktop').appendChild(node);
    }
    var mirrorOn = !!(state.snapshot && state.snapshot.mirror);
    if (!mirrorOn && state.monitorTab === 'mirror') {
      state.monitorTab = 'positions';
    }
    node.querySelector('.tabs button[data-tab="mirror"]').hidden = !mirrorOn;
    Array.prototype.forEach.call(node.querySelectorAll('.tabs button'),
      function (button) {
        button.classList.toggle('on', button.dataset.tab === state.monitorTab);
      });
    var pane = node.querySelector('.pane');
    /* A PANE BEING TYPED INTO IS NOT REDRAWN.
     *
     * This whole pane is rebuilt from innerHTML on every snapshot,
     * about twice a second. That is fine for tables, and fatal for a
     * <select>: an open dropdown belongs to the element, so replacing
     * the element closes it. The Reconciler tab's adopt form blinked
     * open and shut on every poll and no ticket could ever be picked.
     *
     * Skipping the redraw while the focus is inside costs nothing -
     * the numbers behind the form are a poll out of date for as long
     * as somebody is holding a dropdown open, and they are looking at
     * the dropdown.
     */
    if (pane.contains(document.activeElement) &&
        document.activeElement !== document.body) {
      var tag = (document.activeElement.tagName || '').toUpperCase();
      if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') {
        node.classList.toggle('inactive', state.active !== panelId('monitor'));
        return node;
      }
    }
    if (state.monitorTab === 'positions') { pane.innerHTML = positionsTable(); }
    else if (state.monitorTab === 'orders') { pane.innerHTML = ordersTable(); }
    else if (state.monitorTab === 'fills') {
      loadFills();
      pane.innerHTML = fillsTable();
    }
    else if (state.monitorTab === 'slippage') {
      loadSlippage();
      pane.innerHTML = slippageTable();
    }
    else if (state.monitorTab === 'accounts') {
      pane.innerHTML = accountsTable();
    }
    else if (state.monitorTab === 'analysis') {
      loadAnalysis();
      pane.innerHTML = analysisPane();
    }
    else if (state.monitorTab === 'sizing') {
      pane.innerHTML = sizingPane();
    }
    else if (state.monitorTab === 'mirror') {
      pane.innerHTML = mirrorPane(state.snapshot.mirror || {});
    }
    else { pane.innerHTML = reconcileTable(); }
    node.querySelector('.monitor-note').textContent =
      state.monitorTab === 'sizing'
      ? 'A planning table, worked out here from the live prices, beta and ' +
        'leverage of the ETH vs BTC ladder. Fees: the tier each row reaches ' +
        'on OKX\u2019s futures table (30-day volume or assets, at 3.6725 ' +
        'AED a dollar), once a month at that volume is behind it. Target ' +
        'is the profit after every cost; stop is the whole loss. Nothing ' +
        'here sends an order.'
      : state.monitorTab === 'mirror'
      ? 'The Algo\u2019s trades, copied into each follower account at its ' +
        'multiple, and closed when the trade closes. Set in config.json ' +
        '(mirror); each follower\u2019s API key is in .env. The desk\u2019s ' +
        'own positions are never touched.'
      : state.monitorTab === 'analysis'
      ? 'Built from what is recorded: closed positions, the Algo\u2019s ' +
        'own audit trail and the trackers. Nothing here sends an order. ' +
        'A figure nobody measured is a dash, never 0 \u2014 trades closed ' +
        'before this tab existed have no best or worst point.'
      : state.monitorTab === 'slippage'
      ? 'Every figure here was MEASURED against the price that was ' +
        'clicked. Positive is a cost, at both ends. A position whose ' +
        'fill could not be priced is counted as unmeasured, never as ' +
        'zero — averaging it in would flatter every column.'
      : 'Marked at the touches these would actually CLOSE at, less ' +
      'commission only — so a position shows a loss the instant it ' +
      'opens, equal to one round turn of both legs’ bid-ask. That is ' +
      'what closing it immediately would cost.';
    node.classList.toggle('inactive', state.active !== panelId('monitor'));
    fitMonitorPane(node);
    return node;
  }

  //: How close the foot of the monitor may come to the foot of the
  //: desktop, and the least room its pane is ever given.
  var MONITOR_PANE_GAP = 6;
  var MONITOR_PANE_MIN = 160;

  function fitMonitorPane(node) {
    /* THE WINDOW TAKES THE ROOM THAT IS THERE.
     *
     * The pane was capped at a flat 60vh, so the Analysis tab - which
     * is far longer than one screen - left the bottom third of the
     * display as bare desktop while its own content scrolled inside a
     * short box. The room that is actually available is from the
     * pane's own top to the foot of the desktop, less the note that
     * sits under it, and that is what it takes: a window dragged
     * halfway down gets what is left below it rather than running off
     * the bottom.
     */
    var pane = node.querySelector('.pane');
    var desktop = el('desktop');
    if (!pane || !desktop) { return; }
    // Maximized, the window's own height is the room: the pane fills it
    // (a cap measured for the unmaximized window showed five rows of a
    // full-screen table and left the rest bare).
    if (node.classList.contains('maximized')) {
      pane.style.maxHeight = '';
      return;
    }
    var note = node.querySelector('.monitor-note');
    var foot = desktop.getBoundingClientRect().bottom;
    var top = pane.getBoundingClientRect().top;
    var reserved = (note ? note.getBoundingClientRect().height : 0) +
      MONITOR_PANE_GAP;
    pane.style.maxHeight =
      Math.max(foot - top - reserved, MONITOR_PANE_MIN) + 'px';
  }

  function eachPosition(callback) {
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      (state.snapshot.pairs[key].positions || []).forEach(function (position) {
        callback(key, state.snapshot.pairs[key], position);
      });
    });
  }

  function pctSigned(value, digits) {
    return (value > 0 ? '+' : '') + Number(value).toFixed(digits) + '%';
  }

  function pctCell(value, digits, estimated) {
    if (value === null || value === undefined) {
      return '<td>' + DASH + '</td>';
    }
    return '<td class="' + (value > 0 ? 'up' : value < 0 ? 'down' : '') +
      '">' + pctSigned(value, digits) +
      (estimated ? ' <small class="hint">est.</small>' : '') + '</td>';
  }

  function positionReturns(position, pnl) {
    /* The margin BOTH legs tied up at entry (per spread x size) and the
     * notional of both legs, with the P&L as a share of each. The same
     * arithmetic as Telegram's and the Analysis tab's. Unmeasured: null. */
    var margin = position.entry_margin
      ? Number(position.entry_margin) * Number(position.quantity || 1) : null;
    var notional = 0;
    ['leg_a', 'leg_b'].forEach(function (name) {
      var leg = position[name] || {};
      if (notional === null || !leg.volume || !leg.contract_size ||
          !leg.price) { notional = null; return; }
      notional += leg.volume * leg.contract_size * leg.price;
    });
    var have = pnl !== null && pnl !== undefined;
    return {margin: margin, notional: notional,
            estimated: position.entry_margin_source === 'leverage',
            pct_margin: have && margin ? pnl / margin * 100 : null,
            pct_notional: have && notional ? pnl / notional * 100 : null};
  }

  function positionsTable() {
    var html = '<table><thead><tr><th>Pair</th><th>Side</th><th>Net</th>' +
      '<th>Avg entry</th><th>Mark</th>' +
      '<th title="points the spread has moved in your favour, entry to ' +
      'where it would close">Move</th>' +
      '<th>Open P&amp;L</th>' +
      '<th title="open P&amp;L (net of costs) \u00f7 the margin BOTH legs ' +
      'tied up at entry, priced by OKX - the same base as the TP % and ' +
      'SL %">% on margin</th><th>Margin</th>' +
      '<th title="open P&amp;L \u00f7 both legs\u2019 face value at entry">' +
      '% of notional</th><th>Mode</th>' +
      '<th>Slip</th><th>Click→on</th><th>Naked</th><th>Legs</th>' +
      '<th></th></tr></thead><tbody>';
    var any = false;
    eachPosition(function (key, row, position) {
      any = true;
      html += '<tr data-position="' + position.position_id + '">';
      html += '<td>' + (row.name || key) + '</td>';
      html += '<td>' + position.side + '</td>';
      html += '<td>' + qty(position.quantity) + '</td>';
      html += '<td>' + fmt(position.entry_spread, 4) + '</td>';
      html += '<td>' + fmt(position.closing_spread, 4) + '</td>';
      html += '<td class="' + updown(position.spread_move) + '">' +
        signedOr(position.spread_move, 4) + '</td>';
      html += '<td class="' + (position.net_pnl > 0 ? 'up' : 'down') + '">' +
        money(position.net_pnl) + '</td>';
      // As Mirror: % on margin, the margin itself, % of notional - and
      // "est." where the margin was estimated from leverage rather than
      // priced by the exchange.
      var est = position.entry_margin_source === 'leverage'
        ? ' <small class="hint">est.</small>' : '';
      // The engine's figures where it sent them; otherwise worked out
      // here from the position, as Mirror does.
      var back = positionReturns(position, position.net_pnl);
      var pctM = position.pnl_pct_margin === null ||
        position.pnl_pct_margin === undefined ? back.pct_margin
        : position.pnl_pct_margin;
      var pctN = position.pnl_pct_notional === null ||
        position.pnl_pct_notional === undefined ? back.pct_notional
        : position.pnl_pct_notional;
      var marginNow = position.margin === null ||
        position.margin === undefined ? back.margin : position.margin;
      html += '<td class="' + updown(pctM) + '">' + pctOr(pctM, 2) +
        (pctM === null || pctM === undefined ? '' : est) + '</td>';
      html += '<td>' + (marginNow === null || marginNow === undefined
        ? DASH : money(marginNow) + est) + '</td>';
      html += '<td class="' + updown(pctN) + '">' + pctOr(pctN, 3) +
        '</td>';
      html += '<td>' + position.order_type + '</td>';
      html += '<td>' + fmt(position.entry_slippage, 4) + '</td>';
      html += '<td>' + (position.click_to_on_ms === null ||
                        position.click_to_on_ms === undefined
                        ? DASH : Math.round(position.click_to_on_ms) + 'ms') +
        '</td>';
      //: HOW LONG ONE LEG WAS ON BY ITSELF, on the broker's own clock.
      //: Click→on beside it starts when this process NOTICED the fill,
      //: so on a resting order it can read 1s on a leg that was alone
      //: for twelve minutes. This is the column that cannot be
      //: flattered by looking late; over a second it is marked, and
      //: unmeasured stays a dash rather than becoming a comfortable 0.
      html += '<td' + (position.naked_ms > 1000 ? ' class="down"' : '') +
        '>' + (position.naked_ms === null || position.naked_ms === undefined
          ? DASH
          : (position.naked_ms >= 1000
            ? (position.naked_ms / 1000).toFixed(1) + 's'
            : Math.round(position.naked_ms) + 'ms')) + '</td>';
      html += '<td>' + legText(position.leg_a) + ' / ' +
        legText(position.leg_b) + '</td>';
      html += '<td><button class="btn close-position">Flatten</button></td>';
      html += '</tr>';
      html += '<tr class="detail"><td colspan="16">' +
        legDetail(position.leg_a, 'A') + ' &nbsp; ' +
        legDetail(position.leg_b, 'B') + '</td></tr>';
    });
    if (!any) { html += '<tr><td colspan="16">nothing open</td></tr>'; }
    html += '</tbody></table>';
    html += accountReconciliation();
    return html;
  }

  function shareWords(notional, margin) {
    /* ' · -0.137% N · -0.41% M' - nothing at all when neither is
     * measured, rather than two dashes on a short line. */
    if ((notional === null || notional === undefined) &&
        (margin === null || margin === undefined)) { return ''; }
    return ' \u00b7 ' + pctOr(notional, 3) + ' N \u00b7 ' +
      pctOr(margin, 2) + ' M';
  }

  function updown(value) {
    return value === null || value === undefined || value === 0 ? ''
      : (value > 0 ? 'up' : 'down');
  }

  function signedOr(value, digits) {
    if (value === null || value === undefined) { return DASH; }
    return (value > 0 ? '+' : '') + fmt(value, digits);
  }

  function pctOr(value, digits) {
    /* A share, signed. Unmeasured is a dash, never 0.00%. */
    if (value === null || value === undefined || !isFinite(value)) {
      return DASH;
    }
    return (value > 0 ? '+' : '') + Number(value).toFixed(digits) + '%';
  }

  function legText(leg) {
    if (!leg) { return DASH; }
    return leg.side + ' ' + qty(leg.volume) + ' @ ' + fmt(leg.price, 2);
  }

  function legDetail(leg, label) {
    if (!leg) { return 'leg ' + label + ': ' + DASH; }
    return 'leg ' + label + ' ' + leg.symbol + ' on ' + leg.account +
      ' · tickets ' + (leg.position_tickets.join(', ') || DASH) +
      ' · ' + qty(leg.volume) + ' lots × ' +
      (leg.contract_size || DASH) + '/lot';
  }

  function accountReconciliation() {
    /* Our total against MT5's OWN per-account profit. A difference is a
     * fault to be SHOWN, not smoothed.
     *
     * BOTH NUMBERS COME FROM THE ENGINE, and from ONE MOMENT. This
     * used to add up the marks on screen and compare them against an
     * account profit the engine caches for ~5s — a photo taken now
     * against one taken five seconds ago. On a moving market they
     * always differed by more than the one-cent tolerance, so the row
     * was red every session; a light that is always on is one nobody
     * reads. The engine now only takes the comparison on the passes
     * where it read both halves together, and holds the answer between
     * them (coordinator.pnl_check), so a cent of tolerance is honest
     * and any red is real.
     *
     * The on-screen sum also quietly skipped positions that could not
     * be marked, which made the total read as authoritative while
     * being short one position. The engine's figure carries the
     * unmeasured-is-not-zero rule instead: one unmarkable position
     * makes the whole total unknown.
     */
    var check = state.snapshot.pnl_check;
    if (!check) {
      return '<div class="note">our P&amp;L and OKX’s own have not yet ' +
        'been read together, so there is nothing to reconcile — ' +
        'unmeasured, not zero.</div>';
    }
    if (check.ours === null || check.ours === undefined ||
        check.theirs === null || check.theirs === undefined) {
      return '<div class="note">' + (
        check.theirs === null || check.theirs === undefined
          ? 'OKX’s own profit could not be read'
          : 'a position could not be marked, so our own total is unknown') +
        ', so there is nothing to reconcile against — unmeasured, not ' +
        'zero.</div>';
    }
    var bad = Math.abs(check.difference) > 0.01;
    /* GROSS against GROSS, and the row SAYS so. MT5's own profit
     * carries no commission and no swap, so comparing it against our
     * net total put the whole round trip's commission into the
     * difference for ever — a red row about a disagreement that does
     * not exist. The net figure, which is the one on every other
     * panel, is shown beside it and is never the thing compared. */
    var net = (check.ours_net === null || check.ours_net === undefined)
      ? '' : '<td>ours after commission</td><td>' + money(check.ours_net) +
        '</td>';
    return '<table><tbody><tr' + (bad ? ' class="mismatch"' : '') +
      '><td>our total (gross)</td><td>' + money(check.ours) +
      '</td><td>OKX’s own</td><td>' + money(check.theirs) +
      '</td><td>difference</td><td>' + money(check.difference) +
      '</td>' + net +
      '<td>' + agoText(check.at) + '</td></tr></tbody></table>';
  }

  function agoText(at) {
    // Both halves were read together AT THIS TIME — said out loud, so
    // a figure that is a few seconds old is never mistaken for live.
    var now = state.snapshot.at;
    if (typeof at !== 'number' || typeof now !== 'number') { return ''; }
    var seconds = Math.max(0, Math.round(now - at));
    return seconds < 1 ? 'read together just now'
      : 'read together ' + seconds + 's ago';
  }

  // A W3 maker fill in words: which phase, and how far each leg is.
  function makerWords(job) {
    var legs = (job.legs || []).map(function (leg) {
      return leg.leg + ' ' + qty(leg.filled) + '/' + qty(leg.planned);
    }).join(' · ');
    return (job.kind === 'close'
      ? 'MAKER CLOSE: closing orders resting at the touch · '
      : job.phase === 'recovering'
      ? 'MAKER RECOVERY: one leg on, the other resting · '
      : 'MAKER FILL: both legs resting at the touch · ') +
      legs + ' · ' + Math.round(job.age_sec || 0) + 's';
  }

  function ordersTable() {
    var html = '<table><thead><tr><th>Pair</th><th>Side</th><th>Level</th>' +
      '<th>Qty</th><th>TIF</th><th>State</th><th>Pending</th>' +
      '<th>Peg price</th><th>Resting on</th><th>Re-pegs</th><th>Why</th>' +
      '<th></th>' +
      '</tr></thead><tbody>';
    var columns = 12;
    var any = false;
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      var quotes = {};
      (row.quotes || []).forEach(function (quote) {
        quote.orders.forEach(function (id) { quotes[id] = quote; });
      });
      (row.orders || []).forEach(function (order) {
        any = true;
        var quote = quotes[order.order_id] || {};
        html += '<tr data-order="' + order.order_id + '">';
        html += '<td>' + (row.name || key) + '</td>';
        html += '<td>' + order.side + '</td>';
        html += '<td>' + fmt(order.level, 4) + '</td>';
        // The size WORKING, and beside it what this click still owes:
        // a reducing click bigger than the position it covers holds
        // the rest back until the close has gone through, and that
        // part was on no panel at all.
        html += '<td>' + qty(order.quantity - order.filled_quantity) +
          (quote.open_after
            ? ' <span class="after" title="held back until this close '
              + 'goes through">+' + qty(quote.open_after) + '</span>'
            : '') + '</td>';
        html += '<td>' + order.time_in_force + '</td>';
        html += '<td>' + order.state + '</td>';
        html += '<td>' + (quote.ticket || DASH) + '</td>';
        html += '<td>' + fmt(quote.price, 2) + '</td>';
        // WHICH broker will show this order, and which leg crosses
        // when it fills. 'leg a' alone read as leg A having nothing.
        // WHAT THIS ROW IS, before which leg it sits on.
        //
        // One click can arm SEVERAL orders: a click bigger than the
        // oldest position walks down the stack, closing each ticket as
        // far as it reaches. Those rows are identical apart from their
        // size, so two of them at one level read as the same order
        // placed twice at the wrong quantity. They are not — they are
        // one click, split across the tickets it covers — and the row
        // now says which ticket it closes.
        html += '<td>' + (order.position_id
          ? '<span class="closing">closes ' +
            escapeHtml(order.position_id) + '</span><br>'
          : '') + (quote.held
          ? 'both legs<div class="hint">cross at market when the spread ' +
            'reaches it</div>'
          : quote.leg
          ? 'leg ' + quote.leg +
            (quote.symbol ? ' · ' + escapeHtml(quote.symbol) : '') +
            (quote.crosses_leg
              ? '<div class="hint">' + quote.crosses_leg +
                ' crosses on fill</div>'
              : '')
          : DASH) + '</td>';
        html += '<td>' + (quote.repegs === undefined ? DASH : quote.repegs) +
          '</td>';
        html += '<td>' + escapeHtml((quote.reason || order.reason || '') +
          flickerWords(quote)) + '</td>';
        html += '<td><button class="btn cancel-order">Cancel</button></td>';
        html += '</tr>';
        // AN ORDER THAT IS NOT AT THE BROKER, ACROSS THE WHOLE ROW.
        //
        // The reason was already here, in the eleventh column — which
        // is off the right edge on any normal window, and the panel
        // scrolls sideways to reach it. So an order that had never
        // been placed read exactly like one that was working, and the
        // one line explaining it was somewhere nobody scrolls to.
        //
        // Its own row, spanning the table, so no scrolling can hide it.
        var whyNot = order.state === 'WORKING' && !quote.ticket &&
          quote.intent !== 'CLOSE' && !(quote.held && !quote.reason)
          ? (quote.reason || order.reason ||
             'not at the broker yet — no reason given')
          : null;
        if (whyNot) {
          html += '<tr class="not-placed"><td colspan="' + columns + '">' +
            'NOT AT THE BROKER: ' + escapeHtml(whyNot) + '</td></tr>';
        }
      });
    });
    // W3 maker fills: both legs resting at OKX right now.
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      (row.maker_fills || []).forEach(function (job) {
        any = true;
        var legs = job.legs || [];
        html += '<tr data-pair="' + escapeHtml(key) + '" data-side="' +
          job.side + '"' + (job.position_id ? ' data-position="' +
          escapeHtml(job.position_id) + '"' : '') + '>';
        html += '<td>' + (row.name || key) + '</td>';
        html += '<td>' + job.side + '</td>';
        html += '<td>' + fmt(job.level, 4) + '</td>';
        html += '<td>' + qty(job.spreads) + '</td>';
        html += '<td>' + DASH + '</td>';
        html += '<td>' + (job.kind === 'close' ? 'MAKER CLOSE'
          : job.phase === 'recovering' ? 'RECOVERING' : 'MAKER') + '</td>';
        html += '<td>' + DASH + '</td>';
        html += '<td>' + legs.map(function (leg) {
          return leg.leg + ' ' + fmt(leg.resting_at, 2);
        }).join('<br>') + '</td>';
        html += '<td>both legs<div class="hint">' + legs.map(function (leg) {
          return leg.leg + ' ' + leg.side + ' ' + qty(leg.filled) + '/' +
            qty(leg.planned);
        }).join(' · ') + '</div></td>';
        html += '<td>' + legs.reduce(function (n, leg) {
          return n + (leg.amends || 0);
        }, 0) + '</td>';
        html += '<td>' + escapeHtml(job.reason || makerWords(job)) + '</td>';
        html += '<td>' + (job.kind === 'close'
          ? '<button class="btn close-now" title="stop waiting: close ' +
            'what is left at market, by ticket">Market now</button>'
          : '<button class="btn cancel-maker">Cancel</button>') + '</td>';
        html += '</tr>';
      });
    });
    // What stopped working recently, and why — in the broker's own
    // words. Without this a rejected order simply vanished.
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      (row.dead_orders || []).forEach(function (order) {
        any = true;
        html += '<tr class="dead">';
        html += '<td>' + (row.name || key) + '</td>';
        html += '<td>' + order.side + '</td>';
        html += '<td>' + fmt(order.level, 4) + '</td>';
        html += '<td>' + qty(order.quantity) + '</td>';
        html += '<td>' + order.time_in_force + '</td>';
        html += '<td class="mismatch">' + order.state + '</td>';
        html += '<td>' + (order.pending_ticket || DASH) + '</td>';
        html += '<td>' + DASH + '</td><td>' + DASH + '</td><td>' + DASH +
          '</td>';
        html += '<td class="reason">' + (order.reason || '') + '</td>';
        html += '<td></td></tr>';
      });
    });
    if (!any) { html += '<tr><td colspan="12">nothing working</td></tr>'; }
    html += '</tbody></table>';
    html += '<div class="note">GTC here means: until cancelled, or until ' +
      'this system stops. A synthetic order lives in the coordinator, and ' +
      'nothing watches the spread while it is down — so neither DAY ' +
      'nor GTC survives a restart. The real pending behind a LIMIT order ' +
      'DOES survive at the broker, which is why it is swept at shutdown ' +
      'and again at startup.</div>';
    return html;
  }

  function fillsTable() {
    var snapshot = state.snapshot;
    var html = '<table><thead><tr><th>What</th><th>Value</th></tr></thead>' +
      '<tbody>';
    html += timingRow('fill → hedge on (LIMIT)', snapshot.hedge_times_ms,
                      'against the 2.0s escalation deadline');
    html += timingRow('click → both legs on (MARKET)',
                      snapshot.click_to_on_ms, 'measured, not assumed');
    html += '</tbody></table>';

    var journal = state.fills;
    html += '<div class="journal-controls">' +
      '<label class="check"><input type="checkbox" class="ours-only"' +
      (state.fillsFilter.ours ? ' checked' : '') + '> ours only</label>' +
      '<a class="btn" href="/api/fills.csv" download>Export CSV</a>' +
      '<a class="btn" href="/api/algo_signals.csv" download ' +
      'title="Every Algo entry and exit signal: time, z, spread, and what ' +
      'an exit would have made after costs. Signals only — none was sent.">' +
      'Algo signals CSV</a>' +
      '<span class="hint">Read back from OKX\'s own fill history — so it ' +
      'carries the trader\'s terminal clicks too, marked as not ours.' +
      '</span></div>';

    if (journal === null) {
      return html + '<p class="note">loading the journal…</p>';
    }
    if (journal.error) {
      return html + '<p class="note mismatch">' + journal.error + '</p>';
    }

    var totals = journal.totals || {};
    /* A CHARGE NOBODY MEASURED IS NOT A CHARGE OF ZERO.
     *
     * These two used to be COALESCE(SUM(...), 0) over a column that was
     * NULL on every row, so the header stated "commission $0.00" for a
     * cost the broker had really taken. It is the counterweight to the
     * TYPED COMMISSION_PER_LOT the P&L is marked with, and a
     * counterweight that always reads zero can never disagree.
     *
     * `n_measured` is how many fills carried a figure: none means "—",
     * some-but-not-all means the total is real but PARTIAL, and saying
     * which is the difference between a number and a guess.
     */
    function brokerCharge(value, measured, fills) {
      if (!measured) { return DASH; }
      return money(value) +
        (measured < fills ? ' (' + measured + ' of ' + fills + ')' : '');
    }
    // WHAT the totals cover: the whole journal, not one session - and
    // whether the terminal's own clicks are in them.
    function day(ms) {
      return ms ? new Date(ms).toISOString().slice(0, 10) : DASH;
    }
    html += '<div class="hint">Totals: every fill in the journal, ' +
      day(totals.first_ms) + ' to ' + day(totals.last_ms) +
      ' (broker dates), ' + (state.fillsFilter.ours
        ? "this desk's only" : "this desk's AND the terminal's own clicks") +
      '.' + ((totals.by_account || []).length > 1
        ? ' By account: ' + totals.by_account.map(function (a) {
            return a.account + ' ' + money(a.profit) + ' (' + a.fills +
              ' fills, ' + day(a.first_ms) + ' to ' + day(a.last_ms) + ')';
          }).join(' · ')
        : '') + '</div>';
    html += '<table><tbody><tr><td>' + (totals.fills || 0) + ' fills</td>' +
      '<td>' + fmt(totals.volume, 2) + ' lots</td>' +
      '<td>commission ' + brokerCharge(totals.commission,
                                       totals.commission_measured,
                                       totals.fills) + '</td>' +
      '<td>swap ' + brokerCharge(totals.swap, totals.swap_measured,
                                 totals.fills) + '</td>' +
      '<td>the broker\'s own P&amp;L ' +
      '<b class="' + upDown(totals.profit) + '">' + money(totals.profit) +
      '</b></td></tr></tbody></table>';

    html += '<table><thead><tr><th>Broker time</th><th>Account</th>' +
      '<th>Symbol</th><th>Pair</th><th>Leg</th><th>Side</th><th>In/Out</th>' +
      '<th>Volume</th><th>Price</th><th>Comm</th><th>Swap</th>' +
      '<th>P&amp;L</th><th>Ticket</th><th>Deal</th><th>Ours</th>' +
      '<th>Comment</th></tr></thead><tbody>';
    (journal.fills || []).forEach(function (fill) {
      html += '<tr class="' + (fill.is_ours ? '' : 'theirs') + '">';
      html += '<td>' + brokerTime(fill) + '</td>';
      html += '<td>' + (fill.account || DASH) + '</td>';
      html += '<td>' + (fill.symbol || DASH) + '</td>';
      html += '<td>' + (fill.pair_key || DASH) + '</td>';
      html += '<td>' + (fill.leg || DASH) + '</td>';
      html += '<td>' + (fill.side || DASH) + '</td>';
      html += '<td>' + (fill.entry || DASH) + '</td>';
      html += '<td>' + fmt(fill.volume, 2) + '</td>';
      html += '<td>' + fmt(fill.price, 4) + '</td>';
      html += '<td>' + money(fill.commission) + '</td>';
      html += '<td>' + money(fill.swap) + '</td>';
      // The broker's own P&L on this deal: green made, red lost. An
      // opening deal books nothing, so 0.00 stays black — colouring it
      // green would make every entry look like a winner.
      html += moneyCell(fill.profit);
      html += '<td>' + (fill.position_ticket || DASH) + '</td>';
      html += '<td>' + (fill.deal_id || DASH) + '</td>';
      html += '<td>' + (fill.is_ours ? 'yes' : 'no') + '</td>';
      html += '<td>' + (fill.comment || '') + '</td>';
      html += '</tr>';
    });
    if (!(journal.fills || []).length) {
      html += '<tr><td colspan="16">no fills recorded yet</td></tr>';
    }
    return html + '</tbody></table>';
  }

  function upDown(value) {
    if (value === null || value === undefined || !isFinite(value) ||
        value === 0) {
      return '';
    }
    return value > 0 ? 'up' : 'down';
  }

  function moneyCell(value) {
    return '<td class="' + upDown(value) + '">' + money(value) + '</td>';
  }

  function brokerTime(fill) {
    // The BROKER's wall clock, which is what MT5's own History shows.
    // Rendering it in the browser's zone would put every row hours away
    // from the same trade in the terminal.
    if (!fill.broker_time_ms) { return DASH; }
    // WITH the date: a journal that spans days read as one session.
    var stamp = new Date(fill.broker_time_ms);
    return stamp.toISOString().slice(0, 10) + ' ' +
      stamp.toISOString().slice(11, 19) +
      (fill.server_offset_s === null || fill.server_offset_s === undefined
        ? '' : ' (broker)');
  }

  function loadFills(force) {
    var now = Date.now();
    if (!force && state.fills !== null && now - state.fillsAt < 5000) {
      return;
    }
    state.fillsAt = now;
    var query = state.fillsFilter.ours ? '?ours=1' : '';
    fetch('/api/fills' + query).then(function (r) { return r.json(); })
      .then(function (body) {
        state.fills = body.ok ? body : {error: body.error};
        render();
      })
      .catch(function (error) {
        state.fills = {error: 'the journal could not be read: ' +
                              error.message};
      });
  }

  // -- the slippage report -----------------------------------------------

  function slipPoints(value) {
    // Cost is red, improvement is green, unmeasured is a dash. The
    // colour is the whole point of the column: a table of black
    // numbers is one nobody reads twice.
    if (value === null || value === undefined) { return '<td>' + DASH + '</td>'; }
    return '<td class="' + (value > 0 ? 'down' : value < 0 ? 'up' : '') +
      '">' + fmt(value, 4) + '</td>';
  }

  function slipMoney(value) {
    if (value === null || value === undefined) { return '<td>' + DASH + '</td>'; }
    return '<td class="' + (value > 0 ? 'down' : value < 0 ? 'up' : '') +
      '">' + money(value) + '</td>';
  }

  function slipRow(label, stats) {
    if (!stats) { return ''; }
    var html = '<tr><td>' + label + '</td>';
    html += '<td>' + stats.measured + '</td>';
    // Unmeasured stands in its own column, in words, so it can never be
    // mistaken for a zero in the mean.
    html += '<td>' + (stats.unmeasured
      ? '<span class="hint">' + stats.unmeasured + ' unmeasured</span>'
      : '') + '</td>';
    html += slipPoints(stats.points_mean);
    html += slipPoints(stats.points_median);
    html += slipPoints(stats.points_worst);
    html += slipPoints(stats.points_best);
    html += slipMoney(stats.money_total);
    html += '<td>' + (stats.measured
      ? stats.paid + ' paid / ' + stats.earned + ' earned' : DASH) + '</td>';
    return html + '</tr>';
  }

  function slipHead(first) {
    return '<thead><tr><th>' + first + '</th><th>Measured</th><th></th>' +
      '<th>Mean</th><th>Median</th><th>Worst</th><th>Best</th>' +
      '<th>Cost</th><th>Split</th></tr></thead>';
  }

  function slipCard(label, value, sub, cls) {
    return '<div class="slip-card"><div class="k">' + label + '</div>' +
      '<div class="v ' + (cls || '') + '">' + value + '</div>' +
      '<div class="s">' + sub + '</div></div>';
  }

  function slipCost(value) {
    /* Money, coloured as a cost (red) or a gain (green); unmeasured "—". */
    if (value === null || value === undefined) { return DASH; }
    return money(value);
  }

  function costClass(value) {
    return value === null || value === undefined ? ''
      : value > 0 ? 'down' : value < 0 ? 'up' : '';
  }

  function slipBar(label, part) {
    /* One end's slippage as its two parts: the market moving while the
     * legs were sent, and the broker filling off the price sent. */
    part = part || {};
    if (!part.measured) {
      return '<div class="slip-split"><b>' + label + '</b><div class="hint">' +
        'not measured \u2014 a leg\u2019s send price was not kept</div></div>';
    }
    var moved = part.moved_money || 0, broker = part.broker_money || 0;
    var total = Math.abs(moved) + Math.abs(broker);
    var pm = total ? Math.abs(moved) / total * 100 : 50;
    function seg(cls, width, text) {
      return width > 0 ? '<div class="' + cls + '" style="width:' + width +
        '%">' + text + '</div>' : '';
    }
    return '<div class="slip-split"><b>' + label + '</b><div class="slip-bar">' +
      (total ? seg('moved', pm, 'market moved while sending ' +
                   fmt(part.moved_points_mean, 4) + ' \u00b7 ' + money(moved)) +
        seg('broker', 100 - pm, 'broker fill ' +
            fmt(part.broker_points_mean, 4) + ' \u00b7 ' + money(broker))
        : '<div class="none" style="width:100%">no slippage</div>') +
      '</div></div>';
  }

  function slipDetailTables(report) {
    var html = '';
    html += '<table>' + slipHead('This session');
    html += '<tbody>';
    html += slipRow('Entries', report.overall.entry);
    html += slipRow('Exits', report.overall.exit);
    html += slipRow('Round turn', report.overall.round_trip);
    html += '</tbody></table>';

    // MARKET against LIMIT, side by side. This is the split the peg has
    // to justify itself against: a LIMIT that is not beating a market
    // click on the same ladder is costing time for nothing.
    html += '<table>' + slipHead('Entries by order type') + '<tbody>';
    Object.keys(report.by_order_type).forEach(function (type) {
      html += slipRow(type, report.by_order_type[type].entry);
    });
    html += '</tbody></table>';

    html += '<table>' + slipHead('Entries by ladder') + '<tbody>';
    Object.keys(report.by_pair).forEach(function (key) {
      html += slipRow(report.by_pair[key].name || key,
                      report.by_pair[key].entry);
    });
    html += '</tbody></table>';

    return html;
  }

  function slipFillsHtml(f, byPair) {
    /* EVERY FILL: each leg, each end, the price it was sent at against
     * the price OKX filled it at. Said first and in one line - how many
     * filled exactly where sent - because 'was there slippage on my
     * fills?' is the question this tab is opened to answer. */
    if (!f || !f.rows || !f.rows.length) { return ''; }
    var s = f.summary || {};
    function names(key) { return escapeHtml(((byPair[key] || {}).name) || key); }
    var head = s.measured
      ? '<b>' + s.exact + ' of ' + s.measured + '</b> leg fills filled ' +
        'exactly at the price sent' +
        (s.worse ? ' \u00b7 <span class="down"><b>' + s.worse +
          '</b> worse, ' + money(-s.worse_money) + '</span>' : '') +
        (s.better ? ' \u00b7 <span class="up"><b>' + s.better +
          '</b> better, ' + money(-s.better_money) + '</span>' : '') +
        (s.unmeasured ? ' \u00b7 ' + s.unmeasured + ' not measured' : '')
      : 'no fill with its send price recorded yet';
    var html = '<h4 class="slip-h">Slippage on every fill <small>\u2014 ' +
      'each leg against the price it was sent at; positive is a cost. A ' +
      'post-only maker fill fills at its own price, so it reads 0.' +
      '</small></h4><div class="slip-fills-head">' + head + '</div>' +
      '<table class="journal slip-fills"><thead><tr><th>Time</th>' +
      '<th class="l">Ladder</th><th class="l">End</th><th class="l">Leg</th>' +
      '<th class="l">Side</th><th>Lots</th><th>Sent at</th><th>Filled at</th>' +
      '<th>Slip</th><th>bp</th><th>$</th></tr></thead><tbody>';
    f.rows.forEach(function (r) {
      var cls = r.slip === null || r.slip === undefined ? ''
        : r.slip > 1e-9 ? 'down' : (r.slip < -1e-9 ? 'up' : '');
      var when = r.at ? new Date(r.at * 1000) : null;
      html += '<tr><td>' + (when ? when.toLocaleString(undefined, {
          month: 'short', day: 'numeric', hour: '2-digit',
          minute: '2-digit', second: '2-digit'}) : DASH) + '</td>' +
        '<td class="l">' + names(r.pair_key) + '</td>' +
        '<td class="l">' + (r.end === 'entry' ? 'Entry' : 'Exit') + '</td>' +
        '<td class="l">' + r.leg + ' ' + escapeHtml(String(r.symbol || ''))
          + '</td><td class="l">' + escapeHtml(r.side || DASH) + '</td>' +
        '<td>' + fmt(r.volume, 2) + '</td>' +
        '<td>' + fmt(r.sent, 4) + '</td><td>' + fmt(r.filled, 4) + '</td>' +
        '<td class="' + cls + '">' + (r.slip === null || r.slip === undefined
          ? DASH : fmt(r.slip, 4)) + '</td>' +
        '<td class="' + cls + '">' + (r.slip_bps === null ||
          r.slip_bps === undefined ? DASH : fmt(r.slip_bps, 2)) + '</td>' +
        '<td class="' + cls + '">' + (r.slip_money === null ||
          r.slip_money === undefined ? DASH : money(r.slip_money)) +
        '</td></tr>';
    });
    return html + '</tbody></table>';
  }

  function slippageTable() {
    var report = state.slippage;
    var html = '<div class="journal-controls">' +
      '<label class="check"><input type="checkbox" class="all-sessions"' +
      (state.slippageAll ? ' checked' : '') + '> all trades</label>' +
      '<a class="btn" href="/api/slippage.csv' +
      (state.slippageAll ? '?session=all' : '') + '" download>Export CSV</a>' +
      '<span class="hint">Measured on the fills the broker actually gave ' +
      'us. Positive is a cost at both ends.</span></div>';

    if (report === null) { return html + '<p class="note">loading…</p>'; }
    if (report.error) {
      return html + '<p class="note mismatch">' + report.error + '</p>';
    }

    var window_ = report.window || {};
    var journal = report.journal;
    html += '<div class="hint">Showing <b>' + (window_.label || DASH) +
      '</b> \u00b7 ' + report.counts.positions + ' positions (' +
      report.counts.open + ' still open) \u00b7 ' + (journal
        ? journal.fills + ' of our fills at the broker over the same window'
        : 'the journal could not be counted') + '</div>';
    if (window_.note) {
      html += '<div class="' + (window_.clock !== 'machine' ? 'hint' : 'note') +
        '">' + window_.note + '</div>';
    }

    if (!report.counts.positions) {
      return html + '<p class="note">nothing was traded in this window. ' +
        'That is not a slippage of zero \u2014 there is nothing to measure ' +
        'yet.' + (state.slippageAll ? '' : ' Tick <b>all trades</b> for ' +
        'earlier ones.') + '</p>';
    }

    var o = report.overall || {};
    var entry = o.entry || {}, exit_ = o.exit || {}, rt = o.round_trip || {};
    var pnl = report.pnl || {};
    var legs = (report.by_account || []).length;
    html += '<div class="slip-cards">' +
      slipCard('Slippage cost', slipCost(rt.measured ? rt.money_total
                                                     : entry.money_total),
               (rt.measured ? rt.measured + ' round turn' +
                 (rt.measured > 1 ? 's' : '') : 'entries only so far') +
               ' \u00b7 ' + legs + ' leg' + (legs === 1 ? '' : 's') +
               (journal ? ' \u00b7 ' + journal.fills + ' fills' : ''),
               costClass(rt.measured ? rt.money_total : entry.money_total)) +
      slipCard('Entries', slipCost(entry.money_total),
               entry.measured ? 'avg ' + fmt(entry.points_mean, 4) +
                 ' per spread \u00b7 ' + entry.paid + ' paid / ' +
                 entry.earned + ' earned' : 'not measured',
               costClass(entry.money_total)) +
      slipCard('Exits', slipCost(exit_.money_total),
               exit_.measured ? 'avg ' + fmt(exit_.points_mean, 4) +
                 ' per spread \u00b7 ' + exit_.paid + ' paid / ' +
                 exit_.earned + ' earned' : 'no closed trade yet',
               costClass(exit_.money_total)) +
      slipCard('Share of the P&amp;L', pnl.share === null ||
                 pnl.share === undefined ? DASH
                 : (pnl.share * 100).toFixed(1) + '%',
               !pnl.trades ? 'no closed trade measured yet'
                 : pnl.share === null || pnl.share === undefined
                   ? 'the trades lost ' + money(-pnl.net) + ' net \u2014 ' +
                     'slippage added ' + money(pnl.slippage)
                   : money(pnl.slippage) + ' of ' + money(pnl.before) +
                     ' before slippage \u00b7 net ' + money(pnl.net)) +
      slipCard('Click \u2192 both legs on',
               report.click_to_on_median_ms === null ||
                 report.click_to_on_median_ms === undefined ? DASH
                 : Math.round(report.click_to_on_median_ms) + ' ms',
               'median this session') +
      '</div>';

    html += slipFillsHtml(report.fills, report.by_pair || {});

    // WHERE IT CAME FROM: the part a faster send would save, and the
    // part the broker's fills cost.
    var split = report.split || {};
    html += '<h4 class="slip-h">Where the slippage came from</h4>' +
      '<div class="slip-splits">' + slipBar('Entries', split.entry) +
      slipBar('Exits', split.exit) + '</div>' +
      '<div class="hint"><b class="moved-t">Market moved while sending</b>: ' +
      'the spread changed between the decision and the moment the legs ' +
      'were sent (latency). <b class="broker-t">Broker fill</b>: a leg ' +
      'filled worse than the price it was sent at.</div>';

    // BY ACCOUNT: which broker the slippage comes from.
    var info = report.account_info || {};
    html += '<h4 class="slip-h">By account (broker)</h4><table>' +
      '<thead><tr><th>Leg</th><th>Symbol</th><th>Account \u00b7 broker</th>' +
      '<th>Fills</th><th>Entry avg</th><th>Exit avg</th><th>Worst</th>' +
      '<th>Cost</th><th>Paid / earned</th></tr></thead><tbody>';
    (report.by_account || []).forEach(function (a) {
      var who = info[a.account] || {};
      html += '<tr><td>' + escapeHtml(a.legs || '') + '</td><td>' +
        escapeHtml(a.symbol || DASH) + '</td><td>' +
        escapeHtml(a.account || DASH) +
        (who.login ? ' \u00b7 #' + escapeHtml(String(who.login)) : '') +
        (who.broker ? ' \u00b7 ' + escapeHtml(who.broker) : '') + '</td>' +
        '<td>' + a.fills + '</td>' +
        withBps(slipPoints(a.entry_points_mean), a.entry_bps_mean) +
        withBps(slipPoints(a.exit_points_mean), a.exit_bps_mean) +
        slipPoints(a.points_worst) +
        slipMoney(a.money_total) + '<td>' + a.paid + ' / ' + a.earned +
        '</td></tr>';
    });
    html += '</tbody></table>';

    // EVERY TRADE, newest first.
    html += '<h4 class="slip-h">Trades</h4><table><thead><tr>' +
      '<th>Opened</th><th>Ladder</th><th>Side</th><th>Qty</th><th>Type</th>' +
      '<th>Entry slip</th><th>Exit slip</th><th>Round turn</th>' +
      '<th>Moved / broker</th><th>Click\u2192on</th><th>Net P&amp;L</th>' +
      '</tr></thead><tbody>';
    (report.rows || []).slice(0, 50).forEach(function (r) {
      var moved = r.entry_moved_money, broker = r.entry_broker_money;
      if (!r.open && moved !== null && moved !== undefined &&
          r.exit_moved_money !== null && r.exit_moved_money !== undefined) {
        moved += r.exit_moved_money;
        broker += r.exit_broker_money;
      } else if (!r.open) {
        moved = broker = null;
      }
      function both(points, cash) {
        return points === null || points === undefined
          ? '<td>' + DASH + '</td>'
          : '<td class="' + costClass(points) + '">' + fmt(points, 4) +
            ' \u00b7 ' + slipCost(cash) + '</td>';
      }
      html += '<tr><td>' + localTime(r.opened_at) + '</td><td>' +
        escapeHtml((report.by_pair[r.pair_key] || {}).name || r.pair_key ||
                   DASH) + '</td><td>' + (r.side || DASH) + '</td><td>' +
        qty(r.quantity) + '</td><td>' + (r.order_type || DASH) + '</td>' +
        both(r.entry_points, r.entry_money) +
        (r.open ? '<td><span class="hint">open</span></td>'
          : both(r.exit_points, r.exit_money)) +
        (r.open ? '<td>' + DASH + '</td>' : slipMoney(r.round_trip_money)) +
        '<td>' + (moved === null || moved === undefined ? DASH
          : money(moved) + ' / ' + money(broker)) + '</td>' +
        '<td>' + (r.click_to_on_ms === null || r.click_to_on_ms === undefined
          ? DASH : Math.round(r.click_to_on_ms) + ' ms') + '</td>' +
        (r.open ? '<td><span class="hint">open</span></td>'
          : '<td class="' + costClass(-(r.realized_pnl || 0)) + '">' +
            (r.realized_pnl === null || r.realized_pnl === undefined ? DASH
              : money(r.realized_pnl)) + '</td>') + '</tr>';
    });
    html += '</tbody></table>';

    // OKX: was each fill maker or taker, and how long the second leg
    // took after the first - from OKX's own fills.
    html += fillQualityHtml(report.fill_quality);

    // The full statistics, one click away.
    html += '<details class="slip-detail"' +
      (state.slipDetailOpen ? ' open' : '') + '><summary>Statistics: mean, ' +
      'median, worst and best, by order type and by ladder</summary>' +
      slipDetailTables(report) + '</details>';
    return html;
  }

  function withBps(cell, bps) {
    if (bps === null || bps === undefined) { return cell; }
    return cell.replace('</td>', ' <span class="hint">(' + bpsText(bps) +
                        ')</span></td>');
  }

  function bpsText(x) {
    return (x > 0 ? '+' : '') + Number(x).toFixed(2) + ' bp';
  }

  function fillQualityHtml(q) {
    /* From OKX's own fills: was each fill maker or taker, and how long
     * did the second leg take after the first. */
    if (!q) { return ''; }
    var html = '';
    html += '<table><thead><tr><th>Maker or taker (OKX\u2019s own ' +
      'fills)</th><th>Leg</th><th>End</th><th>Fills</th><th>Maker</th>' +
      '<th>Taker</th><th>Taker share</th><th>Fees paid</th>' +
      '<th title="What the taker fills cost over this account\u2019s maker ' +
      'rate">Extra for taker</th></tr></thead><tbody>';
    if (!(q.liquidity || []).length) {
      html += '<tr><td colspan="9"><span class="hint">no fills of ours in ' +
        'this window yet</span></td></tr>';
    }
    (q.liquidity || []).forEach(function (r) {
      var share = r.taker_share;
      html += '<tr>' +
        '<td>' + escapeHtml(pairName(r.pair_key)) + '</td>' +
        '<td>' + escapeHtml(r.leg) + ' <span class="hint">' +
        escapeHtml(r.symbol || '') + '</span></td>' +
        '<td>' + escapeHtml(r.end) + '</td>' +
        '<td>' + r.fills + '</td>' +
        '<td>' + r.maker_fills + ' <span class="hint">' +
        fmt(r.maker_volume, 2) + ' lots</span></td>' +
        '<td>' + r.taker_fills + ' <span class="hint">' +
        fmt(r.taker_volume, 2) + ' lots</span></td>' +
        '<td class="' + (share ? 'down' : '') + '">' +
        (share === null || share === undefined ? DASH
         : Math.round(share * 100) + '%') + '</td>' +
        '<td>' + smallMoney(r.fees) + '</td>' +
        '<td>' + (r.extra === null || r.extra === undefined ? DASH
                  : smallMoney(r.extra)) + '</td></tr>';
    });
    html += '</tbody></table>';

    var eg = q.entry_gap || {}, xg = q.exit_gap || {};
    function ms(x) {
      if (x === null || x === undefined) { return DASH; }
      return x >= 1000 ? (x / 1000).toFixed(1) + ' s' : Math.round(x) + ' ms';
    }
    html += '<table><thead><tr><th>Gap between the legs</th>' +
      '<th>Side</th><th>Entry gap</th><th>Filled first</th>' +
      '<th>Exit gap</th><th>Filled first</th><th>Opened</th></tr>' +
      '<tr class="hint"><td>all</td><td></td><td>mean ' + ms(eg.mean_ms) +
      ' · worst ' + ms(eg.worst_ms) + '</td><td></td><td>mean ' +
      ms(xg.mean_ms) + ' · worst ' + ms(xg.worst_ms) +
      '</td><td></td><td></td></tr></thead><tbody>';
    (q.gaps || []).slice(0, 20).forEach(function (g) {
      html += '<tr><td>' + escapeHtml(pairName(g.pair_key)) + '</td>' +
        '<td>' + escapeHtml(g.side || DASH) + '</td>' +
        '<td>' + ms(g.entry_gap_ms) + '</td>' +
        '<td>' + escapeHtml(g.entry_first || DASH) + '</td>' +
        '<td>' + ms(g.exit_gap_ms) + '</td>' +
        '<td>' + escapeHtml(g.exit_first || DASH) + '</td>' +
        '<td>' + localTime(g.opened_at) + '</td></tr>';
    });
    html += '</tbody></table>';
    html += '<div class="note">Maker or taker is OKX\u2019s own marking of ' +
      'each fill; a taker share above 0% is in red. ' +
      'The gap is from one leg\u2019s last fill to the other\u2019s: under ' +
      'the maker fill, that is the time the first leg is on alone.</div>';
    return html;
  }

  function pairName(key) {
    var pair = ((state.snapshot || {}).pairs || {})[key] || {};
    return pair.name || key || DASH;
  }

  function localTime(seconds) {
    if (!seconds) { return DASH; }
    return new Date(seconds * 1000).toTimeString().slice(0, 8);
  }

  function loadSlippage(force) {
    var now = Date.now();
    if (!force && state.slippage !== null && now - state.slippageAt < 5000) {
      return;
    }
    state.slippageAt = now;
    fetch('/api/slippage' + (state.slippageAll ? '?session=all' : ''))
      .then(function (r) { return r.json(); })
      .then(function (body) {
        state.slippage = body.ok ? body : {error: body.error};
        render();
      })
      .catch(function (error) {
        state.slippage = {error: 'the report could not be read: ' +
                                 error.message};
      });
  }

  // -- the Analysis tab ----------------------------------------------------

  function loadAnalysis(force) {
    var now = Date.now();
    if (!force && state.analysis !== null && now - state.analysisAt < 5000) {
      return;
    }
    state.analysisAt = now;
    fetch('/api/analysis?days=' + state.analysisDays +
          '&zone_days=' + state.zoneDays +
          (state.zoneDate ? '&date=' + encodeURIComponent(state.zoneDate) : '') +
          (state.volumeFrom ? '&volume_from=' +
           encodeURIComponent(state.volumeFrom) : '') +
          (state.volumeTo ? '&volume_to=' +
           encodeURIComponent(state.volumeTo) : '') +
          (state.analysisPair ? '&pair=' +
           encodeURIComponent(state.analysisPair) : ''))
      .then(function (r) { return r.json(); })
      .then(function (body) {
        state.analysis = body.ok ? body : {error: body.error};
        render();
      })
      .catch(function (error) {
        state.analysis = {error: 'the analysis could not be read: ' +
                                 error.message};
      });
  }

  function runAnalysisBacktest() {
    /* The Algo's own backtest, for the ladder picked above. Kept in
     * state: the pane is redrawn on every snapshot. */
    var key = state.analysisPair;
    if (!key) {
      state.analysisBt = {html: 'pick one ladder above \u2014 a backtest ' +
                                'replays one ladder\u2019s Algo', cls: 'down'};
      return render();
    }
    var days = state.analysisBtDays;
    state.analysisBt = {html: 'running on the last ' + days +
                              ' days\u2026', cls: 'hint', running: true};
    render();
    function done(html, cls) {
      state.analysisBt = {html: html, cls: cls || ''};
      render();
    }
    fetch('/api/command', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: 'algo_backtest',
                            payload: {pair: key, days: days}})
    }).then(function (r) { return r.json(); }).then(function (sent) {
      if (!sent.ok) { return done(escapeHtml(sent.error || 'refused'), 'down'); }
      var tries = 0;
      (function poll() {
        fetch('/api/result/' + sent.id).then(function (r) { return r.json(); })
          .then(function (result) {
            if (result && result.pending) {
              if (++tries > 120) {
                return done('no answer from the engine yet \u2014 try again',
                            'down');
              }
              return window.setTimeout(poll, 250);
            }
            var data = (result && result.data) || {};
            if (!result || result.ok === false || data.ok === false) {
              return done('could not run: ' + escapeHtml(
                (result && result.error) || data.reason || 'unknown'), 'down');
            }
            done(backtestHtml(data));
          });
      })();
    }).catch(function (error) { done(escapeHtml(error.message), 'down'); });
  }

  function anCapital() {
    /* The capital managed, as the last analysis read it - or as just
     * typed, until the next one reads it back. 0 / null: not set. */
    if (state.capitalManaged !== undefined) { return state.capitalManaged; }
    var s = (state.analysis || {}).summary || {};
    // The account balance stands in when nothing is typed: it is the
    // placeholder, not a saved setting.
    if (s.capital_source === 'balance') { return null; }
    return s.capital || null;
  }

  function anBalanceCapital() {
    var s = (state.analysis || {}).summary || {};
    return s.capital_source === 'balance' && state.capitalManaged === undefined
      ? s.capital : null;
  }

  function saveCapital(value) {
    var number = value === '' ? 0 : Number(value);
    if (!isFinite(number) || number < 0) { return; }
    state.capitalManaged = number || null;
    fetch('/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({fields: {CAPITAL_MANAGED: number}})
    }).then(function (r) { return r.json(); }).then(function () {
      delete state.capitalManaged;
      loadAnalysis(true);
    }).catch(function () { loadAnalysis(true); });
  }

  function anTile(label, value, cls, title) {
    return '<div class="an-tile"' + (title ? ' title="' + escapeHtml(title) +
      '"' : '') + '><div class="an-val ' + (cls || '') + '">' + value +
      '</div><div class="an-lbl">' + label + '</div></div>';
  }

  function anSign(value) {
    return value === null || value === undefined ? '' :
      (value > 0 ? 'up' : (value < 0 ? 'down' : ''));
  }

  function anPct(value, digits) {
    return value === null || value === undefined ? DASH :
      Number(value).toFixed(digits === undefined ? 1 : digits) + '%';
  }

  function anMin(value) {
    return value === null || value === undefined ? DASH :
      Number(value).toFixed(1) + ' min';
  }

  // MAKER OR TAKER per leg and end, from OKX's own fills.
function anFillsCell(t) {
  var f = t.fills || {};
  function k(v) {
    if (!v) return DASH;
    return '<span class="an-liq an-liq-' + (v === 'M' ? 'm' : v === 'T'
      ? 't' : 'mt') + '">' + v + '</span>';
  }
  var any = f.entry_a || f.entry_b || f.exit_a || f.exit_b;
  if (!any) return '<td class="hint">' + DASH + '</td>';
  return '<td class="an-fills" title="' + (t.maker_fills || 0) +
    ' maker fill(s), ' + (t.taker_fills || 0) + ' taker fill(s)">' +
    k(f.entry_a) + '/' + k(f.entry_b) + ' \u2192 ' + k(f.exit_a) + '/' +
    k(f.exit_b) + '</td>';
}

// THE FEES one trade paid: OKX's, or the desk's estimate marked ~.
function anFeeCell(t) {
  if (t.fee === null || t.fee === undefined) return '<td>' + DASH + '</td>';
  var bp = t.fees_bp_notional === null || t.fees_bp_notional === undefined
    ? '' : ' \u00b7 ' + fmt(t.fees_bp_notional, 1) + ' bp of notional';
  if (t.fees_source === 'OKX' || t.fees_source === 'OKX ledger') {
    // THE BREAKDOWN, as OKX charged it: each leg and end, maker and
    // taker apart.
    var parts = t.fee_parts || {};
    var lines = [['entry_a', 'Leg A in'], ['entry_b', 'Leg B in'],
                 ['exit_a', 'Leg A out'], ['exit_b', 'Leg B out']]
      .filter(function (p) { return parts[p[0]]; })
      .map(function (p) {
        var x = parts[p[0]];
        var bits = [];
        if (x.maker) { bits.push('maker ' + money(x.maker)); }
        if (x.taker) { bits.push('taker ' + money(x.taker)); }
        return p[1] + ': ' + money((x.maker || 0) + (x.taker || 0)) +
          ' (' + bits.join(' + ') + ')';
      });
    return '<td class="an-fee" title="' + escapeHtml(
      (lines.length ? lines.join('\n') + '\n' : '') + 'Total ' +
      money(t.fee) + ' = maker ' + money(t.maker_fees) + ' + taker ' +
      money(t.taker_fees) + ' (OKX fills)' + (t.fees_source === 'OKX ledger'
        ? '\nOKX ledger: ' + money(t.fee) : '') + bp +
      '\nClick for the calculation') +
      '"><a href="#" class="an-fee-link" data-pid="' +
      escapeHtml(t.position_id) + '">' + money(t.fee) + '</a></td>';
  }
  return '<td class="hint" title="the desk\u2019s estimate (gross less ' +
    'net, at your fee rates): OKX\u2019s own fills for this trade are not ' +
    'in the journal' + bp + '"><a href="#" class="an-fee-link" data-pid="' +
    escapeHtml(t.position_id) + '">~' + money(t.fee) + '</a></td>';
}

// THE FEE CALCULATION, one trade: every fill worked through - contracts
// x contract size x price = notional, the fee OKX charged on it and its
// rate - its legs and ends, the total, and the check against OKX's own
// ledger. Opened from the Fees link; closed by x, Esc or a click outside.
function feeDetailHtml(t, expected) {
  var rows = t.fee_fills || [];
  var head = '<div class="fd-head"><b>Fees \u00b7 ' + (t.trade_no
    ? 'Trade #' + t.trade_no : 'this trade') + '</b> <span>' +
    escapeHtml(t.side === 'SELL' ? 'H to L' : 'L to H') + ' \u00b7 ' +
    qty(t.quantity) + ' Qty \u00b7 closed ' + anClock(t.closed_at) +
    '</span><button class="fd-x" title="Close">\u00d7</button></div>';
  if (!rows.length) {
    return head + '<p class="fd-note">OKX\u2019s fills for this trade are ' +
      'not in the journal, so this is the desk\u2019s ESTIMATE: gross ' +
      money(t.gross_pnl) + ' \u2212 net ' + money(t.pnl) + ' = <b>' +
      money(t.fee) + '</b>' + (t.notional ? ', ' + fmt(t.fee / t.notional *
      1e4, 1) + ' bp of ' + money(t.notional) + ' notional' : '') +
      '.</p>';
  }
  var body = rows.map(function (f) {
    return '<tr><td>' + f.leg + ' ' + escapeHtml(f.symbol || '') +
      '</td><td>' + f.end + '</td><td class="' + (f.kind === 'maker'
        ? 'up' : 'warn') + '">' + escapeHtml(f.kind) + '</td><td>' +
      fmt(f.contracts, 2) + ' \u00d7 ' + (f.contract_size === null ||
        f.contract_size === undefined ? DASH : fmt(f.contract_size, 4)) +
      ' \u00d7 ' + fmt(f.price, 2) + '</td><td>' + money(f.notional) +
      '</td><td>' + (f.bp === null || f.bp === undefined ? DASH
        : fmt(f.bp, 2) + ' bp') + '</td><td><b>' + money(f.fee) +
      '</b></td><td>' + (f.bill_fee === null || f.bill_fee === undefined
        ? DASH : money(f.bill_fee)) + '</td></tr>';
  }).join('');
  var sum = function (k) {
    return rows.reduce(function (s, f) { return s + (Number(f[k]) || 0); },
                       0);
  };
  var parts = t.fee_parts || {};
  var legs = [['entry_a', 'Leg A in'], ['entry_b', 'Leg B in'],
              ['exit_a', 'Leg A out'], ['exit_b', 'Leg B out']]
    .filter(function (p) { return parts[p[0]]; })
    .map(function (p) {
      var x = parts[p[0]];
      return '<tr><td colspan="6">' + p[1] + '</td><td><b>' +
        money((x.maker || 0) + (x.taker || 0)) + '</b></td><td>' +
        (x.maker ? 'maker ' + money(x.maker) : '') +
        (x.maker && x.taker ? ' + ' : '') +
        (x.taker ? 'taker ' + money(x.taker) : '') + '</td></tr>';
    }).join('');
  var check = '';
  if (t.bill_fees !== null && t.bill_fees !== undefined) {
    // The ledger against the FILLS: two OKX records of the same fees.
    var fillsFee = t.fills_fees !== undefined && t.fills_fees !== null
      ? t.fills_fees : sum('fee');
    var gap = Math.abs(t.bill_fees - fillsFee);
    check = '<p class="fd-check ' + (gap < 0.01 ? 'up' : 'down') +
      '">OKX ledger: ' + money(t.bill_fees) + ' on ' + t.bills_matched +
      ' bill(s) for ' + (t.bill_orders || 0) + ' order(s) \u2014 ' +
      (gap < 0.01 ? '\u2713 the same as the fills'
        : 'the fills say ' + money(fillsFee) + ', ' + money(gap) +
          ' apart') + (t.fees_source === 'OKX ledger'
        ? '. The journal uses the ledger\u2019s figure.'
        : gap >= 0.01 ? '. The journal uses the fills\u2019 figure: the ' +
          'ledger read is missing entries for ' + ((t.ledger_short || [])
            .length || 'some') + ' of this trade\u2019s orders (' +
          (t.ledger_short || []).map(function (o) {
            return escapeHtml(o.symbol) + ' order ' +
              escapeHtml(String(o.order_id).slice(-6)) + ': ledger ' +
              money(o.ledger) + ' on ' + o.bills + ' bill(s), fills ' +
              money(o.fills);
          }).join('; ') + ').' : '') + '</p>';
  } else {
    check = '<p class="fd-check">OKX ledger: not read for this trade yet ' +
      '(the desk reads it once a minute).</p>';
  }
  var range = '';
  if (expected && t.notional) {
    range = '<p class="fd-note">At your rates on ' + money(t.notional) +
      ' notional: every fill maker ' + money(t.notional *
        expected.maker / 1e4) + ' (' + fmt(expected.maker, 1) +
      ' bp) \u00b7 every fill taker ' + money(t.notional * expected.taker /
        1e4) + ' (' + fmt(expected.taker, 1) + ' bp). This trade: ' +
      money(t.fee) + ' (' + fmt(t.fees_bp_notional, 1) + ' bp).</p>';
  }
  return head + '<table class="fd-table"><thead><tr><th>Leg</th>' +
    '<th>End</th><th>Fill</th><th>Contracts \u00d7 size \u00d7 price</th>' +
    '<th>Notional</th><th>Rate</th><th>Fee (OKX fill)</th>' +
    '<th>Fee (OKX ledger)</th></tr></thead><tbody>' + body +
    '</tbody><tbody class="fd-sub">' + legs + '</tbody><tfoot><tr>' +
    '<td colspan="4">Total \u00b7 ' + rows.length + ' fill(s)</td><td>' +
    money(sum('notional')) + '</td><td>' + (sum('notional')
      ? fmt(t.fee / sum('notional') * 1e4, 2) + ' bp' : DASH) +
    '</td><td><b>' + money(t.fee) + '</b></td><td>' +
    (t.bill_fees === null || t.bill_fees === undefined ? DASH
      : money(t.bill_fees)) + '</td></tr></tfoot></table>' + check + range +
    '<p class="fd-note">Gross ' + money(t.gross_pnl) + ' \u2212 fees ' +
    money(t.fee) + ' = net <b>' + money(t.pnl) + '</b>. Notional here is ' +
    'both ends of both legs; the journal\u2019s bp is on the entry ' +
    'notional of both legs.</p>';
}

function showFeeDetail(pid) {
  var a = state.analysis || {};
  var t = (a.journal || []).filter(function (r) {
    return String(r.position_id) === String(pid);
  })[0];
  if (!t) { return; }
  var old = document.getElementById('fee-detail');
  if (old) { old.remove(); }
  var shade = document.createElement('div');
  shade.id = 'fee-detail';
  shade.className = 'fd-shade';
  shade.innerHTML = '<div class="fd-box">' +
    feeDetailHtml(t, (a.fee_tally || {}).expected_bp) + '</div>';
  function close() {
    shade.remove();
    document.removeEventListener('keydown', onKey, true);
  }
  function onKey(e) { if (e.key === 'Escape') { close(); } }
  shade.addEventListener('click', function (e) {
    if (e.target === shade || e.target.classList.contains('fd-x')) {
      close();
    }
  });
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(shade);
}

document.addEventListener('click', function (e) {
  var link = e.target.closest && e.target.closest('.an-fee-link');
  if (!link) { return; }
  e.preventDefault();
  showFeeDetail(link.dataset.pid);
});

// THE VOLUME, WORKED OUT: one figure of the Notional traded table, line
// by line - what a deal and a lot are, then per instrument the deals, the
// lots (contracts) x contract size x average price = notional, and the
// total. Opened from the figure; closed by x, Esc or a click outside.
function volumeDetailHtml(c, label) {
  function line(sym, end, x, size, sub) {
    // The instrument named on its first line; its total line bold.
    return '<tr' + (sub ? '' : ' class="fd-sym"') + '><td>' +
      (sym ? escapeHtml(sym) : '') + '</td><td>' + end + '</td><td>' +
      x.deals + '</td><td>' + fmt(x.lots, 2) + '</td><td>' +
      (size === null || size === undefined ? DASH : fmt(size, 4)) +
      '</td><td>' + (size ? fmt(x.lots * size, 4) : DASH) + '</td><td>' +
      (x.price === null || x.price === undefined ? DASH
        : fmt(x.price, 2)) + '</td><td>' + (sub ? '' : '<b>') +
      money(x.notional) + (sub ? '' : '</b>') + '</td></tr>';
  }
  var lines = Object.keys(c.symbols || {}).sort().map(function (sym) {
    var x = c.symbols[sym];
    // Each instrument: its opening deals, its closing deals, and both.
    return (x.open ? line(sym, 'open', x.open, x.size, true) +
      line('', 'close', x.close, x.size, true) : '') +
      line(x.open ? '' : sym, x.open ? 'total' : 'both', x, x.size,
           false) +
      (x.unpriced ? '<tr><td></td><td colspan="7" class="down">' +
        x.unpriced + ' deal(s) not priced: contract size unknown</td></tr>'
        : '');
  }).join('');
  return '<div class="fd-head"><b>Notional traded \u00b7 ' +
    escapeHtml(label) + '</b><button class="fd-x" title="Close">\u00d7' +
    '</button></div>' +
    '<p class="fd-note"><b>A deal</b> is one fill as OKX reports it: an ' +
    'order that fills against several resting orders is several deals. ' +
    '<b>A lot</b> is one contract: on BTC-USDT-SWAP one contract is 0.01 ' +
    'BTC, on ETH-USDT-SWAP 0.1 ETH. <b>Notional</b> = lots \u00d7 contract ' +
    'size \u00d7 price, deal by deal, entries and exits of both legs.</p>' +
    '<table class="fd-table"><thead><tr><th>Instrument</th><th>Open / ' +
    'close</th><th>Deals</th>' +
    '<th>Lots (contracts)</th><th>Contract size</th><th>Coins ' +
    '(lots \u00d7 size)</th><th>Average price</th><th>Notional</th>' +
    '</tr></thead><tbody>' + lines + '</tbody><tfoot>' + (c.open
      ? '<tr><td>Opening deals</td><td>open</td><td>' + c.open.fills +
        '</td><td>' + fmt(c.open.lots, 2) + '</td><td></td><td></td>' +
        '<td></td><td>' + money(c.open.notional) + '</td></tr>' +
        '<tr><td>Closing deals</td><td>close</td><td>' + c.close.fills +
        '</td><td>' + fmt(c.close.lots, 2) + '</td><td></td><td></td>' +
        '<td></td><td>' + money(c.close.notional) + '</td></tr>' : '') +
    '<tr><td>Total</td><td>both</td>' +
    '<td>' + c.fills + '</td><td>' + fmt(c.lots, 2) + '</td><td></td>' +
    '<td></td><td></td><td><b>' + money(c.notional) + '</b></td></tr>' +
    '</tfoot></table><p class="fd-note">Volume counts EVERY deal, the ' +
    'ones that open a position and the ones that close it: 1 lot bought ' +
    'and then sold to close is 2 lots. Leg A ' + money(c.a) + ' + leg B ' +
    money(c.b) + '. The average price is the notional \u00f7 the coins: ' +
    'each deal is priced at its own fill price.</p>';
}

function showVolumeDetail(src, label) {
  var data = (state.analysis || {}).notional || {};
  var parts = String(src).split('|');
  var c = parts[0] === 'total' ? (data.total || {})[parts[2]]
    : ((data[parts[0]] || [])[Number(parts[1])] || {})[parts[2]];
  if (!c) { return; }
  var old = document.getElementById('fee-detail');
  if (old) { old.remove(); }
  var shade = document.createElement('div');
  shade.id = 'fee-detail';
  shade.className = 'fd-shade';
  shade.innerHTML = '<div class="fd-box">' + volumeDetailHtml(c, label) +
    '</div>';
  function close() {
    shade.remove();
    document.removeEventListener('keydown', onKey, true);
  }
  function onKey(e) { if (e.key === 'Escape') { close(); } }
  shade.addEventListener('click', function (e) {
    if (e.target === shade || e.target.classList.contains('fd-x')) {
      close();
    }
  });
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(shade);
}

document.addEventListener('click', function (e) {
  var link = e.target.closest && e.target.closest('.an-vol-link');
  if (!link) { return; }
  e.preventDefault();
  showVolumeDetail(link.dataset.src, link.dataset.label);
});

// THE BALANCE, RECONCILED: the journal's last account balance (OKX's
// ledger) against the balance OKX reports for the account now.
function ledgerRead(r) {
  /* When OKX's last ledger entry was, when the desk last read it, and
   * any refusal - what a gap between the two balances comes from. */
  function hm(at) {
    if (!at) { return DASH; }
    var d = new Date(at > 1e11 ? at : at * 1000);
    return d.toISOString().slice(5, 16).replace('T', ' ') + ' UTC';
  }
  var errors = r.read_errors || {};
  var names = Object.keys(errors);
  return ' \u00b7 OKX\u2019s last ledger entry ' + hm(r.last_entry_at) +
    ', read ' + hm(r.read_at) + (r.backfilled === false
      ? ' \u00b7 the three-month read has not completed yet' : '') +
    (names.length ? ' \u00b7 <b>OKX refused the ledger read</b>: ' +
      names.map(function (n) {
        return escapeHtml(n + ': ' + errors[n]);
      }).join('; ') : '');
}

function reconcileHtml(r, cash) {
  if (!r) { return ''; }
  var after = r.after_last_trade || {};
  var moved = Object.keys(after).filter(function (k) {
    return Math.abs(after[k]) >= 0.005;
  }).map(function (k) { return k + ' ' + cash(after[k]); });
  return '<p class="an-recon ' + (r.ok ? 'up' : 'down') + '">' +
    '<b>Balance check (USDT cash):</b> OKX ledger ' + cash(r.ledger) +
    ' \u00b7 OKX now ' + cash(r.okx_balance) + ' \u2014 ' + (r.ok
      ? '\u2713 the same'
      : cash(r.gap) + ' apart') + ledgerRead(r) + '</p>' +
    '<p class="an-recon-note">The Account column is the balance just ' +
    'after each trade' + (moved.length ? '; since the last one: ' +
      moved.join(' \u00b7 ') : '') + '. Equity ' + cash(r.okx_equity) +
    ' = USDT cash ' + cash(r.okx_balance) + ' + open P&amp;L ' +
    cash(r.okx_open_pnl) + (Math.abs(r.other_assets || 0) >= 0.01
      ? ' + other currencies held ' + cash(r.other_assets) : '') + '.</p>';
}

// THE TALLY under the journal: every fee, maker against taker.
function feeTallyHtml(f, cash) {
  if (!f || !f.trades) return '';
  var fills = (f.maker_fills || 0) + (f.taker_fills || 0);
  return '<p class="an-fee-tally">' + (cash && f.gross !== undefined
      ? '<b>Gross</b> ' + cash(f.gross) + ' \u00b7 ' : '') +
    '<b>Fees</b> ' + money(f.fees) +
    ' over ' + f.trades + ' trade(s)' +
    (f.fees_pct_gross === null || f.fees_pct_gross === undefined ? ''
      : ' = <b>' + fmt(f.fees_pct_gross, 1) + '% of gross</b>') +
    (f.fees_bp_notional === null || f.fees_bp_notional === undefined ? ''
      : ' \u00b7 <b>' + fmt(f.fees_bp_notional, 1) + ' bp</b> of notional' +
        (f.expected_bp ? ' (all maker ' + fmt(f.expected_bp.maker, 1) +
          ' bp, all taker ' + fmt(f.expected_bp.taker, 1) + ' bp)' : '')) +
    (f.measured ? ' \u00b7 <b>maker</b> ' + money(f.maker_fees) + ' (' +
      (f.maker_fills || 0) + ' fills) \u00b7 <b>taker</b> ' +
      money(f.taker_fees) + ' (' + (f.taker_fills || 0) + ' fills)' +
      (fills ? ' \u00b7 ' + fmt(100 * (f.maker_fills || 0) / fills, 0) +
        '% of fills maker' : '') : '') +
    (f.ledger_period !== undefined && f.ledger_period !== null
      ? ' \u00b7 <b>OKX ledger</b>: ' + money(f.ledger_period) +
        ' of trading fees over the period, ' + money(f.ledger_matched) +
        ' of them on these trades' + (Math.abs(f.ledger_other || 0) >= 0.01
          ? ' (' + money(f.ledger_other) + ' on orders that are no desk ' +
            'trade here, or a trade still open)' : ' \u2713') : '') +
    (f.estimated ? ' \u00b7 <span class="hint">' + f.estimated +
      ' trade(s) estimated (~), their OKX fills not in the journal' +
      '</span>' : '') + '</p>';
}

function anHeld(seconds) {
    if (seconds === null || seconds === undefined) { return DASH; }
    var s = Math.max(0, Math.round(seconds));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? h + 'h ' + m + 'm' : (m ? m + 'm ' + (s % 60) + 's' : s + 's');
  }

  // A spread PRICE, not money: the two numbers a trade is the
  // difference of. Trailing zeros come off back to two decimals, the
  // same as the engine prints them.
  function anSpread(value) {
    if (value === null || value === undefined) { return DASH; }
    var text = Number(value).toFixed(4);
    var bits = text.split('.');
    var decimals = bits[1].replace(/0+$/, '');
    while (decimals.length < 2) { decimals += '0'; }
    return bits[0] + '.' + decimals;
  }

  // The SIZE that was on, per leg. Two legs, so two numbers: a hedge
  // is not one lot figure and printing one of them hides a mismatch.
  function anLots(row) {
    var a = row.lots_a, b = row.lots_b;
    if ((a === null || a === undefined) && (b === null || b === undefined)) {
      return DASH;
    }
    var one = function (v) {
      return v === null || v === undefined ? DASH : Number(v).toFixed(2);
    };
    return one(a) + ' / ' + one(b);
  }

  // One LEVEL cell. `on` marks the level this exit was measured
  // against, so the eye finds the one that mattered without reading
  // the Exit column first. `sized_by` is the setting that put it
  // there, in a tooltip: it is prose, and prose does not belong in a
  // column of prices.
  function anLevel(value, on, note, sizedBy, money) {
    if (value === null || value === undefined) {
      // A level nobody recorded is NOT today's level, and it is not
      // zero. The dash says so and the tooltip says why.
      return '<td' + (note ? ' class="hint" title="' + escapeHtml(note) +
        '"' : '') + '>' + DASH + '</td>';
    }
    var tip = [];
    if (on && sizedBy) { tip.push(sizedBy); }
    if (on && money !== null && money !== undefined) {
      tip.push('worth ' + money.toFixed(2) + ' at that level');
    }
    return '<td' + (on ? ' class="on-level"' : '') +
      (tip.length ? ' title="' + escapeHtml(tip.join(' · ')) + '"' : '') +
      '>' + fmt(value, 4) + '</td>';
  }

  // How far past the level it was measured against the close went.
  // 'short of' on a take-profit is the slippage the re-check exists
  // for, and it is worth seeing as its own number.
  function anGap(x) {
    if (!x || x.gap === null || x.gap === undefined) { return DASH; }
    return fmt(x.gap, 4) + ' <span class="hint">' +
      escapeHtml(x.gap_word || '') + '</span>';
  }

  function anSide(side) {
    return side === 'SELL' ? 'H to L'
      : (side === 'BUY' ? 'L to H' : (side || DASH));
  }

  function anClock(at) {
    if (!at) { return DASH; }
    var d = new Date(at * 1000);
    return d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) +
      ' ' + d.toLocaleTimeString(undefined, {hour12: false});
  }
  function zoneSince() {
    return state.zoneDays > 0
      ? (Date.now() / 1000) - state.zoneDays * 86400 : 0;
  }

  //: The range dropdown. It heads the whole Z-score excursions view,
  //: because it governs the counters' event list AND the σ zone table
  //: under it — a range that moved one of two tables on one screen
  //: would be worse than no range at all.
  //: The Signals bar: WHICH LADDER and WHEN, together, because those
  //: are the two questions asked of this page and asking them in two
  //: different places on the screen is how you end up reading one
  //: ladder's numbers under another ladder's name.
  function zoneRangeBar(a) {
    var pairs = (state.snapshot && state.snapshot.pairs) || {};
    var dated = !!state.zoneDate;
    var days = a.signal_days || [];
    return '<div class="an-bar">' +
      '<select class="an-pair">' +
      '<option value="">every ladder</option>' +
      Object.keys(pairs).map(function (key) {
        return '<option value="' + escapeHtml(key) + '"' +
          (state.analysisPair === key ? ' selected' : '') + '>' +
          escapeHtml(pairs[key].name || key) + '</option>';
      }).join('') + '</select>' +
      ' over the <select class="an-zone-days"' +
      (dated ? ' disabled' : '') + '>' +
      [[1, 'last 1 day'], [7, 'last 7 days'], [30, 'last 30 days'],
       [0, 'all records']].map(function (o) {
        return '<option value="' + o[0] + '"' +
          (state.zoneDays === o[0] ? ' selected' : '') + '>' + o[1] +
          '</option>';
      }).join('') + '</select>' +
      ' or on <input type="date" class="an-zone-date" value="' +
      escapeHtml(state.zoneDate) + '"' +
      (days.length ? ' max="' + escapeHtml(days[0].day) + '"' : '') + '>' +
      (dated ? ' <span class="hint">the broker\u2019s ' +
        escapeHtml(state.zoneDate) + ', not the last 24 hours</span>' : '') +
      '</div>';
  }

  var SIGNAL_OUTCOME = {
    TP: ['took profit', 'up'], SL: ['stopped out', 'down'],
    EXPIRED: ['neither', ''],
    // Engaged, and let go before its confirmation was met. Nothing
    // refused it; the spread simply did not stay.
    NO_ENTRY: ['not confirmed', ''],
    // A refusal, NOT a result: the Algo looked and declined.
    BLOCKED: ['blocked', 'hint']
  };

  //: How a signal row reads in the Outcome column. EXIT is closed by one
  //: of the Algo's OTHER exits - its z-stop, its time stop, back to the
  //: mean in profit - and says which, coloured by what it made. A row
  //: still armed and being held back says so, rather than 'armed'.
  function signalOutcome(r) {
    if (r.outcome === 'EXIT') {
      return [escapeHtml(r.exit_reason || 'closed by the Algo'),
              anSign(r.net_pnl)];
    }
    if (SIGNAL_OUTCOME[r.outcome]) { return SIGNAL_OUTCOME[r.outcome]; }
    if (r.state === 'OPEN') { return ['running', '']; }
    return [r.block_reason ? 'held back' : 'armed',
            r.block_reason ? 'hint' : ''];
  }

  //: The three numbers the page exists to answer: how often these
  //: settings would have signalled, how many of those would have paid,
  //: and how many would have lost. One set of tiles — a fixed ±2σ/±3σ
  //: study sat beside them for a while and was taken back out, because
  //: two sets of counts made the one that mattered harder to read.
  //: Ask the engine to DERIVE the last 30 days from MT5's bars.
  //: Long-running, so it goes through the command log like the
  //: backtest and is polled for its answer.
  function deriveSignals() {
    if (state.deriving) { return; }
    state.deriving = true;
    state.deriveNote = {html: 'reading OKX\u2019s candles\u2026', cls: 'hint'};
    render();
    function done(html, cls) {
      state.deriving = false;
      state.deriveNote = {html: html, cls: cls || ''};
      loadAnalysis(true);
      render();
    }
    fetch('/api/command', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: 'backfill_signals',
                            payload: {pair: state.analysisPair || null,
                                      days: 30}})
    }).then(function (r) { return r.json(); }).then(function (sent) {
      if (!sent.ok) {
        return done(escapeHtml(sent.error || 'refused'), 'down');
      }
      var tries = 0;
      (function poll() {
        fetch('/api/result/' + sent.id).then(function (r) { return r.json(); })
          .then(function (result) {
            if (result && result.pending) {
              if (++tries > 240) {
                return done('no answer from the engine yet \u2014 try again',
                            'down');
              }
              return window.setTimeout(poll, 500);
            }
            var data = (result && result.data) || {};
            if (!result || result.ok === false || data.ok === false) {
              return done('could not derive: ' +
                          escapeHtml(data.reason || result.error ||
                                     'refused'), 'down');
            }
            var parts = Object.keys(data.pairs || {}).map(function (key) {
              var one = data.pairs[key];
              return escapeHtml(key) + ': ' + (one.ok ?
                one.derived + ' derived' +
                (one.replaced ? ', ' + one.replaced + ' replaced' : '') :
                escapeHtml(one.reason || 'no history'));
            });
            done('derived ' + data.written + ' signal(s) over ' + data.days +
                 ' days \u2014 ' + parts.join(' \u00b7 ') +
                 heldBack(data.held),
                 data.written ? 'up' : 'hint');
          }).catch(function (error) {
            done('could not derive: ' + escapeHtml(error.message), 'down');
          });
      }());
    }).catch(function (error) {
      done('could not derive: ' + escapeHtml(error.message), 'down');
    });
  }

  // '0 derived' on its own reads as a broken replay. The reasons the
  // replay REFUSED each entry are the answer when there are none, and
  // the top three are as much as a one-line note can carry.
  function heldBack(held) {
    var reasons = Object.keys(held || {}).map(function (reason) {
      return [reason, held[reason]];
    }).sort(function (a, b) { return b[1] - a[1]; });
    if (!reasons.length) { return ''; }
    var total = reasons.reduce(function (sum, r) { return sum + r[1]; }, 0);
    return ' \u2014 held back ' + total + ' time(s): ' +
      reasons.slice(0, 3).map(function (r) {
        // The engine counts these under a reason with its NUMBERS
        // taken out, so that '0.03x the cost' and '0.04x the cost' are
        // one reason and not two. It writes that as '#', which on the
        // screen reads as a fault rather than as a placeholder - and
        // the count ran straight on after it, so the line ended
        // '...the #x required 1'.
        return escapeHtml(String(r[0]).replace(/#/g, 'n')) +
          ' (' + r[1] + ')';
      }).join(', ');
  }

  function signalTiles(a) {
    var t = a.signal_tally || {};
    if (!t.armed) { return ''; }
    // The THRESHOLD these were counted against, named on the tile
    // rather than left to be guessed. Blank where two ladders on one
    // page disagree about it: printing either would be wrong.
    var band = t.threshold_z === null || t.threshold_z === undefined ? ''
      : Number(t.threshold_z).toFixed(2) + 'σ';
    return '<div class="an-tiles">' +
      anTile('z triggered' + (band ? ' ±' + band : ''),
             t.triggered === undefined ? t.armed : t.triggered, '',
             'how many times the spread reached the ladder’s own ' +
             'entry z' + (band ? ' of ' + band : '') + ' — a second ' +
             'poke at the band while still armed is NOT a second ' +
             'trigger, because there is nothing new to act on until it ' +
             'has come back through the floor') +
      anTile('↑ above', t.triggered_up, '',
             'stretched HIGH — the Algo SELLS these') +
      anTile('↓ below', t.triggered_down, '',
             'stretched LOW — the Algo BUYS these') +
      anTile('Furthest z', t.peak_z === null || t.peak_z === undefined
             ? DASH : Number(t.peak_z).toFixed(2) + 'σ', '',
             'the furthest a trigger got, either way: ' +
             (t.trough_z === null || t.trough_z === undefined ? DASH
              : Number(t.trough_z).toFixed(2) + 'σ') + ' low, ' +
             (t.peak_z === null || t.peak_z === undefined ? DASH
              : Number(t.peak_z).toFixed(2) + 'σ') + ' high') +
      anTile('Blocked', t.blocked, t.blocked ? 'hint' : '',
             'the Algo looked and declined: its edge, regime, trend or ' +
             'half-life filter, or a stop that would sit inside the ' +
             'bid-ask. A refusal is not a trade and is never counted as ' +
             'a loss') +
      anTile('Traded', t.entered, '', t.no_entry + ' armed and never came ' +
             'back inside the window') +
      anTile('Would have WON', t.tp, 'up', 'closed in profit') +
      anTile('Would have LOST', t.sl, 'down', 'closed at a loss') +
      anTile('Win rate', t.win_rate === null ? DASH : anPct(t.win_rate),
             t.win_rate === null ? '' : (t.win_rate >= 50 ? 'up' : 'down'),
             t.priced + ' of ' + t.settled + ' settled trades carried a ' +
             'measurable P&L; signals still running are not counted as ' +
             'losses') +
      anTile('Net', money(t.net_pnl), anSign(t.net_pnl),
             'over the trades that were actually taken') +
      '</div>' +
      '<div class="hint">' + t.live + ' recorded live off the book, ' +
      t.replayed + ' replayed from OKX\u2019s candles. A replayed row ' +
      'sees one look per candle: a stop touched and recovered inside ' +
      'one is not seen, and the bid-ask is today\u2019s width held ' +
      'constant.</div>';
  }

  function signalTable(a) {
    var all = a.signals || [];
    // A stretch the Algo armed on and let go of WITHOUT confirming is
    // not a signal: it never said enter. Listed, a fast market buried
    // the few real signals under a page of them - a spread that spikes
    // past the entry z and is back inside the mean before the third
    // fresh price arms and lets go many times a day. They carry no
    // money, so hiding them moves no total; one click shows them.
    var unconfirmed = all.filter(function (r) {
      return r.outcome === 'NO_ENTRY';
    }).length;
    var rows = state.showUnconfirmed ? all : all.filter(function (r) {
      return r.outcome !== 'NO_ENTRY';
    });
    // `name` in `analysisPane` is local to it; this is module level,
    // so it resolves the ladder's name itself rather than reaching for
    // a helper that is not in scope.
    var names = a.names || {};
    var label = function (key) {
      return escapeHtml(names[key] || key || DASH);
    };
    var html = '<h4>Signal journal <small>— every stretch the ' +
      'ladder’s settings signalled, recorded as it happened, with ' +
      'its Algo on or off: how often it signalled, what it refused, ' +
      'and what the trades would have made</small>' +
      '<button class="btn an-derive"' +
      (state.deriving ? ' disabled' : '') + '>' +
      (state.deriving ? 'deriving…' : 'Derive 30 days') +
      '</button></h4>';
    if (state.deriveNote) {
      // Its own class as well as the shared style: the backtest panel
      // below also renders an `an-bt-out`, and a bare selector reaches
      // whichever of the two comes first.
      html += '<div class="an-bt-out an-derive-out ' +
        (state.deriveNote.cls || '') + '">' + state.deriveNote.html +
        '</div>';
    }
    html += zoneRangeBar(a);
    html += signalTiles(a);
    // THE SIGNAL JOURNAL. Deliberately the same shape as the trade
    // journal above it, so the two can be read against each other: the
    // same columns, in the same order, with the three levels as
    // columns and a running total down the right. A signal that was
    // refused carries no money and must not move that total.
    var SIGNAL_COLS = 19;
    // Which account, at which broker, each leg was on when the row was
    // recorded. A replayed row has none: it never saw a terminal.
    function legsOf(r) {
      var parts = [['A', r.leg_a_account, r.leg_a_broker],
                   ['B', r.leg_b_account, r.leg_b_broker]]
        .filter(function (p) { return p[1] || p[2]; })
        .map(function (p) {
          return p[0] + ': ' + escapeHtml(p[1] || DASH) +
            (p[2] ? ' \u00b7 ' + escapeHtml(p[2]) : '');
        });
      return parts.length ? '<div class="an-legs hint">' +
        parts.join('<br>') + '</div>' : '';
    }
    // The edge filter's reading on the row: what the move back is worth
    // against what it costs, and what the filter asks for. Said even
    // with the filter off - it is the number that says whether
    // switching it on would have changed this row.
    function edgeOf(r) {
      if (r.edge_ratio === null || r.edge_ratio === undefined) {
        return '<td title="the edge could not be priced">' + DASH +
          (r.edge_on === false ? ' <span class="hint">(off)</span>' : '') +
          '</td>';
      }
      var short = r.edge_required !== null &&
        r.edge_required !== undefined &&
        r.edge_ratio < r.edge_required;
      return '<td class="' + (short ? 'down' : 'up') + '" title="' +
        'capture \u00f7 cost; the filter needs ' +
        fmt(r.edge_required, 2) + '\u00d7' +
        (r.edge_on === false ? ' \u2014 switched OFF on this ladder' : '') +
        '">' + fmt(r.edge_ratio, 2) + '\u00d7 / ' +
        fmt(r.edge_required, 2) + '\u00d7' +
        (r.edge_on === false ? ' <span class="hint">(off)</span>' : '') +
        '</td>';
    }
    html += '<table class="an-table"><thead><tr><th>Signalled</th>' +
      '<th class="l">Pair</th><th class="l">Side</th>' +
      '<th class="l">From</th><th>Size</th>' +
      '<th title="The edge filter when the signal was recorded: what ' +
      'the move back to the mean is worth against its costs, and what ' +
      'the filter requires. (off) when the filter is switched off on ' +
      'that ladder">Edge</th>' +
      '<th>z in → out</th>' +
      '<th title="The spread price the z in was read off - this ' +
      'side\u2019s own executable price, bid for H to L, ask for L to H ' +
      '- recorded whether or not the signal was entered. Not Spread in, ' +
      'which is where an entry was taken">Spread at z</th>' +
      '<th>Spread in → out</th>' +
      '<th title="After every cost. The level the other two are ' +
      'measured from">Break-even</th>' +
      '<th title="The take-profit this signal would have been given">' +
      'Target</th>' +
      '<th title="The stop this signal would have been given">Stop</th>' +
      '<th>Leg A in</th><th>Leg B in</th><th>Held</th>' +
      '<th class="l">Outcome</th>' +
      '<th class="l" title="The one thing about a signal that is not a ' +
      'number: the filter that refused it, the window it never came ' +
      'back inside, or whether the spread came back to its mean">Why' +
      '</th><th>Net P&amp;L</th><th>Running</th>' +
      '</tr></thead><tbody>';
    if (!rows.length) {
      html += '<tr><td colspan="' + SIGNAL_COLS + '" class="hint">no ' +
        'signal in this range</td></tr>';
    }
    rows.forEach(function (r) {
      var out = signalOutcome(r);
      var onLevel = r.outcome === 'SL' ? 'sl'
        : (r.outcome === 'TP' ? 'tp' : null);
      html += '<tr><td>' + anClock(r.armed_at) + '</td>' +
        '<td class="l">' + label(r.pair_key) + legsOf(r) + '</td>' +
        '<td class="l">' + anSide(r.side) + '</td>' +
        '<td class="l' + (r.source === 'REPLAY' ? ' hint' : '') +
        '" title="' + (r.source === 'REPLAY' ?
          'replayed afterwards from OKX’s candles — one look per ' +
          'candle, so a stop touched and recovered inside one is not seen' :
          'recorded live off the book, as it happened') + '">' +
        (r.source === 'REPLAY' ? 'replayed' : 'live') + '</td>' +
        '<td>' + (r.quantity === null || r.quantity === undefined ? DASH
                  : qty(r.quantity)) + '</td>' +
        edgeOf(r) +
        '<td>' + fmt(r.armed_z, 2) + ' → ' + fmt(r.closed_z, 2) +
        '</td>' +
        '<td>' + (r.armed_spread === null || r.armed_spread === undefined
                  ? DASH : fmt(r.armed_spread, 4)) + '</td>' +
        '<td>' + fmt(r.entry, 4) + ' → ' + fmt(r.exit_spread, 4) +
        '</td>' +
        anLevel(r.break_even, false) +
        anLevel(r.tp, onLevel === 'tp') +
        anLevel(r.sl, onLevel === 'sl') +
        '<td>' + (r.leg_a_entry === null || r.leg_a_entry === undefined
                  ? DASH : (r.leg_a_side || '') + ' ' +
                  fmt(r.leg_a_entry, 3)) + '</td>' +
        '<td>' + (r.leg_b_entry === null || r.leg_b_entry === undefined
                  ? DASH : (r.leg_b_side || '') + ' ' +
                  fmt(r.leg_b_entry, 3)) + '</td>' +
        '<td>' + (r.held_min === null || r.held_min === undefined ? DASH
                  : anMin(r.held_min)) + '</td>' +
        '<td class="l ' + out[1] + '">' + out[0] + '</td>' +
        '<td class="l hint">' + escapeHtml(r.why || DASH) + '</td>' +
        '<td class="' + anSign(r.net_pnl) + '">' + money(r.net_pnl) +
        '</td>' +
        '<td class="' + anSign(r.cum_pnl) + '">' + money(r.cum_pnl) +
        '</td></tr>';
    });
    html += '</tbody></table>';
    if (unconfirmed) {
      html += '<div class="hint an-unconfirmed">' +
        (state.showUnconfirmed
          ? 'Showing ' + unconfirmed + ' stretch' +
            (unconfirmed === 1 ? '' : 'es') + ' that never confirmed. ' +
            '<button class="btn an-toggle-unconfirmed">Hide them</button>'
          : unconfirmed + ' stretch' + (unconfirmed === 1 ? '' : 'es') +
            ' armed and let go without confirming ' +
            '\u2014 not signals, so not listed. ' +
            '<button class="btn an-toggle-unconfirmed">Show them</button>') +
        '</div>';
    }
    html += '<div class="note">Every row is decided by the <b>Algo\u2019s own ' +
      'code</b> \u2014 its arming, its filters, its one trade at a time, ' +
      'its cooldown, its day\u2019s limits and its exits \u2014 run ' +
      'beside the desk with nothing to send. With the Algo LIVE it ' +
      'decides as the desk does, at the price the Algo decided on (a ' +
      'real fill can differ by its slippage); a manual position, which ' +
      'holds the live Algo back, does not hold this back. Recorded off ' +
      'the LIVE book as it happened: the ' +
      'z, the executable prices and the two legs are what the market ' +
      'was showing at that moment, on the settings in force then, with ' +
      'the Algo on or off. What did not happen is the ORDER — ' +
      'nothing here was sent, so Net P&amp;L is what the trade would ' +
      'have made, not money taken. A row marked <i>replayed</i> is the ' +
      'exception: it was reconstructed from OKX’s candles by ' +
      '“Derive 30 days” and sees one look per candle. The ' +
      'stop is checked before the target, so a poll where both look ' +
      'reached is booked as the stop.</div>';
    return html;
  }
  function compactNumber(value) {
    /* 1,234,567 -> 1.23M: a notional is read by its size, and seven
     * digits across a table is a column nobody reads. The exact figure
     * is in the cell's title. */
    if (value === null || value === undefined) { return DASH; }
    var size = Math.abs(value);
    var units = [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
    for (var i = 0; i < units.length; i++) {
      if (size >= units[i][0]) {
        return (value / units[i][0]).toFixed(2) + units[i][1];
      }
    }
    return value.toFixed(0);
  }

  function notionalHtml(data, name) {
    /* Notional volume traded, per ladder and in all, today and over the
     * last 7 and 30 days, from the broker's own deals. */
    var html = '<h4>Notional traded <small>\u2014 lots \u00d7 contract ' +
      'size \u00d7 price, both legs, on the broker\u2019s own deals, in ' +
      'the instruments\u2019 price currency</small></h4>' +
      '<div class="an-bar">and from ' +
      '<input type="date" class="an-volume-from" value="' +
      escapeHtml(state.volumeFrom) + '"' +
      ' max="' + new Date().toISOString().slice(0, 10) + '"> to ' +
      '<input type="date" class="an-volume-to" value="' +
      escapeHtml(state.volumeTo) + '"' +
      ' max="' + new Date().toISOString().slice(0, 10) + '">' +
      ' <span class="hint">' + (state.volumeFrom || state.volumeTo
        ? 'the broker\u2019s days, both included \u2014 clear both to ' +
          'hide the column'
        : 'pick a range for a column of its own') + '</span></div>';
    if (!data) {
      return html + '<p class="hint">the deals could not be read</p>';
    }
    var periods = data.periods || [];
    function cell(c, src, label) {
      c = c || {};
      if (!c.fills) { return '<td class="hint">' + DASH + '</td>'; }
      var exact = Math.round(c.notional).toLocaleString();
      var title = exact + ' notional \u00b7 leg A ' +
        Math.round(c.a).toLocaleString() + ' \u00b7 leg B ' +
        Math.round(c.b).toLocaleString() + ' \u00b7 ' + c.fills +
        ' deal(s) \u00b7 ' + fmt(c.lots, 2) + ' lots' +
        (c.unpriced ? ' \u00b7 ' + c.unpriced + ' deal(s) with no ' +
          'contract size, counted but not priced' : '');
      // None of it priced is unmeasured, not zero.
      var shown = c.unpriced >= c.fills ? DASH : compactNumber(c.notional);
      return '<td title="' + escapeHtml(title + ' \u2014 click for the ' +
        'calculation') + '"><a href="#" class="an-vol-link" data-src="' +
        escapeHtml(src) + '" data-label="' + escapeHtml(label || '') +
        '"><b>' + shown + '</b>' +
        (c.unpriced ? '<sup class="down" title="' + c.unpriced +
          ' not priced">*</sup>' : '') +
        '<div class="hint">' + c.fills + ' deal' +
        (c.fills === 1 ? '' : 's') + ' \u00b7 ' + fmt(c.lots, 2) +
        ' lots</div>' + (c.open ? '<div class="hint">open ' +
          compactNumber(c.open.notional) + ' \u00b7 close ' +
          compactNumber(c.close.notional) + '</div>' : '') + '</a></td>';
    }
    html += '<table class="an-table an-notional"><thead><tr>' +
      '<th class="l">Ladder</th>' + periods.map(function (p) {
        return '<th>' + escapeHtml(p.label) + '</th>';
      }).join('') + '</tr></thead><tbody>';
    var rows = data.rows || [];
    if (!rows.length) {
      html += '<tr><td colspan="' + (periods.length + 1) + '" class="hint">' +
        'nothing traded in the last 30 days</td></tr>';
    }
    rows.forEach(function (r, i) {
      html += '<tr><td class="l">' + name(r.pair_key) + '</td>' +
        periods.map(function (p) {
          return cell(r[p.key], 'rows|' + i + '|' + p.key,
                      name(r.pair_key) + ' \u00b7 ' + p.label);
        }).join('') + '</tr>';
    });
    if (rows.length > 1) {
      html += '<tr class="an-total"><td class="l"><b>All ladders</b></td>' +
        periods.map(function (p) {
          return cell(data.total[p.key], 'total||' + p.key,
                      'All ladders \u00b7 ' + p.label);
        }).join('') + '</tr>';
    }
    var unpriced = (data.total && data.total['30d'] &&
                    data.total['30d'].unpriced) || 0;
    html += '</tbody></table>';
    // The same, per ACCOUNT: how much is going through each login at
    // each broker.
    var info = data.account_info || {};
    var accounts = data.accounts || [];
    if (accounts.length) {
      html += '<table class="an-table an-notional an-notional-accounts">' +
        '<thead><tr><th class="l">Account</th><th class="l">Broker</th>' +
        periods.map(function (p) {
          return '<th>' + escapeHtml(p.label) + '</th>';
        }).join('') + '</tr></thead><tbody>';
      accounts.forEach(function (r) {
        var who = info[r.account] || {};
        html += '<tr><td class="l">' + escapeHtml(r.account) +
          (who.login ? '<div class="hint">#' + escapeHtml(who.login) +
            '</div>' : '') + '</td><td class="l">' +
          escapeHtml(who.broker || DASH) + '</td>' +
          periods.map(function (p) {
            return cell(r[p.key], 'accounts|' + accounts.indexOf(r) + '|' +
                        p.key, r.account + ' \u00b7 ' + p.label);
          }).join('') + '</tr>';
      });
      html += '</tbody></table>';
    }
    if (unpriced) {
      html += '<div class="note">* ' + unpriced + ' deal(s) are counted ' +
        'but not priced: their contract size is not known (a ladder ' +
        'deleted before any position on it was recorded).</div>';
    }
    return html;
  }

  function stopStudyHtml(st) {
    /* WINNERS' WORST POINTS, and what a stop at each level would have
     * done to these trades: the winners it would have cut, the losers it
     * would have stopped, and the net in money and on capital. */
    var html = '<h4>Winners\u2019 worst points &amp; where a stop costs ' +
      'least <small>\u2014 from each trade\u2019s worst point while ' +
      'open</small></h4>';
    if (!st.trades) {
      return html + '<p class="hint">No closed trade with its worst point ' +
        'and margin yet.</p>';
    }
    var wp = st.winners_mae_pct || {}, wpts = st.winners_mae_points || {};
    var lp = st.losers_mae_pct || {};
    function pct(v) { return v === null || v === undefined ? DASH
      : fmt(v, 2) + '%'; }
    function pts(v) { return v === null || v === undefined ? ''
      : ' <small>(' + fmt(v, 2) + ' pts)</small>'; }
    html += '<div class="an-tiles">' +
      anTile('Winners', st.winners || 0, 'up',
             (st.losers || 0) + ' loser(s) beside them') +
      anTile('Half the winners dipped', pct(wp.median) + pts(wpts.median), '',
             'the median worst point of a WINNING trade, as a % of its ' +
             'margin (and in spread points)') +
      anTile('3 in 4 dipped less than', pct(wp.p75) + pts(wpts.p75)) +
      anTile('9 in 10 dipped less than', pct(wp.p90) + pts(wpts.p90)) +
      anTile('Deepest winner', pct(wp.max) + pts(wpts.max), 'down',
             'the furthest a trade went against and still closed a winner') +
      anTile('Losers, median worst', pct(lp.median), 'down') + '</div>';
    html += '<table class="an-table"><thead><tr>' +
      '<th title="A stop at this % of the margin both legs tied up - the ' +
      'base SL % of margin is set on">Stop at (% of margin)</th>' +
      '<th title="Winners that went this far against before winning: the ' +
      'stop would have closed them at a loss">Winners it would cut</th>' +
      '<th title="Losers that went this far: closed at the stop instead">' +
      'Losers it would stop</th>' +
      '<th title="Every trade that reached the stop, closed there instead ' +
      'of where it did: positive = the stop would have made money">' +
      'Net effect</th><th>On capital</th></tr></thead><tbody>';
    (st.levels || []).forEach(function (r) {
      html += '<tr><td>' + fmt(r.pct_margin, 1) + '%</td><td>' +
        r.winners_cut + '</td><td>' + r.losers_stopped + '</td>' +
        '<td class="' + anSign(r.net_change) + '">' +
        (r.net_change > 0 ? '+' : '') + money(r.net_change) + '</td><td class="' +
        anSign(r.net_change_pct_capital) + '">' +
        (r.net_change_pct_capital === null ||
         r.net_change_pct_capital === undefined ? DASH
          : pctSigned(r.net_change_pct_capital, 2)) + '</td></tr>';
    });
    return html + '</tbody></table><p class="hint">Read across: the ' +
      'cheapest stop is the one that cuts few winners and still stops the ' +
      'deep losers. Assumes the stop fills at its level - in a spike it ' +
      'fills worse, so a positive net is a best case.' +
      (st.trades < 20 ? ' Only ' + st.trades + ' trade(s) so far: a hint, ' +
        'not a setting.' : '') + '</p>';
  }

  function reportLink(view, label, title) {
    /* THE PRINTABLE REPORTS: this period (and, for the trader, this
     * ladder) on a page of its own laid out for A4. */
    return ' <a class="an-report an-report-' + view + '" target="_blank" ' +
      'rel="noopener" href="/report?view=' + view + '&days=' +
      state.analysisDays + (view === 'trader' && state.analysisPair ?
        '&pair=' + encodeURIComponent(state.analysisPair) : '') +
      '&pnl=' + encodeURIComponent(state.analysisPnl || 'show') +
      '" title="' + title + '">' + label + '</a>';
  }

  function analysisPane() {
    var a = state.analysis;
    var pairs = state.snapshot.pairs || {};
    var html = '<div class="analysis">';
    html += '<div class="an-bar">Trades opened in the ' +
      '<select class="an-days">' +
      [[1, 'last day'], [7, 'last 7 days'], [30, 'last 30 days'],
       [0, 'whole record']].map(function (o) {
        return '<option value="' + o[0] + '"' +
          (state.analysisDays === o[0] ? ' selected' : '') + '>' + o[1] +
          '</option>';
      }).join('') + '</select> on <select class="an-pair">' +
      '<option value="">every ladder</option>' +
      Object.keys(pairs).map(function (key) {
        return '<option value="' + escapeHtml(key) + '"' +
          (state.analysisPair === key ? ' selected' : '') + '>' +
          escapeHtml(pairs[key].name || key) + '</option>';
      }).join('') + '</select>' +
      // THE CAPITAL the trader manages: every trade's return and the
      // capital it used are read against it below. Saved as the
      // CAPITAL_MANAGED setting; blank or 0 = not set.
      ' \u00b7 <label title="The capital you manage on this desk. Each ' +
      'trade\u2019s return on capital and the capital it used are read ' +
      'against it; nothing is sized or sent from it.">Capital managed $' +
      '<input class="an-capital" type="number" min="0" step="100" ' +
      'placeholder="' + (anBalanceCapital() ? escapeHtml(
        Math.round(anBalanceCapital()).toLocaleString('en-US')) : 'not set') +
      '" value="' +
      (anCapital() ? escapeHtml(String(anCapital())) : '') +
      '">' + (anBalanceCapital() ? ' <small class="hint" title="Nothing ' +
        'typed: the balance OKX holds now (the equity of the accounts ' +
        'the ladders trade) is the capital. Type a figure to fix it ' +
        'instead.">= account balance</small>' : '') + '</label> \u00b7 <label title="Show the money each trade made, ' +
      'mask it with dots, or leave the column out - for a screen or a ' +
      'report that others see. The percentages stay.">Net P&amp;L ' +
      '<select class="an-pnl">' + [['show', 'show'], ['mask', 'mask'],
        ['hide', 'hide']].map(function (o) {
        return '<option value="' + o[0] + '"' +
          (state.analysisPnl === o[0] ? ' selected' : '') + '>' + o[1] +
          '</option>';
      }).join('') + '</select></label>' +
      // THE PRINTABLE REPORTS: the trader's whole record, and the
      // client's returns only.
      reportLink('trader', 'Trader report', 'Every trade: ladders, ' +
        'sides, sizes and the journal - for the desk') +
      reportLink('client', 'Client report', 'Returns only - no ladder, ' +
        'instrument or count of trades - to share with a client') +
      '</div>';
    if (a === null) {
      return html + '<p class="hint">reading the record\u2026</p></div>';
    }
    if (a.error) {
      return html + '<p class="down">' + escapeHtml(a.error) + '</p></div>';
    }
    var names = a.names || {};
    function name(key) { return escapeHtml(names[key] || key || DASH); }

    // THE TWO HALVES. Above this line is what the desk actually
    // TRADED; below it is every stretch the settings signalled,
    // whether or not an order followed. Both are measured off the same
    // live book - the difference is that one half moved money. They
    // are different populations and they were being read as one page:
    // four rows of em dashes over a busy signal table looks like a
    // fault rather than like a quiet week.
    var s = a.summary || {};
    html += '<h3 class="an-half">What actually happened ' +
      '<small>\u2014 the trades this desk really put on</small></h3>';
    if (!(s.count || 0)) {
      // ONE SENTENCE, not four rows of dashes. Nothing is wrong: there
      // is simply nothing in this window yet, and saying so plainly
      // beats a wall of em dashes that reads as a broken page.
      html += '<p class="hint">No trades opened in this window. The ' +
        'summary, drawdown, calibration and what-if-held readings ' +
        'below fill in from the first trade \u2014 widen the range ' +
        'above, or look at what the Algo would have done further ' +
        'down.</p>';
    }

    // THE TRADE JOURNAL FIRST: every closed trade, the table this tab
    // is opened for, ahead of the summaries built from it.
    // 7. The journal.
    //
    // THE LEVELS ARE COLUMNS, not a sentence under the row. They were
    // written as prose first - 'stop 71.40 = break-even 71.20 + 0.20
    // . 2% of margin . closed 71.46, 0.06 past it' - and that reads
    // fine for one trade and not at all for forty: a journal is a
    // table, and a number worth printing is worth lining up down the
    // page where it can be compared with the one above it.
    var trips = a.journal || [];
    var total = trips.length ? trips[0].cum_pnl : null;
    var pnlMode = state.analysisPnl || 'show';
    var pnlHidden = pnlMode === 'hide';
    // The money a trade made, as the Net P&L setting above shows it.
    function pnlCash(x) {
      return pnlMode === 'show' ? money(x) : '\u2022\u2022\u2022\u2022\u2022';
    }
    var acct = a.account || {};
    html += '<h4>Trade journal <small>— ' + trips.length + ' closed' +
      (pnlHidden ? '' : ', running total ' + pnlCash(total)) +
      (acct.opening !== null && acct.opening !== undefined
        ? ' \u00b7 OKX account: opening ' + pnlCash(acct.opening) +
          ' \u2192 now ' + pnlCash(acct.latest) + ' (OKX\u2019s ledger)'
        : '') + '</small></h4>' + reconcileHtml(acct.reconcile, pnlCash) +
      '<table class="an-table"><thead><tr><th title="Every closed trade on ' +
      'the desk\u2019s record, numbered in the order it closed \u2014 a ' +
      'trade keeps its number whatever this tab is showing">Trade no</th>' +
      '<th>Closed</th><th class="l">Pair</th>' +
      '<th class="l">Side</th><th class="l">Who</th><th>Size</th>' +
      '<th title="The margin BOTH legs tied up at entry, at OKX\u2019s ' +
      'leverage - the base the TP % and SL % are set on. ~ where it was ' +
      'estimated from the leverage">Margin</th><th>z in → out</th>' +
      '<th>Spread in → out</th>' +
      '<th title="After every cost. The level the other two are ' +
      'measured from">Break-even</th>' +
      '<th title="The take-profit this trade was given, and the ' +
      'setting that put it there">Target</th>' +
      '<th title="The stop this trade was given, and the setting that ' +
      'put it there">Stop</th>' +
      '<th title="How far past the level it was measured against the ' +
      'close actually went">vs level</th>' +
      '<th>Leg A in</th><th>Leg B in</th>' +
      '<th>Held</th><th class="l">Exit</th>' +
      '<th title="How OKX filled each leg: M maker, T taker, M+T both. ' +
      'Leg A / leg B, at the entry and at the exit">Fills A/B in \u2192 out' +
      '</th>' +
      (pnlHidden ? '' : '<th title="The spread\u2019s move times the size, ' +
        'before any fee">Gross P&amp;L</th>') +
      '<th title="The trading fees OKX charged on this trade\u2019s fills ' +
      '(a maker rebate is negative). ~ is the desk\u2019s own estimate, ' +
      'where OKX\u2019s fills are not in the journal">Fees</th>' +
      '<th title="The fees as a share of the gross P&amp;L: what the costs ' +
      'took out of what the spread move made. A dash where the move ' +
      'itself lost">Fees % of gross</th>' +
      '<th title="The fees in basis points of both legs\u2019 notional. ' +
      'At OKX\u2019s 2 bp maker / 5 bp taker, a round trip costs 4 bp if ' +
      'every fill was maker and 10 bp if every fill was taker: a figure ' +
      'outside that range is worth a look">Fees bp</th>' +
      (pnlHidden ? '' : '<th>Net P&amp;L</th>') +
      '<th title="net P&amp;L \u00f7 the margin BOTH legs tied up at entry - ' +
      'the base the TP % and SL % are set on. NET: after every fee">' +
      'Net % on margin</th>' +
      '<th title="net P&amp;L \u00f7 both legs\u2019 face value at entry">' +
      'Net % of notional</th>' +
      '<th title="The margin BOTH legs tied up at entry \u00f7 the capital ' +
      'managed (set above). Hover a cell for the money">% of capital used</th>' +
      '<th title="Both legs\u2019 value at entry: lots \u00d7 contract ' +
      'size \u00d7 price, leg A plus leg B">Notional value</th>' +
      '<th title="net P&amp;L \u00f7 the capital managed">Net % on capital</th>' +
      (pnlHidden ? '' : '<th>Running</th>') +
      '<th title="The OKX account\u2019s balance after this trade\u2019s last ' +
      'fill, from OKX\u2019s own ledger (its bills): funding and transfers ' +
      'included, so the last one is what the account shows">Account</th>' +
      '</tr></thead><tbody>';
    if (!trips.length) {
      html += '<tr><td colspan="30" class="hint">no closed trade in this ' +
        'window</td></tr>';
    }
    trips.forEach(function (t) {
      var x = t.exit || {};
      html += '<tr><td class="an-no">' + (t.trade_no ? '#' + t.trade_no
        : DASH) + '</td><td>' + anClock(t.closed_at) + '</td><td class="l">' +
        name(t.pair_key) + '</td><td class="l">' + anSide(t.side) +
        '</td><td class="l">' +
        (t.source === 'ALGO' ? '<b>Algo</b>' : 'Manual') + '</td><td>' +
        qty(t.quantity) + '</td><td>' + (t.margin ? (t.margin_estimated
          ? '~' : '') + money(t.margin) : DASH) + '</td><td>' +
        fmt(t.entry_z, 2) + ' → ' +
        fmt(t.exit_z, 2) + '</td><td>' + fmt(t.entry_spread, 4) + ' → ' +
        fmt(t.exit_spread, 4) + '</td>' +
        anLevel(x.break_even, false, x.note) +
        anLevel(x.tp, x.on_level === 'tp', x.note, x.sized_by,
                x.level_money) +
        anLevel(x.sl, x.on_level === 'sl', x.note, x.sized_by,
                x.level_money) +
        '<td>' + anGap(x) + '</td>' +
        '<td>' + (t.leg_a.side || '') + ' ' + fmt(t.leg_a.entry, 3) +
        '</td><td>' + (t.leg_b.side || '') + ' ' + fmt(t.leg_b.entry, 3) +
        '</td><td>' + anHeld(t.held_sec) + '</td><td class="l">' +
        escapeHtml(t.exit_reason || DASH) + '</td>' +
        anFillsCell(t) +
        (pnlHidden ? '' : '<td class="' + anSign(t.gross_pnl) + '">' +
          pnlCash(t.gross_pnl) + '</td>') + anFeeCell(t) +
        '<td>' + (t.fees_pct_gross === null || t.fees_pct_gross === undefined
          ? DASH : fmt(t.fees_pct_gross, 1) + '%') + '</td>' +
        '<td>' + (t.fees_bp_notional === null ||
          t.fees_bp_notional === undefined ? DASH
          : fmt(t.fees_bp_notional, 1)) + '</td>' +
        (pnlHidden ? '' : '<td class="' + anSign(t.pnl) + '">' +
          pnlCash(t.pnl) + '</td>') + pctCell(t.pct_margin, 2, t.margin_estimated) +
        pctCell(t.pct_notional, 3, false) +
        '<td' + (t.margin ? ' title="' + escapeHtml(money(t.margin)) +
          ' of margin"' : '') + '>' +
        (t.pct_capital_used === null || t.pct_capital_used === undefined
          ? DASH : fmt(t.pct_capital_used, 1) + '%') + '</td>' +
        '<td>' + (t.notional ? money(t.notional) : DASH) + '</td>' +
        pctCell(t.pct_capital, 2, false) +
        (pnlHidden ? '' : '<td class="' + anSign(t.cum_pnl) + '">' +
          pnlCash(t.cum_pnl) + '</td>') +
        '<td class="an-acct">' + (t.account_bal === null ||
          t.account_bal === undefined ? DASH : pnlCash(t.account_bal)) +
        '</td></tr>';
    });
    html += '</tbody></table>';
    html += feeTallyHtml(a.fee_tally, pnlHidden ? null : pnlCash);

    // 1. The summary.
    html += '<h4>Summary</h4><div class="an-tiles">' +
      anTile('Trades', s.count || 0) +
      anTile('Closed', s.closed || 0) +
      anTile('Net P&amp;L', pnlCash(s.net_pnl), anSign(s.net_pnl),
           'realized, after the fees OKX actually charged (the desk\u2019s ' +
           'estimate, marked ~, where a trade\u2019s fills are not on ' +
           'record)') +
      anTile('Win rate', anPct(s.win_rate), '',
           (s.wins || 0) + ' won, ' + (s.losses || 0) + ' lost') +
      anTile('Return on margin', s.return_on_margin === null ||
               s.return_on_margin === undefined ? DASH
               : pctSigned(s.return_on_margin, 2),
             anSign(s.return_on_margin),
             'net P&L \u00f7 the margin both legs used, over the ' +
             (s.margin_trades || 0) + ' closed trade(s) whose margin was ' +
             'priced') +
      anTile('Return on capital', s.return_on_capital === null ||
               s.return_on_capital === undefined ? DASH
               : pctSigned(s.return_on_capital, 2),
             anSign(s.return_on_capital),
             s.capital ? 'net P&L \u00f7 the ' + money(s.capital) +
               ' capital managed' : 'set the capital you manage above') +
      (function () {
        var r = a.risk || {};
        function two(x) { return x === null || x === undefined ? DASH
          : fmt(x, 2); }
        return anTile('Sharpe \u00b7 Sortino', two(r.sharpe) + ' \u00b7 ' +
          two(r.sortino), '', 'Return per unit of risk. On capital, by ' +
          'day (' + (r.days || 0) + ' days, no-trade days included), ' +
          'annualised by \u221a365, risk-free 0. Per trade on margin (' +
          (r.trades || 0) + ' trades): ' + two(r.trade_sharpe) +
          ' \u00b7 ' + two(r.trade_sortino) + ', average ' +
          (r.avg_on_margin === null || r.avg_on_margin === undefined ? DASH
            : fmt(r.avg_on_margin, 2) + '%') + ' on margin.');
      })() +
      anTile('Capital used', s.capital_used_avg === null ||
               s.capital_used_avg === undefined ? DASH
               : fmt(s.capital_used_avg, 1) + '%', '',
             s.capital ? 'the margin a trade tied up \u00f7 the capital ' +
               'managed: ' + fmt(s.capital_used_avg, 1) + '% on average, ' +
               fmt(s.capital_used_max, 1) + '% at most'
               : 'set the capital you manage above') +
      '</div>';

    // 1b. NOTIONAL TRADED: lots x contract size x price, on the broker's
    // own deals - today, and over the last 7 and 30 days.
    html += notionalHtml(a.notional, name);

    // 2. Drawdown, and each trade's worst and best point.
    var d = a.drawdown || {};
    html += '<h4>Drawdown &amp; excursion <small>\u2014 how bad it got, ' +
      'and how good</small></h4><div class="an-tiles">' +
      anTile('Max drawdown', d.max === null || d.max === undefined ? DASH
           : money(-d.max) + (d.max_pct_capital === null ||
               d.max_pct_capital === undefined ? ''
               : ' <small>(' + fmt(-d.max_pct_capital, 2) + '% of capital)' +
                 '</small>'), d.max ? 'down' : '',
           'the worst fall of the running P&L from a peak, closed trades in ' +
           'the order they closed') +
      // THE DEEPEST OPEN DRAWDOWN: the same curve with each trade at its
      // worst point while still open - the low the account really went
      // through, which a trade that recovered before it closed hides.
      anTile('Deepest open drawdown', d.open_max === null ||
               d.open_max === undefined ? DASH
             : money(-d.open_max) + (d.open_max_pct_capital === null ||
                 d.open_max_pct_capital === undefined ? ''
                 : ' <small>(' + fmt(-d.open_max_pct_capital, 2) +
                   '% of capital)</small>'), d.open_max ? 'down' : '',
             'the worst fall from a peak counting each trade at its WORST ' +
             'point while open (its MAE), not where it closed' +
             (d.open_max_trade ? ' \u2014 on ' +
               name(d.open_max_trade.pair_key) + ' ' +
               anSide(d.open_max_trade.side) + ', closed ' +
               anClock(d.open_max_trade.closed_at) + ': worst ' +
               money(d.open_max_trade.trough_pnl) + ', closed at ' +
               money(d.open_max_trade.pnl) : '') +
             (d.open_max_pct_capital === null ||
              d.open_max_pct_capital === undefined
               ? '. Set the capital you manage above for the %' : '')) +
      anTile('Worst trade', money(d.worst_trade), anSign(d.worst_trade),
             'best: ' + money(d.best_trade)) +
      anTile('Below the peak now', d.current === null || d.current === undefined
           ? DASH : money(-d.current), d.current ? 'down' : '') +
      anTile('Peak equity', money(d.peak_equity), 'up') + '</div>';
    var rows = a.excursion || [];
    html += '<table class="an-table"><thead><tr><th title="The trade\u2019s ' +
      'number on the desk\u2019s record, as in the journal">Trade no</th>' +
      '<th>Closed</th><th class="l">Pair</th>' +
      '<th class="l">Side</th><th class="l">Exit</th>' +
      '<th title="Maximum adverse excursion: the worst net P&L while open">' +
      'MAE (worst)</th>' +
      '<th title="The worst point as a share of the capital: what the ' +
      'account would have lost had the trade closed there">' +
      'MAE % capital</th>' +
      '<th title="The worst point as a share of the margin both legs tied ' +
      'up at entry - the base the SL % is set on">MAE % margin</th>' +
      '<th title="Maximum favourable excursion: the best net P&L while open">' +
      'MFE (best)</th>' +
      '<th title="The best point as a share of the capital: what the ' +
      'account would have made had the trade closed there">' +
      'MFE % capital</th>' +
      '<th title="The best point as a share of the margin both legs tied ' +
      'up at entry - the base the TP % is set on">MFE % margin</th>' +
      '<th>Net P&amp;L</th>' +
      '<th title="How much of its best a WINNING trade kept: net / MFE">Kept</th>' +
      '<th title="Minutes from the open to the best point">Best at</th>' +
      '</tr></thead><tbody>';
    if (!rows.length) {
      html += '<tr><td colspan="14" class="hint">no closed trade with its ' +
        'best and worst point yet \u2014 they are kept from this version on' +
        '</td></tr>';
    }
    // On the capital: the typed figure, or the account balance.
    var excCapital = (a.summary || {}).capital || null;
    function onBase(value, base, missing) {
      if (value === null || value === undefined || !base) {
        return '<td' + (base ? '' : ' title="' + missing + '"') +
          '>' + DASH + '</td>';
      }
      var pct = value / base * 100;
      return '<td class="' + anSign(pct) + '">' + pctSigned(pct, 2) + '</td>';
    }
    function onCapital(value) {
      return onBase(value, excCapital, 'set the capital above');
    }
    rows.forEach(function (r) {
      html += '<tr><td class="an-no">' + (r.trade_no ? '#' + r.trade_no
        : DASH) + '</td><td>' + anClock(r.closed_at) + '</td><td class="l">' +
        name(r.pair_key) + '</td><td class="l">' + anSide(r.side) +
        '</td><td class="l">' + escapeHtml(r.exit_reason || DASH) + '</td>' +
        '<td class="down">' + (r.mae === null ? DASH : money(-r.mae)) + '</td>' +
        onCapital(r.mae === null || r.mae === undefined ? null : -r.mae) +
        onBase(r.mae === null || r.mae === undefined ? null : -r.mae,
               r.margin, 'the margin was not priced') +
        '<td class="up">' + money(r.mfe) + '</td>' +
        onCapital(r.mfe) +
        onBase(r.mfe, r.margin, 'the margin was not priced') +
        '<td class="' + anSign(r.pnl) + '">' + money(r.pnl) + '</td>' +
        '<td>' + anPct(r.utilisation_pct, 0) + '</td>' +
        '<td>' + anMin(r.peak_min) + '</td></tr>';
    });
    html += '</tbody></table>';
    html += stopStudyHtml(a.stop_study || {});

    // 3. Take / hold calibration.
    var c = a.calibration || {};
    var pk = c.peak_pctile || {};
    html += '<h4>Take / hold calibration <small>\u2014 set the take-profit ' +
      'from where trades actually peaked</small></h4><div class="an-tiles">' +
      anTile('Trades', c.n || 0) +
      anTile('Peak p50', money(pk['50']), '', 'half the trades got at least this far') +
      anTile('Peak p70', money(pk['70'])) +
      anTile('Peak p90', money(pk['90'])) +
      anTile('Suggested take', money(c.suggested_take), 'up',
           'the 65th percentile of peaks: about two trades in three got ' +
           'at least this far') +
      anTile('Suggested max hold', anMin(c.suggested_max_hold_min), '',
           'the median minute the WINNERS peaked') + '</div>' +
      '<p class="hint">Compare the suggested take with what TP % of margin ' +
      'asks for on a trade. A target far above p70 is one most trades never ' +
      'reach.' + (c.n && c.n < 20 ? ' Only ' + c.n + ' trade(s) so far: read ' +
      'it as a hint, not a setting.' : '') + '</p>';

    // 4. What-if-held shadow.
    var sh = a.shadow || {};
    html += '<h4>What if held? <small>\u2014 after an exit that was not ' +
      'its target, the trade is marked for an hour more</small></h4>' +
      '<div class="an-tiles">' +
      anTile('Watching', sh.active || 0) +
      anTile('Finished', sh.completed || 0) +
      anTile('Came back to break-even', anPct(sh.revert_be_rate, 0)) +
      anTile('Came back to the target', anPct(sh.revert_target_rate, 0)) +
      anTile('Avg time to break-even', anMin(sh.avg_revert_min)) + '</div>' +
      '<p class="hint">Often back to the target \u21d2 the exits were early ' +
      '(a stop too tight, a cut winner). Rarely \u21d2 the exits were right ' +
      'and the move was real.</p>';
    var watches = sh.watches || [];
    if (watches.length) {
      html += '<table class="an-table"><thead><tr><th>Exited</th><th class="l">Pair</th>' +
        '<th class="l">Side</th><th class="l">Exit</th><th>At exit</th><th>Best after</th>' +
        '<th>Break-even</th><th>Target</th><th></th></tr></thead><tbody>';
      watches.slice(0, 20).forEach(function (w) {
        html += '<tr><td>' + anClock(w.armed_at) + '</td><td class="l">' +
          name(w.pair_key) + '</td><td class="l">' + anSide(w.side) +
          '</td><td class="l">' + escapeHtml(w.exit_reason || DASH) +
          '</td><td class="' +
          anSign(w.exit_pnl) + '">' + money(w.exit_pnl) + '</td>' +
          '<td class="' + anSign(w.peak_net) + '">' + money(w.peak_net) +
          (w.peak_min === null || w.peak_min === undefined ? ''
           : ' <small>@' + w.peak_min + 'm</small>') + '</td>' +
          '<td>' + (w.reverted_be ? 'yes, ' + w.be_min + 'm' : 'no') + '</td>' +
          '<td>' + (w.target_net ? (w.reverted_target ? 'yes, ' +
            w.target_min + 'm' : 'no') : DASH) + '</td>' +
          '<td class="hint">' + (w.done ? 'done' : 'watching') + '</td></tr>';
      });
      html += '</tbody></table>';
    }


    // The ALGO half: nothing below this line happened. It is
    // what these settings WOULD have done, which is a different
    // question from what the desk did, and the two were read as
    // one page until they were given a heading each.
    html += '<h3 class="an-half">What the Algo signalled <small>' +
      '\u2014 recorded off the live book as it happened, on the ' +
      'ladder\u2019s own settings, with its Algo on or off. The ' +
      'signals are real; the ORDERS are what did not happen' +
      '</small></h3>';

    // 5. The backtest.
    var bt = state.analysisBt;
    html += '<h4>Backtest <small>\u2014 the Algo\u2019s own, on OKX\u2019s ' +
      'history; nothing is sent</small></h4><div class="an-bar">' +
      (state.analysisPair ? escapeHtml((pairs[state.analysisPair] || {}).name ||
        state.analysisPair) : '<span class="hint">pick one ladder above</span>') +
      ' \u00b7 last <select class="an-bt-days">' +
      [3, 5, 10].map(function (n) {
        return '<option value="' + n + '"' +
          (state.analysisBtDays === n ? ' selected' : '') + '>' + n +
          ' days</option>';
      }).join('') + '</select> <button class="btn an-bt-run"' +
      (bt && bt.running ? ' disabled' : '') + '>Run backtest</button></div>' +
      '<div class="an-bt-out ' + (bt ? bt.cls || '' : 'hint') + '">' +
      (bt ? bt.html : 'The ladder\u2019s Algo must be on (Signals is fine).') +
      '</div>';

    // 6. Signals, and the zone path they were read off.
    html += signalTable(a);

    html += '</div>';
    return html;
  }

  function timingRow(label, values, note) {
    if (!values || !values.length) {
      // `hint`, not `note`: the note style is a full-width banner, and
      // inside a table cell it paints a yellow band across the table.
      return '<tr><td>' + label + '</td><td>' + DASH +
        ' <span class="hint">nothing measured yet — not zero</span>' +
        '</td></tr>';
    }
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var worst = sorted[sorted.length - 1];
    var median = sorted[Math.floor(sorted.length / 2)];
    return '<tr><td>' + label + '</td><td>median ' + Math.round(median) +
      'ms, worst ' + Math.round(worst) + 'ms over ' + values.length +
      ' — ' + note + '</td></tr>';
  }

  function accountsTable() {
    // Margin is posted PER ACCOUNT with two brokers. There is no
    // combined figure worth showing: the pair can only be carried by
    // the WEAKER of the two, and a total would read comfortable while
    // one side sits at its stop-out.
    var health = state.snapshot.account_health || {};
    var rows = health.accounts || {};
    var names = Object.keys(rows);
    var html = '';

    if (health.weakest) {
      var weakest = rows[health.weakest] || {};
      html += '<div class="note' + (weakest.tight ? ' mismatch' : '') + '">' +
        'The weakest account governs: <b>' + health.weakest + '</b> at ' +
        fmt(health.weakest_level, 1) + '% margin level' +
        (weakest.tight
          ? ' — under the ' + fmt(health.warn_level, 0) + '% you set, and ' +
            'it is this account that stops the pair, not the pair\'s total.'
          : '.') + '</div>';
    }
    if ((health.unknown || []).length) {
      html += '<div class="note mismatch">' + health.unknown.join(', ') +
        ' could not be read — UNKNOWN, not flat and not funded.</div>';
    }

    html += '<table><thead><tr><th>Account</th><th>Login</th>' +
      '<th>Equity</th><th>Balance</th><th>Credit</th><th>Open P&amp;L</th>' +
      '<th>Margin used</th><th>Free</th><th>Level</th><th>Call / Stop</th>' +
      '<th title="OKX fee tier: the maker and taker fees every ladder on ' +
      'this account is priced at">VIP</th>' +
      '<th>Leverage</th><th>Our legs</th><th>Our lots</th><th>Our units</th>' +
      '</tr></thead><tbody>';
    names.forEach(function (name) {
      var row = rows[name];
      html += '<tr class="' + (row.tight ? 'mismatch' : '') + '">';
      html += '<td>' + name + '</td>';
      html += '<td>' + (row.login || DASH) +
        (row.server ? '<div class="hint">' + row.server + '</div>' : '') +
        '</td>';
      // Equity first, then balance and credit beside it: a demo funded
      // with credit shows a balance of 0.00 against real equity.
      html += '<td>' + money(row.equity) + '</td>';
      html += '<td>' + money(row.balance) + '</td>';
      html += '<td>' + money(row.credit) + '</td>';
      html += '<td class="' + (row.profit > 0 ? 'up' : row.profit < 0
                               ? 'down' : '') + '">' + money(row.profit) +
        '</td>';
      html += '<td>' + money(row.margin) + '</td>';
      html += '<td>' + money(row.margin_free) + '</td>';
      html += '<td>' + (row.margin_level === null ||
                        row.margin_level === undefined
                        ? DASH : fmt(row.margin_level, 1) + '%') + '</td>';
      html += '<td>' + (row.so_call === null || row.so_call === undefined
                        ? DASH : fmt(row.so_call, 0) + '% / ' +
                          fmt(row.so_so, 0) + '%') + '</td>';
      html += '<td>' + escapeHtml(row.fee_level || DASH) + '</td>';
      // OKX sets leverage per instrument: one line each.
      var levs = row.leverage_by_symbol || {};
      var levNames = Object.keys(levs);
      html += '<td>' + (levNames.length
        ? levNames.map(function (sym) {
            return '<div>' + escapeHtml(sym) + ' ' +
              (levs[sym] ? fmt(levs[sym], 0) + 'x' : DASH) + '</div>';
          }).join('')
        : (row.leverage ? row.leverage + 'x' : DASH)) + '</td>';
      html += '<td>' + row.our_legs + '</td>';
      html += '<td>' + fmt(row.our_lots, 2) + '</td>';
      html += '<td>' + fmt(row.our_units, 2) + '</td>';
      html += '</tr>';
    });
    if (!names.length) {
      html += '<tr><td colspan="15">no accounts connected</td></tr>';
    }
    html += '</tbody></table>';
    var okx = state.snapshot.okx_positions || [];
    if (okx.length) {
      html += '<table><thead><tr><th>Account</th><th>Instrument</th>' +
        '<th>Side</th><th>Size</th><th>Avg</th><th>Mark</th>' +
        '<th>Liquidation</th><th>To liq.</th><th>Leverage</th>' +
        '<th>Unrealised</th></tr></thead><tbody>';
      okx.forEach(function (p) {
        var near = p.liq_dist_pct !== null && p.liq_dist_pct !== undefined &&
          p.liq_dist_pct < 10;
        html += '<tr class="' + (near ? 'mismatch' : '') + '">' +
          '<td>' + escapeHtml(p.account) + '</td>' +
          '<td>' + escapeHtml(p.inst) + '</td>' +
          '<td>' + escapeHtml(p.pos_side) + '</td>' +
          '<td>' + fmt(p.pos, 2) + '</td>' +
          '<td>' + fmt(p.avg, 2) + '</td><td>' + fmt(p.mark, 2) + '</td>' +
          '<td>' + (p.liq ? fmt(p.liq, 2) : DASH) + '</td>' +
          '<td>' + (p.liq_dist_pct === null || p.liq_dist_pct === undefined
                    ? DASH : fmt(p.liq_dist_pct, 1) + '%') + '</td>' +
          '<td>' + (p.lever ? fmt(p.lever, 0) + 'x' : DASH) + '</td>' +
          '<td class="' + (p.upl > 0 ? 'up' : p.upl < 0 ? 'down' : '') +
          '">' + money(p.upl) + '</td></tr>';
      });
      html += '</tbody></table>';
      html += '<div class="note">OKX\'s own positions on each account, ' +
        'netted per instrument and side: a row is amber when the mark is ' +
        'within 10% of its liquidation price.</div>';
    }
    html += '<div class="note">Equity is what the broker actually has ' +
      'of yours: balance plus credit plus open P&amp;L. Brokers often ' +
      'fund a demo with CREDIT, so a balance of 0.00 against real ' +
      'equity is normal rather than alarming. "Our lots" and "our ' +
      'units" are this system\'s own legs on that account — what is ' +
      'left is somebody else\'s, or the trader\'s own.</div>';
    return html;
  }


  function syncAdoptButton(within) {
    /* The one thing that changes when a ticket is picked, done in
     * place. Redrawing the pane for it would close the dropdown that
     * was just used. */
    var form = within.closest('.adopt-form');
    if (!form) { return; }
    var go = form.querySelector('.adopt-go');
    if (go) {
      go.disabled = !(state.adopt.pair && state.adopt.a && state.adopt.b);
    }
  }

  function adoptRowLabel(row) {
    return row.account + ':' + row.ticket + '  ' + row.symbol + ' ' +
      row.side + ' ' + qty(row.volume);
  }

  function adoptOptions(rows, account, chosen) {
    // Only the tickets on THAT leg's account. A ticket belongs to one
    // account, and offering leg B's on leg A is offering a refusal.
    var html = '<option value="">-- ticket --</option>';
    rows.forEach(function (row) {
      if (account && row.account !== account) { return; }
      html += '<option value="' + row.ticket + '"' +
        (String(row.ticket) === String(chosen) ? ' selected' : '') + '>' +
        adoptRowLabel(row) + '</option>';
    });
    return html;
  }

  function adoptForm(unclaimed) {
    /*
        Put a position BACK into a pair, so the ladder, the marks and
        Flatten work on it again.

        Two tickets, never one: a spread is a hedge, and half of one
        adopted as a pair would be a naked leg the screen calls hedged.
        The engine refuses anything that is not a hedge - leg B's side
        decides the spread's side and leg A must be its opposite - so
        this form's job is only to make the RIGHT choice easy, not to
        judge it.
    */
    var pairs = state.snapshot.pairs || {};
    var keys = Object.keys(pairs);
    if (!keys.length) { return ''; }
    var chosen = state.adopt.pair || '';
    var pair = pairs[chosen];
    var html = '<div class="note">Adopt two of these back into a pair - ' +
      'the ladder, the marks and Flatten then work on it like any other ' +
      'position. Both legs are needed: half a hedge is a naked leg.</div>';
    html += '<div class="adopt-form"><label>Pair ' +
      '<select class="adopt-pair"><option value="">-- pair --</option>';
    keys.forEach(function (key) {
      html += '<option value="' + key + '"' +
        (key === chosen ? ' selected' : '') + '>' +
        (pairs[key].name || key) + '</option>';
    });
    html += '</select></label>';
    html += '<label>Leg A' + (pair && pair.leg_a_account
                              ? ' (' + pair.leg_a_account + ')' : '') +
      ' <select class="adopt-a"' + (pair ? '' : ' disabled') + '>' +
      adoptOptions(unclaimed, pair && pair.leg_a_account, state.adopt.a) +
      '</select></label>';
    html += '<label>Leg B' + (pair && pair.leg_b_account
                              ? ' (' + pair.leg_b_account + ')' : '') +
      ' <select class="adopt-b"' + (pair ? '' : ' disabled') + '>' +
      adoptOptions(unclaimed, pair && pair.leg_b_account, state.adopt.b) +
      '</select></label>';
    var ready = chosen && state.adopt.a && state.adopt.b;
    html += '<button class="btn adopt-go"' + (ready ? '' : ' disabled') +
      '>Adopt into pair</button></div>';
    return html;
  }

  function reconcileTable() {
    var reconciler = state.snapshot.reconciler || {};
    var html = '';
    var unclaimed = reconciler.unclaimed || [];
    if (unclaimed.length) {
      // The positions nothing will close automatically, with the two
      // things a person can do about them.
      html += '<div class="note mismatch">' + unclaimed.length +
        ' position(s) at the broker carry our magic but are not in our ' +
        'book. Nothing is closed automatically: adopt one into a pair, ' +
        'or close it by hand.</div>';
      // VOLUME AT FULL PRECISION, not two decimals. A broker whose
      // volume step is 0.001 - and a PARTIAL fill, which is how these
      // get here - printed 0.00 in this column, so the one number the
      // operator needs to close the position by hand read as nothing
      // at all.
      html += '<table class="unclaimed"><thead><tr><th>Account</th>' +
        '<th>Ticket</th><th>Symbol</th><th>Side</th><th>Volume</th>' +
        '<th>Open</th><th></th></tr></thead><tbody>';
      unclaimed.forEach(function (row) {
        html += '<tr><td>' + row.account + '</td><td>' + row.ticket +
          '</td><td>' + row.symbol + '</td><td>' + row.side + '</td><td>' +
          qty(row.volume) + '</td><td>' + fmt(row.price_open, 4) +
          '</td><td><button class="btn close-unclaimed" data-account="' +
          row.account + '" data-ticket="' + row.ticket +
          '">Close it</button></td></tr>';
      });
      html += '</tbody></table>';
      html += adoptForm(unclaimed);
    }
    html += '<table><thead><tr><th>When</th><th>Symbol</th><th>Ticket</th>' +
      '<th>Volume</th><th>Contract</th><th>P&amp;L</th><th>Note</th>' +
      '</tr></thead><tbody>';
    var closes = reconciler.untracked_closes || [];
    closes.forEach(function (entry) {
      html += '<tr><td>' + new Date(entry.at * 1000).toLocaleTimeString() +
        '</td><td>' + entry.symbol + '</td><td>' + entry.ticket + '</td><td>' +
        entry.volume + '</td><td>' + entry.contract_size +
        (entry.contract_size_assumed ? ' (assumed)' : '') + '</td><td>' +
        money(entry.pnl) + '</td><td>' + (entry.note || '') + '</td></tr>';
    });
    if (!closes.length) {
      html += '<tr><td colspan="7">nothing untracked has been closed</td></tr>';
    }
    html += '</tbody></table>';
    if ((reconciler.escalated || []).length) {
      html += '<div class="note mismatch">CLOSE IT BY HAND: ' +
        reconciler.escalated.join(', ') + '</div>';
    }
    if ((reconciler.unknown_accounts || []).length) {
      html += '<div class="note">' + reconciler.unknown_accounts.join(', ') +
        ' could not be read this pass — UNKNOWN, not flat. No orphans ' +
        'or ghosts were inferred from them.</div>';
    }
    html += okxOrdersHtml();
    return html;
  }

  function loadOkxOrders(fresh) {
    var now = Date.now();
    if (!fresh && state.okxOrdersAt && now - state.okxOrdersAt < 10000) {
      return;
    }
    state.okxOrdersAt = now;
    fetch('/api/okx_orders' + (fresh ? '?fresh=1' : ''))
      .then(function (r) { return r.json(); })
      .then(function (body) { state.okxOrders = body; render(); })
      .catch(function (e) {
        state.okxOrders = {ok: false, error: String(e)};
        render();
      });
  }

  function okxOrdersHtml() {
    /* THE ORDERS AS OKX HOLDS THEM - working and the last week's
     * finished ones - beside the desk's own ticket for each. The
     * reconciliation a person does by eye: every order the desk sent
     * should be here with the desk's ticket on it; an order of OURS
     * with no desk ticket is one the desk does not know about. */
    loadOkxOrders(false);
    var body = state.okxOrders;
    var oursOnly = state.okxOrdersOurs !== false;
    var html = '<div class="journal-controls"><b>OKX orders</b> ' +
      '<label class="check"><input type="checkbox" class="okx-orders-ours"' +
      (oursOnly ? ' checked' : '') + '> ours only</label>' +
      '<button class="btn okx-orders-refresh">Refresh</button>' +
      '<span class="hint">Read live from OKX (working orders, and the ' +
      'last 7 days of finished ones), every 10 s.</span></div>';
    if (!body) { return html + '<p class="note">reading OKX…</p>'; }
    if (!body.ok) {
      return html + '<p class="note mismatch">' +
        escapeHtml(body.error || 'OKX could not be read') + '</p>';
    }
    Object.keys(body.errors || {}).forEach(function (name) {
      html += '<div class="note mismatch">' + escapeHtml(name) + ': ' +
        escapeHtml(body.errors[name]) + '</div>';
    });
    var rows = (body.orders || []).filter(function (o) {
      return !oursOnly || o.ours;
    });
    var unknown = rows.filter(function (o) {
      return o.ours && (o.desk_ticket === null || o.desk_ticket === undefined);
    }).length;
    if (unknown) {
      html += '<div class="note mismatch">' + unknown + ' order(s) of ours ' +
        'on OKX carry no desk ticket - sent by a run whose ledger is gone, ' +
        'or not by this desk. Check them on OKX.</div>';
    }
    html += '<table class="okx-orders"><thead><tr><th>Updated</th>' +
      '<th>Account</th><th>Instrument</th><th>Side</th><th>Pos</th>' +
      '<th>Type</th><th>Price</th><th>Size</th><th>Filled</th>' +
      '<th>Avg</th><th>State</th><th>Fee</th><th>Desk ticket</th>' +
      '<th>Closes</th><th>Comment</th><th>OKX order id</th></tr></thead>' +
      '<tbody>';
    if (!rows.length) {
      html += '<tr><td colspan="16">no orders on OKX in the last 7 days' +
        (oursOnly ? ' from this desk' : '') + '</td></tr>';
    }
    rows.slice(0, 200).forEach(function (o) {
      var working = o.state === 'live' || o.state === 'partially_filled';
      var noTicket = o.ours && (o.desk_ticket === null ||
                                o.desk_ticket === undefined);
      var when = o.updated_ms ? new Date(o.updated_ms) : null;
      html += '<tr class="' + (noTicket ? 'mismatch' : working ? 'working' : '')
        + '">' +
        '<td>' + (when ? when.toLocaleDateString() + ' ' +
                  when.toLocaleTimeString() : DASH) + '</td>' +
        '<td>' + escapeHtml(o.account) + '</td>' +
        '<td>' + escapeHtml(o.symbol || DASH) + '</td>' +
        '<td>' + escapeHtml(o.side) + '</td>' +
        '<td>' + escapeHtml(o.pos_side || DASH) + '</td>' +
        '<td>' + escapeHtml(o.ord_type) + '</td>' +
        '<td>' + (o.price === null || o.price === undefined ? 'market'
                  : fmt(o.price, 4)) + '</td>' +
        '<td>' + qty(o.size) + '</td>' +
        '<td>' + qty(o.filled) + '</td>' +
        '<td>' + (o.avg_price ? fmt(o.avg_price, 4) : DASH) + '</td>' +
        '<td>' + escapeHtml(o.state) + '</td>' +
        // OKX signs a fee negative when charged; shown as a COST here,
        // positive, as on the Slippage page. A rebate reads negative.
        '<td>' + (o.fee ? smallMoney(-o.fee) : DASH) + '</td>' +
        '<td>' + (o.ours ? (noTicket ? '<b>none</b>'
                  : escapeHtml(String(o.desk_ticket)))
                 : '<span class="hint">not ours</span>') + '</td>' +
        '<td>' + (o.position_ticket ? escapeHtml(String(o.position_ticket))
                  : DASH) + '</td>' +
        '<td>' + escapeHtml(o.comment || o.tag || '') + '</td>' +
        '<td class="hint">' + escapeHtml(o.ord_id) + '</td></tr>';
    });
    html += '</tbody></table>';
    html += '<div class="note">Every order the desk sends carries its own ' +
      'id prefix and the tag <b>nexus</b>, so OKX\u2019s list and the ' +
      'desk\u2019s tickets line up row for row. "Closes" is the position ' +
      'ticket a closing order was sent against. A red row is an order of ' +
      'ours the desk has no ticket for.</div>';
    return html;
  }

  function onMonitorClick(e) {
    if (e.target.classList && e.target.classList.contains('okx-orders-ours')) {
      state.okxOrdersOurs = e.target.checked;
      render();
      return;
    }
    var act = e.target.closest('.mr-act');
    if (act) {
      var who = act.dataset.name, what = act.dataset.act;
      var go = function () {
        send('mirror_control', {follower: who, action: what},
             function (result) {
               var data = (result || {}).data || {};
               toast(who + ': ' + (data.ok === false ? (data.reason ||
                 'refused') : what === 'HALT' ? 'halted — no new copies'
                 : what === 'RESUME' ? 'following again'
                 : 'closing ' + (data.closing || 0) + ' copy(ies), halted'),
                 data.ok === false ? '' : 'ok');
             });
      };
      if (what === 'CLOSE_COPIES') {
        ask('Close every copy ' + who + ' holds?', 'Each copy is closed ' +
            'NOW, at market, in ' + who + '’s account, and ' + who +
            ' is halted (no new copies) until resumed. Your own trades ' +
            'and the other clients are not touched.', 'Close copies', go);
      } else {
        go();
      }
      return;
    }
    var trade = e.target.closest('.mr-trade');
    if (trade) {
      state.mirrorOpen = state.mirrorOpen || {};
      state.mirrorOpen[trade.dataset.pid] = !state.mirrorOpen[trade.dataset.pid];
      render();
      return;
    }
    var button = e.target.closest('button');
    if (!button) { return; }
    if (button.classList.contains('sz-reset')) {
      state.sizing = null;
      render();
      return;
    }
    if (button.classList.contains('sz-print')) {
      printSizingSheet();
      return;
    }
    if (button.classList.contains('sz-win')) {
      state.sizing = Object.assign(sizingSettings(),
                                   {win: parseFloat(button.dataset.win)});
      render();
      return;
    }
    if (button.classList.contains('okx-orders-refresh')) {
      loadOkxOrders(true);
      return;
    }
    if (button.classList.contains('an-toggle-unconfirmed')) {
      state.showUnconfirmed = !state.showUnconfirmed;
      render();
      return;
    }
    if (button.classList.contains('an-derive')) {
      deriveSignals();
      return;
    }
    if (button.classList.contains('an-bt-run')) {
      return runAnalysisBacktest();
    }
    var row = button.closest('tr');
    if (button.classList.contains('close-unclaimed')) {
      return ask('Close ' + button.dataset.account + ':' +
                 button.dataset.ticket + '?',
                 'This position is at the broker but not in our book. ' +
                 'Closing it is by ticket, at market, and cannot be ' +
                 'undone.', 'Close it', function () {
                   send('close_unclaimed',
                        {account: button.dataset.account,
                         ticket: button.dataset.ticket});
                 });
    }
    if (button.classList.contains('adopt-go')) {
      var want = state.adopt;
      if (!want.pair || !want.a || !want.b) { return; }
      return ask('Adopt ' + want.a + ' and ' + want.b + '?',
                 'These two tickets are taken into ' + want.pair + ' as ONE ' +
                 'position. Nothing is bought or sold. The engine refuses ' +
                 'them if they are not a hedge of each other.', 'Adopt',
                 function () {
                   send('adopt_unclaimed',
                        {pair: want.pair, ticket_a: want.a, ticket_b: want.b},
                        function () {
                          state.adopt = {pair: '', a: '', b: ''};
                        });
                 });
    }
    if (button.classList.contains('close-position')) {
      send('close_position', {position_id: row.dataset.position});
    } else if (button.classList.contains('cancel-order')) {
      send('cancel_order', {order_id: row.dataset.order});
    } else if (button.classList.contains('close-now')) {
      send('close_position', {position_id: row.dataset.position, now: true});
    } else if (button.classList.contains('cancel-maker')) {
      send('cancel_where', {pair: row.dataset.pair, side: row.dataset.side});
    }
  }

  // -- the whole screen ---------------------------------------------------

  function linkState() {
    /* "Is it working?" answered in one line, from three facts: the
     * engine is publishing, both accounts answered, and every enabled
     * ladder has a two-sided quote. Anything less is not green — a
     * screen that says CONNECTED while one leg is dark is the failure
     * this whole banner exists to prevent.
     */
    var snapshot = state.snapshot;
    if (snapshot.engine !== 'up') {
      // STALLED and DOWN are different faults with different fixes: a
      // process that has stopped publishing while the page still reads
      // its last file, against no file at all. Both were called ENGINE
      // DOWN, and the first one is the one where every price on the
      // screen is a photograph.
      // WAITING is a third case, and the only one a trader can fix
      // themselves: the launcher is holding the engine on something in
      // the setup - a MetaTrader 5 that is not where an account says -
      // and the note names it and says to open Exchanges.
      return {state: 'bad',
              text: (snapshot.engine_held || []).length ? 'ENGINE WAITING'
                : (snapshot.engine === 'stalled' ? 'ENGINE STALLED'
                  : 'ENGINE DOWN'),
              detail: snapshot.engine_note || 'the coordinator is not running'};
    }
    var health = (snapshot.account_health || {}).accounts || {};
    var names = Object.keys(health);
    var known = names.filter(function (name) { return health[name].known; });
    if (!names.length) {
      return {state: 'bad', text: 'NO ACCOUNTS',
              detail: 'no account is configured — open Exchanges'};
    }
    if (known.length < names.length) {
      var dark = names.filter(function (name) { return !health[name].known; });
      return {state: 'bad', text: known.length + '/' + names.length +
              ' ACCOUNTS',
              detail: dark.join(', ') + ' could not be read — check that ' +
                      'terminal is open and logged in'};
    }
    var pairs = snapshot.pairs || {};
    var enabled = Object.keys(pairs).filter(function (key) {
      return pairs[key].enabled !== false;
    });
    var quiet = enabled.filter(function (key) {
      var market = pairs[key].market;
      return !market || market.short_spread === null ||
        market.short_spread === undefined;
    });
    if (enabled.length && quiet.length) {
      return {state: 'warn', text: known.length + '/' + names.length +
              ' ACCOUNTS · NO QUOTE',
              detail: quiet.join(', ') + ': both terminals are attached, ' +
                      'but no two-sided quote has arrived — check the ' +
                      'symbols on the Exchanges page'};
    }
    var stale = enabled.filter(function (key) {
      return (pairs[key].market || {}).stale_reason;
    });
    if (stale.length) {
      // With the leg and the number in the badge itself: "stale" alone
      // sends the operator looking for a fault when the market may
      // simply be quiet.
      var worst = (pairs[stale[0]].market || {}).stale_reason || '';
      return {state: 'warn',
              text: 'QUOTES STALE · ' + (pairs[stale[0]].name || stale[0]),
              detail: worst + '. If this instrument is simply quiet, ' +
                      'raise MAX_QUOTE_AGE_SEC in Settings; if the ' +
                      'terminal has lost its feed, the price is frozen ' +
                      'on OKX too.'};
    }
    return {state: 'ok',
            text: 'LIVE · ' + known.length + '/' + names.length +
                  ' accounts',
            detail: 'both terminals attached and every enabled ladder is ' +
                    'quoting'};
  }

  function renderLink() {
    var link = linkState();
    var badge = el('link-badge');
    badge.textContent = link.text;
    badge.className = 'link ' + link.state;
    badge.title = link.detail;
  }

  function reportDeadOrders() {
    /* A working order that STOPPED working, said out loud once.
     *
     * The broker can refuse the pending behind a synthetic — invalid
     * price, stops level, trading disabled — and the order then leaves
     * the book in the same instant the click was accepted. What the
     * trader saw was a green toast and an empty Work column, with the
     * refusal nowhere on the screen. This is that refusal, in the
     * broker's own words, on the screen, once.
     */
    var pairs = state.snapshot.pairs || {};
    Object.keys(pairs).forEach(function (key) {
      (pairs[key].dead_orders || []).forEach(function (order) {
        if (state.reported[order.order_id]) { return; }
        state.reported[order.order_id] = true;
        // A REFUSAL AND A WITHDRAWAL ARE NOT THE SAME NEWS. The
        // broker rejecting a pending is a fault and stays on the
        // screen until it is read. An order CANCELLED — by the
        // trader's own next click, by a sweep, by AutoRouting
        // standing down — is the system doing as it was told, and
        // dressing it as an alarm taught the desk to dismiss alarms.
        toast((pairs[key].name || key) + ': ' + order.side + ' ' +
              order.quantity + ' at ' + fmt(order.level, 4) + ' — ' +
              order.state.toLowerCase() + '. ' + order.reason,
              order.state === 'CANCELLED' ? 'ok' : undefined);
      });
    });
  }

  function renderStatusLine() {
    /* The two things that change on every poll whether or not the
     * coordinator published anything: whether it is alive, and how old
     * its last snapshot is. Cheap, and separate from the panels, so a
     * stalled engine does not cost a full redraw three times a second
     * — which is exactly when the screen must stay responsive. */
    var snapshot = state.snapshot;
    var banner = el('engine-banner');
    if (snapshot.engine && snapshot.engine !== 'up') {
      banner.textContent = snapshot.engine_note;
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }
    renderLink();
    var loop = snapshot.loop_interval_sec;
    el('loop-stat').textContent = loop
      ? 'loop ' + (loop * 1000).toFixed(0) + 'ms · snapshot ' +
        ((snapshot.status_age_sec || 0) * 1000).toFixed(0) + 'ms old'
      : DASH;
  }


  // -- the screen lock ---------------------------------------------------
  //
  //     Two things, one mechanism: a trader who left for the day and a
  //     stray click on a live ladder.
  //
  //     WHAT IS ON SCREEN IS NOT THE GUARD. webapp.py refuses every
  //     state-changing request while the server says locked, so a
  //     refresh, a second tab or the console gets a 423 and nothing
  //     else. This overlay exists so the trader knows why, and so the
  //     clicks never leave the page in the first place.

  function showLock(state_) {
    var overlay = el('lock-overlay');
    if (!overlay) { return; }
    var locked = !!state_.locked;
    var was = !overlay.classList.contains('hidden');
    overlay.classList.toggle('hidden', !locked);
    var button = el('lock-now');
    if (button) {
      // Nothing to lock until there is a way back in.
      button.hidden = !state_.pin_set;
      button.title = state_.pin_set
        ? 'Lock the screen (Ctrl+L)'
        : 'Set a PIN in Settings to use the lock';
    }
    if (locked && !was) {
      el('lock-error').textContent = '';
      el('lock-pin').value = '';
      // Focus AFTER the browser has drawn it, or the field that gets
      // the first keystroke is whatever had focus before.
      requestAnimationFrame(function () { el('lock-pin').focus(); });
    }
  }

  function screenIsLocked() {
    var overlay = el('lock-overlay');
    return !!overlay && !overlay.classList.contains('hidden');
  }

  function lockNow() {
    fetch('/api/lock', {method: 'POST'}).then(function () {
      showLock({locked: true, pin_set: true});
    });
  }

  function tryUnlock() {
    var pin = el('lock-pin').value;
    fetch('/api/unlock', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pin: pin})
    }).then(function (r) { return r.json(); }).then(function (data) {
      el('lock-pin').value = '';
      if (data && data.ok) {
        el('lock-error').textContent = '';
        showLock({locked: false, pin_set: true});
        return;
      }
      // The server's own words - including how many tries are left and
      // the lockout. Never 'wrong PIN' invented here.
      el('lock-error').textContent = (data && data.error) ||
        'The screen would not unlock.';
      el('lock-pin').focus();
    });
  }

  function render() {
    // Not while a window is being dragged. The whole screen is rebuilt
    // three times a second, and doing that under the pointer is what
    // made a window judder and lag behind the cursor. The drag is short
    // and drop() calls render() itself, so nothing stays stale.
    if (document.querySelector('.window.dragging')) { return; }
    var snapshot = state.snapshot;
    prunePending();
    renderStatusLine();

    renderNaked();
    renderUnclaimed();
    reportDeadOrders();
    renderSameLogin();
    announceSignals();

    var wanted = {};
    state.open.forEach(function (id) { wanted[id] = true; });
    Object.keys(snapshot.pairs || {}).forEach(function (key) {
      var id = panelId('ladder', key);
      if (wanted[id]) { renderLadder(key, snapshot.pairs[key]); }
      // The pair's setting SEEDS this window; the desk decides. Open
      // it because the setting says so, unless this screen has closed
      // it — and keep it open once it is, whatever a later snapshot
      // says, because a window that vanishes when the engine hiccups
      // is a window the trader cannot rely on.
      var fairId = panelId('fair', key);
      if (snapshot.pairs[key].algo_window && !state.closed[fairId]
          && state.open.indexOf(fairId) < 0) {
        state.open.push(fairId);
      }
      if (state.open.indexOf(fairId) >= 0) {
        wanted[fairId] = true;
        renderFairWindow(key, snapshot.pairs[key]);
      }
      // The Algo's window: open while the Algo is on, gone when it is
      // off. Turned on, it opens even if it was closed before — turning
      // it on IS asking to see it.
      var chartId = panelId('chart', key);
      if (wanted[chartId]) {
        renderChartWindow(key, snapshot.pairs[key]);
      }
      var ladderBox = document.querySelector(
        '.ladder[data-pair="' + cssEscape(key) + '"] .chart-btn');
      if (ladderBox) {
        ladderBox.classList.toggle('on', !!wanted[chartId]);
      }
      var calcId = panelId('calc', key);
      if (wanted[calcId]) { renderCalcWindow(key, snapshot.pairs[key]); }
      var calcButton = document.querySelector(
        '.ladder[data-pair="' + cssEscape(key) + '"] .calc-btn');
      if (calcButton) { calcButton.classList.toggle('on', !!wanted[calcId]); }
      var algoId = panelId('algo', key);
      if (snapshot.pairs[key].algo_on) {
        if (state.open.indexOf(algoId) < 0 && !state.closed[algoId]) {
          state.open.push(algoId);
        }
      } else {
        delete state.closed[algoId];
        state.open = state.open.filter(function (id) { return id !== algoId; });
      }
      if (state.open.indexOf(algoId) >= 0) {
        wanted[algoId] = true;
        renderAlgoWindow(key, snapshot.pairs[key]);
      }
    });
    if (wanted[panelId('grid')]) { renderGrid(); }
    if (wanted[panelId('monitor')]) { renderMonitor(); }
    if (wanted[panelId('settings')] && window.MT5Settings) {
      window.MT5Settings.render();
    }
    if (wanted[panelId('managed')] && window.MT5Managed) {
      window.MT5Managed.render();
    }
    if (wanted[panelId('econcal')] && window.MT5Calendar) {
      window.MT5Calendar.render();
    }
    // A box before a release, and another when it lands.
    if (window.MT5Calendar) { window.MT5Calendar.tick(); }

    // Remove the panels that are no longer open.
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (node) {
        if (!wanted[panelIdOf(node)]) { node.remove(); return; }
        // Where it was left, and the size it was left at, every render:
        // panels are created lazily, and one opened later must still
        // come back to its own corner.
        ensureGrip(node);
        applyLayout(node);
        applyMaximized(node);
      });

    renderTabs();
    revealPanel();
  }

  // -- maximize ------------------------------------------------------------
  //
  // The Trading Monitor, a Chart and the Calc can take the whole desk:
  // the window's own button (or a double-click on its title bar) fills
  // the space between the top bar and the taskbar, and the same again
  // puts it back where it was. Per panel, kept across redraws.

  //: The windows that can be maximized, by the class they carry.
  var MAXIMIZABLE = ['monitor', 'chartwin', 'calcwin', 'econcal', 'managed'];

  function canMaximize(node) {
    return MAXIMIZABLE.some(function (cls) {
      return node.classList.contains(cls);
    });
  }

  function applyMaximized(node) {
    if (!canMaximize(node)) { return; }
    var winbtns = node.querySelector('.titlebar .winbtns');
    var button = winbtns && winbtns.querySelector('.winbtn.max');
    if (winbtns && !button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'winbtn max';
      winbtns.insertBefore(button, winbtns.firstChild);
    }
    var on = !!(state.maximized || {})[panelIdOf(node)];
    if (on) {
      // The desk's own box: below the top bar, above the taskbar.
      var desk = el('desktop').getBoundingClientRect();
      node.style.setProperty('--max-top', desk.top + 'px');
      node.style.setProperty('--max-bottom',
                             Math.max(0, window.innerHeight - desk.bottom) +
                             'px');
    }
    node.classList.toggle('maximized', on);
    if (button) {
      button.innerHTML = on ? '&#10064;' : '&#9744;';
      button.title = on ? 'Restore the window to its size and place'
        : 'Maximize: fill the desk (double-click the title bar does too)';
    }
  }

  function toggleMaximized(node) {
    if (!node || !canMaximize(node)) { return; }
    state.maximized = state.maximized || {};
    var id = panelIdOf(node);
    state.maximized[id] = !state.maximized[id];
    applyMaximized(node);
    if (node.classList.contains('monitor')) { render(); }
    // Charts draw at the size of their box: redraw at the new one.
    if (node.classList.contains('chartwin')) {
      node.dataset.drawn = '';
      chartRedraw(node.dataset.pair);
    }
  }

  function revealPanel() {
    /* Scroll a newly opened window into view — once, and never while
     * it is being dragged. A floating window is already where the
     * trader put it, so only the desktop's horizontal scroll moves. */
    var id = state.reveal;
    if (!id) { return; }
    state.reveal = null;
    var node = null;
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (candidate) {
        if (panelIdOf(candidate) === id) { node = candidate; }
      });
    if (!node) { return; }
    var desktop = el('desktop');
    var box = node.getBoundingClientRect();
    var frame = desktop.getBoundingClientRect();
    if (box.left < frame.left || box.right > frame.right) {
      desktop.scrollLeft += box.left - frame.left - 4;
    }
    node.classList.add('revealed');
    window.setTimeout(function () {
      node.classList.remove('revealed');
    }, 900);
  }

  function renderSameLogin() {
    /* Both legs on one MT5 account. Two leg runners can attach to the
     * same running terminal — a blank terminal path on both is enough —
     * and then every "hedge" is two orders on ONE account: no spread,
     * twice the exposure, and nothing on the screen looks wrong. The
     * engine refuses entries on such a pair; this says why. */
    var health = state.snapshot.account_health || {};
    var same = health.same_login || {};
    var logins = Object.keys(same);
    var banner = el('same-login-banner');
    if (!logins.length) {
      banner.classList.add('hidden');
      return;
    }
    var signature = logins.sort().join(',');
    if (state.dismissed['same-login'] === signature) {
      banner.classList.add('hidden');
      return;
    }
    banner.innerHTML = closeButton() +
      '<button class="banner-open">Exchanges</button>' +
      logins.map(function (login) {
        return '<b>' + same[login].join(' and ') + ' are both on OKX ' +
          'account #' + login + '.</b> One account, not two.';
      }).join(' ');
    banner.title = logins.map(function (login) {
      return same[login].join(' and ') + ' are configured as two accounts ' +
        'but are both on OKX account #' + login + '. If two ' +
        'terminals were intended, give each account its own terminal_path ' +
        'on the Exchanges page. If this really is ONE account trading ' +
        'both legs — spot and the future at one broker is an ordinary ' +
        'spread — point both legs of the pair at the same account ' +
        'instead; margin is then one pool.';
    }).join('\n');
    banner.dataset.signature = signature;
    banner.classList.remove('hidden');
  }

  function closeButton() {
    /* Every banner can be put away. This one is an FYI: it names a
     * thing worth knowing and does not stop the trader working. It
     * comes BACK if what it is about changes — a dismissal is for the
     * situation the trader read, not for the banner. */
    return '<button class="banner-close" title="Dismiss — it returns if ' +
      'this changes">&times;</button>';
  }

  function renderUnclaimed() {
    /* One line, and it can be put away.
     *
     * A position at the broker that our book cannot explain is exactly
     * the one an automatic close must never touch, so it has to be
     * SAID — but a wall of table across the top of the screen is not
     * how to say it. The line names the number; the table lives in
     * Positions -> Reconciler, where the buttons that act on it are.
     */
    var reconciler = state.snapshot.reconciler || {};
    var unclaimed = reconciler.unclaimed || [];
    var banner = el('unclaimed-banner');
    if (!unclaimed.length) {
      banner.classList.add('hidden');
      return;
    }
    var signature = unclaimed.map(function (row) {
      return row.account + ':' + row.ticket;
    }).sort().join(',');
    if (state.dismissed.unclaimed === signature) {
      banner.classList.add('hidden');
      return;
    }
    banner.dataset.signature = signature;
    banner.innerHTML = closeButton() +
      '<button class="banner-open">Review</button>' +
      '<b>' + unclaimed.length + ' position(s) at the broker are not in ' +
      'our book.</b> Nothing is closed automatically.';
    banner.title = unclaimed.map(function (row) {
      return row.account + ' #' + row.ticket + ' ' + row.side + ' ' +
        row.volume + ' ' + row.symbol;
    }).join('\n');
    banner.classList.remove('hidden');
  }

  function renderNaked() {
    var messages = [];
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      (row.positions || []).forEach(function (position) {
        var a = position.leg_a, b = position.leg_b;
        if (!a || !b || !a.volume || !b.volume) {
          messages.push(key + ': one leg is on and the other is not');
        }
      });
    });
    var banner = el('naked-banner');
    if (messages.length) {
      banner.textContent = 'NAKED LEG — ' + messages.join(' | ') +
        '. Hedge it or flatten it now.';
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }
  }

  function renderTabs() {
    var html = '';
    state.open.forEach(function (id) {
      var parts = id.split(':');
      var pairName = (state.snapshot.pairs[parts.slice(1).join(':')] || {}).name
        || parts.slice(1).join(':');
      var label = parts[0] === 'ladder' ? pairName
        : parts[0] === 'algo' ? pairName + ' · Algo'
          : parts[0] === 'fair' ? pairName + ' · Fair'
          : parts[0] === 'chart' ? pairName + ' · Chart'
          : parts[0] === 'calc' ? pairName + ' · Calc'
            : parts[0] === 'grid' ? 'Market Grid'
              : parts[0] === 'settings' ? 'Exchanges'
              : parts[0] === 'managed' ? 'Managed accounts'
              : parts[0] === 'econcal' ? 'Calendar' : 'Positions';
      var badge = '';
      if (parts[0] === 'ladder') {
        var row = state.snapshot.pairs[parts.slice(1).join(':')] || {};
        var working = (row.working_buys || 0) + (row.working_sells || 0);
        if (working) { badge = '<span class="badge">' + working + '</span>'; }
      }
      // The label in its own span, so a long one is cut with "…" on
      // ONE line - the whole of it on hover.
      html += '<button class="tab' + (state.active === id ? ' on' : '') +
        '" data-panel="' + id + '" title="' + escapeHtml(label) + '">' +
        '<span class="tab-label">' + escapeHtml(label) + '</span>' + badge +
        '</button>';
    });
    el('tabs').innerHTML = html;
  }

  //: The keypad presets, in the order the reference screen has them.
  var QUANTITY_KEYS = {'1': 1, '2': 5, '3': 10, '4': 50, '5': 100};

  function onKey(e) {
    // Ctrl+L, the shortcut every Windows user already has in their
    // fingers for locking something. Before the modifier check below,
    // which exists to keep the single-letter ORDER keys away from
    // shortcuts - and this one is deliberately not a single letter.
    if ((e.ctrlKey || e.metaKey) && (e.key === 'l' || e.key === 'L')) {
      var button = el('lock-now');
      if (button && !button.hidden) {
        e.preventDefault();
        lockNow();
      }
      return;
    }
    if (e.key === 'Escape') {
      closeModal();
      el('help-overlay').classList.add('hidden');
      return;
    }
    // Never steal a key from a field being typed into.
    var tag = (document.activeElement || {}).tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') { return; }
    if (e.metaKey || e.ctrlKey || e.altKey) { return; }
    // Turned off in the taskbar: B and S are ORDERS, and a desk that
    // does not want a keyboard near them can have none. Escape and ?
    // above still work — neither of them trades.
    if (state.keysOff) { return; }

    if (e.key === '?') {
      el('help-overlay').classList.toggle('hidden');
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      return focusNextLadder();
    }

    var key = activeLadder();
    if (!key) { return; }
    var lower = e.key.toLowerCase();

    if (QUANTITY_KEYS[e.key] !== undefined) {
      setArmed(key, 'buy', QUANTITY_KEYS[e.key]);
      setArmed(key, 'sell', QUANTITY_KEYS[e.key]);
      return render();
    }
    if (e.key === '0') {
      setArmed(key, 'buy', '');
      setArmed(key, 'sell', '');
      return render();
    }
    if (lower === 'b') { return atTouch(key, 'BUY'); }
    if (lower === 's') { return atTouch(key, 'SELL'); }
    if (lower === 'f') { return exitPair(key); }
    if (lower === 'x') { return send('cancel_where', {pair: key}); }
    if (lower === 'l') {
      // Through the same path as the tick, so the keyboard and the
      // checkbox cannot end up disagreeing about a ladder that is
      // held: the key used to set the flag and leave the box unticked.
      setLocked(key, !state.locked[key]);
      return render();
    }
    if (lower === 'm') {
      var pair = state.snapshot.pairs[key] || {};
      return setPair(key, {order_type: pair.order_type === 'MARKET'
                           ? 'LIMIT' : 'MARKET'});
    }
  }

  function activeLadder() {
    if (!state.active || state.active.indexOf('ladder:') !== 0) { return null; }
    return state.active.slice('ladder:'.length);
  }

  function focusNextLadder() {
    var ladders = state.open.filter(function (id) {
      return id.indexOf('ladder:') === 0;
    });
    if (!ladders.length) { return; }
    var at = ladders.indexOf(state.active);
    state.active = ladders[(at + 1) % ladders.length];
    render();
  }

  // -- polling -------------------------------------------------------------

  function poll() {
    fetch('/api/status').then(function (r) { return r.json(); })
      .then(function (snapshot) {
        state.snapshot = snapshot;
        // The server is the authority on this. The overlay follows what
        // it says and never decides for itself - a browser that thought
        // it was unlocked would only be lying to the person in front of
        // it, because every order still goes through the guard.
        showLock(snapshot.lock || {});
        // A ghost whose order is now in the book has done its job.
        state.pending.forEach(function (ghost) {
          var pair = snapshot.pairs[ghost.pair_key] || {};
          var arrived = (pair.orders || []).some(function (order) {
            return order.side === ghost.side &&
              Math.abs(order.level - ghost.level) < 1e-9;
          }) || (pair.positions || []).some(function (position) {
            return position.opened_at * 1000 > ghost.at - 2000;
          });
          if (arrived) { ghost.done = true; }
        });
        var first = !state.open.length;
        // First load is LADDERS, and nothing else. The Market Grid and
        // the Positions window are tools: opened when wanted, from the
        // + button or the taskbar. Opening them here put two wide
        // windows over the ladders before the trader had asked for
        // either.
        // A ladder for every enabled pair — including one added while
        // this screen was open. A pair configured on the Exchanges page
        // and then nowhere to be seen is the whole setup looking
        // broken; the ladder appears beside the others the moment the
        // engine picks the pair up. A ladder the trader CLOSED stays
        // closed: that was a decision, and it is remembered.
        Object.keys(snapshot.pairs || {}).forEach(function (key) {
          var id = panelId('ladder', key);
          if (!snapshot.pairs[key].enabled) { return; }
          if (state.open.indexOf(id) >= 0 || state.closed[id]) { return; }
          state.open.unshift(id);
          if (!first) {
            state.reveal = id;
            toast('new ladder: ' + (snapshot.pairs[key].name || key), 'ok');
          }
        });
        if (first) { state.active = state.open[0]; }
        restoreOpenTools(snapshot);
        // Redraw the PANELS only when the coordinator has actually
        // published something new. `at` is its own publish clock, so a
        // stalled or restarting engine — the state this screen is in
        // exactly when the operator is trying to fix it — costs one
        // banner update per poll instead of a full redraw of every
        // ladder, the grid and the monitor.
        // A RESTING order that fills does so without a click, so the
        // sound has to come from the book changing rather than from a
        // command's answer.
        var positions = 0;
        Object.keys(snapshot.pairs || {}).forEach(function (key) {
          positions += (snapshot.pairs[key].positions || []).length;
        });
        if (state.positionCount !== null && positions > state.positionCount) {
          sound('filled');
        }
        state.positionCount = positions;
        var published = snapshot.at !== state.lastAt;
        state.lastAt = snapshot.at;
        if (published || state.pending.length) {
          render();
        } else {
          renderStatusLine();
        }
      })
      .catch(function (error) {
        el('engine-banner').textContent =
          'the web process is not answering: ' + error.message;
        el('engine-banner').classList.remove('hidden');
      });
  }

  function start() {
    el('add-panel').addEventListener('click', function (e) {
      e.stopPropagation();
      var menu = el('add-menu');
      if (!menu.classList.contains('hidden')) {
        return menu.classList.add('hidden');
      }
      var pairs = state.snapshot.pairs || {};
      var html = '<div class="menu-title">Ladders</div>';
      var keys = Object.keys(pairs);
      keys.forEach(function (key) {
        var pair = pairs[key];
        var open = state.open.indexOf(panelId('ladder', key)) >= 0;
        html += '<button data-panel="' + panelId('ladder', key) + '">' +
          (pair.name || key) + '<small>' + (pair.symbol_a || '?') + ' / ' +
          (pair.symbol_b || '?') + (open ? ' · open' : '') + '</small>' +
          '</button>';
      });
      if (!keys.length) {
        html += '<div class="menu-note">No pair is configured yet — add ' +
          'one on the Exchanges page. Each pair is its own ladder, and ' +
          'they trade side by side on this desktop.</div>';
      }
      html += '<div class="menu-title">Windows</div>' +
        '<button data-panel="' + panelId('grid') + '">Market Grid' +
        '<small>every ladder on one row each</small></button>' +
        '<button data-panel="' + panelId('monitor') + '">Trading Monitor' +
        '<small>positions, orders, fills, slippage, accounts</small>' +
        '</button>' +
        // The calendar is parked: offered only while the engine has it on.
        ((state.snapshot || {}).calendar
          ? '<button data-panel="' + panelId('econcal') + '">Economic ' +
            'calendar<small>G10 high-impact releases: forecast, actual, ' +
            'surprise</small></button>' : '');
      menu.innerHTML = html;
      menu.classList.remove('hidden');
    });
    el('add-menu').addEventListener('click', function (e) {
      var button = e.target.closest('button');
      if (!button) { return; }
      el('add-menu').classList.add('hidden');
      openPanel(button.dataset.panel);
    });
    window.addEventListener('click', function () {
      el('add-menu').classList.add('hidden');
    });
    el('tabs').addEventListener('click', function (e) {
      var button = e.target.closest('.tab');
      if (!button) { return; }
      state.active = button.dataset.panel;
      // Clicking a tab means "show me that window" — including when it
      // is off the right-hand end of the desktop row.
      state.reveal = button.dataset.panel;
      render();
    });
    el('open-settings').addEventListener('click', function () {
      openPanel(panelId('settings'));
    });
    el('open-managed').addEventListener('click', function () {
      openPanel(panelId('managed'));
    });
    el('kill').addEventListener('click', function () {
      ask('Cancel everything, on every ladder?',
          'This pulls every working order across every pair. Tick nothing ' +
          'and it stops there; the second button also FLATTENS every open ' +
          'position at market, by ticket, which cannot be undone.',
          'Cancel all + flatten', function () {
            send('kill', {flatten: true});
          });
    });
    el('same-login-banner').addEventListener('click', function (e) {
      if (e.target.closest('.banner-open')) {
        return openPanel(panelId('settings'));
      }
      if (!e.target.closest('.banner-close')) { return; }
      state.dismissed['same-login'] =
        el('same-login-banner').dataset.signature;
      el('same-login-banner').classList.add('hidden');
    });
    el('unclaimed-banner').addEventListener('click', function (e) {
      if (e.target.closest('.banner-open')) {
        // Where the table and the buttons that act on it live.
        state.monitorTab = 'reconcile';
        return openPanel(panelId('monitor'));
      }
      if (e.target.closest('.banner-close')) {
        state.dismissed.unclaimed = el('unclaimed-banner').dataset.signature;
        el('unclaimed-banner').classList.add('hidden');
        return;
      }
      var button = e.target.closest('.close-unclaimed');
      if (!button) { return; }
      ask('Close ' + button.dataset.account + ':' + button.dataset.ticket +
          '?',
          'This position is at the broker but not in our book. Closing it ' +
          'is by ticket, at market, and cannot be undone.',
          'Close it', function () {
            send('close_unclaimed', {account: button.dataset.account,
                                     ticket: button.dataset.ticket});
          });
    });
    try {
      state.keysOff =
        window.localStorage.getItem('okxtrader.keysOff') === '1';
    } catch (e) { state.keysOff = false; }
    function paintKeys() {
      var button = el('keys-toggle');
      button.textContent = 'Keys: ' + (state.keysOff ? 'off' : 'on');
      button.classList.toggle('off', state.keysOff);
    }
    paintKeys();
    try {
      state.soundOff =
        window.localStorage.getItem('okxtrader.soundOff') === '1';
    } catch (e) { state.soundOff = false; }
    function paintSound() {
      var button = el('sound-toggle');
      button.textContent = 'Sound: ' + (state.soundOff ? 'off' : 'on');
      button.classList.toggle('off', state.soundOff);
    }
    paintSound();
    el('sound-toggle').addEventListener('click', function () {
      state.soundOff = !state.soundOff;
      try {
        window.localStorage.setItem('okxtrader.soundOff',
                                    state.soundOff ? '1' : '0');
      } catch (e) { /* a preference that does not survive a reload */ }
      paintSound();
      if (!state.soundOff) { sound('placed'); }   // so it is heard once
    });
    el('keys-toggle').addEventListener('click', function () {
      state.keysOff = !state.keysOff;
      try {
        window.localStorage.setItem('okxtrader.keysOff',
                                    state.keysOff ? '1' : '0');
      } catch (e) { /* a preference that does not survive a reload */ }
      paintKeys();
      toast(state.keysOff
        ? 'keyboard shortcuts are OFF — the buttons and the ladder still work'
        : 'keyboard shortcuts are back on', 'ok');
    });
    el('help').addEventListener('click', function () {
      el('help-overlay').classList.toggle('hidden');
    });
    el('help-close').addEventListener('click', function () {
      el('help-overlay').classList.add('hidden');
    });
    el('modal-cancel').addEventListener('click', closeModal);
    if (el('modal-alt')) {
      el('modal-alt').addEventListener('click', function () {
        var handler = el('modal')._onAlt;
        closeModal();
        if (handler) { handler(); }
      });
    }
    el('modal-confirm').addEventListener('click', function () {
      var handler = el('modal')._onConfirm;
      closeModal();
      if (handler) { handler(); }
    });
    // FIRST, and in the CAPTURE phase, so nothing downstream ever sees
    // the event: the ladder's own handlers, the shortcut keys and the
    // window manager all hang off listeners added below this one.
    ['click', 'pointerdown', 'dblclick', 'keydown', 'wheel'].forEach(
      function (kind) {
        document.addEventListener(kind, function (e) {
          if (!screenIsLocked()) { return; }
          // Everything inside the lock box is how you get OUT, so it
          // is the one place that goes through.
          if (e.target.closest && e.target.closest('#lock-overlay')) {
            return;
          }
          e.stopPropagation();
          e.preventDefault();
        }, true);
      });
    el('lock-form').addEventListener('submit', function (e) {
      e.preventDefault();
      tryUnlock();
    });
    var lockButton = el('lock-now');
    if (lockButton) { lockButton.addEventListener('click', lockNow); }
    document.addEventListener('keydown', onKey);
    window.addEventListener('click', function (e) {
      var window_ = e.target.closest('.window');
      if (!window_) { return; }
      state.active = panelIdOf(window_);
      raise(window_);
    });
    // Delegated, on the desktop rather than on each window: panels come
    // and go, and a listener per window is a listener per window to
    // forget.
    el('desktop').addEventListener('pointerdown', startDrag);
    loadLayoutFromDesk();
    el('desktop').addEventListener('click', function (e) {
      var button = e.target.closest('.winbtn.max');
      if (button) { toggleMaximized(button.closest('.window')); }
    });
    el('desktop').addEventListener('dblclick', function (e) {
      if (!e.target.closest('.titlebar') ||
          e.target.closest('button, select, input, a')) { return; }
      toggleMaximized(e.target.closest('.window'));
    });
    // The desk changes size with the browser: a maximized window follows.
    window.addEventListener('resize', function () {
      Array.prototype.forEach.call(
        document.querySelectorAll('.window.maximized'), applyMaximized);
    });
    // Tidy tiles the desk; with Shift held, every window back in the
    // plain row instead.
    el('tidy').addEventListener('click', function (e) {
      if (e.shiftKey) { tidyWindows(); render(); } else { tileWindows(); }
    });
    poll();
    window.setInterval(poll, REFRESH_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  // The settings panel lives in its own file and borrows these: one
  // modal, one toast rack, one command path. A second copy of any of
  // them is a second set of house rules to keep.
  // The fills list stays folded or open across the poll's repaint:
  // `toggle` does not bubble, so it is caught on the way down.
  document.addEventListener('toggle', function (event) {
    var box = event.target;
    if (box && box.classList && box.classList.contains('slip-detail')) {
      state.slipDetailOpen = box.open;
      return;
    }
    if (!box || !box.classList || !box.classList.contains('aw-fills')) {
      return;
    }
    state.fillsOpen = state.fillsOpen || {};
    state.fillsOpen[box.dataset.key] = box.open;
  }, true);

  window.MT5Trader = {
    state: state, render: render, toast: toast, ask: ask, send: send,
    panelId: panelId, openPanel: openPanel, closePanel: closePanel,
    fmt: fmt, money: money, DASH: DASH,
    tidyWindows: tidyWindows, tileWindows: tileWindows, sound: sound,
    sizingPane: sizingPane, sizingRow: sizingRow, meanExitWords: meanExitWords, volumeProfile: volumeProfile, addSizeWords: addSizeWords, sizingSheetHtml: sizingSheetHtml, mirrorPane: mirrorPane,
    showFairWindow: showFairWindow, setAlgo: setAlgo,
    calcPosition: calcPosition,
    toastOutcome: toastOutcome
  };
})();
