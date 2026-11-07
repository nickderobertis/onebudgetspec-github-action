"""Run action.yml's real steps against doubled external services.

The driver, plugin, onevcs and core all run for real, installed the way the action installs them:
from a release archive and a wheel served by doubles of the release host and the package index.
The real `gh` and the real `actions/upload-artifact` and `actions/download-artifact` code run too. `gh` reaches the double through
its own development host, `github.localhost`, over `HTTP_PROXY`. One in-process HTTP double stands in for GitHub's
API, its artifact service, blob and object storage, the release host and the package index, and
records every request of a run: nothing is counted that the double did not receive.
"""
import base64
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import time
import tempfile
import threading
import urllib.parse
import urllib.request
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'owner/repo'
WORKFLOW = 42
TOOLS = ROOT / 'target' / 'action-tools'
# The exact code `actions/upload-artifact@v4` currently resolves to.
UPLOAD_ARTIFACT = ('v4.6.2', 'ea165f8d65b6e75b540449e92b4886f43607fa02')
# The exact code `actions/download-artifact@v4` currently resolves to.
DOWNLOAD_ARTIFACT = ('v4.3.0', 'd3f86a106a0bac45b974a628896c90dbdf5c8093')
TWIRP = '/twirp/github.actions.results.api.v1.ArtifactService/'
API_ORIGIN = 'http://api.github.localhost'
_cached = {}


# Real onevcs follows only `Link` URLs on the real API origin, so for a paged listing this routes
# exactly that origin to the double and hands everything else to the real `gh` untouched.
ROUTE_API_ORIGIN = Path(__file__).resolve().parent / 'route-api-origin'


# No journey command legitimately comes near either limit. They exist so a mistake here, such as
# a `gh` that runs itself, stops at a handful of processes on a host other work shares. A runaway
# fails its journey. RLIMIT_NPROC cannot do this: it counts every process of the user, on any host.
MAX_COMMAND_PROCESSES = 64
MAX_COMMAND_SECONDS = 300


def bounded_run(arguments, **options):
    """`subprocess.run`, in its own process group, ended whole if it passes either limit."""
    options.pop('capture_output', None)
    options.pop('check', None)
    text = options.pop('text', False)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(arguments, stdout=stdout, stderr=stderr, start_new_session=True, **options)
        deadline = time.monotonic() + MAX_COMMAND_SECONDS
        while process.poll() is None:
            members = subprocess.run(['ps', '-A', '-o', 'pgid='], capture_output=True, text=True, check=False)
            count = members.stdout.split().count(str(process.pid))
            if count > MAX_COMMAND_PROCESSES or time.monotonic() > deadline:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise AssertionError(f'{arguments} ran away: {count} processes after its limits; nothing else was signalled')
            time.sleep(0.1)
        stdout.seek(0)
        stderr.seek(0)
        out, err = stdout.read(), stderr.read()
    if text:
        out, err = out.decode(errors='replace'), err.decode(errors='replace')
    return subprocess.CompletedProcess(arguments, process.returncode, out, err)


def real_gh_environment(host_url, home):
    """What sends the real `gh` to the double, and never to GitHub, with an empty config.

    With no token for its target `gh` asks the keyring, whose session bus `dbus-launch` starts and
    nothing ends; each one left behind holds inotify instances on the host. `disabled:` stops that.
    """
    if shutil.which('gh') is None:
        raise RuntimeError('the journeys drive the real GitHub CLI; install gh, then re-run')
    return {'GH_HOST': 'github.localhost', 'HTTP_PROXY': host_url, 'GH_CONFIG_DIR': str(Path(home) / 'gh-config'),
            'GH_PROMPT_DISABLED': '1', 'http_proxy': host_url, 'ROUTED_GH': shutil.which('gh'),
            'DBUS_SESSION_BUS_ADDRESS': 'disabled:'}


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


def _action_entry(name, pinned, relative):
    """A real action's entry point, fetched once at its pinned commit."""
    directory = TOOLS / f'{name}-{pinned[0]}'
    entry = directory / relative
    if not entry.is_file():
        url = f'https://codeload.github.com/actions/{name}/tar.gz/{pinned[1]}'
        data = urllib.request.urlopen(url, timeout=120).read()
        directory.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive.getmembers():
                member.name = member.name.split('/', 1)[-1]
                if member.name.startswith(str(Path(relative).parent) + '/') or member.name == 'action.yml':
                    archive.extract(member, directory, filter='data')
    return directory, entry


