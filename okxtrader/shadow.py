"""The Algo, run on the live market with nothing to send to.

The signal journal used to carry its OWN copy of the Algo's rules - an
arm-then-enter machine written to "mirror" `algo.AlgoSignal`, and a
gate check written to "mirror" the filters. A mirror drifts, and every
place it drifted was a trade on the page the Algo would never take. On
a live desk, in one afternoon, the real Algo produced four signals, all
held back by its edge filter; the journal showed dozens, both ways, and
called most of them winners. The copy:

- read ONE z off the mid for both sides, where the Algo reads each
  side's own executable price - on a spread whose bid-ask was 2.4 sigma
  wide that put the copy's z 1.2 sigma ahead of the Algo's either way;
- skipped the edge filter whenever the Algo was ON;
- opened a new trade on every signal, where the Algo holds one;
- traded on a stale feed, which the Algo refuses;
- had no day's limit, no cooldown, no warm-up and no confirmation.

So there is no copy any more. This runs `algo.AlgoSignal` itself - the
class the live desk runs - fed the same market, the same gates and the
same filters (`algodesk.judge_filters`, which the backtest also uses),
and keeps its own day, warm-up and positions the way the desk keeps
them. What it decides is what the Algo would decide, because it IS the
Algo. Its positions are the journal's open rows; its intents are only
ever written down.

Nothing here can reach an order: it imports the Algo, the filters and
nothing else, and a test reads it as code to keep it that way.
"""

from . import algo as algo_module
from . import algofilters
from .algodesk import WARMUP_CARRY_SEC, WARMUP_GAP_SEC, judge_filters


class ShadowAlgo:
    """One ladder's Algo, deciding, with no executor behind it.

    `step` is `AlgoDesk.observe` without the sending: the day settled,
    the day's limits judged, the live warm-up counted, the filters
    priced at the z the Algo would actually be judged at - and then
    the Algo's own `evaluate`.
    """

    #: Saved state from before this class existed belonged to the copy
    #: it replaces, and is not read.
    VERSION = 2

    def __init__(self, params, state=None, now=None):
        self.params = algo_module.clean_params(params)
        self.signal = algo_module.AlgoSignal(self.params)
        self.day = {'date': None, 'trades': 0, 'losses_row': 0, 'pnl': 0.0}
        self.live_sec = 0.0
        self.live_at = None
        state = dict(state or {})
        if state.get('v') != self.VERSION:
            return
        armed = state.get('armed') or {}
        self.signal._armed = {'BUY': bool(armed.get('BUY')),
                              'SELL': bool(armed.get('SELL'))}
        self.signal._cooldown_until = state.get('cooldown_until')
        self.day = dict(self.day, **(state.get('day') or {}))
        # The warm-up carries over a restart the way the desk's does:
        # only when the feed was watched recently. The gap itself is
        # never counted as watched.
        saved_at = state.get('live_saved_at')
        if now is not None and saved_at is not None and \
                0 <= now - float(saved_at) <= WARMUP_CARRY_SEC:
            self.live_sec = float(state.get('live_sec') or 0.0)

    def to_dict(self, now=None):
        return {'v': self.VERSION,
                'armed': dict(self.signal._armed),
                'cooldown_until': self.signal._cooldown_until,
                'day': dict(self.day),
                'live_sec': self.live_sec,
                'live_saved_at': now}

    @property
    def armed(self):
        return dict(self.signal._armed)

    def retune(self, params):
        """New settings are the same Algo read differently, as live."""
        cleaned = algo_module.clean_params(params)
        if cleaned != self.params:
            self.params = cleaned
            self.signal.params = cleaned

    def scored(self, pnl):
        """A position of the shadow's has closed: count it, as the desk
        counts a real one. Unmeasured is not a loss."""
        if pnl is None:
            return
        self.day['pnl'] = float(self.day.get('pnl') or 0.0) + float(pnl)
        self.day['losses_row'] = (self.day['losses_row'] + 1 if pnl < 0
                                  else 0)

    def _halt(self, positions):
        """`AlgoDesk._halt`, on the shadow's own day."""
        p, day = self.params, self.day
        if p['max_trades_day'] and day['trades'] >= p['max_trades_day']:
            return (f"{day['trades']} Algo trades today — the day's limit "
                    f"is {p['max_trades_day']}")
        if p['max_losses_row'] and day['losses_row'] >= p['max_losses_row']:
            return (f"{day['losses_row']} losing Algo trades in a row — "
                    f"paused for the day")
        if p['daily_loss_limit']:
            total = float(day.get('pnl') or 0.0) + sum(
                (pos.get('net_pnl') or 0.0) for pos in positions or ())
            if total <= -abs(p['daily_loss_limit']):
                return (f"Algo P&L today {total:,.2f} — past the "
                        f"-{abs(p['daily_loss_limit']):,.2f} limit")
        return None

    def _warmup(self, now, md, gates):
        """`AlgoDesk.observe`'s warm-up count: live, unguarded prices,
        with a gap longer than WARMUP_GAP_SEC not counted."""
        watched = bool(md and md.get('mid_spread') is not None
                       and not md.get('jump_reason')
                       and not gates.get('health'))
        if watched:
            if self.live_at is not None:
                self.live_sec += max(0.0, min(now - self.live_at,
                                              WARMUP_GAP_SEC))
            self.live_at = now
        else:
            self.live_at = None
        need = float(self.params['warmup_min']) * 60.0
        return {'sec': min(self.live_sec, need) if need else 0.0,
                'need_sec': need, 'done': self.live_sec >= need}

    def step(self, now, md, stats, closes, gates, cost_in, positions=()):
        """One poll. Returns the Algo's body: its z for each side, what
        it is armed on, what it held back and why, and the intents that
        became true on this call - each ENTER carrying the edge it was
        judged at."""
        p = self.params
        gates = dict(gates or {})
        day = gates.get('day')
        if day is not None and day != self.day['date']:
            self.day = {'date': day, 'trades': 0, 'losses_row': 0,
                        'pnl': 0.0}
        halt = self._halt(positions)
        if halt:
            gates['halt'] = halt
        # The live Algo's own warm-up when it is running, so the two can
        # never disagree about whether the feed has been watched; the
        # shadow's count when it is not.
        if not gates.get('warmup'):
            gates['warmup'] = self._warmup(now, md, gates)
        filters, check = judge_filters(p, md, stats, closes, cost_in)
        filters['ready'] = bool(filters['ready'] and
                                gates['warmup'].get('done'))
        gates['entry_check'] = check
        body = self.signal.evaluate(now, md, stats, positions, gates)
        body['filters'] = filters
        body['halt'] = halt

        sigma = stats.get('sigma') if stats.get('ready') else None
        cost = (filters.get('cost') or {}).get('total')

        def edge_at(z):
            if z is None or sigma is None:
                return None
            return dict(algofilters.edge(
                z, sigma, filters.get('k'), p['algo_qty'], cost,
                p['edge_capture_frac'], p['edge_multiple']),
                on=p['edge_on'])

        # The edge filter's reading on each side NOW, switched on or not:
        # what a signal recorded at this moment is stamped with.
        body['edge_by_side'] = {'SELL': edge_at(body.get('z_sell')),
                                'BUY': edge_at(body.get('z_buy'))}
        if body.get('blocked_side'):
            body['blocked_edge'] = edge_at(body.get('blocked_z'))
        for intent in body['intents']:
            if intent['action'] == 'ENTER':
                # A signals run counts its entries toward the day's limit
                # (`AlgoDesk.observe`), and so does this.
                self.day['trades'] += 1
                intent['edge'] = edge_at(intent.get('z'))
        return body
