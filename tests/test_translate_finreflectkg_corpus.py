"""Coverage tracker for a real-world financial knowledge-graph query set.

These 22 queries are an anonymized port of a customer's NL + Cypher + AQL
benchmark against the public **FinReflectKG** graph (an LPG: a single ``Node``
vertex collection and a single ``relations`` edge collection, both
discriminated by a ``type`` field). They exercise the advanced end of Cypher:
path variables, variable-length paths, multi-``MATCH`` pipelines,
``WITH``-aggregation, ``collect()``, list comprehensions over ``nodes(path)``,
regex (``=~``), and string scalar functions.

The set doubles as:

1. **A regression guard** — every query currently supported MUST keep
   transpiling (``SUPPORTED`` entries).
2. **A living coverage ledger** — every query the v0 transpiler cannot yet
   handle is recorded with the exact error it raises (``GAP`` entries). When a
   gap is closed, the matching ``test_known_gap_*`` case fails loudly, which is
   the signal to promote the entry to ``SUPPORTED``.

See ``docs/cypher_coverage_plan.md`` for the roadmap that closes the gaps.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from arango_query_core.errors import CoreError

from arango_cypher.api import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@dataclass(frozen=True)
class CorpusEntry:
    cid: str
    nl: str
    cypher: str
    supported: bool
    gap: str = ""  # substring expected in the CoreError message for GAP entries


CORPUS: list[CorpusEntry] = [
    CorpusEntry(
        "q01_entity_type_distribution",
        "What are the 20 most common entity types in the knowledge graph?",
        "MATCH (n) RETURN labels(n)[0] AS entity_type, COUNT(n) AS count ORDER BY count DESC LIMIT 20",
        supported=True,
    ),
    CorpusEntry(
        "q02_relationship_type_distribution",
        "What are the 30 most common relationship types in the graph?",
        "MATCH ()-[r]->() RETURN type(r) AS relationship_type, COUNT(r) AS count "
        "ORDER BY count DESC LIMIT 30",
        supported=True,
    ),
    CorpusEntry(
        "q03_cinf_stakes_as_paths",
        "Show me, as a graph, the publicly-traded companies that CINF has a stake in.",
        "MATCH p = (a {id: 'CINF'})-[r:Has_Stake_In]->(b) "
        "WHERE b.id IS NOT NULL AND size(b.id) < 6 AND b.id = upper(b.id) "
        "AND b.id <> 'CINF' RETURN p LIMIT 50",
        supported=True,
    ),
    CorpusEntry(
        "q04_cinf_stakes_ticker_name",
        "Which publicly-traded companies does CINF hold a stake in? List their "
        "ticker symbols and company names.",
        "MATCH (a {id: 'CINF'})-[r:Has_Stake_In]->(b) "
        "WHERE b.id IS NOT NULL AND size(b.id) < 6 AND b.id = upper(b.id) "
        "AND b.id <> 'CINF' RETURN b.id AS ticker, b.name AS company_name LIMIT 50",
        supported=True,
    ),
    CorpusEntry(
        "q05_orgs_many_locations",
        "Which organizations operate in more than 3 different geographic "
        "locations? Show the count and a few example locations.",
        "MATCH (org:ORG)-[:Operates_In]->(loc:GPE) "
        "WITH org, COUNT(DISTINCT loc) AS location_count, "
        "COLLECT(DISTINCT loc.name)[0..5] AS sample_locations "
        "WHERE location_count > 3 "
        "RETURN org.name AS organization, location_count, sample_locations "
        "ORDER BY location_count DESC LIMIT 15",
        supported=True,
    ),
    CorpusEntry(
        "q06_metrics_of_cinf_holdings_paths",
        "Show the financial metrics disclosed by the companies that CINF has a stake in, as connected paths.",
        "MATCH path = (a {id: 'CINF'})-[:Has_Stake_In]->(b)"
        "-[:Discloses]->(c:FIN_METRIC) "
        "WHERE b.id IS NOT NULL AND size(b.id) < 6 AND b.id = upper(b.id) "
        "RETURN path LIMIT 25",
        supported=True,
    ),
    CorpusEntry(
        "q07_metrics_by_apple_held_by_cinf",
        "What financial metrics does Apple (AAPL) - a company CINF has a stake in - disclose?",
        "MATCH ({id: 'CINF'})-[:Has_Stake_In]->({id: 'AAPL'})"
        "-[:Discloses]->(m:FIN_METRIC) RETURN DISTINCT m.name LIMIT 25",
        supported=True,
    ),
    CorpusEntry(
        "q08_risk_exposure",
        "For organizations operating in more than 5 locations, which of those "
        "locations are negatively impacted by risks, and what are those risks?",
        "MATCH (org:ORG)-[:Operates_In]->(loc1:GPE) "
        "WITH org, COUNT(DISTINCT loc1) AS location_count WHERE location_count > 5 "
        "MATCH (org)-[:Operates_In]->(loc:GPE)<-[:Negatively_Impacts]-(risk) "
        "WITH org.name AS organization, location_count, loc.name AS risky_location, "
        "COLLECT(DISTINCT risk.name) AS risks ORDER BY SIZE(risks) DESC LIMIT 20 "
        "RETURN organization, location_count, risky_location, risks",
        supported=True,
    ),
    CorpusEntry(
        "q09_stakeholders_bigtech",
        "For major tech companies, which stakeholders are invested in them, and "
        "what financial metrics do those companies disclose (at least 3)?",
        "MATCH (stakeholder)-[:Has_Stake_In]->(company:ORG) "
        "WHERE company.name IN ['AAPL','MSFT','GOOGL'] WITH stakeholder, company "
        "MATCH (company)-[:Discloses]->(metric:FIN_METRIC) "
        "WITH company, stakeholder, COLLECT(DISTINCT metric.name)[0..5] AS disclosed_metrics "
        "WHERE SIZE(disclosed_metrics) > 2 "
        "RETURN company.name AS organization, stakeholder.name AS stakeholder, "
        "disclosed_metrics, SIZE(disclosed_metrics) AS metric_count "
        "ORDER BY stakeholder LIMIT 15",
        supported=True,
    ),
    CorpusEntry(
        "q10_risk_dependency_disclosure",
        "Trace how risks negatively impact organizations that depend on other "
        "organizations, and show the metrics those partners disclose.",
        "MATCH (risk)-[:Negatively_Impacts]->(org1:ORG)"
        "-[:Depends_On]->(org2:ORG)-[:Discloses]->(metric:FIN_METRIC) "
        "WHERE risk:RISK_FACTOR OR risk:EVENT "
        "WITH risk.name AS risk_name, org1.name AS impacted_org, "
        "org2.name AS dependent_org, COLLECT(DISTINCT metric.name)[0..3] AS disclosed_metrics, "
        "COUNT(DISTINCT metric) AS metric_count WHERE metric_count > 0 "
        "RETURN risk_name, impacted_org, dependent_org, disclosed_metrics, metric_count "
        "ORDER BY metric_count DESC LIMIT 10",
        supported=True,
    ),
    CorpusEntry(
        "q11_geo_risk_3hop",
        "Trace 3-hop risk propagation: a risk negatively impacts an org, which "
        "depends on another org, which operates in some location.",
        "MATCH path = (risk)-[:Negatively_Impacts]->(org1:ORG)"
        "-[:Depends_On]->(org2:ORG)-[:Operates_In]->(loc:GPE) "
        "WHERE risk:RISK_FACTOR OR risk:EVENT "
        "RETURN risk.name AS initial_risk, org1.name AS impacted_org, "
        "org2.name AS dependent_org, loc.name AS location LIMIT 20",
        supported=True,
    ),
    CorpusEntry(
        "q12_regulated_metric_disclosing",
        "Among orgs that disclose more than 50 metrics and are regulated by more "
        "than 3 regulators, which disclose the most?",
        "MATCH (org:ORG)-[:Discloses]->(metric:FIN_METRIC) "
        "WITH org, COUNT(DISTINCT metric) AS metrics_disclosed WHERE metrics_disclosed > 50 "
        "MATCH (reg:ORG_REG)-[:Regulates]->(org) "
        "WITH org.name AS organization, metrics_disclosed, COUNT(DISTINCT reg) AS regulator_count "
        "WHERE regulator_count > 3 "
        "RETURN organization, regulator_count, metrics_disclosed "
        "ORDER BY metrics_disclosed DESC LIMIT 10",
        supported=True,
    ),
    CorpusEntry(
        "q13_revenue_cost_metrics",
        "Which organizations disclose a revenue/income/profit metric and also a cost/expense/loss metric?",
        "MATCH (org:ORG)-[:Discloses]->(metric1:FIN_METRIC) "
        "WHERE metric1.name =~ '(?i).*(revenue|income|profit).*' WITH org, metric1 LIMIT 500 "
        "MATCH (org)-[:Discloses]->(metric2:FIN_METRIC) "
        "WHERE metric2.name =~ '(?i).*(cost|expense|loss).*' AND metric2 <> metric1 "
        "RETURN org.name AS organization, metric1.name AS primary_metric, "
        "metric2.name AS correlated_metric, 'Revenue-Cost' AS correlation_type "
        "ORDER BY org.name LIMIT 8",
        supported=True,
    ),
    CorpusEntry(
        "q14_regulators_locations",
        "For financial regulators, which organizations do they regulate and in "
        "which locations do those organizations operate?",
        "MATCH (regulator:ORG_REG)-[:Regulates]->(org:ORG)"
        "-[:Operates_In]->(location:GPE) "
        "WHERE regulator.name CONTAINS 'SEC' OR regulator.name CONTAINS 'Financial' "
        "OR regulator.name CONTAINS 'Exchange' "
        "RETURN regulator.name AS regulator_name, org.name AS regulated_organization, "
        "location.name AS geographic_location ORDER BY regulator.name LIMIT 10",
        supported=True,
    ),
    CorpusEntry(
        "q15_apple_directly_related",
        "What organizations are directly connected to Apple through supply, "
        "stake, or operating-location relationships?",
        "MATCH (apple:ORG) WHERE apple.name CONTAINS 'Apple' OR apple.name CONTAINS 'AAPL' "
        "MATCH (apple)-[edge]-(related:ORG) "
        "WHERE type(edge) IN ['Supplies','Has_Stake_In','Operates_In'] "
        "RETURN apple.name AS apple_entity, type(edge) AS relationship, "
        "related.name AS related_entity LIMIT 5",
        supported=True,
    ),
    CorpusEntry(
        "q16_3hop_dependency_chains",
        "Show 3-hop dependency chains starting from an organization.",
        "MATCH path = (org:ORG)-[:Depends_On]->(:ORG)-[:Depends_On]->(:ORG)"
        "-[:Depends_On]->(:ORG) "
        "RETURN org.name AS organization, [n IN nodes(path) | n.name] AS dependency_chain, "
        "LENGTH(path) + 1 AS chain_length LIMIT 10",
        supported=True,
    ),
    CorpusEntry(
        "q17_bank_disclosure_regulatory",
        "For a set of major firms that disclose more than 10 metrics, who "
        "regulates them and how many other orgs does each regulator oversee?",
        "MATCH (org:ORG) WHERE org.name IN ['AAPL','MSFT','JPM'] "
        "MATCH (org)-[:Discloses]->(metric:FIN_METRIC) "
        "WITH org, COUNT(metric) AS disclosure_strength WHERE disclosure_strength > 10 "
        "MATCH (regulator)-[:Regulates]->(org) "
        "WHERE (regulator:ORG_REG) AND regulator.name IS NOT NULL "
        "MATCH (regulator)-[:Regulates]->(other:ORG) "
        "WITH org, disclosure_strength, regulator, COUNT(other) AS regulatory_scope "
        "WHERE regulatory_scope > 0 "
        "RETURN org.name AS organization_name, disclosure_strength, "
        "regulator.name AS regulator_name, regulatory_scope "
        "ORDER BY disclosure_strength DESC LIMIT 5",
        supported=True,
    ),
    CorpusEntry(
        "q18_circular_deps_all_orgs",
        "Are there circular dependency loops among organizations, 2 to 4 hops back to themselves?",
        "MATCH path = (org:ORG)-[:Depends_On*2..4]->(org) "
        "RETURN org.name AS organization, LENGTH(path) AS cycle_length, "
        "[n IN nodes(path) | n.name] AS cycle_participants LIMIT 10",
        supported=True,
    ),
    CorpusEntry(
        "q19_temporal_risk_chains",
        "Trace impact chains in time order: a risk impacts an org, which then "
        "depends on another org that discloses a metric.",
        "MATCH (risk:RISK_FACTOR)-[imp:Negatively_Impacts]->(org1:ORG)"
        "-[dep:Depends_On]->(org2:ORG)-[disc:Discloses]->(metric:FIN_METRIC) "
        "WHERE imp.startDate IS NOT NULL AND dep.startDate IS NOT NULL "
        "AND imp.startDate <= dep.startDate "
        "RETURN risk.name AS risk, org1.name AS org1, org2.name AS org2, "
        "metric.name AS metric LIMIT 15",
        supported=True,
    ),
    CorpusEntry(
        "q20_location_risk_disclosure",
        "For each location, find orgs operating there that are also negatively "
        "impacted by a risk, and the metrics they disclose.",
        "MATCH (location:GPE)<-[:Operates_In]-(org:ORG) "
        "MATCH (org)<-[:Negatively_Impacts]-(risk) WHERE risk:RISK_FACTOR "
        "MATCH (org)-[:Discloses]->(metric:FIN_METRIC) "
        "RETURN location.name AS location, org.name AS organization, "
        "risk.name AS risk_factor, metric.name AS financial_metric LIMIT 20",
        supported=True,
    ),
    CorpusEntry(
        "q21_two_hop_metadata",
        "For major tech firms, explore two hops out: what entities they disclose "
        "and what those entities in turn relate to.",
        "MATCH (org:ORG) WHERE org.name IN ['AAPL','MSFT'] "
        "MATCH (org)-[meta_edge]->(metadata) "
        "WHERE type(meta_edge) IN ['Discloses'] AND metadata:FIN_METRIC "
        "MATCH (metadata)-[context_edge]->(context) "
        "WHERE type(context_edge) IN ['Discloses'] "
        "RETURN org.name AS organization, metadata.name AS metadata_entity, "
        "context.name AS contextual_entity LIMIT 8",
        supported=True,
    ),
    CorpusEntry(
        "q22_circular_deps_named",
        "Are there circular dependency chains of length 2-3 among named big-tech firms?",
        "MATCH path = (org:ORG)-[:Depends_On*2..3]->(org) "
        "WHERE org.name IN ['aapl','msft','googl'] "
        "RETURN [n IN nodes(path) | n.name] AS circular_chain LIMIT 5",
        supported=True,
    ),
]

_SUPPORTED = [e for e in CORPUS if e.supported]
_GAPS = [e for e in CORPUS if not e.supported]


@pytest.fixture(scope="module")
def bundle():
    return mapping_bundle_for("finreflectkg")


def test_corpus_is_complete() -> None:
    """Guard against accidental drops: the ported set has exactly 22 queries."""
    assert len(CORPUS) == 22
    assert len({e.cid for e in CORPUS}) == 22


@pytest.mark.parametrize("entry", _SUPPORTED, ids=[e.cid for e in _SUPPORTED])
def test_supported_query_transpiles(entry: CorpusEntry, bundle) -> None:
    """Every currently-supported query MUST keep transpiling to non-empty AQL."""
    result = translate(entry.cypher, mapping=bundle)
    assert result.aql.strip(), f"{entry.cid} produced empty AQL"


@pytest.mark.parametrize("entry", _GAPS, ids=[e.cid for e in _GAPS])
def test_known_gap_still_unsupported(entry: CorpusEntry, bundle) -> None:
    """Documents an open coverage gap.

    When the gap is closed this test FAILS on purpose — flip the entry's
    ``supported`` flag to ``True`` and it joins the regression guard above.
    """
    with pytest.raises(CoreError) as exc:
        translate(entry.cypher, mapping=bundle)
    assert entry.gap.lower() in str(exc.value).lower(), (
        f"{entry.cid}: expected gap '{entry.gap}', got '{exc.value}'"
    )


def test_current_coverage_ratio() -> None:
    """Pin coverage so progress is visible in CI.

    Baseline was 15/22. WP-C1 (upper/lower aliases) promoted q03/q04/q06 → 18/22.
    WP-C2 (list subscript/slice) promoted q01 → 19/22. WP-C3 (collect with
    DISTINCT/slice, mixed with aggregates) promoted q05/q09/q10 → 22/22.

    NOTE: transpile-success is not full semantic correctness. WP-S2c closed the
    label-predicate-on-untyped-variable gap (``WHERE risk:RISK_FACTOR`` on
    ``MATCH (risk)`` in q10/q11/q20 now emits the type-discriminator filter
    instead of a no-op — see ``test_label_predicate_on_untyped_var_emits_filter``).
    Remaining WP-S items tracked in docs/cypher_coverage_plan.md are the NL
    "return a graph" / approximate-match concerns.
    """
    assert len(_SUPPORTED) == 22
    assert len(_GAPS) == 0


@pytest.mark.parametrize(
    "cid",
    ["q10_risk_dependency_disclosure", "q11_geo_risk_3hop", "q20_location_risk_disclosure"],
)
def test_label_predicate_on_untyped_var_emits_filter(cid: str, bundle) -> None:
    """WP-S2c regression guard.

    These three queries carry ``WHERE risk:RISK_FACTOR [OR risk:EVENT]`` over an
    untyped ``risk`` variable. Pre-WP-S2c the label suffix was dropped and the
    predicate became a no-op (returning every row). Assert the discriminator
    filter now materialises in the AQL + bind vars.
    """
    entry = next(e for e in CORPUS if e.cid == cid)
    result = translate(entry.cypher, mapping=bundle)
    assert "riskLabel" in "".join(result.bind_vars.keys()), (
        f"{cid}: label predicate produced no discriminator bind var"
    )
    assert "RISK_FACTOR" in result.bind_vars.values()
    assert "risk[@" in result.aql and "Label" in result.aql
