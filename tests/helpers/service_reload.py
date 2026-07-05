"""Snapshot / restore the ``arango_cypher.service`` module tree around reloads.

Reloading ``arango_cypher.service`` re-executes its ``__init__``, which
*purges and re-imports every* ``arango_cypher.service.*`` submodule (see the
purge loop at the top of ``arango_cypher/service/__init__.py``). A reload
therefore replaces ``routes.cypher`` / ``app`` / ``security`` / … with fresh
module objects and registers routes on a brand-new FastAPI ``app``.

If a test restores only the *top-level* ``arango_cypher.service`` entry on
teardown (the historical pattern), the reloaded submodules leak into
``sys.modules`` while the restored package's ``app`` stays bound to the
*original* submodules. A later test that does ``from
arango_cypher.service.routes import cypher`` then monkeypatches the *stale
reloaded* module — which the running app never references — so the patch
silently misses (the symptom: ``/execute`` returns the real ``MAPPING_NOT_FOUND``
instead of the stubbed behaviour). See
``tests/test_session_tenant_binding.py::TestExecuteTenantViolationStatusCode``.

These helpers snapshot the full ``arango_cypher.service*`` tree before a
reload and restore it exactly afterwards, so no reloaded submodule leaks into
subsequent tests.
"""

from __future__ import annotations

import sys
from typing import Any

_PREFIX = "arango_cypher.service"


def _service_module_names() -> list[str]:
    return [n for n in list(sys.modules) if n == _PREFIX or n.startswith(_PREFIX + ".")]


def snapshot_service_modules() -> dict[str, Any]:
    """Return the current ``arango_cypher.service*`` ``sys.modules`` entries."""
    return {n: sys.modules[n] for n in _service_module_names()}


def restore_service_modules(snapshot: dict[str, Any]) -> None:
    """Restore the tree to *snapshot*, dropping any modules created since.

    Removes every current ``arango_cypher.service*`` entry that was not in
    the snapshot (i.e. reloaded submodules) and re-installs the snapshot,
    so both the top-level package and every submodule point back at the
    original objects the rest of the suite already captured.
    """
    for name in _service_module_names():
        if name not in snapshot:
            del sys.modules[name]
    sys.modules.update(snapshot)
