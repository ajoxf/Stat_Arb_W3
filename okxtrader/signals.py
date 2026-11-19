"""Signals: armed, entered, and what became of them.

The zone track in `zones.py` answers "what did the spread DO". This
answers the two questions a trader actually asks of it:

- **how often did it signal** — counted the way the Algo counts, not
  the way a naive crossing check does;
- **would it have paid** — did the spread come back, and would the
  take-profit or the stop have been hit first, on the settings in
  force at the time.

Both are measured whether a ladder's Algo is on, off, dry or live. The
whole point is to be able to answer "is this worth switching on?"
BEFORE switching it on.

**A second poke at the band is not a second signal.** This is the fault
the module exists for. A spread that reaches +2.5, eases to +2.1 and
pushes back to +2.6 has stretched ONCE: it armed on the way out and
never came back inside, so there is nothing new to act on. Counting
that as two signals inflates every count on the page and makes a
quiet day look busy. So a side ARMS on the way out, stays armed, and
only re-arms after it has come back through the floor.

ONE rule, and it is the ladder's own. A fixed +-2 / +-3 sigma study
was tracked beside it for a while and taken back out: two sets of
counts on one page answered a question nobody was asking and made the
one that mattered harder to read. What a desk wants to know is whether
THESE settings would have made money, so `entry_z` and its re-entry
machine are the only threshold here - armed on the stretch out,
ENTERED only on the way back inside the window, disarmed if it falls
through the floor first. A trend rides the band and never comes back,
so it arms and never enters, which is the Algo working and worth
seeing.

**A signal is RECORDED, not simulated.** The z, the executable entry,
the two legs and the mark forward are the live book's own numbers, read
as they happened; the levels are the ones the Algo would have used on
the settings in force at that moment. The only thing that did not
happen is the ORDER - so a row's P&L is what the trade would have made,
never money taken. The one exception is a row stamped REPLAY, which
`backfill_signals` reconstructs from MT5's bars afterwards and which
says so on the screen.

Nothing here reads a price, a broker or a clock, and nothing here can
reach an order. It takes numbers and returns numbers.
"""

import math

#: Stamped on every row. One rule now, but the column stays: rows
#: written when there were three must still read back.
ALGO = 'ALGO'

#: WHERE A ROW CAME FROM, and it matters enough to be on the screen.
#:
#: LIVE is measured forward tick by tick, as it happened. REPLAY is
#: derived afterwards by running the ladder's own settings back over
#: MT5's bars, which is the only way to answer for a day that has
#: already gone - and it sees one look per candle, so a stop touched
#: and recovered inside fifteen minutes is invisible to it. Mixing the
#: two silently would let a trader read a candle-resolution guess as a
#: measurement.
LIVE, REPLAY = 'LIVE', 'REPLAY'
SOURCES = (LIVE, REPLAY)

#: The Algo's own exit reasons, and what each one settles as. Only the
#: take-profit and the stop say a level was REACHED; the rest closed
#: the trade for another reason entirely and are neither.
EXIT_OUTCOME = {'PROFIT_TARGET': 'TP', 'STOP_LOSS': 'SL'}

#: Where a signal is in its life.
ARMED = 'ARMED'          # stretched out; waiting (ALGO) or entered at once
OPEN = 'OPEN'            # would be on, and is marked forward
CLOSED = 'CLOSED'        # it is done; `outcome` says how

#: How it ended. REVERTED is NOT one of these: coming back to the mean
#: and taking a profit are different questions, and a trade can do
#: either without the other, so reversion is recorded beside the
#: outcome rather than instead of it.
TP = 'TP'                # the take-profit would have been reached
SL = 'SL'                # the stop would have been reached
EXPIRED = 'EXPIRED'      # neither, inside the watch window
NO_ENTRY = 'NO_ENTRY'    # armed, then disarmed without ever entering
#: The stretch came back into the window, and the Algo REFUSED it - its
#: edge, regime, trend or half-life filter, or a stop that would sit
#: inside the bid-ask. Not a trade, and the reason is on the row.
#:
#: This is the outcome the page was missing. Without it a desk reads
#: 147 losing trades where the Algo would have taken none, and the
#: edge filter refusing them - which is the filter doing its job - is
#: invisible.
BLOCKED = 'BLOCKED'
#: Closed by one of the Algo's OTHER exits - its z-stop, its time stop,
#: back to the mean in profit. `exit_reason` names which, in its words.
EXIT = 'EXIT'
OUTCOMES = (TP, SL, EXPIRED, NO_ENTRY, BLOCKED, EXIT)
#: Outcomes that are a trade the Algo took and closed.
SETTLED = (TP, SL, EXPIRED, EXIT)

