"""Tests for arango_cypher.cli (translate, run, mapping, doctor)."""
from __future__ import annotations

import json

import pytest

try:
    from typer.testing import CliRunner

    from arango_cypher.cli import app

    HAS_TYPER = True
except ImportError:
    HAS_TYPER = False

pytestmark = pytest.mark.skipif(not HAS_TYPER, reason="typer not installed")

runner = CliRunner() if HAS_TYPER else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_MAPPING: dict = {
    "conceptual_schema": {
        "entities": [{"name": "User", "labels": ["User"], "properties": []}],
        "relationships": [],
    },
    "physical_mapping": {
        "entities": {"User": {"style": "COLLECTION", "collectionName": "users"}},
        "relationships": {},
    },
}


def _write_mapping(tmp_path, data=None):
    mf = tmp_path / "mapping.json"
    mf.write_text(json.dumps(data or _SIMPLE_MAPPING))
    return mf


# ---------------------------------------------------------------------------
# translate
# ---------------------------------------------------------------------------


def test_translate_prints_aql(tmp_path):
    mf = _write_mapping(tmp_path)
    result = runner.invoke(app, ["translate", "MATCH (n:User) RETURN n", "-m", str(mf)])
    assert result.exit_code == 0
    assert "FOR" in result.output or "@@" in result.output


def test_translate_no_mapping_shows_error():
    result = runner.invoke(app, ["translate", "MATCH (n:User) RETURN n"])
    assert result.exit_code != 0


def test_translate_json_output(tmp_path):
    mf = _write_mapping(tmp_path)
    result = runner.invoke(app, ["translate", "MATCH (n:User) RETURN n", "-m", str(mf), "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "aql" in parsed
    assert "bind_vars" in parsed


def test_translate_inline_mapping_json():
    result = runner.invoke(
        app,
        ["translate", "MATCH (n:User) RETURN n", "--mapping-json", json.dumps(_SIMPLE_MAPPING), "--json"],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "aql" in parsed


def test_translate_bad_mapping_file():
    result = runner.invoke(app, ["translate", "MATCH (n:User) RETURN n", "-m", "/nonexistent/path.json"])
    assert result.exit_code != 0


def test_translate_bad_mapping_json():
    result = runner.invoke(app, ["translate", "MATCH (n:User) RETURN n", "--mapping-json", "{bad json}"])
    assert result.exit_code != 0


def test_translate_stdin(tmp_path):
    mf = _write_mapping(tmp_path)
    result = runner.invoke(app, ["translate", "-m", str(mf)], input="MATCH (n:User) RETURN n")
    assert result.exit_code == 0
    assert "FOR" in result.output or "@@" in result.output


def test_translate_with_params(tmp_path):
    mf = _write_mapping(tmp_path)
    result = runner.invoke(
        app,
        [
            "translate",
            "MATCH (n:User) WHERE n.name = $name RETURN n",
            "-m", str(mf),
            "--json",
            "--params", '{"name": "Alice"}',
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "aql" in parsed


def test_translate_bad_params():
    result = runner.invoke(
        app,
        ["translate", "MATCH (n:User) RETURN n",
         "--mapping-json", json.dumps(_SIMPLE_MAPPING), "-p", "{bad}"],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# doctor — should not crash even without a live DB
# ---------------------------------------------------------------------------


def test_doctor_no_connection():
    result = runner.invoke(app, ["doctor", "--host", "127.0.0.1", "--port", "19876"])
    assert result.exit_code in (0, 1)
    assert "ArangoDB connection" in result.output


def test_doctor_defaults():
    result = runner.invoke(app, ["doctor", "--host", "127.0.0.1", "--port", "19877"])
    assert result.exit_code in (0, 1)
    assert "Target:" in result.output


# ---------------------------------------------------------------------------
# mapping / run — need a live DB so just verify they fail gracefully
# ---------------------------------------------------------------------------


def test_run_no_db():
    result = runner.invoke(
        app,
        ["run", "MATCH (n) RETURN n", "--host", "127.0.0.1", "--port", "19878"],
    )
    assert result.exit_code != 0


def test_mapping_no_db():
    result = runner.invoke(
        app,
        ["mapping", "--host", "127.0.0.1", "--port", "19879"],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# help text sanity
# ---------------------------------------------------------------------------


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "translate" in result.output
    assert "run" in result.output
    assert "mapping" in result.output
    assert "doctor" in result.output
