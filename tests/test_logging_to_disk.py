"""Logs on disk, for the morning after.

A trader reported that two legs of a live spread had been closed by
this system sixty seconds after they put them on. The reconciler said
so at CRITICAL, with the account, the ticket and the reason - into a
console window that had been closed. There was nothing on disk, so the
incident could not be investigated, only reasoned about.

Reasoning about money is not good enough. Every process writes to
logs/ now.
"""

import logging
import re
from pathlib import Path

from okxtrader import logsetup

ROOT = Path(__file__).resolve().parent.parent


def _clean():
    logger = logging.getLogger()
    for handler in list(logger.handlers):
        if getattr(handler, '_okxtrader_log', None):
            logger.removeHandler(handler)
            handler.close()


def test_a_critical_line_reaches_a_file(tmp_path):
    _clean()
    try:
        path = logsetup.setup('coordinator', root=str(tmp_path))
        logging.critical('closed ORPHAN XAUUSD_ ticket 1001')
        assert path and Path(path).exists()
        assert 'ticket 1001' in Path(path).read_text(encoding='utf-8')
    finally:
        _clean()


def test_the_line_carries_the_time_to_the_millisecond(tmp_path):
    """Two legs filling in the same second is the ordinary case, and
    the ORDER of them is the whole question afterwards."""
    _clean()
    try:
        path = logsetup.setup('coordinator', root=str(tmp_path))
        logging.critical('a decision')
        text = Path(path).read_text(encoding='utf-8')
        assert re.search(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}', text)
        assert 'CRITICAL' in text
    finally:
        _clean()


def test_it_never_stops_a_process_from_starting(monkeypatch, tmp_path):
    """The control that matters most. A read-only folder, a full disk
    or a locked file means we lose the logs - stopping a trading
    process over it would be worse than the fault being logged.

    THE FAILURE IS INJECTED, not conjured from a path that happens to
    be unwritable on the machine running the tests. This test used to
    pass '/proc/definitely/not/writable', which is unwritable on Linux
    and a perfectly legal FOLDER NAME on Windows - so on the trading
    boxes makedirs simply created it, setup returned a path, the
    assertion failed, and the safety tests refused to let the engine
    start. A test that is wrong about the platform it protects is worse
    than no test: it stopped a desk from trading over a log file.
    """
    _clean()

    def explodes(*_args, **_kwargs):
        raise OSError('read-only file system')

    monkeypatch.setattr(logsetup.os, 'makedirs', explodes)
    assert logsetup.setup('x', root=str(tmp_path)) is None
    # And the process can still log - to its console, as before.
    logging.critical('still running')


def test_control_a_writable_folder_really_does_get_a_file(tmp_path):
    """The control for the control: the tolerance above must not be
    'never writes anything'."""
    _clean()
    try:
        assert logsetup.setup('x', root=str(tmp_path)) is not None
    finally:
        _clean()


def test_no_test_here_assumes_the_platform_it_is_run_on():
    """The suite runs on Linux in review and on Windows at every desk,
    where it GATES THE ENGINE. A posix-only path in an assertion is a
    trading box that will not start."""
    import re
    # An ABSOLUTE POSIX PATH PASSED AS AN ARGUMENT - not the word in a
    # comment or a docstring, which is how this failure gets explained.
    bad = re.compile(r"""(?:root|path|file)\s*=\s*['"]/""")
    for source in sorted((ROOT / 'tests').glob('test_*.py')):
        text = source.read_text(encoding='utf-8')
        hit = bad.search(text)
        assert not hit, f'{source.name}: {text[hit.start():hit.start() + 60]}'
        # Built rather than written out, or this line matches itself.
        rooted = 'Path(' + chr(39) + '/'
        assert rooted not in text, source.name


def test_control_calling_it_twice_does_not_double_every_line(tmp_path):
    _clean()
    try:
        first = logsetup.setup('coordinator', root=str(tmp_path))
        second = logsetup.setup('coordinator', root=str(tmp_path))
        assert first == second
        logging.critical('once')
        text = Path(first).read_text(encoding='utf-8')
        assert text.count('once') == 1
    finally:
        _clean()


def test_the_files_are_rotated_rather_than_growing_for_ever(tmp_path):
    """A trading box that stops because a log filled the volume is a
    worse fault than the one being logged."""
    _clean()
    try:
        path = logsetup.setup('coordinator', root=str(tmp_path),
                              max_bytes=2000, backups=2)
        for i in range(400):
            logging.critical('a reconciler decision number %d', i)
        names = [p.name for p in Path(path).parent.iterdir()]
        # Each file plus its backups: the full log and the problems log.
        for stem in ('coordinator.log', 'coordinator.problems.log'):
            files = sorted(n for n in names if n.startswith(stem)
                           and not n[len(stem):].lstrip('.')[:1].isalpha())
            assert len(files) <= 3, files
            assert stem in files
    finally:
        _clean()


def test_every_process_writes_one():
    """The launcher, the web server and the engine, each to its own
    file."""
    sources = {
        'start.py': "logsetup.setup('launcher')",
        'run_coordinator.py': "logsetup.setup('coordinator')",
        'okxtrader/webapp.py': "logsetup.setup('web')",
    }
    for name, expected in sources.items():
        text = (ROOT / name).read_text(encoding='utf-8')
        assert expected in text, name


def test_control_the_console_keeps_everything_it_had():
    """This ADDS a destination. A desk watching the black window must
    see exactly what it saw before."""
    for name in ('run_coordinator.py',):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert 'logging.StreamHandler()' in text, name
    # And the unbounded files that used to sit loose in the repo root
    # are gone - they were both a disk risk and hard to find.
    for name in ('run_coordinator.py',):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert "FileHandler('coordinator.log'" not in text, name
        assert "FileHandler(f'leg_" not in text, name


def test_problems_get_a_file_of_their_own(tmp_path):
    """The full log is the record; the problems file is the one to open.
    A warning lands in both, a routine line only in the full one."""
    _clean()
    try:
        path = logsetup.setup('coordinator', root=str(tmp_path))
        logging.info('a routine poll line')
        logging.warning('leg A stale for 30s')
        problems = Path(tmp_path, 'logs', 'coordinator.problems.log')
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = problems.read_text(encoding='utf-8')
        assert 'leg A stale' in text
        assert 'a routine poll line' not in text
        # The control: the full log has both.
        full = Path(path).read_text(encoding='utf-8')
        assert 'a routine poll line' in full and 'leg A stale' in full
    finally:
        _clean()


def test_the_screens_polling_is_not_logged_but_a_press_and_an_error_are(
        caplog):
    logsetup.quiet_polling()
    web = logging.getLogger('werkzeug')
    with caplog.at_level(logging.INFO, logger='werkzeug'):
        web.info('127.0.0.1 - - [x] "GET /api/status HTTP/1.1" 200 -')
        web.info('127.0.0.1 - - [x] "GET /static/app.js?v=1 HTTP/1.1" 304 -')
        # As the server really writes a 304: coloured.
        web.info('127.0.0.1 - - [x] "\x1b[36mGET /static/ladder.css?v=1 '
                 'HTTP/1.1\x1b[0m" 304 -')
        web.info('127.0.0.1 - - [x] "POST /api/command HTTP/1.1" 200 -')
        web.info('127.0.0.1 - - [x] "GET /api/status HTTP/1.1" 500 -')
    text = caplog.text
    assert 'GET /api/status HTTP/1.1" 200' not in text
    assert 'app.js' not in text
    assert 'ladder.css' not in text
    # The controls: an action and a failure are still there.
    assert 'POST /api/command' in text
    assert '" 500' in text
