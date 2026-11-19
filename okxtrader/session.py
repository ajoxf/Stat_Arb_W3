"""The session cutoff: what happens to orders and positions at 16:55 —
on the BROKER's clock, not this machine's.

MT5 stamps everything with the server's wall clock, and the trading day
that a cutoff belongs to is the broker's day. A box in London and a
broker on GMT+3 differ by hours, and a cutoff read off the local clock
fires hours early or late — which on EXIT_ALWAYS means flattening a
position in the middle of the session, or not at all.

The offset is MEASURED from the terminal (`leg.server_offset()`), never
configured: a typed-in time zone is a value that goes stale at every
daylight-saving change. Unknown is not zero — with no measurement the
clock says so, and the UI shows which clock it is running on.


One time, configured once, governing two different things:

- **Working orders** are governed by DAY / GTC (spec §3.1). A DAY order
  is cancelled at the cutoff; a GTC order lives until the trader
  cancels it — or until this system stops, which is the honest caveat
  the UI carries beside the selector.
- **Positions** are governed by ALLOW / EXIT_IF_PROFIT / EXIT_ALWAYS
  (spec §3.2), per ladder, defaulting to ALLOW. This is a CARRY
  decision, not a risk rule: holding a rich basis over the swap is
  often the whole trade.

`EXIT_IF_PROFIT` reads NET P&L — marked at the CLOSING touch, less
commission only. Marked at the mid it would flatten trades that are not
actually in profit.

An overnight close is urgent: market, by ticket, never resting. And it
reads no price level, so the staleness and jump guards do not withhold
it — a guard may withhold an order; it must never prevent a close.
"""

from datetime import datetime, timedelta

from .models import OvernightMode, TimeInForce


def parse_hhmm(value):
    """'HH:MM' (broker time) as (hour, minute); None for blank.

    Raises ValueError for anything else, so a save can say what was
    wrong instead of keeping a time nobody meant.
    """
    if value is None or str(value).strip() == '':
        return None
    text = str(value).strip()
    parts = text.split(':')
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(f'{text!r} is not a time - use HH:MM, broker time')
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'{text!r} is not a time - use HH:MM, broker time')
    return hour, minute


def clean_hhmm(value):
    """A stored time, tidied to 'HH:MM', or None - an unreadable one is
    None too: a ladder with a broken time in its file still loads, on
    the desk-wide hours."""
    try:
        parsed = parse_hhmm(value)
    except ValueError:
        return None
    return None if parsed is None else f'{parsed[0]:02d}:{parsed[1]:02d}'


def _minutes(hm):
    return hm[0] * 60 + hm[1]


def _within(t, start, end):
    """Is minute-of-day `t` inside [start, end)? A window whose end is
    before its start runs over midnight."""
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


class PairSession:
    """ONE ladder's trading hours, on the broker's clock.

    - **close**: this ladder's session cutoff - DAY orders cancelled,
      the overnight rule, the Algo's last entry. Blank is the desk-wide
      OVERNIGHT_CLOSE time.
    - **open**: when its session starts. Blank is "any time before the
      close". An open after the close is a session over midnight.
    - **break**: a daily pause (a futures maintenance break). Both ends
      or neither.

    Outside the session or in the break the Algo takes no new entry.
    Exits are never held by any of it: a guard must never prevent a
    close.
    """

    def __init__(self, pair, config):
        desk = (int(config.get('OVERNIGHT_CLOSE_HOUR', 16)),
                int(config.get('OVERNIGHT_CLOSE_MINUTE', 55)))
        self.open = parse_hhmm(clean_hhmm(getattr(pair, 'session_open',
                                                  None)))
        own_close = parse_hhmm(clean_hhmm(getattr(pair, 'session_close',
                                                  None)))
        self.close = own_close or desk
        self.own_hours = bool(self.open or own_close)
        start = parse_hhmm(clean_hhmm(getattr(pair, 'break_start', None)))
        end = parse_hhmm(clean_hhmm(getattr(pair, 'break_end', None)))
        self.brk = (start, end) if start and end else None

    @property
    def cutoff(self):
        return self.close

    def state(self, now):
        """OPEN, BREAK or CLOSED at broker time `now` (None: unknown)."""
        if now is None:
            return None
        t = now.hour * 60 + now.minute
        if self.brk and _within(t, _minutes(self.brk[0]),
                                _minutes(self.brk[1])):
            return 'BREAK'
        if self.open is not None:
            return 'OPEN' if _within(t, _minutes(self.open),
                                     _minutes(self.close)) else 'CLOSED'
        return 'OPEN' if t < _minutes(self.close) else 'CLOSED'

    def quiet(self, now):
        """Is a silent feed EXPECTED now? Only where the trader set this
        ladder's hours - the desk-wide cutoff alone says nothing about
        whether the market still trades after it."""
        state = self.state(now)
        return state == 'BREAK' or (state == 'CLOSED' and self.own_hours)

    def minutes_to_close(self, now):
        """Minutes to the close. With no open set this is TODAY's, and
        negative past it - the Algo's cutoff buffer then holds entries
        until the broker's midnight, as it always has. With an open set,
        it is the NEXT close: the session itself says when it is shut."""
        if now is None:
            return None
        t = now.hour * 60 + now.minute + now.second / 60.0
        left = _minutes(self.close) - t
        if self.open is None or left > 0:
            return left
        return left + 1440.0

    def entry_block(self, now):
        """Why the Algo may not ENTER now, in words, or None."""
        state = self.state(now)
        if state == 'BREAK':
            return (f'session: daily break until '
                    f'{self.brk[1][0]:02d}:{self.brk[1][1]:02d} broker time')
        if state == 'CLOSED' and self.open is not None:
            return (f'session: closed - opens {self.open[0]:02d}:'
                    f'{self.open[1]:02d} broker time')
        # No open set: past the close is the cutoff buffer's to hold,
        # as it was before ladders had hours of their own.
        return None

    def describe(self, now):
        def text(hm):
            return None if hm is None else f'{hm[0]:02d}:{hm[1]:02d}'
        return {'open': text(self.open), 'close': text(self.close),
                'break': None if not self.brk else
                f'{text(self.brk[0])}-{text(self.brk[1])}',
                'own_hours': self.own_hours, 'state': self.state(now),
                'quiet': self.quiet(now) if now is not None else False}


