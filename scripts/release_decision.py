#!/usr/bin/env python3
"""Decide whether the live OpenAPI document needs a new release of `mangools` on PyPI.

Fetches the live document, downloads the document shipped in the latest published
sdist, compares their sha256 and, when they differ, asks oasdiff whether the change
is breaking. Breaking bumps the minor version, anything else bumps the patch.

Usage:
    python scripts/release_decision.py --dry-run
    python scripts/release_decision.py            # also writes openapi.json and build/changelog.md

`oasdiff` is taken from the OASDIFF environment variable or from PATH.
With GITHUB_OUTPUT set, the decision is also written there for the workflow.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_spec import DEFAULT_SPEC_URL, fetch_document, normalise, sha256_of  # noqa: E402

PACKAGE = "mangools"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
OASDIFF_BREAKING_EXIT = 1


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "mangools-sdk-python"})
    with urllib.request.urlopen(request, timeout=120) as response:
        body: bytes = response.read()
    return body


def latest_published() -> tuple[str, Any]:
    """Return the latest version on PyPI and the OpenAPI document its sdist shipped."""
    metadata = json.loads(http_get(PYPI_URL))
    version = metadata["info"]["version"]
    sdists = [f for f in metadata["urls"] if f["packagetype"] == "sdist"]
    if not sdists:
        raise SystemExit(f"{PACKAGE} {version} on PyPI has no sdist to read the shipped spec from")

    with tarfile.open(fileobj=io.BytesIO(http_get(sdists[0]["url"])), mode="r:gz") as archive:
        member = next((m for m in archive.getmembers() if m.name == f"{PACKAGE}-{version}/openapi.json"), None)
        if member is None:
            raise SystemExit(f"the {PACKAGE} {version} sdist does not contain openapi.json")
        extracted = archive.extractfile(member)
        assert extracted is not None
        return version, json.load(extracted)


def oasdiff_command() -> list[str]:
    binary = os.environ.get("OASDIFF") or shutil.which("oasdiff")
    if not binary:
        raise SystemExit("oasdiff is not installed: set OASDIFF to its path or put it on PATH")
    return [binary]


def is_breaking(shipped: Path, live: Path) -> bool:
    result = subprocess.run(
        [*oasdiff_command(), "breaking", str(shipped), str(live), "--fail-on", "ERR"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == OASDIFF_BREAKING_EXIT:
        return True
    raise SystemExit(
        f"oasdiff failed with exit code {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
    )


def changelog(shipped: Path, live: Path) -> str:
    result = subprocess.run(
        [*oasdiff_command(), "changelog", str(shipped), str(live), "--format", "markdown"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"oasdiff changelog failed with exit code {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def next_version(latest: str, breaking: bool) -> str:
    major, minor, patch = (int(part) for part in latest.split("."))
    return f"{major}.{minor + 1}.0" if breaking else f"{major}.{minor}.{patch + 1}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the decision and write nothing to the tree")
    parser.add_argument(
        "--spec", default="openapi.json", help="where to write the live document (default: openapi.json)"
    )
    parser.add_argument("--changelog", default="build/changelog.md", help="where to write the oasdiff changelog")
    args = parser.parse_args()

    url = os.environ.get("SPEC_URL") or DEFAULT_SPEC_URL
    live = fetch_document(url)
    live_sha256 = sha256_of(live)

    latest, shipped = latest_published()
    shipped_sha256 = sha256_of(shipped)

    breaking = False
    version = ""
    notes = ""
    release = live_sha256 != shipped_sha256
    if release:
        with tempfile.TemporaryDirectory() as scratch:
            shipped_path = Path(scratch) / "shipped.json"
            live_path = Path(scratch) / "live.json"
            shipped_path.write_bytes(normalise(shipped))
            live_path.write_bytes(normalise(live))
            breaking = is_breaking(shipped_path, live_path)
            notes = changelog(shipped_path, live_path)
        version = next_version(latest, breaking)

    print(f"latest published version: {latest}")
    print(f"shipped spec sha256:      {shipped_sha256}")
    print(f"live spec sha256:         {live_sha256}")
    print(f"breaking changes:         {'yes' if breaking else 'no'}")
    print(f"next version:             {version or '-'}")
    print(f"decision:                 {'release ' + version if release else 'nothing to release'}")

    if not args.dry_run:
        Path(args.spec).write_bytes(normalise(live))
        if release:
            Path(args.changelog).parent.mkdir(parents=True, exist_ok=True)
            Path(args.changelog).write_text(
                notes or "The OpenAPI document changed; oasdiff reports no API-level change.\n"
            )

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output and not args.dry_run:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"release={'true' if release else 'false'}\n")
            handle.write(f"version={version}\n")
            handle.write(f"live_sha256={live_sha256}\n")
            handle.write(f"breaking={'true' if breaking else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
