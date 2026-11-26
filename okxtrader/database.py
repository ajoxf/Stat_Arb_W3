"""The local database: crash-safe state, and the trade journal.

Two jobs, and they are different in kind:

- **State.** Open positions live here as well as in memory, so a restart
  recovers what is actually on at the broker. Without it the book comes
  back empty, the reconciler reads every real position as an orphan,
  and sixty seconds later it closes them. That is the machinery working
  correctly on a book that lied to it.
- **The journal.** Every FILL the broker reports, ours and the trader's
  own terminal clicks alike, keyed by MT5's deal id. It is the record
  that survives us: an audit trail we did not write from our own
  intentions, but read back from what actually happened.

Mechanics that were paid for in the system this is ported from:

- **WAL and a 30-second busy timeout, through ONE `_connect()`.** The
  web process reads this file while the coordinator writes it, and the
  default raises `database is locked` immediately — which once stopped
  an exit loop for 30 seconds with a live position open.
- **A deal is written once**, by `INSERT OR REPLACE` on the deal id.
  Deal history is re-read on every pass and the same fill arrives many
  times; a journal that grows a row per read is not a journal.
- **Broker time is stored beside our own.** MT5 stamps deals with the
  SERVER's wall clock encoded as an epoch, and reading that as an
  ordinary timestamp puts every row hours away from the same trade in
  MT5's own History — which is enough to make the two tables look like
  different accounts.
"""

import json
import logging
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    position_id     TEXT PRIMARY KEY,
    pair_key        TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        REAL NOT NULL,
    entry_spread    REAL,
    exit_spread     REAL,
    spread_units    REAL,
    order_type      TEXT,
    opened_at       REAL,
    closed_at       REAL,
    close_reason    TEXT,
    realized_pnl    REAL,
    entry_slippage  REAL,
    exit_slippage   REAL,
    click_to_on_ms  REAL,
    naked_ms        REAL,       -- one leg alone, on the BROKER's clock
    leg_a           TEXT,       -- JSON: account, symbol, side, volume,
    leg_b           TEXT        --       price, tickets, contract size
);

CREATE INDEX IF NOT EXISTS positions_open
    ON positions (closed_at, pair_key);

-- The journal. One row per BROKER deal, keyed by the deal id AND the
-- account: deal ids are unique per broker, not across them, so two
-- accounts can each hold a deal #500001. Keyed on the id alone, one
-- would silently overwrite the other and the journal would be missing
-- a leg of every trade.
CREATE TABLE IF NOT EXISTS fills (
    deal_id         TEXT NOT NULL,
    account         TEXT NOT NULL,
    order_id        TEXT,
    position_ticket TEXT,
    symbol          TEXT,
    side            TEXT,        -- buy / sell
    entry           TEXT,        -- open / close
    order_type      TEXT,        -- market / buy limit / ...
    volume          REAL,
    price           REAL,
    commission      REAL,
    swap            REAL,
    profit          REAL,
    fee             REAL,
    broker_time_ms  INTEGER,     -- the SERVER's clock, as MT5 stamps it
    server_offset_s INTEGER,     -- ...and how far that is from UTC
    seen_at         REAL,        -- our own clock, when we first read it
    magic           INTEGER,
    is_ours         INTEGER,     -- our magic, or the trader's own click
    comment         TEXT,
    pair_key        TEXT,        -- resolved from account+symbol
    leg             TEXT,        -- 'A' or 'B' on that pair
    PRIMARY KEY (account, deal_id)
);

CREATE INDEX IF NOT EXISTS fills_time ON fills (broker_time_ms);

-- OKX'S OWN ACCOUNT LEDGER: every change to the trading account's
-- balance - each fill's P&L and fee, funding, transfers - with the
-- balance AFTER it. The one record that tallies with the account to the
-- cent: a trade's fees are its bills' fees, and the account's running
-- balance is the last bill's `bal`.
CREATE TABLE IF NOT EXISTS account_bills (
    account         TEXT NOT NULL,
    bill_id         TEXT NOT NULL,
    ts_ms           INTEGER,
    type            TEXT,
    sub_type        TEXT,
    inst_id         TEXT,
    ccy             TEXT,
    bal_chg         REAL,
    bal             REAL,
    pnl             REAL,
    fee             REAL,
    sz              REAL,
    px              REAL,
    trade_id        TEXT,
    ord_id          TEXT,
    PRIMARY KEY (account, bill_id)
);
CREATE INDEX IF NOT EXISTS account_bills_time ON account_bills (ts_ms);
CREATE INDEX IF NOT EXISTS fills_pair ON fills (pair_key, broker_time_ms);

-- EVERY POSITION TICKET THIS SYSTEM HAS EVER HELD, and the one table
-- that answers "did we place this?" without asking the broker.
--
-- A trader watched both legs of a live spread close sixty seconds
-- after they put it on, because a position that had fallen out of the
-- book looked exactly like a stray position to the reconciler. The
-- first fix read the order COMMENT back off the broker - true, but it
-- is the broker's copy of our words, and comments are truncated,
-- dropped or reformatted differently by every liquidity provider. A
-- desk that changes LP must not silently lose a money guard.
--
-- This is OUR record, written the moment the book holds a ticket, kept
-- after the book stops holding it, and unchanged by any LP. Rows are
-- never deleted: the whole value is remembering a ticket the book has
-- forgotten.
CREATE TABLE IF NOT EXISTS our_tickets (
    account         TEXT NOT NULL,
    ticket          TEXT NOT NULL,
    first_seen      REAL NOT NULL,
    pair_key        TEXT,
    PRIMARY KEY (account, ticket)
);

-- Everything else worth being able to answer "what happened at 14:32?"
-- with: refusals, sweeps, reconciler decisions, session cutoffs.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    kind            TEXT NOT NULL,
    pair_key        TEXT,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS events_at ON events (at);

