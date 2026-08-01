#!/usr/bin/env python3
"""Provision InMobi HyperDX dashboard + webhook alerts via /api/v2."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

HDX = "http://localhost:8000"
KEY = "823d2ef5-d739-46cc-9335-e0c68093a34c"
CONN = "6a6db15979b527244478edf1"
SRC = "6a6db1ee79b527244478eec4"  # Logs → inmobi.ad_events_enriched
DASH_ID = "6a6db3bc79b527244478f148"
WEBHOOK_ID = "6a6db3bc79b527244478f13c"
TEST_DASH = "6a6db3ca79b527244478f15e"


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


def tile(name, x, y, w, h, config, tid=None):
    t = {"name": name, "x": x, "y": y, "w": w, "h": h, "config": config}
    if tid:
        t["id"] = tid
    return t


def sql_tile(name, x, y, w, h, sql, display="line", tid=None):
    return tile(
        name,
        x,
        y,
        w,
        h,
        {
            "displayType": display,
            "configType": "sql",
            "connectionId": CONN,
            "sourceId": SRC,
            "sqlTemplate": sql,
        },
        tid=tid,
    )


def builder_tile(
    name,
    x,
    y,
    w,
    h,
    select,
    display="line",
    group_by=None,
    as_ratio=False,
    where="",
    tid=None,
):
    cfg = {
        "displayType": display,
        "sourceId": SRC,
        "select": select,
        "where": where,
        "whereLanguage": "lucene",
    }
    if group_by:
        cfg["groupBy"] = group_by
    if as_ratio:
        cfg["asRatio"] = True
    return tile(name, x, y, w, h, cfg, tid=tid)


# Stable tile IDs (keep existing ones so alerts stay attached where possible)
T = {
    "revenue": "tile_revenue_daily",
    "requests": "tile_requests_daily",
    "fill_rate": "tile_fill_rate",
    "fill_region": "tile_fill_by_region",
    "ecpm": "tile_ecpm",
    "ecpm_cat": "tile_ecpm_by_category",
    "ctr": "tile_ctr",
    "ctr_os": "tile_ctr_by_os",
    "funnel": "tile_funnel",
    "rev_format": "tile_rev_by_format",
    "fill_os": "tile_fill_by_os",
    "unfilled": "tile_unfilled_count",
}

dashboard = {
    "name": "InMobi Ad Events",
    "tags": ["inmobi", "hackathon"],
    "tiles": [
        builder_tile(
            "Requests (daily)",
            0,
            0,
            6,
            3,
            [{"aggFn": "count", "alias": "requests"}],
            display="number",
            tid=T["requests"],
        ),
        builder_tile(
            "Revenue (sum)",
            6,
            0,
            6,
            3,
            [{"aggFn": "sum", "valueExpression": "revenue", "alias": "revenue"}],
            display="number",
            tid=T["revenue"],
        ),
        sql_tile(
            "Fill rate",
            12,
            0,
            6,
            3,
            """SELECT
  sum(is_filled) / count() AS fill_rate
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}""",
            display="number",
            tid=T["fill_rate"],
        ),
        builder_tile(
            "Unfilled requests",
            18,
            0,
            6,
            3,
            [{"aggFn": "count", "alias": "unfilled"}],
            display="number",
            where="is_filled:0",
            tid=T["unfilled"],
        ),
        sql_tile(
            "Daily revenue",
            0,
            3,
            12,
            5,
            """SELECT
  toStartOfDay(event_time) AS ts,
  sum(revenue) AS revenue
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}
GROUP BY ts
ORDER BY ts""",
            tid=T["revenue"] + "_ts",
        ),
        sql_tile(
            "Daily requests & fills",
            12,
            3,
            12,
            5,
            """SELECT
  toStartOfDay(event_time) AS ts,
  count() AS requests,
  sum(is_filled) AS fills
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}
GROUP BY ts
ORDER BY ts""",
            tid="tile_req_fills_ts",
        ),
        sql_tile(
            "Fill rate by region",
            0,
            8,
            12,
            5,
            """SELECT
  toStartOfDay(event_time) AS ts,
  region,
  sum(is_filled) / count() AS fill_rate
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}
GROUP BY ts, region
ORDER BY ts""",
            tid=T["fill_region"],
        ),
        sql_tile(
            "Fill rate by OS",
            12,
            8,
            12,
            5,
            """SELECT
  toStartOfDay(event_time) AS ts,
  os_version,
  sum(is_filled) / count() AS fill_rate
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}
GROUP BY ts, os_version
ORDER BY ts""",
            tid=T["fill_os"],
        ),
        sql_tile(
            "eCPM by category",
            0,
            13,
            12,
            5,
            """SELECT
  toStartOfDay(event_time) AS ts,
  category,
  sum(revenue) / nullIf(sum(is_impression), 0) * 1000 AS ecpm
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}
GROUP BY ts, category
ORDER BY ts""",
            tid=T["ecpm_cat"],
        ),
        sql_tile(
            "CTR by OS",
            12,
            13,
            12,
            5,
            """SELECT
  toStartOfDay(event_time) AS ts,
  os_version,
  sum(is_click) / nullIf(sum(is_impression), 0) AS ctr
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}
GROUP BY ts, os_version
ORDER BY ts""",
            tid=T["ctr_os"],
        ),
        sql_tile(
            "Revenue by ad format",
            0,
            18,
            12,
            5,
            """SELECT
  toStartOfDay(event_time) AS ts,
  ad_format,
  sum(revenue) AS revenue
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}
GROUP BY ts, ad_format
ORDER BY ts""",
            tid=T["rev_format"],
        ),
        sql_tile(
            "Funnel (daily)",
            12,
            18,
            12,
            5,
            """SELECT
  toStartOfDay(event_time) AS ts,
  count() AS requests,
  sum(is_filled) AS fills,
  sum(is_impression) AS impressions,
  sum(is_click) AS clicks
