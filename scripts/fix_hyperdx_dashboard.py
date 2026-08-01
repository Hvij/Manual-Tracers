#!/usr/bin/env python3
"""Fix InMobi HyperDX dashboard: correct SQL time macros, one dashboard, clean source."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HDX = os.environ.get("HDX_URL", "http://localhost:8000")
KEY = os.environ.get("HDX_KEY", "823d2ef5-d739-46cc-9335-e0c68093a34c")
CONN = "6a6db15979b527244478edf1"
SRC = "6a6db1ee79b527244478eec4"  # Logs → inmobi.ad_events_enriched
DASH_ID = "6a6db3bc79b527244478f148"
DUP_SOURCE = "6a6db3bc79b527244478f155"  # duplicate "InMobi Ad Events" source
WEBHOOK_ID = "6a6db3bc79b527244478f13c"


def api(method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{HDX}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"HTTP {e.code} {method} {path}: {err}", file=sys.stderr)
        raise


def sql_tile(name, x, y, w, h, sql, display="line"):
    return {
        "name": name,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "config": {
            "displayType": display,
            "configType": "sql",
            "connectionId": CONN,
            "sourceId": SRC,
            "sqlTemplate": sql,
        },
    }


def builder_tile(name, x, y, w, h, select, display="line", as_ratio=False, where=""):
    # Put filter on each select item — HyperDX external API drops top-level where
    selects = []
    for s in select:
        item = dict(s)
        if where:
            item["where"] = where
            item["whereLanguage"] = "lucene"
        else:
            item.setdefault("where", "")
            item.setdefault("whereLanguage", "lucene")
        selects.append(item)
    cfg = {
        "displayType": display,
        "sourceId": SRC,
        "select": selects,
    }
    if as_ratio:
        cfg["asRatio"] = True
    return {"name": name, "x": x, "y": y, "w": w, "h": h, "config": cfg}


# DateTime64(3) → use _ms macros. $__timeInterval respects dashboard granularity.
TF = "$__timeFilter_ms(event_time)"
TI = "$__timeInterval(event_time)"

dashboard = {
    "name": "InMobi Ad Events",
    "tags": ["inmobi", "hackathon"],
    "tiles": [
        builder_tile(
            "Requests",
            0, 0, 6, 3,
            [{"aggFn": "count", "alias": "requests"}],
            display="number",
        ),
        builder_tile(
            "Revenue",
            6, 0, 6, 3,
            [{"aggFn": "sum", "valueExpression": "revenue", "alias": "revenue"}],
            display="number",
        ),
        sql_tile(
            "Fill rate",
            12, 0, 6, 3,
            f"SELECT sum(is_filled) / count() AS fill_rate\nFROM inmobi.ad_events_enriched\nWHERE {TF}",
            display="number",
        ),
        builder_tile(
            "Unfilled requests",
            18, 0, 6, 3,
            [{"aggFn": "count", "alias": "unfilled"}],
            display="number",
            where="is_filled:0",
        ),
        sql_tile(
            "Revenue over time",
            0, 3, 12, 5,
            f"""SELECT
  {TI} AS ts,
  sum(revenue) AS revenue
FROM inmobi.ad_events_enriched
WHERE {TF}
GROUP BY ts
ORDER BY ts""",
        ),
        sql_tile(
            "Requests & fills over time",
            12, 3, 12, 5,
            f"""SELECT
  {TI} AS ts,
  count() AS requests,
  sum(is_filled) AS fills
FROM inmobi.ad_events_enriched
WHERE {TF}
GROUP BY ts
ORDER BY ts""",
        ),
        sql_tile(
            "Fill rate by region",
            0, 8, 12, 5,
            f"""SELECT
  {TI} AS ts,
  region,
  sum(is_filled) / count() AS fill_rate
FROM inmobi.ad_events_enriched
WHERE {TF}
GROUP BY ts, region
ORDER BY ts""",
        ),
        sql_tile(
            "Fill rate by OS",
            12, 8, 12, 5,
            f"""SELECT
  {TI} AS ts,
  os_version,
  sum(is_filled) / count() AS fill_rate
FROM inmobi.ad_events_enriched
WHERE {TF}
GROUP BY ts, os_version
ORDER BY ts""",
        ),
        sql_tile(
            "eCPM by category",
            0, 13, 12, 5,
            f"""SELECT
  {TI} AS ts,
  category,
  sum(revenue) / nullIf(sum(is_impression), 0) * 1000 AS ecpm
FROM inmobi.ad_events_enriched
WHERE {TF}
GROUP BY ts, category
ORDER BY ts""",
        ),
        sql_tile(
            "CTR by OS",
            12, 13, 12, 5,
            f"""SELECT
  {TI} AS ts,
  os_version,
  sum(is_click) / nullIf(sum(is_impression), 0) AS ctr
FROM inmobi.ad_events_enriched
WHERE {TF}
GROUP BY ts, os_version
ORDER BY ts""",
        ),
        sql_tile(
            "Revenue by ad format",
            0, 18, 12, 5,
            f"""SELECT
  {TI} AS ts,
  ad_format,
  sum(revenue) AS revenue
FROM inmobi.ad_events_enriched
WHERE {TF}
GROUP BY ts, ad_format
ORDER BY ts""",
        ),
        sql_tile(
            "Funnel over time",
            12, 18, 12, 5,
            f"""SELECT
  {TI} AS ts,
  count() AS requests,
  sum(is_filled) AS fills,
  sum(is_impression) AS impressions,
  sum(is_click) AS clicks