-- The Algo's CLOSED spread candles, so a restart does not start the
-- band from nothing. Keyed by the beta they were built with: a spread
-- of B - 0.98 x A is a different series from B - 1.00 x A, and mixing
-- the two draws a band around neither.
-- The Algo's live warm-up, per ladder: seconds of live prices watched,
-- and when the last one counted. Read back when the Algo is switched on
-- again soon after a restart, so an update does not cost 90 minutes.
CREATE TABLE IF NOT EXISTS algo_warmup (
    pair_key        TEXT PRIMARY KEY,
    live_sec        REAL NOT NULL,
    at              REAL NOT NULL
);

-- The Analysis tab's what-if-held shadows: a closed position marked on
-- its own entry for an hour more. One JSON row per watch, so a restart
-- picks up the ones still running.
CREATE TABLE IF NOT EXISTS shadow_watches (
    position_id     TEXT PRIMARY KEY,
    pair_key        TEXT,
    armed_at        REAL NOT NULL,
    done            INTEGER NOT NULL DEFAULT 0,
    data            TEXT NOT NULL
);

-- The Analysis tab's z-score excursion counters, per ladder, since the
-- last reset.
CREATE TABLE IF NOT EXISTS z_excursions (
    pair_key        TEXT PRIMARY KEY,
    at              REAL NOT NULL,
    data            TEXT NOT NULL
);

-- The Analysis tab's SIGMA ZONE track: one row per zone CHANGE, kept
-- for good. The `z_excursions` blob above is a set of live counters
-- that is overwritten on every save and holds 200 events at most, so
-- it can answer "how many since the reset?" and nothing about last
-- week. This is the history: append-only, indexed by time, and every
-- number on a row frozen as it was judged AT THE EVENT so editing a
-- ladder tomorrow cannot rewrite what yesterday said.
CREATE TABLE IF NOT EXISTS z_zone_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_key        TEXT NOT NULL,
    at              REAL NOT NULL,      -- UTC seconds
    timeframe_sec   REAL,
    zone            TEXT NOT NULL,      -- MEAN, +1, +2, +3, -1, -2, -3
    prev_zone       TEXT,
    direction       TEXT,               -- OUT, IN, CROSS
    z               REAL,
    mean            REAL,
    sigma           REAL,
    side            TEXT,               -- the side the Algo would take
    entry           REAL,               -- hypothetical, frozen
    break_even      REAL,
    tp              REAL,
    sl              REAL,
    edge_ratio      REAL,
    edge_capture    REAL,
    edge_cost       REAL,
    edge_required   REAL,
    edge_ok         INTEGER,            -- NULL = could not be priced
    status          TEXT,
    algo_mode       TEXT,               -- OFF, DRY_RUN or LIVE at the time
    entry_z         REAL,               -- the settings in force, stamped on
    max_entry_z     REAL,
    stop_z          REAL,
    length          INTEGER,
    increment       REAL
);

-- The date-range dropdown filters on this, so it is an index and not a
-- scan of every row ever written.
CREATE INDEX IF NOT EXISTS z_zone_events_at
    ON z_zone_events (pair_key, at DESC);

-- The last zone RECORDED per ladder and timeframe, so a restart does
-- not record the zone the spread is already sitting in. Losing this is
-- what made a parked spread log a fresh touch per restart.
CREATE TABLE IF NOT EXISTS z_zone_state (
    pair_key        TEXT NOT NULL,
    timeframe_sec   REAL NOT NULL,
    zone            TEXT,
    at              REAL NOT NULL,
    PRIMARY KEY (pair_key, timeframe_sec)
);

-- SIGNALS: every stretch that armed, whether it entered, and what
-- became of it if it did. Append-only beside `z_zone_events`, which
-- stays the raw record of what the spread did — a signal row is the
-- reading built ON that path, and the path is what makes it auditable.
--
-- Three bases live in one table and disagree on purpose: SIGMA2 and
-- SIGMA3 are a fixed statistical study, ALGO is the ladder's own
-- entry_z with its re-entry machine. See `signals.py`.
CREATE TABLE IF NOT EXISTS signal_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_key        TEXT NOT NULL,
    basis           TEXT NOT NULL,      -- SIGMA2, SIGMA3 or ALGO
    side            TEXT,               -- the side it would be traded
    broker_day      TEXT,               -- YYYY-MM-DD on the BROKER's clock
    threshold       REAL,
    state           TEXT NOT NULL,      -- ARMED, OPEN or CLOSED
    outcome         TEXT,               -- TP, SL, EXPIRED or NO_ENTRY
    armed_at        REAL NOT NULL,
    armed_z         REAL,
    entered_at      REAL,
    entered_z       REAL,
    entry           REAL,               -- the hypothetical, frozen
    break_even      REAL,
    tp              REAL,
    sl              REAL,
    units           REAL,
    closed_at       REAL,
    closed_z        REAL,
    peak_net        REAL,
    trough_net      REAL,
    reverted        INTEGER,            -- did it come back through the mean
    revert_min      REAL,
    held_min        REAL,
    entry_z         REAL,               -- the settings in force, stamped on
    max_entry_z     REAL,
    reentry_back    REAL,
    reentry_window_pct REAL,
    algo_mode       TEXT
);

-- Asking for one day of one ladder is the common read, so it is an
-- index and not a scan.
CREATE INDEX IF NOT EXISTS signal_events_day
    ON signal_events (pair_key, broker_day);
CREATE INDEX IF NOT EXISTS signal_events_at
    ON signal_events (armed_at DESC);
-- The forward watch reopens whatever is still running after a restart.
CREATE INDEX IF NOT EXISTS signal_events_open
    ON signal_events (state);

