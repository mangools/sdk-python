#!/usr/bin/env python3
"""Fail the build on every `Any` the generated client inherited from the spec.

`mypy --strict` proves the client is internally consistent; it says nothing about
whether the types mean anything. A response the spec leaves unschema'd generates
as `Response[Any]` and type-checks perfectly while telling the caller nothing.
This script is the second half of the gate: it finds those `Any`s and traces each
one back to the node in the OpenAPI document that produced it.

Two kinds of `Any` exist in the generated tree and only one of them is a defect:

  transport  `_kwargs: dict[str, Any]`, `to_dict() -> dict[str, Any]`,
             `additional_properties`, `**httpx_kwargs: Any`. These are how the
             client talks to httpx and are independent of spec quality.
  spec       a model attribute, or an operation's parsed response type. Every one
             of these is a defect in the API definition.

Only the second kind is reported, and the report is a join, not a heuristic:
model classes are matched to schema nodes by their full set of wire keys, and
endpoint modules by the `method` + `url` they build. An `Any` that cannot be
traced is reported as UNATTRIBUTED and still fails, so the gate cannot go quiet
because the join broke.

Usage:
    python scripts/check_spec_any.py openapi.json --package mangools --md TYPE_GAPS.md
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spec_audit import Auditor, ptr  # noqa: E402

PUBLIC_ENDPOINT_FUNCS = ("sync", "sync_detailed", "asyncio", "asyncio_detailed")

# attrs bookkeeping, not a spec field.
TRANSPORT_ATTRS = frozenset({"additional_properties"})

# spec_audit codes whose node generates as `Any` in Python.
ANY_PRODUCING_CODES = frozenset(
    {
        "GAP_EMPTY_ITEMS",
        "GAP_FREEFORM_OBJECT",
        "GAP_UNTYPED_SCHEMA",
        "GAP_ADDITIONAL_PROPS_FREEFORM",
        "GAP_RESPONSE_NO_SCHEMA",
        "GAP_RESPONSE_NO_CONTENT",
        "GAP_REQUEST_BODY_NO_SCHEMA",
        "GAP_REQUEST_BODY_NO_CONTENT",
        "BLOCKER_ARRAY_NO_ITEMS",
        "BLOCKER_INVALID_TYPE",
    }
)


def contains_any(node: ast.AST | None) -> bool:
    if node is None:
        return False
    return any(isinstance(n, ast.Name) and n.id == "Any" for n in ast.walk(node))


def src(node: ast.AST) -> str:
    return ast.unparse(node)


# --------------------------------------------------------------- generated code


class ModelAttr:
    __slots__ = ("file", "line", "cls", "attr", "annotation", "wire_key", "class_keys")

    def __init__(
        self,
        file: str,
        line: int,
        cls: str,
        attr: str,
        annotation: str,
        wire_key: str | None,
        class_keys: frozenset[str],
    ) -> None:
        self.file = file
        self.line = line
        self.cls = cls
        self.attr = attr
        self.annotation = annotation
        self.wire_key = wire_key
        self.class_keys = class_keys


class EndpointAny:
    __slots__ = ("file", "line", "method", "url", "returns")

    def __init__(self, file: str, line: int, method: str, url: str, returns: str) -> None:
        self.file = file
        self.line = line
        self.method = method
        self.url = url
        self.returns = returns


def wire_keys_of(cls: ast.ClassDef) -> dict[str, str]:
    """attrs attribute name -> the JSON key it is read from.

    The generated `from_dict` pops every property by its wire name, so the
    mapping is recovered from the code rather than guessed from the attribute.
    """
    out: dict[str, str] = {}
    for node in ast.walk(cls):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        for call in ast.walk(node.value):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "pop"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                out.setdefault(target.id, call.args[0].value)
                # A `$ref`-typed property is popped into `_name` and only then
                # built into `name`, so the attribute itself never appears as a
                # `pop` target.
                out.setdefault(target.id.lstrip("_"), call.args[0].value)
                break
    return out


def scan_models(package: str) -> list[ModelAttr]:
    out: list[ModelAttr] = []
    models_dir = os.path.join(package, "models")
    for name in sorted(os.listdir(models_dir)):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        path = os.path.join(models_dir, name)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            keys = wire_keys_of(cls)
            declared = [n for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
            class_keys = frozenset(
                keys.get(n.target.id, n.target.id)
                for n in declared
                if isinstance(n.target, ast.Name) and n.target.id not in TRANSPORT_ATTRS
            )
            for n in declared:
                assert isinstance(n.target, ast.Name)
                if n.target.id in TRANSPORT_ATTRS or not contains_any(n.annotation):
                    continue
                out.append(
                    ModelAttr(
                        file=path,
                        line=n.lineno,
                        cls=cls.name,
                        attr=n.target.id,
                        annotation=src(n.annotation) if n.annotation else "",
                        wire_key=keys.get(n.target.id),
                        class_keys=class_keys,
                    )
                )
    return out


def endpoint_route(tree: ast.Module) -> tuple[str, str]:
    """Recover `(METHOD, url)` from the `_kwargs` dict the module builds."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.value, ast.Dict):
            continue
        pairs = {
            k.value: v
            for k, v in zip(node.value.keys, node.value.values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if "method" not in pairs or "url" not in pairs:
            continue
        method = pairs["method"]
        url = pairs["url"]
        if isinstance(method, ast.Constant) and isinstance(url, ast.Constant):
            return str(method.value).upper(), str(url.value)
        if isinstance(method, ast.Constant) and isinstance(url, ast.JoinedStr):
            # f-string: "/aiwatcher/monitor/{id}" comes back with its placeholders.
            parts: list[str] = []
            for part in url.values:
                if isinstance(part, ast.FormattedValue):
                    parts.append("{" + src(part.value) + "}")
                elif isinstance(part, ast.Constant):
                    parts.append(str(part.value))
            return str(method.value).upper(), "".join(parts)
    return "", ""


def scan_endpoints(package: str) -> list[EndpointAny]:
    out: list[EndpointAny] = []
    api_dir = os.path.join(package, "api")
    for root, _dirs, files in os.walk(api_dir):
        for name in sorted(files):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            method, url = endpoint_route(tree)
            for node in tree.body:
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in PUBLIC_ENDPOINT_FUNCS
                    and contains_any(node.returns)
                ):
                    out.append(
                        EndpointAny(
                            file=path,
                            line=node.lineno,
                            method=method,
                            url=url,
                            returns=src(node.returns) if node.returns else "",
                        )
                    )
                    break
    return out


# ---------------------------------------------------------------- spec indexing


def object_fingerprints(spec: dict[str, Any]) -> dict[frozenset[str], list[str]]:
    """Every schema node with `properties`, keyed by its exact property-name set."""
    out: dict[frozenset[str], list[str]] = defaultdict(list)

    def walk(node: Any, pointer: str) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, pointer + ptr(i))
            return
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict) and props:
            out[frozenset(props)].append(pointer)
        for key, value in node.items():
            if key in ("example", "examples", "default", "enum"):
                continue
            walk(value, pointer + ptr(key))

    walk(spec.get("components", {}).get("schemas", {}), ptr("components", "schemas"))
    walk(spec.get("paths", {}), ptr("paths"))
    return dict(out)


