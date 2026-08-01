#!/usr/bin/env python3
"""Render a metric's deviation query from metric_def. Prints SQL, runs nothing.

There is no detection view any more: the same builder that the RCA agent uses
(`RCA/app/metric_sql.py`) is the only place the baseline and z-score are
expressed, and everything else renders it. Two consumers:

  scripts/metric_query.py alert fill_rate      what to paste into a HyperDX chart
  scripts/metric_query.py scan  fill_rate      ranked segment scan, for replay.sh

Both read the metric's formula, threshold and guard rails live from metric_def,
so a registry change needs no edit here. Bounds are now()-relative, because a
HyperDX alert evaluates on wall clock.

Depends on nothing outside the standard library — replay.sh runs it without a venv.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_metric_sql():
    """Load RCA/app/metric_sql.py by path, NOT as `from app import metric_sql`.

    Importing it as part of the `app` package would execute app/__init__.py, which pulls in
    python-dotenv — and replay.sh calls this with the system python, outside RCA's venv.
    metric_sql itself imports nothing, so loading the file directly keeps this script
    dependency-free while still sharing the one copy of the query builder."""
    path = ROOT / "RCA" / "app" / "metric_sql.py"
    spec = importlib.util.spec_from_file_location("metric_sql", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metric_sql = _load_metric_sql()

LOOKBACK_HOURS = 24


def load_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def query(env: dict[str, str], sql: str) -> list[dict]:
    url = f"https://{env['CLICKHOUSE_HOST']}:{env.get('CLICKHOUSE_HTTPS_PORT', '8443')}/?database=inmobi"
    req = urllib.request.Request(url, data=sql.encode())
    auth = f"{env['CLICKHOUSE_USER']}:{env['CLICKHOUSE_PASSWORD']}"
    req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    body = urllib.request.urlopen(req, timeout=120).read().decode()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def get_clock(env: dict[str, str]) -> dict:
    """Not cached: this row is exactly what a compressed replay rewrites, and this script is
    invoked fresh per call, unlike the agent's long-lived process."""
    rows = query(
        env,
        "SELECT bucket_seconds, anchor, origin_dow FROM inmobi.replay_clock "
        "FINAL LIMIT 1 FORMAT JSONEachRow",
    )
    return rows[0] if rows else {"bucket_seconds": 3600, "anchor": 0, "origin_dow": 0}


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("alert", "scan"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, metric_id = sys.argv[1], sys.argv[2]
    env = load_env()

    rows = query(
        env,
        f"SELECT * FROM inmobi.metric_def FINAL "
        f"WHERE metric_id = '{metric_id}' FORMAT JSONEachRow",
    )
    if not rows:
        print(f"no such metric_id: {metric_id}", file=sys.stderr)
        return 1
    meta = rows[0]

    hist = f"now() - INTERVAL {metric_sql.HISTORY_WEEKS} WEEK"
    start = f"now() - INTERVAL {LOOKBACK_HOURS} HOUR"
    clock = get_clock(env)

    if mode == "alert":
        # dim_name = 'ALL' only: alerts fire on the global series, and the agent finds the
        # segment. Shape is a single number under $__timeFilter, which is the contract a
        # ClickStack `number` tile alert evaluates: pair it with thresholdType
        # above_exclusive at 0 — NOT above, which fires unconditionally at zero.
        # The inner bound is the widest window an alert could ask for; $__timeFilter
        # narrows it to the tile's actual range.
        inner = metric_sql.deviation_sql(
            meta, ["ALL"], hist, start, "now()", clock=clock
        )
        print(
            f"SELECT toUInt64(sum(is_anomaly)) AS anomaly_count\nFROM (\n{inner}\n)\n"
            f"WHERE $__timeFilter(ts)"
        )
    else:
        dims = [
            r["dim_id"]
            for r in query(
                env,
                f"SELECT dim_id FROM inmobi.metric_dim_map FINAL "
                f"WHERE metric_id = '{metric_id}' AND dim_id NOT IN "
                f"(SELECT arrayJoin(invalid_dims) FROM inmobi.metric_def FINAL "
                f"WHERE metric_id = '{metric_id}') ORDER BY priority FORMAT JSONEachRow",
            )
        ]
        inner = metric_sql.deviation_sql(
            meta, ["ALL"] + dims, hist, start, "now()", clock=clock
        )
        print(
            "SELECT dim_name, dim_value, count() AS anomalous_hours,\n"
            "       round(max(abs(z_score)), 2) AS peak_abs_z,\n"
            "       round(avg(actual), 4) AS actual, round(avg(expected), 4) AS expected,\n"
            "       round(sum(abs(delta_abs) * sample_count)) AS contribution\n"
            f"FROM (\n{inner}\n)\nWHERE is_anomaly = 1\n"
            "GROUP BY dim_name, dim_value ORDER BY contribution DESC LIMIT 25"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
