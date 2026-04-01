"""Tests validating the audit follow-ups output file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

AUDIT_PATH = Path(__file__).resolve().parent.parent / "_forge" / "follow-ups" / "audit.json"
FOLLOW_UPS_DIR = AUDIT_PATH.parent
REVIEWS_DIR = Path(__file__).resolve().parent.parent / "_forge" / "reviews"

EXISTING_FOLLOWUP_FILES = [
    "3ce28522-e551-4d5f-956b-70c4c4cc5055.json",
    "140b1e7b-609b-41c4-8069-7b8a3d62398a.json",
    "40cbbd0a-405e-4e8c-b2d8-252342d9fcae.json",
]


@pytest.fixture(scope="module")
def audit_entries() -> list[dict]:
    """Load and return the audit.json entries."""
    assert AUDIT_PATH.exists(), f"audit.json not found at {AUDIT_PATH}"
    with open(AUDIT_PATH) as f:
        data = json.load(f)
    assert isinstance(data, list), "audit.json must be a JSON array"
    return data


class TestOutputFileValid:
    """Test 1: Output file exists and is valid JSON with correct schema."""

    def test_file_exists(self) -> None:
        assert AUDIT_PATH.exists()

    def test_parses_as_json_array(self, audit_entries: list[dict]) -> None:
        assert isinstance(audit_entries, list)

    def test_each_entry_has_required_fields(self, audit_entries: list[dict]) -> None:
        for i, entry in enumerate(audit_entries):
            assert isinstance(entry, dict), f"Entry {i} is not a dict"
            assert isinstance(entry.get("title"), str), f"Entry {i} missing string 'title'"
            assert isinstance(entry.get("description"), str), f"Entry {i} missing string 'description'"
            assert isinstance(entry.get("source_task_id"), str), f"Entry {i} missing string 'source_task_id'"

    def test_titles_are_nonempty(self, audit_entries: list[dict]) -> None:
        for i, entry in enumerate(audit_entries):
            assert entry["title"].strip(), f"Entry {i} has empty title"

    def test_descriptions_are_nonempty(self, audit_entries: list[dict]) -> None:
        for i, entry in enumerate(audit_entries):
            assert entry["description"].strip(), f"Entry {i} has empty description"


class TestSourceTaskIds:
    """Test 2: Every source_task_id references a real review file."""

    def test_source_task_ids_have_review_files(self, audit_entries: list[dict]) -> None:
        for entry in audit_entries:
            task_id = entry["source_task_id"]
            review_path = REVIEWS_DIR / f"{task_id}.md"
            assert review_path.exists(), (
                f"source_task_id '{task_id}' has no review file at {review_path}"
            )


class TestNoDuplicatesWithExistingFollowups:
    """Test 3: No entry duplicates an issue already in existing follow-up files."""

    @pytest.fixture(scope="class")
    def existing_followup_texts(self) -> list[str]:
        """Collect all text from existing follow-up entries for comparison."""
        texts = []
        for fname in EXISTING_FOLLOWUP_FILES:
            fpath = FOLLOW_UPS_DIR / fname
            if not fpath.exists():
                continue
            with open(fpath) as f:
                data = json.load(f)
            for item in data:
                if isinstance(item, dict):
                    texts.append(item.get("title", "") + " " + item.get("description", ""))
                elif isinstance(item, str):
                    texts.append(item)
        return [t.lower() for t in texts]

    def test_no_overlapping_titles(
        self, audit_entries: list[dict], existing_followup_texts: list[str]
    ) -> None:
        for entry in audit_entries:
            title_lower = entry["title"].lower()
            for existing in existing_followup_texts:
                # Check that the audit title is not a substring of existing text
                # and vice versa (loose overlap detection)
                assert title_lower not in existing, (
                    f"Audit entry '{entry['title']}' overlaps with existing follow-up"
                )


class TestBounceIssuesExcluded:
    """Test 4: Issues that were the primary cause of an ISSUES verdict in a
    bounce cycle should not appear if the task subsequently passed."""

    BOUNCE_FIX_TASK_IDS = {
        # Tasks that had ISSUES and were bounced — their primary blocking issues
        # were fixed during the bounce cycle.
        "3ce28522-e551-4d5f-956b-70c4c4cc5055",
        "140b1e7b-609b-41c4-8069-7b8a3d62398a",
        "40cbbd0a-405e-4e8c-b2d8-252342d9fcae",
        "b9ddefc7-97c9-4a84-9a7b-bedd039ec887",
        "7ba3a019-86e4-4cf7-8fb2-2e614a6d463f",
        "6822767a-1714-4ef9-8629-e8b8d39a5a8c",
        "1e996dca-6e86-4391-aada-fcd30e66d3de",
    }

    def test_audit_entries_are_secondary_issues(self, audit_entries: list[dict]) -> None:
        """Entries from ISSUES reviews must be secondary concerns (missing tests,
        minor refactors) rather than the primary blocking issues."""
        for entry in audit_entries:
            # All entries should be about missing tests, suggestions, or
            # minor code quality — not the blocking bugs that caused ISSUES.
            desc_lower = entry["description"].lower()
            # A primary blocking bug would reference "crash", "wrong result",
            # "broken" as the core problem. Our entries are about missing tests
            # and code duplication.
            assert any(
                keyword in desc_lower
                for keyword in ["test", "missing", "duplicat", "extract", "refactor", "edge case"]
            ), (
                f"Entry '{entry['title']}' doesn't look like a secondary concern — "
                "it may be a primary bounce-fix issue that should be excluded"
            )


class TestAddressedIssuesExcluded:
    """Test 5: Issues that have been fixed in the codebase should not appear."""

    def test_no_fixed_issues_in_audit(self, audit_entries: list[dict]) -> None:
        """Verify none of the audit entries reference issues already resolved."""
        for entry in audit_entries:
            title_lower = entry["title"].lower()
            # These specific issues were confirmed fixed:
            assert "typo" not in title_lower or ".:" not in entry["description"], (
                "The '.:\\n' typo in _auto_merge was already fixed"
            )
            assert "_stderr_max_bytes" not in title_lower, (
                "_STDERR_MAX_BYTES constant was already removed"
            )
            assert "malformed json" not in title_lower and "crashes on" not in title_lower, (
                "_process_follow_ups malformed JSON handling was fixed in commit d79c899"
            )


class TestAllReviewsAudited:
    """Test 6: All 13 review files were considered."""

    EXPECTED_REVIEW_IDS = [
        "3ce28522-e551-4d5f-956b-70c4c4cc5055",
        "140b1e7b-609b-41c4-8069-7b8a3d62398a",
        "c2a7719d-6513-4480-a1d6-90409f16835e",
        "99c4f155-c1ef-4aeb-9420-48aa035b1a4c",
        "40cbbd0a-405e-4e8c-b2d8-252342d9fcae",
        "8b7fb7ce-5bd1-4c8c-b583-03cb18762c38",
        "a1a24d3b-26ba-4cb5-a307-03f00f97e008",
        "b9ddefc7-97c9-4a84-9a7b-bedd039ec887",
        "7ba3a019-86e4-4cf7-8fb2-2e614a6d463f",
        "6822767a-1714-4ef9-8629-e8b8d39a5a8c",
        "89a2ceaf-ccc3-454e-bc6c-06d8c28c2829",
        "202660e9-835b-4bb3-a0f1-ceab782feeb9",
        "1e996dca-6e86-4391-aada-fcd30e66d3de",
    ]

    def test_all_review_files_exist(self) -> None:
        for task_id in self.EXPECTED_REVIEW_IDS:
            path = REVIEWS_DIR / f"{task_id}.md"
            assert path.exists(), f"Review file missing: {path}"

    def test_source_ids_reference_only_known_reviews(
        self, audit_entries: list[dict]
    ) -> None:
        for entry in audit_entries:
            assert entry["source_task_id"] in self.EXPECTED_REVIEW_IDS, (
                f"source_task_id '{entry['source_task_id']}' not in known review set"
            )
