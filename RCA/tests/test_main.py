from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ALERT = {"title": "fill_rate anomaly", "body": "z=12.3", "link": "https://example.com/d/1"}


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_accepts_and_dedups_alert():
    first = client.post("/webhooks/alerts", json=ALERT)
    assert first.status_code == 202
    assert first.json()["status"] == "accepted"

    second = client.post("/webhooks/alerts", json=ALERT)
    assert second.status_code == 202
    assert second.json()["status"] == "duplicate"

    different = client.post("/webhooks/alerts", json={**ALERT, "body": "z=99.9"})
    assert different.json()["status"] == "accepted"


def test_alert_without_metric_id_skips_investigation():
    resp = client.post("/webhooks/alerts", json={**ALERT, "body": "no metric here"})
    assert resp.json()["investigation"] == "skipped"


def test_unknown_metric_id_skips_investigation():
    with patch("app.main.get_metric", return_value=None) as mocked:
        resp = client.post("/webhooks/alerts", json={**ALERT, "body": "metric_id=not_real"})
    mocked.assert_called_once_with("not_real")
    assert resp.json()["investigation"] == "unknown_metric"


def test_known_metric_id_starts_investigation():
    with patch("app.main.get_metric", return_value={"metric_id": "fill_rate"}), \
         patch("app.main.run_investigation") as mocked_investigate:
        resp = client.post("/webhooks/alerts", json={**ALERT, "body": "metric_id=fill_rate"})
    assert resp.json() == {
        "status": "accepted",
        "delivery_key": resp.json()["delivery_key"],
        "investigation": "started",
        "metric_id": "fill_rate",
    }
    mocked_investigate.assert_called_once_with("fill_rate")
