"""Assume you will lose track. Then find out, and say so.

Two ways our book and the broker's disagree:

- **Orphans** — a position at the broker that is not in our book. It is
  real money, unhedged as far as we know, and nobody is watching it.
- **Ghosts** — a position in our book that is not at the broker. It was
  closed by hand, stopped out, or never really existed.

Both are settled on THREE strikes, not one: a single poll can catch the
broker mid-fill, and acting on that is how a healthy position gets
closed for being briefly invisible.

Three rules that cost money in the system this is ported from:

1. **P&L on an orphan close multiplies by CONTRACT SIZE.** That was
   wrong for months and booked closes at 1% of what they cost — $0.81
   where the answer was $81.10. An unknown symbol gets 1.0 and SAYS SO.
2. **A close that did not go through leaves the position OPEN and
   ACTIVE.** Setting an ERROR/CLOSING status removes it from every
   active lookup — the ladder, the monitor, and this module's own
   known-ticket set — so the money sits at the broker while the UI
   reads flat.
3. **None is not empty.** A leg that could not be read is UNKNOWN, and
   an unknown account produces no orphans and no ghosts. Treating it as
   flat would turn every position we hold into a ghost and clear the
   book while the money is still out there.
"""

import logging
import time

from .models import MAGIC_NUMBER, OrderSide

#: The comment we write on every order this system places. Positions
#: carrying it are OURS by definition - the broker is quoting our own
#: words back at us - so one that is missing from the book is a
#: BOOKKEEPING failure, never a stray position to be tidied away.
OUR_COMMENT_TAGS = ('LADDER', 'HEDGE')


def is_ours(position):
    """Did THIS system place the order that opened this position?

    Read off the broker's own copy of the comment we wrote. A position
    that answers yes is never auto-closed: if it is not in our book,
    the book is wrong, and closing a real trade to tidy up a
    bookkeeping error is the worst available outcome.

    Errs towards LEAVING IT ALONE. An unreadable or missing comment
    answers no, which is the old behaviour - this only ever adds
    protection, never removes it.
    """
    comment = str((position or {}).get('comment') or '').strip().upper()
    return any(comment.startswith(tag) for tag in OUR_COMMENT_TAGS)


