#!/usr/bin/env python
"""Transpile-coverage harness over an external Text2Cypher(→AQL) corpus.

Runs the deterministic Cypher→AQL transpiler over the ``cypher`` column of the
Neo4j-derived movies dataset (``neo4j/text2cypher-2025v1`` → AQL edition) and
reports:

  1. transpile success rate over real, LLM-emitted Cypher, and
  2. ranked "gap buckets" — the failure reasons that block the most queries,
     each with sample Cypher, so the transpiler backlog is data-driven.

Optionally (``--with-db``) it executes the generated AQL against a local movies
ArangoDB (seeded on demand) and reports an execution smoke: ran-without-error
and returned-rows rates.

The dataset is external and its redistribution license is still pending, so this
script reads it from a path (default
``~/Downloads/dataset_neo4j/aql_cypher_comparison.csv``) and is deliberately NOT
part of the committed test suite or fixtures. Treat the dataset's ``cypher`` +
Neo4j ``results`` columns as the reference; the dataset's own ``aql_query`` is a
machine translation and is not used here.

Usage
-----
  python scripts/eval_text2cypher_corpus.py                     # transpile coverage
  python scripts/eval_text2cypher_corpus.py --show 3            # + 3 samples per gap
  python scripts/eval_text2cypher_corpus.py --feature-report    # + feature correlation
  python scripts/eval_text2cypher_corpus.py --with-db --seed \
      --arango-url http://localhost:8529 --arango-pass openSesame
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Make the repo root importable when run as a plain script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from arango_query_core.errors import CoreError  # noqa: E402

from arango_cypher import translate  # noqa: E402
from tests.helpers.mapping_fixtures import mapping_bundle_for  # noqa: E402

_DEFAULT_DATASET = "~/Downloads/dataset_neo4j/aql_cypher_comparison.csv"

# The AQL-keyword feature columns in aql_cypher_comparison.csv (describe the
# dataset's *own* AQL translation — a coarse proxy for query construct/complexity).
_FEATURE_COLUMNS = [
    "COLLECT",
    "Graph_Traversal",
    "WITH",
    "FILTER",
    "LIMIT",
    "COUNT",
    "Sorting_and_Pagination",
    "Distinct_and_Set_Operators",
    "array_functions",
    "string_functions",
    "numeric_functions",
    "Control_Flow_and_Boolean",
    "Graph_Operations",
    "Graph_Logic",
    "document_object_functions",
    "date_functions",
    "arangosearch_functions",
    "SEARCH",
    "Data_Modification",
]

# Data-modification errors we surface as a signal rather than double-count.
_QUOTED = re.compile(r"""(['"])(?:\\.|[^\\])*?\1""")
_NUMS = re.compile(r"\b\d+\b")
_WS = re.compile(r"\s+")


@dataclass
class Outcome:
    ok: bool
    code: str = ""
    bucket: str = ""  # normalized failure message
    raw_message: str = ""


@dataclass
class Stats:
    total: int = 0
    ok: int = 0
    failed: int = 0
    by_code: Counter = field(default_factory=Counter)
    by_bucket: Counter = field(default_factory=Counter)
    bucket_samples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    # execution smoke
    exec_attempted: int = 0
    exec_ok: int = 0
    exec_rows: int = 0
    exec_errors: Counter = field(default_factory=Counter)
    # feature correlation: feature -> [transpiled_ok, total_with_feature]
    feature_hits: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))


def _normalize_message(msg: str) -> str:
    """Collapse a failure message into a stable bucket key.

    Strips quoted identifiers and numbers so ``variable 'p' is assigned`` and
    ``variable 'x' is assigned`` land in the same bucket.
    """
    first_line = msg.strip().splitlines()[0] if msg.strip() else msg
    first_line = _QUOTED.sub("'…'", first_line)
    first_line = _NUMS.sub("N", first_line)
    first_line = _WS.sub(" ", first_line).strip()
    return first_line[:140]


def _transpile_one(cypher: str, mapping) -> Outcome:
    try:
        translate(cypher, mapping=mapping)
        return Outcome(ok=True)
    except CoreError as e:
        return Outcome(
            ok=False,
            code=getattr(e, "code", "CORE_ERROR"),
            bucket=_normalize_message(str(e)),
            raw_message=str(e),
        )
    except Exception as e:  # parser / unexpected — still a real gap
        return Outcome(
            ok=False,
            code=type(e).__name__,
            bucket=_normalize_message(f"{type(e).__name__}: {e}"),
            raw_message=str(e),
        )


def _load_rows(path: Path, limit: int | None) -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


def _feature_on(row: dict[str, str], col: str) -> bool:
    raw = (row.get(col) or "").strip()
    if not raw:
        return False
    try:
        return float(raw) > 0
    except ValueError:
        return raw.lower() in ("true", "yes")


def run_transpile_pass(rows: list[dict[str, str]], mapping, *, show: int) -> Stats:
    st = Stats()
    for row in rows:
        cypher = (row.get("cypher") or "").strip()
        if not cypher:
            continue
        st.total += 1
        out = _transpile_one(cypher, mapping)
        for col in _FEATURE_COLUMNS:
            if _feature_on(row, col):
                st.feature_hits[col][1] += 1
                if out.ok:
                    st.feature_hits[col][0] += 1
        if out.ok:
            st.ok += 1
        else:
            st.failed += 1
            st.by_code[out.code] += 1
            st.by_bucket[out.bucket] += 1
            if len(st.bucket_samples[out.bucket]) < show:
                st.bucket_samples[out.bucket].append(_WS.sub(" ", cypher)[:200])
    return st


