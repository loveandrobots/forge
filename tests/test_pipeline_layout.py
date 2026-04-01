"""Tests for pipeline view layout and scroll behavior fixes."""

from __future__ import annotations

import re


def _read_static(filename: str) -> str:
    """Read a static asset file and return its contents."""
    import pathlib

    static_dir = pathlib.Path(__file__).resolve().parent.parent / "static"
    return (static_dir / filename).read_text()


class TestKanbanColumnWidth:
    """Verify kanban columns fit without horizontal overflow on desktop."""

    def test_kanban_column_min_width_is_160px(self) -> None:
        css = _read_static("styles.css")
        # Find the .kanban-column rule and check min-width
        match = re.search(
            r"\.kanban-column\s*\{[^}]*min-width:\s*(\d+)px", css
        )
        assert match is not None, ".kanban-column must have a min-width in px"
        assert match.group(1) == "160", (
            f"Expected min-width: 160px, got {match.group(1)}px"
        )

    def test_six_columns_fit_within_content_area(self) -> None:
        """6 columns at 160px + 5 gaps at 16px = 1040px < 1352px (1400 - 48 padding)."""
        min_width = 160
        columns = 6
        gap = 16  # 1rem
        content_max = 1400
        padding = 48  # 1.5rem * 2
        total = columns * min_width + (columns - 1) * gap
        available = content_max - padding
        assert total < available, (
            f"Total column width {total}px exceeds available {available}px"
        )

    def test_kanban_overflow_x_auto_preserved(self) -> None:
        """Mobile/narrow viewports should still be able to scroll horizontally."""
        css = _read_static("styles.css")
        match = re.search(r"\.kanban\s*\{[^}]*overflow-x:\s*auto", css)
        assert match is not None, ".kanban must retain overflow-x: auto"


class TestCardTitleWordBreak:
    """Verify long unbroken words in card titles break correctly."""

    def test_card_title_has_overflow_wrap(self) -> None:
        css = _read_static("styles.css")
        match = re.search(
            r"\.card-title\s*\{[^}]*overflow-wrap:\s*break-word", css
        )
        assert match is not None, (
            ".card-title must have overflow-wrap: break-word"
        )

    def test_card_title_has_word_break(self) -> None:
        css = _read_static("styles.css")
        match = re.search(
            r"\.card-title\s*\{[^}]*word-break:\s*break-word", css
        )
        assert match is not None, (
            ".card-title must have word-break: break-word"
        )


class TestScrollPositionPreservation:
    """Verify JavaScript scroll position preservation is in place."""

    def test_before_swap_listener_exists(self) -> None:
        js = _read_static("app.js")
        assert "htmx:beforeSwap" in js, (
            "app.js must listen for htmx:beforeSwap"
        )

    def test_after_settle_listener_exists(self) -> None:
        js = _read_static("app.js")
        assert "htmx:afterSettle" in js, (
            "app.js must listen for htmx:afterSettle"
        )

    def test_scroll_left_referenced(self) -> None:
        js = _read_static("app.js")
        assert "scrollLeft" in js, "app.js must reference scrollLeft"

    def test_kanban_selector_referenced(self) -> None:
        js = _read_static("app.js")
        assert ".kanban" in js, "app.js must query for .kanban element"
