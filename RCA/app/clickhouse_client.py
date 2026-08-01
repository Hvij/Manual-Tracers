import time
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


def query_rows(sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
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
