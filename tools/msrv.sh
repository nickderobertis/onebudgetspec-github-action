#!/usr/bin/env bash
# Build every target under the minimum supported Rust version, read from the one place
# it is declared: `rust-version` in Cargo.toml's [workspace.package] (clippy reads the
# same key). Installs that toolchain with rustup when it is missing. Quiet on success.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || { echo "msrv: cannot enter the repository root; run tools/msrv.sh from a checkout." >&2; exit 1; }

# Exactly one `rust-version` key inside [workspace.package], whose value is "X.Y[.Z]".
declared="$(sed -n '/^\[workspace\.package\]$/,/^\[/{/^rust-version[[:space:]]*=/p;}' Cargo.toml)" || {
  echo "msrv: cannot read Cargo.toml (above); run this from a checkout of the repository." >&2
  exit 1
}
if [ -z "$declared" ] || [ "$(printf '%s\n' "$declared" | wc -l)" -ne 1 ]; then
  echo "msrv: Cargo.toml's [workspace.package] must declare exactly one rust-version (found: ${declared:-none}); fix it, then re-run." >&2
  exit 1
fi
msrv="$(printf '%s\n' "$declared" | sed -n 's/^rust-version = "\([0-9][0-9]*\.[0-9][0-9]*\(\.[0-9][0-9]*\)\{0,1\}\)"$/\1/p')"
if [ -z "$msrv" ]; then
  echo "msrv: $declared is not rust-version = \"X.Y\" or \"X.Y.Z\"; fix it, then re-run." >&2
  exit 1
fi
command -v rustup >/dev/null 2>&1 || {
  echo "msrv: rustup is not installed, so Rust $msrv cannot be selected; install it from https://rustup.rs, then re-run." >&2
  exit 1
}
run() {
  local log
  if ! log="$("$@" 2>&1)"; then
    printf '%s\n' "$log" >&2
    echo "msrv: '$*' failed (above) under Rust $msrv; fix it, or raise rust-version deliberately." >&2
    exit 1
  fi
}
run rustup toolchain install "$msrv" --profile minimal --no-self-update
run cargo "+$msrv" check --locked --all-targets --all-features
