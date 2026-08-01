import time
from datetime import datetime, timezone
from typing import Any

import clickhouse_connect

from app import tracing
from app.settings import get_settings

_client = None


def get_client():
    global _client
    if _client is None:
        settings = get_settings()
        _client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_http_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database="inmobi",
            secure=True,
        )
    return _client


def _as_utc(value: Any) -> Any:
    """clickhouse_connect assumes a naive datetime is in the machine's LOCAL timezone and
    converts it before binding — silently wrong here, since every datetime this app produces
    (get_max_ts's server-side now(), API request bodies) is already a UTC wall-clock value
    with no tzinfo attached. On a UTC-configured machine that assumption happens to be a
    no-op; on any other (e.g. IST, UTC+5:30) it silently shifts every bound query window.
    Labeling it UTC explicitly — not converting it — makes binding a no-op everywhere."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def query_rows(sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if parameters:
        parameters = {k: _as_utc(v) for k, v in parameters.items()}
    start = time.monotonic()
    result = get_client().query(sql, parameters=parameters)
    elapsed_s = time.monotonic() - start

    columns = result.column_names
    rows = [dict(zip(columns, row)) for row in result.result_rows]

    summary = getattr(result, "summary", None) or {}
    read_rows = summary.get("read_rows") if isinstance(summary, dict) else None
    tracing.record_query(sql, parameters, getattr(result, "query_id", None),
                          read_rows if read_rows is not None else len(rows), elapsed_s)

    return rows
