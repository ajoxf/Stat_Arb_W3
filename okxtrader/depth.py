"""Implied depth: how many spreads the two order books can actually do.

A spread has no order book of its own. What it has is two books, and a
size at any spread level is whatever the WORSE of the two legs can fill
at the prices that level implies. So the ladder's Bids and Asks columns
are computed here, by walking both books together, rather than being
left empty or — much worse — filled with one leg's size, which would
show a hundred lots available on a spread that can do four.

The walk is the ordinary implied-matching one: take the best price on
each side, do as much as the smaller of the two allows, consume it from
both, and move on. Liquidity is therefore counted ONCE. Summing every
combination of B's levels against A's would report the same lots many
times over and produce a book several times deeper than either leg.

Two conversions matter and are easy to get wrong:

- **Lots to spreads.** One spread is `clip_a` lots of A and `clip_b`
  lots of B (the matched minimum). A leg with 3.0 lots showing at a
  price supports `3.0 / clip` spreads there, not 3.0.
- **Beta.** The level is `price_B - beta x price_A`, the same
  definition as everywhere else in this system. The SIZE, though, is
  already in spreads once each leg's lots are divided by its own clip —
  beta must not be applied to it a second time.

Where a broker publishes no depth — which is most CFD accounts — this
returns nothing at all rather than inventing a size from the tick
volume. An invented size on a ladder is a lie a trader would click on.
"""


def _levels(book, side):
    """The `side` of one leg's book, best price first."""
    rows = [row for row in (book or ()) if row.get('type') == side
            and row.get('price') and (row.get('volume') or 0) > 0]
    rows.sort(key=lambda row: row['price'], reverse=(side == 'bid'))
    return [{'price': float(row['price']), 'volume': float(row['volume'])}
            for row in rows]


def _walk(b_side, a_side, beta, clip_a, clip_b, sign, increment,
          max_levels=64):
    """Match B against A, best-first, consuming both.

    `sign` is +1 when the level is `price_B - beta x price_A` with B on
    its ask (buying the spread) and -1 is not used: both directions are
    that same formula, and it is the SIDES passed in that differ. The
    argument exists so the caller names its intent at the call site.
    """
    if not b_side or not a_side or not clip_a or not clip_b:
        return {}
    b_side = [dict(level) for level in b_side]
    a_side = [dict(level) for level in a_side]
    out = {}
    i = j = 0
    while i < len(b_side) and j < len(a_side) and len(out) <= max_levels:
        top_b, top_a = b_side[i], a_side[j]
        # The worse of the two, in SPREADS. One spread is clip_a lots of
        # A and clip_b lots of B.
        spreads = min(top_b['volume'] / clip_b, top_a['volume'] / clip_a)
        if spreads <= 0:
            break
        level = top_b['price'] - float(beta) * top_a['price']
        if increment:
            level = round(round(level / increment) * increment, 10)
        out[level] = out.get(level, 0.0) + spreads
        # Consumed from BOTH books: this liquidity cannot be counted
        # again at another level.
        top_b['volume'] -= spreads * clip_b
        top_a['volume'] -= spreads * clip_a
        if top_b['volume'] <= 1e-9:
            i += 1
        if top_a['volume'] <= 1e-9:
            j += 1
    return {level: size for level, size in out.items() if size > 0}


def implied(book_a, book_b, beta=1.0, clip_a=None, clip_b=None,
            increment=None):
    """Both sides of the implied spread book.

    Returns `{'buy': {level: spreads}, 'sell': {level: spreads}}`:

    - **buy** — what can be BOUGHT: lift B's offers, hit A's bids. These
      sizes belong in the ladder's Asks column, which is where the
      spread is bought.
    - **sell** — what can be SOLD: hit B's bids, lift A's offers, and
      they belong in the Bids column.

    Empty dicts where a book is missing. Never a guess.
    """
    if not book_a or not book_b:
        return {'buy': {}, 'sell': {}}
    return {
        'buy': _walk(_levels(book_b, 'ask'), _levels(book_a, 'bid'),
                     beta, clip_a, clip_b, +1, increment),
        'sell': _walk(_levels(book_b, 'bid'), _levels(book_a, 'ask'),
                      beta, clip_a, clip_b, +1, increment),
    }


def at(sizes, level, increment):
    """The implied size at one ladder row, or None if nothing is known.

    None, not 0: a level with no implied size and a level in a book
    nobody publishes look the same on the screen otherwise, and only one
    of them means "there is nothing there".
    """
    if not sizes:
        return None
    if level in sizes:
        return sizes[level]
    if increment:
        for known, size in sizes.items():
            if abs(known - level) < increment / 2.0:
                return size
    return None
