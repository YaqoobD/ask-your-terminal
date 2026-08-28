"""Resolves the canonical metric registry against a client mapping and the
warehouse schema. Raises before any SQL is built if a mapping points at a
table or column that doesn't exist, so a bad client config fails at load
time, not mid-query.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

REGISTRY_DIR = Path(__file__).parent
CANONICAL_PATH = REGISTRY_DIR / "canonical_metrics.yml"
CLIENTS_DIR = REGISTRY_DIR / "clients"
DB_PATH = REGISTRY_DIR.parent / "data" / "terminal.duckdb"


class RegistryError(Exception):
    """A client mapping or canonical signal references something the warehouse doesn't have."""


@dataclass
class ResolvedRegistry:
    client_id: str
    terminal: str
    timezone_offset_hours: int
    berth_alias_prefix: str
    soft_delete_columns: dict
    declared_signals: list
    signals: dict
    metrics: dict


def load_canonical(path: Path = CANONICAL_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def load_client(client_id: str) -> dict:
    path = CLIENTS_DIR / f"{client_id}.yml"
    if not path.exists():
        raise RegistryError(f"no client mapping for '{client_id}' at {path}")
    return yaml.safe_load(path.read_text())


def _table_schema(con: duckdb.DuckDBPyConnection) -> dict:
    tables = [r[0] for r in con.execute("show tables").fetchall()]
    return {
        t: {row[1] for row in con.execute(f"pragma table_info('{t}')").fetchall()}
        for t in tables
    }


def _require_column(schema: dict, table: str, column: str, context: str) -> None:
    if table not in schema:
        raise RegistryError(f"{context}: table '{table}' does not exist in the warehouse")
    if column not in schema[table]:
        raise RegistryError(f"{context}: column '{column}' does not exist on table '{table}'")


def resolve_from_dicts(canonical: dict, client: dict, con: duckdb.DuckDBPyConnection) -> ResolvedRegistry:
    schema = _table_schema(con)
    signals = canonical["signals"]
    metrics = canonical["metrics"]

    for name, signal in signals.items():
        if signal.get("virtual"):
            continue
        if signal["table"] not in schema:
            raise RegistryError(f"canonical signal '{name}': table '{signal['table']}' does not exist")

    for table, column in client.get("soft_delete_columns", {}).items():
        _require_column(schema, table, column, f"client '{client['client_id']}' soft_delete_columns")

    for signal_name, override in client.get("column_overrides", {}).items():
        if signal_name not in signals:
            raise RegistryError(f"client '{client['client_id']}' overrides unknown signal '{signal_name}'")
        _require_column(
            schema, override["table"], override["column"],
            f"client '{client['client_id']}' column_overrides['{signal_name}']",
        )

    for signal_name in client.get("declared_signals", []):
        if signal_name not in signals:
            raise RegistryError(f"client '{client['client_id']}' declares unknown signal '{signal_name}'")

    return ResolvedRegistry(
        client_id=client["client_id"],
        terminal=client["terminal"],
        timezone_offset_hours=client["timezone_offset_hours"],
        berth_alias_prefix=client["berth_alias_prefix"],
        soft_delete_columns=client.get("soft_delete_columns", {}),
        declared_signals=client.get("declared_signals", []),
        signals=signals,
        metrics=metrics,
    )


def resolve(client_id: str, db_path: Path = DB_PATH) -> ResolvedRegistry:
    canonical = load_canonical()
    client = load_client(client_id)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return resolve_from_dicts(canonical, client, con)
    finally:
        con.close()


def check_drift(canonical: dict, tmdl_measures: list) -> list:
    """Flags a canonical metric whose TMDL-derived description disagrees with
    its registry definition. Returns a list of {metric, canonical, tmdl} dicts,
    one per disagreement.
    """
    metrics = canonical["metrics"]
    drift = []
    for measure in tmdl_measures:
        key = measure.get("metric_key")
        if key is None or key not in metrics:
            continue
        canonical_def = metrics[key]["definition"]
        if measure["description"].strip() != canonical_def.strip():
            drift.append({
                "metric": key,
                "canonical": canonical_def,
                "tmdl": measure["description"],
            })
    return drift
