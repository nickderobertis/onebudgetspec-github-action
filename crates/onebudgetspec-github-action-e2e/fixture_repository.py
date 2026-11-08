"""One fixture repository's journey, run the same way against any release of the action.

`run` drives it through a harness module: six base pushes, then two heads of one pull request,
with deterministic figures, so one release's verdicts, flags, exits, saved files and telemetry
can be compared with another's. `fixtures/v0.2.0` holds what v0.2.0 gave for it; capture it again
from a v0.2.0 checkout, its driver built, with

    SCAFFOLD_BINARY=<checkout>/target/debug/onebudgetspec-github-action \\
      uv run --no-project --with pyyaml python fixture_repository.py <checkout> fixtures/v0.2.0
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

# The figures each step writes; the pull request's first head regresses and its second fixes most.
BASE = {'requests-read': 7, 'bundle-bytes': 500, 'coverage-percent': 90, 'enforced-limit': 10, 'shadow-limit': 50}
FIRST_HEAD = {**BASE, 'requests-read': 20, 'enforced-limit': 99, 'shadow-limit': 70}
SECOND_HEAD = {**BASE, 'shadow-limit': 70}
INPUTS = {'token': 'real-token', 'enforcement-exclude-labels': 'provisional'}
OWN = ['onebudgetspec-github-action', 'telemetry', '--metric', 'report-github-api-requests-per-run',
       '--input', '.telemetry/report-job.json']
BASE_PUSHES = 6
FIRST_RUN = 1001
# The files a job's own saved-results artifact holds that are JSON documents of the core's types.
SCHEMA_OF = {'budget-run.json': 'budget-run', 'comparison-input.json': 'comparison-input',
             'comparison-report.json': 'comparison-report'}


def reported(source):
    return ['bash', '-c', f'echo "{{\\"value\\": $(cat {source})}}" > "$ONEBUDGETSPEC_RESULT"']


def catalog(harness, repository, figures):
    for budget, figure in figures.items():
        repository.write(f'values-{budget}', f'{figure}\n')
    reads = 'gh api repos/owner/repo/commits/main > /dev/null || exit 1; '
    repository.write('budgets.yaml', harness.budgets_yaml([
        ('requests-read', ['bash', '-c', reads + reported('values-requests-read')[2]],
         {'unit': 'requests', 'threshold': 100}),
        ('bundle-bytes', reported('values-bundle-bytes'), {'unit': 'bytes', 'threshold': 1000}),
        ('coverage-percent', reported('values-coverage-percent'), {'unit': 'percent', 'direction': 'min', 'threshold': 80}),
        ('enforced-limit', reported('values-enforced-limit'), {'threshold': 40}),
        ('shadow-limit', reported('values-shadow-limit'), {'threshold': 40, 'labels': ['provisional']}),
        ('needs-a-tool', ['no-such-tool-on-this-runner', '--measure'], {'labels': ['ci-report']}),
        ('needs-predecessor', ['cat', '.telemetry/predecessor.json'], {}),
    ]))
    repository.write('.github/reporting/budgets.yaml', harness.budgets_yaml([
        ('job-requests', OWN, {'unit': 'requests', 'threshold': 1000, 'labels': ['ci-report']}),
    ]))


def key_paths(value, prefix=''):
    """Every field path of a JSON value, list items merged, so two documents' shapes compare."""
    if isinstance(value, dict):
        paths = set()
        for key, item in value.items():
            paths |= {f'{prefix}.{key}'} | key_paths(item, f'{prefix}.{key}')
        return paths
    if isinstance(value, list):
        return set().union(*(key_paths(item, prefix + '[]') for item in value)) if value else set()
    return set()


def history_block(body):
    """The hidden history the standing comment carries."""
    start = '<!-- onebudgetspec-history\n'
    return json.loads(body[body.index(start) + len(start):body.rindex('\n-->')])


