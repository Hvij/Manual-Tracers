"""
Replay historical Parquet data into ClickHouse, time-compressed so that
HyperDX's real (scheduled) alert evaluation sees it and can fire real
webhooks -- instead of waiting out the actual N-day span in real time.

MECHANISM
---------
HyperDX alerts run on a fixed interval (e.g. 1 minute) and each tick checks
`Timestamp BETWEEN now() - interval AND now()` against your table. To
replay many days quickly and still get *real* evaluations + webhook fires:

  1. Split the data into chunks (default: one per original calendar day).
  2. TRUNCATE the scratch table before each chunk, so only that chunk's
     data is ever visible -- this keeps chunks isolated even if your test
     alert's query has no time filter of its own.
  3. Rescale that chunk's timestamps -- preserving relative order and
     spacing -- into a short "landing zone" inside the next upcoming
     evaluation window, then insert.
  4. Sleep until that window has closed, then repeat for the next chunk.

Row *count* and relative shape within each chunk are preserved, so
count / threshold / percentile-based alert logic evaluates the same way
it would have against the original, uncompressed data.

SETUP (once)
------------
1. In ClickHouse:
     CREATE TABLE my_table_replay_test AS my_table;

2. In HyperDX: clone the real alert you want to test.
     - point it at the scratch table above, not production
     - set interval to the shortest available (1m)
     - point the webhook at a disposable test endpoint (e.g.
       https://webhook.site) instead of your real Slack/PagerDuty channel
     - keep (or add) a time filter in the query ($__timeFilter(...) or the
       startDateMilliseconds/endDateMilliseconds macros) -- the TRUNCATE
       step protects you even without one, but keeping it gives cleaner
       per-chunk attribution if you ever skip the truncate

3. pip install clickhouse-connect pandas pyarrow
"""

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import clickhouse_connect

# ---------------------------------------------------------------- CONFIG

PARQUET_PATH = "data.parquet"
TIMESTAMP_COL = "timestamp"              # your event-time column

CH_HOST = "localhost"
CH_PORT = 8123
CH_USER = "default"
CH_PASSWORD = ""
CH_DATABASE = "default"
TARGET_TABLE = "my_table_replay_test"    # scratch table -- NOT production

CHUNK_FREQ = "D"          # "D" = one chunk per calendar day (~30 chunks for
                           # 30 days -> ~30 min total at 60s/chunk below).
                           # "h" = one chunk per hour catches intra-day
                           # spikes too, but ~720 chunks -> ~12 hours.

TEST_ALERT_INTERVAL_SECONDS = 60   # must match the cloned test alert's interval
SAFETY_BUFFER_SECONDS = 5          # margin left at each window's edges

# -------------------------------------------------------------------------


def main():
    df = pd.read_parquet(PARQUET_PATH)
    df[TIMESTAMP_COL] = pd.to_datetime(df[TIMESTAMP_COL])
    df = df.sort_values(TIMESTAMP_COL).reset_index(drop=True)

    df["_chunk_key"] = df[TIMESTAMP_COL].dt.floor(CHUNK_FREQ)
    chunks = [g.drop(columns="_chunk_key") for _, g in df.groupby("_chunk_key")]

    est_minutes = len(chunks) * TEST_ALERT_INTERVAL_SECONDS / 60
    print(f"{len(chunks)} chunks ({CHUNK_FREQ}), spanning "
          f"{df[TIMESTAMP_COL].min()} -> {df[TIMESTAMP_COL].max()}")
    print(f"Estimated wall-clock runtime: ~{est_minutes:.0f} minutes "
          f"({TEST_ALERT_INTERVAL_SECONDS}s per chunk)")
    input("Press Enter to start, or Ctrl+C to abort... ")

    client = clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, username=CH_USER,
        password=CH_PASSWORD, database=CH_DATABASE,
    )

    landing_zone = TEST_ALERT_INTERVAL_SECONDS - 2 * SAFETY_BUFFER_SECONDS
    if landing_zone <= 0:
        raise ValueError("SAFETY_BUFFER_SECONDS too large for this interval")

    for i, chunk in enumerate(chunks, start=1):
        client.command(f"TRUNCATE TABLE {TARGET_TABLE}")

        chunk = chunk.copy()
        orig_start = chunk[TIMESTAMP_COL].min()
        orig_span = (chunk[TIMESTAMP_COL].max() - orig_start).total_seconds() or 1.0
        ratio = landing_zone / orig_span

        # naive UTC "now" -- keeps this consistent with typical tz-naive
        # Parquet/ClickHouse DateTime columns. If your timestamps are
        # tz-aware, drop the .replace(tzinfo=None) below.
        target_start = (datetime.now(timezone.utc).replace(tzinfo=None)
                         + timedelta(seconds=SAFETY_BUFFER_SECONDS))
        chunk[TIMESTAMP_COL] = target_start + (chunk[TIMESTAMP_COL] - orig_start) * ratio

        client.insert_df(TARGET_TABLE, chunk)
        print(f"[{i:>3}/{len(chunks)}] {len(chunk):>6} rows | "
              f"orig {orig_start.date()} -> landing ~{target_start.time()}")

        time.sleep(TEST_ALERT_INTERVAL_SECONDS)

    print("Replay complete. Check the alert's evaluation history in HyperDX "
          "and your test webhook endpoint.")


if __name__ == "__main__":
    main()