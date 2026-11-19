"""The command bridge: written by the web process, executed once by the
coordinator, and never replayed.
"""

import pytest

from okxtrader.commands import CommandLog, CommandRunner
from okxtrader.coordinator import Coordinator
from okxtrader.models import OrderType, SpreadSide


@pytest.fixture
def bridge(config, pair, legs, tmp_path):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    runner = CommandRunner(coordinator, str(tmp_path / 'commands.jsonl'),
                           str(tmp_path / 'results.json'))
    log = CommandLog(str(tmp_path / 'commands.jsonl'))
    return coordinator, runner, log


def test_a_restart_does_not_replay_the_command_history(bridge, pair, legs):
    """Every watermark once initialised to 0, so a restart replayed the
    whole history in half a second — opening an unintended live position
    and placing real test orders."""
    coordinator, runner, log = bridge
    pair.order_type = OrderType.MARKET
    log.submit('click', {'pair': pair.key, 'side': 'BUY', 'level': 59.0})
    log.submit('click', {'pair': pair.key, 'side': 'BUY', 'level': 59.0})

    primed = runner.prime()
    done = runner.drain()

    assert primed == 2
    assert done == []
    assert legs['acct_a'].broker.sent == []      # nothing was sent
    assert legs['acct_b'].broker.sent == []


def test_a_command_written_after_startup_is_executed_once(bridge, pair, legs):
    """The control: priming must not swallow live commands too."""
    coordinator, runner, log = bridge
    pair.order_type = OrderType.MARKET
    runner.prime()
    log.submit('click', {'pair': pair.key, 'side': 'BUY', 'level': 59.5})

    first = runner.drain()
    second = runner.drain()

    assert len(first) == 1 and first[0]['ok']
    assert second == []                          # executed ONCE
    assert len(coordinator.book.positions(pair.key)) == 1


def test_draining_before_priming_is_refused(bridge):
    _coordinator, runner, log = bridge
    log.submit('click', {'pair': 'x', 'side': 'BUY', 'level': 1.0})
    with pytest.raises(RuntimeError, match='replay'):
        runner.drain()


def test_a_partial_line_is_skipped_not_guessed_at(bridge, tmp_path):
    """A half-written line is an order we do not have. It will be
    complete next pass."""
    _coordinator, runner, log = bridge
    runner.prime()
    with open(runner.log.path, 'a', encoding='utf-8') as f:
        f.write('{"id": "abc", "kind": "cli')
    assert runner.drain() == []


def test_an_unknown_command_is_an_error_not_a_crash(bridge):
    _coordinator, runner, log = bridge
    runner.prime()
    log.submit('sell_everything_now', {})
    done = runner.drain()
    assert done[0]['ok'] is False
    assert 'unknown command' in done[0]['error']


def test_a_click_through_the_bridge_is_the_same_order_as_a_direct_one(
        bridge, pair, legs):
    """The browser, the Market Grid and the ladder all reach the same
    code path — a test asserts it rather than hoping."""
    coordinator, runner, log = bridge
    runner.prime()
    log.submit('click', {'pair': pair.key, 'side': 'SELL', 'level': 58.5})
    done = runner.drain()

    through_bridge = done[0]['data']['order']
    direct = coordinator.click(pair.key, SpreadSide.SELL, 58.5)['order']
    for field in ('pair_key', 'side', 'level', 'quantity', 'order_type',
                  'time_in_force'):
        assert through_bridge[field] == direct[field]
    assert through_bridge['order_id'] != direct['order_id']   # two clicks


def test_cancel_and_the_global_kill_reach_every_ladder(bridge, pair, legs):
    coordinator, runner, log = bridge
    runner.prime()
    pair.max_positions = 0      # four working orders: the limit is not
                                # what this test is about
    for _ in range(3):
        coordinator.click(pair.key, SpreadSide.BUY, 58.4)
    coordinator.click(pair.key, SpreadSide.SELL, 58.9)

    log.submit('cancel_where', {'pair': pair.key, 'side': 'BUY'})
    assert runner.drain()[0]['data']['cancelled'] == 3
    assert coordinator.book.working_counts(pair.key) == (0, 1)

    log.submit('kill', {})
    assert runner.drain()[0]['data']['cancelled'] == 1
    assert coordinator.book.orders() == []


def test_the_kill_flattens_only_when_the_operator_confirmed(bridge, pair,
                                                            legs):
    coordinator, runner, log = bridge
    runner.prime()
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    coordinator.book.add_position(result.position)

    log.submit('kill', {})
    runner.drain()
    assert legs['acct_a'].broker.open_positions()      # still on

    log.submit('kill', {'flatten': True})
    runner.drain()
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []


def test_a_setting_changed_in_one_panel_is_the_same_setting_in_the_other(
        bridge, pair):
    coordinator, runner, log = bridge
    runner.prime()
    log.submit('set_pair', {'pair': pair.key,
                            'fields': {'order_type': 'MARKET',
                                       'overnight': 'EXIT_ALWAYS',
                                       'increment': 0.05}})
    done = runner.drain()

    assert done[0]['data']['applied']['order_type'] == 'MARKET'
    assert pair.order_type is OrderType.MARKET
    assert pair.overnight.value == 'EXIT_ALWAYS'
    assert pair.effective_increment() == 0.05
    # And the snapshot every panel renders from says the same.
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['order_type'] == 'MARKET' and row['increment'] == 0.05


def test_results_are_published_for_the_web_process_to_read(bridge, pair,
                                                            tmp_path):
    import json
    coordinator, runner, log = bridge
    runner.prime()
    command_id = log.submit('cancel_where', {'pair': pair.key})
    runner.drain()

    published = json.load(open(runner.results_path, encoding='utf-8'))
    assert published[command_id]['ok'] is True


def test_a_click_does_not_wait_for_the_next_poll(bridge, pair, legs):
    """The command thread drains far faster than the poll.

    Waiting for the poll would put up to a whole interval between the
    click and the order — on a product whose promise is that one click
    is one order. The prices a click acts on are the ones already
    published; nothing about it needs a fresh poll.
    """
    import threading
    import time

    coordinator, runner, log = bridge
    pair.order_type = OrderType.MARKET
    coordinator.commands = runner
    runner.prime()
    coordinator.config.settings['COMMAND_POLL_SEC'] = 0.005
    # A poll interval long enough that a click waiting for it would be
    # obvious — this is the fault under test.
    coordinator.config.settings['POLL_INTERVAL_SEC'] = 5.0

    thread = threading.Thread(target=coordinator.serve_commands, daemon=True)
    thread.start()
    try:
        started = time.time()
        log.submit('click', {'pair': pair.key, 'side': 'BUY', 'level': 59.5})
        deadline = started + 2.0
        while not coordinator.book.positions(pair.key) and time.time() < deadline:
            time.sleep(0.005)
        elapsed = time.time() - started
    finally:
        coordinator._stop.set()
        thread.join(timeout=1.0)

    assert coordinator.book.positions(pair.key), 'the click never executed'
    assert elapsed < 0.5, f'the click waited {elapsed:.2f}s for a poll'


def test_a_click_and_a_poll_do_not_read_the_book_at_the_same_time(bridge,
                                                                   pair):
    """One lock over the book and the executor: a poll reading the book
    while a click mutates it would publish a half-placed order."""
    coordinator, runner, log = bridge
    assert coordinator.lock is not None
    with coordinator.lock:
        # Re-entrant, so the coordinator's own nested calls still work.
        assert coordinator.click(pair.key, 'BUY', 58.4)['ok']
