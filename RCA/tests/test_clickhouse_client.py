from datetime import datetime, timezone

from app.clickhouse_client import _as_utc


def test_naive_datetime_is_labeled_utc_not_converted():
    # the bug: clickhouse_connect assumes a naive datetime is in the machine's LOCAL
    # timezone and converts it before binding. Every datetime this app produces is already
    # a UTC wall-clock value with no tzinfo — labeling it UTC must be a no-op on the wall
    # clock, not a conversion (regression for the ~5.5h-off compressed-replay silent miss).
    naive = datetime(2026, 8, 1, 21, 49, 42)
    result = _as_utc(naive)

    assert result == datetime(2026, 8, 1, 21, 49, 42, tzinfo=timezone.utc)
    assert result.hour == 21 and result.minute == 49


def test_already_aware_datetime_is_left_untouched():
    aware = datetime(2026, 8, 1, 21, 49, 42, tzinfo=timezone.utc)
    assert _as_utc(aware) is aware


def test_non_datetime_values_pass_through_unchanged():
    assert _as_utc("fill_rate") == "fill_rate"
    assert _as_utc(42) == 42
    assert _as_utc(None) is None
