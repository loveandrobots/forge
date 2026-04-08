"""Prompt builder — assembles stage-specific prompts from templates and task/artifact data."""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage prompt templates (from spec section 8)
# ---------------------------------------------------------------------------

SPEC_TEMPLATE = """\
You are working on the project "{project_name}".

## Task
{task_title}

{task_description}

## Your job
Write a specification for this task. Your output is captured as structured JSON automatically — do not save a file.

Your response must be a JSON object with these fields:
- **overview** (string): What this task accomplishes in 2-3 sentences.
- **acceptance_criteria** (array): A list of objects, each with a sequential integer `id` (starting at 1) and a `text` string. Each criterion must be genuinely binary (pass/fail) and objectively verifiable — no subjective language like "looks good" or "feels right." A reviewer with no context beyond this spec must be able to mechanically check each criterion.
- **out_of_scope** (array of strings): What this task explicitly does NOT include.
- **dependencies** (array of strings): Any existing code, APIs, or features this task depends on.
- **content** (string): The full spec as markdown prose for human reading.

Read the project's existing documentation before writing the spec to ensure alignment with established patterns and decisions.

Load the following skills for context:
{skill_references}

{retry_context}"""

PLAN_TEMPLATE = """\
You are working on the project "{project_name}".

## Specification
{spec_content}

## Acceptance criteria from spec
{spec_criteria_list}

## Your job
Write an implementation plan for this spec. Your output is captured as structured JSON automatically — do not save a file.

Your response must be a JSON object with these fields:
- **approach** (string): How you will implement this, in 2-3 paragraphs.
- **acceptance_criteria_mapping** (array): For each acceptance criterion listed above, an object with `criterion_id` (integer matching the spec's ID), `criterion_text` (the exact criterion text), and `implementation` (how you will satisfy it). Every criterion ID from the spec must be mapped.
- **files_to_modify** (array of strings): An explicit list of every file path that will be created or modified.
- **test_plan** (array): Objects with `criterion_id` (integer) and `description` (string) describing tests that verify each acceptance criterion. Be specific about what each test asserts.
- **risks** (array of strings): Anything that might go wrong or need human input.

The plan should be detailed enough that a different agent — with no context beyond this plan and the spec — could implement it correctly.

Read the existing codebase before planning to understand current patterns and conventions.

Load the following skills for context:
{skill_references}

{retry_context}"""

IMPLEMENT_TEMPLATE = """\
You are working on the project "{project_name}".
You are on branch: {branch_name}

## Specification
{spec_content}

## Implementation plan
{plan_content}

{structured_context}
## Your job
Implement this task according to the plan. Write tests alongside your implementation.

Rules:
- Only modify files listed in the plan. If you need to modify other files, note it but proceed.
- Write tests that verify each acceptance criterion from the spec.
- Follow the project's coding conventions (load the relevant skills).
- Commit your work with clear, descriptive commit messages.
- Do NOT mark this task as complete — the gate scripts will validate your work.

Load the following skills:
{skill_references}

{retry_context}
{review_feedback}"""

REVIEW_TEMPLATE = """\
You are working on the project "{project_name}".
You are reviewing branch: {branch_name}

## Specification
{spec_content}

## Changes made
{git_diff}

## Your job
Adversarially review this implementation against the spec.

Your review must evaluate:
- **Verdict**: Either "PASS" or "ISSUES"
- **Criteria check**: For each acceptance criterion in the spec, state whether the implementation satisfies it, with evidence.
- **Issues found**: If verdict is ISSUES, list each issue with the file path, severity (critical, major, minor, or nit), and a description of what's wrong and what should be done about it. Be specific — cite file paths and line numbers.
- **Out of scope changes**: Flag any modifications to files not listed in the plan (as file paths).
- **Summary**: A concise overall summary of the review.

Your job is to find problems, not confirm success. Look for: unverified acceptance criteria, missing edge case tests, violations of project conventions or brand guidelines, dead code, and scope creep.

Your verdict must be one of:
- PASS: The implementation fully satisfies every acceptance criterion and you found zero issues.
- ISSUES: You found one or more problems. List every issue, no matter how small. There is no "non-blocking" category — every issue is blocking. Even minor issues like naming, missing edge case tests, or inconsistent patterns must be flagged.

A separate agent will fix the issues you identify. Your job is to find all of them, not to judge which ones matter.

## Issue categorization

Categorize every issue into one of two groups:

1. **Task-related issues** — problems in code this task created or modified. These issues affect your verdict. List them in the "Issues found" section.
2. **Pre-existing issues** — problems in code the task did NOT create or modify. These do NOT affect your verdict. Write them to `_forge/follow-ups/{task_id}.json` as a JSON array of objects, each with `title`, `description`, and an optional `flow` field. The `flow` field can be `"quick"` (default, skip spec/plan, go straight to implement → review) or `"standard"` (full pipeline: spec → plan → implement → review). Use `"standard"` for complex issues that need a spec and plan.

Only task-related issues determine your verdict. Pre-existing issues must not cause an ISSUES verdict.

Load the following skills:
{skill_references}

{retry_context}"""

