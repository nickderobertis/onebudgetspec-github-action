#!/usr/bin/env bash
# Installs the llmlint judged-lint tier: `llmlint-cli` from PyPI via `uv tool`, which
# brings `oneharness` into the same tool environment, where llmlint finds it. Run by
# session-setup.sh, `just setup-llmlint` and CI. The harness llmlint drives is chosen by
# oneharness.toml, so nothing is exported for it here.
#
# Never blocks session startup: no `set -e`, and it always exits 0. Silent on success;
# each problem is logged with what to do. crates/dev-setup-e2e drives it.
set -uo pipefail

# The floor `just lint-llm-diff` (changed-line scoping, three-dot --diff-base) and `just
# lint-llm-validate` (the model-free `validate` gate) need.
readonly LLMLINT_MIN="0.3.23"

log() { printf 'setup-llmlint: %s\n' "$*" >&2; }

if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
  log "HOME is unset or not a directory, so nothing can be installed under it; set HOME, then re-run"
  # llmlint: ignore[cli_output_contract] a SessionStart hook must never fail session startup, so this refusal exits 0 by design after reporting on stderr; crates/dev-setup-e2e asserts both.
  exit 0
fi
readonly BIN_DIR="$HOME/.local/bin"

ensure_toolchain() {
  if ! command -v uv >/dev/null 2>&1; then
    log "uv is not installed, so llmlint cannot be; install uv (https://docs.astral.sh/uv/), then run 'just setup-llmlint'"
    return 0
  fi
  uv tool install --quiet --upgrade "llmlint-cli>=$LLMLINT_MIN" >&2 \
    || log "llmlint-cli install failed (above); run 'uv tool install llmlint-cli' to retry"
}

persist_session_env() {
  [ -n "${CLAUDE_ENV_FILE:-}" ] || return 0
  case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *)
      line="$(printf 'export PATH=%q' "${BIN_DIR}:${PATH}")"
      # Idempotent: a re-run in the same session appends nothing new. A last line left
      # without its newline is read too, and the new line is started on a line of its own.
      separator=""
      if [ -r "$CLAUDE_ENV_FILE" ]; then
        existing=""
        while IFS= read -r existing; do
          [ "$existing" != "$line" ] || return 0
        done < "$CLAUDE_ENV_FILE"
        if [ -n "$existing" ]; then
          [ "$existing" != "$line" ] || return 0
          separator=$'\n'
        fi
      fi
      if ! error="$( { printf '%s%s\n' "$separator" "$line" >> "$CLAUDE_ENV_FILE"; } 2>&1 )"; then
        log "could not write PATH to the session env file $CLAUDE_ENV_FILE (${error##*: }); check that it is writable, then re-run"
      fi ;;
  esac
}

persist_session_env
export PATH="${BIN_DIR}:${PATH}"
ensure_toolchain
if ! command -v llmlint >/dev/null 2>&1; then
  log "llmlint is not on PATH after the install; run 'just setup-llmlint' once uv works"
elif ! doctor="$(llmlint doctor 2>&1)"; then
  printf '%s\n' "$doctor" >&2
  log "llmlint doctor reported an issue (above); authenticate a harness oneharness.toml names"
fi
# llmlint: ignore[cli_output_contract] a SessionStart hook must never fail session startup, so this exits 0 by design; every problem above is reported on stderr with what to do, and the journeys in crates/dev-setup-e2e assert both.
exit 0
