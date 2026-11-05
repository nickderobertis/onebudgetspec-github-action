# AGENTS.md

Durable instructions for maintainers; edit this file, not its CLAUDE.md symlink.
Keep instructions terse and localized; put deterministic steps in just recipes.

## What this repo is

A GitHub Action and the Rust direct tool that runs the same report outside Actions. action.yml is
the public authority: its steps only map workflow context onto the driver's flags. The library
exposes no operational API. Comparison and rendering belong to onebudgetspec core and transport
to onebudgetspec-plugin-onevcs; this repo computes no scalar and never reruns a measurement.

## Stack and composition

- Product shape: cli, supplemented by library.
- Language(s): rust; Bash for tooling, Node/bun for mandatory Nx orchestration.
- References composed: base.md, project-graph.md, shapes/cli.md,
  languages/rust.md, intersections/rust-cli.md, ci.md, releasing.md,
  tools/onebudgetspec.md, llmlint.md; supplemented by shapes/library.md.
- Projects: published library/binary; binary e2e with the action journeys; install journey;
  report-job for this repo's own budget; action steps and journey harness; repo-checks for
  release/configuration; scripts and coverage tooling.
- Excluded, and why: asdf/direnv duplicate the pinned Rust toolchain; no npm/PyPI wrapper
  because cargo is the direct install path; action archives exist only for the action's runner.

## Workflow and invariants

Use just recipes and their Nx graph.
Tests drive real binaries, packaged installation and the real onevcs;
only external registries and the GitHub HTTP service are doubled. Cover success and failure.
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
Release PRs auto-merge once their checks pass; release-plz only versions, tags and cuts the
GitHub Release, whose publication fires release.yml: the action payload first, verified through
the release probe, the crate last. No manual version editing, tagging or deploy step; only a
failed publish is started by hand, dispatching release.yml from main with its tag. Release
orchestration uses RELEASE_PLZ_TOKEN so downstream workflows can run; publication uses
CARGO_REGISTRY_TOKEN. Credentials are user-managed; see README.md.

## Tests and ownership

Keep binary e2e, installation and report-job as separate graph leaves. Add real success and
failure journeys as behavior lands, driving action.yml's steps through the harness. Review suppressions with their substantive site-specific reasons; the
notignored PR comment exposes them for review.
