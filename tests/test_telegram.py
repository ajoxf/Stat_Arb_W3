"""The Telegram bot: who it answers, what it may do, and what it tells.

A fake Telegram records what the bot sends; a fake desk records what it
asks the engine to do. Every test that shows something WITHHELD has a
control beside it showing the same thing go through.
"""

import json
import logging
import urllib.error

import pytest

from okxtrader import telegram as tg

ME, STRANGER = 111, 999
KEY = 'USOILZ6.c|UKOILZ26.p'


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.updates = []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == 'getUpdates':
            updates, self.updates = self.updates, []
            return updates
        return {'message_id': len(self.calls)}

    def texts(self):
        return [p.get('text') for m, p in self.calls
                if m in ('sendMessage', 'editMessageText')]

    def last(self):
        return [p for m, p in self.calls
                if m in ('sendMessage', 'editMessageText')][-1]

    def buttons(self):
        markup = self.last().get('reply_markup') or {}
        return {b['text']: b['callback_data']
                for row in markup.get('inline_keyboard', []) for b in row}


def row(**extra):
    base = {'name': 'Oil', 'algo_on': True, 'algo_mode': 'DRY_RUN',
            'net_position': 0.0, 'open_pnl': None, 'positions': [],
            'short_spread': 12.275, 'long_spread': 12.36,
            'overnight': 'ALLOW',
            'algo_params': {'entry_z': 2.0, 'direction': 'BOTH',
                            'algo_qty': 1.0, 'max_trades_day': 6},
            'algo_block': {'state': 'WATCHING', 'z_sell': 1.87,
                           'z_buy': 2.37, 'upper': 12.298, 'lower': 11.623,
                           'warmup': {'sec': 600, 'need_sec': 5400,
                                      'done': False},
                           'filters': {'ready': False,
                                       'edge': {'on': True, 'ok': True,
                                                'ratio': 1.99,
                                                'required': 1.5},
                                       'regime': {'on': False}},
                           'day': {'trades': 0, 'losses_row': 0,
                                   'pnl': 0.0}},
            'market': {}}
    base.update(extra)
    return base


class FakeDesk:
    def __init__(self):
        self.snapshot = {'engine': 'up', 'pairs': {KEY: row()},
                         'accounts': {}, 'dark_accounts': [],
                         'broker_clock': {}}
        self.saved_config = {'algo_params': {'entry_z': 2.0}}
        self.commands = []
        self.saves = []
        self.answer = {'ok': True, 'data': {'ok': True}}
        self.event_rows = []
        self.records = {}
        self.report = {'ok': True, 'journal': [], 'drawdown': {},
                       'names': {KEY: 'Oil'}}

    def analysis(self, days=0):
        return json.loads(json.dumps(self.report))

    def position(self, position_id):
        return self.records.get(position_id) or {'ok': False}

    def status(self):
        return json.loads(json.dumps(self.snapshot))

    def config_pair(self, key):
        return json.loads(json.dumps(self.saved_config))

    def events(self, limit=50):
        return list(self.event_rows)

    def save_pair(self, key, payload):
        self.saves.append((key, payload))
        return {'ok': True, 'notes': []}

    def command(self, kind, payload, wait=10.0):
        assert kind in tg.ALLOWED_COMMANDS, kind
        self.commands.append((kind, payload))
        answer, self.answer = self.answer, {'ok': True, 'data': {'ok': True}}
        return answer


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def bot():
    api, desk, clock = FakeAPI(), FakeDesk(), Clock()
    b = tg.Bot(api, desk, {ME}, clock=clock)
    b.clock_ = clock
    return b


def say(bot, text, user=ME):
    bot.handle({'update_id': 1, 'message': {
        'message_id': 5, 'from': {'id': user}, 'chat': {'id': user},
        'text': text}})


def press(bot, label, user=ME):
    data = bot.api.buttons()[label]
    bot.handle({'update_id': 2, 'callback_query': {
        'id': 'q', 'from': {'id': user}, 'data': data,
        'message': {'message_id': 7, 'chat': {'id': user}}}})


# -- who it answers -----------------------------------------------------------


def test_a_stranger_gets_no_answer_at_all(bot, caplog):
    with caplog.at_level(logging.WARNING):
        say(bot, '/start', user=STRANGER)
    assert bot.api.calls == []
    assert str(STRANGER) in caplog.text      # so it can be allowed later


def test_the_CONTROL_an_allowed_user_gets_the_menu(bot):
    say(bot, '/start')
    assert 'NEXUS SPREAD DESK' in bot.api.texts()[-1]
    assert {'Status', 'Positions', 'Trades', 'P&L', 'Balance', 'Settings',
            'Alerts: ON', 'KILL ALL'} <= \
        set(bot.api.buttons())


def test_rows_are_a_bold_label_and_a_value_in_code_type():
    """Stat_Arb_W3's layout: every number in its own box."""
    assert tg.table([('Net P&L', '+$5.27'), None, ('Hold', '23m')]) == (
        '<b>Net P&amp;L</b>  <code>+$5.27</code>\n\n'
        '<b>Hold</b>  <code>23m</code>')
    assert tg.table([('Peak', None)]) == '<b>Peak</b>  <code>—</code>'


def test_no_monospace_block_anywhere():
    """Bold labels and boxed values only: a <pre> block was asked
    against."""
    source = open(tg.__file__, encoding='utf-8').read()
    assert '<pre>' not in source


def test_the_menu_lists_the_commands_as_tappable_text(bot):
    say(bot, '/start')
    text = bot.api.texts()[-1]
    assert '/positions  -  Open positions: live P&amp;L + exit levels' \
        in text and '<pre>' not in text


def test_nothing_the_bot_says_carries_an_emoji(bot):
    """A desk tool, not a chat toy: plain words on every screen."""
    source = open(tg.__file__, encoding='utf-8').read()
    assert not [c for c in source if ord(c) >= 0x2190 and c not in
                '\u2265\u2264\u2212\u2192\u221e'], 'an icon crept in'


def test_a_stranger_cannot_press_an_allowed_users_button(bot):
    say(bot, '/start')
    press(bot, 'KILL ALL', user=STRANGER)
    assert bot.desk.commands == []


def test_allowed_users_are_read_from_the_env_list():
    assert tg.allowed_users('111, 222 x 333') == {111, 222, 333}
    assert tg.allowed_users('') == set()


def test_no_token_means_no_bot_and_it_says_so(caplog):
    class App:
        config = {}
    with caplog.at_level('INFO'):
        assert tg.start(App(), env={}) is None
    assert 'TELEGRAM_BOT_TOKEN is not set' in caplog.text


# -- LIVE asks twice ------------------------------------------------------------


def open_algo(bot, mode='Signals'):
    say(bot, '/start')
    press(bot, 'Settings')
    press(bot, 'Oil')
    press(bot, 'Algo mode: ' + mode)


def test_LIVE_needs_two_confirms_and_only_then_is_sent(bot):
    open_algo(bot)
    press(bot, 'LIVE')
    assert bot.desk.commands == []
    assert 'REAL' in bot.api.texts()[-1]
    press(bot, 'Continue')
    assert bot.desk.commands == []          # still not sent
    press(bot, 'Confirm LIVE')
    assert bot.desk.commands == [('set_algo', {
        'pair': KEY, 'algo': 'ALGO', 'mode': 'LIVE', 'confirmed': True})]


def test_the_CONTROL_dry_run_takes_one_confirm(bot):
    bot.desk.snapshot['pairs'][KEY]['algo_on'] = False
    open_algo(bot, 'Off')
    press(bot, 'Signals')
    press(bot, 'Confirm')
    assert bot.desk.commands == [('set_algo', {
        'pair': KEY, 'algo': 'ALGO', 'mode': 'DRY_RUN'})]


def test_cancel_sends_nothing(bot):
    open_algo(bot)
    press(bot, 'LIVE')
    press(bot, 'Cancel')
    assert bot.desk.commands == []


def test_an_expired_button_does_nothing(bot):
    open_algo(bot)
    press(bot, 'Off')
    bot.clock_.now += tg.TOKEN_TTL_SEC + 1
    press(bot, 'Confirm')
    assert bot.desk.commands == []
    assert 'expired' in bot.api.texts()[-1]


