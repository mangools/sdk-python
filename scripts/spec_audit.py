#!/usr/bin/env python3
"""Audit the Mangools OpenAPI document for codegen-blocking and typing gaps.

Emits a machine-readable JSON report and a Markdown summary. Every finding
carries the JSON Pointer where it lives, the operations that reach it, and the
concrete schema edit that would resolve it in the OpenAPI document.

Usage:
    python scripts/spec_audit.py openapi.json --json build/spec-audit.json --md SPEC_GAPS.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
VALID_OAS30_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})
# The only key that is not a sibling of `$ref`. OAS 3.0.x ignores every other key
# next to a `$ref`, so each of them silently loses its meaning.
REF_SIBLING_IGNORED = frozenset({"$ref"})

SEVERITY_ORDER = {"blocker": 0, "gap": 1, "bug": 2, "info": 3}

# `paid`/`free` classify billing, not product area. Everything else the
# document declares at the root is a product-area ("tool") tag.
BILLING_TAGS = frozenset({"paid", "free"})


class Finding:
    __slots__ = ("code", "severity", "pointer", "detail", "fix", "operations", "extra")

    def __init__(
        self,
        code: str,
        severity: str,
        pointer: str,
        detail: str,
        fix: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.severity = severity
        self.pointer = pointer
        self.detail = detail
        self.fix = fix
        self.operations: list[str] = []
        self.extra = extra or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "pointer": self.pointer,
            "detail": self.detail,
            "fix": self.fix,
            "operations": self.operations,
            **self.extra,
        }


def esc(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def ptr(*parts: Any) -> str:
    return "/" + "/".join(esc(str(p)) for p in parts)


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def is_schema_container(node: Any) -> bool:
    return isinstance(node, dict)


class Auditor:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.findings: list[Finding] = []
        self.schema_refs: dict[str, set[str]] = defaultdict(set)
        self.pointer_to_ops: dict[str, set[str]] = defaultdict(set)
        self.operations: list[tuple[str, str, str]] = []
        self.response_component_refs: dict[str, set[str]] = defaultdict(set)

    # ---------------------------------------------------------------- helpers

    def add(
        self,
        code: str,
        severity: str,
        pointer: str,
        detail: str,
        fix: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.findings.append(Finding(code, severity, pointer, detail, fix, extra))

    # ------------------------------------------------------------ ref mapping

    def collect_refs(self, node: Any, owner: str) -> None:
        """Record every `#/components/schemas/X` reachable from `owner`."""
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                self.schema_refs[owner].add(ref.rsplit("/", 1)[-1])
            for value in node.values():
                self.collect_refs(value, owner)
        elif isinstance(node, list):
            for item in node:
                self.collect_refs(item, owner)

    @staticmethod
    def referenced_response_names(op: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        for resp in (op.get("responses") or {}).values():
            ref = resp.get("$ref") if isinstance(resp, dict) else None
            if isinstance(ref, str) and ref.startswith("#/components/responses/"):
                names.add(ref.rsplit("/", 1)[-1])
        return names

    def build_operation_index(self) -> None:
        """Map every component schema to the operations that can reach it."""
        schemas = self.spec.get("components", {}).get("schemas", {})
        direct: dict[str, set[str]] = defaultdict(set)
        for name, schema in schemas.items():
            self.collect_refs(schema, f"schema:{name}")
        for name in schemas:
            direct[name] = set(self.schema_refs.get(f"schema:{name}", set()))

        # An operation reaches a schema through `components.responses` too, so a
        # shared error response has to be an edge in the graph rather than a leaf.
        component_responses = self.spec.get("components", {}).get("responses") or {}
        for name, resp in component_responses.items():
            self.collect_refs(resp, f"response:{name}")

        op_direct: dict[str, set[str]] = defaultdict(set)
        for path, item in (self.spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                    continue
                label = f"{method.upper()} {path}"
                op_id = op.get("operationId")
                if op_id:
                    label = f"{label} ({op_id})"
                self.operations.append((method.upper(), path, op.get("operationId") or ""))
                self.collect_refs(op, f"op:{label}")
                reachable = set(self.schema_refs.get(f"op:{label}", set()))
                for response_name in self.referenced_response_names(op):
                    reachable |= self.schema_refs.get(f"response:{response_name}", set())
                op_direct[label] = reachable

        # transitive closure schema -> schema
        closure: dict[str, set[str]] = {}

        def resolve(name: str, seen: set[str]) -> set[str]:
            if name in closure:
                return closure[name]
            if name in seen:
                return set()
            seen = seen | {name}
            out: set[str] = set(direct.get(name, set()))
            for child in list(out):
                out |= resolve(child, seen)
            closure[name] = out
            return out

        for name in schemas:
            resolve(name, set())

        self.schema_to_ops: dict[str, set[str]] = defaultdict(set)
        for label, names in op_direct.items():
            reach = set(names)
            for name in names:
                reach |= closure.get(name, set())
            for name in reach:
                self.schema_to_ops[name].add(label)

    def attribute(self) -> None:
        """Attach operation labels to every finding."""
        for finding in self.findings:
            pointer = finding.pointer
            if pointer.startswith("/components/schemas/"):
                name = pointer.split("/")[3].replace("~1", "/").replace("~0", "~")
                finding.operations = sorted(self.schema_to_ops.get(name, set()))
            elif pointer.startswith("/components/responses/"):
                name = pointer.split("/")[3].replace("~1", "/").replace("~0", "~")
                finding.operations = sorted(self.response_component_refs.get(name, set()))
            elif pointer.startswith("/paths/"):
                parts = pointer.split("/")
                path = parts[2].replace("~1", "/").replace("~0", "~")
                method = parts[3].upper() if len(parts) > 3 else ""
                if method.lower() in HTTP_METHODS:
                    op = (self.spec["paths"][path] or {}).get(method.lower(), {})
                    op_id = op.get("operationId") if isinstance(op, dict) else None
                    label = f"{method} {path}"
                    finding.operations = [f"{label} ({op_id})" if op_id else label]
                else:
                    finding.operations = [f"(path-level) {path}"]

    # ------------------------------------------------------------ schema walk

    def walk_schema(self, node: Any, pointer: str, *, in_items: bool = False) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                self.walk_schema(item, pointer + ptr(i))
            return
        if not isinstance(node, dict):
            return

        if "$ref" in node:
            extra = sorted(k for k in node if k not in REF_SIBLING_IGNORED)
            if extra:
                self.add(
                    "BLOCKER_REF_SIBLINGS",
                    "blocker",
                    pointer,
                    "`$ref` has sibling keys "
                    + ", ".join(f"`{k}`" for k in extra)
                    + ". OAS 3.0.x ignores every sibling of `$ref`, so these are silently "
                    "dropped; OpenAPI Generator rejects the document outright.",
                    "Delete the sibling keys, or wrap the `$ref` in "
                    "`allOf: [{$ref: ...}]` and put the siblings next to the `allOf`.",
                    {"siblings": extra},
                )
            return

        schema_type = node.get("type")
        has_composition = any(k in node for k in ("allOf", "oneOf", "anyOf", "not"))
        has_enum = "enum" in node

        if "type" in node:
            if isinstance(schema_type, list):
                self.add(
                    "BLOCKER_INVALID_TYPE",
                    "blocker",
                    pointer + "/type",
                    f"`type` is a list ({schema_type!r}). OAS 3.0.x allows exactly one "
                    "type string; type arrays are OAS 3.1 syntax.",
                    "Use a single type plus `nullable: true`, or an `oneOf`.",
                    {"value": schema_type},
                )
            elif schema_type not in VALID_OAS30_TYPES:
                self.add(
                    "BLOCKER_INVALID_TYPE",
                    "blocker",
                    pointer + "/type",
                    f"`type` is {schema_type!r}, which is not one of "
                    f"{sorted(VALID_OAS30_TYPES)}. Both OpenAPI Generator and "
                    "openapi-python-client abort parsing on this.",
                    "Replace with the real JSON type. If the value is a keyed map, use "
                    "`type: object` + `additionalProperties: {<value schema>}`.",
                    {"value": schema_type, "example": node.get("example")},
                )

        if schema_type == "array":
            if "items" not in node:
                self.add(
                    "BLOCKER_ARRAY_NO_ITEMS",
                    "blocker",
                    pointer,
                    "`type: array` with no `items`. The document is not a valid OAS 3.0 "
                    "document; OpenAPI Generator fails validation and defaults the "
                    "element type to `String`.",
                    "Add `items` with the real element schema (extract a named component if the element is an object).",
                    {"example": node.get("example")},
                )
            elif node.get("items") == {}:
                self.add(
                    "GAP_EMPTY_ITEMS",
                    "gap",
                    pointer + "/items",
                    "`items: {}` is a free-form schema, so the element type generates as `Any`.",
                    "Replace `{}` with the real element schema.",
                    {"example": node.get("example")},
                )

        if schema_type == "object":
            has_props = bool(node.get("properties"))
            ap = node.get("additionalProperties")
            if not has_props and not has_composition and ap is None:
                self.add(
                    "GAP_FREEFORM_OBJECT",
                    "gap",
                    pointer,
                    "`type: object` with neither `properties` nor `additionalProperties`. "
                    "Generates as an untyped free-form object (`Any` payload).",
                    "Declare `properties` (+ `required`) for a fixed shape, or "
                    "`additionalProperties: {<value schema>}` for a keyed map.",
                    {"example": node.get("example")},
                )
            elif ap is True or ap == {}:
                self.add(
                    "GAP_ADDITIONAL_PROPS_FREEFORM",
                    "gap",
                    pointer + "/additionalProperties",
                    "`additionalProperties` is free-form, so map values generate as `Any`.",
                    "Give `additionalProperties` an explicit value schema.",
                )

        # A one-member `$ref` composition collapses to the referenced model.
        # openapi-python-client keeps a `nullable: true` that sits alone beside the
        # composition, but drops it when a `type` sits there too, so the generated
        # attribute has no `None` arm and `from_dict` raises `TypeError` on the null
        # the document promises. The `type` carries nothing here — the `$ref` already
        # fixes the shape — so deleting it is lossless.
        if node.get("nullable") is True and "type" in node:
            for kw in ("allOf", "oneOf", "anyOf"):
                members = node.get(kw)
                if (
                    isinstance(members, list)
                    and len(members) == 1
                    and isinstance(members[0], dict)
                    and "$ref" in members[0]
                ):
                    self.add(
                        "BUG_NULLABLE_DROPPED",
                        "bug",
                        pointer + "/type",
                        f"`nullable: true` beside a single-member `{kw}` and a redundant "
                        f"`type: {schema_type}`. openapi-python-client generates the attribute "
                        "without its `None` arm, and the client raises `TypeError` when the "
                        "field arrives null.",
                        f"Delete the `type` sibling. `{kw}: [{{$ref: ...}}]` + `nullable: true` "
                        "alone generates the null arm correctly.",
                        {"composition": kw, "ref": members[0]["$ref"]},
                    )

        if (
            "type" not in node
            and not has_composition
            and not has_enum
            and "properties" not in node
            and "items" not in node
            and node
        ):
            # A schema object carrying only annotations (example/description) is
            # a free-form schema: it types as Any.
            informative = {k for k in node} - {
                "description",
                "example",
                "examples",
                "title",
                "deprecated",
                "readOnly",
                "writeOnly",
                "externalDocs",
                "xml",
                "default",
                "nullable",
            }
            if not informative:
                self.add(
                    "GAP_UNTYPED_SCHEMA",
                    "gap",
                    pointer,
                    "Schema declares no `type`, `$ref`, `enum` or composition — only annotations. Generates as `Any`.",
                    "Add the real `type` (and `properties`/`items` where applicable).",
                    {"keys": sorted(node), "example": node.get("example")},
                )

        # example/type coherence
        if isinstance(schema_type, str) and "example" in node:
            actual = json_type_name(node["example"])
            ok = actual == schema_type
            if schema_type == "number" and actual == "integer":
                ok = True
            if node.get("nullable") and actual == "null":
                ok = True
            if actual == "null":
                ok = ok or bool(node.get("nullable"))
            if not ok and actual != "null":
                self.add(
                    "BUG_EXAMPLE_TYPE_MISMATCH",
                    "bug",
                    pointer + "/example",
                    f"`type: {schema_type}` but the example is a JSON {actual}. One of the "
                    "two is wrong, and the example is what humans copy.",
                    "Fix whichever is wrong. If the example is right, the declared type "
                    "must change (and the generated model type changes with it).",
                    {"declared": schema_type, "example_type": actual},
                )

        for key in ("properties",):
            block = node.get(key)
            if isinstance(block, dict):
                for name, sub in block.items():
                    self.walk_schema(sub, pointer + ptr(key, name))

        for key in ("items", "additionalProperties", "not"):
            if key in node and isinstance(node[key], dict):
                self.walk_schema(node[key], pointer + ptr(key), in_items=key == "items")

        for key in ("allOf", "oneOf", "anyOf"):
            block = node.get(key)
            if isinstance(block, list):
                for i, sub in enumerate(block):
                    self.walk_schema(sub, pointer + ptr(key, i))

    # --------------------------------------------------------- operation walk

    def walk_operations(self) -> None:
        seen_ids: dict[str, list[str]] = defaultdict(list)
        for path, item in (self.spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            base = ptr("paths", path)
            for name, params in (("parameters", item.get("parameters")),):
                if isinstance(params, list):
                    self.walk_parameters(params, base + ptr(name))
            for method, op in item.items():
                if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                    continue
                op_ptr = base + ptr(method)
                op_id = op.get("operationId")
                if not op_id:
                    self.add(
                        "GAP_NO_OPERATION_ID",
                        "gap",
                        op_ptr,
                        "No `operationId`. Generators fall back to a name derived from the "
                        "path+method, which changes whenever the path changes.",
                        "Add a stable `operationId`.",
                    )
                else:
                    seen_ids[op_id].append(f"{method.upper()} {path}")

                for key in ("produces", "consumes", "schemes"):
                    if key in op:
                        self.add(
                            "BUG_SWAGGER2_KEYWORD",
                            "bug",
                            op_ptr + ptr(key),
                            f"`{key}` is Swagger 2.0 syntax and is ignored by OAS 3.0 parsers.",
                            "Delete it; media types belong in `requestBody.content` / `responses.*.content`.",
                        )

                self.walk_parameters(op.get("parameters"), op_ptr + "/parameters")
                self.walk_request_body(op.get("requestBody"), op_ptr + "/requestBody", method, path)
                self.walk_responses(
                    op.get("responses"),
                    op_ptr + "/responses",
                    operation=f"{method.upper()} {path}" + (f" ({op_id})" if op_id else ""),
                )

                if "security" not in op and "security" not in self.spec:
                    self.add(
                        "GAP_NO_SECURITY",
                        "info",
                        op_ptr,
                        "Operation declares no `security` and the document has no "
                        "top-level `security`, so generated clients treat it as public.",
                        "Add `security: [{ApiKeyAuth: []}]` per operation, or once at the document root.",
                    )

        for op_id, where in seen_ids.items():
            if len(where) > 1:
                self.add(
                    "BUG_DUPLICATE_OPERATION_ID",
                    "bug",
                    ptr("paths"),
                    f"`operationId` `{op_id}` is used by {len(where)} operations: " + ", ".join(where),
                    "operationIds must be unique; generators derive function names from "
                    "them and will collide or silently drop one.",
                )

    def walk_parameters(self, params: Any, pointer: str) -> None:
        if not isinstance(params, list):
            return
        for i, param in enumerate(params):
            if not isinstance(param, dict):
                continue
            p_ptr = pointer + ptr(i)
            if param.get("in") == "body":
                self.add(
                    "BUG_SWAGGER2_BODY_PARAM",
                    "bug",
                    p_ptr,
                    "`in: body` is Swagger 2.0 syntax. OAS 3.0 parsers ignore it, so the "
                    "request body vanishes from the generated client.",
                    "Convert to `requestBody: {content: {application/json: {schema: ...}}}`.",
                )
                continue
            if "$ref" in param:
                continue
            if "schema" not in param and "content" not in param:
                self.add(
                    "BUG_PARAM_NO_SCHEMA",
                    "bug",
                    p_ptr,
                    f"Parameter `{param.get('name')}` has no `schema`. OAS 3.0 requires "
                    "one; a bare `type` key is Swagger 2.0 syntax.",
                    "Wrap the type in `schema: {type: ...}`.",
                    {"name": param.get("name"), "keys": sorted(param)},
                )
            elif isinstance(param.get("schema"), dict):
                self.walk_schema(param["schema"], p_ptr + "/schema")

    def walk_request_body(self, body: Any, pointer: str, method: str, path: str) -> None:
        if not isinstance(body, dict):
            return
        content = body.get("content")
        if not isinstance(content, dict) or not content:
            self.add(
                "GAP_REQUEST_BODY_NO_CONTENT",
                "gap",
                pointer,
                "`requestBody` has no `content`, so the generated client sends nothing.",
                "Add `content: {application/json: {schema: ...}}`.",
            )
            return
        for media, media_obj in content.items():
            m_ptr = pointer + ptr("content", media)
            if not isinstance(media_obj, dict) or "schema" not in media_obj:
                self.add(
                    "GAP_REQUEST_BODY_NO_SCHEMA",
                    "gap",
                    m_ptr,
                    f"`{method.upper()} {path}` request body for `{media}` has no "
                    "`schema`, so the body generates as an untyped payload.",
                    "Add an explicit request schema with `required`, types and defaults.",
                )
            else:
                self.walk_schema(media_obj["schema"], m_ptr + "/schema")

    def walk_responses(self, responses: Any, pointer: str, *, operation: str | None = None) -> None:
        if not isinstance(responses, dict):
            return
        for status, resp in responses.items():
            r_ptr = pointer + ptr(status)
            if not isinstance(resp, dict):
                continue
            ref = resp.get("$ref")
            if isinstance(ref, str):
                # The response body lives in `components.responses`, so any defect
                # in it belongs there and is reported once rather than once per
                # referring operation.
                if operation is not None and ref.startswith("#/components/responses/"):
                    self.response_component_refs[ref.rsplit("/", 1)[-1]].add(operation)
                continue
            if "schema" in resp and "content" not in resp:
                self.add(
                    "BUG_SWAGGER2_RESPONSE_SCHEMA",
                    "bug",
                    r_ptr + "/schema",
                    "`schema` sits directly under the response object. That is Swagger 2.0 "
                    "syntax; OAS 3.0 parsers ignore it and the response types as `None`.",
                    "Move it to `content: {application/json: {schema: ...}}`.",
                )
                continue
            content = resp.get("content")
            if status == "204":
                if content:
                    self.add(
                        "BUG_204_WITH_BODY",
                        "bug",
                        r_ptr + "/content",
                        "`204 No Content` declares a response body. Generated clients that "
                        "honour 204 never parse it; clients that parse it break on the "
                        "empty body.",
                        "Either change the status to `200`/`201` and keep the body, or drop the body.",
                    )
                continue
            if not content:
                if str(status).startswith(("2", "default", "4", "5")):
                    self.add(
                        "GAP_RESPONSE_NO_CONTENT",
                        "gap",
                        r_ptr,
                        f"Response `{status}` declares no `content`, so it generates as "
                        "`None` and the caller gets nothing typed back.",
                        "Add `content: {application/json: {schema: ...}}` (error responses "
                        "should reference a shared error envelope).",
                    )
                continue
            for media, media_obj in content.items():
                m_ptr = r_ptr + ptr("content", media)
                if not isinstance(media_obj, dict) or "schema" not in media_obj:
                    self.add(
                        "GAP_RESPONSE_NO_SCHEMA",
                        "gap",
                        m_ptr,
                        f"Response `{status}` for `{media}` has no `schema`; the payload generates as `Any`.",
                        "Add the response schema (reference a named component).",
                    )
                else:
                    self.walk_schema(media_obj["schema"], m_ptr + "/schema")

    def walk_component_responses(self) -> None:
        for name, resp in (self.spec.get("components", {}).get("responses") or {}).items():
            if not isinstance(resp, dict):
                continue
            c_ptr = ptr("components", "responses", name)
            content = resp.get("content")
            if not content:
                self.add(
                    "GAP_RESPONSE_NO_CONTENT",
                    "gap",
                    c_ptr,
                    f"Shared response `{name}` declares no `content`, so it generates as "
                    "`None` and the caller gets nothing typed back.",
                    "Add `content: {application/json: {schema: ...}}` (error responses "
                    "should reference a shared error envelope).",
                )
                continue
            for media, media_obj in content.items():
                m_ptr = c_ptr + ptr("content", media)
                if not isinstance(media_obj, dict) or "schema" not in media_obj:
                    self.add(
                        "GAP_RESPONSE_NO_SCHEMA",
                        "gap",
                        m_ptr,
                        f"Shared response `{name}` for `{media}` has no `schema`; the payload generates as `Any`.",
                        "Add the response schema (reference a named component).",
                    )
                else:
                    self.walk_schema(media_obj["schema"], m_ptr + "/schema")

    # ------------------------------------------------------------------- main

    def run(self) -> None:
        self.build_operation_index()
        schemas = self.spec.get("components", {}).get("schemas", {})
        for name, schema in schemas.items():
            self.walk_schema(schema, ptr("components", "schemas", name))
        self.walk_operations()
        self.walk_component_responses()
        self.check_error_envelope()
        self.check_tags()
        self.attribute()

    def check_tags(self) -> None:
        declared = {t.get("name") for t in (self.spec.get("tags") or []) if isinstance(t, dict)}
        tool_tags = declared - BILLING_TAGS
        undeclared: dict[str, list[str]] = defaultdict(list)
        for path, item in (self.spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                    continue
                tags = op.get("tags") or []
                op_ptr = ptr("paths", path, method) + "/tags"
                for tag in tags:
                    if tag not in declared:
                        undeclared[tag].append(f"{method.upper()} {path}")
                if tags and tags[0] in BILLING_TAGS and any(t in tool_tags for t in tags):
                    tool = next(t for t in tags if t in tool_tags)
                    self.add(
                        "BUG_TAG_ORDER_BILLING_FIRST",
                        "bug",
                        op_ptr,
                        f"`tags[0]` is the billing tag `{tags[0]}` while the product tag "
                        f"`{tool}` sits at index {tags.index(tool)}. Every generator that "
                        "groups by the first tag (openapi-python-client, OpenAPI "
                        "Generator, openapi-to-postman, Fumadocs) files this operation "
                        f"under `{tags[0]}` instead of `{tool}`.",
                        "Put the product tag first and the billing tag second, "
                        f"i.e. `tags: [{tool}, {tags[0]}]`. Tag order is load-bearing for "
                        "codegen even though the OpenAPI spec calls it unordered.",
                        {"tags": list(tags)},
                    )
        for tag, where in sorted(undeclared.items()):
            self.add(
                "BUG_UNDECLARED_TAG",
                "bug",
                ptr("tags"),
                f"Tag `{tag}` is used by {len(where)} operation(s) but is not declared in "
                "the root `tags` array, so it has no name, description or external docs.",
                f"Declare it at the root, or remove it from the operations ({', '.join(where)}).",
                {"tag": tag, "used_by": where},
            )

    def check_error_envelope(self) -> None:
        """Every operation should map 4xx/5xx onto one shared error schema."""
        without_errors: list[str] = []
        for path, item in (self.spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                    continue
                responses = op.get("responses") or {}
                if not any(str(s).startswith(("4", "5")) for s in responses):
                    without_errors.append(f"{method.upper()} {path}")
        if without_errors:
            self.add(
                "GAP_NO_ERROR_RESPONSES",
                "gap",
                ptr("paths"),
                f"{len(without_errors)} of {len(self.operations)} operations declare no "
                "`4xx`/`5xx` response at all, so generated clients have no typed error "
                "path and raise a generic transport error instead.",
                "Define one `Error` component (`code`, `message`, `details`, `request_id`) "
                "and reference it from `400/401/403/404/409/429/5XX` on every operation.",
                {"operations_without_error_responses": sorted(without_errors)},
            )


def render_markdown(spec: dict[str, Any], findings: list[Finding], source: str) -> str:
    by_code: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_code[f.code].append(f)

    lines: list[str] = []
    lines.append("# Spec gap analysis — `mangools` Python SDK")
    lines.append("")
    lines.append(
        "Generated by `scripts/spec_audit.py` from `{}` (`openapi: {}`, `info.version: {}`).".format(
            source, spec.get("openapi"), spec.get("info", {}).get("version")
        )
    )
    lines.append("")
    lines.append(
        "Every row below is a place where the OpenAPI document is incomplete or wrong. "
        "`blocker` stops code generation outright; `gap` leaves something undescribed, and a "
        "`gap` that generates as `Any` fails the spec-`Any` CI gate; `bug` is a contract error "
        "that generates silently wrong client code; `info` is advisory."
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Severity | Code | Count |")
    lines.append("|---|---|---|")
    for code in sorted(by_code, key=lambda c: (SEVERITY_ORDER[by_code[c][0].severity], c)):
        group = by_code[code]
        lines.append(f"| `{group[0].severity}` | `{code}` | {len(group)} |")
    lines.append(f"| | **total** | **{len(findings)}** |")
    lines.append("")

    for code in sorted(by_code, key=lambda c: (SEVERITY_ORDER[by_code[c][0].severity], c)):
        group = by_code[code]
        lines.append(f"## `{code}` — {group[0].severity} ({len(group)})")
        lines.append("")
        lines.append(group[0].detail)
        lines.append("")
        lines.append(f"**Fix:** {group[0].fix}")
        lines.append("")
        lines.append("| # | JSON Pointer | Reached from | Notes |")
        lines.append("|---|---|---|---|")
        for i, f in enumerate(sorted(group, key=lambda x: x.pointer), 1):
            ops = ", ".join(f"`{o}`" for o in f.operations[:6]) or "_(unreferenced)_"
            if len(f.operations) > 6:
                ops += f" _(+{len(f.operations) - 6} more)_"
            notes = []
            if "value" in f.extra:
                notes.append(f"value=`{json.dumps(f.extra['value'])}`")
            if "siblings" in f.extra:
                notes.append("siblings=" + ", ".join(f"`{s}`" for s in f.extra["siblings"]))
            if "declared" in f.extra:
                notes.append(f"declared=`{f.extra['declared']}` example=`{f.extra['example_type']}`")
            if "name" in f.extra:
                notes.append(f"name=`{f.extra['name']}`")
            if "example" in f.extra and f.extra["example"] is not None:
                sample = json.dumps(f.extra["example"])
                if len(sample) > 90:
                    sample = sample[:87] + "..."
                notes.append(f"example=`{sample}`")
            lines.append("| {} | `{}` | {} | {} |".format(i, f.pointer, ops, "; ".join(notes) or "—"))
        lines.append("")
        extra_ops = group[0].extra.get("operations_without_error_responses")
        if extra_ops:
            lines.append("<details><summary>Operations with no 4xx/5xx response</summary>")
            lines.append("")
            for op in extra_ops:
                lines.append(f"- `{op}`")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", help="path to the OpenAPI JSON document")
    parser.add_argument("--json", dest="json_out", help="write the JSON report here")
    parser.add_argument("--md", dest="md_out", help="write the Markdown report here")
    parser.add_argument(
        "--fail-on",
        default="",
        help="comma-separated severities that make this exit non-zero",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)

    auditor = Auditor(spec)
    auditor.run()
    findings = sorted(auditor.findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.code, f.pointer))

    if args.json_out:
        payload = {
            "source": args.spec,
            "openapi": spec.get("openapi"),
            "info_version": spec.get("info", {}).get("version"),
            "counts": {sev: sum(1 for f in findings if f.severity == sev) for sev in ("blocker", "bug", "gap", "info")},
            "findings": [f.as_dict() for f in findings],
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=False)
            fh.write("\n")

    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(spec, findings, args.spec))

    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.severity] += 1
    for sev in ("blocker", "bug", "gap", "info"):
        print(f"{sev:8s} {counts[sev]}", file=sys.stderr)

    fail_on = {s.strip() for s in args.fail_on.split(",") if s.strip()}
    return 1 if any(counts[s] for s in fail_on) else 0


if __name__ == "__main__":
    raise SystemExit(main())
