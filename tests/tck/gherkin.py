from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Step:
    keyword: str  # Given/When/Then/And/But
    text: str
    doc_string: str | None = None
    data_table: list[list[str]] | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: list[Step]


@dataclass(frozen=True)
class Feature:
    name: str
    scenarios: list[Scenario]


_PLACEHOLDER_RE = re.compile(r"<([^>]+)>")


def _substitute(template: str, row: dict[str, str]) -> str:
    """Replace <placeholder> tokens with values from a row dict."""

    def _repl(m: re.Match[str]) -> str:
        key = m.group(1)
        return row.get(key, m.group(0))

    return _PLACEHOLDER_RE.sub(_repl, template)


def _expand_outline(
    name: str,
    steps: list[Step],
    examples_tables: list[list[list[str]]],
) -> list[Scenario]:
    """Expand a Scenario Outline into concrete Scenarios using Examples tables."""
    scenarios: list[Scenario] = []
    for table in examples_tables:
        if len(table) < 2:
            continue
        headers = table[0]
        for data_row in table[1:]:
            row_dict = dict(zip(headers, data_row, strict=False))
            label = ", ".join(f"{k}={v}" for k, v in row_dict.items())
            concrete_name = f"{name} [{label}]"
            concrete_steps: list[Step] = []
            for step in steps:
                new_text = _substitute(step.text, row_dict)
                new_doc = _substitute(step.doc_string, row_dict) if step.doc_string is not None else None
                new_table: list[list[str]] | None = None
                if step.data_table is not None:
                    new_table = [[_substitute(cell, row_dict) for cell in row] for row in step.data_table]
                concrete_steps.append(
                    Step(
                        keyword=step.keyword,
                        text=new_text,
                        doc_string=new_doc,
                        data_table=new_table,
                    )
                )
            scenarios.append(Scenario(name=concrete_name, steps=concrete_steps))
    return scenarios


def parse_feature(path: Path) -> Feature:
    """
    Minimal Gherkin parser for the subset used by openCypher TCK feature files.

    Supports: Feature, Scenario, Scenario Outline + Examples,
    Given/When/Then/And/But steps, doc strings (triple quotes), data tables.
    """
    raw_lines = path.read_text(encoding="utf-8").splitlines()

    feature_name: str | None = None
    scenarios: list[Scenario] = []

    cur_scenario_name: str | None = None
    cur_steps: list[Step] = []
    cur_is_outline: bool = False
    cur_examples: list[list[list[str]]] = []

    def _flush_scenario() -> None:
        nonlocal cur_scenario_name, cur_steps, cur_is_outline, cur_examples
        if cur_scenario_name is None:
            return
        if cur_is_outline:
            scenarios.extend(_expand_outline(cur_scenario_name, cur_steps, cur_examples))
        else:
            scenarios.append(Scenario(name=cur_scenario_name, steps=cur_steps))
        cur_scenario_name = None
        cur_steps = []
        cur_is_outline = False
        cur_examples = []

    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i]
        line = raw.strip()

        if not line or line.startswith("#"):
            i += 1
            continue

        if line.startswith("Feature:"):
            feature_name = line[len("Feature:") :].strip() or "Unnamed Feature"
            i += 1
            continue

        if line.startswith("Scenario Outline:") or line.startswith("Scenario Template:"):
            _flush_scenario()
            prefix = "Scenario Outline:" if line.startswith("Scenario Outline:") else "Scenario Template:"
            cur_scenario_name = line[len(prefix) :].strip() or "Unnamed Outline"
            cur_steps = []
            cur_is_outline = True
            cur_examples = []
            i += 1
            continue

        if line.startswith("Scenario:"):
            _flush_scenario()
            cur_scenario_name = line[len("Scenario:") :].strip() or "Unnamed Scenario"
            cur_steps = []
            cur_is_outline = False
            cur_examples = []
            i += 1
            continue

        if line.startswith("Examples:") or line.startswith("Scenarios:"):
            table: list[list[str]] = []
            j = i + 1
            while j < len(raw_lines) and raw_lines[j].lstrip().startswith("|"):
                row = raw_lines[j].strip()
                cells = [c.strip() for c in row.strip("|").split("|")]
                table.append(cells)
                j += 1
            cur_examples.append(table)
            i = j
            continue

        step_kw = None
        for kw in ("Given", "When", "Then", "And", "But"):
            if line.startswith(kw + " ") or line == kw:
                step_kw = kw
                step_txt = line[len(kw) :].strip()
                break
        if step_kw is None:
            i += 1
            continue

        doc: str | None = None
        table_data: list[list[str]] | None = None

        j = i + 1
        if j < len(raw_lines) and raw_lines[j].strip().startswith('"""'):
            j += 1
            parts: list[str] = []
            while j < len(raw_lines) and not raw_lines[j].strip().startswith('"""'):
                parts.append(raw_lines[j])
                j += 1
            doc = "\n".join(parts).strip("\n")
            if j < len(raw_lines):
                j += 1
        elif j < len(raw_lines) and raw_lines[j].lstrip().startswith("|"):
            table_data = []
            while j < len(raw_lines) and raw_lines[j].lstrip().startswith("|"):
                row = raw_lines[j].strip()
                cells = [c.strip() for c in row.strip("|").split("|")]
                table_data.append(cells)
                j += 1

        cur_steps.append(Step(keyword=step_kw, text=step_txt, doc_string=doc, data_table=table_data))
        i = j

    _flush_scenario()

    if feature_name is None:
        feature_name = path.stem

    return Feature(name=feature_name, scenarios=scenarios)