FROM inmobi.ad_events_enriched
WHERE event_time >= {{start_time}} AND event_time < {{end_time}}
GROUP BY ts
ORDER BY ts""",
            tid=T["funnel"],
        ),
        # Alertable builder line tiles (alerts require line/stacked_bar/number)
        builder_tile(
            "Hourly requests (alert)",
            0,
            23,
            8,
            4,
            [{"aggFn": "count", "alias": "requests"}],
            display="line",
            tid="tile_hourly_requests_alert",
        ),
        builder_tile(
            "Hourly fill rate (alert)",
            8,
            23,
            8,
            4,
            [
                {"aggFn": "sum", "valueExpression": "is_filled", "alias": "fills"},
                {"aggFn": "count", "alias": "requests"},
            ],
            display="line",
            as_ratio=True,
            tid="tile_hourly_fill_alert",
        ),
        builder_tile(
            "Hourly revenue (alert)",
            16,
            23,
            8,
            4,
            [{"aggFn": "sum", "valueExpression": "revenue", "alias": "revenue"}],
            display="line",
            tid="tile_hourly_revenue_alert",
        ),
        builder_tile(
            "Hourly CTR proxy clicks/impr (alert)",
            0,
            27,
            12,
            4,
            [
                {"aggFn": "sum", "valueExpression": "is_click", "alias": "clicks"},
                {"aggFn": "sum", "valueExpression": "is_impression", "alias": "impressions"},
            ],
            display="line",
            as_ratio=True,
            tid="tile_hourly_ctr_alert",
        ),
        builder_tile(
            "Hourly eCPM proxy rev/impr*1000 (alert)",
            12,
            27,
            12,
            4,
            [
                {"aggFn": "sum", "valueExpression": "revenue", "alias": "revenue"},
                {"aggFn": "sum", "valueExpression": "is_impression", "alias": "impressions"},
            ],
            display="line",
            as_ratio=True,
            tid="tile_hourly_ecpm_alert",
        ),
    ],
}

# Validate first
print("Validating dashboard...")
st, val = api("POST", "/api/v2/dashboards/validate", dashboard)
print(json.dumps(val, indent=2)[:2000])
if not val.get("valid", True) and val.get("errors"):
    print("VALIDATION FAILED", file=sys.stderr)
    sys.exit(1)

print("Updating dashboard", DASH_ID)
st, dash = api("PUT", f"/api/v2/dashboards/{DASH_ID}", dashboard)
tiles = {t["name"]: t["id"] for t in dash["data"]["tiles"]}
print(f"Dashboard OK — {len(tiles)} tiles:")
for n, i in tiles.items():
    print(f"  {i}  {n}")

# Delete junk test dashboard
print("Deleting test dashboard", TEST_DASH)
try:
    api("DELETE", f"/api/v2/dashboards/{TEST_DASH}")
    print("  deleted")
except Exception as e:
    print("  skip:", e)

# Update webhook name
print("Updating webhook")
api(
    "PUT",
    f"/api/v2/webhooks/{WEBHOOK_ID}",
    {
        "name": "InMobi Alert Sink (httpbin)",
        "service": "generic",
        "url": "https://httpbin.org/post",
        "description": "Generic webhook for local OSS alerts (no Slack). Swap URL to Slack Incoming Webhook if needed.",
        "body": '{"title":"{{title}}","body":"{{body}}","link":"{{link}}","state":"{{state}}","dashboard":"InMobi Ad Events"}',
    },
)

# Clear existing alerts then recreate
print("Listing/deleting existing alerts")
_, alerts = api("GET", "/api/v2/alerts")
for a in alerts.get("data", []):
    print("  delete", a["id"], a.get("name"))
    api("DELETE", f"/api/v2/alerts/{a['id']}")

# Map alert tile names → ids from updated dashboard
by_name = {t["name"]: t["id"] for t in dash["data"]["tiles"]}

# Thresholds from dataset baselines (daily ~257k req, fill~0.78, eCPM~2.47, CTR~0.011, rev~486)
# Hourly ≈ daily/24 → ~10.7k requests, ~20 revenue
# Note: historical InMobi window ends 2026-07-05; alerts evaluate "now" → may stay OK until live data streams.
alert_specs = [
    {
        "name": "InMobi — hourly requests drop",
        "message": "Hourly ad requests fell below 5,000 (~50% of normal ~10.7k). Check volume incident.",
        "tile": "Hourly requests (alert)",
        "threshold": 5000,
        "thresholdType": "below",
        "interval": "1h",
    },
    {
        "name": "InMobi — hourly requests spike",
        "message": "Hourly ad requests exceeded 20,000 (near 2x normal).",
        "tile": "Hourly requests (alert)",
        "threshold": 20000,
        "thresholdType": "above",
        "interval": "1h",
    },
    {
        "name": "InMobi — fill rate drop",
        "message": "Hourly fill rate below 0.70 (baseline ~0.78). Slice by region/OS.",
        "tile": "Hourly fill rate (alert)",
        "threshold": 0.70,
        "thresholdType": "below",
        "interval": "1h",
    },
    {
        "name": "InMobi — revenue drop",
        "message": "Hourly revenue below 8 (baseline ~20). Walk Revenue ≈ Req × Fill × eCPM/1000.",
        "tile": "Hourly revenue (alert)",
        "threshold": 8,
        "thresholdType": "below",
        "interval": "1h",
    },
    {
        "name": "InMobi — CTR spike",
        "message": "Hourly CTR (clicks/impressions) above 0.025 (baseline ~0.011). Possible news/quality anomaly.",
        "tile": "Hourly CTR proxy clicks/impr (alert)",
        "threshold": 0.025,
        "thresholdType": "above",
        "interval": "1h",
    },
    {
        "name": "InMobi — eCPM drop (rev/impr)",
        "message": "Hourly revenue/impression ratio below 0.0015 (~eCPM 1.5 vs baseline 2.47).",
        "tile": "Hourly eCPM proxy rev/impr*1000 (alert)",
        "threshold": 0.0015,
        "thresholdType": "below",
        "interval": "1h",
    },
]

print("Creating alerts")
for spec in alert_specs:
    tile_id = by_name[spec["tile"]]
    body = {
        "dashboardId": DASH_ID,
        "tileId": tile_id,
        "source": "tile",
        "threshold": spec["threshold"],
        "thresholdType": spec["thresholdType"],
        "interval": spec["interval"],
        "channel": {"type": "webhook", "webhookId": WEBHOOK_ID},
        "name": spec["name"],
        "message": spec["message"],
    }
    st, created = api("POST", "/api/v2/alerts", body)
    a = created["data"]
    print(f"  {a['id']}  {a['name']}  state={a.get('state')}")

print("\nDone.")
print(f"Dashboard: http://localhost:8080/dashboards/{DASH_ID}")
print("Set time range to 2026-06-01 → 2026-07-06 to see charts.")
print("Alerts fire on live 'now' windows via webhook → https://httpbin.org/post")
