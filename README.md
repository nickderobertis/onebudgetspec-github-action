# onebudgetspec-github-action

A GitHub Action, and the same report operation as a direct tool. One job settles every budget, from what
this run's test jobs saved or by running its existing command, compares each with the base branch's recent
results, and keeps one standing report on the pull request. Only an ordinary budget failure fails it.

## The action

Add one job to an existing workflow, after its other jobs; [examples/budget-report.yml](examples/budget-report.yml)
is the whole shape.

```yaml
budgets:
  needs: [build, test]
  if: always()
  runs-on: ubuntu-latest
  permissions: { contents: read, actions: read, pull-requests: write }
  concurrency:
    group: budget-report-${{ github.event.pull_request.number || github.ref }}
    cancel-in-progress: false
  steps:
    - uses: actions/checkout@v5
      with: { fetch-depth: 0 }
    - uses: nickderobertis/onebudgetspec-github-action@v0.2.0
      with:
        token: ${{ github.token }}
```

Run it on `pull_request` with no path filter, including drafts, and on `push` to the base branch. It builds
and tests nothing. The `concurrency` group serializes comment writers, because the host has no atomic
comment create. The full history lets local git prove each saved commit's ancestry.

| Input | Default | Meaning |
| --- | --- | --- |
| `token` | required | Reads run artifacts and posts the comment. |
| `budget-files` | `**/budgets.yaml` | Globs, separated by commas or newlines. Directories named `fixture`, `fixtures`, `__fixtures__`, `testdata` or `conformance` are never production budgets. |
| `labels` | `ci-report` | Declare a budget measurable in this job. A runnable budget without one still runs; a labelled one that cannot run gets a diagnostic. The job's telemetry carries these labels. |
| `artifact-name` | `budget-results` | The artifact holding the results and the rolling history. |
| `enforcement-exclude-labels` | empty | Budgets carrying one of these are report-only: they run once and keep their verdict and flags, and never fail the job. One whose measurement fails has no verdict: its row is unavailable, with the failure's message as the reason. |
| `saved-results-artifacts` | empty | An artifact name pattern in this run, such as `budget-check-results-*`. On a pull request every match is downloaded and its `*.json` files, each a `CheckReport` from `onebudgetspec check --save-results`, settle the rows they hold. Empty leaves today's behaviour. |
| `omit-labels` | empty | Budgets carrying one of these are neither run nor shown, and one line counts them. Separated like `enforcement-exclude-labels`. `budget-run.json`'s catalog keeps them. |

### What runs

On a pull request each budget settles in this order, and no value is ever carried forward:

1. A budget carrying an `omit-labels` label runs no command and has no row. The report carries one line, for example
   `14 budgets are left out because a pull request cannot measure them (label not-measured-on-pr).` A saved result for
   it is ignored.
2. A command reading this tool's own telemetry (`onebudgetspec-github-action telemetry ... --input PATH`) runs last, as
   below, whatever a test job saved under its id.
3. A budget with a saved result, matched by id, takes that ordinary result with its verdict unchanged, and its command
   does not run. If several files report one id, the latest `ended_at` wins. A saved id the catalog does not hold is one
   diagnostic and is otherwise ignored, and so is a result that contradicts its catalog entry.
4. A budget whose command can run here runs exactly once through ordinary `onebudgetspec check --id`, with its original
   command, labels and threshold. Only its own argv can show it cannot: its program is missing, or a relative path it
   reads is absent, as when a failed predecessor never saved that telemetry. A missing label is never a reason.
5. Anything else is unavailable and keeps its base mean and SD. With `saved-results-artifacts` set the reason is exactly
   `no saved result was found on this run`, whether or not any artifact arrived: the job cannot tell a budget this change
   did not reach from a result that was lost, and a job that died is already a red check. With the input empty, the
   catalog's reason stands. The absence alone never fails the job.

A saved result whose `ended_at` is older than the committer date of the pull request's head commit, read from local git,
was replayed by a build cache. It keeps its number and its row notes `replayed from cache, measured <date>`. `ended_at`
is the signal because a restored file's modification time is the restore time, and the head commit is the measure
because the checked-out merge commit is always newer.

