"""Intent -> tenant-scoped DuckDB SQL.

tenant_id is a required keyword argument taken from the authenticated session,
never from the intent (QueryIntent has no field for it). compile() resolves
the client mapping for tenant_id itself, then injects `terminal = ?` as a
bound parameter on every base relation and joined relation it builds, ahead
of any user-derived predicate. Soft-delete exclusion, as-of pinning, and
tenant-ownership checks on filters all happen here, before any SQL string
is returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

from core.intent import QueryIntent
from core.timewindow import resolve_time_window
from registry.load import DB_PATH, ResolvedRegistry, load_canonical, resolve

AGG_SQL = {
    "sum": "sum({expr})",
    "count": "count({expr})",
    "count_distinct": "count(distinct {expr})",
}

_DIMENSIONS = load_canonical().get("dimensions", {})


class TenantScopeError(Exception):
    """An intent's filters name a terminal, berth, or asset the tenant does not own."""


class CompileError(Exception):
    """An intent can't be compiled for a reason unrelated to tenant scope."""


@dataclass
class CompiledQuery:
    sql: str
    params: list
    lineage: dict
    definition: str


def _dim_ref(dim: str, table: str) -> tuple[str, str | None]:
    """Returns (select_expr, join_sql) for `dim` against `table`. join_sql is
    None if the dimension is native to `table` or computed from its own columns.
    """
    spec = _DIMENSIONS.get(dim)
    if spec is None:
        raise CompileError(f"unknown dimension '{dim}'")
    if "column" in spec:
        return f"{table}.{spec['column']}", None
    if "expr" in spec:
        return spec["expr"], None
    join = spec["join"]
    jtable, on, column = join["table"], join["on"], join["column"]
    join_sql = f"left join {jtable} on {table}.{on} = {jtable}.{on} and {jtable}.terminal = ?"
    return f"{jtable}.{column}", join_sql


def _signal_subquery(
    signal_name: str,
    alias: str,
    *,
    signals: dict,
    dims: list[str],
    resolved: ResolvedRegistry,
    intent: QueryIntent,
    start,
    end,
    as_of: str | None,
) -> tuple[str, list, set]:
    """Builds `alias AS (select ... from ... where ... group by ...)` for one
    numerator/denominator signal, fully tenant-scoped and time-scoped on its own.
    Returns (cte_sql, params, tables_touched).
    """
    signal = signals[signal_name]
    table = signal["table"]
    tables_touched = {table}

    join_clauses: list[str] = []
    join_params: list = []
    where: list[str] = []
    where_params: list = []

    def _add_join(dim: str, join_sql: str) -> None:
        if join_sql in join_clauses:
            return
        join_clauses.append(join_sql)
        join_params.append(resolved.terminal)
        tables_touched.add(_DIMENSIONS[dim]["join"]["table"])

    dim_selects = []
    for dim in dims:
        select_expr, join_sql = _dim_ref(dim, table)
        dim_selects.append(f"{select_expr} as {dim}")
        if join_sql is not None:
            _add_join(dim, join_sql)

    agg_expr = AGG_SQL[signal["agg"]].format(expr=signal["expr"])
    select_cols = dim_selects + [f"{agg_expr} as value"]

    where.append(f"{table}.terminal = ?")
    where_params.append(resolved.terminal)

    soft_delete_col = resolved.soft_delete_columns.get(table) or signal.get("soft_delete_flag")
    if soft_delete_col:
        where.append(f"{table}.{soft_delete_col} = false")

    time_col = signal.get("time_column")
    if time_col:
        where.append(f"{table}.{time_col} >= ?")
        where.append(f"{table}.{time_col} < ?")
        where_params.extend([start, end])

    knowledge_col = signal.get("knowledge_time_column")
    if as_of is not None and knowledge_col:
        where.append(f"{table}.{knowledge_col} <= ?")
        where_params.append(as_of)

    if signal.get("filter"):
        where.append(signal["filter"])

    for key, value in intent.filters.items():
        if key == "terminal":
            continue  # redundant with the tenant predicate already in `where`
        if key not in _DIMENSIONS:
            continue
        select_expr, join_sql = _dim_ref(key, table)
        if join_sql is not None:
            _add_join(key, join_sql)
        where.append(f"({select_expr}) = ?")
        where_params.append(value)

    sql = f"select {', '.join(select_cols)} from {table}"
    for j in join_clauses:
        sql += f" {j}"
    sql += f" where {' and '.join(where)}"
    if dims:
        sql += f" group by {', '.join(str(i + 1) for i in range(len(dims)))}"

    return f"{alias} as (\n  {sql}\n)", join_params + where_params, tables_touched


