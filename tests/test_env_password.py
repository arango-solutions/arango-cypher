"""Tests for :func:`arango_cypher._env.read_arango_password`.

Closes audit-v2 finding #7 — until this batch the FastAPI service read
``ARANGO_PASS`` and the CLI read ``ARANGO_PASSWORD``, so a developer who
used both tools had to set both env vars or one would silently fall
back to the empty default. The helper now reads the canonical
``ARANGO_PASSWORD`` first and falls back to ``ARANGO_PASS`` with a
one-time deprecation warning. These tests pin:

* canonical wins when both are set
* legacy is accepted as a fallback
* fallback emits exactly one ``DeprecationWarning`` + one
  ``logging.WARNING`` per (caller, fallback-name) pair (no log spam in
  a long-running service that calls the helper per-request)
* re-arming the warning state works for tests that need to exercise
  the fallback path more than once
* both unset returns the empty string (preserves the prior default)
* the warning text mentions the canonical name so an operator can
  fix it from the log line alone
"""

from __future__ import annotations

import logging
import warnings

import pytest

from arango_cypher._env import _reset_warning_state_for_tests, read_arango_password


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARANGO_PASSWORD", raising=False)
    monkeypatch.delenv("ARANGO_PASS", raising=False)
    _reset_warning_state_for_tests()


class TestReadArangoPassword:
    def test_canonical_wins_when_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARANGO_PASSWORD", "canonical-pw")
        monkeypatch.setenv("ARANGO_PASS", "legacy-pw")
        # No warning when canonical is set even if legacy is too.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert read_arango_password() == "canonical-pw"
        assert not any(issubclass(w.category, DeprecationWarning) for w in caught)

    def test_canonical_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARANGO_PASSWORD", "canonical-pw")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert read_arango_password() == "canonical-pw"
        assert not caught

    def test_legacy_only_returns_value_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ARANGO_PASS", "legacy-pw")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with caplog.at_level(logging.WARNING, logger="arango_cypher"):
                assert read_arango_password(caller="test.suite") == "legacy-pw"

        # One DeprecationWarning + one WARNING log line.
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert "ARANGO_PASS" in str(deprecations[0].message)
        assert "ARANGO_PASSWORD" in str(deprecations[0].message)

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1
        assert "ARANGO_PASS" in warning_records[0].message
        assert "ARANGO_PASSWORD" in warning_records[0].message
        # The caller hint is in the message so an operator can grep for the
        # specific consumer that's still on the legacy name.
        assert "test.suite" in warning_records[0].message

    def test_fallback_warns_once_per_caller(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # In a long-running service that calls the helper per-request the
        # warning must not spam the log on every call. The contract is
        # "once per (caller, fallback-name) pair per process".
        monkeypatch.setenv("ARANGO_PASS", "legacy-pw")
        with caplog.at_level(logging.WARNING, logger="arango_cypher"):
            for _ in range(5):
                assert read_arango_password(caller="same.caller") == "legacy-pw"
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1, (
            f"Expected exactly 1 warning record, got {len(warning_records)}: "
            f"{[r.message for r in warning_records]}"
        )

    def test_fallback_warns_per_distinct_caller(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Different consumers each get their own upgrade reminder so a
        # service + CLI process running in the same shell session both
        # show the deprecation independently.
        monkeypatch.setenv("ARANGO_PASS", "legacy-pw")
        with caplog.at_level(logging.WARNING, logger="arango_cypher"):
            assert read_arango_password(caller="a") == "legacy-pw"
            assert read_arango_password(caller="b") == "legacy-pw"
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 2

    def test_neither_set_returns_empty_string(self) -> None:
        assert read_arango_password() == ""

    def test_canonical_empty_string_is_respected(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An operator who explicitly sets ARANGO_PASSWORD="" (auth-less
        # local dev) must not fall through to the legacy name and trip
        # the deprecation warning. Empty string is a valid intentional
        # value, not "unset".
        monkeypatch.setenv("ARANGO_PASSWORD", "")
        monkeypatch.setenv("ARANGO_PASS", "legacy-pw")
        with caplog.at_level(logging.WARNING, logger="arango_cypher"):
            assert read_arango_password() == ""
        assert not any(r.levelno == logging.WARNING for r in caplog.records)

    def test_reset_warning_state_re_arms(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ARANGO_PASS", "legacy-pw")
        with caplog.at_level(logging.WARNING, logger="arango_cypher"):
            read_arango_password(caller="x")
            _reset_warning_state_for_tests()
            read_arango_password(caller="x")
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 2
