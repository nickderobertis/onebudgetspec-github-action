#!/usr/bin/env bash
# Build the action archive for one target: the driver and the published plugin, compiled at
# release time so a reporting runner never compiles anything. Writes dist/<archive>.
# Usage: scripts/build-action-archive.sh VERSION TARGET
#   DRIVER_BINARY names an already built driver; PLUGIN_ROOT is cargo install's reusable root.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
version="${1:?usage: build-action-archive.sh VERSION TARGET}"
target="${2:?usage: build-action-archive.sh VERSION TARGET}"
# shellcheck source=action/dependencies.env
. action/dependencies.env
plugin_root="${PLUGIN_ROOT:-target/action-tools/plugin-$PLUGIN_VERSION}"
plugin="$plugin_root/bin/onebudgetspec-plugin-onevcs"
if [ ! -x "$plugin" ]; then
  cargo install --quiet --locked --root "$plugin_root" --version "$PLUGIN_VERSION" onebudgetspec-plugin-onevcs \
    || { echo "build-action-archive: cannot install plugin $PLUGIN_VERSION (above); fix it, then re-run." >&2; exit 1; }
fi
driver="${DRIVER_BINARY:-}"
if [ -z "$driver" ]; then
  cargo build --quiet --locked --release -p onebudgetspec-github-action \
    || { echo "build-action-archive: cannot build the driver (above); fix it, then re-run." >&2; exit 1; }
  driver=target/release/onebudgetspec-github-action
fi
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
cp "$driver" "$plugin" "$staging/"
mkdir -p "${DIST_DIRECTORY:-dist}"
tar -czf "${DIST_DIRECTORY:-dist}/onebudgetspec-github-action-$version-$target.tar.gz" -C "$staging" .
