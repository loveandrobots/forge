"""Tests for forge.utils — relative_time utility function."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from forge.utils import relative_time

# Fixed "now" for all tests
_NOW = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)


def _iso(delta: timedelta) -> str:
    """Return an ISO timestamp `delta` before _NOW."""
    return (_NOW - delta).isoformat()


def _patch_now():
    """Patch datetime.now in forge.utils to return _NOW."""
    return patch("forge.utils.datetime", wraps=datetime, **{
        "now.return_value": _NOW,
    })


class TestRelativeTimeNone:
    def test_none_input(self) -> None:
        assert relative_time(None) is None

    def test_none_input_with_suffix(self) -> None:
        assert relative_time(None, suffix="") is None


class TestRelativeTimeUnder60Seconds:
    def test_under_60_seconds(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(seconds=30))) == "just now"

    def test_zero_seconds(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(seconds=0))) == "just now"

    def test_just_now_ignores_suffix(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(seconds=10)), suffix="") == "just now"


class TestRelativeTimeMinutes:
    def test_exactly_60_seconds(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(seconds=60))) == "1m ago"

    def test_under_60_minutes(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(minutes=45))) == "45m ago"

    def test_59_minutes(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(minutes=59, seconds=59))) == "59m ago"


class TestRelativeTimeHours:
    def test_exactly_60_minutes(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(minutes=60))) == "1h ago"

    def test_under_24_hours(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(hours=12))) == "12h ago"

    def test_23_hours(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(hours=23, minutes=59))) == "23h ago"


class TestRelativeTimeDays:
    def test_exactly_24_hours(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(hours=24))) == "1d ago"

    def test_under_7_days(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(days=5))) == "5d ago"

    def test_6_days(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(days=6, hours=23))) == "6d ago"


class TestRelativeTimeDate:
    def test_exactly_7_days(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(days=7))) == "2026-03-23"

    def test_over_7_days(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(days=30))) == "2026-02-28"


class TestRelativeTimeSuffix:
    def test_suffix_empty(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(minutes=5)), suffix="") == "5m"

    def test_suffix_empty_hours(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(hours=2)), suffix="") == "2h"

    def test_suffix_empty_days(self) -> None:
        with _patch_now():
            assert relative_time(_iso(timedelta(days=3)), suffix="") == "3d"


class TestRelativeTimeTimezoneHandling:
    def test_naive_timestamp_treated_as_utc(self) -> None:
        naive_ts = "2026-03-30T11:55:00"
        with _patch_now():
            assert relative_time(naive_ts) == "5m ago"

    def test_aware_timestamp(self) -> None:
        aware_ts = "2026-03-30T11:55:00+00:00"
        with _patch_now():
            assert relative_time(aware_ts) == "5m ago"
