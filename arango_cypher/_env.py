"""Environment-variable helpers shared between the FastAPI service and the
``arango-cypher`` CLI.

The single helper here is :func:`read_arango_password`, which resolves the
historical ``ARANGO_PASS`` / ``ARANGO_PASSWORD`` split (audit-v2 finding
#7). Prior to this module the service read ``ARANGO_PASS`` and the CLI
read ``ARANGO_PASSWORD``, so a developer who used both tools had to set
both env vars to the same value or one tool would silently fall back to
the empty default. The new contract:

* ``ARANGO_PASSWORD`` is the **canonical** name (matches industry
  convention — Postgres, Redis, MongoDB, MySQL, Cassandra, etc. all
  use ``*_PASSWORD``).
* ``ARANGO_PASS`` is read as a deprecated fallback. When only the
  legacy name is set, the helper emits a single ``DeprecationWarning``
  + ``logging.WARNING`` so operators get a clean upgrade signal
  instead of a silent surprise.
* Neither set → empty string (preserves the prior default).

The deprecation warning is emitted at most once per (process, calling
module) pair so a long-running service does not spam its log every
request.

Removal timeline: the legacy ``ARANGO_PASS`` name will be removed at the
next major (1.0). Until then this helper is the single read site for
either name; do not call ``os.getenv("ARANGO_PASS")`` directly anywhere
new — let the helper handle the fallback so the deprecation log fires
exactly where it should.
"""

from __future__ import annotations

import logging
import os
import warnings

_logger = logging.getLogger("arango_cypher")

# Suppress repeated warnings for the same (caller, fallback-name) pair so
# a long-running process logs the deprecation once and then stays quiet.
_warned: set[tuple[str, str]] = set()


def read_arango_password(*, caller: str = "arango_cypher") -> str:
    """Resolve the ArangoDB password from environment variables.

    Returns the value of ``ARANGO_PASSWORD`` if set, otherwise falls back
    to ``ARANGO_PASS`` and emits a one-time deprecation warning when that
    legacy name is the only one defined. Returns the empty string when
    neither variable is set.

    The ``caller`` keyword is used to scope the once-per-process
    deprecation warning so the service and the CLI each log their own
    upgrade reminder if both hit the legacy name. It is not part of the
    public contract beyond that — clients should typically pass their
    own module name or leave the default.

    .. deprecated:: pending
        ``ARANGO_PASS`` will be removed at the next major release.
        Operators should rename their environment variable to
        ``ARANGO_PASSWORD``.
    """
    canonical = os.environ.get("ARANGO_PASSWORD")
    if canonical is not None:
        return canonical

    legacy = os.environ.get("ARANGO_PASS")
    if legacy is not None:
        key = (caller, "ARANGO_PASS")
        if key not in _warned:
            _warned.add(key)
            msg = (
                "ARANGO_PASS is deprecated; use ARANGO_PASSWORD instead. "
                "ARANGO_PASS will be removed at the next major release. "
                f"(read by {caller})"
            )
            _logger.warning(msg)
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
        return legacy

    return ""


def _reset_warning_state_for_tests() -> None:
    """Test-only hook to clear the once-per-process warning set.

    The deprecation log fires at most once per (caller, env-name) pair to
    avoid spamming a long-running service. Tests that exercise the
    fallback path more than once need a way to re-arm the warning between
    cases without resorting to monkey-patching the module-level set
    directly.
    """
    _warned.clear()
