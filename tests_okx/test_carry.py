"""The fair spread, priced from the swap the broker actually charges.

Every test here is a way the reading can be wrong while looking
perfectly reasonable: a swap read in the wrong units, one leg's
financing quietly dropped, both legs charged on the same side, a sign
typed without its minus. None of those produce an error — they produce
a NUMBER, beside the market, that a trader would act on.
"""

import pytest

from okxtrader import carry


def meta(symbol='X', bid=100.0, ask=100.1, contract=100.0, tick=0.01,
         tick_value=1.0, swap_long=None, swap_short=None, mode=None):
    return {'symbol': symbol, 'bid': bid, 'ask': ask,
            'contract_size': contract, 'tick_size': tick,
            'tick_value': tick_value, 'swap_long': swap_long,
            'swap_short': swap_short, 'swap_mode': mode}


# -- the units, which are the whole difficulty ---------------------------


def test_money_modes_are_taken_as_quoted():
    for mode in carry.MONEY_MODES:
        rate, note = carry.swap_per_lot_night(-4.5, mode)
        assert rate == pytest.approx(-4.5)
        assert 'per lot per night' in note


def test_points_are_priced_through_the_tick_value():
    """4.5 points is not 4.5 dollars. A point is worth tick_value per
    tick_size of price, so on a 0.01 tick worth $1 it is $450."""
    rate, note = carry.swap_per_lot_night(-4.5, carry.SWAP_POINTS,
                                          tick_size=0.01, tick_value=1.0)
    assert rate == pytest.approx(-450.0)
    assert '100.00 per point' in note


def test_an_annual_percent_needs_a_price_and_says_so_when_it_has_none():
    rate, note = carry.swap_per_lot_night(-3.0, carry.SWAP_INTEREST_CURRENT,
                                          contract_size=100.0, price=4300.0)
    assert rate == pytest.approx(430000.0 * -3.0 / 100.0 / 360.0)
    missing, why = carry.swap_per_lot_night(-3.0, carry.SWAP_INTEREST_CURRENT)
    assert missing is None and 'contract size' in why


def test_an_unconvertible_swap_is_not_a_zero_swap():
    """The rule this module exists for. A mode nothing can read, or a
    swap with no mode at all, returns None WITH THE REASON — 0.00 on
    the screen would read as "this costs nothing to hold"."""
    assert carry.swap_per_lot_night(-4.5, carry.SWAP_REOPEN_BID)[0] is None
    assert carry.swap_per_lot_night(-4.5, None)[0] is None
    assert carry.swap_per_lot_night(None, carry.SWAP_POINTS)[0] is None
    # ...and the control: a mode it CAN read returns the number.
    assert carry.swap_per_lot_night(-4.5, carry.SWAP_CURRENCY_DEPOSIT)[0] \
        == pytest.approx(-4.5)


def test_a_disabled_swap_really_is_zero():
    """The one case where 0 is a measurement, not a missing one."""
    rate, note = carry.swap_per_lot_night(-4.5, carry.SWAP_DISABLED)
    assert rate == 0.0 and 'no swap' in note


# -- the sign, which is the trade ----------------------------------------


