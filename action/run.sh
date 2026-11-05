#!/usr/bin/env bash
# The action's steps, each mapping the workflow context onto the driver's explicit flags.
# Only `conclude` may fail the job, and only with an ordinary budget verdict.
set -uo pipefail
step="$1"
state="$RUNNER_TEMP/onebudgetspec-action"
out="$RUNNER_TEMP/onebudgetspec-report"
driver="$state/tools/onebudgetspec-github-action"
exit_file="$state/exit"

summarize() {
  [ -z "${GITHUB_STEP_SUMMARY:-}" ] || cat "$1" >> "$GITHUB_STEP_SUMMARY"
}

report() {
  mkdir -p "$out"
  printf 'out=%s\n' "$out" >> "$GITHUB_OUTPUT"
  if [ -f "$state/install-failed" ]; then
    printf '## Budget report\n\nNo budget ran: %s\n' "$(cat "$state/install-failed")" > "$out/report.md"
    return 0
  fi
  local mode read_only_reason="" change_url="" base_ref="${BASE_REF:-}"
  case "$EVENT_NAME" in
    pull_request)
      if [ "$HEAD_REPOSITORY" = "$REPOSITORY" ]; then
        mode=pull-request
        change_url="$SERVER_URL/$REPOSITORY/pull/$PR_NUMBER"
      else
        mode=read-only
        read_only_reason="this change comes from the fork $HEAD_REPOSITORY, whose token cannot post"
      fi
      ;;
    push)
      mode=base
      if [ "$REF_TYPE" != branch ]; then
        mode=read-only
        read_only_reason="a $REF_TYPE push belongs to no base branch"
        base_ref=""
      fi
      ;;
    pull_request_target | workflow_run)
      mode=privileged
      ;;
    *)
      mode=read-only
      read_only_reason="$EVENT_NAME is neither a change request nor a base push"
      base_ref=""
      ;;
  esac
  local code=0
  "$driver" report \
    --budget-files "$BUDGET_FILES" \
    --labels "$LABELS" \
    --artifact-name "$ARTIFACT_NAME" \
    --enforcement-exclude-labels "$ENFORCEMENT_EXCLUDE_LABELS" \
    --repo "$REPOSITORY" --head-sha "$HEAD_SHA" --base-ref "$base_ref" --base-sha "$BASE_SHA" \
    --run-id "$RUN_ID" --run-url "$SERVER_URL/$REPOSITORY/actions/runs/$RUN_ID" --attempt "$RUN_ATTEMPT" \
    --mode "$mode" --read-only-reason "$read_only_reason" --change-url "$change_url" \
    --out "$out" --phased \
    --started-at "$(cat "$state/started-at")" \
    --install-seconds "$(cat "$state/install-seconds")" \
    || code=$?
  if [ "$code" -ne 0 ]; then
    printf '%s\n' "$code" > "$exit_file"
    [ -f "$out/report.md" ] || printf '## Budget report\n\nThe report stopped with exit %s; see this step'"'"'s log.\n' "$code" > "$out/report.md"
  fi
}

finish() {
  # A report that never produced a continuation has nothing to finish, and its exit stands.
  [ -f "$out/.state/continuation.json" ] || return 0
  local code=0
  "$driver" finish --out "$out" --main-upload "$MAIN_UPLOAD" >> "$GITHUB_OUTPUT" || code=$?
  printf '%s\n' "$code" > "$exit_file"
}

conclude() {
  [ ! -f "$out/report.md" ] || summarize "$out/report.md"
  if [ "${FINAL_UPLOAD:-skipped}" = failure ]; then
    echo "::warning::the final result upload failed: this run's own-command verdicts stay in this job's log only."
  fi
  [ -f "$exit_file" ] || exit 0
  exit "$(cat "$exit_file")"
}

"$step"
