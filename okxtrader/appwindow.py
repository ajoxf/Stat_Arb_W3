"""Open the terminal in a window of its own, not a browser tab.

The trader double-clicks an icon and the terminal is there. No address
bar, no tab strip, no bookmark to a port that changed — and, more to the
point on a screen that sends live orders:

- **A tab can be closed by accident.** An app window has one thing in
  it, and closing it is a deliberate act rather than a stray ctrl-W
  aimed at the tab beside it.
- **Extensions do not run here.** `--user-data-dir` puts this window in
  a profile of its own, so whatever is installed in the trader's normal
  browsing profile — ad blockers, script injectors, a password manager
  that autofills the wrong form — is simply not present in the process
  showing the order entry screen.
- **The chrome is vertical space.** A tab strip and an address bar are
  about 70px, and this desk has spent real effort on rail height.

Edge is used because it is on every Windows 10 and 11 machine, so
nothing extra ships and there is no new build toolchain. Chrome takes
the identical flags and is tried next. If neither is found the ordinary
browser is opened instead and the trader is told why the window looks
different — a missing app window must never mean a missing terminal.
"""

import os
import shutil
import subprocess
import sys

#: The flag both Chromium browsers use for "one page, no browser UI".
APP_FLAG = '--app='

#: Where Edge and Chrome install on Windows. `shutil.which` finds
#: neither: they are not on PATH.
WINDOWS_CANDIDATES = (
    ('Microsoft', r'Microsoft\Edge\Application\msedge.exe'),
    ('Google', r'Google\Chrome\Application\chrome.exe'),
)


def find_browser(env=None, exists=os.path.exists, which=shutil.which):
    """The Chromium browser to host the window, or None.

    Edge first — it ships with Windows, so it is the one that is always
    there. Returns the executable path, or None when the trader has
    neither and an ordinary browser will have to do.
    """
    env = os.environ if env is None else env
    roots = [env.get('PROGRAMFILES(X86)'), env.get('PROGRAMFILES'),
             env.get('LOCALAPPDATA')]
    for _vendor, tail in WINDOWS_CANDIDATES:
        for root in roots:
            if not root:
                continue
            path = os.path.join(root, tail)
            if exists(path):
                return path
    # Linux and macOS, where the desk does not run but the tests do.
    for name in ('microsoft-edge', 'google-chrome', 'chromium'):
        found = which(name)
        if found:
            return found
    return None


def profile_dir(env=None):
    """A browser profile of THIS application's own.

    Not the trader's: an extension in their everyday profile would
    otherwise be running inside the page that sends orders.
    """
    env = os.environ if env is None else env
    base = (env.get('LOCALAPPDATA') or env.get('XDG_CACHE_HOME')
            or os.path.join(os.path.expanduser('~'), '.cache'))
    return os.path.join(base, 'Nexus', 'window')


def window_command(url, browser=None, env=None):
    """The argv that opens `url` as an app window, or None.

    None means no Chromium browser was found and the caller should fall
    back to `webbrowser.open`.
    """
    browser = browser or find_browser(env=env)
    if not browser:
        return None
    return [
        browser,
        APP_FLAG + url,
        '--user-data-dir=' + profile_dir(env=env),
        # It is a terminal, not a browsing session. Nothing here should
        # be offering to save a password, restore tabs from a crash, or
        # translate the ladder.
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-features=Translate,PasswordManagerOnboarding',
    ]


def open_window(url, browser=None, env=None, spawn=None, say=print,
                open_browser=None):
    """Open the terminal. Returns True when it got its own window.

    Never raises: a browser that will not start is a cosmetic problem,
    and the terminal is reachable at the URL either way. Saying which
    of the two happened matters, though — a trader who expected an app
    window and got a tab should know it is the browser that is missing
    and not the terminal.

    BOTH WAYS OUT ARE INJECTED — `spawn` for the app window and
    `open_browser` for the fallback. `spawn` always was; the fallback
    was not, and it called `webbrowser.open` directly.

    That is not a tidiness point. Two tests exercise this function's
    failure paths, and both of them end here, so every run of the
    safety tests opened two real browser tabs at 127.0.0.1:8000 — on a
    machine where nothing was serving that port yet, because the tests
    run BEFORE the engine starts. A trader watching SETUP finish got
    two 'This site can't be reached' tabs, then two more when NEXUS
    started and ran the suite again, and every one of them looked like
    the install had failed.

    A function whose fallback cannot be intercepted is a function that
    cannot be tested without doing the thing it does.
    """
    spawn = spawn or subprocess.Popen
    if open_browser is None:
        import webbrowser
        open_browser = webbrowser.open
    argv = window_command(url, browser=browser, env=env)
    if argv:
        try:
            spawn(argv)
            return True
        except Exception as e:
            say(f'[launcher] could not open the app window ({e}) — '
                f'falling back to the browser')
    else:
        say('[launcher] no Edge or Chrome found, so the terminal opens '
            'in your ordinary browser')
    try:
        open_browser(url)
    except Exception as e:                         # a headless box
        say(f'[launcher] could not open a browser ({e}) — browse to {url}')
    return False


def shortcut_target(url, install_dir=None, env=None):
    """What a desktop shortcut should run, as one command line.

    The installer writes this; it is here so the launcher and the
    shortcut cannot drift apart about what "open Nexus" means.
    """
    argv = window_command(url, env=env)
    if not argv:
        return None
    parts = []
    for arg in argv:
        parts.append(f'"{arg}"' if ' ' in arg else arg)
    return ' '.join(parts)


if __name__ == '__main__':                          # a manual check
    print(shortcut_target(sys.argv[1] if len(sys.argv) > 1
                          else 'http://127.0.0.1:8000/'))
