"""The exit price: break-even, and break-even plus a target.

A trader clicking a spread wants one number back immediately — where do
I get out? — and it is not a matter of taste. It is arithmetic on
figures this system already has:

- **Break-even** is the level the OTHER side of the book must reach for
  the trade to be worth nothing. Entering long means lifting the offer
  and leaving on the bid, so the round turn of both legs' bid-ask is
  already inside the two prices being compared; what is left to recover
  is COMMISSION, both legs, both ends.
- **The target** is a percentage of the MARGIN the position ties up.
  Margin is what the trade actually costs to hold, and it is per
  account and per broker — so it is read from the terminals with
  `order_calc_margin` rather than guessed from notional.

Money becomes spread points through `k` — `spread_units x quantity`,
the one multiplier every conversion in this system uses. There is no
second one here.

Two rules kept from everywhere else:

- **Unmeasured is not zero.** No margin figure from the terminals, or
  no `k`, means no target: the box shows an em dash and says what is
  missing. A TP of 0.00 would read as "get out at break-even", which is
  a different instruction.
- **Nothing here places an order.** It is a price on the screen. No
  bracket is sent to the broker, nothing exits by itself, and a leg
  never carries a broker-side stop (spec §4).
"""

from . import carry as carry_module
from .models import SpreadSide


def points(money, spread_units, quantity=1.0):
    """`money` expressed in spread points, or None if it cannot be."""
    if money is None or not spread_units or not quantity:
        return None
    return money / (float(spread_units) * float(quantity))


def one_spread_units(position):
    """`k` for ONE spread of `position`, from its whole-size k."""
    units = getattr(position, 'spread_units', None)
    quantity = getattr(position, 'quantity', None) or 1.0
    if not units:
        return None
    return float(units) / float(quantity)


def commission(pair, settings, quantity=1.0):
    """Commission for one round turn of `quantity` spreads, in money."""
    from . import costs
    lots_a = (pair.clip_lots_a or 0.0) * float(quantity)
    lots_b = (pair.clip_lots_b or 0.0) * float(quantity)
    if not lots_a and not lots_b:
        return None
    return costs.mark_fees(lots_a, lots_b, settings)


def break_even_terms(pair, md, settings, quantity=1.0, spread_units=None,
                     nights=None, carry_for=None):
    """The four terms break-even is built from, each on its own.

    Spec section 5.2 writes it as

        BE = fill  +-  ( bid_ask_round_trip + commission
                         + slippage_allowance + swap x nights ) / k

    and the bid-ask term is the one to be careful with. Quoted on the
    CLOSING side — which is where break-even belongs, because your
    break-even is what you PAID and a long leaves on the bid — that
    round turn is already inside the two prices being compared. Adding
    it again is the bid-ask charged twice, which is the exact fault
    this system's cost model was rewritten to stop. So it is MEASURED
    and SHOWN, because it is the cost of the trade about to be done,
    and it is not added a second time.

    The other three are added, and each is None-able rather than
    zeroed: an unconvertible swap is not a zero swap, and break-even
    says so instead of quietly dropping the term.
    """
    settings = settings or {}
    terms = {'spread_width': None, 'spread_width_money': None,
             'commission': None, 'slippage_allowance': None,
             'swap_money': None, 'nights': nights, 'note': None,
             'added_money': None, 'added_points': None}

    long_spread = (md or {}).get('long_spread')
    short_spread = (md or {}).get('short_spread')
    # Measured live from both books. The setting is an OVERRIDE, and it
    # is clearable: blank means "use the measured value", and an
    # override that cannot be deleted outlives the pair it was typed
    # for.
    override = settings.get('BID_ASK_ROUND_TRIP_OVERRIDE')
    if override not in (None, ''):
        terms['spread_width_money'] = float(override)
        terms['spread_width'] = points(float(override), spread_units,
                                       quantity)
    elif long_spread is not None and short_spread is not None:
        terms['spread_width'] = long_spread - short_spread
        if spread_units:
            terms['spread_width_money'] = (terms['spread_width']
                                           * float(spread_units)
                                           * float(quantity))

    terms['commission'] = commission(pair, settings, quantity)
    allowance = settings.get('SLIPPAGE_ALLOWANCE')
    terms['slippage_allowance'] = (None if allowance in (None, '')
                                   else float(allowance) * float(quantity))

    # Swap: time-dependent, so break-even is only DEFINED given a
    # holding period. 0 nights is intraday and the term vanishes.
    if nights:
        rates = (carry_for or {})
        if rates.get('money') is None:
            terms['note'] = (rates.get('reason')
                             or 'the swap could not be converted, so a '
                                'break-even over nights cannot be quoted')
        else:
            # Signed: a credit REDUCES what has to be recovered.
            terms['swap_money'] = rates['money']
    else:
        terms['swap_money'] = 0.0

    parts = [terms['commission'] or 0.0, terms['slippage_allowance'] or 0.0]
    if terms['swap_money'] is None:
        return terms
    added = sum(parts) - terms['swap_money']
    terms['added_money'] = added
    terms['added_points'] = points(added, spread_units, quantity)
    if terms['added_points'] is None and added == 0.0:
        terms['added_points'] = 0.0
    return terms