def artifact_action(uses):
    """The directory and entry point of the artifact action a step names."""
    name, reference = uses.split('@', 1)
    if name == 'actions/upload-artifact' and harness_major_matches(UPLOAD_ARTIFACT, reference):
        return _action_entry('upload-artifact', UPLOAD_ARTIFACT, 'dist/upload/index.js')
    if name == 'actions/download-artifact' and harness_major_matches(DOWNLOAD_ARTIFACT, reference):
        return _action_entry('download-artifact', DOWNLOAD_ARTIFACT, 'dist/index.js')
    raise AssertionError(f'unexpected action {uses}')


def runtime_token(run_id, attempt):
    """The token a runner hands an action; the service reads its run identity from the claim."""
    def encode(document):
        return base64.urlsafe_b64encode(json.dumps(document).encode()).decode().rstrip('=')
    claim = {'scp': f'Actions.Results:{run_id}-{attempt}:job', 'exp': 4102444800}
    return f'{encode({"alg": "none"})}.{encode(claim)}.signature'


class Host:
    """GitHub's REST and GraphQL service, the release host and the package index."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.transfers = []
        self.pending_uploads = {}
        self.transient_finalize_failures = 0
        self.paged_artifacts = False
        self.requests_not_through_gh = 0
        self.runs = []
        self.artifacts = {}
        self.archives = {}
        self.comments = []
        self.comment_write_times = []
        # Who the token is and who its comments read as; `actions_bot` is the Actions token's shape.
        self.viewer_login = 'viewer'
        self.comment_author = {'__typename': 'User', 'login': 'viewer'}
        self.head = None
        self.refuse = []
        self.write_status = None
        self.read_status = None
        self.upload_failures = 0
        self.uploads_before_failing = 0
        self.next_artifact = 1000
        self.pulls = {}
        self.refuse_list_artifacts = False
        archive = built_archive()
        self.assets = {archive.name: archive.read_bytes()}
        self.assets['SHA256SUMS'] = (
            f'{hashlib.sha256(self.assets[archive.name]).hexdigest()}  {archive.name}\n').encode()
        self.wheel = real_wheel()
        host = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            do_GET = do_POST = do_PATCH = do_PUT = lambda self: host.answer(self)

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
            self.transfers.clear()
            self.requests_not_through_gh = 0

    def transferred(self, service):
        """How many file-transfer requests one service received."""
        with self.lock:
            return sum(1 for name, _, _ in self.transfers if name == service)

    def _transfer(self, service, handler):
        with self.lock:
            self.transfers.append((service, handler.command, handler.path))

    @staticmethod
    def reply(handler, status, body, headers=()):
        handler.send_response(status)
        if body[:1] in (b'{', b'['):
            handler.send_header('Content-Type', 'application/json')
        for key, value in headers:
            handler.send_header(key, value)
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def answer(self, handler):
        length = int(handler.headers.get('Content-Length', 0))
        body = handler.rfile.read(length)
        # A request through HTTP_PROXY names its whole URL.
        path = re.sub(r'^http://[^/]+', '', handler.path)
        if path.startswith(TWIRP):
            self._transfer('artifact-service', handler)
            return self.artifact_service(handler, path.removeprefix(TWIRP), json.loads(body))
        if path.startswith('/blob/'):
            self._transfer('blob-storage', handler)
            return self.blob(handler, path, body)
        if path.startswith('/simple/onebudgetspec-cli/'):
            self._transfer('package-index', handler)
            digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
            page = f'<a href="/files/{self.wheel.name}#sha256={digest}">{self.wheel.name}</a>'
            return self.reply(handler, 200, page.encode())
        if path.startswith('/files/'):
            self._transfer('package-index', handler)
            return self.reply(handler, 200, self.wheel.read_bytes())
        release = re.fullmatch(r'/releases/v[^/]+/(.+)', path)
        if release:
            self._transfer('release-host', handler)
            asset = self.assets.get(release[1])
            return self.reply(handler, 200 if asset is not None else 404, asset or b'missing')
        objects = re.fullmatch(r'/objects/(\d+)', path)
        if objects:
            self._transfer('object-storage', handler)
            return self.reply(handler, 200, self.archives[objects[1]])
        with self.lock:
            self.requests.append((handler.command, path))
            if not handler.headers.get('User-Agent', '').startswith('GitHub CLI'):
                self.requests_not_through_gh += 1
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
        if 'pagedItems' in payload['query']:
            page = payload.get('variables', {}).get('endCursor')
            answer = {'data': {'pagedItems': {'nodes': [1], 'pageInfo': {'hasNextPage': page is None, 'endCursor': 'next'}}}}
            return self.reply(handler, 200, json.dumps(answer).encode())
        nodes = [dict(id=c['node_id'], databaseId=c['id'], url=c['html_url'], body=c['body'],
                      author=self.comment_author) for c in self.comments]
        answer = {'data': {'viewer': {'login': self.viewer_login}, 'repository': {'name': 'repo', 'pullRequest': {
            'headRefOid': self.head,
            'comments': {'nodes': nodes, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}}
        self.reply(handler, 200, json.dumps(answer).encode())

    def write_comment(self, handler, path, payload):
        with self.lock:
            self.comment_write_times.append(time.time())
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
        pulls = re.fullmatch(r'/repos/owner/repo/commits/([^/]+)/pulls', clean)
        if pulls:
            return self.reply(handler, 200, json.dumps(self.pulls.get(pulls[1], [])).encode())
        if '/actions/workflows/' in clean:
            return self.reply(handler, 200, json.dumps({'workflow_runs': self.listed_runs(path)}).encode())
        listed = re.fullmatch(r'/repos/owner/repo/actions/runs/(\d+)/artifacts', clean)
        if listed:
            # As GitHub lists them: newest first, narrowed by an exact `name`.
            artifacts = sorted(self.artifacts.get(listed[1], []), key=lambda item: item['id'], reverse=True)
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get('name')
            if name:
                artifacts = [item for item in artifacts if item['name'] == name[0]]
            if self.paged_artifacts and 'page=2' not in path:
                # The first page holds only an unrelated artifact; the one wanted is on the second.
                unrelated = [{'id': 999, 'name': 'unrelated', 'size_in_bytes': 1, 'expired': False}]
                link = f'<https://api.github.com{clean}?per_page=100&page=2>; rel="next"'
                return self.reply(handler, 200, json.dumps({'artifacts': unrelated}).encode(), [('Link', link)])
            return self.reply(handler, 200, json.dumps({'artifacts': artifacts}).encode())
        archive = re.fullmatch(r'/repos/owner/repo/actions/artifacts/(\d+)/zip', clean)
        if archive:
            return self.reply(handler, 302, b'', [('Location', self.url + '/objects/' + archive[1])])
        if clean == '/repos/owner/repo/items':
            page = int((re.search(r'[?&]page=(\d+)', path) or [0, 1])[1])
            links = [('Link', f'<{API_ORIGIN}{clean}?page={page + 1}>; rel="next"')] if page < 3 else []
            return self.reply(handler, 200, b'[1]', links)
        if clean.startswith('/repos/owner/repo/'):
            return self.reply(handler, 200, b'{"ok":true}')
        return self.reply(handler, 404, b'{"message":"Not Found"}')

    def listed_runs(self, path):
        """A workflow's runs as GitHub filters them: `event`, `head_sha`, then newest first, a page."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        runs = [run for run in self.runs
                if all(run[key] == query[key][0] for key in ('event', 'head_sha') if key in query)]
        if 'head_sha' not in query:
            return runs
        runs.sort(key=lambda run: run['created_at'], reverse=True)
        return runs[:int(query.get('per_page', ['30'])[0])]

    def merge_pull_request(self, merge_commit, head, number=1):
        """What `commits/SHA/pulls` answers for the merge commit of a merged pull request."""
        self.pulls[merge_commit] = [{'number': number, 'state': 'closed', 'merged_at': '2026-11-01T00:00:00Z',
                                     'merge_commit_sha': merge_commit, 'head': {'sha': head}}]

    def actions_bot(self):
        """Answer as GitHub does for the Actions token: one account, spelled two ways."""
        self.viewer_login = 'github-actions[bot]'
        self.comment_author = {'__typename': 'Bot', 'login': 'github-actions'}

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
                'id': self.next_artifact, 'name': name, 'size_in_bytes': len(data), 'expired': False, 'attempt': attempt,
                'created_at': '2026-11-01T00:%02d:%02d.000Z' % (self.next_artifact // 60 % 60, self.next_artifact % 60)})
            return self.next_artifact

    def artifact_service(self, handler, method, request):
        """The results service upload-artifact speaks to, as real as its answers need to be."""
        if method == 'CreateArtifact':
            run_id, attempt = request['workflow_run_backend_id'].rsplit('-', 1)
            name = request['name']
            with self.lock:
                failing = self.upload_failures > 0 and self.uploads_before_failing == 0
                if failing:
                    self.upload_failures -= 1
                self.uploads_before_failing = max(0, self.uploads_before_failing - 1)
                taken = any((a['name'], a['attempt']) == (name, int(attempt))
                            for a in self.artifacts.get(run_id, []))
                if not failing and not taken:
                    self.pending_uploads[name] = {'run_id': run_id, 'attempt': int(attempt), 'blocks': []}
            if failing or taken:
                status = 403 if failing else 409
                return self.reply(handler, status, b'{"code":"permission_denied","msg":"refused"}')
            url = f'{self.url}/blob/{name}?sig=signed'
            return self.reply(handler, 200, json.dumps({'ok': True, 'signed_upload_url': url}).encode())
        if method == 'FinalizeArtifact':
            with self.lock:
                if self.transient_finalize_failures > 0:
                    self.transient_finalize_failures -= 1
                    return self.reply(handler, 503, b'{"code":"unavailable","msg":"try again"}')
                pending = self.pending_uploads.pop(request['name'])
            identifier = self.attach(pending['run_id'], request['name'], b''.join(pending['blocks']), pending['attempt'])
            return self.reply(handler, 200, json.dumps({'ok': True, 'artifact_id': str(identifier)}).encode())
        if method == 'ListArtifacts':
            if self.refuse_list_artifacts:
                return self.reply(handler, 403, b'{"code":"permission_denied","msg":"refused"}')
            run_id = request['workflow_run_backend_id'].rsplit('-', 1)[0]
            with self.lock:
                listed = [{'workflow_run_backend_id': request['workflow_run_backend_id'],
                           'workflow_job_run_backend_id': request['workflow_job_run_backend_id'],
                           'database_id': str(item['id']), 'name': item['name'], 'size': str(item['size_in_bytes']),
                           'created_at': item['created_at']}
                          for item in self.artifacts.get(run_id, []) if not item['expired']
                          and request.get('name_filter', item['name']) == item['name']
                          and str(request.get('id_filter', item['id'])) == str(item['id'])]
            return self.reply(handler, 200, json.dumps({'artifacts': listed}).encode())
        if method == 'GetSignedArtifactURL':
            run_id = request['workflow_run_backend_id'].rsplit('-', 1)[0]
            with self.lock:
                found = [item for item in self.artifacts.get(run_id, []) if item['name'] == request['name']]
            newest = max(found, key=lambda item: item['id'])
            return self.reply(handler, 200, json.dumps({'signed_url': f'{self.url}/objects/{newest["id"]}'}).encode())
        return self.reply(handler, 404, json.dumps({'code': 'not_found', 'msg': method}).encode())

    def blob(self, handler, path, body):
        """Blob storage behind a signed URL: blocks, then the list that commits them."""
        name = path.split('?')[0].removeprefix('/blob/')
        if 'comp=block&' in path:
            with self.lock:
                self.pending_uploads[name]['blocks'].append(body)
        headers = [('ETag', '"etag"'), ('Last-Modified', 'Mon, 01 Jan 2026 00:00:00 GMT'), ('x-ms-request-id', 'id')]
        return self.reply(handler, 201, b'', headers)

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

    def git(self, *args, environment=None):
        result = subprocess.run(
            ['git', '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test', *args],
            cwd=self.path, capture_output=True, text=True, check=False, env={**os.environ, **(environment or {})})
        if result.returncode:
            raise RuntimeError(f'git {args}: {result.stderr}')
        return result.stdout.strip()

    def write(self, relative, content):
        path = self.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, message='change', committed_at=None):
        (self.path / 'commit-marker').write_text(message + os.urandom(8).hex())
        self.git('add', '-A')
        self.git('commit', '--quiet', '-m', message,
                 environment={'GIT_COMMITTER_DATE': committed_at} if committed_at else None)
        return self.git('rev-parse', 'HEAD')

    def close(self):
        self.temporary.cleanup()


