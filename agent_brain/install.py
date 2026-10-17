"""Wire the brain into Claude Code and Codex, and map a second machine's paths.

`install-hooks` adds a SessionStart hook (injects routed context) and a Stop hook (captures the final reply as
unreviewed evidence) to ~/.claude/settings.json and/or ~/.codex/hooks.json. Existing hooks and settings are
kept, nothing is added twice, and each file is backed up before it is changed.

`map-machine` records, for this machine, which local prefix stands for each canonical prefix stored in
handoffs and routes.json, so one brain repo can be shared between machines with different folder layouts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import sys
from datetime import datetime

MATCHER = 'startup|resume|clear|compact'
ENGINE = Path(__file__).resolve().parent / 'continuity.py'


def backup(path):
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.bak-' + datetime.now().strftime('%Y%m%d-%H%M%S')))


def hook_commands(root):
    base = f'"{sys.executable}" "{ENGINE}" --brain "{root}"'
    return f'{base} hook-start', f'{base} hook-stop'


def add_hooks(path, start, stop, dry_run=False):
    data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    hooks = data.setdefault('hooks', {})
    added = []
    for event, command in (('SessionStart', start), ('Stop', stop)):
        groups = hooks.setdefault(event, [])
        if any('agent_brain' in h.get('command', '') and ENGINE.name in h.get('command', '')
               for g in groups for h in g.get('hooks', [])):
            continue
        group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 30}]}
        if event == 'SessionStart':
            group = {'matcher': MATCHER, **group}
        groups.append(group)
        added.append(event)
    if added and not dry_run:
        backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')
    return added


def install_hooks(root, claude=True, codex=True, dry_run=False):
    start, stop = hook_commands(root)
    out = dict(commands=dict(start=start, stop=stop), dry_run=dry_run)
    if claude:
        home = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        out['claude'] = add_hooks(home / 'settings.json', start, stop, dry_run) or 'already present'
    if codex:
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        out['codex'] = add_hooks(home / 'hooks.json', start, stop, dry_run) or 'already present'
        out['codex_note'] = 'Codex asks you to trust new hooks once (/hooks in Codex).'
    return out


def map_machine(root, pairs, name=None):
    name = (name or os.environ.get('BRAIN_MACHINE') or socket.gethostname()).casefold()
    mapping = {}
    for pair in pairs:
        canon, _, local = pair.partition('=')
        if not canon or not local:
            raise ValueError(f'Bad --map {pair!r}; use CANONICAL=LOCAL')
        mapping[canon.rstrip('/\\')] = str(Path(os.path.expanduser(local)).resolve())
    path = Path(root) / 'machines.json'
    data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {
        'note': 'Handoffs and routes.json store canonical paths. Each machine maps canonical prefix -> local prefix. '
                'The key is the lowercased hostname, or BRAIN_MACHINE if set.', 'machines': {}}
    data.setdefault('machines', {})[name] = dict(map=mapping, registered=datetime.now().strftime('%Y-%m-%d'))
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')
    return dict(machine=name, map=mapping, path=str(path))
