"""Run action.yml's real steps against doubled external services.

The driver, plugin, onevcs and core all run for real, installed the way the action installs them:
from a release archive and a wheel served by doubles of the release host and the package index.
The GitHub service is one in-process HTTP double; `actions/upload-artifact`, which reaches a
service no test can, is replaced at that same boundary and fails or overwrites as the real one does.
"""
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'owner/repo'
WORKFLOW = 42
TOOLS = ROOT / 'target' / 'action-tools'
GH = Path(__file__).resolve().parent / 'bin'
_cached = {}


def dependencies():
    """The versions action/dependencies.env pins."""
    lines = (ROOT / 'action/dependencies.env').read_text().splitlines()
    return dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('#'))


def driver_binary():
    """The driver under test: what the e2e wrapper built, instrumented when it is measured."""
    return Path(os.environ.get('SCAFFOLD_BINARY') or ROOT / 'target/debug/onebudgetspec-github-action')


def _clean_environment():
    """Coverage flags must not reach the release build of a published crate."""
    return {key: value for key, value in os.environ.items()
            if not key.startswith(('CARGO', 'RUST', 'LLVM_', '__CARGO'))}


def version():
    text = (ROOT / 'Cargo.toml').read_text()
    return re.search(r'^version = "(.+)"$', text, re.M)[1]


def rust_target():
    return {
        ('Linux', 'x86_64'): 'x86_64-unknown-linux-gnu',
        ('Linux', 'aarch64'): 'aarch64-unknown-linux-gnu',
        ('Darwin', 'arm64'): 'aarch64-apple-darwin',
    }[(platform.system(), platform.machine())]


def built_archive():
    """The real release archive of the driver under test and the published plugin."""
    if 'archive' not in _cached:
        directory = Path(tempfile.mkdtemp(prefix='action-dist-'))
        result = subprocess.run(
            ['bash', str(ROOT / 'scripts/build-action-archive.sh'), version(), rust_target()],
            env={**_clean_environment(), 'DRIVER_BINARY': str(driver_binary()), 'DIST_DIRECTORY': str(directory)},
            capture_output=True, text=True, check=False, cwd=ROOT)
        if result.returncode:
            raise RuntimeError(result.stderr)
        _cached['archive'] = next(directory.glob('*.tar.gz'))
    return _cached['archive']


def real_wheel():
    """The published core wheel, fetched once from the real index and then served by the double."""
    pinned = dependencies()['CORE_CLI_VERSION']
    tag = {('Linux', 'x86_64'): 'manylinux_2_39_x86_64', ('Linux', 'aarch64'): 'manylinux_2_39_aarch64',
           ('Darwin', 'arm64'): 'macosx_11_0_arm64'}[(platform.system(), platform.machine())]
    wheel = TOOLS / 'wheels' / f'onebudgetspec_cli-{pinned}-py3-none-{tag}.whl'
    if not wheel.is_file():
        wheel.parent.mkdir(parents=True, exist_ok=True)
        page = urllib.request.urlopen('https://pypi.org/simple/onebudgetspec-cli/', timeout=30).read().decode()
        url = re.search(r'href="([^"#]*%s)' % re.escape(wheel.name), page)[1]
        wheel.write_bytes(urllib.request.urlopen(url, timeout=120).read())
    return wheel