#: Stamped on every row the Algo's OWN code decided - live through
#: `shadow.ShadowAlgo`, or replayed through `backtest.run`. Its column
#: was added with a purge: rows written before it came from a copy of
#: the Algo's rules that did not match the Algo.
DECIDED_BY = 'algo'

#: The Algo's own exit words, so a row says what the Algo would say.
#: Kept as data here (not imported) so this module stays arithmetic.
EXIT_WORDS = {
    'STOP_LOSS': 'stop loss',
    'PROFIT_TARGET': 'profit target (after costs)',
    'Z_STOP': 'z-stop',
    'MEAN_REVERSION': 'back to the mean, in profit',
    'TIME_STOP': 'time stop',
}


def new_signal(pair_key, basis, side, at, z, params, broker_day=None,
               threshold=None):
    """A signal the moment it arms. No levels priced yet —
    that happens when (and if) it enters."""
    return {
        'pair_key': pair_key, 'basis': basis, 'side': side,
        'broker_day': broker_day,
        'threshold': threshold,
        'state': ARMED, 'outcome': None,
        'armed_at': float(at), 'armed_z': round(float(z), 3),
        'entered_at': None, 'entered_z': None,
        'entry': None, 'break_even': None, 'tp': None, 'sl': None,
        'units': None, 'quantity': None,
        #: The two legs this entry would have crossed, and at what.
        #: None on a DERIVED row: a replay works on the spread series
        #: and never saw a leg's book, and a leg price invented from
        #: the spread would read as a fill nobody got.
        'leg_a_side': None, 'leg_a_entry': None,
        'leg_b_side': None, 'leg_b_entry': None,
        #: Which account, at which broker, each leg is on - and whether
        #: the edge filter was switched on - when the row was recorded.
        'leg_a_account': None, 'leg_a_broker': None,
        'leg_b_account': None, 'leg_b_broker': None,
        'edge_on': params.get('edge_on'),
        'closed_at': None, 'closed_z': None,
        #: The spread it would have come off at. Together with `entry`
        #: this is the move the money is the product of.
        'exit_spread': None,
        'peak_net': None, 'trough_net': None,
        #: What the trade would have MADE, in money, at its close. The
        #: page's "would have won / would have lost" is this, not which
        #: level was touched: an exit on the mean reversion in profit
        #: is a win however it got out.
        'net_pnl': None,
        'source': LIVE, 'exit_reason': None,
        'decided_by': DECIDED_BY,
        #: WHY the Algo refused it, in its own words, and the edge
        #: reading at that moment. An edge of 0.02x against the 1.5x
        #: required is the single most useful number on a page about
        #: whether these settings are worth switching on.
        'block_reason': None, 'edge_ratio': None, 'edge_required': None,
        'reverted': False, 'revert_min': None,
        'held_min': None,
        'entry_z': params.get('entry_z'),
        'max_entry_z': params.get('max_entry_z'),
        'reentry_back': params.get('reentry_back'),
        'reentry_window_pct': params.get('reentry_window_pct'),
        'algo_mode': None,
    }


def enter(signal, at, z, prices, units, legs=None, quantity=None):
    """Freeze the entry onto a signal that has entered.

    The prices are the book's at this moment, not a guess: what did
    not happen is the order, not the quote.
    """
    signal['state'] = OPEN
    signal['entered_at'] = float(at)
    signal['entered_z'] = round(float(z), 3)
    signal['entry'] = (prices or {}).get('entry')
    signal['break_even'] = (prices or {}).get('break_even')
    signal['tp'] = (prices or {}).get('tp')
    signal['sl'] = (prices or {}).get('sl')
    signal['units'] = units
    signal['quantity'] = quantity
    for name, value in (legs or {}).items():
        signal[name] = value
    return signal


