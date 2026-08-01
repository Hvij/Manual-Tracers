from app.clickhouse_client import query_rows


def get_metric(metric_id: str) -> dict | None:
    rows = query_rows(
        "SELECT * FROM inmobi.metric_registry FINAL WHERE metric_id = {metric_id:String}",
        {"metric_id": metric_id},
    )
    return rows[0] if rows else None


def get_dim_priority(metric_id: str) -> list[dict]:
    return query_rows(
        "SELECT dim_name, priority, rationale FROM inmobi.metric_dim_priority FINAL "
        "WHERE metric_id = {metric_id:String} ORDER BY priority",
        {"metric_id": metric_id},
    )
