#!/usr/bin/env python3
"""Install the published core CLI wheel: one exact version, checked against its index hash.

Usage: install_core.py VERSION DIRECTORY. A wheel is a zip, and this one holds one executable,
so a cold job needs neither pip nor a virtual environment. PIP_INDEX_URL names another index,
as it does for pip. Quiet on success; a failure names what went wrong and exits 1.
"""
import hashlib
import html.parser
import io
import os
from pathlib import Path
import platform
import sys
import urllib.parse
import urllib.request
import zipfile

PROJECT = 'onebudgetspec-cli'


class Links(html.parser.HTMLParser):
    """The file links of one PEP 503 simple page."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.extend(value for name, value in attrs if name == 'href' and value)


def platform_tag():
    """The wheel platform tag core publishes for this runner, or a refusal naming it."""
    machine = platform.machine().lower()
    tags = {
        ('linux', 'x86_64'): 'manylinux_2_39_x86_64',
        ('linux', 'aarch64'): 'manylinux_2_39_aarch64',
        ('darwin', 'arm64'): 'macosx_11_0_arm64',
    }
    tag = tags.get((sys.platform, machine))
    if tag is None:
        raise ValueError(f'core publishes no wheel for {sys.platform} {machine}; use a Linux or Apple Silicon runner')
    return tag


def fetch(url):
    """Read one URL, naming it on failure."""
    request = urllib.request.Request(url, headers={'User-Agent': 'onebudgetspec-github-action'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def wheel_url(index, version):
    """The exact wheel's URL and its index-declared sha256."""
    page_url = f'{index.rstrip("/")}/{PROJECT}/'
    parser = Links()
    parser.feed(fetch(page_url).decode())
    name = f'onebudgetspec_cli-{version}-py3-none-{platform_tag()}.whl'
    for link in parser.links:
        address, _, fragment = link.partition('#')
        if address.rsplit('/', 1)[-1] == name:
            digest = dict(urllib.parse.parse_qsl(fragment)).get('sha256')
            if not digest:
                raise ValueError(f'the index lists {name} without a sha256')
            return urllib.parse.urljoin(page_url, address), digest
    raise ValueError(f'the index at {page_url} has no {name}; wait for that release or fix the pin')


def main():
    if len(sys.argv) != 3:
        raise ValueError('usage: install_core.py VERSION DIRECTORY')
    version, directory = sys.argv[1], Path(sys.argv[2])
    index = os.environ.get('PIP_INDEX_URL', 'https://pypi.org/simple')
    url, digest = wheel_url(index, version)
    wheel = fetch(url)
    if hashlib.sha256(wheel).hexdigest() != digest:
        raise ValueError(f'{url} does not match its index sha256; retry, or report the release')
    member = f'onebudgetspec_cli-{version}.data/scripts/onebudgetspec'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'onebudgetspec'
    target.write_bytes(zipfile.ZipFile(io.BytesIO(wheel)).read(member))
    target.chmod(0o755)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        print(f'install_core: {error}', file=sys.stderr)
        sys.exit(1)
