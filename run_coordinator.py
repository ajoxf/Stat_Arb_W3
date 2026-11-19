"""Start the coordinator: two feeds in, one spread per pair out.

    python run_coordinator.py --config config.json

Connects to each account's leg runner, fuses their ticks into a spread
per configured pair, and publishes one status snapshot the ladders, the
Market Grid and the positions monitor all render from.
"""

import argparse
import logging
import signal
import sys
import threading

from okxtrader.commands import CommandRunner
from okxtrader.config import TraderConfig
from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader import logsetup
from okxtrader.legs import RemoteLeg
from okxtrader.shutdown import should_close


def build_legs(config, retries=3, delay=1.0):
    """One RemoteLeg per account with an endpoint.

    An account without one has no runner: it cannot serve a leg, and
    saying so here beats a connection error three layers down.

    Accounts that do not answer are simply absent. They are NOT fatal:
    see `main`.
    """
    legs = {}
    for name, account in config.accounts.items():
        if not account.endpoint:
            logging.error(
                "account '%s' has no endpoint — give it one (e.g. "
                "127.0.0.1:9101) and start its leg runner", name)
            continue
        leg = RemoteLeg(name, account.endpoint)
        if leg.connect(retries=retries, delay=delay):
            legs[name] = leg
    return legs


def leg_factory(config):
    """Try ONCE to bring up a leg that was not there before.

    The coordinator calls this on a slow clock for every configured
    account it has no runner for, so a terminal started late — or
    restarted at lunchtime — is picked up without anybody restarting
    the engine.
    """
    def connect(name):
        account = config.accounts.get(name)
        if account is None or not account.endpoint:
            return None
        leg = RemoteLeg(name, account.endpoint)
        return leg if leg.connect(retries=1, delay=0.0) else None
    return connect


def main():
    parser = argparse.ArgumentParser(description='MT5-Trader coordinator')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--db', default='okxtrader.db')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [coord] %(message)s',
        handlers=[logging.StreamHandler()])
    # ROTATING, and in logs/ beside the other processes. The plain
    # FileHandler that stood here grew without limit and sat loose in
    # the repo root, so it was both a disk risk and the only process
    # that logged at all.
    logsetup.setup('coordinator')

    config = TraderConfig.from_file(args.config)
    legs = build_legs(config)
    if not legs:
        # NOT fatal, and this is the whole point of the change. Exiting
        # here meant the launcher restarted the coordinator, it exited
        # again, and around it went — while the web process went on
        # serving the LAST status file it had. Live 2026-08-31 that
        # file was 19 hours old: three ladders of yesterday's prices,
        # ages reading 12.5s, and nothing on the screen able to say the
        # engine had never come up.
        #
        # So it runs anyway. It publishes a snapshot that says which
        # accounts are dark, the screen goes honest instead of stale,
        # and every few seconds it tries the runners again — so a
        # terminal started late is picked up without a restart.
        logging.error(
            'no leg runner answered yet — starting anyway, and retrying. '
            'Start them with: python run_leg.py --config %s --account '
            '<name>', args.config)

    # The database is what makes a restart safe: without it the book
    # comes back empty and every live position looks like an orphan.
    coordinator = Coordinator(config, legs, status_path=args.status,
                              store=Store(args.db),
                              leg_factory=leg_factory(config))
    # PRIME before the first drain. Everything already in the command
    # file was written to a process that is gone; replaying it would
    # place those orders again, now, at today's prices.
    runner = CommandRunner(coordinator, args.commands, args.results)
    runner.prime()
    coordinator.commands = runner

    def stop(signum, frame):
        # Positions first, because closing them is irreversible and an
        # unanswered prompt must mean NO (spec §12). The pending sweep
        # runs either way — a pending of ours left resting can fill
        # unhedged with nobody watching.
        open_positions = coordinator.book.positions()
        if should_close(open_positions,
                        config.get('SHUTDOWN_CLOSE_POSITIONS', 'ask')):
            for position in open_positions:
                coordinator.executor.close_position(
                    config.pairs[position.pair_key], position,
                    coordinator.market.get(position.pair_key),
                    reason='shutdown')
        report = coordinator.stop()
        if report['failed'] or report['unknown']:
            logging.critical('SHUTDOWN SWEEP INCOMPLETE: %s', report)
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    # Clicks are drained on their own thread so one never waits for the
    # next poll: the whole product is that a click is an order.
    threading.Thread(target=coordinator.serve_commands, daemon=True).start()
    coordinator.run()


if __name__ == '__main__':
    main()
