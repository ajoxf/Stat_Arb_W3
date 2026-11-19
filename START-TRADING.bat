@echo off
REM  MT5-Trader - double-click this to start trading.
REM
REM  It does the whole start: finds a Python, brings the dependencies up
REM  to date, checks the machine can actually connect, runs the safety
REM  tests, starts the engine and opens the ladders in your browser.
REM
REM  It does NOT change the version of the code on this machine. That is
REM  deploy\UPDATE.BAT, run deliberately by whoever maintains it.
REM
REM  Two rules it will not bend:
REM
REM    * It never starts the engine on a failing test suite. That rule
REM      is what keeps a bad build away from a live account.
REM
REM    * It never leaves the trader with a black window. The dependency
REM      check needs the internet and the office internet goes down, so
REM      that failure warns and carries on with what is already
REM      installed. Only a fault that would make trading WRONG stops
REM      the start.
REM
REM  Plain ASCII on purpose: a console running the default code page
REM  turns anything else into mojibake in the one message that matters.

setlocal
title MT5-Trader
cd /d "%~dp0"
color 0F

echo.
echo   =====================================================
echo     MT5-Trader - starting up
echo   =====================================================
echo.

REM --- 1. Python -------------------------------------------------------
REM  Three ways a working Python turns up on these boxes, and all three
REM  are normal: the py launcher from a python.org install, a plain
REM  "python" on PATH (a conda or venv prompt has this and NO launcher),
REM  or nothing at all. Assuming the launcher is what tells a machine
REM  that already has Python that it has none.
REM  FOUR ways a working Python turns up on these boxes, and all four
REM  are normal.
REM
REM  The launcher by exact version first: py -3.11 on a machine that
REM  also has 3.9 on PATH is the good outcome, and looking at PATH
REM  first would miss it. Then 3.14, which rollout.json also allows and
REM  the suite has passed on - asking only for 3.11 told a perfectly
REM  good 3.14 desk it had no Python at all. Then py -3 for a version
REM  nobody listed here. Then plain "python", which is what a conda or
REM  venv prompt has and a python.org install without the launcher.
REM
REM  A test with NO assert in it, deliberately. "python" on a bare
REM  Windows is the Microsoft Store stub, which exits non-zero without
REM  running anything, so a plain import is enough to tell a real
REM  interpreter from the stub - and what version it is gets decided
REM  below, where it can be SAID rather than hidden in an exit code.
set "PY="
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.11"
if not defined PY (
  py -3.14 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY=py -3.14"
)
if not defined PY (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo   [X] No Python was found on this machine.
  echo.
  echo       Install Python 3.11, 64-bit, from python.org and tick
  echo       "Add python.exe to PATH" during the install. If you use
  echo       conda, open the prompt that has your environment active
  echo       and run this file from there.
  echo.
  pause
  exit /b 1
)
echo   Using Python: %PY%

REM --- Is it a Python this can actually trade through? ------------------
REM  32-BIT IS FATAL. MetaTrader5's IPC handshake fails against a 32-bit
REM  Python with an error that says nothing, and the symptom on the desk
REM  is a leg that reads unknown forever. Starting would be worse than
REM  refusing, because it looks like it worked.
REM
REM  AN UNTESTED VERSION IS A WARNING, not a refusal. It may well be
REM  fine, the safety tests are the real gate, and stopping a trader at
REM  9am over a version number the suite then passes would be the wrong
REM  trade-off.
REM
REM  chr(80) is "P" - there is no quote character inside the quoted
REM  argument, because cmd would end the argument on it.
%PY% -c "import struct,sys;sys.exit(0 if struct.calcsize(chr(80))*8==64 else 1)" >nul 2>&1
if errorlevel 1 (
  echo   [X] This machine's Python is 32-bit.
  echo.
  echo       MetaTrader 5 will not talk to it: the handshake fails with
  echo       an error that says nothing, and the leg simply never
  echo       connects. Install Python 3.11 64-bit from python.org and
  echo       run this again.
  echo.
  pause
  exit /b 1
)
%PY% -c "import sys;sys.exit(0 if sys.version_info[:2] in ((3,11),(3,14)) else 1)" >nul 2>&1
if errorlevel 1 (
  echo   [!] This Python is not one of the versions this build has been
  echo       tested on ^(3.11 and 3.14^). Carrying on.
)

REM --- 2. Configuration -------------------------------------------------
REM
REM  NOTE: this file does NOT update itself. Nothing is pulled here on
REM  purpose - a desk's version changes when somebody DECIDES it
REM  changes, not because a commit landed overnight. deploy\UPDATE.BAT
REM  is that decision, run deliberately on the machine being updated.
REM
REM  The cost of the choice, so it is not a surprise: a fix pushed today
REM  is not on this PC tomorrow. Whoever maintains it visits the machine.
if not exist config.json (
  echo   [i] First run: creating config.json from the example.
  copy /y config.example.json config.json >nul
)
if not exist .env (
  copy /y .env.example .env >nul
)

REM --- 3. Dependencies --------------------------------------------------
REM  Also allowed to fail. What matters is not whether pip could reach
REM  the internet, it is whether the imports the engine needs are HERE.
echo   Checking dependencies...
%PY% -m pip install --quiet --disable-pip-version-check --no-warn-script-location -r requirements.txt >nul 2>&1
if errorlevel 1 (
  %PY% -c "import flask, dotenv, MetaTrader5" >nul 2>&1
  if errorlevel 1 (
    echo   [X] The dependencies are not installed and could not be
    echo       fetched. Check this machine's internet connection and
    echo       run this again.
    pause
    exit /b 1
  )
  echo   [!] Could not check for newer dependencies - the ones already
  echo       installed are complete, so carrying on.
)

REM --- 4. Can this machine actually connect? ----------------------------
REM  Counted, not guessed. An account that names its own MT5 folder is
REM  OPENED AND SIGNED IN by the engine, so a terminal that is not
REM  running is not a reason to refuse - preflight.py knows which case
REM  this config is and says so in words.
set "TERMINALS=0"
for /f %%c in ('tasklist /fi "imagename eq terminal64.exe" /nh 2^>nul ^| find /c /i "terminal64.exe"') do set "TERMINALS=%%c"

REM  An older clone has no preflight.py. Skipping it is right: it is a
REM  CHECK, and a missing check must not be the thing that stops a
REM  trader working. The safety tests below are the gate that matters.
if not exist deploy\preflight.py (
  echo   [i] No preflight in this copy - skipping the connection check.
) else (
  %PY% deploy\preflight.py --config config.json --terminals-running %TERMINALS%
  if errorlevel 1 goto :refused
)
goto :tests

:refused
echo.
echo   [X] The engine has NOT been started - see above.
echo.
pause
exit /b 1

:tests

REM --- 5. The safety tests ---------------------------------------------
echo   Running the safety tests (about 20 seconds)...
%PY% -m pytest tests_okx -q
if errorlevel 1 (
  echo.
  echo   [X] THE SAFETY TESTS FAILED. The engine has NOT been started.
  echo.
  echo       If this machine was updated recently, the fault most
  echo       likely arrived with that update - deploy\UPDATE.BAT can
  echo       put it back on the version it had before.
  echo.
  echo       Do not trade on this build. Send the lines above to whoever
  echo       maintains it.
  echo.
  pause
  exit /b 1
)

REM --- 6. Go ------------------------------------------------------------
echo.
echo   All checks passed. Starting the engine and opening the ladders.
echo   Leave this window open - closing it stops trading.
echo.
%PY% start.py --config config.json

echo.
echo   The engine has stopped. Any positions you had are still at the
echo   broker; open the terminals to see them.
pause
