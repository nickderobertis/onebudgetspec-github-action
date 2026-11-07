"""Record one complete report job's traffic, for `action-report-github-api-requests-per-run`.

The workload: 49 budget rows, a rolling file of 20 commits and a retried rate-limit response, through
action.yml's real steps with only external services doubled: a pull request whose test job's saved
results settle the rows, with the 2 CI-time reads its commands make, and a base push of that pull
request, with the merged-run reads. The other two lookups a base push can make cost fewer requests.
The figure saved is what the doubled GitHub API received over the whole job, from every client,
final persistence included. The job's own count plus what reached the API by another client must
equal it. File transfers are recorded beside it.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'tests/action-harness'))
import harness
from harness import Host, Job, Repository, budgets_yaml, check_report, check_result, event_pull_request, zipped

ROWS = 49
CI_TIME_READS = 2
COMMITS = 20
METRIC = 'report-github-api-requests-per-run'
TRANSFER_SERVICES = ('artifact-service', 'blob-storage', 'object-storage', 'release-host', 'package-index')
TELEMETRY = HERE / '.telemetry' / 'report-job.json'


def catalog():
    """Every shape of row: CI-time reads, test-job figures, own telemetry, one nothing here can run."""
    write = 'echo "{\\"value\\": 1}" > "$ONEBUDGETSPEC_RESULT"'
    entries = [
        ('own-job-requests', ['onebudgetspec-github-action', 'telemetry', '--metric', METRIC, '--input', 'report-job.json'],
         {'unit': 'requests', 'labels': ['ci-report']}),
        ('not-on-this-runner', ['tool-this-runner-lacks'], {}),
    ]
    for index in range(CI_TIME_READS):
        # One command reads through curl: the job cannot see it, and the boundary still counts it.
        read = (f'curl -fsS -H "Authorization: Bearer $GH_TOKEN" http://api.github.localhost/repos/owner/repo/commits/{index}'
                if index == 0 else f'gh api repos/owner/repo/commits/{index}')
        command = f'{read} > /dev/null || exit 1; {write}'
        entries.append((f'reads-rest-{index}', ['bash', '-c', command], {'unit': 'requests'}))
    while len(entries) < ROWS:
        entries.append((f'test-job-{len(entries)}', ['tool-only-a-test-job-has'], {'unit': 'bytes'}))
    return budgets_yaml(entries)


def saved_results():
    """What the test job saved: every test-job row and the row with no tool stays without one."""
    return check_report(*(check_result(f'test-job-{index}', 1, unit='bytes')
                          for index in range(2 + CI_TIME_READS, ROWS)))


def seeded(run, runs_heads):
    """A rolling file of synthetic copies of one real run, one per real commit, oldest first."""
    runs = []
    for index, head in enumerate(runs_heads):
        runs.append({**run, 'head_sha': head, 'run_id': str(500 + index), 'attempt': 1})
    return {'schema_version': 1, 'repository': harness.REPOSITORY, 'base_ref': 'main', 'runs': runs}


class ReportJob(unittest.TestCase):
    def test_record_a_complete_job(self):
        host = Host()
        self.addCleanup(host.close)
        repository = Repository()
        self.addCleanup(repository.close)
        repository.write('budgets.yaml', catalog())
        repository.write('.gitignore', 'report-job.json\n')
        heads = [repository.commit(f'base {index}') for index in range(COMMITS)]
        first = self.job(host, repository, 'push', heads[-1], run_id=400)
        self.assertEqual(first.exit_code, 0, first.stdout)

        host.artifacts.clear()
        host.runs.clear()
        host.add_run(519, heads[-1], minute=519)
        host.attach(519, 'budget-results', zipped({'rolling-history.json': json.dumps(seeded(first.saved('budget-run.json'), heads))}))

        repository.git('checkout', '--quiet', '-b', 'feature')
        head = repository.commit('change')
        host.head = head
        host.attach(601, 'budget-check-results-test-linux', zipped({'test.json': saved_results()}))

        # The pull request: the saved results, the CI-time reads, the history and the one comment.
        host.clear_requests()
        host.refuse.append({'path': r'artifacts\?', 'status': 429, 'times': 1, 'retry_after': 1})
        pull_request = self.job(host, repository, 'pull_request', head, run_id=601, base_ref='main',
                                event=event_pull_request(1, head, heads[-1]))
        self.assert_complete_job(host, pull_request, baseline=COMMITS)
        self.assertEqual(host.requests_not_through_gh, 1, 'the one curl read')
        self.assertEqual(host.count('commits/[0-9]+$'), CI_TIME_READS)
        self.assertEqual(pull_request.rows()['test-job-10']['current'], 1.0, 'a figure the test job saved')
        figures = [boundary_record(host, pull_request.saved('report-job.json'))]

        # The base push of that pull request, with the merged-run reads and no command to run.
        host.add_run(601, head, minute=601, event='pull_request')
        repository.git('checkout', '--quiet', 'main')
        repository.git('merge', '--quiet', '--squash', 'feature')
        merge = repository.commit('squash')
        host.merge_pull_request(merge, head)
        host.clear_requests()
        host.refuse.append({'path': r'artifacts\?', 'status': 429, 'times': 1, 'retry_after': 1})
        base = self.job(host, repository, 'push', merge, run_id=602, register=False)
        self.assert_complete_job(host, base, baseline=COMMITS)
        self.assertEqual(host.count(f'/commits/{merge}/pulls$'), 1)
        self.assertEqual(host.count(r'/runs/601/artifacts\?name=budget-results$'), 2, 'the retried rate-limit response')
        self.assertEqual(host.count('/actions/artifacts/[0-9]+/zip$'), 2, 'the merged run and the history')
        self.assertEqual(base.rows()['test-job-10']['current'], 1.0, 'the merged pull request\'s figure')
        figures.append(boundary_record(host, base.saved('report-job.json')))

        save_worst(figures, TELEMETRY)

    def assert_complete_job(self, host, job, baseline):
        self.assertEqual(job.exit_code, 0, job.stdout)
        self.assert_reconciles(host, job.saved('report-job.json'))
        self.assertEqual(job.outcomes['final-upload'], 'success', job.stdout)
        self.assertEqual(len(job.rows()), ROWS)
        self.assertIn('not-on-this-runner', job.markdown())
        self.assertEqual(len(job.saved('comparison-input.json')['baseline']), baseline)

    def test_an_unavailable_figure_is_read_back_as_unavailable(self):
        # The real gh refuses this before sending anything, so the job cannot know its count.
        refused = 'gh api --no-such-flag repos/owner/repo/commits/refused > /dev/null 2>&1 || true;'
        write = 'echo "{\\"value\\": 1}" > "$ONEBUDGETSPEC_RESULT"'
        host = Host()
        self.addCleanup(host.close)
        repository = Repository()
        self.addCleanup(repository.close)
        repository.write('budgets.yaml', budgets_yaml([('refused-read', ['bash', '-c', refused + write], {})]))
        job = self.job(host, repository, 'push', repository.commit('catalog'), run_id=700)
        self.assertEqual(job.exit_code, 0, job.stdout)
        telemetry = job.saved('report-job.json')
        self.assertIsNone(telemetry['metrics'][METRIC]['value'])
        self.assertGreater(host.count(''), 0, 'the boundary saw traffic, and it must not stand in for the figure')
        self.assert_reconciles(host, telemetry)

        # What `budgets` does: the real catalog and the real reader, over what this test saves.
        with tempfile.TemporaryDirectory() as directory:
            reporting = Path(directory)
            shutil.copy(HERE / 'budgets.yaml', reporting / 'budgets.yaml')
            save_worst([boundary_record(host, telemetry)], reporting / '.telemetry' / 'report-job.json')
            saved = json.loads((reporting / '.telemetry' / 'report-job.json').read_text())
            self.assertIsNone(saved['metrics'][METRIC]['value'], 'no number replaces it')
            self.assertIn('no record of its requests', saved['metrics'][METRIC]['unavailable'])
            self.assertNotIn('github_api_requests', saved['boundary'])
            tools = job.temp / 'onebudgetspec-action/tools'
            checked = harness.bounded_run(
                [str(tools / 'onebudgetspec'), 'check', '--json', 'budgets.yaml'], cwd=reporting,
                env={'PATH': f'{tools}:{os.environ["PATH"]}', 'HOME': directory})
        result, = json.loads(checked.stdout)['results']
        self.assertEqual(result['id'], 'action-report-github-api-requests-per-run')
        self.assertEqual(result['verdict'], 'error', checked.stdout)
        self.assertIsNone(result.get('actual'), 'no figure is reported')
        self.assertIn('is unavailable', checked.stderr.decode() + json.dumps(result))
        self.assertNotEqual(checked.returncode, 0, 'an unavailable budget is no pass')

    def assert_reconciles(self, host, telemetry):
        """The job's own count plus other clients' must equal the boundary's, when it has one."""
        figure = telemetry['metrics'][METRIC]['value']
        if figure is not None:
            self.assertEqual(figure + host.requests_not_through_gh, host.count(''),
                             f'the job and the host disagree: {host.requests}')

    def job(self, host, repository, event_name, sha, **options):
        inputs = {'token': 'real-token', 'saved-results-artifacts': 'budget-check-results-*'}
        job = Job(host, repository, event_name, sha, inputs=inputs, **options)
        self.addCleanup(job.close)
        return job.run()