def test_leaving_LIVE_with_a_position_asks_what_to_do(bot):
    bot.desk.snapshot['pairs'][KEY]['algo_mode'] = 'LIVE'
    open_algo(bot, 'LIVE')
    press(bot, 'Off')
    bot.desk.answer = {'ok': False, 'data': {'ok': False, 'choose':
                                             ['close', 'manual'],
                                             'positions': ['P1']}}
    press(bot, 'Confirm')
    assert 'Close now' in bot.api.buttons()
    press(bot, 'Hand to manual')
    assert bot.desk.commands[-1] == ('set_algo', {
        'pair': KEY, 'algo': 'NONE', 'mode': 'DRY_RUN',
        'off_action': 'manual'})


def test_a_refusal_comes_back_in_the_engines_words(bot):
    open_algo(bot)
    press(bot, 'LIVE')
    press(bot, 'Continue')
    bot.desk.answer = {'ok': False, 'reason': 'the ladder holds a manual '
                                              'position'}
    press(bot, 'Confirm LIVE')
    assert any('holds a manual position' in t for t in bot.api.texts())


# -- closing ---------------------------------------------------------------------


def test_close_all_on_a_ladder_is_confirmed_first(bot):
    bot.desk.snapshot['pairs'][KEY].update(
        net_position=1.0, open_pnl=3.2,
        positions=[{'side': 'SELL', 'quantity': 1, 'entry_spread': 12.3,
                    'closing_spread': 12.2, 'net_pnl': 3.2,
                    'source': 'ALGO'}])
    say(bot, '/dashboard')
    assert '(algo)' in bot.api.texts()[-1]
    press(bot, 'Close all: Oil')
    assert bot.desk.commands == []
    bot.desk.answer = {'ok': True, 'data': {'closed': 1, 'failed': []}}
    press(bot, 'Close all')
    assert bot.desk.commands == [('flatten_pair', {'pair': KEY})]


def test_kill_all_is_confirmed_first(bot):
    say(bot, '/start')
    press(bot, 'KILL ALL')
    assert bot.desk.commands == []
    press(bot, 'Confirm KILL ALL')
    assert bot.desk.commands == [('kill', {'flatten': True})]


# -- settings ---------------------------------------------------------------------


def open_settings(bot, section='Entry'):
    say(bot, '/start')
    press(bot, 'Settings')
    press(bot, 'Oil')
    press(bot, section)


def test_a_typed_setting_is_confirmed_saved_and_applied(bot):
    open_settings(bot)
    press(bot, 'Entry z: 2.0')
    say(bot, '2.5')
    assert bot.desk.saves == []
    press(bot, 'Confirm')
    typed = {'entry_z': 2.5}
    assert bot.desk.saves == [(KEY, {'algo_params': typed})]
    assert bot.desk.commands == [('set_pair', {
        'pair': KEY, 'fields': {'algo_params': typed}})]


def test_a_bad_value_changes_nothing(bot):
    open_settings(bot)
    press(bot, 'Entry z: 2.0')
    say(bot, 'abc')
    assert 'not a number' in bot.api.texts()[-1]
    assert bot.desk.saves == [] and bot.desk.commands == []


def test_an_entry_z_of_zero_is_refused_like_the_screen_refuses_it(bot):
    open_settings(bot)
    press(bot, 'Entry z: 2.0')
    say(bot, '0')
    assert 'entry z must be above 0' in bot.api.texts()[-1]
    assert bot.desk.saves == []


def test_a_choice_setting_offers_its_choices(bot):
    open_settings(bot)
    press(bot, 'Direction: Both')
    press(bot, 'H to L only')
    press(bot, 'Confirm')
    assert bot.desk.saves == [(KEY, {'algo_params': {
        'entry_z': 2.0, 'direction': 'H_TO_L'}})]


def test_every_exit_switch_is_in_the_settings_menu(bot):
    """The exits that can close a trade early are switchable from a
    phone — "Back to mean" scratched a trade at break-even once, and the
    trader has to be able to turn it off away from the desk."""
    open_settings(bot, 'Exits')
    labels = set(bot.api.buttons())
    for label in ('Back to mean: ON', 'Z-stop: OFF',
                  'Z-stop at |z|: 4.0', 'Time stop: OFF',
                  'Time stop (candles): 20', 'Stop loss: ON'):
        assert label in labels, label


def test_back_to_mean_is_switched_and_saved(bot):
    open_settings(bot, 'Exits')
    press(bot, 'Back to mean: ON')
    press(bot, 'OFF')
    press(bot, 'Confirm')
    assert bot.desk.saves == [(KEY, {'algo_params': {
        'entry_z': 2.0, 'reversion_on': False}})]


def test_a_pair_setting_is_saved_on_the_pair(bot):
    open_settings(bot, 'Session and costs')
    press(bot, 'Commission per lot, leg A (per side): desk default')
    say(bot, '3.5')
    press(bot, 'Confirm')
    assert bot.desk.saves == [(KEY, {'commission_per_lot_a': 3.5})]


# -- what it may send --------------------------------------------------------------


def test_the_bot_can_never_place_an_order():
    """No click, no level, no quantity: it switches, saves and CLOSES."""
    assert tg.ALLOWED_COMMANDS == {'set_algo', 'set_pair', 'flatten_pair',
                                   'kill'}
    source = open(tg.__file__, encoding='utf-8').read()
    for kind in ('click', 'close_at_limit', 'cancel_order', 'cancel_where'):
        assert f"'{kind}'" not in source


def test_the_web_desk_refuses_any_other_command():
    class App:
        def test_client(self):
            return None
    desk = tg.WebDesk(App(), 'secret')
    with pytest.raises(ValueError):
        desk.command('click', {'pair': KEY, 'side': 'BUY', 'level': 1})


# -- the token stays secret ----------------------------------------------------------


def test_the_token_never_appears_in_an_error(caplog):
    token = '123456:SECRET-TOKEN'

    def opener(request, timeout):
        raise urllib.error.URLError(OSError('down'))
    api = tg.TelegramAPI(token, opener=opener)
    with pytest.raises(tg.TelegramError) as raised:
        api.call('getUpdates')
    assert token not in str(raised.value)


# -- alerts -------------------------------------------------------------------------


def watch(bot):
    return bot.alert_once()


def failed_order(id_):
    return {'id': id_, 'kind': 'algo_order', 'pair_key': KEY,
            'detail': {'action': 'ENTER', 'side': 'BUY', 'ok': False,
                       'reason': '10027 AutoTrading disabled by client'}}


def test_history_already_in_the_journal_is_not_replayed(bot):
    bot.desk.event_rows = [failed_order(5)]
    assert watch(bot) == []
    # The control: one that arrives after the first look IS told.
    bot.desk.event_rows.insert(0, failed_order(6))
    told = watch(bot)
    assert len(told) == 1 and 'ORDER FAILED' in told[0]


def test_a_signal_is_not_an_alert_the_trade_is(bot):
    """A signal, LIVE or dry run, is the desk's business: in LIVE the
    TRADE ENTRY / EXIT says it, in a dry run nothing happened."""
    watch(bot)
    for i, (action, mode) in enumerate((('ENTER', 'LIVE'), ('EXIT', 'LIVE'),
                                        ('ENTER', 'DRY_RUN'))):
        bot.desk.event_rows.insert(0, {
            'id': 10 + i, 'kind': 'algo_signal', 'pair_key': KEY,
            'detail': {'action': action, 'side': 'SELL', 'z': 2.1,
                       'spread': 12.3, 'mode': mode}})
    assert watch(bot) == []
    # The control: a failure among them still is.
    bot.desk.event_rows.insert(0, failed_order(20))
    assert 'ORDER FAILED' in watch(bot)[0]


def test_an_algo_order_that_failed_says_why(bot):
    watch(bot)
    bot.desk.event_rows = [{'id': 9, 'kind': 'algo_order', 'pair_key': KEY,
                            'detail': {'action': 'ENTER', 'side': 'BUY',
                                       'ok': False,
                                       'reason': '10027 AutoTrading '
                                                 'disabled by client'}}]
    told = watch(bot)
    assert 'FAILED' in told[0] and '10027' in told[0]


