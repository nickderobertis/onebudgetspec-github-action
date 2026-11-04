# onebudgetspec-github-action

Rust scaffold only: help and version work; all unsupported arguments fail without
performing work. Exit codes are 0 for help/version, 1 for output failure, and 2
for unsupported arguments. The library exposes no operational API. No action.yml is shipped.

The planned plugin is a general onebudgetspec-to-onevcs tool independent of GitHub
Actions or PR comments. This action repository will install/run onebudgetspec with
that plugin and will also support direct tool installation. Protocol, runtime,
inputs and outputs belong to the implementation plan.

After the first authorized crates.io release, install the direct tool with:

```sh
cargo install onebudgetspec-github-action --locked
onebudgetspec-github-action --help
```

Until then, CI packages the crate, installs that packaged directory into an empty
root, and exercises the installed binary outside the checkout. GitHub source tags
and releases provide the future action distribution; none has been created yet.

Develop with Rust (rustup), Node, bun and just: `just bootstrap`, then `just check`.
Use `just --list` for scoped targets; CI runs the strict gate and installation on
Linux/macOS, with the install journey also on Windows. Dependencies upgrade through
`just upgrade`. Coverage has a 95% line floor. No measured budgets exist yet;
onebudgetspec is pinned and wired so future measured budgets enter the gate.

## Credentials and later releases

The checked-in gh-secrets.json contains mappings only. The user can run
`gh-secrets sync` from this clone after reviewing them. No secret synchronization
is performed during scaffold creation. Bitwarden access requires an unlocked
vault/session. GitHub synchronization needs repository administration/secrets-write
permission (classic PAT repo scope, or fine-grained repository secrets write).
RELEASE_PLZ_TOKEN needs contents and pull-requests write (classic repo and workflow
scopes where workflow changes require it); CARGO_REGISTRY_TOKEN needs crates.io
publish rights for this crate. The release workflow awaits these credentials.
The required llmlint check awaits CLAUDE_CODE_OAUTH_TOKEN provider authentication;
OPENAI_API_KEY is mapped for the user's OpenAI provider path. Actions' GITHUB_TOKEN
is built in, not synchronized. No npm/PyPI credentials are needed.

Release-plz derives semantic versions and changelogs from Conventional Commits.
The release PR is a human checkpoint; merging it authorizes publication, tags and
GitHub releases. Scaffold verification uses only update in a disposable copy and
Cargo package/publish dry runs; it performs no registry upload.
