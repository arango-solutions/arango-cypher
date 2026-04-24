from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

try:
    from arango import ArangoClient
except ImportError:
    ArangoClient = None  # type: ignore[misc, assignment]


def _load_dotenv_if_present() -> None:
    """
    Minimal .env loader for local integration runs.
    We intentionally avoid introducing dotenv dependencies this early.
    """
    root = Path(__file__).resolve().parents[2]
    p = root / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


def pytest_collection_modifyitems(config, items):
    _load_dotenv_if_present()
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="Set RUN_INTEGRATION=1 to run integration tests")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def arango_pytest_url() -> str:
    """
    Start ArangoDB via ``docker-compose.pytest.yml`` on host port **28530**, wait until
    healthy, yield base URL, then ``docker compose down`` (project ``arango_cypher_pytest``).

    Requires ``RUN_INTEGRATION=1`` and a working Docker daemon. Skips when unavailable.
    """
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")

    if ArangoClient is None:
        pytest.skip("python-arango not installed")

    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("Docker not available")

    root = Path(__file__).resolve().parents[2]
    compose_file = "docker-compose.pytest.yml"
    project = "arango_cypher_pytest"
    up = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "-p",
        project,
        "up",
        "-d",
    ]
    down = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "-p",
        project,
        "down",
    ]

    subprocess.run(up, cwd=root, check=True, capture_output=True, text=True)

    url = "http://127.0.0.1:28530"
    user, pw = "root", "openSesame"
    deadline = time.time() + 120
    last_err: BaseException | None = None
    while time.time() < deadline:
        try:
            client = ArangoClient(hosts=url)
            db = client.db("_system", username=user, password=pw)
            if db.version():
                break
        except Exception as e:
            last_err = e
        time.sleep(1)
    else:
        subprocess.run(down, cwd=root, capture_output=True, text=True)
        raise AssertionError(f"ArangoDB did not become ready at {url}") from last_err

    yield url

    subprocess.run(down, cwd=root, capture_output=True, text=True)
