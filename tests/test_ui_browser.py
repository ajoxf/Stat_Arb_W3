"""The UI, in a real browser.

Python tests cannot see a temporal-dead-zone `ReferenceError` that
aborts an entire script block and silently unregisters a handler —
which is exactly what happened in the system this is ported from, where
clicking Save did a native form submit and reset the page. So this
drives Chromium under Playwright and reads `pageerror`.

It also cross-checks every number input's min/max/step against the
shipped default: two of them once rejected the engine's own defaults,
so Chrome refused to submit and fired no event at all.
"""

import json
import math
import re
import os
import threading
import time
from pathlib import Path

import pytest

# SKIPPED ON ANY ImportError, NOT JUST A MISSING MODULE.
#
# This suite is optional everywhere it matters: a trading desk has no
# browser and does not need one, and `pytest tests/ -q` is the gate
# START-TRADING and UPDATE.BAT run before they will let the machine
# trade. An optional suite must therefore SKIP on a desk, never error.
#
# `pytest.importorskip` alone stopped doing that. Since pytest 9.1 it
# defaults to catching ModuleNotFoundError only, and requirements.txt
# asks for `pytest>=8.0.0`, so a desk installed today gets 9.1 and the
# new behaviour without anyone choosing it.
#
# Live on a locked-down desk: Windows Application Control blocked
# `_greenlet.pyd`, which playwright imports. That is a plain
# ImportError, not a missing module -
#
#     ImportError: DLL load failed while importing _greenlet:
#     An Application Control policy has blocked this file.
#
# - so it was re-raised, collection ERRORED, the gate failed, and
# UPDATE.BAT rolled the machine back off a good version. The engine was
# fine. Nothing could reach it.
#
# Written as try/except rather than `exc_type=ImportError` because that
# parameter only exists from pytest 8.2, and this has to hold across
# every version the fleet might install.
try:
    import playwright.sync_api as playwright_api
except ImportError as exc:                 # missing, or a blocked DLL
    pytest.skip(f'playwright unavailable: {exc}', allow_module_level=True)

from okxtrader.webapp import create_app                        # noqa: E402


#: How long a browser test waits for a publish to reach the screen.
#: Generous on purpose: the assertion is about WHAT the page shows, not
#: how fast, and a container under load runs this suite two-thirds
#: slower — where a 5s wait fails tests that are perfectly correct.
WAIT = 10000


class Publisher:
    """Stands in for the coordinator: it republishes the snapshot on a
    timer, exactly as the real one does. Without that the file ages, the
    web process correctly calls the engine stalled, and every click is
    refused — which is the behaviour under test elsewhere, not here."""

    def __init__(self, path, every=0.2):
        self.path = path
        self.every = every
        self.order_type = 'LIMIT'
        self.confirm = False
        #: Two accounts reporting one MT5 login, as the engine publishes
        #: it — the banner under test is driven from the SNAPSHOT, not
        #: from a value poked into the page that the next poll erases.
        self.same_login = None
        #: One leg frozen, as the age tracker reports it.
        self.stale_leg = False
        #: Orders the broker refused, as the engine republishes them.
        self.dead_orders = None
        #: Break-even and take-profit, and any open position.
        self.exits = None
        self.positions = None
        #: Positions at the broker that our book cannot explain.
        self.unclaimed = None
        #: What the carry says the basis should be, as the engine
        #: publishes it — including the case where the reading is
        #: REPLACED by a warning about the input it came from.
        self.fair = None
        #: The fair-value window is per pair and off by default; the
        #: fixture turns it on so the panels it holds can be read.
        self.show_fair_window = True
        #: Which algo the ladder is running, and what it says. NONE by
        #: default — the state every test that is not about an algo
        #: must see, because an algo must change nothing else.
        self.algo = 'NONE'
        self.algo_block = None
        #: Working orders and their quote groups, as the engine
        #: publishes them. Poked into `state.snapshot` instead, the
        #: next poll — 200ms away — erases them, and the test passes or
        #: fails on which side of that it lands.
        self.orders = None
        self.quotes = None
        #: The counts the cancel buttons are enabled from.
        self.working_buys = 0
        self.working_sells = 0
        #: What is ON. Close @ Limit refuses on a flat ladder, so a
        #: test of it has to publish a position first.
        self.net_position = 0.0
        #: How this ladder gets OUT by default. MARKET in the engine
        #: too: a default that WAITS is a position nobody is getting
        #: out of.
        self.exit_type = 'MARKET'
        self.max_qty = None
        #: How many of the working orders are resting CLOSES. They put
        #: nothing at the broker BY DESIGN, so they must not be counted
        #: against the broker's own number.
        self.resting_closes = 0
        self.broker_pendings = None
        #: Any other field for the pair's row, as the engine sends it.
        self.pair_extra = {}
        #: Fields the terminals report about each account (login,
        #: server, company), merged into the snapshot's accounts.
        self.accounts_extra = {}
        #: Which leg LIMIT mode rests its real pending on. The other
        #: leg is crossed at market when it fills, and a broker window
        #: showing nothing on that leg is the mode, not a fault.
        self.quoting_leg = 'b'
        self.live = threading.Event()
        self.live.set()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            if self.live.is_set():
                self.publish()
            time.sleep(self.every)

    def publish(self, at=None):
        payload = snapshot(self.order_type, self.confirm, self.same_login,
                           self.stale_leg, self.dead_orders, self.exits,
                           self.positions, self.unclaimed, self.fair,
                           self.show_fair_window, self.orders,
                           self.quotes, self.working_buys,
                           self.working_sells, self.algo, self.algo_block,
                           self.net_position, self.quoting_leg,
                           self.exit_type, self.resting_closes,
                           self.broker_pendings, self.max_qty)
        if at is not None:
            payload['at'] = at
        for row in payload['pairs'].values():
            row.update(self.pair_extra)
        if getattr(self, 'btc_dominance', None) is not None:
            payload['btc_dominance'] = self.btc_dominance
        for name, extra in self.accounts_extra.items():
            if extra is None:
                payload['accounts'].pop(name, None)
            else:
                payload['accounts'].setdefault(name, {}).update(extra)
        # A tmp name of its OWN. The timer thread and the test publish
        # at the same moment often enough to matter: sharing one tmp
        # file, the slower writer's file is gone before it renames it,
        # the timer thread dies of the FileNotFoundError, the snapshot
        # stops arriving, and the UI — correctly — refuses every click
        # into a screen with nothing behind it. That looked like a
        # flaky click test and was a flaky FIXTURE.
        tmp = '%s.%d.tmp' % (self.path, threading.get_ident())
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp, self.path)


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    """The real Flask app, on a real port, over a published snapshot."""
    tmp = tmp_path_factory.mktemp('ui')
    paths = {name: str(tmp / f'{name}.json') for name in
             ('status', 'results', 'config')}
    paths['commands'] = str(tmp / 'commands.jsonl')

    publisher = Publisher(paths['status'])
    publisher.publish()
    publisher.thread.start()
    paths['publisher'] = publisher
    with open(paths['config'], 'w', encoding='utf-8') as f:
        json.dump({'accounts': {}, 'pairs': {}}, f)

    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'])
    from werkzeug.serving import make_server
    httpd = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_port}', paths
    publisher.stop.set()
    httpd.shutdown()


def snapshot(order_type='LIMIT', confirm=False, same_login=None,
             stale_leg=False, dead_orders=None, exits=None,
             positions=None, unclaimed=None, fair=None,
             show_fair_window=True, orders=None, quotes=None,
             working_buys=0, working_sells=0, algo='NONE', algo_block=None,
             net_position=0.0, quoting_leg='b', exit_type='MARKET',
             resting_closes=0, broker_pendings=None, max_qty=None):
    rows = []
    for step in range(20, -21, -1):
        level = round(59.10 + step * 0.01, 4)
        rows.append({'level': level, 'work_buy': None, 'work_sell': None,
                     'is_best_bid': step == -1, 'is_best_ask': step == 1,
                     # The mid, which is where the heavy rule goes and
                     # what the ladder centres on.
                     'is_mid': step == 0})
    return {
        'at': time.time(), 'loop_interval_sec': 0.31,
        'confirm_market_clicks': confirm, 'row_height_px': 17,
        'command_poll_sec': 0.02,
        'accounts': {'acct_a': {'profit': 0.0}, 'acct_b': {'profit': 0.0}},
        'account_health': {
            'warn_level': 200.0, 'weakest': 'acct_b', 'weakest_level': 125.0,
            'unknown': [], 'same_login': same_login or {},
            'accounts': {
                'acct_a': {'account': 'acct_a', 'known': True, 'login': 5001,
                           'server': 'CFI-Live', 'currency': 'USD',
                           'leverage': 100, 'balance': 0.0, 'credit': 5000.0,
                           'equity': 5000.0, 'profit': 0.0, 'margin': 500.0,
                           'margin_free': 4500.0, 'margin_level': 1000.0,
                           'so_call': 100.0, 'so_so': 50.0, 'our_lots': 0.1,
                           'our_units': 10.0, 'our_legs': 1, 'tight': False},
                'acct_b': {'account': 'acct_b', 'known': True, 'login': 5002,
                           'server': 'CFI-Live', 'currency': 'USD',
                           'leverage': 100, 'balance': 1000.0, 'credit': 0.0,
                           'equity': 1000.0, 'profit': -20.0, 'margin': 800.0,
                           'margin_free': 200.0, 'margin_level': 125.0,
                           'so_call': 100.0, 'so_so': 50.0, 'our_lots': 0.1,
                           'our_units': 10.0, 'our_legs': 1, 'tight': True}}},
        'reconciler': {'untracked_closes': [], 'escalated': [],
                       'unknown_accounts': [],
                       'unclaimed': unclaimed or []},
        'hedge_times_ms': [], 'click_to_on_ms': [],
        'pairs': {
            'XAUUSD_|GC1226': {
                'key': 'XAUUSD_|GC1226', 'name': 'Gold basis', 'enabled': True,
                'account_a': 'acct_a', 'account_b': 'acct_b',
                'symbol_a': 'XAUUSD_', 'symbol_b': 'GC1226',
                'hedge_ratio': 1.0, 'hedge_ratio_for': 'XAUUSD_|GC1226',
                'pair_type': 'SPOT_FUTURE',
                'increment': 0.01, 'increment_derived': 0.01,
                'order_type': order_type, 'exit_type': exit_type,
                'time_in_force': 'DAY',
                'overnight': 'ALLOW', 'default_quantity': 1.0,
                # What both brokers will take, in Qty. The keypad reads
                # it; None where neither caps volume.
                'max_qty': max_qty,
                'algo': algo,
                'algo_on': algo == 'ALGO',
                'algo_mode': (algo_block or {}).get('mode') or 'DRY_RUN',
                'algo_window': show_fair_window,
                'algo_block': algo_block or {'algo': algo,
                                             'window': show_fair_window},
                'show_fair_window': show_fair_window,
                'clip_lots_a': 0.1, 'clip_lots_b': 0.1, 'spread_units': 10.0,
                'contract_a': 100.0, 'contract_b': 100.0,
                'short_spread': 59.09, 'long_spread': 59.11,
                'market': {'spread': 59.10, 'short_spread': 59.09,
                           'long_spread': 59.11, 'net_change': -0.61,
                           # The two legs' own books, as the engine
                           # always publishes them.
                           'leg_a_bid': 4607.38, 'leg_a_ask': 4607.63,
                           'leg_b_bid': 4660.40, 'leg_b_ask': 4660.80,
                           'leg_a_quote_age_sec': 0.3,
                           'leg_b_quote_age_sec': 9.0 if stale_leg else 1.5,
                           'feed_badge': 'OK (oldest leg 0.2s)',
                           'session': {'open': 59.71, 'high': 59.80,
                                       'low': 58.90, 'volume': 0.0,
                                       'ours': True}},
                'rows': rows, 'row_count': 30, 'orders': orders or [],
                'quotes': quotes or [],
                'positions': positions or [],
                'dead_orders': dead_orders or [],
                'exit': exits or {},
                'fair': fair or {},
                'working_buys': working_buys,
                'working_sells': working_sells,
                'resting_closes': resting_closes,
                'broker_pendings': (working_buys + working_sells
                                    - resting_closes)
                if broker_pendings is None else broker_pendings,
                'net_position': net_position,
                'quoting_leg_effective': quoting_leg,
                'avg_entry': None, 'open_pnl': None, 'last_print': None,
                'errors': [],
            }
        },
    }


#: Intercept the pair save so a test can read exactly what the form
#: sent — a blank that arrives as 0, or not at all, is the bug.
SPY_ON_PAIR_SAVE = """() => {
    window.__realFetch = window.__realFetch || window.fetch;
    window.__sent = null;
    window.fetch = function (url, options) {
        if (String(url).indexOf('/api/pairs/') >= 0 && options &&
            options.method === 'POST') {
          window.__sent = JSON.parse(options.body);
          return Promise.resolve(new Response(
            JSON.stringify({ok: true, notes: []}),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        }
        return window.__realFetch(url, options);
    };
}"""


def chromium_path():
    """The browser this machine actually has.

    Playwright pins a build number per release; a box whose browsers were
    installed for a different release has a perfectly good Chromium under
    another name, and pointing at it beats downloading a second copy.
    None lets Playwright find its own.
    """
    root = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/pw-browsers'))
    for candidate in sorted(root.glob('chromium-*/chrome-linux*/chrome')):
        return str(candidate)
    return None


@pytest.fixture(scope='module')
def page(server):
    url, paths = server
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=chromium_path())
        except Exception as e:                    # no browser on this box
            pytest.skip(f'chromium unavailable: {e}')
        # PINNED, because half this file measures pixels. Playwright's
        # default viewport is not a promise, and a desk that is narrower
        # on one machine than another makes `clampTo` bite on a drag
        # here and not there - which is a test that fails by geography
        # rather than by fault. Seen on Windows, where the default came
        # out smaller than on this box.
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text)
                if message.type == 'error' else None)
        page.goto(url)
        page.wait_for_selector('.ladder .grid tbody tr')

        # A FIXED, quiet session clock (a Saturday noon UTC: no centre
        # opens and no OKX funding falls within the 10-minute alert), so
        # a Market Sessions pop-up can never land on top of the toast a
        # test is reading - which it did whenever the suite ran in the
        # ten minutes before a real open.
        page.evaluate("""() => {
            window.MT5Sessions.now = () => new Date('2026-11-21T12:00:00Z');
            window.MT5Sessions.tick();
            document.getElementById('toasts').innerHTML = '';
        }""")
        page.errors = errors
        page.paths = paths
        yield page
        browser.close()


def test_the_page_loads_without_a_single_script_error(page):
    """A TDZ ReferenceError aborts the whole script block and silently
    unregisters every handler after it. Nothing in Python can see that."""
    page.wait_for_timeout(900)                    # several refresh cycles
    assert page.errors == [], '\n'.join(page.errors)


def test_the_ladder_draws_the_reference_screens_columns(page):
    # Scoped to the GRID: the footer's leg table has headers of its own
    # now, and they are not these.
    headers = page.eval_on_selector_all(
        '.ladder .grid thead th', 'nodes => nodes.map(n => n.textContent)')
    assert headers == ['Work', 'Bids', 'Price', 'Asks', 'LTQ']
    assert page.locator('.ladder .grid tbody tr').count() == 41


def test_one_rule_marks_the_market_and_it_is_at_the_mid(page):
    """Two black rules — one at the inside, one at the mid — meant
    neither read as "the market is here"."""
    line = page.locator('.ladder tr.mid-line')
    assert line.count() == 1
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'.ladder tr.mid-line td')).borderTopWidth") == '2px'
    # The inside market is still marked — by the two bands meeting, not
    # by a second line: the best-bid row carries the ordinary 1px grid
    # rule and nothing heavier.
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'.ladder tr.market-line td')).borderTopWidth") == '1px'
    assert page.locator('.ladder tr.in-bid td.bid').count() > 0
    assert page.locator('.ladder tr.in-ask td.ask').count() > 0


def rgb_parts(value):
    return [int(n) for n in re.findall(r'\d+', value)[:3]]


def is_blue(value):
    r, g, b = rgb_parts(value)
    return b > r and b > g


def is_red(value):
    r, g, b = rgb_parts(value)
    return r > g and r > b


def css_var_rgb(locator, name):
    """What a CSS variable resolves to, as the browser reports
    backgroundColor — so a test can name the variable, not a hex."""
    return locator.evaluate(
        """(n, v) => {
            const probe = document.createElement('span');
            probe.style.backgroundColor = getComputedStyle(n)
                .getPropertyValue(v).trim();
            document.body.appendChild(probe);
            const out = getComputedStyle(probe).backgroundColor;
            probe.remove();
            return out;
        }""", name)


def test_both_columns_are_painted_on_EVERY_row_not_just_at_the_market(page):
    """A TT ladder's Bids column is solid blue top to bottom and its
    Asks column solid red, always.

    Ours painted blue only from the best bid DOWN and red only from the
    best offer UP, so both edges were touch prices and the blocks grew
    and shrank with every tick. That is movement the row freeze cannot
    reach, because the rows were never what was moving.
    """
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr')
    rows = page.locator('.ladder .grid tbody tr')
    assert rows.count() >= 6, 'not enough rows to prove anything'

    # Rows the OLD rule left bare: neither in-bid nor in-ask — the ones
    # between the two touches.
    bare = page.locator(
        '.ladder .grid tbody tr:not(.in-bid):not(.in-ask)')
    assert bare.count() > 0, (
        'no row sits between the touches, so this cannot show the '
        'difference — widen the spread in the fixture')

    for index in range(min(bare.count(), 4)):
        row = bare.nth(index)
        bid = row.locator('td.bid')
        ask = row.locator('td.ask')
        assert is_blue(bid.evaluate('n => getComputedStyle(n).backgroundColor')), (
            'a row between the touches has an unpainted Bids cell')
        assert is_red(ask.evaluate('n => getComputedStyle(n).backgroundColor')), (
            'a row between the touches has an unpainted Asks cell')


def test_the_columns_do_not_change_size_when_the_market_moves(page):
    """The band edges were the touches, so they walked. Count the
    painted cells before and after moving the market: the number must
    not change, because every row is painted either way."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr')

    def painted():
        return page.evaluate("""() => {
            const rows = document.querySelectorAll(
                '.ladder .grid tbody tr');
            let blue = 0, red = 0;
            rows.forEach(tr => {
                const b = tr.querySelector('td.bid');
                const a = tr.querySelector('td.ask');
                if (b && getComputedStyle(b).backgroundColor !==
                    'rgba(0, 0, 0, 0)') { blue += 1; }
                if (a && getComputedStyle(a).backgroundColor !==
                    'rgba(0, 0, 0, 0)') { red += 1; }
            });
            return [blue, red, rows.length];
        }""")

    rows_in_bid = page.locator('.ladder .grid tbody tr.in-bid').count()
    before = painted()
    assert before[0] == before[2] and before[1] == before[2], (
        f'not every row is painted: {before[0]} blue and {before[1]} red '
        f'of {before[2]} rows')

    # Walk the market a long way, on every poll, so the touches land
    # well away from where they were.
    page.evaluate("""() => {
        if (!window.__mkReal) { window.__mkReal = window.fetch; }
        const real = window.__mkReal;
        window.fetch = function (url, options) {
            const answer = real(url, options);
            if (String(url).indexOf('/api/status') < 0) { return answer; }
            return answer.then(r => r.json()).then(body => {
                Object.keys(body.pairs || {}).forEach(k => {
                    const m = body.pairs[k].market;
                    if (!m) { return; }
                    if (m.short_spread != null) { m.short_spread += 0.40; }
                    if (m.long_spread != null) { m.long_spread += 0.40; }
                });
                return new Response(JSON.stringify(body), {
                    status: 200,
                    headers: {'Content-Type': 'application/json'}});
            });
        };
    }""")
    page.wait_for_timeout(700)
    try:
        moved = page.locator('.ladder .grid tbody tr.in-bid').count()
        assert moved != rows_in_bid, (
            'the touches did not actually move, so this proves nothing')
        after = painted()
        assert after[0] == after[2] and after[1] == after[2], (
            f'the painted region changed size when the market moved: '
            f'{after[0]} blue and {after[1]} red of {after[2]} rows')
    finally:
        page.evaluate("() => { if (window.__mkReal) "
                      "{ window.fetch = window.__mkReal; } }")
        page.wait_for_timeout(400)


def test_bid_is_blue_and_ask_is_red_on_the_rendered_page(page):
    # The ladder repaints three times a second; wait for a painted row
    # rather than racing one.
    page.wait_for_selector('.ladder tr.in-bid td.bid')
    page.wait_for_selector('.ladder tr.in-ask td.ask')
    bid = page.locator('.ladder tr.in-bid td.bid').first
    ask = page.locator('.ladder tr.in-ask td.ask').first
    # Softened for a light frame, and the shade is free to be tuned —
    # the CONVENTION is what is pinned: bid is blue, ask is red, in
    # every table on the screen. Asserting the hex meant that nudging
    # the palette broke a test about which colour means which side.
    assert is_blue(bid.evaluate('n => getComputedStyle(n).backgroundColor'))
    assert is_red(ask.evaluate('n => getComputedStyle(n).backgroundColor'))
    # And each really is drawn from the variable it is supposed to be.
    assert bid.evaluate('n => getComputedStyle(n).backgroundColor') == \
        css_var_rgb(bid, '--bid')
    assert ask.evaluate('n => getComputedStyle(n).backgroundColor') == \
        css_var_rgb(ask, '--ask')


def test_a_limit_click_places_one_order_and_asks_nothing(page):
    # PIN THE CONVENTION. Which column sends which side is a SETTING,
    # and this test is about one click being one order, not about the
    # mapping — so it says which convention it is standing in rather
    # than inheriting whatever the default happens to be today. It
    # inherited it before, and when the default moved to TT this test
    # went red for a reason that had nothing to do with what it tests.
    # The mapping itself is owned by the TT/TOUCH pair further down.
    set_convention(page, 'TT')
    before = command_count(page)
    page.locator('.ladder .grid tbody tr td.bid').nth(5).click()
    page.wait_for_timeout(250)
    assert page.locator('#modal.hidden').count() == 1     # no confirmation
    assert command_count(page) == before + 1
    command = last_command(page)
    assert command['kind'] == 'click'
    # Under TT — the shipped default, and the convention every desk
    # arrives with — clicking the BIDS column joins the bid, which is a
    # resting BUY. Getting this pair of columns the wrong way round is
    # the most expensive mistake this screen could make.
    assert command['payload']['side'] == 'BUY'


def test_the_buttons_are_coloured_by_the_side_they_send(page):
    """BUY is red and SELL is blue on the buttons, in BOTH conventions.

    This used to be tacked onto an assertion that the asks column buys,
    and read as though the button borrowed the colour of the column it
    acted on. It does not, and it must not: which column sends which
    side is a SETTING (TT or TOUCH), while a button names its side
    outright and never goes through that mapping at all. Under the
    shipped TT default the two even disagree - the bids column is blue
    and sends BUY, and the BUY button beside it is red - so a test that
    tied the button to the column was asserting something that is only
    true on a TOUCH desk.

    The colour rule that IS stable: red is where a purchase executes
    (the offer) and blue is where a sale does (the bid), so the BUY
    button is red and the SELL button is blue whatever the desk clicks
    to get there. Checked under both conventions, because 'whatever the
    convention' is the whole claim.
    """
    # TOUCH FIRST, TT LAST, and the order is not cosmetic: the page
    # fixture is module-scoped and set_convention patches every poll,
    # so whatever this loop ends on is what every later test in this
    # file inherits. It ends on TT, which is what the engine ships.
    for convention in ('TOUCH', 'TT'):
        set_convention(page, convention)
        buy = page.locator('.ladder .buy-touch')
        sell = page.locator('.ladder .sell-touch')
        # One step deeper than the bands: a button carries white text
        # and needs the contrast, while a band is data and gets out of
        # the way.
        assert is_red(
            buy.evaluate('n => getComputedStyle(n).backgroundColor')), \
            'the BUY button lost its colour under ' + convention
        assert is_blue(
            sell.evaluate('n => getComputedStyle(n).backgroundColor')), \
            'the SELL button lost its colour under ' + convention


def test_three_clicks_at_one_price_send_three_orders(page):
    before = command_count(page)
    cell = page.locator('.ladder .grid tbody tr td.ask').nth(3)
    for _ in range(3):
        cell.click()
        page.wait_for_timeout(120)
    # The server writes each command after its POST lands; on a slow
    # runner the last clicks are still in flight when the loop ends.
    assert wait_for_commands(page, before + 3) == before + 3
    levels = {json.loads(line)['payload']['level']
              for line in commands(page)[-3:]}
    assert len(levels) == 1                       # same price, three orders


def test_a_market_click_fires_on_ONE_click(page):
    """One click is one order. The arming carries the weight instead:
    the mode badge, the tinted columns, the cursor."""
    # The selector sends a command; the UI arms itself from what the
    # ENGINE says came back, never from the click.
    page.select_option('.ladder .order-type', 'MARKET')
    page.paths['publisher'].order_type = 'MARKET'
    page.wait_for_selector('.window.mode-market', timeout=WAIT)
    # Read in ONE evaluate: the ladder repaints on every publish, and a
    # node resolved in Python can be replaced before the style is read.
    # The M is on every price cell — it names the MODE, and the mode is
    # the same at the touch and ten rows away. The touch rows are told
    # apart by their box instead.
    cursors = page.evaluate(
        "() => ({touch: getComputedStyle(document.querySelector("
        "'.window.mode-market .grid tr.in-bid td.bid')).cursor,"
        " away: getComputedStyle(document.querySelector("
        "'.window.mode-market .grid tbody td.bid')).cursor,"
        " box: getComputedStyle(document.querySelector("
        "'.window.mode-market .grid tr.market-line td.bid')).boxShadow})")
    assert 'svg+xml' in cursors['touch'] and 'crosshair' in cursors['touch']
    assert 'svg+xml' in cursors['away']
    assert cursors['box'] != 'none'

    before = command_count(page)
    page.locator('.ladder .grid tbody tr td.bid').nth(6).click()
    page.wait_for_timeout(250)

    assert command_count(page) == before + 1        # no second gesture
    assert page.locator('#modal.hidden').count() == 1
    assert last_command(page)['kind'] == 'click'


def test_the_click_shows_up_before_the_engine_answers(page):
    """A trader who cannot see their click clicks again. The ghost is
    drawn immediately and is never added into the working total."""
    # Earlier tests in this file have clicked too; start from nothing in
    # flight so the count means what it says.
    page.evaluate('() => { window.MT5Trader.state.pending.length = 0; }')
    page.locator('.ladder .grid tbody tr td.ask').nth(8).click()
    # No waiting: it is on the screen in the same frame.
    page.wait_for_selector('.ladder td.work.pending', timeout=500)
    assert page.locator('.ladder td.work.pending .ghost').first.text_content() \
        == '1'


def test_a_desk_that_wants_the_extra_gesture_can_have_it(page):
    """The control: CONFIRM_MARKET_CLICKS turns the one click back into
    a confirmed one — through OUR modal, never a native dialog."""
    page.paths['publisher'].confirm = True
    page.wait_for_timeout(400)

    before = command_count(page)
    page.locator('.ladder .grid tbody tr td.bid').nth(6).click()
    page.wait_for_selector('#modal:not(.hidden)')
    assert command_count(page) == before          # nothing sent yet
    page.click('#modal-confirm')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1

    page.paths['publisher'].confirm = False
    page.paths['publisher'].order_type = 'LIMIT'
    page.wait_for_timeout(400)


def test_every_number_input_accepts_the_value_the_engine_shipped(page):
    """Two of these once rejected the engine's own defaults, so Chrome
    refused to submit and fired no event at all."""
    bad = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('input[type=number]').forEach(input => {
            if (input.value !== '' && !input.checkValidity()) {
                out.push([input.className, input.value, input.min, input.max,
                          input.step]);
            }
        });
        return out;
    }""")
    assert bad == []


def test_the_engine_banner_appears_when_the_snapshot_goes_stale(page):
    """A dead coordinator must never read as a quiet market."""
    publisher = page.paths['publisher']
    publisher.live.clear()                 # the engine stops publishing
    publisher.publish(at=0)
    page.wait_for_selector('#engine-banner:not(.hidden)', timeout=WAIT)
    assert 'Nothing on this screen is live' in \
        page.text_content('#engine-banner')

    publisher.live.set()                   # ...and comes back
    # `state='attached'`: a hidden element is never "visible", which is
    # the point of asserting on it.
    page.wait_for_selector('#engine-banner.hidden', state='attached',
                           timeout=WAIT)
    assert page.locator('#engine-banner.hidden').count() == 1


def commands(page):
    try:
        with open(page.paths['commands'], encoding='utf-8') as f:
            return [line for line in f.read().splitlines() if line.strip()]
    except OSError:
        return []


def command_count(page):
    return len(commands(page))


def wait_for_commands(page, count, timeout_ms=5000):
    """The command count once it reaches `count`, or what it is when
    `timeout_ms` runs out."""
    waited = 0
    while command_count(page) < count and waited < timeout_ms:
        page.wait_for_timeout(50)
        waited += 50
    return command_count(page)


def last_command(page):
    return json.loads(commands(page)[-1])


# -- the settings page ---------------------------------------------------