def describe(pair, md, settings, margin_per_spread=None, quantity=1.0,
             spread_units=None, nights=None, carry_buy=None, carry_sell=None,
             session_range=None, margin_source=None):
    """Break-even and take-profit for both directions, in spread points.

    Returned as the LEVELS the market has to reach, on the side that
    would actually close the trade:

    - a long is entered at the offer and closed on the bid, so its exit
      levels are compared against the bid (`short_spread`);
    - a short is entered on the bid and closed at the offer.

    That is the same convention the ladder, the mark and the slippage
    report use. Anything else here would be a fourth definition of the
    spread on one screen.
    """
    body = {'break_even_buy': None, 'break_even_sell': None,
            'tp_buy': None, 'tp_sell': None,
            # The figures the exit is BUILT from, published beside it:
            # the round turn the market charges, the commission the
            # broker charges, the slippage BUDGET, the swap over the
            # nights held, and the profit the target asks for. A
            # break-even with no workings is a number to be trusted or
            # not; with them it can be checked.
            'spread_width': None, 'spread_width_money': None,
            'commission': None, 'slippage_allowance': None,
            'swap_money': None, 'nights': nights,
            'added_money': None, 'added_points': None,
            'target_money': None, 'target_pct': None,
            'target_points': None,
            # How far the target is, in the unit the ladder is quoted
            # in, with the session's own range beside it. At small size
            # a percentage of margin can be far outside anything the
            # pair moves in a day — the number is honest, but whether
            # it is REACHABLE is the trader's call, and they can only
            # make it if the range is on the screen.
            'session_range': session_range, 'target_reachable': None,
            'margin_per_spread': margin_per_spread,
            'margin_source': margin_source,
            # Which side each break-even is READ on. A long's is a BID
            # level and a short's is an ASK level, and unlabelled the
            # number reads as "the price now" rather than "the bid you
            # need" — which is exactly the fault where a short filled
            # at 54.98 showed +$0.02 while closing would have booked
            # -$0.58.
            'break_even_side_buy': 'bid', 'break_even_side_sell': 'ask',
            'note': None}
    if not md:
        body['note'] = 'no market'
        return body

    long_spread = md.get('long_spread')      # what a BUY pays
    short_spread = md.get('short_spread')    # what a SELL receives

    buy_terms = break_even_terms(pair, md, settings, quantity, spread_units,
                                 nights, carry_buy)
    sell_terms = break_even_terms(pair, md, settings, quantity, spread_units,
                                  nights, carry_sell)
    for field in ('spread_width', 'spread_width_money', 'commission',
                  'slippage_allowance'):
        body[field] = buy_terms[field]
    # The swap differs by direction — the two legs are charged on
    # opposite sides — so the panel carries both and the row shows the
    # one for the column it is in.
    body['swap_money'] = buy_terms['swap_money']
    body['swap_money_sell'] = sell_terms['swap_money']
    body['added_money'] = buy_terms['added_money']
    body['added_points'] = buy_terms['added_points']
    body['added_money_sell'] = sell_terms['added_money']
    body['added_points_sell'] = sell_terms['added_points']

    if long_spread is not None and buy_terms['added_points'] is not None:
        # Long: bought at the offer, leaves on the bid. Break-even is
        # the bid reaching the offer paid, plus what the two prices do
        # not already carry.
        body['break_even_buy'] = long_spread + buy_terms['added_points']
    if short_spread is not None and sell_terms['added_points'] is not None:
        body['break_even_sell'] = short_spread - sell_terms['added_points']
    if buy_terms['note'] or sell_terms['note']:
        body['note'] = buy_terms['note'] or sell_terms['note']
        return body

    pct = settings.get('TP_TARGET_PCT_OF_MARGIN')
    body['target_pct'] = pct
    if not pct:
        body['note'] = ('set TP_TARGET_PCT_OF_MARGIN in Settings to see a '
                        'target beside break-even')
        return body
    if not margin_per_spread:
        body['note'] = ('the terminals have not priced the margin for one '
                        'spread yet — break-even stands, the target does not')
        return body

    target_money = float(pct) / 100.0 * float(margin_per_spread) \
        * float(quantity)
    target_points = points(target_money, spread_units, quantity)
    body['target_money'] = target_money
    body['target_points'] = target_points
    if target_points is None:
        body['note'] = 'no spread_units yet, so money cannot become points'
        return body
    if body['break_even_buy'] is not None:
        body['tp_buy'] = body['break_even_buy'] + target_points
    if body['break_even_sell'] is not None:
        body['tp_sell'] = body['break_even_sell'] - target_points
    if session_range:
        body['target_reachable'] = target_points <= float(session_range)
    body['note'] = (f'{pct:g}% of {margin_per_spread:,.2f} margin per '
                    f'spread = {target_money:,.2f} = {target_points:,.4f} of '
                    f'spread' +
                    (f", against a {float(session_range):,.4f} session range"
                     if session_range else '') +
                    (f' (margin from {margin_source})' if margin_source
                     else ''))
    return body


