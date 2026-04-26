"""OWL/Turtle export / import endpoints — ``/mapping/export-owl``,
``/mapping/import-owl``.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ..app import app
from ..mapping import _mapping_from_dict
from ..models import OwlExportRequest, OwlImportRequest
from ..security import _check_compute_rate_limit


@app.post("/mapping/export-owl")
def export_owl(
    req: OwlExportRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Convert a mapping to OWL/Turtle format."""
    from arango_query_core.owl_turtle import mapping_to_turtle

    bundle = _mapping_from_dict(req.mapping)
    if bundle is None:
        raise HTTPException(status_code=400, detail="mapping is required")
    return {"turtle": mapping_to_turtle(bundle)}


@app.post("/mapping/import-owl")
def import_owl(
    req: OwlImportRequest,
    _: None = Depends(_check_compute_rate_limit),
):
    """Parse OWL/Turtle into a MappingBundle (as JSON)."""
    from arango_query_core.owl_turtle import turtle_to_mapping

    bundle = turtle_to_mapping(req.turtle)
    return {
        "conceptualSchema": bundle.conceptual_schema,
        "physicalMapping": bundle.physical_mapping,
        "metadata": bundle.metadata,
    }