def test_the_quick_buttons_hit_the_touch_without_hunting_for_a_row(page):
    """BUY lifts the offer, SELL hits the bid — the price the market is
    actually offering for that direction, never the mid."""
    before = command_count(page)
    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    assert last_command(page)['payload']['level'] == 59.11      # long spread
    assert last_command(page)['payload']['side'] == 'BUY'

    page.click('.ladder .sell-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['level'] == 59.09      # short spread
    assert last_command(page)['payload']['side'] == 'SELL'


def test_one_key_is_one_order_too(page):
    """B, S, and the quantity presets — a hand that never leaves the
    keyboard is faster than one that hunts for a button."""
    page.click('.ladder .titlebar')                 # focus this ladder
    before = command_count(page)

    page.keyboard.press('3')                        # arm 10 spreads
    page.wait_for_timeout(120)
    assert page.input_value('.ladder .armed') == '10'
    assert page.input_value('.ladder .sell-qty') == '10'

    page.keyboard.press('b')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    command = last_command(page)
    assert command['payload']['side'] == 'BUY'
    assert command['payload']['quantity'] == 10

    page.keyboard.press('s')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['side'] == 'SELL'

    page.keyboard.press('0')                        # back to the default
    page.wait_for_timeout(120)
    # Never blank: the box says what ONE CLICK sends, which with
    # nothing armed is this ladder's own default size.
    assert page.input_value('.ladder .armed') == '1'


def test_x_pulls_this_ladders_orders_and_l_locks_the_scroll(page):
    page.click('.ladder .titlebar')
    before = command_count(page)
    page.keyboard.press('x')
    page.wait_for_timeout(250)
    assert last_command(page)['kind'] == 'cancel_where'
    assert command_count(page) == before + 1

    assert page.evaluate("() => !!window.MT5Trader.state.locked['XAUUSD_|GC1226']") \
        is False
    page.keyboard.press('l')
    page.wait_for_timeout(120)
    assert page.evaluate("() => !!window.MT5Trader.state.locked['XAUUSD_|GC1226']") \
        is True
    page.keyboard.press('l')


def test_a_key_typed_into_a_field_is_never_an_order(page):
    """The one way a shortcut becomes dangerous: typing a quantity and
    having the B in 'BUY' fire."""
    before = command_count(page)
    page.click('.ladder .sell-qty')
    page.keyboard.type('b')
    page.keyboard.press('s')
    page.wait_for_timeout(250)
    assert command_count(page) == before
    page.keyboard.press('Escape')


def test_the_shortcuts_are_on_the_screen_not_in_a_manual(page):
    page.click('#help')
    page.wait_for_selector('#help-overlay:not(.hidden)')
    text = page.text_content('#help-overlay')
    assert 'buy the spread at the offer' in text
    assert 'no confirmation, by design' in text
    page.keyboard.press('Escape')
    page.wait_for_selector('#help-overlay.hidden', state='attached')


def test_flatten_asks_once_and_then_goes(page):
    """Flattening is irreversible and it is the button pressed in a
    hurry — so it asks, but with one key and only once."""
    # PUBLISHED, not poked in. The publisher republishes every 200ms
    # and a snapshot edited in the page is gone by the next one, so a
    # poked net_position made this test pass or fail on which side of
    # that it landed — and it landed on the wrong side often enough to
    # be noticed.
    publisher = page.paths['publisher']
    publisher.net_position = -2.0
    publisher.publish()
    page.wait_for_function(
        "() => window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
        ".net_position === -2", timeout=WAIT)
    before = command_count(page)
    page.click('.ladder .flatten')
    page.wait_for_selector('#modal:not(.hidden)')
    assert 'cannot be undone' in page.text_content('#modal-body')
    page.click('#modal-confirm')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    assert last_command(page)['kind'] == 'flatten_pair'
    publisher.net_position = 0.0
    publisher.publish()


def test_flatten_says_so_when_there_is_nothing_to_flatten(page):
    publisher = page.paths['publisher']
    publisher.net_position = 0.0
    publisher.publish()
    page.wait_for_function(
        "() => window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
        ".net_position === 0", timeout=WAIT)
    before = command_count(page)
    page.click('.ladder .flatten')
    page.wait_for_selector('.toast')
    assert 'already flat' in page.text_content('.toast')
    assert command_count(page) == before
    page.click('.toast')


def test_the_monitor_shows_margin_per_account_and_names_the_weakest(page):
    """With two brokers there is no combined margin: the pair can only
    be carried by the weaker of the two, and a total would read
    comfortable while one side sits at its stop-out."""
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'accounts';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .pane table', timeout=WAIT)

    text = page.text_content('.monitor .pane')
    assert 'The weakest account governs' in text
    assert 'acct_b' in text and '125.0%' in text
    assert 'it is this account that stops the pair' in text
    # The tight account is marked, the comfortable one is not.
    assert page.locator('.monitor tr.mismatch').count() >= 1
    # Equity, balance and credit side by side: a demo funded with CREDIT
    # shows a balance of 0.00 against real equity.
    assert '$5,000.00' in text and '$0.00' in text
    assert 'fund a demo with CREDIT' in text
    # And our own exposure on each account, in the units it was sized in.
    assert 'Our lots' in text and 'Our units' in text

    page.evaluate("""() => {
        window.MT5Trader.state.monitorTab = 'positions';
        window.MT5Trader.render();
    }""")










def test_the_exit_costs_belong_to_ONE_LADDER_and_the_override_CLEARS(page):
    """Commission, the allowance, the nights and the target are not one
    trade repeated: a gold basis and an oil differential are charged
    differently and held for different lengths of time, so a single set
    of numbers is wrong for at least one of them. They live behind the
    cog on each ladder, and the Exchanges page says where.

    And 0 is a real number: a form that reads a typed zero as "unset"
    charges a commission nobody agreed to.
    """
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ladder-settings .ls-tp', timeout=WAIT)

    for field in ('.ls-comm-a', '.ls-comm-b', '.ls-slip',
                  '.ls-nights', '.ls-tp', '.ls-carry-rate',
                  '.ls-overnight'):
        assert page.locator(
            '.ladder .ladder-settings ' + field).count() == 1, field

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.fill('.ladder .ladder-settings .ls-slip', '2.5')
    page.fill('.ladder .ladder-settings .ls-comm-a', '0')
    page.click('.ladder .ladder-settings .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)

    sent = page.evaluate('() => window.__sent')
    assert sent['slippage_allowance'] == 2.5
    assert sent['commission_per_lot_a'] == 0             # ...and 0 is real
    page.evaluate('() => { window.fetch = window.__realFetch; }')
    # Leave the desk as it was found: this test's own "applied" toast is
    # a message a LATER test reads as its own.
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.wait_for_selector('.ladder .grid tbody tr')


def test_every_setting_shows_the_value_ACTUALLY_IN_FORCE(page):
    """A form that cannot tell you what the ladder is doing.

    Mode, Time in force and Overnight came up BLANK whenever the config
    had no saved value — while the title bar said LIMIT · DAY and the
    engine ran exactly that. And the cost fields said "default", which
    is the NAME of a number you cannot see. Both are now the value in
    force: the running one for what the ladder is doing, the system
    default for what it is inheriting, marked as inherited rather than
    hidden behind a word.
    """
    open_ladder(page)
    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            const get = !(options && options.method === 'POST');
            if (String(url).indexOf('/api/pairs') >= 0 && get) {
              // A config with almost nothing in it, which is the case
              // that broke: the engine is running defaults and the
              // file has no opinion at all.
              return Promise.resolve(new Response(JSON.stringify({
                ok: true, pairs: {'XAUUSD_|GC1226': {
                  pair_type: 'SPOT_FUTURE', commission_per_lot_b: 3.5}}}),
                {status: 200,
                 headers: {'Content-Type': 'application/json'}}));
            }
            if (String(url).indexOf('/api/settings') >= 0 && get) {
              return Promise.resolve(new Response(JSON.stringify({
                settings: {COMMISSION_PER_LOT_A: 2.0,
                           COMMISSION_PER_LOT_B: 2.0,
                           TP_TARGET_PCT_OF_MARGIN: 2.0,
                           SLIPPAGE_ALLOWANCE: 0.0,
                           BREAK_EVEN_NIGHTS: 0.0}}),
                {status: 200,
                 headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
    }""")

    try:
        page.click('.ladder .ladder-cog')
        page.wait_for_function(
            # The pair's OWN cost, which only the stubbed fetch carries:
            # the form has been filled from it. "Not blank" was met by a
            # value left in the box from before the fetch landed.
            "() => document.querySelector('.ladder .ls-comm-b').value "
            "=== '3.5'", timeout=WAIT)

        # What the ladder is RUNNING, from the snapshot — never blank.
        assert page.input_value('.ladder .ls-order-type') == 'LIMIT'
        assert page.input_value('.ladder .ls-tif') == 'DAY'
        assert page.input_value('.ladder .ls-overnight') == 'ALLOW'
        assert page.input_value('.ladder .ls-increment') == '0.01'
        # Rows read the ROW COUNT, not the snapshot's `rows` — which is
        # the ladder's forty price rows, and left this field blank on
        # every ladder.
        assert page.input_value('.ladder .ls-rows') == '30'

        # OKX: a commission this pair has never been given is priced
        # from the account's VIP fees - the box stays EMPTY and says so,
        # because copying the desk default in would freeze it on Apply.
        assert page.input_value('.ladder .ls-comm-a') == ''
        assert page.get_attribute('.ladder .ls-comm-a',
                                  'placeholder').startswith('OKX')

        # ...and one it HAS been given shows that.
        assert page.input_value('.ladder .ls-comm-b') == '3.5'

        # Applying sends the RUNNING value for the live fields — never a
        # null, which raises on the engine — and writes every cost to
        # this pair, so nothing is left implicit.
        page.evaluate(SPY_ON_PAIR_SAVE)
        page.click('.ladder .ls-save')
        page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
        sent = page.evaluate('() => window.__sent')

        assert sent['order_type'] == 'LIMIT'
        assert sent['commission_per_lot_a'] is None      # still VIP-priced
        assert sent['commission_per_lot_b'] == 3.5       # its own
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch; }')
        page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.wait_for_selector('.ladder .grid tbody tr')


def test_a_cost_typed_on_one_ladder_is_saved_to_THAT_pair(page):
    """Every ladder carries its own numbers. There is no defaults page
    to fall back to, so what is typed here is what this pair uses."""
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-tp', timeout=WAIT)

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.fill('.ladder .ls-tp', '4.5')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
    assert page.evaluate('() => window.__sent')['tp_target_pct_of_margin'] \
        == 4.5

    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-tp', timeout=WAIT)
    page.evaluate("() => { window.__sent = null; }")
    page.fill('.ladder .ls-tp', '0')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
    # 0 is a real target: break-even alone, and no profit on top.
    assert page.evaluate('() => window.__sent')['tp_target_pct_of_margin'] == 0

    page.evaluate('() => { window.fetch = window.__realFetch; }')
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")


def test_the_rail_carries_no_form_the_market_can_outrun(page):
    """The rail is read top to bottom while the market moves. The
    overnight rule is exit logic, not something pressed at the touch,
    so it is in this ladder's settings — and the three cancels are one
    row, not three."""
    open_ladder(page)

    assert page.locator('.ladder .rail .overnight').count() == 0
    assert page.locator('.ladder .rail .cxl-row .cxl').count() == 3

    # ...and the rail fits without scrolling at the default size.
    fits = page.evaluate("""() => {
        const rail = document.querySelector('.window.ladder .rail');
        return rail.scrollHeight <= rail.clientHeight + 1;
    }""")
    assert fits


def test_the_exchanges_page_says_where_the_per_ladder_settings_are(page):
    """A generic Exits page would be four numbers that are right for
    whichever pair the operator had in mind when they typed them."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .trading-fields', timeout=WAIT)

    assert page.locator('.window.settings .s-comm-a').count() == 0
    assert 'per LADDER' in page.text_content('.window.settings .where-exits')
    page.click('.window.settings .close')
    page.wait_for_selector('.ladder .grid tbody tr')




def test_the_operator_is_told_when_the_system_is_connected(page):
    """Said once, when it becomes true — a banner that never changes is
    a banner nobody reads."""
    page.click('#open-settings')
    page.wait_for_selector('.conn', timeout=WAIT)
    # Nothing is really connected in this fixture, and it says so
    # plainly rather than showing a green light.
    assert 'NOT READY' in page.text_content('.conn')
    # A toast an earlier test left up (an "applied to ..." lasts four
    # seconds) is not the one this test is about.
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")

    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/api/connection') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true, connected: true, blockers: [],
                    accounts: [], feeds: [],
                    broker_clock: {broker_time: '19:55:02', offset_sec: 10800,
                                   cutoff: '16:55',
                                   note: "broker time, +3.0h from this "
                                       + "machine — the 16:55 cutoff is on "
                                       + "the broker's clock"},
                    summary: 'Connected — both accounts logged in, Algo '
                        + 'Trading on, and prices arriving. You can trade.'
                }), {status: 200,
                     headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
        window.MT5Settings.refresh();
    }""")
    page.wait_for_selector('.conn.up', timeout=6000)

    assert 'CONNECTED' in page.text_content('.conn.up')
    assert 'You can trade' in page.text_content('.conn.up')
    # And which clock the session cutoff is on.
    assert 'Broker time 19:55:02' in page.text_content('.conn.up')
    assert "the 16:55 cutoff is on the broker's clock" in \
        page.text_content('.conn.up')
    # It is also said once, out loud.
    page.wait_for_selector('.toast.ok:has-text("You can trade")',
                           timeout=WAIT)
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_the_pair_form_offers_the_number_it_derived_as_a_click(page):
    """A warning nobody can act on is not a fix — but nothing is
    corrected silently either. The derived value is offered as a
    button."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    page.click('.window.settings .new-pair')
    page.wait_for_selector('.pair-form')

    page.fill('.p-key', 'XAUUSD_|GC1226')
    page.fill('.p-beta', '10')            # a beta from another instrument
    page.evaluate("""() => {
        // Stand in for the leg runners: this page talks to them
        // directly, and there are none in a browser test.
        window.__realFetch = window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/derive') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true, suggested_beta: 1.0,
                    beta_reason: 'SPOT_FUTURE: the two legs are the same '
                        + 'underlying, so the spread IS the basis and beta is 1',
                    increment: 0.01,
                    increment_derivation: 'max(tick B 0.01, beta 1 x tick A 0.01)',
                    clip_lots_a: 0.1, clip_lots_b: 0.1,
                    clip_derivation: '1 spread = 0.1 A / 0.1 B',
                    spread_units: 10, min_notional_usd: 42921,
                    spread_now: 59.1, widths: {a: 0.2, b: 0.4},
                    quoting_leg_suggestion: 'b',
                    quoting_note: 'the wider bid-ask is the spread you earn, '
                        + 'and usually the less liquid leg',
                    specs: {a: {contract_size: 100}, b: {contract_size: 100}},
                    stamped_for: 'XAUUSD_|GC1226'
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
    }""")
    page.click('.derive-pair')
    page.wait_for_selector('.use-beta')

    # The wrong beta is still in the field — nothing was changed for us.
    assert page.input_value('.p-beta') == '10'
    assert 'the spread IS the basis' in page.text_content('.pair-form')
    page.click('.use-beta')
    page.wait_for_timeout(150)
    assert page.input_value('.p-beta') == '1'

    # And the derivation is on the screen beside the number.
    assert 'max(tick B 0.01' in page.text_content('.pair-form')
    assert '$42,921' in page.text_content('.derived') or \
        '42921' in page.text_content('.derived')
    page.evaluate('() => { window.fetch = window.__realFetch; }')




# -- moving the windows ---------------------------------------------------

def drag(page, selector, dx, dy, steps=8):
    """Drag one window by its title bar, the way a hand does it.

    The drop runs `writeLayout()` and a full `render()`, and a render
    can re-place the window. Reading the box the instant the button
    comes up therefore reads a position that is about to change - never
    on this box, sometimes on a slower one. So the move SETTLES before
    this returns: the same pixels twice in a row, or the wait times out
    and says so.
    """
    settle(page, selector)
    start = grab_point(page, selector)
    # The whole desk at the press, and just after it: on CI the ladder
    # has been seen to be at x 141 when measured and at x 4 by the
    # first step, so the jump is somewhere around here.
    desk = """(sel) => {
        const node = document.querySelector(sel);
        const desk = document.getElementById('desktop');
        return {node: Math.round(node.getBoundingClientRect().left),
                scroll: desk.scrollLeft, active: window.MT5Trader.state.active,
                windows: Array.from(desk.querySelectorAll('.window')).map(
                    w => w.className.replace('window ', '') + '@' +
                         Math.round(w.getBoundingClientRect().left) + '/' +
                         Math.round(w.getBoundingClientRect().width))};
    }"""
    page.trace = [{'pre': page.evaluate(desk, selector), 'grab': start}]
    page.mouse.move(*start)
    page.mouse.down()
    page.trace.append({'down': page.evaluate(desk, selector)})
    # One step at a time, and what the window and the desk did at each,
    # so a drag that stops short says WHERE and WHY in the failure.
    for i in range(1, steps + 1):
        page.mouse.move(start[0] + dx * i / steps, start[1] + dy * i / steps)
        page.trace.append(page.evaluate(
            """(sel) => {
                const node = document.querySelector(sel);
                const desk = document.getElementById('desktop');
                return {x: Math.round(node.getBoundingClientRect().left),
                        left: node.style.left,
                        dragging: node.classList.contains('dragging'),
                        scroll: desk.scrollLeft, width: desk.clientWidth,
                        windows: desk.querySelectorAll('.window').length};
            }""", selector))
    assert started(page, selector), (
        'the press missed the title bar, so nothing was dragged')
    page.mouse.up()
    settle(page, selector)


def press(page, selector, x, y):
    """Put the pointer down and CHECK the window took it.

    `render()` re-lays every window three times a second and strips the
    inline geometry while it does — so a coordinate measured a moment
    ago can be pointing at empty desk by the time the button goes down.
    The press then hits nothing, the interaction never starts, and the
    assertion downstream reports a drag that moved 0px as a drag bug.

    Both `startDrag`'s resize branch and `startResize` mark the window
    `.dragging` on pointerdown, so whether the press landed is a
    question with an answer. Ask it.
    """
    page.mouse.move(x, y)
    page.mouse.down()


def started(page, selector):
    return page.locator(selector + '.dragging').count() == 1


def grab_point(page, selector):
    """A spot on the title bar that is NOT a control.

    `startDrag` bails out on `button, select, input, a` so the widgets
    in the bar keep working — which means grabbing its CENTRE only
    drags while the centre happens to be clear. The bar carries a
    route, a mode badge and the window buttons, and how much room they
    leave depends on the window's width, which is whatever the last
    resize test left in this browser's saved layout.

    So the drag was silently not starting at all: the window never
    floated, the assertion measured an unrelated re-tidy, and it
    depended on the order the tests ran in. Pick a point that is clear,
    and say so if none is.
    """
    at = page.evaluate(
        """(sel) => {
            const bar = document.querySelector(sel + ' .titlebar');
            const box = bar.getBoundingClientRect();
            const y = box.y + box.height / 2;
            for (let x = box.x + 6; x < box.x + box.width - 4; x += 6) {
                const node = document.elementFromPoint(x, y);
                if (!node || !bar.contains(node)) { continue; }
                if (node.closest('button, select, input, a')) { continue; }
                return {x: x, y: y};
            }
            return null;
        }""", selector)
    assert at is not None, (
        f'every point on {selector} title bar is a control, so nothing '
        f'could start a drag')
    return (at['x'], at['y'])


def settle(page, selector):
    """Wait until this element has stopped moving."""
    page.wait_for_function(
        """(sel) => {
            const node = document.querySelector(sel);
            // NOTHING TO SETTLE IS SETTLED. Several tests tidy a desk
            // with no ladder on it, and waiting for an element that was
            // never there to stop moving is a ten-second timeout and a
            // failure about nothing.
            if (!node) { return true; }
            const now = node.getBoundingClientRect();
            // Keyed by SELECTOR: two settles in a row would otherwise
            // compare one element against the other's last position and
            // call a desk still that never was.
            window.__settleWas = window.__settleWas || {};
            const was = window.__settleWas[sel];
            window.__settleWas[sel] = {x: now.left, y: now.top,
                                       w: now.width, h: now.height};
            return !!was && Math.abs(was.x - now.left) < 0.5 &&
                   Math.abs(was.y - now.top) < 0.5 &&
                   Math.abs(was.w - now.width) < 0.5 &&
                   Math.abs(was.h - now.height) < 0.5;
        }""", arg=selector, timeout=WAIT)
    page.evaluate("(sel) => { if (window.__settleWas) "
                  "{ delete window.__settleWas[sel]; } }", selector)


#: HOLD THE SNAPSHOT STILL.
#:
#: Several tests inject a snapshot and render it. The page also polls
#: `/api/status` every refresh, and a poll that lands between the
#: injection and the assertion REPLACES the whole snapshot - so the
#: injected row simply vanishes and the wait times out. It never
#: happens on a fast box because the assertion wins the race; it
#: happens on a loaded one, which is why this looked like a Windows
#: problem.
#:
#: The stub answers every poll with whatever is in `state.snapshot`
#: RIGHT NOW, so the page goes on polling and rendering exactly as it
#: does in life, and can never overwrite what the test put there.
HOLD_THE_SNAPSHOT = """() => {
    window.__realFetch = window.__realFetch || window.fetch;
    window.__held = 0;
    window.fetch = function (url, options) {
        if (String(url).indexOf('/api/status') >= 0) {
            window.__held += 1;
            return Promise.resolve(new Response(
              JSON.stringify(window.MT5Trader.state.snapshot || {}),
              {status: 200, headers: {'Content-Type': 'application/json'}}));
        }
        return window.__realFetch(url, options);
    };
}"""

RELEASE_THE_SNAPSHOT = ("() => { if (window.__realFetch) "
                        "{ window.fetch = window.__realFetch; } }")


def tidy(page):
    """Tidy the desk, and WAIT for it to be tidy.

    Tidy re-lays the row, and on a slow runner the windows can still be
    moving when the click returns; a position read then is where a
    window WAS.
    """
    # Shift: the plain row, the known baseline these tests start from.
    # A plain click TILES the desk (test_tidy_tiles_the_desk...).
    page.click('#tidy', modifiers=['Shift'])
    # A fresh baseline: two readings from THIS layout, not one left
    # over from a previous test's.
    page.evaluate("() => { window.__settleWas = {}; }")
    settle(page, '.window.ladder')


def open_ladder(page):
    """The settings tests leave the desk showing their own panel; these
    are about a window, so put one on it.

    And put it back to a KNOWN state while we are here. These tests run
    in a random order against one shared page, so whatever the last one
    left behind is what this one starts from — a settings pane left
    open sits over the ladder and swallows the click a click test is
    about, which is a real failure of the test and not of the code.
    """
    page.wait_for_function(
        "() => Object.keys((window.MT5Trader.state.snapshot || {}).pairs "
        "|| {}).length > 0", timeout=WAIT)
    page.evaluate("""() => {
        const state = window.MT5Trader.state;
        const key = Object.keys(state.snapshot.pairs || {})[0];
        state.open = [window.MT5Trader.panelId('ladder', key)];
        // A window CLOSED by a previous test stays closed — that is
        // the product's rule, and it is remembered in this browser. So
        // the desk is cleared here rather than each test inheriting
        // whatever the last one decided.
        state.closed = {};
        window.MT5Trader.render();
        // A pane over the ladder, a stubbed fetch and a stale toast are
        // all things one test leaves for the next one.
        document.querySelectorAll('.ladder-settings').forEach(
          function (pane) { pane.hidden = true; });
        if (window.__realFetch) { window.fetch = window.__realFetch; }
        const toasts = document.getElementById('toasts');
        if (toasts) { toasts.innerHTML = ''; }
        // ...AND THE DESK'S OWN GEOMETRY. `scroll_desk` appends a
        // spacer and scrolls sideways; if the test that did it fails
        // before its cleanup, every later test in this shared page
        // inherits a narrower visible desk and a non-zero scrollLeft.
        // Both feed straight into `clampTo`, so a drag that fits on a
        // clean desk silently stops short on a dirty one - which is
        // precisely the window-drag flake, and it depends on the order
        // the tests happened to run in.
        const pad = document.getElementById('__spacer');
        if (pad) { pad.remove(); }
        const desk = document.getElementById('desktop');
        if (desk) { desk.scrollLeft = 0; desk.scrollTop = 0; }
    }""")
    page.wait_for_selector('.window.ladder .titlebar', timeout=WAIT)


def room_to_drag(page, selector):
    """How far this window can travel before the clamp stops it.

    `clampTo` keeps a window inside the VISIBLE desk. A test that just
    asks for 220px is asserting there are 220px to give, which depends
    on the viewport, on what else is open and on whatever the last test
    left behind - and when there are not, the window stops short and
    the test reports a drag bug that is not there.
    """
    return page.evaluate(
        """(sel) => {
            const node = document.querySelector(sel);
            const desk = document.getElementById('desktop');
            const box = node.getBoundingClientRect();
            const deskBox = desk.getBoundingClientRect();
            return {
              right: (deskBox.left + desk.clientWidth - 8) - box.left,
              down: (deskBox.top + desk.clientHeight - 24) - box.top,
            };
        }""", selector)


def test_a_window_goes_where_it_is_dragged_and_is_still_there_after_a_reload(
        page):
    """The layout is a preference about this screen, so it lives in the
    browser — but it has to SURVIVE, or every reload rearranges the desk
    under the trader."""
    open_ladder(page)
    tidy(page)
    room = room_to_drag(page, '.window.ladder')
    # Asserted, not assumed. If the desk really is too small the test
    # says THAT, instead of reporting a drag that stopped short as a
    # drag that went the wrong way.
    assert room['right'] > 240 and room['down'] > 150, (
        f'the desk has no room to drag into: {room}')
    before = page.locator('.window.ladder').first.bounding_box()

    drag(page, '.window.ladder', 220, 130)

    after = page.locator('.window.ladder').first.bounding_box()
    assert after['x'] - before['x'] == pytest.approx(220, abs=6), (
        f'room {room}, before {before}, after {after}, '
        f'steps {getattr(page, "trace", None)}')
    assert after['y'] - before['y'] == pytest.approx(130, abs=6)
    assert page.locator('.window.ladder.floating').count() == 1

    page.reload()
    page.wait_for_selector('.ladder .grid tbody tr')
    open_ladder(page)
    restored = page.locator('.window.ladder').first.bounding_box()
    assert restored['x'] == pytest.approx(after['x'], abs=2)
    assert restored['y'] == pytest.approx(after['y'], abs=2)
    tidy(page)


def scroll_desk(page, spacer_px=400, to=100):
    """Put the desk into the state the bug needed: SCROLLED SIDEWAYS.

    A spacer before the windows makes `#desktop` (overflow-x: auto)
    scrollable, and scrolling it moves the content origin away from the
    visible edge. Every drag test that ran on an unscrolled desk passed
    with the bug present, because the two origins coincide at
    scrollLeft 0.
    """
    page.evaluate("""([w, to]) => {
        const desk = document.getElementById('desktop');
        let pad = document.getElementById('__spacer');
        if (!pad) {
            pad = document.createElement('div');
            pad.id = '__spacer';
        }
        // AFTER the windows, so a modest scroll leaves the ladder's
        // title bar on screen to be grabbed, and wider than the desk
        // so there is something to scroll at all.
        desk.appendChild(pad);
        pad.style.cssText = 'flex:0 0 auto;height:1px;width:'
            + (desk.clientWidth + w) + 'px';
        desk.scrollLeft = to;
    }""", [spacer_px, to])
    page.wait_for_timeout(60)
    return page.evaluate("document.getElementById('desktop').scrollLeft")


def unscroll_desk(page):
    page.evaluate("""() => {
        const pad = document.getElementById('__spacer');
        if (pad) { pad.remove(); }
        document.getElementById('desktop').scrollLeft = 0;
    }""")
    page.wait_for_timeout(60)


def test_a_window_follows_the_cursor_on_a_desk_scrolled_sideways(page):
    """The window is positioned against the desktop's CONTENT origin,
    and the drag used to measure from its VISIBLE edge. Those differ by
    exactly scrollLeft, so on a scrolled desk the window jumped away
    from the cursor — and the clamp, computed from the visible width,
    then flung it back the other way."""
    open_ladder(page)
    tidy(page)
    scrolled = scroll_desk(page)
    assert scrolled > 0, 'the desk did not scroll, so this proves nothing'
    try:
        before = page.locator('.window.ladder').first.bounding_box()
        drag(page, '.window.ladder', -150, 60)
        after = page.locator('.window.ladder').first.bounding_box()
        # On the SCREEN, where the hand is: dragged left 150, it moves
        # left 150. Not right, and not by the scroll distance.
        assert after['x'] - before['x'] == pytest.approx(-150, abs=8), (
            f"dragged 150px left, moved {after['x'] - before['x']:+.0f}px")
        assert after['y'] - before['y'] == pytest.approx(60, abs=8)
    finally:
        unscroll_desk(page)
        tidy(page)


def test_a_drag_on_a_scrolled_desk_moves_by_exactly_the_drag_distance(page):
    """The other direction, and to the pixel.

    The error was the scroll distance, so a loose assertion here passes
    with the bug present. It has to be exact: drag 240, move 240.
    """
    open_ladder(page)
    tidy(page)
    scrolled = scroll_desk(page)
    assert scrolled > 0, 'the desk did not scroll, so this proves nothing'
    try:
        before = page.locator('.window.ladder').first.bounding_box()
        drag(page, '.window.ladder', 240, -40)
        after = page.locator('.window.ladder').first.bounding_box()
        assert after['x'] - before['x'] == pytest.approx(240, abs=8), (
            f"dragged 240px right, moved {after['x'] - before['x']:+.0f}px "
            f'on a desk scrolled {scrolled}px')
    finally:
        unscroll_desk(page)
        tidy(page)


def test_ticking_lock_tells_the_ENGINE_and_not_only_the_browser(page):
    """Lock has to reach the server, or the two things that actually
    move a price off its row — the anchor and the row window — carry on
    following the market under a ladder the trader believes is still."""
    open_ladder(page)
    sent = []
    page.expose_binding('__sawSend', lambda source, body: sent.append(body))
    page.evaluate("""() => {
        if (!window.__sendReal) { window.__sendReal = window.fetch; }
        const real = window.__sendReal;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/api/command') >= 0 && options) {
                try { window.__sawSend(String(options.body)); } catch (e) {}
            }
            return real(url, options);
        };
    }""")
    box = page.locator('.ladder .lock-scroll').first
    was = box.is_checked()
    box.set_checked(not was)
    page.wait_for_timeout(300)
    try:
        assert any('lock_ladder' in body for body in sent), (
            'ticking Lock sent no lock_ladder command: the engine never '
            f'heard about it. Commands seen: {sent}')
    finally:
        box.set_checked(was)
        page.wait_for_timeout(200)
        page.evaluate("() => { if (window.__sendReal) "
                      "{ window.fetch = window.__sendReal; } }")




def test_a_success_toast_still_clears_sooner_than_an_error(page):
    """The control. An error must OUTLAST a success — otherwise the fix
    is just "everything vanishes fast", which is the opposite problem.
    """
    source = page.evaluate("""async () => {
        const r = await fetch('/static/app.js');
        return await r.text();
    }""")
    assert "kind === 'ok' ? 4000 : 10000" in source, (
        'the two durations are no longer the success-then-error pair '
        'this test was written against')


def test_a_window_can_never_be_dropped_where_it_cannot_be_got_back(page):
    """A window dragged off the edge with no title bar left on screen is
    a window that cannot be moved, closed or focused again — and the
    only way back would be clearing browser storage."""
    open_ladder(page)
    tidy(page)

    drag(page, '.window.ladder', -4000, -4000)

    bar = page.locator('.window.ladder .titlebar').first.bounding_box()
    desktop = page.locator('#desktop').bounding_box()
    assert bar['y'] >= desktop['y'] - 1
    assert bar['x'] + bar['width'] > desktop['x'] + 40   # still grabbable

    drag(page, '.window.ladder', 4000, 4000)
    bar = page.locator('.window.ladder .titlebar').first.bounding_box()
    assert bar['x'] < desktop['x'] + desktop['width']
    assert bar['y'] < desktop['y'] + desktop['height']
    tidy(page)


def test_dragging_a_title_bar_is_never_an_order(page):
    """The drag passes right over the ladder, which is where a click
    places an order. It must not be one."""
    open_ladder(page)
    tidy(page)
    before = command_count(page)

    drag(page, '.window.ladder', 60, 220)

    page.wait_for_timeout(300)
    assert command_count(page) == before
    tidy(page)


def test_a_button_in_the_title_bar_still_works_while_windows_move(page):
    """The close button lives IN the title bar. A drag handler that
    swallowed its click would make a window impossible to close."""
    tidy(page)
    page.evaluate("""() => {
        window.MT5Trader.state.open = ['monitor:'];
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.window.monitor', timeout=WAIT)

    page.click('.window.monitor .titlebar .close')

    page.wait_for_selector('.window.monitor', state='detached', timeout=WAIT)


def test_tidy_puts_every_window_back_in_the_row(page):
    """The way out of a mess, and the one instruction that fixes any
    arrangement a non-technical operator can get into."""
    open_ladder(page)
    drag(page, '.window.ladder', 180, 90)
    assert page.locator('.window.floating').count() >= 1

    tidy(page)

    assert page.locator('.window.floating').count() == 0
    assert page.evaluate(
        "() => window.localStorage.getItem('okxtrader.windows.v1')") in (
            None, '{}')


def test_tidy_keeps_the_fair_window_in_the_row_across_renders(page):
    """The fair window floats to its own corner by default. Tidy puts it
    in the row — and it has to STAY there: it used to float off again on
    the next render, shifting the row under the pointer, so a ladder
    grabbed just after Tidy jumped sideways by the fair window's width."""
    open_ladder(page)
    # A FRESH fair window: closed first, so no earlier test's Tidy is on it.
    page.evaluate(
        "() => window.MT5Trader.showFairWindow('XAUUSD_|GC1226', false)")
    page.wait_for_selector('.window.fairwin', state='detached',
                           timeout=WAIT)
    page.evaluate("""() => {
        window.localStorage.removeItem('okxtrader.windows.v1');
        window.MT5Trader.showFairWindow('XAUUSD_|GC1226', true);
    }""")
    page.wait_for_selector('.window.fairwin', timeout=WAIT)
    try:
        # The control: untidied, it floats by default.
        page.evaluate("() => window.MT5Trader.render()")
        assert page.locator('.window.fairwin.floating').count() == 1
        tidy(page)
        for _ in range(3):
            page.evaluate("() => window.MT5Trader.render()")
        assert page.locator('.window.floating').count() == 0
        ladder_x = page.locator('.window.ladder').first.bounding_box()['x']
        page.evaluate("() => window.MT5Trader.render()")
        assert page.locator('.window.ladder').first.bounding_box()['x'] == \
            pytest.approx(ladder_x, abs=1)
    finally:
        page.evaluate(
            "() => window.MT5Trader.showFairWindow('XAUUSD_|GC1226', false)")


def test_a_floating_ladder_is_really_lifted_out_of_the_row(page):
    """`.window.ladder { position: relative }` used to beat
    `.window.floating` on source order: a dragged ladder stayed in the
    row, its `left` counted from where the row put it, and with a window
    in front of it, it jumped by that window's width when grabbed."""
    open_ladder(page)
    tidy(page)
    page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        node.classList.add('floating');
    }""")
    position = page.evaluate(
        "() => getComputedStyle(document.querySelector('.window.ladder'))"
        ".position")
    page.evaluate("() => window.MT5Trader.tidyWindows()")
    assert position == 'absolute'


def seed_config(page, pairs=True):
    """A saved account and pair, as a configured install has."""
    # OKX accounts on a closed local port: configured, never reached.
    local = {'exchange': 'okx', 'demo': True, 'base_url': 'http://127.0.0.1:9'}
    config = {'accounts': {'acct_a': dict(local), 'acct_b': dict(local)},
              'pairs': {}}
    if pairs:
        config['pairs'] = {'XAUUSD_|GC1226': {
            'name': 'Gold basis', 'enabled': True, 'hedge_ratio': 1.0,
            'hedge_ratio_for': 'XAUUSD_|GC1226',
            'leg_a': {'account': 'acct_a', 'symbol': 'XAUUSD_'},
            'leg_b': {'account': 'acct_b', 'symbol': 'GC1226'}}}
    with open(page.paths['config'], 'w', encoding='utf-8') as f:
        json.dump(config, f)
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .pairs', timeout=WAIT)
    page.evaluate('() => window.MT5Settings.refresh()')
    page.wait_for_timeout(300)


def test_a_window_is_freely_expandable_from_its_corner(page):
    """A ladder is a price column: how many rows fit on screen is the
    difference between seeing the market and scrolling for it."""
    open_ladder(page)
    tidy(page)
    # SETTLED BEFORE MEASURED. A refresh re-lays the window and strips
    # its inline geometry, so a grip coordinate read a moment too early
    # points at empty desk when the button goes down - the resize never
    # starts and the window is reported as having grown by 0px.
    settle(page, '.window.ladder')
    before = page.locator('.window.ladder').first.bounding_box()
    # `size()` clamps a window to the desk, so "200 wider" is only 200
    # wider when there are 200 to give. Asserted rather than assumed,
    # so a desk too narrow to prove anything says THAT.
    desk = page.evaluate(
        "() => document.getElementById('desktop').clientWidth")
    assert before['x'] + before['width'] + 200 < desk, (
        f'the desk has no room to grow into: {before}, desk {desk}')
    grip = page.locator('.window.ladder .grip').first.bounding_box()

    # Wider, and SHORTER: a window is never taller than the desktop it
    # is on — a ladder whose bottom rows are off screen is a ladder the
    # trader cannot click.
    press(page, '.window.ladder', grip['x'] + 6, grip['y'] + 6)
    assert started(page, '.window.ladder'), (
        'the press missed the grip, so this proves nothing about resizing')
    page.mouse.move(grip['x'] + 206, grip['y'] - 144, steps=8)
    page.mouse.up()
    settle(page, '.window.ladder')

    after = page.locator('.window.ladder').first.bounding_box()
    assert after['width'] - before['width'] == pytest.approx(200, abs=8)
    # SHORTER — by what was dragged, or as far as the floor allows.
    #
    # It used to be pinned at 150, the exact drag. That is a number
    # about the desk's height on the day it was written: the window's
    # floor is MEASURED from its own rail, so anything that changes the
    # desk or the rail moves how much room there is above it, and the
    # test failed for a reason it was never about. What it is about is
    # that the grip shrinks as well as grows, and that it stops at the
    # floor rather than going through it — which is the same floor
    # `test_the_ladder_cannot_be_made_shorter_than_its_own_rail` pins.
    shrunk = before['height'] - after['height']
    floor = page.evaluate(
        "() => parseInt(getComputedStyle("
        "document.querySelector('.window.ladder')).minHeight, 10) || 0")
    assert shrunk > 0, 'the grip did not shrink the window at all'
    assert shrunk == pytest.approx(150, abs=10) or \
        after['height'] == pytest.approx(floor, abs=2), \
        f'shrank {shrunk} and landed at {after["height"]}, floor {floor}'

    page.reload()
    page.wait_for_selector('.ladder .grid tbody tr')
    open_ladder(page)
    restored = page.locator('.window.ladder').first.bounding_box()
    assert restored['width'] == pytest.approx(after['width'], abs=2)
    assert restored['height'] == pytest.approx(after['height'], abs=2)
    tidy(page)
    assert page.locator('.window.ladder').first.bounding_box()['width'] == \
        pytest.approx(before['width'], abs=2)


def test_the_panels_are_not_redrawn_when_the_engine_published_nothing(page):
    """The screen polls three times a second. Redrawing every ladder,
    the grid and the monitor on a snapshot the coordinator has not
    republished is wasted work — and it is worst exactly when the
    coordinator is stalled or restarting, which is when the operator is
    trying to click things to fix it."""
    open_ladder(page)
    frozen = page.evaluate("""() => {
        const snapshot = JSON.parse(JSON.stringify(
            window.MT5Trader.state.snapshot));
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/api/status') >= 0) {
                return Promise.resolve(new Response(JSON.stringify(snapshot),
                    {status: 200,
                     headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
        document.querySelector('.window.ladder .grid').dataset.stamp = 'kept';
        return snapshot.at;
    }""")
    assert frozen

    page.wait_for_timeout(1200)              # four polls, same snapshot

    assert page.get_attribute('.window.ladder .grid', 'data-stamp') == 'kept'
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_the_pairs_table_says_whether_each_ladder_is_actually_quoting(page):
    """"Saved" is not "connected". A pair whose symbol does not exist at
    the broker looks perfect in the config and cannot trade — the engine
    knows, so the row says so."""
    seed_config(page)

    cell = page.locator('td.pair-status').first
    assert 'CONNECTED' in cell.text_content()

    # ...and when the engine reports a problem on that pair, the row
    # carries the engine's own words rather than a green light. HELD
    # first: a poll landing after the error is written replaces the
    # snapshot and takes the error away before the row is read.
    page.evaluate(HOLD_THE_SNAPSHOT)
    try:
        page.wait_for_function("() => window.__held >= 2", timeout=WAIT)
        page.evaluate("""() => {
            const pairs = window.MT5Trader.state.snapshot.pairs;
            const key = Object.keys(pairs)[0];
            pairs[key].errors = ["leg A: 'XAUUSD_' is not on account 'leg_a'"];
            window.MT5Settings.render();
        }""")
        text = page.text_content('td.pair-status')
        assert "is not on account" in text
        assert page.locator('td.pair-status.c-fail').count() >= 1
    finally:
        page.evaluate("""() => {
            const pairs = window.MT5Trader.state.snapshot.pairs;
            Object.keys(pairs).forEach(function (key) {
                delete pairs[key].errors;
            });
        }""")
        page.evaluate(RELEASE_THE_SNAPSHOT)


def test_a_repaint_under_the_pointer_does_not_swallow_the_click(page):
    """A click is mousedown AND mouseup on the SAME element. The
    connection poll rebuilds this whole section every five seconds with
    `innerHTML =`; landing between the two, it destroyed the button the
    operator was pressing and the browser fired NO click — Save pair did
    nothing, said nothing and sent nothing."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)
    page.fill('.p-symbol-a', 'XAUUSD.f')
    page.fill('.p-symbol-b', 'GCZ6.f')
    page.select_option('.p-account-a', 'acct_a')
    page.select_option('.p-account-b', 'acct_b')

    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.__sent = [];
        window.fetch = function (url, options) {
            window.__sent.push((options && options.method) + ' ' + String(url));
            return Promise.resolve(new Response(
                JSON.stringify({ok: true, pair: 'x'}),
                {status: 200, headers: {'Content-Type': 'application/json'}}));
        };
    }""")

    page.locator('.save-pair').scroll_into_view_if_needed()
    box = page.locator('.save-pair').bounding_box()
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.mouse.down()
    # Mark the button the operator is actually pressing.
    page.evaluate("() => { document.querySelector('.save-pair').__me = 1; }")
    # ...and the poll fires, with the button still held down.
    page.evaluate('() => window.MT5Settings.render()')
    assert page.evaluate("() => !!document.querySelector('.save-pair').__me"), \
        'the button was replaced under the pointer — the click is lost'
    page.mouse.up()
    page.wait_for_timeout(400)

    sent = page.evaluate('() => window.__sent')
    page.evaluate('() => { window.fetch = window.__realFetch; }')
    assert any(u.startswith('POST /api/pairs') for u in sent), sent
    # ...and the repaint it held off is not lost, only deferred.
    page.wait_for_timeout(100)
    assert page.evaluate('() => window.MT5Settings.state.pressing') is False


def test_a_click_that_throws_says_so(page):
    """A handler that raised did nothing and SAID nothing — from the
    operator's side, identical to a button that is not wired up."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)
    page.fill('.p-symbol-a', 'XAUUSD.f')
    page.fill('.p-symbol-b', 'GCZ6.f')
    page.select_option('.p-account-a', 'acct_a')
    page.select_option('.p-account-b', 'acct_b')
    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function () { throw new Error('boom'); };
    }""")
    page.click('.save-pair')
    page.wait_for_selector('.toast', timeout=WAIT)
    assert 'that click failed' in page.text_content('#toasts')
    assert 'boom' in page.text_content('#toasts')
    page.evaluate("""() => {
        window.fetch = window.__realFetch;
        document.getElementById('toasts').innerHTML = '';
        window.MT5Settings.state.editing = null;
        window.MT5Settings.render();
    }""")


def test_a_key_typed_with_spaces_is_saved_as_one_pair(page):
    """`XAUUSD.f | GCZ6.f` is what a person types. It is an IDENTIFIER,
    matched exactly by the snapshot and by every panel, so it is tidied
    to the one spelling everything else writes rather than becoming a
    second pair nothing can find."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)

    page.fill('.p-key', '  XAUUSD.f | GCZ6.f ')
    page.fill('.p-symbol-a', 'XAUUSD.f')
    page.fill('.p-symbol-b', 'GCZ6.f')
    page.select_option('.p-account-a', 'acct_a')
    page.select_option('.p-account-b', 'acct_b')
    saved = page.evaluate("""async () => {
        window.__realFetch = window.__realFetch || window.fetch;
        const seen = [];
        window.fetch = function (url, options) {
            seen.push((options && options.method) + ' ' + String(url));
            return Promise.resolve(new Response(
                JSON.stringify({ok: true, pair: 'x'}),
                {status: 200, headers: {'Content-Type': 'application/json'}}));
        };
        document.querySelector('.save-pair').click();
        await new Promise(function (r) { window.setTimeout(r, 400); });
        window.fetch = window.__realFetch;
        return seen;
    }""")
    posts = [u for u in saved if u.startswith('POST')]
    assert posts, saved
    assert 'XAUUSD.f%7CGCZ6.f' in posts[0], posts
    assert '%20' not in posts[0], posts


def test_the_pair_type_is_the_one_the_ladder_also_shows(page):
    """The Exchanges page offered DIFFERENT and the ladder offers
    RELATED. A pair saved under the first left the ladder's Pair type
    box showing nothing selected — and saving that box wrote back
    whatever it happened to be showing."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)
    options = page.eval_on_selector_all(
        '.p-type option', 'els => els.map(e => e.value)')
    assert options == ['SPOT_FUTURE', 'FUTURE_FUTURE', 'RELATED'], options


def test_a_new_pairs_key_is_built_from_its_two_symbols(page):
    """Typing `XAUUSD|GCZ6` by hand is not something a trading screen
    should ask for — and a key typed wrong is a pair that never loads."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)

    page.fill('.p-symbol-a', 'XAUUSD')
    page.fill('.p-symbol-b', 'GCZ6')
    page.select_option('.p-account-a', 'acct_a')
    page.select_option('.p-account-b', 'acct_b')
    saved = page.evaluate("""async () => {
        window.__realFetch = window.__realFetch || window.fetch;
        const seen = [];
        window.fetch = function (url, options) {
            seen.push(String(url));
            return Promise.resolve(new Response(
                JSON.stringify({ok: true, pair: 'x', restart_required: true}),
                {status: 200,
                 headers: {'Content-Type': 'application/json'}}));
        };
        document.querySelector('.save-pair').click();
        await new Promise(function (r) { window.setTimeout(r, 400); });
        window.fetch = window.__realFetch;
        return seen;
    }""")

    assert any('XAUUSD%7CGCZ6' in url or 'XAUUSD|GCZ6' in url
               for url in saved), saved


def test_the_ladder_centres_on_the_mid_and_leaves_a_hand_scroll_alone(page):
    """Both halves matter: the market belongs in the middle, and a
    trader reading a level twenty rows away must not have it snatched
    back under the cursor."""
    open_ladder(page)
    tidy(page)
    page.evaluate("() => { window.MT5Trader.state.locked = {}; }")
    page.wait_for_timeout(600)

    centred = page.evaluate("""() => {
        const grid = document.querySelector('.ladder .grid');
        const rows = [...document.querySelectorAll('.ladder .grid tbody tr')];
        const mid = window.MT5Trader.state.snapshot
            .pairs['XAUUSD_|GC1226'].market.spread;
        const row = rows.reduce((best, r) =>
            Math.abs(r.dataset.level - mid) < Math.abs(best.dataset.level - mid)
                ? r : best);
        const top = row.offsetTop - grid.scrollTop;
        return {top: top, height: grid.clientHeight};
    }""")
    # The mid row sits in the middle third of the window, not at an edge.
    assert centred['height'] / 3 < centred['top'] < \
        centred['height'] * 2 / 3

    # Now scroll by hand and hold it there through several polls.
    page.evaluate("""() => {
        const grid = document.querySelector('.ladder .grid');
        grid.scrollTop = 0;
        grid.dispatchEvent(new Event('scroll'));
    }""")
    page.wait_for_timeout(1200)
    assert page.evaluate(
        "() => document.querySelector('.ladder .grid').scrollTop") == 0


def test_the_taskbar_says_whether_the_whole_thing_is_working(page):
    """One line, always on screen: both accounts attached AND every
    enabled ladder quoting. Anything less is not green."""
    page.wait_for_timeout(400)
    badge = page.locator('#link-badge')
    assert 'LIVE' in badge.text_content()
    assert 'ok' in (badge.get_attribute('class') or '')

    # A ladder with no quote is not "connected", whatever the accounts
    # say — the pair cannot be traded.
    page.evaluate("""() => {
        const state = window.MT5Trader.state;
        state.snapshot.pairs['XAUUSD_|GC1226'].market = null;
        window.MT5Trader.render();
    }""")
    assert 'NO QUOTE' in page.text_content('#link-badge')


def test_the_keyboard_can_be_turned_off_entirely(page):
    """B and S are ORDERS. A desk that does not want a keyboard near
    them can have none — and the buttons still work."""
    open_ladder(page)
    page.click('.ladder .titlebar')
    page.click('#keys-toggle')
    before = command_count(page)

    page.keyboard.press('b')
    page.wait_for_timeout(250)

    assert command_count(page) == before
    assert 'off' in page.text_content('#keys-toggle')
    # The button beside it still sends: turning the keys off is not
    # turning trading off.
    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    page.click('#keys-toggle')


def test_every_ladder_is_reachable_from_one_menu(page):
    """A desk trades several spreads at once — Spot vs Future, WTI vs
    Brent — and each one is a window on this desktop."""
    page.click('#add-panel')
    page.wait_for_selector('#add-menu:not(.hidden)', timeout=WAIT)

    text = page.text_content('#add-menu')
    assert 'Gold basis' in text and 'XAUUSD_' in text
    assert 'Market Grid' in text and 'Trading Monitor' in text

    page.click('#add-menu button[data-panel="monitor:"]')
    page.wait_for_selector('.window.monitor', timeout=WAIT)
    assert page.locator('#add-menu.hidden').count() == 1
    page.click('.window.monitor .titlebar .close')


def test_the_sounds_are_generated_here_and_can_be_silenced(page):
    """No files, nothing fetched: a blocked network must not be able to
    silence a fill. And silence is a first-class setting."""
    played = page.evaluate("""() => {
        const seen = [];
        const Ctor = window.AudioContext || window.webkitAudioContext;
        const original = Ctor.prototype.createOscillator;
        Ctor.prototype.createOscillator = function () {
            const osc = original.call(this);
            seen.push(true);
            return osc;
        };
        window.MT5Trader.sound('filled');
        window.MT5Trader.state.soundOff = true;
        window.MT5Trader.sound('filled');
        window.MT5Trader.state.soundOff = false;
        Ctor.prototype.createOscillator = original;
        return seen.length;
    }""")
    # Two notes for a fill, and nothing at all while it is off.
    assert played == 2
    assert page.locator('#sound-toggle').count() == 1


def test_a_pair_added_while_the_screen_is_open_gets_its_own_ladder(page):
    """A pair configured on the Exchanges page and then nowhere to be
    seen is the whole setup looking broken. It appears beside the
    others, and a ladder the trader CLOSED stays closed.

    The SERVER gains the pair: every /api/status from here on carries
    it, and the real poller opens its ladder. Pushing it into the page's
    snapshot by hand raced that poller — a poll landing between two
    steps replaced the snapshot and took the added pair away with it.
    """
    page.evaluate("""() => {
        window.MT5Trader.state.closed = {};
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            const answer = window.__realFetch(url, options);
            if (String(url).indexOf('/api/status') < 0) { return answer; }
            return answer.then(function (r) { return r.json(); })
              .then(function (snapshot) {
                const pairs = snapshot.pairs || {};
                const copy = JSON.parse(JSON.stringify(
                    pairs['XAUUSD_|GC1226']));
                copy.key = 'EURUSD|GBPUSD';
                copy.name = 'EURUSD - GBPUSD';
                copy.symbol_a = 'EURUSD';
                copy.symbol_b = 'GBPUSD';
                pairs['EURUSD|GBPUSD'] = copy;
                return new Response(JSON.stringify(snapshot), {status: 200,
                    headers: {'Content-Type': 'application/json'}});
              });
        };
    }""")
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('.window.ladder').length === 2",
            timeout=WAIT)
        assert 'EURUSD - GBPUSD' in page.text_content('#tabs')

        # Closed stays closed — across polls that still carry the pair.
        page.evaluate("""() => window.MT5Trader.closePanel(
            window.MT5Trader.panelId('ladder', 'EURUSD|GBPUSD'))""")
        page.wait_for_timeout(1200)
        assert page.locator('.window.ladder').count() == 1
    finally:
        page.evaluate("""() => {
            window.fetch = window.__realFetch || window.fetch;
        }""")
        page.wait_for_function(
            "() => !('EURUSD|GBPUSD' in "
            "(window.MT5Trader.state.snapshot.pairs || {}))", timeout=WAIT)


def test_a_window_opened_at_the_end_of_the_row_is_scrolled_into_view(page):
    """The desktop is a row that scrolls. With ladders and the grid
    already on it, a window opened at the end sits off the right-hand
    edge — and "Positions opens into nothing" is what that looks like
    from the outside."""
    tidy(page)
    page.evaluate("""() => {
        const UI = window.MT5Trader;
        UI.state.closed = {};
        UI.closePanel(UI.panelId('monitor'));
        // A desktop wide enough that the end of the row is off screen.
        document.querySelectorAll('.window.ladder').forEach(function (node) {
            node.style.width = '900px';
        });
        UI.state.open = [UI.panelId('ladder', 'XAUUSD_|GC1226'),
                         UI.panelId('grid')];
        UI.render();
        document.getElementById('desktop').scrollLeft = 0;
    }""")

    page.evaluate("() => window.MT5Trader.openPanel('monitor:')")
    page.wait_for_selector('.window.monitor', timeout=WAIT)
    page.wait_for_timeout(200)

    visible = page.evaluate("""() => {
        const node = document.querySelector('.window.monitor');
        const desktop = document.getElementById('desktop');
        const box = node.getBoundingClientRect();
        const frame = desktop.getBoundingClientRect();
        return box.left >= frame.left - 8 && box.left < frame.right;
    }""")
    assert visible

    page.evaluate("""() => {
        document.querySelectorAll('.window.ladder').forEach(function (node) {
            node.style.width = '';
        });
    }""")
    tidy(page)


def test_a_pair_row_names_the_account_each_leg_trades_on(page):
    """Two accounts is the architecture, and which login a leg is on is
    the first question anyone asks of a spread that has gone wrong."""
    seed_config(page)

    # Scoped to the settings window: the Market Grid marks its rows
    # with data-pair too.
    row = page.locator('.window.settings tr[data-pair] td').nth(2)  # leg A

    assert 'XAUUSD_' in row.text_content()
    assert 'acct_a' in row.text_content()


def test_the_tool_windows_float_over_the_ladders_instead_of_queueing(page):
    """The Market Grid, Positions and Exchanges are wide and are opened
    all day. At the end of the desktop row they push every ladder off
    the screen, and reaching one means scrolling sideways past the
    lot."""
    tidy(page)
    open_ladder(page)
    page.evaluate("() => window.MT5Trader.openPanel('monitor:')")
    page.wait_for_selector('.window.monitor', timeout=WAIT)
    page.wait_for_timeout(200)

    assert page.locator('.window.monitor.floating').count() == 1
    inside = page.evaluate("""() => {
        const box = document.querySelector('.window.monitor')
            .getBoundingClientRect();
        const frame = document.getElementById('desktop')
            .getBoundingClientRect();
        return box.left >= frame.left - 2 && box.right <= frame.right + 2;
    }""")
    assert inside
    # ...and the ladder is still where it was, in the row behind it.
    assert page.locator('.window.ladder.floating').count() == 0
    page.evaluate("() => window.MT5Trader.closePanel('monitor:')")
    tidy(page)


def test_the_journal_colours_what_was_made_and_what_was_lost(page):
    """A column of black numbers is one nobody reads twice. An OPENING
    deal books nothing, though, so 0.00 stays black — colouring it green
    would make every entry look like a winner."""
    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/api/fills') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true,
                    totals: {fills: 3, volume: 3, commission: -2,
                             commission_measured: 3, swap: 0,
                             swap_measured: 3, profit: 64.9},
                    fills: [
                        {deal_id: '1', account: 'a', symbol: 'X',
                         side: 'buy', entry: 'close', volume: 1,
                         price: 10, profit: 81.1, is_ours: 1},
                        {deal_id: '2', account: 'a', symbol: 'X',
                         side: 'sell', entry: 'close', volume: 1,
                         price: 10, profit: -16.2, is_ours: 1},
                        {deal_id: '3', account: 'a', symbol: 'X',
                         side: 'buy', entry: 'open', volume: 1,
                         price: 10, profit: 0.0, is_ours: 1}
                    ]}), {status: 200,
                          headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
        const UI = window.MT5Trader;
        UI.state.fills = null;
        UI.state.open = [UI.panelId('monitor')];
        UI.state.monitorTab = 'fills';
        UI.render();
    }""")
    page.wait_for_function(
        "() => document.querySelectorAll('.monitor td.up').length > 0",
        timeout=WAIT)

    # Read in ONE evaluate: the monitor repaints on every publish, and a
    # node resolved in Python can be detached before its style is read —
    # which comes back as an empty string, not as a wrong colour.
    colours = page.evaluate("""() => {
        const up = [...document.querySelectorAll('.monitor td.up')];
        const down = [...document.querySelectorAll('.monitor td.down')];
        return {ups: up.length, downs: down.length,
                made: up[0] ? getComputedStyle(up[0]).color : null,
                lost: down[0] ? getComputedStyle(down[0]).color : null};
    }""")

    assert colours['ups'] == 1                                # the winner
    assert colours['downs'] == 1                              # the loser
    assert colours['made'] == 'rgb(74, 156, 93)'
    assert colours['lost'] == 'rgb(192, 80, 77)'
    page.evaluate("""() => {
        window.fetch = window.__realFetch;
        window.MT5Trader.state.fills = null;
        window.MT5Trader.state.monitorTab = 'positions';
    }""")


def test_the_armed_size_applies_to_both_sides(page):
    """One number, both directions: a keypad that armed only the buy
    side would be a size that means something different depending on
    which button is pressed."""
    open_ladder(page)
    page.click('.ladder .titlebar')
    page.click('.ladder .keypad button[data-qty="10"]')
    page.wait_for_timeout(150)
    assert page.input_value('.ladder .armed') == '10'
    assert page.input_value('.ladder .sell-qty') == '10'

    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['quantity'] == 10
    assert last_command(page)['payload']['side'] == 'BUY'

    page.click('.ladder .sell-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['quantity'] == 10
    assert last_command(page)['payload']['side'] == 'SELL'

    # Armed is not the ladder's default, so it does not look like it.
    colour = page.locator('.ladder .armed').evaluate(
        'n => getComputedStyle(n).backgroundColor')
    assert colour == 'rgb(12, 133, 153)'
    page.click('.ladder .keypad button.clr')


def test_the_cancel_buttons_are_dead_when_there_is_nothing_to_cancel(page):
    """Three buttons that look pressable and do nothing are three
    buttons the trader wonders about while the market moves."""
    open_ladder(page)
    # Wait for a render off a fresh snapshot rather than a fixed pause:
    # the button's state comes from the published working counts.
    page.wait_for_function(
        "() => document.querySelector('.ladder .cxl-all')"
        " && document.querySelector('.ladder .cxl-all').disabled",
        timeout=WAIT)

    assert page.locator('.ladder .cxl-all').is_disabled()
    assert 'CXL All' in page.text_content('.ladder .cxl-all')

    # Published, not poked into the page: the next snapshot is 200ms
    # away and erases anything written into it by hand.
    page.paths['publisher'].working_buys = 2
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => !document.querySelector('.ladder .cxl-b').disabled",
        timeout=WAIT)

    assert '2' in page.text_content('.ladder .cxl-b')
    page.paths['publisher'].working_buys = 0
    page.paths['publisher'].publish()


def test_the_mid_carries_a_rule_of_its_own(page):
    """On a wide spread the two touches are many rows apart, and "where
    is the market" is a question the inside rule alone cannot answer."""
    open_ladder(page)
    page.wait_for_selector('.ladder tr.mid-line', timeout=WAIT)

    border = page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'.ladder tr.mid-line td')).borderTopWidth")
    assert border == '2px'
    assert page.locator('.ladder tr.mid-line').count() == 1