def resolve_pointer(doc: Any, pointer: str) -> Any:
    node = doc
    for raw in pointer.lstrip("/").split("/"):
        if raw == "":
            continue
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        else:
            return None
    return node


def defects_under(spec: dict[str, Any], findings_by_pointer: dict[str, Any], pointer: str) -> list[str]:
    """Every `Any`-producing finding at or below `pointer`, following `$ref`.

    A property that is a bare `$ref` carries no finding of its own — the defect
    lives in the component it points at. Matching only on the property's own
    pointer therefore reports nothing and looks like a clean attribution, so the
    whole reachable subtree is searched instead.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any, ptr_: str) -> None:
        if ptr_ in seen:
            return
        seen.add(ptr_)
        if ptr_ in findings_by_pointer:
            found.append(ptr_)
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                target = "/" + ref[2:]
                walk(resolve_pointer(spec, target), target)
            for key, value in node.items():
                if key in ("example", "examples", "default", "enum", "description"):
                    continue
                walk(value, ptr_ + ptr(key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, ptr_ + ptr(i))

    walk(resolve_pointer(spec, pointer), pointer)
    return found


def route_label(spec: dict[str, Any], method: str, url: str) -> tuple[str, str] | None:
    """Match a generated endpoint's (METHOD, url) back to a spec path."""
    paths = spec.get("paths") or {}
    if url in paths and method.lower() in paths[url]:
        return url, method.lower()

    # openapi-python-client renders `{id}` placeholders verbatim, but a path that
    # ends in a slash or carries a different placeholder name still has to match.
    def normalise(p: str) -> str:
        return "/".join("{}" if seg.startswith("{") and seg.endswith("}") else seg for seg in p.strip("/").split("/"))

    target = normalise(url)
    for path, item in paths.items():
        if normalise(path) == target and isinstance(item, dict) and method.lower() in item:
            return path, method.lower()
    return None


