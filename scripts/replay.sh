#!/usr/bin/env bash
# =====================================================================
# InMobi Click-a-thon 2026 — replay ad_events into ClickHouse Cloud
# =====================================================================
#
#   ./scripts/replay.sh              apply SQL, then replay AD_EVENTS_FILE
#   ./scripts/replay.sh --schema     apply SQL only, no data
#   ./scripts/replay.sh --data       replay data only, no DDL
#   ./scripts/replay.sh --dims       also reload the 3 dimension CSVs
#
# ON SEALED-DATA NIGHT: change AD_EVENTS_FILE below to the jury's file,
# truncate manually (see TRUNCATE HELPER at the bottom), then run.
# This script never truncates by itself.
# ---------------------------------------------------------------------

set -euo pipefail

# ===================== CHANGE THIS FOR THE JURY FILE =================
AD_EVENTS_FILE="InMobi/data/ad_events.parquet"

# Shift every event_time forward by N WEEKS on ingest.
#
# Why this exists: ClickStack alert rules are evaluated against WALL CLOCK
# time. The shipped data ends 2026-07-05, which is 4 weeks in the past, so
# a "last 1 hour" alert rule sees an empty window and can never fire.
#
# Why WEEKS and not days/hours: shifting by a whole number of weeks keeps
# day-of-week AND hour-of-day alignment intact, so the seasonal baseline
# (RCA/app/metric_sql.py, partitioned on hour-of-day and weekday/weekend)
# stays valid. An arbitrary offset would silently corrupt every comparison.
#
# 0 = load timestamps exactly as delivered (correct for offline analysis).
# Set to the output of scripts/suggest_shift.sh for a live alerting demo.
# 6 puts the 2026-07-05 tail of the shipped file at ~2026-08-16 (2 weeks of
# future headroom past today, 2026-08-02).
TIME_SHIFT_WEEKS=6
# =====================================================================

DIM_DIR="InMobi/data"
DB="inmobi"
SQL_DIR="sql"
# metric_sql.py uses `X | None` annotations (3.10+); the system python3 here is 3.9.
# .venv is the same interpreter `uv run` gives RCA, so this stays in sync with it.
PY=".venv/bin/python3"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------- env
if [[ ! -f .env ]]; then echo "FATAL: .env not found in $REPO_ROOT" >&2; exit 1; fi
set -a; # shellcheck disable=SC1091
source .env; set +a

: "${CLICKHOUSE_HOST:?missing in .env}"
: "${CLICKHOUSE_USER:?missing in .env}"
: "${CLICKHOUSE_PASSWORD:?missing in .env}"
PORT="${CLICKHOUSE_HTTPS_PORT:-8443}"
URL="https://${CLICKHOUSE_HOST}:${PORT}"
AUTH="${CLICKHOUSE_USER}:${CLICKHOUSE_PASSWORD}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

# Run one SQL statement. Body on stdin so quoting never bites us.
#
# Sessions run against `default`, NOT ${DB}: this script has to be able to
# bootstrap a dropped database, and the connection check plus CREATE DATABASE
# both run before ${DB} exists. Every statement in sql/ is fully qualified
# (inmobi.x, and the dictionaries name DB 'inmobi' explicitly), so nothing
# depends on the session database.
ch() {
  local out
  out=$(curl -sS --fail-with-body -u "$AUTH" "${URL}/?database=default" \
          --data-binary @- 2>&1) || die "query failed: ${out}"
  printf '%s' "$out"
}
ch_sql() { printf '%s' "$1" | ch; }

# Stream a file in as a given FORMAT. Query goes in the URL, body is data.
ch_load() {
  local table="$1" file="$2" fmt="$3" extra="${4:-}"
  [[ -f "$file" ]] || die "input file not found: $file"
  local q; q=$(printf 'INSERT INTO %s.%s FORMAT %s' "$DB" "$table" "$fmt")
  local enc; enc=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$q")
  local out
  out=$(curl -sS --fail-with-body -u "$AUTH" \
          "${URL}/?query=${enc}${extra}" \
          --data-binary @- < "$file" 2>&1) || die "load into $table failed: ${out}"
}

# Split a .sql file on ';' at end-of-statement and execute sequentially.
# Uses process substitution (not a pipe) so a failing statement aborts the
# whole script instead of dying quietly in a subshell.
SPLIT_PY='
import re, sys
raw = open(sys.argv[1]).read()
body = re.sub(r"^\s*--.*$", "", raw, flags=re.M)   # strip line comments
for s in body.split(";"):
    if s.strip():
        sys.stdout.write(s.strip() + "\0")
'
apply_sql_file() {
  local f="$1" stmt
  log "applying $f"
  while IFS= read -r -d '' stmt; do
    printf '   · %s\n' "$(printf '%s' "$stmt" | head -c 72 | tr '\n' ' ')"
    ch_sql "$stmt" >/dev/null
  done < <(python3 -c "$SPLIT_PY" "$f")
}

