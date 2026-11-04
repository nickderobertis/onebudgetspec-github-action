#!/usr/bin/env bash
# The Rust line-coverage floor, in three steps over cargo-llvm-cov's one profile directory:
#
#   rust-coverage.sh --clear     empty it, once, before the crates run
#   rust-coverage.sh <crate>     that crate's tests, instrumented, profiles kept
#   rust-coverage.sh --report    one report over every crate's run, held to 95% lines
#
# The crates holding or spawning production code (the deliverable and its e2e journeys)
# run their Nx `test` target through this script, after the clear (workspace:coverage-clear);
# `just test` reports once over every run, so the binary the journeys spawn counts. The
# test-only crates that exercise scripts measure no Rust and run uninstrumented. Quiet on
# success; on failure the reason and what to do.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "rust-coverage: takes exactly one argument, and was given $#; usage: tools/coverage/rust-coverage.sh --clear | <package> | --report" >&2
  exit 64
fi
readonly REQUEST="$1"
readonly MIN_LINES=95
# One marker per crate run since the last clear, so the report knows whether any Rust
# suite ran without assuming where cargo-llvm-cov keeps its profiles.
readonly RUNS="target/coverage-runs"
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || { echo "rust-coverage: cannot enter the repository root; run tools/coverage/rust-coverage.sh from a checkout." >&2; exit 1; }

for tool in llvm-cov nextest; do
  if ! probe="$(cargo "$tool" --version 2>&1)"; then
    printf '%s\n' "$probe" >&2
    echo "rust-coverage: 'cargo $tool' did not run (above); install it with 'cargo install --locked cargo-$tool', then re-run." >&2
    exit 1
  fi
done

quietly() {
  local what="$1" log
  shift
  if ! log="$("$@" 2>&1)"; then
    printf '%s\n' "$log" >&2
    echo "rust-coverage: $what" >&2
    exit 1
  fi
}

case "$REQUEST" in
  --clear)
    quietly "could not clear cargo-llvm-cov's profiles; fix the error above, then re-run." \
      cargo llvm-cov clean --workspace
    rm -rf "$RUNS" || { echo "rust-coverage: could not remove $RUNS (above); delete it by hand, then re-run." >&2; exit 1; }
    ;;
  --report)
    # Nothing to hold to the floor when no measured suite ran since the clear.
    [ -d "$RUNS" ] || exit 0
    quietly "below ${MIN_LINES}% lines over every crate's run (above); cover the listed lines with a test that drives the real behaviour." \
      cargo llvm-cov report --summary-only --show-missing-lines --fail-under-lines "$MIN_LINES"
    ;;
  -*)
    echo "rust-coverage: unknown option '$REQUEST'; pass a workspace member's package name, --clear or --report." >&2
    exit 64
    ;;
  *)
    # A package name, as cargo spells one; cargo itself refuses a name that is not a
    # workspace member, so the member list is stated nowhere here.
    case "$REQUEST" in
      "" | *[!A-Za-z0-9_-]*)
        echo "rust-coverage: '$REQUEST' is not a package name; pass a workspace member's package name, --clear or --report." >&2
        exit 64
        ;;
    esac
    quietly "$REQUEST's tests failed under instrumentation (above); fix them, then re-run." \
      cargo llvm-cov --no-report nextest --locked --no-tests=pass --package "$REQUEST"
    { mkdir -p "$RUNS" && touch "$RUNS/$REQUEST"; } \
      || { echo "rust-coverage: could not record $REQUEST's run under $RUNS (above); check target/ is writable, then re-run." >&2; exit 1; }
    ;;
esac