def test_alerts_go_to_allowed_users_unless_they_turned_them_off(bot):
    watch(bot)
    bot.desk.snapshot['engine'] = 'stalled'
    watch(bot)
    assert any('SYSTEM ERROR' in t and 'STALLED' in t
               for t in bot.api.texts())
    # The control: with /alerts off, the same news is not sent.
    say(bot, '/alerts')
    before = len(bot.api.texts())
    bot.desk.snapshot['engine'] = 'up'
    watch(bot)
    assert len(bot.api.texts()) == before


def test_a_price_problem_is_told_once_it_has_lasted(bot):
    watch(bot)
    bot.desk.snapshot['pairs'][KEY]['market'] = {'stale_reason': 'leg A '
                                                                 'stale 30s'}
    assert watch(bot) == []                      # just started
    bot.clock_.now += tg.PROBLEM_SETTLE_SEC + 1
    told = watch(bot)
    assert 'leg A stale' in told[0]
    assert watch(bot) == []                      # once, not every look
    bot.desk.snapshot['pairs'][KEY]['market'] = {}
    assert 'back to normal' in watch(bot)[0]


def test_a_blocked_signal_is_never_an_alert(bot):
    """The edge filter re-judges every tick; its ratio changes with the
    price, so each look read as new - a stream of SIGNAL BLOCKED."""
    watch(bot)
    block = bot.desk.snapshot['pairs'][KEY]['algo_block']
    for at, ratio in enumerate(('0.11', '0.12', '0.10'), start=1):
        block['last_blocked'] = {'side': 'BUY', 'z': -0.77, 'at': at,
                                 'reason': f'edge filter: capture {ratio}x '
                                           f'the cost, under the 1.3x '
                                           f'required'}
        assert watch(bot) == []
    # The control: a day's limit on the same ladder IS told.
    block['halt'] = 'max trades for the day (10) reached'
    assert 'DAILY LIMIT' in watch(bot)[0]


def test_going_into_or_out_of_live_is_told(bot):
    row = bot.desk.snapshot['pairs'][KEY]
    row.update(algo_on=True, algo_mode='DRY_RUN')
    watch(bot)
    row['algo_mode'] = 'LIVE'
    told = watch(bot)
    assert len(told) == 1 and 'ALGO MODE' in told[0] and 'LIVE' in told[0]
    assert watch(bot) == []                      # once
    row['algo_on'] = False                       # e.g. a restart
    assert 'LIVE is off' in watch(bot)[0]


def test_the_CONTROL_dry_run_and_off_are_not_news(bot):
    row = bot.desk.snapshot['pairs'][KEY]
    row.update(algo_on=False)
    watch(bot)
    row.update(algo_on=True, algo_mode='DRY_RUN')
    assert watch(bot) == []
    row.update(algo_on=False)
    assert watch(bot) == []


def signal(alert_id, kind='ENTRY'):
    return {'id': alert_id, 'seq': 1, 'kind': kind, 'side': 'SELL',
            'reason': None if kind == 'ENTRY' else 'PROFIT_TARGET',
            'text': ('H to L — SELL 1 @ 13.8050 (z +1.52)' if kind == 'ENTRY'
                     else 'Profit target — close your SELL 1 at 12.7270'),
            'at': 1.0, 'position_id': None, 'spread': 13.805, 'z': 1.523}


def test_a_signal_in_signals_mode_is_told_once(bot):
    row = bot.desk.snapshot['pairs'][KEY]
    row.update(algo_on=True, algo_mode='DRY_RUN')
    block = row['algo_block']
    block['signal_alert'] = signal('S0')
    assert watch(bot) == []                    # on first look: not replayed
    assert watch(bot) == []
    block['signal_alert'] = signal('S1')
    [told] = watch(bot)
    assert 'SIGNAL  ·  ENTRY' in told
    assert 'H to L — SELL 1 @ 13.8050' in told
    assert 'Z-score</b>  <code>+1.52' in told
    assert watch(bot) == []                    # once, by its id
    block['signal_alert'] = signal('S2', 'EXIT')
    [told] = watch(bot)
    assert 'EXIT YOUR POSITION' in told and 'close your SELL' in told


def test_the_CONTROL_in_LIVE_a_signal_is_not_a_signal_alert(bot):
    row = bot.desk.snapshot['pairs'][KEY]
    row.update(algo_on=True, algo_mode='LIVE')
    block = row['algo_block']
    watch(bot)
    block['signal_alert'] = signal('S1')
    assert not [t for t in watch(bot) if 'SIGNAL' in t]


def test_set_mode_signals_is_understood(bot):
    say(bot, f'/set {KEY} mode signals')
    press(bot, 'Confirm')
    assert bot.desk.commands[-1] == ('set_algo', {
        'pair': KEY, 'algo': 'ALGO', 'mode': 'DRY_RUN'})


def test_the_daily_summary_is_sent_once_at_the_cutoff(bot):
    bot.desk.snapshot['broker_clock'] = {'broker_time': '16:50:00',
                                         'cutoff': '16:55'}
    watch(bot)
    assert watch(bot) == []
    bot.desk.snapshot['broker_clock']['broker_time'] = '16:55:03'
    told = watch(bot)
    assert 'END OF DAY' in told[0]
    bot.desk.snapshot['broker_clock']['broker_time'] = '17:30:00'
    assert watch(bot) == []


# -- through the real web app --------------------------------------------------------


@pytest.fixture
def web(tmp_path):
    from okxtrader.webapp import create_app
    paths = {n: str(tmp_path / n) for n in ('status.json', 'commands.jsonl',
                                            'results.json', 'config.json',
                                            'db.sqlite')}
    with open(paths['config.json'], 'w') as f:
        json.dump({'accounts': {}, 'pairs': {KEY: {'name': 'Oil'}}}, f)
    app = create_app(paths['status.json'], paths['commands.jsonl'],
                     paths['results.json'], paths['config.json'],
                     paths['db.sqlite'])
    return app, paths


def test_the_bot_passes_the_screen_lock_with_its_secret(web, monkeypatch):
    from okxtrader import screenlock
    # A machine with no PIN is never locked: give this one a PIN.
    monkeypatch.setenv(screenlock.PIN_ENV_KEY,
                       screenlock.hash_pin('1234', iterations=1))
    app, paths = web
    app.config['BOT_SECRET'] = 'in-process-secret'
    browser = app.test_client()
    browser.post('/api/lock')
    # Locked: a browser cannot save.
    assert browser.post('/api/pairs/' + KEY.replace('|', '%7C'),
                        json={'tp_target_pct_of_margin': 3}).status_code == 423
    # The control: the bot's in-process request can.
    desk = tg.WebDesk(app, 'in-process-secret')
    assert desk.save_pair(KEY, {'tp_target_pct_of_margin': 3})['ok'] is True
    # And a wrong secret is just a locked browser.
    assert tg.WebDesk(app, 'guess').save_pair(
        KEY, {'tp_target_pct_of_margin': 4}).get('ok') is False


def test_the_web_desk_saves_through_the_same_checks_as_the_screen(web):
    app, paths = web
    app.config['BOT_SECRET'] = 's'
    desk = tg.WebDesk(app, 's')
    answer = desk.save_pair(KEY, {'algo_params': {'entry_z': 0}})
    assert answer['ok'] is False and 'entry z' in answer['error']
    assert desk.config_pair(KEY)['name'] == 'Oil'


def test_a_command_with_the_engine_down_is_refused_not_queued(web):
    app, paths = web
    app.config['BOT_SECRET'] = 's'
    desk = tg.WebDesk(app, 's', sleep=lambda s: None)
    answer = desk.command('set_algo', {'pair': KEY, 'algo': 'NONE'})
    assert answer['ok'] is False and 'not running' in answer['reason']


# -- the trade messages and the W3 screens -------------------------------------------


