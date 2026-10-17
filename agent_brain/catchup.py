"""Catch-up: one read-only answer to "what am I active on", for any session on any tool.

Used by `agent-brain catchup` and `agent-brain render-agents`. Standard library only; no model calls,
no network, nothing written except the rendered AGENTS.md when that feature is configured.

Sections: current handoff heads updated in the window (reviewed state), git branch / last commit / dirty
count per registered source repo, the latest Codex and Claude Code sessions on disk, and unreviewed Stop
captures. Everything runs in parallel with bounded timeouts so the command stays under about five seconds
even when a large repo's git status is slow.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import continuity as C
except ImportError:
    import continuity as C

GIT_TIMEOUT = 3.0
BUDGET = 3.8


def claude_projects():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'projects'


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def recent_heads(days, root):
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out = []
    for project in C.registry(root):
        folder = Path(root) / 'handoffs' / project
        if not folder.is_dir():
            continue
        for r in C.heads(project, root):
            if datetime.fromisoformat(r['created_at']).timestamp() < cutoff:
                continue
            out.append(dict(project=project, task=r['task'], updated=r['created_at'][:16].replace('T', ' '),
                            overview=(r.get('overview') or '')[:300], remaining=r['remaining'][:6],
                            remaining_more=max(0, len(r['remaining']) - 6), parked=len(r.get('parked', [])),
                            path=str(folder / (r['id'] + '.json'))))
    out.sort(key=lambda x: x['updated'], reverse=True)
    return out


def repo_status(repo):
    def git(*args):
        return subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=GIT_TIMEOUT)
    try:
        if git('rev-parse', '--git-dir').returncode != 0:
            return None
        branch = git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
        last = git('log', '-1', '--format=%cs %s').stdout.strip()[:110]
        try:
            dirty = len(git('status', '--porcelain', '--untracked-files=no').stdout.splitlines())
        except subprocess.TimeoutExpired:
            dirty = f'status took over {GIT_TIMEOUT:g} s; run git status there'
        return dict(repo=repo, branch=branch, last_commit=last, dirty_tracked=dirty)
    except (OSError, subprocess.SubprocessError) as error:
        return dict(repo=repo, error=str(error)[:120])


def source_repos(root):
    seen, out = set(), []
    for key, spec in C.registry(root).items():
        for alias in spec['paths']:
            if alias['role'] != 'source':
                continue
            local = C.to_local(alias['path'], root)
            n = C.normalized(local)
            if n in seen or not Path(local).is_dir():
                continue
            seen.add(n)
            out.append((key, local))
    return out


def _strip_tags(text):
    text = re.sub(r'<[^>]+>', ' ', text or '')
    return re.sub(r'\s+', ' ', text).strip()


def codex_sessions(days, limit=12):
    home = codex_home()
    cutoff_ms = (datetime.now(timezone.utc).timestamp() - days * 86400) * 1000
    rows = []
    try:
        dbs = sorted(home.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
        if not dbs:
            raise FileNotFoundError('no state_*.sqlite')
        con = sqlite3.connect(f'file:{dbs[0].as_posix()}?mode=ro', uri=True, timeout=1)
        try:
            query = ('select rollout_path, title, cwd, updated_at_ms, first_user_message from threads '
                     'where archived = 0 and updated_at_ms > ? order by updated_at_ms desc limit ?')
            for rp, title, cwd, updated, first in con.execute(query, (cutoff_ms, limit)):
                rows.append(dict(tool='codex', when=datetime.fromtimestamp(updated / 1000).strftime('%Y-%m-%d %H:%M'),
                                 cwd=(cwd or '').replace('\\\\?\\', ''), title=_strip_tags(title or first)[:90],
                                 transcript=(rp or '').replace('\\\\?\\', '')))
        finally:
            con.close()
        return rows
    except Exception as error:  # the DB belongs to Codex; on any failure list rollout files by time instead
        fallback = []
        sessions = home / 'sessions'
        if sessions.is_dir():
            cutoff = datetime.now().timestamp() - days * 86400
            for f in sessions.glob('*/*/*/rollout-*.jsonl'):
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if m >= cutoff:
                    fallback.append(dict(tool='codex', when=datetime.fromtimestamp(m).strftime('%Y-%m-%d %H:%M'),
                                         cwd='', title='', transcript=str(f)))
            fallback.sort(key=lambda x: x['when'], reverse=True)
        if not fallback and not home.is_dir():
            return []
        return fallback[:limit] + [dict(tool='codex', note=f'state DB unreadable ({str(error)[:60]}); listed by file time')]


def claude_sessions(days, limit=12):
    base = claude_projects()
    if not base.is_dir():
        return []
    cutoff = datetime.now().timestamp() - days * 86400
    found = []
    for folder in base.iterdir():
        if not folder.is_dir():
            continue
        for f in folder.glob('*.jsonl'):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m >= cutoff:
                found.append((m, f))
    found.sort(reverse=True)
    rows = []
    for m, f in found[:limit]:
        cwd = title = first_user = ''
        try:
            with open(f, 'rb') as handle:
                head = handle.read(98304).decode('utf-8', errors='replace').splitlines()
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 65536))
                tail = handle.read().decode('utf-8', errors='replace').splitlines()
            for line in head:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                cwd = cwd or row.get('cwd') or ''
                if not first_user and row.get('type') == 'user' and not row.get('isSidechain'):
                    content = (row.get('message') or {}).get('content')
                    text = content if isinstance(content, str) else ' '.join(
                        c.get('text', '') for c in (content or []) if isinstance(c, dict) and c.get('type') == 'text')
                    text = re.sub(r'\s+', ' ', text).strip()
                    if text and not text.startswith('<'):
                        first_user = text[:90]
            for line in reversed(tail):
                if '"custom-title"' in line:
                    try:
                        title = json.loads(line).get('customTitle') or ''
                        break
                    except ValueError:
                        continue
        except OSError:
            pass
        rows.append(dict(tool='claude', when=datetime.fromtimestamp(m).strftime('%Y-%m-%d %H:%M'), cwd=cwd,
                         title=(title or first_user)[:90], transcript=str(f)))
    return rows


def captures(days, root):
    cutoff = datetime.now().timestamp() - days * 86400
    out = {}
    base = Path(root) / 'captures'
    if base.is_dir():
        for folder in base.iterdir():
            if not folder.is_dir():
                continue
            n = sum(1 for f in folder.glob('*.json') if f.stat().st_mtime >= cutoff)
            if n:
                out[folder.name] = n
    return out


def catchup(days=7, root=C.ROOT, git=True):
    started = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=12)
    futures = dict(heads=pool.submit(recent_heads, days, root),
                   codex=pool.submit(codex_sessions, days), claude=pool.submit(claude_sessions, days),
                   captures=pool.submit(captures, days, root))
    repo_futures = [(key, pool.submit(repo_status, path)) for key, path in (source_repos(root) if git else [])]
    result = dict(days=days, generated=datetime.now().strftime('%Y-%m-%d %H:%M'), brain=str(root),
                  note='Read-only. Handoffs are reviewed state; captures are unreviewed Stop evidence; '
                       'sessions are transcripts on disk.')
    for name, fut in futures.items():
        try:
            result[name] = fut.result(timeout=max(0.2, BUDGET - (time.monotonic() - started)))
        except Exception as error:
            result[name] = dict(error=str(error)[:120])
    repos = []
    for key, fut in repo_futures:
        try:
            status = fut.result(timeout=max(0.2, BUDGET - (time.monotonic() - started)))
        except Exception as error:
            status = dict(repo=key, error=f'not finished in time ({type(error).__name__})')
        if status:
            status['project'] = key
            repos.append(status)
    result['repos'] = repos
    pool.shutdown(wait=False, cancel_futures=True)
    result['seconds'] = round(time.monotonic() - started, 2)
    return result


def render(result):
    lines = [f"CATCH-UP, last {result['days']} days, {result['generated']} ({result['seconds']} s). Brain: {result['brain']}",
             result['note'], '']
    heads = result.get('heads') if isinstance(result.get('heads'), list) else []
    lines.append(f'## Current handoff heads updated in the window ({len(heads)})')
    if isinstance(result.get('heads'), dict):
        lines.append(f"  {result['heads'].get('error')}")
    for h in heads:
        total = len(h['remaining']) + h['remaining_more']
        lines.append(f"- [{h['project']}] {h['task']}  ({h['updated']}; {total} remaining, {h['parked']} parked)")
        if h['overview']:
            lines.append(f"    {h['overview']}")
        for item in h['remaining']:
            lines.append(f"    - {item[:220]}")
        if h['remaining_more']:
            lines.append(f"    - ... {h['remaining_more']} more in {h['path']}")
    lines += ['', f"## Repos ({len(result.get('repos', []))}; dirty = modified tracked files, untracked not counted)"]
    for r in result.get('repos', []):
        if r.get('error'):
            lines.append(f"- {r['project']}: {r['repo']}  {r['error']}")
        else:
            lines.append(f"- {r['project']}: {r['repo']}  [{r['branch']}] dirty {r['dirty_tracked']}  last: {r['last_commit']}")
    for tool, label in (('codex', 'Codex sessions'), ('claude', 'Claude Code sessions')):
        rows = result.get(tool) if isinstance(result.get(tool), list) else []
        lines += ['', f"## Latest {label} ({len([r for r in rows if 'when' in r])})"]
        if isinstance(result.get(tool), dict):
            lines.append(f"  {result[tool].get('error')}")
        for r in rows:
            if 'note' in r:
                lines.append(f"  {r['note']}")
            else:
                lines.append(f"- {r['when']}  {r['cwd']}  {r['title']}")
    caps = result.get('captures') or {}
    caps_text = ', '.join(f'{k} {v}' for k, v in sorted(caps.items())) if isinstance(caps, dict) and caps else 'none'
    lines += ['', f'## Unreviewed Stop captures in the window: {caps_text}']
    lines += ['', 'Next: current --project <id> for one project, then read its handoffs and their evidence.']
    return '\n'.join(lines)


def render_agents(root=C.ROOT):
    """Codex reads one global AGENTS.md and has no include syntax, while Claude Code can import a file with
    @path. To keep one copy of shared rules, configure in brain.json:

        "render_agents": {"sources": ["shared-rules.md", "agents.codex.md"], "target": "~/.codex/AGENTS.md"}

    Sources are relative to the brain folder. The target is rewritten only when a source is newer."""
    root = Path(root)
    spec = C.config(root).get('render_agents')
    if not spec:
        return dict(render_agents='skipped: not configured in brain.json')
    sources = [root / s for s in spec.get('sources', [])]
    target = Path(os.path.expanduser(spec['target']))
    missing = [str(s) for s in sources if not s.is_file()]
    if not sources or missing:
        return dict(render_agents=f'skipped: missing sources {missing}')
    newest = max(s.stat().st_mtime for s in sources)
    if target.is_file() and target.stat().st_mtime >= newest:
        return dict(render_agents='current')
    names = ' and '.join(s.name for s in sources)
    body = (f'<!-- GENERATED by agent-brain render-agents from {names} in {root}.\n'
            '     Edit those files, never this one; the SessionStart hook re-renders it. -->\n\n'
            + '\n\n'.join(s.read_text(encoding='utf-8-sig').rstrip() for s in sources) + '\n')
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(body, encoding='utf-8', newline='\n')
    tmp.replace(target)
    return dict(render_agents=f'rewrote {target} ({len(body)} chars)')
