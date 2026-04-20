from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CorpusCase:
    id: str
    name: str
    mapping_fixture: str
    extensions_enabled: bool
    cypher: str
    params: dict[str, Any]
    expected_aql: str | None
    expected_bind_vars: dict[str, Any]


def iter_case_files(cases_dir: Path) -> list[Path]:
    files = sorted(cases_dir.glob("*.yml"))
    return files


def load_cases_from_file(path: Path) -> list[CorpusCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid corpus YAML (expected object): {path}")
    cases = raw.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"Invalid corpus YAML (missing cases list): {path}")

    out: list[CorpusCase] = []
    for idx, c in enumerate(cases):
        if not isinstance(c, dict):
            raise ValueError(f"Invalid case entry (expected object) at index {idx}: {path}")
        expected = c.get("expected") if isinstance(c.get("expected"), dict) else {}
        params = c.get("params") if isinstance(c.get("params"), dict) else {}

        case = CorpusCase(
            id=str(c.get("id") or "").strip(),
            name=str(c.get("name") or "").strip(),
            mapping_fixture=str(c.get("mapping_fixture") or "").strip(),
            extensions_enabled=bool(c.get("extensions_enabled") or False),
            cypher=str(c.get("cypher") or ""),
            params=dict(params),
            expected_aql=(expected.get("aql") if isinstance(expected.get("aql"), str) else None),
            expected_bind_vars=(expected.get("bind_vars") if isinstance(expected.get("bind_vars"), dict) else {}),
        )
        if not case.id:
            raise ValueError(f"Case missing id at index {idx}: {path}")
        if not case.cypher.strip():
            raise ValueError(f"Case {case.id} missing cypher text: {path}")
        if not case.mapping_fixture:
            raise ValueError(f"Case {case.id} missing mapping_fixture: {path}")
        out.append(case)
    return out


def load_all_cases(cases_dir: Path) -> list[CorpusCase]:
    seen: set[str] = set()
    all_cases: list[CorpusCase] = []
    for f in iter_case_files(cases_dir):
        for c in load_cases_from_file(f):
            if c.id in seen:
                raise ValueError(f"Duplicate case id: {c.id}")
            seen.add(c.id)
            all_cases.append(c)
    return all_cases


def iter_cases(cases_dir: Path) -> Iterable[CorpusCase]:
    return load_all_cases(cases_dir)

