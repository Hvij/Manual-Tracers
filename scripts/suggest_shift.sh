#!/usr/bin/env bash
# Print the TIME_SHIFT_WEEKS value that lands a dataset's newest event on
# "now", rounded DOWN to whole weeks so day-of-week alignment is preserved.
#
#   ./scripts/suggest_shift.sh                       # inspect loaded data
#   ./scripts/suggest_shift.sh 2026-07-05            # from a known max date
#
# Paste the result into TIME_SHIFT_WEEKS at the top of scripts/replay.sh.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO_ROOT"

if [[ -n "${1:-}" ]]; then
  MAX_TS="$1"
else
  set -a; source .env; set +a
  MAX_TS=$(curl -sS -u "${CLICKHOUSE_USER}:${CLICKHOUSE_PASSWORD}" \
    "https://${CLICKHOUSE_HOST}:${CLICKHOUSE_HTTPS_PORT:-8443}/?database=inmobi" \
    --data-binary "SELECT toDate(max(event_time)) FROM inmobi.ad_events" | tr -d '[:space:]')
  [[ -n "$MAX_TS" ]] || { echo "could not read max(event_time) — pass a date instead" >&2; exit 1; }
fi

DAYS=$(( ( $(date -u +%s) - $(date -u -d "$MAX_TS" +%s 2>/dev/null || date -u -j -f %Y-%m-%d "$MAX_TS" +%s) ) / 86400 ))
WEEKS=$(( DAYS / 7 ))

echo "newest event : $MAX_TS"
echo "days stale   : $DAYS"
echo
echo "TIME_SHIFT_WEEKS=$WEEKS"
echo
if (( DAYS % 7 != 0 )); then
  echo "note: $(( DAYS % 7 )) day(s) of residual staleness after a ${WEEKS}-week shift."
  echo "      Rounding down is deliberate — never shift by partial weeks, it"
  echo "      breaks weekday alignment and corrupts the seasonal baseline."
fi
