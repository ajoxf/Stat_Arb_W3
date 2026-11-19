"""The signal journal is the Algo's OWN decision, with the Algo on or off.

The fault these are written against was measured on a live desk. In
one afternoon the real Algo produced four signals, every one held back
by its edge filter - and the journal showed dozens of trades, both
ways, most of them "took profit". The journal ran its own COPY of the
Algo's rules, and the copy did not match:

1. it read ONE z off the mid for both sides, where the Algo reads each
   side's own executable price - on a spread whose bid-ask is wide
   against sigma, that put it well ahead of the Algo either way;
2. with the Algo ON it skipped the edge filter altogether;
3. it opened a new trade on every signal, where the Algo holds one;
4. it traded through a stale feed, which the Algo refuses;
5. it had no day's limit, no cooldown and no confirmation.

Every test here drives REAL PRICES through the real engine and reads
what the journal wrote. The decisive one runs the Algo LIVE, with real
fills on the test brokers, and asks the journal for exactly the trades
the desk took.

The prices are set by where they put the SELL side's executable spread
against the band: `ARM` stretches it past the entry z, `WINDOW` brings
it back inside the re-entry window, `HOME` takes it back through the
floor.
"""

import sqlite3

import pytest

from okxtrader import algo as algo_module, shadow as shadow_module
from okxtrader.coordinator import Coordinator
from okxtrader.database import Store

from test_algo_desk import QUIET, Clock, block, give_history

#: Where each step puts the bid-side spread, from the band's mean. On
#: the test band (mean 59.10, sigma 0.10) these land the Algo's own
#: SELL z at about +2.56, +1.88 and -0.10.
ARM, WINDOW, HOME = 0.50, 0.30, 0.00
#: Far enough back that a short's offer is under its break-even.
BACK = -0.40

#: The desk's shape: re-entry on, arm at 2.5, back at 2.0.
REENTRY = dict(reentry_on=True, entry_z=2.5, reentry_back=0.5)


class Desk:
    """The engine, one ladder, and a way to put its spread anywhere."""

    def __init__(self, config, pair, legs, tmp_path, algo='DRY_RUN',
                 store=None, **params):
        give_history(legs)
        pair.algo_params = dict(QUIET, **params)
        pair.tp_target_pct_of_margin = 0.0        # the target is break-even
        self.pair, self.legs = pair, legs
        self.store = store or Store(str(tmp_path / 'trader.db'))
        self.clock = Clock()
        self.co = Coordinator(config, legs, sleep=lambda s: None,
                              clock=self.clock, store=self.store)
        self.co.start()
        self.co.poll_once()
        # Turning the Algo on loads MT5's history into the candle store,
        # which is also what the analysis band reads - so ON and OFF
        # judge on the same band.
        on = self.co.set_algo(pair.key, 'ALGO', mode='DRY_RUN')
        assert on['ok'], on
        self.co.poll_once()
        self.mean = block(self.co, pair)['mean']
        if algo == 'LIVE':
            on = self.co.set_algo(pair.key, 'ALGO', mode='LIVE',
                                  confirmed=True)
            assert on['ok'], on
        elif algo == 'OFF':
            assert self.co.set_algo(pair.key, 'NONE')['ok']
            self.co._bands.clear()
        md = self.co.market[pair.key]
        self.a_ask = md['leg_a_ask']

    def push(self, *offsets, width=0.10, every=20.0):
        """Put the bid-side spread at mean + offset, one poll each."""
        bodies = []
        for offset in offsets:
            bid = round(self.a_ask + self.mean + offset, 2)
            self.legs['acct_b'].broker.quote('GC1226', bid,
                                             round(bid + width, 2))
            self.clock.now += every
            self.co.poll_once()
            bodies.append(block(self.co, self.pair))
        return bodies

    def rows(self):
        return sorted(self.store.signals(self.pair.key),
                      key=lambda r: r['armed_at'])

    def entered(self):
        return [r for r in self.rows() if r['entered_at'] is not None]

    def algo_entries(self):
        return [e for e in self.store.events('algo_signal')
                if (e['detail'] or {}).get('action') == 'ENTER']


