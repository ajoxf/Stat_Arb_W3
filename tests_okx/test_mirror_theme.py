"""The desk LOOKS exactly like Mirror MT5 Trader: same colours, same
ladder and window theme. The trader asked for it in those words.

ladder.css is Mirror's stylesheet byte for byte. If this fails, a style
changed: put it back, or - if the trader asked for that exact change -
update the hash here in the same commit and say so.
"""

import hashlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'okxtrader' / 'static'

#: sha256 of Mirror's mt5trader/static/ladder.css (main @ 96e5121).
MIRROR_LADDER_CSS = 'bd481998089b0cae457354a0df6c286fa947749f3d3b1dfa9b858cbf1250a40c'


def test_the_ladder_and_window_theme_is_mirrors_byte_for_byte():
    got = hashlib.sha256((STATIC / 'ladder.css').read_bytes()).hexdigest()
    assert got == MIRROR_LADDER_CSS, (
        'ladder.css no longer matches Mirror MT5 Trader - the colour '
        'scheme and window theme must stay exactly Mirror\'s')
