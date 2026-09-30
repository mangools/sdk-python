#!/usr/bin/env bash
# Generate the `mangools/` package from `openapi.json`, which scripts/fetch_spec.py downloads.
#
# VERSION sets the package version and defaults to 0.0.0.dev0; nothing commits it.
set -euo pipefail

cd "$(dirname "$0")/.."

SPEC=${SPEC:-openapi.json}
PKG=mangools
VERSION=${VERSION:-0.0.0.dev0}

[ -f "$SPEC" ] || {
	echo "$SPEC is missing: run python scripts/fetch_spec.py first" >&2
	exit 1
}
command -v openapi-python-client >/dev/null || {
	echo "openapi-python-client is not on PATH: pip install -r requirements-dev.txt" >&2
	exit 1
}
command -v ruff >/dev/null || {
	echo "ruff is not on PATH; the generator's format post-hook would be skipped: pip install -r requirements-dev.txt" >&2
	exit 1
}

mkdir -p build
{
	cat openapi-python-client.yaml
	printf '\npackage_version_override: "%s"\n' "$VERSION"
} >build/generator.yaml

echo "== 1/5 generate $VERSION"
rm -rf "$PKG"
openapi-python-client generate \
	--path "$SPEC" \
	--meta none \
	--output-path "$PKG" \
	--config build/generator.yaml \
	--custom-template-path templates \
	--overwrite \
	--fail-on-warning

echo "== 2/5 spec-derived constants"
python scripts/emit_spec_constants.py "$SPEC" "$PKG/_spec.py"

echo "== 3/5 PEP 561 marker"
printf '' >"$PKG/py.typed"

echo "== 4/5 format the file written after the generator's post-hooks"
ruff check "$PKG/_spec.py" --fix --extend-select=I
ruff format "$PKG/_spec.py"

echo "== 5/5 spec gap analysis -> SPEC_GAPS.md, spec-derived Any -> TYPE_GAPS.md"
python scripts/spec_audit.py "$SPEC" --json build/spec-audit.json --md SPEC_GAPS.md
python scripts/check_spec_any.py "$SPEC" --package "$PKG" --md TYPE_GAPS.md \
	--json build/type-gaps.json --report-only >/dev/null

echo
echo "generated $(find "$PKG" -name '*.py' | wc -l | tr -d ' ') modules"
