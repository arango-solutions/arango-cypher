"""Tests for the startup-time UI bundle freshness check.

Pins the four-state contract of :func:`arango_cypher.service._check_ui_dist_freshness`
(``ok`` / ``stale`` / ``missing`` / ``no_src``) plus the matching log lines.

Background — 2026-04-24 incident: a developer rebased ``main``, picked up
several UI source changes (branding rename, schema-warning-banner key fix,
WP-29/30 reducer changes), and started uvicorn against a ~10-hour-old
``ui/dist/`` left over from the previous evening's build. The browser
faithfully served the stale "Cypher Workbench" title and there was no
log signal that the bundle was out of date. This module is the regression
fence for that exact failure mode.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from arango_cypher.service import _check_ui_dist_freshness


def _make_dist(root: Path, *, mtime: float | None = None) -> Path:
    """Create a minimal ``dist/`` tree with ``index.html`` at the requested mtime."""
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    index = dist / "index.html"
    index.write_text("<!doctype html><title>fixture</title>")
    if mtime is not None:
        os.utime(index, (mtime, mtime))
    return dist


def _make_src(root: Path, *, mtime: float | None = None) -> Path:
    """Create a minimal ``src/`` tree with one ``.tsx`` file at the requested mtime."""
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    f = src / "App.tsx"
    f.write_text("export default function App() { return null; }\n")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return src


class TestUiDistFreshness:
    """Exercises every branch of ``_check_ui_dist_freshness`` against tmp dirs."""

    def test_fresh_dist_returns_ok_and_emits_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = time.time()
        dist = _make_dist(tmp_path, mtime=now)
        src = _make_src(tmp_path, mtime=now - 60)  # src is older

        with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
            verdict = _check_ui_dist_freshness(dist_dir=dist, src_dir=src)

        assert verdict == "ok"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_stale_dist_returns_stale_and_logs_rebuild_command(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = time.time()
        dist = _make_dist(tmp_path, mtime=now - 3600)  # built an hour ago
        src = _make_src(tmp_path, mtime=now)  # src updated since

        with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
            verdict = _check_ui_dist_freshness(dist_dir=dist, src_dir=src)

        assert verdict == "stale"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "stale" in msg.lower()
        assert "cd ui && npm run build" in msg

    def test_missing_dist_with_src_present_returns_missing_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No dist/ directory at all — fresh-clone scenario.
        src = _make_src(tmp_path, mtime=time.time())
        dist = tmp_path / "dist"  # does not exist

        with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
            verdict = _check_ui_dist_freshness(dist_dir=dist, src_dir=src)

        assert verdict == "missing"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "not built" in msg.lower()
        assert "cd ui && npm run build" in msg

    def test_missing_dist_without_src_returns_no_src_silently(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Wheel-installed deployment: neither tree exists.
        dist = tmp_path / "dist"
        src = tmp_path / "src"

        with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
            verdict = _check_ui_dist_freshness(dist_dir=dist, src_dir=src)

        assert verdict == "no_src"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_dist_present_but_no_src_returns_no_src_silently(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Wheel deployment that bundled a pre-built dist (future packaging),
        # with no src tree shipped — freshness is undefined, stay silent.
        dist = _make_dist(tmp_path, mtime=time.time())
        src = tmp_path / "src"  # absent

        with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
            verdict = _check_ui_dist_freshness(dist_dir=dist, src_dir=src)

        assert verdict == "no_src"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_stale_check_walks_nested_src_subdirectories(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression: the rglob() walk must descend into ui/src/components/."""
        now = time.time()
        dist = _make_dist(tmp_path, mtime=now)
        src = _make_src(tmp_path, mtime=now - 3600)
        # Nested file edited *after* the dist was built — the bug we're guarding
        # against is exactly an edit to ui/src/components/Foo.tsx not being noticed.
        nested = src / "components" / "Banner.tsx"
        nested.parent.mkdir(parents=True)
        nested.write_text("export const Banner = () => null;\n")
        os.utime(nested, (now + 60, now + 60))

        with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
            verdict = _check_ui_dist_freshness(dist_dir=dist, src_dir=src)

        assert verdict == "stale"

    def test_top_level_index_html_drift_also_triggers_stale(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ui/index.html (Vite entry shell) is part of the freshness contract."""
        now = time.time()
        dist = _make_dist(tmp_path, mtime=now)
        src = _make_src(tmp_path, mtime=now - 3600)
        # Top-level ui/index.html — sibling of src/, not under it.
        top_index = tmp_path / "index.html"
        top_index.write_text("<!doctype html><title>Vite shell</title>")
        os.utime(top_index, (now + 60, now + 60))

        with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
            verdict = _check_ui_dist_freshness(dist_dir=dist, src_dir=src)

        assert verdict == "stale"

    def test_oserror_during_probe_short_circuits_to_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A best-effort check must never crash service boot.

        Most realistic failure mode: an unreadable subdirectory under
        ``ui/src`` makes ``Path.rglob`` raise mid-iteration. We patch
        rglob directly because patching ``Path.stat`` also breaks the
        existence-probing branches that run *before* the try-block.
        """
        dist = _make_dist(tmp_path, mtime=time.time())
        src = _make_src(tmp_path, mtime=time.time())

        def flaky_rglob(self: Path, pattern: str):
            raise OSError(f"simulated permission denied during rglob({pattern!r})")

        monkeypatch.setattr(Path, "rglob", flaky_rglob)
        verdict = _check_ui_dist_freshness(dist_dir=dist, src_dir=src)
        assert verdict == "ok"

    def test_real_repo_freshness_is_never_stale(self) -> None:
        """Smoke test against the real repo — assert the dist (if present)
        is not older than the src.

        ``stale`` is the regression we actually care about: someone edited
        ``ui/src`` without rerunning ``npm run build``. The other three
        verdicts are all acceptable in their respective contexts:

        * ``ok``      — local dev after a fresh build (the 2026-04-24
                        rebuild that motivated this PR)
        * ``missing`` — CI workflows that don't run ``npm run build``
                        (lint / unit / packaging / integration jobs all
                        skip the UI build; acceptable, the job is not
                        exercising the UI mount)
        * ``no_src``  — wheel-installed deployments with no ``ui/src``
                        tree shipped

        If this asserts ``stale``, the fix is the literal log message:
        ``cd ui && npm run build`` and recommit.
        """
        verdict = _check_ui_dist_freshness()
        assert verdict != "stale", (
            f"ui/dist is older than ui/src; verdict={verdict!r}. "
            "Run `cd ui && npm run build` and recommit."
        )
        assert verdict in ("ok", "missing", "no_src"), (
            f"Unexpected freshness verdict {verdict!r}; expected one of "
            "ok / missing / no_src."
        )