POSITION = {
    'position_id': 'P7', 'pair_key': KEY, 'side': 'SELL', 'quantity': 0.5,
    'entry_spread': 13.196, 'closing_spread': 13.365, 'net_pnl': -8.45,
    'spread_units': 50.0, 'source': 'ALGO', 'opened_at': 1_799_999_000.0,
    'entry_margin': 191.5, 'entry_slippage': 0.002, 'click_to_on_ms': 180,
    'leg_a': {'side': 'BUY', 'volume': 0.05, 'symbol': 'USOILZ6.c',
              'price': 89.08, 'contract_size': 1000},
    'leg_b': {'side': 'SELL', 'volume': 0.05, 'symbol': 'UKOILZ26.p',
              'price': 102.276, 'contract_size': 1000}}
LEVELS = {'position_id': 'P7', 'tp': 13.119, 'sl': 13.388,
          'break_even': 13.196, 'entry_z': 1.49}


def in_order(text, parts):
    """Each part is in the message, and after the one before it."""
    at = 0
    for part in parts:
        found = text.find(part, at)
        assert found >= 0, (part, text[at:at + 300])
        at = found + len(part)


def holding(bot):
    pair = bot.desk.snapshot['pairs'][KEY]
    pair['positions'] = [dict(POSITION)]
    pair['algo_block']['positions'] = [dict(LEVELS)]
    pair['market'] = {'leg_a_bid': 89.171, 'leg_a_ask': 89.223,
                      'leg_b_bid': 102.503, 'leg_b_ask': 102.536}


def test_a_position_that_opens_is_a_trade_entry(bot):
    watch(bot)                                   # bearings: flat
    holding(bot)
    [told] = watch(bot)
    assert '<b>TRADE ENTRY  ·  H to L Oil</b>' in told
    # Stat_Arb_W3's rows, in W3's order.
    in_order(told, ['ID</b>  <code>#P7', 'Entry Time', 'Leg A Lots',
                    'Notional', 'Margin Req', 'Leg A Entry</b>  <code>$89.0800',
                    'Leg B Entry</b>  <code>$102.2760',
                    'Spread</b>  <code>+13.1960  (+1481.37 bps)',
                    'Z-score</b>  <code>+1.4900', 'Est. Fees', 'Breakeven',
                    'Orders at', 'Filled at', 'Latency</b>  <code>180 ms'])
    assert 'Take Profit' in told and '$+3.85' in told    # 0.077 x 50


def test_a_position_that_closes_is_a_trade_exit_with_its_analysis(bot):
    holding(bot)
    watch(bot)                                   # bearings: it is open
    closed = dict(POSITION, closed_at=POSITION['opened_at'] + 1359,
                  exit_spread=13.119, realized_pnl=3.85,
                  close_reason='Algo: profit target (after costs)',
                  peak_pnl=4.10, peak_min=20.0, trough_pnl=-9.0,
                  trough_min=6.0, exit_slippage=0.001)
    bot.desk.records['P7'] = {'ok': True, 'position': closed,
                              'exit_prices': {'leg_a': 89.2, 'leg_b': 102.3},
                              'entry_z': 1.49, 'exit_z': -0.4}
    bot.desk.snapshot['pairs'][KEY]['positions'] = []
    [told] = watch(bot)
    assert '<b>TRADE EXIT  ·  H to L Oil  ·  PROFIT</b>' in told
    in_order(told, ['Reason', 'Duration</b>  <code>22m 39s', 'Exit Time',
                    'Leg A Entry', 'Leg A Exit</b>  <code>$89.2000',
                    'Leg B Entry', 'Leg B Exit',
                    'Entry Spread</b>  <code>+13.1960  (Z: +1.4900)',
                    'Exit Spread</b>  <code>+13.1190  (Z: -0.4000)',
                    'Spread Chg</b>  <code>+0.0770  (with)',
                    'Orders at', 'Filled at', 'Latency',
                    'Gross PnL</b>  <code>$+3.8500',
                    'Est. Fees</b>  <code>-$0.0000',
                    'Net PnL</b>  <code>$+3.8500  (+0.0402% of notional '
                    '\u00b7 +4.02% of margin)',
                    'ANALYSIS', 'TARGET HIT', 'Peak/Trough',
                    'Capture</b>  <code>$+3.85 of $+4.10 best (+94%)',
                    'Z path</b>  <code>+1.4900 -&gt; -0.4000'])


def test_the_result_reads_against_the_tp_percent_of_margin():
    """A 2% target on 95.75 of margin nets 1.915: +2.00% of margin,
    and 0.0200% of the 9,567.80 the two legs are worth."""
    text = tg.pnl_shares(1.915, POSITION)
    assert text == '  (+0.0200% of notional \u00b7 +2.00% of margin)'


def test_an_unmeasured_margin_is_a_dash_not_zero():
    position = dict(POSITION, entry_margin=None)
    assert tg.pnl_shares(-8.45, position) == \
        '  (-0.0883% of notional \u00b7 — of margin)'
    assert tg.pnl_shares(None, POSITION) == ''


def test_the_CONTROL_positions_open_at_start_are_not_news(bot):
    holding(bot)
    assert watch(bot) == []


def test_open_positions_shows_the_whole_position_now(bot):
    holding(bot)
    say(bot, '/positions')
    text = bot.api.texts()[-1]
    assert 'OPEN POSITIONS' in text
    in_order(text, ['Position', 'Leg A Lots', 'Notional', 'Margin Req',
                    'Entry Time', 'Leg A Entry', 'Entry Spread',
                    'Leg A Now</b>  <code>$89.1710  (+0.10%)', 'Leg B Now',
                    'Spread Now', 'Orders at', 'Filled at', 'Latency',
                    'Spread Chg</b>  <code>+0.1690  (against)',
                    'Net PnL</b>  <code>$-8.45',
                    'Take Profit</b>  <code>13.1190  ($+3.85 gross)',
                    'Stop Loss</b>  <code>13.3880  ($-9.60 gross)'])
    assert 'Close all: Oil' in bot.api.buttons()


def test_the_settings_list_carries_every_key_set_takes(bot):
    say(bot, '/settings')
    listing = bot.api.texts()[-2]
    for key in ('mode', 'entry_z', 'reversion_on', 'stop_loss_pct',
                'commission_per_lot_a'):
        assert f'  {key}' in listing, key
    assert '/set &lt;key&gt; &lt;value&gt;' in bot.api.texts()[-1]


def test_set_is_confirmed_before_it_saves(bot):
    say(bot, '/set entry_z 2.5')
    assert bot.desk.saves == []
    press(bot, 'Confirm')
    assert bot.desk.saves == [(KEY, {'algo_params': {'entry_z': 2.5}})]


def test_set_a_switch_and_a_choice_by_their_words(bot):
    say(bot, '/set reversion_on off')
    press(bot, 'Confirm')
    say(bot, '/set direction h_to_l')
    press(bot, 'Confirm')
    assert [s[1]['algo_params'] for s in bot.desk.saves] == [
        {'entry_z': 2.0, 'reversion_on': False},
        {'entry_z': 2.0, 'direction': 'H_TO_L'}]


def test_set_mode_live_still_asks_twice(bot):
    say(bot, '/set mode live')
    assert bot.desk.commands == [] and 'Continue' in bot.api.buttons()
    press(bot, 'Continue')
    assert bot.desk.commands == []
    press(bot, 'Confirm LIVE')
    assert bot.desk.commands[-1][1]['mode'] == 'LIVE'


def test_set_with_a_bad_key_or_value_changes_nothing(bot):
    say(bot, '/set no_such_key 1')
    assert 'No setting' in bot.api.texts()[-1]
    say(bot, '/set entry_z 0')
    assert 'entry z must be above 0' in bot.api.texts()[-1]
    assert bot.desk.saves == [] and bot.desk.commands == []


def test_balance_and_ping(bot):
    bot.desk.snapshot['accounts'] = {'AC-10006': {
        'balance': 16000.0, 'equity': 16031.2, 'margin': 45.0,
        'margin_free': 15986.2, 'margin_level': 35625, 'profit': 31.2,
        'currency': 'USD'}}
    say(bot, '/balance')
    text = bot.api.texts()[-1]
    assert 'ACCOUNT BALANCE' in text
    in_order(text, ['Account', 'Equity</b>  <code>16,031.20 USD',
                    'Available</b>  <code>15,986.20 USD',
                    'Used</b>  <code>45.00 USD',
                    'Margin</b>  <code>35,625.0%  [OK]',
                    'Unrealized</b>  <code>$+31.20'])
    say(bot, '/ping')
    assert bot.api.texts()[-1].startswith('pong  ·  ')


