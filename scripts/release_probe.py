"""Read a declared release target; failures are never interpreted as absence.

`release_probe.py IDENTIFIER` prints that target's newest usable stable version, or nothing.
`release_probe.py github-release:OWNER/NAME VERSION` asks about exactly that version: a release
counts only when its action.yml, its checksums and every archive they list were all published
and read back, so a partial upload is no release at all.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

STABLE = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
ARCHIVES = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "aarch64-apple-darwin")


def curl(url, accept=None):
    """One request; the body and status, or a refusal naming the failure."""
    arguments = [
        "curl", "-q", "-sSL", "--connect-timeout", "10", "--max-time", "25",
        "-A", "onebudgetspec-github-action-release-probe", "-w", "\n%{http_code}",
    ]
    if accept:
        arguments += ["-H", f"Accept: {accept}"]
    result = subprocess.run([*arguments, "--", url], capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"registry request failed ({result.returncode}): {result.stderr.decode().strip()}")
    body, status = result.stdout.rsplit(b"\n", 1)
    return body, status.decode()


def crate_version(name):
    """The newest stable version crates.io serves, or None."""
    body, status = curl(f"https://crates.io/api/v1/crates/{name}")
    if status == "404":
        return None
    if status != "200":
        raise ValueError(f"registry request returned HTTP {status}")
    crate = json.loads(body)["crate"]
    if crate["id"] != name:
        raise ValueError("registry answered for a different crate")
    version = crate["max_stable_version"]
    if version is None:
        return None
    if not isinstance(version, str) or not re.fullmatch(STABLE, version):
        raise ValueError("registry returned an invalid stable version")
    return version


def payload_version(repository, wanted):
    """The version of a release whose whole action payload reads back, or None."""
    route = f"releases/tags/v{wanted}" if wanted else "releases/latest"
    body, status = curl(
        f"https://api.github.com/repos/{repository}/{route}", "application/vnd.github+json")
    if status == "404":
        return None
    if status != "200":
        raise ValueError(f"release request returned HTTP {status}")
    release = json.loads(body)
    tag = release["tag_name"]
    if not isinstance(tag, str) or not re.fullmatch("v" + STABLE, tag):
        raise ValueError(f"release tag {tag!r} is not a stable version")
    version = tag[1:]
    if wanted and version != wanted:
        raise ValueError(f"the host answered {tag} when asked for v{wanted}")
    if release["draft"] or release["prerelease"]:
        return None
    assets = {asset["name"]: asset["browser_download_url"] for asset in release["assets"]}
    return version if verified(assets, version) else None


def verified(assets, version):
    """Whether every required asset exists and matches its checksum."""
    required = {"action.yml", "SHA256SUMS"} | {
        f"onebudgetspec-github-action-{version}-{target}.tar.gz" for target in ARCHIVES}
    if not required <= assets.keys():
        return False
    sums, status = curl(assets["SHA256SUMS"])
    if status != "200":
        raise ValueError(f"SHA256SUMS returned HTTP {status}")
    listed = dict(reversed(line.split(maxsplit=1)) for line in sums.decode().splitlines())
    if not required - {"SHA256SUMS"} <= listed.keys():
        return False
    for name in sorted(required - {"SHA256SUMS"}):
        asset, status = curl(assets[name])
        if status != "200":
            raise ValueError(f"{name} returned HTTP {status}")
        if hashlib.sha256(asset).hexdigest() != listed[name]:
            return False
    return True


def main():
    """Emit one stable version, no version, or a reason for refusing to answer."""
    declaration = tomllib.loads((Path(__file__).resolve().parent.parent / "release-targets.toml").read_text())
    if len(sys.argv) not in (2, 3):
        raise ValueError("expected one registry-qualified identifier and at most one exact version")
    identifier = sys.argv[1]
    wanted = sys.argv[2] if len(sys.argv) == 3 else None
    if identifier not in [target["id"] for target in declaration["target"]]:
        raise ValueError(f"unknown identifier: {identifier}")
    if wanted and not re.fullmatch(STABLE, wanted):
        raise ValueError(f"{wanted} is not a stable version")
    if re.fullmatch(r"crate:[A-Za-z0-9][A-Za-z0-9_-]*", identifier):
        if wanted:
            raise ValueError("an exact version is only asked of a github-release target")
        version = crate_version(identifier.split(":", 1)[1])
    elif re.fullmatch(r"github-release:[A-Za-z0-9-]+/[A-Za-z0-9._-]+", identifier):
        version = payload_version(identifier.split(":", 1)[1], wanted)
    else:
        raise ValueError("unsupported registry identifier")
    if version:
        print(version)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"release-probe: {error}; restore the declaration or retry when the registry answers correctly", file=sys.stderr)
        sys.exit(1)
