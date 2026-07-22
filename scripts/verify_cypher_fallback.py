"""Live walk-through of the Cypher->AQL AI fallback (PRD §18).

Exercises the exact path the workbench "Generate AQL with AI" button drives,
against the live FinReflectKG database and a live LLM:

  1. translate() rejects an unsupported Cypher query with CoreError UNSUPPORTED
     (this is what /execute turns into HTTP 422).
  2. POST /execute returns 422 with {"code": "UNSUPPORTED"} over the wire.
  3. POST /nl2aql {cypher: <failing cypher>} -> LLM translates Cypher->AQL.
  4. POST /execute-aql runs that AQL (Layer 4/5 still apply) -> real rows.

Run OUTSIDE the sandbox (needs the remote DB + LLM):
  .venv/bin/python scripts/verify_cypher_fallback.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests
from arango import ArangoClient
from arango_query_core import MappingResolver
from arango_query_core.errors import CoreError

from arango_cypher.api import translate
from arango_cypher.schema_acquire import get_mapping

BASE = os.environ.get("VERIFY_BASE", "http://localhost:8001")


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    dotenv = Path(__file__).resolve().parent.parent / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            env.setdefault(k.strip(), v)  # real env wins
            if k.strip() not in os.environ:
                env[k.strip()] = v
    return env


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    env = load_env()
    url = env["ARANGO_URL"]
    dbn = env["ARANGO_DB"]
    user = env.get("ARANGO_USER", "root")
    pw = env["ARANGO_PASSWORD"]

    banner(f"0. Connect to {dbn} @ {url} and build mapping")
    db = ArangoClient(hosts=url).db(dbn, username=user, password=pw, verify=True)
    print("server version:", db.version())
    # Cache the (slow) live introspection so re-runs are fast.
    cache = Path("/tmp/finreflect_mapping.json")
    if cache.exists():
        print("using cached mapping:", cache)
        from arango_query_core.mapping import mapping_from_wire_dict

        bundle = mapping_from_wire_dict(json.loads(cache.read_text()))
    else:
        bundle = get_mapping(db)
        cache.write_text(
            json.dumps(
                {
                    "conceptual_schema": bundle.conceptual_schema,
                    "physical_mapping": bundle.physical_mapping,
                    "metadata": bundle.metadata,
                }
            )
        )
        print("introspected and cached mapping:", cache)
    res = MappingResolver(bundle)
    labels = sorted(res.all_entity_labels())
    rels = sorted(res.all_relationship_types())
    print(f"entities ({len(labels)}):", labels)
    print(f"relationship types ({len(rels)}):", rels)

    if len(rels) < 2:
        print("!! need >=2 relationship types to build a multi-type-edge example")
        return 2
    t1, t2 = rels[0], rels[1]
    cypher = (
        f"MATCH (a)-[r:`{t1}`|`{t2}`]->(b) RETURN type(r) AS relType, count(*) AS c ORDER BY c DESC LIMIT 5"
    )

    banner("1. Deterministic transpiler must REJECT this Cypher")
    print("cypher:", cypher)
    try:
        translate(cypher, mapping=bundle)
        print("!! UNEXPECTED: transpiler accepted the query (no fallback needed)")
        return 3
    except CoreError as e:
        print(f"-> CoreError code={e.code}: {e}")
        if e.code not in {"UNSUPPORTED", "NOT_IMPLEMENTED"}:
            print("!! code is not a recoverable transpile code; UI would NOT offer fallback")
            return 4

    mapping_wire = {
        "conceptual_schema": bundle.conceptual_schema,
        "physical_mapping": bundle.physical_mapping,
        "metadata": bundle.metadata,
    }

    banner("2. HTTP: POST /connect -> session token")
    conn = requests.post(
        f"{BASE}/connect",
        json={"url": url, "database": dbn, "username": user, "password": pw},
        timeout=60,
    )
    conn.raise_for_status()
    token = conn.json()["token"]
    headers = {"X-Arango-Session": token}
    print("token acquired:", token[:8] + "...")

    banner("3. HTTP: POST /execute -> expect 422 UNSUPPORTED (the dead-end users saw)")
    ex = requests.post(
        f"{BASE}/execute",
        json={"cypher": cypher, "mapping": mapping_wire},
        headers=headers,
        timeout=60,
    )
    print("status:", ex.status_code)
    print("body:", json.dumps(ex.json(), indent=2)[:600])
    code = (ex.json().get("detail") or {}).get("code") if isinstance(ex.json().get("detail"), dict) else None
    if ex.status_code != 422 or code not in {"UNSUPPORTED", "NOT_IMPLEMENTED"}:
        print("!! expected 422 with a recoverable code")
        return 5

    banner("4. HTTP: POST /nl2aql {cypher: ...} -> LLM translates Cypher->AQL")
    fb = requests.post(
        f"{BASE}/nl2aql",
        json={"question": "", "mapping": mapping_wire, "cypher": cypher},
        timeout=120,
    )
    print("status:", fb.status_code)
    fbj = fb.json()
    aql = fbj.get("aql")
    print(
        "method:", fbj.get("method"), "| confidence:", fbj.get("confidence"), "| ms:", fbj.get("elapsed_ms")
    )
    print("generated AQL:\n", aql)
    if not aql:
        print("!! LLM produced no AQL; explanation:", fbj.get("explanation"))
        return 6

    banner("5. HTTP: POST /execute-aql -> run the generated AQL (Layer 4/5 apply)")
    run = requests.post(
        f"{BASE}/execute-aql",
        json={"aql": aql, "bind_vars": fbj.get("bind_vars") or {}, "mapping": mapping_wire},
        headers=headers,
        timeout=120,
    )
    print("status:", run.status_code)
    body = run.json()
    if run.status_code != 200:
        print("body:", json.dumps(body, indent=2)[:800])
        print("!! execution of generated AQL failed")
        return 7
    rows = body.get("results", [])
    print(f"rows returned: {len(rows)}")
    print(json.dumps(rows, indent=2)[:800])

    banner("RESULT: PASS - Cypher 422 -> AI AQL -> live rows. End-to-end fallback verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
