"""Standalone utility functions."""

from __future__ import annotations

from datetime import datetime, timezone


def relative_time(iso_timestamp: str | None, *, suffix: str = " ago") -> str | None:
    """Convert an ISO timestamp to a human-readable relative time string.

    Returns None if iso_timestamp is None.
    """
    if iso_timestamp is None:
        return None

    dt = datetime.fromisoformat(iso_timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    total_seconds = max(0, int((now - dt).total_seconds()))

    if total_seconds < 60:
        return "just now"
    elif total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes}m{suffix}"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        return f"{hours}h{suffix}"
    elif total_seconds < 604800:
        days = total_seconds // 86400
        return f"{days}d{suffix}"
    else:
        return dt.strftime("%Y-%m-%d")
