"""agent-brain: one shared, source-linked memory for coding agents.

Routes a working directory to a registered project, stores append-only handoffs that keep
completed / remaining / validated / released / constraints apart, and retrieves bounded evidence.
Python standard library only. No model calls, embeddings, network access or billing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone

FIELDS = ('completed', 'remaining', 'validated', 'released', 'constraints')
# Bounds for new handoffs. Without them one long-running task grew a single handoff to hundreds of KB
# with about a thousand evidence entries, because every update copied the whole history forward.
MAX_EVIDENCE = 40
MAX_LIST_ITEMS = 60
MAX_ITEM_CHARS = 1500
MAX_RECORD_BYTES = 128 * 1024
MAX_OVERVIEW_CHARS = 1200
ROLES = ('source', 'task', 'staging', 'worktree', 'historical')
DEFAULT_CONFIG = {
    'startup_files': ['facts.md', 'user.md'],
    'global_files': ['facts.md', 'user.md', 'notes.md'],
    'startup_budget': 8000,
    'active_days': 14,
    'git_sync': False,
    'render_agents': None,
}


def default_root():
    return Path(os.environ.get('AGENT_BRAIN_HOME') or Path.home() / '.agent-brain').expanduser()


ROOT = default_root()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def config(root=ROOT):
    path = Path(root) / 'brain.json'
    data = read_json(path) if path.exists() else {}
    return {**DEFAULT_CONFIG, **data}


def normalized(path):
    return ntpath.normcase(ntpath.normpath(str(path))).rstrip('\\')


def machine(root=ROOT):
    # Records and routes keep canonical paths (as on the first machine); machines.json maps them per host.
    path = Path(root) / 'machines.json'
    if not path.exists():
        return {}
    host = (os.environ.get('BRAIN_MACHINE') or socket.gethostname()).casefold()
    return read_json(path).get('machines', {}).get(host, {})


def _swap(path, pairs):
    p = normalized(path)
    for src, dst in sorted(pairs, key=lambda x: len(normalized(x[0])), reverse=True):
        s = normalized(src)
        if p == s or p.startswith(s + '\\'):
            rest = str(path)[len(str(src).rstrip('/\\')):].strip('/\\')
            sep = '\\' if '\\' in dst else '/'
            return dst.rstrip('/\\') + (sep + rest.replace('\\', '/').replace('/', sep) if rest else '')
    return str(path)


def to_canonical(path, root=ROOT):
    return _swap(path, [(local, canon) for canon, local in machine(root).get('map', {}).items()])


def to_local(path, root=ROOT):
    return _swap(path, list(machine(root).get('map', {}).items()))


def is_absolute(path):
    return Path(path).is_absolute() or ntpath.isabs(str(path)) or posixpath.isabs(str(path))


def anchored(path, root=ROOT):
    # Paths in routes.json may be relative to the brain folder, which keeps an example brain portable.
    return str(path) if is_absolute(path) else str((Path(root) / path).resolve())


def beneath(path, root):
    p, r = normalized(path), normalized(root)
    return p == r or p.startswith(r + '\\')


def registry(root=ROOT):
    projects = read_json(Path(root) / 'routes.json')['projects']
    resolved = {}
    for key, spec in projects.items():
        spec = dict(spec)
        spec['source'] = anchored(spec['source'], root)
        spec['paths'] = [dict(a, path=anchored(a['path'], root)) for a in spec.get('paths', [])]
        spec['evidence_files'] = [anchored(p, root) for p in spec.get('evidence_files', [])]
        resolved[key] = spec
    return resolved


def direct_route(cwd, root=ROOT):
    matches = [(len(normalized(a['path'])), key, a, p)
               for key, p in registry(root).items() for a in p['paths']
               if beneath(cwd, a['path'])]
    if not matches:
        return None
    matches.sort(key=lambda x: x[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0] and matches[0][1] != matches[1][1]:
        raise ValueError('Ambiguous route: repair routes.json before continuing')
    _, key, alias, project = matches[0]
    return dict(project=key, role=alias['role'], matched_path=alias['path'],
                source=project['source'], memory=project.get('memory'), cwd=str(cwd))


def route(cwd, root=ROOT):
    # Explicit task/staging aliases take precedence. Git discovers only real worktrees.
    found = direct_route(to_canonical(cwd, root), root)
    if found:
        return found
    if Path(cwd).is_dir():
        try:
            result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--path-format=absolute',
                                     '--git-common-dir', '--show-toplevel'],
                                    capture_output=True, text=True, timeout=2, check=True)
            common, top = result.stdout.strip().splitlines()
            original = Path(common).parent
            rel = Path(cwd).resolve().relative_to(Path(top).resolve())
            found = direct_route(to_canonical(original / rel, root), root)
            if found and normalized(top) != normalized(original):
                found.update(role='worktree', matched_path=top, cwd=str(cwd))
                return found
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return dict(project=None, role='unmapped', cwd=str(cwd))


def records(project, root=ROOT):
    return [read_json(p) for p in sorted((Path(root) / 'handoffs' / project).glob('*.json'))]


def archived(project, root=ROOT):
    return [read_json(p) for p in sorted((Path(root) / 'handoffs' / project / 'archive').glob('*.json'))]


def archive_superseded(project, root=ROOT):
    # Superseded handoffs move to handoffs/<project>/archive/. Nothing is deleted and search still
    # covers them, but routing and `current` no longer parse the whole history on every call.
    folder = Path(root) / 'handoffs' / project
    items = records(project, root)
    superseded = {i for r in items for i in r.get('supersedes', [])}
    moved = 0
    for r in items:
        if r['id'] in superseded:
            (folder / 'archive').mkdir(exist_ok=True)
            os.replace(folder / (r['id'] + '.json'), folder / 'archive' / (r['id'] + '.json'))
            moved += 1
    return moved


def is_active(record, days=DEFAULT_CONFIG['active_days']):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(record['created_at'])).days < days


def heads(project, root=ROOT):
    items = records(project, root)
    superseded = {i for r in items for i in r.get('supersedes', [])}
    return sorted([r for r in items if r['id'] not in superseded],
                  key=lambda r: (r['created_at'], r['id']), reverse=True)


def atomic_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique immutable filenames prevent concurrent tasks from overwriting each other.
    temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    try:
        if path.exists():
            raise ValueError(f'Record already exists: {path}')
        os.rename(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def save_handoff(data, root=ROOT):
    root = Path(root)
    project = data['project']
    if project not in registry(root):
        raise ValueError('Register the project first (add-project)')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', data['task']):
        raise ValueError('task must be a short stable slug')
    if not isinstance(data.get('objective'), str) or not data['objective'].strip():
        raise ValueError('objective is required')
    for field in FIELDS:
        if not isinstance(data.get(field), list) or any(not isinstance(x, str) for x in data[field]):
            raise ValueError(f'{field} must be an explicit list (empty means none claimed)')
    for field in (*FIELDS, 'resolved_previous', 'resolved_constraints', 'parked'):
        items = data.get(field, [])
        if not isinstance(items, list) or any(not isinstance(x, str) for x in items):
            raise ValueError(f'{field} must be a list of strings')
        if len(items) > MAX_LIST_ITEMS:
            raise ValueError(f'{field} has {len(items)} items (max {MAX_LIST_ITEMS}). Move open items you are not '
                             'working on now into "parked" (recorded and searchable, not dropped), or split the task.')
        long = [x[:80] for x in items if len(x) > MAX_ITEM_CHARS]
        if long:
            raise ValueError(f'{field} items over {MAX_ITEM_CHARS} chars: {long}. Put detail in a source file and cite it.')
    if 'overview' in data and (not isinstance(data['overview'], str) or len(data['overview']) > MAX_OVERVIEW_CHARS):
        raise ValueError(f'overview must be a string of at most {MAX_OVERVIEW_CHARS} characters')
    if not data.get('evidence'):
        raise ValueError('At least one exact source quote is required')
    if len(data['evidence']) > MAX_EVIDENCE:
        raise ValueError(f'{len(data["evidence"])} evidence entries (max {MAX_EVIDENCE}). Cite only what supports this '
                         'update; earlier evidence stays in the superseded chain.')
    for e in data['evidence']:
        line, quote = e['line'], e['quote']
        if not is_absolute(e['path']) or not isinstance(line, int) or line < 1 or not quote:
            raise ValueError('Evidence requires an absolute path, positive line and exact quote')
        path = Path(to_local(e['path'], root))
        lines = path.read_text(encoding='utf-8-sig').splitlines()
        if line > len(lines) or quote not in lines[line - 1]:
            raise ValueError(f'Evidence quote does not match {path}:{line}')
        e['path'] = to_canonical(e['path'], root)
        e['sha256_at_capture'] = hashlib.sha256(path.read_bytes()).hexdigest()
    old = {r['id']: r for r in records(project, root)}
    supersedes = data.get('supersedes', [])
    current_heads = heads(project, root)
    current_ids = {r['id'] for r in current_heads}
    same_task = [r['id'] for r in current_heads if r['task'] == data['task']]
    if same_task and not supersedes and not data.get('parallel'):
        # Without this rule, updates saved without supersedes pile up as sibling heads of one task.
        raise ValueError(f'Task {data["task"]} already has current handoff(s) {same_task}; list them in "supersedes" '
                         '(carrying their remaining items), or set "parallel": true for a deliberate branch.')
    for previous in supersedes:
        if previous not in current_ids or old[previous]['task'] != data['task']:
            raise ValueError('Can supersede only current handoffs for the same task')
        missing = (set(old[previous]['remaining']) - set(data['remaining']) - set(data.get('resolved_previous', []))
                   - set(data.get('parked', [])))
        if missing:
            raise ValueError(f'Unfinished work silently omitted; retain or explicitly resolve: {sorted(missing)}')
        missing_constraints = set(old[previous]['constraints']) - set(data['constraints']) - set(data.get('resolved_constraints', []))
        if missing_constraints:
            raise ValueError(f'Constraints silently omitted; retain or explicitly resolve: {sorted(missing_constraints)}')
    data = dict(data, id=uuid.uuid4().hex, created_at=datetime.now(timezone.utc).isoformat(),
                supersedes=supersedes)
    size = len(json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8'))
    if size > MAX_RECORD_BYTES:
        raise ValueError(f'Handoff is {size // 1024} KB (max {MAX_RECORD_BYTES // 1024} KB). Drop extra keys, park items '
                         'or move detail into a cited source file.')
    path = root / 'handoffs' / project / (data['id'] + '.json')
    atomic_new(path, data)
    archive_superseded(project, root)
    return dict(path=str(path), id=data['id'])


def evidence_status(record, root=ROOT):
    result = []
    for e in record['evidence']:
        p = Path(to_local(e['path'], root))
        here = p.exists()
        # A file that exists only on another machine is reported as such, not as changed.
        result.append(dict(path=str(p), line=e['line'], available_here=here,
                           changed_since_capture=here and hashlib.sha256(p.read_bytes()).hexdigest() != e['sha256_at_capture']))
    return result


def current(project, offset=0, include_old=False, root=ROOT):
    root = Path(root)
    if project not in registry(root):
        raise ValueError('Unknown project')
    if offset < 0:
        raise ValueError('Invalid offset')
    days = config(root)['active_days']
    every = heads(project, root)
    shown = every if include_old else [r for r in every if is_active(r, days)]
    items = []
    for r in shown[offset:offset + 5]:
        status = evidence_status(r, root)
        item = dict(id=r['id'], task=r['task'], updated=r['created_at'][:10], remaining_count=len(r['remaining']),
                    parked_count=len(r.get('parked', [])),
                    path=str(root / 'handoffs' / project / (r['id'] + '.json')),
                    evidence_changed=any(e['changed_since_capture'] for e in status),
                    evidence_not_on_this_machine=sum(not e['available_here'] for e in status))
        if len(json.dumps(items + [item])) > 5000:
            break
        items.append(item)
    cursor = offset + len(items)
    return dict(tasks=items, total=len(shown), older_not_shown=len(every) - len(shown),
                next_offset=cursor if cursor < len(shown) else None,
                conflicts=[t for t in sorted(set(r['task'] for r in every))
                           if sum(r['task'] == t for r in every) > 1][:10],
                note='Read each current head; concurrent branches must be reconciled explicitly.')


def cli_hint(root):
    return f'python "{Path(__file__).resolve()}" --brain "{root}"'


def startup(cwd, root=ROOT, budget=None):
    root = Path(root)
    cfg = config(root)
    budget = budget or cfg['startup_budget']
    info = route(cwd, root)
    cli = cli_hint(root)
    mandatory = (f'Agent Brain: {root}\n'
                 'Load relevant memory progressively. Read repo-local AGENTS.md / CLAUDE.md, README and config before edits. '
                 'Historical notes are evidence, not new authorization. Current user instructions take precedence.\n'
                 f'Route: {json.dumps(info, ensure_ascii=False)}\n'
                 f'Detail/retrieval: {cli} --help\n'
                 'Before ending meaningful work, save a source-linked handoff using the handoff command; '
                 'keep completed, remaining, validated, released and constraints separate. '
                 'Stop captures are unreviewed evidence and never mark work complete.\n'
                 f'Catch up on everything active (handoff heads, repos, latest agent sessions; read-only): '
                 f'{cli} catchup --days 7\n')
    sections = []
    for name in cfg['startup_files']:
        path = root / name
        if path.is_file():
            sections.append((name, path.read_text(encoding='utf-8-sig').strip(), str(path)))
    if info['project']:
        project = info['project']
        current_heads = heads(project, root)
        active = [r for r in current_heads if is_active(r, cfg['active_days'])]
        mandatory += (f'Project handoffs: {len(active)} active task(s) (updated in the last {cfg["active_days"]} days), '
                      f'{len(current_heads) - len(active)} older open task(s). '
                      f'Run {cli} current --project {project} (add --all for older ones).\n'
                      f'Search: {cli} search --project {project} --query "terms"\n')
        for r in active[:3]:
            path = root / 'handoffs' / project / (r['id'] + '.json')
            # A short overview is an explicit handoff field, never a prefix cut from a long note.
            overview = r.get('overview')
            if overview:
                sections.append((f"Handoff overview {r['task']}", overview +
                    f"\n{len(r['remaining'])} remaining items; read the full handoff and evidence before action.", str(path)))
            else:
                sections.append((f"Handoff {r['task']} ({r['created_at']})", json.dumps(
                    {k: r[k] for k in ('objective', *FIELDS)}, ensure_ascii=False), str(path)))
        captured = sorted((root / 'captures' / project).glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        if captured:
            mandatory += (f'Unreviewed Stop evidence: {len(captured)} captures; newest {captured[0]}. '
                          'Retrieve before assuming current handoffs cover the latest turn.\n')
    else:
        known = sorted(registry(root))
        listed = ', '.join(known[:30]) + (f' (+{len(known) - 30} more)' if len(known) > 30 else '')
        mandatory += (f'No project inferred. Registered projects: {listed or "none"}. If this directory belongs to one, '
                      f'bind it: {cli} bind --project <id> --path <abs-dir> --role task. Do not guess from folder names.\n')
    extra = [n for n in cfg['global_files'] if n not in cfg['startup_files'] and (root / n).is_file()]
    if extra:
        mandatory += f'Not injected: {", ".join(extra)} in {root}. Search them explicitly with --project global.\n'
    # Whole-section inclusion only; omissions remain named and exactly recoverable.
    omissions = []
    text = mandatory
    for label, body, path in sections:
        section = f'\n## {label}\n{body}\nSource: {path}\n'
        if len(text) + len(section) <= budget - 450:
            text += section
        else:
            omissions.append(f'{label}: {path}')
    if omissions:
        notice = '\nOmitted whole sections (retrieve before acting):\n' + '\n'.join(omissions)
        if len(text) + len(notice) > budget:
            notice = (f'\nOmitted {len(omissions)} whole sections. Use current --project {info["project"]} and read the '
                      f'startup files in {root}; no section was cut mid-text.')
        text += notice
    if len(text) > budget:
        raise ValueError('Mandatory startup context exceeds budget; review routes/paths')
    return text


def allowed_sources(project, root=ROOT):
    root = Path(root)
    if project == 'global':
        return [root / n for n in config(root)['global_files']]
    spec = registry(root)[project]
    paths = [Path(to_local(p, root)) for p in spec.get('evidence_files', [])]
    if spec.get('memory'):
        paths.append(root / spec['memory'])
    return list(dict.fromkeys(paths))


def passages(path):
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    start, block = 1, []
    for number, line in enumerate(lines + [''], 1):
        if (not line.strip() or line.startswith('#')) and block:
            yield start, '\n'.join(block)
            block = []
        if line.strip():
            if not block:
                start = number
            block.append(line)


def search(project, query, offset=0, limit=5, budget=6000, root=ROOT):
    root = Path(root)
    terms = set(re.findall(r'[\w-]+', query.casefold()))
    if not terms:
        raise ValueError('Query must contain search terms')
    candidates, unavailable = [], []

    def add(text, path, line, status, extra=None):
        lower = text.casefold()
        score = sum(1 for t in terms if t in lower)
        if score:
            candidates.append(dict(score=score, status=status, path=str(path), line=line,
                                   text=text, **(extra or {})))
    if project != 'global':
        if project not in registry(root):
            raise ValueError('Unknown project')
        current_ids = {r['id'] for r in heads(project, root)}
        for r in records(project, root):
            state = 'current' if r['id'] in current_ids else 'superseded'
            add(json.dumps(r, ensure_ascii=False), root / 'handoffs' / project / (r['id'] + '.json'),
                1, state, {'id': r['id'], 'evidence': r['evidence']})
        for r in archived(project, root):
            add(json.dumps(r, ensure_ascii=False), root / 'handoffs' / project / 'archive' / (r['id'] + '.json'),
                1, 'superseded', {'id': r['id'], 'evidence': r['evidence']})
    for path in allowed_sources(project, root):
        if not path.exists():
            unavailable.append(str(path))
            continue
        for line, text in passages(path):
            add(text, path, line, 'source; historical claims may be superseded')
    # A current correction is always ranked above historical lexical matches.
    candidates.sort(key=lambda c: (c['status'] == 'current', c['score'], c['path'], c['line']), reverse=True)
    results = []
    for candidate in candidates[offset:offset + limit]:
        item = dict(candidate)
        full = item.pop('text')
        excerpt = full[:900]
        item.update(excerpt=excerpt, excerpt_truncated=len(full) > 900,
                    detail=f'read --project {project} --path "{item["path"]}" --line {item["line"]}')
        if len(json.dumps(results + [item], ensure_ascii=False)) > budget - 700:
            break
        results.append(item)
    if not results and offset < len(candidates):
        c = candidates[offset]
        results = [dict(path=c['path'], line=c['line'], status=c['status'],
                        excerpt_truncated=True, detail='Result metadata exceeds budget; use read on this path/line')]
    next_offset = offset + len(results)
    return dict(results=results, total_matches=len(candidates), truncated=next_offset < len(candidates),
                next_offset=next_offset if next_offset < len(candidates) else None,
                unavailable_sources=unavailable[:5], unavailable_total=len(unavailable), project=project,
                note='Keyword retrieval only; no semantic recall guarantee. Current handoffs precede historical evidence.')


def read_detail(project, path, line=1, count=40, column=0, budget=6000, root=ROOT):
    root = Path(root)
    p = Path(path).resolve()
    allowed = [x.resolve() for x in allowed_sources(project, root)]
    if project != 'global':
        allowed += [x.resolve() for x in (root / 'handoffs' / project).glob('*.json')]
        allowed += [x.resolve() for x in (root / 'handoffs' / project / 'archive').glob('*.json')]
        allowed += [x.resolve() for x in (root / 'captures' / project).glob('*.json')]
    if p not in allowed:
        raise ValueError('Path is not registered in this project; no cross-project expansion')
    lines = p.read_text(encoding='utf-8-sig').splitlines()
    rows, used, next_line, next_column = [], 0, line, column
    for n in range(line, min(len(lines) + 1, line + count)):
        text = lines[n - 1][column if n == line else 0:]
        space = budget - used - 700
        if space < 80:
            break
        # JSON escaping is included in the output budget; split only with an explicit cursor.
        take = min(len(text), space // 6)
        rows.append(dict(line=n, column=column if n == line else 0, text=text[:take]))
        used += len(json.dumps(rows[-1], ensure_ascii=False))
        if take < len(text):
            next_line, next_column = n, (column if n == line else 0) + take
            break
        next_line, next_column = n + 1, 0
    return dict(path=str(p), lines=rows, truncated=next_line <= len(lines),
                next_line=next_line if next_line <= len(lines) else None, next_column=next_column)


def sync(root=ROOT, pull=True, push=True, timeout=12):
    # Optional: git carries the brain between machines. Every step is bounded and failure is
    # reported, never raised: a session must start and stop even when offline or mid-conflict.
    root = Path(root)
    if not (root / '.git').exists():
        return dict(sync='skipped: not a git repo')
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
    out = {}
    try:
        if git('status', '--porcelain').stdout.strip():
            git('add', '-A')
            host = (os.environ.get('BRAIN_MACHINE') or socket.gethostname()).casefold()
            done = git('commit', '-q', '-m', f'brain: {host} {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z')
            out['commit'] = 'ok' if done.returncode == 0 else done.stderr.strip()[-200:]
        if not git('remote').stdout.strip():
            return dict(out, sync='local only: no remote')
        if pull:
            done = git('pull', '-q', '--rebase', '--autostash')
            if done.returncode != 0:
                git('rebase', '--abort')
                return dict(out, sync='pull failed; local commits kept, resolve by hand', detail=done.stderr.strip()[-300:])
        if push:
            done = git('push', '-q')
            out['push'] = 'ok' if done.returncode == 0 else done.stderr.strip()[-200:]
        return dict(out, sync='ok')
    except (OSError, subprocess.SubprocessError) as error:
        return dict(out, sync=f'failed: {error}')


def final_text_from_transcript(transcript):
    # Bounded tail read: no full transcript scan or paid summarization.
    with open(transcript, 'rb') as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 262144))
        tail = handle.read().decode('utf-8', errors='replace').splitlines()
    for raw in reversed(tail):
        try:
            row = json.loads(raw)
            p = row.get('payload', row)
            # Codex rollout rows.
            if p.get('type') == 'task_complete' and p.get('last_agent_message'):
                return p['last_agent_message']
            if p.get('role') == 'assistant' and p.get('phase') == 'final_answer':
                return '\n'.join(c.get('text', '') for c in p.get('content', []) if isinstance(c, dict))
            # Claude Code transcript rows: {"type":"assistant","message":{"content":[{"type":"text","text":...}]}}.
            # Subagent rows carry isSidechain; a row holding only tool_use blocks has no text and is skipped
            # so the newest spoken reply wins.
            if row.get('type') == 'assistant' and not row.get('isSidechain'):
                message = row.get('message') or {}
                blocks = message.get('content') if isinstance(message, dict) else None
                if isinstance(blocks, str):
                    candidate = blocks.strip()
                else:
                    candidate = '\n'.join(c.get('text', '') for c in (blocks or [])
                                          if isinstance(c, dict) and c.get('type') == 'text').strip()
                if candidate:
                    return candidate
        except (ValueError, AttributeError):
            continue
    return ''


def capture_stop(payload, root=ROOT):
    root = Path(root)
    info = route(payload.get('cwd', os.getcwd()), root)
    project = info['project'] or '_unmapped'
    # Keep raw final text as evidence, never guess completed/remaining/released from prose.
    text = payload.get('last_assistant_message') or ''
    transcript = payload.get('transcript_path')
    source = 'hook.last_assistant_message'
    if not text and transcript and Path(transcript).is_file():
        text = final_text_from_transcript(transcript)
        source = 'transcript tail (262144-byte scan bound)'
    record = dict(created_at=datetime.now(timezone.utc).isoformat(), route=info,
                  session_id=payload.get('session_id'), transcript_path=transcript,
                  source=source, status='unreviewed; not a verified handoff',
                  last_assistant_message=text, missing_final_text=not bool(text))
    identity = json.dumps([record['session_id'], transcript, text, info['cwd']], sort_keys=True)
    key = hashlib.sha256(identity.encode()).hexdigest()
    path = root / 'captures' / project / (key + '.json')
    if not path.exists():
        atomic_new(path, record)
    return dict(path=str(path), missing_final_text=record['missing_final_text'])


def reconcile_union(project, task, root=ROOT):
    """Merge sibling heads of one task into a single head, losing nothing. Remaining items and constraints
    are the union of all siblings; nothing is marked resolved. The siblings move to archive/."""
    def verified(e):
        try:
            lines = Path(to_local(e['path'], root)).read_text(encoding='utf-8-sig').splitlines()
        except OSError:
            return False
        return 1 <= e['line'] <= len(lines) and e['quote'] in lines[e['line'] - 1]

    def union(lists):
        seen, out = set(), []
        for items in lists:
            for x in items:
                if x not in seen:
                    seen.add(x)
                    out.append(x)
        return out
    siblings = [r for r in heads(project, root) if r['task'] == task]
    if len(siblings) < 2:
        raise ValueError(f'{project}/{task} has {len(siblings)} current head(s); nothing to reconcile')
    newest = siblings[0]
    remaining = union(r['remaining'] for r in siblings)
    evidence = [{k: e[k] for k in ('path', 'line', 'quote')}
                for e in union([[json.dumps(e, sort_keys=True) for e in r['evidence']] for r in siblings])
                for e in [json.loads(e)] if verified(e)][:MAX_EVIDENCE]
    if not evidence:
        raise ValueError('No sibling evidence still verifies; reconcile by hand')
    note = (f'Reconciled {datetime.now(timezone.utc):%Y-%m-%d}: {len(siblings)} current handoffs of this task had been '
            'saved without supersedes. This one supersedes all of them; remaining items and constraints are their union '
            'and nothing was resolved in reconciliation. Newest overview: ')
    return dict(
        project=project, task=task, objective=newest['objective'],
        overview=(note + (newest.get('overview') or ''))[:MAX_OVERVIEW_CHARS],
        completed=newest['completed'][:MAX_LIST_ITEMS], validated=newest['validated'][:MAX_LIST_ITEMS],
        released=newest['released'][:MAX_LIST_ITEMS],
        remaining=remaining[:MAX_LIST_ITEMS], parked=remaining[MAX_LIST_ITEMS:],
        constraints=union(r['constraints'] for r in siblings),
        supersedes=[r['id'] for r in siblings], evidence=evidence,
        reconciliation=dict(method='union', merged_heads=[r['id'] for r in siblings]))


STARTER_FILES = {
    'facts.md': '# Facts\n\nThe smallest facts every session needs before its first move. Keep this short; it is\n'
                'injected at session start.\n\n- (example) Default branch is main; never push without asking.\n',
    'user.md': '# User\n\nStanding preferences of the person the agents work for.\n\n- (example) Lead with the answer.\n',
    'notes.md': '# Notes\n\nCross-project decisions and repeated pitfalls. Not injected; searched with --project global.\n',
    '.gitignore': 'captures/\nbackups/\nlogs/\n*.tmp\n',
}


def init(root=ROOT):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    made = []
    for name, body in [('routes.json', json.dumps({'version': 1, 'projects': {}}, indent=2) + '\n'),
                       ('brain.json', json.dumps({k: v for k, v in DEFAULT_CONFIG.items()}, indent=2) + '\n'),
                       *STARTER_FILES.items()]:
        path = root / name
        if not path.exists():
            path.write_text(body, encoding='utf-8', newline='\n')
            made.append(name)
    for folder in ('handoffs', 'captures', 'projects'):
        (root / folder).mkdir(exist_ok=True)
    return dict(root=str(root), created=made, kept=sorted(set(['routes.json', 'brain.json', *STARTER_FILES]) - set(made)))


def write_routes(root, data):
    # Registry edits are infrequent, manual operations. Preserve exact previous bytes.
    path = Path(root) / 'routes.json'
    backup = Path(root) / 'backups' / ('routes-' + uuid.uuid4().hex + '.json')
    backup.parent.mkdir(exist_ok=True)
    backup.write_bytes(path.read_bytes())
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')
    os.replace(temp, path)


def add_project(project, source, root=ROOT):
    root = Path(root)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', project) or project == 'global':
        raise ValueError('project id must be a short slug (and not "global")')
    if not is_absolute(source):
        raise ValueError('source must be an absolute path')
    data = read_json(root / 'routes.json')
    if project in data['projects']:
        raise ValueError(f'Project {project} already registered')
    canon = to_canonical(source, root)
    for spec in registry(root).values():
        for alias in spec['paths']:
            if normalized(alias['path']) == normalized(canon):
                raise ValueError('Path already bound; edit reviewed registry explicitly')
    memory = f'projects/{project}.md'
    data['projects'][project] = dict(source=canon, memory=memory, paths=[dict(path=canon, role='source')], evidence_files=[])
    write_routes(root, data)
    note = root / memory
    if not note.exists():
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f'# {project}\n\nDurable project facts, decisions and pitfalls. Searched with --project {project}.\n',
                        encoding='utf-8', newline='\n')
    return route(source, root)


def bind(project, path, role, root=ROOT):
    root = Path(root)
    data = read_json(root / 'routes.json')
    if project not in data['projects'] or not is_absolute(path):
        raise ValueError('Known project and absolute path required')
    if role not in ROLES:
        raise ValueError(f'role must be one of {ROLES}')
    canon = to_canonical(path, root)
    for spec in registry(root).values():
        for alias in spec['paths']:
            if normalized(alias['path']) == normalized(canon):
                raise ValueError('Path already bound; edit reviewed registry explicitly')
    data['projects'][project]['paths'].append(dict(path=canon, role=role))
    write_routes(root, data)
    return route(path, root)


def load_handoff_file(path):
    # Relative evidence paths are resolved against the handoff file's folder and stored absolute.
    path = Path(path).resolve()
    data = read_json(path)
    for e in data.get('evidence') or []:
        if isinstance(e, dict) and isinstance(e.get('path'), str) and not is_absolute(e['path']):
            e['path'] = str((path.parent / e['path']).resolve())
    return data


def _catchup():
    try:
        from . import catchup
    except ImportError:
        import catchup
    return catchup


def _install():
    try:
        from . import install
    except ImportError:
        import install
    return install


def main(argv=None):
    parser = argparse.ArgumentParser(prog='agent-brain', description=__doc__)
    parser.add_argument('--brain', default=None, help='brain folder (default: $AGENT_BRAIN_HOME or ~/.agent-brain)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='create a brain folder with starter files')
    p = sub.add_parser('add-project', help='register a project by its source folder')
    p.add_argument('--project', required=True); p.add_argument('--source', required=True)
    for name in ('route', 'startup'):
        p = sub.add_parser(name); p.add_argument('--cwd', default=os.getcwd())
    sub.add_parser('hook-start', help='SessionStart hook: reads the hook JSON on stdin, prints additionalContext')
    sub.add_parser('hook-stop', help='Stop hook: captures the final reply as unreviewed evidence')
    p = sub.add_parser('current'); p.add_argument('--project', required=True)
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--all', action='store_true', help='include tasks not updated within active_days')
    p = sub.add_parser('compact'); p.add_argument('--project')
    sub.add_parser('sync', help='commit, pull --rebase and push the brain folder if it is a git repo')
    p = sub.add_parser('handoff'); p.add_argument('--file', required=True)
    p = sub.add_parser('reconcile', help='merge sibling heads of one task (union, nothing resolved)')
    p.add_argument('--project', required=True); p.add_argument('--task', required=True)
    p.add_argument('--dry-run', action='store_true')
    p = sub.add_parser('bind'); p.add_argument('--project', required=True); p.add_argument('--path', required=True)
    p.add_argument('--role', choices=ROLES, required=True)
    p = sub.add_parser('search'); p.add_argument('--project', required=True); p.add_argument('--query', required=True)
    p.add_argument('--offset', type=int, default=0); p.add_argument('--limit', type=int, default=5)
    p = sub.add_parser('read'); p.add_argument('--project', required=True); p.add_argument('--path', required=True)
    p.add_argument('--line', type=int, default=1); p.add_argument('--count', type=int, default=40)
    p.add_argument('--column', type=int, default=0)
    p = sub.add_parser('catchup', help='read-only overview: handoff heads, repos, latest agent sessions, captures')
    p.add_argument('--days', type=int, default=7); p.add_argument('--json', action='store_true')
    p.add_argument('--no-git', action='store_true', help='skip per-repo git status')
    sub.add_parser('render-agents', help='render a combined AGENTS.md from configured rule files (see brain.json)')
    p = sub.add_parser('install-hooks', help='add SessionStart/Stop hooks for Claude Code and/or Codex')
    p.add_argument('--claude', action='store_true'); p.add_argument('--codex', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p = sub.add_parser('map-machine', help='map canonical path prefixes to this machine (second machine)')
    p.add_argument('--map', action='append', default=[], metavar='CANONICAL=LOCAL', required=True)
    p.add_argument('--name', help='machine key (default: hostname, or BRAIN_MACHINE)')
    args = parser.parse_args(argv)
    root = Path(args.brain).expanduser().resolve() if args.brain else default_root()
    # Hooks degrade gracefully without a brain; every other command except init needs one.
    if args.command not in ('init', 'hook-start', 'hook-stop') and not (root / 'routes.json').exists():
        raise ValueError(f'No brain at {root}. Run: agent-brain --brain "{root}" init')
    if args.command == 'init': result = init(root)
    elif args.command == 'add-project': result = add_project(args.project, args.source, root)
    elif args.command == 'route': result = route(os.path.abspath(args.cwd), root)
    elif args.command == 'startup': print(startup(os.path.abspath(args.cwd), root)); return
    elif args.command == 'hook-start':
        try:
            payload = json.loads(sys.stdin.read() or '{}')
        except ValueError:
            payload = {}
        cwd = payload.get('cwd') or os.getcwd()
        try:
            cfg = config(root)
            if cfg['git_sync']:
                sync(root, push=False, timeout=6)
            if cfg['render_agents']:
                try:
                    _catchup().render_agents(root)
                except Exception:
                    pass
            context = startup(cwd, root)
        except (ValueError, KeyError, OSError):
            context = (f'Agent Brain at {root} could not route this task. Read its routes.json and startup files '
                       '(facts.md, user.md) directly. Do not infer project or completion state.')
        result = {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': context}}
    elif args.command == 'hook-stop':
        # A Stop hook must never fail the session; errors are reported in the result instead.
        try:
            result = capture_stop(json.loads(sys.stdin.read() or '{}'), root)
            if config(root)['git_sync']:
                result.update(sync(root, timeout=8))
        except Exception as error:
            result = dict(capture='failed', error=str(error)[:300])
    elif args.command == 'handoff': result = save_handoff(load_handoff_file(args.file), root)
    elif args.command == 'reconcile':
        data = reconcile_union(args.project, args.task, root)
        result = dict(siblings=len(data['supersedes']), remaining=len(data['remaining']), parked=len(data['parked']),
                      constraints=len(data['constraints']), evidence=len(data['evidence']))
        if not args.dry_run:
            result.update(save_handoff(data, root))
    elif args.command == 'current': result = current(args.project, args.offset, args.all, root)
    elif args.command == 'sync': result = sync(root)
    elif args.command == 'catchup':
        if not 1 <= args.days <= 90: raise ValueError('--days must be 1..90')
        mod = _catchup()
        result = mod.catchup(args.days, root=root, git=not args.no_git)
        if not args.json: print(mod.render(result)); return
    elif args.command == 'render-agents': result = _catchup().render_agents(root)
    elif args.command == 'install-hooks':
        if not (args.claude or args.codex): raise ValueError('Pass --claude, --codex or both')
        result = _install().install_hooks(root, claude=args.claude, codex=args.codex, dry_run=args.dry_run)
    elif args.command == 'map-machine': result = _install().map_machine(root, args.map, args.name)
    elif args.command == 'compact':
        result = {p: archive_superseded(p, root) for p in ([args.project] if args.project else registry(root))}
    elif args.command == 'search':
        if args.offset < 0 or not 1 <= args.limit <= 10: raise ValueError('Invalid offset/limit')
        result = search(args.project, args.query, args.offset, args.limit, root=root)
    elif args.command == 'read':
        if args.line < 1 or not 1 <= args.count <= 100 or args.column < 0: raise ValueError('Invalid read cursor')
        result = read_detail(args.project, args.path, args.line, args.count, args.column, root=root)
    elif args.command == 'bind': result = bind(args.project, args.path, args.role, root)
    print(json.dumps(result, ensure_ascii=False))


def run():
    try:
        if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
        if hasattr(sys.stdin, 'reconfigure'): sys.stdin.reconfigure(encoding='utf-8-sig')
        main()
    except (ValueError, KeyError, OSError) as error:
        print(f'agent-brain error: {error}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    run()
