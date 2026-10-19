"""The evaluator must catch bad outputs rather than merely emit a green score."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from evals import run as evaluation


class EvaluationTests(unittest.TestCase):
    def test_missing_evidence_and_wrong_types_fail(self):
        checks = [{'path': ['data', 'line'], 'op': 'equals', 'value': 1}]
        self.assertEqual(evaluation.grade({'data': {'line': 1}}, checks), [])
        for bad in ({}, {'data': {}}, {'data': {'line': True}}, {'data': {'line': '1'}}):
            self.assertEqual(len(evaluation.grade(bad, checks)), 1)

    def test_unknown_operator_fails(self):
        check = {'path': [], 'op': 'always_pass', 'value': {}}
        self.assertEqual(evaluation.grade({}, [check]), [check])

    def test_observations_preserved_and_bad_candidate_rejected(self):
        case = json.loads(evaluation.CORPUS.read_text())['cases'][1]
        def fabricated(args, root):
            return {'exit_code': 0, 'data': {'results': [{'excerpt': 'fabricated'}], 'total_matches': 1}}
        result = evaluation.evaluate_case(case, fabricated)
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['failures']), 2)
        self.assertEqual(result['observations'][0]['output']['data']['total_matches'], 1)

    def test_driver_crash_counts_as_failure(self):
        def broken(args, root):
            raise RuntimeError('candidate crashed')
        report = evaluation.run(driver=broken)
        self.assertFalse(report['accepted'])
        self.assertEqual(report['passed'], 0)
        self.assertTrue(all('RuntimeError' in row['error'] for row in report['cases']))

    def test_history_first_regression_fails_the_ranking_case(self):
        case = json.loads(evaluation.CORPUS.read_text())['cases'][0]
        def history_first(args, root):
            output = evaluation.invoke(args, root)
            if args[0] == 'search':
                results = output['data']['results']
                results.sort(key=lambda row: row['status'] == 'current')
            return output
        result = evaluation.evaluate_case(case, history_first)
        self.assertFalse(result['passed'])
        self.assertTrue(any(f['check']['path'][-1] == 'status' for f in result['failures']))

    def test_unsafe_commands_and_brain_override_rejected(self):
        for args in (['install-hooks', '--claude'], ['sync'], ['current', '--brain', '/elsewhere']):
            with self.assertRaises(ValueError):
                evaluation.invoke(args, Path('.'))

    def test_empty_duplicate_and_unscored_corpora_rejected(self):
        dataset = json.loads(evaluation.CORPUS.read_text())
        malformed = [dict(dataset, cases=[]), dict(dataset, cases=[dataset['cases'][0]] * 2)]
        unchecked = copy.deepcopy(dataset['cases'][1])
        unchecked['steps'][0]['checks'] = []
        malformed.append(dict(dataset, cases=[unchecked]))
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / 'cases.json'
            for data in malformed:
                corpus.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    evaluation.run(corpus)

    def test_fixture_writes_cannot_escape_temporary_root(self):
        case = {'id': 'escape', 'dimension': 'isolation', 'steps': [
            {'write': '../outside.md', 'text': 'must not be written'}]}
        with self.assertRaises(ValueError):
            evaluation.evaluate_case(case)

    def test_real_cli_suite(self):
        report = evaluation.run()
        self.assertTrue(report['accepted'], json.dumps(report, indent=2))
        self.assertEqual(report['total'], 12)
        self.assertTrue(all(d['total'] > 0 for d in report['dimensions'].values()))
        for case in report['cases']:
            self.assertNotIn('agent-brain-eval-', json.dumps(case))


if __name__ == '__main__':
    unittest.main()