-- Where each ladder's arming had got to. Held in the database for the
-- same reason the excursion counters' arming now is: arming that lives
-- only in memory re-arms on every restart, and a spread that has not
-- moved then logs a fresh signal on the next poll.
CREATE TABLE IF NOT EXISTS signal_arming (
    pair_key        TEXT PRIMARY KEY,
    at              REAL NOT NULL,
    data            TEXT NOT NULL
);

-- The ANALYSIS band's own candles, kept apart from `algo_candles` on
-- purpose. The analysis band runs whether a ladder's Algo is on or
-- off, so it would otherwise be writing into the table the Algo seeds
-- its band from — and a reading must never be able to change what the
-- Algo decides, however identical the two closes look. It READS
-- `algo_candles` to start warm; it writes only here.
CREATE TABLE IF NOT EXISTS analysis_candles (
    pair_key        TEXT NOT NULL,
    timeframe_sec   REAL NOT NULL,
    beta            REAL NOT NULL,
    bucket          REAL NOT NULL,      -- candle start, UTC seconds
    close           REAL NOT NULL,
    source          TEXT,
    PRIMARY KEY (pair_key, timeframe_sec, beta, bucket)
);

CREATE TABLE IF NOT EXISTS algo_candles (
    pair_key        TEXT NOT NULL,
    timeframe_sec   REAL NOT NULL,
    beta            REAL NOT NULL,
    bucket          REAL NOT NULL,      -- candle start, UTC seconds
    close           REAL NOT NULL,
    source          TEXT,               -- 'mt5' backfill or 'live'
    PRIMARY KEY (pair_key, timeframe_sec, beta, bucket)
);
"""


def _resolve(resolve, account, row):
    """(pair key, leg) for one fill: by its position TICKET where the
    resolver takes one (the coordinator's does), else by symbol."""
    try:
        return resolve(account, row.get('symbol'), row.get('position_id'))
    except TypeError:
        return resolve(account, row.get('symbol'))


class Store:
    """One SQLite file, opened the same way everywhere."""

    def __init__(self, path='okxtrader.db', clock=time.time):
        self.path = path
        self.clock = clock
        self._ensure()

    def _connect(self):
        """The ONE way this database is opened.

        WAL so a reader never blocks the writer, and a 30-second busy
        timeout so the one that does wait, waits — rather than raising
        `database is locked` while a position is open.
        """
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=30000')
        connection.execute('PRAGMA synchronous=NORMAL')
        return connection

    def _ensure(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._add_missing_columns(connection)

    #: Columns added after the first release. `CREATE TABLE IF NOT
    #: EXISTS` does nothing to a table that already exists, so a desk
    #: that has been running since before a column was added would open
    #: its own database and get "no such column" on the first write -
    #: with a live position in hand. Additive only: nothing here drops,
    #: renames or re-types anything, so an older build reading the same
    #: file still works.
    LATER_COLUMNS = (
        # The BROKER's calendar day a zone change fell on, so "5 Nov"
        # on the screen means the same day the Algo counts its trades
        # over. NULL on rows written before it existed; those answer a
        # date filter with nothing rather than with the wrong day.
        ('z_zone_events', 'broker_day', 'TEXT'),
        # Where a signal row came from: LIVE, measured forward as it
        # happened, or REPLAY, derived afterwards from MT5's bars. NULL
        # on rows written before the two could be told apart, which
        # read back as LIVE - which is what they were.
        ('signal_events', 'source', 'TEXT'),
        ('signal_events', 'exit_reason', 'TEXT'),
        # What the trade would have MADE at its close.
        ('signal_events', 'net_pnl', 'REAL'),
        # WHY the Algo refused a stretch, in its own words, and the edge
        # reading behind it.
        #
        # Adding this column also PURGES the signal rows written before
        # it existed, and that is deliberate. Those rows were produced
        # without the Algo's entry gates: on one live desk that put 147
        # trades on the page the Algo would never have taken, every one
        # of them stopped out on its first mark. Keeping known-wrong
        # numbers in a total is worse than an empty page, and the page
        # refills from `Derive 30 days`.
        ('signal_events', 'block_reason', 'TEXT'),
        ('signal_events', 'edge_ratio', 'REAL'),
        ('signal_events', 'edge_required', 'REAL'),
        # The signal journal's own columns: the spread it would have
        # come off at, the size, and the two legs it would have
        # crossed. NULL on every row written before they were kept, and
        # NULL for ever on a DERIVED row - a replay works on the spread
        # series and never saw a leg's book.
        ('signal_events', 'exit_spread', 'REAL'),
        ('signal_events', 'quantity', 'REAL'),
        ('signal_events', 'leg_a_side', 'TEXT'),
        ('signal_events', 'leg_a_entry', 'REAL'),
        ('signal_events', 'leg_b_side', 'TEXT'),
        ('signal_events', 'leg_b_entry', 'REAL'),
        # WHO DECIDED THE ROW: 'algo' - the Algo's own code, live
        # through `shadow.ShadowAlgo` or replayed through `backtest.run`.
        #
        # Adding it PURGES every signal row written before, and the
        # saved arming with them. Those rows came from a COPY of the
        # Algo's rules that did not match the Algo: one z off the mid
        # for both sides, no edge filter while the Algo was on, a new
        # trade on every signal, trading through a stale feed. On a
        # live desk it showed dozens of trades in an afternoon in which
        # the real Algo took none - every one of its four signals was
        # held back by the edge filter. The page refills from the live
        # feed and from `Derive 30 days`.
        ('signal_events', 'decided_by', 'TEXT'),
        # The margin and ATR a shadow trade FROZE at entry, as a live
        # position freezes them - so its levels are repriced every pass
        # by the same arithmetic, and survive a restart.
        ('signal_events', 'entry_margin', 'REAL'),
        ('signal_events', 'entry_atr', 'REAL'),
        # WHICH ACCOUNT, AT WHICH BROKER, each leg was on when the signal
        # was recorded, and whether the edge filter was switched on: a
        # row has to say what it was measured against.
        #
        # Adding it PURGES the signal rows before it, and the saved
        # arming. Those were armed on prices the feed guards had
        # already called untrusted - a leg a moment behind the other,
        # or a side MT5 answered 0.0 for - and showed a page of
        # stretches the Algo was never really in, one of them at z
        # -1006.90. The page refills from the live feed and from
        # `Derive 30 days`.
        ('signal_events', 'leg_a_account', 'TEXT'),
        ('signal_events', 'leg_a_broker', 'TEXT'),
        ('signal_events', 'leg_b_account', 'TEXT'),
        ('signal_events', 'leg_b_broker', 'TEXT'),
        ('signal_events', 'edge_on', 'INTEGER'),
        # The spread price the arming z was read off; NULL on a row
        # written before it was kept.
        ('signal_events', 'armed_spread', 'REAL'),
        ('positions', 'naked_ms', 'REAL'),
        # MANUAL or ALGO. NULL on a row written before it existed, which
        # reads back as MANUAL: nothing traded by itself then either.
        ('positions', 'source', 'TEXT'),
        # The margin per spread frozen when the position was first
        # priced; NULL on older rows, which freeze on their next read.
        ('positions', 'entry_margin', 'REAL'),
        # The trade's best and worst net P&L while open (MFE / MAE),
        # and the minute each was reached. NULL before they were kept.
        ('positions', 'peak_pnl', 'REAL'),
        ('positions', 'peak_min', 'REAL'),
        ('positions', 'trough_pnl', 'REAL'),
        ('positions', 'trough_min', 'REAL'),
        # The ATR frozen when the position was first priced.
        ('positions', 'entry_atr', 'REAL'),
        # JSON: the break-even, take-profit and stop this position was
        # last judged against, and which setting produced each. NULL on
        # every row closed before it was kept, which is why the Analysis
        # tab says a trade's exit "was not recorded" rather than
        # inventing a level for it.
        ('positions', 'exit_levels', 'TEXT'),
        # JSON: which sides the live Algo was ARMED on when its warm-up
        # was last saved - carried over a restart with the warm-up, so an
        # update does not lose a stretch that is coming back. NULL on a
        # row saved before it was kept: nothing armed.
        ('algo_warmup', 'armed', 'TEXT'),
    )

    #: Rows to clear when a column is added for the first time, because
    #: everything written before it was produced by logic now known to
    #: be wrong. Keyed (table, column) -> the table to empty.
    #:
    #: Used ONCE, for `signal_events.block_reason`: the rows written
    #: before it existed were entered without the Algo's own gates, so
    #: they record trades it would never have taken. Nothing else in
    #: this database is ever deleted by an upgrade — positions, fills
    #: and the journal are the record of what really happened, and a
    #: migration must never touch those.
    PURGE_ON_ADD = {('signal_events', 'block_reason'): 'signal_events',
                    ('signal_events', 'decided_by'): ('signal_events',
                                                      'signal_arming'),
                    ('signal_events', 'leg_a_account'): ('signal_events',
                                                         'signal_arming')}

    def _add_missing_columns(self, connection):
        for table, column, kind in self.LATER_COLUMNS:
            have = {row['name'] for row in
                    connection.execute(f'PRAGMA table_info({table})')}
            if not have or column in have:
                continue
            connection.execute(
                f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
            purge = self.PURGE_ON_ADD.get((table, column))
            if purge:
                gone = 0
                for target in ((purge,) if isinstance(purge, str)
                               else purge):
                    count = connection.execute(
                        f'DELETE FROM {target}').rowcount
                    if target == 'signal_events':
                        gone += max(count, 0)
                if gone:
                    logging.warning(
                        '%d signal row(s) cleared: they were recorded by '
                        'rules that did not match the Algo\'s own, so '
                        'they describe trades it would not have taken. '
                        'The page refills from the live feed; press '
                        '"Derive 30 days" to rebuild the history.', gone)

    # -- positions: the state a restart recovers ---------------------------

    def save_position(self, position):
        """Write a position, open or closed. Called on every change.

        Cheap enough to do on every change and far cheaper than the
        alternative, which is a restart that cannot tell a live position
        from an orphan.
        """
        row = position.to_dict()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO positions
                   (position_id, pair_key, side, quantity, entry_spread,
                    exit_spread, spread_units, order_type, opened_at,
                    closed_at, close_reason, realized_pnl, entry_slippage,
                    exit_slippage, click_to_on_ms, naked_ms, leg_a, leg_b,
                    source, entry_margin, peak_pnl, peak_min, trough_pnl,
                    trough_min, entry_atr, exit_levels)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                           ?)""",
                (row['position_id'], row['pair_key'], row['side'],
                 row['quantity'], row['entry_spread'], row['exit_spread'],
                 row['spread_units'], row['order_type'], row['opened_at'],
                 row['closed_at'], row['close_reason'], row['realized_pnl'],
                 row['entry_slippage'], row['exit_slippage'],
                 row['click_to_on_ms'], row.get('naked_ms'),
                 json.dumps(row['leg_a']), json.dumps(row['leg_b']),
                 row.get('source'), row.get('entry_margin'),
                 row.get('peak_pnl'), row.get('peak_min'),
                 row.get('trough_pnl'), row.get('trough_min'),
                 row.get('entry_atr'),
                 json.dumps(row['exit_levels'])
                 if row.get('exit_levels') else None))
        return position

    def remember_tickets(self, rows):
        """Stamp (account, ticket, pair_key) as ours. Idempotent.

        Called on every poll with whatever the book is holding, so a
        ticket is in here from the first snapshot after it fills and
        stays here for good. INSERT OR IGNORE, so the first_seen of a
        ticket is the first time we saw it and not the last.
        """
        rows = [r for r in (rows or []) if r and r[0] and r[1]]
        if not rows:
            return 0
        now = time.time()
        with self._connect() as connection:
            connection.executemany(
                'INSERT OR IGNORE INTO our_tickets '
                '(account, ticket, first_seen, pair_key) VALUES (?,?,?,?)',
                [(str(a), str(t), now, key) for a, t, key in rows])
        return len(rows)

    def ticket_pair(self, account, ticket):
        """The pair a ticket of ours was held on, or None."""
        if not ticket:
            return None
        with self._connect() as connection:
            row = connection.execute(
                'SELECT pair_key FROM our_tickets WHERE account = ? AND '
                'ticket = ?', (str(account), str(ticket))).fetchone()
        return row['pair_key'] if row else None

    def our_tickets(self):
        """Every (account, ticket) this system has ever held."""
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT account, ticket FROM our_tickets').fetchall()
        return {(str(r['account']), str(r['ticket'])) for r in rows}

    def open_positions(self):
        """Every position that was open when we last wrote it.

        A position whose close FAILED is still open, and comes back
        open: removing it from this list is how the money ends up at the
        broker with the screen reading flat.
        """
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM positions WHERE closed_at IS NULL '
                'ORDER BY opened_at').fetchall()
        return [_position_row(row) for row in rows]

    def position(self, position_id):
        """One position by id, open or closed, or None."""
        with self._connect() as connection:
            row = connection.execute(
                'SELECT * FROM positions WHERE position_id = ?',
                (position_id,)).fetchone()
        return None if row is None else _position_row(row)

    def closing_price(self, account, tickets):
        """The volume-weighted price the broker CLOSED these position
        tickets at, from the journal; None when it has no closing deal."""
        tickets = [str(t) for t in tickets or ()]
        if not account or not tickets:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT volume, price FROM fills WHERE account = ? AND '
                "entry = 'close' AND position_ticket IN (%s)"
                % ','.join('?' * len(tickets)),
                [str(account)] + tickets).fetchall()
        volume = sum(float(r['volume'] or 0) for r in rows)
        if not volume:
            return None
        return sum(float(r['volume'] or 0) * float(r['price'] or 0)
                   for r in rows) / volume

    def closed_positions(self, limit=200):
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM positions WHERE closed_at IS NOT NULL '
                'ORDER BY closed_at DESC LIMIT ?', (limit,)).fetchall()
        return [_position_row(row) for row in rows]

    def positions_between(self, start=None, end=None, pair_key=None):
        """Positions OPENED inside a window, open ones included.

        Anchored on `opened_at`, not on the close: a position belongs to
        the session it was PUT ON in, which is the session whose click
        the entry slippage was measured against. Anchoring on the close
        would move a trade carried overnight into the next day's report
        and credit its entry to a click nobody made that day.
        """
        where, params = [], []
        if start is not None:
            where.append('opened_at >= ?')
            params.append(start)
        if end is not None:
            where.append('opened_at <= ?')
            params.append(end)
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM positions {clause} ORDER BY opened_at',
                params).fetchall()
        return [_position_row(row) for row in rows]

    # -- the journal --------------------------------------------------------

    def record_fills(self, account, rows, resolve=None):
        """Write what the BROKER says happened. Idempotent by deal id.

        `rows` are `leg.order_log()` records — MT5's own deal history,
        which includes the trader's manual terminal clicks as well as
        ours. Both belong in the journal: a fill on the account is a
        fill on the account, and `is_ours` says which is which.

        `resolve(account, symbol)` maps a fill onto a configured pair
        and leg where it can; where it cannot, the row is still kept.
        """
        written = 0
        now = self.clock()
        with self._connect() as connection:
            for row in rows or ():
                if row.get('inst_type') != 'DEAL':
                    # Orders and pendings are not fills. They are in the
                    # events table and in MT5's own order history.
                    continue
                deal_id = str(row.get('deal_id') or '')
                if not deal_id:
                    continue
                pair_key, leg = (_resolve(resolve, account, row)
                                 if resolve else (None, None))
                connection.execute(
                    """INSERT OR REPLACE INTO fills
                       (deal_id, account, order_id, position_ticket, symbol,
                        side, entry, order_type, volume, price, commission,
                        swap, profit, fee, broker_time_ms, server_offset_s,
                        seen_at, magic, is_ours, comment, pair_key, leg)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                               COALESCE((SELECT seen_at FROM fills
                                         WHERE account = ? AND deal_id = ?),
                                        ?),
                               ?,?,?,?,?)""",
                    (deal_id, account, str(row.get('order_id') or ''),
                     str(row.get('position_id') or ''), row.get('symbol'),
                     row.get('side'), row.get('pos_side'),
                     row.get('order_type'), row.get('fill_qty'),
                     row.get('fill_price'), row.get('commission'),
                     row.get('swap'), row.get('pnl'), row.get('fee'),
                     row.get('filled_at'), row.get('server_offset_sec'),
                     account, deal_id, now,
                     row.get('magic'), 1 if row.get('is_bot') else 0,
                     row.get('comment'), pair_key, leg))
                written += 1
        return written

    def record_bills(self, account, rows):
        """OKX's account bills, as `OKXBroker.account_bills` reads them.
        Idempotent by bill id."""
        written = 0
        with self._connect() as connection:
            for r in rows or ():
                bill_id = str(r.get('billId') or '')
                if not bill_id:
                    continue

                def num(key):
                    try:
                        value = r.get(key)
                        return None if value in (None, '') else float(value)
                    except (TypeError, ValueError):
                        return None
                connection.execute(
                    """INSERT OR REPLACE INTO account_bills
                       (account, bill_id, ts_ms, type, sub_type, inst_id,
                        ccy, bal_chg, bal, pnl, fee, sz, px, trade_id,
                        ord_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (account, bill_id, int(float(r.get('ts') or 0)),
                     str(r.get('type') or ''), str(r.get('subType') or ''),
                     r.get('instId') or '', r.get('ccy') or '',
                     num('balChg'), num('bal'), num('pnl'), num('fee'),
                     num('sz'), num('px'), str(r.get('tradeId') or ''),
                     str(r.get('ordId') or '')))
                written += 1
        return written

    def bills(self, since_ms=None, until_ms=None, account=None):
        """The account bills, oldest first."""
        where, params = [], []
        if since_ms is not None:
            where.append('ts_ms >= ?')
            params.append(since_ms)
        if until_ms is not None:
            where.append('ts_ms <= ?')
            params.append(until_ms)
        if account:
            where.append('account = ?')
            params.append(account)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM account_bills {clause} '
                f'ORDER BY ts_ms, bill_id', params).fetchall()
        return [dict(row) for row in rows]

    def fills(self, pair_key=None, account=None, ours_only=False,
              since_ms=None, limit=500):
        """The journal, newest first."""
        where, params = [], []
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        if account:
            where.append('account = ?')
            params.append(account)
        if ours_only:
            where.append('is_ours = 1')
        if since_ms:
            where.append('broker_time_ms >= ?')
            params.append(since_ms)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM fills {clause} '
                f'ORDER BY broker_time_ms DESC, rowid DESC LIMIT ?',
                params).fetchall()
        return [dict(row) for row in rows]

    def fill_totals(self, pair_key=None, ours_only=False):
        """What the journal adds up to — from the BROKER's numbers.

        This is the honest counterweight to our own marks: commission
        and swap are the broker's, and profit on a closing deal is what
        MT5 itself booked. Over the WHOLE journal - `first_ms` and
        `last_ms` say which stretch that is - and, with `ours_only`,
        over this desk's own fills, the same rows the table shows.
        """
        where, params = [], []
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        if ours_only:
            where.append('is_ours = 1')
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS fills,
                           COALESCE(SUM(volume), 0) AS volume,
                           -- NOT coalesced to zero. A column of NULLs
                           -- sums to NULL, and that is the honest
                           -- answer: nobody measured this. The COUNTs
                           -- say how many rows carried a figure, so a
                           -- PARTIAL total can be labelled rather than
                           -- passed off as the whole.
                           SUM(commission) AS commission,
                           COUNT(commission) AS commission_measured,
                           SUM(swap) AS swap,
                           COUNT(swap) AS swap_measured,
                           COALESCE(SUM(profit), 0) AS profit,
                           MIN(broker_time_ms) AS first_ms,
                           MAX(broker_time_ms) AS last_ms
                    FROM fills {clause}""", params).fetchone()
            # ...and the same, account by account: a journal that has
            # seen more than one pair of accounts adds them all up.
            accounts = connection.execute(
                f"""SELECT account, COUNT(*) AS fills,
                           COALESCE(SUM(profit), 0) AS profit,
                           MIN(broker_time_ms) AS first_ms,
                           MAX(broker_time_ms) AS last_ms
                    FROM fills {clause}
                    GROUP BY account ORDER BY account""", params).fetchall()
        return dict(row, by_account=[dict(a) for a in accounts])

    def fills_between(self, from_ms=None, to_ms=None, ours_only=True):
        """What the BROKER filled in a window, on the broker's stamps.

        The counterweight to the slippage report: the report is built
        from OUR positions, and this says how many deals the account
        actually saw over the same stretch. Two numbers that disagree
        mean fills the report is blind to — a manual click in the
        terminal, or a leg we never paired — and that is worth showing
        rather than reconciling away.
        """
        where, params = [], []
        if ours_only:
            where.append('is_ours = 1')
        if from_ms is not None:
            where.append('broker_time_ms >= ?')
            params.append(from_ms)
        if to_ms is not None:
            where.append('broker_time_ms <= ?')
            params.append(to_ms)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS fills,
                           COALESCE(SUM(volume), 0) AS volume,
                           -- NOT coalesced to zero. A column of NULLs
                           -- sums to NULL, and that is the honest
                           -- answer: nobody measured this. The COUNTs
                           -- say how many rows carried a figure, so a
                           -- PARTIAL total can be labelled rather than
                           -- passed off as the whole.
                           SUM(commission) AS commission,
                           COUNT(commission) AS commission_measured,
                           SUM(swap) AS swap,
                           COUNT(swap) AS swap_measured,
                           COALESCE(SUM(profit), 0) AS profit
                    FROM fills {clause}""", params).fetchone()
        return dict(row)

    # -- events -------------------------------------------------------------

    def event(self, kind, pair_key=None, **detail):
        """One line of the audit trail. Never a substitute for saying it
        on the screen — this is for answering questions afterwards."""
        with self._connect() as connection:
            connection.execute(
                'INSERT INTO events (at, kind, pair_key, detail) '
                'VALUES (?,?,?,?)',
                (self.clock(), kind, pair_key, json.dumps(detail,
                                                          default=str)))

    # -- the Algo's candles ---------------------------------------------------

    def save_candles(self, pair_key, timeframe_sec, beta, rows,
                     source='live'):
        """Closed candles [(bucket, close), ...]. A candle is written once:
        one we watched close ourselves is never replaced by a backfill."""
        rows = [(pair_key, float(timeframe_sec), float(beta), float(b),
                 float(c), source) for b, c in rows or () if c is not None]
        if not rows:
            return 0
        verb = 'INSERT OR REPLACE' if source == 'live' else 'INSERT OR IGNORE'
        with self._connect() as connection:
            connection.executemany(
                f'{verb} INTO algo_candles (pair_key, timeframe_sec, beta, '
                f'bucket, close, source) VALUES (?,?,?,?,?,?)', rows)
        return len(rows)

    def save_warmup(self, pair_key, live_sec, at, armed=None):
        with self._connect() as connection:
            connection.execute(
                'INSERT OR REPLACE INTO algo_warmup (pair_key, live_sec, at, '
                'armed) VALUES (?,?,?,?)',
                (pair_key, float(live_sec), float(at),
                 None if armed is None else json.dumps(armed)))

    def warmup(self, pair_key):
        """(live_sec, at) last saved for this ladder, or None."""
        saved = self.warmup_state(pair_key)
        return None if saved is None else (saved['live_sec'], saved['at'])

    def warmup_state(self, pair_key):
        """{'live_sec', 'at', 'armed'} last saved for this ladder, or
        None. `armed`: {'SELL': bool, 'BUY': bool}, all False when the
        row was saved before arming was kept."""
        with self._connect() as connection:
            row = connection.execute(
                'SELECT live_sec, at, armed FROM algo_warmup '
                'WHERE pair_key = ?', (pair_key,)).fetchone()
        if row is None:
            return None
        try:
            armed = json.loads(row['armed']) if row['armed'] else {}
        except (TypeError, ValueError):
            armed = {}
        extreme = armed.get('extreme') or {}

        def number(value):
            try:
                return None if value is None else float(value)
            except (TypeError, ValueError):
                return None
        return {'live_sec': row['live_sec'], 'at': row['at'],
                'armed': {'SELL': bool(armed.get('SELL')),
                          'BUY': bool(armed.get('BUY'))},
                'extreme': {'SELL': number(extreme.get('SELL')),
                            'BUY': number(extreme.get('BUY'))}}

    def clear_warmup(self, pair_key):
        with self._connect() as connection:
            connection.execute('DELETE FROM algo_warmup WHERE pair_key = ?',
                               (pair_key,))

    def candles(self, pair_key, timeframe_sec, beta, limit=500):
        """The newest `limit` closed candles, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT bucket, close FROM algo_candles WHERE pair_key = ? '
                'AND timeframe_sec = ? AND beta = ? ORDER BY bucket DESC '
                'LIMIT ?', (pair_key, float(timeframe_sec), float(beta),
                            int(limit))).fetchall()
        return [(row['bucket'], row['close']) for row in reversed(rows)]

    def save_analysis_candles(self, pair_key, timeframe_sec, beta, rows):
        """Closed candles the ANALYSIS band watched, in its own table.

        Never `algo_candles`: the analysis band runs with a ladder's
        Algo off as well as on, and a reading that writes into the
        table the Algo seeds from could change what the Algo decides
        on a later run. Separate tables make that impossible rather
        than merely unlikely.
        """
        rows = [(pair_key, float(timeframe_sec), float(beta), float(b),
                 float(c), 'live') for b, c in rows or () if c is not None]
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                'INSERT OR REPLACE INTO analysis_candles (pair_key, '
                'timeframe_sec, beta, bucket, close, source) '
                'VALUES (?,?,?,?,?,?)', rows)
        return len(rows)

    def analysis_candles(self, pair_key, timeframe_sec, beta, limit=500):
        """The analysis band's own candles, and the Algo's where it has
        none of its own for that bucket.

        Reading `algo_candles` is safe in the direction that matters:
        it lets the band start warm from whatever the Algo already
        collected, without the analysis side ever writing there.
        """
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT bucket, close FROM ('
                '  SELECT bucket, close, 0 AS rank FROM analysis_candles '
                '    WHERE pair_key = ? AND timeframe_sec = ? AND beta = ? '
                '  UNION ALL '
                '  SELECT bucket, close, 1 AS rank FROM algo_candles '
                '    WHERE pair_key = ? AND timeframe_sec = ? AND beta = ?'
                ') GROUP BY bucket HAVING rank = MIN(rank) '
                'ORDER BY bucket DESC LIMIT ?',
                (pair_key, float(timeframe_sec), float(beta),
                 pair_key, float(timeframe_sec), float(beta),
                 int(limit))).fetchall()
        return [(row['bucket'], row['close']) for row in reversed(rows)]

    # -- the Analysis tab ------------------------------------------------------

    def save_shadow(self, watch):
        with self._connect() as connection:
            connection.execute(
                'INSERT OR REPLACE INTO shadow_watches '
                '(position_id, pair_key, armed_at, done, data) '
                'VALUES (?,?,?,?,?)',
                (watch['position_id'], watch.get('pair_key'),
                 watch['armed_at'], 1 if watch.get('done') else 0,
                 json.dumps(watch, default=str)))

    def shadows(self, active_only=False, since=None, pair_key=None,
                limit=500):
        where, params = [], []
        if active_only:
            where.append('done = 0')
        if since is not None:
            where.append('armed_at >= ?')
            params.append(since)
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT data FROM shadow_watches {clause} '
                'ORDER BY armed_at DESC LIMIT ?', params + [limit]).fetchall()
        return [json.loads(row['data']) for row in rows]

    def save_excursions(self, pair_key, data):
        with self._connect() as connection:
            connection.execute(
                'INSERT OR REPLACE INTO z_excursions (pair_key, at, data) '
                'VALUES (?,?,?)', (pair_key, self.clock(), json.dumps(data)))

    def excursions(self):
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT pair_key, data FROM z_excursions').fetchall()
        return {row['pair_key']: json.loads(row['data']) for row in rows}

    #: One signal row, in the order it is written.
    SIGNAL_COLUMNS = ('pair_key', 'basis', 'side', 'broker_day', 'threshold',
                      'state', 'outcome', 'armed_at', 'armed_z',
                      'entered_at', 'entered_z', 'entry', 'break_even',
                      'tp', 'sl', 'units', 'closed_at', 'closed_z',
                      'peak_net', 'trough_net', 'reverted', 'revert_min',
                      'held_min', 'entry_z', 'max_entry_z', 'reentry_back',
                      'reentry_window_pct', 'algo_mode', 'source',
                      'exit_reason', 'net_pnl', 'block_reason',
                      'edge_ratio', 'edge_required', 'exit_spread',
                      'quantity', 'leg_a_side', 'leg_a_entry',
                      'leg_b_side', 'leg_b_entry', 'entry_margin',
                      'entry_atr', 'decided_by', 'leg_a_account',
                      'leg_a_broker', 'leg_b_account', 'leg_b_broker',
                      'edge_on', 'armed_spread')

    def save_signal(self, row):
        """Write a signal, or update the one `row['id']` names.

        A signal is the one record here that legitimately CHANGES after
        it is written: it arms, then enters, then settles, over hours.
        The zone rows beside it never change, which is why they are a
        separate table rather than a status column on this one.
        """
        values = []
        for name in self.SIGNAL_COLUMNS:
            value = row.get(name)
            if name in ('reverted', 'edge_on') and value is not None:
                value = 1 if value else 0
            values.append(value)
        with self._connect() as connection:
            if row.get('id'):
                connection.execute(
                    'UPDATE signal_events SET %s WHERE id = ?'
                    % ','.join(f'{n} = ?' for n in self.SIGNAL_COLUMNS),
                    values + [row['id']])
                return row['id']
            cursor = connection.execute(
                'INSERT INTO signal_events (%s) VALUES (%s)'
                % (','.join(self.SIGNAL_COLUMNS),
                   ','.join('?' * len(self.SIGNAL_COLUMNS))), values)
            return cursor.lastrowid

    def _signal_rows(self, rows):
        out = []
        for row in rows:
            body = {name: row[name] for name in self.SIGNAL_COLUMNS}
            body['id'] = row['id']
            body['reverted'] = bool(body['reverted'])
            if body['edge_on'] is not None:
                body['edge_on'] = bool(body['edge_on'])
            body['source'] = body['source'] or 'LIVE'
            out.append(body)
        return out

    def signals(self, pair_key=None, since=None, broker_day=None,
                limit=2000):
        """Signals, newest armed first, filtered IN THE QUERY.

        `broker_day` is one calendar day on the BROKER's clock and is
        matched exactly — the date picker must not become "read every
        row and compare timestamps in the browser".
        """
        where, params = [], []
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        if broker_day:
            where.append('broker_day = ?')
            params.append(str(broker_day))
        elif since is not None:
            where.append('armed_at >= ?')
            params.append(float(since))
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        params.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM signal_events {clause} '
                f'ORDER BY armed_at DESC, id DESC LIMIT ?', params).fetchall()
        return self._signal_rows(rows)

    def drop_replayed(self, pair_key=None, since=None):
        """Remove DERIVED rows before deriving them again.

        A replay is a function of the candles and the settings, so
        running it twice must not leave two of every trade. Live rows
        are never touched: they were measured, and nothing re-derives
        a measurement.
        """
        where = ["source = 'REPLAY'"]
        params = []
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        if since is not None:
            where.append('armed_at >= ?')
            params.append(float(since))
        with self._connect() as connection:
            cursor = connection.execute(
                'DELETE FROM signal_events WHERE ' + ' AND '.join(where),
                params)
            return cursor.rowcount

    def open_signals(self):
        """Everything still arming or running, so a restart picks the
        forward watch back up instead of losing it."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM signal_events "
                "WHERE state IN ('ARMED','OPEN') "
                # A REPLAY row that ran out of candles is not something
                # the live watch can carry on marking.
                "AND (source IS NULL OR source != 'REPLAY') "
                "ORDER BY armed_at").fetchall()
        return self._signal_rows(rows)

    def signal_days(self, pair_key=None):
        """Which broker days have signals, and how many — what fills
        the date picker, so it offers days that exist."""
        where, params = [], []
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        where.append('broker_day IS NOT NULL')
        clause = 'WHERE ' + ' AND '.join(where)
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT broker_day, COUNT(*) AS rows FROM signal_events '
                f'{clause} GROUP BY broker_day ORDER BY broker_day DESC '
                f'LIMIT 400', params).fetchall()
        return [{'day': row['broker_day'], 'rows': row['rows']}
                for row in rows]

    def save_signal_arming(self, pair_key, data):
        with self._connect() as connection:
            connection.execute(
                'INSERT OR REPLACE INTO signal_arming (pair_key, at, data) '
                'VALUES (?,?,?)',
                (pair_key, self.clock(), json.dumps(data)))

    def signal_arming(self):
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT pair_key, data FROM signal_arming').fetchall()
        return {row['pair_key']: json.loads(row['data']) for row in rows}

    def events_between(self, kinds, start=None, end=None):
        """Audit-trail rows of these kinds, oldest first."""
        where = ['kind IN (%s)' % ','.join('?' * len(kinds))]
        params = list(kinds)
        if start is not None:
            where.append('at >= ?')
            params.append(start)
        if end is not None:
            where.append('at <= ?')
            params.append(end)
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM events WHERE ' + ' AND '.join(where) +
                ' ORDER BY at', params).fetchall()
        return [dict(row, detail=json.loads(row['detail'] or '{}'))
                for row in rows]

    def events(self, kind=None, limit=200):
        clause = 'WHERE kind = ?' if kind else ''
        params = ([kind] if kind else []) + [limit]
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM events {clause} ORDER BY at DESC LIMIT ?',
                params).fetchall()
        return [dict(row, detail=json.loads(row['detail'] or '{}'))
                for row in rows]


def _position_row(row):
    """A stored row, as the plain dict the book rebuilds a position from."""
    data = dict(row)
    for leg in ('leg_a', 'leg_b', 'exit_levels'):
        if leg not in data:                 # an older database
            continue
        try:
            data[leg] = json.loads(data[leg]) if data[leg] else None
        except ValueError:
            logging.error('position %s has an unreadable %s',
                          data.get('position_id'), leg)
            data[leg] = None
    return data