TRIP = {'position_id': 'P1', 'pair_key': KEY, 'side': 'SELL',
        'quantity': 0.5, 'source': 'ALGO', 'closed_at': 1_800_000_000.0,
        'held_sec': 1359, 'entry_spread': 13.196, 'exit_spread': 13.119,
        'entry_z': 1.49, 'exit_z': -0.4,
        'exit_reason': 'Algo: profit target (after costs)', 'pnl': 3.85,
        'gross_pnl': 3.85, 'fees': 0.0, 'cum_pnl': 3.85,
        'leg_a': {'side': 'BUY', 'volume': 0.05, 'symbol': 'USOILZ6.c',
                  'entry': 89.08},
        'leg_b': {'side': 'SELL', 'volume': 0.05, 'symbol': 'UKOILZ26.p',
                  'entry': 102.276}}


def test_the_command_menu_is_registered_with_telegram(bot):
    bot.register_commands()
    [(method, params)] = [c for c in bot.api.calls if c[0] == 'setMyCommands']
    listed = {c['command']: c['description'] for c in params['commands']}
    assert listed['positions'] == 'Open positions: live P&L + exit levels'
    assert {'status', 'trades', 'pnl', 'balance', 'settings', 'set',
            'alerts', 'ping', 'start'} <= set(listed)
    # Telegram takes the names without the slash.
    assert not any(name.startswith('/') for name in listed)


def test_trades_shows_the_recent_closed_trades(bot):
    bot.desk.report['journal'] = [dict(TRIP)]
    say(bot, '/trades')
    text = bot.api.texts()[-1]
    assert 'RECENT TRADES' in text and '#P1  H to L Oil  PROFIT' in text
    in_order(text, ['Exit</b>', 'Duration', 'Leg A Entry', 'Leg A Exit',
                    'Leg B Entry', 'Leg B Exit',
                    'Entry Spread</b>  <code>+13.1960', 'Exit Spread',
                    'Entry Z</b>  <code>+1.4900', 'Exit Z</b>  <code>-0.4000',
                    'Gross PnL</b>  <code>$+3.85',
                    'Est. Fees</b>  <code>-$0.00',
                    'Net PnL</b>  <code>$+3.85  PROFIT'])


def test_the_CONTROL_no_trades_says_so(bot):
    say(bot, '/trades')
    assert 'No closed trades yet.' in bot.api.texts()[-1]


def test_pnl_sums_the_record(bot):
    loss = dict(TRIP, position_id='P2', pnl=-1.85, gross_pnl=-1.45,
                fees=0.40, cum_pnl=2.0)
    bot.desk.report['journal'] = [loss, dict(TRIP)]
    bot.desk.snapshot['pnl_check'] = {'ours': -8.45}
    say(bot, '/pnl')
    text = bot.api.texts()[-1]
    assert 'P&amp;L SUMMARY' in text
    in_order(text, ['Closed Trades</b>  <code>2',
                    'Win Rate</b>  <code>50.0%  (1W / 1L net)',
                    'Avg Win (net)</b>  <code>$+3.85',
                    'Avg Loss (net)</b>  <code>$-1.85',
                    'Today Gross', 'Today Fees', 'Today Net',
                    'All-time Gross</b>  <code>$+2.40',
                    'All-time Fees</b>  <code>-$0.40',
                    'All-time Net</b>  <code>$+2.00',
                    'Unrealized</b>  <code>$-8.45'])


# -- each ladder's hours --------------------------------------------------------------


def test_set_a_ladders_session_close(bot):
    say(bot, '/set session_close 23:45')
    press(bot, 'Confirm')
    assert bot.desk.saves == [(KEY, {'session_close': '23:45'})]


def test_the_daily_break_is_one_setting_with_both_ends(bot):
    """The desk refuses a break with one end, so the phone sets both at
    once - two separate saves would fail on the first."""
    say(bot, '/set break 23:55-1:05')
    press(bot, 'Confirm')
    assert bot.desk.saves == [(KEY, {'break_start': '23:55',
                                     'break_end': '01:05'})]
    say(bot, '/set break none')
    press(bot, 'Confirm')
    assert bot.desk.saves[-1] == (KEY, {'break_start': None,
                                        'break_end': None})


def test_a_bad_time_changes_nothing(bot):
    say(bot, '/set session_open 25:00')
    assert 'is not a time' in bot.api.texts()[-1]
    assert bot.desk.saves == []


def test_a_silent_feed_in_the_ladders_break_is_not_news(bot):
    pair = bot.desk.snapshot['pairs'][KEY]
    pair['market'] = {'stale_reason': 'leg B unchanged 40s'}
    pair['session'] = {'state': 'BREAK', 'quiet': True}
    watch(bot)
    bot.clock_.now += tg.PROBLEM_SETTLE_SEC + 1
    assert watch(bot) == []
    # The CONTROL: the same silence in session is a warning.
    pair['session'] = {'state': 'OPEN', 'quiet': False}
    watch(bot)
    bot.clock_.now += tg.PROBLEM_SETTLE_SEC + 1
    assert any('FEED WARNING' in t for t in watch(bot))


def test_end_of_day_comes_at_each_ladders_own_close(bot):
    pair = bot.desk.snapshot['pairs'][KEY]
    pair['session'] = {'close': '23:45', 'state': 'OPEN'}
    bot.desk.snapshot['broker_clock'] = {'broker_time': '16:56:00',
                                         'cutoff': '16:55'}
    watch(bot)
    assert watch(bot) == []                 # past the desk's, not ITS own
    bot.desk.snapshot['broker_clock']['broker_time'] = '23:46:00'
    [told] = watch(bot)
    assert 'END OF DAY' in told


def test_the_dashboard_shows_each_ladders_session(bot):
    bot.desk.snapshot['pairs'][KEY]['session'] = {
        'open': '01:05', 'close': '23:45', 'break': '12:00-12:30',
        'state': 'OPEN'}
    say(bot, '/status')
    assert ('Session</b>  <code>open  (01:05 to 23:45, break 12:00-12:30 '
            'broker time)') in bot.api.texts()[-1]


# -- the ATR stop and target -------------------------------------------------------


def test_set_the_stop_and_target_to_atr(bot):
    say(bot, '/set stop_mode atr')
    press(bot, 'Confirm')
    say(bot, '/set atr_period 21')
    press(bot, 'Confirm')
    assert [s[1]['algo_params'] for s in bot.desk.saves] == [
        {'entry_z': 2.0, 'stop_mode': 'ATR'},
        {'entry_z': 2.0, 'atr_period': 21}]


def test_the_exits_section_lists_the_atr_settings(bot):
    open_settings(bot, 'Exits')
    labels = set(bot.api.buttons())
    for label in ('Stop sized by: % of margin', 'Target sized by: % of margin',
                  'ATR period (candles): 14', 'Stop x ATR: 2.0',
                  'Target x ATR: 1.5'):
        assert label in labels, label


def test_positions_and_status_show_the_atr(bot):
    holding(bot)
    pair = bot.desk.snapshot['pairs'][KEY]
    pair['algo_block']['positions'][0].update(
        entry_atr=0.0960, stop_mode='ATR', target_mode='ATR')
    pair['algo_block'].update(atr=0.0912, atr_period=14)
    say(bot, '/positions')
    assert ('ATR at entry</b>  <code>0.0960  (TP by ATR, SL by ATR)'
            in bot.api.texts()[-1])
    say(bot, '/status')
    assert 'ATR(14)</b>  <code>0.0912' in bot.api.texts()[-1]


# -- Stat_Arb_W3's layout on the screens -----------------------------------------------