QUICK_IMPLEMENT_TEMPLATE = """\
You are working on the project "{project_name}".
You are on branch: {branch_name}

## Task
{task_title}

{task_description}

## Your job
Implement this task. Write tests alongside your implementation.

Rules:
- Write tests that verify the task requirements described above.
- Follow the project's coding conventions (load the relevant skills).
- Commit your work with clear, descriptive commit messages.
- Do NOT mark this task as complete — the gate scripts will validate your work.

Load the following skills:
{skill_references}

{retry_context}
{review_feedback}"""

QUICK_REVIEW_TEMPLATE = """\
You are working on the project "{project_name}".
You are reviewing branch: {branch_name}

## Task description
{task_title}

{task_description}

## Changes made
{git_diff}

## Your job
Adversarially review this implementation against the task description above.

Your review must evaluate:
- **Verdict**: Either "PASS" or "ISSUES"
- **Requirements check**: For each requirement in the task description, state whether the implementation satisfies it, with evidence.
- **Issues found**: If verdict is ISSUES, list each issue with the file path, severity (critical, major, minor, or nit), and a description of what's wrong and what should be done about it. Be specific — cite file paths and line numbers.
- **Out of scope changes**: Flag any file modifications not related to the task (as file paths).
- **Summary**: A concise overall summary of the review.

Your job is to find problems, not confirm success. Look for: unverified requirements, missing edge case tests, violations of project conventions, dead code, and scope creep.

Your verdict must be one of:
- PASS: The implementation fully satisfies the task requirements and you found zero issues.
- ISSUES: You found one or more problems. List every issue, no matter how small. Every issue is blocking.

A separate agent will fix the issues you identify. Your job is to find all of them, not to judge which ones matter.

## Issue categorization

Categorize every issue into one of two groups:

1. **Task-related issues** — problems in code this task created or modified. These issues affect your verdict. List them in the "Issues found" section.
2. **Pre-existing issues** — problems in code the task did NOT create or modify. These do NOT affect your verdict. Write them to `_forge/follow-ups/{task_id}.json` as a JSON array of objects, each with `title`, `description`, and an optional `flow` field. The `flow` field can be `"quick"` (default, skip spec/plan, go straight to implement → review) or `"standard"` (full pipeline: spec → plan → implement → review). Use `"standard"` for complex issues that need a spec and plan.

Only task-related issues determine your verdict. Pre-existing issues must not cause an ISSUES verdict.

Load the following skills:
{skill_references}

{retry_context}"""

EPIC_SPEC_TEMPLATE = """\
You are working on the project "{project_name}".

## Epic
{task_title}

{task_description}

## Your job
Decompose this epic into concrete, actionable child tasks. Your output is captured as structured JSON via --json-schema — do not save a file.

Before decomposing:
- Read the project's existing code and documentation to understand the current state.
- Identify logical units of work that can each be completed in a single pipeline pass.
- Keep each child task narrow and self-contained.

Your response must be a JSON object with these fields:
- **tasks** (array, required): An array of task objects, each with:
  - `title` (string, required): A concise, descriptive title for the child task.
  - `description` (string, optional): What the task should accomplish, with enough detail for an agent to implement it without additional context.
  - `flow` (string, optional): Either `"standard"` (default — full spec/plan/implement/review pipeline) or `"quick"` (skip spec/plan, go straight to implement/review). Use `"quick"` only for simple, mechanical fixes.
  - `priority` (integer, required): Higher numbers run first. See priority tier guidance below.
- **rationale** (string, required): Explanation of how the epic was decomposed and why these tasks were chosen.
- **content** (string, required): Full decomposition as markdown prose for human reading.

## Priority tiers

| Tier | Range | Meaning |
|------|-------|---------|
| Critical | 100 | Pipeline is broken |
| Blocking | 80-99 | Fixes blocking an in-progress epic |
| Active | 60-79 | Sub-tasks of current focus area |
| Queued | 40-59 | Next batch of work |
| Background | 20-39 | Polish and refactors |
| Someday | 1-19 | Parked ideas |

The parent epic's priority is **{parent_priority}**. Child tasks should inherit the parent epic's priority tier. Start near the tier ceiling and count down by twos for sequential tasks (e.g., for a parent at priority 65 in the Active tier: 79, 77, 75, 73...). Tasks that must run first get higher numbers.

Load the following skills for context:
{skill_references}

{retry_context}"""

