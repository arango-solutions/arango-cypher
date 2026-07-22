"""Tests for arango_cypher.catalog.registry — declarative DB registry loading."""

from __future__ import annotations

import pytest

from arango_cypher.catalog.registry import (
    CatalogConfigError,
    DatabaseEntry,
    _registry_from_env,
    load_registry,
)


def _write(tmp_path, text: str):
    p = tmp_path / "catalog.yml"
    p.write_text(text, encoding="utf-8")
    return p


class TestLoadFromYaml:
    def test_minimal_entry_with_password_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_DB_PW", "s3cret")
        path = _write(
            tmp_path,
            """
            sync:
              interval_seconds: 600
            databases:
              - name: kg
                url: https://host.example/
                database: KG
                username: root
                password_env: MY_DB_PW
            """,
        )
        reg = load_registry(path)
        assert reg.interval_seconds == 600
        assert len(reg.databases) == 1
        entry = reg.databases[0]
        assert entry.name == "kg"
        assert entry.database == "KG"
        assert entry.username == "root"
        assert entry.password == "s3cret"
        assert entry.verify_ssl is True
        assert entry.graphs == ()

    def test_graphs_and_username_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("U", "svc")
        monkeypatch.setenv("P", "pw")
        path = _write(
            tmp_path,
            """
            databases:
              - name: kg
                url: https://host/
                database: KG
                username_env: U
                password_env: P
                graphs: [G1, G2]
            """,
        )
        reg = load_registry(path)
        entry = reg.databases[0]
        assert entry.username == "svc"
        assert entry.password == "pw"
        assert entry.graphs == ("G1", "G2")

    def test_unset_password_env_is_hard_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ABSENT_PW", raising=False)
        path = _write(
            tmp_path,
            """
            databases:
              - name: kg
                url: https://host/
                database: KG
                password_env: ABSENT_PW
            """,
        )
        with pytest.raises(CatalogConfigError, match="ABSENT_PW"):
            load_registry(path)

    def test_missing_required_url_raises(self, tmp_path):
        path = _write(
            tmp_path,
            """
            databases:
              - name: kg
                database: KG
            """,
        )
        with pytest.raises(CatalogConfigError, match="url"):
            load_registry(path)

    def test_empty_databases_list_raises(self, tmp_path):
        path = _write(tmp_path, "databases: []\n")
        with pytest.raises(CatalogConfigError, match="non-empty"):
            load_registry(path)

    def test_duplicate_target_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("P", "pw")
        path = _write(
            tmp_path,
            """
            databases:
              - name: a
                url: https://host/
                database: KG
                password_env: P
              - name: b
                url: https://host/
                database: KG
                password_env: P
            """,
        )
        with pytest.raises(CatalogConfigError, match="duplicate"):
            load_registry(path)

    def test_nonpositive_interval_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("P", "pw")
        path = _write(
            tmp_path,
            """
            sync:
              interval_seconds: 0
            databases:
              - name: a
                url: https://host/
                database: KG
                password_env: P
            """,
        )
        with pytest.raises(CatalogConfigError, match="positive"):
            load_registry(path)

    def test_explicit_missing_path_raises(self, tmp_path):
        with pytest.raises(CatalogConfigError, match="not found"):
            load_registry(tmp_path / "does-not-exist.yml")


class TestEnvFallback:
    def test_env_fallback_builds_single_entry(self, monkeypatch):
        monkeypatch.setenv("ARANGO_URL", "https://h/")
        monkeypatch.setenv("ARANGO_DB", "KG")
        monkeypatch.setenv("ARANGO_USER", "root")
        monkeypatch.setenv("ARANGO_PASSWORD", "pw")
        reg = _registry_from_env()
        assert reg.source == "env"
        assert len(reg.databases) == 1
        e = reg.databases[0]
        assert isinstance(e, DatabaseEntry)
        assert e.url == "https://h/"
        assert e.database == "KG"
        assert e.password == "pw"

    def test_env_fallback_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("ARANGO_URL", raising=False)
        monkeypatch.delenv("ARANGO_DB", raising=False)
        reg = _registry_from_env()
        assert not reg
        assert reg.databases == ()

    def test_verify_ssl_parsed_from_env(self, monkeypatch):
        monkeypatch.setenv("ARANGO_URL", "https://h/")
        monkeypatch.setenv("ARANGO_DB", "KG")
        monkeypatch.setenv("ARANGO_PASSWORD", "pw")
        monkeypatch.setenv("ARANGO_VERIFY_SSL", "false")
        reg = _registry_from_env()
        assert reg.databases[0].verify_ssl is False


def test_redacted_hides_password():
    e = DatabaseEntry(name="kg", url="u", database="KG", username="root", password="secret")
    red = e.redacted()
    assert red["password"] == "***"
    assert "secret" not in str(red)
