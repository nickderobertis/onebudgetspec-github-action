"""Offline release journeys; only crates.io is replaced at its curl boundary."""
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


class Repository(unittest.TestCase):
    def test_workflow_and_secrets(self):
        target, = DECLARATION['target']
        self.assertEqual(DECLARATION['schema_version'], 2)
        manifest = tomllib.loads((ROOT / target['manifest']).read_text())
        self.assertEqual(target['id'], 'crate:' + manifest['package']['name'])
        self.assertEqual(target['name'], 'crate')
        config = tomllib.loads((ROOT / 'release-plz.toml').read_text())
        published = [p['name'] for p in config['package'] if p.get('publish')]
        self.assertEqual(published, [manifest['package']['name']])
        workflow = (ROOT / '.github/workflows/release-plz.yml').read_text()
        self.assertRegex(workflow, r'push:\s+branches: \[main\]')
        self.assertIn('--config release-plz.toml', workflow)
        self.assertIn('GITHUB_TOKEN: ${{ secrets.RELEASE_PLZ_TOKEN }}', workflow)
        self.assertIn('CARGO_REGISTRY_TOKEN: ${{ secrets.CARGO_REGISTRY_TOKEN }}', workflow)
        self.assertNotIn('secrets.GITHUB_TOKEN', workflow)
        self.assertNotIn('gh pr merge', workflow)
        self.assertIn('the release pr is a human checkpoint', (ROOT / 'AGENTS.md').read_text().lower())
        secrets = json.loads((ROOT / 'gh-secrets.json').read_text())
        self.assertEqual(secrets['source'], {'type': 'bitwarden', 'default_field': 'notes'})
        self.assertEqual(secrets['destinations'], [{'type': 'github', 'repository': 'nickderobertis/onebudgetspec-github-action'}])
        expected = {name: ('GH_TOKEN' if name == 'RELEASE_PLZ_TOKEN' else name) for name in ['RELEASE_PLZ_TOKEN', 'CARGO_REGISTRY_TOKEN', 'OPENAI_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN']}
        self.assertEqual(secrets['secrets'], [{'name': name, 'item': item} for name, item in expected.items()])
        used = set()
        for file in (ROOT / '.github/workflows').glob('*.yml'):
            used.update(re.findall(r'secrets\.([A-Z_]+)', file.read_text()))
        self.assertEqual(used - {'GITHUB_TOKEN'}, set(expected))
        self.assertFalse((ROOT / 'action.yml').exists())

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
            self.assertIn(DECLARATION['target'][0]['id'], result.stdout)
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
            unknown = run([str(checkout / DECLARATION['probe']), 'crate:unknown'], env=env)
            self.assertNotEqual(unknown.returncode, 0)
            self.assertIn('unknown identifier', unknown.stderr)
            unknown = run([binary, 'release', 'latest', str(checkout), '--target', 'unknown', '--json'], env=env)
            self.assertNotEqual(unknown.returncode, 0)
            self.assertIn('unknown', unknown.stderr)


if __name__ == '__main__':
    unittest.main()
