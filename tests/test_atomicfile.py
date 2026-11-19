"""Replacing a file, on the platform where that is not free.

Every file this system publishes goes through a tmp file and a rename,
so a reader never catches half a document. On Windows that rename fails
outright while any other process has the destination open — and the web
process reads the status snapshot several times a second. Unretried,
the collision failed the whole poll and froze the screen with the
market moving.
"""

import json
import os

import pytest

from okxtrader import atomicfile


def test_a_collision_is_waited_out_rather_than_raised(tmp_path):
    """The Windows case, modelled: the first attempts are refused, one
    later attempt succeeds, and the caller never sees an exception."""
    target = tmp_path / 'status.json'
    target.write_text('old', encoding='utf-8')
    source = tmp_path / 'status.json.tmp'
    source.write_text('new', encoding='utf-8')

    attempts = {'n': 0}
    real = os.replace

    def flaky(src, dst):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise PermissionError(5, 'Access is denied')
        return real(src, dst)

    original, atomicfile.os.replace = atomicfile.os.replace, flaky
    try:
        ok = atomicfile.replace(str(source), str(target), delay=0.0)
    finally:
        atomicfile.os.replace = original

    assert ok is True
    assert attempts['n'] == 3
    assert target.read_text(encoding='utf-8') == 'new'


def test_a_file_that_never_frees_up_leaves_the_OLD_one_intact(tmp_path):
    """Anything outlasting the retries is not a collision — it is a
    scanner, a backup agent or a read-only file. The destination then
    keeps the PREVIOUS document: a half-written one is never the
    alternative."""
    target = tmp_path / 'status.json'
    target.write_text('old', encoding='utf-8')
    source = tmp_path / 'status.json.tmp'
    source.write_text('new', encoding='utf-8')

    def always_denied(src, dst):
        raise PermissionError(5, 'Access is denied')

    original, atomicfile.os.replace = atomicfile.os.replace, always_denied
    try:
        ok = atomicfile.replace(str(source), str(target), attempts=3,
                                delay=0.0)
    finally:
        atomicfile.os.replace = original

    assert ok is False
    assert target.read_text(encoding='utf-8') == 'old'
    assert source.exists()          # the next pass overwrites it


def test_a_real_permissions_fault_is_not_mistaken_for_a_collision(tmp_path):
    """It still comes back False and is logged — but the point of the
    test is that nothing is written to the destination in the attempt."""
    target = tmp_path / 'status.json'
    source = tmp_path / 'status.json.tmp'
    source.write_text('new', encoding='utf-8')

    def always_denied(src, dst):
        raise PermissionError(13, 'Permission denied')

    original, atomicfile.os.replace = atomicfile.os.replace, always_denied
    try:
        assert atomicfile.replace(str(source), str(target), attempts=2,
                                  delay=0.0) is False
    finally:
        atomicfile.os.replace = original
    assert not target.exists()


def test_the_engine_keeps_publishing_through_a_locked_reader(config, pair,
                                                              legs, tmp_path):
    """End to end: a poll whose replace is refused does not fail the
    poll. The screen shows the previous snapshot and its age, which is
    what the staleness banner is for."""
    from okxtrader.coordinator import Coordinator
    status = tmp_path / 'status.json'
    coordinator = Coordinator(config, legs, status_path=str(status))
    coordinator.start()
    coordinator.poll_once()
    coordinator.publish()
    first = json.loads(status.read_text(encoding='utf-8'))

    def always_denied(src, dst):
        raise PermissionError(5, 'Access is denied')

    original, atomicfile.os.replace = atomicfile.os.replace, always_denied
    try:
        coordinator.poll_once()
        coordinator.publish()            # must not raise
    finally:
        atomicfile.os.replace = original

    assert json.loads(status.read_text(encoding='utf-8'))['at'] == first['at']
    coordinator.publish()                # and it recovers by itself
    assert json.loads(status.read_text(encoding='utf-8'))['at'] > first['at']


# -- the other half: not blocking the writer in the first place ------------

def test_a_read_asks_windows_for_share_delete(tmp_path, monkeypatch):
    """The retries exist for scanners. THIS is the fix for us.

    Windows refuses a rename over a file another process has open, and
    the share mode is what decides it — Python's `open()` asks for read
    and write sharing but not FILE_SHARE_DELETE, so every poll of
    `status.json` held the coordinator's publish off. Asking for delete
    sharing lets the publish land underneath a reader that goes on
    reading the bytes it already has.
    """
    path = tmp_path / 'status.json'
    path.write_text('{"at": 1}', encoding='utf-8')
    asked = {}

    def fake_open(name, encoding='utf-8'):
        asked['name'] = name
        return open(name, 'r', encoding=encoding)

    monkeypatch.setattr(atomicfile.os, 'name', 'nt')

    body = atomicfile.read_text(str(path), open_shared=fake_open)

    assert body == '{"at": 1}'
    assert asked['name'] == str(path)
    # And the flags the Windows opener actually passes.
    assert atomicfile.SHARE_ALL & atomicfile.FILE_SHARE_DELETE
    assert atomicfile.SHARE_ALL & atomicfile.FILE_SHARE_READ
    assert atomicfile.SHARE_ALL & atomicfile.FILE_SHARE_WRITE


def test_a_share_delete_open_that_fails_still_reads_the_file(tmp_path,
                                                             monkeypatch):
    """A worse share mode is a collision the retries then handle. A
    screen that cannot read the snapshot at all is a screen with nothing
    behind it."""
    path = tmp_path / 'status.json'
    path.write_text('{"at": 2}', encoding='utf-8')

    def refuses(name, encoding='utf-8'):
        raise OSError(5, 'Access is denied')

    monkeypatch.setattr(atomicfile.os, 'name', 'nt')

    assert atomicfile.read_text(str(path), open_shared=refuses) == '{"at": 2}'


def test_a_missing_file_is_missing_and_not_an_empty_snapshot(tmp_path):
    """"No file" and "a file that says nothing" are different, and the
    screen says different things about them."""
    missing = str(tmp_path / 'nothing.json')

    with pytest.raises(FileNotFoundError):
        atomicfile.read_text(missing)
    assert atomicfile.read_json(missing, 'the default') == 'the default'


def test_a_half_written_file_reads_as_the_default_not_as_a_snapshot(tmp_path):
    """It should never happen — that is what the tmp file is for — but
    if it does, a truncated snapshot must not be served as a real one."""
    path = tmp_path / 'status.json'
    path.write_text('{"at": 1, "pairs": {', encoding='utf-8')

    assert atomicfile.read_json(str(path), None) is None