def refused(desk_body):
    return desk_body.get('blocked') if desk_body else None


# -- 2. the edge filter is NOT skipped with the Algo on ----------------------

def test_with_the_algo_ON_the_edge_filter_still_refuses(config, pair, legs,
                                                       tmp_path):
    """The bug in one line: with the Algo on, the copy only refused a
    signal on the poll where the Algo itself had refused one - every
    other poll it entered. Live, it recorded an edge of 0.32x against
    the 1x required and entered anyway."""
    desk = Desk(config, pair, legs, tmp_path, edge_on=True,
                edge_multiple=1000.0, **REENTRY)
    bodies = desk.push(HOME, ARM, WINDOW, HOME) * 1
    bodies += desk.push(ARM, WINDOW, HOME)
    assert desk.entered() == []
    rows = desk.rows()
    assert [r['outcome'] for r in rows] == ['BLOCKED', 'BLOCKED']
    # In the Algo's own words, word for word.
    said = {refused(b) for b in bodies if refused(b)}
    assert {r['block_reason'] for r in rows} <= said
    assert all('edge filter' in r['block_reason'] for r in rows)
    assert all(r['edge_ratio'] < r['edge_required'] for r in rows)


def test_CONTROL_with_the_edge_filter_off_the_same_stretch_enters(
        config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM, WINDOW)
    [row] = desk.entered()
    assert row['side'] == 'SELL' and row['state'] == 'OPEN'
    # Entered where the Algo enters: back inside the window, at its own
    # executable z.
    assert 1.0 < row['entered_z'] <= 2.0


# -- 1. the Algo's own z, not the mid ----------------------------------------

def test_a_stretch_only_the_MID_shows_is_not_a_signal(config, pair, legs,
                                                     tmp_path):
    """The copy read z off the mid for both sides. On a spread whose own
    bid-ask is wide against sigma, the mid stretches long before the
    price a SELL would actually get does - so the copy signalled what
    the Algo never saw. Here the mid is past the entry z and the bid is
    nowhere near it."""
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    # The future's offer lifts and its bid does not: the mid rises, the
    # price a sell receives stays home.
    [body] = desk.push(0.20, width=1.20)
    assert body['z_mid'] >= 2.5 > body['z_sell']
    assert not body['armed']['SELL']
    assert desk.rows() == []


def test_CONTROL_when_the_price_a_sell_gets_stretches_it_is_one(
        config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    [body] = desk.push(ARM)
    assert body['z_sell'] >= 2.5 and body['armed']['SELL']
    [row] = desk.rows()
    assert row['side'] == 'SELL' and row['state'] == 'ARMED'
    # Armed at the Algo's own z for that side - the same number the
    # desk's panel shows.
    assert row['armed_z'] == pytest.approx(body['z_sell'], abs=1e-3)


# -- 3. one trade at a time ---------------------------------------------------

def test_a_signal_while_in_a_trade_is_not_a_second_trade(config, pair, legs,
                                                        tmp_path):
    """The copy opened a new trade on every signal; the Algo holds one.
    Twenty overlapping 'trades' all closed on one print is how one move
    became twenty winners on the live page."""
    desk = Desk(config, pair, legs, tmp_path, edge_on=False,
                cooldown_min=0, **REENTRY)
    desk.push(HOME, ARM, WINDOW)                 # in
    desk.push(0.10, ARM, WINDOW)                 # still in: out and back
    first, second = desk.rows()
    assert first['state'] == 'OPEN'
    # Confirmed while the first is on: held back, and still armed.
    assert second['state'] == 'ARMED'
    assert 'one trade at a time' in second['block_reason']
    desk.push(HOME)                              # and the stretch ends
    first, second = desk.rows()
    assert second['outcome'] == 'BLOCKED' and second['entered_at'] is None
    assert len(desk.entered()) == 1


def test_CONTROL_once_the_trade_is_closed_the_next_one_enters(
        config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False,
                cooldown_min=0, **REENTRY)
    desk.push(HOME, ARM, WINDOW)                 # in
    desk.push(BACK)                              # out, at its target
    desk.push(HOME, ARM, WINDOW)                 # and in again
    first, second = desk.entered()
    assert first['outcome'] == 'TP'
    assert first['exit_reason'] == 'profit target (after costs)'
    assert second['state'] == 'OPEN'


# -- 4. a stale feed is not traded -------------------------------------------

def test_a_stale_feed_is_not_traded(config, pair, legs, tmp_path,
                                    monkeypatch):
    """The Algo will not act on a price it cannot trust - one leg frozen
    while the other moves swings the spread by the whole move. The copy
    traded straight through it."""
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM)
    import okxtrader.coordinator as coordinator_module
    monkeypatch.setattr(coordinator_module, 'stale_quote',
                        lambda md, age: 'leg B (GC1226): 15.2s old')
    [body] = desk.push(WINDOW)
    assert body['state'] == 'BLOCKED' and '15.2s old' in body['blocked']
    assert desk.entered() == []
    [row] = desk.rows()
    assert row['state'] == 'ARMED' and '15.2s old' in row['block_reason']


