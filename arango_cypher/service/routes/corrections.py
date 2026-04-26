"""Local-learning correction stores —
``/corrections{,/<id>}`` (Cypher→AQL fixes) and
``/nl-corrections{,/<id>}`` (NL→Cypher few-shot pairs).

Cypher→AQL corrections fix the transpiler's output for a specific
``(cypher, mapping)`` pair. NL corrections operate one layer higher:
they capture approved ``(natural_language_question, cypher)`` pairs
and feed them into the FewShotIndex BM25 corpus so future similar
questions benefit. The two stores are deliberately separate — they
have different lookup keys, different lifecycle triggers, and
different callers.
"""

from __future__ import annotations

import time

from fastapi import Depends, HTTPException

from ... import corrections as _corrections
from ... import nl_corrections as _nl_corrections
from ..app import app
from ..models import CorrectionRequest, NLCorrectionRequest
from ..observability import log_endpoint_timing
from ..security import _require_session_in_public_mode, _Session


@app.post("/corrections")
def save_correction(
    req: CorrectionRequest,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Save a user-corrected AQL query for future reuse."""
    t0 = time.perf_counter()
    row_id = _corrections.save(
        cypher=req.cypher,
        mapping=req.mapping,
        database=req.database,
        original_aql=req.original_aql,
        corrected_aql=req.corrected_aql,
        bind_vars=req.bind_vars,
        note=req.note,
    )
    log_endpoint_timing(
        "/corrections",
        round((time.perf_counter() - t0) * 1000, 1),
        action="save",
        correction_id=row_id,
    )
    return {"id": row_id, "status": "saved"}


@app.get("/corrections")
def list_corrections(
    limit: int = 100,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """List stored corrections, most recent first."""
    t0 = time.perf_counter()
    items = _corrections.list_all(limit=limit)
    log_endpoint_timing(
        "/corrections",
        round((time.perf_counter() - t0) * 1000, 1),
        action="list",
        items=len(items),
        limit=limit,
    )
    return {
        "corrections": [
            {
                "id": c.id,
                "cypher": c.cypher,
                "mapping_hash": c.mapping_hash,
                "database": c.database,
                "original_aql": c.original_aql,
                "corrected_aql": c.corrected_aql,
                "bind_vars": c.bind_vars,
                "created_at": c.created_at,
                "note": c.note,
            }
            for c in items
        ]
    }


@app.delete("/corrections/{correction_id}")
def delete_correction(
    correction_id: int,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Delete a single correction."""
    t0 = time.perf_counter()
    found = _corrections.delete(correction_id)
    log_endpoint_timing(
        "/corrections/{id}",
        round((time.perf_counter() - t0) * 1000, 1),
        action="delete",
        correction_id=correction_id,
        found=bool(found),
    )
    if not found:
        raise HTTPException(status_code=404, detail="Correction not found")
    return {"status": "deleted"}


@app.delete("/corrections")
def delete_all_corrections(
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Delete all corrections."""
    t0 = time.perf_counter()
    count = _corrections.delete_all()
    log_endpoint_timing(
        "/corrections",
        round((time.perf_counter() - t0) * 1000, 1),
        action="delete_all",
        deleted=count,
    )
    return {"status": "deleted", "count": count}


@app.post("/nl-corrections")
def save_nl_correction(
    req: NLCorrectionRequest,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Save an approved (NL question → Cypher) pair for few-shot retrieval.

    The pair is appended to the BM25 corpus the next time
    ``POST /nl2cypher`` builds (or rebuilds) its default
    :class:`FewShotIndex`. The FewShotIndex cache is invalidated
    synchronously before this endpoint returns, so the improvement takes
    effect on the very next translation request.
    """
    t0 = time.perf_counter()
    try:
        row_id = _nl_corrections.save(
            question=req.question,
            cypher=req.cypher,
            mapping=req.mapping or None,
            database=req.database,
            note=req.note,
        )
    except ValueError as exc:
        log_endpoint_timing(
            "/nl-corrections",
            round((time.perf_counter() - t0) * 1000, 1),
            action="save",
            status="error",
            error_type="ValueError",
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_endpoint_timing(
        "/nl-corrections",
        round((time.perf_counter() - t0) * 1000, 1),
        action="save",
        correction_id=row_id,
    )
    return {"id": row_id, "status": "saved"}


@app.get("/nl-corrections")
def list_nl_corrections(
    limit: int = 100,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """List stored NL corrections, most recent first."""
    t0 = time.perf_counter()
    items = _nl_corrections.list_all(limit=limit)
    log_endpoint_timing(
        "/nl-corrections",
        round((time.perf_counter() - t0) * 1000, 1),
        action="list",
        items=len(items),
        limit=limit,
    )
    return {
        "corrections": [
            {
                "id": c.id,
                "question": c.question,
                "cypher": c.cypher,
                "mapping_hash": c.mapping_hash,
                "database": c.database,
                "created_at": c.created_at,
                "note": c.note,
            }
            for c in items
        ]
    }


@app.delete("/nl-corrections/{correction_id}")
def delete_nl_correction(
    correction_id: int,
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Delete a single NL correction."""
    t0 = time.perf_counter()
    found = _nl_corrections.delete(correction_id)
    log_endpoint_timing(
        "/nl-corrections/{id}",
        round((time.perf_counter() - t0) * 1000, 1),
        action="delete",
        correction_id=correction_id,
        found=bool(found),
    )
    if not found:
        raise HTTPException(status_code=404, detail="NL correction not found")
    return {"status": "deleted"}


@app.delete("/nl-corrections")
def delete_all_nl_corrections(
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Delete all NL corrections."""
    t0 = time.perf_counter()
    count = _nl_corrections.delete_all()
    log_endpoint_timing(
        "/nl-corrections",
        round((time.perf_counter() - t0) * 1000, 1),
        action="delete_all",
        deleted=count,
    )
    return {"status": "deleted", "count": count}