def test_each_side_can_carry_its_own_size(page):
    """Usually they are the same — the keypad sets both — but a desk
    that wants to lift 1 and offer 5 types each one, in the box beside
    the button that will send it."""
    open_ladder(page)
    page.click('.ladder .keypad button[data-qty="1"]')
    page.fill('.ladder .sell-qty', '5')
    page.wait_for_timeout(150)

    assert 'BUY 1' in page.text_content('.ladder .buy-touch')
    assert 'SELL 5' in page.text_content('.ladder .sell-touch')

    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['quantity'] == 1

    page.click('.ladder .sell-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['quantity'] == 5

    # ...and a ladder click takes the size of the SIDE IT SENDS, not of
    # the column it is in. Those are not the same thing: under the
    # shipped TT default the asks column sends a SELL, so it must carry
    # the SELL box's 5 and not the BUY box's 1. A click that took its
    # size from the column would send 1 where the desk had typed 5.
    #
    # The convention is pinned rather than inherited, so that a change
    # of default cannot silently turn this into a test of the other
    # mapping - which is exactly what happened to it once.
    try:
        set_convention(page, 'TT')
        page.locator('.ladder .grid tbody tr td.ask').nth(3).click()
        page.wait_for_timeout(250)
        assert last_command(page)['payload']['side'] == 'SELL'
        assert last_command(page)['payload']['quantity'] == 5

        page.locator('.ladder .grid tbody tr td.bid').nth(3).click()
        page.wait_for_timeout(250)
        assert last_command(page)['payload']['side'] == 'BUY'
        assert last_command(page)['payload']['quantity'] == 1
    finally:
        # ALWAYS clear the pad. The page fixture is module-scoped, so a
        # size left in the box is still there for every later test in
        # this file: when this test failed above, the next one read a
        # qty of 5 it never typed and failed too. A cleanup that only
        # runs when the test passes is not a cleanup.
        page.click('.ladder .keypad button.clr')
        page.fill('.ladder .sell-qty', '')
        page.wait_for_timeout(150)


def test_any_size_from_0_01_can_be_typed_into_the_qty_box(page):
    """The keypad is five common sizes; the box is every other one. A
    lot is 0.01 at most brokers, and a ladder that only offers 1, 5, 10,
    50 and 100 cannot trade the size the account is sized for."""
    open_ladder(page)
    page.fill('.ladder .armed', '0.25')
    page.wait_for_timeout(150)

    before = command_count(page)
    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)

    assert command_count(page) == before + 1
    assert last_command(page)['payload']['quantity'] == 0.25
    # Emptying it goes back to the ladder's own default, not to zero —
    # a click that sends nothing is worse than one that sends the
    # default.
    page.fill('.ladder .armed', '')
    page.wait_for_timeout(150)
    page.click('.ladder .sell-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload'].get('quantity') in (None, 1)


def test_the_shared_account_notice_can_be_put_away_and_comes_back(page):
    """It is an FYI, not a blocker: it names something worth knowing and
    the trader keeps working. Dismissing it is for the SITUATION they
    read — a different one brings it back."""
    page.evaluate('() => { window.MT5Trader.state.dismissed = {}; }')
    page.paths['publisher'].same_login = {'100006': ['leg_a', 'leg_b']}
    page.paths['publisher'].publish()
    page.wait_for_selector('#same-login-banner:not(.hidden)', timeout=WAIT)
    assert '#100006' in page.text_content('#same-login-banner')
    # One LINE on the screen; the full reading of it is the tooltip, and
    # the button goes where it is fixed.
    assert 'One account, not two' in page.text_content('#same-login-banner')
    assert 'one pool' in page.get_attribute('#same-login-banner', 'title')
    assert page.locator('#same-login-banner .banner-open').count() == 1

    page.click('#same-login-banner .banner-close')
    page.wait_for_selector('#same-login-banner.hidden', state='attached',
                           timeout=WAIT)

    # A DIFFERENT login is a different situation, and it is said again.
    page.paths['publisher'].same_login = {'200007': ['leg_a', 'leg_b']}
    page.paths['publisher'].publish()
    page.wait_for_selector('#same-login-banner:not(.hidden)', timeout=WAIT)

    page.paths['publisher'].same_login = None
    page.paths['publisher'].publish()
    page.wait_for_selector('#same-login-banner.hidden', state='attached',
                           timeout=WAIT)


def test_nothing_in_the_window_is_cropped_when_it_is_made_short(page):
    """The rail is taller than a short window, and the footer carries
    the feed badge and the position. Neither may be cut off: the ladder
    scrolls instead."""
    open_ladder(page)
    tidy(page)
    page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        node.classList.add('sized');
        node.style.height = '320px';
    }""")
    page.wait_for_timeout(200)

    fits = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const box = node.getBoundingClientRect();
        const footer = node.querySelector('.footer').getBoundingClientRect();
        const rail = node.querySelector('.rail');
        return {footerInside: footer.bottom <= box.bottom + 1,
                railScrolls: rail.scrollHeight > rail.clientHeight
                    ? getComputedStyle(rail).overflowY === 'auto' : true};
    }""")
    assert fits['footerInside']
    assert fits['railScrolls']
    tidy(page)


def test_each_legs_book_is_laid_out_with_its_width(page):
    """Bid, ask and the width each leg is charging — and the same three
    for the spread, whose width IS the round turn. Read by comparing
    them, so it is a table."""
    open_ladder(page)
    page.wait_for_selector('table.legbook', timeout=WAIT)

    # The PRICE rows, addressed by what they are rather than by how
    # many there are. A direction hint (H -> L / L -> H) was later
    # added between leg B and the spread, and a bare count of 3 turned
    # red for a row that carries no prices at all - the panel was
    # right, the arithmetic in the test was stale. Counting only the
    # rows under test survives another row being added beside them.
    rows = page.locator('table.legbook tbody tr:not(.spread-hint)')
    assert rows.count() == 3                       # A, B, and the spread
    assert '0.2500' in rows.nth(0).text_content()  # leg A's own width
    assert '0.4000' in rows.nth(1).text_content()
    # ...and the hint really is there, directly above the spread row,
    # so 'not .spread-hint' is excluding something that exists rather
    # than quietly matching everything.
    assert page.locator('table.legbook tr.spread-hint').count() == 1
    # A leg that has stopped is marked — on its AGE cell, not down the
    # whole line: painting the row red made a quiet market look like a
    # fault.
    assert page.locator('table.legbook td.age').count() == 3
    page.paths['publisher'].stale_leg = True
    page.paths['publisher'].publish()
    page.wait_for_selector('table.legbook td.age.bad', timeout=WAIT)
    assert page.locator('table.legbook td.age.bad').count() == 1
    page.paths['publisher'].stale_leg = False
    page.paths['publisher'].publish()
    spread = rows.nth(2).text_content()
    assert '59.0900' in spread and '59.1100' in spread
    assert '0.0200' in spread                      # one round turn


def test_an_order_the_broker_refused_is_said_out_loud_and_kept_on_screen(page):
    """The failure this was built for: the broker refuses the pending
    behind a synthetic, the order leaves the book in the same instant
    the click was accepted, and the trader is left with a green toast
    and an empty Work column."""
    open_ladder(page)
    page.evaluate('() => { window.MT5Trader.state.reported = {}; }')
    page.paths['publisher'].dead_orders = [{
        'order_id': 'SO-DEAD', 'pair_key': 'XAUUSD_|GC1226', 'side': 'BUY',
        'level': 59.05, 'quantity': 2, 'filled_quantity': 0,
        'order_type': 'LIMIT', 'time_in_force': 'DAY', 'state': 'REJECTED',
        'pending_ticket': None,
        'reason': "10016 Invalid stops — the price is inside the broker's "
                  "stops level"}]
    page.paths['publisher'].publish()

    # Said once, in the broker's own words, and errors do not auto-hide.
    page.wait_for_selector('.toast', timeout=WAIT)
    assert '10016' in page.text_content('.toast')
    assert 'REJECTED' in page.text_content('.toast').upper()

    # ...and it keeps its place in the Working Orders tab with the
    # reason, rather than vanishing.
    page.evaluate("""() => {
        const UI = window.MT5Trader;
        UI.state.open = [UI.panelId('monitor')];
        UI.state.monitorTab = 'orders';
        UI.render();
    }""")
    page.wait_for_selector('.monitor tr.dead', timeout=WAIT)
    text = page.text_content('.monitor tr.dead')
    assert 'REJECTED' in text and '10016' in text

    page.click('.toast')
    page.paths['publisher'].dead_orders = None
    page.paths['publisher'].publish()
    page.evaluate("() => window.MT5Trader.closePanel('monitor:')")

def test_the_leg_panel_never_runs_off_the_edge_of_the_ladder(page):
    """It was laid out in `ch` units, which is fine until the prices are
    four digits and a symbol is long — then it ran off the right-hand
    edge and the AGE column went first, which is the column that says
    which leg stopped."""
    open_ladder(page)
    tidy(page)
    page.evaluate("""() => {
        const market = window.MT5Trader.state.snapshot
            .pairs['XAUUSD_|GC1226'].market;
        market.leg_a_bid = 4609.79; market.leg_a_ask = 4609.99;
        market.leg_b_bid = 4662.75; market.leg_b_ask = 4663.14;
        market.leg_a_quote_age_sec = 0.9;
        market.leg_b_quote_age_sec = 0.6;
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('table.legbook', timeout=WAIT)

    fits = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const table = node.querySelector('table.legbook');
        const win = node.getBoundingClientRect();
        const box = table.getBoundingClientRect();
        const age = table.querySelector('td.c-age').getBoundingClientRect();
        return {inside: box.right <= win.right + 1 && box.left >= win.left - 1,
                ageVisible: age.right <= win.right + 1 && age.width > 0};
    }""")

    assert fits['inside'], 'the leg panel is wider than the ladder'
    assert fits['ageVisible'], 'the Age column fell off the edge'


def test_the_exit_price_is_on_the_rail_for_both_directions(page):
    """The trader wants one number back when they click: where do I get
    out? Break-even first, then break-even plus the target — and both
    directions, because a long leaves on the bid and a short buys back
    on the offer. At the LADDER's precision: the rail is 100px wide and
    two seven-character numbers side by side collide."""
    open_ladder(page)
    page.paths['publisher'].exits = {
        'break_even_buy': 59.21, 'break_even_sell': 58.99,
        'tp_buy': 60.21, 'tp_sell': 57.99, 'target_money': 10.0,
        'target_pct': 2.0, 'margin_per_spread': 500.0,
        'note': '2% of 500.00 margin per spread = 10.00, over commission'}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .tp-buy')"
        " && document.querySelector('.fairwin .tp-buy').textContent"
        " === '60.21'", timeout=WAIT)

    assert page.text_content('.fairwin .be-buy') == '59.21'
    assert page.text_content('.fairwin .tp-sell') == '57.99'
    # The long wording is the tooltip; the rail carries the short form.
    assert '2% of' in page.text_content('.fairwin .exit-note')
    assert 'over commission' in page.get_attribute('.fairwin .exit-note',
                                                    'title')

    # A position that is ON gets its own line, anchored on the price it
    # was ENTERED at.
    page.paths['publisher'].positions = [{
        'position_id': 'P1', 'side': 'BUY', 'quantity': 1,
        'entry_spread': 59.11, 'spread_units': 10, 'order_type': 'MARKET',
        'leg_a': None, 'leg_b': None, 'net_pnl': None, 'gross_pnl': None,
        'closing_spread': None,
        'exit': {'break_even': 59.21, 'tp': 60.21, 'side': 'BUY'}}]
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .exit-note')"
        ".textContent.indexOf('out') >= 0", timeout=WAIT)
    assert 'flat at 59.2100' in page.get_attribute('.fairwin .exit-note',
                                                    'title')

    page.paths['publisher'].exits = None
    page.paths['publisher'].positions = None
    page.paths['publisher'].publish()

def test_the_fair_spread_is_quoted_for_both_directions(page):
    """Buying the spread pays the offer and selling it receives the bid,
    and the two are charged different swaps on different legs. One fair
    value here would be right half the time — and a gap measured off a
    midpoint compares the market against a price nobody fills at."""
    open_ladder(page)
    page.paths['publisher'].fair = {
        'source': 'swap', 'days_to_expiry': 30, 'expects_expiry': True,
        'fair_buy': 0.60, 'fair_sell': 0.40,
        'gap_buy': 0.15, 'gap_sell': -0.20,
        'warning': None, 'fix': None,
        'note': "30 nights of the broker's own swap, over k"}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .fair-buy')"
        " && document.querySelector('.fairwin .fair-buy').textContent"
        " === '0.60'", timeout=WAIT)

    assert page.text_content('.fairwin .fair-sell') == '0.40'
    # Rich on the buy side, cheap on the sell side — and the sign is
    # said in colour as well, because the direction of a basis is the
    # thing everyone gets backwards once.
    assert page.text_content('.fairwin .gap-buy') == '+0.15'
    assert 'down' in (page.get_attribute('.fairwin .gap-buy', 'class') or '')
    assert 'up' in (page.get_attribute('.fairwin .gap-sell', 'class') or '')
    assert '30d' in page.text_content('.fairwin .fair-note')
    assert page.is_hidden('.fairwin .fair-warn')

    page.paths['publisher'].fair = None
    page.paths['publisher'].publish()


def test_a_disputed_swap_replaces_the_reading_and_offers_the_correction(page):
    """A conclusion drawn from an input that can be proven wrong should
    not render at all. The warning stands where the number was, names
    the field, and offers the corrected value as ONE CLICK — never
    applied behind the operator, because a sign the engine flipped by
    itself is a sign nobody would ever notice was wrong."""
    open_ladder(page)
    page.paths['publisher'].fair = {
        'source': 'swap', 'days_to_expiry': 30, 'expects_expiry': True,
        'fair_buy': None, 'fair_sell': None,
        'gap_buy': None, 'gap_sell': None,
        'warning': 'XAGUSD is the leg you would be LONG and its swap is a '
                   'CREDIT (+58.00 a night). A long leg is normally charged '
                   '— check the sign.',
        'fix': {'field': 'swap_a_long_per_lot', 'value': -58.0,
                'symbol': 'XAGUSD'},
        'disputed': {'fair_buy': -51.82, 'fair_sell': -51.82}}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .fair-warn')"
        " && !document.querySelector('.fairwin .fair-warn').hidden",
        timeout=WAIT)

    # The number is GONE, not printed beneath the warning.
    assert page.text_content('.fairwin .fair-buy') == '—'
    assert 'CREDIT' in page.text_content('.fairwin .fair-warn-text')
    assert 'swap a long per lot' in page.text_content('.fairwin .fair-fix')
    assert '-58.00' in page.text_content('.fairwin .fair-fix')

    page.paths['publisher'].fair = None
    page.paths['publisher'].publish()


def test_fair_value_and_the_exit_live_in_their_OWN_window(page):
    """The ladder is for the price. A panel of derived figures beside it
    is a panel between the trader and the market, so fair value and the
    exit are a separate window — off by default, per pair, and floating
    where it is put.

    And the pair is named across the top of it: two of these open at
    once are otherwise two identical tables of figures with nothing to
    say which instrument either belongs to.
    """
    open_ladder(page)
    page.wait_for_selector('.window.fairwin .exit', timeout=WAIT)

    assert page.locator('.window.ladder .fair').count() == 0
    assert page.locator('.window.ladder .exit').count() == 0
    assert page.text_content('.window.fairwin .fw-pair') == 'Gold basis'
    assert 'XAUUSD_' in page.get_attribute('.window.fairwin .fw-pair', 'title')


def test_the_fair_window_is_no_bigger_than_the_figures_in_it(page):
    """Screen beside a ladder is not spare. Two short tables do not
    need a pane the size of a ladder — the labels are read down the
    left and the numbers down the right, and everything between them is
    room a price could have had.

    Its HEIGHT is the content's, not a number: a fixed one leaves a
    panel of empty grey under two short tables, which is what it did.
    """
    open_ladder(page)
    page.wait_for_selector('.window.fairwin .exit', timeout=WAIT)

    box = page.evaluate("""() => {
        const win = document.querySelector('.window.fairwin');
        const body = win.querySelector('.fw-body');
        const rect = win.getBoundingClientRect();
        const chrome = win.querySelector('.titlebar').offsetHeight;
        return {width: rect.width, height: rect.height,
                content: body.scrollHeight + chrome,
                ladder: document.querySelector('.window.ladder')
                    .getBoundingClientRect().width};
    }""")

    # Narrower than a ladder, and no taller than what is in it.
    assert box['width'] <= box['ladder'] * 0.75, box
    assert box['height'] <= box['content'] + 8, box

    # And no CORRIDOR: on every row the figure follows its label rather
    # than sitting against the far edge with nothing in between. That
    # empty middle was most of the window.
    gaps = page.evaluate("""() => {
        const rows = [...document.querySelectorAll(
            '.window.fairwin table.exits tr')];
        return rows.map(function (tr) {
            const label = tr.querySelector('th');
            const value = tr.querySelector('td');
            if (!label || !value) { return 0; }
            return Math.round(value.getBoundingClientRect().left
                              - label.getBoundingClientRect().right);
        });
    }""")
    assert max(gaps) <= 40, gaps

    # A long note does not set the width: it wraps inside whatever the
    # figures need. `width: max-content` sizes to the longest line in
    # the window, and the derivation is a sentence.
    page.paths['publisher'].exits = {
        'break_even_buy': 59.21, 'break_even_sell': 58.99,
        'tp_buy': 60.21, 'tp_sell': 57.99, 'target_money': 10.0,
        'target_pct': 2.0, 'margin_per_spread': 135.8,
        'note': '2% of 135.80 margin per spread = 10.00 = 1.0000 of '
                'spread, against a 0.3000 session range (margin from '
                'the terminal)'}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => (document.querySelector('.window.fairwin .exit-note')"
        ".title || '').indexOf('session range') >= 0", timeout=WAIT)

    wide = page.evaluate(
        "() => document.querySelector('.window.fairwin')"
        ".getBoundingClientRect().width")
    # Room for the figures themselves to be wider than em dashes, and
    # nothing like the width of the sentence underneath them.
    assert wide <= box['width'] + 24, (box['width'], wide)
    page.paths['publisher'].exits = None
    page.paths['publisher'].publish()
    # ...and the figures still fit: nothing is clipped to achieve it.
    clipped = page.evaluate("""() => {
        const cells = [...document.querySelectorAll(
            '.window.fairwin table.exits td, .window.fairwin table.exits th')];
        return cells.filter(c => c.scrollWidth > c.clientWidth + 1).length;
    }""")
    assert clipped == 0


def test_the_window_is_ONE_choice_on_the_ladder_it_belongs_to(page):
    """Per ladder, None by default, and one control for one decision.

    There was a dropdown (None / Fair spread) beside a Show window tick
    once, and either alone did nothing anybody could see. Now there are
    two readings — Fair spread and the Algo — and still ONE control
    picks which the window shows. No tick beside it."""
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-algo', timeout=WAIT)

    assert page.locator('.ladder .ls-algo').count() == 1
    assert page.locator('.ladder .ls-algo-window').count() == 0
    options = page.eval_on_selector(
        '.ladder .ls-algo', 'el => [...el.options].map(o => o.value)')
    assert options == ['NONE', 'FAIR_SPREAD', 'ALGO', 'ALGO_LIVE']
    # The group says out loud what the Algo is NOT.
    note = ' '.join(page.text_content(
        '.ladder .ls-group:has(.ls-algo) .lsf-note').split())
    assert 'nothing is sent' in note and 'clicks on the ladder work' in note
    page.click('.ladder .ls-close')


def test_turning_the_window_off_closes_it_and_KEEPS_it_closed(page):
    """Closing it IS turning it off. A window that comes back on the
    next poll — because the pair's setting still says so — is a window
    the trader cannot get rid of."""
    open_ladder(page)
    page.wait_for_selector('.window.fairwin', timeout=WAIT)

    page.click('.window.fairwin .close')
    page.wait_for_function(
        "() => !document.querySelector('.window.fairwin')", timeout=WAIT)

    # ...and it stays gone across the polls that follow, with the
    # engine still publishing the setting as ON.
    page.wait_for_timeout(700)
    assert page.locator('.window.fairwin').count() == 0

    page.evaluate(
        "() => window.MT5Trader.showFairWindow('XAUUSD_|GC1226', true)")
    page.wait_for_selector('.window.fairwin', timeout=WAIT)


def test_the_settings_behind_the_exit_are_one_click_from_it(page):
    """"Where do I change these?" is a question the panel showing the
    numbers should answer itself — and answer with THIS ladder\'s own
    settings, because the next ladder is a different instrument."""
    open_ladder(page)

    page.click('.fairwin .exit .open-exits')
    page.wait_for_selector('.ladder .ladder-settings .ls-tp', timeout=WAIT)

    assert page.text_content('.ladder .ls-pair') == 'XAUUSD_|GC1226'
    page.click('.ladder .ls-close')
    assert page.locator('.ladder .ladder-settings').is_hidden()


def test_the_leg_books_width_column_is_called_Spread(page):
    """The operator's word for it. The spread row's own figure is the
    two legs' summed — one round turn of both books."""
    open_ladder(page)
    page.wait_for_selector('.ladder table.legbook', timeout=WAIT)

    assert page.text_content('.ladder table.legbook th.c-width') == 'Spread'


def test_GTC_carries_its_caveat_on_the_screen(page):
    """A synthetic order lives in THIS process. Nothing at the broker
    knows what a spread is, so GTC cannot mean what it means on an
    exchange — and an operator who reads it as exchange-resident
    believes an order is watching a market that nothing is watching.
    The caveat belongs on the screen, not only in the code."""
    open_ladder(page)

    caveat = page.get_attribute('.ladder .tif', 'title')
    assert 'UNTIL THIS SYSTEM STOPS' in caveat
    assert 'does not resume' in caveat or 'none resumes' in caveat
    assert 'until this system stops' in page.get_attribute(
        '.ladder .tif option[value="GTC"]', 'title')


def test_AutoRouting_is_gone_from_every_screen(page):
    """AutoRouting has been removed: the Algo is the one thing that
    exits by itself, and two would confuse the desk. No tick in the
    ladder's settings, no row on the Exit panel, no AUTO in the title."""
    open_ladder(page)
    assert page.locator('.ls-auto-route').count() == 0
    assert page.locator('.auto-route-state').count() == 0
    assert 'AUTO' not in page.text_content('.ladder .mode-badge')
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ladder-settings .ls-tp', timeout=WAIT)
    assert 'AutoRoute' not in page.text_content('.ladder .ladder-settings')
    page.evaluate("() => document.querySelectorAll('.ladder-settings')"
                  ".forEach(p => p.hidden = true)")


def test_nothing_about_the_take_profit_is_sent_to_the_broker():
    """It is a price on the SCREEN. No bracket order, no broker-side
    stop — one leg stopping alone converts the hedge into a naked
    position, which is the rule this whole system is built around."""
    source = (Path(__file__).resolve().parent.parent / 'okxtrader'
              / 'takeprofit.py').read_text(encoding='utf-8')
    for forbidden in ('place_limit', 'order(', 'send_market_order',
                      'sl=', 'tp='):
        assert forbidden not in source, forbidden


def test_the_ladder_grows_in_both_directions_and_the_grid_takes_the_room(page):
    """Wider means a wider price column and a wider leg table under it;
    taller means more price rows on screen, which is the whole point of
    a ladder."""
    open_ladder(page)
    tidy(page)
    page.evaluate("""() => {
        const market = window.MT5Trader.state.snapshot
            .pairs['XAUUSD_|GC1226'].market;
        market.leg_a_bid = 4607.38; market.leg_a_ask = 4607.63;
        market.leg_b_bid = 4660.40; market.leg_b_ask = 4660.80;
        market.leg_a_quote_age_sec = 0.3;
        market.leg_b_quote_age_sec = 1.5;
        window.MT5Trader.render();
    }""")
    before = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const rows = node.querySelectorAll('.grid tbody tr').length;
        const grid = node.querySelector('.grid').getBoundingClientRect();
        return {w: node.getBoundingClientRect().width,
                gridW: grid.width, gridH: grid.height, rows: rows};
    }""")

    grip = page.locator('.window.ladder .grip').first.bounding_box()
    page.mouse.move(grip['x'] + 6, grip['y'] + 6)
    page.mouse.down()
    page.mouse.move(grip['x'] + 246, grip['y'] - 106, steps=8)
    page.mouse.up()

    after = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const grid = node.querySelector('.grid').getBoundingClientRect();
        const table = node.querySelector('table.legbook')
            .getBoundingClientRect();
        const win = node.getBoundingClientRect();
        return {w: win.width, gridW: grid.width, gridH: grid.height,
                tableInside: table.right <= win.right + 1};
    }""")

    assert after['w'] - before['w'] == pytest.approx(240, abs=10)
    # The extra width goes to the ladder itself, not to the rail.
    assert after['gridW'] - before['gridW'] == pytest.approx(240, abs=12)
    assert after['gridH'] < before['gridH']          # shorter, as dragged
    assert after['tableInside']
    tidy(page)