def test_status_opens_each_ladder_with_w3s_rows(bot):
    block = bot.desk.snapshot['pairs'][KEY]['algo_block']
    block['z_mid'] = 1.2345
    block['filters'].update(half_life_candles=296.5, half_life_minutes=4447,
                            regime={'on': True, 'state': 'MEAN_REVERTING'})
    holding(bot)
    say(bot, '/status')
    text = bot.api.texts()[-1]
    assert '<b>SYSTEM STATUS  ·  ' in text
    in_order(text, ['Algo</b>  <code>Enabled', 'Mode</b>  <code>Signals',
                    'Position</b>  <code>H to L 0.50',
                    'Z-score</b>  <code>+1.2345',
                    'Regime</b>  <code>mean reverting',
                    'Half-Life</b>  <code>296.5 periods  (4,447 min)'])


def test_the_CONTROL_a_ladder_with_the_algo_off_says_disabled_and_flat(bot):
    bot.desk.snapshot['pairs'][KEY]['algo_on'] = False
    say(bot, '/status')
    text = bot.api.texts()[-1]
    assert 'Algo</b>  <code>Disabled' in text
    assert 'Position</b>  <code>flat' in text


def test_end_of_day_is_w3s_report_per_ladder():
    snapshot = {'pairs': {KEY: row(algo_block={
        'z_mid': -0.5, 'day': {'trades': 2, 'pnl': 4.1, 'losses_row': 0},
        'filters': {'regime': {'state': 'TRENDING'}}})}}
    text = tg.AlertWatch._summary(snapshot, 1_800_000_000)
    assert text.startswith('<b>END OF DAY  ·  2027-01-15 08:00 UTC</b>')
    in_order(text, ['Trades', 'PnL</b>  <code>$+4.10', 'Equity',
                    'Unrealized', 'Position</b>  <code>flat',
                    'Z-score</b>  <code>-0.5000',
                    'Regime</b>  <code>trending'])


def test_an_unmeasured_gross_is_a_dash_not_a_zero(bot):
    bot.desk.report['journal'] = [dict(TRIP, gross_pnl=None, fees=None)]
    say(bot, '/pnl')
    text = bot.api.texts()[-1]
    assert 'All-time Gross</b>  <code>—' in text
    assert 'All-time Fees</b>  <code>—' in text
    # The net was measured, and is still said.
    assert 'All-time Net</b>  <code>$+3.85' in text


def test_the_dashboard_shows_the_whole_re_entry_window(bot):
    row = bot.desk.snapshot['pairs'][KEY]
    row['algo_params'] = dict(row['algo_params'], reentry_on=True,
                              reentry_back=0.5)
    say(bot, '/status')
    assert 'Enters at</b>  <code>z +/-1.50 to +/-0.75 (way back)' \
        in bot.api.texts()[-1]


def test_set_the_re_entry_window(bot):
    say(bot, '/set reentry_window_pct 60')
    press(bot, 'Confirm')
    assert bot.desk.saves[-1][1]['algo_params']['reentry_window_pct'] == 60
    say(bot, '/set reentry_window_pct 0')            # the control: refused
    assert 're-entry window' in bot.api.texts()[-1]


def test_a_trade_exit_falls_back_to_each_legs_own_close_price(bot):
    holding(bot)
    watch(bot)
    legs = {leg: dict(POSITION[leg], exit_price=price) for leg, price in
            (('leg_a', 88.95), ('leg_b', 101.75))}
    closed = dict(POSITION, closed_at=POSITION['opened_at'] + 60,
                  exit_spread=12.80, realized_pnl=0.1,
                  close_reason='Algo: profit target (after costs)', **legs)
    # The fills journal has nothing for it yet.
    bot.desk.records['P7'] = {'ok': True, 'position': closed,
                              'exit_prices': {'leg_a': None, 'leg_b': None}}
    bot.desk.snapshot['pairs'][KEY]['positions'] = []
    [told] = watch(bot)
    assert 'Leg A Exit</b>  <code>$88.9500' in told
    assert 'Leg B Exit</b>  <code>$101.7500' in told


# -- each side's own exits ------------------------------------------------------------


def test_set_a_sides_own_stop_and_clear_it_back(bot):
    say(bot, '/set atr_stop_mult_sell 3')
    press(bot, 'Confirm')
    assert bot.desk.saves[-1][1]['algo_params']['atr_stop_mult_sell'] == 3
    say(bot, '/set atr_stop_mult_sell same')            # back to the ladder's
    press(bot, 'Confirm')
    assert bot.desk.saves[-1][1]['algo_params']['atr_stop_mult_sell'] is None


def test_set_a_sides_own_sizing_mode(bot):
    say(bot, '/set stop_mode_buy atr')
    press(bot, 'Confirm')
    assert bot.desk.saves[-1][1]['algo_params']['stop_mode_buy'] == 'ATR'
    say(bot, '/set stop_mode_buy same')
    press(bot, 'Confirm')
    assert bot.desk.saves[-1][1]['algo_params']['stop_mode_buy'] is None


def test_a_sides_zero_is_refused_not_saved_as_zero(bot):
    say(bot, '/set tp_pct_buy 0')
    assert 'must be above 0' in bot.api.texts()[-1]
    assert bot.desk.saves == []


def test_the_settings_list_has_the_exits_per_side(bot):
    say(bot, '/settings')
    listing = '\n'.join(bot.api.texts()[-3:])
    assert 'EXITS PER SIDE' in listing.upper()
    assert 'stop_mode_sell' in listing and 'tp_pct_buy' in listing
    assert 'same as ladder' in listing


# -- OKX ----------------------------------------------------------------------

PERP = {'account': 'okx', 'inst': 'BTC-USDT-SWAP', 'pos_side': 'short',
        'pos': 1.0, 'avg': 65000.0, 'mark': 65100.0, 'liq': 90000.0,
        'liq_dist_pct': 38.2, 'lever': 3.0, 'upl': -1.0}


def test_next_funding_is_the_next_of_okxs_three_daily_settlements():
    # 1_800_000_000 is 08:00:00 UTC exactly: the 08:00 one has passed.
    assert tg.next_funding(1_800_000_000.0) == ('16:00', 480.0)
    assert tg.next_funding(1_800_000_000.0 - 300) == ('08:00', 5.0)
    assert tg.next_funding(1_800_000_000.0 + 16 * 3600 - 60)[0] == '00:00'


def test_funding_is_announced_once_when_perps_are_held(bot):
    bot.clock_.now = 1_800_000_000.0 - 3600
    bot.desk.snapshot['okx_positions'] = [PERP]
    watch(bot)                                   # bearings
    bot.clock_.now = 1_800_000_000.0 - 300       # 5 min to 08:00 UTC
    told = watch(bot)
    assert any('OKX FUNDING' in t and '08:00 UTC' in t and
               'BTC-USDT-SWAP' in t for t in told)
    bot.clock_.now += 60
    assert not any('OKX FUNDING' in t for t in watch(bot))


def test_the_CONTROL_no_perps_held_no_funding_alert(bot):
    bot.clock_.now = 1_800_000_000.0 - 3600
    bot.desk.snapshot['okx_positions'] = [dict(PERP, inst='BTC-USD_UM-261225')]
    watch(bot)
    bot.clock_.now = 1_800_000_000.0 - 300
    assert not any('OKX FUNDING' in t for t in watch(bot))


def test_a_position_near_liquidation_is_pushed_and_repeated_hourly(bot):
    watch(bot)
    bot.desk.snapshot['okx_positions'] = [dict(PERP, liq=68000.0,
                                               liq_dist_pct=4.5)]
    told = watch(bot)
    assert any('LIQUIDATION RISK' in t and '4.5%' in t for t in told)
    assert not any('LIQUIDATION' in t for t in watch(bot))
    bot.clock_.now += tg.LIQUIDATION_REPEAT_SEC + 1
    assert any('LIQUIDATION RISK' in t for t in watch(bot))


def test_the_dashboard_says_24_7_funding_and_the_maker_fill(bot):
    bot.desk.snapshot['broker_clock'] = {'broker_time': '08:00:00',
                                         'cutoff': '24/7'}
    row = bot.desk.snapshot['pairs'][KEY]
    row['session'] = {'close': '24/7', 'state': 'OPEN'}
    row['fill_style'] = 'maker'
    row['maker_fills'] = [{'phase': 'resting', 'age_sec': 4, 'legs': [
        {'leg': 'A', 'filled': 0, 'planned': 1},
        {'leg': 'B', 'filled': 1, 'planned': 2}]}]
    say(bot, '/status')
    text = bot.api.texts()[-1]
    assert 'around the clock (24/7)' in text
    assert 'Next funding' in text and '16:00 UTC' in text
    assert 'open 24/7 (OKX, UTC)' in text
    assert 'maker both legs' in text
    assert 'both legs resting as maker: A 0.00/1.00 · B 1.00/2.00' in text


