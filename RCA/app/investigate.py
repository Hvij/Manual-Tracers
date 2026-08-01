from datetime import timedelta
from statistics import mean

from app.clickhouse_client import query_rows
from app.registry import get_dim_priority, get_metric

REVENUE_IDENTITY_FACTORS = ["requests", "fill_rate", "render_rate", "ecpm"]

# raw ad_events_enriched expression for each metric_registry numerator/denominator name
RAW_EXPR = {
    "fills": "sum(is_filled)",
    "requests": "count()",
    "impressions": "sum(is_impression)",
    "clicks": "sum(is_click)",
    "revenue": "sum(revenue)",
}

# not derived from the alert body (only metric_id is), but validated anyway before
# splicing into SQL since dim_name becomes a raw column reference, not a bound parameter
DIMENSION_COLUMNS = {
    "ad_format", "category", "publisher_tier", "region",
    "country", "device_model", "os_version", "vertical", "campaign_type",
}

HOLDOUT_RATIO_THRESHOLD = 0.25


def get_max_ts():
    rows = query_rows("SELECT max(event_time) AS max_ts FROM inmobi.ad_events_enriched")
    return rows[0]["max_ts"]


def reproduce_global(metric_id: str, start, end) -> list[dict]:
    return query_rows(
        "SELECT ts, actual, expected, z_score, delta_rel, is_anomaly "
        "FROM inmobi.v_metric_deviation "
        "WHERE metric_id = {metric_id:String} AND dim_name = 'ALL' "
        "AND ts > {start:DateTime} AND ts <= {end:DateTime} ORDER BY ts",
        {"metric_id": metric_id, "start": start, "end": end},
    )


def _anomalous(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["is_anomaly"]]


def decompose(start, end) -> dict:
    """Walk the revenue identity before touching any dimension (CLAUDE.md rule 5)."""
    checked = []
    for factor in REVENUE_IDENTITY_FACTORS:
        anomalous = _anomalous(reproduce_global(factor, start, end))
        peak_z = max((abs(r["z_score"]) for r in anomalous), default=0.0)
        checked.append({"metric_id": factor, "anomalous_hours": len(anomalous), "peak_abs_z": peak_z})
    driving = max(checked, key=lambda c: c["peak_abs_z"])
    return {
        "factors_checked": checked,
        "driving_factor": driving["metric_id"] if driving["peak_abs_z"] > 0 else None,
    }


def scan_dims(metric_id: str, start, end) -> list[dict]:
    meta = get_metric(metric_id)
    invalid = set(meta["invalid_dims"] or [])
    eligible = [r["dim_name"] for r in get_dim_priority(metric_id) if r["dim_name"] not in invalid]
    if not eligible:
        return []
    return query_rows(
        "SELECT dim_name, dim_value, count() AS anomalous_hours, "
        "max(abs(z_score)) AS peak_abs_z, avg(actual) AS avg_actual, "
        "avg(expected) AS avg_expected, avg(delta_rel) AS avg_delta_rel, "
        "sum(abs(delta_abs) * sample_count) AS contribution "
        "FROM inmobi.v_metric_deviation "
        "WHERE metric_id = {metric_id:String} AND dim_name IN {dims:Array(String)} "
        "AND ts > {start:DateTime} AND ts <= {end:DateTime} AND is_anomaly = 1 "
        "GROUP BY dim_name, dim_value ORDER BY contribution DESC",
        {"metric_id": metric_id, "dims": eligible, "start": start, "end": end},
    )


def compute_holdout_verdict(candidate_delta: float, residual_delta: float,
                             ratio_threshold: float = HOLDOUT_RATIO_THRESHOLD) -> str:
    """Residual close to zero relative to the candidate's own delta => the candidate is the sole cause."""
    if candidate_delta == 0:
        return "inconclusive"
    ratio = abs(residual_delta) / abs(candidate_delta)
    return "localized" if ratio <= ratio_threshold else "inconclusive"


def holdout_check(metric_id: str, candidate: dict, global_expected_ref: float, start, end) -> dict:
    dim_col = candidate["dim_name"]
    if dim_col not in DIMENSION_COLUMNS:
        raise ValueError(f"refusing to holdout-check unknown dimension column {dim_col!r}")

    meta = get_metric(metric_id)
    num_expr = RAW_EXPR[meta["numerator"]]
    den_expr = RAW_EXPR[meta["denominator"]] if meta["denominator"] else "1"

    rows = query_rows(
        f"SELECT {num_expr} AS num, {den_expr} AS den FROM inmobi.ad_events_enriched "
        f"WHERE event_time > {{start:DateTime}} AND event_time <= {{end:DateTime}} "
        f"AND {dim_col} != {{dim_val:String}}",
        {"start": start, "end": end, "dim_val": candidate["dim_value"]},
    )
    num, den = rows[0]["num"], rows[0]["den"]
    residual_actual = (num / den * meta["scale"]) if meta["is_ratio"] else num * meta["scale"]

    residual_delta = residual_actual - global_expected_ref
    candidate_delta = candidate["avg_actual"] - candidate["avg_expected"]
    verdict = compute_holdout_verdict(candidate_delta, residual_delta)

    return {
        "candidate": {"dim_name": dim_col, "dim_value": candidate["dim_value"]},
        "residual_actual": residual_actual,
        "residual_delta": residual_delta,
        "candidate_delta": candidate_delta,
        "verdict": verdict,
    }


def run_investigation(metric_id: str, lookback_hours: int = 24) -> dict:
    meta = get_metric(metric_id)
    if meta is None:
        return {"metric_id": metric_id, "verdict": "unknown_metric"}

    max_ts = get_max_ts()
    start, end = max_ts - timedelta(hours=lookback_hours), max_ts
    window = {"start": start.isoformat(), "end": end.isoformat()}

    target_metric = metric_id
    decomposition = None
    if meta["level"] == 1:
        decomposition = decompose(start, end)
        if not decomposition["driving_factor"]:
            return {"metric_id": metric_id, "window": window, "decomposition": decomposition,
                     "verdict": "not_reproducible"}
        target_metric = decomposition["driving_factor"]

    anomalous = _anomalous(reproduce_global(target_metric, start, end))
    if not anomalous:
        return {"metric_id": metric_id, "target_metric": target_metric, "window": window,
                 "decomposition": decomposition, "verdict": "not_reproducible"}

    global_summary = {
        "actual": mean(r["actual"] for r in anomalous),
        "expected": mean(r["expected"] for r in anomalous),
        "anomalous_hours": len(anomalous),
        "peak_abs_z": max(abs(r["z_score"]) for r in anomalous),
    }

    candidates = scan_dims(target_metric, start, end)
    if not candidates:
        return {"metric_id": metric_id, "target_metric": target_metric, "window": window,
                 "decomposition": decomposition, "global": global_summary,
                 "verdict": "broad_based", "ruled_out": []}

    holdout = holdout_check(target_metric, candidates[0], global_summary["expected"], start, end)
    ruled_out = (
        [f"{c['dim_name']}={c['dim_value']}" for c in candidates[1:]]
        if holdout["verdict"] == "localized" else []
    )

    return {
        "metric_id": metric_id,
        "target_metric": target_metric,
        "window": window,
        "decomposition": decomposition,
        "global": global_summary,
        "candidates": candidates[:10],
        "holdout": holdout,
        "verdict": holdout["verdict"],
        "ruled_out": ruled_out,
    }
