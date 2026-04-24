"""CLI entry point for arango-cypher-py: Cypher → AQL transpiler."""
# ruff: noqa: B008  — typer.Option / typer.Argument in signatures is idiomatic
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from arango_query_core import CoreError, MappingBundle, MappingSource, mapping_from_wire_dict

app = typer.Typer(
    name="arango-cypher-py",
    help="Cypher → AQL transpiler for ArangoDB",
    no_args_is_help=True,
)
console = Console(stderr=True)
out = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_mapping(
    mapping_file: Path | None,
    mapping_json: str | None,
) -> MappingBundle | None:
    """Build a MappingBundle from a file path or inline JSON string."""
    raw: dict[str, Any] | None = None

    if mapping_file is not None:
        try:
            raw = json.loads(mapping_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            console.print(f"[red]Failed to read mapping file: {exc}[/red]")
            raise typer.Exit(1) from exc
    elif mapping_json is not None:
        try:
            raw = json.loads(mapping_json)
        except json.JSONDecodeError as exc:
            console.print(f"[red]Invalid mapping JSON: {exc}[/red]")
            raise typer.Exit(1) from exc

    if raw is None:
        return None

    return mapping_from_wire_dict(
        raw,
        source=MappingSource(
            kind="explicit",
            notes=f"from {mapping_file}" if mapping_file else "inline JSON",
        ),
    )


def _read_cypher(cypher: str | None) -> str:
    """Return cypher from argument or stdin; exit if empty."""
    if cypher is not None:
        return cypher
    if not sys.stdin.isatty():
        cypher = sys.stdin.read().strip()
    if not cypher:
        console.print("[red]No Cypher query provided. Pass as argument or pipe via stdin.[/red]")
        raise typer.Exit(1)
    return cypher


def _connect(
    host: str | None,
    port: int | None,
    db: str | None,
    user: str | None,
    password: str | None,
) -> Any:
    """Create a python-arango StandardDatabase from flags / env vars / defaults."""
    from arango import ArangoClient

    h = host or os.getenv("ARANGO_HOST", "localhost")
    p = port or int(os.getenv("ARANGO_PORT", "8529"))
    d = db or os.getenv("ARANGO_DB", "_system")
    u = user or os.getenv("ARANGO_USER", "root")
    pw = password if password is not None else os.getenv("ARANGO_PASSWORD", "")
    client = ArangoClient(hosts=f"http://{h}:{p}")
    return client.db(d, username=u, password=pw)


def _parse_params(params_json: str | None) -> dict[str, Any] | None:
    if params_json is None:
        return None
    try:
        return json.loads(params_json)
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid --params JSON: {exc}[/red]")
        raise typer.Exit(1) from exc


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@app.command()
def translate(
    cypher: str = typer.Argument(None, help="Cypher query (reads stdin if omitted)"),
    mapping_file: Path = typer.Option(None, "--mapping-file", "-m", help="Path to mapping JSON file"),
    mapping_json: str = typer.Option(None, "--mapping-json", help="Inline mapping JSON"),
    extensions: bool = typer.Option(True, "--extensions/--no-extensions"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    params_json: str = typer.Option(None, "--params", "-p", help="Query parameters as JSON"),
) -> None:
    """Translate Cypher to AQL. Reads from stdin if no argument given."""
    from arango_cypher.api import translate as do_translate

    cypher = _read_cypher(cypher)
    bundle = _load_mapping(mapping_file, mapping_json)
    if bundle is None:
        console.print(
            "[red]No mapping provided.[/red] "
            "Use [bold]--mapping-file[/bold] or [bold]--mapping-json[/bold], "
            "or use the [bold]run[/bold] subcommand to auto-acquire from a live database."
        )
        raise typer.Exit(1)

    params = _parse_params(params_json)

    try:
        result = do_translate(cypher, mapping=bundle, params=params)
    except CoreError as exc:
        console.print(f"[red]Translation error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        out.print(json.dumps({"aql": result.aql, "bind_vars": result.bind_vars}, indent=2))
    else:
        out.print(Syntax(result.aql, "sql", theme="monokai"))
        if result.bind_vars:
            out.print("\n[bold]Bind variables:[/bold]")
            out.print(json.dumps(result.bind_vars, indent=2))


@app.command()
def run(
    cypher: str = typer.Argument(None, help="Cypher query (reads stdin if omitted)"),
    mapping_file: Path = typer.Option(None, "--mapping-file", "-m", help="Path to mapping JSON file"),
    mapping_json: str = typer.Option(None, "--mapping-json", help="Inline mapping JSON"),
    host: str = typer.Option(None, "--host", help="ArangoDB host"),
    port: int = typer.Option(None, "--port", help="ArangoDB port"),
    db: str = typer.Option(None, "--db", help="Database name"),
    user: str = typer.Option(None, "--user", help="Username"),
    password: str = typer.Option(None, "--password", help="Password"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    params_json: str = typer.Option(None, "--params", "-p", help="Query parameters as JSON"),
) -> None:
    """Translate and execute Cypher against ArangoDB."""
    from arango_cypher.api import execute as do_execute
    from arango_cypher.schema_acquire import get_mapping

    cypher = _read_cypher(cypher)
    params = _parse_params(params_json)

    try:
        database = _connect(host, port, db, user, password)
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    bundle = _load_mapping(mapping_file, mapping_json)
    if bundle is None:
        console.print("[dim]No mapping file provided — acquiring from database…[/dim]")
        try:
            bundle = get_mapping(database)
        except Exception as exc:
            console.print(f"[red]Failed to acquire mapping:[/red] {exc}")
            raise typer.Exit(1) from exc

    try:
        cursor = do_execute(cypher, db=database, mapping=bundle, params=params)
        rows = list(cursor)
    except CoreError as exc:
        console.print(f"[red]Execution error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        out.print(json.dumps(rows, indent=2, default=str))
    else:
        _print_result_table(rows)


@app.command()
def mapping(
    host: str = typer.Option(None, "--host", help="ArangoDB host"),
    port: int = typer.Option(None, "--port", help="ArangoDB port"),
    db: str = typer.Option(None, "--db", help="Database name"),
    user: str = typer.Option(None, "--user", help="Username"),
    password: str = typer.Option(None, "--password", help="Password"),
    strategy: str = typer.Option("auto", "--strategy", "-s", help="auto | analyzer | heuristic"),
    owl_output: Path = typer.Option(None, "--owl-output", help="Write OWL Turtle to file"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Print mapping summary for a database."""
    from arango_cypher.schema_acquire import get_mapping
    from arango_query_core import MappingResolver

    try:
        database = _connect(host, port, db, user, password)
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    try:
        bundle = get_mapping(database, strategy=strategy, include_owl=bool(owl_output))
    except Exception as exc:
        console.print(f"[red]Failed to acquire mapping:[/red] {exc}")
        raise typer.Exit(1) from exc

    resolver = MappingResolver(bundle)
    summary = resolver.schema_summary()

    if json_output:
        out.print(json.dumps(summary, indent=2, default=str))
    else:
        _print_mapping_summary(summary)

    if owl_output and bundle.owl_turtle:
        owl_output.write_text(bundle.owl_turtle)
        console.print(f"[green]OWL Turtle written to {owl_output}[/green]")


@app.command()
def doctor(
    host: str = typer.Option(None, "--host", help="ArangoDB host"),
    port: int = typer.Option(None, "--port", help="ArangoDB port"),
    db: str = typer.Option(None, "--db", help="Database name"),
    user: str = typer.Option(None, "--user", help="Username"),
    password: str = typer.Option(None, "--password", help="Password"),
) -> None:
    """Check connectivity, collections, and schema analyzer availability."""
    h = host or os.getenv("ARANGO_HOST", "localhost")
    p = port or int(os.getenv("ARANGO_PORT", "8529"))
    d = db or os.getenv("ARANGO_DB", "_system")

    out.print(f"[bold]Target:[/bold] http://{h}:{p}  db={d}")
    out.print()

    # --- connectivity ---
    try:
        database = _connect(host, port, db, user, password)
        database.version()
        out.print("[green]✓[/green] ArangoDB connection … OK")
    except Exception as exc:
        out.print(f"[red]✗[/red] ArangoDB connection … FAILED ({exc})")
        database = None

    # --- collections ---
    if database is not None:
        try:
            cols = database.collections()
            user_cols = [c["name"] for c in cols if isinstance(c, dict) and not c["name"].startswith("_")]
            out.print(f"[green]✓[/green] Collections … {len(user_cols)} user collection(s)")
            if user_cols:
                out.print(f"    {', '.join(sorted(user_cols)[:20])}")
        except Exception as exc:
            out.print(f"[red]✗[/red] Collections … FAILED ({exc})")

    # --- schema analyzer ---
    try:
        import schema_analyzer  # noqa: F401

        out.print("[green]✓[/green] arangodb-schema-analyzer … installed")
    except ImportError:
        out.print("[yellow]○[/yellow] arangodb-schema-analyzer … not installed (optional)")

    # --- classify ---
    bundle = None
    if database is not None:
        try:
            from arango_cypher.schema_acquire import classify_schema

            schema_type = classify_schema(database)
            out.print(f"[green]✓[/green] Schema classification … {schema_type}")
        except Exception as exc:
            out.print(f"[yellow]○[/yellow] Schema classification … skipped ({exc})")

    # --- VCI checks ---
    if database is not None:
        try:
            from arango_cypher.schema_acquire import get_mapping
            from arango_query_core import MappingResolver

            if bundle is None:
                bundle = get_mapping(database)
            resolver = MappingResolver(bundle)
            vci_issues: list[tuple[str, str, str]] = []
            for rtype in resolver.all_relationship_types():
                rmap = resolver.resolve_relationship(rtype)
                if rmap.get("style") != "GENERIC_WITH_TYPE":
                    continue
                if resolver.has_vci(rtype):
                    continue
                edge_coll = rmap.get("edgeCollectionName", "?")
                type_field = rmap.get("typeField", "type")
                vci_issues.append((rtype, edge_coll, type_field))

            if vci_issues:
                out.print()
                out.print(f"[yellow]⚠[/yellow]  Missing VCI indexes ({len(vci_issues)} relationship(s)):")
                seen_colls: set[str] = set()
                for rtype, edge_coll, type_field in vci_issues:
                    out.print(f"    [yellow]•[/yellow] '{rtype}' on edge collection '{edge_coll}' (type field: '{type_field}')")
                    if edge_coll not in seen_colls:
                        seen_colls.add(edge_coll)
                        out.print(
                            f"      [dim]Suggestion:[/dim] "
                            f'db.{edge_coll}.ensureIndex({{ type: "persistent", fields: ["{type_field}"], inBackground: true }})'
                        )
            else:
                has_gwt = any(
                    resolver.resolve_relationship(rt).get("style") == "GENERIC_WITH_TYPE"
                    for rt in resolver.all_relationship_types()
                )
                if has_gwt:
                    out.print("[green]✓[/green] VCI indexes … all GENERIC_WITH_TYPE relationships covered")
        except Exception as exc:
            out.print(f"[yellow]○[/yellow] VCI check … skipped ({exc})")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _print_result_table(rows: list[Any]) -> None:
    if not rows:
        out.print("[dim]No results.[/dim]")
        return

    if isinstance(rows[0], dict):
        table = Table(show_header=True, header_style="bold cyan")
        keys = list(rows[0].keys())
        for k in keys:
            table.add_column(k)
        for row in rows:
            table.add_row(*(str(row.get(k, "")) for k in keys))
        out.print(table)
    else:
        for row in rows:
            out.print(row)


def _print_mapping_summary(summary: dict[str, Any]) -> None:
    entities = summary.get("entities", [])
    rels = summary.get("relationships", [])

    if entities:
        t = Table(title="Entities", show_header=True, header_style="bold cyan")
        t.add_column("Label")
        t.add_column("Collection")
        t.add_column("Style")
        t.add_column("Properties")
        for e in entities:
            props = ", ".join(e.get("properties", {}).keys()) or "—"
            t.add_row(e["label"], e.get("collection", ""), e.get("style", ""), props)
        out.print(t)

    if rels:
        t = Table(title="Relationships", show_header=True, header_style="bold cyan")
        t.add_column("Type")
        t.add_column("Edge Collection")
        t.add_column("Style")
        t.add_column("Domain → Range")
        for r in rels:
            dr = f"{r.get('domain', '?')} → {r.get('range', '?')}"
            t.add_row(r["type"], r.get("edgeCollection", ""), r.get("style", ""), dr)
        out.print(t)

    if not entities and not rels:
        out.print("[dim]Empty mapping — no entities or relationships found.[/dim]")