def test_CONTROL_and_when_the_feed_comes_back_the_entry_counts(
        config, pair, legs, tmp_path, monkeypatch):
    """A block that clears inside the same stretch is not the end of it:
    the entry that follows is what the Algo did."""
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM)
    import okxtrader.coordinator as coordinator_module
    real = coordinator_module.stale_quote
    monkeypatch.setattr(coordinator_module, 'stale_quote',
                        lambda md, age: 'leg B (GC1226): 15.2s old')
    desk.push(WINDOW)
    monkeypatch.setattr(coordinator_module, 'stale_quote', real)
    desk.push(WINDOW + 0.01)
    [row] = desk.entered()
    assert row['block_reason'] is None and row['state'] == 'OPEN'


# -- 5. the day's limit, the cooldown ----------------------------------------

def test_the_days_limit_holds_the_next_one_back(config, pair, legs,
                                                tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False,
                cooldown_min=0, max_trades_day=1, **REENTRY)
    desk.push(HOME, ARM, WINDOW, BACK)           # one trade, closed
    desk.push(HOME, ARM, WINDOW, HOME)
    assert len(desk.entered()) == 1
    last = desk.rows()[-1]
    assert last['outcome'] == 'BLOCKED'
    assert "the day's limit is 1" in last['block_reason']


def test_CONTROL_with_no_limit_the_second_one_trades(config, pair, legs,
                                                     tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False,
                cooldown_min=0, max_trades_day=0, **REENTRY)
    desk.push(HOME, ARM, WINDOW, BACK)
    desk.push(HOME, ARM, WINDOW)
    assert len(desk.entered()) == 2


def test_the_cooldown_after_a_trade_holds_the_next_one_back(
        config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False,
                cooldown_min=5, **REENTRY)
    desk.push(HOME, ARM, WINDOW, BACK)           # closed; cooldown starts
    desk.push(HOME, ARM, WINDOW, HOME)           # inside five minutes
    assert len(desk.entered()) == 1
    assert desk.rows()[-1]['block_reason'].startswith('cooldown')


# -- the arming: one stretch is one signal ------------------------------------

def test_a_second_poke_at_the_band_is_not_a_second_signal(config, pair,
                                                         legs, tmp_path):
    """Out past the band, eased back but not into the window, and out
    again: it stretched ONCE. That is the Algo's own arming."""
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM, 0.45, ARM + 0.1, 0.45, ARM)
    assert len(desk.rows()) == 1
    assert desk.algo_entries() == []


