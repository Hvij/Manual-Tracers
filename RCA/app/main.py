import json
import logging
import re

from fastapi import BackgroundTasks, FastAPI, status

from app import tracing
from app.investigate import run_investigation
from app.narrate import narrate
from app.registry import get_metric
from app.schemas import ClickStackAlertPayload
from app.utils import TTLCache, sha256_hex

METRIC_ID_RE = re.compile(r"metric_id=(\w+)")
DEDUP_WINDOW_S = 300

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rca_agent")

app = FastAPI(title="RCA Agent Webhook")

_dedup = TTLCache(ttl_seconds=DEDUP_WINDOW_S)


@app.get("/health")
def health():
    return {"status": "ok"}


def _extract_metric_id(payload: ClickStackAlertPayload) -> str | None:
    match = METRIC_ID_RE.search(payload.body) or METRIC_ID_RE.search(payload.title)
    return match.group(1) if match else None


def _investigate(metric_id: str) -> None:
    ledger = run_investigation(metric_id)
    result = narrate(ledger)
    logger.info(
        "investigation for %s -> %s",
        metric_id,
        json.dumps({"ledger": ledger, **result}, default=str),
    )
    tracing.flush()


@app.post("/webhooks/alerts", status_code=status.HTTP_202_ACCEPTED)
async def receive_alert(payload: ClickStackAlertPayload, background_tasks: BackgroundTasks):
    key = sha256_hex(payload.title, payload.body)
    if _dedup.seen(key):
        logger.info("duplicate alert ignored: %s", payload.title)
        return {"status": "duplicate", "delivery_key": key}

    logger.info("alert received: title=%r body=%r link=%r", payload.title, payload.body, payload.link)

    metric_id = _extract_metric_id(payload)
    if metric_id is None:
        logger.info("no metric_id found in alert body/title, skipping investigation")
        return {"status": "accepted", "delivery_key": key, "investigation": "skipped"}

    if get_metric(metric_id) is None:
        logger.warning("metric_id=%r not in metric_registry, skipping investigation", metric_id)
        return {"status": "accepted", "delivery_key": key, "investigation": "unknown_metric"}

    background_tasks.add_task(_investigate, metric_id)
    return {"status": "accepted", "delivery_key": key, "investigation": "started", "metric_id": metric_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
