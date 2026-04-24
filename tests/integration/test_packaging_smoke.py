"""Packaging smoke test — WP-19 (Arango Platform deployment enablement).

See ``docs/implementation_plan.md`` §WP-19 scope item 2 and
``docs/arango_packaging_service/deployment_runbook.md`` for the context.

Two tests live here, with different cost profiles and different gates:

* :func:`test_pyproject_extras_pin_published_versions_only` — unconditional
  unit-level guard. Reads ``pyproject.toml`` directly, refuses local-path or
  VCS references in the ``[analyzer]`` / ``[service]`` / ``[dev]`` /
  ``[cli]`` / ``[owl]`` extras. Runs in milliseconds on every ``pytest``
  invocation; guards the WP-19 acceptance criterion #3 ("the ``[analyzer]``
  extra pins a published version with no local-path or git references") as
  a standing regression.
* :func:`test_sdist_builds_and_imports_with_service_extras` — the expensive
  end-to-end smoke. Gated behind ``RUN_PACKAGING=1`` (tens of seconds of
  build + fresh venv + wheel download for transitive deps, needs PyPI
  network access); runs the canonical WP-19 flow: build an sdist, install
  it with the ``[service,analyzer]`` extras into a throwaway venv, and
  assert ``import arango_cypher.service`` succeeds.

The WP-19 runbook mentions ``uv build`` / ``uv sync`` — those are
convenience wrappers for the PEP 517 + PEP 508 tools this test invokes
directly via ``python -m build`` and ``pip install``. The smoke test uses
the portable form so CI doesn't need a non-stdlib ``uv`` install; a dev
running the runbook manually can use either.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


try:
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover — Python < 3.11, unsupported per pyproject
    import tomli as _toml  # type: ignore[no-redef]


def _load_pyproject() -> dict[str, Any]:
    with _PYPROJECT.open("rb") as fh:
        return _toml.load(fh)


def _require_packaging_gate() -> None:
    if os.environ.get("RUN_PACKAGING") != "1":
        pytest.skip("Set RUN_PACKAGING=1 to run the packaging smoke test")


def _venv_python(venv_dir: Path) -> Path:
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    candidate = venv_dir / bin_dir / exe
    if not candidate.exists():
        raise AssertionError(f"venv python not found at {candidate}")
    return candidate


# --------------------------------------------------------------------------- #
# Unconditional guard — runs on every `pytest` invocation.
# --------------------------------------------------------------------------- #


def test_pyproject_extras_pin_published_versions_only() -> None:
    """Acceptance criterion WP-19 #3.

    Every entry in every ``[project.optional-dependencies]`` list must be a
    PEP 508 requirement against a *published* name, i.e.:

    * no ``file:`` / ``./`` / absolute-path references,
    * no ``git+…`` / ``hg+…`` / ``svn+…`` VCS URLs,
    * no bare ``@ url`` direct references,
    * self-referencing extras (``arango-cypher-py[...]``) are allowed — they
      are PEP 735 indirection, not a deployment blocker.

    Breaking this guard means deployment via sdist-to-platform will fail
    at install time; it's the cheapest test that guards the WP-19 happy
    path without building anything.
    """
    data = _load_pyproject()
    project_name = data["project"]["name"]

    extras: dict[str, list[str]] = data["project"].get("optional-dependencies", {})
    assert extras, (
        "pyproject.toml has no [project.optional-dependencies]; WP-19 assumes at least [analyzer] + [service]"
    )

    forbidden_prefixes = ("file:", "./", "/", "git+", "hg+", "svn+", "bzr+")
    violations: list[tuple[str, str, str]] = []

    for extra_name, reqs in extras.items():
        for req in reqs:
            stripped = req.strip()
            if stripped.startswith(f"{project_name}["):
                continue
            for prefix in forbidden_prefixes:
                if stripped.startswith(prefix):
                    violations.append((extra_name, stripped, f"starts with {prefix!r}"))
                    break
            if " @ " in stripped:
                violations.append((extra_name, stripped, "contains ' @ ' (PEP 508 direct reference)"))

    assert not violations, (
        "pyproject.toml contains unpublished references that will break "
        "sdist deployment (WP-19 acceptance criterion #3):\n  "
        + "\n  ".join(f"[{e}] {r} — {why}" for e, r, why in violations)
    )


# --------------------------------------------------------------------------- #
# Expensive end-to-end — gated behind RUN_PACKAGING=1.
# --------------------------------------------------------------------------- #


def test_sdist_builds_and_imports_with_service_extras(tmp_path: Path) -> None:
    """Canonical WP-19 packaging smoke test.

    Flow
    ----
    1. ``python -m build --sdist --outdir <tmp>/dist <repo>`` — produces
       ``arango_cypher_py-<ver>.tar.gz``. ``build`` uses an isolated PEP
       517 build env; hatchling is resolved fresh, which also validates
       that the build-system.requires pin is correct.
    2. ``python -m venv <tmp>/venv`` — fresh venv, no pollution from the
       dev tree.
    3. ``<tmp>/venv/bin/pip install --upgrade pip`` — keeps the resolver
       modern (some older pip versions don't accept ``<path>[extras]``).
    4. ``<tmp>/venv/bin/pip install <sdist>[service,analyzer]`` — pulls
       the full service dependency closure (FastAPI, uvicorn,
       python-arango, arangodb-schema-analyzer, …) from PyPI.
    5. ``<tmp>/venv/bin/python -c 'import arango_cypher.service'`` —
       asserts the resulting import graph is actually coherent.

    If any step fails, the full subprocess output is captured into the
    assertion message so the failure mode is diagnosable without re-running.

    Runtime: typically 30–90 s depending on the pip cache and the latency
    to PyPI. Should not be in the default CI fast lane — the intended
    invocation is a nightly / cron job or pre-release verification.
    """
    _require_packaging_gate()

    try:
        import build as _build_mod  # noqa: F401
    except ImportError:
        pytest.skip("the `build` package is not installed in the current interpreter")

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    _run_or_fail(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(dist_dir), str(_REPO_ROOT)],
        label="sdist build",
    )

    sdists = sorted(dist_dir.glob("*.tar.gz"))
    assert len(sdists) == 1, f"expected exactly one sdist in {dist_dir}, got {sdists!r}"
    sdist = sdists[0]

    venv_dir = tmp_path / "venv"
    _run_or_fail(
        [sys.executable, "-m", "venv", str(venv_dir)],
        label="venv creation",
    )
    venv_python = _venv_python(venv_dir)

    _run_or_fail(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
        label="pip upgrade",
    )

    _run_or_fail(
        [str(venv_python), "-m", "pip", "install", f"{sdist}[service,analyzer]"],
        label="sdist install with [service,analyzer] extras",
    )

    completed = _run_or_fail(
        [str(venv_python), "-c", "import arango_cypher.service as _svc; print(_svc.__name__)"],
        label="import arango_cypher.service in fresh venv",
    )
    assert completed.stdout.strip() == "arango_cypher.service", (
        f"unexpected stdout from import check: {completed.stdout!r}"
    )


def _run_or_fail(cmd: list[str], *, label: str, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    """Run ``cmd``, capturing output; raise a diagnosable ``AssertionError`` on failure.

    Using :class:`subprocess.run` with ``check=True`` swallows the captured
    stdout/stderr on failure, which makes remote-CI log triage painful.
    This wrapper re-raises with the full output inlined so the pytest
    failure message is self-contained.
    """
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"{label} failed (exit {completed.returncode}).\n"
            f"  command: {cmd}\n"
            f"  stdout:\n{completed.stdout}\n"
            f"  stderr:\n{completed.stderr}"
        )
    return completed
