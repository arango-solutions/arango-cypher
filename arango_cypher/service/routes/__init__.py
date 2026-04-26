"""HTTP route registration for :mod:`arango_cypher.service`.

Importing this package registers all 35-odd FastAPI endpoint handlers
on :data:`arango_cypher.service.app.app` via decorator side-effects.
The package init does nothing else — it exists purely so the parent
package's ``__init__.py`` can do a single ``from . import routes``
to wire the surface, and so reviewers can navigate endpoints by
cluster (``connect``, ``cypher``, ``schema``, ``tools``, ``nl``,
``owl``, ``corrections``).

Per the audit-v2 #8 split, each submodule corresponds to one logical
endpoint cluster. Adding a new endpoint = drop a new ``@app.route(...)``
decorator into the appropriate cluster file (or create a new one and
import it here). Cross-cluster shared helpers live in :mod:`..security`
or :mod:`..mapping` so the route modules stay leaf-position.
"""

from __future__ import annotations

from . import (
    connect,  # noqa: F401
    corrections,  # noqa: F401
    cypher,  # noqa: F401
    health,  # noqa: F401
    nl,  # noqa: F401
    owl,  # noqa: F401
    schema,  # noqa: F401
    tools,  # noqa: F401
)
