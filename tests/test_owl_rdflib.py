"""Tests for rdflib-based OWL/Turtle ingestion."""
from __future__ import annotations

from unittest.mock import patch

import pytest

rdflib = pytest.importorskip("rdflib", reason="rdflib not installed")

from arango_query_core.owl_rdflib import parse_owl_with_rdflib  # noqa: E402

MINIMAL_OWL = """\
@prefix : <http://example.org/schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

: a owl:Ontology ;
  rdfs:label "Test Schema" .

:Person a owl:Class ;
  rdfs:label "Person" .

:Movie a owl:Class ;
  rdfs:label "Movie" .

:name a owl:DatatypeProperty ;
  rdfs:domain :Person ;
  rdfs:range xsd:string .

:age a owl:DatatypeProperty ;
  rdfs:domain :Person ;
  rdfs:range xsd:integer .

:title a owl:DatatypeProperty ;
  rdfs:domain :Movie ;
  rdfs:range xsd:string .

:ACTED_IN a owl:ObjectProperty ;
  rdfs:label "ACTED_IN" ;
  rdfs:domain :Person ;
  rdfs:range :Movie .
"""

OWL_WITH_PHYS = """\
@prefix : <http://example.org/schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix phys: <http://arangodb.com/schema/physical#> .

: a owl:Ontology .

:Person a owl:Class ;
  rdfs:label "Person" ;
  phys:collectionName "persons" ;
  phys:style "COLLECTION" .

:Movie a owl:Class ;
  rdfs:label "Movie" ;
  phys:collectionName "movies" ;
  phys:style "COLLECTION" .

:name a owl:DatatypeProperty ;
  rdfs:domain :Person ;
  rdfs:range xsd:string .

:ACTED_IN a owl:ObjectProperty ;
  rdfs:domain :Person ;
  rdfs:range :Movie ;
  phys:edgeCollectionName "acted_in" ;
  phys:style "DEDICATED_COLLECTION" .
"""

OWL_WITH_LABEL_STYLE = """\
@prefix : <http://example.org/schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <http://arangodb.com/schema/physical#> .

:Animal a owl:Class ;
  phys:collectionName "nodes" ;
  phys:style "LABEL" ;
  phys:typeField "_type" ;
  phys:typeValue "animal" .
"""


class TestParseMinimalOwl:
    def test_extracts_entities(self) -> None:
        bundle = parse_owl_with_rdflib(MINIMAL_OWL)
        names = {e["name"] for e in bundle.conceptual_schema["entities"]}
        assert names == {"Person", "Movie"}

    def test_extracts_properties(self) -> None:
        bundle = parse_owl_with_rdflib(MINIMAL_OWL)
        person = next(e for e in bundle.conceptual_schema["entities"] if e["name"] == "Person")
        prop_names = {p["name"] for p in person["properties"]}
        assert "name" in prop_names
        assert "age" in prop_names

    def test_property_types(self) -> None:
        bundle = parse_owl_with_rdflib(MINIMAL_OWL)
        person = next(e for e in bundle.conceptual_schema["entities"] if e["name"] == "Person")
        age_prop = next(p for p in person["properties"] if p["name"] == "age")
        assert age_prop["type"] == "integer"

    def test_extracts_relationships(self) -> None:
        bundle = parse_owl_with_rdflib(MINIMAL_OWL)
        rels = bundle.conceptual_schema["relationships"]
        assert len(rels) == 1
        assert rels[0]["type"] == "ACTED_IN"
        assert rels[0]["fromEntity"] == "Person"
        assert rels[0]["toEntity"] == "Movie"

    def test_movie_has_title_property(self) -> None:
        bundle = parse_owl_with_rdflib(MINIMAL_OWL)
        movie = next(e for e in bundle.conceptual_schema["entities"] if e["name"] == "Movie")
        prop_names = {p["name"] for p in movie["properties"]}
        assert "title" in prop_names

    def test_source_metadata(self) -> None:
        bundle = parse_owl_with_rdflib(MINIMAL_OWL)
        assert bundle.metadata["source"] == "owl_rdflib"
        assert bundle.source is not None
        assert bundle.source.kind == "owl_turtle"

    def test_owl_turtle_preserved(self) -> None:
        bundle = parse_owl_with_rdflib(MINIMAL_OWL)
        assert bundle.owl_turtle == MINIMAL_OWL


class TestPhysicalAnnotations:
    def test_entity_collection_names(self) -> None:
        bundle = parse_owl_with_rdflib(OWL_WITH_PHYS)
        pm = bundle.physical_mapping
        assert pm["entities"]["Person"]["collectionName"] == "persons"
        assert pm["entities"]["Movie"]["collectionName"] == "movies"

    def test_entity_style(self) -> None:
        bundle = parse_owl_with_rdflib(OWL_WITH_PHYS)
        pm = bundle.physical_mapping
        assert pm["entities"]["Person"]["style"] == "COLLECTION"

    def test_edge_collection(self) -> None:
        bundle = parse_owl_with_rdflib(OWL_WITH_PHYS)
        pm = bundle.physical_mapping
        acted_in = pm["relationships"]["ACTED_IN"]
        assert acted_in["edgeCollectionName"] == "acted_in"
        assert acted_in["style"] == "DEDICATED_COLLECTION"

    def test_edge_domain_range(self) -> None:
        bundle = parse_owl_with_rdflib(OWL_WITH_PHYS)
        pm = bundle.physical_mapping
        acted_in = pm["relationships"]["ACTED_IN"]
        assert acted_in["domain"] == "Person"
        assert acted_in["range"] == "Movie"

    def test_label_style_type_field(self) -> None:
        bundle = parse_owl_with_rdflib(OWL_WITH_LABEL_STYLE)
        pm = bundle.physical_mapping
        animal = pm["entities"]["Animal"]
        assert animal["style"] == "LABEL"
        assert animal["typeField"] == "_type"
        assert animal["typeValue"] == "animal"
        assert animal["collectionName"] == "nodes"

    def test_no_phys_annotations_gives_empty_mapping(self) -> None:
        bundle = parse_owl_with_rdflib(MINIMAL_OWL)
        assert bundle.physical_mapping["entities"] == {}
        assert bundle.physical_mapping["relationships"] == {}


class TestImportError:
    def test_raises_import_error_when_rdflib_missing(self) -> None:
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "rdflib":
                raise ImportError("No module named 'rdflib'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(ImportError, match="rdflib is required"):
                parse_owl_with_rdflib(MINIMAL_OWL)
