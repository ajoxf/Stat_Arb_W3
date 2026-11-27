"""The whole thing, end to end.

Two leg runners on real sockets, a coordinator with a real database, the
Flask process, and Chromium clicking the ladder. Only MetaTrader5 itself
is faked — and it is faked as a HEDGING-mode broker that keeps a real
book and a real deal history, so the parts that have cost money in the
past behave here the way they behave there.

What this covers that the unit tests cannot: the click actually crossing
four process boundaries (browser → Flask → command file → coordinator →
leg runner → broker), the position surviving a coordinator restart, and
the journal being written from what the broker reports rather than from
what we intended.
"""

import json
import os
import sys
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeBroker, FakeSymbol                      # noqa: E402
from okxtrader.commands import CommandRunner                     # noqa: E402
from okxtrader.config import PairConfig, TraderConfig            # noqa: E402
from okxtrader.coordinator import Coordinator                    # noqa: E402
from okxtrader.database import Store                             # noqa: E402
from okxtrader.legs import LocalLeg                              # noqa: E402
from okxtrader.models import OrderSide                           # noqa: E402
from okxtrader.webapp import create_app                          # noqa: E402
from test_ui_browser import chromium_path                        # noqa: E402


class Desk:
    """One installation: two accounts, two runners, and the files."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.paths = {
            'status': str(tmp / 'status.json'),
            'commands': str(tmp / 'commands.jsonl'),
            'results': str(tmp / 'results.json'),
            'config': str(tmp / 'config.json'),
            'db': str(tmp / 'trader.db'),
        }
        self.spot = FakeSymbol('XAUUSD_', 4292.00, 4292.20, contract_size=100.0,
                               volume_min=0.01, volume_step=0.01)
        self.future = FakeSymbol('GC1226', 4351.00, 4351.40,
                                 contract_size=100.0, volume_min=0.10,
                                 volume_step=0.10)
        self.brokers = {'spot': FakeBroker('spot', [self.spot]),
                        'fut': FakeBroker('fut', [self.future])}
        for broker in self.brokers.values():
            broker.initialize()

        with open(self.paths['config'], 'w', encoding='utf-8') as f:
            json.dump({'accounts': {
                name: {'exchange': 'okx', 'demo': True}
                for name in self.brokers}, 'pairs': {}}, f)

        self.coordinator = None
        self.runner = None
        self.threads = []
        self.stopped = threading.Event()

    # -- the engine, startable and stoppable like the real launcher -----

    def config(self):
        pair = PairConfig(
            'XAUUSD_|GC1226', name='Gold basis',
            leg_a={'account': 'spot', 'symbol': 'XAUUSD_'},
            leg_b={'account': 'fut', 'symbol': 'GC1226'},
            hedge_ratio=1.0, hedge_ratio_for='XAUUSD_|GC1226',
            increment=0.01, default_quantity=1.0, order_type='MARKET',
            # What one Qty is on each leg, as the trader would type it.
            # The future's minimum is 0.10 against the spot's 0.01, so
            # this is the smallest size that clears both.
            clip_lots_a=0.10, clip_lots_b=0.10)
        config = TraderConfig(pairs={pair.key: pair})
        config.settings.update({'POLL_INTERVAL_SEC': 0.05,
                                'COMMAND_POLL_SEC': 0.01,
                                'JOURNAL_INTERVAL_SEC': 0.5,
                                'RECONCILE_INTERVAL_SEC': 0.5,
                                'LEG_DEADLINE_SEC': 0.5})
        return config

    def start_engine(self):
        """Exactly what run_coordinator.py does, in-process."""
        legs = {name: LocalLeg(broker)
                for name, broker in self.brokers.items()}
        self.coordinator = Coordinator(self.config(), legs,
                                       status_path=self.paths['status'],
                                       store=Store(self.paths['db']))
        self.runner = CommandRunner(self.coordinator, self.paths['commands'],
                                    self.paths['results'])
        self.runner.prime()
        self.coordinator.commands = self.runner
        self.coordinator.start()
        for target in (self.coordinator.run, self.coordinator.serve_commands):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.wait_for(lambda: os.path.exists(self.paths['status']),
                      'the engine never published a snapshot')
        return self.coordinator

    def stop_engine(self):
        """Stop as the launcher does — the sweep runs, the book stays at
        the broker."""
        self.coordinator._stop.set()
        for thread in self.threads:
            thread.join(timeout=2.0)
        self.threads = []
        for leg in self.coordinator.legs.values():
            leg.close()

    # -- the same commands the screen sends, without a browser ---------

    def send(self, kind, payload, timeout=10.0):
        """Through the REAL bridge: the file the web process writes and
        the coordinator's own command thread drains.

        A box with no Chromium still has to prove this path works — the
        browser is the only piece missing here, and everything the click
        actually costs money through is still in it.
        """
        from okxtrader.commands import CommandLog
        command_id = CommandLog(self.paths['commands']).submit(kind, payload)
        self.wait_for(lambda: self.result(command_id) is not None,
                      f'the {kind} command was never executed', timeout)
        return self.result(command_id)

    def result(self, command_id):
        try:
            with open(self.paths['results'], 'r', encoding='utf-8') as f:
                return json.load(f).get(command_id)
        except (OSError, ValueError):
            return None

    def buy_the_touch(self, quantity=1.0):
        """One click, at the price a BUY would actually cross at."""
        key = 'XAUUSD_|GC1226'
        md = self.coordinator.market.get(key)
        assert md, 'no market data to click at'
        answer = self.send('click', {'pair': key, 'side': 'BUY',
                                     'level': md['long_spread'],
                                     'quantity': quantity})
        assert answer.get('ok'), answer
        self.wait_for(lambda: self.coordinator.book.positions(),
                      'the click never became a position')
        return self.coordinator.book.positions()[0]

    def flatten(self):
        answer = self.send('flatten_pair', {'pair': 'XAUUSD_|GC1226'})
        assert answer.get('ok'), answer
        self.wait_for(lambda: not self.coordinator.book.positions(),
                      'the flatten never emptied the book')

    def serve_web(self):
        from werkzeug.serving import make_server
        app = create_app(self.paths['status'], self.paths['commands'],
                         self.paths['results'], self.paths['config'],
                         self.paths['db'])
        self.httpd = make_server('127.0.0.1', 0, app, threaded=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{self.httpd.server_port}'

    def close(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    @staticmethod
    def wait_for(predicate, message, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        raise AssertionError(message)


@pytest.fixture(scope='module')
def desk(tmp_path_factory):
    desk = Desk(tmp_path_factory.mktemp('e2e'))
    desk.start_engine()
    yield desk
    desk.close()


@pytest.fixture
def pair_on(desk):
    """A pair ON — clicked in the browser if there is one, through the
    command bridge if there is not.

    The tests below are about what happens to a LIVE position: recovery,
    the reconciler, the journal, the record. None of that is about the
    browser, and on a box with no Chromium they were failing for the
    absence of one — which reads as a broken build on a machine that is
    fine.
    """
    if not desk.coordinator.book.positions():
        desk.buy_the_touch()
    return desk.coordinator.book.positions()[0]


@pytest.fixture
def round_turn(desk):
    """A completed round turn: on, and off again."""
    if Store(desk.paths['db']).closed_positions():
        return                       # the browser already did it
    if not desk.coordinator.book.positions():
        desk.buy_the_touch()
    desk.flatten()
    desk.wait_for(lambda: not desk.brokers['spot'].positions_by_magic(),
                  'leg A never closed')
    desk.wait_for(lambda: len(Store(desk.paths['db']).fills()) >= 5,
                  'the closing fills never reached the journal')


@pytest.fixture(scope='module')
def browser_page(desk):
    url = desk.serve_web()
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=chromium_path())
        except Exception as e:
            pytest.skip(f'chromium unavailable: {e}')
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        page.wait_for_selector('.ladder .grid tbody tr', timeout=15000)

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
        yield page
        browser.close()


def test_a_click_in_the_browser_reaches_both_brokers(desk, browser_page):
    """Browser → Flask → command file → coordinator → two leg runners →
    two brokers. Every boundary is real except MT5 itself."""
    page = browser_page
    assert page.locator('.window.mode-market').count() == 1   # armed
    started = time.time()

    # At the touch, which is where a market order belongs. One click.
    page.click('.ladder .buy-touch')

    desk.wait_for(lambda: desk.brokers['spot'].open_positions()
                  and desk.brokers['fut'].open_positions(),
                  'the click never reached both brokers')
    elapsed = time.time() - started

    spot = desk.brokers['spot'].open_positions()[0]
    future = desk.brokers['fut'].open_positions()[0]
    # BUY the spread = buy leg B, sell leg A.
    assert spot['side'] == 'SELL' and future['side'] == 'BUY'
    assert spot['volume'] == pytest.approx(0.1)
    assert future['volume'] == pytest.approx(0.1)
    # One click, one order, and it does not wait for a poll.
    assert elapsed < 2.0, f'the click took {elapsed:.2f}s to reach the broker'
    assert page.errors == []


def test_a_click_away_from_the_touch_rests_instead_of_being_refused(
        desk, browser_page):
    """A buy under the offer cannot cross at any price. The trader
    clicking there means "work it here", so it rests — and the toast
    says so, because a market click that quietly became a working order
    is a surprise."""
    page = browser_page
    before = len(desk.brokers['spot'].sent)

    # A BUY, far down the ladder - a price well under the offer.
    #
    # WHICH COLUMN BUYS IS A SETTING, so it is read from the snapshot
    # the engine published rather than written in here. Under the
    # shipped TT default the BIDS column buys; under TOUCH it is the
    # asks. This test hard-coded 'ask' and, once TT became the default,
    # was clicking a SELL instead - which CAN cross below the market,
    # so it reduced the desk's position at market and never rested
    # anything. It then left that half-traded desk behind for the four
    # tests after it, which is why they failed too.
    #
    # Clicked in ONE evaluate: the ladder rebuilds on every publish, and
    # a row resolved in Python can be replaced before the click lands.
    page.evaluate("""() => {
        const tt = window.MT5Trader.state.snapshot.click_convention
            !== 'TOUCH';
        const buys = tt ? 'bid' : 'ask';
        const cells = document.querySelectorAll(
            '.ladder .grid tbody tr td.' + buys);
        cells[cells.length - 1].click();
    }""")
    page.wait_for_selector('.toast', timeout=8000)

    assert 'away from the market' in page.text_content('.toast')
    assert 'working order' in page.text_content('.toast')
    # Nothing was sent to either broker: it is resting in the book.
    assert len(desk.brokers['spot'].sent) == before
    desk.wait_for(lambda: desk.coordinator.book.orders('XAUUSD_|GC1226'),
                  'the click never became a working order')
    page.click('.toast')
    desk.send('cancel_where', {'pair': 'XAUUSD_|GC1226'})
    desk.wait_for(lambda: not desk.coordinator.book.orders('XAUUSD_|GC1226'),
                  'the working order was never pulled')


def test_with_resting_turned_off_that_same_click_is_refused_in_words(desk):
    """The control. With CLICK_AWAY_RESTS off the old behaviour is back
    — refused, in the engine's own words, and nothing sent."""
    desk.coordinator.config.settings['CLICK_AWAY_RESTS'] = False
    before = len(desk.brokers['spot'].sent)
    md = desk.coordinator.market['XAUUSD_|GC1226']

    answer = desk.send('click', {'pair': 'XAUUSD_|GC1226', 'side': 'BUY',
                                 'level': md['long_spread'] - 5.0,
                                 'quantity': 1.0})

    assert answer['data'].get('refused'), answer
    # In the engine's OWN words — whichever guard caught it. What is
    # being pinned is that with resting off the click does not become a
    # working order and nothing reaches either broker.
    assert answer['data'].get('reason')
    assert len(desk.brokers['spot'].sent) == before
    assert not desk.coordinator.book.orders('XAUUSD_|GC1226')
    desk.coordinator.config.settings['CLICK_AWAY_RESTS'] = True


