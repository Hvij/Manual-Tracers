# InMobi Click-a-thon 2026 — Automated Root-Cause Analyst

A metric moves. The system detects it, drills down in ClickHouse to isolate the
responsible segment, and produces an evidence-backed diagnosis where every number
is computed — not narrated into existence.

- **Architecture:** [architecture.md](architecture.md)
- **As-built RCA agent design:** [docs/RCA_AGENT_DESIGN.md](docs/RCA_AGENT_DESIGN.md)
- **RCA output contract:** [docs/RCA_OUTPUT_CONTRACT.md](docs/RCA_OUTPUT_CONTRACT.md)
- **Problem statement:** [InMobi/PROBLEM_STATEMENT.md](InMobi/PROBLEM_STATEMENT.md)
- **Metric definitions:** [InMobi/metrics_glossary.md](InMobi/metrics_glossary.md)

## Layers

| Layer | Object | Role |
|---|---|---|
| bronze | `inmobi.ad_events` | raw, replayed |
| silver | `inmobi.ad_events_enriched` | denormalised via dictionaries · RCA drill surface |
| gold | `inmobi.metric_1h` | hourly marginals · **alert surface** (~53K rows) |
| metric layer | `v_metric_points` | the only place a formula is written |
| detection | `v_metric_baseline` → `v_metric_deviation` | seasonal baseline, guard-railed scoring |
| alert → agent | ClickStack tile alert on `v_metric_deviation` → webhook → `RCA/app/` | see [docs/RCA_AGENT_DESIGN.md](docs/RCA_AGENT_DESIGN.md) §3 |

No persisted incident table sits between detection and the agent — it
re-derives everything live per alert. An earlier `v_incidents`/`anomaly_events`
ledger was removed as a duplicate source of truth.

## Run

```bash
cp .env.example .env      # fill in ClickHouse Cloud creds
./scripts/replay.sh       # apply SQL + replay ad_events; MVs populate silver + gold
```

Modes: `--schema` (DDL only) · `--data` (replay only) · `--dims` (reload dimensions).

**Sealed dataset:** change `AD_EVENTS_FILE` at the top of `scripts/replay.sh`,
truncate manually (helper at the bottom of the script), then re-run. The script
never truncates by itself.

## Confirmed detections

| Day(s) | Segment | Actual | Expected | Peak z |
|---|---|---|---|---|
| Jun 23–25 | `os_version=Android 15` | 0.434 | 0.785 | 28.1 |
| Jun 29–30 | `os_version=iOS 18.1` | 0.683 | 0.780 | 10.6 |
| Jun 23–25 | global fill rate | 0.750 | 0.785 | 11.4 |

~2s over 9M rows.