def compile(
    intent: QueryIntent,
    *,
    tenant_id: str,
    registry: ResolvedRegistry | None = None,
    db_path=DB_PATH,
) -> CompiledQuery:
    if registry is not None and registry.client_id != tenant_id:
        raise TenantScopeError(
            f"registry resolved for client '{registry.client_id}' does not match tenant_id '{tenant_id}'"
        )
    resolved = registry or resolve(tenant_id, db_path=db_path)

    metric = resolved.metrics[intent.metric]
    signals = resolved.signals

    if "terminal" in intent.filters and intent.filters["terminal"] != resolved.terminal:
        raise TenantScopeError(
            f"filter terminal='{intent.filters['terminal']}' is not tenant '{tenant_id}''s terminal"
        )
    if "berth" in intent.filters and not intent.filters["berth"].startswith(resolved.berth_alias_prefix):
        raise TenantScopeError(
            f"filter berth='{intent.filters['berth']}' does not match tenant '{tenant_id}''s berth naming "
            f"('{resolved.berth_alias_prefix}*')"
        )

    allowed_filter_keys = set(metric["allowed_dimensions"]) | {"terminal"}
    unknown_filters = set(intent.filters) - allowed_filter_keys
    if unknown_filters:
        raise CompileError(
            f"metric '{intent.metric}' can't be filtered on {sorted(unknown_filters)}; "
            f"allowed: {sorted(allowed_filter_keys)}"
        )

    dims = intent.dimensions or (metric["contribution_dimensions"] if intent.op == "diagnose" else [])

    start, end = resolve_time_window(intent.time_window)

    num_cte, num_params, num_tables = _signal_subquery(
        metric["numerator"], "num_agg",
        signals=signals, dims=dims, resolved=resolved, intent=intent,
        start=start, end=end, as_of=intent.as_of,
    )

    denom_signal = signals[metric["denominator"]]
    tables_touched = set(num_tables)
    params = list(num_params)

    if denom_signal.get("virtual"):
        window_hours = (end - start).total_seconds() / 3600.0
        select_dims = ", ".join(dims) + ", " if dims else ""
        sql = (
            f"with {num_cte}\n"
            f"select {select_dims}value as numerator, ? as denominator, value / ? as value\n"
            f"from num_agg"
        )
        params.extend([window_hours, window_hours])
    else:
        denom_cte, denom_params, denom_tables = _signal_subquery(
            metric["denominator"], "denom_agg",
            signals=signals, dims=dims, resolved=resolved, intent=intent,
            start=start, end=end, as_of=intent.as_of,
        )
        tables_touched |= denom_tables
        params.extend(denom_params)

        select_dims = ", ".join(f"num_agg.{d}" for d in dims) + ", " if dims else ""
        join_clause = (
            f"join denom_agg using ({', '.join(dims)})" if dims else "cross join denom_agg"
        )
        sql = (
            f"with {num_cte},\n{denom_cte}\n"
            f"select {select_dims}num_agg.value as numerator, denom_agg.value as denominator, "
            f"num_agg.value * 1.0 / denom_agg.value as value\n"
            f"from num_agg {join_clause}"
        )

    if intent.sort:
        order_col = "value" if intent.sort.by == "value" else (dims[0] if dims else "value")
        sql += f"\norder by {order_col} {intent.sort.direction}"
    if intent.limit:
        sql += f"\nlimit {int(intent.limit)}"

    return CompiledQuery(
        sql=sql,
        params=params,
        lineage={"tables": sorted(tables_touched), "tenant": resolved.terminal},
        definition=metric["definition"],
    )