# Statuses that are defined to carry no body. A generated `Any` on one of these
# is the generator's imprecision, not a spec defect.
BODYLESS_STATUSES = frozenset({"204", "205", "304"})


def deref_response(spec: dict[str, Any], resp: Any) -> tuple[Any, str | None]:
    """A response object and, when it came from `components.responses`, its pointer."""
    if isinstance(resp, dict):
        ref = resp.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target = "/" + ref[2:]
            return resolve_pointer(spec, target), target
    return resp, None


def resolves_to_schema(spec: dict[str, Any], resp: Any) -> bool:
    """True when a response object names a real schema for every media type."""
    obj, _ = deref_response(spec, resp)
    content = (obj or {}).get("content")
    if not content:
        return False
    for media_obj in content.values():
        if not isinstance(media_obj, dict) or "schema" not in media_obj:
            return False
        schema = media_obj["schema"]
        if isinstance(schema, dict) and not (
            set(schema) & {"type", "$ref", "allOf", "oneOf", "anyOf", "enum", "properties"}
        ):
            return False
    return True


def is_generator_artifact(spec: dict[str, Any], path: str, method: str) -> bool:
    """True when every `Any` in the union came from a status HTTP defines as body-less.

    openapi-python-client types a body-less response as `Response[Any]` with
    `parsed=None` instead of `Response[None]`. For 204/205/304 there is no schema
    an author could add, so charging it to the spec would make the gate demand a
    response schema for endpoints that must not have one. Every other status
    declared without `content` stays blocking, including when it sits beside a
    204 — that one the document can still describe.
    """
    responses = ((spec["paths"][path] or {}).get(method) or {}).get("responses") or {}
    bodyless = {s for s in responses if not (deref_response(spec, responses[s])[0] or {}).get("content")}
    if not bodyless or not {str(s) for s in bodyless} <= BODYLESS_STATUSES:
        return False
    # A 2xx that does carry content but resolves to nothing is a second source of
    # `Any`, and this branch must not hide it.
    return all(
        resolves_to_schema(spec, responses[s]) for s in responses if str(s).startswith("2") and s not in bodyless
    )


def response_any_reason(spec: dict[str, Any], path: str, method: str) -> tuple[str, str, str]:
    """Why this operation's parsed response is `Any`: (pointer, defect, fix)."""
    op = (spec["paths"][path] or {}).get(method) or {}
    responses = op.get("responses") or {}
    base = ptr("paths", path, method, "responses")
    success = [s for s in responses if str(s).startswith("2")]
    if not success:
        return (
            base,
            "the operation declares no 2xx response at all",
            "Add a `200` response with `content: {application/json: {schema: {$ref: ...}}}`.",
        )
    status = sorted(success)[0]
    resp, _ = deref_response(spec, responses[status])
    resp = resp or {}
    if not resp.get("content"):
        return (
            base + ptr(status),
            f"response `{status}` declares no `content`",
            f"Add `content: {{application/json: {{schema: ...}}}}` to `{status}`.",
        )
    for media, media_obj in resp["content"].items():
        if not isinstance(media_obj, dict) or "schema" not in media_obj:
            return (
                base + ptr(status, "content", media),
                f"response `{status}` for `{media}` declares no `schema`",
                "Add the response schema (reference a named component).",
            )
        schema = media_obj["schema"]
        if isinstance(schema, dict) and not (
            set(schema) & {"type", "$ref", "allOf", "oneOf", "anyOf", "enum", "properties"}
        ):
            return (
                base + ptr(status, "content", media, "schema"),
                f"response `{status}` schema declares no `type`, `$ref` or composition (keys: {sorted(schema)})",
                "Give the response schema a real `type` and shape.",
            )

    # The success body is fully typed, so the `Any` came in through another declared
    # status: a response with no `content` is rendered as `cast(Any, None)`, and that
    # branch widens the whole parsed union.
    bodyless: list[tuple[str, str]] = []
    for other_status in sorted(responses, key=str):
        if str(other_status).startswith("2"):
            continue
        other, component = deref_response(spec, responses[other_status])
        if isinstance(other, dict) and not other.get("content"):
            bodyless.append((str(other_status), component or base + ptr(str(other_status))))
    if bodyless:
        noun, verb = ("response", "declares") if len(bodyless) == 1 else ("responses", "declare")
        return (
            bodyless[0][1],
            f"{noun} {' and '.join(f'`{s}`' for s, _ in bodyless)} {verb} no `content`, so the "
            "generator types that branch `Any` and widens the parsed union",
            "Give it a `content` block describing the body actually returned, or drop the "
            "declaration if the status is not part of the documented contract.",
        )

    return (
        base + ptr(status),
        f"response `{status}` resolves to a free-form schema",
        "Replace the free-form schema with a named component.",
    )


