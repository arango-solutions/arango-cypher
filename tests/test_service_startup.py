"""Tests for the service import-time analyzer guard (WP-28 / defect D2).

The service refuses to boot when ``arangodb-schema-analyzer`` is not
importable unless ``ARANGO_CYPHER_ALLOW_HEURISTIC=1`` is explicitly set.
These tests drive that guard directly via
:func:`arango_cypher.service._require_analyzer_unless_opted_out` so they
do not need to reload the (stateful, app-registering) service module.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import patch

import pytest

from arango_cypher import service as _service

# ---------------------------------------------------------------------------
# _require_analyzer_unless_opted_out
# ---------------------------------------------------------------------------


class TestRequireAnalyzer:
    def test_startup_fails_without_analyzer(self, monkeypatch):
        monkeypatch.delenv("ARANGO_CYPHER_ALLOW_HEURISTIC", raising=False)
        with patch.dict(sys.modules, {"schema_analyzer": None}):
            with pytest.raises(RuntimeError, match="arangodb-schema-analyzer"):
                _service._require_analyzer_unless_opted_out()

    def test_startup_fails_message_names_opt_out_env_var(self, monkeypatch):
        monkeypatch.delenv("ARANGO_CYPHER_ALLOW_HEURISTIC", raising=False)
        with patch.dict(sys.modules, {"schema_analyzer": None}):
            with pytest.raises(RuntimeError) as excinfo:
                _service._require_analyzer_unless_opted_out()
        msg = str(excinfo.value)
        assert "ARANGO_CYPHER_ALLOW_HEURISTIC" in msg
        assert "pip install arangodb-schema-analyzer" in msg

    def test_startup_succeeds_with_opt_out(self, monkeypatch):
        monkeypatch.setenv("ARANGO_CYPHER_ALLOW_HEURISTIC", "1")
        with patch.dict(sys.modules, {"schema_analyzer": None}):
            _service._require_analyzer_unless_opted_out()

    def test_startup_succeeds_with_analyzer(self, monkeypatch):
        monkeypatch.delenv("ARANGO_CYPHER_ALLOW_HEURISTIC", raising=False)
        # Don't stub schema_analyzer — if it's genuinely installed in the
        # test environment the import will succeed; if not, this test is
        # skipped. Either way, no RuntimeError should be raised.
        try:
            import schema_analyzer  # noqa: F401
        except ImportError:
            pytest.skip("schema_analyzer not installed in this test environment")
        _service._require_analyzer_unless_opted_out()


# ---------------------------------------------------------------------------
# Fresh-import path — exercise the module-level call site, not just the
# helper. Reloads the service module under a patched sys.modules so the
# guard actually runs at import time.
# ---------------------------------------------------------------------------


class TestImportTimeGuard:
    def _purge_service_modules(self) -> list[str]:
        """Drop the service module + close-by submodules from sys.modules.

        Returns the list of names removed so the caller can restore
        them in teardown — reloading the service registers routes on a
        *new* FastAPI ``app`` instance, which would break every
        downstream test that imported the original app before us.
        """
        names = [
            name for name in list(sys.modules)
            if name == "arango_cypher.service"
        ]
        return names

    def test_import_raises_when_analyzer_missing_and_no_opt_out(
        self, monkeypatch
    ):
        monkeypatch.delenv("ARANGO_CYPHER_ALLOW_HEURISTIC", raising=False)
        removed = self._purge_service_modules()
        original = {name: sys.modules.pop(name) for name in removed}
        try:
            with patch.dict(sys.modules, {"schema_analyzer": None}):
                with pytest.raises(RuntimeError, match="arangodb-schema-analyzer"):
                    importlib.import_module("arango_cypher.service")
        finally:
            # Drop the partially-imported module (if any) and restore
            # the original so the rest of the suite keeps seeing the
            # app / session registry it already captured.
            sys.modules.pop("arango_cypher.service", None)
            for name, mod in original.items():
                sys.modules[name] = mod

    def test_import_succeeds_when_opt_out_set(self, monkeypatch):
        monkeypatch.setenv("ARANGO_CYPHER_ALLOW_HEURISTIC", "1")
        removed = self._purge_service_modules()
        original = {name: sys.modules.pop(name) for name in removed}
        try:
            with patch.dict(sys.modules, {"schema_analyzer": None}):
                mod = importlib.import_module("arango_cypher.service")
                assert hasattr(mod, "app")
        finally:
            sys.modules.pop("arango_cypher.service", None)
            for name, mod in original.items():
                sys.modules[name] = mod