def test_CONTROL_back_through_the_floor_and_out_again_is_a_second(
        config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM, HOME, ARM)
    first, second = desk.rows()
    # The first let go without confirming - nothing refused it.
    assert first['outcome'] == 'NO_ENTRY'
    assert 'before a fresh price confirmed it' in first['exit_reason']
    assert second['state'] == 'ARMED'


# -- the levels: a stop inside the bid-ask is not a stop -----------------------

def test_a_stop_inside_the_bid_ask_is_refused_not_entered(config, pair,
                                                          legs, tmp_path):
    """Why a live desk's whole day closed at 0.0 min: entered at one
    executable side and marked against the other, a stop nearer than
    the bid-ask is reached on the FIRST mark - every time. The Algo's
    levels gate refuses it, and so must the journal, in its words."""
    desk = Desk(config, pair, legs, tmp_path, edge_on=False,
                stop_loss_on=True, stop_mode='ATR', atr_stop_mult=0.05,
                **REENTRY)
    desk.push(HOME, ARM, WINDOW, HOME)
    [row] = desk.rows()
    assert row['outcome'] == 'BLOCKED' and row['entered_at'] is None
    assert 'inside the bid-ask' in row['block_reason']


def test_CONTROL_a_stop_well_outside_it_is_taken(config, pair, legs,
                                                 tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False,
                stop_loss_on=True, stop_mode='ATR', atr_stop_mult=20.0,
                **REENTRY)
    desk.push(HOME, ARM, WINDOW)
    [row] = desk.entered()
    # Its stop is where the Algo would put it: the multiple of the ATR
    # frozen at entry, past break-even.
    assert row['entry_atr'] and row['sl'] == pytest.approx(
        row['break_even'] + 20.0 * row['entry_atr'])
    # And it is filed under the broker's day, the day the Algo counts.
    assert row['broker_day'] == desk.co.broker_day()


# -- with the Algo OFF ---------------------------------------------------------

def test_with_the_algo_OFF_it_journals_what_the_algo_would_do(
        config, pair, legs, tmp_path):
    """The whole point: 'is this worth switching on?' answered BEFORE it
    is switched on, by the same code that would then trade it."""
    desk = Desk(config, pair, legs, tmp_path, algo='OFF', edge_on=True,
                edge_multiple=1000.0, **REENTRY)
    assert not desk.co.algos.is_on(pair.key)
    desk.push(HOME, ARM, WINDOW, HOME)
    [row] = desk.rows()
    assert row['algo_mode'] == 'OFF'
    assert row['outcome'] == 'BLOCKED'
    assert 'edge filter' in row['block_reason']


def test_CONTROL_with_the_algo_OFF_and_nothing_refusing_it_enters(
        config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, algo='OFF', edge_on=False,
                **REENTRY)
    desk.push(HOME, ARM, WINDOW)
    [row] = desk.entered()
    assert row['algo_mode'] == 'OFF' and row['state'] == 'OPEN'
    # Nothing was sent: the shadow has no executor.
    assert legs['acct_a'].broker.sent == [] == legs['acct_b'].broker.sent


# -- THE DECISIVE ONE: LIVE, trade for trade ----------------------------------

def test_with_the_algo_LIVE_the_journal_is_the_trades_it_took(
        config, pair, legs, tmp_path):
    """Real fills on the test brokers, the real Algo trading - and the
    journal must hold exactly the trades the desk took: the same side,
    the same entry and exit, the same exit in the same words. And the
    stretch the desk did NOT take must say why, in the Algo's words."""
    desk = Desk(config, pair, legs, tmp_path, algo='LIVE', edge_on=False,
                cooldown_min=5, **REENTRY)
    desk.push(HOME, ARM, WINDOW, BACK)           # a trade, in and out
    desk.push(HOME, ARM, WINDOW, HOME)           # inside the cooldown
    real = [p for p in desk.store.positions_between(pair_key=pair.key)
            if p['source'] == 'ALGO']
    journal = desk.entered()
    assert len(real) == len(journal) == 1
    [taken], [row] = real, journal
    assert row['side'] == taken['side']
    assert row['entry'] == pytest.approx(taken['entry_spread'], abs=1e-9)
    assert row['exit_spread'] == pytest.approx(taken['exit_spread'],
                                               abs=1e-9)
    assert taken['close_reason'] == 'Algo: ' + row['exit_reason']
    # The second stretch: not taken by the desk, and the journal says
    # so in the Algo's words.
    last = desk.rows()[-1]
    assert last['outcome'] == 'BLOCKED'
    assert last['block_reason'].startswith('cooldown')