An argv cannot show everything a command needs: `bash scripts/figures.sh` may need a tool only a release job has. Such a
command runs and fails. If its budget is report-only, its row is unavailable with the failure's message as the reason;
if it is enforced, the row is the ordinary `error` and fails the job.

A base push settles the same way, except that it reads no saved results. Each row it would leave unavailable, and each
row whose command would run, takes the merged pull request's figure, so a pull request's value and its baseline are one
measurement: the commit's pull request through `commits/SHA/pulls`, that head's latest `pull_request` run of this
workflow, and the newest `<artifact-name>` artifact of that run, reading only `budget-run.json`'s `results`. These reads
cost 4 GitHub API requests, 3 when the run has no such artifact, and 1 when the commit has no pull request. Each figure
keeps its number and verdict and is saved without its `detail`, which the merged run's own artifact keeps. The own
telemetry budget stays this job's. A commit with no pull request, or a missing or expired artifact, leaves those rows as
they would have been, with a diagnostic.

The measured interval (`report-command-seconds`) runs from cold install through the checks, the merged-run and history
reads and the main result upload, so it ends before the comment post. Only then is the telemetry written to `PATH`, the
command run, the one comment posted with its figure and verdict, and the completed results saved. If that command cannot
judge, the comment still posts, with the row unavailable and its true reason. A budget reading
`report-github-api-requests-per-run` this way sees the count before the post; the saved `report-job.json` then adds the
post's requests. The main results go to the immutable artifact `<artifact-name>-main`, and the completed ones to
`<artifact-name>`, the name the base history reads. Nothing is overwritten, so a failed final upload leaves the main
results whole and only loses that commit from the history. A repository with no such command uploads once, to
`<artifact-name>`, and still posts once.

### What fails the job

An enforced budget over its threshold, an enforced budget's measurement error, or an invalid budgets file: the ordinary
exits 1, 3 and 2.
A missing baseline, a failed comment, an install failure, a refused credential and a failed upload each give a reason
in the report and change nothing. A budget command refused by a GitHub rate limit or credential is an unavailable row,
not an error.

### Forks and privileged events

A fork's `pull_request` run is read-only: no comment, and no GitHub credential reaches a budget command. The report goes
to the job summary and artifact. `pull_request_target` and `workflow_run` hold a privileged token, so no budget command
runs at all, whatever the workflow says.

### What each run saves

The artifact holds `budget-run.json`, `comparison-input.json`, `comparison-report.json`, `report.md` and `report-job.json`,
whose request and timing breakdown covers the job. A base push that measured at least one budget adds
`rolling-history.json`; the job's own telemetry budget counts, though it joins only the final upload. Artifacts expire under the
repository's retention setting, and an expired baseline reads as unavailable with that reason.

### Base history

Each base push reads one previous rolling file through `onebudgetspec-plugin-onevcs history`, appends its own completed
result, deduplicates by commit, trims to 20 and uploads one new immutable file. A failed upload leaves the earlier one
as the next run's source; the lost commit is disclosed. Concurrent pushes upload separately and files are never merged,
so an unrelated commit cannot enter. A push that measured nothing, or whose ordinary check could not run, saves no rolling
file and says so, and the next push reads the one before it. Below five samples a row has no baseline, and from 5 to 19 it is
provisional.

### Requests

`report-github-api-requests-per-run` counts GitHub API requests, REST and GraphQL with retries and pages. Every `gh` command
of a job, any verb and budget commands included, passes through a proxy that reads `gh`'s own one-line record of each HTTP
exchange, and the plugin's own `QueryCost` is shown beside it. That is all a live job can see, so its saved figure covers
requests made through `gh`, and its telemetry says other clients are not seen. If `gh api` gives no such record, whether it
succeeded or failed, the figure is unavailable with that reason, never estimated. A successful verb other than `api` that
gives none, such as `gh repo view`, cannot be told apart from a local command such as `gh auth token`, so it counts as zero,
and the telemetry says that too. The budget's verdict comes from `report-job:test`, measured at the external
boundary, which includes every client.
It is not every HTTP operation. Artifact-service, blob, object-storage, release and index transfers are a different
allowance. A job cannot see them, since `upload-artifact` is another process, so its saved telemetry leaves them out and
its `persistence` field says which uploads were still pending. `report-job:test` runs the real `upload-artifact` code
against the doubled services and records them beside the figure, reconciled against that boundary's complete traffic.
A rate-limited call is retried once, only when `Retry-After` is at most 60 seconds, and at
most twice in a job. Budget commands run one at a time.

