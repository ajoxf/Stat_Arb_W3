"""Which COLUMN sends which side, and nothing else.

The desk arrives from TT, where clicking the BIDS column joins the bid
and is a resting BUY. This app read the other way — asks buy — so every
click a trader made was the opposite of the one they intended.
"""

from okxtrader.commands import CommandRunner
from okxtrader.config import DEFAULT_SETTINGS


def coerce(value):
    return CommandRunner.HOT_SETTINGS['CLICK_CONVENTION'](value)


def test_a_fresh_machine_trades_the_way_its_screen_says_it_does():
    """The default is TT, and that decision was taken on its own,
    after the switch had been in use for a while.

    It also settles a disagreement between three files. The Settings
    pane has always drawn an unset value as TT, and the snapshot has
    always defaulted to TT, while DEFAULT_SETTINGS said TOUCH. A fresh
    machine therefore SHOWED 'Bids buy - TT price ladder' and CROSSED
    the other way: the one shape of bug that costs money without
    looking wrong on the screen."""
    assert DEFAULT_SETTINGS['CLICK_CONVENTION'] == 'TT'


def test_control_the_three_places_that_default_it_now_agree():
    """The control, and the part worth keeping. Any one of them
    drifting reintroduces a ladder that trades against its own
    label."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    settings_js = (root / 'okxtrader' / 'static' /
                   'settings.js').read_text(encoding='utf-8')
    coordinator = (root / 'okxtrader' /
                   'coordinator.py').read_text(encoding='utf-8')
    assert "settings.CLICK_CONVENTION || 'TT'" in settings_js
    assert "self.config.get('CLICK_CONVENTION', 'TT')" in coordinator
    assert DEFAULT_SETTINGS['CLICK_CONVENTION'] == 'TT'


def test_a_desk_that_chose_touch_keeps_it():
    """Nobody who set it deliberately is moved by the default, and a
    machine that has ever pressed Apply on the Trading pane carries its
    own value."""
    from okxtrader.config import TraderConfig
    config = TraderConfig.from_raw(
        {'accounts': {}, 'pairs': {},
         'settings': {'CLICK_CONVENTION': 'TOUCH'}}, 'x.json')
    assert config.get('CLICK_CONVENTION') == 'TOUCH'


def test_tt_is_reachable_because_that_is_the_point():
    assert coerce('TT') == 'TT'
    assert coerce('tt') == 'TT'


def test_it_is_hot_so_the_desk_can_be_shown_both():
    """A restart to settle an argument about a click is a restart
    nobody makes; it has to change under them."""
    assert 'CLICK_CONVENTION' in CommandRunner.HOT_SETTINGS


def test_only_touch_turns_it_off():
    assert coerce('TOUCH') == 'TOUCH'
    assert coerce('touch') == 'TOUCH'
    assert coerce('  Touch  ') == 'TOUCH'


def test_anything_unrecognised_falls_back_to_tt():
    """A ladder with no convention at all is a ladder whose clicks mean
    nothing. Garbage picks the safe reading rather than breaking."""
    for junk in ('', None, 'nonsense', 0, 'BUY'):
        assert coerce(junk) == 'TT', junk


def test_the_engine_never_sees_a_column():
    """The guarantee the whole change rests on: the side reaches the
    coordinator already decided, so sizing, hedging and execution are
    identical under either convention."""
    import inspect
    from okxtrader.coordinator import Coordinator
    source = inspect.getsource(Coordinator._click)
    assert "'ask'" not in source and "'bid'" not in source


def test_a_fresh_install_carries_it_explicitly():
    """In config.example.json as well as in the defaults, so the file a
    trader can open says which way their clicks go rather than leaving
    it to be inferred from code they cannot read."""
    import json
    from pathlib import Path
    example = json.loads(
        (Path(__file__).resolve().parent.parent /
         'config.example.json').read_text(encoding='utf-8'))
    assert example['settings']['CLICK_CONVENTION'] == 'TT'


# --- The screen must never name a side the click does not send ----------
#
#     Re-verified end to end after the default was flipped, because a
#     ladder that says BUY and sends SELL costs money without looking
#     wrong. Three defects were found by that pass and are fixed; each
#     is pinned here.

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / 'okxtrader' / 'static' / 'app.js').read_text(encoding='utf-8')


def test_the_side_is_decided_in_exactly_one_place():
    """The structural guarantee the whole thing rests on. The browser
    decides the side and the engine takes it as given - if the server
    re-applied the convention, every click would be inverted twice and
    a desk on TT would trade TOUCH."""
    import re
    server = (ROOT / 'okxtrader' / 'coordinator.py').read_text(
        encoding='utf-8') + (ROOT / 'okxtrader' / 'commands.py').read_text(
        encoding='utf-8') + (ROOT / 'okxtrader' / 'executor.py').read_text(
        encoding='utf-8')
    # The server may PUBLISH it and COERCE it. It must never branch on
    # it to choose a side.
    for hit in re.finditer(r'CLICK_CONVENTION', server):
        line = server[server.rfind('\n', 0, hit.start()) + 1:
                      server.find('\n', hit.start())]
        assert ('self.config.get' in line or "'CLICK_CONVENTION':" in line
                or line.strip().startswith('#')), line.strip()
    assert APP_JS.count('sideForColumn') >= 3      # defined, ladder, grid


def test_the_mapping_itself_is_the_right_way_round():
    """TT: bids buy, asks sell. TOUCH: asks buy, bids sell. Asserted as
    the exact source line, because this is the one line where a typo is
    a trade in the wrong direction."""
    assert "if (column === 'ask') { return tt ? 'SELL' : 'BUY'; }" in APP_JS
    assert "return tt ? 'BUY' : 'SELL';" in APP_JS


def test_a_snapshot_without_the_field_agrees_with_everything_else():
    """It read `=== 'TT'`, so a missing field meant TOUCH while the
    engine, the snapshot fallback and the Settings pane all meant TT.
    That is a second convention nobody chose, reachable whenever the
    field is absent."""
    assert "var tt = state.snapshot.click_convention !== 'TOUCH';" in APP_JS
    assert "state.snapshot.click_convention === 'TT'" not in APP_JS


def test_control_all_four_defaults_now_agree():
    """The control. Any one of them drifting puts a ladder back to
    trading against its own label."""
    settings_js = (ROOT / 'okxtrader' / 'static' /
                   'settings.js').read_text(encoding='utf-8')
    coordinator = (ROOT / 'okxtrader' /
                   'coordinator.py').read_text(encoding='utf-8')
    assert DEFAULT_SETTINGS['CLICK_CONVENTION'] == 'TT'          # engine
    assert "self.config.get('CLICK_CONVENTION', 'TT')" in coordinator
    assert "settings.CLICK_CONVENTION || 'TT'" in settings_js     # the pane
    assert "click_convention !== 'TOUCH'" in APP_JS               # the click


def test_the_market_grid_names_the_side_it_will_actually_send():
    """Its tooltips were written out by hand as 'Click: SELL' on the bid
    and 'Click: BUY' on the ask - the hit-and-lift reading. On a TT desk
    both were exactly backwards. The CLICK was right the whole time,
    which is what made it dangerous: only the words disagreed."""
    assert 'Click: SELL the spread here' not in APP_JS
    assert 'Click: BUY the spread here' not in APP_JS
    assert "clickHint('bid', row.short_spread)" in APP_JS
    assert "clickHint('ask', row.long_spread)" in APP_JS


def test_control_the_grid_click_always_did_use_the_shared_mapping():
    """The control that says how bad it was: the orders were correct,
    so no trade went the wrong way - a trader reading the tooltip would
    simply have been told the opposite of what they were about to do."""
    assert "clickLevel(key, sideForColumn('bid'), row.short_spread)" in APP_JS
    assert "clickLevel(key, sideForColumn('ask'), row.long_spread)" in APP_JS


def test_the_bid_ask_direction_hint_does_not_read_as_a_click():
    """H -> L sits under the BID of the quote panel, which is not
    clickable. Worded as a click it would contradict a TT desk, where
    the bid column is where a BUY rests. Worded as what the price IS,
    it is true under either convention."""
    hint = APP_JS[APP_JS.index('spread-hint'):][:1200]
    assert 'whichever column your desk clicks' in hint
    assert 'Selling the spread here' not in APP_JS
    assert 'Buying the spread here' not in APP_JS