# -- a restart -----------------------------------------------------------------

def test_a_restart_keeps_the_open_trade_and_closes_it_on_its_exit(
        config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM, WINDOW)
    [row] = desk.entered()
    assert row['state'] == 'OPEN'
    # Same database, a new process.
    again = Coordinator(config, legs, sleep=lambda s: None,
                        clock=desk.clock, store=desk.store)
    again.start()
    desk.co = again
    desk.push(BACK)
    [row] = [r for r in desk.store.signals(pair.key)
             if r['entered_at'] is not None]
    assert row['outcome'] == 'TP'


def test_a_restart_does_not_re_arm_a_spread_that_has_not_moved(
        config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM)
    again = Coordinator(config, legs, sleep=lambda s: None,
                        clock=desk.clock, store=desk.store)
    again.start()
    desk.co = again
    desk.push(ARM, ARM + 0.1)
    assert len(desk.rows()) == 1


# -- the rows the copy wrote are cleared, once ---------------------------------

def test_the_rows_the_copy_wrote_are_purged_once(tmp_path):
    """Rows from before the journal ran on the Algo's own code describe
    trades the Algo would not have taken. They are cleared when the
    column that marks the new rows is added - and only then."""
    path = str(tmp_path / 'trader.db')
    store = Store(path)
    store.save_signal({'pair_key': 'A|B', 'basis': 'ALGO', 'side': 'SELL',
                       'state': 'CLOSED', 'outcome': 'TP', 'armed_at': 1.0,
                       'armed_z': 2.6})
    store.save_signal_arming('A|B', {'algo': {'SELL': True}})
    # The database as an older build left it: no `decided_by` column.
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE signal_events DROP COLUMN '
                           'decided_by')
    assert len(Store(path).signals('A|B')) == 0
    assert Store(path).signal_arming() == {}
    # CONTROL: with the column there, nothing is cleared.
    store = Store(path)
    store.save_signal({'pair_key': 'A|B', 'basis': 'ALGO', 'side': 'SELL',
                       'state': 'ARMED', 'armed_at': 2.0, 'armed_z': 2.6,
                       'decided_by': 'algo'})
    assert len(Store(path).signals('A|B')) == 1


def test_the_rows_armed_on_untrusted_prices_are_purged_once(tmp_path):
    """Rows from before the Algo refused to arm on a guarded price (and
    before a 0.0 side was no price) include stretches it was never
    really in. Cleared when the leg columns arrive - and only then."""
    path = str(tmp_path / 'trader.db')
    store = Store(path)
    store.save_signal({'pair_key': 'A|B', 'basis': 'ALGO', 'side': 'SELL',
                       'state': 'CLOSED', 'outcome': 'NO_ENTRY',
                       'armed_at': 1.0, 'armed_z': -1006.9,
                       'decided_by': 'algo'})
    store.save_signal_arming('A|B', {'algo': {'SELL': True}})
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE signal_events DROP COLUMN '
                           'leg_a_account')
    assert len(Store(path).signals('A|B')) == 0
    assert Store(path).signal_arming() == {}
    # CONTROL: with the column there, nothing is cleared.
    store = Store(path)
    store.save_signal({'pair_key': 'A|B', 'basis': 'ALGO', 'side': 'SELL',
                       'state': 'ARMED', 'armed_at': 2.0, 'armed_z': 2.6,
                       'decided_by': 'algo', 'leg_a_account': 'CFI'})
    [row] = Store(path).signals('A|B')
    assert row['leg_a_account'] == 'CFI'


