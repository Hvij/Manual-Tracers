import json
import logging
import math
import re

from fastapi import BackgroundTasks, FastAPI, HTTPException, status

from app import tracing
from app.investigate import reproduce_global, reproduce_segment, run_investigation
from app.narrate import narrate
from app.registry import get_metric
from app.report import ledger_to_report
from app.report_store import persist_report
from app.schemas import ClickStackAlertPayload, GlobalSeriesRequest, SegmentSeriesRequest
from app.utils import TTLCache, sha256_hex

METRIC_ID_RE = re.compile(r"metric_id=(\w+)")
# Optional, and ClickStack cannot currently produce it — its webhook template exposes only
# {{title}}/{{body}}/{{link}}, with no access to the firing row's group-by value. Parsed
# anyway so an alerting source that CAN name a segment is a config change, not a code change.
# It is only ever a hint: investigate.py re-derives every number regardless.
DIMENSION_ID_RE = re.compile(r"dimension_id=(\w+)")
DEDUP_WINDOW_S = 300

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rca_agent")

app = FastAPI(title="RCA Agent Webhook")

_dedup = TTLCache(ttl_seconds=DEDUP_WINDOW_S)


@app.get("/health")
def health():
    return {"status": "ok"}


def _extract(pattern: re.Pattern, payload: ClickStackAlertPayload) -> str | None:
    match = pattern.search(payload.body) or pattern.search(payload.title)
    return match.group(1) if match else None


def _investigate(metric_id: str, dimension_id: str | None, payload: ClickStackAlertPayload) -> None:
    ledger = run_investigation(metric_id, dimension_id)
    result = narrate(ledger)
    report = ledger_to_report(ledger, payload, result)
    persist_report(report)
    logger.info("investigation for %s -> %s", metric_id, json.dumps(report, default=str))
    tracing.flush()


@app.post("/webhooks/alerts", status_code=status.HTTP_202_ACCEPTED)
async def receive_alert(payload: ClickStackAlertPayload, background_tasks: BackgroundTasks):
    key = sha256_hex(payload.title, payload.body)
    if _dedup.seen(key):
        logger.info("duplicate alert ignored: %s", payload.title)
        return {"status": "duplicate", "delivery_key": key}

    logger.info("alert received: title=%r body=%r link=%r", payload.title, payload.body, payload.link)

    metric_id = _extract(METRIC_ID_RE, payload)
    if metric_id is None:
        logger.info("no metric_id found in alert body/title, skipping investigation")
        return {"status": "accepted", "delivery_key": key, "investigation": "skipped"}

    if get_metric(metric_id) is None:
        logger.warning("metric_id=%r not in metric_def, skipping investigation", metric_id)
        return {"status": "accepted", "delivery_key": key, "investigation": "unknown_metric"}

    dimension_id = _extract(DIMENSION_ID_RE, payload)
    background_tasks.add_task(_investigate, metric_id, dimension_id, payload)
    return {"status": "accepted", "delivery_key": key, "investigation": "started",
            "metric_id": metric_id, "dimension_id": dimension_id}


def _json_safe(rows: list[dict]) -> list[dict]:
    """A bucket without enough baseline history yet (MIN_BASE_POINTS) has expected/z_score
    = NaN — fine internally, but Starlette's JSONResponse rejects NaN as invalid JSON and
    500s the whole request. NaN/Infinity mean the same thing JSON's null already means here:
    no value to report."""
    return [
        {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in row.items()}
        for row in rows
    ]


# docs/RCA_UI_TEMPLATE.md Step 3 — rca-api proxies its chart endpoints to these instead of
# querying ClickHouse itself, so credentials stay in exactly one process (this one).
@app.post("/internal/global-series")
def global_series(req: GlobalSeriesRequest):
    if get_metric(req.metric_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown metric_id: {req.metric_id}")
    return _json_safe(reproduce_global(req.metric_id, req.start, req.end))


@app.post("/internal/segment-series")
def segment_series(req: SegmentSeriesRequest):
    if get_metric(req.metric_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown metric_id: {req.metric_id}")
    try:
        rows = reproduce_segment(req.metric_id, req.dim_name, req.start, req.end)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    if req.dim_values:
        rows = [r for r in rows if r["dim_value"] in req.dim_values]
    return _json_safe(rows)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
