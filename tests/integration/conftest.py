from __future__ import annotations

import os
from pathlib import Path

import pytest


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