def test_balance_lists_okx_positions_with_their_liquidation(bot):
    bot.desk.snapshot['accounts'] = {'okx': {
        'login': '448', 'server': 'OKX demo', 'currency': 'USD',
        'equity': 1000.0, 'margin_free': 900.0, 'margin': 100.0,
        'margin_level': 250.0, 'margin_so_call': 300.0, 'margin_so_so': 100.0,
        'profit': 0.0, 'balance': 1000.0}}
    bot.desk.snapshot['okx_positions'] = [PERP]
    say(bot, '/balance')
    text = bot.api.texts()[-1]
    assert 'OKX WARNING' in text
    assert 'BTC-USDT-SWAP short' in text and '38.2% away' in text


def test_status_names_all_three_fills():
    """Every Fill but 'maker' used to read 'taker (both legs at
    market)' - a Maker + taker ladder included."""
    from okxtrader import telegram as tg
    import inspect
    source = inspect.getsource(tg)
    for words in ('maker both legs', 'maker + taker', 'taker both legs'):
        assert words in source


def test_a_trade_exit_says_when_its_close_was_sent_and_how_long_it_took():
    """'Orders at - / Latency -' on every exit: the close's send time
    was not kept. It is now (exit_sent_at)."""
    from okxtrader import telegram as tg
    record = {'position': {'side': 'BUY', 'opened_at': 1000.0,
                           'closed_at': 1062.25, 'exit_sent_at': 1060.0,
                           'entry_spread': -12.0, 'exit_spread': -10.0,
                           'spread_units': 0.175, 'quantity': 5,
                           'realized_pnl': 0.1, 'close_reason': 'x'},
              'entry_z': -1.5, 'exit_z': -0.05}
    text = tg.trade_exit_text(record, 'ETH vs BTC')
    assert 'Latency  2250 ms' in text.replace('</b>', '').replace('<b>', '') \
        or '2250 ms' in text
    assert '-1.5000 -&gt; -0.0500' in text or '-1.5000 -> -0.0500' in text


def test_a_maker_entry_keeps_the_z_its_signal_was_taken_at():
    """The Algo's maker entries reported 'Z-score -': the position
    arrives later, and the z was only kept for one booked on the spot."""
    import types
    from okxtrader.algodesk import AlgoDesk
    desk = AlgoDesk.__new__(AlgoDesk)
    run = types.SimpleNamespace(mine=set(), entry_z={}, pending_z=-1.52)
    desk._runs = {'K': run}
    desk.adopt('K', 'POS1')
    assert run.entry_z['POS1'] == -1.52 and run.pending_z is None


def test_a_webhook_on_the_bot_is_cleared_so_commands_are_read():
    """Live: alerts arrived, every command went unanswered. A webhook
    on the bot makes Telegram refuse getUpdates (409), while sending
    still works. The bot clears it and reads commands again."""
    from okxtrader import telegram as tg

    class Api:
        def __init__(self):
            self.calls, self.webhook = [], True

        def call(self, method, **p):
            self.calls.append(method)
            if method == 'deleteWebhook':
                self.webhook = False
                return True
            if method == 'getUpdates' and self.webhook:
                raise tg.TelegramError("getUpdates: HTTP 409 Conflict: can't "
                                       "use getUpdates method while webhook "
                                       "is active; use deleteWebhook")
            return []
    api = Api()
    bot = tg.Bot(api, None, {1})
    try:
        bot.poll_once(timeout=0)
    except tg.TelegramError:
        pass
    assert 'deleteWebhook' in api.calls
    assert bot.poll_once(timeout=0) == 0            # reading now


def test_another_program_on_the_same_bot_is_named_in_the_log(caplog):
    from okxtrader import telegram as tg

    class Api:
        def call(self, method, **p):
            raise tg.TelegramError('getUpdates: HTTP 409 Conflict: terminated '
                                   'by other getUpdates request')
    bot = tg.Bot(Api(), None, {1})
    import logging
    with caplog.at_level(logging.ERROR):
        try:
            bot.poll_once(timeout=0)
        except tg.TelegramError:
            pass
    assert 'ANOTHER PROGRAM' in caplog.text


def test_the_status_carries_the_mean_the_z_is_measured_from(bot):
    """The band's middle (the Algo's EMA) and its sigma, under the
    z-score: where the spread reverts to."""
    assert tg.mean_words({'mean': -17.5834, 'sigma': 4.2612}) == \
        '-17.583  (σ 4.26)'
    assert tg.mean_words({}) == '—'
    snapshot = {'engine': 'up', 'pairs': {KEY: row(algo_block={
        'state': 'WATCHING', 'z_mid': 0.74, 'mean': -11.869,
        'sigma': 1.66, 'warmup': {'done': True, 'need_sec': 60}})}}
    bot.desk.snapshot = snapshot
    say(bot, '/status')
    text = bot.api.texts()[-1]
    assert 'Mean</b>  <code>-11.869  (σ 1.66)' in text


# -- size and margin on the status ------------------------------------------------


def test_the_status_says_each_holdings_size_and_margin(bot):
    holding(bot)
    bot.desk.snapshot['pairs'][KEY]['leg_a_account'] = 'main'
    bot.desk.snapshot['accounts'] = {'main': {'equity': 3500.0,
                                              'currency': 'USDT'}}
    say(bot, '/status')
    text = bot.api.texts()[-1]
    # 0.05 x 1000 x 89.08 + 0.05 x 1000 x 102.276; 191.50 x 0.5 Qty.
    in_order(text, ['Holding</b>  <code>H to L 0.50',
                    'Size</b>  <code>$9,567.80 (both legs)',
                    'Margin</b>  <code>$95.75  (100x)  · 2.7% of equity',
                    'Entry', 'Now', 'P&amp;L</b>'])


def test_the_status_says_the_algos_next_trade_size(bot):
    pair = bot.desk.snapshot['pairs'][KEY]
    pair['leg_notional'] = {'a': 4454.0, 'b': 5113.8}
    pair['algo_params'].update(algo_qty=2.0)
    say(bot, '/status')
    assert 'Trade size</b>  <code>2.00 Qty · $19,136' in bot.api.texts()[-1]
    pair['algo_sizing'] = {'by': 'MARGIN', 'margin': 1700.0,
                           'per_qty': 383.0, 'qty': 4.43}
    pair['algo_params'].update(add_on=True, add_size_by='MARGIN',
                               add_margin=600.0, add_qty=1.56)
    say(bot, '/status')
    text = bot.api.texts()[-1]
    assert ('Trade size</b>  <code>4.43 Qty · $42,385  (from $1,700 margin)'
            in text)
    assert 'Add size</b>  <code>1.56 Qty · $14,926  (from $600 margin)' in text


def test_the_trade_entry_says_its_size_and_margin(bot):
    watch(bot)
    holding(bot)
    bot.desk.snapshot['pairs'][KEY]['leg_a_account'] = 'main'
    bot.desk.snapshot['accounts'] = {'main': {'equity': 3500.0}}
    [told] = watch(bot)
    in_order(told, ['Size</b>  <code>0.50 Qty · $9,567.80 (both legs)',
                    'Notional',
                    'Margin Req</b>  <code>$95.75  (100x)  · 2.7% of equity'])


def test_a_trade_entry_whose_fills_are_unpriced_takes_the_algos_figures():
    position = {'position_id': 'P9', 'side': 'BUY', 'quantity': 2.0,
                'leg_a': {}, 'leg_b': {}}
    text = tg.trade_entry_text(position, 'ETH vs BTC',
                               {'notional': 40000.0, 'margin': 2000.0},
                               equity=10000.0)
    assert 'Size</b>  <code>2.00 Qty · $40,000.00 (both legs)' in text
    assert 'Margin Req</b>  <code>$2,000.00  · 20.0% of equity' in text


