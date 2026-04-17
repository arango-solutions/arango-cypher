#!/usr/bin/env python3
"""Download and seed the ICIJ Paradise Papers dataset into ArangoDB.

Usage::

    python scripts/download_icij_dataset.py [--url URL] [--db DB_NAME] [--csv-dir DIR]

The full dataset is available from https://offshoreleaks.icij.org/pages/database
(requires accepting terms of use).  This script can ingest the CSV files if
already downloaded, or generate a small sample dataset for integration testing.

CSV files expected (when using real data)::

    paradise_papers.nodes.entity.csv
    paradise_papers.nodes.officer.csv
    paradise_papers.nodes.intermediary.csv
    paradise_papers.nodes.address.csv
    paradise_papers.edges.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from io import StringIO
from pathlib import Path

# ── Schema constants ─────────────────────────────────────────────────

DOCUMENT_COLLECTIONS = ["Entity", "Officer", "Intermediary", "Address"]
EDGE_COLLECTIONS = ["officer_of", "intermediary_of", "registered_address", "similar_name"]

MAPPING = {
    "conceptual_schema": {
        "entities": [
            {
                "name": "Entity",
                "labels": ["Entity"],
                "properties": [
                    {"name": "name"},
                    {"name": "jurisdiction"},
                    {"name": "incorporation_date"},
                    {"name": "status"},
                ],
            },
            {
                "name": "Officer",
                "labels": ["Officer"],
                "properties": [{"name": "name"}, {"name": "country"}],
            },
            {
                "name": "Intermediary",
                "labels": ["Intermediary"],
                "properties": [
                    {"name": "name"},
                    {"name": "country"},
                    {"name": "status"},
                ],
            },
            {
                "name": "Address",
                "labels": ["Address"],
                "properties": [{"name": "address"}, {"name": "country"}],
            },
        ],
        "relationships": [
            {"type": "OFFICER_OF", "fromEntity": "Officer", "toEntity": "Entity", "properties": []},
            {"type": "INTERMEDIARY_OF", "fromEntity": "Intermediary", "toEntity": "Entity", "properties": []},
            {"type": "REGISTERED_ADDRESS", "fromEntity": "Entity", "toEntity": "Address", "properties": []},
            {
                "type": "SIMILAR_NAME",
                "fromEntity": "Entity",
                "toEntity": "Entity",
                "properties": [{"name": "similarity"}],
            },
        ],
    },
    "physical_mapping": {
        "entities": {
            "Entity": {
                "style": "COLLECTION",
                "collectionName": "Entity",
                "properties": {
                    "name": {"field": "name"},
                    "jurisdiction": {"field": "jurisdiction"},
                    "incorporation_date": {"field": "incorporation_date"},
                    "status": {"field": "status"},
                },
            },
            "Officer": {
                "style": "COLLECTION",
                "collectionName": "Officer",
                "properties": {
                    "name": {"field": "name"},
                    "country": {"field": "country_codes"},
                },
            },
            "Intermediary": {
                "style": "COLLECTION",
                "collectionName": "Intermediary",
                "properties": {
                    "name": {"field": "name"},
                    "country": {"field": "country_codes"},
                    "status": {"field": "status"},
                },
            },
            "Address": {
                "style": "COLLECTION",
                "collectionName": "Address",
                "properties": {
                    "address": {"field": "address"},
                    "country": {"field": "countries"},
                },
            },
        },
        "relationships": {
            "OFFICER_OF": {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": "officer_of",
                "domain": "Officer",
                "range": "Entity",
            },
            "INTERMEDIARY_OF": {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": "intermediary_of",
                "domain": "Intermediary",
                "range": "Entity",
            },
            "REGISTERED_ADDRESS": {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": "registered_address",
                "domain": "Entity",
                "range": "Address",
            },
            "SIMILAR_NAME": {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": "similar_name",
                "domain": "Entity",
                "range": "Entity",
            },
        },
    },
    "metadata": {"provider": "icij_paradise_papers"},
}


# ── Sample data for testing (no real download needed) ────────────────

SAMPLE_ENTITIES = [
    {"_key": "e1", "name": "Acme Holdings Ltd", "jurisdiction": "BVI", "incorporation_date": "2005-03-15", "status": "Active"},
    {"_key": "e2", "name": "Global Ventures Inc", "jurisdiction": "Panama", "incorporation_date": "2010-08-22", "status": "Active"},
    {"_key": "e3", "name": "Oceanic Trust Corp", "jurisdiction": "Cayman Islands", "incorporation_date": "2007-01-10", "status": "Struck Off"},
    {"_key": "e4", "name": "Pacific Trading Co", "jurisdiction": "BVI", "incorporation_date": "2012-06-01", "status": "Active"},
    {"_key": "e5", "name": "Alpine Finance SA", "jurisdiction": "Switzerland", "incorporation_date": "2003-11-20", "status": "Active"},
]

SAMPLE_OFFICERS = [
    {"_key": "o1", "name": "John Smith", "country_codes": "GBR"},
    {"_key": "o2", "name": "Maria Garcia", "country_codes": "ESP"},
    {"_key": "o3", "name": "Hans Mueller", "country_codes": "CHE"},
    {"_key": "o4", "name": "Li Wei", "country_codes": "CHN"},
]

SAMPLE_INTERMEDIARIES = [
    {"_key": "i1", "name": "Mossack Fonseca", "country_codes": "PAN", "status": "Active"},
    {"_key": "i2", "name": "Appleby Services", "country_codes": "BMU", "status": "Active"},
]

SAMPLE_ADDRESSES = [
    {"_key": "a1", "address": "123 Palm Avenue, Road Town", "countries": "VGB"},
    {"_key": "a2", "address": "45 Harbour Drive, George Town", "countries": "CYM"},
    {"_key": "a3", "address": "78 Bahnhofstrasse, Zurich", "countries": "CHE"},
]

SAMPLE_EDGES = [
    {"_from": "Officer/o1", "_to": "Entity/e1", "_collection": "officer_of"},
    {"_from": "Officer/o1", "_to": "Entity/e2", "_collection": "officer_of"},
    {"_from": "Officer/o2", "_to": "Entity/e2", "_collection": "officer_of"},
    {"_from": "Officer/o3", "_to": "Entity/e5", "_collection": "officer_of"},
    {"_from": "Officer/o4", "_to": "Entity/e4", "_collection": "officer_of"},
    {"_from": "Intermediary/i1", "_to": "Entity/e1", "_collection": "intermediary_of"},
    {"_from": "Intermediary/i1", "_to": "Entity/e2", "_collection": "intermediary_of"},
    {"_from": "Intermediary/i2", "_to": "Entity/e3", "_collection": "intermediary_of"},
    {"_from": "Intermediary/i2", "_to": "Entity/e4", "_collection": "intermediary_of"},
    {"_from": "Entity/e1", "_to": "Address/a1", "_collection": "registered_address"},
    {"_from": "Entity/e3", "_to": "Address/a2", "_collection": "registered_address"},
    {"_from": "Entity/e5", "_to": "Address/a3", "_collection": "registered_address"},
]


def seed_sample_data(db) -> dict[str, int]:
    """Insert sample data into ArangoDB collections. Returns insert counts."""
    counts: dict[str, int] = {}

    for coll_name in DOCUMENT_COLLECTIONS:
        if not db.has_collection(coll_name):
            db.create_collection(coll_name)

    for coll_name in EDGE_COLLECTIONS:
        if not db.has_collection(coll_name):
            db.create_collection(coll_name, edge=True)

    data_map = {
        "Entity": SAMPLE_ENTITIES,
        "Officer": SAMPLE_OFFICERS,
        "Intermediary": SAMPLE_INTERMEDIARIES,
        "Address": SAMPLE_ADDRESSES,
    }
    for coll_name, docs in data_map.items():
        coll = db.collection(coll_name)
        for doc in docs:
            coll.insert(doc, overwrite=True, silent=True)
        counts[coll_name] = len(docs)

    edge_groups: dict[str, list] = {}
    for edge in SAMPLE_EDGES:
        coll_name = edge["_collection"]
        edge_groups.setdefault(coll_name, []).append(
            {"_from": edge["_from"], "_to": edge["_to"]}
        )

    for coll_name, edges in edge_groups.items():
        coll = db.collection(coll_name)
        for edge in edges:
            coll.insert(edge, overwrite=True, silent=True)
        counts[coll_name] = len(edges)

    return counts


def seed_from_csv(db, csv_dir: Path) -> dict[str, int]:
    """Seed ArangoDB from ICIJ Paradise Papers CSV files."""
    counts: dict[str, int] = {}

    csv_map = {
        "Entity": "paradise_papers.nodes.entity.csv",
        "Officer": "paradise_papers.nodes.officer.csv",
        "Intermediary": "paradise_papers.nodes.intermediary.csv",
        "Address": "paradise_papers.nodes.address.csv",
    }

    for coll_name in DOCUMENT_COLLECTIONS:
        if not db.has_collection(coll_name):
            db.create_collection(coll_name)

    for coll_name in EDGE_COLLECTIONS:
        if not db.has_collection(coll_name):
            db.create_collection(coll_name, edge=True)

    for coll_name, csv_file in csv_map.items():
        csv_path = csv_dir / csv_file
        if not csv_path.exists():
            print(f"  SKIP: {csv_file} not found")
            continue

        coll = db.collection(coll_name)
        count = 0
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            batch = []
            for row in reader:
                doc = {k: v for k, v in row.items() if v}
                if "node_id" in doc:
                    doc["_key"] = str(doc.pop("node_id"))
                batch.append(doc)
                if len(batch) >= 1000:
                    coll.import_bulk(batch, on_duplicate="replace")
                    count += len(batch)
                    batch = []
            if batch:
                coll.import_bulk(batch, on_duplicate="replace")
                count += len(batch)
        counts[coll_name] = count
        print(f"  {coll_name}: {count} documents")

    edges_csv = csv_dir / "paradise_papers.edges.csv"
    if edges_csv.exists():
        edge_count = 0
        rel_type_map = {
            "officer_of": "officer_of",
            "intermediary_of": "intermediary_of",
            "registered_address": "registered_address",
            "similar": "similar_name",
        }
        with open(edges_csv, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            edge_batches: dict[str, list] = {name: [] for name in EDGE_COLLECTIONS}
            for row in reader:
                rel = row.get("rel_type", "").lower()
                coll_name = rel_type_map.get(rel, "officer_of")
                start_id = row.get("START_ID", row.get("node_id_start", ""))
                end_id = row.get("END_ID", row.get("node_id_end", ""))
                if not start_id or not end_id:
                    continue
                edge_batches[coll_name].append({
                    "_from": f"Entity/{start_id}",
                    "_to": f"Entity/{end_id}",
                })

            for coll_name, edges in edge_batches.items():
                if edges:
                    db.collection(coll_name).import_bulk(edges, on_duplicate="replace")
                    counts[coll_name] = len(edges)
                    edge_count += len(edges)
        print(f"  Edges total: {edge_count}")
    else:
        print("  SKIP: paradise_papers.edges.csv not found")

    return counts


def save_mapping(output_path: Path) -> None:
    """Write the ICIJ mapping bundle to a JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(MAPPING, f, indent=2)
        f.write("\n")
    print(f"Mapping saved to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed ICIJ Paradise Papers into ArangoDB")
    parser.add_argument("--url", default="http://localhost:8529", help="ArangoDB URL")
    parser.add_argument("--db", default="icij_paradise_papers", help="Database name")
    parser.add_argument("--csv-dir", default=None, help="Directory with ICIJ CSV files")
    parser.add_argument("--sample", action="store_true", help="Use built-in sample data instead of CSV")
    parser.add_argument("--save-mapping", default=None, help="Path to write mapping JSON")
    args = parser.parse_args()

    if args.save_mapping:
        save_mapping(Path(args.save_mapping))
        if not args.csv_dir and not args.sample:
            return

    try:
        from arango import ArangoClient
    except ImportError:
        print("ERROR: python-arango is required.  pip install python-arango", file=sys.stderr)
        sys.exit(1)

    client = ArangoClient(hosts=args.url)
    sys_db = client.db("_system", username="root", password="")

    if not sys_db.has_database(args.db):
        sys_db.create_database(args.db)
        print(f"Created database: {args.db}")

    db = client.db(args.db, username="root", password="")

    if args.csv_dir:
        csv_path = Path(args.csv_dir)
        if not csv_path.is_dir():
            print(f"ERROR: CSV directory not found: {csv_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Seeding from CSV files in {csv_path} ...")
        counts = seed_from_csv(db, csv_path)
    else:
        print("Seeding sample data ...")
        counts = seed_sample_data(db)

    print("\n=== Seed Statistics ===")
    for name, count in sorted(counts.items()):
        print(f"  {name}: {count}")
    print(f"  Total: {sum(counts.values())}")


if __name__ == "__main__":
    main()
