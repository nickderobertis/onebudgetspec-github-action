#!/usr/bin/env bash
# Registry reads are unauthenticated; stdout belongs only to the release answer.
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/release_probe.py" "$@"
