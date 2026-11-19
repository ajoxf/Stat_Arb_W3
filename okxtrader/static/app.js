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

  function toast(message, kind) {
    var box = document.createElement('div');
    box.className = 'toast' + (kind === 'ok' ? ' ok' : '');
    box.innerHTML = '<span class="dismiss">&times;</span>';
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
                  {hz: 523, ms: 180, gain: 0.08}]
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

  function stickyToast(message) {
    /* A SIGNALS alert: it stays until the trader dismisses it. The
     * trade it asks for is theirs to make, so it must not scroll away
     * unread the way an ordinary outcome does. */
    var box = document.createElement('div');
    box.className = 'toast signal';
    box.innerHTML = '<span class="dismiss">&times;</span>';
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
      stickyToast((row.name || key) + ': ' + alert.text);
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
    render();
  }

  function closePanel(id) {
    state.open = state.open.filter(function (other) { return other !== id; });
    // Closing is a decision, and it is remembered: a ladder that came
    // back on the next poll would be a window that cannot be closed.
    state.closed[id] = true;
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
          : node.classList.contains('chartwin')
            ? panelId('chart', node.dataset.pair)
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

  function writeLayout() {
    try {
      window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    } catch (e) {
      // A layout that cannot be saved is a layout that does not survive
      // a reload. Nothing else about the screen depends on it.
    }
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
  var FLOATING_BY_DEFAULT = ['grid:', 'monitor:', 'settings:'];

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
               node.classList.contains('algowin')) {
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
      layout[id] = {left: at.left, top: at.top, z: saved.z || 10};
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
      var at = place(node, event.clientX - grabX - frame.left,
                     event.clientY - grabY - frame.top);
      layout[panelIdOf(node)].left = at.left;
      layout[panelIdOf(node)].top = at.top;
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
      var to = size(node, box.width + (event.clientX - startX),
                    box.height + (event.clientY - startY));
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
  var CHART_SHOWN = 100;
  //: How old a history may be before the engine is asked again. The
  //: engine reuses its own for five minutes as well.
  var CHART_HISTORY_MS = 300000;

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
    if (held.busy) { return; }
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
      if (fresh) { return finish(body); }
      if (body && body.ok) { state.chartHist[id] = body; }
      // Asked already, a moment ago: wait for that answer.
      if (held.asked && at - held.asked < 20000) { return finish(null); }
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
            var data = (result && result.data) || {};
            if (!result || result.ok === false || data.ok === false) {
              state.chartHistNote[id] = (result && result.error) ||
                data.reason || 'no answer from the engine';
              return finish(null);
            }
            delete state.chartHistNote[id];
            read().then(finish).catch(function () { finish(null); });
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
               grid: 'rgba(128,128,128,.25)', text: '#6c757d'};

  function mergeLive(history, live, seconds, anchor, since) {
    /* MT5's candles with the live 10-second ones folded into them: a
     * live price makes a candle's high, low and close; a bucket the
     * history has not reached yet starts a new candle. */
    var byT = {};
    var out = history.map(function (c) {
      var copy = {t: c.t, o: c.o, h: c.h, l: c.l, c: c.c};
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
    var w = Math.max(box.clientWidth, 200);
    var h = Math.max(box.clientHeight, 120);
    var left = 52, right = 8, top = 8, bottom = 18;
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
    var body = Math.max(1, Math.min(12, slot * 0.7));
    var svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' +
      w + ' ' + h + '" xmlns="http://www.w3.org/2000/svg">';
    // Ticks the caller names (z: whole sigmas), or five even ones.
    var ticks = (opts.ticks || [0, 1, 2, 3, 4].map(function (k) {
      return lo + (hi - lo) * k / 4;
    })).filter(function (v) { return v >= lo && v <= hi; });
    ticks.forEach(function (v) {
      svg += '<line x1="' + left + '" x2="' + (w - right) + '" y1="' +
        Y(v).toFixed(1) + '" y2="' + Y(v).toFixed(1) + '" stroke="' +
        CHART.grid + '" stroke-width="1"/>' +
        '<text x="' + (left - 4) + '" y="' + (Y(v) + 3).toFixed(1) +
        '" text-anchor="end" font-size="10" fill="' + CHART.text + '">' +
        v.toFixed(opts.digits) + '</text>';
    });
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
        '<line x1="' + x.toFixed(1) + '" x2="' + x.toFixed(1) + '" y1="' +
        Y(c.h).toFixed(1) + '" y2="' + Y(c.l).toFixed(1) + '" stroke="' +
        colour + '" stroke-width="1"/>' +
        '<rect x="' + (x - body / 2).toFixed(1) + '" y="' + yTop.toFixed(1) +
        '" width="' + body.toFixed(1) + '" height="' +
        Math.max(1, yBot - yTop).toFixed(1) + '" fill="' + colour +
        '" stroke="' + (c.beyond ? '#000' : colour) + '"/></g>';
    });
    // The time along the bottom: five labels evenly across the clock,
    // the day added where the chart spans more than one.
    if (times.length) {
      var withDay = t1 - t0 > 20 * 3600;
      [0, 0.25, 0.5, 0.75, 1].forEach(function (f, k) {
        var anchor = k === 0 ? 'start' : k === 4 ? 'end' : 'middle';
        svg += '<text x="' + (left + f * plotW).toFixed(1) + '" y="' +
          (h - 4) + '" font-size="10" text-anchor="' + anchor + '" fill="' +
          CHART.text + '">' + chartClock(t0 + f * (t1 - t0), withDay) +
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
    var stamp = [tf, hist ? hist.at : '', live.at || '',
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
        ? 'no candles: ' + why : 'reading MT5’s bars…') + '</div>';
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
    var shown = all.slice(-CHART_SHOWN);
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
                                         times: times, seconds: seconds});
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
      ' Outlined: closed beyond the entry band. Candles from MT5’s ' +
      'bars, the newest moving live.';
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
      // Redraw THIS ladder as it is typed, so the button beside the box
      // carries the size before the next poll. The box being typed into
      // is never written back over — it has focus.
      if (state.snapshot.pairs[key]) {
        renderLadder(key, state.snapshot.pairs[key]);
      }
    });
    node.querySelector('.sell-qty').addEventListener('input', function (e) {
      setArmed(key, 'sell', e.target.value);
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
      render();
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
    ['.ls-clip-a', 'clip_lots_a', 'live-number'],
    ['.ls-clip-b', 'clip_lots_b', 'live-number'],
    // Blank is a REAL state here and the normal one: it means MT5's.
    ['.ls-contract-a', 'contract_size_a', 'blank-number'],
    ['.ls-contract-b', 'contract_size_b', 'blank-number'],
    ['.ls-quoting', 'quoting_leg', 'live'],
    // What the window reads: None, Fair spread or Algo. ONE control
    // for one decision. Fair spread is the saved `algo_window`; Algo is
    // a switch on the running engine, never saved, off after a restart.
    ['.ls-algo', 'algo_window', 'algo-choice'],
    ['.ls-comm-a', 'commission_per_lot_a', 'number', 'COMMISSION_PER_LOT_A'],
    ['.ls-comm-b', 'commission_per_lot_b', 'number', 'COMMISSION_PER_LOT_B'],
    // Blank is a REAL state: it means the desk-wide threshold.
    ['.ls-stale', 'max_quote_age_sec', 'blank-number'],
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
        ', MT5 says ' + fmt(fromMt5, 2);
    }
    function inUnits(lots, contract) {
      return contract ? ' = ' + fmt(lots * contract, 2) + ' units' : '';
    }
    note.textContent =
      'In force: 1 Qty = ' + fmt(lotsA, 2) + ' lots ' +
      (live.symbol_a || 'A') + inUnits(lotsA, live.contract_a) +
      ' / ' + fmt(lotsB, 2) + ' lots ' +
      (live.symbol_b || 'B') + inUnits(lotsB, live.contract_b) +
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
        : ' Contract sizes come from MT5 and are never typed in.');
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
        input.placeholder = entry[1] ? 'from MT5' : entry[2];
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
                          '.qty-box').forEach(function (control) {
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
    ours.textContent = session.ours === false ? 'legs' : 'ours';
    ours.classList.toggle('legs', session.ours === false);

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
      button.textContent = (side === 'buy' ? 'BUY' : 'SELL') + sized;
    });
    Array.prototype.forEach.call(node.querySelectorAll('.keypad .qty'),
      function (button) {
        var size = parseFloat(button.dataset.qty);
        button.classList.toggle('on',
          armedFor(key, 'buy') === size && armedFor(key, 'sell') === size);
      });
    // Which accounts this ladder is routed across, and their logins.
    var accounts = state.snapshot.accounts || {};
    function routeText(account, symbol) {
      var info = accounts[account] || {};
      var login = info.login ? ' #' + info.login : '';
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
    node.querySelector('.route-a b').innerHTML =
      routeText(row.account_a, row.symbol_a);
    node.querySelector('.route-b b').innerHTML =
      routeText(row.account_b, row.symbol_b);
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
      ? DASH : money(row.open_pnl);
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
      return 'in ' + positionWords(first, digits) +
        (held.length > 1 ? ' (+' + (held.length - 1) + ' more)' : '') +
        ' — TP ' + levelWords(first.tp, first.tp_money, digits) + ' · SL ' +
        (first.sl === null || first.sl === undefined
          ? (((block.params || {}).stop_loss_on) ? '—' : 'off')
          : levelWords(first.sl, first.sl_money, digits));
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
    /* "BUY @ 59.11": the side and the spread it was ENTERED at. */
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
      '">' + (s.candles || 0) + ' candles of MT5 history, ' + when(s.from) +
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
      ? (first.source && first.source !== 'ALGO' ? 'YOUR ' : '') +
        (first.side === 'BUY' ? 'LONG' : 'SHORT') + ' ' +
        fmt(first.quantity, 2) : 'FLAT';
    var html = '<div class="aw-head">Signal &amp; Position</div>' +
      '<div class="aw-tiles">' + tile('SELL') + tile('BUY') +
      '<div class="aw-pos ' + (first ? (first.side === 'BUY' ? 'long' : 'short')
        : 'flat') + '">' + position + '</div></div>' +
      '<div class="aw-line" title="' + escapeHtml(block.blocked || '') + '">' +
      escapeHtml(algoLine(block, digits)) +
      (block.health && block.state !== 'BLOCKED' ? ' — ' + escapeHtml(block.health)
        : '') + '</div>';
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
        return kv('Leg ' + leg.toUpperCase() + ' (' +
                  (first['leg_' + leg + '_side'] || '?') + ')',
                  fmt(entry, 2) + ' \u2192 ' + fmt(now, 2) +
                  (change === null ? '' : ' <small>(' +
                    (change > 0 ? '+' : '') + fmt(change, 2) + ')</small>'),
                  '', 'the leg\u2019s fill \u2192 the price it would close at now',
                  true);
      }
      html += '<div class="aw-grid">' +
        kv('Entry spread', fmt(first.entry_spread, digits)) +
        kv('\u0394 spread', delta === null ? DASH
          : (delta > 0 ? '+' : '') + fmt(delta, digits), good,
          'closing price now \u2212 entry: a LONG makes money when this is '
          + 'positive, a SHORT when it is negative') +
        kv('Entry z', signed(first.entry_z)) +
        kv('Net P&amp;L', moneyOr(first.net_pnl),
           first.net_pnl === null || first.net_pnl === undefined ? ''
             : (first.net_pnl >= 0 ? 'up' : 'down')) +
        legRow('a') + legRow('b') +
        kv('Age', first.age_sec === null || first.age_sec === undefined ? DASH
          : Math.floor(first.age_sec / 60) + 'm ' +
            Math.floor(first.age_sec % 60) + 's') +
        kv('Size', fmt(first.quantity, 2) + ' spread(s)') +
        '</div>' +
        kv('Levels', 'BE ' + fmt(first.break_even, digits) + ' · TP ' +
           (first.tp === null || first.tp === undefined ? DASH
             : levelWords(first.tp, first.tp_money, digits)) + ' · SL ' +
           (first.sl === null || first.sl === undefined
             ? (params.stop_loss_on ? DASH : 'off')
             : levelWords(first.sl, first.sl_money, digits)) +
           (first.stop_mode === 'ATR' || first.target_mode === 'ATR'
             ? ' <small>(ATR ' + fmt(first.entry_atr, digits) + ' at entry: ' +
               (first.target_mode === 'ATR' ? 'TP ' + (first.atr_target_mult || params.atr_target_mult) + '\u00d7' : 'TP %') +
               ', ' + (first.stop_mode === 'ATR' ? 'SL ' + (first.atr_stop_mult || params.atr_stop_mult) + '\u00d7' : 'SL %') +
               ')</small>' : ''),
           '', 'compare with the CLOSING price: the Sell spread for a LONG, '
           + 'the Buy spread for a SHORT. In brackets: the net P&L of the '
           + 'whole position closing there, after every cost') +
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
      kv('Candles', (block.timeframe_min || params.timeframe_min) + 'm \u00d7 ' +
         needed) +
      kv('Band', fmt(block.lower, digits) + ' \u2026 ' + fmt(block.upper, digits)) +
      '</div><div class="aw-data" title="' + escapeHtml(history.note || '') + '">' +
      '<span>Data</span><div class="aw-bar"><div class="' +
      (block.ready ? 'ok' : 'wait') + '" style="width:' +
      (block.ready ? 100 : pct) + '%"></div></div><span>' +
      (block.ready ? 'candles ready' : count + '/' + needed + ' candles') +
      '</span></div>' + warmupHtml(block.warmup);

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
      '</div><div class="aw-grid">' +
      kv('Capture / cost', (edge.ratio === null || edge.ratio === undefined
          ? DASH : edge.ratio.toFixed(2) + '\u00d7') + ' / req ' +
          (edge.required === undefined ? DASH : edge.required + '\u00d7'),
         edge.ok === true ? 'up' : (edge.ok === false ? 'down' : ''),
         'expected capture ' + moneyOr(edge.capture) +
         ' (' + (params.edge_capture_frac || 0.5) + ' \u00d7 entry z \u00d7 '
         + '\u03c3) against the round trip, at the entry z', true) +
      kv('Round trip', moneyOr(cost.total), '',
         'crossing ' + moneyOr(cost.crossing) + ' + commission ' +
         moneyOr(cost.commission) + ' + slippage ' + moneyOr(cost.slippage)) +
      kv('Today', (day.trades || 0) + (params.max_trades_day
          ? '/' + params.max_trades_day : '') + ' trades · ' +
          (day.losses_row || 0) + ' in a row · ' + moneyOr(day.pnl),
         '', 'the day\u2019s limits stop entries, never exits', true) +
      kv('Size', fmt(filters.qty || params.algo_qty || 1, 2) + ' spread(s)') +
      '</div><div class="aw-blocked"><div class="aw-head2">Last signal blocked'
      + '</div>' + (last
        ? '<div><b>' + escapeHtml(sideWords(last.side)) + '</b> z ' +
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
                     + 'look at this symbol in MT5'
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
    return html + '</tbody></table>';
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
      [['ENTRY', held.entry_spread, 'entry'], ['BE', held.break_even, 'be'],
       ['TP', held.tp, 'tp'], ['SL', held.sl, 'sl']].forEach(function (m) {
        if (m[1] !== null && m[1] !== undefined && isFinite(m[1])) {
          levelMarks.push({tag: m[0], at: m[1], cls: m[2]});
        }
      });
    });
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
        '<button data-tab="analysis">Analysis</button></div>' +
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
    else { pane.innerHTML = reconcileTable(); }
    node.querySelector('.monitor-note').textContent =
      state.monitorTab === 'analysis'
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

  function positionsTable() {
    var html = '<table><thead><tr><th>Pair</th><th>Side</th><th>Net</th>' +
      '<th>Avg entry</th><th>Mark</th><th>Open P&amp;L</th><th>Mode</th>' +
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
      html += '<td class="' + (position.net_pnl > 0 ? 'up' : 'down') + '">' +
        money(position.net_pnl) + '</td>';
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
      html += '<tr class="detail"><td colspan="11">' +
        legDetail(position.leg_a, 'A') + ' &nbsp; ' +
        legDetail(position.leg_b, 'B') + '</td></tr>';
    });
    if (!any) { html += '<tr><td colspan="11">nothing open</td></tr>'; }
    html += '</tbody></table>';
    html += accountReconciliation();
    return html;
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
      return '<div class="note">our P&amp;L and MT5’s own have not yet ' +
        'been read together, so there is nothing to reconcile — ' +
        'unmeasured, not zero.</div>';
    }
    if (check.ours === null || check.ours === undefined ||
        check.theirs === null || check.theirs === undefined) {
      return '<div class="note">' + (
        check.theirs === null || check.theirs === undefined
          ? 'MT5’s own profit could not be read'
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
      '</td><td>MT5’s own</td><td>' + money(check.theirs) +
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
    return (job.phase === 'recovering'
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
          job.side + '">';
        html += '<td>' + (row.name || key) + '</td>';
        html += '<td>' + job.side + '</td>';
        html += '<td>' + fmt(job.level, 4) + '</td>';
        html += '<td>' + qty(job.spreads) + '</td>';
        html += '<td>' + DASH + '</td>';
        html += '<td>' + (job.phase === 'recovering' ? 'RECOVERING'
                                                     : 'MAKER') + '</td>';
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
        html += '<td><button class="btn cancel-maker">Cancel</button></td>';
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
      '<span class="hint">Read back from MT5\'s own deal history — so it ' +
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

  function slippageTable() {
    var report = state.slippage;
    var html = '<div class="journal-controls">' +
      '<label class="check"><input type="checkbox" class="all-sessions"' +
      (state.slippageAll ? ' checked' : '') + '> every session</label>' +
      '<a class="btn" href="/api/slippage.csv' +
      (state.slippageAll ? '?session=all' : '') + '" download>Export CSV</a>' +
      '<span class="hint">Measured against the price that was clicked, ' +
      'on the fills the broker actually gave us.</span></div>';

    if (report === null) { return html + '<p class="note">loading…</p>'; }
    if (report.error) {
      return html + '<p class="note mismatch">' + report.error + '</p>';
    }

    var window_ = report.window || {};
    html += '<table><tbody><tr><td>' + (window_.label || DASH) + '</td>' +
      '<td>' + report.counts.positions + ' positions (' +
      report.counts.open + ' still open)</td>';
    var journal = report.journal;
    html += '<td>' + (journal
      ? journal.fills + ' of our fills at the broker over the same window'
      : 'the journal could not be counted') + '</td></tr></tbody></table>';
    if (window_.note) {
      html += '<div class="' + (window_.clock === 'broker' ? 'hint' : 'note') +
        '">' + window_.note + '</div>';
    }

    if (!report.counts.positions) {
      return html + '<p class="note">nothing was traded in this window. ' +
        'That is not a slippage of zero — there is nothing to measure ' +
        'yet.</p>';
    }

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

    // WHICH LEG it comes from: each leg against the touch it was SENT
    // at, in that leg's own price points (positive a cost).
    html += '<table><thead><tr><th>By leg (own price points)</th>' +
      '<th>Leg A</th><th>Leg A entry</th><th>Leg A exit</th>' +
      '<th>Leg B</th><th>Leg B entry</th><th>Leg B exit</th>' +
      '</tr></thead><tbody>';
    Object.keys(report.by_pair).forEach(function (key) {
      var group = report.by_pair[key];
      var legs = group.legs || {};
      var sample = (report.rows || []).filter(function (r) {
        return r.pair_key === key;
      })[0] || {};
      function cell(name) {
        var leg = legs[name] || {};
        return leg.measured ? slipPoints(leg.points_mean)
          : '<td><span class="hint">not kept</span></td>';
      }
      html += '<tr><td>' + escapeHtml(group.name || key) + '</td>' +
        '<td>' + escapeHtml(sample.symbol_a || 'A') + '</td>' +
        cell('entry_leg_a') + cell('exit_leg_a') +
        '<td>' + escapeHtml(sample.symbol_b || 'B') + '</td>' +
        cell('entry_leg_b') + cell('exit_leg_b') + '</tr>';
    });
    html += '</tbody></table>';

    if ((report.worst || []).length) {
      html += '<table><thead><tr><th>Worst entries</th><th>Side</th>' +
        '<th>Qty</th><th>Type</th><th>Slip</th><th>Cost</th>' +
        '<th>Leg A</th><th>Leg B</th>' +
        '<th>Click→on</th><th>Opened</th></tr></thead><tbody>';
      report.worst.forEach(function (row) {
        html += '<tr><td>' + (row.pair_key || DASH) + '</td>';
        html += '<td>' + (row.side || DASH) + '</td>';
        html += '<td>' + qty(row.quantity) + '</td>';
        html += '<td>' + (row.order_type || DASH) + '</td>';
        html += slipPoints(row.entry_points);
        html += slipMoney(row.entry_money);
        html += slipPoints(row.entry_leg_a);
        html += slipPoints(row.entry_leg_b);
        html += '<td>' + (row.click_to_on_ms === null ||
                          row.click_to_on_ms === undefined
          ? DASH : Math.round(row.click_to_on_ms) + 'ms') + '</td>';
        html += '<td>' + localTime(row.opened_at) + '</td></tr>';
      });
      html += '</tbody></table>';
    }
    return html;
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
    state.deriveNote = {html: 'reading MT5\u2019s bars\u2026', cls: 'hint'};
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
      t.replayed + ' replayed from MT5\u2019s bars. A replayed row ' +
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
    var SIGNAL_COLS = 18;
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
      '<th>z in → out</th><th>Spread in → out</th>' +
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
          'replayed afterwards from MT5’s bars — one look per ' +
          'candle, so a stop touched and recovered inside one is not seen' :
          'recorded live off the book, as it happened') + '">' +
        (r.source === 'REPLAY' ? 'replayed' : 'live') + '</td>' +
        '<td>' + (r.quantity === null || r.quantity === undefined ? DASH
                  : qty(r.quantity)) + '</td>' +
        edgeOf(r) +
        '<td>' + fmt(r.armed_z, 2) + ' → ' + fmt(r.closed_z, 2) +
        '</td>' +
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
      'exception: it was reconstructed from MT5’s bars by ' +
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
    function cell(c) {
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
      return '<td title="' + escapeHtml(title) + '"><b>' + shown + '</b>' +
        (c.unpriced ? '<sup class="down" title="' + c.unpriced +
          ' not priced">*</sup>' : '') +
        '<div class="hint">' + c.fills + ' deal' +
        (c.fills === 1 ? '' : 's') + ' \u00b7 ' + fmt(c.lots, 2) +
        ' lots</div></td>';
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
    rows.forEach(function (r) {
      html += '<tr><td class="l">' + name(r.pair_key) + '</td>' +
        periods.map(function (p) { return cell(r[p.key]); }).join('') +
        '</tr>';
    });
    if (rows.length > 1) {
      html += '<tr class="an-total"><td class="l"><b>All ladders</b></td>' +
        periods.map(function (p) { return cell(data.total[p.key]); })
          .join('') + '</tr>';
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
          periods.map(function (p) { return cell(r[p.key]); }).join('') +
          '</tr>';
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
      }).join('') + '</select></div>';
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

    // 1. The summary.
    html += '<h4>Summary</h4><div class="an-tiles">' +
      anTile('Trades', s.count || 0) +
      anTile('Closed', s.closed || 0) +
      anTile('Net P&amp;L', money(s.net_pnl), anSign(s.net_pnl),
           'realized, after the commission the settings charge') +
      anTile('Win rate', anPct(s.win_rate), '',
           (s.wins || 0) + ' won, ' + (s.losses || 0) + ' lost') +
      '</div>';

    // 1b. NOTIONAL TRADED: lots x contract size x price, on the broker's
    // own deals - today, and over the last 7 and 30 days.
    html += notionalHtml(a.notional, name);

    // 2. Drawdown, and each trade's worst and best point.
    var d = a.drawdown || {};
    html += '<h4>Drawdown &amp; excursion <small>\u2014 how bad it got, ' +
      'and how good</small></h4><div class="an-tiles">' +
      anTile('Max drawdown', d.max === null || d.max === undefined ? DASH
           : money(-d.max), d.max ? 'down' : '',
           'the worst fall of the running P&L from a peak, closed trades in ' +
           'the order they closed') +
      anTile('Worst trade', money(d.worst_trade), anSign(d.worst_trade),
             'best: ' + money(d.best_trade)) +
      anTile('Below the peak now', d.current === null || d.current === undefined
           ? DASH : money(-d.current), d.current ? 'down' : '') +
      anTile('Peak equity', money(d.peak_equity), 'up') + '</div>';
    var rows = a.excursion || [];
    html += '<table class="an-table"><thead><tr><th>Closed</th><th class="l">Pair</th>' +
      '<th class="l">Side</th><th class="l">Exit</th>' +
      '<th title="Maximum adverse excursion: the worst net P&L while open">' +
      'MAE (worst)</th>' +
      '<th title="Maximum favourable excursion: the best net P&L while open">' +
      'MFE (best)</th><th>Net P&amp;L</th>' +
      '<th title="How much of its best a WINNING trade kept: net / MFE">Kept</th>' +
      '<th title="Minutes from the open to the best point">Best at</th>' +
      '</tr></thead><tbody>';
    if (!rows.length) {
      html += '<tr><td colspan="9" class="hint">no closed trade with its ' +
        'best and worst point yet \u2014 they are kept from this version on' +
        '</td></tr>';
    }
    rows.forEach(function (r) {
      html += '<tr><td>' + anClock(r.closed_at) + '</td><td class="l">' +
        name(r.pair_key) + '</td><td class="l">' + anSide(r.side) +
        '</td><td class="l">' + escapeHtml(r.exit_reason || DASH) + '</td>' +
        '<td class="down">' + (r.mae === null ? DASH : money(-r.mae)) + '</td>' +
        '<td class="up">' + money(r.mfe) + '</td>' +
        '<td class="' + anSign(r.pnl) + '">' + money(r.pnl) + '</td>' +
        '<td>' + anPct(r.utilisation_pct, 0) + '</td>' +
        '<td>' + anMin(r.peak_min) + '</td></tr>';
    });
    html += '</tbody></table>';

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
    html += '<h4>Trade journal <small>— ' + trips.length +
      ' closed, running total ' + money(total) + '</small></h4>' +
      '<table class="an-table"><thead><tr><th>Closed</th><th class="l">Pair</th>' +
      '<th class="l">Side</th><th class="l">Who</th><th>Size</th><th>z in → out</th>' +
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
      '<th>Held</th><th class="l">Exit</th><th>Net P&amp;L</th><th>Running</th>' +
      '</tr></thead><tbody>';
    if (!trips.length) {
      html += '<tr><td colspan="17" class="hint">no closed trade in this ' +
        'window</td></tr>';
    }
    trips.forEach(function (t) {
      var x = t.exit || {};
      html += '<tr><td>' + anClock(t.closed_at) + '</td><td class="l">' +
        name(t.pair_key) + '</td><td class="l">' + anSide(t.side) +
        '</td><td class="l">' +
        (t.source === 'ALGO' ? '<b>Algo</b>' : 'Manual') + '</td><td>' +
        qty(t.quantity) + '</td><td>' + fmt(t.entry_z, 2) + ' → ' +
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
        escapeHtml(t.exit_reason || DASH) +
        '</td><td class="' + anSign(t.pnl) + '">' + money(t.pnl) +
        '</td><td class="' + anSign(t.cum_pnl) + '">' + money(t.cum_pnl) +
        '</td></tr>';
    });
    html += '</tbody></table>';

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
    html += '<h4>Backtest <small>\u2014 the Algo\u2019s own, on MT5\u2019s ' +
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
      html += '<td>' + (row.leverage ? row.leverage + 'x' : DASH) + '</td>';
      html += '<td>' + row.our_legs + '</td>';
      html += '<td>' + fmt(row.our_lots, 2) + '</td>';
      html += '<td>' + fmt(row.our_units, 2) + '</td>';
      html += '</tr>';
    });
    if (!names.length) {
      html += '<tr><td colspan="14">no accounts connected</td></tr>';
    }
    html += '</tbody></table>';
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
    return html;
  }

  function onMonitorClick(e) {
    var button = e.target.closest('button');
    if (!button) { return; }
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
                      'in MT5 too.'};
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

    // Remove the panels that are no longer open.
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (node) {
        if (!wanted[panelIdOf(node)]) { node.remove(); return; }
        // Where it was left, and the size it was left at, every render:
        // panels are created lazily, and one opened later must still
        // come back to its own corner.
        ensureGrip(node);
        applyLayout(node);
      });

    renderTabs();
    revealPanel();
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
        return '<b>' + same[login].join(' and ') + ' are both on MT5 ' +
          'account #' + login + '.</b> One account, not two.';
      }).join(' ');
    banner.title = logins.map(function (login) {
      return same[login].join(' and ') + ' are configured as two accounts ' +
        'but are both attached to MT5 login #' + login + '. If two ' +
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
            : parts[0] === 'grid' ? 'Market Grid'
              : parts[0] === 'settings' ? 'Exchanges' : 'Positions';
      var badge = '';
      if (parts[0] === 'ladder') {
        var row = state.snapshot.pairs[parts.slice(1).join(':')] || {};
        var working = (row.working_buys || 0) + (row.working_sells || 0);
        if (working) { badge = '<span class="badge">' + working + '</span>'; }
      }
      html += '<button class="tab' + (state.active === id ? ' on' : '') +
        '" data-panel="' + id + '">' + label + badge + '</button>';
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
        '</button>';
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
    el('tidy').addEventListener('click', tidyWindows);
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
  window.MT5Trader = {
    state: state, render: render, toast: toast, ask: ask, send: send,
    panelId: panelId, openPanel: openPanel, closePanel: closePanel,
    fmt: fmt, money: money, DASH: DASH,
    tidyWindows: tidyWindows, sound: sound,
    showFairWindow: showFairWindow, setAlgo: setAlgo,
    toastOutcome: toastOutcome
  };
})();
