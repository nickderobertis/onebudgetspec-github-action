"""The action's real entry point, end to end: action.yml's steps, the installed driver, plugin,
core and onevcs. Only the GitHub service, release host and package index are doubled."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests/action-harness'))
import yaml

import harness
from harness import Host, Job, Repository, budgets_yaml, event_pull_request

COUNT = 'echo "$ONEBUDGETSPEC_BUDGET_ID" >> "$JOURNEY_COUNTS";'


def reported(value, before=''):
    """A reported command that counts its own execution and writes `value`."""
    return ['bash', '-c', f'{COUNT}{before} echo "{{\\"value\\": {value}}}" > "$ONEBUDGETSPEC_RESULT"']


def from_data(before=''):
    """The same, reading its figure from the checkout, so a change moves the value, not the scope."""
    return reported('$(cat "values-$ONEBUDGETSPEC_BUDGET_ID")', before)


READS_REST = 'gh api repos/owner/repo/commits/main > /dev/null || exit 1;'
API_METRIC = 'report-github-api-requests-per-run'
OWN = ['onebudgetspec-github-action', 'telemetry', '--metric', 'report-command-seconds', '--input', '.telemetry/report-job.json']


class Journey(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.addCleanup(self.host.close)
        self.repository = Repository()
        self.addCleanup(self.repository.close)
        self.counts_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.counts_directory.cleanup)
        self.counts = Path(self.counts_directory.name) / 'counts'
        self.next_run = 1000
        # Trees discovery must skip: both would make `list` refuse the whole catalog.
        self.repository.write('tests/fixtures/budgets.yaml', 'not: a budgets file\n')
        self.repository.write('conformance/case/budgets.yaml', 'not: a budgets file\n')

    def catalog(self, requests=7, bundle=500, enforced=10, shadow=50):
        """Budgets with different units and commands, one with no runner tool, one report-only."""
        figures = {'requests-read': requests, 'bundle-bytes': bundle, 'coverage-percent': 90,
                   'enforced-limit': enforced, 'shadow-limit': shadow}
        for budget, figure in figures.items():
            self.repository.write(f'values-{budget}', f'{figure}\n')
        self.repository.write('budgets.yaml', budgets_yaml([
            ('requests-read', from_data(READS_REST), {'unit': 'requests', 'threshold': 100}),
            ('bundle-bytes', from_data(), {'unit': 'bytes', 'threshold': 1000}),
            ('coverage-percent', from_data(), {'unit': 'percent', 'direction': 'min', 'threshold': 80}),
            ('walltime', ['bash', '-c', COUNT], {'measure': 'elapsed', 'unit': 'seconds', 'threshold': 60}),
            ('enforced-limit', from_data(), {'threshold': 40}),
            ('shadow-limit', from_data(), {'threshold': 40, 'labels': ['provisional']}),
            ('needs-a-tool', ['no-such-tool-on-this-runner', '--measure'], {'labels': ['ci-report']}),
            ('needs-predecessor', ['cat', '.telemetry/predecessor.json'], {}),
        ]))
        self.repository.write('.github/reporting/budgets.yaml', budgets_yaml([
            ('job-seconds', OWN, {'unit': 'seconds', 'threshold': 120, 'labels': ['ci-report']}),
        ]))

    def job(self, event_name, sha, **options):
        self.next_run += 1
        options.setdefault('run_id', self.next_run)
        options.setdefault('inputs', {'token': 'real-token', 'enforcement-exclude-labels': 'provisional'})
        options.setdefault('environment', {'JOURNEY_COUNTS': str(self.counts)})
        job = Job(self.host, self.repository, event_name, sha, **options)
        self.addCleanup(job.close)
        self.reset_counts()
        return job.run()

    def base(self, sha=None, **options):
        return self.job('push', sha or self.repository.commit('base'), **options)

    def pull_request(self, head, base, **options):
        self.host.head = head
        event = event_pull_request(1, head, base, **{k: options.pop(k) for k in ('head_repository', 'draft') if k in options})
        return self.job('pull_request', head, event=event, base_ref='main', **options)

    def reset_counts(self):
        self.counts.unlink(missing_ok=True)

    def executed(self):
        return sorted(self.counts.read_text().split()) if self.counts.exists() else []

    def assert_saved_results(self, job):
        files = self.host.artifact_files(job.run_id)
        self.assertIsNotNone(files, job.stdout)
        for name in ('budget-run.json', 'comparison-input.json', 'comparison-report.json', 'report.md', 'report-job.json'):
            self.assertIn(name, files)
        self.assertFalse([name for name in files if name.startswith('.')], 'state is not uploaded')
        return files

    def history_of(self, run_id):
        files = self.host.artifact_files(run_id)
        return json.loads(files['rolling-history.json']) if files and 'rolling-history.json' in files else None


class Lifecycle(Journey):
    def test_pull_request_report_from_a_real_base_history(self):
        self.catalog(enforced=10)
        self.repository.commit('catalog')
        for _ in range(6):
            base = self.base()
        # Base pushes save the same result format and post nothing.
        self.assertEqual(base.exit_code, 0, base.stdout)
        self.assertEqual(self.host.comments, [])
        files = self.assert_saved_results(base)
        rolling = self.history_of(base.run_id)
        self.assertEqual(len(rolling['runs']), 6)
        self.assertEqual(rolling['runs'][-1]['head_sha'], base.sha)

        base_sha = base.sha
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        self.catalog(requests=20, enforced=99, shadow=70)
        head = self.repository.commit('a change that regresses')
        job = self.pull_request(head, base_sha, draft=True)

        # Both events save the identical format.
        self.assertEqual(set(job.saved('budget-run.json')), set(json.loads(files['budget-run.json'])))
        # A draft on the same path: every runnable budget ran exactly once, unlabelled ones too.
        self.assertEqual(self.executed(), sorted(
            ['requests-read', 'bundle-bytes', 'coverage-percent', 'walltime', 'enforced-limit', 'shadow-limit']))
        # The enforced overrun is the ordinary failing exit; the report-only one adds nothing.
        self.assertEqual(job.exit_code, 1, job.stdout)
        rows = job.rows()
        self.assertEqual(set(rows), {'requests-read', 'bundle-bytes', 'coverage-percent', 'walltime', 'enforced-limit',
                                     'shadow-limit', 'needs-a-tool', 'needs-predecessor', 'job-seconds'})
        self.assertEqual(rows['requests-read']['current'], 20.0)
        self.assertEqual(rows['requests-read']['baseline_n'], 6)
        self.assertEqual(rows['bundle-bytes']['unit'], 'bytes')
        self.assertEqual(rows['coverage-percent']['direction'], 'min')
        self.assertEqual(rows['enforced-limit']['verdict'], 'over')
        self.assertEqual(rows['shadow-limit']['verdict'], 'over')
        self.assertTrue(rows['shadow-limit']['flag'], rows['shadow-limit'])
        self.assertTrue(rows['requests-read']['flag'])
        self.assertIn('no-such-tool-on-this-runner', rows['needs-a-tool']['unavailable'])
        self.assertIn('.telemetry/predecessor.json', rows['needs-predecessor']['unavailable'])
        verdicts = [row.get('verdict') for row in job.saved('comparison-report.json')['rows']]
        self.assertEqual(verdicts[:2], ['over', 'over'], 'failed rows lead')

        # The one comment shows the table; a report-only row says so with its flag kept.
        self.assertEqual(len(self.host.comments), 1)
        body = self.host.comments[0]['body']
        self.assertIn('budgets.yaml / enforced-limit', body)
        shadow_row = next(line for line in body.splitlines() if 'shadow-limit' in line and line.startswith('|'))
        self.assertIn('report-only', shadow_row)
        self.assertIn('over', shadow_row)
        self.assertIn('constant-baseline-change', shadow_row)
        # The own row was unavailable while running; the saved artifact holds its verdict.
        own_row = next(line for line in body.splitlines() if 'job-seconds' in line and line.startswith('|'))
        self.assertIn('measured once', own_row)
        saved = json.loads(self.host.artifact_files(job.run_id)['budget-run.json'])
        own = next(result for result in saved['results']['results'] if result['id'] == 'job-seconds')
        self.assertEqual(own['verdict'], 'within')
        self.assertLess(own['actual'], 120)
        self.assertFalse([row for row in saved['unavailable'] if row['id'] == 'job-seconds'])
        self.assertEqual(saved['enforcement_exclude_labels'], ['provisional'])
        # A pull request never writes the base history.
        self.assertIsNone(self.history_of(job.run_id))
        self.assertEqual(job.outcomes['final-upload'], 'success')
        main = json.loads(self.host.artifact_files(job.run_id, 'budget-results-main')['budget-run.json'])
        self.assertFalse([r for r in main['results']['results'] if r['id'] == 'job-seconds'],
                         'the main upload precedes the own verdict, and stays immutable')

        # Another head updates that comment and retains the first head's flags.
        self.catalog(requests=7, enforced=10, shadow=70)
        second = self.repository.commit('a fix')
        self.host.clear_requests()
        update = self.pull_request(second, base_sha)
        self.assertEqual(update.exit_code, 0, update.stdout)
        self.assertEqual(len(self.host.comments), 1)
        body = self.host.comments[0]['body']
        self.assertIn(head, body)
        self.assertIn(second, body)
        self.assertTrue([line for line in body.splitlines() if line.startswith('- ') and 'requests-read' in line],
                        f'the first head keeps its flag after the second fixed it: {body}')

        # An identical rerun neither duplicates the comment nor its head's entry.
        entries = self.host.comments[0]['body'].split('### Per-head adverse flags')[1].count(second)
        rerun = self.pull_request(second, base_sha, run_id=update.run_id, attempt=2)
        self.assertEqual(rerun.exit_code, 0)
        self.assertEqual(len(self.host.comments), 1)
        retained = self.host.comments[0]['body'].split('### Per-head adverse flags')[1]
        self.assertEqual(retained.count(second), entries, 'one entry per head, however many attempts')

        # The saved input reproduces this report offline through the direct tool.
        with tempfile.TemporaryDirectory() as directory:
            saved_input = Path(directory) / 'comparison-input.json'
            saved_input.write_bytes(self.host.artifact_files(update.run_id)['comparison-input.json'])
            reproduced = harness.bounded_run(
                [str(update.temp / 'onebudgetspec-action/tools/onebudgetspec-github-action'), 'reproduce',
                 '--input', str(saved_input), '--json'],
                capture_output=True, text=True, check=False, cwd=directory, env=clean_environment())
            self.assertEqual(reproduced.returncode, 0, reproduced.stderr)
            rows = {row['id']: row for row in json.loads(reproduced.stdout)['rows']}
            self.assertEqual(rows, {row['id']: row for row in json.loads(
                self.host.artifact_files(update.run_id)['comparison-report.json'])['rows']})
            self.assertEqual(rows['shadow-limit']['verdict'], 'over')

    def test_an_actions_token_keeps_one_comment_across_heads(self):
        self.host.actions_bot()
        self.repository.write('budgets.yaml', budgets_yaml([('figure', from_data(), {})]))
        self.repository.write('values-figure', '11\n')
        base = self.repository.commit('catalog')
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        self.repository.write('values-figure', '22\n')
        first = self.repository.commit('first head')
        self.pull_request(first, base)
        self.assertEqual(len(self.host.comments), 1)
        self.assertIn(f'head: {first}', self.host.comments[0]['body'])

        self.repository.write('values-figure', '33\n')
        second = self.repository.commit('second head')
        job = self.pull_request(second, base)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertNotIn('report not delivered', job.markdown())
        [comment] = self.host.comments
        self.assertIn(f'head: {second}', comment['body'])
        self.assertNotIn(f'head: {first}', comment['body'])
        row = next(line for line in comment['body'].splitlines() if line.startswith('|') and 'figure' in line)
        self.assertIn('33.000', row)
        self.assertEqual(self.host.count(r'/issues/1/comments$'), 1, 'only the first run creates')

    def test_enforced_error_keeps_its_exit_and_its_completed_results(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('fine', reported(1), {}),
            ('broken', ['bash', '-c', f'{COUNT} exit 9'], {}),
        ]))
        job = self.base(self.repository.commit('catalog'))
        self.assertEqual(job.exit_code, 3, job.stdout)
        rolling = self.history_of(job.run_id)
        results = {r['id']: r for r in rolling['runs'][-1]['results']['results']}
        self.assertEqual(results['fine']['actual'], 1.0)
        self.assertEqual(results['broken']['verdict'], 'error')

    def test_report_only_partition_runs_each_command_once(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('enforced-a', reported(1), {}),
            ('enforced-b', reported(500), {}),
            ('shadow', reported(500), {'labels': ['provisional']}),
        ]))
        job = self.base(self.repository.commit('catalog'))
        self.assertEqual(self.executed(), ['enforced-a', 'enforced-b', 'shadow'])
        self.assertEqual(job.exit_code, 1, 'enforced-b is over; shadow alone would not fail')
        saved = job.saved('budget-run.json')
        self.assertEqual({r['id']: r['verdict'] for r in saved['results']['results']},
                         {'enforced-a': 'within', 'enforced-b': 'over', 'shadow': 'over'})

        self.repository.write('budgets.yaml', budgets_yaml([('shadow', reported(500), {'labels': ['provisional']})]))
        alone = self.base(self.repository.commit('only report-only'))
        self.assertEqual(alone.exit_code, 0, alone.stdout)
        self.assertIn('report-only', alone.markdown())
        self.assertEqual(alone.rows()['shadow']['verdict'], 'over')

        # The default enforces everything.
        enforced = self.base(self.repository.commit('default'), inputs={'token': 'real-token'})
        self.assertEqual(enforced.exit_code, 1)

    def release_tail_catalog(self, *others):
        """A command the argv check passes, that needs a tool only a release job has."""
        self.repository.write('scripts/figures.sh', f'{COUNT} echo "onevcs is not on PATH" >&2\nexit 1\n')
        self.repository.write('budgets.yaml', budgets_yaml([
            ('steady', reported(5), {}),
            ('release-figure', ['bash', 'scripts/figures.sh'], {'labels': ['provisional']}),
            *others,
        ]))

    def test_a_failing_report_only_command_is_unavailable_with_its_own_message(self):
        self.release_tail_catalog(
            ('own-failure', ['onebudgetspec-github-action', 'telemetry', '--metric', 'no-such-metric',
                             '--input', '.telemetry/own.json'], {'labels': ['provisional']}),
        )
        base = self.repository.commit('catalog')
        job = self.pull_request(base, base)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.executed(), ['release-figure', 'steady'], 'it still ran, once')

        saved = job.saved('budget-run.json')
        self.assertEqual({r['id']: r['verdict'] for r in saved['results']['results']}, {'steady': 'within'})
        reasons = {row['id']: row['reason'] for row in saved['unavailable']}
        self.assertEqual(set(reasons), {'release-figure', 'own-failure'})
        self.assertIn('onevcs is not on PATH', reasons['release-figure'])
        # A command reading this job's own telemetry fails later, and reads the same way.
        self.assertIn('no-such-metric', reasons['own-failure'])

        rows = job.rows()
        self.assertNotIn('verdict', rows['release-figure'])
        self.assertIn('onevcs is not on PATH', rows['release-figure']['unavailable'])
        body = self.host.comments[0]['body']
        comment_row = next(line for line in body.splitlines() if 'release-figure' in line and line.startswith('|'))
        self.assertIn('onevcs is not on PATH', comment_row)
        self.assertIn('unavailable', comment_row)
        self.assertNotIn('| error', comment_row)

    def test_a_base_run_whose_report_only_commands_all_failed_saves_no_measurement(self):
        self.release_tail_catalog()
        first = self.base(self.repository.commit('catalog'))
        self.assertEqual(first.exit_code, 0, first.stdout)
        self.assertEqual(len(self.history_of(first.run_id)['runs']), 1, 'steady measured, so it is history')

        self.repository.write('budgets.yaml', budgets_yaml([
            ('release-figure', ['bash', 'scripts/figures.sh'], {'labels': ['provisional']}),
        ]))
        alone = self.base(self.repository.commit('nothing else'))
        self.assertEqual(alone.exit_code, 0, alone.stdout)
        self.assertIsNone(self.history_of(alone.run_id))
        self.assertIn('no rolling file saved', alone.markdown())

    def test_a_failing_enforced_command_stays_an_error_and_fails_the_job(self):
        enforced = ['bash', '-c', f'{COUNT} echo "onevcs is not on PATH" >&2; exit 1']
        self.release_tail_catalog(
            ('enforced-figure', enforced, {}),
            ('other-label-figure', enforced, {'labels': ['not-excluded']}),
        )
        base = self.repository.commit('catalog')
        job = self.pull_request(base, base)
        self.assertEqual(job.exit_code, 3, job.stdout)

        saved = job.saved('budget-run.json')
        results = {r['id']: r for r in saved['results']['results']}
        self.assertEqual([row['id'] for row in saved['unavailable']], ['release-figure'])
        for budget in ('enforced-figure', 'other-label-figure'):
            self.assertEqual(results[budget]['verdict'], 'error')
            self.assertIn('onevcs is not on PATH', results[budget]['error'])
            self.assertEqual(job.rows()[budget]['verdict'], 'error')
        verdicts = [row.get('verdict') for row in job.saved('comparison-report.json')['rows']]
        self.assertEqual(verdicts[:2], ['error', 'error'], 'failed rows lead')

        # With no exclusion, the very command that was report-only is an enforced error.
        everything = self.pull_request(base, base, inputs={'token': 'real-token'})
        self.assertEqual(everything.exit_code, 3, everything.stdout)
        self.assertEqual(everything.rows()['release-figure']['verdict'], 'error')

    def test_a_report_only_budget_that_measures_keeps_its_verdict_and_flags(self):
        self.repository.write('values-shadow', '10\n')
        self.repository.write('budgets.yaml', budgets_yaml([
            ('shadow', from_data(), {'threshold': 40, 'labels': ['provisional']}),
        ]))
        self.repository.commit('catalog')
        for _ in range(6):
            base = self.base()
        self.repository.write('values-shadow', '70\n')
        head = self.repository.commit('a regression')
        job = self.pull_request(head, base.sha)
        self.assertEqual(job.exit_code, 0, job.stdout)

        rows = job.rows()
        self.assertEqual(rows['shadow']['verdict'], 'over')
        self.assertEqual(rows['shadow']['current'], 70.0)
        self.assertTrue(rows['shadow']['flag'], rows['shadow'])
        self.assertEqual(job.saved('budget-run.json')['unavailable'], [])
        comment_row = next(line for line in self.host.comments[0]['body'].splitlines()
                           if 'shadow' in line and line.startswith('|'))
        self.assertIn('report-only', comment_row)
        self.assertIn('over', comment_row)

    def test_invalid_budgets_and_inputs_are_ordinary_refusals(self):
        self.repository.write('budgets.yaml', 'schema_version: 1\nbudgets: [{unknown: key}]\n')
        job = self.base(self.repository.commit('broken file'))
        self.assertEqual(job.exit_code, 2, job.stdout)
        self.assertIn('refused', job.stdout)
        self.repository.write('budgets.yaml', budgets_yaml([('fine', reported(1), {})]))
        sha = self.repository.commit('valid file')
        job = self.base(sha, inputs={'token': 'real-token', 'enforcement-exclude-labels': 'Not A Label'})
        self.assertEqual(job.exit_code, 2)
        self.assertIn('must match', job.stdout)

    def test_an_empty_catalog_reports_success(self):
        job = self.base(self.repository.commit('no budgets'))
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(job.saved('comparison-report.json')['rows'], [])
        self.assertIsNone(self.history_of(job.run_id), 'no measurement is ever appended')
        self.assertIn('no rolling file', job.markdown())


class Diagnostics(Journey):
    def test_first_run_shows_every_row_with_its_reason(self):
        self.catalog()
        base_sha = self.repository.commit('catalog')
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        head = self.repository.commit('change')
        job = self.pull_request(head, base_sha)
        self.assertEqual(job.exit_code, 0, job.stdout)
        rows = job.rows()
        self.assertEqual(rows['requests-read']['baseline_n'], 0)
        self.assertIn('baseline unavailable', rows['requests-read']['unavailable'])
        self.assertEqual(len(rows), 9)
        self.assertEqual(len(self.host.comments), 1)
        self.assertIn('Budget report', job.summary)

    def test_partial_baseline_is_unavailable_below_five_and_provisional_after(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        self.repository.commit('catalog')
        for _ in range(4):
            base = self.base()
        self.repository.git('checkout', '--quiet', '-b', 'few')
        job = self.pull_request(self.repository.commit('change'), base.sha)
        self.assertEqual(job.rows()['steady']['baseline_n'], 4)
        self.assertIn('minimum 5', job.rows()['steady']['unavailable'])
        self.repository.git('checkout', '--quiet', 'main')
        base = self.base()
        self.repository.git('checkout', '--quiet', '-b', 'enough')
        job = self.pull_request(self.repository.commit('change'), base.sha)
        self.assertEqual(job.rows()['steady']['baseline_n'], 5)
        self.assertIn('provisional', job.markdown())

    def test_transport_failures_are_diagnostics_that_preserve_a_real_failure(self):
        self.catalog(enforced=10)
        base = self.base(self.repository.commit('catalog'))
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        head = self.repository.commit('change')
        # Posting refused: the job passes with the reason saved.
        self.host.write_status = 403
        job = self.pull_request(head, base.sha)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertIn('not delivered', job.summary)
        self.assertEqual(self.host.comments, [])
        self.host.write_status = None
        # Rate limited with nothing to wait on: the baseline is unavailable, not a failure.
        self.host.refuse.append({'path': r'actions/runs/\d+$', 'status': 429, 'times': 1})
        job = self.pull_request(head, base.sha)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertIn('baseline unavailable', job.markdown())
        # A credential the host refuses, and a genuine overrun under it: only the overrun fails.
        self.catalog(enforced=99)
        head = self.repository.commit('now over')
        self.host.refuse.append({'path': '.', 'status': 401, 'times': 30})
        job = self.pull_request(head, base.sha)
        self.assertEqual(job.exit_code, 1, job.stdout)
        self.assertIn('not delivered', job.markdown())
        self.assertEqual(job.rows()['enforced-limit']['verdict'], 'over')
        self.assertIn('credential', job.rows()['requests-read']['unavailable'])

    def test_rate_limit_retry_honours_retry_after_and_is_counted(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('reads', reported(3, 'gh api --include repos/owner/repo/commits/main > /dev/null;'), {}),
            ('over-budget', reported(500), {}),
        ]))
        base = self.base(self.repository.commit('catalog'))
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        head = self.repository.commit('change')
        self.host.clear_requests()
        self.host.refuse.append({'path': r'artifacts\?', 'status': 429, 'times': 1, 'retry_after': 1})
        job = self.pull_request(head, base.sha)
        self.assertEqual(job.exit_code, 1)
        metric = job.saved('report-job.json')['metrics']['report-github-api-requests-per-run']
        self.assertIn('retries=1', metric['detail'])
        self.assertEqual(metric['value'], self.host.count(''), 'the counter agrees with the host')
        self.assertEqual(self.host.count(r'artifacts\?'), 2, 'the retry is one more request')
        self.assertEqual(job.rows()['reads']['baseline_n'], 1)

    def test_a_rate_limited_budget_read_is_unavailable_while_a_real_overrun_fails(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('reads', ['bash', '-c', f'{COUNT} gh api repos/owner/repo/commits/main'], {}),
            ('over-budget', reported(500), {}),
        ]))
        self.host.refuse.append({'path': 'commits/main', 'status': 429, 'times': 1})
        job = self.base(self.repository.commit('catalog'))
        self.assertEqual(job.exit_code, 1, 'the overrun, not the rate limit')
        rows = job.rows()
        self.assertIn('rate limit', rows['reads']['unavailable'])
        self.assertNotEqual(rows['reads'].get('verdict'), 'error')
        self.assertEqual(rows['over-budget']['verdict'], 'over')
        self.assertEqual(self.executed().count('reads'), 1)
        self.host.refuse.append({'path': 'commits/main', 'status': 429, 'times': 1})
        self.repository.write('budgets.yaml', budgets_yaml([
            ('reads', ['bash', '-c', 'gh api repos/owner/repo/commits/main'], {}),
        ]))
        job = self.base(self.repository.commit('only reads'))
        self.assertEqual(job.exit_code, 0, 'nothing else failed')

    def test_a_failed_predecessor_leaves_its_telemetry_unavailable(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('reads-saved', ['cat', '.telemetry/saved.json'], {'labels': ['ci-report'], 'measure': 'elapsed', 'unit': 'seconds'}),
            ('other', reported(1), {}),
        ]))
        sha = self.repository.commit('catalog')
        job = self.base(sha)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertIn('.telemetry/saved.json', job.rows()['reads-saved']['unavailable'])
        self.assertIn('labelled as measurable', job.markdown())
        # A predecessor that saved it: the existing command reads it, nothing is rerun.
        self.repository.write('.telemetry/saved.json', '{}')
        job = self.base(sha, attempt=2)
        self.assertEqual(job.rows()['reads-saved']['verdict'], 'within')

    def test_install_failures_are_diagnostic(self):
        self.repository.write('budgets.yaml', budgets_yaml([('over-budget', reported(500), {})]))
        sha = self.repository.commit('catalog')
        archive = next(name for name in self.host.assets if name.endswith('.gz'))
        # A wrong digest and a line that is no digest at all: neither may pass as verified.
        for attempt, digest in enumerate(('0' * 64, '0000'), start=1):
            self.host.assets['SHA256SUMS'] = f'{digest}  {archive}\n'.encode()
            job = self.base(sha, attempt=attempt)
            self.assertEqual(job.exit_code, 0, job.stdout)
            self.assertIn('checksum', job.summary)
            self.assertEqual(self.executed(), [])
        self.host.assets = {}
        job = self.base(sha, attempt=3)
        self.assertEqual(job.exit_code, 0)
        self.assertIn('cannot download', job.summary)


class Forks(Journey):
    def test_fork_gets_a_read_only_summary_and_no_credential(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('token-free', ['bash', '-c', f'{COUNT} [ -z "$GH_TOKEN$GITHUB_TOKEN" ]; echo "{{\\"value\\": $?}}" > "$ONEBUDGETSPEC_RESULT"'], {}),
            ('needs-host', ['bash', '-c', 'gh api repos/owner/repo/commits/main'], {}),
        ]))
        base = self.base(self.repository.commit('catalog'))
        self.repository.git('checkout', '--quiet', '-b', 'fork-change')
        head = self.repository.commit('fork change')
        self.host.clear_requests()
        job = self.pull_request(head, base.sha, head_repository='someone/fork')
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.host.comments, [])
        self.assertEqual(self.host.count('comments|graphql'), 0, 'no comment is even read')
        self.assertEqual(self.host.count('commits/main'), 0, 'no budget command reached the host')
        rows = job.rows()
        self.assertEqual(rows['token-free']['current'], 0.0, 'the fork command saw no token')
        self.assertEqual(rows['token-free']['baseline_n'], 1, 'the baseline is still readable')
        self.assertEqual(json.loads(self.host.artifact_files(base.run_id)['budget-run.json'])['results']['results'][0]['actual'],
                         1.0, 'a base run does hold the token')
        self.assertIn('no GitHub credential', rows['needs-host']['unavailable'])
        self.assertIn('someone/fork', job.summary)
        self.assertIsNotNone(self.host.artifact_files(job.run_id))
        self.assertIsNone(self.history_of(job.run_id))

    def test_privileged_events_run_no_command(self):
        self.repository.write('budgets.yaml', budgets_yaml([('would-run', reported(500), {})]))
        head = self.repository.commit('fork code')
        job = self.job('pull_request_target', head, event=event_pull_request(1, head, head, 'someone/fork'), base_ref='main')
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.executed(), [])
        self.assertIn('privileged', job.rows()['would-run']['unavailable'])
        self.assertEqual(self.host.count(''), 0, 'no GitHub API request at all')
        self.assertEqual(self.host.transferred('artifact-service'), 2, 'the one saved result upload')


class History(Journey):
    def catalog_one(self, value=5):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(value), {})]))

    def test_base_carries_history_forward_and_trims_to_twenty(self):
        self.catalog_one()
        self.repository.commit('catalog')
        heads = []
        for _ in range(22):
            job = self.base()
            heads.append(job.sha)
        rolling = self.history_of(job.run_id)
        self.assertEqual([run['head_sha'] for run in rolling['runs']], heads[-20:])
        self.assertEqual(rolling['repository'], 'owner/repo')
        # A rerun of the same commit replaces its entry.
        rerun = self.base(job.sha, run_id=job.run_id, attempt=2)
        rolling = self.history_of(job.run_id)
        self.assertEqual([run['head_sha'] for run in rolling['runs']], heads[-20:])
        self.assertEqual(rolling['runs'][-1]['attempt'], 2)
        self.assertEqual(rerun.exit_code, 0)

    def test_failed_upload_leaves_the_prior_artifact_and_discloses_the_loss(self):
        self.catalog_one()
        first = self.base(self.repository.commit('catalog'))
        self.host.upload_failures = 1
        failed = self.base()
        self.assertEqual(failed.exit_code, 0, failed.stdout)
        self.assertEqual(failed.outcomes['main-upload'], 'failure')
        self.assertIsNone(self.host.artifact_files(failed.run_id))
        self.assertIn('the previous artifact stays intact', failed.markdown())
        self.assertIsNotNone(self.host.artifact_files(first.run_id))
        following = self.base()
        rolling = self.history_of(following.run_id)
        self.assertEqual([run['head_sha'] for run in rolling['runs']], [first.sha, following.sha])
        self.assertIn(failed.sha, following.markdown())
        self.assertIn('lost', following.markdown())

    def own_catalog(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('steady', reported(5), {}),
            ('job-seconds', OWN, {'unit': 'seconds', 'threshold': 120, 'labels': ['ci-report']}),
        ]))

    def test_failed_final_persistence_leaves_accurate_results_and_counts(self):
        self.own_catalog()
        first = self.base(self.repository.commit('catalog'))
        self.host.clear_requests()
        self.host.uploads_before_failing = 1
        self.host.upload_failures = 1
        failed = self.base()
        self.assertEqual(failed.exit_code, 0, failed.stdout)
        self.assertEqual(failed.outcomes['main-upload'], 'success')
        self.assertEqual(failed.outcomes['final-upload'], 'failure')
        self.assertIn('artifact budget-results-main holds the results', failed.summary)
        # The main results survive, saying truthfully what they do not hold.
        files = self.host.artifact_files(failed.run_id, 'budget-results-main')
        saved = json.loads(files['budget-run.json'])
        self.assertEqual([r['id'] for r in saved['results']['results']], ['steady'])
        self.assertIn('measured once', next(r['reason'] for r in saved['unavailable'] if r['id'] == 'job-seconds'))
        self.assertIsNone(self.host.artifact_files(failed.run_id), 'no half-written final artifact')
        # The count saved before any upload equals everything the API received: uploads, failed
        # or not, are transfers, and were tried and recorded without entering the figure.
        interim = json.loads(files['report-job.json'])
        metric = interim['metrics']['report-github-api-requests-per-run']
        self.assertEqual(metric['value'], self.host.count(''), self.host.requests)
        self.assertIn('still pending', interim['persistence'])
        self.assertEqual(self.host.transferred('artifact-service'), 3, 'create and finalize, then the refused create')
        self.assertEqual(self.host.transferred('blob-storage'), 2)
        # Recovery: the next push reads the run before it, discloses the loss, inserts nothing else.
        following = self.base()
        heads = [run['head_sha'] for run in self.history_of(following.run_id)['runs']]
        self.assertEqual(heads, [first.sha, following.sha])
        self.assertIn(failed.sha, following.markdown())
        self.assertEqual(following.outcomes['final-upload'], 'success')

    def only_own_measures_catalog(self, own=OWN, labels=('ci-report',)):
        """A repository whose catalog budgets cannot run in the job, leaving only its own."""
        self.repository.write('budgets.yaml', budgets_yaml([
            ('needs-a-tool', ['no-such-tool-on-this-runner', '--measure'], {}),
            ('needs-predecessor', ['cat', '.telemetry/predecessor.json'], {}),
            ('job-seconds', own, {'unit': 'seconds', 'threshold': 120, 'labels': list(labels)}),
        ]))

    def test_a_base_run_measuring_only_its_own_budget_gives_pull_requests_a_baseline(self):
        self.only_own_measures_catalog()
        first = self.base(self.repository.commit('catalog'))
        self.assertEqual(first.exit_code, 0, first.stdout)
        self.assertEqual(first.outcomes['final-upload'], 'success')
        main = self.host.artifact_files(first.run_id, 'budget-results-main')
        self.assertNotIn('rolling-history.json', main, 'nothing is measured before the own command')
        rolling = self.history_of(first.run_id)
        self.assertEqual([run['head_sha'] for run in rolling['runs']], [first.sha])
        [own] = rolling['runs'][-1]['results']['results']
        self.assertEqual(own['id'], 'job-seconds')
        self.assertEqual(own['verdict'], 'within')
        self.assertGreater(own['actual'], 0)
        self.assertEqual(sorted(row['id'] for row in rolling['runs'][-1]['unavailable']),
                         ['needs-a-tool', 'needs-predecessor'])
        self.assertNotIn('no rolling file saved', first.markdown())
        self.assertIn('base history starts fresh', first.markdown())

        # The next push carries it forward, and a rerun replaces its own entry.
        second = self.base()
        self.assertEqual([run['head_sha'] for run in self.history_of(second.run_id)['runs']], [first.sha, second.sha])
        self.assertNotIn('base history starts fresh', second.markdown())
        rerun = self.base(second.sha, run_id=second.run_id, attempt=2)
        rolling = self.history_of(rerun.run_id)
        self.assertEqual([run['head_sha'] for run in rolling['runs']], [first.sha, second.sha])
        self.assertEqual(rolling['runs'][-1]['attempt'], 2)

        # A pull request reads that file: its comment and its saved report both show the samples.
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        job = self.pull_request(self.repository.commit('a change'), second.sha)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(job.rows()['job-seconds']['baseline_n'], 2, job.markdown())
        self.assertIn('2 eligible samples', job.rows()['job-seconds']['unavailable'])
        self.assertEqual(job.rows()['needs-a-tool']['baseline_n'], 0)
        comment_row = next(line for line in self.host.comments[0]['body'].splitlines()
                           if line.startswith('|') and 'job-seconds' in line)
        self.assertIn('2 eligible samples', comment_row)

    def test_a_base_run_whose_only_budget_cannot_measure_saves_no_rolling_file(self):
        # With no own command at all, nothing is left for `finish` to decide.
        self.repository.write('budgets.yaml', budgets_yaml([
            ('needs-a-tool', ['no-such-tool-on-this-runner', '--measure'], {}),
        ]))
        nothing = self.base(self.repository.commit('catalog'))
        self.assertEqual(nothing.exit_code, 0, nothing.stdout)
        self.assertIsNotNone(self.host.artifact_files(nothing.run_id))
        self.assertIsNone(self.history_of(nothing.run_id))
        self.assertIn('no rolling file saved: this run completed no measurement', nothing.markdown())

        # An own command that fails measures nothing either, enforced or report-only.
        unreadable = [*OWN[:2], '--metric', 'no-such-metric', *OWN[4:]]
        self.only_own_measures_catalog(own=unreadable)
        enforced = self.base(self.repository.commit('an enforced own command that errors'))
        self.assertEqual(enforced.exit_code, 3, enforced.stdout)
        self.assertEqual(enforced.outcomes['final-upload'], 'success')
        self.assertEqual(enforced.rows()['job-seconds']['verdict'], 'error')
        self.assertIsNone(self.history_of(enforced.run_id))
        self.assertIn('no rolling file saved: this run completed no measurement', enforced.markdown())

        self.only_own_measures_catalog(own=unreadable, labels=['provisional'])
        shadow = self.base(self.repository.commit('a report-only own command that fails'))
        self.assertEqual(shadow.exit_code, 0, shadow.stdout)
        self.assertIn('report-only measurement failed', shadow.rows()['job-seconds']['unavailable'])
        self.assertIsNone(self.history_of(shadow.run_id))
        self.assertIn('no rolling file saved: this run completed no measurement', shadow.markdown())

        # An errored row with no figure beside it is not a measurement in the report phase either.
        self.repository.write('budgets.yaml', budgets_yaml([('broken', ['bash', '-c', f'{COUNT} exit 9'], {})]))
        broken = self.base(self.repository.commit('a command that errors'))
        self.assertEqual(broken.exit_code, 3, broken.stdout)
        self.assertIsNone(self.history_of(broken.run_id))
        self.assertIn('no rolling file saved: this run completed no measurement', broken.markdown())

        # None of them entered the history, so measuring again starts it fresh.
        self.only_own_measures_catalog()
        again = self.base(self.repository.commit('measures again'))
        self.assertEqual([run['head_sha'] for run in self.history_of(again.run_id)['runs']], [again.sha])

    def test_a_base_run_with_measured_budgets_keeps_its_rows_and_holds_its_own_once(self):
        self.own_catalog()
        first = self.base(self.repository.commit('catalog'))
        second = self.base()
        rolling = self.history_of(second.run_id)
        self.assertEqual([run['head_sha'] for run in rolling['runs']], [first.sha, second.sha])
        for run in rolling['runs']:
            results = {result['id']: result for result in run['results']['results']}
            self.assertEqual(sorted(result['id'] for result in run['results']['results']), ['job-seconds', 'steady'])
            self.assertEqual(results['steady']['actual'], 5.0)
            self.assertEqual(results['job-seconds']['verdict'], 'within')
            self.assertEqual(run['unavailable'], [])
        # The report-phase upload already held the history; the final one adds only the own verdict.
        main = json.loads(self.host.artifact_files(second.run_id, 'budget-results-main')['rolling-history.json'])
        self.assertEqual([r['id'] for r in main['runs'][-1]['results']['results']], ['steady'])
        self.assertEqual(main['runs'][0], rolling['runs'][0])

    def test_a_retried_artifact_service_call_is_a_transfer_and_the_count_holds(self):
        self.own_catalog()
        self.repository.commit('catalog')
        self.host.clear_requests()
        self.host.transient_finalize_failures = 1
        job = self.base()
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.host.transferred('artifact-service'), 5, 'two uploads and the real client retry')
        metric = job.saved('report-job.json')['metrics']['report-github-api-requests-per-run']
        self.assertEqual(metric['value'], self.host.count(''))

    def test_real_gh_exchanges_are_counted_not_its_invocations(self):
        graphql = 'query($endCursor: String) { pagedItems(after: $endCursor) { nodes pageInfo { hasNextPage endCursor } } }'
        self.repository.write('budgets.yaml', budgets_yaml([
            ('one-paged-rest-call', reported(1, 'gh api --paginate repos/owner/repo/items > /dev/null || exit 1;'), {}),
            ('one-paged-graphql-call', reported(1, f"gh api graphql --paginate -f query='{graphql}' > /dev/null || exit 1;"), {}),
            ('not-the-api-verb', reported(1, 'gh repo view owner/repo --json name > /dev/null || exit 1;'), {}),
            ('no-request-at-all', reported(1, 'gh --version > /dev/null || exit 1;'), {}),
            ('own-seconds', OWN, {'unit': 'seconds', 'threshold': 120}),
        ]))
        self.repository.commit('catalog')
        self.host.clear_requests()
        job = self.base()
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.host.count('/items'), 3, 'one invocation, three pages')
        self.assertEqual(self.host.count('^/graphql$'), 3, 'one invocation with two pages, and gh repo view')
        metric = job.saved('report-job.json')['metrics']['report-github-api-requests-per-run']
        self.assertEqual(metric['value'], self.host.count(''), self.host.requests)
        self.assertIn('commands=6', metric['detail'])
        saved = job.saved('budget-run.json')['results']['results']
        self.assertEqual({r['verdict'] for r in saved}, {'within'}, 'each command really got its answers')
        # The request lines are this tool's reading, never a command's output.
        self.assertNotIn('* Request', job.stdout)

    def test_debug_output_a_command_asked_for_stays(self):
        stderr = Path(self.counts_directory.name) / 'stderr'
        self.repository.write('budgets.yaml', budgets_yaml([
            ('asks-for-debug', reported(1, f'GH_DEBUG=api gh api repos/owner/repo/commits/a > /dev/null 2> {stderr} || exit 1;'), {}),
        ]))
        job = self.base(self.repository.commit('catalog'))
        self.assertIn('* Request to', stderr.read_text())
        metric = job.saved('report-job.json')['metrics'][API_METRIC]
        self.assertEqual(metric['value'], self.host.count(''))

    def test_missing_request_instrumentation_is_unavailable_never_an_estimate(self):
        # A gh that answers without saying what it sent: the count cannot be established.
        silent = Path(self.counts_directory.name) / 'silent-gh'
        silent.write_text('#!/usr/bin/env bash\necho "{}"\n')
        silent.chmod(0o755)
        own = ['onebudgetspec-github-action', 'telemetry', '--metric', API_METRIC, '--input', '.telemetry/report-job.json']
        catalog = [
            ('reads', reported(1, 'gh api anything > /dev/null || exit 1;'), {}),
            ('own-requests', own, {'unit': 'requests', 'threshold': 20}),
            ('own-seconds', OWN, {'unit': 'seconds', 'threshold': 120}),
        ]
        self.repository.write('budgets.yaml', budgets_yaml(catalog))
        environment = {'ONEVCS_GH': str(silent), 'JOURNEY_COUNTS': str(self.counts)}
        job = self.base(self.repository.commit('catalog'), environment=environment)
        self.assertEqual(job.exit_code, 0, job.stdout)
        metric = job.saved('report-job.json')['metrics'][API_METRIC]
        self.assertIsNone(metric['value'], 'no passing number stands in for an unknown count')
        self.assertIn('no record of its requests', metric['unavailable'])
        rows = job.rows()
        self.assertIn('no record of its requests', rows['own-requests']['unavailable'])
        self.assertNotIn('verdict', rows['own-requests'])
        self.assertEqual(rows['own-seconds']['verdict'], 'within', 'a metric that is known still reports')
        self.assertEqual(rows['reads']['verdict'], 'within')
        # A known overrun under the same gap is still the ordinary failure.
        self.repository.write('budgets.yaml', budgets_yaml([*catalog, ('over-budget', reported(500), {})]))
        job = self.base(self.repository.commit('over'), environment=environment)
        self.assertEqual(job.exit_code, 1, job.stdout)
        self.assertEqual(job.rows()['over-budget']['verdict'], 'over')
        self.assertIsNone(job.saved('report-job.json')['metrics'][API_METRIC]['value'])

    def test_a_failed_gh_api_call_with_no_request_line_is_unavailable_too(self):
        # The real gh refuses this before sending anything, and everything else it does is on record.
        refused = 'gh api --no-such-flag repos/owner/repo/commits/refused > /dev/null'
        own = ['onebudgetspec-github-action', 'telemetry', '--metric', API_METRIC, '--input', '.telemetry/report-job.json']
        catalog = [
            ('counted', reported(1, READS_REST), {}),
            ('own-requests', own, {'unit': 'requests', 'threshold': 20}),
        ]
        self.repository.write('budgets.yaml', budgets_yaml(catalog))
        self.repository.commit('control')
        self.host.clear_requests()
        job = self.base()
        self.assertEqual(job.saved('report-job.json')['metrics'][API_METRIC]['value'], self.host.count(''),
                         'with every gh api call on record the same job has its number')
        for budget, read, expectation in (
            ('tolerates-failure', f'{refused} 2>&1 || true;', 'within'),
            ('fails', f'{refused} || exit 1;', 'report-only measurement failed'),
        ):
            with self.subTest(budget):
                self.repository.write('budgets.yaml', budgets_yaml([*catalog, (budget, reported(1, read), {'labels': ['provisional']})]))
                job = self.base(self.repository.commit(budget))
                self.assertEqual(job.exit_code, 0, job.stdout)
                metric = job.saved('report-job.json')['metrics'][API_METRIC]
                self.assertIsNone(metric['value'], 'no number stands in for an unknown count')
                self.assertIn('no record of its requests', metric['unavailable'])
                rows = job.rows()
                if expectation == 'within':
                    self.assertEqual(rows[budget]['verdict'], expectation)
                else:
                    self.assertNotIn('verdict', rows[budget])
                    self.assertIn(expectation, rows[budget]['unavailable'])
                self.assertIn('no record of its requests', rows['own-requests']['unavailable'])
                self.assertNotIn('verdict', rows['own-requests'])
                self.assertEqual(rows['counted']['verdict'], 'within')

    def test_a_gh_that_cannot_start_gives_no_record_either(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('tolerates-failure', reported(1, 'gh api repos/owner/repo/commits/a > /dev/null 2>&1 || true;'), {}),
        ]))
        job = self.base(self.repository.commit('catalog'), environment={'ONEVCS_GH': '/nonexistent/gh', 'JOURNEY_COUNTS': str(self.counts)})
        metric = job.saved('report-job.json')['metrics'][API_METRIC]
        self.assertIsNone(metric['value'], job.stdout)
        self.assertIn('no record of its requests', metric['unavailable'])

    def test_other_verbs_with_no_request_line_count_as_zero_and_the_telemetry_says_so(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('local-verb', reported(1, 'gh auth token > /dev/null || exit 1;'), {}),
            ('reads', reported(1, READS_REST), {}),
        ]))
        self.repository.commit('catalog')
        self.host.clear_requests()
        job = self.base()
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(job.rows()['local-verb']['verdict'], 'within')
        saved = job.saved('report-job.json')
        self.assertEqual(saved['metrics'][API_METRIC]['value'], self.host.count(''), 'the local verb adds nothing')
        self.assertIn('other than api that shows no request line counts as zero', saved['scope'])
        self.assertIn('requests from any other client are not seen', saved['scope'])

    def test_a_gh_api_call_that_shows_only_a_storage_transfer_is_on_record(self):
        transfer = 'gh api http://storage.github.localhost/simple/onebudgetspec-cli/ > /dev/null || exit 1;'
        self.repository.write('budgets.yaml', budgets_yaml([('transfer', reported(1, transfer), {})]))
        self.repository.commit('catalog')
        self.host.clear_requests()
        job = self.base()
        self.assertTrue(any('storage.github.localhost' in path for _, _, path in self.host.transfers), job.stdout)
        metric = job.saved('report-job.json')['metrics'][API_METRIC]
        self.assertEqual(metric['value'], self.host.count(''), 'a transfer is neither a request nor an unknown')

    def test_a_command_reading_github_through_curl_reconciles_at_the_boundary(self):
        read = 'curl -fsS -H "Authorization: Bearer $GH_TOKEN" http://api.github.localhost/repos/owner/repo/commits/curl > /dev/null || exit 1;'
        self.repository.write('budgets.yaml', budgets_yaml([
            ('reads-with-curl', reported(1, read), {}),
            ('reads-with-gh', reported(1, READS_REST), {}),
        ]))
        self.repository.commit('catalog')
        self.host.clear_requests()
        job = self.base()
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.executed(), ['reads-with-curl', 'reads-with-gh'])
        self.assertEqual(job.rows()['reads-with-curl']['verdict'], 'within')
        self.assertEqual(self.host.count('commits/curl'), 1)
        self.assertEqual(self.host.requests_not_through_gh, 1, 'the boundary knows the client')
        saved = job.saved('report-job.json')
        self.assertEqual(saved['metrics'][API_METRIC]['value'] + self.host.requests_not_through_gh, self.host.count(''))
        self.assertIn('only requests made through gh', saved['scope'])

    def test_a_runaway_command_stops_at_the_process_limit(self):
        # A chain of the kind the limit exists for: each link starts the next and waits for it.
        script = Path(self.counts_directory.name) / 'runaway'
        script.write_text('#!/usr/bin/env bash\nsleep 30 &\n"$0"\n')
        script.chmod(0o755)
        with self.assertRaisesRegex(AssertionError, 'ran away'):
            harness.bounded_run([str(script)])

    def test_a_gh_that_resolves_back_to_the_counter_cannot_loop(self):
        looping = Path(self.counts_directory.name) / 'looping-gh'
        looping.write_text('#!/usr/bin/env bash\nexec gh "$@"\n')
        looping.chmod(0o755)
        self.repository.write('budgets.yaml', budgets_yaml([('reads', ['bash', '-c', 'gh api repos/owner/repo/commits/a'], {})]))
        job = self.base(self.repository.commit('catalog'), environment={'ONEVCS_GH': str(looping)})
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertIn('needs the GitHub CLI', job.rows()['reads']['unavailable'])

    def test_plugin_follows_real_pages_and_every_page_is_counted(self):
        self.own_catalog()
        self.repository.commit('catalog')
        first = self.base()
        self.host.paged_artifacts = True
        self.host.clear_requests()
        routed = {'ONEVCS_GH': str(harness.ROUTE_API_ORIGIN), 'JOURNEY_COUNTS': str(self.counts)}
        job = self.base(environment=routed)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.host.count(r'/artifacts\?'), 2, 'the listing took two real gh calls')
        metric = job.saved('report-job.json')['metrics'][API_METRIC]
        self.assertEqual(metric['value'], self.host.count(''))
        self.assertIn('archive=4 (QueryCost 4)', metric['detail'])
        # Provenance held across the pages: the unrelated artifact on the first was never read.
        rolling = self.history_of(job.run_id)
        self.assertEqual([run['head_sha'] for run in rolling['runs']], [first.sha, job.sha])
        self.assertEqual(self.host.count('/artifacts/999/'), 0)
        # A later page that fails: the baseline is unavailable, its request still counted.
        self.host.refuse.append({'path': '&page=2$', 'status': 500, 'times': 5})
        self.host.clear_requests()
        failed = self.base(environment=routed)
        self.assertEqual(failed.exit_code, 0, failed.stdout)
        self.assertEqual(failed.saved('report-job.json')['metrics'][API_METRIC]['value'], self.host.count(''))
        self.assertIn('could not list its artifacts', failed.markdown())
        self.assertEqual(len(self.history_of(failed.run_id)['runs']), 1, 'a fresh history, nothing merged')

    def test_concurrent_pushes_never_merge_divergent_histories(self):
        self.catalog_one()
        first = self.base(self.repository.commit('catalog'))
        shared = first.sha
        self.repository.git('checkout', '--quiet', '-b', 'divergent')
        unrelated = self.base(self.repository.commit('a push that was force-replaced'), ref_name='main')
        self.repository.git('checkout', '--quiet', 'main')
        self.repository.git('branch', '-D', 'divergent')
        self.repository.git('reset', '--hard', '--quiet', shared)
        after = self.base(self.repository.commit('the real main'))
        heads = [run['head_sha'] for run in self.history_of(after.run_id)['runs']]
        self.assertEqual(heads, [shared, after.sha])
        self.assertNotIn(unrelated.sha, heads)

    def test_provenance_and_retention_failures_start_fresh_with_a_reason(self):
        self.catalog_one()
        first = self.base(self.repository.commit('catalog'))
        # An artifact whose file names another repository is refused whole.
        files = self.host.artifact_files(first.run_id)
        document = json.loads(files['rolling-history.json'])
        document['repository'] = 'other/repo'
        for run in document['runs']:
            run['repository'] = 'other/repo'
        self.host.artifacts[str(first.run_id)] = []
        self.host.attach(first.run_id, 'budget-results', zipped({'rolling-history.json': json.dumps(document)}))
        job = self.base()
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(len(self.history_of(job.run_id)['runs']), 1)
        self.assertIn('names other/repo', job.markdown())
        self.assertIn('starts fresh', job.markdown())
        # A file that does not end at its own run's head is refused whole too.
        document = json.loads(self.host.artifact_files(job.run_id)['rolling-history.json'])
        document['runs'][-1]['attempt'] = 9
        self.host.artifacts[str(job.run_id)] = []
        self.host.attach(job.run_id, 'budget-results', zipped({'rolling-history.json': json.dumps(document)}))
        job = self.base()
        self.assertEqual(len(self.history_of(job.run_id)['runs']), 1)
        self.assertIn('not this run', job.markdown())
        # Wrong branch and wrong workflow runs are ignored: the host lists them, onevcs refuses them.
        self.host.add_run(77001, job.sha, branch='other')
        self.host.add_run(77002, job.sha, workflow=7)
        self.host.add_run(77003, job.sha, event='pull_request')
        self.host.attach(77001, 'budget-results', zipped({'rolling-history.json': json.dumps(document)}))
        # Expired artifacts are disclosed.
        self.host.expire(job.run_id)
        job = self.base()
        self.assertEqual(len(self.history_of(job.run_id)['runs']), 1)
        self.assertIn('has expired', job.markdown())
        self.assertNotIn('77001', job.markdown())

    def test_a_pull_request_with_a_partial_checkout_reports_why(self):
        self.catalog_one()
        base = self.base(self.repository.commit('catalog'))
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        head = self.repository.commit('change')
        unknown = '0' * 40
        job = self.pull_request(head, unknown)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertIn('baseline unavailable', job.markdown())
        self.assertIsNotNone(base)

    def test_a_named_workflow_needs_no_lookup_and_a_base_without_history_starts_fresh(self):
        self.catalog_one()
        first = self.base(self.repository.commit('catalog'))
        tools = first.temp / 'onebudgetspec-action/tools'
        with tempfile.TemporaryDirectory() as directory:
            self.host.clear_requests()
            head = self.repository.commit('next')
            answer = harness.bounded_run(
                [str(tools / 'onebudgetspec-github-action'), 'report', '--repo', 'owner/repo', '--base-ref', 'main',
                 '--mode', 'base', '--workflow', str(harness.WORKFLOW), '--out', directory],
                cwd=self.repository.path, capture_output=True, text=True, check=False,
                env={**clean_environment(), 'GH_TOKEN': 'real-token', 'JOURNEY_COUNTS': str(self.counts),
                     **harness.real_gh_environment(self.host.url, directory)})
            self.assertEqual(answer.returncode, 0, answer.stderr)
            self.assertEqual(self.host.count(r'actions/runs/\d+$'), 0, 'no lookup: the workflow was given')
            rolling = json.loads((Path(directory) / 'rolling-history.json').read_text())
            self.assertEqual([run['head_sha'] for run in rolling['runs']], [first.sha, head])
            # Without a token, the plugin is never asked; the report still stands.
            answer = harness.bounded_run(
                [str(tools / 'onebudgetspec-github-action'), 'report', '--repo', 'owner/repo', '--base-ref', 'main',
                 '--mode', 'base', '--run-id', '123', '--out', directory],
                cwd=self.repository.path, capture_output=True, text=True, check=False,
                env={**clean_environment(), 'JOURNEY_COUNTS': str(self.counts)})
            self.assertEqual(answer.returncode, 0, answer.stderr)
            self.assertIn('no GitHub credential', answer.stdout)


class DirectTool(Journey):
    def driver(self):
        job = self.base(self.repository.commit('install the tools once'))
        return job, job.temp / 'onebudgetspec-action/tools/onebudgetspec-github-action'

    def test_direct_report_outside_actions_with_explicit_saved_input(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('steady', reported(5), {}),
            ('own-seconds', OWN, {'unit': 'seconds', 'threshold': 120, 'labels': ['ci-report']}),
        ]))
        job, driver = self.driver()
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / 'rolling.json'
            saved.write_bytes(self.host.artifact_files(job.run_id)['rolling-history.json'])
            out = Path(directory) / 'out'
            head = self.repository.commit('next')
            answer = harness.bounded_run(
                [str(driver), 'report', '--repo', 'owner/repo', '--base-ref', 'main', '--mode', 'base',
                 '--saved-history', str(saved), '--out', str(out)],
                cwd=self.repository.path, capture_output=True, text=True, check=False,
                env={**clean_environment(), 'JOURNEY_COUNTS': str(self.counts)})
            self.assertEqual(answer.returncode, 0, answer.stderr)
            self.assertIn('## Budget report', answer.stdout)
            self.assertEqual(self.host.count('zzzz'), 0)
            run = json.loads((out / 'budget-run.json').read_text())
            self.assertEqual(run['head_sha'], head)
            self.assertEqual({r['id'] for r in run['results']['results']}, {'steady', 'own-seconds'})
            rolling = json.loads((out / 'rolling-history.json').read_text())
            self.assertEqual(len(rolling['runs']), 2)
            telemetry = json.loads((out / 'report-job.json').read_text())
            self.assertEqual(telemetry['labels'], ['ci-report'])

            # Saved history of another repository, or a commit this checkout lacks, is refused.
            document = json.loads(saved.read_text())
            original = json.dumps(document)
            document['repository'] = 'other/repo'
            for run_entry in document['runs']:
                run_entry['repository'] = 'other/repo'
            saved.write_text(json.dumps(document))
            refused = self.direct(driver, out, saved)
            self.assertEqual(refused.returncode, 0, refused.stderr)
            self.assertIn('it names other/repo', refused.stdout)
            document = json.loads(original)
            for run_entry in document['runs']:
                run_entry['head_sha'] = 'f' * 40
            document['runs'] = document['runs'][:1]
            saved.write_text(json.dumps(document))
            refused = self.direct(driver, out, saved)
            self.assertIn('is not an ancestor', refused.stdout)

            # A real overrun is the same ordinary exit outside Actions.
            self.repository.write('budgets.yaml', budgets_yaml([
                ('steady', reported(500), {}), ('shadow', reported(500), {'labels': ['provisional']})]))
            self.assertEqual(self.direct(driver, out, saved).returncode, 1)
            # The same report-only setting as the action's input, saved with the run.
            self.repository.write('budgets.yaml', budgets_yaml([('shadow', reported(500), {'labels': ['provisional']})]))
            report_only = self.direct(driver, out, saved, '--enforcement-exclude-labels', 'provisional')
            self.assertEqual(report_only.returncode, 0, report_only.stderr)
            self.assertIn('report-only', report_only.stdout)
            self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(500), {})]))
            self.assertEqual(self.direct(driver, out, saved).returncode, 1)

            # Reproduction needs nothing but the saved file.
            reproduced = harness.bounded_run([str(driver), 'reproduce', '--input', str(out / 'comparison-input.json')],
                                        cwd=directory, capture_output=True, text=True, check=False, env=clean_environment())
            self.assertEqual(reproduced.returncode, 1, reproduced.stderr)
            self.assertIn('steady', reproduced.stdout)

    def direct(self, driver, out, saved, *extra):
        return harness.bounded_run(
            [str(driver), 'report', '--repo', 'owner/repo', '--base-ref', 'main', '--mode', 'base',
             '--saved-history', str(saved), '--out', str(out), *extra],
            cwd=self.repository.path, capture_output=True, text=True, check=False,
            env={**clean_environment(), 'JOURNEY_COUNTS': str(self.counts)})

    def test_custom_job_labels_keep_the_telemetry_readable(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('own-seconds', OWN, {'unit': 'seconds', 'threshold': 120}),
        ]))
        job = self.base(self.repository.commit('catalog'), inputs={'token': 'real-token', 'labels': 'our-job'})
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(job.rows()['own-seconds']['verdict'], 'within')
        self.assertEqual(sorted(job.saved('report-job.json')['labels']), ['ci-report', 'our-job'])

    def test_refusals_name_the_problem(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        _, driver = self.driver()
        with tempfile.TemporaryDirectory() as directory:
            out = str(Path(directory) / 'out')
            for arguments, reason in [
                (['--repo', 'nonsense'], 'OWNER/NAME'),
                (['--repo', 'owner/repo', '--attempt', '0'], 'positive'),
                (['--repo', 'owner/repo', '--artifact-name', ' '], 'artifact-name'),
                (['--repo', 'owner/repo', '--mode', 'pull-request'], 'change-url'),
                (['--repo', 'owner/repo', '--mode', 'base'], 'base-ref'),
                (['--repo', 'owner/repo', '--budget-files', '{'], 'valid glob'),
            ]:
                answer = harness.bounded_run([str(driver), 'report', '--out', out, *arguments], cwd=self.repository.path,
                                        capture_output=True, text=True, check=False, env=clean_environment())
                self.assertEqual(answer.returncode, 2, answer.stderr)
                self.assertIn(reason, answer.stderr)
            outside = harness.bounded_run([str(driver), 'report', '--out', out, '--repo', 'owner/repo'], cwd=directory,
                                     capture_output=True, text=True, check=False, env=clean_environment())
            self.assertEqual(outside.returncode, 2)
            self.assertIn('checkout', outside.stderr)
            missing = harness.bounded_run([str(driver), 'reproduce', '--input', 'absent.json'], cwd=directory,
                                     capture_output=True, text=True, check=False, env=clean_environment())
            self.assertEqual(missing.returncode, 2)
            self.assertIn('refused', missing.stderr)
            unfinished = harness.bounded_run([str(driver), 'finish', '--out', str(Path(directory) / 'nothing')], cwd=directory,
                                        capture_output=True, text=True, check=False, env=clean_environment())
            self.assertEqual(unfinished.returncode, 1)
            self.assertIn('run the step that writes it first', unfinished.stderr)

    def test_telemetry_reader_reports_the_saved_metric_or_fails_with_a_reason(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        _, driver = self.driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report-job.json'
            result = Path(directory) / 'result.json'
            path.write_text(json.dumps({'schema_version': 1, 'labels': ['ci-report'],
                                        'metrics': {'report-github-api-requests-per-run': {'value': 12, 'detail': 'requests: commands=3'}}}))
            read = [str(driver), 'telemetry', '--metric', 'report-github-api-requests-per-run', '--input', str(path)]
            answer = harness.bounded_run(read, capture_output=True, text=True, check=False,
                                    env={**clean_environment(), 'ONEBUDGETSPEC_RESULT': str(result)})
            self.assertEqual(answer.returncode, 0, answer.stderr)
            self.assertEqual(json.loads(result.read_text()), {'value': 12.0, 'detail': 'requests: commands=3'})
            plain = harness.bounded_run(read, capture_output=True, text=True, check=False, env=clean_environment())
            self.assertIn('12 (requests: commands=3)', plain.stdout)
            for document, reason in [
                ('not json', 'not JSON'),
                (json.dumps({'schema_version': 2}), 'schema_version'),
                (json.dumps({'schema_version': 1, 'labels': ['other'], 'metrics': {}}), 'not labelled ci-report'),
                (json.dumps({'schema_version': 1, 'labels': ['ci-report'], 'metrics': {}}), 'no finite metric'),
            ]:
                path.write_text(document)
                answer = harness.bounded_run(read, capture_output=True, text=True, check=False, env=clean_environment())
                self.assertEqual(answer.returncode, 1)
                self.assertIn(reason, answer.stderr)
            path.unlink()
            answer = harness.bounded_run(read, capture_output=True, text=True, check=False, env=clean_environment())
            self.assertIn('cannot read', answer.stderr)


class Degraded(Journey):
    """Runners and files that misbehave: each is a reason, and a real verdict still stands."""

    def tools(self):
        """An install whose core and plugin a test can replace, with the driver copied beside them."""
        job = self.base(self.repository.commit('install the tools once'))
        directory = Path(tempfile.mkdtemp(dir=job.temp))
        shutil.copy2(job.temp / 'onebudgetspec-action/tools/onebudgetspec-github-action', directory)
        return job.temp / 'onebudgetspec-action/tools', directory

    @staticmethod
    def script(path, body):
        path.write_text('#!/usr/bin/env bash\n' + body)
        path.chmod(0o755)

    def direct(self, directory, *arguments, environment=None, cwd=None):
        return harness.bounded_run(
            [str(directory / 'onebudgetspec-github-action'), 'report', '--repo', 'owner/repo', *arguments],
            cwd=cwd or self.repository.path, capture_output=True, text=True, check=False,
            env={**clean_environment(), 'JOURNEY_COUNTS': str(self.counts), **(environment or {})})

    def test_a_core_that_cannot_check_keeps_its_exit_and_every_row(self):
        # The first budget's own command breaks the catalog, so the real core refuses the later check of the job's figures.
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        real, directory = self.tools()
        os.symlink(real / 'onebudgetspec', directory / 'onebudgetspec')
        self.repository.write('budgets.yaml', budgets_yaml([
            ('steady', reported(5, "echo 'not: [a catalog' > budgets.yaml;"), {}),
            ('own-seconds', OWN, {'unit': 'seconds', 'threshold': 120}),
        ]))
        with tempfile.TemporaryDirectory() as out:
            answer = self.direct(directory, '--base-ref', 'main', '--mode', 'base', '--workflow', '1', '--out', out)
            self.assertEqual(answer.returncode, 2, answer.stdout + answer.stderr)
            report = json.loads((Path(out) / 'comparison-report.json').read_text())
            rows = {row['id']: row for row in report['rows']}
            self.assertIn('ordinary check could not run', rows['own-seconds']['unavailable'])
            self.assertEqual(rows['steady']['verdict'], 'within', 'the first phase saw real results')
            self.assertIn('not installed', json.dumps(report['diagnostics']), 'the plugin is missing here too')

    def test_a_core_exiting_zero_with_no_report_is_never_a_success(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        real, directory = self.tools()
        # llmlint: ignore[e2e_not_mocked] the real core always writes a readable report when it exits zero, so only a scripted core can reach this refusal; everything past its `check` answer stays real.
        self.script(directory / 'onebudgetspec', f'[ "$1" = check ] && {{ echo broken; exit 0; }}\nexec "{real}/onebudgetspec" "$@"\n')
        with tempfile.TemporaryDirectory() as out:
            answer = self.direct(directory, '--base-ref', 'main', '--mode', 'base', '--workflow', '1', '--out', out)
            self.assertEqual(answer.returncode, 3, answer.stderr)
            report = json.loads((Path(out) / 'comparison-report.json').read_text())
            self.assertIn('ordinary check could not run', report['rows'][0]['unavailable'])
            self.assertFalse((Path(out) / 'rolling-history.json').exists(), 'incomplete results are never appended')

    def test_an_own_telemetry_check_with_no_report_is_never_a_success(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('steady', reported(5), {}),
            ('own-seconds', OWN, {'unit': 'seconds', 'threshold': 120}),
        ]))
        real, directory = self.tools()
        # llmlint: ignore[e2e_not_mocked] the real core always writes a readable report when it exits zero, so only a scripted core can reach this refusal; every other core call, and the driver's whole first phase, stays real.
        self.script(directory / 'onebudgetspec', f'[ "$1" = check ] && [[ "$*" == *own-seconds* ]] && {{ echo broken; exit 0; }}\nexec "{real}/onebudgetspec" "$@"\n')
        with tempfile.TemporaryDirectory() as out:
            answer = self.direct(directory, '--base-ref', 'main', '--mode', 'base', '--workflow', '1', '--out', out)
            self.assertEqual(answer.returncode, 3, answer.stderr)
            rows = {row['id']: row for row in json.loads((Path(out) / 'comparison-report.json').read_text())['rows']}
            self.assertIn('ordinary check could not run', rows['own-seconds']['unavailable'])
            self.assertEqual(rows['steady']['verdict'], 'within', 'the first phase saw real results')

    def test_an_own_check_that_cannot_run_leaves_the_pending_history_unsaved(self):
        # A report-only own budget measures while the enforced one's check cannot run at all.
        self.repository.write('budgets.yaml', budgets_yaml([
            ('needs-a-tool', ['no-such-tool-on-this-runner', '--measure'], {}),
            ('own-seconds', OWN, {'unit': 'seconds', 'threshold': 120}),
            ('own-shadow', OWN, {'unit': 'seconds', 'threshold': 120, 'labels': ['provisional']}),
        ]))
        real, directory = self.tools()
        # llmlint: ignore[e2e_not_mocked] the real core always writes a readable report when it exits zero, so only a scripted core can reach this refusal; every other core call, and the driver's whole first phase, stays real.
        self.script(directory / 'onebudgetspec', f'[ "$1" = check ] && [[ "$*" == *own-seconds* ]] && {{ echo broken; exit 0; }}\nexec "{real}/onebudgetspec" "$@"\n')
        with tempfile.TemporaryDirectory() as out:
            answer = self.direct(directory, '--base-ref', 'main', '--mode', 'base', '--workflow', '1',
                                 '--enforcement-exclude-labels', 'provisional', '--out', out)
            self.assertEqual(answer.returncode, 3, answer.stderr)
            rows = {row['id']: row for row in json.loads((Path(out) / 'comparison-report.json').read_text())['rows']}
            self.assertEqual(rows['own-shadow']['verdict'], 'within', 'the report-only partition still measured')
            self.assertFalse((Path(out) / 'rolling-history.json').exists())
            self.assertIn('no rolling file saved', answer.stdout + (Path(out) / 'report.md').read_text())

    def test_tools_missing_or_answering_nonsense_are_reasons(self):
        self.repository.write('budgets.yaml', budgets_yaml([('over-budget', reported(500), {})]))
        real, directory = self.tools()
        with tempfile.TemporaryDirectory() as out:
            missing = self.direct(directory, '--out', out, environment={'PATH': '/usr/bin:/bin'})
            self.assertEqual(missing.returncode, 1)
            self.assertIn('onebudgetspec is not installed', missing.stderr)
            os.symlink(real / 'onebudgetspec', directory / 'onebudgetspec')
            head = self.repository.git('rev-parse', 'HEAD')
            url = ['--change-url', 'https://github.com/owner/repo/pull/1']
            # Reporter and history both absent: the report stands, with both reasons.
            absent = self.direct(directory, '--base-ref', 'main', '--base-sha', head, '--mode', 'pull-request', *url,
                                 '--workflow', '1', '--out', out, environment={'PATH': '/usr/bin:/bin'})
            self.assertEqual(absent.returncode, 1, 'the real overrun')
            self.assertIn('not delivered', absent.stdout)
            self.assertIn('not installed', absent.stdout)
            # The real plugin refuses an artifact name it reads as a flag, and answers no JSON.
            os.symlink(real / 'onebudgetspec-plugin-onevcs', directory / 'onebudgetspec-plugin-onevcs')
            garbled = self.direct(directory, '--base-ref', 'main', '--base-sha', head, '--mode', 'pull-request', *url,
                                  '--artifact-name=-unreadable', '--workflow', '1', '--out', out,
                                  environment={'PATH': '/usr/bin:/bin'})
            self.assertEqual(garbled.returncode, 1)
            self.assertIn('answered unreadably', garbled.stdout)
            # No way to name the workflow, and no base branch at all.
            unnamed = self.direct(directory, '--base-ref', 'main', '--run-id', 'local', '--out', out)
            self.assertIn('names the workflow', unnamed.stdout)
            read_only = self.direct(directory, '--read-only-reason', 'because', '--out', out)
            self.assertIn('this run names no base branch', read_only.stdout)
            self.assertIn('read-only run, no comment posted: because', read_only.stdout)

    def test_unusable_output_directories_are_named(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        tools, _ = self.tools()
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / 'file'
            blocked.write_text('')
            answer = self.direct(tools, '--out', str(blocked / 'out'))
            self.assertEqual(answer.returncode, 1)
            self.assertIn('choose a writable --out', answer.stderr)
            nested = Path(directory) / 'nested'
            (nested / '.state').mkdir(parents=True)
            (nested / '.state/bin').write_text('')
            answer = self.direct(tools, '--out', str(nested))
            self.assertEqual(answer.returncode, 1)
            self.assertIn('request counter', answer.stderr)
            rolling = Path(directory) / 'rolling'
            (rolling / 'rolling-history.json').mkdir(parents=True)
            answer = self.direct(tools, '--out', str(rolling))
            self.assertEqual(answer.returncode, 1)
            self.assertIn('cannot replace', answer.stderr)
            markdown = Path(directory) / 'markdown'
            (markdown / 'report.md').mkdir(parents=True)
            answer = self.direct(tools, '--out', str(markdown))
            self.assertEqual(answer.returncode, 1)
            self.assertIn('choose a writable directory', answer.stderr)
            # A run identity core refuses: nothing is compared, and the reason is core's.
            refused = self.direct(tools, '--run-id', ' ', '--base-ref', 'main', '--mode', 'base', '--workflow', '1',
                                  '--out', str(Path(directory) / 'blank'))
            self.assertEqual(refused.returncode, 2)
            self.assertIn('refused', refused.stderr)
            outside = self.direct(tools, '--head-sha', 'a' * 40, '--out', str(Path(directory) / 'o'), cwd=directory)
            self.assertEqual(outside.returncode, 2)
            self.assertIn('git cannot list', outside.stderr)
            self.assertEqual(self.direct(tools, '--repo', 'a/b/c', '--out', directory).returncode, 2)

    def test_a_finish_that_cannot_save_still_answers_the_known_verdict(self):
        self.repository.write('budgets.yaml', budgets_yaml([('over-budget', reported(500), {})]))
        tools, _ = self.tools()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            phased = self.direct(tools, '--out', directory, '--phased')
            self.assertEqual(phased.returncode, 0, phased.stderr)
            (out / 'report.md').unlink()
            (out / 'report.md').mkdir()
            finish = harness.bounded_run([str(tools / 'onebudgetspec-github-action'), 'finish', '--out', directory],
                                    cwd=self.repository.path, capture_output=True, text=True, check=False, env=clean_environment())
            self.assertEqual(finish.returncode, 1, 'the overrun the saved run already holds')
            (out / 'budget-run.json').unlink()
            finish = harness.bounded_run([str(tools / 'onebudgetspec-github-action'), 'finish', '--out', directory],
                                    cwd=self.repository.path, capture_output=True, text=True, check=False, env=clean_environment())
            self.assertEqual(finish.returncode, 1)
            self.assertIn('cannot read', finish.stderr)

    def test_an_action_step_that_cannot_write_is_diagnostic_not_a_failed_job(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(500), {})]))

        def block_report(job):
            (job.out / 'report.md').mkdir(parents=True)
        job = self.base(self.repository.commit('catalog'), before_run=block_report)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertIn('stopped with exit 1', job.stdout)

    def test_own_commands_that_cannot_be_written_or_escape_are_rows(self):
        self.repository.write('.telemetry', 'a file where the telemetry directory would go\n')
        self.repository.write('scripts/measure.sh', '#!/usr/bin/env bash\necho "{\\"value\\": 2}" > "$ONEBUDGETSPEC_RESULT"\n')
        (self.repository.path / 'scripts/measure.sh').chmod(0o755)
        self.repository.write('budgets.yaml', budgets_yaml([
            ('blocked-own', OWN, {'unit': 'seconds', 'threshold': 120}),
            ('escaping-own', ['onebudgetspec-github-action', 'telemetry', '--metric', 'report-command-seconds', '--input', '../../x.json'], {}),
            ('no-input', ['onebudgetspec-github-action', 'telemetry', '--metric', 'report-command-seconds'], {'labels': ['provisional']}),
            ('by-relative-path', ['scripts/measure.sh'], {}),
            ('missing-relative-path', ['scripts/absent.sh'], {}),
        ]))
        job = self.base(self.repository.commit('catalog'))
        self.assertEqual(job.exit_code, 0, job.stdout)
        saved = job.saved('comparison-report.json')['rows']
        rows = {row['id']: row for row in saved}
        self.assertIn('not saved', rows['blocked-own']['unavailable'])
        self.assertIn('leaves the checkout', rows['escaping-own']['unavailable'])
        self.assertNotIn('verdict', rows['no-input'], 'an ordinary command, report-only here')
        self.assertIn('report-only measurement failed', rows['no-input']['unavailable'])
        self.assertEqual(rows['by-relative-path']['current'], 2.0)
        self.assertIn('scripts/absent.sh', rows['missing-relative-path']['unavailable'])

    def test_the_request_counter_passes_everything_through(self):
        self.repository.write('budgets.yaml', budgets_yaml([
            ('non-api', reported(1, 'gh --version > /dev/null 2>&1 || true;'), {}),
            ('usage-error', reported(1, 'gh api --include > /dev/null 2>&1 || true;'), {}),
            ('unlogged', reported(1, 'env -u BUDGET_PROXY_LOG gh api repos/owner/repo/commits/a > /dev/null;'), {}),
            ('unwritable-log', reported(1, 'BUDGET_PROXY_LOG=/nonexistent/log gh api repos/owner/repo/commits/b > /dev/null;'), {}),
            ('host-refuses', ['bash', '-c', 'gh api elsewhere'], {'labels': ['provisional']}),
            ('jq-field', reported(1, 'gh api -X GET -f q=1 --jq .ok repos/owner/repo/commits/c > /dev/null;'), {}),
        ]))
        job = self.base(self.repository.commit('catalog'))
        self.assertEqual(job.exit_code, 0, job.stdout)
        refused_row = job.rows()['host-refuses']
        self.assertNotIn('verdict', refused_row)
        self.assertIn('report-only measurement failed', refused_row['unavailable'], 'a plain 404 is the measurement failing')
        self.assertEqual(self.host.count('commits/[ab]'), 2)

        # No gh on the runner, and a gh that cannot start: both are that budget's own result.
        self.repository.write('budgets.yaml', budgets_yaml([('reads', ['bash', '-c', 'gh api repos/owner/repo/commits/d'], {})]))
        sha = self.repository.commit('reads')
        tools = job.temp / 'onebudgetspec-action/tools'
        with tempfile.TemporaryDirectory() as directory:
            # Only what the journey needs on PATH: this host's real gh must stay out of reach.
            essentials = Path(directory) / 'essentials'
            essentials.mkdir()
            for tool in ('bash', 'git', 'env', 'cat'):
                os.symlink(shutil.which(tool), essentials / tool)
            path = {'PATH': f'{tools}:{essentials}'}
            answer = harness.bounded_run([str(tools / 'onebudgetspec-github-action'), 'report', '--repo', 'owner/repo', '--head-sha', sha, '--out', str(Path(directory) / 'first')],
                                    cwd=self.repository.path, capture_output=True, text=True, check=False, env={**clean_environment(), **path})
            self.assertEqual(answer.returncode, 0, answer.stderr)
            self.assertIn('no GitHub credential', answer.stdout)
            mode = ['--mode', 'base', '--base-ref', 'main', '--workflow', '1', '--saved-history', str(Path(directory) / 'none')]
            answer = harness.bounded_run([str(tools / 'onebudgetspec-github-action'), 'report', '--repo', 'owner/repo', '--head-sha', sha, '--out', str(Path(directory) / 'second'), *mode],
                                    cwd=self.repository.path, capture_output=True, text=True, check=False, env={**clean_environment(), **path})
            self.assertEqual(answer.returncode, 0, answer.stderr)
            self.assertIn('needs the GitHub CLI', answer.stdout)
            answer = harness.bounded_run([str(tools / 'onebudgetspec-github-action'), 'report', '--repo', 'owner/repo', '--head-sha', sha, '--out', str(Path(directory) / 'second'), *mode],
                                    cwd=self.repository.path, capture_output=True, text=True, check=False,
                                    env={**clean_environment(), **path, 'ONEVCS_GH': '/nonexistent/gh'})
            self.assertEqual(answer.returncode, 3, 'nothing says the host refused it, so this stays an error')


class Contract(unittest.TestCase):
    """action.yml is the authority; the driver, the reference workflow and the README follow it."""

    def setUp(self):
        self.action = yaml.safe_load((harness.ROOT / 'action.yml').read_text())

    def test_inputs_reconcile_with_the_driver_and_documents(self):
        inputs = self.action['inputs']
        self.assertEqual(set(inputs), {'token', 'budget-files', 'labels', 'artifact-name', 'enforcement-exclude-labels'})
        self.assertTrue(inputs['token']['required'])
        for spec in inputs.values():
            self.assertIn('description', spec)
        help_text = harness.bounded_run([str(harness.driver_binary()), 'report', '--help'], text=True).stdout
        run_script = (harness.ROOT / 'action/run.sh').read_text()
        readme = (harness.ROOT / 'README.md').read_text()
        for name, spec in inputs.items():
            self.assertIn(f'`{name}`', readme)
            if name == 'token':
                continue
            self.assertIn(f'--{name} ', help_text)
            self.assertIn(f'--{name} "$', run_script)
            default = spec['default']
            if default:
                self.assertIn(f'[default: {default}]', help_text)

    def test_reference_workflow_runs_after_predecessors_unfiltered_and_serialized(self):
        workflow = yaml.safe_load((harness.ROOT / 'examples/budget-report.yml').read_text())
        triggers = workflow[True]
        self.assertNotIn('pull_request_target', triggers)
        self.assertEqual(triggers['push'], {'branches': ['main']})
        request = triggers['pull_request']
        self.assertNotIn('paths', request)
        self.assertNotIn('paths-ignore', request)
        self.assertNotIn('branches', request)
        job = workflow['jobs']['budgets']
        self.assertEqual(job['if'], 'always()')
        self.assertTrue(job['needs'])
        self.assertIn('github.event.pull_request.number', job['concurrency']['group'])
        self.assertIs(job['concurrency']['cancel-in-progress'], False)
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read', 'pull-requests': 'write'})
        checkout, action = job['steps'][0], job['steps'][-1]
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        self.assertTrue(action['uses'].startswith('nickderobertis/onebudgetspec-github-action@v'))
        self.assertEqual(set(action['with']) - set(self.action['inputs']), set())
        self.assertIn('token', action['with'])
        self.assertEqual(len(workflow['jobs']), 3, 'the example adds exactly one job to its predecessors')


def zipped(members):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def clean_environment():
    """Outside Actions: no GITHUB_* variable, no token, no doubled service."""
    keep = ('PATH', 'HOME', 'LLVM_PROFILE_FILE')
    return {key: os.environ[key] for key in keep if key in os.environ}


if __name__ == '__main__':
    unittest.main(verbosity=1)