class Reconciler:
    def __init__(self, config, legs, book, executor, clock=time.time,
                 store=None):
        self.config = config
        self.legs = legs
        self.book = book
        self.executor = executor
        self.clock = clock
        #: OUR OWN record of every ticket the book has ever held, which
        #: is the answer to 'did we place this' that does not depend on
        #: a broker. See database.py's our_tickets table. None where
        #: there is no database, and then only the comment and the book
        #: are left - which is the old behaviour, not a worse one.
        self.store = store
        self._ledger = set()
        self._ledger_read = 0.0
        self.orphan_strikes = {}       # (account, ticket) -> count
        self.ghost_strikes = {}        # position_id -> count
        self.close_failures = {}       # (account, ticket) -> failed closes
        #: What we closed that we never opened, with what it cost. The
        #: operator can read this; it is not a log line.
        self.untracked_closes = []
        #: Tickets we have given up on, so the broker is not hammered.
        self.escalated = set()
        self.last_run = None
        self.unknown_accounts = []
        #: Set by recovery at startup. FALSE means our book may be
        #: incomplete — no database, or an account that could not be
        #: read — and nothing is auto-closed while it is false. An
        #: orphan is only an orphan if we are sure it is not ours.
        self.book_complete = False
        #: Positions at the broker carrying our magic that recovery
        #: could not account for. Never struck out, never auto-closed:
        #: they are put on the screen for a person to adopt or close.
        self.unclaimed = {}

    #: Strikes before acting. One poll can catch the broker mid-fill.
    STRIKES = 3

    def run(self):
        """One pass. Returns what it found and what it did."""
        self.last_run = self.clock()
        strikes_needed = self.STRIKES
        attempts = int(self.config.get('CLOSE_ATTEMPTS', 3))

        broker = {}
        self.unknown_accounts = []
        for name, leg in self.legs.items():
            positions = leg.positions()
            if positions is None:
                # UNKNOWN, not flat.
                self.unknown_accounts.append(name)
                logging.warning(
                    "reconcile: account '%s' could not be read — skipped, "
                    "not treated as flat", name)
                continue
            broker[name] = {str(p['ticket']): p for p in positions}

        known = self.known_tickets() | self.ledger()
        # The same TICKET seen under another account name is the same
        # position seen through a second connection to one terminal —
        # which is what two configured accounts sharing an MT5 login
        # are. Without this, every leg we open is read as an orphan on
        # the other account and closed within seconds of being placed;
        # that is not a theory, it happened on a live box and cost the
        # trader every trade they tried to put on. Two brokers CAN
        # issue the same ticket number, so this errs towards leaving a
        # stray position alone and listing it — never towards closing
        # something that might be ours.
        known_ids = {ticket for _, ticket in known}
        report = {'at': self.last_run, 'orphans': [], 'ghosts': [],
                  'closed': [], 'escalated': [],
                  'unknown_accounts': list(self.unknown_accounts)}
        report['book_complete'] = self.book_complete

        for account, positions in broker.items():
            for ticket, position in positions.items():
                if (account, ticket) in known or ticket in known_ids:
                    # IT IS IN THE BOOK. If it was ever listed as
                    # unexplained, that listing is now wrong and has to
                    # go - a warning nobody can clear is a warning
                    # everybody learns to ignore.
                    #
                    # This is the ordinary life of a resting order, not
                    # an edge case. The broker fills a pending on ITS
                    # clock; we see it on our next poll and book the
                    # pair a moment later. A reconcile pass landing in
                    # that gap sees a position carrying our own comment
                    # that the book does not hold yet, and correctly
                    # refuses to touch it. Nothing then took the note
                    # back down, so one entirely normal second left a
                    # PERMANENT false alarm against a healthy, hedged
                    # leg - with "Close it" as the only button offered,
                    # which would have made the other leg naked.
                    self._claimed(account, ticket)
                    continue
                key = (account, ticket)
                if key in self.escalated:
                    continue
                if key in self.unclaimed:
                    # Older than this process, and unexplained. It is on
                    # the screen for a person; it is not ours to close.
                    continue
                if is_ours(position):
                    if not self.book_complete:
                        # Recovery has not finished, so "not in the
                        # book" means nothing yet. Listing it here
                        # would accuse the book of losing something
                        # before the book has been read.
                        continue
                    """OURS, and the book lost it. Never close it.

                    A trader put a spread on and watched both legs
                    close sixty seconds later - three strikes at a
                    twenty-second poll - with RECONCILE in the MT5
                    Reason column. The positions were ours: they
                    carried the LADDER tag this system writes on every
                    order it sends.

                    positions_by_magic did not report the comment, so
                    the reconciler compared tickets and nothing else.
                    A leg we placed that had fallen out of the book was
                    indistinguishable from a stray position somebody
                    opened by hand, and the tidy-up closed real trades.

                    The magic number alone cannot settle this: it is
                    shared by every desk running this system against
                    the same terminal, and it is on the reconciler's
                    own closes too. The COMMENT is the thing only we
                    write.

                    So: never auto-closed, put on the screen for a
                    person, and said in the log at CRITICAL - because
                    reaching here at all means the book lost something
                    it opened, which is a fault worth chasing even
                    though the money is now safe.
                    """
                    logging.critical(
                        "%s:%s carries OUR comment (%s) but is not in the "
                        "book - NOT closing it. The book lost a position "
                        "this system opened; adopt or close it by hand.",
                        account, ticket, position.get('comment'))
                    self.unclaimed[key] = dict(position, account=account)
                    report['orphans'].append(
                        dict(position, account=account, strikes=0,
                             held='this carries our own order comment, so '
                                  'it is ours and is never auto-closed - '
                                  'the book lost it'))
                    continue
                if not self.book_complete:
                    # We are not sure our book is the whole truth, so we
                    # are not sure this is an orphan. Report it, never
                    # act on it.
                    report['orphans'].append(
                        dict(position, account=account, strikes=0,
                             held='the book is incomplete, so nothing is '
                                  'auto-closed'))
                    continue
                strikes = self.orphan_strikes.get(key, 0) + 1
                self.orphan_strikes[key] = strikes
                report['orphans'].append(
                    dict(position, account=account, strikes=strikes))
                if strikes < strikes_needed:
                    continue
                outcome = self._close_orphan(account, position, attempts)
                report['closed'].append(outcome)
                if outcome.get('escalated'):
                    report['escalated'].append(outcome)

        # Anything that came back is no longer striking out.
        for account, positions in broker.items():
            for account_ticket in list(self.orphan_strikes):
                if account_ticket[0] == account and \
                        account_ticket[1] not in positions:
                    self.orphan_strikes.pop(account_ticket, None)

        for position in self.book.positions():
            missing = self._missing_legs(position, broker)
            if not missing:
                self.ghost_strikes.pop(position.position_id, None)
                continue
            strikes = self.ghost_strikes.get(position.position_id, 0) + 1
            self.ghost_strikes[position.position_id] = strikes
            report['ghosts'].append({'position_id': position.position_id,
                                     'pair_key': position.pair_key,
                                     'missing': missing, 'strikes': strikes})
            if strikes >= strikes_needed:
                position.closed_at = self.clock()
                position.close_reason = (
                    f"force-cleared: {', '.join(missing)} not at the broker "
                    f"after {strikes} checks")
                logging.critical("%s: %s", position.pair_key,
                                 position.close_reason)
        # BUILT LAST, from what this pass actually found.
        #
        # It used to be built before the scan, so a new unexplained
        # position was reported one whole pass late and a cleared one
        # stayed on screen for a pass after it was resolved. The screen
        # was always describing the previous twenty seconds.
        report['unclaimed'] = [dict(row) for row in self.unclaimed.values()]
        return report

    def _claimed(self, account, ticket):
        """This ticket is accounted for. Take back any note about it.

        Only ever REMOVES a warning, and only for a ticket the book (or
        our own ledger) can now explain. A position nobody can explain
        is untouched, stays listed, and is still never auto-closed.
        """
        if self.unclaimed.pop((account, str(ticket)), None) is not None:
            logging.info(
                '%s:%s is in the book again - clearing the unexplained '
                'position notice.', account, ticket)

    def known_tickets(self):
        """Every (account, ticket) our OPEN positions hold.

        Open means ACTIVE. A position whose close failed is still open,
        so its tickets are still ours — dropping them here is what turns
        a failed close into an orphan we then close twice.
        """
        known = set()
        for position in self.book.positions():
            for fill in (position.leg_a, position.leg_b):
                if not fill:
                    continue
                for ticket in fill.position_tickets:
                    known.add((fill.account, str(ticket)))
        return known

    #: How often the ledger is re-read. It only grows, and a ticket
    #: that appears between reads is still covered by the book itself -
    #: this is the memory of tickets the book has FORGOTTEN.
    LEDGER_REFRESH_SEC = 30.0

    def ledger(self):
        """Every (account, ticket) we have ever held, from OUR records.

        Re-read rather than held forever, so a ticket stamped by this
        poll is known to the next one. A database that will not read
        gives an EMPTY set and says so - never an exception out of the
        reconciler, and never a claim that nothing is ours.
        """
        if self.store is None:
            return set()
        if self.clock() - self._ledger_read < self.LEDGER_REFRESH_SEC:
            return self._ledger
        try:
            self._ledger = self.store.our_tickets()
        except Exception as e:                       # a broken database
            logging.critical(
                'could not read the ticket ledger (%s) - falling back to '
                'the book and the order comment alone for this pass', e)
            self._ledger = set()
        self._ledger_read = self.clock()
        return self._ledger

    def _missing_legs(self, position, broker):
        missing = []
        for label, fill in (('leg A', position.leg_a), ('leg B', position.leg_b)):
            if not fill or not fill.position_tickets:
                continue
            if fill.account not in broker:
                continue                 # unknown account: no opinion
            at_broker = broker[fill.account]
            if not any(str(t) in at_broker for t in fill.position_tickets):
                missing.append(label)
        return missing

    def _close_orphan(self, account, position, attempts):
        """Close a position we never opened, and book what it cost."""
        leg = self.legs[account]
        ticket = str(position['ticket'])
        key = (account, ticket)
        entry_side = OrderSide(position['side'])
        before = leg.tick(position['symbol']) or {}
        result = leg.close_ticket(position['symbol'], int(ticket),
                                  position['volume'], entry_side.value,
                                  comment='RECONCILE')
        if not result.get('ok'):
            failures = self.close_failures.get(key, 0) + 1
            self.close_failures[key] = failures
            if failures >= attempts:
                # Escalate ONCE, then stop hammering the broker.
                self.escalated.add(key)
                logging.critical(
                    "CLOSE IT BY HAND: %s ticket %s on '%s' (%s %s lots) "
                    "would not close after %d attempts: %s",
                    position['symbol'], ticket, account, position['side'],
                    position['volume'], failures, result.get('error'))
                return {'account': account, 'ticket': ticket, 'ok': False,
                        'escalated': True, 'attempts': failures,
                        'error': result.get('error'),
                        'say': 'CLOSE IT BY HAND'}
            return {'account': account, 'ticket': ticket, 'ok': False,
                    'attempts': failures, 'error': result.get('error')}

        contract_size, assumed = self._contract_size(account,
                                                     position['symbol'])
        close_price = result.get('price') or (before.get('bid'))
        pnl = None
        if close_price is not None and position.get('price_open') is not None:
            move = close_price - position['price_open']
            if entry_side is OrderSide.SELL:
                move = -move
            # x CONTRACT SIZE. Without it these were booked at 1% of what
            # they really cost.
            pnl = move * position['volume'] * contract_size
        entry = {
            'at': self.clock(), 'account': account, 'ticket': ticket,
            'symbol': position['symbol'], 'side': position['side'],
            'volume': position['volume'], 'price_open': position['price_open'],
            'price_close': close_price, 'contract_size': contract_size,
            'contract_size_assumed': assumed, 'pnl': pnl, 'ok': True,
        }
        if assumed:
            entry['note'] = (f"{position['symbol']} is not a configured leg, "
                             f"so its contract size is ASSUMED to be 1.0 — "
                             f"this P&L is a lower bound, not a measurement")
            logging.warning("reconcile: %s", entry['note'])
        self.untracked_closes.append(entry)
        self.orphan_strikes.pop(key, None)
        self.close_failures.pop(key, None)
        logging.critical(
            "reconcile: closed ORPHAN %s ticket %s on '%s' — %s %s lots, "
            "P&L %s", position['symbol'], ticket, account, position['side'],
            position['volume'],
            f"${pnl:,.2f}" if pnl is not None else 'unmeasured')
        return entry

    def _contract_size(self, account, symbol):
        """(contract size, assumed?) — from the configured pairs' MT5
        metadata, never guessed silently."""
        for pair in self.config.pairs.values():
            if pair.account_a == account and pair.symbol_a == symbol:
                size = (pair.meta_a or {}).get('contract_size')
                if size:
                    return float(size), False
            if pair.account_b == account and pair.symbol_b == symbol:
                size = (pair.meta_b or {}).get('contract_size')
                if size:
                    return float(size), False
        return 1.0, True

    def snapshot(self):
        """What the positions monitor shows for the reconciler (spec §16)."""
        return {
            'last_run': self.last_run,
            'magic': MAGIC_NUMBER,
            'orphan_strikes': {f'{a}:{t}': n
                               for (a, t), n in self.orphan_strikes.items()},
            'ghost_strikes': dict(self.ghost_strikes),
            'untracked_closes': list(self.untracked_closes),
            'escalated': [f'{a}:{t}' for a, t in self.escalated],
            'unknown_accounts': list(self.unknown_accounts),
            'book_complete': self.book_complete,
            'unclaimed': [dict(row) for row in self.unclaimed.values()],
        }