def harness_major_matches(pinned, reference):
    """action.yml's floating major must be the one the pinned code belongs to."""
    return pinned[0].startswith(reference + '.')


def budgets_yaml(entries):
    """A budgets file from (id, command, options) entries."""
    return yaml.safe_dump({'schema_version': 1, 'budgets': [
        {'id': entry, 'measure': options.get('measure', 'reported'), 'command': command,
         'unit': options.get('unit', 'count'), 'direction': options.get('direction', 'max'),
         'threshold': options.get('threshold', 100), **({'labels': options['labels']} if 'labels' in options else {})}
        for entry, command, options in entries]})


def check_result(budget, actual, ended_at='2099-01-01T00:00:00Z', file='budgets.yaml', labels=(), unit='count',
                 direction='max', threshold=100.0, detail=None):
    """One result as `onebudgetspec check --save-results` writes it, its verdict core's own rule."""
    headroom = threshold - actual if direction == 'max' else actual - threshold
    return {'id': budget, 'file': file, 'labels': list(labels), 'unit': unit, 'direction': direction,
            'threshold': float(threshold), 'verdict': 'within' if headroom >= 0 else 'over', 'actual': float(actual),
            'headroom': float(headroom), 'headroom_percent': headroom / threshold * 100 if threshold else None,
            'detail': detail, 'error': None, 'started_at': ended_at, 'ended_at': ended_at,
            'host': {'load1': 0.5, 'cpus': 4, 'mem_available_mib': 1024, 'conditions': {}}}