def note_block(signal, reason, edge=None):
    """The Algo held this stretch back - in its own words, with the
    edge reading it was judged at.

    The row stays ARMED. A block that clears before the stretch is
    over - a cooldown running out, a stale feed coming back - can still
    be followed by the entry, and then it is the entry that counts.
    Only a stretch that ENDS unentered settles as BLOCKED, with the last
    reason it was held back for.
    """
    signal['block_reason'] = reason
    if edge:
        signal['edge_ratio'] = edge.get('ratio')
        signal['edge_required'] = edge.get('required')
    return signal


def settle_unentered(signal, at, why_not=None):
    """The stretch is over and the Algo never entered it.

    BLOCKED if it was held back - a refusal, never a loss - with the
    reason already on the row. NO_ENTRY if nothing refused it and it
    simply never confirmed: back through the window's floor before the
    Algo's confirmation was met. `why_not` says which, in words.
    """
    signal['state'] = CLOSED
    signal['closed_at'] = float(at)
    if signal.get('block_reason'):
        signal['outcome'] = BLOCKED
    else:
        signal['outcome'] = NO_ENTRY
        signal['exit_reason'] = why_not
    return signal


def net_at(signal, closing_spread):
    """What the trade would be making, in money, closing
    at this spread. None where it cannot be measured."""
    if closing_spread is None or signal.get('break_even') is None \
            or not signal.get('units'):
        return None
    move = float(closing_spread) - float(signal['break_even'])
    if signal['side'] == 'SELL':
        move = -move
    return move * float(signal['units'])


def mark(signal, closing_spread, z, at):
    """Mark an OPEN signal forward: its best and its worst, and whether
    the spread has come back to its mean.

    It decides nothing. When a trade comes off is the Algo's decision
    (`algo.AlgoSignal._judge_exit`, run by the shadow) - this used to
    carry its own stop and target checks, and a copy of a rule is a
    rule that drifts. Returns True when anything changed.
    """
    if signal.get('state') != OPEN:
        return False
    changed = False
    net = net_at(signal, closing_spread)
    if net is not None:
        if signal['peak_net'] is None or net > signal['peak_net']:
            signal['peak_net'] = round(net, 2)
            changed = True
        if signal['trough_net'] is None or net < signal['trough_net']:
            signal['trough_net'] = round(net, 2)
            changed = True
    if z is not None and math.isfinite(z) and not signal.get('reverted') \
            and signal.get('entered_at') is not None:
        # "It came back": the stretch crossed the mean. A separate
        # question from whether it paid, so it is recorded beside the
        # outcome rather than as one.
        if (signal['side'] == 'SELL' and z <= 0) or \
                (signal['side'] == 'BUY' and z >= 0):
            signal['reverted'] = True
            signal['revert_min'] = round(
                max(0.0, at - signal['entered_at']) / 60.0, 1)
            changed = True
    return changed


def close(signal, at, reason, closing_spread, z):
    """The Algo's own exit, as it decided it: which rule, at what
    spread, and what that is worth. TP and SL say a level was REACHED;
    any other of the Algo's exits settles as EXIT, named."""
    signal['state'] = CLOSED
    signal['outcome'] = EXIT_OUTCOME.get(reason, EXIT)
    signal['exit_reason'] = EXIT_WORDS.get(reason, reason)
    signal['closed_at'] = float(at)
    signal['closed_z'] = None if z is None else round(float(z), 3)
    if closing_spread is not None:
        signal['exit_spread'] = float(closing_spread)
    if signal.get('entered_at') is not None:
        signal['held_min'] = round(
            max(0.0, at - signal['entered_at']) / 60.0, 1)
    net = net_at(signal, closing_spread)
    signal['net_pnl'] = None if net is None else round(float(net), 2)
    return signal


