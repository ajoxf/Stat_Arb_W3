"""Rename an account everywhere it is referenced. Run with the launcher STOPPED.

    python tools/rename_account.py LegA "AC:100006"
    python tools/rename_account.py LegB "AC:100002"

An account name is not a label: it is the key in config.json, the name
every pair leg routes by, and the stem of the password's key in .env.
Renaming it by hand means editing all three consistently, and a missed
pair leg is a launcher that will not start.

Nothing is written until every change is known, both files are backed
up (.rename.bak), and the new name is checked for a clash.
"""

import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxtrader.config import env_key_for                      # noqa: E402


def rename(config_path, env_path, old, new):
    with open(config_path, encoding='utf-8') as handle:
        raw = json.load(handle)

    accounts = raw.get('accounts') or {}
    if old not in accounts:
        raise SystemExit(f"no account named {old!r}. Have: "
                         f"{', '.join(accounts) or '(none)'}")
    if new in accounts:
        raise SystemExit(f"an account named {new!r} already exists")
    if not new.strip():
        raise SystemExit('the new name is empty')

    old_key, new_key = env_key_for(old), env_key_for(new)
    changes = [f'accounts: {old!r} -> {new!r}']

    # Rebuild in order: the file stays readable, rows keep their places.
    raw['accounts'] = {(new if name == old else name): body
                       for name, body in accounts.items()}
    if raw['accounts'][new].get('password_env') == old_key:
        raw['accounts'][new]['password_env'] = new_key
        changes.append(f'  password_env: {old_key} -> {new_key}')

    for key, pair in (raw.get('pairs') or {}).items():
        for side in ('leg_a', 'leg_b'):
            leg = (pair or {}).get(side) or {}
            if leg.get('account') == old:
                leg['account'] = new
                changes.append(f'pair {key}: {side}.account -> {new!r}')

    env_text = None
    if os.path.exists(env_path):
        with open(env_path, encoding='utf-8') as handle:
            env_text = handle.read()
        # Only a line that IS that key, never a substring of another.
        pattern = re.compile(rf'(?m)^(\s*(?:export\s+)?){re.escape(old_key)}(\s*=)')
        env_text, hits = pattern.subn(rf'\1{new_key}\2', env_text)
        if hits:
            changes.append(f'.env: {old_key} -> {new_key} ({hits} line)')

    print('\n'.join(changes))

    shutil.copy2(config_path, config_path + '.rename.bak')
    with open(config_path, 'w', encoding='utf-8') as handle:
        json.dump(raw, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
    if env_text is not None:
        shutil.copy2(env_path, env_path + '.rename.bak')
        with open(env_path, 'w', encoding='utf-8') as handle:
            handle.write(env_text)

    print(f'\nDone. Backups: {os.path.basename(config_path)}.rename.bak'
          + (', .env.rename.bak' if env_text is not None else ''))
    print('Restart the launcher — accounts are structural.')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rename(os.path.join(here, 'config.json'),
           os.path.join(here, '.env'), sys.argv[1], sys.argv[2])
