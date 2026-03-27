from __future__ import annotations

import os
import time

import pytest
from arango import ArangoClient


@pytest.mark.integration
def test_arangodb_connectivity_smoke():
    url = os.environ.get("ARANGO_URL", "http://localhost:8529")
    user = os.environ.get("ARANGO_USER", "root")
    pw = os.environ.get("ARANGO_PASS", "openSesame")
    db_name = os.environ.get("ARANGO_DB", "_system")

    client = ArangoClient(hosts=url)
    deadline = time.time() + 60
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            db = client.db(db_name, username=user, password=pw)
            v = db.version()
            assert v
            return
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise AssertionError(f"Failed to connect to ArangoDB at {url} within timeout") from last_err