def test_the_exit_box_shows_the_figures_it_is_built_from(page):
    """Break-even with no workings is a number to be trusted or not.
    With them it can be checked: the round turn the market charges, the
    commission the broker charges, and the profit the target asks for —
    and every one of them fits in a 100px rail."""
    open_ladder(page)
    page.paths['publisher'].exits = {
        'break_even_buy': 59.21, 'break_even_sell': 58.99,
        'tp_buy': 60.21, 'tp_sell': 57.99, 'target_money': 10.0,
        'target_pct': 2.0, 'margin_per_spread': 500.0,
        'spread_width': 0.02, 'commission': 1.0,
        'note': '2% of 500.00 margin per spread = 10.00, over commission'}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .x-comm')"
        " && document.querySelector('.fairwin .x-comm').textContent"
        " !== '\\u2014'", timeout=WAIT)

    assert page.text_content('.fairwin .x-width') == '0.02'
    assert page.text_content('.fairwin .x-comm') == '$1.00'
    assert page.text_content('.fairwin .x-target') == '$10.00'
    assert '2% of' in page.get_attribute('.fairwin .x-target', 'title')

    # Nothing overflows the rail it lives in.
    fits = page.evaluate("""() => {
        const rail = document.querySelector('.ladder .rail');
        const box = rail.getBoundingClientRect();
        return [...rail.querySelectorAll('.exits td, .exits th')].every(
            cell => cell.getBoundingClientRect().right <= box.right + 1
                 && cell.scrollWidth <= cell.clientWidth + 1);
    }""")
    assert fits, 'a figure in the Exit box is wider than the rail'
    page.paths['publisher'].exits = None
    page.paths['publisher'].publish()


def test_the_leg_book_is_moved_to_the_bottom_whatever_the_markup_says(page):
    """Where this panel sits is not a detail: beside the ladder's own
    prices it reads as part of them. A page served from a template an
    older process cached can put it anywhere, so the position is made
    true at render time rather than assumed from the markup."""
    open_ladder(page)
    page.wait_for_selector('.ladder .footer table.legbook', timeout=WAIT)

    # Put it back in the quote strip, as an older page had it, and let
    # one render pass happen.
    page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        node.querySelector('.quotestrip').appendChild(
            node.querySelector('.legs'));
        window.MT5Trader.render();
    }""")

    assert page.locator('.ladder .footer .legs').count() == 1
    assert page.locator('.ladder .quotestrip .legs').count() == 0
    # ...and it is still the last thing in the window, under the ladder.
    assert page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const grid = node.querySelector('.grid').getBoundingClientRect();
        const legs = node.querySelector('.legs').getBoundingClientRect();
        return legs.top >= grid.top;
    }""")


def test_the_unclaimed_notice_is_one_line_and_the_table_is_where_it_acts(page):
    """A position at the broker our book cannot explain is exactly the
    one an automatic close must never touch, so it has to be said. A
    wall of table across the top of the screen is not how to say it:
    the line names the number, and the table lives with the buttons
    that act on it."""
    page.evaluate('() => { window.MT5Trader.state.dismissed = {}; }')
    page.paths['publisher'].unclaimed = [
        {'account': 'acct_a', 'ticket': 1326, 'symbol': 'GCZ6',
         'side': 'BUY', 'volume': 0.01, 'price_open': 4631.84}]
    page.paths['publisher'].publish()
    page.wait_for_selector('#unclaimed-banner:not(.hidden)', timeout=WAIT)

    text = page.text_content('#unclaimed-banner')
    assert '1 position(s)' in text
    assert 'Nothing is closed automatically' in text
    assert 'GCZ6' not in text                     # the table is elsewhere
    assert 'GCZ6' in page.get_attribute('#unclaimed-banner', 'title')

    # Review opens the Reconciler, where the position and its Close it
    # button are.
    page.click('#unclaimed-banner .banner-open')
    page.wait_for_selector('.monitor table.unclaimed', timeout=WAIT)
    row = page.text_content('.monitor table.unclaimed')
    assert 'GCZ6' in row and '1326' in row
    assert page.locator('.monitor .close-unclaimed').count() == 1

    # ...and the notice can be put away.
    page.click('#unclaimed-banner .banner-close')
    page.wait_for_selector('#unclaimed-banner.hidden', state='attached',
                           timeout=WAIT)
    page.paths['publisher'].unclaimed = None
    page.paths['publisher'].publish()
    page.evaluate("() => window.MT5Trader.closePanel('monitor:')")


def test_the_size_is_on_the_two_buttons_as_well_as_in_the_box(page):
    """A trader who arms 10 and then presses SELL should not have to
    look back at a box on the other side of the rail to know what they
    are about to send."""
    open_ladder(page)
    page.click('.ladder .keypad button[data-qty="10"]')
    page.wait_for_timeout(150)

    assert 'BUY 10' in page.text_content('.ladder .buy-touch')
    assert 'SELL 10' in page.text_content('.ladder .sell-touch')

    page.click('.ladder .keypad button.clr')
    page.wait_for_function(
        "() => document.querySelector('.ladder .buy-touch')"
        ".textContent.trim() === 'BUY 1'", timeout=WAIT)
    # CLR is the ladder's own default, not a blank button.
    assert 'SELL 1' in page.text_content('.ladder .sell-touch')


def test_the_feed_can_be_refreshed_from_the_ladder(page):
    """"How do I refresh it?" needs a button, not an explanation. It
    sits beside the badge that reports the trouble."""
    open_ladder(page)
    before = command_count(page)

    page.click('.ladder .refresh-feed')
    page.wait_for_timeout(300)

    assert command_count(page) == before + 1
    command = last_command(page)
    assert command['kind'] == 'refresh_feed'
    assert command['payload']['pair'] == 'XAUUSD_|GC1226'


def test_the_two_columns_say_what_they_are(page):
    """Work and LTQ are the two easiest things on a ladder to confuse:
    one is an order that is still live, the other is a trade that
    already happened."""
    open_ladder(page)

    work = page.get_attribute('.ladder .grid th.c-work', 'title')
    ltq = page.get_attribute('.ladder .grid th.c-ltq', 'title')

    assert 'resting orders' in work
    # ...and what the two buttons on it do. Clicking a level you are
    # working ADDS one; the pull is the right button.
    assert 'CLICK to add one more' in work
    assert 'RIGHT-CLICK to pull one' in work
    assert 'most recent FILL' in ltq
    assert 'not an order' in ltq

    page.click('#help')
    page.wait_for_selector('#help-overlay:not(.hidden)')
    text = page.text_content('#help-overlay')
    assert 'RESTING orders' in text and 'last FILL' in text
    page.click('#help-close')


def test_an_unapplied_setting_survives_the_connection_repaint(page):
    """The 5s connection poll repaints the Trading form from the SAVED
    settings, and isTyping() stops protecting a field the moment focus
    leaves it. So a number typed and then clicked away from reverted —
    and Apply, which reads the form, saved the OLD value back over it.
    Live: the stale-quote limit was set to 15 three times and stayed 5.
    """
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .trading .s-stale')

    # A value that differs from the saved one, or the assertion below
    # would pass on a form that reverted perfectly.
    was = page.input_value('.window.settings .s-stale')
    assert was != '22', 'pick a value the config does not already hold'
    page.fill('.window.settings .s-stale', '22')
    # Focus LEAVES the field — the operator clicks Apply, or tabs away,
    # or the window loses focus. isTyping() protects nothing from here.
    page.evaluate('() => document.activeElement.blur()')

    # The repaint the poll would do.
    page.evaluate('() => window.MT5Settings.render()')

    assert page.input_value('.window.settings .s-stale') == '22'

    # And the CONTROL: a field nobody touched still follows the server,
    # or this would be a form frozen against its own data.
    rendered = page.evaluate(
        '() => document.querySelector(".window.settings .s-repeg")'
        '.dataset.rendered')
    assert page.input_value('.window.settings .s-repeg') == rendered


def test_the_close_button_sits_at_the_right_of_the_title_bar(page):
    """It merely followed the title, which looks right only while the
    window is narrow enough for the title to fill the bar. The wide
    Exchanges window put it against the title, on the left."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .titlebar .winbtns')

    bar = page.evaluate(
        '() => document.querySelector(".window.settings .titlebar")'
        '.getBoundingClientRect().right')
    button = page.evaluate(
        '() => document.querySelector(".window.settings .titlebar .winbtns")'
        '.getBoundingClientRect().right')
    title = page.evaluate(
        '() => document.querySelector(".window.settings .titlebar .title")'
        '.getBoundingClientRect().right')

    # Hard against the right edge, and nowhere near the title it used to
    # sit beside.
    assert bar - button < 12, f'close button is {bar - button:.0f}px from the right'
    assert button - title > 100


def test_the_dialog_outranks_a_window_however_often_it_was_clicked(page):
    """Windows were stacked by a counter that climbed with every raise
    and never came back down. Past enough of them a window passed the
    modal, and the Delete confirmation opened BEHIND the window that
    asked for it — a question nobody can answer and nothing to dismiss.
    """
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')

    # Float the window, then raise it a day's worth of times through the
    # real drag path — raise() does nothing for a window still in the row.
    top = page.evaluate("""() => {
        const win = document.querySelector('.window.settings');
        const bar = win.querySelector('.titlebar');
        for (let i = 0; i < 300; i++) {
            bar.dispatchEvent(new PointerEvent('pointerdown',
                {bubbles: true, clientX: 200, clientY: 100}));
            document.dispatchEvent(new PointerEvent('pointermove',
                {bubbles: true, clientX: 260 + (i % 5), clientY: 160}));
            document.dispatchEvent(new PointerEvent('pointerup',
                {bubbles: true}));
        }
        return Math.max(...Array.from(document.querySelectorAll('.window'))
            .map(n => parseInt(n.style.zIndex, 10) || 0));
    }""")
    modal = page.evaluate("""() => parseInt(getComputedStyle(
        document.getElementById('modal')).zIndex, 10)""")

    assert top > 10, 'the window never actually rose; the test proves nothing'
    assert top < modal, f'a window reached z={top}, the dialog is at {modal}'


def test_the_screen_does_not_repaint_under_a_drag(page):
    """The whole screen is rebuilt three times a second. Doing that
    under the pointer is what made a window judder and lag behind the
    cursor."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .accounts')

    # Wiped by hand, so "was it rebuilt?" has an answer even when the
    # data behind it has not changed — comparing the HTML with itself
    # would pass whether the repaint ran or not.
    held = page.evaluate("""() => {
        const win = document.querySelector('.window.settings');
        const section = win.querySelector('.accounts');
        win.classList.add('dragging');
        section.innerHTML = '<!--wiped-->';
        window.MT5Settings.render();
        window.MT5Trader.render();
        const still = section.innerHTML.indexOf('wiped') >= 0;
        win.classList.remove('dragging');
        return still;
    }""")
    assert held, 'the table was rebuilt mid-drag'

    # CONTROL: with the drag over, the very same wipe is repainted away.
    repainted = page.evaluate("""() => {
        const section = document.querySelector('.window.settings .accounts');
        section.innerHTML = '<!--wiped-->';
        window.MT5Settings.render();
        return section.innerHTML.indexOf('wiped') < 0;
    }""")
    assert repainted, 'the table stopped repainting even when not dragging'


def test_an_order_held_back_from_the_broker_does_not_look_like_one_resting(page):
    """A synthetic order joins the book the instant it is clicked, but
    the real pending on the quoting leg is only placed once the guards
    are clear (quoter._rest_or_repeg holds off on stale or desynced). So
    while the feed is bad the order exists HERE and nowhere else — and
    it used to be drawn exactly like one resting at the broker. The only
    hint was W:n (broker 0) in small text in the footer, and a trader
    watched a level they believed was working while nothing of theirs
    was in the market.
    """
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr td.work', timeout=WAIT)

    def put_order(ticket):
        """One working order at a level, with its quote group either
        holding a broker ticket or not — PUBLISHED, not poked in. The
        publisher republishes every 200ms, and a snapshot edited in the
        page is gone by the next one."""
        level = page.evaluate(
            """() => window.MT5Trader.state.snapshot
                 .pairs['XAUUSD_|GC1226'].rows[5].level""")
        publisher = page.paths['publisher']
        publisher.orders = [{'order_id': 'O1', 'level': level,
                             'side': 'BUY', 'quantity': 1,
                             'filled_quantity': 0, 'state': 'WORKING'}]
        publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'BUY',
                             'level': level, 'leg': 'B', 'ticket': ticket,
                             'reason': None if ticket else
                             'the spread is stale — holding off',
                             'orders': ['O1']}]
        publisher.publish()

    # Held back: no ticket at the broker.
    put_order(None)
    page.wait_for_selector('.ladder .grid td.work.held', timeout=WAIT)
    held = page.locator('.ladder .grid td.work.held')
    assert held.count() == 1, 'a held-off order is not marked'
    assert 'NOT at the broker' in (held.first.get_attribute('title') or '')
    assert 'holding off' in (held.first.get_attribute('title') or '')

    # CONTROL: the same order, now actually resting, is NOT marked — or
    # the mark would mean nothing.
    put_order(987654)
    page.wait_for_function(
        "() => document.querySelectorAll('.ladder .grid td.work.held')"
        ".length === 0", timeout=WAIT)
    assert page.locator('.ladder .grid td.work[data-order-id]').count() == 1

    page.paths['publisher'].orders = None
    page.paths['publisher'].quotes = None
    page.paths['publisher'].publish()


def test_a_price_row_is_the_same_element_across_a_repaint(page):
    """The tbody was rebuilt with innerHTML three times a second. That
    threw away the row the pointer was over — losing :hover and the
    pressed state — and a click landing mid-replacement hit a detached
    element or whatever had just slid into that position."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr[data-level]', timeout=WAIT)

    same = page.evaluate("""() => {
        const body = document.querySelector('.ladder .grid tbody');
        const row = body.querySelector('tr[data-level]');
        const level = row.dataset.level;
        row.dataset.witness = 'marked';       // survives only if reused
        // Sizes change; the rows do not.
        const state = window.MT5Trader.state;
        const pair = state.snapshot.pairs['XAUUSD_|GC1226'];
        pair.rows.forEach((r, i) => { r.bid_size = 100 + i; });
        window.MT5Trader.render();
        const after = body.querySelector('tr[data-level="' + level + '"]');
        return after && after.dataset.witness === 'marked';
    }""")
    assert same, 'the row was destroyed and rebuilt instead of updated'


def test_the_ladder_holds_still_while_the_pointer_is_on_it(page):
    """Re-centring between a mousedown and the mouseup is how the wrong
    price gets sent."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr', timeout=WAIT)

    held = page.evaluate("""() => {
        const grid = document.querySelector('.ladder .grid');
        grid.dispatchEvent(new PointerEvent('pointerenter', {bubbles: true}));
        const before = grid.scrollTop;
        grid.scrollTop = before + 60;          // the trader looked away
        const moved = grid.scrollTop;
        const state = window.MT5Trader.state;
        state.centredAt['XAUUSD_|GC1226'] = 0;   // long overdue a centre
        window.MT5Trader.render();
        return grid.scrollTop === moved;
    }""")
    assert held, 'the ladder re-centred while the pointer was over it'


def test_a_click_sends_the_price_that_was_on_the_row(page):
    """Never the index: an index moves when the window does."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody td.ask', timeout=WAIT)
    set_convention(page, 'TT')

    cell = page.locator('.ladder .grid tbody tr[data-level] td.ask').nth(4)
    level = float(cell.evaluate('n => n.closest("tr").dataset.level'))
    before = command_count(page)
    cell.click()
    page.wait_for_timeout(300)

    assert command_count(page) == before + 1
    sent = last_command(page)
    assert sent['kind'] == 'click'
    assert sent['payload']['level'] == level, (
        f"clicked {level}, sent {sent['payload']['level']}")
    # Under the shipped TT default the asks column joins the offer,
    # which is a resting SELL. Pinned for the same reason as above: the
    # subject here is the PRICE, and the side is only along for the
    # ride, so it must not depend on which default is in force.
    assert sent['payload']['side'] == 'SELL'


def test_ticking_the_setting_OPENS_the_fair_window(page):
    """The whole path, as the trader walks it: open this ladder's
    settings, tick Fair Spread window, press Apply — and the window is
    there. Every piece of this was tested on its own and the walk
    itself was not, which is exactly where it broke."""
    open_ladder(page)
    # Start from OFF, as a fresh config does — closed HERE, because a
    # snapshot alone no longer takes a window off the desk.
    page.paths['publisher'].show_fair_window = False
    page.paths['publisher'].publish()
    page.evaluate(
        "() => window.MT5Trader.showFairWindow('XAUUSD_|GC1226', false)")
    page.wait_for_function(
        "() => !document.querySelector('.window.fairwin')", timeout=WAIT)

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-algo', timeout=WAIT)
    page.select_option('.ladder .ls-algo', 'FAIR_SPREAD')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)

    sent = page.evaluate('() => window.__sent')
    assert sent['algo_window'] is True
    # The choice is the whole decision: `algo` is derived on the engine,
    # so the form does not send one — and ALGO is never saved at all.
    assert 'algo' not in sent
    # ...and it is on the screen NOW, not a poll later and not only once
    # the engine has written the file back.
    page.wait_for_selector('.window.fairwin', timeout=WAIT)

    page.evaluate('() => { window.fetch = window.__realFetch; }')
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    # Left as it was found, for whichever test runs next.
    page.paths['publisher'].show_fair_window = True
    page.paths['publisher'].publish()


def test_the_ladder_cannot_be_made_shorter_than_its_own_rail(page):
    """The durable half of it. A window dragged short enough puts the
    rail behind a scrollbar however tight the rail is, so the window
    has a floor: it stops at the height its own controls need, and the
    LADDER scrolls instead — which is what a ladder is for."""
    measured = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const floor = parseInt(getComputedStyle(node).minHeight, 10) || 0;
        // Drag it well under the floor and read what the browser
        // actually gives back.
        node.classList.add('sized');
        node.style.height = '200px';
        const rail = node.querySelector('.rail');
        const footer = node.querySelector('.footer');
        return {floor: floor, height: node.getBoundingClientRect().height,
                railOver: rail.scrollHeight - rail.clientHeight,
                footerOver: footer.scrollHeight - footer.clientHeight};
    }""")

    assert measured['floor'] > 0, 'the ladder has no floor at all'
    assert measured['height'] >= measured['floor'] - 1, measured
    assert measured['railOver'] <= 1, measured
    assert measured['footerOver'] <= 1, measured


def test_the_ladder_is_never_cropped_sideways(page):
    """A ladder wide enough to hold its columns, whatever is done to
    it. The grid used to be a flex item at its default "never narrower
    than my content": drag the rail wider, or render the fonts larger,
    and it pushed past the window's right edge — where `.body`'s
    overflow cut the LTQ column off silently. A cropped ladder is a
    column of the market that is simply not there."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr', timeout=WAIT)

    def cropping():
        return page.evaluate("""() => {
            const node = document.querySelector('.window.ladder');
            const body = node.querySelector('.body');
            const grid = node.querySelector('.grid');
            const box = node.getBoundingClientRect();
            const cells = [...node.querySelectorAll('.grid thead th')];
            return {
                bodyOver: body.scrollWidth - body.clientWidth,
                gridRight: Math.round(
                    grid.getBoundingClientRect().right - box.right),
                clipped: cells.filter(
                    c => c.getBoundingClientRect().right > box.right + 1).length,
            };
        }""")

    assert cropping()['bodyOver'] <= 1, cropping()

    # Now shove the rail as wide as it will go — the operator drags it.
    page.evaluate("""() => {
        document.querySelector('.window.ladder .rail').style.width = '400px';
    }""")
    page.wait_for_timeout(120)
    wide = cropping()

    assert wide['bodyOver'] <= 1, wide
    assert wide['gridRight'] <= 1, wide
    assert wide['clipped'] == 0, wide
    page.evaluate("""() => {
        document.querySelector('.window.ladder .rail').style.width = '';
    }""")


def test_nothing_in_the_rail_is_clipped_at_any_rail_width(page):
    """`overflow-x: hidden` on the rail means anything too wide for it
    disappears in SILENCE — the Centre button rendered as "Ce", a
    button with half a word on it, and nothing anywhere said so.

    So this asks the rail itself: is every control inside your width?
    At the narrowest the rail can be dragged, where it is hardest.
    """
    open_ladder(page)
    page.wait_for_selector('.ladder .rail .centre-row', timeout=WAIT)

    for width in (96, 116, 160):
        over = page.evaluate("""(width) => {
            const rail = document.querySelector('.window.ladder .rail');
            rail.style.width = width + 'px';
            const box = rail.getBoundingClientRect();
            const room = rail.clientWidth;
            const bad = [];
            rail.querySelectorAll('*').forEach(function (el) {
                if (el.offsetParent === null) { return; }   // hidden
                const r = el.getBoundingClientRect();
                if (r.width > room + 1 || r.right > box.right + 1) {
                    bad.push((el.className || el.tagName) + ' ' +
                             Math.round(r.width) + '/' + room);
                }
            });
            return {bad: bad, scroll: rail.scrollWidth - rail.clientWidth};
        }""", width)

        assert over['bad'] == [], (width, over['bad'])
        assert over['scroll'] <= 1, (width, over)

    page.evaluate(
        "() => { document.querySelector('.ladder .rail').style.width = ''; }")


def test_the_desk_ends_exactly_where_the_taskbar_begins(page):
    """The desk was `calc(100vh - 30px)` against a taskbar 33px tall,
    so the last three pixels of every ladder sat behind a fixed bar —
    and what lives there is the spread row of the two legs' books."""
    fits = page.evaluate("""() => {
        const desk = document.getElementById('desktop')
            .getBoundingClientRect();
        const bar = document.getElementById('taskbar')
            .getBoundingClientRect();
        return {gap: Math.round(bar.top - desk.bottom),
                barBottom: Math.round(window.innerHeight - bar.bottom)};
    }""")

    # Flush: no overlap, and no strip of nothing between them.
    assert -1 <= fits['gap'] <= 1, fits
    assert -1 <= fits['barBottom'] <= 1, fits


def test_a_ladder_is_never_taller_than_the_desktop_it_is_on(page):
    """`min-height` beats `max-height` in CSS, so a measured floor
    larger than the screen would push the two legs' books off the
    bottom of a desktop that does not scroll vertically — gone, with no
    way to get them back."""
    open_ladder(page)
    page.wait_for_selector('.ladder .footer', timeout=WAIT)

    fits = page.evaluate("""() => {
        const desk = document.getElementById('desktop')
            .getBoundingClientRect();
        const node = document.querySelector('.window.ladder');
        const floor = parseInt(getComputedStyle(node).minHeight, 10) || 0;
        return {floor: floor, desk: Math.floor(desk.height),
                bottom: Math.round(
                    node.querySelector('.footer').getBoundingClientRect().bottom
                    - desk.bottom)};
    }""")

    assert fits['floor'] <= fits['desk'], fits
    assert fits['bottom'] <= 1, fits


def test_the_left_pane_does_not_scroll_on_a_short_window(page):
    """The trader reads the rail top to bottom while the market moves.
    A scrollbar there means the button they want is behind a scroll —
    and the two legs' books, which live under the ladder, are what got
    pushed out of the frame to make room for it."""
    page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        node.classList.add('sized');
        node.style.height = '480px';
    }""")
    page.wait_for_timeout(250)

    measured = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const rail = node.querySelector('.rail');
        const footer = node.querySelector('.footer');
        const box = node.getBoundingClientRect();
        return {railOver: rail.scrollHeight - rail.clientHeight,
                footerOver: footer.scrollHeight - footer.clientHeight,
                footerInside:
                    footer.getBoundingClientRect().bottom <= box.bottom + 1};
    }""")

    assert measured['railOver'] <= 1, measured
    assert measured['footerOver'] <= 1, measured
    assert measured['footerInside']


def test_the_fair_window_survives_being_clicked_and_dragged(page):
    """It vanished when it was moved. A window that disappears when the
    trader touches it is worse than one that never opened: they now
    have to work out whether they closed it, whether it crashed, or
    whether the setting came undone."""
    open_ladder(page)
    page.wait_for_selector('.window.fairwin .titlebar', timeout=WAIT)

    bar = page.locator('.window.fairwin .titlebar').bounding_box()
    before = page.locator('.window.fairwin').bounding_box()

    # A click on it first — focus, not a drag.
    page.mouse.click(bar['x'] + 40, bar['y'] + 8)
    page.wait_for_timeout(400)
    assert page.locator('.window.fairwin').count() == 1, \
        'clicking the title bar closed it'

    # ...and now drag it somewhere else.
    page.mouse.move(bar['x'] + 40, bar['y'] + 8)
    page.mouse.down()
    page.mouse.move(bar['x'] - 120, bar['y'] + 90, steps=10)
    page.mouse.up()
    page.wait_for_timeout(600)

    assert page.locator('.window.fairwin').count() == 1, \
        'dragging the window closed it'
    after = page.locator('.window.fairwin').bounding_box()
    assert abs(after['x'] - before['x']) > 40, (before, after)

    # And it is still there several polls later.
    page.wait_for_timeout(800)
    assert page.locator('.window.fairwin').count() == 1, \
        'the window went away on a later poll'


def test_an_engine_that_has_not_caught_up_does_not_take_the_window_away(page):
    """THE fault. Whether this window is open is a decision about this
    screen, and the engine's copy of the setting is where it STARTS,
    not who owns it. Owned by the snapshot, a poll arriving before the
    save has landed — or with the engine down, or restarted, or writing
    a config the web process is not reading — takes the window away
    under the trader's hands, a third of a second after they opened
    it."""
    open_ladder(page)
    page.wait_for_selector('.window.fairwin', timeout=WAIT)

    # The engine says nothing of the sort, poll after poll.
    page.paths['publisher'].show_fair_window = False
    page.paths['publisher'].publish()
    page.wait_for_timeout(900)

    assert page.locator('.window.fairwin').count() == 1, \
        'a snapshot without the setting closed a window the trader opened'

    # Closing it by hand still closes it, and it STAYS closed.
    page.click('.window.fairwin .close')
    page.wait_for_function(
        "() => !document.querySelector('.window.fairwin')", timeout=WAIT)
    page.paths['publisher'].show_fair_window = True
    page.paths['publisher'].publish()
    page.wait_for_timeout(700)
    assert page.locator('.window.fairwin').count() == 0, \
        'a closed window came back on the next poll'

    # ...until it is asked for again.
    page.evaluate("""() => {
        const UI = window.MT5Trader;
        UI.showFairWindow('XAUUSD_|GC1226', true);
    }""")
    page.wait_for_selector('.window.fairwin', timeout=WAIT)


def test_a_stalled_engine_is_not_called_a_dead_one(page):
    """Two different faults with two different fixes: a coordinator
    that has stopped publishing while this page still reads its last
    file, against no file at all. Both said ENGINE DOWN — and it is the
    first one where every price on the screen is a photograph, which is
    the more dangerous of the two to misread."""
    open_ladder(page)

    # A snapshot from ten minutes ago: the file is there, the engine is
    # not running.
    page.paths['publisher'].live.clear()
    page.paths['publisher'].publish(at=time.time() - 600)
    page.wait_for_function(
        "() => document.getElementById('link-badge')"
        ".textContent.indexOf('STALLED') >= 0", timeout=WAIT)

    detail = page.get_attribute('#link-badge', 'title') or ''
    assert 's old' in detail and 'Nothing on this screen is live' in detail

    page.paths['publisher'].live.set()
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.getElementById('link-badge')"
        ".textContent.indexOf('STALLED') < 0", timeout=WAIT)


def slow_pane_reads(page, ms=1200):
    """Make the two reads that FILL the ladder settings pane arrive
    late, as a loaded desk does.

    The pane is shown at once and filled when /api/pairs and
    /api/settings come back. On this container those land in a few
    milliseconds and the gap is invisible; on a desk running four MT5
    terminals it is not. Widening it on purpose is the only way to test
    what happens inside it.
    """
    page.evaluate("""(ms) => {
        if (!window.__slowReal) { window.__slowReal = window.fetch; }
        const real = window.__slowReal;
        window.fetch = function (url, options) {
            const u = String(url);
            if (u.indexOf('/api/pairs') < 0 && u.indexOf('/api/settings') < 0) {
                return real(url, options);
            }
            return new Promise(function (done) {
                setTimeout(function () { done(real(url, options)); }, ms);
            });
        };
    }""", ms)


def restore_pane_reads(page):
    page.evaluate("() => { if (window.__slowReal) "
                  "{ window.fetch = window.__slowReal; } }")


def test_a_slow_read_does_not_revert_what_the_operator_just_chose(page):
    """The pane opens INSTANTLY and fills when two reads come back, and
    that fill used to write over every field unconditionally.

    An operator quick enough to change something in the gap had their
    choice silently reverted — and Apply then saved the OLD value,
    because the box no longer said what they had picked. Pair type is
    the worst one to lose: it decides which legs carry an expiry and
    whether there is a fair spread to quote at all.

    Found as a flaky test rather than as a bug report: on a loaded CI
    runner the reads landed after the click and the assertion below
    failed, which is the same thing happening to a trader on a busy
    desk.
    """
    slow_pane_reads(page)
    try:
        open_ladder(page)
        page.click('.ladder .ladder-cog')
        page.wait_for_selector('.ladder .ls-pair-type', timeout=WAIT)

        page.select_option('.ladder .ls-pair-type', 'FUTURE_FUTURE')
        assert page.input_value('.ladder .ls-pair-type') == 'FUTURE_FUTURE'

        # Let the late reads land ON TOP of the choice.
        page.wait_for_timeout(2500)

        assert page.input_value('.ladder .ls-pair-type') == 'FUTURE_FUTURE', (
            'the late read reverted the pair type the operator chose')
        # ...and the rows that follow the pair type followed the
        # OPERATOR, not the file: a note saying one thing over a
        # control saying another is how the wrong value gets saved.
        assert 'two futures' in page.text_content('.ladder .ls-pairtype')
        assert page.locator('.ladder .ls-expiry-a').is_enabled()
    finally:
        page.click('.ladder .ls-close')
        restore_pane_reads(page)


def test_control_a_field_nobody_touched_is_still_filled_from_the_file(page):
    """The control. "Do not overwrite what the operator changed" must
    not become "do not fill the form at all" — a pane that opens empty
    would be a far worse bug than the one being fixed, and every box
    here is a real number: a commission, a slippage allowance, a
    target. So: same slow reads, nothing touched, and the values still
    arrive."""
    slow_pane_reads(page)
    try:
        open_ladder(page)
        page.click('.ladder .ladder-cog')
        page.wait_for_selector('.ladder .ls-pair-type', timeout=WAIT)
        # Touch NOTHING. Wait for the same late reads.
        page.wait_for_timeout(2500)

        assert page.input_value('.ladder .ls-pair-type') != '', (
            'the pane never filled — the guard is swallowing the fill')
        assert page.input_value('.ladder .ls-slip') != ''
        assert page.text_content('.ladder .ls-pairtype') != ''
    finally:
        page.click('.ladder .ls-close')
        restore_pane_reads(page)


def test_the_pair_type_is_declared_and_the_expiries_follow_it(page):
    """Two futures with two expiries are NOT a calendar. UKOILV6
    against USOILV6 is Brent against WTI: two different instruments
    with no carry between them, and nothing in an expiry says whether
    two contracts share an underlying. So the operator declares it —
    and the form follows the declaration as it is changed, not only
    when it opens."""
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-pair-type', timeout=WAIT)

    page.select_option('.ladder .ls-pair-type', 'FUTURE_FUTURE')
    page.wait_for_function(
        "() => document.querySelector('.ladder .ls-pairtype')"
        ".textContent.indexOf('two futures') >= 0", timeout=WAIT)
    assert page.locator('.ladder .ls-expiry-a').is_enabled()
    assert 'NEAR' in page.text_content('.ladder .ls-pairtype')

    page.select_option('.ladder .ls-pair-type', 'RELATED')
    page.wait_for_function(
        "() => document.querySelector('.ladder .ls-pairtype')"
        ".textContent.indexOf('no fair spread') >= 0", timeout=WAIT)
    assert page.locator('.ladder .ls-expiry-a').is_disabled()
    assert page.locator('.ladder .ls-expiry').is_disabled()

    page.select_option('.ladder .ls-pair-type', 'SPOT_FUTURE')
    page.wait_for_function(
        "() => document.querySelector('.ladder .ls-pairtype')"
        ".textContent.indexOf('only leg B') >= 0", timeout=WAIT)
    page.click('.ladder .ls-close')


# -- the order-execution pass ---------------------------------------------

def test_clicking_a_working_level_ADDS_one_instead_of_pulling_it(page):
    """A trader who clicks a level they are already working wants MORE
    of it. Pulling was the wrong default, and the toast that came back
    ('resting at … to close position') read as an error on an ordinary
    click.

    Right-click is the pull, and it is in the tooltip.
    """
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr td.work', timeout=WAIT)
    publisher = page.paths['publisher']
    level = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows[5].level""")
    publisher.orders = [{'order_id': 'O9', 'level': level, 'side': 'SELL',
                         'quantity': 1, 'filled_quantity': 0,
                         'state': 'WORKING'}]
    publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'SELL',
                         'level': level, 'leg': 'B', 'ticket': 5150,
                         'price': 4660.4, 'symbol': 'GC1226',
                         'crosses_leg': 'A', 'orders': ['O9']}]
    publisher.publish()
    page.wait_for_selector('.ladder .grid td.work[data-order-id]',
                           timeout=WAIT)

    cell = page.locator('.ladder .grid td.work[data-order-id]').first
    title = cell.get_attribute('title') or ''
    assert 'Click to add another' in title
    assert 'right-click to pull one' in title

    before = command_count(page)
    cell.click()
    page.wait_for_timeout(250)
    command = last_command(page)
    assert command_count(page) == before + 1
    # ONE MORE ORDER at that level, on the side already resting there —
    # not a cancel, and not the other side.
    assert command['kind'] == 'click'
    assert command['payload']['side'] == 'SELL'
    assert abs(command['payload']['level'] - level) < 1e-9

    # The control: the pull still exists, on the right button.
    page.locator('.ladder .grid td.work[data-order-id]').first.click(
        button='right')
    page.wait_for_timeout(250)
    pull = last_command(page)
    assert pull['kind'] == 'cancel_order'
    assert pull['payload']['order_id'] == 'O9'

    publisher.orders = None
    publisher.quotes = None
    publisher.publish()


def test_the_ladder_says_which_leg_the_real_pending_rests_on(page):
    """LIMIT quotes ONE leg and crosses the other at market on the
    fill. Leg A showing no order at the broker is the mode working —
    and nothing on the screen said so, so it read as leg A having
    failed to place."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.order_type = 'LIMIT'
    publisher.quoting_leg = 'b'
    publisher.publish()
    page.wait_for_function(
        "() => document.querySelector('.ladder .quoting-note')"
        ".textContent.indexOf('leg B') >= 0", timeout=WAIT)
    note = page.text_content('.ladder .quoting-note')
    assert 'GC1226' in note                       # the symbol, by name
    assert 'leg A crosses on fill' in note        # ...and what leg A does

    # The control: in MARKET mode neither leg rests anything, and the
    # note must not go on naming a quoting leg.
    publisher.order_type = 'MARKET'
    publisher.publish()
    page.wait_for_function(
        "() => document.querySelector('.ladder .quoting-note')"
        ".textContent.indexOf('both legs') >= 0", timeout=WAIT)
    publisher.order_type = 'LIMIT'
    publisher.publish()


def test_MARKET_mode_carries_the_M_over_every_price_cell(page):
    """The mode is what the M names, and the mode does not change
    halfway down the ladder. It used to appear on the two touch rows
    only, so nine rows in ten the cursor said 'cell' while the badge
    said MARKET."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.order_type = 'MARKET'
    publisher.publish()
    page.wait_for_selector('.window.mode-market', timeout=WAIT)

    def cursor(nth):
        return page.eval_on_selector_all(
            '.ladder .grid tbody tr td.bid',
            "(cells, n) => getComputedStyle(cells[n]).cursor", nth)

    assert 'svg+xml' in cursor(1), 'no M away from the touch'
    assert 'svg+xml' in cursor(15)

    # The control: LIMIT mode carries no M anywhere.
    publisher.order_type = 'LIMIT'
    publisher.publish()
    page.wait_for_function(
        "() => !document.querySelector('.window.mode-market')", timeout=WAIT)
    assert 'svg+xml' not in cursor(1)


def test_the_three_cancels_are_labelled_as_what_they_act_on(page):
    """S / B / CXL All are the WORKING ORDER controls. Unlabelled, three
    initials sat under a column of size boxes and read as sides."""
    open_ladder(page)
    label = page.evaluate("""() => {
        const row = document.querySelector('.ladder .cxl-row');
        const previous = row.previousElementSibling;
        return previous ? previous.textContent.trim() : null;
    }""")
    assert label == 'Working orders'


def test_a_position_can_be_closed_at_a_PRICE_not_only_at_the_market(page):
    """CLOSE ALL crosses now. This rests one working order per position
    at a level the trader names — and it asks first, because it is an
    exit that WAITS and a trader who thought it crossed is a trader
    holding a position they believe is closed."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.net_position = 2.0
    publisher.publish()
    page.wait_for_function(
        "() => (window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
        " || {}).net_position === 2", timeout=WAIT)

    before = command_count(page)
    page.fill('.ladder .close-limit', '59.40')
    page.click('.ladder .close-limit-go')
    page.wait_for_selector('#modal:not(.hidden)')
    assert command_count(page) == before, 'it sent before it asked'
    body = page.text_content('#modal')
    assert 'WAITS' in body and 'no stop' in body
    page.click('#modal-confirm')
    page.wait_for_timeout(250)

    command = last_command(page)
    assert command['kind'] == 'close_at_limit'
    assert command['payload']['level'] == 59.40
    assert command['payload']['pair'] == 'XAUUSD_|GC1226'

    publisher.net_position = 0.0
    publisher.publish()


