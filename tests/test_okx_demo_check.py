"""tools/okx_demo_check.py - the check traders run against their OKX
demo account - run here, whole, against the simulated OKX over HTTP."""

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from okxtrader import okx_sim

ROOT = Path(__file__).resolve().parent.parent


def _port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _run(tmp_path, demo=True, port=None):
    (tmp_path / 'config.json').write_text(json.dumps({
        'accounts': {'okx': {'exchange': 'okx', 'demo': demo,
                             'base_url': f'http://127.0.0.1:{port or 9}'}},
        'pairs': {}, 'settings': {}}))
    (tmp_path / '.env').write_text(
        'OKX_API_KEY="k"\nOKX_SECRET_KEY="s"\nOKX_PASSPHRASE="p"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith('OKX_')}
    done = subprocess.run(
        [sys.executable, str(ROOT / 'tools' / 'okx_demo_check.py'),
         '--config', str(tmp_path / 'config.json')],
        capture_output=True, text=True, timeout=240, env=env)
    return done.stdout + done.stderr, \
        (tmp_path / 'okx_demo_check_report.txt').read_text()


def test_the_whole_check_passes_against_the_simulated_okx(tmp_path):
    port = _port()
    server = okx_sim.serve(port, maker_fill_prob=0.6)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        out, report = _run(tmp_path, port=port)
    finally:
        server.shutdown()
        server.sim.stop()
    assert 'FAIL' not in report, report
    for step in ('account checklist', 'close by ticket reduces',
                 'closing it again sends nothing', 'post-only order never',
                 'W3 maker spread fill and maker close',
                 'spot buy and close', 'fills journal', 'clean-up'):
        assert f'PASS  {step}' in report, (step, report)
    assert 'demo-key' not in report and '"k"' not in report


def test_it_refuses_a_live_account(tmp_path):
    out, report = _run(tmp_path, demo=False)
    assert 'FAIL  account is DEMO' in report
    assert 'market order' not in report            # nothing was sent
