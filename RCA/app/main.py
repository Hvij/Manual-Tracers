import hashlib
import json
import logging
import re
import time

from fastapi import BackgroundTasks, FastAPI, status

from app.investigate import run_investigation
from app.registry import get_metric
from app.schemas import ClickStackAlertPayload

METRIC_ID_RE = re.compile(r"metric_id=(\w+)")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rca_agent")

app = FastAPI(title="RCA Agent Webhook")

# ponytail: in-memory dedup, good for one process only — move to a ClickHouse/redis
# table if this ever runs behind more than one worker.
_seen: dict[str, float] = {}
_DEDUP_WINDOW_S = 300


def _dedup_key(payload: ClickStackAlertPayload) -> str:
    return hashlib.sha256(f"{payload.title}|{payload.body}".encode()).hexdigest()


def _cleanup(now: float) -> None:
    stale = [k for k, ts in _seen.items() if now - ts > _DEDUP_WINDOW_S]
    for k in stale:
        del _seen[k]


@app.get("/health")
def health():
    return {"status": "ok"}


def _extract_metric_id(payload: ClickStackAlertPayload) -> str | None:
    match = METRIC_ID_RE.search(payload.body) or METRIC_ID_RE.search(payload.title)
    return match.group(1) if match else None


def _investigate(metric_id: str) -> None:
    ledger = run_investigation(metric_id)
    logger.info("investigation for %s -> %s", metric_id, json.dumps(ledger, default=str))


@app.post("/webhooks/alerts", status_code=status.HTTP_202_ACCEPTED)
async def receive_alert(payload: ClickStackAlertPayload, background_tasks: BackgroundTasks):
    now = time.time()
    _cleanup(now)

    key = _dedup_key(payload)
    if key in _seen:
        logger.info("duplicate alert ignored: %s", payload.title)
        return {"status": "duplicate", "delivery_key": key}

    _seen[key] = now
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