def test_the_status_says_each_instruments_price():
    rows = tg.leg_price_rows({
        'symbol_a': 'BTC-USDT-SWAP', 'symbol_b': 'ETH-USDT-SWAP',
        'market': {'leg_a_bid': 87420.5, 'leg_a_ask': 87420.6,
                   'leg_b_bid': 2925.41, 'leg_b_ask': 2925.42}})
    assert rows == [('BTC', '$87,420.55  (87,420.50 / 87,420.60)'),
                    ('ETH', '$2,925.41  (2,925.41 / 2,925.42)')]
    # A book not read yet says nothing rather than a price of nothing.
    assert tg.leg_price_rows({'market': {}}) == []


def test_the_status_puts_the_prices_by_the_spread(bot):
    row = bot.desk.snapshot['pairs'][KEY]
    row.update(symbol_a='USOIL', symbol_b='UKOIL',
               market={'leg_a_bid': 89.171, 'leg_a_ask': 89.223,
                       'leg_b_bid': 102.503, 'leg_b_ask': 102.536})
    say(bot, '/status')
    in_order(bot.api.texts()[-1], ['USOIL</b>  <code>$89.197  (89.171 / 89.223)',
                                   'UKOIL</b>  <code>$102.519',
                                   'H to L</b>'])


def test_the_status_says_the_spread_at_which_each_side_arms_and_enters(bot):
    row = bot.desk.snapshot['pairs'][KEY]
    row['algo_params'] = dict(row['algo_params'], reentry_on=True,
                              entry_z=1.8, reentry_back=0.2,
                              reentry_window_pct=50)
    row['algo_block'].update(mean=-22.395, sigma=2.63,
                             armed={'BUY': True})
    say(bot, '/status')
    text = bot.api.texts()[-1]
    # mean + z x sigma: 1.8 to arm, 1.6 to 0.8 to come back in.
    in_order(text, ['Arms at</b>  <code>z +/-1.80',
                    'Arms H to L</b>  <code>-17.661 and above',
                    'Arms L to H</b>  <code>-27.129 and below',
                    'Enters at</b>  <code>z +/-1.60 to +/-0.80',
                    'Enters H to L</b>  <code>-18.187 to -20.291</code>',
                    'Enters L to H</b>  <code>-26.603 to -24.499  (armed)',
                    'Armed</b>  <code>L to H'])
    # No band yet: the z's alone, no made-up prices.
    row['algo_block'].update(mean=None)
    say(bot, '/status')
    assert 'Arms H to L' not in bot.api.texts()[-1]


# -- the economic calendar ------------------------------------------------------

def _cal(now, status='upcoming', at_in=600):
    return {'alert_min': 15.0, 'events': [{
        'id': 'cpi:2027-01-15', 'release': 'cpi', 'name': 'CPI',
        'agency': 'BLS', 'at': now + at_in, 'status': status,
        'released_at': now if status == 'released' else None,
        'figures': [{'key': 'cpi_mm', 'name': 'CPI m/m',
                     'actual': '0.4%' if status == 'released' else None,
                     'previous': '0.3%', 'forecast': '0.3%',
                     'surprise': ('0.1 pt above forecast'
                                  if status == 'released' else None)}]}]}


def test_a_release_is_announced_ahead_and_again_when_it_lands():
    clock = type('C', (), {'at': 1_800_000_000.0,
                                     '__call__': lambda self: self.at})()
    watch = tg.AlertWatch(clock=clock)
    snapshot = {'engine': 'up', 'pairs': {KEY: row(short_spread=-31.124)}}
    # Bearings: nothing said on the first look.
    assert watch.check(dict(snapshot, calendar=_cal(clock.at, at_in=3600)),
                       [], lambda r: None) == []
    told = watch.check(dict(snapshot, calendar=_cal(clock.at)), [],
                       lambda r: None)
    [soon] = [t for t in told if 'ECONOMIC EVENT' in t]
    assert 'ECONOMIC EVENT IN 10 MIN' in soon
    assert 'US CPI (BLS)' in soon
    assert 'forecast 0.3% · previous 0.3%' in soon
    # Said once.
    assert not [t for t in watch.check(dict(snapshot, calendar=_cal(
        clock.at)), [], lambda r: None) if 'ECONOMIC' in t]
    clock.at += 600
    told = watch.check(dict(snapshot, calendar=_cal(clock.at, 'released',
                                                    0)), [], lambda r: None)
    [out] = [t for t in told if 'RELEASED' in t]
    assert 'CPI m/m</b>  <code>0.4%  (previous 0.3%, forecast 0.3%)' in out
    assert 'Surprise</b>  <code>0.1 pt above forecast' in out
    assert 'Oil</b>  <code>-31.124' in out


def _okx_fills(complete=True):
    legs = {'leg_a': {'open_price': 89.05, 'close_price': 89.31,
                      'gross': 5.2, 'fees': 0.9, 'maker': 2, 'taker': 0},
            'leg_b': {'open_price': 101.9, 'close_price': 102.41,
                      'gross': -1.1, 'fees': 0.7, 'maker': 1, 'taker': 1}}
    return {'legs': legs, 'gross': 4.1, 'fees': 1.6, 'net': 2.5,
            'complete': complete, 'maker': 3, 'taker': 1}


def test_a_trade_exit_reports_okxs_own_fills_and_fees(bot):
    holding(bot)
    watch(bot)
    closed = dict(POSITION, closed_at=POSITION['opened_at'] + 600,
                  exit_spread=13.119, realized_pnl=3.85,
                  close_reason='Algo: profit target (after costs)')
    bot.desk.records['P7'] = {'ok': True, 'position': closed,
                              'exit_prices': {'leg_a': 89.2, 'leg_b': 102.3},
                              'okx': _okx_fills()}
    bot.desk.snapshot['pairs'][KEY]['positions'] = []
    [told] = watch(bot)
    in_order(told, ['Leg A Entry</b>  <code>$89.0500',
                    'Leg A Exit</b>  <code>$89.3100',
                    'Leg B Entry</b>  <code>$101.9000',
                    'Leg B Exit</b>  <code>$102.4100',
                    'Gross PnL</b>  <code>$+4.1000  (OKX, both legs)',
                    'Fees</b>  <code>-$1.6000  (OKX, both legs · '
                    '3 maker / 1 taker fills)',
                    'Net PnL</b>  <code>$+2.5000'])
    assert 'Est. Fees' not in told


def test_a_trade_exit_waits_for_okxs_fills_then_goes_without_them(bot):
    holding(bot)
    watch(bot)
    closed = dict(POSITION, closed_at=POSITION['opened_at'] + 600,
                  exit_spread=13.119, realized_pnl=3.85,
                  close_reason='Algo: profit target (after costs)')
    bot.desk.records['P7'] = {'ok': True, 'position': closed,
                              'exit_prices': {'leg_a': 89.2, 'leg_b': 102.3},
                              'okx': _okx_fills(complete=False)}
    bot.desk.snapshot['pairs'][KEY]['positions'] = []
    assert watch(bot) == []                     # the fills are not in yet
    bot.desk.records['P7']['okx'] = _okx_fills()
    [told] = watch(bot)
    assert 'Net PnL</b>  <code>$+2.5000' in told


def test_a_trade_exit_without_okxs_fills_goes_after_the_wait(bot):
    holding(bot)
    watch(bot)
    closed = dict(POSITION, closed_at=POSITION['opened_at'] + 600,
                  exit_spread=13.119, realized_pnl=3.85,
                  close_reason='Algo: profit target (after costs)')
    bot.desk.records['P7'] = {'ok': True, 'position': closed,
                              'exit_prices': {'leg_a': 89.2, 'leg_b': 102.3},
                              'okx': _okx_fills(complete=False)}
    bot.desk.snapshot['pairs'][KEY]['positions'] = []
    assert watch(bot) == []
    bot.clock_.now += tg.EXIT_WAIT_SEC + 1
    [told] = watch(bot)
    assert 'Est. Fees' in told and 'not on record yet' in told
    assert 'Net PnL</b>  <code>$+3.8500' in told
    assert watch(bot) == []                     # told once