FROM inmobi.ad_events_enriched
WHERE {TF}
GROUP BY ts
ORDER BY ts""",
        ),
        # Alertable builder line tiles
        builder_tile(
            "Hourly requests (alert)",
            0, 23, 8, 4,
            [{"aggFn": "count", "alias": "requests"}],
        ),
        builder_tile(
            "Hourly fill rate (alert)",
            8, 23, 8, 4,
            [
                {"aggFn": "sum", "valueExpression": "is_filled", "alias": "fills"},
                {"aggFn": "count", "alias": "requests"},
            ],
            as_ratio=True,
        ),
        builder_tile(
            "Hourly revenue (alert)",
            16, 23, 8, 4,
            [{"aggFn": "sum", "valueExpression": "revenue", "alias": "revenue"}],
        ),
        builder_tile(
            "Hourly CTR (alert)",
            0, 27, 12, 4,
            [
                {"aggFn": "sum", "valueExpression": "is_click", "alias": "clicks"},
                {"aggFn": "sum", "valueExpression": "is_impression", "alias": "impressions"},
            ],
            as_ratio=True,
        ),
        builder_tile(
            "Hourly eCPM proxy (alert)",
            12, 27, 12, 4,
            [
                {"aggFn": "sum", "valueExpression": "revenue", "alias": "revenue"},
                {"aggFn": "sum", "valueExpression": "is_impression", "alias": "impressions"},
            ],
            as_ratio=True,
        ),
    ],
}


def main() -> None:
    # Delete any extra dashboards; keep InMobi Ad Events
    _, listed = api("GET", "/api/v2/dashboards")
    for d in listed.get("data", []):
        if d["id"] != DASH_ID:
            print(f"Deleting extra dashboard {d['id']} ({d.get('name')})")
            api("DELETE", f"/api/v2/dashboards/{d['id']}")
        else:
            print(f"Keeping dashboard {d['id']} ({d.get('name')})")

    # Delete duplicate source (same table as Logs)
    _, sources = api("GET", "/api/v2/sources")
    for s in sources.get("data", []):
        if s["id"] == DUP_SOURCE or (
            s.get("name") == "InMobi Ad Events" and s["id"] != SRC
        ):
            print(f"Deleting duplicate source {s['id']} ({s.get('name')})")
            try:
                api("DELETE", f"/api/v2/sources/{s['id']}")
            except Exception as e:
                print("  skip source delete:", e)

    print("Validating dashboard...")
    _, val = api("POST", "/api/v2/dashboards/validate", dashboard)
    if val.get("valid") is False:
        print(json.dumps(val, indent=2), file=sys.stderr)
        raise SystemExit("VALIDATION FAILED")
    print("  valid")

    print("Updating dashboard with fixed SQL macros...")
    _, dash = api("PUT", f"/api/v2/dashboards/{DASH_ID}", dashboard)
    tiles = dash["data"]["tiles"]
    print(f"  {len(tiles)} tiles")
    for t in tiles:
        cfg = t["config"]
        sql = (cfg.get("sqlTemplate") or "")[:60].replace("\n", " ")
        wh = ""
        if cfg.get("select"):
            wh = cfg["select"][0].get("where", "")
        print(f"  - {t['name']}: sql={bool(cfg.get('configType'))} where={wh!r} {sql}")

    # Recreate alerts on new tile ids
    print("Refreshing alerts...")
    _, alerts = api("GET", "/api/v2/alerts")
    for a in alerts.get("data", []):
        api("DELETE", f"/api/v2/alerts/{a['id']}")

    by_name = {t["name"]: t["id"] for t in tiles}
    alert_specs = [
        ("Hourly requests (alert)", "InMobi — hourly requests drop", 5000, "below",
         "Hourly requests below 5,000."),
        ("Hourly requests (alert)", "InMobi — hourly requests spike", 20000, "above",
         "Hourly requests above 20,000."),
        ("Hourly fill rate (alert)", "InMobi — fill rate drop", 0.70, "below",
         "Hourly fill rate below 0.70."),
        ("Hourly revenue (alert)", "InMobi — revenue drop", 8, "below",
         "Hourly revenue below 8."),
        ("Hourly CTR (alert)", "InMobi — CTR spike", 0.025, "above",
         "Hourly CTR above 0.025."),
        ("Hourly eCPM proxy (alert)", "InMobi — eCPM drop", 0.0015, "below",
         "Hourly rev/impr below 0.0015."),
    ]
    for tile_name, name, thr, thr_type, msg in alert_specs:
        body = {
            "dashboardId": DASH_ID,
            "tileId": by_name[tile_name],
            "source": "tile",
            "threshold": thr,
            "thresholdType": thr_type,
            "interval": "1h",
            "channel": {"type": "webhook", "webhookId": WEBHOOK_ID},
            "name": name,
            "message": msg,
        }
        _, created = api("POST", "/api/v2/alerts", body)
        print(f"  alert {created['data']['id']} {name}")

    print(f"\nOpen: http://localhost:8080/dashboards/{DASH_ID}")
    print("Time range: 2026-06-01 → 2026-07-06")
    print("SQL tiles now use $__timeFilter_ms / $__timeInterval (not {{start_time}}).")


if __name__ == "__main__":
    main()
