"""End-to-end through the command line: the quickstart on the example brain, hooks, install and render."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / 'agent_brain' / 'continuity.py'


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name).resolve()
        shutil.copytree(REPO / 'examples', self.tmp / 'examples')
        self.brain = self.tmp / 'examples' / 'tiny-brain'
        self.project = self.tmp / 'examples' / 'todo-api'
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.tmp / 'claude'), CODEX_HOME=str(self.tmp / 'codex'))
        self.env.pop('AGENT_BRAIN_HOME', None)

    def run_cli(self, *args, stdin=None, ok=True, module=False):
        cmd = [sys.executable, '-m', 'agent_brain'] if module else [sys.executable, str(ENGINE)]
        done = subprocess.run([*cmd, '--brain', str(self.brain), *args], input=stdin, capture_output=True,
                              text=True, encoding='utf-8', env=self.env, cwd=str(REPO if module else self.tmp))
        if ok:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def json_cli(self, *args, **kw):
        return json.loads(self.run_cli(*args, **kw).stdout)

    def test_quickstart_route_handoff_current_search_read(self):
        route = self.json_cli('route', '--cwd', str(self.project / 'src'))
        self.assertEqual((route['project'], route['role']), ('todo-api', 'source'))
        saved = self.json_cli('handoff', '--file', str(self.tmp / 'examples' / 'handoff.example.json'))
        current = self.json_cli('current', '--project', 'todo-api')
        self.assertEqual([t['task'] for t in current['tasks']], ['cursor-pagination'])
        self.assertEqual(current['tasks'][0]['remaining_count'], 2)
        found = self.json_cli('search', '--project', 'todo-api', '--query', 'duplicate rows')
        self.assertEqual(found['results'][0]['id'], saved['id'])
        self.assertTrue(any(r['path'].endswith('NOTES.md') for r in found['results']))
        read = self.json_cli('read', '--project', 'todo-api', '--path', str(self.project / 'NOTES.md'), '--line', '9', '--count', '2')
        self.assertIn('Fixed: the cursor', read['lines'][0]['text'])
        denied = self.run_cli('read', '--project', 'todo-api', '--path', str(self.project / 'src' / 'app.py'), ok=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('not registered', denied.stderr)

    def test_module_entry_point(self):
        self.assertEqual(self.json_cli('route', '--cwd', str(self.project), module=True)['project'], 'todo-api')

    def test_init_and_add_project(self):
        self.brain = self.tmp / 'fresh-brain'
        self.assertIn('routes.json', self.json_cli('init')['created'])
        found = self.json_cli('add-project', '--project', 'demo', '--source', str(self.project))
        self.assertEqual(found['project'], 'demo')
        self.assertTrue((self.brain / 'projects' / 'demo.md').exists())
        self.brain = self.tmp / 'nowhere'
        self.assertIn('No brain', self.run_cli('route', ok=False).stderr)

    def test_hook_start_emits_session_context(self):
        payload = json.dumps(dict(cwd=str(self.project), session_id='s1'))
        out = self.json_cli('hook-start', stdin=payload)
        context = out['hookSpecificOutput']['additionalContext']
        self.assertEqual(out['hookSpecificOutput']['hookEventName'], 'SessionStart')
        self.assertIn('"project": "todo-api"', context)
        self.assertIn('Default branch is `main`', context)

    def test_hooks_never_fail_without_a_brain(self):
        self.brain = self.tmp / 'missing'
        start = self.json_cli('hook-start', stdin=json.dumps(dict(cwd=str(self.tmp))))
        self.assertIn('could not route', start['hookSpecificOutput']['additionalContext'])
        stop = self.json_cli('hook-stop', stdin=json.dumps(dict(cwd=str(self.tmp))))
        self.assertEqual(stop['capture'], 'failed')

    def test_hook_stop_captures_final_message(self):
        payload = json.dumps(dict(cwd=str(self.project), session_id='s2', last_assistant_message='Done locally; staging remains.'))
        out = self.json_cli('hook-stop', stdin=payload)
        record = json.loads(Path(out['path']).read_text(encoding='utf-8'))
        self.assertEqual(record['status'], 'unreviewed; not a verified handoff')
        self.assertEqual(record['route']['project'], 'todo-api')

    def test_install_hooks_is_idempotent_and_keeps_existing(self):
        settings = self.tmp / 'claude' / 'settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps(dict(theme='dark', hooks=dict(Stop=[dict(hooks=[dict(type='command', command='echo mine')])]))), encoding='utf-8')
        first = self.json_cli('install-hooks', '--claude', '--codex')
        self.assertEqual(first['claude'], ['SessionStart', 'Stop'])
        self.assertEqual(first['codex'], ['SessionStart', 'Stop'])
        second = self.json_cli('install-hooks', '--claude', '--codex')
        self.assertEqual((second['claude'], second['codex']), ('already present', 'already present'))
        data = json.loads(settings.read_text(encoding='utf-8'))
        self.assertEqual(data['theme'], 'dark')
        commands = [h['command'] for g in data['hooks']['Stop'] for h in g['hooks']]
        self.assertIn('echo mine', commands)
        self.assertTrue(any('hook-stop' in cmd for cmd in commands))
        self.assertTrue(list(settings.parent.glob('settings.json.bak-*')))
        self.assertTrue((self.tmp / 'codex' / 'hooks.json').exists())

    def test_render_agents_when_configured(self):
        (self.brain / 'shared-rules.md').write_text('# Shared\n\nRule one.', encoding='utf-8')
        (self.brain / 'agents.codex.md').write_text('# Codex only\n\nRule two.', encoding='utf-8')
        target = self.tmp / 'codex' / 'AGENTS.md'
        cfg = json.loads((self.brain / 'brain.json').read_text(encoding='utf-8'))
        cfg['render_agents'] = dict(sources=['shared-rules.md', 'agents.codex.md'], target=str(target))
        (self.brain / 'brain.json').write_text(json.dumps(cfg), encoding='utf-8')
        self.assertIn('rewrote', self.json_cli('render-agents')['render_agents'])
        body = target.read_text(encoding='utf-8')
        self.assertIn('Rule one.', body)
        self.assertIn('Rule two.', body)
        self.assertEqual(self.json_cli('render-agents')['render_agents'], 'current')

    def test_catchup_lists_heads(self):
        self.json_cli('handoff', '--file', str(self.tmp / 'examples' / 'handoff.example.json'))
        out = self.json_cli('catchup', '--json', '--no-git')
        self.assertEqual([h['task'] for h in out['heads']], ['cursor-pagination'])
        text = self.run_cli('catchup', '--no-git').stdout
        self.assertIn('[todo-api] cursor-pagination', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
