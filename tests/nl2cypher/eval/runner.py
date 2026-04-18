"""Evaluation harness runner for WP-25.5.

Executes a curated corpus (``corpus.yml``) against one or more named
configurations (``configs.yml``) and collects per-case metrics plus
an aggregate :class:`Report`.  Reports serialize to both Markdown
(human review) and JSON (the regression gate's input format).

The runner is deliberately small and framework-free: it accepts any
``LLMProvider`` so unit tests can drive it with a scripted mock, and
production sweeps can pass a real :class:`~arango_cypher.nl2cypher.OpenAIProvider`.
"""
from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from arango_cypher.nl2cypher import (
    LLMProvider,
    nl_to_cypher,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for

EVAL_DIR = Path(__file__).parent
CORPUS_PATH = EVAL_DIR / "corpus.yml"
CONFIGS_PATH = EVAL_DIR / "configs.yml"
REPORTS_DIR = EVAL_DIR / "reports"


@dataclass
class EvalCase:
    id: str
    mapping_fixture: str
    question: str
    expected_patterns: list[str]
    category: str = "baseline"


@dataclass
class CaseResult:
    id: str
    category: str
    question: str
    cypher: str
    parse_ok: bool
    pattern_match: bool
    explain_ok: bool | None
    row_match: bool | None
    tokens: int
    cached_tokens: int
    retries: int
    latency_ms: float
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    """Aggregate metrics + per-case results for one config run."""

    config_name: str
    generated_at: str
    case_count: int
    results: list[CaseResult] = field(default_factory=list)
    parse_ok_rate: float = 0.0
    pattern_match_rate: float = 0.0
    explain_ok_rate: float | None = None
    row_match_rate: float | None = None
    tokens_mean: float = 0.0
    cached_tokens_mean: float = 0.0
    retries_mean: float = 0.0
    latency_mean_ms: float = 0.0
    by_category: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def to_markdown(self) -> str:
        return _render_markdown(self)


def load_corpus(path: Path = CORPUS_PATH) -> list[EvalCase]:
    """Load evaluation cases from ``corpus.yml``."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = raw.get("cases", []) if isinstance(raw, dict) else []
    out: list[EvalCase] = []
    for c in cases:
        if not isinstance(c, dict):
            continue
        out.append(EvalCase(
            id=str(c.get("id", "")),
            mapping_fixture=str(c.get("mapping_fixture", "")),
            question=str(c.get("question", "")),
            expected_patterns=[str(p) for p in (c.get("expected_patterns") or [])],
            category=str(c.get("category", "baseline")),
        ))
    return out


def load_configs(path: Path = CONFIGS_PATH) -> list[dict[str, Any]]:
    """Load named configurations from ``configs.yml``."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(raw.get("configs", [])) if isinstance(raw, dict) else []


def _pattern_match(cypher: str, patterns: list[str]) -> bool:
    """All declared regex patterns must match the generated Cypher.

    Patterns are anchored in regex-land, not substring; callers embed
    ``(?is)`` to get case-insensitive DOTALL matching where needed.
    Empty pattern lists vacuously match (the case becomes parse-only).
    """
    if not patterns:
        return True
    for pat in patterns:
        try:
            if not re.search(pat, cypher):
                return False
        except re.error:
            return False
    return True


def _parse_ok(cypher: str) -> bool:
    """Return True iff the ANTLR grammar accepts *cypher*."""
    if not cypher:
        return False
    try:
        from arango_cypher.parser import parse_cypher
        parse_cypher(cypher)
        return True
    except Exception:
        return False


def run_case(
    case: EvalCase,
    *,
    provider: LLMProvider | None,
    config: dict[str, Any],
    db: Any | None = None,
) -> CaseResult:
    """Run one eval case and collect metrics."""
    mapping = mapping_bundle_for(case.mapping_fixture)
    t0 = time.perf_counter()
    try:
        res = nl_to_cypher(
            case.question,
            mapping=mapping,
            use_llm=provider is not None,
            llm_provider=provider,
            use_fewshot=bool(config.get("use_fewshot", True)),
            use_entity_resolution=bool(config.get("use_entity_resolution", True)),
            db=db if config.get("use_execution_grounded") else None,
        )
    except Exception as exc:
        return CaseResult(
            id=case.id,
            category=case.category,
            question=case.question,
            cypher="",
            parse_ok=False,
            pattern_match=False,
            explain_ok=None,
            row_match=None,
            tokens=0,
            cached_tokens=0,
            retries=0,
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(exc)[:500],
        )

    latency_ms = (time.perf_counter() - t0) * 1000
    parse_ok = _parse_ok(res.cypher)
    pattern_match = _pattern_match(res.cypher, case.expected_patterns) if parse_ok else False
    return CaseResult(
        id=case.id,
        category=case.category,
        question=case.question,
        cypher=res.cypher,
        parse_ok=parse_ok,
        pattern_match=pattern_match,
        explain_ok=None,
        row_match=None,
        tokens=res.total_tokens,
        cached_tokens=res.cached_tokens,
        retries=res.retries,
        latency_ms=round(latency_ms, 2),
        error="",
    )


def run_eval(
    *,
    config: dict[str, Any],
    provider: LLMProvider | None,
    cases: list[EvalCase] | None = None,
    db: Any | None = None,
) -> Report:
    """Run the full corpus under *config* and return a :class:`Report`."""
    if cases is None:
        cases = load_corpus()
    results: list[CaseResult] = [
        run_case(c, provider=provider, config=config, db=db) for c in cases
    ]

    def _rate(key: str) -> float:
        return (
            sum(1 for r in results if getattr(r, key)) / len(results)
            if results else 0.0
        )

    by_cat: dict[str, dict[str, float]] = {}
    cats = sorted({r.category for r in results})
    for cat in cats:
        sub = [r for r in results if r.category == cat]
        if not sub:
            continue
        by_cat[cat] = {
            "n": len(sub),
            "parse_ok_rate": sum(1 for r in sub if r.parse_ok) / len(sub),
            "pattern_match_rate": sum(1 for r in sub if r.pattern_match) / len(sub),
            "tokens_mean": statistics.mean(r.tokens for r in sub) if sub else 0.0,
            "retries_mean": statistics.mean(r.retries for r in sub) if sub else 0.0,
        }

    report = Report(
        config_name=str(config.get("name", "unnamed")),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        case_count=len(results),
        results=results,
        parse_ok_rate=_rate("parse_ok"),
        pattern_match_rate=_rate("pattern_match"),
        tokens_mean=statistics.mean(r.tokens for r in results) if results else 0.0,
        cached_tokens_mean=statistics.mean(r.cached_tokens for r in results) if results else 0.0,
        retries_mean=statistics.mean(r.retries for r in results) if results else 0.0,
        latency_mean_ms=statistics.mean(r.latency_ms for r in results) if results else 0.0,
        by_category=by_cat,
    )
    return report


def _render_markdown(report: Report) -> str:
    lines: list[str] = [
        f"# NL→Cypher eval report — `{report.config_name}`",
        "",
        f"- Generated: {report.generated_at}",
        f"- Cases: {report.case_count}",
        f"- `parse_ok` rate: **{report.parse_ok_rate:.0%}**",
        f"- `pattern_match` rate: **{report.pattern_match_rate:.0%}**",
        f"- tokens (mean): {report.tokens_mean:.1f}",
        f"- cached_tokens (mean): {report.cached_tokens_mean:.1f}",
        f"- retries (mean): {report.retries_mean:.2f}",
        f"- latency (mean ms): {report.latency_mean_ms:.1f}",
        "",
        "## By category",
        "",
        "| category | n | parse_ok | pattern_match | tokens | retries |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for cat, m in sorted(report.by_category.items()):
        lines.append(
            f"| {cat} | {int(m['n'])} | {m['parse_ok_rate']:.0%} | "
            f"{m['pattern_match_rate']:.0%} | {m['tokens_mean']:.0f} | "
            f"{m['retries_mean']:.2f} |"
        )
    lines.append("")
    lines.append("## Per-case")
    lines.append("")
    lines.append("| id | category | parse | match | retries | tokens | cypher (trunc) |")
    lines.append("|---|---|:-:|:-:|--:|--:|---|")
    for r in report.results:
        cy = (r.cypher or "").replace("\n", " ")[:80]
        lines.append(
            f"| {r.id} | {r.category} | "
            f"{'Y' if r.parse_ok else '.'} | "
            f"{'Y' if r.pattern_match else '.'} | "
            f"{r.retries} | {r.tokens} | `{cy}` |"
        )
    return "\n".join(lines) + "\n"


def save_report(report: Report, *, out_dir: Path = REPORTS_DIR) -> tuple[Path, Path]:
    """Persist a report as both ``<date>-<config>.{json,md}`` files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y%m%d", time.gmtime())
    stem = f"{date}-{report.config_name}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path


__all__ = [
    "CORPUS_PATH",
    "CONFIGS_PATH",
    "REPORTS_DIR",
    "CaseResult",
    "EvalCase",
    "Report",
    "load_configs",
    "load_corpus",
    "run_case",
    "run_eval",
    "save_report",
]


def _main(argv: list[str] | None = None) -> int:
    """Command-line entrypoint for refreshing evaluation reports / baselines.

    Usage:
        python -m tests.nl2cypher.eval.runner --config full
        python -m tests.nl2cypher.eval.runner --config full --baseline
    """
    import argparse

    parser = argparse.ArgumentParser(description="Run the NL→Cypher evaluation harness")
    parser.add_argument(
        "--config",
        default="full",
        help="Name of the config in configs.yml (default: full)",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Overwrite tests/nl2cypher/eval/baseline.json with the fresh report "
        "(summary only, no per-case rows).",
    )
    args = parser.parse_args(argv)

    from arango_cypher.nl2cypher import get_llm_provider

    provider = get_llm_provider()
    if provider is None:
        print(
            "No LLM provider configured. Set OPENAI_API_KEY, OPENROUTER_API_KEY, "
            "or ANTHROPIC_API_KEY (see README)."
        )
        return 2

    cfgs = {c.get("name"): c for c in load_configs()}
    cfg = cfgs.get(args.config)
    if cfg is None:
        print(f"Unknown config {args.config!r}; available: {sorted(cfgs)}")
        return 2

    report = run_eval(config=cfg, provider=provider)
    json_path, md_path = save_report(report)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")

    if args.baseline:
        baseline_path = EVAL_DIR / "baseline.json"
        summary = report.to_dict()
        summary["results"] = []
        summary["_comment"] = (
            "Baseline for the NL->Cypher regression gate. Regenerate with "
            "`python -m tests.nl2cypher.eval.runner --config full --baseline`."
        )
        baseline_path.write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8",
        )
        print(f"Refreshed baseline: {baseline_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
