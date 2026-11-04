"""Drive real setup scripts, with installer binaries as external subprocess doubles."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class Provisioning(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.checkout = self.root / 'checkout'
        shutil.copytree(ROOT / 'scripts', self.checkout / 'scripts')
        self.envfile = self.root / 'session.env'
        self.log = self.root / 'calls'
        for tool in ['dirname', 'bash']:
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.env = {'HOME': str(self.home), 'PATH': str(self.bin), 'CLAUDE_ENV_FILE': str(self.envfile), 'TOOL_LOG': str(self.log)}
        for tool in ['cargo', 'bun', 'just']:
            self.tool(tool, 'exit 0')
        self.tool('uv', '''printf 'uv %s\\n' "$*" >> "$TOOL_LOG"
if [ -n "${INSTALL_FAIL:-}" ]; then echo 'installer refused' >&2; exit 1; fi
mkdir_path="$HOME/.local/bin"
/bin/mkdir -p "$mkdir_path"
case "$*" in
  *rust-just*) printf '#!/bin/bash\\nexit 0\\n' > "$mkdir_path/just"; /bin/chmod +x "$mkdir_path/just" ;;
  *llmlint-cli*) printf '#!/bin/bash\\nprintf "doctor\\\\n" >> "$TOOL_LOG"\\nif [ -n "${DOCTOR_FAIL:-}" ]; then echo "doctor refused" >&2; exit 1; fi\\n' > "$mkdir_path/llmlint"; /bin/chmod +x "$mkdir_path/llmlint" ;;
esac''')

    def tool(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def invoke(self, name='session-setup.sh', **env):
        result = subprocess.run(['/bin/bash', str(self.checkout / 'scripts' / name)], env={**self.env, **env}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        return result

    def test_install_and_repeat_preserve_session_path(self):
        (self.bin / 'just').unlink()
        self.assertEqual(self.invoke().stderr, '')
        self.assertTrue((self.home / '.local/bin/just').is_file())
        self.assertTrue((self.home / '.local/bin/llmlint').is_file())
        first = self.envfile.read_text()
        self.assertIn(str(self.home / '.local/bin'), first)
        self.assertEqual(self.invoke().stderr, '')
        self.assertEqual(self.envfile.read_text(), first)
        calls = self.log.read_text()
        self.assertEqual(calls.count('rust-just'), 1)
        self.assertEqual(calls.count('doctor'), 2)

    def test_missing_prerequisites_and_install_failure_are_actionable(self):
        for tool in ['cargo', 'bun', 'just', 'uv']:
            (self.bin / tool).unlink()
        result = self.invoke()
        for message in ['neither just nor uv', 'cargo is not on PATH', 'bun is not on PATH', 'uv is not installed', 'llmlint is not on PATH']:
            self.assertIn(message, result.stderr)
        self.tool('uv', 'echo "installer refused" >&2; exit 1')
        result = self.invoke()
        self.assertIn('rust-just install failed', result.stderr)
        self.assertIn('llmlint-cli install failed', result.stderr)

    def test_ci_and_unavailable_home_do_not_provision(self):
        self.assertEqual(self.invoke(CI='true').stderr, '')
        self.assertFalse(self.log.exists())
        self.assertFalse(self.envfile.exists())
        for name in ['session-setup.sh', 'setup-llmlint.sh']:
            self.assertIn('HOME is unset or not a directory', self.invoke(name, HOME=str(self.root / 'missing')).stderr)
        self.assertFalse(self.log.exists())

    def test_doctor_and_environment_write_failures_are_reported(self):
        self.assertIn('doctor reported an issue', self.invoke('setup-llmlint.sh', DOCTOR_FAIL='1').stderr)
        self.assertIn('could not write PATH', self.invoke(CLAUDE_ENV_FILE=str(self.root)).stderr)
        self.assertIn('could not write PATH', self.invoke('setup-llmlint.sh', CLAUDE_ENV_FILE=str(self.root)).stderr)

    def test_existing_path_and_unterminated_environment_file(self):
        self.envfile.write_text('export EXISTING=1')
        self.assertEqual(self.invoke('setup-llmlint.sh').stderr, '')
        self.assertTrue(self.envfile.read_text().startswith('export EXISTING=1\nexport PATH='))
        before = self.envfile.read_text()
        self.invoke('setup-llmlint.sh', PATH=str(self.home / '.local/bin') + os.pathsep + str(self.bin))
        self.assertEqual(self.envfile.read_text(), before)
        (self.checkout / 'scripts/setup-llmlint.sh').unlink()
        self.assertIn('missing or not executable', self.invoke().stderr)


if __name__ == '__main__':
    unittest.main()