class Host:
    """GitHub's REST and GraphQL service, the release host and the package index."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.runs = []
        self.artifacts = {}
        self.archives = {}
        self.comments = []
        self.head = None
        self.refuse = []
        self.write_status = None
        self.read_status = None
        self.upload_failures = 0
        self.uploads_before_failing = 0
        self.next_artifact = 1000
        archive = built_archive()
        self.assets = {archive.name: archive.read_bytes()}
        self.assets['SHA256SUMS'] = (
            f'{hashlib.sha256(self.assets[archive.name]).hexdigest()}  {archive.name}\n').encode()
        self.wheel = real_wheel()
        host = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            do_GET = do_POST = do_PATCH = lambda self: host.answer(self)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def count(self, pattern=''):
        with self.lock:
            return sum(1 for _, path in self.requests if re.search(pattern, path))

    def clear_requests(self):
        with self.lock:
            self.requests.clear()

    @staticmethod
    def reply(handler, status, body, headers=()):
        handler.send_response(status)
        for key, value in headers:
            handler.send_header(key, value)
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def answer(self, handler):
        length = int(handler.headers.get('Content-Length', 0))
        body = handler.rfile.read(length)
        path = handler.path
        if path.startswith('/simple/onebudgetspec-cli/'):
            digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
            page = f'<a href="/files/{self.wheel.name}#sha256={digest}">{self.wheel.name}</a>'
            return self.reply(handler, 200, page.encode())
        if path.startswith('/files/'):
            return self.reply(handler, 200, self.wheel.read_bytes())
        release = re.fullmatch(r'/releases/v[^/]+/(.+)', path)
        if release:
            asset = self.assets.get(release[1])
            return self.reply(handler, 200 if asset is not None else 404, asset or b'missing')
        objects = re.fullmatch(r'/objects/(\d+)', path)
        if objects:
            return self.reply(handler, 200, self.archives[objects[1]])
        with self.lock:
            self.requests.append((handler.command, path))
            for rule in self.refuse:
                if re.search(rule['path'], path) and rule['times'] > 0:
                    rule['times'] -= 1
                    headers = [('Retry-After', str(rule['retry_after']))] if rule.get('retry_after') is not None else []
                    return self.reply(handler, rule['status'], b'{"message":"API rate limit exceeded"}', headers)
        if handler.headers.get('Authorization', '') in ('', 'Bearer '):
            return self.reply(handler, 401, b'{"message":"Bad credentials"}')
        if path == '/graphql':
            return self.graphql(handler, json.loads(body))
        if handler.command in ('POST', 'PATCH'):
            return self.write_comment(handler, path, json.loads(body))
        return self.read(handler, path)

    def graphql(self, handler, payload):
        if self.read_status:
            return self.reply(handler, self.read_status, b'{"message":"read refused"}')
        nodes = [dict(id=c['node_id'], databaseId=c['id'], url=c['html_url'], body=c['body'],
                      author={'login': 'viewer'}) for c in self.comments]
        answer = {'data': {'viewer': {'login': 'viewer'}, 'repository': {'pullRequest': {
            'headRefOid': self.head,
            'comments': {'nodes': nodes, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}}
        self.reply(handler, 200, json.dumps(answer).encode())

    def write_comment(self, handler, path, payload):
        if self.write_status:
            return self.reply(handler, self.write_status, b'{"message":"Resource not accessible by integration"}')
        with self.lock:
            if handler.command == 'POST':
                number = 100 + len(self.comments)
                comment = dict(id=number, node_id='owned-%d' % number,
                               html_url='https://github.com/owner/repo/pull/1#issuecomment-%d' % number)
                self.comments.append(comment)
            else:
                comment = next(c for c in self.comments if str(c['id']) == path.split('/')[-1])
            comment['body'] = payload['body']
        self.reply(handler, 200, json.dumps(comment).encode())

    def read(self, handler, path):
        clean = path.split('?')[0]
        run = re.fullmatch(r'/repos/owner/repo/actions/runs/(\d+)', clean)
        if run:
            return self.reply(handler, 200, json.dumps({'id': int(run[1]), 'workflow_id': WORKFLOW}).encode())
        if '/actions/workflows/' in clean:
            return self.reply(handler, 200, json.dumps({'workflow_runs': self.runs}).encode())
        listed = re.fullmatch(r'/repos/owner/repo/actions/runs/(\d+)/artifacts', clean)
        if listed:
            return self.reply(handler, 200, json.dumps({'artifacts': self.artifacts.get(listed[1], [])}).encode())
        archive = re.fullmatch(r'/repos/owner/repo/actions/artifacts/(\d+)/zip', clean)
        if archive:
            return self.reply(handler, 302, b'', [('Location', self.url + '/objects/' + archive[1])])
        if clean.startswith('/repos/owner/repo/'):
            return self.reply(handler, 200, b'{"ok":true}')
        return self.reply(handler, 404, b'{"message":"Not Found"}')

    def add_run(self, run_id, head, attempt=1, minute=0, branch='main', event='push', workflow=WORKFLOW, status='completed'):
        with self.lock:
            self.runs = [run for run in self.runs if run['id'] != run_id]
            self.runs.append({
                'id': run_id, 'head_sha': head, 'run_attempt': attempt, 'workflow_id': workflow,
                'name': 'budgets', 'event': event, 'head_branch': branch, 'status': status,
                'conclusion': 'success', 'run_started_at': None,
                'created_at': '2026-11-01T%02d:%02d:00Z' % (minute // 60, minute % 60),
                'updated_at': '2026-11-01T23:59:00Z'})

    def attach(self, run_id, name, data, attempt=1):
        """One completed artifact upload: immutable, with its own id."""
        with self.lock:
            self.next_artifact += 1
            self.archives[str(self.next_artifact)] = data
            self.artifacts.setdefault(str(run_id), []).append({
                'id': self.next_artifact, 'name': name, 'size_in_bytes': len(data), 'expired': False, 'attempt': attempt})
            return self.next_artifact

    def upload(self, run_id, attempt, name, directory, overwrite):
        """What upload-artifact does: one counted operation; within one attempt, a name is
        replaced only if told, and an earlier attempt's artifact stays."""
        with self.lock:
            self.requests.append(('UPLOAD', f'artifact/{name}'))
            failing = self.upload_failures > 0 and self.uploads_before_failing == 0
            if failing:
                self.upload_failures -= 1
            self.uploads_before_failing = max(0, self.uploads_before_failing - 1)
            existing = self.artifacts.get(str(run_id), [])
            if overwrite:
                self.artifacts[str(run_id)] = [a for a in existing if (a['name'], a['attempt']) != (name, attempt)]
            elif any((a['name'], a['attempt']) == (name, attempt) for a in existing):
                failing = True
        if failing:
            return False
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(Path(directory).rglob('*')):
                relative = path.relative_to(directory)
                if path.is_file() and not any(part.startswith('.') for part in relative.parts):
                    archive.write(path, relative)
        self.attach(run_id, name, buffer.getvalue(), attempt)
        return True

    def artifact_files(self, run_id, name='budget-results'):
        """The saved files of a run's newest artifact of that name."""
        listed = [a for a in self.artifacts.get(str(run_id), []) if a['name'] == name]
        if not listed:
            return None
        data = self.archives[str(max(a['id'] for a in listed))]
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {member: archive.read(member) for member in archive.namelist()}

    def expire(self, run_id):
        for artifact in self.artifacts.get(str(run_id), []):
            artifact['expired'] = True


