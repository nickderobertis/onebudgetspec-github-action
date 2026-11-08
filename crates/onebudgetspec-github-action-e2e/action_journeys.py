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
from harness import Host, Job, Repository, budgets_yaml, check_report, check_result, event_pull_request, zipped

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

# Where each part of the core's comment layout opens; each closes at its next `</details>`.
SECTIONS = {'attention': '<details open><summary><b>Needs attention', 'within': '<details><summary><b>Within budget',
            'details': '<details><summary>Details</summary>', 'earlier': '<details><summary>Flags on earlier heads'}


def section(body, name):
    """One collapsible part of a posted comment, or None when the comment has none."""
    start = body.find(SECTIONS[name])
    return None if start < 0 else body[start:body.index('</details>', start)]


def table_rows(text):
    """A section's table rows: each budget's name, then its cells."""
    rows = {}
    for line in (text or '').splitlines():
        if line.startswith('| **'):
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            rows[cells[0].strip('*')] = cells[1:]
    return rows


def listed(text, heading):
    """The `- names: text` bullets under one bold heading of the Details section, by name."""
    if not text or f'**{heading}' not in text:
        return {}
    block = text.split(f'**{heading}', 1)[1].split('\n\n', 2)[1]
    items = {}
    for line in block.splitlines():
        if line.startswith('- '):
            names, said = line[2:].split(': ', 1)
            items.update({name: said for name in names.split(', ')})
    return items


def placements(body, budget):
    """Every place the report part of a comment shows `budget` as a row, as (section, text)."""
    found = [(name, cells) for name in ('attention', 'within') for row, cells in table_rows(section(body, name)).items()
             if row == budget]
    unmeasured = listed(section(body, 'details'), 'Not measured on this run')
    return found + ([('not-measured', unmeasured[budget])] if budget in unmeasured else [])


def figure(value):
    """A figure as the core's layout writes it: grouped whole from 100, one decimal from 10, else two."""
    return f'{value:,.0f}' if abs(value) >= 100 else f'{value:.1f}' if abs(value) >= 10 else f'{value:.2f}'


def history_block(body):
    """The hidden history a standing comment carries."""
    start = '<!-- onebudgetspec-history\n'
    return json.loads(body[body.index(start) + len(start):body.rindex('\n-->')])


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

        # The one comment leads with what needs attention; a report-only row says so with its flag kept.
        self.assertEqual(len(self.host.comments), 1)
        body = self.host.comments[0]['body']
        attention = table_rows(section(body, 'attention'))
        self.assertEqual(attention['enforced-limit'][1], 'Over limit · flagged')
        self.assertEqual(attention['shadow-limit'][1], 'Over limit · flagged (report-only)')
        self.assertEqual(attention['requests-read'][1], 'Flagged (was constant)')
        # The own row holds its figure on the comment, the same as the saved artifact; its seconds can
        # move a standard deviation against the base runs', which flags it and never fails it.
        [(place, cells)] = placements(body, 'job-seconds')
        self.assertIn(place, ('within', 'attention'), body)
        self.assertTrue(place == 'within' or cells[1].startswith('Flagged'), cells)
        self.assertNotIn('job-seconds', listed(section(body, 'details'), 'Why some figures are missing'))
        saved = json.loads(self.host.artifact_files(job.run_id)['budget-run.json'])
        own = next(result for result in saved['results']['results'] if result['id'] == 'job-seconds')
        self.assertEqual(own['verdict'], 'within')
        self.assertLess(own['actual'], 120)
        self.assertTrue(cells[0].startswith(f'{figure(own["actual"])} seconds<br>'), cells)
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
        self.assertIn(f'head: {second}', section(body, 'details'))
        earlier = section(body, 'earlier')
        self.assertIn(head, earlier)
        self.assertIn('- budgets.yaml/requests-read: constant-baseline-change', earlier,
                      f'the first head keeps its flag, folded, after the second fixed it: {body}')

        # An identical rerun neither duplicates the comment nor its head's entry.
        rerun = self.pull_request(second, base_sha, run_id=update.run_id, attempt=2)
        self.assertEqual(rerun.exit_code, 0)
        self.assertEqual(len(self.host.comments), 1)
        heads = [entry['head_sha'] for entry in history_block(self.host.comments[0]['body'])['entries']]
        self.assertEqual(heads, [head, second], 'one entry per head, however many attempts')

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
        self.assertEqual(placements(comment['body'], 'figure'), [('within', ['33.0 count<br>baseline 0/5'])])
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
        # Not measured, never an error: it needs no attention, and its reason is under Details.
        self.assertEqual(placements(body, 'release-figure'),
                         [('not-measured', 'no result this run (report-only) · baseline 0/5')])
        self.assertIn('onevcs is not on PATH', listed(section(body, 'details'), 'Why some figures are missing')['release-figure'])

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
        self.assertEqual(table_rows(section(self.host.comments[0]['body'], 'attention'))['shadow'][1],
                         'Over limit · flagged (report-only)')

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