def test_closing_at_a_price_is_refused_on_a_flat_ladder(page):
    """The control: nothing to close is not an order to place."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.net_position = 0.0
    publisher.publish()
    page.wait_for_function(
        "() => (window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
        " || {}).net_position === 0", timeout=WAIT)

    before = command_count(page)
    page.fill('.ladder .close-limit', '59.40')
    page.click('.ladder .close-limit-go')
    page.wait_for_selector('#toasts .toast', timeout=WAIT)
    assert 'already flat' in page.text_content('#toasts .toast')
    assert command_count(page) == before
    # An error toast stays until it is dismissed, and this page is
    # shared with every other test in the file.
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")


def click_side_at(page, column, convention, index=4):
    """Click a bids/asks cell and return (side, level) actually sent.

    The convention is set immediately before the click: the 300ms poll
    replaces state.snapshot wholesale, and the fixture's status.json
    carries no click_convention, so a value injected earlier is gone by
    the time the next click lands. The live engine writes it into every
    snapshot, so this is a harness detail only.
    """
    set_convention(page, convention)
    cell = page.locator(
        '.ladder .grid tbody tr[data-level] td.' + column).nth(index)
    level = float(cell.evaluate('n => n.closest("tr").dataset.level'))
    before = command_count(page)
    cell.click()
    page.wait_for_timeout(300)
    assert command_count(page) == before + 1, 'no order was sent'
    sent = last_command(page)
    return sent['payload']['side'], sent['payload']['level'], level


def set_convention(page, value):
    """Make EVERY poll carry the convention.

    Setting it on state.snapshot alone loses the race: /api/status
    arrives three times a second and replaces the snapshot wholesale,
    so the value can be gone before the click lands. The live engine
    puts it in every snapshot, so stamping it onto the response is the
    faithful thing to simulate.
    """
    page.evaluate("""(v) => {
        window.__conv = v;
        // No one-time guard: other tests in this file swap window.fetch
        // for stubs of their own and restore it afterwards, which can
        // drop this patch. Reinstalling from whatever fetch is current
        // survives that in any order.
        if (!window.__convReal) { window.__convReal = window.fetch; }
        const real = window.__convReal;
        window.fetch = function (url, options) {
            const answer = real(url, options);
            if (String(url).indexOf('/api/status') < 0) { return answer; }
            return answer.then(r => r.json()).then(body => {
                body.click_convention = window.__conv;
                return new Response(JSON.stringify(body), {
                    status: 200,
                    headers: {'Content-Type': 'application/json'}});
            });
        };
    }""", value)
    # One full poll, so the patched response is the one on screen.
    page.wait_for_timeout(400)


def test_the_tt_convention_buys_from_the_bids_column(page):
    """TT's price ladder: clicking BIDS joins the bid, which is a
    resting BUY. This app read the other way, so every click a trader
    arriving from TT made was the opposite of the one they meant."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody td.bid', timeout=5000)
    set_convention(page, 'TT')

    side, sent_level, clicked = click_side_at(page, 'bid', 'TT')
    assert side == 'BUY'
    assert sent_level == clicked, 'the price moved between click and send'

    side, sent_level, clicked = click_side_at(page, 'ask', 'TT')
    assert side == 'SELL'
    assert sent_level == clicked


def test_the_touch_convention_is_the_other_way_round(page):
    """The CONTROL. A setting that sent the same side either way would
    be a switch that does nothing."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody td.bid', timeout=5000)
    set_convention(page, 'TOUCH')

    side, _, _ = click_side_at(page, 'bid', 'TOUCH')
    assert side == 'SELL'
    side, _, _ = click_side_at(page, 'ask', 'TOUCH')
    assert side == 'BUY'


def test_the_tooltip_says_what_the_click_will_do(page):
    """A tooltip promising BUY while the click sends SELL is worse than
    no tooltip at all."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody td.bid', timeout=5000)

    for convention, on_bid, on_ask in (('TT', 'BUY', 'SELL'),
                                       ('TOUCH', 'SELL', 'BUY')):
        set_convention(page, convention)
        bid = page.get_attribute('.ladder .grid tbody td.bid', 'title')
        ask = page.get_attribute('.ladder .grid tbody td.ask', 'title')
        assert bid.startswith('Click: ' + on_bid), (convention, bid)
        assert ask.startswith('Click: ' + on_ask), (convention, ask)
        # And the leg wording follows the side, not the column.
        assert ('buy leg B' in bid) == (on_bid == 'BUY')


def test_the_buy_and_sell_buttons_ignore_the_convention(page):
    """They name their own side. If the switch moved them too, the one
    unambiguous way to get in would become ambiguous."""
    open_ladder(page)
    page.wait_for_selector('.ladder .buy-touch', timeout=5000)

    sides = {}
    for convention in ('TT', 'TOUCH'):
        set_convention(page, convention)
        before = command_count(page)
        set_convention(page, convention)
        page.click('.ladder .buy-touch')
        page.wait_for_timeout(300)
        assert command_count(page) == before + 1
        sides[convention] = last_command(page)['payload']['side']

    assert sides == {'TT': 'BUY', 'TOUCH': 'BUY'}, sides


def test_the_lots_one_spread_costs_can_be_typed_and_says_what_is_in_force(
        page):
    """It was derived and shown nowhere: the smallest size at which
    both legs clear their own minimum volume, 0.01 on this desk's
    broker, with no field anywhere to change it. Leg B stays derived —
    it is the hedge, and a box for it is how a hedged pair ends up net
    long one leg."""
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-clip-a', timeout=WAIT)

    # BOTH legs have a box, and each holds what the engine is running
    # — never blank. A blank box MEANS 1 now, so an empty one beside a
    # ladder trading 0.10 would send 1 on the next Apply: a ten-fold
    # resize nobody typed.
    assert page.input_value('.ladder .ls-clip-a') == '0.1'
    assert page.input_value('.ladder .ls-clip-b') == '0.1'
    # ...and the presets that used to derive leg B are gone.
    assert page.locator('.ladder .ls-sizing-basis').count() == 0

    note = ' '.join(page.text_content('.ladder .ls-clip-note').split())
    # PER QTY and FOR THE QTY IN THE BOX, side by side.
    assert '1 Qty = 0.10 lots XAUUSD_ = 10 units' in note
    assert '0.10 lots GC1226 = 10 units' in note
    assert 'per 1.00 of spread' in note
    assert 'Qty 1.00 sends 0.10 / 0.10 lots' in note
    # The contract size is MT5's, never the trader's.
    assert 'come from OKX and are never typed in' in note

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.fill('.ladder .ls-clip-a', '1')
    page.fill('.ladder .ls-clip-b', '2')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
    assert page.evaluate('() => window.__sent.clip_lots_a') == 1
    # Leg B is TYPED, not derived from leg A — a ratio of 1:2 reaches
    # the engine exactly as it was entered.
    assert page.evaluate('() => window.__sent.clip_lots_b') == 2
    page.evaluate("() => { window.fetch = window.__realFetch; "
                  "document.getElementById('toasts').innerHTML = ''; }")


def test_the_default_way_out_is_shown_on_the_button_F_will_press(page):
    """Two ways out, and F fires one of them. Which one is the ladder's
    OWN setting, so it is said on the buttons rather than remembered —
    and both stay on the screen either way: the way out must not change
    meaning under the button pressed in a hurry."""
    open_ladder(page)
    publisher = page.paths['publisher']

    publisher.exit_type = 'MARKET'
    publisher.publish()
    page.wait_for_selector('.ladder .flatten.primary-exit', timeout=WAIT)
    assert page.locator('.ladder .close-limit-go.primary-exit').count() == 0
    assert '(F)' in (page.get_attribute('.ladder .flatten', 'title') or '')

    publisher.exit_type = 'LIMIT'
    publisher.publish()
    page.wait_for_selector('.ladder .close-limit-go.primary-exit',
                           timeout=WAIT)
    assert page.locator('.ladder .flatten.primary-exit').count() == 0
    assert '(F)' in (page.get_attribute('.ladder .close-limit-go',
                                        'title') or '')
    # CLOSE ALL is still there, and still says it crosses NOW.
    assert page.locator('.ladder .flatten').count() == 1
    assert 'NOW' in (page.get_attribute('.ladder .flatten', 'title') or '')

    publisher.exit_type = 'MARKET'
    publisher.publish()


def test_the_exit_type_is_set_where_the_entry_mode_s_twin_is(page):
    """In the ladder's own settings, beside the exit fields it governs
    — not on the rail, which has no row to spare."""
    open_ladder(page)
    # The page polls; the pane seeds from the snapshot it HAS, so wait
    # for the engine's default to have arrived before reading it.
    page.wait_for_function(
        "() => window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
        ".exit_type === 'MARKET'", timeout=WAIT)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-exit-type', timeout=WAIT)
    assert page.locator(
        '.ladder .ls-group:has(.ls-overnight) .ls-exit-type').count() == 1
    assert page.input_value('.ladder .ls-exit-type') == 'MARKET'

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.select_option('.ladder .ls-exit-type', 'LIMIT')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
    assert page.evaluate('() => window.__sent.exit_type') == 'LIMIT'
    page.evaluate("() => { window.fetch = window.__realFetch; "
                  "document.getElementById('toasts').innerHTML = ''; }")


def test_the_default_way_out_is_shown_on_the_button_F_will_press(page):
    """Two ways out, and F fires one of them. Which one is the ladder's
    OWN setting, so it is said on the buttons rather than remembered —
    and both stay on the screen either way: the way out must not change
    meaning under the button pressed in a hurry."""
    open_ladder(page)
    publisher = page.paths['publisher']

    publisher.exit_type = 'MARKET'
    publisher.publish()
    page.wait_for_selector('.ladder .flatten.primary-exit', timeout=WAIT)
    assert page.locator('.ladder .close-limit-go.primary-exit').count() == 0
    assert '(F)' in (page.get_attribute('.ladder .flatten', 'title') or '')

    publisher.exit_type = 'LIMIT'
    publisher.publish()
    page.wait_for_selector('.ladder .close-limit-go.primary-exit',
                           timeout=WAIT)
    assert page.locator('.ladder .flatten.primary-exit').count() == 0
    assert '(F)' in (page.get_attribute('.ladder .close-limit-go',
                                        'title') or '')
    # CLOSE ALL is still there, and still says it crosses NOW.
    assert page.locator('.ladder .flatten').count() == 1
    assert 'NOW' in (page.get_attribute('.ladder .flatten', 'title') or '')

    publisher.exit_type = 'MARKET'
    publisher.publish()


def test_the_exit_type_is_set_where_the_entry_mode_s_twin_is(page):
    """In the ladder's own settings, beside the exit fields it governs
    — not on the rail, which has no row to spare."""
    open_ladder(page)
    # The page polls; the pane seeds from the snapshot it HAS, so wait
    # for the engine's default to have arrived before reading it.
    page.wait_for_function(
        "() => window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
        ".exit_type === 'MARKET'", timeout=WAIT)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-exit-type', timeout=WAIT)
    assert page.locator(
        '.ladder .ls-group:has(.ls-overnight) .ls-exit-type').count() == 1
    assert page.input_value('.ladder .ls-exit-type') == 'MARKET'

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.select_option('.ladder .ls-exit-type', 'LIMIT')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
    assert page.evaluate('() => window.__sent.exit_type') == 'LIMIT'
    page.evaluate("() => { window.fetch = window.__realFetch; "
                  "document.getElementById('toasts').innerHTML = ''; }")


def test_a_resting_CLOSE_is_not_reported_as_an_order_that_failed_to_place(
        page):
    """A closing order puts NOTHING at the broker, by design: MT5
    ignores `position` on a pending, so the level is held here and both
    legs are closed by ticket when the market reaches it.

    Counted against the broker's own number it read as an order that
    had failed to place — amber in the Work cell and W:3 (broker 1) in
    red in the footer, permanently, on every reducing click.
    """
    open_ladder(page)
    publisher = page.paths['publisher']
    level = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows[5].level""")
    # One ordinary working order AT the broker, and one resting close
    # that never will be.
    publisher.orders = [
        {'order_id': 'OPEN1', 'level': level, 'side': 'SELL', 'quantity': 1,
         'filled_quantity': 0, 'state': 'WORKING'},
        {'order_id': 'CLOSE1', 'level': level - 0.05, 'side': 'BUY',
         'quantity': 2, 'filled_quantity': 0, 'state': 'WORKING',
         'position_id': 'POS1'}]
    publisher.quotes = [
        {'pair_key': 'XAUUSD_|GC1226', 'side': 'SELL', 'level': level,
         'leg': 'B', 'ticket': 5150, 'price': 4660.4, 'intent': 'OPEN',
         'orders': ['OPEN1']},
        {'pair_key': 'XAUUSD_|GC1226', 'side': 'BUY', 'level': level - 0.05,
         'leg': 'B', 'ticket': None, 'intent': 'CLOSE',
         'position_id': 'POS1', 'orders': ['CLOSE1']}]
    publisher.working_buys = 1
    publisher.working_sells = 1
    publisher.resting_closes = 1
    publisher.broker_pendings = 1
    publisher.publish()

    page.wait_for_function(
        "() => document.querySelectorAll('.ladder .grid td.work"
        "[data-order-id]').length === 2", timeout=WAIT)

    # NOT amber, and not "NOT at the broker": it is the mode working.
    assert page.locator('.ladder .grid td.work.held').count() == 0
    # ...and the footer says so in words instead of flagging a mismatch.
    counts = page.text_content('.ladder .counts .cnt-w')
    assert 'W:2' in counts and '1 resting close' in counts
    assert 'broker' not in counts
    assert page.locator('.ladder .counts .cnt-w.mismatch').count() == 0

    # THE CONTROL: a genuine held-off OPENING order is still flagged.
    publisher.quotes[0]['ticket'] = None
    publisher.quotes[0]['reason'] = 'the spread is stale — holding off'
    publisher.broker_pendings = 0
    publisher.publish()
    page.wait_for_selector('.ladder .grid td.work.held', timeout=WAIT)
    assert page.locator('.ladder .grid td.work.held').count() == 1
    page.wait_for_selector('.ladder .counts .cnt-w.mismatch', timeout=WAIT)

    publisher.orders = None
    publisher.quotes = None
    publisher.working_buys = 0
    publisher.working_sells = 0
    publisher.resting_closes = 0
    publisher.broker_pendings = None
    publisher.publish()


def test_an_ordinary_OUTCOME_is_not_toasted_as_an_error(page):
    """An error toast stays on the screen until it is dismissed, on
    purpose. "resting at 7.69 to CLOSE 2 position(s)" went out in that
    style, so a click that did exactly what it was asked read as a
    fault and sat there until somebody clicked it away."""
    open_ladder(page)
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.evaluate("""() => window.MT5Trader.toastOutcome(
        {ok: true, data: {reason: 'resting at 7.69 to CLOSE 2 position(s)'}})
    """)
    page.wait_for_selector('#toasts .toast', timeout=WAIT)
    assert page.locator('#toasts .toast.ok').count() == 1

    # THE CONTROL: a refusal still stays, in the style that stays.
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.evaluate("""() => window.MT5Trader.toastOutcome(
        {ok: true, data: {refused: true,
                          reason: '10027 AutoTrading disabled by client'}})
    """)
    page.wait_for_selector('#toasts .toast', timeout=WAIT)
    assert page.locator('#toasts .toast.ok').count() == 0
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")


def test_the_system_says_its_own_name_top_left(page):
    """It is called Nexus, and a screen that a desk sits in front of all
    day should say what it is. Top left, where a name belongs."""
    assert page.title() == 'Nexus'

    brand = page.locator('#brand')
    assert brand.count() == 1
    assert 'NEXUS' in brand.text_content()
    # The mark is INLINE, not a second request: a logo that arrives a
    # round trip after the page is a logo that flickers on every
    # reload, and everything here is self-hosted anyway.
    assert brand.locator('svg.brand-mark').count() == 1

    # TOP LEFT, and above the desk rather than over it: a mark sitting
    # on the first ladder's title bar hides the pair name.
    box = brand.bounding_box()
    desk = page.locator('#desktop').bounding_box()
    assert box['x'] <= 2 and box['y'] <= 2
    assert box['y'] + box['height'] <= desk['y'] + 1

    # And it costs ONE title-bar height. Every pixel here is a pixel
    # off the ladders, so this is the number to defend.
    assert box['height'] <= 22, box


def test_the_name_never_pushes_the_desk_off_the_screen(page):
    """The desk still ends exactly where the taskbar begins — the brand
    takes its height out of the desk, not out of the viewport."""
    measured = page.evaluate("""() => {
        const brand = document.getElementById('brand').getBoundingClientRect();
        const desk = document.getElementById('desktop').getBoundingClientRect();
        const bar = document.getElementById('taskbar').getBoundingClientRect();
        return {brandBottom: brand.bottom, deskTop: desk.top,
                deskBottom: desk.bottom, barTop: bar.top,
                barBottom: bar.bottom, viewport: window.innerHeight};
    }""")
    assert measured['deskTop'] == pytest.approx(measured['brandBottom'], abs=1)
    assert measured['deskBottom'] == pytest.approx(measured['barTop'], abs=1)
    assert measured['barBottom'] == pytest.approx(measured['viewport'], abs=1)


def test_what_a_reducing_click_still_OWES_is_on_the_screen(page):
    """A click bigger than the position it covers holds the rest back
    until the close has gone through — one instruction, in order.

    From the desk's screen: short 1707, SELL 100 clicked at 8.11 over a
    93-lot long. The Work cell read 93 and the other 7 existed only
    inside the engine. Nothing on the screen said 7 more were coming,
    what they were waiting for, or — if something else closed that
    position first — that they had been dropped.
    """
    open_ladder(page)
    publisher = page.paths['publisher']
    level = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows[5].level""")
    publisher.orders = [{'order_id': 'C1', 'level': level, 'side': 'SELL',
                         'quantity': 93, 'filled_quantity': 0,
                         'state': 'WORKING', 'position_id': 'POS1',
                         'time_in_force': 'DAY'}]
    publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'SELL',
                         'level': level, 'leg': 'BOTH', 'ticket': None,
                         'intent': 'CLOSE', 'position_id': 'POS1',
                         'open_after': 7, 'orders': ['C1']}]
    publisher.working_sells = 1
    publisher.resting_closes = 1
    publisher.broker_pendings = 0
    publisher.publish()

    page.wait_for_selector('.ladder .grid td.work .after', timeout=WAIT)
    cell = page.locator('.ladder .grid td.work[data-order-id]').first
    # BOTH numbers: what is working, and what the click still owes.
    assert '93' in cell.text_content() and '+7' in cell.text_content()
    after = page.locator('.ladder .grid td.work .after').first
    assert 'held back until this close goes through' in \
        (after.get_attribute('title') or '')

    # THE CONTROL: an ordinary order owes nothing, and must not sprout
    # a marker that means "there is more coming".
    publisher.quotes[0]['open_after'] = 0
    publisher.publish()
    page.wait_for_function(
        "() => document.querySelectorAll('.ladder .grid td.work .after')"
        ".length === 0", timeout=WAIT)

    publisher.orders = None
    publisher.quotes = None
    publisher.working_sells = 0
    publisher.resting_closes = 0
    publisher.broker_pendings = None
    publisher.publish()


def test_an_order_the_TRADER_withdrew_is_not_dressed_as_a_refusal(page):
    """"SELL 93 at 8.09 — cancelled. the trader clicked 8.11 to close
    this instead" is the system doing exactly as it was told. It went
    out in the same style as a broker refusal — the alarm style, which
    stays on the screen until it is dismissed — and a desk that reads
    every notice as an alarm stops reading them.

    A REJECTED order still is one, and still stays.
    """
    open_ladder(page)
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    publisher = page.paths['publisher']
    publisher.dead_orders = [
        {'order_id': 'X1', 'side': 'SELL', 'quantity': 93, 'level': 8.09,
         'state': 'CANCELLED',
         'reason': 'the trader clicked 8.11 to close this instead'}]
    publisher.publish()
    page.wait_for_selector('#toasts .toast', timeout=WAIT)
    assert page.locator('#toasts .toast.ok').count() == 1
    assert 'clicked 8.11' in page.text_content('#toasts .toast')

    # THE CONTROL: the broker refusing one is a fault, and stays.
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    publisher.dead_orders = [
        {'order_id': 'X2', 'side': 'SELL', 'quantity': 1, 'level': 8.09,
         'state': 'REJECTED',
         'reason': '10027 AutoTrading disabled by client'}]
    publisher.publish()
    page.wait_for_selector('#toasts .toast', timeout=WAIT)
    assert page.locator('#toasts .toast.ok').count() == 0
    assert '10027' in page.text_content('#toasts .toast')

    publisher.dead_orders = None
    publisher.publish()
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")


def test_why_an_opposite_click_closes_something_is_on_the_settings_page(page):
    """The first time a click the other way CLOSES a ticket instead of
    stacking one, it is a surprise — and on a mixed book a click that
    ADDS to the net still clears an opposite ticket, which is a bigger
    surprise. There was no switch and no sentence about it anywhere."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .trading-fields', timeout=WAIT)

    box = page.locator('.window.settings .s-closefirst')
    assert box.count() == 1
    assert box.is_checked(), 'reduce-before-open must be the default'
    text = page.text_content('.window.settings .trading-fields')
    assert 'reduce before opening' in text
    # ...and it says WHY, in the terms that make it matter here.
    assert 'long/short mode' in text and 'never nets' in text
    # ...and what it costs, which is the part a trader has to weigh.
    assert 'CROSSES when the level prints' in text

    page.click('.window.settings .close')
    page.wait_for_selector('.ladder .grid tbody tr')


def test_an_order_that_reached_NEITHER_broker_says_so_without_hovering(page):
    """An entry whose pending was never placed exists in this process
    and nowhere else — and the ladder draws it exactly like one that is
    resting. The reason was in the Work cell's TOOLTIP, which nobody
    hovers when the thing they are looking at appears to be working,
    and in "W:1 (broker 0)" at 9px in the footer. Three separate
    sessions were spent asking why an order was not in MT5."""
    open_ladder(page)
    publisher = page.paths['publisher']
    level = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows[5].level""")
    publisher.orders = [{'order_id': 'O1', 'level': level, 'side': 'SELL',
                         'quantity': 100, 'filled_quantity': 0,
                         'state': 'WORKING', 'time_in_force': 'DAY'}]
    publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'SELL',
                         'level': level, 'leg': 'B', 'ticket': None,
                         'intent': 'OPEN',
                         'reason': 'the hedge for 1 lots on leg A is under '
                                   "leg B's 0.1-lot minimum",
                         'orders': ['O1']}]
    publisher.working_sells = 1
    publisher.broker_pendings = 0
    publisher.publish()

    page.wait_for_selector('.ladder .quoting-note.held', timeout=WAIT)
    note = page.text_content('.ladder .quoting-note')
    assert 'NOT AT THE BROKER' in note
    # ...in the ENGINE'S OWN WORDS, never "check the log".
    assert "under leg B's 0.1-lot minimum" in note

    # THE CONTROL: once it rests, the line goes back to saying which
    # leg quotes — and does NOT cry wolf.
    publisher.quotes[0]['ticket'] = 5150
    publisher.quotes[0]['reason'] = None
    publisher.broker_pendings = 1
    publisher.publish()
    page.wait_for_function(
        "() => !document.querySelector('.ladder .quoting-note.held')",
        timeout=WAIT)
    assert 'LIMIT: quotes leg B' in page.text_content('.ladder .quoting-note')

    # A resting CLOSE has no pending by design and must never light it.
    publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'BUY',
                         'level': level, 'leg': 'BOTH', 'ticket': None,
                         'intent': 'CLOSE', 'position_id': 'P1',
                         'orders': ['O1']}]
    publisher.publish()
    page.wait_for_timeout(400)
    assert page.locator('.ladder .quoting-note.held').count() == 0

    publisher.orders = None
    publisher.quotes = None
    publisher.working_sells = 0
    publisher.broker_pendings = None
    publisher.publish()


def test_an_order_that_never_reached_the_broker_says_so_across_the_row(page):
    """The reason has always been in the Working orders table — in the
    ELEVENTH column, off the right edge of any normal window, behind a
    sideways scroll nobody makes. So an order that had never been
    placed read exactly like one that was working, and the desk spent
    several sessions asking why MT5 showed nothing.

    Its own row, spanning the table, where no scrolling can hide it.
    """
    open_ladder(page)
    publisher = page.paths['publisher']
    level = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows[5].level""")
    publisher.orders = [{'order_id': 'O1', 'level': level, 'side': 'SELL',
                         'quantity': 100, 'filled_quantity': 0,
                         'state': 'WORKING', 'time_in_force': 'DAY'}]
    publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'SELL',
                         'level': level, 'leg': 'B', 'ticket': None,
                         'intent': 'OPEN', 'price': None,
                         'reason': '100 x 1 is 100 lots on leg B, over the '
                                   "broker's 50-lot maximum",
                         'orders': ['O1']}]
    publisher.working_sells = 1
    publisher.broker_pendings = 0
    publisher.publish()

    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'orders';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .pane table', timeout=WAIT)

    page.wait_for_selector('.monitor tr.not-placed', timeout=WAIT)
    said = page.text_content('.monitor tr.not-placed')
    assert 'NOT AT THE BROKER' in said
    # ...in the ENGINE'S own words, with the number that explains it.
    assert "50-lot maximum" in said
    # It spans the table, so a sideways scroll cannot hide it.
    assert int(page.get_attribute('.monitor tr.not-placed td', 'colspan')) > 6

    # THE CONTROL: once it rests, the row goes. It must not cry wolf.
    publisher.quotes[0]['ticket'] = 5150
    publisher.quotes[0]['reason'] = None
    publisher.broker_pendings = 1
    publisher.publish()
    page.wait_for_function(
        "() => !document.querySelector('.monitor tr.not-placed')",
        timeout=WAIT)

    publisher.orders = None
    publisher.quotes = None
    publisher.working_sells = 0
    publisher.broker_pendings = None
    publisher.publish()
    # This page is shared with every test after it: put the desk back
    # to a ladder rather than leaving the monitor over it.
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = [window.MT5Trader.panelId(
            'ladder', 'XAUUSD_|GC1226')];
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.ladder .grid tbody tr', timeout=WAIT)


def test_two_closes_from_one_click_read_as_one_click(page):
    """Live 2026-09-04, the desk's own Working Orders panel:

        BUY  7.3400  Qty 0.1
        BUY  7.3400  Qty 0.04999999999999999

    One BUY 0.15 over two shorts. Nothing was wrong with the sizes —
    the two rows are one click, split across the two tickets it covers,
    and they sum to the 0.15 that was clicked. But seventeen digits of
    float dust beside a clean 0.1, on two rows that were otherwise
    identical, reads as the size having been changed on the way to the
    broker. So: the size prints as a size, and each row says which
    ticket it is closing.
    """
    open_ladder(page)
    publisher = page.paths['publisher']
    level = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows[5].level""")
    publisher.orders = [
        {'order_id': 'O1', 'level': level, 'side': 'BUY', 'quantity': 0.1,
         'filled_quantity': 0, 'state': 'WORKING', 'time_in_force': 'DAY',
         'position_id': 'POS-AAA'},
        # What the engine used to hand the panel, kept verbatim: a
        # publisher that had already been tidied would test nothing.
        {'order_id': 'O2', 'level': level, 'side': 'BUY',
         'quantity': 0.15 - 0.1, 'filled_quantity': 0, 'state': 'WORKING',
         'time_in_force': 'DAY', 'position_id': 'POS-BBB'},
    ]
    publisher.quotes = [
        {'pair_key': 'XAUUSD_|GC1226', 'side': 'BUY', 'level': level,
         'leg': 'BOTH', 'ticket': None, 'intent': 'CLOSE',
         'position_id': 'POS-AAA', 'orders': ['O1']},
        {'pair_key': 'XAUUSD_|GC1226', 'side': 'BUY', 'level': level,
         'leg': 'BOTH', 'ticket': None, 'intent': 'CLOSE',
         'position_id': 'POS-BBB', 'orders': ['O2']},
    ]
    publisher.publish()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'orders';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .pane table', timeout=WAIT)
    # The panel renders from the SNAPSHOT, which arrives on the poll —
    # rendering before it lands reads the previous one and finds
    # nothing working.
    page.wait_for_function(
        "() => document.querySelector('.monitor .pane')"
        ".textContent.indexOf('closes POS-AAA') >= 0", timeout=WAIT)

    text = page.text_content('.monitor .pane')

    assert '0.04999999999999999' not in text
    assert '0.05' in text and '0.1' in text
    # And each row names the ticket it closes, so two rows differing
    # only in size are not two goes at the same order.
    assert 'closes POS-AAA' in text and 'closes POS-BBB' in text

    publisher.orders = None
    publisher.quotes = None
    publisher.publish()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['XAUUSD_|GC1226'];
        window.MT5Trader.render();
    }""")


def test_the_MARKET_badge_stays_readable_on_an_unfocused_ladder(page):
    """It took the title bar's colour, which is a mid grey once the
    window is inactive — so the badge went grey on dark red the moment
    the ladder lost focus, which is most of the time with several open.

    That badge is the one label saying a click will cross the market
    immediately, so it is the last thing on the screen that may become
    hard to read."""
    open_ladder(page)
    page.paths['publisher'].order_type = 'MARKET'
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.ladder .mode-badge')"
        ".textContent.indexOf('MARKET') >= 0", timeout=WAIT)

    def badge_colours():
        return page.evaluate(
            """() => {
                 const n = document.querySelector('.ladder .mode-badge');
                 const s = getComputedStyle(n);
                 return [s.color, s.backgroundColor];
               }""")

    active_ink, active_bg = badge_colours()
    assert is_red(active_bg)

    # Now take the focus away, exactly as opening another window does.
    page.evaluate(
        "() => document.querySelector('.window.ladder')"
        ".classList.add('inactive')")
    inactive_ink, inactive_bg = badge_colours()

    try:
        # The ink does not change with focus, and it is white on both.
        assert inactive_ink == active_ink, (active_ink, inactive_ink)
        assert inactive_ink.replace(' ', '') in ('rgb(255,255,255)',)
        assert is_red(inactive_bg)
    finally:
        page.evaluate(
            "() => document.querySelector('.window.ladder')"
            ".classList.remove('inactive')")
        page.paths['publisher'].order_type = 'LIMIT'
        page.paths['publisher'].publish()


def test_the_keypad_does_not_offer_a_size_the_broker_will_refuse(page):
    """The oil ladder's broker caps leg A at 10 lots, and the keypad was
    offering 50 and 100. Pressing one got the refusal back — "Qty 100 x
    1 wants 100 lots on leg A, over this broker's 10-lot maximum". The
    arithmetic was right; the button should not have been there."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.max_qty = 10
    publisher.publish()
    page.wait_for_function(
        "() => document.querySelector('.ladder .keypad .qty[data-qty=\"50\"]')"
        ".disabled === true", timeout=WAIT)

    assert page.locator('.ladder .keypad .qty[data-qty="100"]').is_disabled()
    assert not page.locator('.ladder .keypad .qty[data-qty="10"]').is_disabled()
    assert not page.locator('.ladder .keypad .qty[data-qty="1"]').is_disabled()
    assert 'tops out at 10' in page.get_attribute(
        '.ladder .keypad .qty[data-qty="100"]', 'title')

    # And a size TYPED over the cap is marked on the box itself, before
    # the click rather than only in the refusal after it.
    page.fill('.ladder .qty-box.buy', '97')
    page.wait_for_selector('.ladder .qty-box.buy.over', timeout=WAIT)
    page.fill('.ladder .qty-box.buy', '5')
    page.wait_for_function(
        "() => !document.querySelector('.ladder .qty-box.buy.over')",
        timeout=WAIT)

    # THE CONTROL: a broker that caps nothing greys out nothing.
    publisher.max_qty = None
    publisher.publish()
    page.wait_for_function(
        "() => document.querySelector('.ladder .keypad .qty[data-qty=\"100\"]')"
        ".disabled === false", timeout=WAIT)
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.armed = {};
        window.MT5Trader.render();
    }""")


def test_a_close_the_broker_refused_says_so_on_the_screen(page):
    """"There is no way to close it."

    The Reconciler's "Close it" button sends a command that RUNS
    fine and comes back {ok: false, error: '<the broker's words>'} in
    its payload. `toastOutcome` read the ENVELOPE's ok, which was true,
    and had no branch for the payload's — so a refused close produced
    no toast at all. From the desk that is a dead button on the one
    position nothing else is allowed to close.
    """
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    try:
        page.evaluate("""() => window.MT5Trader.toastOutcome({
            ok: true,
            data: {ok: false,
                   error: '3677: 10027 AutoTrading disabled by client'}
        })""")
        page.wait_for_selector('.toast:not(.ok)', timeout=WAIT)
        toast = page.text_content('.toast:not(.ok)')
        # The BROKER's own words, never "check the log" (spec §11).
        assert '10027' in toast
        assert 'AutoTrading disabled by client' in toast
    finally:
        page.evaluate("() => document.getElementById('toasts').innerHTML = ''")


def test_control_a_close_that_worked_does_not_raise_an_error_toast(page):
    """The control. Without it the branch above would pass on a build
    that toasts a refusal at every outcome — and a success styled as a
    fault is the noise the error style exists to stand out from."""
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    try:
        page.evaluate("""() => window.MT5Trader.toastOutcome({
            ok: true,
            data: {ok: true, closed: [{ticket: '3677', volume: 0.01}],
                   left: 0.0}
        })""")
        page.wait_for_timeout(200)
        assert page.locator('.toast:not(.ok)').count() == 0, (
            'a close that worked was reported as a refusal')
    finally:
        page.evaluate("() => document.getElementById('toasts').innerHTML = ''")


def fills_totals(page, totals):
    """Render the Fills tab against one totals block."""
    page.evaluate("""(totals) => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/api/fills') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true, totals: totals, fills: []}),
                    {status: 200,
                     headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
        const UI = window.MT5Trader;
        UI.state.fills = null;
        UI.state.open = [UI.panelId('monitor')];
        UI.state.monitorTab = 'fills';
        UI.render();
    }""", totals)
    page.wait_for_function(
        "() => document.querySelector('.monitor') && "
        "document.querySelector('.monitor').textContent.indexOf('fills') >= 0",
        timeout=WAIT)
    return page.text_content('.monitor')


def test_a_commission_nobody_measured_is_not_printed_as_zero(page):
    """The SCREEN's half of the contract, pinned.

    Said plainly: this one passes against the old build too. The
    "$0.00" came from the database — COALESCE(SUM(commission), 0) over
    a column that was NULL on every fill — and the old screen would
    have rendered a true null as "—" if it had ever been handed one.
    test_journal_row_shape.py is what catches that fault.

    It is still worth pinning here. It is the screen half of the rule
    the totals now depend on, and without it a later change that reads
    the value instead of the COUNT would print "$0.00" again with
    nothing to stop it.
    """
    try:
        text = fills_totals(page, {'fills': 4, 'volume': 4, 'profit': -90.0,
                                   'commission': None,
                                   'commission_measured': 0,
                                   'swap': None, 'swap_measured': 0})
        assert 'commission $0.00' not in text, (
            'a charge nobody measured was printed as zero')
        assert 'commission —' in text, text[:400]
    finally:
        page.evaluate("() => { if (window.__realFetch) "
                      "{ window.fetch = window.__realFetch; } }")


def test_control_a_commission_the_broker_DID_report_is_printed(page):
    """The control for the two above: without it, "always say —" would
    pass, and that hides the real number as thoroughly as the zero did.
    """
    try:
        text = fills_totals(page, {'fills': 4, 'volume': 4, 'profit': -90.0,
                                   'commission': -9.0,
                                   'commission_measured': 4,
                                   'swap': -2.0, 'swap_measured': 4})
        assert 'commission -$9.00' in text, text[:400]
        assert 'swap -$2.00' in text, text[:400]
    finally:
        page.evaluate("() => { if (window.__realFetch) "
                      "{ window.fetch = window.__realFetch; } }")


def test_a_partial_commission_total_says_how_many_fills_it_covers(page):
    try:
        text = fills_totals(page, {'fills': 4, 'volume': 4, 'profit': -90.0,
                                   'commission': -9.0,
                                   'commission_measured': 2,
                                   'swap': None, 'swap_measured': 0})
        assert '(2 of 4)' in text, text[:400]
    finally:
        page.evaluate("() => { if (window.__realFetch) "
                      "{ window.fetch = window.__realFetch; } }")


def show_pnl_check(page, check):
    """Put one pnl_check row on the monitor and hold it there.

    Two HELD polls first: a real /api/status already in flight when the
    hold went on lands after it, replaces the snapshot, and takes the
    row away with it.
    """
    page.evaluate(HOLD_THE_SNAPSHOT)
    page.wait_for_function("() => window.__held >= 2", timeout=WAIT)
    page.evaluate("""(check) => {
        const UI = window.MT5Trader;
        UI.state.snapshot.at = 1000.0;
        UI.state.snapshot.pnl_check = check;
        UI.state.open = [UI.panelId('monitor')];
        UI.state.monitorTab = 'positions';
        UI.render();
    }""", check)


def test_the_pnl_row_reports_when_the_two_halves_were_read_together(page):
    """Our total against MT5's own. Both come from the ENGINE now, from
    one moment, and the row says when — so a figure a few seconds old is
    never mistaken for live."""
    try:
        show_pnl_check(page, {'at': 996.0, 'ours': 7.0, 'theirs': 7.0,
                              'difference': 0.0, 'ours_net': 3.5,
                              'basis': 'gross'})
        page.wait_for_function(
            "() => document.querySelector('.monitor').textContent"
            ".indexOf('read together') >= 0", timeout=WAIT)
        text = page.text_content('.monitor')

        assert 'read together 4s ago' in text
        # Agreement is not a fault: the row must NOT be red.
        assert page.locator('.monitor tr.mismatch').count() == 0, (
            'two totals that agree were flagged as a disagreement')
    finally:
        page.evaluate(RELEASE_THE_SNAPSHOT)


def test_the_row_says_it_compared_GROSS_and_shows_the_net_beside_it(page):
    """MT5's own profit carries no commission, so the thing compared
    against it must not carry commission either — and the row has to
    SAY which of the two numbers it used, or a trader reading 'our
    total' against the P&L on every other panel sees two figures that
    differ and no reason why."""
    try:
        show_pnl_check(page, {'at': 1000.0, 'ours': 7.0, 'theirs': 7.0,
                              'difference': 0.0, 'ours_net': -1.0,
                              'basis': 'gross'})
        page.wait_for_function(
            "() => document.querySelector('.monitor').textContent"
            ".indexOf('gross') >= 0", timeout=WAIT)
        text = page.text_content('.monitor')

        assert 'our total (gross)' in text
        assert 'ours after commission' in text and '-$1.00' in text
        assert page.locator('.monitor tr.mismatch').count() == 0, (
            'the commission was compared and reported as a disagreement')
    finally:
        page.evaluate(RELEASE_THE_SNAPSHOT)


def test_control_a_real_disagreement_is_still_flagged_red(page):
    """The control. The row exists to catch a genuine gap; a fix that
    simply stopped reddening would remove the check entirely."""
    try:
        show_pnl_check(page, {'at': 1000.0, 'ours': 7.0, 'theirs': -21.0,
                              'difference': 28.0, 'ours_net': 3.5,
                              'basis': 'gross'})
        page.wait_for_function(
            "() => document.querySelectorAll('.monitor tr.mismatch')"
            ".length > 0", timeout=WAIT)

        assert '$28.00' in page.text_content('.monitor tr.mismatch')
    finally:
        page.evaluate(RELEASE_THE_SNAPSHOT)


def test_an_unreadable_account_leaves_the_row_UNMEASURED(page):
    """Unmeasured is not zero: an account that could not be read does
    not make the difference nil, it makes it unknown."""
    try:
        show_pnl_check(page, {'at': 1000.0, 'ours': 7.0, 'theirs': None,
                              'difference': None, 'ours_net': 3.5,
                              'basis': 'gross'})
        page.wait_for_function(
            "() => document.querySelector('.monitor').textContent"
            ".indexOf('nothing to reconcile') >= 0", timeout=WAIT)
        text = page.text_content('.monitor')

        assert 'unmeasured, not zero' in text
        assert '$0.00' not in text.split('nothing to reconcile')[0][-120:]
    finally:
        page.evaluate(RELEASE_THE_SNAPSHOT)


def _algo_block(**over):
    block = {
        'algo': 'ALGO', 'on': True, 'mode': 'DRY_RUN', 'state': 'SIGNAL',
        'signal': 'SELL', 'ready': True, 'z_sell': 2.61, 'z_buy': 1.9,
        'mean': 59.0, 'sigma': 0.04, 'upper': 59.10, 'lower': 58.90,
        'count': 20, 'needed': 20, 'timeframe_min': 15, 'length': 20,
        'params': {'entry_z': 2.5, 'confirm_ticks': 3, 'progress_bar': True,
                   'stop_loss_on': True, 'max_trades_day': 10,
                   'edge_capture_frac': 0.5},
        'history': {'note': 'mt5'}, 'positions': [], 'health': None,
        'day': {'trades': 2, 'losses_row': 0, 'pnl': 1.4},
        'last_blocked': None, 'window': True,
        'filters': {
            'ready': True, 'qty': 1,
            'cost': {'crossing': 2.0, 'commission': 1.0, 'slippage': 0.0,
                     'total': 3.0},
            'edge': {'on': True, 'ok': True, 'ratio': 2.4, 'required': 1.5,
                     'capture': 7.2},
            'probability': {'on': True, 'ok': True, 'win': 0.986,
                            'ev': 4.1},
            'regime': {'on': True, 'state': 'RANGE',
                       'efficiency_ratio': 0.2, 'crossings': 9},
            'half_life_minutes': 42.0, 'half_life_band': [0, 0]}}
    block.update(over)
    return block


def test_the_Algo_has_a_window_of_its_own_with_the_three_panels(page):
    """Signal & Position, Statistics, Filters — the stat-arb dashboard's
    three panels — in one window per ladder while its Algo is on. The
    control: the Algo off, and the window is gone."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block()
    try:
        publisher.publish()
        page.wait_for_selector('.window.algowin', timeout=WAIT)
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-signal')"
            " || {textContent: ''}).textContent.includes('H to L signal')", timeout=WAIT)
        text = page.text_content('.window.algowin')
        for heading in ('Signal & Position', 'Statistics', 'Filters'):
            assert heading.upper() in text.upper(), heading
        assert 'H to L' in text and 'L to H' in text
        assert '2.40' in text and 'req 1.5' in text        # capture / cost
        assert 'Mean-rev' in text and '42 min' in text
        assert page.locator('.window.algowin .aw-tile.sell.hit').count() == 1
        assert page.text_content('.window.algowin .aw-mode') == 'SIGNALS'
        # Each leg's account number and broker, across the top.
        publisher.accounts_extra = {
            'acct_a': {'login': 19514405, 'server': 'CFI3-Real',
                       'company': 'CFI Financial Group'},
            'acct_b': None}               # a leg that could not be read
        publisher.publish()
        page.wait_for_function(
            "() => document.querySelector('.window.algowin .aw-legs')"
            ".textContent.includes('#19514405')", timeout=WAIT)
        legs = page.text_content('.window.algowin .aw-legs')
        assert 'A XAUUSD_ \u00b7 #19514405 \u00b7 CFI Financial Group' in legs
        # Control: unread is said, not left blank or shown as a number.
        assert 'B GC1226 \u00b7 not read' in legs
        # No company reported: the server's name stands in for it.
        publisher.accounts_extra = {
            'acct_a': {'login': 19514405, 'server': 'CFI3-Real',
                       'company': None}}
        publisher.publish()
        page.wait_for_function(
            "() => document.querySelector('.window.algowin .aw-legs')"
            ".textContent.includes('CFI3-Real')", timeout=WAIT)
        # The Fair Spread window no longer carries the Algo.
        assert 'H to L signal' not in (page.text_content('.window.fairwin')
                                     if page.locator('.window.fairwin').count()
                                     else '')
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.accounts_extra = {}
        publisher.publish()
    page.wait_for_function(
        "() => !document.querySelector('.window.algowin')", timeout=WAIT)


