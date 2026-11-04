# The one command surface. Gate recipes delegate to the Nx project graph: `nx affected`
# for what a change can reach (keyed off an explicit merge base), `nx run-many` for the
# full sweep. What each target does is declared in that project's project.json.

set shell := ["bash", "-euo", "pipefail", "-c"]
# Recipe arguments reach the shell as "$1", "$@", never spliced into its source.
set positional-arguments

# The merge base the affected tier keys off. CI exports NX_BASE (nx-set-shas); locally it
# falls back to origin/main. Validated here, before anything interpolates it.
_nx_base_input := env_var_or_default("NX_BASE", "origin/main")
base := if _nx_base_input =~ '^[A-Za-z0-9._/-]+$' { _nx_base_input } else { error("NX_BASE must be a plain git ref or SHA — letters, digits and . _ / - only; got: " + _nx_base_input) }

# List available recipes.
default:
    @just --list

# Set up from a clean clone: the locked crates, Nx's locked Node toolchain, the pinned
# Rust toolchain, and cargo-llvm-cov and cargo-nextest.
bootstrap: fetch
    @bash scripts/bootstrap.sh

# The full gate at a tier: `affected` (default) is what a change can reach; `all` is the
# full sweep CI runs on the release pull request. Format check, clippy, the tests
# (unit, release wiring, the e2e journeys that spawn the built binary) with the 95%
# line-coverage floor over every crate's run, and the builds. Fails on any issue.
check tier="affected": (budgets tier)
    {{ if tier == "all" { "bunx nx run-many" } else if tier == "affected" { "bunx nx affected --base=" + base } else { error("unknown tier '" + tier + "' — use 'affected' (the default) or 'all'") } }} -t format-check lint typecheck test build docs --outputStyle=static
    bunx nx run workspace:coverage --excludeTaskDependencies --outputStyle=static

# The tests the affected tier reaches, then the coverage floor over their runs.
test:
    bunx nx affected --base={{ base }} -t test --outputStyle=static
    bunx nx run workspace:coverage --excludeTaskDependencies --outputStyle=static

# Every Rust test, instrumented, then the coverage floor over the union of their runs.
coverage:
    bunx nx run workspace:coverage --outputStyle=static

# The e2e journeys on their own (also run by `check`): the built binary, spawned.
test-e2e:
    bunx nx run onebudgetspec-github-action-e2e:test --outputStyle=static

# Download the locked crates into cargo's cache (what the offline install smoke builds from).
fetch:
    @cargo fetch --locked --quiet

# The end-user install path: package the crate as crates.io would serve it, install it
# locked into an empty root, run the installed binary. Its own CI job, outside `check`.
install-smoke:
    @bash tests/install/smoke.sh

# Lint and type check the projects this change can reach: clippy -D warnings, cargo
# check, shellcheck, actionlint and the project-boundary check.
lint:
    bunx nx affected --base={{ base }} -t lint typecheck --outputStyle=static

# Check formatting without changing anything.
format-check:
    bunx nx affected --base={{ base }} -t format-check --outputStyle=static

# Format every project in place.
format:
    bunx nx run-many -t format --outputStyle=static

# Supply chain: advisories, licences, bans and sources (cargo-deny), and unused crates
# (cargo-machete). A CI job of its own, on Linux.
deny:
    bunx nx run workspace:deny --outputStyle=static

# Build under the declared minimum supported Rust version.
msrv:
    bunx nx run workspace:msrv --outputStyle=static

# Upgrade every ecosystem's dependencies, then re-run the whole gate on the result.
upgrade:
    bun update --latest
    cargo update
    @just check all

# Branch protection and the merge model on GitHub, from create-repo's governance script
# (dero-skills). Extra flags pass through: `--verify` reads the live state back.
# llmlint: ignore[tool_output_is_signal] the governance script's report is the answer the caller asked for.
governance *flags:
    @script="${CREATE_REPO_SKILL_DIR:-$HOME/.agents/skills/create-repo}/scripts/setup_github_governance.py"; \
        test -f "$script" || { echo "governance: $script not found; install the create-repo skill or set CREATE_REPO_SKILL_DIR" >&2; exit 1; }; \
        uv run --quiet --script "$script" check pr-title llmlint "$@"

# Provision the dev toolchain for a session (the SessionStart hook runs it; idempotent).
session-setup:
    ./scripts/session-setup.sh

# Install or refresh the judged-lint toolchain (oneharness + llmlint). Idempotent.
setup-llmlint:
    ./scripts/setup-llmlint.sh

# Out of the deterministic gate: these need an authenticated harness and the network.

# LLM-judge lint over the configured set (or the paths given).
lint-llm *paths:
    @command -v llmlint >/dev/null 2>&1 || { echo "llmlint not installed — run 'just setup-llmlint'" >&2; exit 1; }
    @llmlint "$@"

# Model-free llmlint gate (config, suppressions, versions). CI runs it before the judged lint.
lint-llm-validate *args:
    @command -v llmlint >/dev/null 2>&1 || { echo "llmlint not installed — run 'just setup-llmlint'" >&2; exit 1; }
    @llmlint validate "$@"

# The judged lint over what this branch changed since it forked from the base.
lint-llm-diff base="origin/main" *args:
    @command -v llmlint >/dev/null 2>&1 || { echo "llmlint not installed — run 'just setup-llmlint'" >&2; exit 1; }
    @llmlint --diff --diff-base "$1" "${@:2}"

# The onebudgetspec budgets `check` runs at its tier: every domain's budget
# targets, then the root `budgets.yaml` (references/tools/onebudgetspec.md).
budgets tier="affected":
    {{ if tier == "all" { "bunx nx run-many" } else if tier == "affected" { "bunx nx affected --base=" + base } else { error("unknown tier '" + tier + "' — use 'affected' (the default) or 'all'") } }} -t budgets budgets-host
    [ ! -f budgets.yaml ] || bunx onebudgetspec check budgets.yaml