def past_cutoff(now, close_hour, close_minute):
    """Is `now` at or past today's session cutoff?

    `now` is the BROKER-session local time; the caller converts, so this
    stays a pure comparison with nothing to get wrong about clocks.
    """
    cutoff = now.replace(hour=int(close_hour), minute=int(close_minute),
                         second=0, microsecond=0)
    return now >= cutoff


def overnight_action(mode, net_pnl, now, close_hour, close_minute):
    """'OVERNIGHT_CLOSE' or None, for one position at one moment."""
    mode = OvernightMode(getattr(mode, 'value', mode) or 'ALLOW')
    if mode is OvernightMode.ALLOW:
        return None
    if not past_cutoff(now, close_hour, close_minute):
        return None
    if mode is OvernightMode.EXIT_ALWAYS:
        return 'OVERNIGHT_CLOSE'
    # EXIT_IF_PROFIT: unmeasured P&L is NOT a profit. A position whose
    # mark could not be taken is left alone rather than flattened on a
    # number nobody has.
    if net_pnl is not None and net_pnl > 0:
        return 'OVERNIGHT_CLOSE'
    return None


class SessionClock:
    """The BROKER's clock, and the cutoff fired once a day per pair.

    Once, because a rule that re-fires every poll after 16:55 would
    cancel a working order the trader deliberately placed at 16:56 — and
    would keep trying to flatten a position whose close failed.

    `offset()` returns the seconds the broker's clock runs ahead of ours,
    or None when it has not been measured. With no measurement the
    cutoff does NOT fire: a session rule on the wrong clock is worse
    than one that waits for the right one, and the UI says which it is
    running on.
    """

    def __init__(self, config, now=datetime.now, offset=None):
        self.config = config
        self.now = now
        #: A callable returning the measured offset in seconds, or None.
        self.offset = offset or (lambda: 0)
        self._fired = {}                # pair key -> date it last fired

    def broker_now(self):
        """What time it is where the broker is, or None if unknown."""
        offset = self.offset()
        if offset is None:
            return None
        return self.now() + timedelta(seconds=offset)

    def describe(self):
        """Which clock the cutoff is running on, for the screen."""
        offset = self.offset()
        broker = self.broker_now()
        cutoff = (f"{int(self.config.get('OVERNIGHT_CLOSE_HOUR', 16)):02d}:"
                  f"{int(self.config.get('OVERNIGHT_CLOSE_MINUTE', 55)):02d}")
        if offset is None:
            return {'broker_time': None, 'offset_sec': None, 'cutoff': cutoff,
                    'note': ('the broker clock has not been measured yet — '
                             'the session cutoff will not fire until it is')}
        hours = offset / 3600.0
        return {'broker_time': broker.strftime('%H:%M:%S'),
                'offset_sec': offset,
                'cutoff': cutoff,
                'note': (f'broker time, {hours:+.1f}h from this machine — '
                         f'the {cutoff} cutoff is on the broker\'s clock')}

    def due(self, pair_key, close=None):
        """Has this pair's cutoff come today? `close` is its own (hour,
        minute); None is the desk-wide time."""
        now = self.broker_now()
        if now is None:
            # Unmeasured is not zero: without the broker's clock we do
            # not know whether its day has reached the cutoff.
            return False
        hour, minute = close or (self.config.get('OVERNIGHT_CLOSE_HOUR', 16),
                                 self.config.get('OVERNIGHT_CLOSE_MINUTE', 55))
        if not past_cutoff(now, hour, minute):
            return False
        # The broker's DATE too: a cutoff either side of midnight
        # belongs to the broker's trading day, not to ours.
        return self._fired.get(pair_key) != now.date()

    def mark(self, pair_key):
        now = self.broker_now()
        if now is not None:
            self._fired[pair_key] = now.date()


def day_orders(orders):
    """The working orders the cutoff cancels — DAY only."""
    return [o for o in orders if o.time_in_force is TimeInForce.DAY]


def gtc_caveat():
    """What GTC actually means here. On the screen, not only in the code.

    Nothing at the broker knows what a spread is, so a synthetic order
    that "survived" a restart would be a promise nothing could keep.
    """
    return ('GTC: until cancelled, or until this system stops — a '
            'synthetic order lives in this process, and nothing watches '
            'the spread while it is down')
