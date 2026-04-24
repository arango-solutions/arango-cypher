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
import logging
import os
import re
import statistics
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from arango_cypher.nl2cypher import (
    LLMProvider,
    nl_to_cypher,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
CORPUS_PATH = EVAL_DIR / "corpus.yml"
CONFIGS_PATH = EVAL_DIR / "configs.yml"
REPORTS_DIR = EVAL_DIR / "reports"

#: Default database name per mapping fixture.  Override via env vars
#: ``NL2CYPHER_EVAL_<FIXTURE>_DB`` (uppercased) for a custom layout —
#: e.g. ``NL2CYPHER_EVAL_MOVIES_PG_DB=my_movies_db``.
_DEFAULT_FIXTURE_DBS: dict[str, str] = {
    "movies_pg": "nl2cypher_eval_movies_pg",
    "northwind_pg": "northwind_cross_test",
}


def _fixture_db_name(fixture: str) -> str | None:
    """Return the database name to use for *fixture*, honoring env overrides.

    Returns ``None`` when no default exists and no override is set —
    the runner then falls back to its no-DB code path for that fixture.
    """
    env_var = f"NL2CYPHER_EVAL_{fixture.upper()}_DB"
    override = os.environ.get(env_var)
    if override:
        return override
    return _DEFAULT_FIXTURE_DBS.get(fixture)


def open_eval_db_handles(
    *,
    fixtures: list[str] | None = None,
) -> dict[str, Any]:
    """Build a ``{fixture: StandardDatabase}`` map from ``ARANGO_*`` env vars.

    Reads ``ARANGO_URL`` / ``ARANGO_USER`` / ``ARANGO_PASS`` and opens
    one connection per fixture against the database resolved by
    :func:`_fixture_db_name`.  Fixtures whose database doesn't exist are
    silently skipped so the runner falls back to no-DB for them — the
    caller can inspect the returned map's keys to see which fixtures
    actually got a live DB.

    Returns ``{}`` when ``ARANGO_URL`` isn't set or python-arango isn't
    importable, so the function is always safe to call.

    *fixtures* defaults to the keys of :data:`_DEFAULT_FIXTURE_DBS` plus
    any env-override-only fixtures discoverable via the corpus.
    """
    url = os.environ.get("ARANGO_URL")
    if not url:
        return {}
    try:
        from arango import ArangoClient
    except ImportError:
        logger.info("python-arango not installed; eval runner falling back to no-DB mode")
        return {}

    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "")
    fixture_list = fixtures if fixtures is not None else list(_DEFAULT_FIXTURE_DBS.keys())

    client = ArangoClient(hosts=url)
    handles: dict[str, Any] = {}
    for fx in fixture_list:
        db_name = _fixture_db_name(fx)
        if not db_name:
            continue
        try:
            db = client.db(db_name, username=user, password=password)
            db.version()
        except Exception as exc:
            logger.info(
                "Eval runner: skipping %s (cannot open DB %r): %s",
                fx,
                db_name,
                exc,
            )
            continue
        handles[fx] = db
    return handles


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
        out.append(
            EvalCase(
                id=str(c.get("id", "")),
                mapping_fixture=str(c.get("mapping_fixture", "")),
                question=str(c.get("question", "")),
                expected_patterns=[str(p) for p in (c.get("expected_patterns") or [])],
                category=str(c.get("category", "baseline")),
            )
        )
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
    db_for_fixture: Mapping[str, Any] | None = None,
) -> CaseResult:
    """Run one eval case and collect metrics.

    The DB used for WP-25.2 entity resolution and WP-25.3 EXPLAIN-grounded
    retry is resolved as follows:

    1. ``db_for_fixture[case.mapping_fixture]`` if present — the
       per-fixture map produced by :func:`open_eval_db_handles`.
    2. ``db`` if non-None — back-compat for tests that pass a single DB.
    3. ``None`` — runner stays in no-DB mode for this case.

    The resolved DB is then passed to :func:`nl_to_cypher` whenever
    *either* ``use_entity_resolution`` *or* ``use_execution_grounded`` is
    set; previously it was gated on ``use_execution_grounded`` alone, so
    the ``few_shot_plus_entity`` config silently skipped WP-25.2.
    """
    mapping = mapping_bundle_for(case.mapping_fixture)
    case_db: Any | None = None
    if db_for_fixture and case.mapping_fixture in db_for_fixture:
        case_db = db_for_fixture[case.mapping_fixture]
    elif db is not None:
        case_db = db

    needs_db = bool(
        config.get("use_execution_grounded") or config.get("use_entity_resolution", True),
    )
    db_for_call = case_db if needs_db else None

    t0 = time.perf_counter()
    try:
        res = nl_to_cypher(
            case.question,
            mapping=mapping,
            use_llm=provider is not None,
            llm_provider=provider,
            use_fewshot=bool(config.get("use_fewshot", True)),
            use_entity_resolution=bool(config.get("use_entity_resolution", True)),
            db=db_for_call,
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
    db_for_fixture: Mapping[str, Any] | None = None,
) -> Report:
    """Run the full corpus under *config* and return a :class:`Report`.

    Pass *db_for_fixture* (typically from :func:`open_eval_db_handles`)
    to engage WP-25.2 entity resolution and/or WP-25.3 EXPLAIN-grounded
    retry per case.  *db* is the legacy single-handle parameter and is
    used as a fallback when no per-fixture handle is registered.
    """
    if cases is None:
        cases = load_corpus()
    results: list[CaseResult] = [
        run_case(
            c,
            provider=provider,
            config=config,
            db=db,
            db_for_fixture=db_for_fixture,
        )
        for c in cases
    ]

    def _rate(key: str) -> float:
        return sum(1 for r in results if getattr(r, key)) / len(results) if results else 0.0

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
    "open_eval_db_handles",
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
    parser.add_argument(
        "--with-db",
        action="store_true",
        help="Open an ArangoDB connection per mapping fixture (driven by "
        "ARANGO_URL / ARANGO_USER / ARANGO_PASS env vars and the "
        "NL2CYPHER_EVAL_<FIXTURE>_DB overrides) so WP-25.2 entity resolution "
        "and WP-25.3 EXPLAIN-grounded retry actually engage.",
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

    db_for_fixture: dict[str, Any] = {}
    if args.with_db:
        db_for_fixture = open_eval_db_handles()
        if db_for_fixture:
            print(
                "Live DB enabled for fixtures: " + ", ".join(sorted(db_for_fixture)),
            )
        else:
            print(
                "--with-db requested but no DB handles opened (check ARANGO_URL / "
                "ARANGO_USER / ARANGO_PASS); falling back to no-DB mode.",
            )

    report = run_eval(config=cfg, provider=provider, db_for_fixture=db_for_fixture)
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
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"Refreshed baseline: {baseline_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
