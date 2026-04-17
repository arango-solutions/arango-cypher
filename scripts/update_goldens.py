"""Surgically update ``expected.aql`` for the listed corpus cases.

Re-runs the translator and rewrites *only* the AQL block, preserving all
surrounding YAML formatting (block-literal vs quoted strings, indentation,
key order, comments, blank lines).

Usage:
    python scripts/update_goldens.py CXXX [CYYY ...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arango_cypher.api import translate  # noqa: E402
from tests.helpers.mapping_fixtures import mapping_bundle_for  # noqa: E402

CASES_DIR = ROOT / "tests" / "fixtures" / "cases"


def _detect_indent(case_block: str) -> int:
    """Return the column count where case fields begin (e.g. 2 or 4)."""
    for line in case_block.splitlines():
        m = re.match(r"^(\s*)\S", line)
        if m and not line.lstrip().startswith("- "):
            return len(m.group(1))
    return 2


_PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./@-]*$")
_RESERVED_PLAIN = {
    "y", "Y", "yes", "Yes", "YES", "n", "N", "no", "No", "NO",
    "true", "True", "TRUE", "false", "False", "FALSE",
    "on", "On", "ON", "off", "Off", "OFF", "null", "Null", "NULL", "~", "",
}


def _format_bind_value(value: object) -> str:
    """Render a bind-var value as YAML, preferring plain scalars for cleanliness."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if (
            value
            and value not in _RESERVED_PLAIN
            and _PLAIN_SCALAR_RE.match(value)
        ):
            return value
        return yaml.safe_dump(value, default_style='"').strip()
    return yaml.safe_dump(value, default_flow_style=True).strip()


def _format_bind_key(key: str) -> str:
    if any(c in key for c in "@:#&*!|>'\"%@`") or key.startswith("@"):
        return f'"{key}"'
    return key


def _replace_case_aql(
    text: str,
    case_id: str,
    new_aql: str,
    new_bind_vars: dict[str, object],
) -> tuple[str, bool]:
    """Locate ``- id: <case_id>`` and replace its ``aql:`` and ``bind_vars`` blocks.

    Supports both ``aql: |`` block-literal and ``aql: "..."`` quoted styles
    by replacing with the same form found.
    """
    case_re = re.compile(
        rf"(^[ \t]*-\s+id:\s*{re.escape(case_id)}\b.*?)(?=^[ \t]*-\s+id:|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = case_re.search(text)
    if not m:
        return text, False
    case_block = m.group(1)

    field_indent = _detect_indent(case_block)
    aql_indent = field_indent + 2  # under "expected:"

    block_re = re.compile(
        rf"^([ \t]{{{aql_indent}}}aql:\s*\|[+-]?\d*\s*\n)((?:^[ \t]{{{aql_indent + 2},}}.*\n?|^\s*\n)*)",
        re.MULTILINE,
    )
    bm = block_re.search(case_block)
    if bm:
        body_indent = " " * (aql_indent + 2)
        body = "".join(body_indent + line + "\n" for line in new_aql.splitlines())
        new_block = bm.group(1) + body
        case_block = case_block[: bm.start()] + new_block + case_block[bm.end():]
    else:
        quoted_re = re.compile(
            rf"^([ \t]{{{aql_indent}}}aql:\s*)(\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*')(\s*\n)",
            re.MULTILINE,
        )
        qm = quoted_re.search(case_block)
        if not qm:
            return text, False
        escaped = (
            new_aql.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")
        )
        if not escaped.endswith("\\n"):
            escaped += "\\n"
        case_block = (
            case_block[: qm.start()]
            + qm.group(1) + f"\"{escaped}\"" + qm.group(3)
            + case_block[qm.end():]
        )

    # Replace the bind_vars block (mapping under expected:).
    bv_re = re.compile(
        rf"(^[ \t]{{{aql_indent}}}bind_vars:\s*\n)((?:^[ \t]{{{aql_indent + 2},}}.*\n?|^\s*\n)*)",
        re.MULTILINE,
    )
    bvm = bv_re.search(case_block)
    if bvm:
        item_indent = " " * (aql_indent + 2)
        body_lines = [
            f"{item_indent}{_format_bind_key(k)}: {_format_bind_value(v)}\n"
            for k, v in new_bind_vars.items()
        ]
        body = "".join(body_lines)
        case_block = case_block[: bvm.start()] + bvm.group(1) + body + case_block[bvm.end():]

    return text[: m.start()] + case_block + text[m.end():], True


def update_case_ids(case_ids: set[str]) -> None:
    remaining = set(case_ids)
    for path in sorted(CASES_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
            continue

        for case in raw["cases"]:
            if not isinstance(case, dict):
                continue
            cid = str(case.get("id") or "").strip()
            if cid not in remaining:
                continue
            mapping = mapping_bundle_for(str(case.get("mapping_fixture") or "").strip())
            params = case.get("params") if isinstance(case.get("params"), dict) else {}
            out = translate(str(case.get("cypher") or ""), mapping=mapping, params=params)
            new_aql = out.aql
            if not new_aql.endswith("\n"):
                new_aql += "\n"
            new_text, ok = _replace_case_aql(text, cid, new_aql, dict(out.bind_vars))
            if ok:
                text = new_text
                print(f"updated {cid} in {path.name}")
                remaining.discard(cid)
            else:
                print(f"WARNING: could not locate aql block for {cid} in {path.name}")

        if text != path.read_text(encoding="utf-8"):
            path.write_text(text, encoding="utf-8")

    if remaining:
        print(f"WARNING: case ids not found: {sorted(remaining)}")


if __name__ == "__main__":
    ids = set(sys.argv[1:])
    if not ids:
        print("usage: update_goldens.py CXXX [CYYY ...]")
        sys.exit(2)
    update_case_ids(ids)