MODE="${1:-all}"
DO_SCHEMA=1; DO_DATA=1; DO_DIMS=0
case "$MODE" in
  --schema) DO_DATA=0 ;;
  --data)   DO_SCHEMA=0 ;;
  --dims)   DO_DIMS=1 ;;
  all|"")   ;;
  *) die "unknown mode: $MODE" ;;
esac

log "target ${URL} · db=${DB}"
ch_sql "SELECT 1" >/dev/null && echo "   connection OK"

# ------------------------------------------------------------- schema
if (( DO_SCHEMA )); then
  ch_sql "CREATE DATABASE IF NOT EXISTS ${DB}" >/dev/null
  for f in 01_schema 02_dictionaries 03_silver 04_semantic_layer; do
    apply_sql_file "${SQL_DIR}/${f}.sql"
  done
fi

# --------------------------------------------------------- dimensions
# Loaded when explicitly asked, or automatically when apps is empty.
APPS_N=$(ch_sql "SELECT count() FROM ${DB}.apps" | tr -d '[:space:]')
if (( DO_DIMS )) || [[ "$APPS_N" == "0" ]]; then
  log "loading dimension tables"
  CSV_OPTS="&input_format_with_names_use_header=1"
  ch_load apps        "${DIM_DIR}/apps.csv"        CSVWithNames "$CSV_OPTS"
  ch_load advertisers "${DIM_DIR}/advertisers.csv" CSVWithNames "$CSV_OPTS"
  ch_load geo_device  "${DIM_DIR}/geo_device.csv"  CSVWithNames "$CSV_OPTS"
  for d in dict_apps dict_advertisers dict_geo_device; do
    ch_sql "SYSTEM RELOAD DICTIONARY ${DB}.${d}" >/dev/null
  done
  echo "   dims loaded and dictionaries reloaded"
else
  echo "   dimensions already present (${APPS_N} apps) — skipping"
fi

# --------------------------------------------------------------- data
if (( DO_DATA )); then
  log "replaying ${AD_EVENTS_FILE}"
  echo "   MV1 will populate ad_events_enriched on insert"
  START=$(date +%s)

  if [[ "$TIME_SHIFT_WEEKS" -eq 0 ]]; then
    ch_load ad_events "$AD_EVENTS_FILE" Parquet
  else
    # input() transforms on ingest, so the shift costs one pass and no
    # staging table. Column list must match the parquet schema exactly.
    echo "   shifting event_time by +${TIME_SHIFT_WEEKS} week(s)"
    SCHEMA='event_time DateTime64(3), app_id String, geo_device_id String,
            advertiser_id String, ad_format String, is_filled UInt8,
            is_impression UInt8, is_click UInt8, revenue Float64'
    Q="INSERT INTO ${DB}.ad_events
       SELECT event_time + INTERVAL ${TIME_SHIFT_WEEKS} WEEK,
              app_id, geo_device_id, advertiser_id, ad_format,
              is_filled, is_impression, is_click, revenue
       FROM input('${SCHEMA}') FORMAT Parquet"
    ENC=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$Q")
    OUT=$(curl -sS --fail-with-body -u "$AUTH" "${URL}/?query=${ENC}" \
            --data-binary @- < "$AD_EVENTS_FILE" 2>&1) || die "shifted load failed: ${OUT}"
  fi
  echo "   done in $(( $(date +%s) - START ))s"
fi

# ---------------------------------------------------------- verify
log "row counts"
ch_sql "
SELECT 'ad_events' AS layer, count() AS rows, toString(min(event_time)) AS from_ts, toString(max(event_time)) AS to_ts FROM ${DB}.ad_events
UNION ALL SELECT 'ad_events_enriched', count(), toString(min(event_time)), toString(max(event_time)) FROM ${DB}.ad_events_enriched
FORMAT PrettyCompactMonoBlock"

log "enrichment health (want 0 unknowns)"
ch_sql "
SELECT
  countIf(category='unknown')   AS unknown_category,
  countIf(region='unknown')     AS unknown_region,
  countIf(os_version='unknown') AS unknown_os,
  countIf(advertiser_id!='' AND vertical='unknown') AS unknown_vertical
FROM ${DB}.ad_events_enriched
FORMAT PrettyCompactMonoBlock"

# Deviation is not a stored view — it is rendered from metric_def by the same
# builder the RCA agent uses, so this check exercises the real detection path
# rather than a parallel copy of the maths.
for m in fill_rate requests ecpm revenue; do
  log "top segments for ${m} (live deviation scan)"
  SCAN=$("$PY" scripts/metric_query.py scan "$m") || die "could not render scan for ${m}"
  ch_sql "${SCAN} FORMAT PrettyCompactMonoBlock"
done

log "done"

# =====================================================================
# TRUNCATE HELPER — deliberately NOT run by this script.
# Paste manually before replaying a different dataset:
#
#   TRUNCATE TABLE inmobi.ad_events;
#   TRUNCATE TABLE inmobi.ad_events_enriched;
# =====================================================================
