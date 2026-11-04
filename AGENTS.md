# AGENTS.md

Durable instructions for maintainers; edit this file, not its CLAUDE.md symlink.
Keep instructions terse and localized; put deterministic steps in just recipes.

## What this repo is

A Rust library and CLI scaffold. The library intentionally exposes no operational
API. Do not choose a plugin protocol, action runtime, inputs or outputs here.
The future plugin is a general onebudgetspec-to-onevcs tool independent of GitHub
Actions and PR comments. The future action installs/runs onebudgetspec with that
plugin; direct tool installation remains supported.

## Stack and composition

- Product shape: cli, supplemented by library.
- Language(s): rust; Bash for tooling, Node/bun for mandatory Nx orchestration.
- References composed: base.md, project-graph.md, shapes/cli.md,
  languages/rust.md, intersections/rust-cli.md, ci.md, releasing.md,
  tools/onebudgetspec.md, llmlint.md; supplemented by shapes/library.md.
- Projects: published library/binary; binary e2e depends only on its build;
  install journey; repo-checks for release/configuration; scripts and coverage tooling.
- Excluded, and why: asdf/direnv duplicate the pinned Rust toolchain; no archive
  installer or npm/PyPI wrapper because cargo is the direct install path; no
  performance budgets until measurements exist; no executable action.yml or
  operational API until the implementation plan fixes those contracts.

## Workflow and invariants

Use just recipes and their Nx graph.
Formatting, clippy and docs deny warnings; coverage enforces 95% lines.
Tests drive real binaries, packaged installation and the real onevcs CLI;
only the external crates.io boundary is stubbed. Cover success and failure.
Validate boundary inputs, keep public contracts intentional and backward compatible,
and never commit secret values. Keep the narrow agent allowlist current.
Scripts stay quiet on success and name the failure plus its remedy.
Engineer future context through realistic tests and repeatable tooling; propose
unrelated improvements separately rather than widening a task.

## Commits, releases, and merging

Conventional Commits and semantic versioning drive release-plz. Pre-1.0 feat and
breaking changes bump minor; fix/perf/refactor/build bump patch; docs/ci/test/style/
chore do not release. Post-1.0 breaking changes bump major.
Squash-only merging, ordinary PR auto-merge allowed, merged branch deletion,
linear history and all correctness checks required; admins may override.
The broader tier runs on release PRs; ordinary changes use affected checks.
The release PR is a human checkpoint. Never arm release-PR auto-merge.
Merging that PR authorizes automated crates.io publication and GitHub source tags/
releases; no manual version editing, tagging or deploy step. Release orchestration
uses RELEASE_PLZ_TOKEN so downstream workflows can run; publication uses
CARGO_REGISTRY_TOKEN. Credentials are user-managed; see README.md.

## Tests and ownership

Keep binary e2e and installation as separate graph leaves. The library has no
operational API to test yet. Add real success and failure journeys as behavior
lands. Review suppressions with their substantive site-specific reasons; the
notignored PR comment exposes them for review.
