"""Declarative registry of databases the catalog sidecar keeps analyzed.

The registry is intentionally small and boring: a handful of database entries,
each pointing at a live ArangoDB endpoint, with credentials resolved from
environment variables (never stored inline). Resolution order:

1. An explicit YAML file (``--config`` / ``ARANGO_CYPHER_CATALOG_CONFIG`` /
   ``configs/catalog.yml``).
2. Fallback: a single entry built from the standard ``ARANGO_URL`` /
   ``ARANGO_DB`` / ``ARANGO_USER`` / ``ARANGO_PASSWORD`` variables, so the
   sidecar works out of the box against the same database the CLI and service
   already use.

YAML shape (see ``configs/catalog.example.yml``)::

    sync:
      interval_seconds: 1800
    databases:
      - name: FinReflectKG
        url: https://host.example/
        database: FinReflectKG
        username: root            # or username_env: ARANGO_USER
        password_env: ARANGO_PASSWORD
        verify_ssl: true
        graphs: [FinReflectKG]    # optional named-graph scopes to also warm
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_ENV = "ARANGO_CYPHER_CATALOG_CONFIG"
DEFAULT_CONFIG_PATH = Path("configs/catalog.yml")
DEFAULT_INTERVAL_SECONDS = 1800  # 30 minutes


class CatalogConfigError(Exception):
    """Raised when a catalog registry file is present but malformed.

    A *missing* file is not an error (the env fallback handles it); only an
    existing-but-invalid file raises, so operators get a loud, actionable
    failure instead of a silently empty catalog.
    """


@dataclass(frozen=True)
class DatabaseEntry:
    """One database (optionally scoped to named graphs) to keep analyzed."""

    name: str
    url: str
    database: str
    username: str
    password: str
    verify_ssl: bool = True
    graphs: tuple[str, ...] = ()

    def redacted(self) -> dict[str, Any]:
        """Loggable view with the password removed."""
        return {
            "name": self.name,
            "url": self.url,
            "database": self.database,
            "username": self.username,
            "verify_ssl": self.verify_ssl,
            "graphs": list(self.graphs),
            "password": "***" if self.password else "",
        }


@dataclass(frozen=True)
class CatalogRegistry:
    """The full set of databases to sync plus the schedule interval."""

    databases: tuple[DatabaseEntry, ...]
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    source: str = "env"
    _seen: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __bool__(self) -> bool:
        return bool(self.databases)


def _resolve_secret(
    *, literal: Any, env_name: Any, what: str, entry_name: str
) -> str:
    """Resolve a credential from a literal or an env-var reference.

    Env-var reference (``*_env``) wins when both are present so a checked-in
    config never has to carry a secret. A referenced-but-unset env var is a
    hard error (better a loud failure than connecting with an empty password).
    """
    if env_name is not None:
        if not isinstance(env_name, str) or not env_name.strip():
            raise CatalogConfigError(
                f"database {entry_name!r}: {what}_env must be a non-empty string"
            )
        val = os.environ.get(env_name)
        if val is None:
            raise CatalogConfigError(
                f"database {entry_name!r}: {what}_env={env_name!r} is not set in the environment"
            )
        return val
    if literal is not None:
        if not isinstance(literal, str):
            raise CatalogConfigError(f"database {entry_name!r}: {what} must be a string")
        return literal
    return ""


def _entry_from_dict(raw: dict[str, Any]) -> DatabaseEntry:
    if not isinstance(raw, dict):
        raise CatalogConfigError(f"database entry must be a mapping, got {type(raw).__name__}")
    name = raw.get("name") or raw.get("database")
    url = raw.get("url")
    database = raw.get("database")
    if not isinstance(url, str) or not url.strip():
        raise CatalogConfigError(f"database {name!r}: 'url' is required")
    if not isinstance(database, str) or not database.strip():
        raise CatalogConfigError(f"database {name!r}: 'database' is required")

    username = _resolve_secret(
        literal=raw.get("username"),
        env_name=raw.get("username_env"),
        what="username",
        entry_name=str(name),
    )
    password = _resolve_secret(
        literal=raw.get("password"),
        env_name=raw.get("password_env"),
        what="password",
        entry_name=str(name),
    )

    graphs_raw = raw.get("graphs") or ()
    if isinstance(graphs_raw, str):
        graphs_raw = [graphs_raw]
    if not isinstance(graphs_raw, (list, tuple)):
        raise CatalogConfigError(f"database {name!r}: 'graphs' must be a list")
    graphs = tuple(str(g) for g in graphs_raw if str(g).strip())

    verify_ssl = raw.get("verify_ssl", True)
    if not isinstance(verify_ssl, bool):
        raise CatalogConfigError(f"database {name!r}: 'verify_ssl' must be a boolean")

    return DatabaseEntry(
        name=str(name or database),
        url=url,
        database=database,
        username=username or "root",
        password=password,
        verify_ssl=verify_ssl,
        graphs=graphs,
    )


def _registry_from_env() -> CatalogRegistry:
    """Single-entry fallback built from the standard ARANGO_* variables."""
    from .._env import read_arango_password

    url = os.environ.get("ARANGO_URL")
    database = os.environ.get("ARANGO_DB")
    if not url or not database:
        logger.warning(
            "No catalog config file and ARANGO_URL/ARANGO_DB are not both set; "
            "catalog registry is empty"
        )
        return CatalogRegistry(databases=(), source="env-empty")

    verify_raw = os.environ.get("ARANGO_VERIFY_SSL", "true").strip().lower()
    verify_ssl = verify_raw not in ("0", "false", "no", "off")
    entry = DatabaseEntry(
        name=database,
        url=url,
        database=database,
        username=os.environ.get("ARANGO_USER", "root"),
        password=read_arango_password(caller="arango_cypher.catalog"),
        verify_ssl=verify_ssl,
    )
    return CatalogRegistry(databases=(entry,), source="env")


def _registry_from_yaml(path: Path) -> CatalogRegistry:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CatalogConfigError(f"catalog config {path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogConfigError(f"catalog config {path} must be a mapping at the top level")

    sync_cfg = raw.get("sync") or {}
    interval = sync_cfg.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    try:
        interval = int(interval)
    except (TypeError, ValueError) as exc:
        raise CatalogConfigError(
            f"catalog config {path}: sync.interval_seconds must be an integer"
        ) from exc
    if interval <= 0:
        raise CatalogConfigError(
            f"catalog config {path}: sync.interval_seconds must be positive"
        )

    dbs_raw = raw.get("databases")
    if not isinstance(dbs_raw, list) or not dbs_raw:
        raise CatalogConfigError(
            f"catalog config {path}: 'databases' must be a non-empty list"
        )
    entries = tuple(_entry_from_dict(d) for d in dbs_raw)

    # Guard against duplicate (database, graph-scope) targets that would have
    # the sidecar redundantly re-analyze the same cache slot every cycle.
    seen: set[str] = set()
    for e in entries:
        for scope in (None, *e.graphs):
            target = f"{e.database}::{scope or ''}"
            if target in seen:
                raise CatalogConfigError(
                    f"catalog config {path}: duplicate target {target!r}"
                )
            seen.add(target)

    return CatalogRegistry(
        databases=entries,
        interval_seconds=interval,
        source=str(path),
        _seen=frozenset(seen),
    )


def load_registry(path: str | Path | None = None) -> CatalogRegistry:
    """Load the catalog registry from YAML, falling back to env variables.

    ``path`` (or ``ARANGO_CYPHER_CATALOG_CONFIG`` / ``configs/catalog.yml``) is
    used when present; an existing-but-invalid file raises
    :class:`CatalogConfigError`. When no file is found, a single entry is built
    from the standard ``ARANGO_*`` variables so the sidecar works out of the box.
    """
    candidate: Path | None = None
    if path is not None:
        candidate = Path(path)
        if not candidate.exists():
            raise CatalogConfigError(f"catalog config not found: {candidate}")
    else:
        env_path = os.environ.get(DEFAULT_CONFIG_ENV)
        if env_path:
            candidate = Path(env_path)
            if not candidate.exists():
                raise CatalogConfigError(
                    f"{DEFAULT_CONFIG_ENV}={env_path!r} points at a missing file"
                )
        elif DEFAULT_CONFIG_PATH.exists():
            candidate = DEFAULT_CONFIG_PATH

    if candidate is not None:
        registry = _registry_from_yaml(candidate)
        logger.info(
            "Loaded catalog registry from %s: %d database(s)",
            candidate,
            len(registry.databases),
        )
        return registry

    return _registry_from_env()
