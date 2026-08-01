from functools import lru_cache

from app.clickhouse_client import query_rows


def get_metric(metric_id: str) -> dict | None:
    """metric_def row: sql (the formula, executed as-is), numerator/denominator, dependencies
    (funnel factors, in order), z_score_threshold and the guard rails. Everything the
    deviation query needs comes from this one row."""
    rows = query_rows(
        "SELECT * FROM inmobi.metric_def FINAL WHERE metric_id = {metric_id:String}",
        {"metric_id": metric_id},
    )
    return rows[0] if rows else None


def get_dim_map(metric_id: str) -> list[dict]:
    return query_rows(
        "SELECT dim_id, priority, rationale, dependencies FROM inmobi.metric_dim_map FINAL "
        "WHERE metric_id = {metric_id:String} ORDER BY priority",
        {"metric_id": metric_id},
    )


def get_dim_deps(metric_id: str, dim_id: str) -> list[str]:
    """The dimensions to check after `dim_id` has been cut, in the order the map gives them."""
    row = next((r for r in get_dim_map(metric_id) if r["dim_id"] == dim_id), None)
    return list(row["dependencies"]) if row and row["dependencies"] else []


@lru_cache
def known_dims() -> frozenset[str]:
    """Every dimension metric_dim_map knows about — the whitelist a dim_id must pass before it
    can be spliced into SQL as a column reference. Read from the map rather than restated in
    Python, so adding a dimension is a row and nothing else. 'ALL' is the global bucket the
    deviation query emits, not a column, so it never qualifies.
    ponytail: cached for process life — the dimension set changes when the schema does, i.e.
    on redeploy. Drop the cache if the map ever becomes hot-editable."""
    rows = query_rows(
        "SELECT DISTINCT dim_id FROM inmobi.metric_dim_map FINAL WHERE dim_id != 'ALL'"
    )
    return frozenset(r["dim_id"] for r in rows)
