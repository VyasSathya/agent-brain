"""Core behavior: routing, handoff rules, bounded search/read, startup context and Stop capture.

Every test builds an isolated brain in a temporary folder; nothing outside it is touched.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_brain import continuity as c  # noqa: E402


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'app-source'
        self.source.mkdir()
        self.evidence = self.root / 'evidence.md'
        self.evidence.write_text('# Earlier\n\nLibrary pagination failed.\n\n# Correction\n\nLibrary pagination now passed on original RPC.\n', encoding='utf-8')
        self.other = self.root / 'other.md'
        self.other.write_text('UNRELATED SECRET WORD pagination', encoding='utf-8')
        self.spec = dict(projects={
            'app': dict(source=str(self.source), paths=[dict(path=str(self.source), role='source'),
                        dict(path=str(self.root / 'task'), role='task'), dict(path=str(self.root / 'stage'), role='staging')],
                        evidence_files=[str(self.evidence)]),
            'other': dict(source=str(self.root / 'other'), paths=[dict(path=str(self.root / 'other'), role='source')],
                          evidence_files=[str(self.other)])})
        self.write_registry()
        for name in ['facts.md', 'user.md']:
            (self.root / name).write_text('Stable preference: lead with the answer.', encoding='utf-8')
        self.base = dict(project='app', task='release', objective='Verify library pagination',
                         completed=[], remaining=['Device acceptance', 'Pagination acceptance'],
                         validated=[], released=[], constraints=['Do not deploy'],
                         evidence=[dict(path=str(self.evidence), line=3, quote='Library pagination failed.')])

    def write_registry(self):
        (self.root / 'routes.json').write_text(json.dumps(self.spec), encoding='utf-8')

    def save(self, data=None):
        return c.save_handoff(copy.deepcopy(data or self.base), self.root)

    # route

    def test_source_task_and_staging_roles(self):
        for path, role in [(self.source / 'nested', 'source'), (self.root / 'task', 'task'), (self.root / 'stage', 'staging')]:
            route = c.route(path, self.root)
            self.assertEqual(route['project'], 'app')
            self.assertEqual(route['role'], role)
            self.assertEqual(route['source'], str(self.source))

    def test_boundaries_case_normalization(self):
        self.assertIsNone(c.route(str(self.source) + '-unrelated', self.root)['project'])
        self.assertEqual(c.direct_route(str(self.source).upper().replace('\\', '/') + '/nested/../file', self.root)['project'], 'app')

    def test_specific_app_beats_monorepo(self):
        self.spec['projects']['parent'] = dict(source=str(self.root), paths=[dict(path=str(self.root), role='source')])
        self.write_registry()
        self.assertEqual(c.route(self.source, self.root)['project'], 'app')

    def test_ambiguous_alias_rejected(self):
        self.spec['projects']['other']['paths'].append(dict(path=str(self.source), role='source'))
        self.write_registry()
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            c.route(self.source, self.root)

    def test_unknown_directory_not_name_guessed(self):
        self.assertIsNone(c.route(self.root / 'app-new-task', self.root)['project'])

    def test_relative_registry_paths_anchor_to_brain(self):
        self.spec['projects']['rel'] = dict(source='rel-src', paths=[dict(path='rel-src', role='source')])
        self.write_registry()
        found = c.route(self.root / 'rel-src' / 'deep', self.root)
        self.assertEqual((found['project'], found['source']), ('rel', str(self.root / 'rel-src')))

    def test_real_git_worktree(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.source), *args], check=True, capture_output=True, text=True)
        git('init')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'fixture')
        tree = self.root / 'actual-worktree'
        git('worktree', 'add', '--detach', str(tree), 'HEAD')
        result = c.route(tree, self.root)
        self.assertEqual((result['project'], result['role']), ('app', 'worktree'))

    def test_bind_and_add_project(self):
        c.bind('app', str(self.root / 'new-task'), 'task', self.root)
        self.assertEqual(c.route(self.root / 'new-task', self.root)['role'], 'task')
        with self.assertRaisesRegex(ValueError, 'already bound'):
            c.bind('other', str(self.root / 'new-task'), 'task', self.root)
        found = c.add_project('fresh', str(self.root / 'fresh-src'), self.root)
        self.assertEqual((found['project'], found['role']), ('fresh', 'source'))
        self.assertTrue((self.root / 'projects' / 'fresh.md').exists())
        self.assertTrue(list((self.root / 'backups').glob('routes-*.json')))

    # handoff

    def test_correction_beats_old_failure(self):
        old = self.save()
        new = copy.deepcopy(self.base)
        new.update(supersedes=[old['id']], remaining=['Device acceptance'],
                   resolved_previous=['Pagination acceptance'], validated=['Library pagination now passed on original RPC.'],
                   evidence=[dict(path=str(self.evidence), line=7, quote='Library pagination now passed on original RPC.')])
        latest = self.save(new)
        results = c.search('app', 'Library pagination', root=self.root)
        self.assertEqual(results['results'][0]['id'], latest['id'])
        self.assertEqual(results['results'][0]['status'], 'current')
        self.assertEqual(results['results'][0]['evidence'][0]['line'], 7)
        self.assertTrue(any(r['status'] == 'superseded' for r in results['results']))

    def test_unfinished_work_cannot_disappear(self):
        old = self.save()
        new = copy.deepcopy(self.base)
        new.update(supersedes=[old['id']], remaining=[])
        with self.assertRaisesRegex(ValueError, 'Unfinished'):
            self.save(new)
        self.assertEqual(c.heads('app', self.root)[0]['remaining'], self.base['remaining'])

    def test_update_without_supersedes_rejected(self):
        self.save()
        with self.assertRaisesRegex(ValueError, 'supersedes'):
            self.save()
        branch = copy.deepcopy(self.base)
        branch['parallel'] = True
        self.save(branch)
        self.assertEqual(len(c.heads('app', self.root)), 2)

    def test_separate_tasks_survive(self):
        self.save()
        other = copy.deepcopy(self.base)
        other['task'] = 'another'
        self.save(other)
        self.assertEqual(len(c.heads('app', self.root)), 2)

    def test_constraints_cannot_disappear(self):
        old = self.save()
        new = copy.deepcopy(self.base)
        new.update(supersedes=[old['id']], constraints=[])
        with self.assertRaisesRegex(ValueError, 'Constraints silently'):
            self.save(new)

    def test_stale_supersession_rejected(self):
        old = self.save()
        new = copy.deepcopy(self.base)
        new['supersedes'] = [old['id']]
        self.save(new)
        with self.assertRaisesRegex(ValueError, 'current handoffs'):
            self.save(new)

    def test_large_overview_rejected(self):
        new = copy.deepcopy(self.base)
        new['overview'] = 'x' * 1201
        with self.assertRaisesRegex(ValueError, 'overview'):
            self.save(new)

    def test_cannot_supersede_other_task(self):
        old = self.save()
        other = copy.deepcopy(self.base)
        other.update(task='another', supersedes=[old['id']])
        with self.assertRaisesRegex(ValueError, 'same task'):
            self.save(other)

    def test_exact_evidence_required(self):
        new = copy.deepcopy(self.base)
        new['evidence'][0]['line'] = 7
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.save(new)

    def test_unregistered_project_rejected(self):
        new = copy.deepcopy(self.base)
        new['project'] = 'nope'
        with self.assertRaisesRegex(ValueError, 'Register'):
            self.save(new)

    def test_superseded_moves_to_archive_and_stays_searchable(self):
        old = self.save()
        new = copy.deepcopy(self.base)
        new['supersedes'] = [old['id']]
        self.save(new)
        self.assertTrue((self.root / 'handoffs/app/archive' / (old['id'] + '.json')).exists())
        self.assertEqual(len(list((self.root / 'handoffs/app').glob('*.json'))), 1)
        self.assertTrue(any(r['id'] == old['id'] for r in c.search('app', 'pagination', limit=10, root=self.root)['results']))
        c.read_detail('app', self.root / 'handoffs/app/archive' / (old['id'] + '.json'), root=self.root)

    def test_parked_items_count_as_accounted(self):
        old = self.save()
        new = copy.deepcopy(self.base)
        new.update(supersedes=[old['id']], remaining=['Device acceptance'], parked=['Pagination acceptance'])
        self.save(new)
        self.assertEqual(c.heads('app', self.root)[0]['parked'], ['Pagination acceptance'])

    def test_bounds_on_new_handoffs(self):
        for field, value, message in [('remaining', [f'item {i}' for i in range(61)], 'parked'),
                                      ('validated', ['x' * 1501], 'chars'),
                                      ('evidence', [self.base['evidence'][0]] * 41, 'evidence entries'),
                                      ('extra', ['y' * 1000] * 200, 'KB')]:
            new = copy.deepcopy(self.base)
            new[field] = value
            with self.assertRaisesRegex(ValueError, message):
                self.save(new)

    def test_relative_evidence_in_handoff_file(self):
        folder = self.root / 'work'
        folder.mkdir()
        data = copy.deepcopy(self.base)
        data['evidence'] = [dict(path='../evidence.md', line=3, quote='Library pagination failed.')]
        (folder / 'h.json').write_text(json.dumps(data), encoding='utf-8')
        saved = c.save_handoff(c.load_handoff_file(folder / 'h.json'), self.root)
        stored = c.read_json(saved['path'])
        self.assertEqual(stored['evidence'][0]['path'], str(self.evidence))

    def test_reconcile_union_keeps_everything(self):
        self.save()
        branch = copy.deepcopy(self.base)
        branch.update(parallel=True, remaining=['Branch item'], constraints=['Branch constraint'])
        self.save(branch)
        merged = c.reconcile_union('app', 'release', self.root)
        self.assertEqual(set(merged['remaining']), {'Device acceptance', 'Pagination acceptance', 'Branch item'})
        self.assertEqual(set(merged['constraints']), {'Do not deploy', 'Branch constraint'})
        c.save_handoff(merged, self.root)
        self.assertEqual(len(c.heads('app', self.root)), 1)

    # current

    def test_current_reports_heads_changes_and_age(self):
        self.save()
        other = copy.deepcopy(self.base)
        other['task'] = 'another'
        saved = self.save(other)
        result = c.current('app', root=self.root)
        self.assertEqual(result['total'], 2)
        self.assertFalse(any(t['evidence_changed'] for t in result['tasks']))
        self.evidence.write_text('Changed later', encoding='utf-8')
        self.assertTrue(all(t['evidence_changed'] for t in c.current('app', root=self.root)['tasks']))
        record = c.read_json(saved['path'])
        record['created_at'] = '2020-01-01T00:00:00+00:00'
        Path(saved['path']).write_text(json.dumps(record), encoding='utf-8')
        aged = c.current('app', root=self.root)
        self.assertEqual((aged['total'], aged['older_not_shown']), (1, 1))
        self.assertEqual(c.current('app', include_old=True, root=self.root)['total'], 2)
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            c.current('nope', root=self.root)

    def test_current_paginates(self):
        for i in range(7):
            data = copy.deepcopy(self.base)
            data['task'] = f'task-{i}'
            self.save(data)
        first = c.current('app', root=self.root)
        self.assertEqual((len(first['tasks']), first['next_offset']), (5, 5))
        second = c.current('app', offset=5, root=self.root)
        self.assertEqual((len(second['tasks']), second['next_offset']), (2, None))

    def test_changed_source_detected(self):
        self.save()
        record = c.heads('app', self.root)[0]
        self.assertFalse(c.evidence_status(record, self.root)[0]['changed_since_capture'])
        self.evidence.write_text('Changed later', encoding='utf-8')
        self.assertTrue(c.evidence_status(record, self.root)[0]['changed_since_capture'])

    # search / read

    def test_search_does_not_leak(self):
        result = c.search('app', 'pagination', root=self.root)
        self.assertNotIn('UNRELATED', json.dumps(result))
        self.assertEqual(c.search('app', 'UNRELATED', root=self.root)['total_matches'], 0)
        with self.assertRaisesRegex(ValueError, 'cross-project'):
            c.read_detail('app', self.other, root=self.root)

    def test_search_pagination_exact_links(self):
        self.evidence.write_text('\n\n'.join(f'pagination evidence {i}' for i in range(20)), encoding='utf-8')
        result = c.search('app', 'pagination', limit=3, root=self.root)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['next_offset'], 3)
        second = c.search('app', 'pagination', offset=3, limit=3, root=self.root)
        self.assertFalse(set(r['line'] for r in result['results']) & set(r['line'] for r in second['results']))
        for r in result['results']:
            self.assertEqual(r['excerpt'], self.evidence.read_text().splitlines()[r['line'] - 1])

    def test_search_budget_and_explicit_excerpt(self):
        self.evidence.write_text(('pagination ' + '\\"' * 10000) + '\n\nshort pagination', encoding='utf-8')
        result = c.search('app', 'pagination', root=self.root)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 6000)
        self.assertTrue(any(r['excerpt_truncated'] for r in result['results']))

    def test_global_search_reads_only_global_files(self):
        (self.root / 'notes.md').write_text('# Decision\n\nAlways run the linter before commit.\n', encoding='utf-8')
        result = c.search('global', 'linter', root=self.root)
        self.assertEqual(result['total_matches'], 1)
        self.assertEqual(c.search('global', 'pagination', root=self.root)['total_matches'], 0)

    def test_long_line_retrieval_lossless_and_bounded(self):
        original = 'prefix ' + ('Ω\\"' * 1500) + ' suffix'
        self.evidence.write_text(original, encoding='utf-8')
        line, column, collected = 1, 0, ''
        for _ in range(100):
            result = c.read_detail('app', self.evidence, line=line, column=column, root=self.root)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 6000)
            collected += ''.join(row['text'] for row in result['lines'])
            if not result['truncated']:
                break
            line, column = result['next_line'], result['next_column']
        else:
            self.fail('Cursor did not progress')
        self.assertEqual(collected, original)

    def test_read_returns_exact_lines(self):
        result = c.read_detail('app', self.evidence, line=3, count=1, root=self.root)
        self.assertEqual(result['lines'], [dict(line=3, column=0, text='Library pagination failed.')])
        self.assertEqual(result['next_line'], 4)

    # startup / stop

    def test_startup_bounded_omissions_and_no_unrelated_history(self):
        self.save()
        (self.root / 'user.md').write_text('very long preference ' * 1000, encoding='utf-8')
        (self.root / 'notes.md').write_text('UNRELATED HISTORY', encoding='utf-8')
        text = c.startup(self.root / 'task', root=self.root)
        self.assertLessEqual(len(text), 8000)
        self.assertIn('Omitted whole sections', text)
        self.assertNotIn('UNRELATED HISTORY', text)
        self.assertIn('user.md', text)

    def test_startup_unmapped_lists_projects(self):
        text = c.startup(self.root / 'elsewhere', root=self.root)
        self.assertIn('No project inferred', text)
        self.assertIn('app, other', text)

    def test_old_tasks_hidden_from_startup_not_lost(self):
        saved = self.save()
        path = self.root / 'handoffs/app' / (saved['id'] + '.json')
        record = c.read_json(path)
        record['created_at'] = '2020-01-01T00:00:00+00:00'
        record['overview'] = 'OLD TASK OVERVIEW'
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertFalse(c.is_active(record))
        self.assertEqual(len(c.heads('app', self.root)), 1)
        text = c.startup(self.root / 'task', root=self.root)
        self.assertNotIn('OLD TASK OVERVIEW', text)
        self.assertIn('1 older open task', text)

    def test_stop_preserves_final_text_but_not_completion(self):
        self.save()
        payload = dict(cwd=str(self.root / 'stage'), session_id='test', last_assistant_message='Implemented locally; device check remains. Ω')
        path = c.capture_stop(payload, self.root)['path']
        self.assertEqual(c.read_json(path)['last_assistant_message'], payload['last_assistant_message'])
        self.assertEqual(c.heads('app', self.root)[0]['remaining'], self.base['remaining'])
        self.assertEqual(c.capture_stop(payload, self.root)['path'], path)
        self.assertEqual(len(list((self.root / 'captures/app').glob('*.json'))), 1)

    def test_stop_missing_text_is_explicit(self):
        result = c.capture_stop(dict(cwd=str(self.root / 'task')), self.root)
        self.assertTrue(result['missing_final_text'])

    def test_stop_codex_transcript_fallback(self):
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text(json.dumps(dict(type='response_item', payload=dict(role='assistant', phase='final_answer', content=[dict(type='output_text', text='Next: device check')]))), encoding='utf-8')
        result = c.capture_stop(dict(cwd=str(self.root / 'task'), transcript_path=str(transcript)), self.root)
        self.assertEqual(c.read_json(result['path'])['last_assistant_message'], 'Next: device check')

    def test_stop_claude_transcript_fallback_skips_tool_rows_and_sidechains(self):
        rows = [dict(type='assistant', message=dict(content=[dict(type='text', text='Main reply')])),
                dict(type='assistant', isSidechain=True, message=dict(content=[dict(type='text', text='Subagent')])),
                dict(type='assistant', message=dict(content=[dict(type='tool_use', name='x')]))]
        transcript = self.root / 'claude.jsonl'
        transcript.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        result = c.capture_stop(dict(cwd=str(self.root / 'task'), transcript_path=str(transcript)), self.root)
        self.assertEqual(c.read_json(result['path'])['last_assistant_message'], 'Main reply')

    def test_other_machine_paths_map_to_canonical(self):
        mac = '/Users/someone/Dev'
        (self.root / 'machines.json').write_text(json.dumps(dict(machines=dict(mac=dict(map={str(self.root): mac})))), encoding='utf-8')
        with unittest.mock.patch.dict('os.environ', {'BRAIN_MACHINE': 'mac'}):
            self.assertEqual(c.to_local(str(self.source), self.root), mac + '/app-source')
            self.assertEqual(c.to_canonical(mac + '/app-source/x', self.root), str(self.source / 'x'))
            self.assertEqual(c.direct_route(c.to_canonical(mac + '/app-source/x', self.root), self.root)['project'], 'app')
            status = c.evidence_status(dict(evidence=[dict(path=str(self.evidence), line=3, sha256_at_capture='0')]), self.root)
            self.assertFalse(status[0]['available_here'])
            self.assertFalse(status[0]['changed_since_capture'])
        self.assertEqual(c.to_local(str(self.source), self.root), str(self.source))


class InitTests(unittest.TestCase):
    def test_init_is_idempotent_and_keeps_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'brain'
            first = c.init(root)
            self.assertIn('routes.json', first['created'])
            (root / 'facts.md').write_text('mine', encoding='utf-8')
            second = c.init(root)
            self.assertEqual(second['created'], [])
            self.assertEqual((root / 'facts.md').read_text(encoding='utf-8'), 'mine')
            self.assertEqual(c.registry(root), {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