class Layout(Journey):
    """The comment as the core lays it out: what needs attention open, the rest and every reason folded."""

    def layout_catalog(self, requests=7, shadow=50):
        """Measured rows with fixed figures, a report-only one, and two that measure nothing here."""
        for budget, figure in {'requests-read': requests, 'bundle-bytes': 500, 'enforced-limit': 10,
                               'shadow-limit': shadow}.items():
            self.repository.write(f'values-{budget}', f'{figure}\n')
        self.repository.write('budgets.yaml', budgets_yaml([
            ('requests-read', from_data(READS_REST), {'unit': 'requests', 'threshold': 100}),
            ('bundle-bytes', from_data(), {'unit': 'bytes', 'threshold': 1000}),
            ('enforced-limit', from_data(), {'threshold': 40}),
            ('shadow-limit', from_data(), {'threshold': 40, 'labels': ['provisional']}),
            ('needs-a-tool', ['no-such-tool-on-this-runner', '--measure'], {}),
            ('needs-predecessor', ['cat', '.telemetry/predecessor.json'], {}),
        ]))

    def based_feature(self, **figures):
        self.layout_catalog(**figures)
        self.repository.commit('catalog')
        commits = [self.base().sha for _ in range(6)]
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        return commits

    def test_a_flagged_row_and_a_report_only_overrun_alone_need_attention(self):
        # The report-only budget is over on every base run alike: over, and not flagged.
        commits = self.based_feature(shadow=50)
        self.layout_catalog(requests=20, shadow=50)
        head = self.repository.commit('a change that regresses')
        job = self.pull_request(head, commits[-1])
        self.assertEqual(job.exit_code, 0, job.stdout)
        body = self.host.comments[0]['body']

        attention = section(body, 'attention')
        self.assertIn('| Budget | Now · vs base | Reason |\n', attention)
        self.assertEqual(table_rows(attention), {
            'requests-read': ['20.0 requests<br>base 7.00 requests', 'Flagged (was constant)'],
            'shadow-limit': ['50.0 count<br>+0.0% (+0.0 SD)', 'Over limit (report-only)'],
        })
        self.assertIn('| Budget | Now · vs base |\n', section(body, 'within'))
        for budget in ('requests-read', 'shadow-limit'):
            [(place, _)] = placements(body, budget)
            self.assertEqual(place, 'attention', f'{budget} is in that section and no other')
            row = next(line for line in attention.splitlines() if line.startswith(f'| **{budget}** |'))
            self.assertNotIn('unavailable', row)
            for sha in commits + [head]:
                self.assertNotIn(sha[:7], row, 'no commit hash in a row')
        tables = [line for line in body.splitlines() if line.startswith('|')]
        self.assertTrue(tables)
        for mark in ('✅', '❌', '⚠️'):
            self.assertFalse([line for line in tables if mark in line], f'no table cell holds {mark}')

        # A second head carries the first head's flag inside the collapsed earlier-heads section.
        self.layout_catalog(requests=7, shadow=50)
        second = self.repository.commit('a fix')
        self.assertEqual(self.pull_request(second, commits[-1]).exit_code, 0)
        [comment] = self.host.comments
        earlier = section(comment['body'], 'earlier')
        self.assertTrue(earlier.startswith('<details><summary>Flags on earlier heads (1)</summary>'), comment['body'])
        self.assertIn(f'\n{head} (attempt 1): 1 adverse flag(s)\n- budgets.yaml/requests-read: constant-baseline-change\n',
                      earlier)
        self.assertEqual([place for place, _ in placements(comment['body'], 'requests-read')], ['within'])

    def test_nothing_failed_or_flagged_folds_every_row_once(self):
        commits = self.based_feature(shadow=30)
        head = self.repository.commit('an unchanged figure')
        job = self.pull_request(head, commits[-1])
        self.assertEqual(job.exit_code, 0, job.stdout)
        body = self.host.comments[0]['body']
        self.assertEqual(section(body, 'attention').split('</summary>\n\n', 1)[1], 'Nothing failed or was flagged.\n\n')
        rows = job.rows()
        measured = sorted(budget for budget, row in rows.items() if 'verdict' in row)
        unmeasured = sorted(set(rows) - set(measured))
        self.assertEqual(unmeasured, ['needs-a-tool', 'needs-predecessor'])
        self.assertIn(f'<details><summary><b>Within budget ({len(measured)})</b></summary>', body)
        self.assertIn(f'\n**Not measured on this run ({len(unmeasured)})**\n', section(body, 'details'))
        for budget in measured:
            self.assertEqual([place for place, _ in placements(body, budget)], ['within'], budget)
            self.assertEqual(body.count(f'| **{budget}** |'), 1, budget)
        for budget in unmeasured:
            self.assertEqual(placements(body, budget), [('not-measured', 'no result this run · baseline 0/5')])

    def test_history_diagnostics_read_one_line_per_reason(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        first = self.base(self.repository.commit('catalog'))
        real = first.saved('budget-run.json')
        self.host.runs.clear()
        self.host.artifacts.clear()

        def entry(head, run_id):
            return {**real, 'head_sha': head, 'run_id': str(run_id), 'run_url': f'https://github.com/owner/repo/actions/runs/{run_id}',
                    'attempt': 1, 'completed_at': '2026-11-01T00:00:00Z'}

        # Five commits whose history entries are their first attempts, though each ran a second.
        reattempted = [self.repository.commit(f'reattempted {index}') for index in range(5)]
        # Eight whose runs the history never took in.
        missing = [self.repository.commit(f'missing {index}') for index in range(8)]
        selected = self.repository.commit('the run whose file is read')
        # The two newest ran without saving anything: refused, and also lost.
        unsaved = [self.repository.commit(f'unsaved {index}') for index in range(2)]
        self.repository.git('checkout', '--quiet', '-b', 'side', first.sha)
        unrelated = self.repository.commit('a push on no ancestor of the base')
        self.repository.git('checkout', '--quiet', 'main')
        runs = ([(2001 + index, head, 2) for index, head in enumerate(reattempted)]
                + [(2101 + index, head, 1) for index, head in enumerate(missing)]
                + [(2200, selected, 1)] + [(2301 + index, head, 1) for index, head in enumerate(unsaved)]
                + [(2400, unrelated, 1)])
        for run_id, head, attempt in runs:
            self.host.add_run(run_id, head, attempt=attempt, minute=run_id - 2000)
        history = {'schema_version': 1, 'repository': harness.REPOSITORY, 'base_ref': 'main',
                   'runs': [entry(head, 2001 + index) for index, head in enumerate(reattempted)] + [entry(selected, 2200)]}
        self.host.attach(2200, 'budget-results', zipped({'rolling-history.json': json.dumps(history)}))

        base = unsaved[-1]
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        job = self.pull_request(self.repository.commit('a change'), base)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(job.rows()['steady']['baseline_n'], 6, 'the selected file is the baseline')
        details = section(self.host.comments[0]['body'], 'details')
        lines = [line for line in details.splitlines() if 'history entr' in line or 'history run' in line]
        self.assertEqual(lines, [
            "- 10 history entries lost: this base commit's entry is not in the selected history",
            '- 5 history entries lost: the history holds attempt 1 of this commit; attempt 2 did not reach it',
            '- 1 history runs refused: not reachable from the requested base head',
            '- 2 history runs refused: no artifact named budget-results: its upload failed or never ran',
        ], details)
        for line in lines:
            for run_id, head, _ in runs:
                self.assertNotIn(str(run_id), line)
                self.assertNotIn(head[:7], line)


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
        self.assertNotIn('unavailable', rows['requests-read'], 'a short baseline is a count, not a missing figure')
        self.assertIn('<br>baseline 0/5', placements(self.host.comments[0]['body'], 'requests-read')[0][1][0])
        self.assertEqual(len(rows), 9)
        self.assertEqual(len(self.host.comments), 1)
        self.assertIn('Budget report', job.summary)

    def test_partial_baseline_is_counted_below_five_and_compared_from_five(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        self.repository.commit('catalog')
        for _ in range(4):
            base = self.base()
        self.repository.git('checkout', '--quiet', '-b', 'few')
        job = self.pull_request(self.repository.commit('change'), base.sha)
        self.assertEqual(job.rows()['steady']['baseline_n'], 4)
        self.assertEqual(placements(job.markdown(), 'steady'), [('within', ['5.00 count<br>baseline 4/5'])])
        self.repository.git('checkout', '--quiet', 'main')
        base = self.base()
        self.repository.git('checkout', '--quiet', '-b', 'enough')
        job = self.pull_request(self.repository.commit('change'), base.sha)
        self.assertEqual(job.rows()['steady']['baseline_n'], 5)
        self.assertEqual(placements(job.markdown(), 'steady'), [('within', ['5.00 count<br>+0.0% (+0.0 SD)'])])

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


NO_SAVED = 'no saved result was found on this run'
ARTIFACTS = 'budget-check-results-*'
SAVED_INPUTS = {'token': 'real-token', 'enforcement-exclude-labels': 'provisional', 'saved-results-artifacts': ARTIFACTS}
LACKS_TOOL = ['tool-only-a-test-job-has', '--measure']


class Measured(Journey):
    """Rows settled from what this run's test jobs saved, and from the merged pull request's run."""

    def measured_catalog(self):
        """A budget a test job measures, others only it can, one this job can, and the job's own."""
        for budget, figure in {'also-runnable': 7, 'runs-here': 9}.items():
            self.repository.write(f'values-{budget}', f'{figure}\n')
        self.repository.write('budgets.yaml', budgets_yaml([
            ('also-runnable', from_data(), {}),
            ('test-only', LACKS_TOOL, {}),
            ('other-producer', LACKS_TOOL, {}),
            ('runs-here', from_data(), {}),
        ]))
        self.repository.write('.github/reporting/budgets.yaml', budgets_yaml([
            ('job-seconds', OWN, {'unit': 'seconds', 'threshold': 120, 'labels': ['ci-report']}),
        ]))

    def seed_history(self, commits):
        """A rolling file of one real base run's shape, one entry per real commit, every row measured."""
        first = self.base(self.repository.commit('catalog'))
        self.assertEqual(first.exit_code, 0, first.stdout)
        real = first.saved('budget-run.json')
        heads = [first.sha] + [self.repository.commit(f'base {index}') for index in range(commits - 1)]
        runs = []
        for index, head in enumerate(heads):
            results = []
            for budget in real['catalog']:
                results.append(check_result(budget['id'], 10 + index, file=budget['file'], labels=budget.get('labels', []),
                                            unit=budget['unit'], threshold=budget['threshold'],
                                            ended_at='2026-11-01T00:00:00Z'))
            runs.append({**real, 'head_sha': head, 'run_id': str(499 - len(heads) + 1 + index), 'results': {'schema_version': 1, 'results': results},
                         'unavailable': [], 'completed_at': f'2026-11-01T00:{index:02d}:00Z'})
        history = {'schema_version': 1, 'repository': harness.REPOSITORY, 'base_ref': 'main', 'runs': runs}
        self.host.artifacts.clear()
        self.host.runs.clear()
        self.host.add_run(499, heads[-1], minute=499)
        self.host.attach(499, 'budget-results', zipped({'rolling-history.json': json.dumps(history)}))
        return heads[-1]

    def saved_run(self, head, base, artifacts, inputs=None, **options):
        """A pull request whose earlier jobs saved `artifacts`, a name to its files, in this run."""
        self.next_run += 1
        for name, files in artifacts.items():
            self.host.attach(self.next_run, name, zipped(files))
        return self.pull_request(head, base, run_id=self.next_run, inputs=inputs or SAVED_INPUTS, **options)

    def row_text(self, budget):
        """Where the latest comment shows `budget`, which must be exactly one place."""
        body = self.host.comments[-1]['body']
        found = placements(body, budget)
        self.assertEqual(len(found), 1, body)
        return found[0]

    def details(self, heading):
        return listed(section(self.host.comments[-1]['body'], 'details'), heading)

    def assert_no_saved_result(self, job, budget, baseline_n=6):
        row = job.rows()[budget]
        self.assertEqual(row['unavailable'], NO_SAVED, 'exactly that, with no cause')
        self.assertIsNone(row.get('current'), 'no value is carried forward')
        self.assertEqual(row['baseline_n'], baseline_n)
        self.assertEqual([item['reason'] for item in job.saved('budget-run.json')['unavailable'] if item['id'] == budget], [NO_SAVED])
        if baseline_n >= 5:
            self.assertEqual(row['mean'], 12.5)
            self.assertGreater(row['sd'], 0)
            self.assertEqual(self.row_text(budget), ('not-measured', f'no result this run · baseline {baseline_n}/5'))
            self.assertEqual(self.details('Why some figures are missing')[budget], NO_SAVED)

    def test_a_saved_result_replaces_the_command_and_keeps_its_verdict(self):
        self.repository.write('values-enforced', '5\n')
        self.repository.write('values-shadow', '5\n')
        self.repository.write('budgets.yaml', budgets_yaml([
            ('enforced', from_data(), {'threshold': 40}),
            ('shadow', from_data(), {'threshold': 40, 'labels': ['provisional']}),
        ]))
        base = self.repository.commit('catalog')
        shadow_over = check_report(check_result('shadow', 70, labels=['provisional'], threshold=40))

        # A report-only overrun shows its verdict and fails nothing; the older duplicate loses.
        passing = self.saved_run(base, base, {
            'budget-check-results-a': {'one.json': check_report(check_result('enforced', 31, threshold=40, ended_at='2099-01-02T00:00:00Z'))},
            'budget-check-results-b': {'nested/two.json': check_report(check_result('enforced', 39, threshold=40, ended_at='2099-01-01T00:00:00Z')),
                                       'three.json': shadow_over},
            'unrelated-artifact': {'four.json': check_report(check_result('enforced', 99, threshold=40))},
        })
        self.assertEqual(passing.exit_code, 0, passing.stdout)
        self.assertEqual(self.executed(), [], 'no command ran')
        rows = passing.rows()
        self.assertEqual((rows['enforced']['current'], rows['enforced']['verdict']), (31.0, 'within'))
        self.assertEqual((rows['shadow']['current'], rows['shadow']['verdict']), (70.0, 'over'))
        self.assertEqual(self.row_text('shadow'), ('attention', ['70.0 count<br>baseline 0/5', 'Over limit (report-only)']))

        # An enforced overrun fails the job through the ordinary exit.
        failing = self.saved_run(base, base, {'budget-check-results-a': {'one.json': check_report(
            check_result('enforced', 41, threshold=40), check_result('shadow', 1, labels=['provisional'], threshold=40))}})
        self.assertEqual(failing.exit_code, 1, failing.stdout)
        self.assertEqual(self.executed(), [])
        self.assertEqual(failing.rows()['enforced']['verdict'], 'over')
        self.assertEqual(len(self.host.comments), 1)

    def test_the_latest_result_for_an_id_wins_wherever_its_file_says_it_ran(self):
        self.repository.write('values-steady', '5\n')
        self.repository.write('budgets.yaml', budgets_yaml([('steady', from_data(), {'threshold': 40})]))
        base = self.repository.commit('catalog')
        job = self.saved_run(base, base, {
            'budget-check-results-a': {'matching-file.json': check_report(
                check_result('steady', 31, threshold=40, ended_at='2099-01-01T00:00:00Z'))},
            'budget-check-results-b': {'elsewhere.json': check_report(
                check_result('steady', 45, threshold=40, ended_at='2099-01-02T00:00:00Z', file='apps/elsewhere/budgets.yaml'))},
        })
        self.assertEqual(self.executed(), [], 'its command does not run')
        row = job.rows()['steady']
        self.assertEqual((row['current'], row['verdict']), (45.0, 'over'), 'the newer number and verdict')
        self.assertEqual(job.exit_code, 1, job.stdout)
        [saved] = job.saved('budget-run.json')['results']['results']
        self.assertEqual((saved['file'], saved['ended_at']), ('budgets.yaml', '2099-01-02T00:00:00Z'))

    def test_what_a_saved_file_says_that_cannot_stand_is_a_diagnostic(self):
        self.repository.write('values-steady', '5\n')
        self.repository.write('budgets.yaml', budgets_yaml([
            ('steady', from_data(), {}), ('contradicted', reported(4), {}), ('shadow-error', reported(4), {'labels': ['provisional']}),
            ('enforced-error', LACKS_TOOL, {}),
        ]))
        base = self.repository.commit('catalog')
        errored = {**check_result('shadow-error', 1, labels=['provisional']), 'verdict': 'error', 'actual': None, 'headroom': None,
                   'headroom_percent': None, 'error': 'no such figure'}
        job = self.saved_run(base, base, {'budget-check-results-a': {
            'results.json': check_report(
                check_result('steady', 3), check_result('removed-budget', 3), check_result('removed-budget', 4),
                check_result('contradicted', 3, threshold=7), errored,
                {**errored, 'id': 'enforced-error', 'labels': []}),
            'not-a-report.json': '{"nothing": "here"}',
            'notes.txt': 'not json at all',
        }})
        diagnostics = job.saved('comparison-report.json')['diagnostics']
        unknown = [line for line in diagnostics if 'removed-budget' in line]
        self.assertEqual(len(unknown), 1, 'one diagnostic per saved id the catalog does not hold')
        self.assertTrue([line for line in diagnostics if 'not-a-report.json' in line], diagnostics)
        self.assertFalse([line for line in diagnostics if 'notes.txt' in line], 'only *.json files are results')
        rows = job.rows()
        self.assertEqual(rows['steady']['current'], 3.0)
        # A result contradicting the catalog would make core refuse the run: this row runs instead.
        self.assertTrue([line for line in diagnostics if 'contradicted' in line], diagnostics)
        self.assertEqual(rows['contradicted']['current'], 4.0)
        self.assertEqual(self.executed(), ['contradicted'])
        # A saved error reads as a command's would: report-only unavailable, enforced the ordinary exit.
        self.assertIn('no such figure', rows['shadow-error']['unavailable'])
        self.assertEqual(rows['enforced-error']['verdict'], 'error')
        self.assertEqual(job.exit_code, 3, job.stdout)
        self.assertEqual(len(self.host.comments), 1)

    def test_a_result_older_than_the_head_commit_reads_as_replayed(self):
        for budget in ('replayed', 'fresh', 'between'):
            self.repository.write(f'values-{budget}', '1\n')
        self.repository.write('budgets.yaml', budgets_yaml(
            [(budget, from_data(), {}) for budget in ('replayed', 'fresh', 'between')]))
        base = self.repository.commit('catalog', committed_at='2026-01-01T00:00:00Z')
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        head = self.repository.commit('the pull request head', committed_at='2026-03-10T12:00:00Z')
        # What a pull request job checks out: a merge commit newer than every result here.
        self.repository.git('checkout', '--quiet', 'main')
        self.repository.git('merge', '--quiet', '--no-ff', '-m', 'merge', 'feature',
                            environment={'GIT_COMMITTER_DATE': '2026-03-10T18:00:00Z'})
        job = self.saved_run(head, base, {'budget-check-results-a': {'results.json': check_report(
            check_result('replayed', 3, ended_at='2026-03-09T23:00:00Z'),
            check_result('between', 4, ended_at='2026-03-10T15:00:00Z'),
            check_result('fresh', 5, ended_at='2026-03-10T12:30:00Z'))}})
        self.assertEqual(job.exit_code, 0, job.stdout)
        rows = job.rows()
        self.assertEqual({budget: rows[budget]['current'] for budget in rows}, {'replayed': 3.0, 'between': 4.0, 'fresh': 5.0})
        self.assertEqual(self.details('Notes on this run'), {'replayed': 'replayed from cache, measured 2026-03-09'},
                         'newer than the head though older than the merge commit, and restored just now: both fresh')
        self.assertEqual([note['id'] for note in job.saved('budget-run.json')['result_notes']], ['replayed'])

        # A checkout without the head commit cannot judge a replay: every number stands, and it says so.
        job = self.saved_run('0' * 40, base, {'budget-check-results-a': {'results.json': check_report(
            check_result('replayed', 3, ended_at='2026-03-09T23:00:00Z'))}})
        self.assertEqual(job.rows()['replayed']['current'], 3.0)
        self.assertNotIn('replayed from cache', job.markdown())
        self.assertIn('cannot tell replayed results from fresh ones', job.markdown())

    def assert_settled_rows(self, job, shown, absent):
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.executed().count('runs-here'), 1, 'a command that can run here runs once')
        rows = job.rows()
        self.assertEqual(rows['runs-here']['current'], 9.0)
        for budget, figure in shown.items():
            self.assertEqual(rows[budget]['current'], figure, budget)
        for budget in absent:
            self.assert_no_saved_result(job, budget)
        own = rows['job-seconds']
        self.assertEqual(own['verdict'], 'within', 'the action\'s own telemetry budget is this job\'s figure')
        self.assertAlmostEqual(own['current'], job.saved('report-job.json')['metrics']['report-command-seconds']['value'], places=6)
        self.assertEqual(job.outcomes['saved-results'], 'success')

    def test_absent_saved_results_read_only_as_absent(self):
        self.measured_catalog()
        base = self.seed_history(6)
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        head = self.repository.commit('change')
        # The journey's own figure, in an artifact, must not stand in for this job's.
        journey_figure = check_result('job-seconds', 1, file='.github/reporting/budgets.yaml', labels=['ci-report'],
                                      unit='seconds', threshold=120)

        # (a) One producer failed before saving; the other's artifact arrives.
        arrived = {'budget-check-results-linux': {'test.json': check_report(
            check_result('also-runnable', 21), check_result('test-only', 22), journey_figure)}}
        job = self.saved_run(head, base, arrived)
        self.assertEqual(self.executed(), ['runs-here'], 'the arriving artifact replaced also-runnable\'s command')
        self.assert_settled_rows(job, {'also-runnable': 21.0, 'test-only': 22.0}, ['other-producer'])

        # (b) A selected target's result is absent from an artifact that did arrive.
        job = self.saved_run(head, base, {'budget-check-results-linux': {'test.json': check_report(check_result('other-producer', 23))}})
        self.assertEqual(self.executed(), ['also-runnable', 'runs-here'])
        self.assert_settled_rows(job, {'other-producer': 23.0}, ['test-only'])

        # (c) No artifact arrives at all: the command that can run does, once, and the rest say so.
        job = self.saved_run(head, base, {})
        self.assertEqual(self.executed(), ['also-runnable', 'runs-here'])
        self.assert_settled_rows(job, {'also-runnable': 7.0}, ['test-only', 'other-producer'])
        self.assertEqual(len(self.host.comments), 1)

    def test_no_saved_result_stays_exact_beside_a_baseline_that_is_short(self):
        self.measured_catalog()
        base = self.repository.commit('catalog')
        job = self.saved_run(base, base, {})
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assert_no_saved_result(job, 'test-only', baseline_n=0)

        # The same run with the input empty keeps the catalog's reason, as it always has.
        empty = self.pull_request(base, base)
        self.assertEqual(empty.exit_code, 0, empty.stdout)
        self.assertIn('tool-only-a-test-job-has', empty.rows()['test-only']['unavailable'])
        self.assertNotIn(NO_SAVED, empty.markdown())

    def test_a_download_that_fails_is_a_diagnostic(self):
        self.measured_catalog()
        base = self.repository.commit('catalog')
        self.host.refuse_list_artifacts = True
        job = self.saved_run(base, base, {'budget-check-results-linux': {'test.json': check_report(check_result('test-only', 2))}})
        self.assertEqual(job.outcomes['saved-results'], 'failure')
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assert_no_saved_result(job, 'test-only', baseline_n=0)
        self.assertIn(f'downloading the {ARTIFACTS} artifacts failed', job.markdown())

    def test_omitted_budgets_run_nothing_show_nothing_and_are_counted(self):
        self.measured_catalog()
        extra = yaml.safe_load((self.repository.path / 'budgets.yaml').read_text())
        for entry in extra['budgets']:
            if entry['id'] in ('also-runnable', 'test-only'):
                entry['labels'] = ['not-measured-on-pr']
        self.repository.write('budgets.yaml', yaml.safe_dump(extra))
        base = self.repository.commit('catalog')
        inputs = {**SAVED_INPUTS, 'omit-labels': 'not-measured-on-pr, other-label'}
        job = self.saved_run(base, base, {'budget-check-results-linux': {'test.json': check_report(
            check_result('test-only', 2, labels=['not-measured-on-pr']))}}, inputs=inputs)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.executed(), ['runs-here'])
        self.assertEqual(set(job.rows()), {'other-producer', 'runs-here', 'job-seconds'})
        line = '2 budgets are left out because a pull request cannot measure them (label not-measured-on-pr).'
        body = self.host.comments[-1]['body']
        # The same line v0.2.0 gave, once, among the diagnostics inside the collapsed Details.
        self.assertEqual(len([text for text in body.splitlines() if 'left out because' in text]), 1, body)
        self.assertIn(f'\n- {line}\n', section(body, 'details'))
        self.assertFalse([text for text in body.splitlines() if 'test-only' in text], 'its saved result is ignored, with no line')
        saved = job.saved('budget-run.json')
        self.assertEqual({budget['id'] for budget in saved['catalog']},
                         {'also-runnable', 'test-only', 'other-producer', 'runs-here', 'job-seconds'})
        self.assertFalse({'also-runnable', 'test-only'} & {result['id'] for result in saved['results']['results']})
        self.assertFalse({'also-runnable', 'test-only'} & {row['id'] for row in saved['unavailable']})

        # Saved input still reproduces the rows that were shown.
        reproduced = harness.bounded_run(
            [str(job.temp / 'onebudgetspec-action/tools/onebudgetspec-github-action'), 'reproduce',
             '--input', str(job.out / 'comparison-input.json'), '--json'],
            capture_output=True, text=True, check=False, env=clean_environment())
        self.assertEqual({row['id'] for row in json.loads(reproduced.stdout)['rows']}, set(job.rows()))
        self.assertEqual(json.loads(reproduced.stdout)['rows'], job.saved('comparison-report.json')['rows'])

        # The line follows what matched: its count's grammar and the labels that carried a budget.
        single = self.pull_request(base, base, inputs={'token': 'real-token', 'omit-labels': 'absent-label'})
        self.assertNotIn('left out because', self.host.comments[-1]['body'], 'nothing carries it, so no line')
        self.assertEqual(len(single.rows()), 5)
        extra['budgets'][0]['labels'] = ['first-label', 'second-label']
        extra['budgets'][1]['labels'] = ['first-label']
        for entry in extra['budgets'][2:]:
            entry.pop('labels', None)
        self.repository.write('budgets.yaml', yaml.safe_dump(extra))
        changed = self.repository.commit('relabelled')
        one = self.pull_request(changed, base, inputs={'token': 'real-token', 'omit-labels': 'second-label'})
        self.assertIn('1 budget is left out because a pull request cannot measure it (label second-label).', one.markdown())
        both = self.pull_request(changed, base, inputs={'token': 'real-token', 'omit-labels': 'first-label\nsecond-label'})
        self.assertIn('2 budgets are left out because a pull request cannot measure them (labels first-label, second-label).',
                      both.markdown())

        # A base push omits them too, and its rolling entry's catalog keeps them.
        self.repository.git('checkout', '--quiet', 'main')
        pushed = self.base(changed, inputs={'token': 'real-token', 'omit-labels': 'first-label'})
        self.assertEqual(pushed.exit_code, 0, pushed.stdout)
        self.assertNotIn('also-runnable', self.executed())
        self.assertNotIn('also-runnable', pushed.rows())
        entry = self.history_of(pushed.run_id)['runs'][-1]
        self.assertIn('also-runnable', {budget['id'] for budget in entry['catalog']})

    def merged_pull_request(self, *saved_runs, squash_adds=()):
        """A real pull request's real run or re-runs, one per `saved_runs`, then its squash onto main,
        which also adds the budgets `squash_adds` names, whose figures that run cannot hold.

        Answers its run, its head and the merge commit, with main checked out."""
        self.measured_catalog()
        base = self.repository.commit('catalog')
        self.repository.git('checkout', '--quiet', '-b', 'feature')
        head = self.repository.commit('the pull request')
        self.next_run += 1
        run_id = self.next_run
        for attempt, artifacts in enumerate(saved_runs, start=1):
            for name, files in artifacts.items():
                self.host.attach(run_id, name, zipped(files), attempt=attempt)
            self.pull_request(head, base, run_id=run_id, attempt=attempt, inputs=SAVED_INPUTS)
        self.repository.git('checkout', '--quiet', 'main')
        self.repository.git('merge', '--quiet', '--squash', 'feature')
        catalog = yaml.safe_load((self.repository.path / 'budgets.yaml').read_text())
        for budget in squash_adds:
            self.repository.write(f'values-{budget}', '9\n')
            catalog['budgets'].extend(yaml.safe_load(budgets_yaml([(budget, from_data(), {})]))['budgets'])
        self.repository.write('budgets.yaml', yaml.safe_dump(catalog))
        merge = self.repository.commit('squash')
        self.host.merge_pull_request(merge, head)
        return run_id, head, merge

    def test_a_base_push_records_the_merged_pull_requests_figures(self):
        over_100_kb = 'x' * 120_000
        first = check_report(check_result('also-runnable', 100), check_result('test-only', 101, detail=over_100_kb))
        newer = check_report(check_result('also-runnable', 40, detail=over_100_kb), check_result('test-only', 200),
                             check_result('other-producer', 120, threshold=100, detail=over_100_kb),
                             check_result('job-seconds', 1, file='.github/reporting/budgets.yaml', labels=['ci-report'],
                                          unit='seconds', threshold=120))
        name = 'budget-check-results-linux'
        run_id, head, merge = self.merged_pull_request({name: {'r.json': first}}, {name: {'r.json': newer}},
                                                       squash_adds=['added-by-the-merge'])

        # A re-run left two artifacts of one name, and the listing puts the newer first.
        self.assertEqual(len([a for a in self.host.artifacts[str(run_id)] if a['name'] == 'budget-results']), 2)
        merged_run = json.loads(self.host.artifact_files(run_id)['budget-run.json'])
        artifact = {result['id']: result for result in merged_run['results']['results']}
        self.assertEqual(artifact['test-only']['actual'], 200.0)
        self.assertIsNotNone(artifact['also-runnable']['detail'], 'the merged run\'s own artifact keeps it')

        self.host.clear_requests()
        job = self.base(merge)
        self.assertEqual(job.exit_code, 1, 'other-producer is over, as the merged run recorded')
        self.assertEqual(self.executed(), ['added-by-the-merge'], 'a figure the merged run holds runs no command')
        self.assertEqual(self.host.count(f'/commits/{merge}/pulls'), 1)
        self.assertEqual(self.host.count(rf'/runs\?event=pull_request&head_sha={head}&per_page=1'), 1)
        self.assertEqual(self.host.count(rf'/runs/{run_id}/artifacts\?name=budget-results'), 1)
        self.assertEqual(self.host.count('/actions/runs/[0-9]+$'), 1, 'the workflow is looked up once')
        history = self.history_of(job.run_id)
        entry = history['runs'][-1]
        self.assertEqual(entry['head_sha'], merge)
        results = {result['id']: result for result in entry['results']['results']}
        for budget in ('also-runnable', 'test-only', 'other-producer', 'runs-here'):
            self.assertEqual(results[budget]['actual'], artifact[budget]['actual'], budget)
            self.assertEqual(results[budget]['verdict'], artifact[budget]['verdict'], budget)
            self.assertEqual(results[budget]['ended_at'], artifact[budget]['ended_at'], budget)
            self.assertIsNone(results[budget]['detail'], budget)
        self.assertEqual(results['test-only']['actual'], 200.0, 'the newer attempt')
        self.assertEqual(results['other-producer']['verdict'], 'over')
        self.assertEqual(results['added-by-the-merge']['actual'], 9.0, 'the merged run held none, so it ran here')
        own = results['job-seconds']
        self.assertAlmostEqual(own['actual'], job.saved('report-job.json')['metrics']['report-command-seconds']['value'], places=6)
        self.assertNotEqual(own['actual'], artifact['job-seconds']['actual'], 'this job\'s own figure')
        self.assertLess(len(json.dumps(entry)), 20_000, 'the rolling entry stays small')
        metric = job.saved('report-job.json')['metrics'][API_METRIC]
        self.assertEqual(metric['value'], self.host.count(''), 'the job counted its merged-run reads')
        self.assertIn(f'4 rows are the figures of merged pull request #1\'s run {run_id}', job.markdown())

        # A later pull request reads them as its baseline.
        self.repository.git('checkout', '--quiet', '-b', 'later')
        later_head = self.repository.commit('a later change')
        later = self.saved_run(later_head, merge, {})
        rows = later.rows()
        self.assertEqual(rows['test-only']['baseline_n'], 1)
        self.assertEqual(rows['other-producer']['baseline_n'], 1)
        baseline = later.saved('comparison-input.json')['baseline']
        self.assertEqual([run['head_sha'] for run in baseline], [merge])
        self.assertEqual({r['id']: r['actual'] for r in baseline[0]['results']['results']}['test-only'], 200.0)

    def test_a_base_push_without_a_merged_run_ends_as_it_would_have(self):
        run_id, _, merge = self.merged_pull_request({})
        name = 'budget-results'

        # An expired artifact: the listing answers and the archive is never read.
        for artifact in self.host.artifacts[str(run_id)]:
            artifact['expired'] = True
        self.host.clear_requests()
        expired = self.base(merge)
        self.assertEqual(expired.exit_code, 0, expired.stdout)
        self.assertIn('has expired', expired.markdown())
        self.assertEqual(self.executed(), ['also-runnable', 'runs-here'])
        self.assertIn('tool-only-a-test-job-has', expired.rows()['test-only']['unavailable'])
        self.assertEqual(self.host.count('/actions/artifacts/[0-9]+/zip'), 0, 'no archive of the merged run')
        self.assertEqual(self.host.count(f'/commits/{merge}/pulls'), 1)
        self.assertEqual(self.host.count(rf'/runs/{run_id}/artifacts\?name={name}'), 1)

        # The run has no such artifact: stops at the listing, in 3 requests.
        self.host.artifacts[str(run_id)] = []
        missing = self.base(merge)
        self.assertEqual(missing.exit_code, 0, missing.stdout)
        self.assertIn(f'run {run_id} holds no budget-results artifact', missing.markdown())
        self.assertEqual(self.executed(), ['also-runnable', 'runs-here'])

        # A commit whose pull request is open, or that has none: 1 request, as today.
        self.host.pulls[merge] = [{'number': 2, 'merged_at': None, 'merge_commit_sha': 'other', 'head': {'sha': 'abc'}}]
        self.host.clear_requests()
        none = self.base(merge)
        self.assertEqual(none.exit_code, 0, none.stdout)
        self.assertIn(f'commit {merge} has no merged pull request', none.markdown())
        self.assertEqual(self.host.count('/runs\\?event=pull_request'), 0)
        self.assertEqual(self.executed(), ['also-runnable', 'runs-here'])
        results = {r['id']: r for r in self.history_of(none.run_id)['runs'][-1]['results']['results']}
        self.assertEqual((results['also-runnable']['actual'], results['runs-here']['actual']), (7.0, 9.0))
        self.assertEqual(none.rows()['job-seconds']['verdict'], 'within')

        # GitHub refusing the lookup is the same: a diagnostic and a green job.
        self.host.refuse.append({'path': '/pulls$', 'status': 500, 'times': 5})
        refused = self.base(merge)
        self.assertEqual(refused.exit_code, 0, refused.stdout)
        self.assertIn('merged pull request\'s figures not read', refused.markdown())
        self.assertEqual(self.executed(), ['also-runnable', 'runs-here'])

    def test_a_merged_artifact_that_cannot_stand_leaves_the_rows_as_they_were(self):
        run_id, _, merge = self.merged_pull_request({})
        run = json.loads(self.host.artifact_files(run_id)['budget-run.json'])
        for archive, reason in [
            (b'not an archive', 'not a readable archive'),
            (zipped({'report.md': 'no results here'}), 'no budget-run.json'),
            (zipped({'budget-run.json': 'not json'}), 'budget-run.json is not JSON'),
            (zipped({'budget-run.json': json.dumps({'results': []})}), 'no readable results'),
        ]:
            self.host.attach(run_id, 'budget-results', archive)
            job = self.base(merge)
            self.assertEqual(job.exit_code, 0, job.stdout)
            self.assertIn(reason, job.markdown())
            self.assertEqual(self.executed(), ['also-runnable', 'runs-here'], reason)

        # A figure contradicting this commit's catalog, or one that errored, leaves only its own row.
        results = {result['id']: result for result in run['results']['results']}
        results['also-runnable']['threshold'] = 7.0
        results['runs-here'] = {**results['runs-here'], 'verdict': 'error', 'actual': None, 'headroom': None,
                                'headroom_percent': None, 'error': 'it failed there'}
        run['results']['results'] = list(results.values())
        self.host.attach(run_id, 'budget-results', zipped({'budget-run.json': json.dumps(run)}))
        job = self.base(merge)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.executed(), ['also-runnable', 'runs-here'])
        self.assertIn(f"also-runnable's figure from merged pull request #1's run {run_id} ignored", job.markdown())

        # A pull request whose head has no run of this workflow stops at that read.
        self.host.runs = [item for item in self.host.runs if item['event'] != 'pull_request']
        self.host.clear_requests()
        job = self.base(merge)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertIn('has no pull_request run of this workflow', job.markdown())
        self.assertEqual(self.host.count(rf'/runs/{run_id}/artifacts'), 0)

    def test_the_one_comment_holds_the_jobs_own_figure(self):
        self.measured_catalog()
        base = self.repository.commit('catalog')
        job = self.saved_run(base, base, {})
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(self.host.count(r'/issues/1/comments$'), 1)
        self.assertEqual(len(self.host.comment_write_times), 1, 'one write, once the figure is known')
        place, cells = self.row_text('job-seconds')
        telemetry = job.saved('report-job.json')
        seconds = telemetry['metrics']['report-command-seconds']
        self.assertEqual(place, 'within')
        self.assertEqual(cells, [f'{figure(seconds["value"])} seconds<br>baseline 0/5'])
        self.assertNotIn('measured once', self.host.comments[0]['body'])
        # The interval the saved file records ended before the post left.
        started = float((job.temp / 'onebudgetspec-action/started-at').read_text())
        self.assertLess(started + seconds['value'], self.host.comment_write_times[0])
        self.assertIn('before the comment post', seconds['detail'])
        requests = telemetry['metrics'][API_METRIC]
        self.assertEqual(requests['value'], self.host.count(''), 'the saved file holds the post\'s requests too')
        self.assertRegex(requests['detail'], r'delivery=[1-9]')
        # What the own command read was written before the post, and says so.
        read = json.loads((self.repository.path / '.github/reporting/.telemetry/report-job.json').read_text())
        self.assertIn('delivery=pending', read['metrics'][API_METRIC]['detail'])
        self.assertLess(read['metrics'][API_METRIC]['value'], requests['value'])
        # The main upload, saved before that, holds a true reason and no verdict for it.
        main = json.loads(self.host.artifact_files(job.run_id, 'budget-results-main')['budget-run.json'])
        self.assertEqual([row['reason'] for row in main['unavailable'] if row['id'] == 'job-seconds'][0].split(':')[0],
                         'not judged when this file was saved')
        # The completed comment and the completed artifact agree.
        self.assertEqual(json.loads(self.host.artifact_files(job.run_id)['comparison-report.json'])['rows'],
                         job.saved('comparison-report.json')['rows'])

    def test_the_comment_still_posts_when_the_own_budget_cannot_judge(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        no_metric = ['onebudgetspec-github-action', 'telemetry', '--metric', 'no-such-metric', '--input', '.telemetry/job.json']
        self.repository.write('.github/reporting/budgets.yaml', budgets_yaml([
            ('own-shadow', no_metric, {'labels': ['provisional']}),
            ('own-enforced', no_metric, {}),
        ]))
        base = self.repository.commit('catalog')
        job = self.pull_request(base, base)
        self.assertEqual(self.host.count(r'/issues/1/comments$'), 1)
        self.assertEqual(len(self.host.comment_write_times), 1)
        self.assertEqual(job.exit_code, 3, job.stdout)
        self.assertEqual(self.row_text('own-shadow'), ('not-measured', 'no result this run (report-only) · baseline 0/5'))
        self.assertEqual(self.row_text('own-enforced'), ('attention', ['—<br>baseline 0/5', 'Error']))
        reasons = self.details('Why some figures are missing')
        self.assertIn('no-such-metric', reasons['own-shadow'])
        self.assertIn('no-such-metric', reasons['own-enforced'])
        self.assertIn('no-such-metric', job.rows()['own-enforced']['unavailable'])
        self.assertNotIn('not judged', self.host.comments[0]['body'])

    def test_a_repository_with_no_own_command_posts_once_and_uploads_once(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        base = self.repository.commit('catalog')
        job = self.pull_request(base, base)
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assertEqual(len(self.host.comment_write_times), 1)
        self.assertEqual(self.host.count(r'/issues/1/comments$'), 1)
        self.assertEqual(job.outcomes['main-upload'], 'success')
        self.assertNotIn('final-upload', job.outcomes)
        self.assertIsNotNone(self.host.artifact_files(job.run_id))
        self.assertEqual(self.row_text('steady'), ('within', ['5.00 count<br>baseline 0/5']))


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
        self.assertIn('not judged when this file was saved', next(r['reason'] for r in saved['unavailable'] if r['id'] == 'job-seconds'))
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
        self.assertEqual(job.rows()['needs-a-tool']['baseline_n'], 0)
        [(place, cells)] = placements(self.host.comments[0]['body'], 'job-seconds')
        self.assertEqual(place, 'within')
        self.assertTrue(cells[0].endswith(' seconds<br>baseline 2/5'), cells)

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
        # The one archive read beyond the plugin's cost is the merged-run lookup finding no pull request.
        self.assertIn('archive=5 (history QueryCost 4, the rest merged-run reads and retries)', metric['detail'])
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
                (['--repo', 'owner/repo', '--omit-labels', 'Not A Label'], 'must match'),
                (['--repo', 'owner/repo', '--saved-results-artifacts', 'results-*'], '--saved-results-dir'),
                (['--repo', 'owner/repo', '--mode', 'base', '--base-ref', 'main', '--saved-results-artifacts', 'results-*',
                  '--saved-results-dir', '.'], 'pull-request or read-only'),
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

    def test_a_finish_with_an_unreadable_rolling_decision_refuses_it(self):
        self.repository.write('budgets.yaml', budgets_yaml([('steady', reported(5), {})]))
        tools, _ = self.tools()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            phased = self.direct(tools, '--out', directory, '--phased')
            self.assertEqual(phased.returncode, 0, phased.stderr)
            # With no readable saved run to fall back on, the refusal itself is the answer.
            # llmlint: ignore[tests_mirror_real_usage] only a damaged state directory reaches this refusal, and no command writes one, so the journey damages the saved run the fallback would otherwise answer from; the report phase before it and the finish after it stay real.
            (out / 'budget-run.json').write_text('{}')
            continuation_path = out / '.state/continuation.json'
            continuation = json.loads(continuation_path.read_text())
            self.assertEqual(continuation['rolling'], 'settled')
            for bad in ('nonsense', None):
                # llmlint: ignore[tests_mirror_real_usage] no command writes a bad decision, since report only saves the three spellings it can parse, so rewriting the file is the only way to prove finish refuses one instead of skipping a pending rolling file.
                continuation_path.write_text(json.dumps({**continuation, 'rolling': bad}))
                finish = harness.bounded_run([str(tools / 'onebudgetspec-github-action'), 'finish', '--out', directory],
                                             cwd=self.repository.path, capture_output=True, text=True, check=False, env=clean_environment())
                self.assertEqual(finish.returncode, 1, finish.stderr)
                self.assertIn('rolling decision', finish.stderr)

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
        self.assertEqual(self.host.count('commits/[ab]$'), 2, 'a merged-run lookup names a sha, which can start with either')

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


class SessionBus(unittest.TestCase):
    """No environment handed to the real `gh` lets it autolaunch a session bus, which nothing ends.

    The stand-in only records and fails, and it is first on the PATH `gh` searches, so a failing
    guard starts nothing. Its positive control shows this `gh` still asks once the variable goes.
    """

    def setUp(self):
        if not sys.platform.startswith('linux') or shutil.which('gh') is None:
            self.skipTest('no session bus can start here: this needs the real gh on Linux')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.scratch = Path(directory.name)
        self.calls = self.scratch / 'dbus-launch-calls'
        stand_in = self.scratch / 'bin' / 'dbus-launch'
        stand_in.parent.mkdir()
        stand_in.write_text(f'#!/bin/sh\necho called >> "{self.calls}"\nexit 1\n')
        stand_in.chmod(0o755)
        self.path = f'{stand_in.parent}{os.pathsep}{os.environ["PATH"]}'

    def asks(self, environment):
        """Run the real `gh`: what it answered, and how many buses it tried to start doing so."""
        self.calls.unlink(missing_ok=True)
        # `auth token` asks the keyring just as a tokenless request does, and sends nothing anywhere.
        answer = harness.bounded_run([shutil.which('gh'), 'auth', 'token'], capture_output=True, text=True, cwd=self.scratch,
                                     env={**environment, 'HOME': str(self.scratch), 'PATH': self.path})
        count = len(self.calls.read_text().splitlines()) if self.calls.exists() else 0
        return count, (answer.returncode, answer.stdout, answer.stderr)

    def assert_starts_no_bus(self, environment):
        unguarded = {key: value for key, value in environment.items() if key != 'DBUS_SESSION_BUS_ADDRESS'}
        asked, without = self.asks(unguarded)
        if not asked:
            self.skipTest('this gh no longer asks for a session bus without the variable, so none can leak')
        self.assertEqual(asked, 1, 'the control: one call autolaunches one bus')
        count, guarded = self.asks(environment)
        self.assertEqual(count, 0, 'this environment lets gh start a session bus; give it DBUS_SESSION_BUS_ADDRESS=disabled:')
        self.assertEqual(guarded, without, 'the variable changes nothing else about the call')

    def test_the_harness_environment_starts_no_bus(self):
        self.assert_starts_no_bus(harness.real_gh_environment('http://127.0.0.1:9', self.scratch))

    def test_the_direct_environment_starts_no_bus(self):
        # `asks` gives this call a HOME with no login; a tokenless direct run reaches the real `gh` with the ambient one's.
        self.assert_starts_no_bus(clean_environment())


class Contract(unittest.TestCase):
    """action.yml is the authority; the driver, the reference workflow and the README follow it."""

    def setUp(self):
        self.action = yaml.safe_load((harness.ROOT / 'action.yml').read_text())

    def test_inputs_reconcile_with_the_driver_and_documents(self):
        inputs = self.action['inputs']
        self.assertEqual(set(inputs), {'token', 'budget-files', 'labels', 'artifact-name', 'enforcement-exclude-labels',
                                       'saved-results-artifacts', 'omit-labels'})
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


def clean_environment():
    """Outside Actions: no GITHUB_* variable, no token, no doubled service."""
    keep = ('PATH', 'HOME', 'LLVM_PROFILE_FILE')
    # A tokenless call that reaches the real `gh` under this HOME asks the keyring: no session bus.
    return {**{key: os.environ[key] for key in keep if key in os.environ}, 'DBUS_SESSION_BUS_ADDRESS': 'disabled:'}


if __name__ == '__main__':
    unittest.main(verbosity=1)
