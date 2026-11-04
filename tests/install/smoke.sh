#!/usr/bin/env bash
# Install exactly the archive Cargo prepares, then run the same user journeys.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
crate=onebudgetspec-github-action
version=$(cargo metadata --no-deps --format-version 1 --locked | python3 -c 'import json,sys; print(next(p["version"] for p in json.load(sys.stdin)["packages"] if p["name"] == "onebudgetspec-github-action"))')
cargo package --locked -p "$crate"
install_root=$(mktemp -d)
trap 'rm -rf "$install_root"' EXIT
cargo install --locked --root "$install_root" --path "target/package/$crate-$version"
SCAFFOLD_BINARY="$install_root/bin/$crate" cargo test --locked -p "$crate-e2e"