def save_worst(figures, path):
    """Save the largest figure, or an unavailable one: no number outranks an unknown count."""
    unknown = [telemetry for telemetry in figures if telemetry['metrics'][METRIC]['value'] is None]
    worst = unknown[0] if unknown else max(figures, key=lambda telemetry: telemetry['metrics'][METRIC]['value'])
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(worst))


def boundary_record(host, telemetry):
    """The job's telemetry, restated from the complete traffic the boundary received."""
    transfers = {service: host.transferred(service) for service in TRANSFER_SERVICES}
    assert sum(transfers.values()) == len(host.transfers), f'an unclassified transfer: {host.transfers}'
    api_requests = host.count('')
    record = json.loads(json.dumps(telemetry))
    metric = record['metrics'][METRIC]
    if metric['value'] is None:
        # The job's count could not be reconciled, so an unknown figure stays unknown.
        record['boundary'] = {'file_transfers': transfers}
        return record
    metric['value'] = api_requests
    listed = ' '.join(f'{service}={count}' for service, count in transfers.items())
    metric['detail'] += (f'; measured at the boundary over the whole job: not through gh={host.requests_not_through_gh}, '
                         f'file transfers {listed}')
    record['persistence'] = 'measured at the boundary after the job ended, final persistence included'
    record['scope'] = 'every client: measured at the external boundary'
    record['boundary'] = {'github_api_requests': api_requests, 'not_through_gh': host.requests_not_through_gh,
                          'file_transfers': transfers,
                          'all_http_requests': api_requests + sum(transfers.values())}
    return record


if __name__ == '__main__':
    unittest.main(verbosity=1)