def from_replay(trade, pair_key, params, broker_day=None, units=None):
    """One trade from `backtest.run` as a signal row.

    The backtest replays the SAME `algo.AlgoSignal` the live Algo runs,
    so its arming, its filters and its exits are not a second model of
    the Algo - they are the Algo, fed candles instead of ticks. What
    differs is resolution, and that is why the row is stamped REPLAY:
    one look per candle cannot see a stop touched and recovered inside
    one.
    """
    params = dict(params or {})
    opened = trade.get('opened_at')
    closed = trade.get('closed_at')
    reason = trade.get('reason')
    z_in = trade.get('entry_z')
    row = new_signal(pair_key, ALGO, trade.get('side'), opened,
                     0.0 if z_in is None else z_in, params,
                     broker_day=broker_day,
                     threshold=params.get('entry_z'))
    row['source'] = REPLAY
    row['decided_by'] = DECIDED_BY
    edge = trade.get('edge') or {}
    row['edge_ratio'] = edge.get('ratio')
    row['edge_required'] = edge.get('required')
    row['exit_reason'] = EXIT_WORDS.get(reason, reason)
    row['entered_at'] = opened
    row['entered_z'] = None if z_in is None else round(float(z_in), 3)
    row['entry'] = trade.get('entry')
    row['break_even'] = trade.get('break_even')
    row['tp'] = trade.get('tp')
    row['sl'] = trade.get('sl')
    row['units'] = units
    row['quantity'] = params.get('algo_qty')
    if closed is None:
        # Still open when the replay ran out of candles. Not a result.
        row['state'] = OPEN
        return row
    row['state'] = CLOSED
    # The Algo's own exit: TP and SL say a level was reached, any other
    # of its rules is EXIT, named.
    row['outcome'] = EXIT_OUTCOME.get(reason, EXIT)
    row['closed_at'] = closed
    row['exit_spread'] = trade.get('exit')
    row['held_min'] = round(max(0.0, closed - opened) / 60.0, 1)
    pnl = trade.get('pnl')
    row['net_pnl'] = None if pnl is None else round(float(pnl), 2)
    return row


def tally(rows):
    """The three numbers the page exists to answer, and their workings.

    How often did it signal, how many of those would have made money,
    and how many would have lost. Rates are over SETTLED signals only
    — a day with three still running is not a day with three losses,
    and dividing by them would read as one.
    """
    mine = list(rows or ())
    entered = [r for r in mine if r.get('entered_at') is not None]
    settled = [r for r in entered if r.get('outcome') in SETTLED]
    blocked = [r for r in mine if r.get('outcome') == BLOCKED]
    # WON and LOST are measured in money, not by which level was
    # touched. An exit on the mean reversion in profit is a win however
    # it got out, and a page that called it "neither" would under-report
    # exactly the trades the Algo's own exits are there to take. A row
    # whose P&L was never measured is in neither count.
    priced = [r for r in settled if r.get('net_pnl') is not None]
    wins = [r for r in priced if r['net_pnl'] > 0]
    losses = [r for r in priced if r['net_pnl'] < 0]
    reverted = [r for r in settled if r.get('reverted')]
    nets = [r['peak_net'] for r in settled if r.get('peak_net') is not None]
    return {
        'armed': len(mine),
        'entered': len(entered),
        'no_entry': sum(1 for r in mine if r.get('outcome') == NO_ENTRY),
        'blocked': len(blocked),
        'open': sum(1 for r in mine if r.get('state') == OPEN),
        'settled': len(settled),
        'priced': len(priced),
        'tp': len(wins), 'sl': len(losses),
        'net_pnl': (round(sum(r['net_pnl'] for r in priced), 2)
                    if priced else None),
        'live': sum(1 for r in mine if r.get('source') != REPLAY),
        'replayed': sum(1 for r in mine if r.get('source') == REPLAY),
        'expired': sum(1 for r in settled if r.get('outcome') == EXPIRED),
        'reverted': len(reverted),
        # None, not 0, where nothing has settled: a win rate of 0% on no
        # trades is a statement nobody measured.
        'win_rate': (round(100.0 * len(wins) / len(priced), 1)
                     if priced else None),
        'revert_rate': (round(100.0 * len(reverted) / len(settled), 1)
                        if settled else None),
        'best_peak': round(max(nets), 2) if nets else None,
        # HOW OFTEN THE Z WAS TRIGGERED, which is the count the page is
        # for. It is the ARMED count, split by side - a stretch is one
        # trigger however many times it pokes the band while it is
        # still out there, because there is nothing new to act on until
        # it has come back through the floor. Counting the pokes
        # instead inflates every number on the page and makes a quiet
        # day look busy.
        **_z_triggers(mine),
    }


