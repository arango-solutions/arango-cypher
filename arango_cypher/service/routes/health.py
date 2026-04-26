"""Liveness / readiness probe endpoint."""

from __future__ import annotations

from ..app import app


# Liveness / readiness probe for container orchestrators (Arango Platform's
# Container Manager, Kubernetes, docker-compose healthchecks, etc.). Cheap,
# unauthenticated, no DB call -- returning 200 proves the process is up and
# the FastAPI event loop is serving. Actual DB reachability is tested per
# session via POST /connect, which is where a connection failure should
# surface (not at startup).
@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "arango-cypher-py",
        "version": app.version,
    }