Supported runners: Linux x86_64 and aarch64, and macOS on Apple Silicon. Any other runner gets a diagnostic and no failure.

## The direct tool

```sh
cargo install onebudgetspec-github-action --locked
onebudgetspec-github-action --help
```

The same report needs no Actions variable. Everything arrives as a flag; the input names are the action's:

```sh
onebudgetspec-github-action report --repo OWNER/NAME --base-ref main --mode base \
  --saved-history rolling-history.json --out report
```

It needs `onebudgetspec` (`pip install onebudgetspec-cli`) on `PATH`, and to read history or post also
`onebudgetspec-plugin-onevcs` and `gh`, authenticated by `GH_TOKEN`. Run it in a checkout.

Reproduce any job offline from its saved input, with nothing executed:

```sh
onebudgetspec-github-action reproduce --input comparison-input.json
```

`telemetry --metric NAME --input PATH` reports one metric of a saved telemetry file as a budget's measurement, and
refuses a file without the `ci-report` label (`--label` names another).

Exit codes: 0 success; 1 an enforced budget is over, or output failed; 2 invalid arguments or budgets file; 3 an enforced
measurement errored.

## Development

Develop with Rust (rustup), Node, bun, uv and just: `just bootstrap`, then `just check`.
Use `just --list` for scoped targets; CI runs the strict gate and installation on
Linux/macOS, with the install journey also on Windows. Dependencies upgrade through
`just upgrade`. Coverage has a 95% line floor.

Action journeys run `action.yml`'s own steps with the real driver, plugin, core, onevcs and `gh`; only the GitHub service, the
release host and the package index are doubled (`just test-e2e`). The first run builds the published plugin into
`target/action-tools` and fetches the core wheel. `report-job:test` records the job's request figure, which
`action-report-github-api-requests-per-run` in [.github/reporting/budgets.yaml](.github/reporting/budgets.yaml) reads back.

## Credentials and releases

The checked-in gh-secrets.json contains mappings only. The user can run
`gh-secrets sync` from this clone after reviewing them. Bitwarden access requires an unlocked
vault/session. GitHub synchronization needs repository administration/secrets-write
permission (classic PAT repo scope, or fine-grained repository secrets write).
RELEASE_PLZ_TOKEN needs contents and pull-requests write (classic repo and workflow
scopes where workflow changes require it); CARGO_REGISTRY_TOKEN needs crates.io
publish rights for this crate. The required llmlint check awaits CLAUDE_CODE_OAUTH_TOKEN
provider authentication; OPENAI_API_KEY is mapped for the user's OpenAI provider path.
Actions' GITHUB_TOKEN is built in, not synchronized. No npm/PyPI credentials are needed.

Release-plz derives semantic versions and changelogs from Conventional Commits and arms auto-merge on its
release pull request, which lands once its checks pass. It tags and cuts the GitHub Release and touches no registry.
That release fires `.github/workflows/release.yml`, which, for that immutable version, in order: builds each runner's
archive, uploads `action.yml`, the archives and `SHA256SUMS` to the release, verifies them through the real release
probe, and only then publishes the crate. A missing or mismatched payload stops the crate.
A publish that failed partway runs again by starting that workflow by hand with the release's tag, for example
`gh workflow run release.yml --ref main -f tag=v1.2.3`: everything published comes from the tag, and only the
runner labels come from `main`.
`release-targets.toml` declares both targets; consumers wait on the crate, which therefore means the whole payload
of that version is usable.