def test_a_blocked_signal_names_the_filter_that_held_it(page):
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block(
        state='BLOCKED', signal=None,
        blocked='edge filter: capture 0.42x the cost, under the 1.5x required',
        last_blocked={'side': 'SELL', 'z': 3.04, 'at': time.time(),
                      'reason': 'edge filter: capture 0.42x the cost, under '
                                'the 1.5x required'})
    publisher.algo_block['filters']['edge'].update(ok=False, ratio=0.42)
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-blocked')"
            " || {textContent: ''}).textContent.includes('edge filter')", timeout=WAIT)
        assert 'H to L' in page.text_content('.window.algowin .aw-blocked')
        assert page.locator('.window.algowin .aw-badge.bad').count() >= 1
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_in_a_position_the_Algo_window_shows_entry_legs_levels_and_the_bar(
        page):
    """In a position: what it is in, at what price, each leg's fill and
    where it would close now, BE/TP/SL, and SL <- entry -> TP. The
    control: flat, there is no position detail and no bar."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    held = {'position_id': 'POS-7', 'side': 'BUY', 'quantity': 1,
            'entry_spread': 59.11, 'closing_spread': 59.21, 'tp': 59.31,
            'sl': 58.91, 'break_even': 59.13, 'progress': 0.5, 'exit': None,
            'net_pnl': 0.8, 'entry_z': -2.6, 'age_sec': 125,
            'leg_a_side': 'SELL', 'leg_a_entry': 4292.0, 'leg_a_now': 4292.2,
            'leg_b_side': 'BUY', 'leg_b_entry': 4351.1, 'leg_b_now': 4351.4}
    publisher.algo_block = _algo_block(state='IN_POSITION', signal=None,
                                       positions=[held])
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-signal')"
            " || {textContent: ''}).textContent.includes('BUY @ 59.11')", timeout=WAIT)
        text = page.text_content('.window.algowin .aw-signal')
        assert 'LONG' in text and 'TP 59.31' in text and 'SL 58.91' in text
        assert '4351.10' in text and '4351.40' in text      # leg B fill/now
        assert '50% to TP' in text
        publisher.algo_block = _algo_block(state='WATCHING', signal=None)
        publisher.publish()
        page.wait_for_function(
            "() => !document.querySelector('.window.algowin .algo-progress')",
            timeout=WAIT)
        assert 'FLAT' in page.text_content('.window.algowin .aw-signal')
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_the_ladder_title_bar_carries_the_Algo_switch(page):
    """One click from the ladder: OFF / DRY RUN / LIVE. Picking LIVE
    still asks first and sends nothing until confirmed."""
    open_ladder(page)
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => (document.querySelector('.ladder .algo-btn') || {})"
        ".textContent === 'ALGO OFF'", timeout=WAIT)
    page.evaluate(SPY_ON_COMMANDS)
    try:
        page.click('.ladder .algo-btn')
        page.wait_for_selector('.ladder .algo-menu:not([hidden])',
                               timeout=WAIT)
        page.click('.ladder .algo-menu button[data-algo="ALGO_LIVE"]')
        page.wait_for_selector('#modal:not(.hidden)', timeout=WAIT)
        assert 'LIVE' in page.text_content('#modal-title')
        assert algo_commands(page) == []                 # nothing sent yet
        page.click('#modal-cancel')
        # The control: a dry run goes straight through.
        page.click('.ladder .algo-btn')
        page.click('.ladder .algo-menu button[data-algo="ALGO"]')
        page.wait_for_function(
            "() => window.__commands.some(c => c.kind === 'set_algo')",
            timeout=WAIT)
        assert algo_commands(page)[-1]['payload']['mode'] == 'DRY_RUN'
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_the_switch_says_LIVE_when_the_Algo_trades(page):
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block(mode='LIVE')
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.ladder .algo-btn') || {})"
            ".textContent === 'ALGO LIVE'", timeout=WAIT)
        assert 'live' in page.get_attribute('.ladder .algo-btn', 'class')
        assert page.text_content('.window.algowin .aw-mode') == 'LIVE'
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()
    page.wait_for_function(
        "() => (document.querySelector('.ladder .algo-btn') || {})"
        ".textContent === 'ALGO OFF'", timeout=WAIT)

def _ladder_with_algo(page, mode):
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block(mode=mode)
    publisher.publish()
    want = 'ALGO LIVE' if mode == 'LIVE' else 'ALGO SIGNALS'
    page.wait_for_function(
        "(want) => (document.querySelector('.ladder .algo-btn') || {})"
        ".textContent === want", arg=want, timeout=WAIT)


def _algo_off(page):
    publisher = page.paths['publisher']
    publisher.algo = 'NONE'
    publisher.algo_block = None
    publisher.publish()
    page.wait_for_function(
        "() => (document.querySelector('.ladder .algo-btn') || {})"
        ".textContent === 'ALGO OFF'", timeout=WAIT)


def _click_a_bid(page):
    page.evaluate("() => document.querySelector("
                  "'.ladder .grid tbody td.bid').click()")


def test_a_LIVE_ladder_is_locked_and_says_so(page):
    """Algo or Manual, never both — on the SCREEN, not only in the
    engine. While LIVE the ways in are off (and a click on the grid
    sends nothing), the ladder says why, and the ways out stay."""
    open_ladder(page)
    try:
        _ladder_with_algo(page, 'LIVE')
        page.wait_for_selector('.ladder .algo-lock:not([hidden])',
                               timeout=WAIT)
        assert 'manual orders are off' in page.text_content(
            '.ladder .algo-lock')
        # A strip ABOVE the ladder, never in place of it: the prices are
        # still the thing the trader is watching.
        grid = page.locator('.ladder .grid').bounding_box()
        assert grid['width'] > 200 and grid['height'] > 200, grid
        lock = page.locator('.ladder .algo-lock').bounding_box()
        assert lock['y'] + lock['height'] <= grid['y'] + 1
        assert page.is_disabled('.ladder .buy-touch')
        assert page.is_disabled('.ladder .sell-touch')
        assert page.is_disabled('.ladder .keypad .qty')
        # A close is never withheld.
        assert page.is_enabled('.ladder .flatten')
        assert page.is_enabled('.ladder .close-limit-go')

        page.evaluate(SPY_ON_COMMANDS)
        page.evaluate("() => { document.getElementById('toasts')"
                      ".innerHTML = ''; }")
        _click_a_bid(page)
        page.wait_for_selector('.toast:has-text("manual orders are off")',
                               timeout=WAIT)
        assert not page.evaluate(
            "() => window.__commands.some(c => c.kind === 'click')")
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch || '
                      'window.fetch; }')
        _algo_off(page)


def test_the_CONTROL_a_dry_run_ladder_still_trades_by_hand(page):
    open_ladder(page)
    try:
        _ladder_with_algo(page, 'DRY_RUN')
        assert page.locator('.ladder .algo-lock[hidden]').count() == 1
        assert page.is_enabled('.ladder .buy-touch')
        assert page.is_enabled('.ladder .keypad .qty')
        page.evaluate(SPY_ON_COMMANDS)
        page.evaluate("() => { window.MT5Trader.state.snapshot"
                      ".confirm_market_clicks = false; }")
        _click_a_bid(page)
        page.wait_for_function(
            "() => window.__commands.some(c => c.kind === 'click')",
            timeout=WAIT)
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch || '
                      'window.fetch; }')
        _algo_off(page)


#: Capture every engine command the page sends, answering each as the
#: engine would — so a test can read exactly what was asked for.
SPY_ON_COMMANDS = """(answer) => {
    window.__realFetch = window.__realFetch || window.fetch;
    window.__commands = [];
    window.__answer = answer || {ok: true};
    window.fetch = function (url, options) {
        const u = String(url);
        const json = (body) => Promise.resolve(new Response(
            JSON.stringify(body),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (u.indexOf('/api/command') >= 0 && options) {
            window.__commands.push(JSON.parse(options.body));
            return json({ok: true, id: 'c' + window.__commands.length});
        }
        if (u.indexOf('/api/result/') >= 0) {
            return json({ok: true, data: window.__answer});
        }
        if (u.indexOf('/api/pairs/') >= 0 && options &&
            options.method === 'POST') {
            return json({ok: true, notes: []});
        }
        return window.__realFetch(url, options);
    };
}"""


def algo_commands(page):
    return [c for c in page.evaluate('() => window.__commands')
            if c['kind'] == 'set_algo']


def test_going_LIVE_asks_first_and_sends_nothing_until_confirmed(page):
    open_ladder(page)
    page.evaluate(SPY_ON_COMMANDS)
    try:
        page.click('.ladder .ladder-cog')
        page.wait_for_selector('.ladder .ls-algo', timeout=WAIT)
        page.select_option('.ladder .ls-algo', 'ALGO_LIVE')
        page.click('.ladder .ls-save')
        page.wait_for_selector('#modal:not(.hidden)', timeout=WAIT)
        assert 'LIVE' in page.text_content('#modal-title')
        assert 'REAL orders' in page.text_content('#modal-body')
        assert algo_commands(page) == []            # nothing sent yet

        page.click('#modal-confirm')
        page.wait_for_function(
            "() => window.__commands.some(c => c.kind === 'set_algo')",
            timeout=WAIT)
        [command] = algo_commands(page)
        assert command['payload']['mode'] == 'LIVE'
        assert command['payload']['confirmed'] is True
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_a_dry_run_is_not_asked_about(page):
    """The control: the Algo in dry run sends nothing, so it asks
    nothing."""
    open_ladder(page)
    page.evaluate(SPY_ON_COMMANDS)
    try:
        page.click('.ladder .ladder-cog')
        page.wait_for_selector('.ladder .ls-algo', timeout=WAIT)
        page.select_option('.ladder .ls-algo', 'ALGO')
        page.click('.ladder .ls-save')
        page.wait_for_function(
            "() => window.__commands.some(c => c.kind === 'set_algo')",
            timeout=WAIT)
        [command] = algo_commands(page)
        assert command['payload']['mode'] == 'DRY_RUN'
        assert page.locator('#modal:not(.hidden)').count() == 0
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_leaving_LIVE_with_a_position_offers_close_or_hand_over(page):
    open_ladder(page)
    page.evaluate(SPY_ON_COMMANDS, {
        'ok': False, 'choose': ['close', 'manual'],
        'reason': 'the Algo holds 1 position(s) on XAUUSD_|GC1226'})
    try:
        page.evaluate(
            "() => window.MT5Trader.setAlgo('XAUUSD_|GC1226', 'NONE')")
        page.wait_for_selector('#modal:not(.hidden)', timeout=WAIT)
        assert page.is_visible('#modal-alt')
        assert 'Hand it to manual' in page.text_content('#modal-alt')
        page.evaluate("() => { window.__answer = {ok: true}; }")
        page.click('#modal-alt')
        page.wait_for_function(
            "() => window.__commands.filter(c => c.kind === 'set_algo')"
            ".length === 2", timeout=WAIT)
        assert algo_commands(page)[-1]['payload']['off_action'] == 'manual'
        # The second answer is gone again for every other question.
        assert not page.is_visible('#modal-alt')
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_the_backtest_button_runs_it_on_the_engine_and_shows_both_runs(page):
    """What these settings would have done on MT5's history — and,
    beside it, without the trend protections. It asks the ENGINE, which
    holds the history and the costs, and it sends no order."""
    open_ladder(page)
    answer = {'ok': True, 'summary': {
                  'trades': 3, 'wins': 2, 'losses': 1, 'net': 4.5,
                  'max_drawdown': -1.2, 'candles': 480,
                  'from': 1790000000, 'to': 1790432000},
              'without_protections': {'trades': 9, 'wins': 3, 'losses': 6,
                                      'net': -12.25, 'max_drawdown': -15.0},
              'held': {'trend: the middle ROSE #σ in the last # min — no '
                       'H to L against it': 14},
              'trades': [{'side': 'SELL', 'opened_at': 1790100000,
                          'entry_z': 1.48, 'reason': 'PROFIT_TARGET',
                          'pnl': 2.5}],
              'caveats': ['one look per candle']}
    try:
        _ladder_with_algo(page, 'DRY_RUN')
        page.wait_for_selector('.window.algowin .aw-bt-run', timeout=WAIT)
        page.evaluate(SPY_ON_COMMANDS, answer)
        page.click('.window.algowin .aw-bt-run')
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-bt-out') "
            "|| {textContent: ''}).textContent.indexOf('These settings') >= 0",
            timeout=WAIT)
        text = page.text_content('.window.algowin .aw-bt-out')
        assert '3 trade(s)' in text and '9 trade(s)' in text
        assert 'Without re-entry and the trend filter' in text
        assert 'ROSE' in text and 'H to L' in text
        kinds = page.evaluate("() => window.__commands.map(c => c.kind)")
        assert kinds == ['algo_backtest']
        assert page.evaluate(
            "() => window.__commands[0].payload.days") == 5
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch || '
                      'window.fetch; }')
        _algo_off(page)


def test_a_held_working_order_is_the_mode_not_a_fault(page):
    """A working order is HELD by the desk and both legs cross when the
    spread reaches it, so nothing is at the broker - by design. It must
    not light up as an order that failed to place."""
    open_ladder(page)
    publisher = page.paths['publisher']
    level = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows[5].level""")
    publisher.orders = [{'order_id': 'O1', 'level': level, 'side': 'SELL',
                         'quantity': 1, 'filled_quantity': 0,
                         'state': 'WORKING', 'time_in_force': 'DAY'}]
    publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'SELL',
                         'level': level, 'leg': 'BOTH', 'ticket': None,
                         'held': True, 'intent': 'OPEN', 'reason': None,
                         'orders': ['O1']}]
    publisher.working_sells = 1
    publisher.broker_pendings = 0
    publisher.pair_extra = {'working_orders': 'TRIGGER', 'held_entries': 1}
    publisher.publish()
    page.wait_for_function(
        """() => (document.querySelector('.ladder .quoting-note') || {})
             .textContent && document.querySelector('.ladder .quoting-note')
             .textContent.indexOf('held here') >= 0""", timeout=WAIT)
    assert page.locator('.ladder .quoting-note.held').count() == 0
    assert page.locator('.ladder .cnt-w.mismatch').count() == 0
    assert page.locator('.ladder td.work.held').count() == 0

    # THE CONTROL: a held order something IS holding (a stale price)
    # says so, in its own words.
    publisher.quotes[0]['reason'] = ('leg A stale 30s - holding the '
                                     'order, nothing sent')
    publisher.publish()
    page.wait_for_selector('.ladder .quoting-note.held', timeout=WAIT)
    note = page.text_content('.ladder .quoting-note')
    assert 'ORDER HELD' in note and 'stale' in note

    publisher.orders = None
    publisher.quotes = None
    publisher.working_sells = 0
    publisher.broker_pendings = None
    publisher.pair_extra = {}
    publisher.publish()


def test_each_end_of_the_re_entry_window_carries_its_own_price(page):
    """'short back at +2.00 (59.08) to +1.00 (59.04)': the bracketed
    price sat after the far end and read as ITS price; it was the near
    end's. Each z now has its own, both sides."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    block = _algo_block(state='WATCHING', signal=None)
    block['params'].update(reentry_on=True, reentry_back=0.5,
                           reentry_window_pct=50)
    publisher.algo_block = block
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-signal')"
            " || {textContent: ''}).textContent.includes('back at')",
            timeout=WAIT)
        text = page.text_content('.window.algowin .aw-signal')
        assert '+2.00 (59.08) to +1.00 (59.04)' in text, text
        assert '−2.00 (58.92) to −1.00 (58.96)' in text, text
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_each_side_can_have_its_own_exits_and_blank_is_the_ladders(page):
    """H to L and L to H each get an optional stop / target of their
    own. A typed value is sent under the side's key; a blank one is not
    sent at all - the ladder's own value stands, never a zero."""
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ladder-settings .ls-az-atr-stop-sell',
                           timeout=WAIT)
    page.locator('.ladder .ladder-settings .ls-az-atr-stop-sell') \
        .scroll_into_view_if_needed()
    if os.environ.get('SIDE_SHOT'):
        page.screenshot(path=os.environ['SIDE_SHOT'])
    page.evaluate(SPY_ON_PAIR_SAVE)
    page.select_option('.ladder .ladder-settings .ls-az-stop-mode-sell',
                       'ATR')
    page.fill('.ladder .ladder-settings .ls-az-atr-stop-sell', '3')
    page.fill('.ladder .ladder-settings .ls-az-tp-buy', '6')
    page.click('.ladder .ladder-settings .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
    params = page.evaluate('() => window.__sent')['algo_params']
    page.evaluate('() => { window.fetch = window.__realFetch; }')
    assert params['stop_mode_sell'] == 'ATR'
    assert params['atr_stop_mult_sell'] == 3
    assert params['tp_pct_buy'] == 6
    # The CONTROL: the boxes left blank are not sent.
    for key in ('stop_mode_buy', 'atr_stop_mult_buy', 'tp_pct_sell',
                'stop_loss_pct_sell', 'target_mode_sell'):
        assert key not in params, key
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.wait_for_selector('.ladder .grid tbody tr')


def test_a_signals_alert_is_said_once_and_stays_until_dismissed(page):
    """SIGNALS mode: the trade is the trader's to make, so the alert
    stays on screen. Said once by its id - the next poll carrying the
    same alert says nothing more. The control: a NEW id is said."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'

    def alert(alert_id):
        return {'id': alert_id, 'seq': 1, 'kind': 'ENTRY', 'side': 'SELL',
                'reason': None, 'at': time.time(),
                'text': 'H to L — SELL 1 @ 59.1000 (z +2.61) ' + alert_id}
    publisher.algo_block = _algo_block(signal_alert=alert('A1'))
    try:
        publisher.publish()
        page.wait_for_selector('#toasts .toast.signal', timeout=WAIT)
        page.wait_for_timeout(600)                 # three more polls
        toasts = page.locator('#toasts .toast.signal')
        assert toasts.count() == 1
        assert 'H to L — SELL 1 @ 59.1000' in toasts.first.text_content()
        toasts.first.click()                       # dismissed
        page.wait_for_timeout(600)
        assert page.locator('#toasts .toast.signal').count() == 0
        publisher.algo_block = _algo_block(signal_alert=alert('A2'))
        publisher.publish()
        page.wait_for_selector('#toasts .toast.signal', timeout=WAIT)
        assert 'A2' in page.text_content('#toasts .toast.signal')
        page.locator('#toasts .toast.signal').first.click()
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_every_alert_box_says_when_it_happened(page):
    """A wall of alerts is a history: each box carries the time of the
    ALERT (the engine's own `at`), not when the page drew it, with its
    date - today's included."""
    open_ladder(page)
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    now = time.time()
    expect = page.evaluate(
        """(at) => { const d = new Date(at * 1000);
                     const p = n => (n < 10 ? '0' : '') + n;
                     return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-'
                            + p(d.getDate()) + ' ' + p(d.getHours()) + ':'
                            + p(d.getMinutes()) + ':' + p(d.getSeconds()); }""",
        now - 20)
    publisher.algo_block = _algo_block(signal_alert={
        'id': 'T1', 'seq': 1, 'kind': 'EXIT', 'side': 'SELL',
        'reason': 'stop', 'at': now - 20,
        'text': 'Stop loss — close your SELL 1 at 9.9860 (net -502.00)'})
    try:
        publisher.publish()
        page.wait_for_selector('#toasts .toast.signal', timeout=WAIT)
        stamp = page.text_content('#toasts .toast.signal .toast-time')
        assert stamp == expect, (stamp, expect)
        page.locator('#toasts .toast.signal').first.click()
        # Another day: its own date, not today's.
        dated = page.evaluate(
            """(at) => { const d = new Date(at * 1000);
                         return d.getFullYear() + '-'; }""", now - 3 * 86400)
        publisher.algo_block = _algo_block(signal_alert={
            'id': 'T2', 'seq': 2, 'kind': 'EXIT', 'side': 'SELL',
            'reason': 'stop', 'at': now - 3 * 86400, 'text': 'old one'})
        publisher.publish()
        page.wait_for_selector('#toasts .toast.signal', timeout=WAIT)
        assert page.text_content(
            '#toasts .toast.signal .toast-time').startswith(dated)
        page.locator('#toasts .toast.signal').first.click()
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_an_ordinary_toast_is_stamped_too(page):
    open_ladder(page)
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.evaluate("() => window.MT5Trader.toast('hello', 'ok')")
    stamp = page.text_content('#toasts .toast .toast-time')
    assert len(stamp) == 19 and stamp.count(':') == 2, stamp
    assert stamp[4] == '-' and stamp[7] == '-' and stamp[10] == ' ', stamp
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")


def test_a_position_is_marked_on_the_ladder_and_named_plainly(page):
    """A manual position the Algo watches: SHORT in its window - not
    "YOUR SHORT", the desk's own word - and its entry / target / stop
    tagged on the ladder's own prices."""
    open_ladder(page)
    publisher = page.paths['publisher']
    levels = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows.map(r => r.level)""")
    publisher.algo = 'ALGO'
    held = {'position_id': 'P1', 'side': 'SELL', 'quantity': 1,
            'source': 'MANUAL', 'entry_spread': levels[4],
            'closing_spread': levels[5], 'break_even': None,
            'tp': levels[7], 'sl': levels[2], 'exit': None}
    publisher.algo_block = _algo_block(state='IN_POSITION', signal=None,
                                       positions=[held], mean=levels[6])
    try:
        publisher.publish()
        page.wait_for_selector('.ladder .lvl-tag.tp', timeout=WAIT)
        assert page.locator('.ladder .lvl-tag.entry').count() == 1
        assert page.locator('.ladder .lvl-tag.sl').count() == 1
        # M: the Algo's mean, on its row, while the position is on.
        mean = page.locator('.ladder .lvl-tag.mean')
        assert mean.count() == 1 and mean.text_content() == 'M'
        # Unmeasured is not marked: no break-even, no BE tag.
        assert page.locator('.ladder .lvl-tag.be').count() == 0
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-pos')"
            " || {textContent: ''}).textContent.indexOf('SHORT 1') === 0",
            timeout=WAIT)
        assert 'YOUR' not in page.text_content('.window.algowin .aw-pos')
        # Flat: no M - it is there to track a position, nothing else.
        publisher.algo_block = _algo_block(mean=levels[6])
        publisher.publish()
        page.wait_for_selector('.ladder .lvl-tag.tp', state='detached',
                               timeout=WAIT)
        assert page.locator('.ladder .lvl-tag.mean').count() == 0
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def _combined_short(levels, covered=0.0):
    return {'position_id': 'AVG:P1+P2', 'combined': True,
            'position_ids': ['P1', 'P2'], 'side': 'SELL', 'quantity': 2.0,
            'source': 'MANUAL', 'entry_spread': levels[4],
            'closing_spread': levels[5], 'break_even': levels[4],
            'tp': levels[7], 'sl': levels[2], 'tp_money': 6.0,
            'sl_money': -9.0, 'net_pnl': -1.5, 'exit': None,
            'opened_at': time.time() - 600, 'age_sec': 600.0,
            'leg_a_side': 'BUY', 'leg_a_volume': 0.2, 'leg_a_entry': 4607.5,
            'leg_a_now': 4607.4, 'leg_b_side': 'SELL', 'leg_b_volume': 0.2,
            'leg_b_entry': 4666.6, 'leg_b_now': 4660.8,
            'fills': [{'position_id': 'P1', 'quantity': 1.0,
                       'entry_spread': levels[6], 'opened_at': time.time() - 600,
                       'net_pnl': -3.0},
                      {'position_id': 'P2', 'quantity': 1.0,
                       'entry_spread': levels[2], 'opened_at': time.time() - 60,
                       'net_pnl': 1.5}],
            'close_covered': covered}


def test_two_fills_read_as_one_averaged_position(page):
    """The ladder and the Algo window show ONE position: its size, its
    average, one set of tags - and the fills behind it folded away."""
    open_ladder(page)
    publisher = page.paths['publisher']
    levels = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows.map(r => r.level)""")
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block(state='IN_POSITION', signal=None,
                                       positions=[_combined_short(levels)])
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-pos')"
            " || {textContent: ''}).textContent.indexOf('SHORT 2') === 0",
            timeout=WAIT)
        line = page.text_content('.window.algowin .aw-line')
        assert 'SELL 2 @' in line and 'avg' in line, line
        signal = page.text_content('.window.algowin .aw-signal')
        assert 'Avg entry' in signal and '(2 fills)' in signal
        assert 'Leg B (SELL 0.2)' in signal and '(oldest)' in signal
        # One set of tags, the entry marked as the average.
        assert page.locator('.ladder .lvl-tag.entry').count() == 1
        assert page.text_content('.ladder .lvl-tag.entry') == 'AVG'
        # The fills: folded by default...
        assert page.evaluate(
            "() => document.querySelector('.aw-fills').open") is False
        page.click('.aw-fills summary')
        page.wait_for_timeout(700)               # three repaints later
        assert page.evaluate(
            "() => document.querySelector('.aw-fills').open") is True
        assert '#2 SELL 1' in page.text_content('.aw-fills')
        page.click('.aw-fills summary')          # folded again
        page.wait_for_timeout(500)
        assert page.evaluate(
            "() => document.querySelector('.aw-fills').open") is False
        # No close order, or one that covers the whole: no warning.
        assert page.locator('.aw-cover').count() == 0
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_a_close_that_covers_part_of_the_whole_is_said(page):
    open_ladder(page)
    publisher = page.paths['publisher']
    levels = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows.map(r => r.level)""")
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block(
        state='IN_POSITION', signal=None,
        positions=[_combined_short(levels, covered=1.0)])
    try:
        publisher.publish()
        page.wait_for_selector('.aw-cover', timeout=WAIT)
        text = page.text_content('.aw-cover')
        assert 'covers 1 of 2' in text and 'other 1 has no close' in text
        # Control: covering the whole, nothing to say.
        publisher.algo_block = _algo_block(
            state='IN_POSITION', signal=None,
            positions=[_combined_short(levels, covered=2.0)])
        publisher.publish()
        page.wait_for_function(
            "() => !document.querySelector('.aw-cover')", timeout=WAIT)
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_the_position_limit_is_said_once_it_is_reached(page):
    open_ladder(page)
    publisher = page.paths['publisher']
    levels = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows.map(r => r.level)""")
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block(
        state='IN_POSITION', signal=None,
        positions=[_combined_short(levels)])
    two = [{'position_id': 'P%d' % i, 'side': 'SELL', 'quantity': 1.0,
            'entry_spread': levels[4], 'source': 'MANUAL',
            'leg_a': {'volume': 0.1}, 'leg_b': {'volume': 0.1}}
           for i in (1, 2)]
    try:
        publisher.pair_extra = {'max_positions': 2, 'positions': two}
        publisher.publish()
        page.wait_for_selector('.aw-limit', timeout=WAIT)
        assert '2 of 2 positions on' in page.text_content('.aw-limit')
        assert 'Closes always work' in page.text_content('.aw-limit')
        # Control: one on, nothing working - under the limit, not said.
        publisher.pair_extra = {'max_positions': 2, 'positions': two[:1]}
        publisher.publish()
        page.wait_for_function(
            "() => !document.querySelector('.aw-limit')", timeout=WAIT)
        # A working add counts: one on + one working = the limit.
        publisher.pair_extra = {'max_positions': 2, 'positions': two[:1],
                                'orders': [{'order_id': 'W1', 'side': 'SELL',
                                            'level': levels[1],
                                            'quantity': 1.0,
                                            'filled_quantity': 0.0,
                                            'state': 'WORKING',
                                            'position_id': None}]}
        publisher.publish()
        page.wait_for_selector('.aw-limit', timeout=WAIT)
        assert '1 on + 1 working add = 2 of 2' in page.text_content(
            '.aw-limit')
        # Control: the limit off, never said.
        publisher.pair_extra = {'max_positions': 0, 'positions': two}
        publisher.publish()
        page.wait_for_function(
            "() => !document.querySelector('.aw-limit')", timeout=WAIT)
    finally:
        publisher.pair_extra = {}
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_a_derive_that_found_nothing_says_what_held_it_back(page):
    """'derived 0 signal(s)' on its own reads as a broken replay.

    This is the whole reason the engine now returns the reasons: the
    note has to say that the Algo LOOKED and declined, and why, or the
    screen cannot tell a filter doing its job from a derive that never
    ran.
    """
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .an-derive', timeout=WAIT)
    page.evaluate("""() => {
        // Stand in for the engine: the command log and its result.
        window.__realFetch = window.fetch;
        const json = (body) => Promise.resolve(new Response(
            JSON.stringify(body),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        window.fetch = function (url, options) {
            const at = String(url);
            if (at.indexOf('/api/command') >= 0) {
                return json({ok: true, id: 'cmd1'});
            }
            if (at.indexOf('/api/result/') >= 0) {
                return json({ok: true, data: {
                    ok: true, days: 30, written: 0,
                    pairs: {'XAUUSD_|GC1226': {
                        ok: true, derived: 0, replaced: 0,
                        held: {'edge filter': 400, 'trend': 12}}},
                    held: {'edge filter': 400, 'trend': 12}}});
            }
            return window.__realFetch(url, options);
        };
    }""")
    page.click('.monitor .an-derive')
    page.wait_for_selector('.monitor .an-derive-out', timeout=WAIT)
    for _ in range(40):
        note = page.text_content('.monitor .an-derive-out')
        if 'held back' in note:
            break
        page.wait_for_timeout(100)
    note = page.text_content('.monitor .an-derive-out')
    assert 'derived 0 signal(s) over 30 days' in note
    # The counts, biggest first, and the total so the size of it reads.
    assert 'held back 412 time(s)' in note
    # The count is in brackets, not run on after the reason: a reason
    # that ends in a word made "under the nx required 1" of it.
    assert 'edge filter (400)' in note and 'trend (12)' in note
    assert note.index('edge filter') < note.index('trend')
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_the_journal_puts_the_levels_in_columns(page):
    """'Algo: stop loss' on its own cannot tell a stop that was hit
    from a stop that was set in the wrong place \u2014 and a prose line
    under the row cannot be compared with the row above it."""
    page.errors.clear()      # earlier tests raise on purpose
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        s.analysis = {ok: true, names: {'A|B': 'Oil basis'},
          summary: {count: 1, closed: 1, net_pnl: -205.0, win_rate: 0.0,
                    wins: 0, losses: 1},
          drawdown: {}, calibration: {}, shadow: {}, excursion: [],
          signals: [],
          journal: [{position_id: 'P1', pair_key: 'A|B', side: 'SELL',
            closed_at: 1762600000, source: 'ALGO', quantity: 1.0,
            exit_reason: 'Algo: stop loss',
            exit: {break_even: 71.20, tp: 70.55, sl: 71.40,
                   on_level: 'sl',
                   sized_by: '2% of margin (4,720.00/spread)',
                   level_money: -205.0, gap: 0.06, gap_word: 'past',
                   note: null},
            entry_spread: 71.0, exit_spread: 71.46, held_sec: 900,
            entry_z: 2.6, exit_z: 3.1,
            leg_a: {side: 'BUY', entry: 101.118},
            leg_b: {side: 'SELL', entry: 115.554},
            pnl: -205.0, cum_pnl: -205.0}]};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .analysis table', timeout=WAIT)
    text = page.text_content('.monitor .analysis')
    for heading in ('Break-even', 'Target', 'Stop', 'vs level'):
        assert heading in text, heading
    assert '71.2000' in text and '70.5500' in text and '71.4000' in text
    assert '0.0600' in text and 'past' in text
    # The one that fired is marked, and only that one.
    assert page.locator('.monitor .analysis td.on-level').count() == 1
    marked = page.text_content('.monitor .analysis td.on-level')
    assert '71.4000' in marked
    # The setting that put it there is a tooltip, not a column of prose.
    assert '2% of margin' in page.get_attribute(
        '.monitor .analysis td.on-level', 'title')
    # And no prose line survives under the row.
    assert page.locator('.monitor .analysis tr.detail').count() == 0
    assert page.errors == []


