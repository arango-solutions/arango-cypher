"""OWL/Turtle export / import endpoints — ``/mapping/export-owl``,
``/mapping/import-owl``.
"""

from __future__ import annotations

import time

from fastapi import Depends, HTTPException

from ..app import app
from ..mapping import _mapping_from_dict
from ..models import OwlExportRequest, OwlImportRequest
from ..observability import log_endpoint_timing
from ..security import _check_compute_rate_limit


@app.post("/mapping/export-owl")
def export_owl(
    req: OwlExportRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Convert a mapping to OWL/Turtle format."""
    from arango_query_core.owl_turtle import mapping_to_turtle

    t0 = time.perf_counter()
    bundle = _mapping_from_dict(req.mapping)
    if bundle is None:
        raise HTTPException(status_code=400, detail="mapping is required")
    turtle = mapping_to_turtle(bundle)
    log_endpoint_timing(
        "/mapping/export-owl",
        round((time.perf_counter() - t0) * 1000, 1),
        turtle_len=len(turtle or ""),
    )
    return {"turtle": turtle}


@app.post("/mapping/import-owl")
def import_owl(
    req: OwlImportRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Parse OWL/Turtle into a MappingBundle (as JSON)."""
    from arango_query_core.owl_turtle import turtle_to_mapping

    t0 = time.perf_counter()
    bundle = turtle_to_mapping(req.turtle)
    log_endpoint_timing(
        "/mapping/import-owl",
        round((time.perf_counter() - t0) * 1000, 1),
        turtle_len=len(req.turtle or ""),
        entities=len((bundle.conceptual_schema or {}).get("entities") or []),
        relationships=len((bundle.conceptual_schema or {}).get("relationships") or []),
    )
    return {
        "conceptualSchema": bundle.conceptual_schema,
        "physicalMapping": bundle.physical_mapping,
        "metadata": bundle.metadata,
    }
