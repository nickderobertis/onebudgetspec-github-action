#!/usr/bin/env bash
# The action's cold install: the driver and plugin archive released at the action's own
# version, its checksum verified, then the published core wheel. Nothing is compiled here.
# A failure is a diagnostic, recorded for the later steps: it never fails the job.
set -uo pipefail
action_path="$1"
state="$2"
started_at="$(python3 -c 'import time; print(time.time())')"
mkdir -p "$state/tools"
printf '%s\n' "$started_at" > "$state/started-at"

fail() {
  printf '%s\n' "$1" > "$state/install-failed"
  echo "::warning::budget report tools not installed: $1"
  exit 0
}

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) target=x86_64-unknown-linux-gnu ;;
  Linux-aarch64) target=aarch64-unknown-linux-gnu ;;
  Darwin-arm64) target=aarch64-apple-darwin ;;
  *) fail "no action archive exists for $(uname -s) $(uname -m); use a Linux or Apple Silicon runner, or the direct tool" ;;
esac
version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$action_path/Cargo.toml" | head -n 1)"
[ -n "$version" ] || fail "cannot read the action's version from $action_path/Cargo.toml"
base="${ONEBUDGETSPEC_ACTION_RELEASES:-https://github.com/nickderobertis/onebudgetspec-github-action/releases/download}"
archive="onebudgetspec-github-action-$version-$target.tar.gz"
cd "$state" || fail "cannot enter $state"
curl -fsSL --retry 2 -o "$archive" "$base/v$version/$archive" \
  || fail "cannot download $archive from release v$version"
curl -fsSL --retry 2 -o SHA256SUMS "$base/v$version/SHA256SUMS" \
  || fail "cannot download SHA256SUMS from release v$version"
grep -F "  $archive" SHA256SUMS > archive.sha256 \
  || fail "SHA256SUMS of release v$version does not list $archive"
if command -v sha256sum > /dev/null 2>&1; then
  sha256sum --status -c archive.sha256 || fail "$archive does not match its checksum"
else
  shasum -a 256 --status -c archive.sha256 || fail "$archive does not match its checksum"
fi
tar -xzf "$archive" -C tools || fail "cannot unpack $archive"
rm -f "$archive" archive.sha256 SHA256SUMS

# shellcheck source=action/dependencies.env
. "$action_path/action/dependencies.env"
reason="$(python3 "$action_path/action/install_core.py" "$CORE_CLI_VERSION" tools 2>&1)" \
  || fail "cannot install core $CORE_CLI_VERSION: $reason"
python3 -c "import time, sys; print(time.time() - float(sys.argv[1]))" "$started_at" > "$state/install-seconds"