def test_a_level_nobody_recorded_is_a_dash_not_a_number(page):
    """CONTROL. Unmeasured is not zero and is not today's settings."""
    page.errors.clear()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        s.analysis = {ok: true, names: {'A|B': 'Oil basis'},
          summary: {}, drawdown: {}, calibration: {}, shadow: {},
          excursion: [], signals: [],
          journal: [{position_id: 'P9', pair_key: 'A|B', side: 'SELL',
            closed_at: 1762600000, source: 'ALGO', quantity: 1.0,
            exit_reason: 'Algo: stop loss',
            exit: {break_even: null, tp: null, sl: null, on_level: 'sl',
                   sized_by: null, level_money: null, gap: null,
                   gap_word: null,
                   note: 'the level it was measured against was not ' +
                         'recorded \u2014 kept from this version on'},
            entry_spread: 71.0, exit_spread: 71.46, held_sec: 900,
            leg_a: {side: 'BUY', entry: 101.118},
            leg_b: {side: 'SELL', entry: 115.554},
            pnl: -205.0, cum_pnl: -205.0}]};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .analysis table', timeout=WAIT)
    assert page.locator('.monitor .analysis td.on-level').count() == 0
    titles = page.locator('.monitor .analysis td.hint[title]')
    assert titles.count() >= 3          # all three levels say why
    assert 'not recorded' in titles.first.get_attribute('title')
    assert page.errors == []


def test_the_signal_journal_reads_like_the_trade_journal(page):
    """The same columns in the same order, so the two halves of the
    page can be read against each other \u2014 and the signals are
    recorded off the live book, not simulated."""
    page.errors.clear()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        s.analysis = {ok: true, names: {'A|B': 'Oil basis'},
          summary: {}, drawdown: {}, calibration: {}, shadow: {},
          excursion: [], journal: [],
          signal_tally: {armed: 2, blocked: 1, entered: 1, no_entry: 0,
                         tp: 0, sl: 1, win_rate: 0.0, net_pnl: -205.0,
                         live: 2, replayed: 0, priced: 1, settled: 1},
          signals: [
            {pair_key: 'A|B', side: 'SELL', source: 'LIVE', state: 'CLOSED',
             outcome: 'SL', armed_at: 1762600000, armed_z: 2.61,
             closed_z: 3.12, entry: 71.0, exit_spread: 71.46,
             break_even: 71.20, tp: 70.55, sl: 71.40,
             quantity: 1.0, held_min: 15.0,
             leg_a_side: 'BUY', leg_a_entry: 101.118,
             leg_b_side: 'SELL', leg_b_entry: 115.554,
             net_pnl: -205.0, cum_pnl: -205.0,
             why: 'never came back to the mean'},
            {pair_key: 'A|B', side: 'SELL', source: 'LIVE', state: 'CLOSED',
             outcome: 'BLOCKED', armed_at: 1762500000, armed_z: 2.55,
             break_even: null, tp: null, sl: null,
             net_pnl: null, cum_pnl: -205.0,
             why: 'edge filter: capture is 0.02x the round trip \u00b7 ' +
               'edge 0.02x of 1.50x'}]};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .analysis table', timeout=WAIT)
    text = page.text_content('.monitor .analysis')
    for heading in ('Size', 'Leg A in', 'Leg B in', 'Running', 'Why',
                    'Break-even'):
        assert heading in text, heading
    assert 'BUY 101.118' in text and 'SELL 115.554' in text
    assert '2.61' in text and '3.12' in text
    # The levels as columns, with the one that fired marked.
    assert '71.2000' in text and '70.5500' in text and '71.4000' in text
    assert page.locator('.monitor .analysis td.on-level').count() == 1
    # The refusal keeps its words, in the WHY column.
    assert 'edge 0.02x of 1.50x' in text
    # Recorded, not simulated: the word that was wrong is gone.
    assert 'hypothetical' not in text.lower()
    assert 'recorded off the live book' in text.lower()
    assert page.errors == []


def write_chart_history(page, key, tf, count=150, age_sec=0.0,
                        volume=False):
    """What the engine writes when the chart asks for MT5's candles.
    `age_sec`: how long ago it was read - its candles end then."""
    from okxtrader import chart
    seconds = tf * 60
    now = time.time() - age_sec
    end = now - now % seconds
    candles, x = [], 59.1
    for i in range(count):
        t = end - seconds * (count - 1 - i)
        o = x
        x += 0.02 * math.sin(i / 6.0) + (0.6 if i == count - 3 else 0)
        candles.append({'t': t, 'o': o, 'h': max(o, x) + 0.01,
                        'l': min(o, x) - 0.01, 'c': x})
        if volume:
            # Busiest near 59.10: the point of control sits there.
            weight = 1000.0 / (1.0 + 400.0 * (x - 59.1) ** 2)
            candles[-1]['f'] = [[o, weight], [x, weight]]
            candles[-1]['v'] = 2 * weight
    path = chart.history_path(page.paths['status'])
    try:
        body = json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError):
        body = {}
    body[f'{key}#{tf}'] = {
        'ok': True, 'pair': key, 'timeframe_min': tf, 'length': 20,
        'entry_z': 2.5, 'stop_z': None, 'algo_timeframe_min': 15,
        'anchor': 0.0, 'at': now, 'candles': candles}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(body, f)


def test_the_chart_opens_and_closes_from_the_ladder(page):
    """15m, 1h and 4h candles of the spread with the Algo's bands round
    them - the mean and the entry - and the z of
    each candle against its own band: whether the spread is past the
    band, on the screen. Opened and closed from the ladder's title bar."""
    key = 'XAUUSD_|GC1226'
    write_chart_history(page, key, 15)
    write_chart_history(page, key, 60)
    page.errors.clear()
    button = page.locator('.ladder[data-pair="%s"] .chart-btn' % key)
    assert page.locator('.window.chartwin').count() == 0
    button.click()
    page.wait_for_selector('.window.chartwin .cw-spread svg', timeout=WAIT)
    win = page.locator('.window.chartwin')
    assert 'Gold basis' in win.locator('.cw-pair').text_content()
    assert '15-minute candles' in win.locator('.cw-size').text_content()
    assert 'on' in win.locator('.cw-tfs button[data-tf="15"]') \
        .get_attribute('class')
    # The spread: candles, the band's middle, +-1 and +-2 sigma, and the
    # entry either side.
    assert win.locator('.cw-spread .cw-candle').count() == 60
    assert win.locator('.cw-spread .cw-mean').count() == 1
    # The +-1 and +-2 sigma lines are not drawn: the entry and the mean
    # are the levels a trade is read against.
    assert win.locator('.cw-spread .cw-band').count() == 0
    assert win.locator('.cw-spread .cw-band-entry').count() == 2
    # z against each candle's own band, with the same levels across.
    assert win.locator('.cw-z .cw-candle').count() == 60
    assert win.locator('.cw-z .cw-sd').count() == 0
    assert win.locator('.cw-z .cw-entry-up').count() == 1
    assert win.locator('.cw-z .cw-entry-down').count() == 1
    assert win.locator('.cw-z .cw-stop').count() == 0     # the z-stop is off
    # The jump near the end closes beyond the entry band, and is marked.
    assert win.locator('.cw-z .cw-candle.beyond').count() >= 1
    assert 'these are its bands' in win.locator('.cw-note').text_content()
    # HOVER: the candle under the pointer, in price and in sigma - its
    # open, high, low and close, the mean and every band line - with a
    # crosshair through it on BOTH charts.
    box = win.locator('.cw-spread').bounding_box()
    page.mouse.move(box['x'] + box['width'] * 0.6,
                    box['y'] + box['height'] * 0.5)
    page.wait_for_selector('.chartwin .cw-tip:not([hidden])', timeout=WAIT)
    tip = win.locator('.cw-tip').text_content()
    for word in ('Price', 'Score', 'Open', 'High', 'Low', 'Close', 'Range',
                 'Mean', '+ entry', '\u2212 entry', '15-minute candles'):
        assert word in tip, word
    # Each price with its score, a plain number - no sigma symbol.
    values = win.locator('.cw-tip td').all_text_contents()
    assert values and not any('\u03c3' in v for v in values), values
    scores = win.locator('.cw-tip td.z').all_text_contents()
    # The bands' own scores: the entry either side, and the mean at 0.
    assert {'+2.50', '0.00', '-2.50'} <= set(scores), scores
    # The bands it gives are the entry and the mean - nothing between.
    labels = win.locator('.cw-tip th').all_text_contents()
    for word in ('+2\u03c3', '+1\u03c3', '\u22121\u03c3', '\u22122\u03c3', '1\u03c3'):
        assert word not in labels, word
    assert win.locator('.cw-z .cw-xline:not([hidden])').count() == 1
    assert win.locator('.cw-spread .cw-xline:not([hidden])').count() == 1
    # The level under the pointer, read off the axis it is over.
    assert win.locator('.cw-spread .cw-ylabel:not([hidden])').count() == 1
    # The readout is the candle's own numbers: its high is its high.
    readout = page.evaluate('''() => {
        const node = document.querySelector('.chartwin');
        const tip = node.querySelector('.cw-tip').textContent;
        const shown = node._drawn.shown;
        return {tip, highs: shown.map(c => c.h.toFixed(4))};
    }''')
    assert any(h in readout['tip'] for h in readout['highs'])
    # Away from the chart, it all goes.
    page.mouse.move(box['x'] - 40, box['y'] - 40)
    page.wait_for_timeout(150)
    assert win.locator('.cw-tip:not([hidden])').count() == 0
    assert win.locator('.cw-xline:not([hidden])').count() == 0
    assert 'Min:' in win.text_content() and 'Max:' in win.text_content()
    assert 'on' in button.get_attribute('class')
    # 1h candles: the same chart, read off the 1-hour history.
    win.locator('.cw-tfs button[data-tf="60"]').click()
    page.wait_for_function(
        "document.querySelector('.chartwin .cw-size').textContent"
        ".indexOf('1-hour') === 0", timeout=WAIT)
    page.wait_for_function(
        "document.querySelector('.chartwin .cw-note').textContent"
        ".indexOf('the Algo itself reads 15-minute') >= 0", timeout=WAIT)
    # Closed by the trader: the same button...
    button.click()
    page.wait_for_timeout(200)
    assert page.locator('.window.chartwin').count() == 0
    assert 'on' not in button.get_attribute('class').split()
    # ...or the window's own close.
    button.click()
    page.wait_for_selector('.window.chartwin', timeout=WAIT)
    page.click('.window.chartwin .close')
    page.wait_for_timeout(200)
    assert page.locator('.window.chartwin').count() == 0
    assert page.errors == []


def test_the_signal_journal_names_the_accounts_the_edge_and_hides_the_unconfirmed(
        page):
    """Each row says which account at which broker each leg was on and
    what the edge filter read; a stretch armed and let go without ever
    confirming is not a signal, so it is not listed until asked for."""
    page.errors.clear()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        s.showUnconfirmed = false;
        s.analysis = {ok: true, names: {'A|B': 'Oil basis'},
          summary: {}, drawdown: {}, calibration: {}, shadow: {},
          excursion: [], journal: [],
          signal_tally: {armed: 2, blocked: 1, entered: 0, no_entry: 1},
          signals: [
            {pair_key: 'A|B', side: 'SELL', source: 'LIVE', state: 'CLOSED',
             outcome: 'BLOCKED', armed_at: 1762500000, armed_z: 2.55,
             armed_spread: -33.9484,
             leg_a_account: 'CFI', leg_a_broker: 'CFI Financial Group',
             leg_b_account: 'FxPro', leg_b_broker: 'FxPro Financial',
             edge_ratio: 0.42, edge_required: 1.5, edge_on: true,
             net_pnl: null, cum_pnl: null, why: 'edge filter'},
            {pair_key: 'A|B', side: 'BUY', source: 'LIVE', state: 'CLOSED',
             outcome: 'NO_ENTRY', armed_at: 1762400000, armed_z: -7.73,
             edge_ratio: 2.1, edge_required: 1.5, edge_on: false,
             net_pnl: null, cum_pnl: null, why: 'came back'}]};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .analysis table', timeout=WAIT)
    text = page.text_content('.monitor .analysis')
    assert 'A: CFI · CFI Financial Group' in text
    assert 'B: FxPro · FxPro Financial' in text
    assert '0.42× / 1.50×' in text
    # The spread price that z was read off, blocked as it was - in its
    # own column, beside the z.
    heads = page.locator('.monitor .analysis table', has_text='Spread at z') \
        .locator('th').all_text_contents()
    assert heads.index('Spread at z') == heads.index('z in → out') + 1
    assert '-33.9484' in text
    # The unconfirmed stretch is counted, not listed...
    assert '-7.73' not in text
    assert '1 stretch armed and let go without confirming' in text
    # ...and one click lists it, with its edge and the filter said OFF.
    page.click('.monitor .an-toggle-unconfirmed')
    text = page.text_content('.monitor .analysis')
    assert '-7.73' in text
    assert '2.10× / 1.50× (off)' in text
    page.click('.monitor .an-toggle-unconfirmed')
    assert '-7.73' not in page.text_content('.monitor .analysis')
    assert page.errors == []


def test_the_analysis_page_shows_the_notional_traded(page):
    """Today, 7 days and 30 days, per ladder and in all - compact, the
    exact figure and each leg in the cell's title, and a deal that could
    not be priced flagged rather than dropped."""
    page.errors.clear()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        const cell = (n, a, b, fills, lots, unpriced) => ({notional: n,
            a: a, b: b, fills: fills, lots: lots, unpriced: unpriced || 0});
        s.analysis = {ok: true, names: {'A|B': 'Oil basis', 'C|D': 'Gold'},
          summary: {}, drawdown: {}, calibration: {}, shadow: {},
          excursion: [], journal: [], signals: [], signal_tally: {},
          notional: {
            periods: [{key: 'today', label: 'Today'},
                      {key: '7d', label: 'Last 7 days'},
                      {key: '30d', label: 'Last 30 days'}],
            rows: [
              {pair_key: 'A|B', today: cell(1234567, 600000, 634567, 4, 2),
               '7d': cell(9876543, 5e6, 4876543, 30, 15),
               '30d': cell(45e6, 22e6, 23e6, 120, 60)},
              {pair_key: 'C|D', today: cell(0, 0, 0, 0, 0),
               '7d': cell(250000, 125000, 125000, 2, 1),
               '30d': cell(250000, 125000, 125000, 3, 1, 1)}],
            accounts: [
              {account: 'CFI 19514405', today: cell(600000, 600000, 0, 2, 1),
               '7d': cell(5e6, 5e6, 0, 15, 8), '30d': cell(22e6, 22e6, 0, 60, 30)}],
            account_info: {'CFI 19514405': {login: 19514405,
                                            broker: 'CFI Financial Group'}},
            total: {today: cell(1234567, 600000, 634567, 4, 2),
                    '7d': cell(10126543, 5125000, 5001543, 32, 16),
                    '30d': cell(45250000, 22125000, 23125000, 123, 61, 1)}}};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .an-notional', timeout=WAIT)
    text = page.text_content('.monitor .an-notional')
    for word in ('Today', 'Last 7 days', 'Last 30 days', 'Oil basis', 'Gold',
                 'All ladders', '1.23M', '9.88M', '45.00M', '250.00K',
                 '4 deals'):
        assert word in text, word
    title = page.locator('.monitor .an-notional td[title]').first \
        .get_attribute('title')
    assert '1,234,567' in title and 'leg A 600,000' in title
    # A ladder that traded nothing today shows a dash there, not 0.
    gold = page.locator('.monitor .an-notional tr', has_text='Gold')
    assert gold.locator('td').nth(1).text_content().strip() == '\u2014'
    assert 'counted but not priced' in page.text_content('.monitor .analysis')
    # Per ACCOUNT: the login and the broker it is at.
    accounts = page.text_content('.monitor .an-notional-accounts')
    assert 'CFI 19514405' in accounts and '#19514405' in accounts
    assert 'CFI Financial Group' in accounts and '600.00K' in accounts
    # Nothing priced at all is unmeasured: a dash, never a 0.
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.analysis.notional.rows[1]['30d'] = {notional: 0, a: 0, b: 0,
            fills: 3, lots: 1, unpriced: 3};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    gold = page.locator('.monitor .an-notional tr', has_text='Gold')
    assert gold.locator('td').nth(3).locator('b').text_content() == '\u2014'
    # A day of the trader's choosing: the picker asks the page for it.
    # `fill` fires the change itself: ONE reload each, waited for.
    with page.expect_response(lambda r: '/api/analysis' in r.url,
                              timeout=WAIT) as reply:
        page.fill('.monitor .an-volume-from', '2026-11-03')
    assert 'volume_from=2026-11-03' in reply.value.url
    with page.expect_response(lambda r: '/api/analysis' in r.url,
                              timeout=WAIT) as reply:
        page.fill('.monitor .an-volume-to', '2026-11-05')
    assert 'volume_from=2026-11-03' in reply.value.url
    assert 'volume_to=2026-11-05' in reply.value.url
    # Leave the page as found: the reload this asked for has landed and
    # is not left to arrive in the next test's data, nor the day picked.
    page.wait_for_timeout(200)
    # ...and the focus off the picker: the pane is not rebuilt under a
    # field being typed into, so a focused picker freezes it.
    page.evaluate("() => { const s = window.MT5Trader.state;"
                  " s.volumeFrom = ''; s.volumeTo = '';"
                  " document.activeElement.blur(); }")
    assert page.errors == []


def test_the_monitor_takes_the_room_there_is(page):
    """A flat 560px default left the bottom third of the screen as bare
    desktop under a tab that is several screens long."""
    page.evaluate("""() => {
        window.localStorage.removeItem('okxtrader.windows.v1');
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .analysis', timeout=WAIT)
    room = page.evaluate("""() => {
        const w = document.querySelector('.window.monitor');
        const d = document.getElementById('desktop');
        return d.getBoundingClientRect().bottom -
               w.getBoundingClientRect().bottom;
    }""")
    # Within one window's furniture of the foot of the desktop, not a
    # third of the screen away.
    assert 0 <= room < 120, room


def test_the_z_trigger_count_is_on_the_page_with_its_threshold(page):
    """'How many times did the z trigger' — split by side, with the
    band it was counted against named rather than left to be guessed."""
    page.errors.clear()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        s.analysis = {ok: true, names: {'A|B': 'Oil basis'},
          summary: {}, drawdown: {}, calibration: {}, shadow: {},
          excursion: [], journal: [], signals: [],
          signal_tally: {armed: 9, triggered: 9, triggered_up: 6,
                         triggered_down: 3, threshold_z: 2.5,
                         peak_z: 3.41, trough_z: -2.88,
                         blocked: 2, entered: 5, no_entry: 2, tp: 3, sl: 2,
                         win_rate: 60.0, net_pnl: 410.0, live: 9,
                         replayed: 0, priced: 5, settled: 5}};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .analysis .an-tiles', timeout=WAIT)
    text = page.text_content('.monitor .analysis')
    assert 'z triggered ±2.50σ' in text
    assert '↑ above' in text and '↓ below' in text
    assert 'Furthest z' in text and '3.41σ' in text
    # The two sides add up to the total, on the screen as in the engine.
    assert '9' in text and '6' in text and '3' in text
    assert page.errors == []


def test_two_ladders_that_disagree_name_no_band_on_the_tile(page):
    """CONTROL. Printing either threshold would be wrong on half the
    rows, so the tile names none."""
    page.errors.clear()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        s.analysis = {ok: true, names: {}, summary: {}, drawdown: {},
          calibration: {}, shadow: {}, excursion: [], journal: [],
          signals: [],
          signal_tally: {armed: 4, triggered: 4, triggered_up: 2,
                         triggered_down: 2, threshold_z: null,
                         peak_z: null, trough_z: null, blocked: 0,
                         entered: 0, no_entry: 0, tp: 0, sl: 0,
                         win_rate: null, net_pnl: null, live: 4,
                         replayed: 0, priced: 0, settled: 0}};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .analysis .an-tiles', timeout=WAIT)
    text = page.text_content('.monitor .analysis')
    assert 'z triggered' in text and 'σ' not in text.split('Blocked')[0]
    assert page.errors == []


def test_a_derive_that_found_nothing_reads_as_a_sentence(page):
    """The engine groups held-back reasons with their NUMBERS taken
    out, writing '#' where they were. On a live desk that reached the
    screen as 'edge filter: capture #x the cost, under the #x required
    1' — which reads as a fault, and runs the count straight on
    after the word 'required'."""
    page.errors.clear()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .an-derive', timeout=WAIT)
    page.evaluate("""() => {
        // An earlier test in this module leaves its own note behind,
        // and it also says 'held back'.
        window.MT5Trader.state.deriveNote = null;
        window.MT5Trader.state.deriving = false;
        window.MT5Trader.render();
        window.__realFetch = window.fetch;
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        window.fetch = function (url, options) {
            const at = String(url);
            if (at.indexOf('/api/command') >= 0) {
                return json({ok: true, id: 'cmd1'});
            }
            if (at.indexOf('/api/result/') >= 0) {
                return json({ok: true, data: {
                    ok: true, days: 30, written: 0,
                    pairs: {'A|B': {ok: true, derived: 0, replaced: 0}},
                    held: {'edge filter: capture #x the cost, under the #x required': 1}}});
            }
            return window.__realFetch(url, options);
        };
    }""")
    page.click('.monitor .an-derive')
    for _ in range(40):
        note = page.text_content('.monitor .an-derive-out')
        if 'held back' in note:
            break
        page.wait_for_timeout(100)
    note = page.text_content('.monitor .an-derive-out')
    assert 'capture nx the cost, under the nx required (1)' in note
    assert '#' not in note
    assert 'required 1' not in note
    page.evaluate('() => { window.fetch = window.__realFetch; }')
    assert page.errors == []


# -- market sessions -----------------------------------------------------------

def _sessions_at(page, iso):
    """Stand the sessions clock at `iso` (UTC) and repaint."""
    page.evaluate("""(iso) => {
        window.MT5Sessions.now = () => new Date(iso);
        window.MT5Sessions.tick();
    }""", iso)
    return page.evaluate("(iso) => window.MT5Sessions.snapshot(new Date(iso))",
                         iso)


def _clear_session_alerts(page):
    page.evaluate("""() => {
        try { Object.keys(localStorage).filter(k => k.startsWith('mkt:'))
                .forEach(k => localStorage.removeItem(k)); } catch (e) {}
        document.getElementById('toasts').innerHTML = '';
    }""")


def _restore_sessions_clock(page):
    page.evaluate("""() => {
        window.MT5Sessions.now = () => new Date('2026-11-21T12:00:00Z');
        window.MT5Sessions.tick();
        document.getElementById('toasts').innerHTML = '';
        document.getElementById('mkt-panel').hidden = true;
    }""")


def test_the_strip_counts_down_to_the_centres_and_okx_funding(page):
    open_ladder(page)
    try:
        # Thu 01:00 UTC: Tokyo (09:00 JST = 00:00 UTC) has been open an
        # hour; Hong Kong opens at 09:30 HKT = 01:30 UTC.
        snap = _sessions_at(page, '2026-11-19T01:00:00Z')
        rows = {r['name']: r for r in snap['rows']}
        assert list(rows) == ['Tokyo', 'Hong Kong', 'London', 'New York',
                              'OKX funding']
        assert rows['Tokyo']['open'] and rows['Tokyo']['tone'] == 'open'
        assert rows['Hong Kong']['tone'] == 'soon'
        assert round(rows['Hong Kong']['toOpen']) == 30
        assert snap['headline'] == 'Next: Hong Kong opens in 30m'
        # Funding is the next of 00:00 / 08:00 / 16:00 UTC: 08:00.
        assert rows['OKX funding']['at'] == '08:00'
        assert round(rows['OKX funding']['toOpen']) == 7 * 60
        strip = page.text_content('#mkt-strip')
        assert 'Hong Kong Thu 09:00 · opens in 30m' in strip
        assert 'OKX funding 08:00 UTC · in 7h 0m' in strip
        assert page.locator('#mkt-strip .mkt-chip').count() == 5
        # Thu 14:40 UTC: New York (09:30 ET) opened 10 minutes ago.
        snap = _sessions_at(page, '2026-11-19T14:40:00Z')
        rows = {r['name']: r for r in snap['rows']}
        assert rows['New York']['tone'] == 'hot'
        assert rows['London']['tone'] == 'open'
        assert snap['headline'] == ('New York opened 10m ago — expect '
                                    'volatility')
        assert 'JUST OPENED · closes in 6h 20m' in page.text_content(
            '#mkt-strip')
    finally:
        _restore_sessions_clock(page)


def test_new_york_daylight_saving_moves_the_open_by_itself(page):
    """09:30 in New York is 14:30 UTC in November and 13:30 in July."""
    open_ladder(page)
    try:
        summer = {r['name']: r for r in
                  _sessions_at(page, '2026-07-15T13:35:00Z')['rows']}
        assert summer['New York']['open'] and \
            summer['New York']['tone'] == 'hot'
        winter = {r['name']: r for r in
                  _sessions_at(page, '2026-11-18T13:35:00Z')['rows']}
        assert not winter['New York']['open']          # still 08:35 ET
        assert round(winter['New York']['toOpen']) == 55
    finally:
        _restore_sessions_clock(page)


def test_the_weekend_counts_the_centres_to_monday_but_funding_goes_on(page):
    open_ladder(page)
    try:
        # Saturday noon UTC: every centre shut; Tokyo opens Monday 09:00
        # JST = Sunday 00:00 UTC, 36h away. Funding is at 16:00 today.
        rows = {r['name']: r for r in
                _sessions_at(page, '2026-11-21T12:00:00Z')['rows']}
        assert not any(r['open'] for r in rows.values())
        assert round(rows['Tokyo']['toOpen']) == 36 * 60
        assert round(rows['OKX funding']['toOpen']) == 4 * 60
    finally:
        _restore_sessions_clock(page)


def test_ten_minutes_before_an_open_a_pop_up_says_so_once(page):
    open_ladder(page)
    _clear_session_alerts(page)
    try:
        # Control: half an hour before London, too early - nothing said.
        _sessions_at(page, '2026-11-19T07:30:00Z')
        assert page.locator('#toasts .toast').count() == 0
        # 8 minutes before London's 08:00 - which is also the 08:00 UTC
        # funding: both said.
        _sessions_at(page, '2026-11-19T07:52:00Z')
        page.wait_for_selector('#toasts .toast', timeout=WAIT)
        text = page.text_content('#toasts')
        assert 'London opens in 8m' in text
        assert 'OKX funding in 8m (08:00 UTC)' in text
        # ...and only once, however many seconds tick by.
        _sessions_at(page, '2026-11-19T07:53:00Z')
        assert page.locator('#toasts .toast').count() == 2
    finally:
        _clear_session_alerts(page)
        _restore_sessions_clock(page)


def test_funding_is_alerted_on_a_saturday_too(page):
    open_ladder(page)
    _clear_session_alerts(page)
    try:
        _sessions_at(page, '2026-11-21T15:55:00Z')
        page.wait_for_selector('#toasts .toast', timeout=WAIT)
        assert 'OKX funding in 5m (16:00 UTC)' in page.text_content(
            '#toasts')
    finally:
        _clear_session_alerts(page)
        _restore_sessions_clock(page)


def test_ten_minutes_before_new_yorks_close_a_pop_up_says_so(page):
    open_ladder(page)
    _clear_session_alerts(page)
    try:
        # 16:00 ET is 21:00 UTC in November.
        _sessions_at(page, '2026-11-19T20:55:00Z')
        page.wait_for_selector('#toasts .toast', timeout=WAIT)
        assert 'New York closes in 5m (16:00 local)' in page.text_content(
            '#toasts')
        # Control: London's close is not one it alerts on.
        _clear_session_alerts(page)
        _sessions_at(page, '2026-11-19T16:25:00Z')
        assert page.locator('#toasts .toast').count() == 0
    finally:
        _clear_session_alerts(page)
        _restore_sessions_clock(page)


def test_sessions_opens_a_timeline_with_the_moment_marked(page):
    open_ladder(page)
    try:
        _sessions_at(page, '2026-11-19T14:40:00Z')
        assert page.evaluate(
            "() => document.getElementById('mkt-panel').hidden") is True
        page.click('#mkt-strip .mkt-more')
        page.wait_for_selector('#mkt-panel .mkt-row', timeout=WAIT)
        assert page.locator('#mkt-panel .mkt-row').count() == 5
        assert page.locator('#mkt-panel .mkt-now').count() == 1
        # New York's bar starts at 14:30 UTC in November.
        left = page.evaluate("""() => document.querySelectorAll(
            '#mkt-panel .mkt-row')[3].querySelector('.mkt-bar').style.left""")
        assert left.startswith('60.41'), left
        # Funding: three marks, at 00:00, 08:00 and 16:00 UTC.
        marks = page.evaluate("""() => Array.from(document.querySelectorAll(
            '#mkt-panel .mkt-row')[4].querySelectorAll('.mkt-bar'))
            .map(b => b.style.left)""")
        assert [m[:5] for m in marks] == ['0%', '33.33', '66.66'], marks
        page.click('#mkt-panel .mkt-close')
        assert page.evaluate(
            "() => document.getElementById('mkt-panel').hidden") is True
    finally:
        _restore_sessions_clock(page)


# -- the Exchanges page: an OKX account ----------------------------------------

def test_an_okx_account_is_saved_from_its_row_and_its_key_leaves_the_page(page):
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .okx-accounts')
    row = page.locator('.window.settings tr.okx-new')
    row.locator('.f-name').fill('OKX Desk')
    row.locator('.f-demo').select_option('demo')
    row.locator('.f-key').fill('key-abc')
    row.locator('.f-secret').fill('secret-def')
    row.locator('.f-passphrase').fill('pass-ghi')
    # A closed local port: this desk must never reach out to OKX itself.
    row.locator('.f-baseurl').fill('http://127.0.0.1:9')
    row.locator('.save-account').click()
    saved = 'tr[data-account="OKX Desk"][data-exchange="okx"]'
    page.wait_for_selector(saved, timeout=WAIT)
    # The row says the parts are SET, and where - never what they are.
    text = page.inner_html(saved)
    assert 'OKX_API_KEY_OKX_DESK' in text and 'saved — type to replace' in text
    assert 'secret-def' not in page.content()
    for cls in ('.f-key', '.f-secret', '.f-passphrase'):
        assert page.locator(saved + ' ' + cls).input_value() == ''
    # Test and Diagnose are on it; Connect is an MT5 question.
    assert page.locator(saved + ' .test-account').count() == 1
    assert page.locator(saved + ' .diagnose-account').count() == 1
    assert page.locator(saved + ' .connect-account').count() == 0


def _okx_account(page, name='OKX Check'):
    """An OKX account saved through the page's own API, pointed at a
    closed local port so nothing here ever reaches OKX itself."""
    page.evaluate("""async (name) => {
        await fetch('/api/accounts/' + encodeURIComponent(name), {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({exchange: 'okx', demo: true,
                                  base_url: 'http://127.0.0.1:9'})});
        await window.MT5Settings.refresh();
    }""", name)
    page.wait_for_selector(f'tr[data-account="{name}"]', timeout=WAIT)


def test_the_exchanges_page_carries_test_and_diagnose_on_an_okx_row(page):
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    _okx_account(page)
    row = page.locator('tr[data-account="OKX Check"]')
    for name in ('test-account', 'diagnose-account', 'save-account',
                 'delete-account'):
        assert row.locator('.' + name).count() == 1
    assert 'Exchanges' in page.text_content('.window.settings .accounts h3')


def test_a_check_shows_its_answer_as_a_checklist_with_fixes(page):
    """A checklist that only says FAIL is one that sends the operator
    to a forum: OKX's own code, and what to do about it."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    _okx_account(page)
    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/test') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: false, overall: 'FAIL', passed: 1, warnings: 0,
                    failed: 1, connected: false,
                    checks: [
                        {scope: 'OKX Check', name: 'OKX reachable',
                         status: 'PASS', message: 'OKX demo trading answered',
                         fix: []},
                        {scope: 'OKX Check', name: 'API key',
                         status: 'FAIL', message: 'OKX 50113: Invalid Sign',
                         fix: ['The signature did not match - the SECRET '
                               + 'KEY is wrong. Paste it again']}
                    ]}), {status: 200,
                          headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
    }""")
    page.locator('tr[data-account="OKX Check"] .test-account').click()
    page.wait_for_selector('tr.checklist', timeout=WAIT)
    text = page.text_content('tr.checklist')
    assert '50113' in text                        # OKX's own words
    assert 'SECRET KEY is wrong' in text          # ...and what to do
    assert page.locator('tr.c-fail').count() == 1
    assert page.locator('tr.c-pass').count() == 1
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_an_error_toast_is_given_a_timer_so_refusals_stop_stacking(page):
    """An error toast lasts longer than a success one, but it does not
    stay forever: a refusal, read, then gone."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .okx-accounts')
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.evaluate("""() => {
        window.__delays = [];
        window.__realTimeout = window.setTimeout;
        window.setTimeout = function (fn, ms) {
            window.__delays.push(ms);
            return window.__realTimeout(fn, 999999);
        };
    }""")
    try:
        row = page.locator('.window.settings tr.okx-new')
        row.locator('.f-name').fill('Bad URL')
        row.locator('.f-baseurl').fill('ftp://not-okx')
        row.locator('.save-account').click()
        page.wait_for_selector('.toast:not(.ok)')
        assert 'https://' in page.text_content('.toast:not(.ok)')
        delays = page.evaluate('() => window.__delays')
        assert delays, 'the error toast was given no timer at all'
        longest = max(delays)
        assert 5000 <= longest <= 20000, (
            f'{longest}ms is not "readable, then gone": {delays}')
    finally:
        page.evaluate("""() => {
            if (window.__realTimeout) { window.setTimeout = window.__realTimeout; }
            document.getElementById('toasts').innerHTML = '';
        }""")


# -- the Slippage page -----------------------------------------------------------

def _slip_report(**over):
    """A report as `/api/slippage` builds it, from the real report code."""
    from okxtrader import slippage
    from test_slippage_split import position
    body = slippage.report([position(**over)], names={'OIL': 'USOIL|UKOIL'},
                           window={'label': 'since Wed 18 Nov 16:55 broker '
                                            'time', 'clock': 'broker',
                                   'note': ''})
    body['journal'] = {'fills': 4}
    body['account_info'] = {'CFI': {'login': 19514405,
                                    'broker': 'CFI Financial Markets'},
                            'AC': {'login': 7001, 'broker': 'AC Markets'}}
    return body


def _show_slippage(page, report):
    page.evaluate("""(report) => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:']; s.monitorTab = 'slippage';
        s.slippage = report; s.slippageAt = Date.now() + 60000;
        window.MT5Trader.render();
    }""", report)
    page.wait_for_selector('.monitor .slip-cards', timeout=WAIT)


def test_the_slippage_page_says_what_it_cost_and_where_it_came_from(page):
    open_ladder(page)
    try:
        _show_slippage(page, _slip_report())
        cards = page.text_content('.monitor .slip-cards')
        assert '$10.00' in cards and '$2.00' in cards and '$8.00' in cards
        assert '7.4%' in cards and '752 ms' in cards
        bars = page.text_content('.monitor .slip-splits')
        assert 'market moved while sending' in bars
        assert 'broker fill' not in bars             # the broker cost nothing
        pane = page.text_content('.monitor .pane')
        assert '#19514405 · CFI Financial Markets' in pane
        assert '#7001 · AC Markets' in pane
        assert 'USOIL|UKOIL' in pane and '$124.30' in pane   # net P&L
        # The full statistics are there, folded away.
        assert page.evaluate(
            "() => document.querySelector('.slip-detail').open") is False
        assert 'Round turn' in pane and 'Entries by ladder' in pane
        # EVERY FILL, in one line and then one row each: sent against
        # filled, per leg, per end.
        head = page.text_content('.monitor .slip-fills-head')
        assert 'leg fills filled exactly at the price sent' in head
        heads = page.locator('.monitor .slip-fills th').all_text_contents()
        for word in ('Sent at', 'Filled at', 'Slip', 'bp', '$'):
            assert word in heads, word
        assert page.locator('.monitor .slip-fills tbody tr').count() >= 2
    finally:
        page.evaluate("""() => { const s = window.MT5Trader.state;
            s.slippage = null; s.monitorTab = 'positions';
            s.open = []; window.MT5Trader.render(); }""")


def test_a_broker_that_filled_off_its_price_shows_as_the_broker(page):
    open_ladder(page)
    try:
        _show_slippage(page, _slip_report(fill_b=91.999))
        assert 'broker fill' in page.text_content('.monitor .slip-splits')
        # Control: a leg with no send price is unmeasured, said in words.
        report = _slip_report()
        for row in report['rows']:
            for end in ('entry', 'exit'):
                row[end + '_broker_money'] = row[end + '_moved_money'] = None
        report['split'] = {'entry': {'measured': 0},
                           'exit': {'measured': 0}}
        _show_slippage(page, report)
        assert 'not measured' in page.text_content('.monitor .slip-splits')
    finally:
        page.evaluate("""() => { const s = window.MT5Trader.state;
            s.slippage = null; s.monitorTab = 'positions';
            s.open = []; window.MT5Trader.render(); }""")


# -- profit % on margin --------------------------------------------------------

def _open_position(**over):
    leg = lambda side, price: {'side': side, 'volume': 1.0,
                               'contract_size': 1000.0, 'price': price,
                               'symbol': 'X', 'account': 'acct_a',
                               'position_tickets': [], 'order_ticket': None}
    base = {'position_id': 'PX', 'side': 'SELL', 'quantity': 2.0,
            'entry_spread': 9.76, 'closing_spread': 9.90, 'net_pnl': -280.0,
            'order_type': 'LIMIT', 'entry_slippage': 0.002,
            'click_to_on_ms': 752.0, 'entry_margin': 1740.0,
            'entry_margin_source': 'terminal',
            'leg_a': leg('BUY', 82.0), 'leg_b': leg('SELL', 92.0)}
    base.update(over)
    return base


