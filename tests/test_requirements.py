"""Tests that requirements.txt declares all necessary dependencies."""

import pathlib


REQ_PATH = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"


def _read_requirements() -> list[str]:
    return REQ_PATH.read_text().splitlines()


def test_pytest_asyncio_in_requirements() -> None:
    """pytest-asyncio must be declared since asyncio_mode=auto in pyproject.toml."""
    lines = _read_requirements()
    assert any(line.startswith("pytest-asyncio") for line in lines)


def test_pytest_asyncio_pinned_below_v2() -> None:
    """pytest-asyncio should be pinned to <2 to avoid breaking changes."""
    lines = _read_requirements()
    asyncio_lines = [l for l in lines if l.startswith("pytest-asyncio")]
    assert len(asyncio_lines) == 1
    spec = asyncio_lines[0]
    assert "<2" in spec
