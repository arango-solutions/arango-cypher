"""Packaging smoke test — gated behind ``RUN_PACKAGING=1``.

What this verifies
------------------
The same failure mode that would break a ServiceMaker / Container Manager
deploy: ``pyproject.toml`` resolves to an installable set of wheels inside
a clean Python environment that has *no* access to the developer's local
editable installs.

We build an sdist with ``python -m build --sdist``, unpack it into a fresh
temp directory, create an isolated venv there, and run
``pip install <sdist>[service]``. If a dependency has been declared as a
bare name with no index hit (the ``arangodb-schema-analyzer`` case in
PRD §15.1 — but applies to any future typo or un-published sibling), the
install errors out here rather than at deploy time.

Design notes
------------
* **Off by default.** Day-to-day ``pytest`` must stay fast and offline,
  so the real work is gated on ``RUN_PACKAGING=1`` plus the ``packaging``
  marker (skipped by default in ``pyproject.toml``'s markers config).
* **Tests only the ``[service]`` extra**, matching what gets deployed
  headlessly to the Arango Platform. As of
  ``arangodb-schema-analyzer`` 0.6.0 (2026-04-23) that extra also
  pulls the analyzer from PyPI, so this test now exercises the full
  service-import chain end-to-end inside the clean venv (the
  pre-0.6.0 packaging blocker tracked in PRD §15.1 is closed). If a
  future ``[analyzer]``-only install path needs separate coverage,
  add a second case here.
* **Uses pip**, not ``uv``, for portability across runner images that may
  or may not have ``uv`` preinstalled. The question being answered —
  "does dependency resolution succeed in a clean environment" — is the
  same either way. ServiceMaker itself uses ``uv``; if pip resolves,
  ``uv sync`` will also resolve (pip is the more permissive resolver).
* Runs ``python -c "import arango_cypher.service"`` inside the isolated
  venv to confirm the installed package is actually importable end-to-end,
  not just that pip thought it downloaded wheels.

Runtime
-------
A cold run installs FastAPI + uvicorn + python-arango + antlr4, ~40-70 MB
of wheels. Expect 30-60 seconds on a warm-cached runner, up to ~2 minutes
cold. Well within the CI budget but way too slow for the default loop.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

RUN_PACKAGING = os.environ.get("RUN_PACKAGING") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_PACKAGING,
    reason="Set RUN_PACKAGING=1 to enable the clean-env install smoke test.",
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    """Run a command, streaming output, and fail loudly with full context."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(cmd)}\n"
            f"exit={result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
        )


def _build_sdist(workdir: Path) -> Path:
    """Build an sdist of the repo into ``workdir`` and return its path."""
    dist_dir = workdir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    shutil.which("python") or pytest.fail("python not on PATH")

    _run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "build"],
    )

    _run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(dist_dir)],
        cwd=REPO_ROOT,
    )

    sdists = sorted(dist_dir.glob("arango_cypher_py-*.tar.gz")) + sorted(
        dist_dir.glob("arango-cypher-py-*.tar.gz")
    )
    assert sdists, f"No sdist produced in {dist_dir} (contents: {list(dist_dir.iterdir())})"
    return sdists[-1]


def test_service_extra_installs_in_clean_venv(tmp_path: Path) -> None:
    """``pip install '<sdist>[service]'`` must succeed in a fresh venv.

    This mirrors what ServiceMaker does for a headless deploy.
    """
    sdist = _build_sdist(tmp_path)

    venv_dir = tmp_path / "clean_venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    py = _venv_python(venv_dir)

    _run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])

    _run([str(py), "-m", "pip", "install", "--quiet", f"{sdist}[service]"])

    _run([str(py), "-c", "import arango_cypher; import arango_cypher.service"])

    _run(
        [
            str(py),
            "-c",
            (
                "from arango_cypher.service import app; "
                "assert app.version, 'FastAPI app has no version'; "
                "print('service.version =', app.version)"
            ),
        ],
    )


def test_base_install_succeeds_without_extras(tmp_path: Path) -> None:
    """``pip install '<sdist>'`` (no extras) must also succeed.

    Catches regressions where a production dependency accidentally leaks
    into an optional extra. The base install is what anyone doing a quick
    ``pip install arango-cypher-py`` from an index will get.
    """
    sdist = _build_sdist(tmp_path)

    venv_dir = tmp_path / "base_venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    py = _venv_python(venv_dir)

    _run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    _run([str(py), "-m", "pip", "install", "--quiet", str(sdist)])

    _run([str(py), "-c", "import arango_cypher, arango_query_core"])
