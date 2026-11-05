"""Record one complete report job's REST traffic, for `action-report-rest-requests-per-run`.

The workload: 100 budget rows, a rolling file of 20 commits, every runnable command's reads and a
retried rate-limit response, through action.yml's real steps with only external services doubled.
The figure saved is the proxy's count, which must agree with what the host double received.
"""
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'tests/action-harness'))
import harness
from harness import Host, Job, Repository, budgets_yaml, event_pull_request

ROWS = 100
COMMANDS_READING_REST = 10
COMMITS = 20
METRIC = 'report-rest-requests-per-run'
TELEMETRY = HERE / '.telemetry' / 'report-job.json'


def catalog():
    """Every shape of row: REST-reading commands, local ones, own telemetry, one unrunnable."""
    write = 'echo "{\\"value\\": 1}" > "$ONEBUDGETSPEC_RESULT"'
    entries = [
        ('own-job-requests', ['onebudgetspec-github-action', 'telemetry', '--metric', METRIC, '--input', 'report-job.json'],
         {'unit': 'requests', 'labels': ['ci-report']}),
        ('not-on-this-runner', ['tool-this-runner-lacks'], {}),
    ]
    for index in range(COMMANDS_READING_REST):
        command = f'gh api repos/owner/repo/commits/{index} > /dev/null || exit 1; {write}'
        entries.append((f'reads-rest-{index}', ['bash', '-c', command], {'unit': 'requests'}))
    while len(entries) < ROWS:
        entries.append((f'local-{len(entries)}', ['bash', '-c', write], {'unit': 'bytes'}))
    return budgets_yaml(entries)


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
        heads = [repository.commit(f'base {index}') for index in range(COMMITS)]
        first = self.job(host, repository, 'push', heads[-1], run_id=400)
        self.assertEqual(first.exit_code, 0, first.stdout)

        host.artifacts.clear()
        host.runs.clear()
        history = seeded(first.saved('budget-run.json'), heads)
        host.add_run(519, heads[-1], minute=519)
        host.attach(519, 'budget-results', zip_of('rolling-history.json', json.dumps(history)))

        repository.git('checkout', '--quiet', '-b', 'feature')
        head = repository.commit('change')
        host.head = head
        figures = []
        for job_options in (
            {'event_name': 'pull_request', 'sha': head, 'run_id': 601, 'base_ref': 'main', 'register': False,
             'event': event_pull_request(1, head, heads[-1])},
            {'event_name': 'push', 'sha': heads[-1], 'run_id': 602, 'attempt': 2, 'register': False},
        ):
            host.clear_requests()
            host.refuse.append({'path': r'artifacts\?', 'status': 429, 'times': 1, 'retry_after': 1})
            job = self.job(host, repository, **job_options)
            self.assertEqual(job.exit_code, 0, job.stdout)
            telemetry = job.saved('report-job.json')
            self.assertEqual(telemetry['metrics'][METRIC]['value'], host.count(''),
                             f'the proxy and the host disagree: {host.requests}')
            self.assertEqual(host.count('commits/'), COMMANDS_READING_REST)
            self.assertEqual(len(job.rows()), ROWS)
            self.assertIn('not-on-this-runner', job.markdown())
            self.assertEqual(len(job.saved('comparison-input.json')['baseline']), COMMITS - (job_options['event_name'] == 'push'))
            figures.append(telemetry)

        worst = max(figures, key=lambda telemetry: telemetry['metrics'][METRIC]['value'])
        TELEMETRY.parent.mkdir(exist_ok=True)
        TELEMETRY.write_text(json.dumps(worst))

    def job(self, host, repository, event_name, sha, **options):
        job = Job(host, repository, event_name, sha, inputs={'token': 'real-token'}, **options)
        self.addCleanup(job.close)
        return job.run()


def zip_of(name, content):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


if __name__ == '__main__':
    unittest.main(verbosity=1)