def test_the_positions_tab_shows_profit_on_both_legs_margin(page):
    open_ladder(page)
    publisher = page.paths['publisher']
    try:
        publisher.positions = [_open_position()]
        publisher.publish()
        page.wait_for_function(
            "() => (window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
            ".positions || []).length === 1", timeout=WAIT)
        page.evaluate("""() => { const s = window.MT5Trader.state;
            s.open = ['monitor:']; s.monitorTab = 'positions';
            window.MT5Trader.render(); }""")
        page.wait_for_selector('.monitor tr[data-position="PX"]',
                               timeout=WAIT)
        row = page.text_content('.monitor tr[data-position="PX"]')
        assert '-8.05%' in row and '$3,480.00' in row     # -280 / (1,740 x 2)
        assert 'est.' not in row
        # An estimated margin says so; no margin is a dash, never 0%.
        publisher.positions = [_open_position(entry_margin_source='leverage')]
        publisher.publish()
        page.wait_for_function(
            "() => (window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
            ".positions || [{}])[0].entry_margin_source === 'leverage'",
            timeout=WAIT)
        page.evaluate("() => window.MT5Trader.render()")
        page.wait_for_function(
            "() => (document.querySelector('.monitor tr[data-position=\"PX\"]')"
            " || {textContent: ''}).textContent.includes('est.')",
            timeout=WAIT)
        publisher.positions = [_open_position(entry_margin=None)]
        publisher.publish()
        page.wait_for_function(
            "() => ((window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
            ".positions || [{}])[0] || {}).entry_margin === null",
            timeout=WAIT)
        page.evaluate("() => window.MT5Trader.render()")
        page.wait_for_function(
            "() => !(document.querySelector('.monitor tr[data-position=\"PX\"]')"
            " || {textContent: '%'}).textContent.includes('-8.05%')",
            timeout=WAIT)
        assert '0.00%' not in page.text_content(
            '.monitor tr[data-position="PX"]')
    finally:
        publisher.positions = None
        publisher.publish()
        page.evaluate("""() => { const s = window.MT5Trader.state;
            s.open = []; window.MT5Trader.render(); }""")


def test_the_algo_window_says_the_pnl_as_a_share_of_margin(page):
    open_ladder(page)
    publisher = page.paths['publisher']
    levels = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows.map(r => r.level)""")
    held = dict(_combined_short(levels), net_pnl=-280.0, entry_margin=1740.0,
                entry_margin_source='terminal')
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block(state='IN_POSITION', signal=None,
                                       positions=[held])
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-signal')"
            " || {textContent: ''}).textContent.includes('margin)')",
            timeout=WAIT)
        assert '(-8.05% margin)' in page.text_content(
            '.window.algowin .aw-signal')
        # Control: no margin priced, no share shown.
        held['entry_margin'] = None
        publisher.publish()
        page.wait_for_function(
            "() => !(document.querySelector('.window.algowin .aw-signal')"
            " || {textContent: 'margin)'}).textContent.includes('margin)')",
            timeout=WAIT)
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_a_chart_on_an_out_of_date_history_says_so_and_why(page):
    """The 15m chart drew a history fifteen hours old, its candles
    stopping mid-afternoon and the live ones alone at the right - and
    said nothing. Out of date, the window now says how far OKX's candles
    go and why the refresh failed."""
    key = 'XAUUSD_|GC1226'
    write_chart_history(page, key, 15, age_sec=15 * 3600)
    page.errors.clear()
    page.locator('.ladder[data-pair="%s"] .chart-btn' % key).click()
    page.wait_for_selector('.window.chartwin .cw-spread svg', timeout=WAIT)
    # 15m, whatever an earlier test left it on.
    page.locator('.window.chartwin .cw-tfs button[data-tf="15"]').click()
    size = page.locator('.window.chartwin .cw-size')
    try:
        page.wait_for_function(
            "() => /history only to .+ - /.test(document.querySelector("
            "'.window.chartwin .cw-size').textContent)", timeout=WAIT)
    except Exception:
        raise AssertionError(size.text_content())
    assert 'cw-stale' in size.get_attribute('class')
    # A fresh one clears it.
    write_chart_history(page, key, 15)
    page.locator('.window.chartwin .cw-tfs button[data-tf="15"]').click()
    try:
        page.wait_for_function(
            "() => !document.querySelector('.window.chartwin .cw-size')"
            ".classList.contains('cw-stale')", timeout=WAIT)
    except Exception:
        raise AssertionError('still: ' + size.text_content())
    assert page.errors == []
    page.locator('.window.chartwin .close').click()


def test_the_analysis_page_reads_trades_against_the_capital_managed(page):
    """A box for the capital the trader manages, saved as the
    CAPITAL_MANAGED setting; return on capital and capital used in the
    summary, and per trade the capital used, the exposure and the return
    on capital beside the return on margin."""
    page.errors.clear()
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'analysis';
        delete s.capitalManaged;
        const leg = {side: 'BUY', entry: 1.0};
        s.analysis = {ok: true, names: {'A|B': 'ETH vs BTC'},
          summary: {count: 1, closed: 1, net_pnl: 70, wins: 1, losses: 0,
                    win_rate: 1, return_on_margin: 40, margin_trades: 1,
                    capital: 3500, return_on_capital: 2,
                    capital_used_avg: 5, capital_used_max: 5},
          drawdown: {}, calibration: {}, shadow: {}, excursion: [],
          signals: [], signal_tally: {},
          journal: [{pair_key: 'A|B', side: 'BUY', source: 'ALGO',
                     quantity: 3, closed_at: Date.now() / 1000, pnl: 70,
                     cum_pnl: 70, leg_a: leg, leg_b: leg, exit: {},
                     margin: 175, notional: 3500, pct_margin: 40,
                     pct_notional: 2, pct_capital: 2,
                     pct_capital_used: 5, exposure: 1}]};
        s.analysisAt = Date.now();
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .an-capital', timeout=WAIT)
    assert page.locator('.monitor .an-capital').input_value() == '3500'
    tiles = page.text_content('.monitor .an-tiles')
    assert 'Return on capital' in tiles and '+2.00%' in tiles
    assert 'Capital used' in tiles and '5.0%' in tiles
    table = page.locator('.monitor table.an-table', has_text='% on capital')
    heads = table.locator('th').all_text_contents()
    for word in ('Capital used', 'Exposure', 'Net % on capital',
                 'Net % on margin', 'Net % of notional'):
        assert word in heads, word
    row = table.locator('tbody tr').first.text_content()
    assert '5.0%' in row and '1.00x' in row and '+2.00%' in row
    # Typing a capital saves it as the setting.
    box = page.locator('.monitor .an-capital')
    box.fill('5000')
    box.press('Enter')
    box.evaluate('b => b.blur()')
    for _ in range(50):
        saved = json.load(open(page.paths['config'], encoding='utf-8'))
        if (saved.get('settings') or {}).get('CAPITAL_MANAGED') == 5000:
            break
        time.sleep(0.1)
    assert saved['settings']['CAPITAL_MANAGED'] == 5000
    assert page.errors == []


def test_the_price_tag_sits_beside_the_newest_candle_not_on_it(page):
    """The 'now' tag at the right-hand end covered the newest candle and
    its wick. It sits in a margin of its own now, on both charts, past
    the last candle."""
    key = 'XAUUSD_|GC1226'
    write_chart_history(page, key, 15)
    page.errors.clear()
    page.locator('.ladder[data-pair="%s"] .chart-btn' % key).click()
    page.wait_for_selector('.window.chartwin .cw-spread .cw-now-tag',
                           timeout=WAIT)
    page.locator('.window.chartwin .cw-tfs button[data-tf="15"]').click()
    for plot in ('.cw-z', '.cw-spread'):
        edges = page.evaluate("""(sel) => {
            const box = document.querySelector('.window.chartwin ' + sel);
            const candles = box.querySelectorAll('.cw-candle rect');
            const last = candles[candles.length - 1].getBoundingClientRect();
            const tag = box.querySelector('.cw-now-tag')
                .getBoundingClientRect();
            return [last.right, tag.left];
        }""", plot)
        assert edges[1] >= edges[0], (plot, edges)
    assert page.errors == []
    page.locator('.window.chartwin .close').click()


def test_the_algo_window_shows_btc_dominance(page):
    """BTC.D - Bitcoin's share of the crypto market and its 24 h move -
    in the Algo window's Statistics, as context. Marked old while the
    source is not answering."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block()
    publisher.btc_dominance = {'pct': 57.123, 'change_24h': 0.314,
                               'age_sec': 120, 'source': 'CoinGecko',
                               'error': None}
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-stats')"
            " || {textContent: ''}).textContent.includes('BTC.D')",
            timeout=WAIT)
        stats = page.text_content('.window.algowin .aw-stats')
        assert '57.12%' in stats and '+0.31' in stats
        publisher.btc_dominance = dict(publisher.btc_dominance,
                                       error='CoinGecko did not answer')
        publisher.publish()
        page.wait_for_function(
            "() => document.querySelector('.window.algowin .aw-stats')"
            ".textContent.includes('(old)')", timeout=WAIT)
    finally:
        publisher.btc_dominance = None
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_the_algo_window_shows_margin_and_leverage(page):
    """Margin & leverage in the Algo window: each leg's OKX leverage
    (flagged when they differ), what one Qty and one Algo trade tie up,
    that against equity, and whether the legs are beta's hedge."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    block = _algo_block()
    block['filters'] = dict(block['filters'], qty=30)
    publisher.algo_block = block
    was = dict(publisher.pair_extra)
    publisher.pair_extra.update({
        'algo_sizing': {'by': 'MARGIN', 'margin': 180.0, 'per_qty': 6.04,
                        'qty': 29.8, 'note': None},
        'leverage_a': 10, 'leverage_b': 1,
        'fee_bps': {'a': {'maker': 2.0, 'taker': 5.0},
                    'b': {'maker': 2.0, 'taker': 5.0}},
        'leg_notional': {'a': 70.62, 'b': 608.63, 'hedge_pct': 11.7}})
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-stats')"
            " || {textContent: ''}).textContent.includes('A 10x')",
            timeout=WAIT)
        stats = page.text_content('.window.algowin .aw-stats')
        assert 'Margin & leverage' in stats
        assert 'A 10x' in stats and 'B 1x' in stats
        assert '$679.25' in stats                      # notional per Qty
        # The TRADE first, labelled: 30 x $679.25, and its margin.
        trade = page.locator('.window.algowin .aw-kv', has_text='Trade size')
        assert '$20,377.50' in trade.text_content()
        assert '30.00 Qty' in trade.text_content()
        assert page.locator('.window.algowin .aw-kv',
                            has_text='Per 1 Qty').count() == 1
        # The Algo's size, in money as well: 30 x $679.25.
        size = page.locator('.window.algowin .aw-filters .aw-kv',
                            has_text='Size')
        assert '$20,378' in size.first.text_content()
        # Sized by total margin: said, and each leg's share of it.
        sized = page.locator('.window.algowin .aw-kv', has_text='Sized by')
        assert 'Total margin $180.00' in sized.text_content()
        margin = page.locator('.window.algowin .aw-kv',
                              has_text='Trade margin').text_content()
        assert 'A ' in margin and 'B ' in margin
        # The settings carry the choice.
        assert page.locator('.ls-az-size-by option[value="MARGIN"]').count()
        assert page.locator('.ls-az-margin').count()
        # The round trip broken down, one row each, in the Filters.
        filters = page.text_content('.window.algowin .aw-filters')
        assert 'Round trip' in filters and 'pts' in filters
        assert 'Fees' in filters and '2.0 bp \u00d7 2 legs \u00d7 in + out' in filters
        assert 'Bid-ask' in filters
        # No hedge-of-beta or beta-drift row: they confused.
        assert 'Hedge' not in stats and '\u03b2 drift' not in stats
        lev = page.locator('.window.algowin .aw-kv', has_text='Leverage')
        assert lev.locator('b.down').count() == 1
    finally:
        publisher.pair_extra.clear()
        publisher.pair_extra.update(was)
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_the_position_calculator_works_out_capital_at_risk(page):
    """A calculator per ladder: size, stop and target in, capital at
    risk out - at OKX's leverage and fees, from the ladder's own prices,
    beta and lots. The arithmetic checked on the ETH vs BTC ladder's
    numbers: one Qty $61.05 + $59.80, $0.035 a point, 20x both, maker
    2 bp. A calculation only."""
    ctx = {'perA': 61.05, 'perB': 59.80, 'point': 0.035, 'levA': 20,
           'levB': 20, 'fees': {'maker': {'a': 2.0, 'b': 2.0},
                                'taker': {'a': 5.0, 'b': 5.0}},
           'bid': -19.33, 'ask': -19.32, 'equity': 2701.0}
    r = page.evaluate("""([inp, ctx]) =>
        window.MT5Trader.calcPosition(inp, ctx)""", [
        {'side': 'BUY', 'sizeBy': 'QTY', 'size': 30, 'entry': -44,
         'sl': -105, 'tp': -36, 'fees': 'maker', 'riskPct': 1,
         'stressPct': 80}, ctx])
    assert r['notional'] == pytest.approx(3625.5)
    assert r['margin'] == pytest.approx(181.275)
    assert r['point'] == pytest.approx(1.05)
    assert r['fees'] == pytest.approx(2 * 120.85 * 2 / 1e4 * 30)   # $1.45
    assert r['stopPts'] == pytest.approx(61)
    assert r['loss'] == pytest.approx(61 * 1.05 + r['fees'])        # ~$65.5
    assert r['profit'] == pytest.approx(8 * 1.05 - r['fees'])       # ~$6.95
    assert r['lossPct'] == pytest.approx(r['loss'] / 2701 * 100)
    # 1% of $2,701 at this stop: $27.01 over (61 x 0.035 + fees per Qty).
    per_qty_loss = 61 * 0.035 + r['fees'] / 30
    assert r['riskQty'] == pytest.approx(27.01 / per_qty_loss)
    assert r['stressLoss'] == pytest.approx(0.8 * 59.80 * 30)
    assert r['feesStandard'] is False
    # Liquidation more points away than ETH's whole price: out of reach.
    ctx_b = dict(ctx, priceB=1708.5)
    r = page.evaluate("([inp, ctx]) => window.MT5Trader.calcPosition(inp, ctx)",
                      [{'side': 'BUY', 'sizeBy': 'QTY', 'size': 30,
                        'entry': -44, 'capital': 2701}, ctx_b])
    assert r['liqOutOfReach'] is True
    # The account's fees not read: OKX's standard, never a silent 0.
    r = page.evaluate("([inp, ctx]) => window.MT5Trader.calcPosition(inp, ctx)",
                      [{'side': 'BUY', 'sizeBy': 'QTY', 'size': 30,
                        'fees': 'maker'}, dict(ctx, fees={})])
    assert r['feesStandard'] is True and r['fees'] > 1.4
    # Sized by total margin: $180 at $6.0425 a Qty.
    r = page.evaluate("""([inp, ctx]) =>
        window.MT5Trader.calcPosition(inp, ctx)""", [
        {'side': 'SELL', 'sizeBy': 'MARGIN', 'size': 180, 'sl': -10,
         'tp': -30, 'fees': 'taker'}, ctx])
    assert r['qty'] == pytest.approx(180 / 6.0425)
    assert r['entry'] == -19.33                    # a short: the bid now
    assert r['stopPts'] == pytest.approx(9.33)
    # A stop on the wrong side is said, not priced.
    r = page.evaluate("""([inp, ctx]) =>
        window.MT5Trader.calcPosition(inp, ctx)""", [
        {'side': 'BUY', 'sizeBy': 'QTY', 'size': 1, 'entry': -44,
         'sl': -30}, ctx])
    assert 'wrong side' in r['stopError']
    # The window, from the ladder's Calc button, and closed again.
    open_ladder(page)
    publisher = page.paths['publisher']
    was = dict(publisher.pair_extra)
    publisher.pair_extra.update({
        'leverage_a': 20, 'leverage_b': 20,
        'leg_notional': {'a': 61.05, 'b': 59.80, 'hedge_pct': 100.6},
        'fee_bps': {'a': {'maker': 2.0, 'taker': 5.0},
                    'b': {'maker': 2.0, 'taker': 5.0}}})
    try:
        publisher.publish()
        page.errors.clear()
        page.locator('.ladder .calc-btn').first.click()
        page.wait_for_selector('.window.calcwin .pc-out .pc-kv',
                               timeout=WAIT)
        win = page.locator('.window.calcwin')
        win.locator('.pc-sl').fill('50')
        win.locator('.pc-sl').press('Tab')
        text = win.text_content()
        for word in ('Position', 'Margin', '$ per point', 'Fees round trip',
                     'Loss at stop', 'Liquidation, est.', 'Stress'):
            assert word in text, word
        assert 'nothing here sends an order' in text
        win.locator('.close').click()
        assert page.locator('.window.calcwin').count() == 0
        assert page.errors == []
    finally:
        publisher.pair_extra.clear()
        publisher.pair_extra.update(was)
        publisher.publish()


def test_the_trading_monitor_can_be_maximized_and_restored(page):
    """The Trading Monitor fills the desk from its own button, or a
    double-click on its title bar, and the same again puts it back."""
    page.errors.clear()
    page.evaluate("""() => { const s = window.MT5Trader.state;
        s.open = ['monitor:']; s.monitorTab = 'positions';
        window.MT5Trader.render(); }""")
    page.wait_for_selector('.window.monitor .winbtn.max', timeout=WAIT)
    win = page.locator('.window.monitor')
    before = win.bounding_box()
    win.locator('.winbtn.max').click()
    assert 'maximized' in win.get_attribute('class')
    desk = page.locator('#desktop').bounding_box()
    full = win.bounding_box()
    assert full['width'] >= desk['width'] - 20
    assert full['height'] >= desk['height'] - 20
    # It stays maximized through the next redraw.
    page.evaluate('() => window.MT5Trader.render()')
    assert 'maximized' in win.get_attribute('class')
    win.locator('.winbtn.max').click()
    assert 'maximized' not in win.get_attribute('class')
    assert win.bounding_box()['width'] == pytest.approx(before['width'], abs=2)
    # A double-click on the title bar does the same.
    win.locator('.titlebar .title').dblclick()
    assert 'maximized' in win.get_attribute('class')
    win.locator('.titlebar .title').dblclick()
    assert 'maximized' not in win.get_attribute('class')
    assert page.errors == []
    page.evaluate("""() => { const s = window.MT5Trader.state;
        s.open = []; window.MT5Trader.render(); }""")


def test_the_trade_journal_is_the_first_table_on_the_analysis_tab(page):
    """The table the Analysis tab is opened for comes first: the trade
    journal, under 'What actually happened' and above the summary."""
    page.errors.clear()
    page.evaluate("""() => { const s = window.MT5Trader.state;
        s.open = ['monitor:']; s.monitorTab = 'analysis';
        s.analysis = {ok: true, names: {}, summary: {count: 1, closed: 1},
          drawdown: {}, calibration: {}, shadow: {}, excursion: [],
          signals: [], signal_tally: {}, journal: []};
        s.analysisAt = Date.now(); window.MT5Trader.render(); }""")
    page.wait_for_selector('.monitor .analysis table', timeout=WAIT)
    first = page.locator('.monitor .analysis table').first
    assert 'Running' in first.text_content()          # the journal's own
    order = page.evaluate("""() => Array.from(document.querySelectorAll(
        '.monitor .analysis h4')).map(h => h.textContent)""")
    journal = next(i for i, h in enumerate(order)
                   if h.startswith('Trade journal'))
    summary = order.index('Summary')
    assert journal < summary
    assert page.errors == []
    page.evaluate("""() => { const s = window.MT5Trader.state;
        s.open = []; window.MT5Trader.render(); }""")


def test_windows_snap_edge_to_edge_and_the_layout_is_kept_on_the_desk(page):
    """A window dragged to within a few pixels of another's edge lands
    exactly beside it. The layout - where each window is, and which
    tools are open - is kept on the desk as well as in the browser: a
    browser with none of its own (a cleared cache) gets the desk's back,
    tools and all."""
    open_ladder(page)
    tidy(page)
    page.errors.clear()
    page.evaluate("() => window.MT5Trader.openPanel('monitor:')")
    page.wait_for_selector('.window.monitor', timeout=WAIT)
    settle(page, '.window.monitor')
    ladder = page.locator('.window.ladder').first.bounding_box()
    monitor = page.locator('.window.monitor').bounding_box()
    # Aim 6 px short of the ladder's right edge (+ the 2 px gap): caught.
    target_x = ladder['x'] + ladder['width'] + 2 + 6
    drag(page, '.window.monitor', target_x - monitor['x'],
         ladder['y'] + 40 - monitor['y'])
    snapped = page.locator('.window.monitor').bounding_box()
    assert snapped['x'] == pytest.approx(ladder['x'] + ladder['width'] + 2,
                                         abs=1.5)
    # Kept on the desk, a second or so after the change: the save is
    # throttled to once a second, so poll for it rather than sleeping a
    # fixed time (a save from mid-drag can land first).
    deadline = time.time() + 6.0
    while True:
        saved = page.evaluate("""() => fetch('/api/layout')
            .then(r => r.json())""")
        mon = (saved.get('layout') or {}).get('monitor:') or {}
        if abs((mon.get('left') or -1e9) - snapped['x']) < 3 and \
                'monitor:' in (saved['layout'].get('_open') or []):
            break
        if time.time() > deadline:
            break
        page.wait_for_timeout(250)
    if not saved['layout']:
        probe = page.evaluate("""() => fetch('/api/layout', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({layout: {x: 1}})}).then(r => r.text())""")
        raise AssertionError(probe)
    assert 'monitor:' in saved['layout']
    assert 'monitor:' in saved['layout'].get('_open', [])
    # A browser with no layout of its own: the desk's comes back, the
    # monitor open where it was.
    page.evaluate("() => window.localStorage.clear()")
    page.reload()
    page.wait_for_selector('.window.monitor', timeout=WAIT)
    settle(page, '.window.monitor')
    back = page.locator('.window.monitor').bounding_box()
    assert back['x'] == pytest.approx(snapped['x'], abs=2)
    assert page.errors == []
    # Leave the desk as found: the monitor closed, the desk tidy, and
    # the desk's own copy saying so.
    page.evaluate("() => window.MT5Trader.closePanel('monitor:')")
    tidy(page)
    page.wait_for_timeout(1500)


def test_tidy_tiles_the_desk_edge_to_edge(page):
    """Tidy lays the desk out in one click: ladders full height on the
    left, the Algo window beside them with the Trading Monitor under it,
    charts and calculators stacked down the right - every window touching
    its neighbour, nothing overlapping, the desk filled. Shift-Tidy is
    the plain row."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'
    publisher.algo_block = _algo_block()
    key = 'XAUUSD_|GC1226'
    write_chart_history(page, key, 15)
    try:
        publisher.publish()
        page.wait_for_selector('.window.algowin', timeout=WAIT)
        page.evaluate("""(key) => { window.MT5Trader.openPanel('monitor:');
            window.MT5Trader.openPanel('chart:' + key);
            window.MT5Trader.openPanel('calc:' + key); }""", key)
        page.wait_for_selector('.window.calcwin', timeout=WAIT)
        page.wait_for_selector('.window.chartwin', timeout=WAIT)
        page.errors.clear()
        page.click('#tidy')
        page.wait_for_timeout(300)
        box = {name: page.locator(sel).first.bounding_box() for name, sel in (
            ('ladder', '.window.ladder'), ('algo', '.window.algowin'),
            ('monitor', '.window.monitor'), ('chart', '.window.chartwin'),
            ('calc', '.window.calcwin'))}
        desk = page.locator('#desktop').bounding_box()
        near = lambda a, b: abs(a - b) <= 4
        right = lambda b: b['x'] + b['width']
        bottom = lambda b: b['y'] + b['height']
        # Ladder on the left, full height.
        assert near(box['ladder']['x'], desk['x'])
        assert box['ladder']['height'] >= desk['height'] - 12
        # The Algo window beside it, the Monitor under it, same column.
        assert near(box['algo']['x'], right(box['ladder']))
        assert near(box['monitor']['x'], box['algo']['x'])
        assert near(box['monitor']['y'], bottom(box['algo']))
        assert near(bottom(box['monitor']), bottom(box['ladder']))
        # Chart and calculator stacked down the right, to the bottom.
        assert near(box['chart']['x'], right(box['algo']))
        assert near(box['calc']['x'], box['chart']['x'])
        assert near(box['calc']['y'], bottom(box['chart'])) or \
            near(box['chart']['y'], bottom(box['calc']))
        assert near(max(bottom(box['chart']), bottom(box['calc'])),
                    bottom(box['ladder']))
        # Saved like a layout made by hand.
        saved = page.evaluate(
            "() => JSON.parse(localStorage.getItem('okxtrader.windows.v1'))")
        assert saved['monitor:']['auto'] is False
        assert page.errors == []
        # Shift-Tidy: the plain row.
        tidy(page)
        assert page.locator('.window.floating').count() == 0
    finally:
        page.evaluate("""(key) => { const t = window.MT5Trader;
            t.closePanel('monitor:'); t.closePanel('chart:' + key);
            t.closePanel('calc:' + key); }""", key)
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()
        tidy(page)


def test_the_dollar_keypad_sizes_by_total_margin_and_the_buttons_say_it(
        page):
    """The ladder's $ keypad and box are the TOTAL MARGIN of the trade,
    both legs together: Qty = margin / what one Qty ties up, rounded
    down. The buttons say the size in the unit it was set in - '$500'
    from the margin keypad, '5' from a Qty."""
    open_ladder(page)
    publisher = page.paths['publisher']
    was = dict(publisher.pair_extra)
    publisher.pair_extra.update({
        'exit': {'margin_per_spread': 6.0},
        'leg_notional': {'a': 61.0, 'b': 59.0}})
    try:
        publisher.publish()
        page.wait_for_function(
            "() => ((window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
            " || {}).exit || {}).margin_per_spread === 6", timeout=WAIT)
        page.errors.clear()
        ladder = page.locator('.window.ladder').first
        ladder.locator('.usd-keypad .usd[data-usd="500"]').click()
        page.wait_for_function(
            "() => document.querySelector('.window.ladder .buy-touch')"
            ".textContent.includes('$500')", timeout=WAIT)
        assert 'SELL $500' in ladder.locator('.sell-touch').text_content()
        # $500 / $6.00 a Qty = 83.33, rounded down.
        assert '83.33' in ladder.locator('.usd-qty').text_content()
        assert 'Qty 83.33' in ladder.locator('.buy-touch').get_attribute(
            'title')
        # The box: type a margin, Enter.
        box = ladder.locator('.usd-box')
        box.fill('120')
        box.press('Enter')
        page.wait_for_function(
            "() => document.querySelector('.window.ladder .buy-touch')"
            ".textContent.includes('$120')", timeout=WAIT)
        # A Qty from the keypad: the buttons say the Qty, not dollars.
        ladder.locator('.keypad .qty[data-qty="5"]').click()
        page.wait_for_function(
            "() => /BUY 5$/.test(document.querySelector("
            "'.window.ladder .buy-touch').textContent.trim())",
            timeout=WAIT)
        assert page.errors == []
    finally:
        publisher.pair_extra.clear()
        publisher.pair_extra.update(was)
        publisher.publish()
        page.evaluate("""() => { const s = window.MT5Trader.state;
            s.armed = {}; s.armedUsd = {}; window.MT5Trader.render(); }""")


def test_the_sizing_tab_is_the_sheet_live_with_a_win_rate_toggle(page):
    """A Sizing tab beside Analysis: capital in hand from $5k to $500k,
    per trade, a month and drawdowns, from the ETH vs BTC ladder's live
    prices (fixed figures without one) - with the win rate toggled and
    the target, stop, trades a day and margin share typed in."""
    page.errors.clear()
    page.evaluate("""() => { const s = window.MT5Trader.state;
        s.open = ['monitor:']; s.monitorTab = 'sizing'; s.sizing = null;
        window.MT5Trader.render(); }""")
    page.wait_for_selector('.monitor table.sz-table', timeout=WAIT)
    rows = page.locator('.monitor table.sz-table tbody tr')
    assert rows.count() == 51
    caps = [rows.nth(i).locator('td').nth(1).text_content()
            for i in range(51)]
    # $25k steps to $200k, $100k steps to $500k, $250k steps to $10M.
    assert caps[:4] == ['$5k', '$10k', '$25k', '$50k']
    assert caps[9:13] == ['$200k', '$300k', '$400k', '$500k']
    assert caps[13:16] == ['$750k', '$1M', '$1.25M']
    assert rows.first.locator('td').nth(1).text_content() == '$5k'
    assert rows.last.locator('td').nth(1).text_content() == '$10M'
    # The defaults: target 1.5% of margin (0.9% of capital), stop 1.5% of
    # capital, 65% wins, 90 trades: +5.4% of capital a month.
    summary = page.text_content('.monitor .sz-sum')
    assert 'break-even win rate 62.5%' in summary
    assert '+5.4% of capital' in summary
    hundred = rows.nth(5).locator('td')
    assert hundred.nth(1).text_content() == '$100k'
    assert hundred.nth(6).text_content() == '+$900'
    assert hundred.nth(7).text_content() == '−$1,500'
    assert hundred.nth(15).text_content() == '+$5.4k'
    # 70%: 0.7 x 0.9 - 0.3 x 1.5 = +0.18% a trade, +16.2% a month.
    page.click('.monitor .sz-win[data-win="70"]')
    page.wait_for_function(
        "() => document.querySelector('.monitor .sz-sum').textContent"
        ".includes('+16.2% of capital')", timeout=WAIT)
    assert 'on' in page.locator('.monitor .sz-win[data-win="70"]') \
        .get_attribute('class')

    def typed(key, value, expect):
        box = page.locator(f'.monitor .sz-input[data-key="{key}"]')
        if box.evaluate('e => e.tagName') == 'SELECT':
            box.select_option(value)
        else:
            box.fill(value)
            box.press('Tab')
        page.wait_for_function(
            "t => document.querySelector('.monitor .sz-sum').textContent"
            ".includes(t)", arg=expect, timeout=WAIT)

    # A free win rate: 65 again, typed.
    typed('win', '65', '+5.4% of capital')
    # A stop of 1.5% of MARGIN ($900): 0.65 x 900 - 0.35 x 900 = $270 a
    # trade, +24.3% a month.
    typed('stopBase', 'margin', '+24.3% of capital')
    typed('stopBase', 'capital', '+5.4% of capital')
    # The target is net of every cost and the stop the whole loss, so the
    # maker / taker mix leaves the P&L alone: it sets the fees (half taker
    # at VIP 3: 5.75 bp on $2.4M = $1,380) and so how far the spread must
    # move to reach each.
    typed('makerPct', '50', 'fees $1,380')
    summary = page.text_content('.monitor .sz-sum')
    assert '+5.4% of capital' in summary
    assert 'break-even win rate 62.5%' in summary
    hundred = rows.nth(5).locator('td')
    assert hundred.nth(13).text_content() == '$1,380'
    # $346 a point: (900 + 1,380) / 346 = 6.6 pts to the target, (1,500 -
    # 1,380) / 346 = 0.3 pts to the stop.
    assert hundred.nth(8).text_content() == '6.6'
    assert hundred.nth(9).text_content() == '0.3'
    # 30% free capital: $70k margin at 20x.
    typed('makerPct', '100', '+5.4% of capital')
    typed('freePct', '30', '+14.2% of capital')
    assert rows.nth(5).locator('td').nth(2).text_content() == '$70k'
    # Reset returns every default.
    page.click('.monitor .sz-reset')
    page.wait_for_function(
        "() => document.querySelector('.monitor .sz-sum').textContent"
        ".includes('break-even win rate 62.5%')", timeout=WAIT)
    assert page.errors == []
    page.evaluate("""() => { const s = window.MT5Trader.state;
        s.sizing = null; s.open = []; window.MT5Trader.render(); }""")


def test_the_algo_levels_say_where_the_mean_exit_is(page):
    """Back to mean on: the mean is a fourth level beside BE / TP / SL,
    and says which exit it is for this position."""
    words = page.evaluate("""() => {
        const f = window.MT5Trader.meanExitWords, on = {reversion_on: true};
        const long = {side: 'BUY', break_even: -21.3, tp: -17.7};
        return [f(long, -18.1, on, 1), f(long, -22.0, on, 1),
                f(long, -17.0, on, 1), f(long, -18.1, {}, 1),
                f({side: 'SELL', break_even: -15.0, tp: -19.0}, -18.0, on, 1)];
    }""")
    assert 'Mean -18.1' in words[0] and 'exits here first' in words[0]
    assert 'not past BE' in words[1]
    assert 'past TP' in words[2]
    assert words[3] == ''
    assert 'exits here first' in words[4]


def test_the_volume_profile_puts_volume_in_its_price_band(page):
    """[price, volume] of each finer bar, binned into even price bands
    over the candles shown; nothing at all when no candle has volume."""
    vp = page.evaluate("""() => window.MT5Trader.volumeProfile(
        [{f: [[-20.1, 100], [-19.6, 50]]}, {f: [[-20.05, 300]]}, null],
        -21, -19, 4)""")
    assert [round(r['v']) for r in vp] == [0, 400, 50, 0]
    assert page.evaluate(
        "() => window.MT5Trader.volumeProfile([{c: 1}], 0, 1, 4)") == []


def test_the_spread_chart_draws_a_volume_profile_and_turns_it_off(page):
    """Both legs' traded value at each spread price, over the candles
    shown: bars from the right edge, the fullest (POC) marked with its
    price - and a VP button that takes it away."""
    key = 'XAUUSD_|GC1226'
    write_chart_history(page, key, 15, volume=True)
    page.errors.clear()
    # An earlier test's history (no volume) may still be held: read again.
    page.evaluate("""(key) => { const t = window.MT5Trader;
        t.closePanel('chart:' + key);
        t.state.chartHist = {}; t.state.chartHistFetch = {};
        t.state.chartVp = true;
        t.openPanel('chart:' + key); }""", key)
    page.wait_for_selector('.window.chartwin .cw-spread .cw-vp',
                           timeout=WAIT)
    win = page.locator('.window.chartwin')
    assert win.locator('.cw-spread .cw-vp-bar').count() >= 3
    assert win.locator('.cw-spread .cw-vp-bar.poc').count() == 1
    assert 'POC 59.1' in win.locator('.cw-spread .cw-vp').text_content()
    assert win.locator('.cw-z .cw-vp').count() == 0     # spread chart only
    assert 'on' in win.locator('.cw-vp-toggle').get_attribute('class')
    win.locator('.cw-vp-toggle').click()
    page.wait_for_function(
        "() => !document.querySelector('.window.chartwin .cw-spread .cw-vp')",
        timeout=WAIT)
    win.locator('.cw-vp-toggle').click()
    page.wait_for_selector('.window.chartwin .cw-spread .cw-vp',
                           timeout=WAIT)
    page.evaluate("(key) => window.MT5Trader.closePanel('chart:' + key)",
                  key)
    assert page.errors == []


def test_the_algo_box_shows_an_add_and_where_the_next_would_go(page):
    """Holding one short: the line says where an add would go on. With
    the add on: both sizes in the tile, and the add's own levels."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.algo = 'ALGO'

    def pos(pid, entry, net):
        return {'position_id': pid, 'side': 'SELL', 'entry_spread': entry,
                'closing_spread': 59.02, 'quantity': 1.0, 'tp': entry - 0.1,
                'sl': None, 'break_even': entry - 0.02, 'net_pnl': net,
                'exit': None}
    publisher.algo_block = _algo_block(
        state='IN_POSITION', signal=None, positions=[pos('P1', 59.0, -1.0)],
        add={'side': 'SELL', 'beyond': 59.08, 'count': 0,
             'extreme': 59.05, 'reached': False})
    try:
        publisher.publish()
        page.wait_for_function(
            "() => (document.querySelector('.window.algowin .aw-signal')"
            " || {textContent: ''}).textContent.includes('on re-entry')",
            timeout=WAIT)
        text = page.text_content('.window.algowin .aw-signal')
        assert 'add once past ≥ 59.08 (furthest 59.05), then on re-entry' \
            in text
        publisher.algo_block = _algo_block(
            state='IN_POSITION', signal=None,
            positions=[pos('P1', 59.0, -1.0), pos('P2', 59.09, 2.5)],
            add={'why': '1 add(s) on - the most', 'count': 1})
        publisher.publish()
        page.wait_for_function(
            "() => document.querySelector('.window.algowin .aw-signal')"
            ".textContent.includes('Add 1')", timeout=WAIT)
        text = page.text_content('.window.algowin .aw-signal')
        assert 'SHORT 1.00 + 1.00' in text and '(+1 add)' in text
        assert 'in @ 59.09' in text
        assert page.errors == []
    finally:
        publisher.algo = 'NONE'
        publisher.algo_block = None
        publisher.publish()


def test_the_add_says_the_size_it_would_go_on_at(page):
    words = page.evaluate("""() => { const f = window.MT5Trader.addSizeWords;
        return [f({algo_qty: 17.25}), f({add_size_by: 'QTY', add_qty: 5}),
                f({add_size_by: 'MARGIN', add_qty: 0, algo_qty: 3}), f({})];
    }""")
    assert words == ['17.25 ', '5.00 ', '(size not set) ', '']


def test_the_sizing_sheet_prints_on_one_page_with_its_settings(page):
    """Print sheet: the table as the tab shows it, with the settings it
    was worked out on, on a page of its own scaled to one A4 page."""
    page.errors.clear()
    page.evaluate("""() => { const s = window.MT5Trader.state;
        s.open = ['monitor:']; s.monitorTab = 'sizing';
        s.sizing = {freePct: 20}; window.MT5Trader.render(); }""")
    page.wait_for_selector('.monitor .sz-print', timeout=WAIT)
    with page.expect_popup() as opened:
        page.click('.monitor .sz-print')
    sheet = opened.value
    sheet.wait_for_selector('table.sz-table')
    assert 'Position Sizing Sheet' in sheet.text_content('h1')
    assert sheet.locator('table.sz-table tbody tr').count() == 51
    assert 'Free capital20%' in sheet.text_content('.set')
    # Scaled to the printable area of one A4 landscape page.
    w, h, k = sheet.evaluate("""() => { const s = document.querySelector(
        '.sheet'); return [s.offsetWidth, s.offsetHeight,
        parseFloat(s.style.zoom)]; }""")
    # (A4 landscape less 6 mm margins: 1077 x 748 px at 96 dpi.)
    assert w * k <= 1077 and h * k <= 748 and k > 0.5
    sheet.close()
    assert page.errors == []
    page.evaluate("""() => { const s = window.MT5Trader.state;
        s.sizing = null; s.open = []; window.MT5Trader.render(); }""")
