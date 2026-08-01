import math
from datetime import timedelta
from statistics import mean

from app.clickhouse_client import query_rows
from app.registry import get_dim_priority, get_metric
from app.tracing import traced

REVENUE_IDENTITY_FACTORS = ["requests", "fill_rate", "render_rate", "ecpm"]

# guard against dividing by a near-zero total log-move when identity factors offset each
# other (e.g. fill_rate down, ecpm up, net revenue roughly flat) — see
# docs/RCA_DECOMPOSITION_MATH.md §2.4 "degenerate-G guard rail"
EPSILON_G = 0.005

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


@traced("reproduce_global")
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


def _mean_actual_expected(rows: list[dict]) -> tuple[float, float] | None:
    if not rows:
        return None
    return mean(r["actual"] for r in rows), mean(r["expected"] for r in rows)


def _global_summary(rows: list[dict]) -> dict:
    actual, expected = _mean_actual_expected(rows)
    return {"actual": actual, "expected": expected, "hours": len(rows),
             "peak_abs_z": max(abs(r["z_score"]) for r in rows)}


def _log_growth(actual: float, expected: float) -> float:
    if actual <= 0 or expected <= 0:
        return 0.0
    return math.log(actual / expected)


def compute_factor_contributions(growth: dict[str, float], total_delta_rel: float,
                                   min_effect_rel: dict[str, float]) -> dict:
    """Pure log-share allocation, split out from decompose() so the share/offsetting math is
    unit-testable without a ClickHouse round-trip — same pattern as compute_holdout_verdict.
    ln(Revenue_actual/Revenue_expected) = sum(ln(factor_actual/factor_expected)) is exact
    (log of a product is the sum of logs), so each factor's share of that total log-move is
    used to allocate the *observed* revenue delta_rel — contributions sum to the total by
    construction. See docs/RCA_DECOMPOSITION_MATH.md §2.4."""
    total_growth = sum(growth.values())
    offsetting = abs(total_growth) < EPSILON_G

    factors = []
    for metric_id, g in growth.items():
        contribution_rel = g if offsetting else (g / total_growth) * total_delta_rel
        verdict = "implicated" if abs(contribution_rel) >= min_effect_rel[metric_id] else "cleared"
        factors.append({
            "metric_id": metric_id, "log_growth": g,
            "contribution_rel": contribution_rel, "verdict": verdict,
        })
    return {"total_revenue_delta_rel": total_delta_rel, "offsetting": offsetting, "factors": factors}


@traced("decompose")
def decompose(anomalous_rows: list[dict], start, end) -> dict:
    """Walk the revenue identity before touching any dimension (CLAUDE.md rule 5). Every
    factor gets a verdict — not just the loudest one — per CLAUDE.md rule 6."""
    total_actual, total_expected = _mean_actual_expected(anomalous_rows)
    total_delta_rel = (total_actual - total_expected) / total_expected
    anomalous_ts = {r["ts"] for r in anomalous_rows}

    factor_rows = {}
    growth = {}
    for factor in REVENUE_IDENTITY_FACTORS:
        rows = [r for r in reproduce_global(factor, start, end) if r["ts"] in anomalous_ts]
        factor_rows[factor] = rows
        means = _mean_actual_expected(rows)
        growth[factor] = _log_growth(*means) if means else 0.0

    min_effect_rel = {f: get_metric(f)["min_effect_rel"] for f in REVENUE_IDENTITY_FACTORS}
    result = compute_factor_contributions(growth, total_delta_rel, min_effect_rel)

    for entry in result["factors"]:
        rows = factor_rows[entry["metric_id"]]
        entry["global"] = _global_summary(rows) if rows else None

    return result


@traced("scan_dims")
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


@traced("holdout_check")
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


@traced("investigate_factor")
def _investigate_factor(target_metric: str, global_summary: dict, start, end) -> dict:
    candidates = scan_dims(target_metric, start, end)
    if not candidates:
        return {"factor": target_metric, "global": global_summary, "verdict": "broad_based",
                 "candidates": [], "ruled_out": []}

    holdout = holdout_check(target_metric, candidates[0], global_summary["expected"], start, end)
    ruled_out = (
        [f"{c['dim_name']}={c['dim_value']}" for c in candidates[1:]]
        if holdout["verdict"] == "localized" else []
    )
    return {
        "factor": target_metric,
        "global": global_summary,
        "candidates": candidates[:10],
        "holdout": holdout,
        "verdict": holdout["verdict"],
        "ruled_out": ruled_out,
    }


@traced("run_investigation")
def run_investigation(metric_id: str, lookback_hours: int = 24) -> dict:
    meta = get_metric(metric_id)
    if meta is None:
        return {"metric_id": metric_id, "verdict": "unknown_metric"}

    max_ts = get_max_ts()
    start, end = max_ts - timedelta(hours=lookback_hours), max_ts
    window = {"start": start.isoformat(), "end": end.isoformat()}

    anomalous = _anomalous(reproduce_global(metric_id, start, end))
    if not anomalous:
        return {"metric_id": metric_id, "window": window, "verdict": "not_reproducible"}

    decomposition = None
    if meta["level"] == 1:
        decomposition = decompose(anomalous, start, end)
        implicated = [f for f in decomposition["factors"] if f["verdict"] == "implicated"]
        if not implicated:
            return {"metric_id": metric_id, "window": window, "decomposition": decomposition,
                     "verdict": "not_reproducible"}
        findings = [_investigate_factor(f["metric_id"], f["global"], start, end) for f in implicated]
    else:
        findings = [_investigate_factor(metric_id, _global_summary(anomalous), start, end)]

    verdicts = {f["verdict"] for f in findings}
    overall_verdict = (
        "localized" if "localized" in verdicts else
        "inconclusive" if "inconclusive" in verdicts else
        "broad_based"
    )

    return {
        "metric_id": metric_id,
        "window": window,
        "decomposition": decomposition,
        "findings": findings,
        "verdict": overall_verdict,
    }
