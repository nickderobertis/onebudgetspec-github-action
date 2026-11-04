#!/usr/bin/env bash
# Set up a clean clone: the locked Node toolchain Nx runs on, the pinned Rust toolchain
# and the cargo test tools the gate runs (`just bootstrap` fetches the locked crates first,
# through `just fetch`). Idempotent and quiet.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || { echo "bootstrap: cannot enter the repository root; run scripts/bootstrap.sh from a checkout." >&2; exit 1; }

run() {
  local log
  if ! log="$("$@" 2>&1)"; then
    printf '%s\n' "$log" >&2
    echo "bootstrap: '$*' failed (above); fix it, then re-run 'just bootstrap'." >&2
    exit 1
  fi
}

for tool in cargo bun; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "bootstrap: $tool is not installed; install it (cargo: https://rustup.rs, bun: https://bun.sh), then re-run 'just bootstrap'." >&2
    exit 1
  }
done
run bun install --frozen-lockfile
# With no argument, installs the toolchain rust-toolchain.toml pins.
if command -v rustup >/dev/null 2>&1; then
  run rustup toolchain install --no-self-update
fi
for tool in cargo-llvm-cov cargo-nextest; do
  command -v "$tool" >/dev/null 2>&1 || run cargo install --locked "$tool"
done

# The release contract suite drives the installed public CLI in an isolated home.
command -v onevcs >/dev/null 2>&1 || run uv tool install onevcs-cli==0.57.4

just setup-llmlint
command -v llmlint >/dev/null 2>&1 || { echo "bootstrap: llmlint unavailable; fix setup-llmlint output and retry." >&2; exit 1; }