def run_execution_smoke(rows: list[dict[str, str]], mapping, db) -> Stats:
    """Execute translated AQL against a live movies DB (smoke, not a diff)."""
    from arango_query_core.exec import AqlExecutor

    st = Stats()
    executor = AqlExecutor(db)
    for row in rows:
        cypher = (row.get("cypher") or "").strip()
        if not cypher:
            continue
        out = _transpile_one(cypher, mapping)
        if not out.ok:
            continue
        st.exec_attempted += 1
        try:
            tq = translate(cypher, mapping=mapping)
            result = list(executor.execute(tq.to_aql_query()))
            st.exec_ok += 1
            if result:
                st.exec_rows += 1
        except Exception as e:  # AQL execution error
            st.exec_errors[_normalize_message(f"{type(e).__name__}: {e}")] += 1
    return st


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def _print_transpile_report(st: Stats, *, show: int, feature_report: bool) -> None:
    print("=" * 72)
    print("TRANSPILE COVERAGE")
    print("=" * 72)
    print(f"  queries evaluated : {st.total}")
    print(f"  transpiled OK     : {st.ok}  ({_pct(st.ok, st.total)})")
    print(f"  failed            : {st.failed}  ({_pct(st.failed, st.total)})")
    print()
    if st.by_code:
        print("Failures by error code:")
        for code, n in st.by_code.most_common():
            print(f"  {n:5d}  {code}")
        print()
        print(f"Top gap buckets (normalized failure message){' + samples' if show else ''}:")
        for bucket, n in st.by_bucket.most_common(20):
            print(f"  {n:5d}  {bucket}")
            for sample in st.bucket_samples.get(bucket, []):
                print(f"           e.g. {sample}")
        print()
    if feature_report and st.feature_hits:
        print("Transpile success rate by AQL-feature (dataset's own AQL; proxy for construct):")
        rows = sorted(st.feature_hits.items(), key=lambda kv: kv[1][1], reverse=True)
        for feat, (ok, tot) in rows:
            if tot == 0:
                continue
            print(f"  {tot:5d} queries  {_pct(ok, tot):>6}  {feat}")
        print()


def _print_exec_report(st: Stats) -> None:
    print("=" * 72)
    print("EXECUTION SMOKE (translated AQL against local movies DB)")
    print("=" * 72)
    print(f"  attempted (transpiled OK) : {st.exec_attempted}")
    print(f"  ran without error         : {st.exec_ok}  ({_pct(st.exec_ok, st.exec_attempted)})")
    print(f"  returned >=1 row          : {st.exec_rows}  ({_pct(st.exec_rows, st.exec_attempted)})")
    if st.exec_errors:
        print()
        print("  AQL execution errors:")
        for msg, n in st.exec_errors.most_common(15):
            print(f"    {n:5d}  {msg}")
    print()


def _open_db(args):
    from arango import ArangoClient

    client = ArangoClient(hosts=args.arango_url)
    sys_db = client.db("_system", username=args.arango_user, password=args.arango_pass)
    if not sys_db.has_database(args.arango_db):
        sys_db.create_database(args.arango_db)
    db = client.db(args.arango_db, username=args.arango_user, password=args.arango_pass)
    if args.seed:
        from tests.integration.datasets import seed_movies_pg_dataset

        print(f"Seeding movies PG dataset into '{args.arango_db}' ...")
        seed_movies_pg_dataset(db)
    return db


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--dataset",
        default=_DEFAULT_DATASET,
        help=f"path to aql_cypher_comparison.csv (default: {_DEFAULT_DATASET})",
    )
    ap.add_argument("--mapping", default="movies_pg", help="mapping fixture name (default: movies_pg)")
    ap.add_argument("--limit", type=int, default=None, help="only evaluate the first N rows")
    ap.add_argument("--show", type=int, default=2, help="sample Cypher per gap bucket (default: 2)")
    ap.add_argument("--feature-report", action="store_true", help="print success rate by AQL feature")
    ap.add_argument("--with-db", action="store_true", help="also execute translated AQL against a movies DB")
    ap.add_argument("--seed", action="store_true", help="seed the movies PG dataset before executing")
    ap.add_argument("--arango-url", default=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    ap.add_argument("--arango-user", default=os.environ.get("ARANGO_USER", "root"))
    ap.add_argument("--arango-pass", default=os.environ.get("ARANGO_PASS", "openSesame"))
    ap.add_argument("--arango-db", default="text2cypher_movies_pg")
    args = ap.parse_args()

    path = Path(os.path.expanduser(args.dataset))
    if not path.exists():
        print(f"error: dataset not found: {path}", file=sys.stderr)
        return 2

    mapping = mapping_bundle_for(args.mapping)
    rows = _load_rows(path, args.limit)
    print(f"Loaded {len(rows)} rows from {path}")
    print(f"Mapping: {args.mapping}\n")

    st = run_transpile_pass(rows, mapping, show=args.show)
    _print_transpile_report(st, show=args.show, feature_report=args.feature_report)

    if args.with_db:
        db = _open_db(args)
        ex = run_execution_smoke(rows, mapping, db)
        _print_exec_report(ex)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