class Repository:
    """A real git repository the action checks out."""

    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        self.git('init', '--quiet', '--initial-branch', 'main')

    def git(self, *args):
        result = subprocess.run(
            ['git', '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test', *args],
            cwd=self.path, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(f'git {args}: {result.stderr}')
        return result.stdout.strip()

    def write(self, relative, content):
        path = self.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, message='change'):
        (self.path / 'commit-marker').write_text(message + os.urandom(8).hex())
        self.git('add', '-A')
        self.git('commit', '--quiet', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def close(self):
        self.temporary.cleanup()


def budgets_yaml(entries):
    """A budgets file from (id, command, options) entries."""
    return yaml.safe_dump({'schema_version': 1, 'budgets': [
        {'id': entry, 'measure': options.get('measure', 'reported'), 'command': command,
         'unit': options.get('unit', 'count'), 'direction': options.get('direction', 'max'),
         'threshold': options.get('threshold', 100), **({'labels': options['labels']} if 'labels' in options else {})}
        for entry, command, options in entries]})


def event_pull_request(number, head, base, head_repository=REPOSITORY, draft=False):
    return {'pull_request': {
        'number': number, 'draft': draft,
        'head': {'sha': head, 'repo': {'full_name': head_repository}},
        'base': {'sha': base}}}


class Expression:
    """The `${{ }}` language action.yml uses, evaluated over the workflow contexts."""

    def __init__(self, context):
        self.context = context

    def value(self, text):
        text = text.strip()
        for separator, combine in (('||', lambda a, b: a or b), ('&&', lambda a, b: a and b)):
            if separator in text:
                left, right = text.split(separator, 1)
                return combine(self.value(left), self.value(right))
        if '==' in text:
            left, right = text.split('==', 1)
            return self.value(left) == self.value(right)
        if text == 'always()':
            return True
        if text.startswith("'"):
            return text.strip("'")
        value = self.context
        for part in text.split('.'):
            value = value.get(part, '') if isinstance(value, dict) else ''
        return value

    def substitute(self, text):
        return re.sub(r'\$\{\{(.*?)\}\}', lambda m: str(self.value(m[1])), str(text))


class Job:
    """One run of the action's composite steps, in order, as the runner would take them."""

    def __init__(self, host, repository, event_name, sha, event=None, inputs=None, run_id=1, attempt=1,
                 ref_name='main', ref_type='branch', base_ref='', environment=None, register=True, action_path=None):
        self.host, self.repository = host, repository
        self.event_name, self.sha = event_name, sha
        self.run_id, self.attempt = run_id, attempt
        self.register = register
        self.inputs = inputs or {}
        self.environment = environment or {}
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.github = {
            'event_name': event_name, 'repository': REPOSITORY, 'server_url': 'https://github.com',
            'run_id': str(run_id), 'run_attempt': str(attempt), 'sha': sha, 'ref_name': ref_name,
            'ref_type': ref_type, 'base_ref': base_ref, 'event': event or {}}
        self.action_path = Path(action_path) if action_path else self._packaged_action()
        self.outcomes = {}
        self.stdout = ''
        self.exit_code = None
        self.summary = ''

    def _packaged_action(self):
        """What `uses:` checks out: the repository at the action's tag."""
        path = self.temp / 'action'
        path.mkdir()
        shutil.copy2(ROOT / 'action.yml', path)
        shutil.copy2(ROOT / 'Cargo.toml', path)
        shutil.copytree(ROOT / 'action', path / 'action')
        return path

    def _defaults(self):
        document = yaml.safe_load((self.action_path / 'action.yml').read_text())
        return document, {name: str(spec.get('default', '')) for name, spec in document['inputs'].items()}

    def run(self):
        document, inputs = self._defaults()
        inputs.update(self.inputs)
        steps_context = {}
        context = {'inputs': inputs, 'github': self.github, 'steps': steps_context}
        expression = Expression(context)
        output_directory = self.temp / 'outputs'
        output_directory.mkdir()
        summary = self.temp / 'summary.md'
        failed = False
        for index, step in enumerate(document['runs']['steps']):
            condition = step.get('if')
            should_run = expression.value(condition) if condition else not failed
            identifier = step.get('id', f'step-{index}')
            if not should_run:
                steps_context[identifier] = {'outcome': 'skipped', 'outputs': {}}
                continue
            if 'uses' in step:
                outcome, outputs = self._upload(step, expression)
            else:
                outcome, outputs = self._shell(step, expression, output_directory / identifier, summary)
            steps_context[identifier] = {'outcome': outcome, 'outputs': outputs}
            self.outcomes[identifier] = outcome
            if outcome == 'failure' and not step.get('continue-on-error'):
                failed = True
        self.summary = summary.read_text() if summary.exists() else ''
        if self.exit_code is None:
            self.exit_code = 1 if failed else 0
        if self.register:
            self.host.add_run(self.run_id, self.sha, self.attempt, minute=self.run_id)
        return self

    def _upload(self, step, expression):
        arguments = {key: expression.substitute(value) for key, value in step['with'].items()}
        if not step['uses'].startswith('actions/upload-artifact@'):
            raise AssertionError(f'unexpected action {step["uses"]}')
        uploaded = self.host.upload(self.run_id, self.attempt, arguments['name'], arguments['path'],
                                    arguments.get('overwrite') == 'True')
        return ('success' if uploaded else 'failure'), {}

    def _shell(self, step, expression, output_file, summary):
        environment = {
            'PATH': f'{GH}:{os.environ["PATH"]}',
            'HOME': str(self.temp),
            'ONEVCS_HOME': str(self.temp / 'onevcs-home'),
            'GITHUB_ACTION_PATH': str(self.action_path),
            'GITHUB_OUTPUT': str(output_file),
            'GITHUB_STEP_SUMMARY': str(summary),
            'RUNNER_TEMP': str(self.temp),
            'BOUNDARY_HOST': self.host.url,
            'ONEBUDGETSPEC_ACTION_RELEASES': self.host.url + '/releases',
            'PIP_INDEX_URL': self.host.url + '/simple',
            **self.environment,
        }
        if 'LLVM_PROFILE_FILE' in os.environ:
            environment['LLVM_PROFILE_FILE'] = os.environ['LLVM_PROFILE_FILE']
        for key, value in step.get('env', {}).items():
            environment[key] = expression.substitute(value)
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', expression.substitute(step['run'])],
            cwd=self.repository.path, env=environment, capture_output=True, text=True, check=False)
        self.stdout += result.stdout + result.stderr
        outputs = {}
        if output_file.exists():
            outputs = dict(line.split('=', 1) for line in output_file.read_text().splitlines() if '=' in line)
        if result.returncode:
            self.exit_code = result.returncode
        return ('success' if result.returncode == 0 else 'failure'), outputs

    @property
    def out(self):
        return self.temp / 'onebudgetspec-report'

    def saved(self, name):
        return json.loads((self.out / name).read_text())

    def rows(self):
        return {row['id']: row for row in self.saved('comparison-report.json')['rows']}

    def markdown(self):
        return (self.out / 'report.md').read_text()

    def close(self):
        self.temporary.cleanup()