def check_report(*results):
    """The document `check --save-results` saves."""
    return json.dumps({'schema_version': 1, 'results': list(results)})


def zipped(members):
    """An artifact's archive of the named files."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


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
        for separator, compare in (('!=', lambda a, b: a != b), ('==', lambda a, b: a == b)):
            if separator in text:
                left, right = text.split(separator, 1)
                return compare(self.value(left), self.value(right))
        if text == 'always()':
            return True
        if text.startswith("'"):
            return text[1:-1]
        value = self.context
        for part in text.split('.'):
            value = value.get(part, '') if isinstance(value, dict) else ''
        return value

    def substitute(self, text):
        return re.sub(r'\$\{\{(.*?)\}\}', lambda m: str(self.value(m[1])), str(text))


class Job:
    """One run of the action's composite steps, in order, as the runner would take them."""

    def __init__(self, host, repository, event_name, sha, event=None, inputs=None, run_id=1, attempt=1,
                 ref_name='main', ref_type='branch', base_ref='', environment=None, register=True, action_path=None,
                 before_run=None):
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
        self.before_run = before_run
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
        if self.before_run:
            self.before_run(self)
        document, inputs = self._defaults()
        inputs.update(self.inputs)
        steps_context = {}
        context = {'inputs': inputs, 'github': self.github, 'steps': steps_context, 'runner': {'temp': str(self.temp)}}
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
                outcome, outputs = self._artifact_action(step, expression, output_directory / identifier)
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
            self.host.add_run(self.run_id, self.sha, self.attempt, minute=self.run_id, event=self.event_name)
        return self

    def _artifact_action(self, step, expression, output_file):
        """Run the real action's code against the doubled services, as the runner would."""
        directory, entry = artifact_action(step['uses'])
        declared = yaml.safe_load((directory / 'action.yml').read_text())['inputs']
        arguments = {name: str(spec.get('default', '')) for name, spec in declared.items()}
        arguments.update({key: expression.substitute(value) for key, value in step['with'].items()})
        output_file.touch()
        environment = {
            'PATH': os.environ['PATH'],
            'HOME': str(self.temp),
            'GITHUB_OUTPUT': str(output_file),
            'GITHUB_WORKSPACE': str(self.repository.path),
            'GITHUB_REPOSITORY': REPOSITORY,
            'GITHUB_RUN_ID': str(self.run_id),
            'GITHUB_SERVER_URL': 'https://github.com',
            'RUNNER_TEMP': str(self.temp),
            'ACTIONS_RUNTIME_TOKEN': runtime_token(self.run_id, self.attempt),
            'ACTIONS_RESULTS_URL': self.host.url + '/',
            **{f'INPUT_{name.upper()}': value for name, value in arguments.items()},
        }
        result = bounded_run(['node', str(entry)], cwd=self.repository.path, env=environment,
                             capture_output=True, text=True, check=False)
        self.stdout += result.stdout + result.stderr
        return ('success' if result.returncode == 0 else 'failure'), {}

    def _shell(self, step, expression, output_file, summary):
        environment = {
            'PATH': os.environ['PATH'],
            'HOME': str(self.temp),
            'ONEVCS_HOME': str(self.temp / 'onevcs-home'),
            'GITHUB_ACTION_PATH': str(self.action_path),
            'GITHUB_OUTPUT': str(output_file),
            'GITHUB_STEP_SUMMARY': str(summary),
            'RUNNER_TEMP': str(self.temp),
            **real_gh_environment(self.host.url, self.temp),
            'ONEBUDGETSPEC_ACTION_RELEASES': self.host.url + '/releases',
            'PIP_INDEX_URL': self.host.url + '/simple',
            **self.environment,
        }
        if 'LLVM_PROFILE_FILE' in os.environ:
            environment['LLVM_PROFILE_FILE'] = os.environ['LLVM_PROFILE_FILE']
        for key, value in step.get('env', {}).items():
            environment[key] = expression.substitute(value)
        result = bounded_run(
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
