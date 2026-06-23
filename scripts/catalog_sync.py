#!/usr/bin/env python
"""Schema-catalog sidecar entrypoint.

Keeps the analyzed schema mappings for a handful of databases warm in their
shared persistent cache, so the FastAPI service never runs schema analysis on
the request path.

Usage
-----
    # one pass then exit (cron-friendly)
    python scripts/catalog_sync.py --once

    # long-running sidecar: pass, sleep, repeat
    python scripts/catalog_sync.py

    # explicit config + override interval
    python scripts/catalog_sync.py --config configs/catalog.yml --interval 900

Configuration is resolved by ``arango_cypher.catalog.load_registry`` (a YAML
file if present, else the standard ARANGO_* environment variables). Credentials
are read from the environment — never pass secrets on the command line.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

try:
    # Load .env so the sidecar resolves ARANGO_* / *_env credential references
    # the same way the FastAPI service does.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from arango_cypher.catalog import load_registry, sync_forever, sync_once
from arango_cypher.catalog.registry import CatalogConfigError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--config",
        default=None,
        help="Path to a catalog YAML file (default: ARANGO_CYPHER_CATALOG_CONFIG or configs/catalog.yml).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync pass and exit (otherwise loop forever).",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override the loop interval in seconds (ignored with --once).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ...). Default: INFO.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("arango_cypher.catalog.cli")

    try:
        registry = load_registry(args.config)
    except CatalogConfigError as exc:
        log.error("Catalog configuration error: %s", exc)
        return 2

    if not registry:
        log.error(
            "Catalog registry is empty. Provide configs/catalog.yml (see "
            "configs/catalog.example.yml) or set ARANGO_URL/ARANGO_DB."
        )
        return 1

    if args.interval is not None:
        if args.interval <= 0:
            log.error("--interval must be positive")
            return 2
        registry = replace(registry, interval_seconds=args.interval)

    if args.once:
        results = sync_once(registry)
        failed = [r for r in results if not r.ok]
        for r in results:
            log.info("result: %s", r.log_fields())
        if failed:
            log.error("%d/%d target(s) failed", len(failed), len(results))
            return 1
        return 0

    try:
        sync_forever(registry)
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down catalog sidecar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
