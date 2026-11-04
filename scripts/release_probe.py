"""Read the declared crates.io target; failures are never interpreted as absence."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib


def main():
    """Emit one stable version, no version, or a reason for refusing to answer."""
    declaration = tomllib.loads((Path(__file__).resolve().parent.parent / "release-targets.toml").read_text())
    if len(sys.argv) != 2:
        raise ValueError("expected one registry-qualified identifier")
    identifier = sys.argv[1]
    if identifier not in [target["id"] for target in declaration["target"]]:
        raise ValueError(f"unknown identifier: {identifier}")
    if not re.fullmatch(r"crate:[A-Za-z0-9][A-Za-z0-9_-]*", identifier):
        raise ValueError("unsupported registry identifier")
    name = identifier.split(":", 1)[1]
    result = subprocess.run([
        "curl", "-q", "-sS", "--connect-timeout", "10", "--max-time", "25",
        "-A", "onebudgetspec-github-action-release-probe", "-w", "\n%{http_code}",
        "--", f"https://crates.io/api/v1/crates/{name}",
    ], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"registry request failed ({result.returncode}): {result.stderr.strip()}")
    body, status = result.stdout.rsplit("\n", 1)
    if status == "404":
        return
    if status != "200":
        raise ValueError(f"registry request returned HTTP {status}")
    document = json.loads(body)
    crate = document["crate"]
    if crate["id"] != name:
        raise ValueError("registry answered for a different crate")
    version = crate["max_stable_version"]
    if version is None:
        return
    if not isinstance(version, str) or not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?", version
    ):
        raise ValueError("registry returned an invalid stable version")
    print(version)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"release-probe: {error}; restore the declaration or retry when the registry answers correctly", file=sys.stderr)
        sys.exit(1)