def for_position(position, md, pair, settings, margin_per_spread=None,
                 nights=None, carry_for=None):
    """The same two numbers for a position that is ON — anchored on the
    price it was actually ENTERED at, not on the current touch.

    A take-profit that moves with the market is not a take-profit.
    """
    if position is None or position.entry_spread is None:
        return None
    quantity = position.quantity or 1.0
    # A POSITION's spread_units is its WHOLE size already (0.05 lots x
    # 1,000 = 50 for half a spread of 0.1 lots); every formula below
    # takes k for ONE spread and multiplies by `quantity` itself. Passing
    # the whole one charged the size twice: at 0.5 spreads the TP and SL
    # sat twice as far away as the % asked for.
    units = one_spread_units(position)
    # The same four terms as the pre-trade box, so the two never
    # disagree about what break-even means — only the anchor differs.
    terms = break_even_terms(pair, md, settings, quantity, units, nights,
                             carry_for)
    fee_points = terms['added_points'] or 0.0
    target_points = 0.0
    pct = settings.get('TP_TARGET_PCT_OF_MARGIN')
    if pct and margin_per_spread:
        target_points = points(
            float(pct) / 100.0 * float(margin_per_spread) * float(quantity),
            units, quantity) or 0.0
    if position.side is SpreadSide.BUY:
        break_even = position.entry_spread + fee_points
        return {'break_even': break_even, 'tp': break_even + target_points,
                # Whether there is a TARGET at all, as opposed to
                # break-even wearing its name. AutoRouting rests an
                # order on the first and never on the second: getting
                # out at break-even is a different instruction.
                'target_points': target_points, 'side': 'BUY'}
    break_even = position.entry_spread - fee_points
    return {'break_even': break_even, 'tp': break_even - target_points,
            'target_points': target_points, 'side': 'SELL'}
