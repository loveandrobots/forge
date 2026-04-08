"""Tests for the forge-task-writer skill file content.

Validates that the skill contains the required priority tier guidance
consistent with docs/priority-conventions.md.
"""
import json
import re
from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "forge-task-writer" / "SKILL.md"
CONVENTIONS_PATH = Path(__file__).resolve().parent.parent / "docs" / "priority-conventions.md"


def _read_skill() -> str:
    return SKILL_PATH.read_text()


def _read_conventions() -> str:
    return CONVENTIONS_PATH.read_text()


def test_skill_file_exists():
    assert SKILL_PATH.exists(), f"Skill file not found at {SKILL_PATH}"


def test_skill_contains_priority_assignment_section():
    content = _read_skill()
    assert "## Priority Assignment" in content


def test_skill_contains_all_tier_names():
    content = _read_skill()
    for tier in ["Critical", "Blocking", "Active", "Queued", "Background", "Someday"]:
        assert tier in content, f"Missing tier: {tier}"


def test_skill_contains_tier_ranges():
    content = _read_skill()
    assert "100" in content, "Missing Critical range 100"
    # Check that each tier's numeric boundary appears in the skill.
    # Using loose checks since the table uses en-dashes.
    for boundary in ["80", "99", "60", "79", "40", "59", "20", "39", "1", "19"]:
        assert boundary in content, f"Missing tier boundary: {boundary}"


def test_standalone_task_default_priority_is_50():
    content = _read_skill()
    assert "50" in content
    # Verify the standalone default is explicitly 50, not 0.
    lower = content.lower()
    assert "standalone" in lower
    # The old default of 0 should not appear as a priority default.
    assert "priority 0" not in lower
    assert "priority (0" not in lower


def test_epic_subtask_countdown_by_twos_pattern():
    content = _read_skill()
    assert "71, 69, 67, 65, 63, 61" in content


def test_follow_up_task_parent_minus_one():
    content = _read_skill()
    lower = content.lower()
    assert "parent priority minus 1" in lower or "parent minus 1" in lower


def test_hotfix_priority_80_plus():
    content = _read_skill()
    assert "80+" in content
    lower = content.lower()
    assert "hotfix" in lower


def test_gaps_of_20_mentioned():
    content = _read_skill()
    assert "20" in content
    lower = content.lower()
    assert "gap" in lower
    assert "renumber" in lower


def test_renumber_only_for_two_or_more_insertions():
    content = _read_skill()
    lower = content.lower()
    assert "two or more" in lower


def test_guidance_framed_as_defaults_not_requirements():
    content = _read_skill()
    lower = content.lower()
    # Should frame as defaults / overridable.
    assert "default" in lower
    # Should mention user can override.
    assert "user specifies" in lower or "user override" in lower or "override" in lower


def test_example_output_uses_new_priority_scheme():
    """The example JSON should use priorities > 0 consistent with the tier system."""
    content = _read_skill()
    # Extract the JSON array after the "## Example Output" heading.
    example_section = content.split("## Example Output")[-1]
    example_match = re.search(r'(\[.*\])\s*$', example_section, re.DOTALL)
    assert example_match, "No JSON example found in skill"
    example_json = json.loads(example_match.group(1))
    for task in example_json:
        assert task["priority"] > 0, f"Example task has priority 0 or below: {task}"
        assert task["priority"] != 0, f"Example still uses old priority 0: {task}"


def test_higher_numbers_run_first_documented():
    """The skill should clarify that higher priority numbers run first."""
    content = _read_skill()
    lower = content.lower()
    assert "higher numbers run first" in lower or "highest-priority" in lower or "highest first" in lower


def test_skill_consistent_with_conventions_doc():
    """Cross-check key facts between the skill and the conventions doc."""
    skill = _read_skill()
    conventions = _read_conventions()

    # Both should mention the same tier names.
    for tier in ["Critical", "Blocking", "Active", "Queued", "Background", "Someday"]:
        assert tier in skill, f"Skill missing tier: {tier}"
        assert tier in conventions, f"Conventions missing tier: {tier}"

    # Both should agree on standalone default of 50.
    assert "50" in skill
    assert "50" in conventions

    # Both should mention the countdown example.
    assert "71, 69, 67, 65, 63, 61" in skill
    assert "71, 69, 67, 65, 63, 61" in conventions

    # Both should mention parent minus 1 for follow-ups.
    assert "minus 1" in skill.lower()
    assert "minus 1" in conventions.lower()

    # Both should mention 80+ for hotfixes.
    assert "80+" in skill
    assert "80+" in conventions