# -- the shadow, on its own ----------------------------------------------------

def stats(mean=59.1, sigma=0.1):
    return {'ready': True, 'mean': mean, 'sigma': sigma, 'count': 20}


def md_at(z_sell, width=0.1, quote=1):
    bid = 59.1 + z_sell * 0.1
    return {'short_spread': bid, 'long_spread': bid + width,
            'mid_spread': bid + width / 2, 'quote_id': quote}


def test_the_shadow_is_the_algos_own_class():
    """Not a copy that mirrors it: the class itself."""
    shadow = shadow_module.ShadowAlgo(dict(QUIET, **REENTRY))
    assert type(shadow.signal) is algo_module.AlgoSignal


def test_the_shadow_refuses_on_a_price_it_cannot_trust():
    shadow = shadow_module.ShadowAlgo(dict(QUIET, **REENTRY))
    closes = [59.0, 59.2] * 10
    shadow.step(0, md_at(2.6, quote=1), stats(), closes, {}, {'k': 10.0})
    body = shadow.step(1, md_at(1.8, quote=2), stats(), closes,
                       {'health': 'leg B stale'}, {'k': 10.0})
    assert body['intents'] == [] and body['blocked'] == 'leg B stale'
    # CONTROL: the same price, trusted, is an entry.
    body = shadow.step(2, md_at(1.8, quote=3), stats(), closes, {},
                       {'k': 10.0})
    assert [i['action'] for i in body['intents']] == ['ENTER']


def test_the_shadows_warm_up_and_day_survive_a_restart_only_if_recent():
    params = dict(QUIET, warmup_min=10, max_trades_day=3, **REENTRY)
    shadow = shadow_module.ShadowAlgo(params)
    closes = [59.0, 59.2] * 10
    for t in range(0, 300, 5):
        shadow.step(t, md_at(0.0, quote=t), stats(), closes,
                    {'day': '2026-11-10'}, {'k': 10.0})
    shadow.day['trades'] = 2
    saved = shadow.to_dict(now=300)
    soon = shadow_module.ShadowAlgo(params, saved, now=400)
    assert soon.live_sec == pytest.approx(shadow.live_sec)
    assert soon.day['trades'] == 2
    # CONTROL: back an hour later, the warm-up starts again.
    late = shadow_module.ShadowAlgo(params, saved, now=300 + 3600)
    assert late.live_sec == 0.0
    # And state from the copy it replaced is not read at all.
    old = shadow_module.ShadowAlgo(params, {'SELL': True}, now=300)
    assert old.armed == {'BUY': False, 'SELL': False}


# -- what a row says it was measured against --------------------------------

def test_a_row_names_each_legs_account_and_broker(config, pair, legs,
                                                 tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM)
    [row] = desk.rows()
    assert (row['leg_a_account'], row['leg_b_account']) == \
        (pair.account_a, pair.account_b)
    # The terminal's own word for its broker, read live.
    assert row['leg_a_broker'] == 'FakeServer'
    assert row['leg_b_broker'] == 'FakeServer'


def test_a_row_carries_the_edge_reading_it_was_recorded_at(
        config, pair, legs, tmp_path):
    """With the filter OFF too: it is the number that says whether
    switching it on would have changed the row."""
    desk = Desk(config, pair, legs, tmp_path, edge_on=False, **REENTRY)
    desk.push(HOME, ARM)
    [row] = desk.rows()
    assert row['edge_on'] is False
    assert row['edge_ratio'] is not None and row['edge_ratio'] > 0
    assert row['edge_required'] is not None


def test_CONTROL_the_filter_on_is_said_as_on(config, pair, legs, tmp_path):
    desk = Desk(config, pair, legs, tmp_path, edge_on=True, **REENTRY)
    desk.push(HOME, ARM)
    [row] = desk.rows()
    assert row['edge_on'] is True
