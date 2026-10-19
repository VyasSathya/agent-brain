"""Replay a versioned retrieval corpus, score observable contracts, and fail closed.

Only synthetic fixtures are used. Each scenario gets a fresh temporary directory;
every operation invokes the production CLI with an explicit --brain path.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).with_name('cases.json')
DIMENSIONS = ('retrieval', 'provenance', 'isolation', 'continuity')


def lookup(value, path):
    for key in path:
        if key == '$json_lines':
            value = json.loads('\n'.join(row['text'] for row in value))
            continue
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def grade(output, checks):
    """Return every failed observable check; missing fields never count as passes."""
    failures = []
    for check in checks:
        try:
            actual = lookup(output, check.get('path', []))
            expected = check['value']
            op = check['op']
            if op == 'equals':
                passed = type(actual) is type(expected) and actual == expected
            elif op == 'contains':
                passed = expected in actual
            elif op == 'excludes':
                passed = expected not in actual
            else:
                raise ValueError('Unknown check operator: ' + op)
        except (KeyError, IndexError, TypeError, ValueError):
            passed = False
        if not passed:
            failures.append(check)
    return failures


def invoke(args, root):
    if not args or args[0] not in {'route', 'search', 'read', 'current', 'handoff'} or '--brain' in args:
        raise ValueError('Only fixture-scoped retrieval and handoff commands are allowed')
    done = subprocess.run(
        [sys.executable, '-m', 'agent_brain', '--brain', str(root), *args],
        cwd=str(REPO), capture_output=True, text=True, encoding='utf-8', timeout=20,
    )
    if done.returncode:
        return {'exit_code': done.returncode, 'error': done.stderr.strip()}
    return {'exit_code': 0, 'data': json.loads(done.stdout)}


def substitute(value, variables):
    if isinstance(value, str):
        for name, replacement in variables.items():
            value = value.replace('${' + name + '}', str(replacement))
        if '${' in value:
            raise ValueError('Unresolved fixture variable: ' + value)
        return value
    if isinstance(value, list):
        return [substitute(x, variables) for x in value]
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    return value


def prepare(root):
    """One correction, a misleading historical match, and another project's decoy."""
    app = root / 'app'
    other = root / 'other'
    app.mkdir()
    other.mkdir()
    (app / 'notes.md').write_text(
        'Pagination failed: duplicate rows.\nPagination fixed: cursor skips zero rows.\n', encoding='utf-8')
    (other / 'notes.md').write_text('Pagination PRIVATE_DECOY do not return.\n', encoding='utf-8')
    registry = {'projects': {name: {
        'source': str(folder), 'paths': [{'path': str(folder), 'role': 'source'}],
        'evidence_files': [str(folder / 'notes.md')],
    } for name, folder in [('app', app), ('other', other)]}}
    (root / 'routes.json').write_text(json.dumps(registry), encoding='utf-8')
    variables = {'ROOT': root, 'APP': app, 'OTHER': other}
    handoff = dict(project='app', task='pagination', objective='Pagination correction',
                   completed=['Implement cursor'], remaining=['Device acceptance'],
                   validated=[], released=[], constraints=['Do not deploy'],
                   evidence=[dict(path=str(app / 'notes.md'), line=1, quote='Pagination failed: duplicate rows.')])
    return variables, handoff


def evaluate_case(case, driver=invoke):
    observations = []
    failures = []
    with tempfile.TemporaryDirectory(prefix='agent-brain-eval-') as directory:
        root = Path(directory).resolve()
        variables, base = prepare(root)
        for number, step in enumerate(case['steps']):
            if 'write' in step:
                # Fixture writes are relative and confined even for a modified corpus.
                target = (root / step['write']).resolve()
                target.relative_to(root)
                target.write_text(step['text'], encoding='utf-8')
                continue
            if 'handoff' in step:
                payload = dict(base, **substitute(step['handoff'], variables))
                target = root / 'input.json'
                target.write_text(json.dumps(payload), encoding='utf-8')
                args = ['handoff', '--file', str(target)]
            else:
                args = substitute(step['args'], variables)
            output = driver(args, root)
            checks = substitute(step['checks'], variables)
            failed = grade(output, checks)
            if 'remember' in step and not failed:
                variables[step['remember']] = output['data']['id']
            observations.append({'step': number, 'args': args, 'output': output})
            failures.extend({'step': number, 'check': check} for check in failed)
    # Remove run-specific temp paths and IDs without discarding actual observations.
    replacements = sorted(((str(v), '${' + k + '}') for k, v in variables.items()),
                          key=lambda pair: len(pair[0]), reverse=True)
    def normalize(value):
        if isinstance(value, str):
            for original, token in replacements:
                # Search excerpts contain serialized JSON inside a string.
                value = value.replace(json.dumps(original)[1:-1], token)
                value = value.replace(original, token)
            return value
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value
    detail = normalize({'observations': observations, 'failures': failures})
    return dict(id=case['id'], dimension=case['dimension'], passed=not failures, **detail)


def run(corpus=CORPUS, driver=invoke):
    raw = Path(corpus).read_bytes()
    dataset = json.loads(raw)
    if dataset['version'] != 1 or not dataset['cases']:
        raise ValueError('Expected a nonempty v1 evaluation corpus')
    ids = [case['id'] for case in dataset['cases']]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate case IDs')
    results = []
    for case in dataset['cases']:
        if case['dimension'] not in DIMENSIONS:
            raise ValueError('Unknown rubric dimension')
        operations = [step for step in case['steps'] if 'write' not in step]
        if not operations or not all(step.get('checks') for step in operations):
            raise ValueError('Every case requires observable checks')
        if any(sum(key in step for key in ('write', 'args', 'handoff')) != 1 for step in case['steps']):
            raise ValueError('Each step must specify exactly one operation')
        try:
            results.append(evaluate_case(case, driver))
        except Exception as error:
            results.append(dict(id=case['id'], dimension=case['dimension'], passed=False,
                                error=type(error).__name__ + ': ' + str(error)))
    scores = {dimension: {
        'passed': sum(r['passed'] for r in results if r['dimension'] == dimension),
        'total': sum(r['dimension'] == dimension for r in results),
    } for dimension in DIMENSIONS}
    passed = sum(r['passed'] for r in results)
    return dict(schema_version=1, rubric_version=dataset['rubric'],
                corpus_sha256=hashlib.sha256(raw).hexdigest(),
                engine_sha256=hashlib.sha256((REPO / 'agent_brain/continuity.py').read_bytes()).hexdigest(),
                evaluator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                passed=passed, total=len(results), accepted=passed == len(results),
                dimensions=scores, cases=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=CORPUS)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = run(args.corpus)
    rendered = json.dumps(report, indent=2, ensure_ascii=True) + '\n'
    if args.output:
        args.output.write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')
    return 0 if report['accepted'] else 1


if __name__ == '__main__':
    sys.exit(main())
