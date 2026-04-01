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
Write a specification for this task. Save it to: _forge/specs/{task_id}.md

The spec must include these sections:
- **Overview**: What this task accomplishes in 2-3 sentences.
- **Acceptance criteria**: A numbered list of binary (pass/fail) criteria. Each criterion must be objectively verifiable — no subjective language like "looks good" or "feels right."
- **Out of scope**: What this task explicitly does NOT include.
- **Dependencies**: Any existing code, APIs, or features this task depends on.

Read the project's existing documentation before writing the spec to ensure alignment with established patterns and decisions.

Load the following skills for context:
{skill_references}

{retry_context}"""

PLAN_TEMPLATE = """\
You are working on the project "{project_name}".

## Specification
{spec_content}

## Your job
Write an implementation plan for this spec. Save it to: _forge/plans/{task_id}.md

The plan must include:
- **Approach**: How you will implement this, in 2-3 paragraphs.
- **Acceptance criteria mapping**: For each acceptance criterion in the spec, describe how the implementation will satisfy it. Use the exact criterion text from the spec.
- **Files to create or modify**: An explicit list of every file path that will be touched.
- **Test plan**: Descriptions of tests that verify each acceptance criterion from the spec. Be specific about what each test asserts.
- **Risks**: Anything that might go wrong or need human input.

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
Adversarially review this implementation against the spec. Save your review to: _forge/reviews/{task_id}.md

Your review must include:
- **Verdict**: Either "PASS" or "ISSUES"
- **Criteria check**: For each acceptance criterion in the spec, state whether the implementation satisfies it (yes/no with evidence).
- **Issues found**: If verdict is ISSUES, list each issue with: what's wrong, where it is, and what should be done about it. Be specific — cite file paths and line numbers.
- **Out of scope changes**: Flag any modifications to files not listed in the plan.

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
Adversarially review this implementation against the task description above. Save your review to: _forge/reviews/{task_id}.md

Your review must include:
- **Verdict**: Either "PASS" or "ISSUES"
- **Requirements check**: For each requirement in the task description, state whether the implementation satisfies it (yes/no with evidence).
- **Issues found**: If verdict is ISSUES, list each issue with: what's wrong, where it is, and what should be done about it. Be specific — cite file paths and line numbers.

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

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


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


def build_retry_context(attempt: int, previous_gate_stderr: str) -> str:
    """Format the retry section, only when attempt > 1."""
    if attempt <= 1:
        return ""
    return (
        f"## Previous attempt failed\n"
        f"This is attempt {attempt}. The previous attempt failed the gate check.\n"
        f"Gate failure reason:\n"
        f"{previous_gate_stderr}\n\n"
        f"Fix the specific issues identified above. Do not start from scratch unless the problems are fundamental."
    )


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
    if flow == "quick" and stage in QUICK_STAGE_TEMPLATES:
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
    retry_context = build_retry_context(attempt, previous_gate_stderr)

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
    )