# ------------------------------------------------------------------- attribution


class Row:
    __slots__ = (
        "kind",
        "where",
        "symbol",
        "annotation",
        "pointer",
        "defect",
        "fix",
        "operations",
        "attributed",
    )

    def __init__(
        self,
        kind: str,
        where: str,
        symbol: str,
        annotation: str,
        pointer: str,
        defect: str,
        fix: str,
        operations: list[str],
        attributed: bool = True,
    ) -> None:
        self.kind = kind
        self.where = where
        self.symbol = symbol
        self.annotation = annotation
        self.pointer = pointer
        self.defect = defect
        self.fix = fix
        self.operations = operations
        self.attributed = attributed

    def as_dict(self) -> dict[str, Any]:
        return {s: getattr(self, s) for s in self.__slots__}


def attribute_models(
    spec: dict[str, Any],
    attrs: list[ModelAttr],
    findings_by_pointer: dict[str, Any],
    schema_to_ops: dict[str, set[str]],
) -> list[Row]:
    fingerprints = object_fingerprints(spec)
    rows: list[Row] = []
    for a in attrs:
        candidates = fingerprints.get(a.class_keys, [])
        key = a.wire_key or a.attr
        matches: list[str] = []
        prop_pointer = ""
        for candidate in candidates:
            prop_pointer = candidate + ptr("properties", key)
            matches = defects_under(spec, findings_by_pointer, prop_pointer)
            if matches:
                break

        if not matches:
            rows.append(
                Row(
                    "model",
                    f"{a.file}:{a.line}",
                    f"{a.cls}.{a.attr}",
                    a.annotation,
                    "UNATTRIBUTED" + (f" (searched `{prop_pointer}`)" if prop_pointer else ""),
                    "UNATTRIBUTED — no spec node matched this attribute. Either the "
                    "join in scripts/check_spec_any.py is stale or the generator "
                    "introduced an `Any` of its own.",
                    "Update the attribution in scripts/check_spec_any.py to match this shape.",
                    [],
                    attributed=False,
                )
            )
            continue

        pointer = matches[0]
        finding = findings_by_pointer[pointer]
        owner = pointer.split("/")[3] if pointer.startswith("/components/schemas/") else ""
        rows.append(
            Row(
                "model",
                f"{a.file}:{a.line}",
                f"{a.cls}.{a.attr}",
                a.annotation,
                " ".join(f"`{m}`" for m in matches),
                finding["detail"],
                finding["fix"],
                finding.get("operations") or sorted(schema_to_ops.get(owner, set())),
            )
        )
    return rows


def attribute_endpoints(spec: dict[str, Any], endpoints: list[EndpointAny]) -> list[Row]:
    rows: list[Row] = []
    for e in endpoints:
        match = route_label(spec, e.method, e.url) if e.url else None
        if match is None:
            rows.append(
                Row(
                    "operation",
                    f"{e.file}:{e.line}",
                    f"{e.method} {e.url}".strip(),
                    e.returns,
                    "UNATTRIBUTED",
                    "UNATTRIBUTED — the generated module's route did not match any spec path.",
                    "Update the attribution in scripts/check_spec_any.py to match this shape.",
                    [],
                    attributed=False,
                )
            )
            continue
        path, method = match
        op = (spec["paths"][path] or {}).get(method) or {}
        op_id = op.get("operationId")
        label = f"{method.upper()} {path}" + (f" ({op_id})" if op_id else "")
        if is_generator_artifact(spec, path, method):
            statuses = ", ".join(sorted(op.get("responses") or {}))
            rows.append(
                Row(
                    "generator",
                    f"{e.file}:{e.line}",
                    label,
                    e.returns,
                    ptr("paths", path, method, "responses"),
                    f"the spec correctly declares `{statuses}` with no body; "
                    "openapi-python-client types a body-less response as "
                    "`Response[Any]` with `parsed=None` rather than `Response[None]`",
                    "No change to the OpenAPI document. Fixing this means an "
                    "openapi-python-client change or a custom `endpoint_module.py.jinja`.",
                    [label],
                )
            )
            continue
        pointer, defect, fix = response_any_reason(spec, path, method)
        rows.append(Row("operation", f"{e.file}:{e.line}", label, e.returns, pointer, defect, fix, [label]))
    return rows


# ----------------------------------------------------------------------- report


