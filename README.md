# mangools

Python client for the [Mangools API](https://apidocs.mangools.com): KWFinder, SERPChecker,
SERPWatcher, LinkMiner, SiteProfiler and AI Search Watcher. `httpx`-based, fully typed, `py.typed`.

> **Generated client.** Every module under `mangools/` is generated from the live OpenAPI document
> at <https://api.mangools.com/v3/openapi.json>; only the `MangoolsClient` factory comes from a
> hand-written template. The `0.x` series changes shape when the API does. See [Known gaps](#known-gaps).

## Install

```console
pip install mangools
```

Python 3.9 or newer. Runtime dependencies: `httpx`, `attrs`, `python-dateutil`.

## Hello world

```python
import os

from mangools import MangoolsClient
from mangools.api.aisearchwatcher import get_aiwatcher_monitors
from mangools.api.kwfinder import get_kwfinder_related_keywords
from mangools.models import Error

client = MangoolsClient(api_key=os.environ["MANGOOLS_API_KEY"])

related = get_kwfinder_related_keywords.sync(client=client, kw="seo tools")
if isinstance(related, Error):
    raise SystemExit(f"{related.error.type_}: {related.error.message}")
if related is not None and related.keywords:
    print(f"related keywords ({related.count_keywords_before_limit} before the limit):")
    for keyword in related.keywords[:5]:
        print(f"  {keyword.kw!r:<28} sv={keyword.sv} cpc={keyword.cpc} seo={keyword.seo}")

monitors = get_aiwatcher_monitors.sync(client=client)
if isinstance(monitors, Error):
    raise SystemExit(f"{monitors.error.type_}: {monitors.error.message}")
if monitors is not None and monitors.monitors:
    print(f"\n{len(monitors.monitors)} AI Search Watcher monitors:")
    for monitor in monitors.monitors:
        print(f"  {monitor.field_id}  {monitor.brand!r} -> {monitor.domain}")
```

`get_kwfinder_related_keywords` also takes `location_id` and `language_id`; both default to `0`
(worldwide / all languages). Resolve location IDs with
`mangools.api.kwfinder.get_mangools_locations`.

## Authentication

Mangools authenticates with an API key in the **`x-access-token`** header. It is not an RFC 6750
bearer token, so `Authorization: Bearer …` will not work.

```python
import os

from mangools import MangoolsClient

client = MangoolsClient(api_key=os.environ["MANGOOLS_API_KEY"])
```

`MangoolsClient` is a thin factory over the generated `AuthenticatedClient` that fills in the header
name from the spec's `ApiKeyAuth` scheme. Keyword arguments are forwarded verbatim, so
`timeout=httpx.Timeout(30.0)`, `headers={...}`, `follow_redirects=True`, `verify_ssl=False` and
`httpx_args={...}` all work:

```python
import httpx

client = MangoolsClient(
    api_key=os.environ["MANGOOLS_API_KEY"],
    timeout=httpx.Timeout(30.0),
    raise_on_unexpected_status=True,
)
```

`base_url` defaults to the spec's `servers[0].url`, `https://api.mangools.com/v3`. Override it to
point at another server, for example a local mock of the API.

Never hard-code the key. Read it from the environment or a secret manager.

## Calling an endpoint

Every operation is a module under `mangools.api.<tag>` exposing four functions:

| function | returns | on an undocumented status |
|---|---|---|
| `sync(...)` | the parsed body, or `None` | `None` (or raises if `raise_on_unexpected_status`) |
| `sync_detailed(...)` | `Response[T]` with `status_code`, `headers`, `content`, `parsed` | same |
| `asyncio(...)` | awaitable parsed body | same |
| `asyncio_detailed(...)` | awaitable `Response[T]` | same |

```python
import asyncio

from mangools.api.kwfinder import get_kwfinder_limits

async def main() -> None:
    response = await get_kwfinder_limits.asyncio_detailed(client=client)
    print(response.status_code, response.parsed)

asyncio.run(main())
```

Use `sync_detailed` / `asyncio_detailed` when you need the status code or headers — for example the
`Retry-After` on a 429.

Optional fields are `Unset`, not `None`, because the API distinguishes "absent" from "explicitly
null". `Unset` is falsy, and `isinstance(value, Unset)` narrows under `mypy`:

```python
from mangools.types import Unset

if not isinstance(keyword.sv, Unset):
    ...       # keyword.sv is a float here
```

## Errors

Most non-2xx responses parse into the `Error` envelope, so error handling is a type check rather
than a status-code table:

```python
from mangools.models import Error
from mangools.types import UNSET

result = get_kwfinder_related_keywords.sync(client=client, kw="seo tools")
if isinstance(result, Error):
    print(result.error.type_, result.error.message)
    if result.error.retry_after is not UNSET:
        print("retry after", result.error.retry_after, "s")
```

`error.errors` carries the validation messages on a 422 and `error.retry_after` the wait in seconds on a 429.
A status the spec does not document raises `mangools.errors.UnexpectedStatus` when the client is built
with `raise_on_unexpected_status=True`.

Two 429 responses exist and they do not look alike. The application's own 429 is the `Error`
envelope above. The gateway's is a plain HTML page produced before the request reaches the
application; the document declares it as `text/html`, and the operations that carry it gain a `str`
arm rather than an `Error` arm:

```python
from mangools.api.kwfinder import get_kwfinder_limits

limits = get_kwfinder_limits.sync(client=client)   # Union[Limit, str] | None
if isinstance(limits, str):
    ...       # the gateway HTML page: the request rate limit was exceeded
```

`GET /kwfinder/limits` accepts an anonymous caller and has no validation or quota gate, so there is
no `Error` branch to check for there.

## Typing

The package ships a PEP 561 `py.typed` marker, so `mypy` and `pyright` type-check your call sites
with no stub package. The generated client itself passes `mypy --strict` with no `type: ignore`
anywhere.

## How the package is built

Nothing generated and no copy of the API definition is committed. Git holds the generator
configuration, two templates and the build scripts; everything else is produced
from the live document:

```console
pip install -r requirements-dev.txt
python scripts/fetch_spec.py        # writes openapi.json from https://api.mangools.com/v3/openapi.json
VERSION=0.0.0.dev0 ./scripts/generate.sh
```

`SPEC_URL` overrides the address of the document, and `VERSION` sets the package version (default `0.0.0.dev0`). A build that cannot
download the document fails; there is no fallback copy. `generate.sh` also writes `SPEC_GAPS.md` and
`TYPE_GAPS.md`, two reports of what the document does not describe yet.

The package is generated by [`openapi-python-client`](https://github.com/openapi-generators/openapi-python-client), pinned in `requirements-dev.txt`.

## Versions and releases

The `info.version` of the document stays `3.0.0` when its content changes, so a release is decided by
the sha256 of the document instead. Every published sdist contains the `openapi.json` it was built
from, and the package exposes the same hash:

```python
import mangools

mangools.SPEC_SHA256   # sha256 of the document this version was generated from
```

A scheduled workflow compares the live document with the one inside the latest sdist on PyPI. When
they differ, [oasdiff](https://github.com/oasdiff/oasdiff) decides the next version: a breaking
change raises the minor version, anything else raises the patch version. The workflow then generates
the package, runs every check, publishes through PyPI trusted publishing and creates a GitHub Release
with the oasdiff changelog. Release notes live in GitHub Releases.

To see the decision without releasing anything:

```console
OASDIFF=/path/to/oasdiff python scripts/release_decision.py --dry-run
```

## Known gaps

`SPEC_GAPS.md` audits the live document and `TYPE_GAPS.md` lists every place where a type in this
client stayed `Any` because the document does not describe the value. Both are written by
`generate.sh`. CI fails when a response or model attribute is untyped for
that reason, so a published version has none.

Optional fields are `Unset`, not `None`, and an optional group of fields that the API leaves out of
a response stays `Unset`. Compare with `UNSET`, not with `None`.

This repository is maintained by the Mangools team and does not accept external pull requests or
issues. Please send questions and bug reports to support@mangools.com.

## License

Apache-2.0. See [LICENSE](LICENSE).
