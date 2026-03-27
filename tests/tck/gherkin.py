from __future__ import annotations

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


def parse_feature(path: Path) -> Feature:
    """
    Minimal Gherkin parser for the subset used by openCypher TCK feature files.

    It supports:
    - Feature:
    - Scenario:
    - Steps: Given/When/Then/And/But
    - Doc strings (triple quotes)
    - Data tables (pipe-separated)
    """
    raw_lines = path.read_text(encoding="utf-8").splitlines()

    feature_name: str | None = None
    scenarios: list[Scenario] = []
    cur_scenario_name: str | None = None
    cur_steps: list[Step] = []

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

        if line.startswith("Scenario:"):
            # flush previous scenario
            if cur_scenario_name is not None:
                scenarios.append(Scenario(name=cur_scenario_name, steps=cur_steps))
            cur_scenario_name = line[len("Scenario:") :].strip() or "Unnamed Scenario"
            cur_steps = []
            i += 1
            continue

        # Step line
        step_kw = None
        for kw in ("Given", "When", "Then", "And", "But"):
            if line.startswith(kw + " "):
                step_kw = kw
                step_txt = line[len(kw) :].strip()
                break
        if step_kw is None:
            i += 1
            continue

        doc: str | None = None
        table: list[list[str]] | None = None

        # Doc string?
        j = i + 1
        if j < len(raw_lines) and raw_lines[j].strip().startswith('"""'):
            j += 1
            parts: list[str] = []
            while j < len(raw_lines) and not raw_lines[j].strip().startswith('"""'):
                parts.append(raw_lines[j])
                j += 1
            doc = "\n".join(parts).strip("\n")
            # consume closing """
            if j < len(raw_lines):
                j += 1
        # Data table?
        elif j < len(raw_lines) and raw_lines[j].lstrip().startswith("|"):
            table = []
            while j < len(raw_lines) and raw_lines[j].lstrip().startswith("|"):
                row = raw_lines[j].strip()
                # split | a | b |
                cells = [c.strip() for c in row.strip("|").split("|")]
                table.append(cells)
                j += 1

        cur_steps.append(Step(keyword=step_kw, text=step_txt, doc_string=doc, data_table=table))
        i = j

    if cur_scenario_name is not None:
        scenarios.append(Scenario(name=cur_scenario_name, steps=cur_steps))

    if feature_name is None:
        feature_name = path.stem

    return Feature(name=feature_name, scenarios=scenarios)

