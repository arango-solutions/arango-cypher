from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arango_query_core import AqlQuery, CoreError, ExtensionPolicy, ExtensionRegistry, MappingBundle
from arango_query_core.exec import AqlExecutor

from .parser import parse_cypher
from .profile import build_cypher_profile
from .translate_v0 import TranslateOptions, translate_v0


@dataclass(frozen=True)
class ValidationResult:
    """Result of :func:`validate_cypher_profile` (syntax-only or parse+translate)."""

    ok: bool
    errors: tuple[dict[str, str], ...] = ()

    @property
    def first_error_code(self) -> str | None:
        return self.errors[0]["code"] if self.errors else None


@dataclass(frozen=True)
class TranspiledQuery:
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]]
    debug: dict[str, Any] | None = None

    def to_aql_query(self) -> AqlQuery:
        return AqlQuery(text=self.aql, bind_vars=self.bind_vars, debug=self.debug)


def get_cypher_profile() -> dict[str, Any]:
    """
    Return a JSON-serializable **Arango Cypher profile** manifest for agents and gateways.

    Describes the declared v0 translator surface, limitations, and extension policy
    defaults. See ``docs/python_prd.md`` §2A.0.
    """
    return build_cypher_profile()


def validate_cypher_profile(
    cypher: str,
    *,
    mapping: MappingBundle | None = None,
    extensions: ExtensionPolicy | None = None,
    params: dict[str, Any] | None = None,
) -> ValidationResult:
    """
    Validate Cypher against the current translator profile.

    - If ``mapping`` is **None**: **syntax only** (successful ANTLR parse).
    - If ``mapping`` is set: parse and attempt :func:`translate` (same rules as execution path).

    On failure returns :class:`ValidationResult` with ``ok=False`` and structured
    ``errors`` entries ``{"code", "message"}`` (typically ``CoreError.code``).
    """
    if not isinstance(cypher, str) or not cypher.strip():
        return ValidationResult(
            ok=False,
            errors=({"code": "INVALID_ARGUMENT", "message": "cypher must be a non-empty string"},),
        )

    try:
        parse_cypher(cypher)
    except CoreError as e:
        return ValidationResult(ok=False, errors=({"code": e.code, "message": str(e)},))

    if mapping is None:
        return ValidationResult(ok=True, errors=())

    try:
        translate(cypher, mapping=mapping, extensions=extensions, params=params)
    except CoreError as e:
        return ValidationResult(ok=False, errors=({"code": e.code, "message": str(e)},))

    return ValidationResult(ok=True, errors=())


def translate(
    cypher: str,
    *,
    mapping: MappingBundle | None = None,
    extensions: ExtensionPolicy | None = None,
    registry: ExtensionRegistry | None = None,
    params: dict[str, Any] | None = None,
) -> TranspiledQuery:
    """Translate a Cypher query to AQL using the provided schema mapping.

    *registry* is an optional :class:`ExtensionRegistry` carrying ``arango.*``
    function/procedure compilers.  When supplied, the translator delegates
    ``arango.bm25()``, ``arango.tfidf()`` etc. to registered compilers.
    """
    if not isinstance(cypher, str) or not cypher.strip():
        raise CoreError("cypher must be a non-empty string", code="INVALID_ARGUMENT")

    if mapping is None:
        raise CoreError("mapping is required", code="INVALID_ARGUMENT")

    options = TranslateOptions(
        extensions=(extensions or ExtensionPolicy(enabled=False)),
        registry=registry,
    )
    q = translate_v0(cypher, mapping=mapping, params=params, options=options)

    return TranspiledQuery(aql=q.text, bind_vars=q.bind_vars, warnings=[], debug=q.debug)


def execute(
    cypher: str,
    *,
    db: Any,
    mapping: MappingBundle | None = None,
    extensions: ExtensionPolicy | None = None,
    registry: ExtensionRegistry | None = None,
    params: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Translate a Cypher query and execute the resulting AQL against *db*.

    *db* must be a ``python-arango`` ``Database`` instance (or compatible).
    Returns the AQL cursor result.
    """
    result = translate(
        cypher, mapping=mapping, extensions=extensions,
        registry=registry, params=params,
    )
    executor = AqlExecutor(db=db)
    return executor.execute(result.to_aql_query(), **kwargs)

