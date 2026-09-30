#!/usr/bin/env python3
"""Download the published OpenAPI document into a gitignored file.

The document is the only source of truth for the client and is never committed.
Set SPEC_URL to build against another deployment.

Usage:
    python scripts/fetch_spec.py [openapi.json]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_SPEC_URL = "https://api.mangools.com/v3/openapi.json"


def normalise(document: object) -> bytes:
    """Render the document the one way its identity is computed."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_of(document: object) -> str:
    return hashlib.sha256(normalise(document)).hexdigest()


def fetch(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "mangools-sdk-python"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as error:
        raise SystemExit(f"could not fetch the OpenAPI document from {url}: {error}") from error


def fetch_document(url: str) -> dict[str, object]:
    document = fetch(url)
    if not isinstance(document, dict) or "openapi" not in document or not document.get("paths"):
        raise SystemExit(f"{url} did not return an OpenAPI document")
    return document


def main(argv: list[str]) -> int:
    destination = argv[1] if len(argv) > 1 else "openapi.json"
    url = os.environ.get("SPEC_URL") or DEFAULT_SPEC_URL

    document = fetch_document(url)
    with open(destination, "wb") as handle:
        handle.write(normalise(document))
    print(f"fetched {url} -> {destination} (sha256 {sha256_of(document)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
