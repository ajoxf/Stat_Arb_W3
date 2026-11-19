"""One way to replace a file, and it has to work on Windows.

Every file this system publishes — the status snapshot, the command
results, the config — is written to a tmp file and then renamed over
the destination, so a reader never catches half a document.

On Windows that rename FAILS while any other process has the
destination open:

    PermissionError: [WinError 5] Access is denied:
        'status.json.tmp' -> 'status.json'

The web process reads `status.json` several times a second, so the
collision is not rare — it is the normal case under load, and when it
hits, the coordinator's whole poll fails and the screen goes stale
while the market moves. (POSIX has no such rule: a rename over an open
file is always allowed there, which is why this only ever shows up on
the trading box.)

There are two halves to fixing that, and both are here.

The first is on the READING side, and it is the real one. Windows
blocks the rename because of the share mode the reader opened the file
with — Python's `open()` asks for read and write sharing but NOT
`FILE_SHARE_DELETE`, and a rename over a file counts as a delete of the
name. So every read of `status.json` by the web process holds the
coordinator's publish off for as long as the read takes. `read_text()`
opens with `FILE_SHARE_DELETE` as well, and the publish then goes
through underneath it: the reader keeps reading the bytes it already
had a handle to, which is exactly the isolation the tmp-file dance
wanted in the first place.

The second is to WAIT and try again, for the collisions that are not
ours — an antivirus scanner opening the file the moment it changes, a
sync client uploading it. Those hold it for far longer than a reader
does, and no share mode of ours affects them. What is not acceptable is
either alternative:

- writing straight over the destination, which is exactly the
  half-read this tmp-file dance exists to prevent;
- giving up and letting the poll fail, which is what was happening.

`replace()` returns True when the file was replaced and False when it
could not be, after trying — the caller decides whether that is worth
saying out loud. It never raises on the collision it is here for.
"""

import errno
import logging
import os
import time

#: About a fifth of a second of retries. A reader holds the file for
#: microseconds; anything that outlasts this is not a collision — it is
#: an antivirus scanner, a backup agent, or a file that has been made
#: read-only, and none of those is fixed by waiting longer.
ATTEMPTS = 20
DELAY_SEC = 0.01


def replace(tmp, path, attempts=ATTEMPTS, delay=DELAY_SEC, sleep=time.sleep):
    """`os.replace(tmp, path)`, retried through a Windows reader."""
    last = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError as e:
            # WinError 5 (access denied) and WinError 32 (in use) are
            # both "someone has it open"; a genuine permissions problem
            # raises the same code and simply keeps failing, which the
            # caller then reports.
            last = e
            if attempt + 1 < attempts:
                sleep(delay)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EBUSY):
                raise
            last = e
            if attempt + 1 < attempts:
                sleep(delay)
    # WARNING, not ERROR, and the distinction is the point of this
    # module. Reaching here is the designed fallback working: the
    # previous file is intact, the next poll publishes over it, and one
    # snapshot a cycle old is far inside the staleness threshold the
    # screen warns at. On a live desk this fires a few times an hour
    # against several thousand publishes — an antivirus scanner holding
    # the file for longer than a reader does — and logging that at
    # ERROR teaches a trader to ignore the level that means their money
    # is at risk.
    logging.warning('could not replace %s (%s). The file on disk is the '
                    'PREVIOUS one — nothing was half-written, and the next '
                    'publish writes over it. Frequent on this file means '
                    'something outside this system is holding it open, '
                    'usually a virus scanner.', path, last)
    return False


#: The Windows constants involved. Spelled out rather than imported,
#: because `msvcrt` and the rest of this only exist on the trading box
#: and the tests have to run everywhere.
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
#: The one that matters: without it, a reader blocks a rename over the
#: file it is reading.
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE


def read_text(path, encoding='utf-8', open_shared=None):
    """Read a file WITHOUT holding a writer's rename off.

    On anything but Windows this is `open()`: POSIX has never had the
    rule, and a rename over an open file is always allowed there.

    On Windows it goes through `CreateFileW` with `FILE_SHARE_DELETE`,
    which is the only way to ask for it — `open()` does not, and cannot
    be told to. Any failure to do that falls back to a plain read: a
    slightly worse share mode is a collision the retries then handle,
    and it must never be a file the screen cannot read at all.
    """
    opener = open_shared or _open_shared
    if os.name == 'nt':
        try:
            with opener(path, encoding) as f:
                return f.read()
        except FileNotFoundError:
            raise
        except Exception as e:                        # pragma: no cover
            logging.warning('%s could not be opened share-delete (%s) — '
                            'falling back to an ordinary read', path, e)
    with open(path, 'r', encoding=encoding) as f:
        return f.read()


def _open_shared(path, encoding='utf-8'):             # pragma: no cover
    """The Windows half of `read_text`. Returns an open text file."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create = ctypes.windll.kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING,
                    FILE_ATTRIBUTE_NORMAL, None)
    if handle == ctypes.c_void_p(-1).value or not handle:
        error = ctypes.get_last_error() or ctypes.GetLastError()
        if error in (2, 3):                           # not found / no path
            raise FileNotFoundError(error, os.strerror(errno.ENOENT), path)
        raise OSError(error, f'CreateFileW failed ({error})', path)
    # `open_osfhandle` takes ownership: closing the file closes the
    # handle, and there is exactly one close.
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    return os.fdopen(descriptor, 'r', encoding=encoding)


def read_json(path, default=None, encoding='utf-8', open_shared=None):
    """`read_text`, parsed. `default` for anything unreadable."""
    import json
    try:
        return json.loads(read_text(path, encoding, open_shared=open_shared))
    except (OSError, ValueError):
        return default


def write_json(path, payload, dumps=None):
    """Write a JSON document atomically, or leave the old one alone."""
    import json
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write((dumps or json.dumps)(payload))
    if not replace(tmp, path):
        # The tmp file is left where it is: the next pass overwrites it,
        # and a half-written destination is never the alternative.
        return False
    return True
