"""What happens to open positions when the process stops (spec §12).

Closing at market is irreversible; a position left open is recovered on
the next start. So an UNANSWERED prompt means NO — no tty, a timeout,
or a second Ctrl+C all resolve the same way, and the reader runs on a
daemon thread so an unanswered prompt cannot hold the process open.
"""

import logging
import sys
import threading


def prompt_text(positions):
    """The prompt, spelled out — an operator deciding this under time
    pressure needs the consequences on the screen, not in a manual."""
    lines = [f"  SHUTTING DOWN with {len(positions)} OPEN POSITION"
             f"{'S' if len(positions) != 1 else ''}", ""]
    for position in positions:
        lines.append(
            f"    {position.pair_key}  {position.side.value} "
            f"{position.quantity:g} @ {position.entry_spread}")
    lines += [
        "",
        "  y  close them now, at market",
        "  N  leave them open at the broker — no engine, until you "
        "start up again",
        "",
    ]
    return '\n'.join(lines)


def should_close(positions, mode='ask', timeout=20.0, stream=None,
                 reader=None):
    """True only when the operator (or the setting) actually said yes.

    `mode` is the `SHUTDOWN_CLOSE_POSITIONS` setting: 'ask' / 'always' /
    'never'.
    """
    mode = str(mode or 'ask').lower()
    if not positions:
        return False
    if mode == 'always':
        return True
    if mode == 'never':
        return False

    stream = stream or sys.stdout
    stream.write(prompt_text(positions))
    stream.flush()

    if reader is None:
        if not sys.stdin or not sys.stdin.isatty():
            # No tty: nobody can answer, so the answer is NO.
            logging.warning("no tty to ask on — leaving %d position(s) open "
                            "at the broker", len(positions))
            return False
        reader = sys.stdin.readline

    answer = []

    def read():
        try:
            answer.append(reader())
        except Exception:                       # stdin closed under us
            pass

    # Daemon, so an unanswered prompt cannot hold the process open.
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout)
    if not answer:
        logging.warning("no answer in %gs — leaving %d position(s) open at "
                        "the broker", timeout, len(positions))
        return False
    return answer[0].strip().lower() in ('y', 'yes')
