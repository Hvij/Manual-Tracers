import os
from pathlib import Path
from typing import Any

import clickhouse_connect
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

_client = None


def get_client():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=os.environ["CLICKHOUSE_HOST"],
            port=int(os.environ["CLICKHOUSE_HTTP_PORT"]),
            username=os.environ["CLICKHOUSE_USER"],
            password=os.environ["CLICKHOUSE_PASSWORD"],
            database="inmobi",
            secure=True,
        )
    return _client


def query_rows(sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = get_client().query(sql, parameters=parameters)
    columns = result.column_names
    return [dict(zip(columns, row)) for row in result.result_rows]