STAGE_TEMPLATES: dict[str, str] = {
    "spec": SPEC_TEMPLATE,
    "plan": PLAN_TEMPLATE,
    "implement": IMPLEMENT_TEMPLATE,
    "review": REVIEW_TEMPLATE,
}

QUICK_STAGE_TEMPLATES: dict[str, str] = {
    "implement": QUICK_IMPLEMENT_TEMPLATE,
    "review": QUICK_REVIEW_TEMPLATE,
}

EPIC_REVIEW_TEMPLATE = """\
You are working on the project "{project_name}".

## Epic
{task_title}

{task_description}

## Decomposition spec
{spec_content}

## Changes on default branch
{git_diff}

## Your job
Review the integrated result of all child tasks against the original epic intent. Your output is captured as structured JSON via --json-schema — do not save a review file.

Your response must be a JSON object with these fields:
- **verdict** (string, required): Either "PASS" or "ISSUES".
- **epic_intent_check** (string, required): Does the current state of the codebase satisfy what the epic set out to accomplish? Evaluate each child task's contribution.
- **integration_check** (string, required): Do the child task results work together coherently? Look for inconsistencies, missing glue code, or gaps between individually-completed pieces.
- **issues** (array, required): If verdict is ISSUES, list each issue as an object with `severity` (string), `description` (string), and optional `file` (string). Be specific — cite file paths and line numbers. Empty array if PASS.
- **summary** (string, required): A concise overall summary of the review.
- **content** (string, required): Full review as markdown prose for human reading.

Your verdict must be one of:
- PASS: The integrated result fully satisfies the original epic intent and all child tasks work together coherently.
- ISSUES: You found gaps or problems. List every issue.

If verdict is ISSUES, also write follow-up tasks to `_forge/follow-ups/{task_id}.json` as a JSON array of objects, each with `title`, `description`, and an optional `flow` field (default `"quick"`).

Load the following skills for context:
{skill_references}

{retry_context}"""

EPIC_STAGE_TEMPLATES: dict[str, str] = {
    "spec": EPIC_SPEC_TEMPLATE,
    "review": EPIC_REVIEW_TEMPLATE,
}

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def format_spec_criteria_list(spec_data: dict) -> str:
    """Format spec acceptance criteria as a numbered list for plan template injection."""
    criteria = spec_data.get("acceptance_criteria", [])
    if not criteria:
        return "(no acceptance criteria found)"
    lines = []
    for c in criteria:
        lines.append(f"{c.get('id', '?')}. {c.get('text', '')}")
    return "\n".join(lines)


def format_structured_implement_context(spec_data: dict, plan_data: dict) -> dict:
    """Format structured spec/plan JSON into organized context for IMPLEMENT_TEMPLATE.

    Returns a dict with keys that can be merged into the artifacts dict.
    """
    parts: list[str] = []

    # Acceptance criteria checklist
    criteria = spec_data.get("acceptance_criteria", [])
    if criteria:
        parts.append("## Acceptance criteria checklist")
        for c in criteria:
            parts.append(f"- [ ] AC {c.get('id', '?')}: {c.get('text', '')}")
        parts.append("")

    # Files to modify
    files = plan_data.get("files_to_modify", [])
    if files:
        parts.append("## Files to modify")
        for f in files:
            parts.append(f"- {f}")
        parts.append("")

    # Approach
    approach = plan_data.get("approach", "")
    if approach:
        parts.append("## Approach")
        parts.append(approach)
        parts.append("")

    # Test plan grouped by criterion
    test_plan = plan_data.get("test_plan", [])
    if test_plan:
        parts.append("## Test plan")
        by_criterion: dict[int, list[str]] = {}
        for t in test_plan:
            cid = t.get("criterion_id", 0)
            by_criterion.setdefault(cid, []).append(t.get("description", ""))
        for cid in sorted(by_criterion):
            parts.append(f"### AC {cid}")
            for desc in by_criterion[cid]:
                parts.append(f"- {desc}")
        parts.append("")

    return {"structured_context": "\n".join(parts)}


