"""Diagnose engine: reality check, then per-dimension contribution with an
explicit remainder, then correlation against only the registry's declared
signals. Never claims causation; `unexplained_remainder` is a first-class
result, not an error path.

Contribution analysis decomposes the metric's numerator (its additive base
measure), independently per dimension in `contribution_dimensions`. Ratio
metrics are not additive at the ratio level, so the ratio itself is never
decomposed; the denominator delta is carried alongside for context.
Cross-dimensional interaction, Shapley attribution, and mix-versus-rate
splits are out of scope by decision, not by omission.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

from core.compile import AGG_SQL, compile as compile_intent
from core.intent import QueryIntent
from core.timewindow import resolve_time_window
from registry.load import DB_PATH, ResolvedRegistry, resolve


@dataclass
class RealityCheck:
    is_artefact: bool
    reason: str
    raw_value: float | None
    deduped_value: float | None


@dataclass
class Contribution:
    level: str
    numerator_delta: float
    denominator_delta: float | None
    share: float | None


@dataclass
class DimensionResult:
    dimension: str
    contributions: list[Contribution]
    total_numerator_delta: float
    unexplained_remainder: float


@dataclass
class SignalCorrelation:
    signal: str
    base_value: float
    comparison_value: float
    delta: float


@dataclass
class DiagnoseResult:
    metric: str
    base_value: float
    comparison_value: float
    total_delta: float
    reality: RealityCheck
    dimensions: list[DimensionResult] = field(default_factory=list)
    signals: list[SignalCorrelation] = field(default_factory=list)


def check_correction_replay(
    con: duckdb.DuckDBPyConnection,
    *,
    table: str,
    value_expr: str,
    agg: str,
    time_col: str,
    start,
    end,
    key_col: str | None = None,
    knowledge_col: str | None = None,
    tenant_value: str | None = None,
    soft_delete_col: str | None = None,
    artefact_ratio: float = 0.4,
) -> RealityCheck:
    """Compares the raw reported total against the same total deduplicated to
    one row per `key_col` (the latest `knowledge_col` wins). A figure that
    shrinks by more than `artefact_ratio` under dedup was inflated by
    duplicate or superseded rows, not by a genuine operational change.
    """
    where = [f"{time_col} >= ?", f"{time_col} < ?"]
    params = [start, end]
    if tenant_value is not None:
        where.append("terminal = ?")
        params.append(tenant_value)
    where_sql = " and ".join(where)

    agg_sql = AGG_SQL[agg].format(expr=value_expr)
    raw_value = con.execute(f"select {agg_sql} from {table} where {where_sql}", params).fetchone()[0]
    raw_value = raw_value or 0.0

    if key_col is None or knowledge_col is None:
        return RealityCheck(
            is_artefact=False,
            reason="no natural key declared for this signal; correction replay skipped",
            raw_value=raw_value,
            deduped_value=None,
        )

    dedup_where = where_sql
    if soft_delete_col:
        dedup_where += f" and {soft_delete_col} = false"
    deduped_value = con.execute(
        f"""
        select {agg_sql} from (
            select *, row_number() over (partition by {key_col} order by {knowledge_col} desc) as rn
            from {table}
            where {dedup_where}
        ) t where rn = 1
        """,
        params,
    ).fetchone()[0]
    deduped_value = deduped_value or 0.0

    if raw_value == 0:
        return RealityCheck(
            is_artefact=False,
            reason="no signal in the window to assess",
            raw_value=raw_value,
            deduped_value=deduped_value,
        )

    shrink = abs(deduped_value - raw_value) / abs(raw_value)
    is_artefact = shrink >= artefact_ratio
    reason = (
        f"deduplicating by {key_col} (keeping only the latest {knowledge_col} row) changes "
        f"the total by {shrink:.0%}, so the reported figure is inflated by duplicate or "
        f"superseded rows rather than a genuine operational change"
        if is_artefact
        else
        f"deduplicating by {key_col} changes the total by only {shrink:.0%}; the figure "
        f"survives correction replay"
    )
    return RealityCheck(is_artefact=is_artefact, reason=reason, raw_value=raw_value, deduped_value=deduped_value)


def _reality_check_for_signal(con, signal: dict, resolved: ResolvedRegistry, start, end) -> RealityCheck:
    if signal.get("virtual"):
        return RealityCheck(is_artefact=False, reason="virtual signal has no rows to replay", raw_value=None, deduped_value=None)
    table = signal["table"]
    soft_delete_col = resolved.soft_delete_columns.get(table) or signal.get("soft_delete_flag")
    return check_correction_replay(
        con,
        table=table,
        value_expr=signal["expr"],
        agg=signal["agg"],
        time_col=signal["time_column"],
        start=start,
        end=end,
        key_col=signal.get("key_column"),
        knowledge_col=signal.get("knowledge_time_column"),
        tenant_value=resolved.terminal,
        soft_delete_col=soft_delete_col,
    )


def _signal_total(con, signal: dict, resolved: ResolvedRegistry, start, end, as_of: str | None) -> float:
    if signal.get("virtual"):
        return (end - start).total_seconds() / 3600.0
    table = signal["table"]
    where = [f"{table}.terminal = ?", f"{table}.{signal['time_column']} >= ?", f"{table}.{signal['time_column']} < ?"]
    params = [resolved.terminal, start, end]
    soft_delete_col = resolved.soft_delete_columns.get(table) or signal.get("soft_delete_flag")
    if soft_delete_col:
        where.append(f"{table}.{soft_delete_col} = false")
    knowledge_col = signal.get("knowledge_time_column")
    if as_of is not None and knowledge_col:
        where.append(f"{table}.{knowledge_col} <= ?")
        params.append(as_of)
    if signal.get("filter"):
        where.append(signal["filter"])
    agg_sql = AGG_SQL[signal["agg"]].format(expr=signal["expr"])
    value = con.execute(f"select {agg_sql} from {table} where {' and '.join(where)}", params).fetchone()[0]
    return value or 0.0


def _window(spec_start, spec_end) -> dict:
    return {"start": spec_start.isoformat(), "end": spec_end.isoformat()}


def diagnose(
    intent: QueryIntent,
    *,
    tenant_id: str,
    registry: ResolvedRegistry | None = None,
    db_path=DB_PATH,
) -> DiagnoseResult:
    if intent.op != "diagnose":
        raise ValueError("diagnose() requires an intent with op='diagnose'")

    resolved = registry or resolve(tenant_id, db_path=db_path)
    metric_name = intent.metric
    metric = resolved.metrics[metric_name]
    signals = resolved.signals

    comparison_start, comparison_end = resolve_time_window(intent.time_window)
    window_length = comparison_end - comparison_start
    base_start, base_end = comparison_start - window_length, comparison_start

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        base_total = compile_intent(
            QueryIntent(op="aggregate", metric=metric_name, time_window=_window(base_start, base_end), as_of=intent.as_of),
            tenant_id=tenant_id, registry=resolved,
        )
        comp_total = compile_intent(
            QueryIntent(op="aggregate", metric=metric_name, time_window=_window(comparison_start, comparison_end), as_of=intent.as_of),
            tenant_id=tenant_id, registry=resolved,
        )
        base_num, base_den, base_value = con.execute(base_total.sql, base_total.params).fetchone()
        comp_num, comp_den, comp_value = con.execute(comp_total.sql, comp_total.params).fetchone()
        total_numerator_delta = (comp_num or 0.0) - (base_num or 0.0)

        reality = _reality_check_for_signal(con, signals[metric["numerator"]], resolved, comparison_start, comparison_end)

        dimension_results = []
        for dim in metric.get("contribution_dimensions", []):
            base_cq = compile_intent(
                QueryIntent(op="diagnose", metric=metric_name, dimensions=[dim], time_window=_window(base_start, base_end), as_of=intent.as_of),
                tenant_id=tenant_id, registry=resolved,
            )
            comp_cq = compile_intent(
                QueryIntent(op="diagnose", metric=metric_name, dimensions=[dim], time_window=_window(comparison_start, comparison_end), as_of=intent.as_of),
                tenant_id=tenant_id, registry=resolved,
            )
            base_map = {row[0]: (row[1] or 0.0, row[2] or 0.0) for row in con.execute(base_cq.sql, base_cq.params).fetchall()}
            comp_map = {row[0]: (row[1] or 0.0, row[2] or 0.0) for row in con.execute(comp_cq.sql, comp_cq.params).fetchall()}

            contributions = []
            for level in sorted(set(base_map) | set(comp_map), key=str):
                b_num, b_den = base_map.get(level, (0.0, 0.0))
                c_num, c_den = comp_map.get(level, (0.0, 0.0))
                num_delta = c_num - b_num
                contributions.append(Contribution(
                    level=str(level),
                    numerator_delta=num_delta,
                    denominator_delta=c_den - b_den,
                    share=(num_delta / total_numerator_delta) if total_numerator_delta else None,
                ))
            contributions.sort(key=lambda c: abs(c.numerator_delta), reverse=True)

            explained = sum(c.numerator_delta for c in contributions)
            dimension_results.append(DimensionResult(
                dimension=dim,
                contributions=contributions,
                total_numerator_delta=total_numerator_delta,
                unexplained_remainder=total_numerator_delta - explained,
            ))

        signal_correlations = [
            SignalCorrelation(
                signal=signal_name,
                base_value=(base_v := _signal_total(con, signals[signal_name], resolved, base_start, base_end, intent.as_of)),
                comparison_value=(comp_v := _signal_total(con, signals[signal_name], resolved, comparison_start, comparison_end, intent.as_of)),
                delta=comp_v - base_v,
            )
            for signal_name in resolved.declared_signals
        ]

        return DiagnoseResult(
            metric=metric_name,
            base_value=base_value,
            comparison_value=comp_value,
            total_delta=total_numerator_delta,
            reality=reality,
            dimensions=dimension_results,
            signals=signal_correlations,
        )
    finally:
        con.close()