class Recorder:
    """What one journey gave: per job, and the commits it made, in order."""

    def __init__(self, harness, host, repository):
        self.harness, self.host, self.repository = harness, host, repository
        self.commits, self.jobs, self.next_run = [], [], FIRST_RUN

    def commit(self, message):
        sha = self.repository.commit(message)
        self.commits.append(sha)
        return sha

    def job(self, name, event_name, sha, **options):
        job = self.harness.Job(self.host, self.repository, event_name, sha, run_id=self.next_run,
                               inputs=INPUTS, **options).run()
        self.next_run += 1
        files = self.host.artifact_files(job.run_id) or {}
        root = self.repository.path
        self.jobs.append({
            'name': name, 'run_id': job.run_id, 'exit_code': job.exit_code,
            'rows': {row['id']: {'verdict': row.get('verdict'), 'flag': row['flag']} for row in job.rows().values()},
            'artifact_files': sorted(files),
            'documents': {name: json.loads(content) for name, content in files.items() if name.endswith('.json')},
            'markdown': files.get('report.md', b'').decode(),
            # The saved report's own bytes, which a render is compared against; never captured.
            'saved_report': files.get('comparison-report.json', b'').decode(),
            # The released core's own JSON report for the saved input, from the CLI this job
            # installed; what the saved report is checked against, and never captured either.
            'core_report': core_report(job),
            'telemetry': {str(path.relative_to(root)): json.loads(path.read_text())
                          for path in sorted(root.glob('**/.telemetry/*.json'))},
            'comment': self.host.comments[-1]['body'] if self.host.comments else None,
        })
        return job


def core_report(job):
    """`onebudgetspec report --json` over the job's saved input, by the core the action installed."""
    saved = job.out / 'comparison-input.json'
    if not saved.is_file():
        return None
    answer = subprocess.run([str(job.temp / 'onebudgetspec-action/tools/onebudgetspec'), 'report', '--json',
                             '--input', str(saved)], capture_output=True, text=True, check=False)
    # 1 is a report with a row over its limit, as `check` exits; anything else gave no report.
    if answer.returncode not in (0, 1):
        raise RuntimeError(f'onebudgetspec report exited {answer.returncode}: {answer.stderr}')
    return json.loads(answer.stdout)


def run(harness):
    """The journey, through `harness`: what each job gave, the commits and the base history."""
    host, repository = harness.Host(), harness.Repository()
    try:
        recorder = Recorder(harness, host, repository)
        catalog(harness, repository, BASE)
        recorder.commit('catalog')
        for index in range(BASE_PUSHES):
            recorder.job(f'base-{index + 1}', 'push', recorder.commit(f'base {index + 1}'))
        base = recorder.commits[-1]
        repository.git('checkout', '--quiet', '-b', 'feature')
        for name, figures in (('first-head', FIRST_HEAD), ('second-head', SECOND_HEAD)):
            catalog(harness, repository, figures)
            head = recorder.commit(name)
            host.head = head
            recorder.job(name, 'pull_request', head, event=harness.event_pull_request(1, head, base), base_ref='main')
        return {'commits': recorder.commits, 'jobs': recorder.jobs,
                'rolling_history': recorder.jobs[BASE_PUSHES - 1]['documents']['rolling-history.json']}
    finally:
        host.close()
        repository.close()


def capture(checkout, destination):
    """Record a release's journey, its action.yml and its core's schema bundle under `destination`."""
    sys.path.insert(0, str(Path(checkout) / 'tests/action-harness'))
    import harness
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    journey = run(harness)
    # The earlier base pushes' files repeat the last one's shape; only their verdicts are kept.
    for job in journey['jobs'][:BASE_PUSHES - 1]:
        for bulky in ('documents', 'markdown', 'comment'):
            job.pop(bulky)
    for job in journey['jobs']:
        job.pop('saved_report')
        job.pop('core_report')
    (destination / 'journey.json').write_text(json.dumps(journey, indent=1, sort_keys=True) + '\n')
    shutil.copy2(Path(checkout) / 'action.yml', destination / 'action.yml')
    version = harness.dependencies()['CORE_CLI_VERSION']
    tools = Path(checkout) / 'target/action-tools/schema-cli'
    subprocess.run(['python3', str(Path(checkout) / 'action/install_core.py'), version, str(tools)], check=True)
    bundle = subprocess.run([str(tools / 'onebudgetspec'), 'schema'], capture_output=True, text=True, check=True).stdout
    (destination / 'schema-bundle.json').write_text(json.dumps(json.loads(bundle), indent=1, sort_keys=True) + '\n')


if __name__ == '__main__':
    capture(*sys.argv[1:])
