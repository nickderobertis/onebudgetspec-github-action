#!/usr/bin/env bash
# Install exactly the archive Cargo prepares, then run the same user journeys.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
crate=onebudgetspec-github-action
version=$(cargo metadata --no-deps --format-version 1 --locked | python3 -c 'import json,sys; print(next(p["version"] for p in json.load(sys.stdin)["packages"] if p["name"] == "onebudgetspec-github-action"))')
cargo package --locked -p "$crate"
install_root=$(mktemp -d)
trap 'rm -rf "$install_root"' EXIT
cargo install --locked --root "$install_root" --path "target/package/$crate-$version"
SCAFFOLD_BINARY="$install_root/bin/$crate" cargo test --locked -p "$crate-e2e" --test cli

# The report operation itself, installed, outside this checkout and with no Actions variable.
case "$(uname -s)" in
  Linux | Darwin) ;;
  *) exit 0 ;;
esac
# shellcheck source=action/dependencies.env
. action/dependencies.env
python3 action/install_core.py "$CORE_CLI_VERSION" "$install_root/bin"
journey="$install_root/journey"
mkdir "$journey"
cat > "$journey/budgets.yaml" <<'YAML'
schema_version: 1
budgets:
  - id: installed-journey
    measure: elapsed
    command: ["true"]
    unit: seconds
    direction: max
    threshold: 60
YAML
git -C "$journey" init --quiet
git -C "$journey" add budgets.yaml
git -C "$journey" -c user.name=smoke -c user.email=smoke@example.test commit --quiet -m journey
(
  cd "$journey"
  env -i PATH="$PATH" "$install_root/bin/$crate" report --repo owner/name --base-ref main --mode base --saved-history absent.json --out report \
    | grep -q 'installed-journey' \
    || { echo "smoke: the installed report did not show its budget; run it by hand to see why." >&2; exit 1; }
  env -i PATH="$PATH" "$install_root/bin/$crate" reproduce --input report/comparison-input.json \
    | grep -q 'installed-journey' \
    || { echo "smoke: the installed reproduce did not show its budget; run it by hand to see why." >&2; exit 1; }
)
