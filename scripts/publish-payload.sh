#!/usr/bin/env bash
# Publish one release's payload: action.yml, every action archive and SHA256SUMS, onto the
# GitHub Release its tag names. Run before the crate; a re-run replaces identical assets.
# Usage: scripts/publish-payload.sh vX.Y.Z DIST_DIRECTORY
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
tag="${1:-}"
dist="${2:-}"
case "$tag" in
  v[0-9]*) ;;
  *) echo "publish-payload: expected a release tag like v1.2.3, got '$tag'; run it from the release workflow." >&2; exit 2 ;;
esac
[ -d "$dist" ] || { echo "publish-payload: $dist holds no archives; build them first." >&2; exit 1; }
cp action.yml "$dist/action.yml"
( cd "$dist" && sha256sum action.yml ./*.tar.gz | sed 's# \./# #; s#  \./#  #' > SHA256SUMS )
gh release upload "$tag" "$dist"/* --clobber --repo "${GITHUB_REPOSITORY:?run from the release workflow, or set GITHUB_REPOSITORY}"
