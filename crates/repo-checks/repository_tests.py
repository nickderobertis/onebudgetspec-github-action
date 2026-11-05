"""Offline release journeys; only the registries are replaced, at their curl boundary."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
DECLARATION = tomllib.loads((ROOT / 'release-targets.toml').read_text())


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


def release_probe_archives():
    source = (ROOT / 'scripts/release_probe.py').read_text()
    return re.search(r'ARCHIVES = \((.+?)\)', source)[1].replace('"', '').split(', ')


class Repository(unittest.TestCase):
    # Live release orchestration requires PAT/crates credentials and would publish,
    # tag or release. This scaffold task forbids those operations; inspect the actual
    # workflow/configuration here. Release-plz update and Cargo dry runs are separate
    # local verification commands, not operations this suite performs.
    def test_workflow_and_secrets(self):
        by_name = {target['name']: target for target in DECLARATION['target']}
        self.assertEqual(set(by_name), {'crate', 'github-release'})
        self.assertEqual(DECLARATION['schema_version'], 2)
        target = by_name['crate']
        manifest = tomllib.loads((ROOT / target['manifest']).read_text())
        self.assertEqual(target['id'], 'crate:' + manifest['package']['name'])
        self.assertEqual(by_name['github-release']['id'], 'github-release:nickderobertis/onebudgetspec-github-action')
        self.assertEqual(by_name['github-release']['manifest'], 'action.yml')
        config = tomllib.loads((ROOT / 'release-plz.toml').read_text())
        self.assertIs(config['workspace']['publish'], False, 'release-plz only versions and tags')
        self.assertEqual([p['name'] for p in config['package'] if p.get('publish')], [])
        versioned = [p['name'] for p in config['package'] if p.get('release', True)]
        self.assertEqual(versioned, [manifest['package']['name']])
        plz = (ROOT / '.github/workflows/release-plz.yml').read_text()
        self.assertRegex(plz, r'push:\s+branches: \[main\]')
        self.assertIn('--config release-plz.toml', plz)
        self.assertIn('GITHUB_TOKEN: ${{ secrets.RELEASE_PLZ_TOKEN }}', plz)
        self.assertNotIn('CARGO_REGISTRY_TOKEN', plz)
        self.assertNotIn('secrets.GITHUB_TOKEN', plz)
        self.assertIn('gh pr merge "$pr" --repo "$GITHUB_REPOSITORY" --auto --squash', plz)
        agents = (ROOT / 'AGENTS.md').read_text().lower()
        self.assertNotIn('human checkpoint', agents)
        self.assertNotIn('never arm', agents)
        self.assertIn('auto-merge', agents)
        release = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertRegex(release, r'release:\s+types: \[published\]')
        self.assertLess(release.index('  payload:'), release.index('  crate:'))
        self.assertRegex(release, r'crate:\s+needs: \[payload\]')
        self.assertRegex(release, r'payload:\s+needs: \[archives\]')
        self.assertIn('scripts/publish-crate.sh "$TAG"', release)
        self.assertIn('scripts/publish-payload.sh "$TAG" dist', release)
        for runner_target in release_probe_archives():
            self.assertIn(f'target: {runner_target}', release)
        self.assertNotIn('cargo publish', release)
        secrets = json.loads((ROOT / 'gh-secrets.json').read_text())
        self.assertEqual(secrets['source'], {'type': 'bitwarden', 'default_field': 'notes'})
        self.assertEqual(secrets['destinations'], [{'type': 'github', 'repository': 'nickderobertis/onebudgetspec-github-action'}])
        expected = {name: ('GH_TOKEN' if name == 'RELEASE_PLZ_TOKEN' else name) for name in ['RELEASE_PLZ_TOKEN', 'CARGO_REGISTRY_TOKEN', 'OPENAI_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN']}
        self.assertEqual(secrets['secrets'], [{'name': name, 'item': item} for name, item in expected.items()])
        used = set()
        for file in (ROOT / '.github/workflows').glob('*.yml'):
            used.update(re.findall(r'secrets\.([A-Z_]+)', file.read_text()))
        self.assertEqual(used - {'GITHUB_TOKEN'}, set(expected))
        self.assertEqual((ROOT / 'README.md').read_text(), (ROOT / target['manifest']).parent.joinpath('README.md').read_text())

    def test_dependency_pins_agree(self):
        pins = dict(line.split('=', 1) for line in (ROOT / 'action/dependencies.env').read_text().splitlines() if line and not line.startswith('#'))
        self.assertEqual(json.loads((ROOT / 'package.json').read_text())['devDependencies']['@onebudgetspec/cli'], pins['CORE_CLI_VERSION'])
        workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text())
        self.assertEqual(workspace['workspace']['dependencies']['onebudgetspec-core'], '=' + pins['CORE_CLI_VERSION'])

    def test_configuration_restatements(self):
        workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text())
        self.assertEqual(tomllib.loads((ROOT / 'clippy.toml').read_text())['msrv'], workspace['workspace']['package']['rust-version'])
        measured = set()
        for file in (ROOT / 'crates').glob('*/project.json'):
            project = json.loads(file.read_text())
            if 'rust-coverage.sh ' in project['targets'].get('test', {}).get('options', {}).get('command', ''):
                measured.add(project['name'])
                self.assertIn('{workspaceRoot}/target', project['targets']['test']['outputs'])
        aggregate = json.loads((ROOT / 'project.json').read_text())['targets']['coverage']['dependsOn']
        self.assertEqual(measured, {name for dependency in aggregate for name in dependency['projects']})

    def test_probe_and_real_onevcs(self):
        binary = shutil.which('onevcs')
        self.assertIsNotNone(binary, 'bootstrap must install onevcs')
        binary = str(Path(binary).resolve())
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            checkout = temp / 'checkout'
            shutil.copytree(ROOT / 'scripts', checkout / 'scripts')
            shutil.copy2(ROOT / 'release-targets.toml', checkout)
            home = temp / 'home'
            home.mkdir()
            env = {**os.environ, 'HOME': str(home), 'ONEVCS_HOME': str(home / 'onevcs')}
            for args in [['git', 'init', '-b', 'main', str(checkout)], ['git', '-C', str(checkout), 'add', '.'], ['git', '-C', str(checkout), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'feat: scaffold']]:
                result = run(args, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = run([binary, 'release', 'declaration', str(checkout), '--json'], env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            for target in DECLARATION['target']:
                self.assertIn(target['id'], result.stdout)
            result = run([binary, 'register', str(checkout), '--origin', 'https://github.com/example/scaffold-test'], env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            stub = temp / 'bin'
            stub.mkdir()
            curl = stub / 'curl'
            response_file = temp / 'response'
            exit_file = temp / 'exit'
            curl.write_text(f'#!/usr/bin/env python3\nfrom pathlib import Path\nimport sys\nsys.stdout.write(Path({str(response_file)!r}).read_text())\nsys.exit(int(Path({str(exit_file)!r}).read_text()))\n')
            curl.chmod(0o755)
            env['PATH'] = str(stub) + os.pathsep + os.environ['PATH']
            identifier = DECLARATION['target'][0]['id']
            name = identifier.split(':')[1]
            for response, code, answer, reason in [
                (json.dumps({'crate': {'id': name, 'max_stable_version': '1.2.3'}}) + '\n200', '0', '1.2.3', None),
                ('{}\n404', '0', '', None),
                (json.dumps({'crate': {'id': name, 'max_stable_version': None}}) + '\n200', '0', '', None),
                ('{invalid}\n200', '0', '', 'release-probe:'),
                ('', '28', '', 'request failed'),
                ('{}\n503', '0', '', 'HTTP 503'),
                (json.dumps({'crate': {'id': 'another', 'max_stable_version': '1.2.3'}}) + '\n200', '0', '', 'different crate'),
                ('{}\n200', '0', '', 'release-probe:'),
                ('[]\n200', '0', '', 'release-probe:'),
                (json.dumps({'crate': {'id': name}}) + '\n200', '0', '', 'release-probe:'),
                (json.dumps({'crate': {'id': name, 'max_stable_version': 123}}) + '\n200', '0', '', 'invalid stable version'),
                (json.dumps({'crate': {'id': name, 'max_stable_version': 'bad'}}) + '\n200', '0', '', 'invalid stable version'),
            ]:
                response_file.write_text(response)
                exit_file.write_text(code)
                probe = run([str(checkout / DECLARATION['probe']), identifier], env=env, cwd=temp)
                if reason:
                    self.assertNotEqual(probe.returncode, 0)
                    self.assertIn(reason, probe.stderr)
                else:
                    self.assertEqual(probe.returncode, 0, probe.stderr)
                    self.assertEqual(probe.stdout.strip(), answer)
                latest = run([binary, 'release', 'latest', str(checkout), '--target', 'crate', '--json'], env=env)
                if reason:
                    self.assertIn('not-answered', latest.stdout + latest.stderr)
                    self.assertIn(reason, latest.stdout + latest.stderr)
                else:
                    self.assertEqual(latest.returncode, 0, latest.stderr)
                    self.assertIn(answer if answer else 'no-release', latest.stdout)
            for arguments in [[], [identifier, '1.0.0', 'extra']]:
                output = run([str(checkout / DECLARATION['probe']), *arguments], env=env)
                self.assertNotEqual(output.returncode, 0)
                self.assertIn('expected one', output.stderr)
            output = run([str(checkout / DECLARATION['probe']), identifier, '1.0.0'], env=env)
            self.assertIn('only asked of a github-release', output.stderr)
            output = run([str(checkout / DECLARATION['probe']), DECLARATION['target'][1]['id'], 'bad'], env=env)
            self.assertIn('not a stable version', output.stderr)
            declaration_path = checkout / 'release-targets.toml'
            original = declaration_path.read_text()
            declaration_path.write_text(original.replace(identifier, 'npm:unsupported'))
            output = run([str(checkout / DECLARATION['probe']), 'npm:unsupported'], env=env)
            self.assertNotEqual(output.returncode, 0)
            self.assertIn('unsupported registry', output.stderr)
            declaration_path.write_text(original)
            unknown = run([str(checkout / DECLARATION['probe']), 'crate:unknown'], env=env)
            self.assertNotEqual(unknown.returncode, 0)
            self.assertIn('unknown identifier', unknown.stderr)
            unknown = run([binary, 'release', 'latest', str(checkout), '--target', 'unknown', '--json'], env=env)
            self.assertNotEqual(unknown.returncode, 0)
            self.assertIn('unknown', unknown.stderr)


class Publishing(unittest.TestCase):
    """The real scripts and probe; only curl's registries and cargo's upload are stubbed."""

    VERSION = '7.8.9'
    REPOSITORY = 'nickderobertis/onebudgetspec-github-action'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.checkout = self.temp / 'checkout'
        shutil.copytree(ROOT / 'scripts', self.checkout / 'scripts')
        shutil.copy2(ROOT / 'release-targets.toml', self.checkout)
        shutil.copy2(ROOT / 'action.yml', self.checkout)
        self.bin = self.temp / 'bin'
        self.bin.mkdir()
        self.published = self.temp / 'published'
        self.responses = self.temp / 'responses.json'
        (self.bin / 'curl').write_text(f'''#!/usr/bin/env python3
import json, sys
answers = json.load(open({str(self.responses)!r}))
url = sys.argv[-1]
for key, (body, status) in answers.items():
    if url.endswith(key):
        sys.stdout.write(body + "\\n" + str(status))
        sys.exit(0)
sys.stdout.write("missing\\n404")
''')
        (self.bin / 'cargo').write_text(f'''#!/usr/bin/env bash
case "$1" in
  pkgid) echo "path+file:///checkout#{self.VERSION}" ;;
  publish) echo "$*" >> {self.published} ;;
  *) exit 64 ;;
esac
''')
        (self.bin / 'gh').write_text(f'''#!/usr/bin/env bash
printf '%s\\n' "$*" >> {self.temp / 'gh-calls'}
''')
        for tool in ('curl', 'cargo', 'gh'):
            (self.bin / tool).chmod(0o755)
        self.environment = {**os.environ, 'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}', 'GITHUB_REPOSITORY': self.REPOSITORY}

    def payload(self, **overrides):
        assets = {'action.yml': 'name: action'}
        for target in release_probe_archives():
            assets[f'onebudgetspec-github-action-{self.VERSION}-{target}.tar.gz'] = 'archive ' + target
        sums = ''.join(f'{hashlib.sha256(body.encode()).hexdigest()}  {name}\n' for name, body in assets.items())
        assets['SHA256SUMS'] = sums
        assets.update(overrides)
        return {name: body for name, body in assets.items() if body is not None}

    def serve(self, assets, tag=None, crate=None, draft=False):
        tag = tag or f'v{self.VERSION}'
        listing = {
            'tag_name': tag, 'draft': draft, 'prerelease': False,
            'assets': [{'name': name, 'browser_download_url': f'https://assets.example/{name}'} for name in assets]}
        answers = {f'/releases/tags/v{self.VERSION}': (json.dumps(listing), 200)}
        answers.update({f'/{name}': (body, 200) for name, body in assets.items()})
        name = 'onebudgetspec-github-action'
        answers[f'/crates/{name}'] = (json.dumps({'crate': {'id': name, 'max_stable_version': crate}}), 200)
        self.responses.write_text(json.dumps(answers))

    def publish(self):
        return run(['bash', str(self.checkout / 'scripts/publish-crate.sh'), f'v{self.VERSION}'], env=self.environment, cwd=self.temp)

    def test_crate_is_published_only_after_the_exact_payload_is_verified(self):
        self.serve(self.payload())
        answer = self.publish()
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(self.published.read_text().strip(), 'publish --locked -p onebudgetspec-github-action')

    def test_absent_or_mismatched_payload_stops_the_crate(self):
        archive = f'onebudgetspec-github-action-{self.VERSION}-aarch64-apple-darwin.tar.gz'
        for name, assets, kwargs, reason in [
            ('no release', None, {}, 'no usable action payload'),
            ('missing archive', self.payload(**{archive: None}), {}, 'no usable action payload'),
            ('missing action', self.payload(**{'action.yml': None}), {}, 'no usable action payload'),
            ('missing checksums', self.payload(SHA256SUMS=None), {}, 'no usable action payload'),
            ('tampered archive', self.payload(**{archive: 'changed'}), {}, 'no usable action payload'),
            ('draft', self.payload(), {'draft': True}, 'no usable action payload'),
            ('another version', self.payload(), {'tag': 'v7.8.8'}, 'could not verify'),
        ]:
            with self.subTest(name):
                if assets is None:
                    self.responses.write_text('{}')
                else:
                    self.serve(assets, **kwargs)
                answer = self.publish()
                self.assertNotEqual(answer.returncode, 0)
                self.assertIn(reason, answer.stderr)
                self.assertFalse(self.published.exists(), 'nothing reaches the registry')

    def test_a_rerun_skips_a_version_already_served(self):
        self.serve(self.payload(), crate=self.VERSION)
        answer = self.publish()
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertFalse(self.published.exists())

    def test_probe_answers_the_newest_usable_release(self):
        assets = self.payload()
        listing = {'tag_name': f'v{self.VERSION}', 'draft': False, 'prerelease': False,
                   'assets': [{'name': name, 'browser_download_url': f'https://assets.example/{name}'} for name in assets]}
        answers = {'/releases/latest': (json.dumps(listing), 200), **{f'/{n}': (b, 200) for n, b in assets.items()}}
        self.responses.write_text(json.dumps(answers))
        identifier = f'github-release:{self.REPOSITORY}'
        probe = run([str(self.checkout / 'scripts/release-probe.sh'), identifier], env=self.environment)
        self.assertEqual((probe.returncode, probe.stdout.strip()), (0, self.VERSION), probe.stderr)
        answers['/releases/latest'] = ('{}', 500)
        self.responses.write_text(json.dumps(answers))
        probe = run([str(self.checkout / 'scripts/release-probe.sh'), identifier], env=self.environment)
        self.assertNotEqual(probe.returncode, 0)
        self.assertIn('HTTP 500', probe.stderr)

    def test_payload_upload_names_every_asset_with_checksums(self):
        dist = self.temp / 'dist'
        dist.mkdir()
        (dist / f'onebudgetspec-github-action-{self.VERSION}-x.tar.gz').write_text('archive')
        answer = run(['bash', str(self.checkout / 'scripts/publish-payload.sh'), f'v{self.VERSION}', str(dist)], env=self.environment, cwd=self.temp)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        calls = (self.temp / 'gh-calls').read_text()
        self.assertIn(f'release upload v{self.VERSION}', calls)
        for name in ('action.yml', 'SHA256SUMS', '.tar.gz'):
            self.assertIn(name, calls)
        sums = (dist / 'SHA256SUMS').read_text()
        self.assertIn('  action.yml', sums)
        self.assertIn(f'  onebudgetspec-github-action-{self.VERSION}-x.tar.gz', sums)
        refused = run(['bash', str(self.checkout / 'scripts/publish-payload.sh'), 'nonsense', str(dist)], env=self.environment, cwd=self.temp)
        self.assertEqual(refused.returncode, 2)


if __name__ == '__main__':
    unittest.main()