def _z_triggers(rows):
    """The z count, by side, with the threshold it was counted against
    and the furthest either way."""
    up = [r for r in rows if (r.get('armed_z') or 0.0) > 0]
    down = [r for r in rows if (r.get('armed_z') or 0.0) < 0]
    zs = [float(r['armed_z']) for r in rows if r.get('armed_z') is not None]
    # The threshold these were counted against. Only quoted when every
    # row agrees: two ladders with different entry_z on one page have
    # no single threshold, and printing either would be wrong.
    thresholds = {r.get('entry_z') for r in rows
                  if r.get('entry_z') is not None}
    return {
        'triggered': len(rows),
        'triggered_up': len(up),
        'triggered_down': len(down),
        'threshold_z': (thresholds.pop() if len(thresholds) == 1 else None),
        # Furthest the spread actually got, either way. None, not 0,
        # where nothing armed.
        'peak_z': round(max(zs), 2) if zs else None,
        'trough_z': round(min(zs), 2) if zs else None,
    }


def why(row):
    """The one thing about a signal that is NOT a number.

    Everything else it has to say - the levels, the z, the prices, the
    size - is a column of its own. What is left is the sentence: which
    filter refused it, which window it never came back inside, or
    whether the spread came back to its mean. Written as a column too,
    so the journal stays one table.
    """
    row = row or {}
    outcome = row.get('outcome')
    if outcome == BLOCKED:
        parts = []
        if row.get('block_reason'):
            parts.append(str(row['block_reason']))
        ratio, needed = row.get('edge_ratio'), row.get('edge_required')
        if ratio is not None:
            said = f'edge {float(ratio):.2f}x'
            if needed is not None:
                said += f' of {float(needed):.2f}x'
            parts.append(said)
        return ' \u00b7 '.join(parts) or None
    if outcome == NO_ENTRY:
        # The Algo's own reason it never confirmed, written when the
        # stretch ended. A row from before that was kept has none.
        if row.get('exit_reason'):
            return str(row['exit_reason'])
        said = 'never back inside the window'
        if row.get('entry_z') is not None:
            said += f' (enters {float(row["entry_z"]):.2f}\u03c3'
            if row.get('reentry_back') is not None:
                said += f', back {float(row["reentry_back"]):.2f}\u03c3'
            said += ')'
        return said
    if row.get('entered_at') is None:
        # Still armed, and being held back right now: say by what, so a
        # stretch the edge filter is refusing does not read as waiting.
        if row.get('state') == ARMED and row.get('block_reason'):
            return 'held back: ' + str(row['block_reason'])
        return None
    if row.get('reverted'):
        minutes = row.get('revert_min')
        return ('came back to the mean' if minutes is None else
                f'came back to the mean after {float(minutes):.1f} min')
    return 'never came back to the mean'


def journal(rows):
    """Every signal, newest first, with a running total.

    The same shape as the trade journal and for the same reason: a
    column of outcomes says how it went, and a running total says what
    it was WORTH - which is the question the page exists to answer.
    The total runs over the signals that ENTERED and were priced; a
    refusal and a stretch that never came back carry no money and must
    not move it.
    """
    ordered = sorted(rows or (),
                     key=lambda r: (r.get('closed_at')
                                    or r.get('entered_at')
                                    or r.get('armed_at') or 0.0))
    # None, not 0.0, until something has actually been priced.
    # UNMEASURED IS NOT ZERO: a page of refusals and stretches that
    # never entered has no running total, and printing $0.00 down the
    # column reads as 'it broke even', which is a measurement nobody
    # made. Once one row carries money the total is real, and a later
    # unpriced row leaves it where it was.
    running = None
    out = []
    for row in ordered:
        pnl = row.get('net_pnl')
        if pnl is not None:
            running = float(pnl) + (running or 0.0)
        out.append(dict(row, why=why(row),
                        cum_pnl=None if running is None
                        else round(running, 2)))
    out.reverse()
    return out