def test_the_two_legs_are_charged_on_opposite_sides():
    """Buying the spread is long B and short A, so it reads B's LONG
    swap and A's SHORT one. Reading swap_long on both prices a trade
    nobody places."""
    a = meta('A', swap_long=-1.0, swap_short=-9.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('B', swap_long=-2.0, swap_short=-5.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)

    buy = carry.carry_money(a, b, 'BUY', 1.0, 1.0, 10)
    sell = carry.carry_money(a, b, 'SELL', 1.0, 1.0, 10)

    assert buy['money'] == pytest.approx((-9.0 + -2.0) * 10)
    assert sell['money'] == pytest.approx((-1.0 + -5.0) * 10)
    assert buy['money'] != sell['money']


def test_a_credit_is_kept_as_a_credit():
    """Being PAID to wait is the case worth finding, so the sign
    survives: collapsing both legs to costs would hide it."""
    a = meta('A', swap_long=+3.0, swap_short=+3.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('B', swap_long=+1.0, swap_short=+1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    assert carry.carry_money(a, b, 'BUY', 1.0, 1.0, 5)['money'] > 0


def test_one_unpriced_leg_makes_the_whole_estimate_none():
    """Half a carry estimate is not a smaller estimate. A fair value
    that quietly dropped a leg's financing reads as an edge that is not
    there."""
    a = meta('A', swap_long=-1.0, swap_short=-1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('B', swap_long=-2.0, swap_short=-2.0, mode=None)   # no mode

    body = carry.carry_money(a, b, 'BUY', 1.0, 1.0, 10)

    assert body['money'] is None
    assert 'swap_mode' in body['reason']
    # The control: give B its mode and the same call answers.
    b['swap_mode'] = carry.SWAP_CURRENCY_SYMBOL
    assert carry.carry_money(a, b, 'BUY', 1.0, 1.0, 10)['money'] is not None


# -- the reading itself --------------------------------------------------


def test_the_fair_spread_is_minus_carry_over_k_and_is_size_free():
    """`-carry/k` does not move when the clip does — which is what lets
    it sit beside a ladder quoted in spread points."""
    a = meta('A', swap_long=-1.0, swap_short=-1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('B', swap_long=-1.0, swap_short=-1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)

    one = carry.describe(a, b, 1.0, 1.0, 1.0, spread_units=100.0, days=10)
    ten = carry.describe(a, b, 1.0, 10.0, 10.0, spread_units=1000.0, days=10)

    assert one['fair_buy'] == pytest.approx(0.2)      # -(-2 x 10)/100
    assert ten['fair_buy'] == pytest.approx(one['fair_buy'])


def test_the_gap_is_measured_on_the_price_each_direction_would_trade_at():
    """There is no mid here and there is not meant to be. A buy pays
    the ask, a sell receives the bid, and a gap measured off a midpoint
    is a comparison against a price nobody fills at."""
    a = meta('A', swap_long=-1.0, swap_short=-1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('B', swap_long=-1.0, swap_short=-1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    market = {'long_spread': 3.16, 'short_spread': 3.06, 'spread': 3.11}

    body = carry.describe(a, b, 1.0, 1.0, 1.0, 100.0, 10, market=market)

    assert body['gap_buy'] == pytest.approx(3.16 - body['fair_buy'])
    assert body['gap_sell'] == pytest.approx(3.06 - body['fair_sell'])
    # Neither reading came off the mid.
    assert body['gap_buy'] != pytest.approx(3.11 - body['fair_buy'])


def test_a_typed_override_wins_and_a_blank_one_clears_it():
    """Blank means "use MT5's", and 0 is a real statement — so the
    override is None-checked, never truthiness-checked. An override
    that cannot be deleted outlives the pair it was typed for."""
    m = meta('A', swap_long=-1.0, swap_short=-1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    assert carry.leg_rate(m, 'short', override=-4.0)[0] == pytest.approx(-4.0)
    assert carry.leg_rate(m, 'short', override=0.0)[0] == 0.0
    assert carry.leg_rate(m, 'short', override=None)[0] == pytest.approx(-1.0)
    assert carry.overrides_for({'swap_a_short_per_lot': ''}, 'a',
                               'short') is None
    assert carry.overrides_for({'swap_a_short_per_lot': 0}, 'a',
                               'short') == 0.0


def test_a_pair_that_cannot_have_an_expiry_is_not_prompted_for_one():
    """WTI vs Brent legitimately has no expiry. Prompting there is an
    error message for a correct configuration, and an operator who has
    just typed one cannot tell whether it was rejected or ignored."""
    related = carry.describe(meta(), meta(), 1.0, 1.0, 1.0, 100.0, None,
                             expects_expiry=False)
    assert related['fair_buy'] is None
    assert 'no carry ties them together' in related['note']

    basis = carry.describe(meta(), meta(), 1.0, 1.0, 1.0, 100.0, None,
                           expects_expiry=True)
    assert "expiry" in basis['note'] and 'Exchanges' in basis['note']


# -- the cross-check -----------------------------------------------------


def test_a_swap_and_a_rate_that_disagree_REPLACE_the_reading():
    """A conclusion drawn from an input that can be proven wrong should
    not render at all. In the system this is ported from, +58.00 where
    -58.00 belonged produced "you are paid to hold this at any spread".
    """
    a = meta('SPOT', bid=4300.0, ask=4300.2, swap_long=+58.0, swap_short=+58.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('FUT', bid=4360.0, ask=4360.4, swap_long=-1.0, swap_short=-1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)

    body = carry.describe(a, b, 1.0, 1.0, 1.0, 100.0, 30, rate_pct=4.5)

    assert body['warning']
    assert body['fair_buy'] is None and body['fair_sell'] is None
    assert body['disputed']['fair_buy'] is not None   # kept, not rendered


def test_two_estimates_that_agree_leave_the_reading_alone():
    """The control. Without it the test above would pass on a system
    that suppresses every fair value it ever computes."""
    spot = 4300.0
    # A 4.5% annual rate over 30 days on 4,300 is about 15.9 of basis;
    # a swap of -53 a night on a 1-lot leg over 30 days, divided by
    # k = 100, is 15.9 the other way round — the same story.
    a = meta('SPOT', bid=spot, ask=spot + 0.2, swap_long=-53.0,
             swap_short=-53.0, mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('FUT', bid=4360.0, ask=4360.4, swap_long=0.0, swap_short=0.0,
             mode=carry.SWAP_DISABLED)

    body = carry.describe(a, b, 1.0, 1.0, 1.0, 100.0, 30, rate_pct=4.5)

    assert body['warning'] is None, body['warning']
    assert body['fair_buy'] == pytest.approx(15.9, abs=1.0)


def test_a_long_leg_showing_a_credit_is_named_with_its_correction():
    """This check needs no second estimate — which matters, because a
    RELATED pair has none and an inverted sign sailed straight through
    on those."""
    a = meta('XAGUSD', swap_long=+58.0, swap_short=-58.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('SIU6', swap_long=-1.0, swap_short=-1.0,
             mode=carry.SWAP_CURRENCY_SYMBOL)

    # SELL the spread is long A, and A's long swap is a credit.
    body = carry.describe(a, b, 1.0, 1.0, 1.0, 100.0, 30)

    assert 'CREDIT' in body['warning'] and 'XAGUSD' in body['warning']
    assert body['fix'] == {'field': 'swap_a_long_per_lot', 'value': -58.0,
                           'symbol': 'XAGUSD'}
    assert body['fair_buy'] is None      # replaced, not printed beneath


def test_ordinary_swaps_raise_nothing():
    """The control for the credit check: both legs charged, as normal."""
    a = meta('A', swap_long=-1.0, swap_short=-0.5,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    b = meta('B', swap_long=-2.0, swap_short=-1.5,
             mode=carry.SWAP_CURRENCY_SYMBOL)
    body = carry.describe(a, b, 1.0, 1.0, 1.0, 100.0, 30)
    assert body['warning'] is None and body['fix'] is None
    assert body['fair_buy'] is not None


# -- through the engine --------------------------------------------------


def test_the_ladder_gets_a_fair_spread_from_the_brokers_own_swaps(
        config, pair, legs, gold_symbols):
    """End to end: MT5's swap fields travel with their MODE, the
    coordinator prices both directions, and the panel gets a number it
    can show beside the two executable prices."""
    from okxtrader.carry import SWAP_CURRENCY_SYMBOL
    from okxtrader.coordinator import Coordinator
    spot, future = gold_symbols
    for symbol, rate in ((spot, -1.20), (future, -0.40)):
        symbol.swap_long = rate
        symbol.swap_short = rate / 2.0
        symbol.swap_mode = SWAP_CURRENCY_SYMBOL
    pair.expiry = '2026-09-26'
    coordinator = Coordinator(config, legs)
    coordinator.session_clock.now = lambda: __import__(
        'datetime').datetime(2026, 8, 27)
    coordinator.session_clock.offset = lambda: 0
    coordinator.start()
    coordinator.poll_once()

    fair = coordinator.snapshot()['pairs'][pair.key]['fair']

    assert fair['source'] == 'swap'
    assert fair['days_to_expiry'] == 30
    assert fair['fair_buy'] is not None and fair['fair_sell'] is not None
    # The two directions are charged differently, so they differ.
    assert fair['fair_buy'] != fair['fair_sell']
    assert fair['gap_buy'] is not None


def test_a_symbol_with_no_swap_mode_shows_the_reason_not_a_zero(
        config, pair, legs):
    """The control on the same path: nothing reported, nothing shown —
    and the panel says which symbol could not be priced."""
    from okxtrader.coordinator import Coordinator
    pair.expiry = '2026-09-26'
    coordinator = Coordinator(config, legs)
    coordinator.start()
    coordinator.poll_once()

    fair = coordinator.snapshot()['pairs'][pair.key]['fair']

    assert fair['fair_buy'] is None
    assert 'XAUUSD_' in fair['note'] or 'GC1226' in fair['note']


def test_the_carry_inputs_apply_without_a_restart(config, pair, legs,
                                                  tmp_path):
    """Symbols, contract sizes and beta are structural. Four fields
    whose only consumer is a panel are not — and blocking them behind a
    restart is what put "an assets change requires a restart" ten lines
    above a live trade while the values sat saved and correct."""
    import json
    from okxtrader.coordinator import Coordinator
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'pairs': {pair.key: {'expiry': None}}}))
    config.path = str(path)
    coordinator = Coordinator(config, legs)
    coordinator.poll_once()
    assert coordinator.snapshot()['pairs'][pair.key]['fair'][
        'days_to_expiry'] is None

    path.write_text(json.dumps({'pairs': {pair.key: {
        'expiry': '2026-09-26'}}}))
    # Nothing restarted; the next poll reads it.
    coordinator.session_clock.now = lambda: __import__(
        'datetime').datetime(2026, 8, 27)
    coordinator.session_clock.offset = lambda: 0
    coordinator.poll_once()

    fair = coordinator.snapshot()['pairs'][pair.key]['fair']
    assert fair['days_to_expiry'] == 30


def test_a_config_that_did_not_change_is_not_re_read(config, pair, legs,
                                                     tmp_path):
    """The control: a watcher that opens the file every poll is a
    watcher that costs a syscall three times a second for nothing."""
    import json
    from okxtrader.coordinator import Coordinator
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'pairs': {pair.key: {'expiry': None}}}))
    config.path = str(path)
    coordinator = Coordinator(config, legs)
    coordinator.reload_reference_fields()

    assert coordinator.reload_reference_fields() == []


# -- one ladder's settings are its own -----------------------------------


def test_each_ladder_prices_its_own_exit(config, pair, legs):
    """A gold basis and an oil differential are charged different
    commissions and held for different lengths of time. One set of
    numbers covering both is a set that is wrong for at least one of
    them — so the ladder's own value wins over the system default."""
    from okxtrader.coordinator import Coordinator
    config.settings['COMMISSION_PER_LOT_A'] = 2.0
    config.settings['COMMISSION_PER_LOT_B'] = 2.0
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    pair.commission_per_lot_a = 20.0        # this ladder is expensive
    coordinator = Coordinator(config, legs)
    coordinator.start()
    coordinator.poll_once()

    exit_box = coordinator.snapshot()['pairs'][pair.key]['exit']

    # 2 x (20.00 x 0.1 + 2.00 x 0.1) = 4.40, not the default's 0.80.
    assert exit_box['commission'] == pytest.approx(4.40)


def test_a_blank_field_means_the_default_and_zero_means_zero(pair):
    """The difference the whole per-ladder form turns on. Blank is not
    0: a form that reads it as 0 charges a commission of nothing to
    every pair the operator has not visited."""
    defaults = {'COMMISSION_PER_LOT_A': 7.0, 'TP_TARGET_PCT_OF_MARGIN': 2.0}

    assert pair.exit_settings(defaults)['COMMISSION_PER_LOT_A'] == 7.0

    pair.commission_per_lot_a = 0.0
    assert pair.exit_settings(defaults)['COMMISSION_PER_LOT_A'] == 0.0

    pair.commission_per_lot_a = None            # cleared again
    assert pair.exit_settings(defaults)['COMMISSION_PER_LOT_A'] == 7.0


def test_the_per_ladder_settings_apply_without_a_restart(config, pair, legs,
                                                         tmp_path):
    """They are saved to the config and read on the next poll — which is
    what lets one form both persist a setting and change the ladder in
    front of the trader."""
    import json
    from okxtrader.coordinator import Coordinator
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'pairs': {pair.key: {}}}))
    config.path = str(path)
    coordinator = Coordinator(config, legs)
    coordinator.start()
    coordinator.poll_once()
    assert pair.algo_window is False

    path.write_text(json.dumps({'pairs': {pair.key: {
        'algo_window': True, 'tp_target_pct_of_margin': 5.0,
        'order_type': 'MARKET', 'overnight': 'EXIT_IF_PROFIT'}}}))
    coordinator.poll_once()

    assert pair.algo_window is True
    assert pair.tp_target_pct_of_margin == 5.0
    assert pair.order_type.value == 'MARKET'
    assert pair.overnight.value == 'EXIT_IF_PROFIT'


def test_the_engine_publishes_the_fair_window_setting(config, pair, legs,
                                                      tmp_path):
    """The UI opens the window the moment the box is ticked; the ENGINE
    is what keeps it open on every poll after that. Both halves, or the
    window appears and then vanishes a third of a second later.

    A field the command bridge does not know is silently ignored — the
    save reports success and nothing happens, which is the exact shape
    of the fault this test exists to catch.
    """
    from okxtrader.commands import CommandRunner
    from okxtrader.coordinator import Coordinator
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    assert coordinator.snapshot()['pairs'][pair.key][
        'show_fair_window'] is False

    runner = CommandRunner(coordinator, str(tmp_path / 'commands.jsonl'),
                           str(tmp_path / 'results.json'))
    answer = runner._do_set_pair({'pair': pair.key,
                                  'fields': {'algo_window': True,
                                             'auto_route': True}})

    assert answer['applied']['algo_window'] is True
    # AutoRouting has been removed: a page still sending it changes
    # nothing, rather than failing the whole save.
    assert 'auto_route' not in answer['applied']
    coordinator.poll_once()
    assert coordinator.snapshot()['pairs'][pair.key][
        'show_fair_window'] is True