def test_the_position_and_its_legs_appear_on_the_screen(desk, browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.MT5Trader.state.open = ['monitor:'];
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor', timeout=5000)
    page.wait_for_function(
        "() => document.querySelector('.monitor .pane').textContent"
        ".includes('XAUUSD_')", timeout=8000)

    text = page.text_content('.monitor .pane')
    assert 'Gold basis' in text
    assert 'GC1226' in text                     # both legs, with tickets
    # Marked where it would actually CLOSE, so it shows the round turn
    # as a loss the instant it opens.
    assert '-$' in text


def test_the_journal_records_what_the_broker_reported(desk, browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.MT5Trader.state.monitorTab = 'fills';
        window.MT5Trader.render();
    }""")
    page.wait_for_function(
        "() => document.querySelector('.monitor .pane').textContent"
        ".includes('GC1226')", timeout=8000)

    # BOTH legs' deals: the journal writes one account, then the other,
    # and the page can show the first before the second is written.
    desk.wait_for(lambda: len(Store(desk.paths['db']).fills()) == 2,
                  'the second leg never reached the journal')
    fills = Store(desk.paths['db']).fills()
    assert len(fills) == 2
    assert {fill['leg'] for fill in fills} == {'A', 'B'}
    assert all(fill['is_ours'] == 1 for fill in fills)
    assert all(fill['pair_key'] == 'XAUUSD_|GC1226' for fill in fills)

    # And the same rows are on the screen, with the broker's commission.
    text = page.text_content('.monitor .pane')
    assert 'commission' in text
    assert 'Export CSV' in text
    # Each row carries its DATE, and the totals say what they cover.
    import re
    assert re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', text), text
    assert 'every fill in the journal' in text


def test_a_manual_trade_in_the_terminal_is_journalled_but_not_ours(
        desk, pair_on):
    """A fill on the account is a fill on the account — and it is never
    mistaken for one of ours, in the book or in the journal."""
    desk.brokers['fut'].send_market_order(
        'GC1226', OrderSide.BUY, 0.2, comment='by hand')

    desk.wait_for(
        lambda: any(fill['is_ours'] == 0
                    for fill in Store(desk.paths['db']).fills()),
        'the manual trade never reached the journal')

    theirs = [fill for fill in Store(desk.paths['db']).fills()
              if fill['is_ours'] == 0]
    assert len(theirs) == 1 and theirs[0]['volume'] == pytest.approx(0.2)
    # It is not in our book, and the reconciler leaves it alone.
    assert len(desk.coordinator.book.positions()) == 1


def test_the_position_survives_a_coordinator_restart(desk, pair_on):
    """The blocker this was built for: an empty book at startup makes
    every live position look like an orphan, and sixty seconds later the
    reconciler closes them."""
    before = pair_on
    tickets = {
        'spot': before.leg_a.position_tickets,
        'fut': before.leg_b.position_tickets,
    }
    desk.stop_engine()

    # The positions are still at the broker, as they would be.
    assert desk.brokers['spot'].open_positions()
    assert desk.brokers['fut'].open_positions()

    desk.start_engine()

    recovered = desk.coordinator.book.positions()
    assert len(recovered) == 1
    assert recovered[0].position_id == before.position_id
    assert recovered[0].leg_a.position_tickets == tickets['spot']
    assert recovered[0].recovered is True
    assert desk.coordinator.recovery['recovered'] == 1

    # Now let the reconciler run several times over. It must NOT close
    # the position it just recovered.
    time.sleep(2.5)
    assert desk.brokers['spot'].open_positions()
    assert desk.brokers['fut'].open_positions()
    assert desk.coordinator.book.positions()


def test_closing_from_the_screen_leaves_both_books_empty(desk, browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.MT5Trader.state.monitorTab = 'positions';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .close-position', timeout=8000)
    page.click('.monitor .close-position')

    desk.wait_for(lambda: not desk.brokers['spot'].positions_by_magic(),
                  'leg A never closed')
    desk.wait_for(lambda: not desk.brokers['fut'].positions_by_magic(),
                  'leg B never closed')

    # Closed by TICKET: one market order in, one close out, per leg —
    # never an opposite order that would open a second position.
    for name in ('spot', 'fut'):
        actions = [entry['action'] for entry in desk.brokers[name].sent
                   if entry['action'] in ('market', 'close')]
        assert actions.count('close') == 1
    # The manual position is untouched by any of it.
    assert desk.brokers['fut'].open_positions()

    position = desk.coordinator.book.positions(open_only=False)[0]
    assert not position.is_open
    assert position.realized_pnl is not None
    # And the round trip is in the journal, both ends, both legs.
    desk.wait_for(lambda: len(Store(desk.paths['db']).fills()) >= 5,
                  'the closing fills never reached the journal')


def test_the_closed_trade_is_in_the_record_after_everything(desk,
                                                            round_turn):
    store = Store(desk.paths['db'])
    assert store.open_positions() == []
    closed = store.closed_positions()
    assert len(closed) == 1
    assert closed[0]['close_reason']
    assert closed[0]['entry_spread'] is not None

    # Two legs, in and out — plus the trader's own click on leg B's
    # symbol, which belongs to that pair's totals as much as ours does.
    # The broker's statement does not separate them, and neither does
    # this.
    totals = store.fill_totals('XAUUSD_|GC1226')
    assert totals['fills'] == 5
    assert totals['commission'] < 0
    ours = store.fills(pair_key='XAUUSD_|GC1226', ours_only=True)
    assert len(ours) == 4
    assert sorted(fill['entry'] for fill in ours) == \
        ['close', 'close', 'open', 'open']
    # The audit trail answers "what happened" without being the place
    # the operator was ever SENT to.
    assert store.events('recovery')


def test_the_slippage_report_covers_the_session_that_was_just_traded(
        desk, round_turn):
    """The report, over a REAL session: the one this module just traded.

    Nothing here is modelled. The entry was measured against the price
    that was clicked, the exit against the touch the close was sent at,
    and both came back through the broker, the journal and the database
    before this test read them.
    """
    app = create_app(desk.paths['status'], desk.paths['commands'],
                     desk.paths['results'], desk.paths['config'],
                     desk.paths['db'])
    app.config.update(TESTING=True)

    body = app.test_client().get('/api/slippage').get_json()

    assert body['ok']
    assert body['counts']['positions'] == 1
    assert body['counts']['closed'] == 1
    entry = body['overall']['entry']
    assert entry['measured'] == 1 and entry['unmeasured'] == 0
    assert entry['points_mean'] is not None
    # A market click that crosses the touch costs money at both ends;
    # the round turn is the two added, never one of them doubled.
    exit_ = body['overall']['exit']
    assert exit_['measured'] == 1
    assert body['overall']['round_trip']['points_mean'] == pytest.approx(
        entry['points_mean'] + exit_['points_mean'])
    # The window is cut on the broker's clock, and the journal is
    # counted over the same stretch as a check on coverage.
    assert body['window']['clock'] == 'broker'
    assert body['journal']['fills'] >= 4
    # WHICH LEG: each one's send price came back through the leg runner
    # and the database, so every leg at both ends is measured - here at
    # zero, because the fake fills at its quote.
    [row] = body['rows']
    for key in ('entry_leg_a', 'entry_leg_b', 'exit_leg_a', 'exit_leg_b'):
        assert row[key] == pytest.approx(0.0), key
    csv_text = app.test_client().get('/api/slippage.csv').get_data(
        as_text=True)
    header = csv_text.splitlines()[0].split(',')
    for column in ('entry_leg_a', 'exit_leg_a', 'entry_leg_b', 'exit_leg_b'):
        assert column in header


def test_the_slippage_report_is_on_the_screen(desk, browser_page, round_turn):
    page = browser_page
    page.evaluate("""() => {
        window.MT5Trader.state.open = ['monitor:'];
        window.MT5Trader.state.monitorTab = 'slippage';
        window.MT5Trader.state.slippage = null;
        window.MT5Trader.render();
    }""")
    page.wait_for_function(
        "() => document.querySelector('.monitor .pane').textContent"
        ".includes('Round turn')", timeout=8000)
    text = page.text_content('.monitor .pane')
    assert 'Entries by ladder' in text and 'XAUUSD_|GC1226' in text
    assert 'MARKET' in text                      # by order type
    assert 'unmeasured' not in text              # everything was priced
    assert page.errors == []


def test_the_analysis_covers_the_round_turn_just_traded(desk, round_turn):
    """The Analysis tab's report, over the trade this module made: in
    the summary, in the journal with both legs, and nothing invented
    for what was not measured."""
    app = create_app(desk.paths['status'], desk.paths['commands'],
                     desk.paths['results'], desk.paths['config'],
                     desk.paths['db'])
    app.config.update(TESTING=True)

    body = app.test_client().get('/api/analysis?days=1').get_json()

    assert body['ok']
    assert body['summary']['closed'] == 1
    assert body['summary']['net_pnl'] is not None
    [trip] = body['journal']
    assert trip['pair_key'] == 'XAUUSD_|GC1226'
    assert trip['entry_spread'] is not None and trip['exit_spread'] is not None
    assert trip['leg_a']['entry'] is not None
    assert trip['cum_pnl'] == pytest.approx(trip['pnl'])
    assert trip['trade_no'] == 1              # the first trade ever
    # A manual trade has no Algo z at either end — a dash, not 0.
    assert trip['entry_z'] is None
    assert body['drawdown']['trades'] == 1


def test_the_printable_report_covers_the_same_trade(desk, round_turn):
    """/report: the Analysis record as a printable page - the logo, the
    headline returns and the trade journal - and the trader's own logo
    in place of the Nexus mark once one is put beside config.json."""
    import os
    app = create_app(desk.paths['status'], desk.paths['commands'],
                     desk.paths['results'], desk.paths['config'],
                     desk.paths['db'])
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.get('/report?days=1')
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'Trading Report' in page and 'class="mark"' in page
    for heading in ('Net P&amp;L', 'Return', 'time-weighted', 'On margin',
                    'Win rate', 'Max drawdown', 'Performance statistics',
                    'Equity curve',
                    'Trade journal', 'Print / Save as PDF'):
        assert heading in page, heading
    assert '1 closed' in page and 'Last day' in page
    hidden = client.get('/report?days=1&pnl=hide').get_data(as_text=True)
    assert '<th>Net P&amp;L</th>' not in hidden
    assert 'href="/report?days=1&amp;pnl=show"' in hidden
    # The CLIENT's report: returns only - the ladder, the journal and
    # the count of trades are not on it. (The trader's page above, the
    # control, carries them.)
    mine = client.get('/report?view=client&days=1').get_data(as_text=True)
    assert 'Performance Report' in mine and 'Return on capital' in mine
    assert 'Returns by day' in mine and 'Max drawdown' in mine
    assert 'Funding is not' in mine
    for detail in ('Trade journal', 'Equity curve', 'Breakdown', '1 closed',
                   'OKX', 'Win rate'):
        assert detail not in mine, detail
    logo = os.path.join(os.path.dirname(desk.paths['config']),
                        'report_logo.png')
    with open(logo, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\nlogo')
    try:
        page = client.get('/report?days=1').get_data(as_text=True)
        assert 'src="data:image/png;base64,' in page
        assert 'class="mark"' not in page
    finally:
        os.remove(logo)


def test_the_analysis_tab_is_on_the_screen(desk, browser_page, round_turn):
    page = browser_page
    page.evaluate("""() => {
        window.MT5Trader.state.open = ['monitor:'];
        window.MT5Trader.state.monitorTab = 'analysis';
        window.MT5Trader.state.analysis = null;
        window.MT5Trader.state.analysisDays = 1;
        window.MT5Trader.render();
    }""")
    page.wait_for_function(
        "() => document.querySelector('.monitor .pane').textContent"
        ".includes('Trade journal')", timeout=8000)
    text = page.text_content('.monitor .pane')
    assert 'Deepest open drawdown' in text
    assert 'Trade no' in text and '#1' in text
    assert 'where a stop costs least' in text
    for heading in ('Summary', 'Drawdown', 'Take / hold calibration',
                    'What if held?', 'Backtest', 'Signals',
                    'Trade journal'):
        assert heading in text, heading
    assert '1 closed' in text
    # Net P&L masked, then left out, from the bar - and the report link
    # carries the choice.
    page.select_option('.monitor .an-pnl', 'mask')
    page.wait_for_function(
        "() => document.querySelector('.monitor .pane').textContent"
        ".includes('\u2022\u2022\u2022\u2022\u2022')", timeout=8000)
    trader = page.get_attribute('.monitor .an-report-trader', 'href')
    client = page.get_attribute('.monitor .an-report-client', 'href')
    assert 'pnl=mask' in trader and 'view=trader' in trader
    assert 'pnl=mask' in client and 'view=client' in client
    assert page.text_content('.monitor .an-report-trader') == 'Trader report'
    assert page.text_content('.monitor .an-report-client') == 'Client report'
    page.select_option('.monitor .an-pnl', 'hide')
    page.wait_for_function(
        "() => !Array.from(document.querySelector('.monitor .an-table')"
        ".querySelectorAll('th'))"
        ".some(th => th.textContent === 'Running')", timeout=8000)
    page.select_option('.monitor .an-pnl', 'show')
    # The tab button is there for the trader to find.
    assert page.locator('.monitor .tabs button[data-tab="analysis"]').count() == 1
    assert page.errors == []


def test_a_session_with_nothing_traded_is_not_a_slippage_of_zero(desk,
                                                                  tmp_path):
    """The report opens on an account that has done nothing, and says
    there is nothing to measure — rather than a table of 0.0000, which
    reads as a session of perfect fills."""
    paths = dict(desk.paths, db=str(tmp_path / 'empty.db'))
    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'], paths['db'])
    app.config.update(TESTING=True)

    body = app.test_client().get('/api/slippage').get_json()

    assert body['ok'] and body['counts']['positions'] == 0
    assert body['overall']['entry']['measured'] == 0
    assert body['overall']['entry']['points_mean'] is None