def render_markdown(rows: list[Row], source: str, package: str) -> str:
    models = [r for r in rows if r.kind == "model"]
    ops = [r for r in rows if r.kind == "operation"]
    artifacts = [r for r in rows if r.kind == "generator"]
    blocking = len(models) + len(ops)
    lines = [
        "# Spec-derived `Any` in the generated client",
        "",
        f"Generated by `scripts/check_spec_any.py` from `{source}` against `{package}/`.",
        "",
        "Each blocking row is an `Any` that reached the SDK from the OpenAPI document, "
        "not from the generator. `mypy --strict` accepts all of them, because a "
        "well-formed client built on an under-specified contract type-checks and still "
        "tells the caller nothing. The build fails while any blocking row remains.",
        "",
        "Transport `Any` (`_kwargs: dict[str, Any]`, `to_dict() -> dict[str, Any]`, "
        "`additional_properties`, `**httpx_kwargs: Any`) is deliberately not listed: it "
        "is how the client talks to httpx and is independent of spec quality.",
        "",
        "| | count | fails the build |",
        "|---|---|---|",
        f"| operations whose parsed response is `Any` | {len(ops)} | yes |",
        f"| model attributes typed `Any` | {len(models)} | yes |",
        f"| body-less responses the generator widened to `Any` | {len(artifacts)} | no |",
        f"| **blocking total** | **{blocking}** | |",
        "",
    ]

    if ops:
        lines += [
            "## Operations returning an untyped body",
            "",
            "The generated `sync()` / `asyncio()` return `Any`, so the caller has no "
            "field names, no autocomplete and no `mypy` protection on the response.",
            "",
            "| # | Operation | Generated at | JSON Pointer | What is missing | Fix |",
            "|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(sorted(ops, key=lambda x: x.symbol), 1):
            lines.append(f"| {i} | `{r.symbol}` | `{r.where}` | `{r.pointer}` | {r.defect} | {r.fix} |")
        lines.append("")

    if models:
        lines += [
            "## Model attributes typed `Any`",
            "",
            "| # | Attribute | Generated type | Reached from | JSON Pointer | What is missing | Fix |",
            "|---|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(sorted(models, key=lambda x: (x.symbol,)), 1):
            reached = ", ".join(f"`{o}`" for o in r.operations[:4]) or "_(unreferenced)_"
            if len(r.operations) > 4:
                reached += f" _(+{len(r.operations) - 4} more)_"
            # `attribute_models` may match several pointers and formats them itself.
            lines.append(f"| {i} | `{r.symbol}` | `{r.annotation}` | {reached} | {r.pointer} | {r.defect} | {r.fix} |")
        lines.append("")

    if artifacts:
        lines += [
            "## Not a spec defect — body-less responses",
            "",
            "These operations declare a status that is defined to carry no body, which "
            "is correct. They are listed so the gate's arithmetic is auditable, and they "
            "do not fail the build.",
            "",
            "| # | Operation | Generated at | Declared |",
            "|---|---|---|---|",
        ]
        for i, r in enumerate(sorted(artifacts, key=lambda x: x.symbol), 1):
            lines.append(f"| {i} | `{r.symbol}` | `{r.where}` | `{r.pointer}` |")
        lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", help="the OpenAPI document the client was generated from")
    parser.add_argument("--package", default="mangools", help="generated package directory")
    parser.add_argument("--md", dest="md_out", help="write the Markdown report here")
    parser.add_argument("--json", dest="json_out", help="write the JSON report here")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="write the report and exit 0 (used by scripts/generate.sh; CI runs the check without it)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)

    auditor = Auditor(spec)
    auditor.run()
    findings_by_pointer = {f.pointer: f.as_dict() for f in auditor.findings if f.code in ANY_PRODUCING_CODES}

    rows = attribute_models(
        spec, scan_models(args.package), findings_by_pointer, auditor.schema_to_ops
    ) + attribute_endpoints(spec, scan_endpoints(args.package))

    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(rows, args.spec, args.package))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump([r.as_dict() for r in rows], fh, indent=2)
            fh.write("\n")

    unattributed = [r for r in rows if not r.attributed]
    blocking = [r for r in rows if r.kind != "generator"]
    for r in sorted(rows, key=lambda x: (x.kind, x.symbol)):
        print(f"{r.where}: {r.kind} `{r.symbol}` is `{r.annotation}` <- {r.pointer}")
    print(
        f"\n{len(blocking)} spec-derived Any "
        f"({sum(1 for r in rows if r.kind == 'operation')} operations, "
        f"{sum(1 for r in rows if r.kind == 'model')} model attributes); "
        f"{sum(1 for r in rows if r.kind == 'generator')} body-less responses ignored"
        + (f"; {len(unattributed)} UNATTRIBUTED" if unattributed else ""),
        file=sys.stderr,
    )
    if args.report_only:
        return 0
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