def load_artifact(path: str) -> str:
    """Read an artifact file and return its content, or empty string on failure."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, IOError):
        logger.warning("Could not read artifact: %s", path)
        return ""


def build_structured_review_feedback(data: dict) -> str:
    """Convert a structured review dict into human-readable markdown for bounce context.

    Expects keys: verdict, issues, criteria_check, summary.
    """
    parts: list[str] = []
    if data.get("verdict"):
        parts.append(f"**Verdict**: {data['verdict']}")
    if data.get("summary"):
        parts.append(f"\n{data['summary']}")
    if data.get("criteria_check"):
        parts.append("\n### Criteria check")
        for item in data["criteria_check"]:
            if isinstance(item, dict):
                satisfied = item.get("satisfied", item.get("met"))
                status = "PASS" if satisfied else "FAIL"
                criterion = item.get("criterion", "")
                evidence = item.get("evidence", "")
                line = f"- [{status}] {criterion}"
                if evidence:
                    line += f" — {evidence}"
                parts.append(line)
            else:
                parts.append(f"- {item}")
    if data.get("issues"):
        parts.append("\n### Issues")
        for issue in data["issues"]:
            if isinstance(issue, str):
                parts.append(f"- {issue}")
            elif isinstance(issue, dict):
                severity = issue.get("severity", "")
                file_path = issue.get("file", "")
                desc = issue.get("description", str(issue))
                prefix = f"[{severity}] " if severity else ""
                suffix = f" ({file_path})" if file_path else ""
                parts.append(f"- {prefix}{desc}{suffix}")
    return "\n".join(parts)


def build_review_feedback_context(review_content: str) -> str:
    """Format review feedback for an implement retry after a review bounce."""
    if not review_content:
        return ""
    return (
        "## Review feedback\n"
        "A previous review found issues with your implementation. "
        "Fix all issues listed below:\n\n"
        f"{review_content}"
    )


def build_retry_context(
    attempt: int,
    previous_gate_stderr: str,
    previous_gate_structured: str = "",
) -> str:
    """Format the retry section, only when attempt > 1."""
    if attempt <= 1:
        return ""
    parts = [
        f"## Previous attempt failed\n"
        f"This is attempt {attempt}. The previous attempt failed the gate check.\n"
    ]
    if previous_gate_structured:
        parts.append(f"Structured gate result:\n{previous_gate_structured}\n")
    if previous_gate_stderr:
        parts.append(f"Gate failure reason:\n{previous_gate_stderr}\n")
    parts.append(
        "Fix the specific issues identified above. Do not start from scratch unless the problems are fundamental."
    )
    return "\n".join(parts)


def get_git_diff(repo_path: str, branch: str, base_branch: str) -> str:
    """Run ``git diff`` to produce the diff for the review stage."""
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_branch}...{branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("git diff failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_prompt(
    stage: str,
    task: dict,
    project: dict,
    stage_run: dict,
    artifacts: dict,
) -> str:
    """Assemble a stage-specific prompt from templates and task/artifact data.

    Parameters
    ----------
    stage:
        One of "spec", "plan", "implement", "review".
    task:
        Dict with task fields (id, title, description, branch_name, spec_path,
        plan_path, skill_overrides).
    project:
        Dict with project fields (name, skill_refs).
    stage_run:
        Dict with stage_run fields (attempt).
    artifacts:
        Dict with optional keys: ``spec_content``, ``plan_content``,
        ``git_diff``, ``previous_gate_stderr``.
    """
    flow = task.get("flow", "standard")
    if flow == "epic" and stage in EPIC_STAGE_TEMPLATES:
        template = EPIC_STAGE_TEMPLATES[stage]
    elif flow == "quick" and stage in QUICK_STAGE_TEMPLATES:
        template = QUICK_STAGE_TEMPLATES[stage]
    else:
        template = STAGE_TEMPLATES.get(stage)
    if template is None:
        raise ValueError(f"Unknown stage: {stage!r}")

    # Resolve skill references: task overrides take precedence over project defaults
    skill_refs = task.get("skill_overrides") or project.get("skill_refs") or []
    if isinstance(skill_refs, str):
        # Handle JSON-serialised lists stored as strings
        import json

        try:
            skill_refs = json.loads(skill_refs)
        except (json.JSONDecodeError, TypeError):
            skill_refs = []
    skill_references = "\n".join(skill_refs) if skill_refs else "(none)"

    attempt = stage_run.get("attempt", 1)
    previous_gate_stderr = artifacts.get("previous_gate_stderr", "")
    previous_gate_structured = artifacts.get("previous_gate_structured", "")
    retry_context = build_retry_context(
        attempt, previous_gate_stderr, previous_gate_structured
    )

    review_feedback_content = artifacts.get("review_feedback", "")
    review_feedback = build_review_feedback_context(review_feedback_content)

    return template.format(
        project_name=project.get("name", ""),
        task_title=task.get("title", ""),
        task_description=task.get("description", ""),
        task_id=task.get("id", ""),
        branch_name=task.get("branch_name", ""),
        spec_content=artifacts.get("spec_content", ""),
        plan_content=artifacts.get("plan_content", ""),
        git_diff=artifacts.get("git_diff", ""),
        skill_references=skill_references,
        retry_context=retry_context,
        review_feedback=review_feedback,
        spec_criteria_list=artifacts.get("spec_criteria_list", ""),
        structured_context=artifacts.get("structured_context", ""),
        parent_priority=task.get("priority", 0),
    )
