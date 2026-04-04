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

{retry_context}

## Output protocol
When you are finished, emit a fenced JSON block with the artifact paths you produced. This block MUST appear at the very end of your output, using the exact fence marker shown below. Do not nest code fences inside it.

```forge-output
{{"spec_path": "_forge/specs/{task_id}.md"}}
```"""

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

{retry_context}

## Output protocol
When you are finished, emit a fenced JSON block with the artifact paths you produced. This block MUST appear at the very end of your output, using the exact fence marker shown below. Do not nest code fences inside it.

```forge-output
{{"plan_path": "_forge/plans/{task_id}.md"}}
```"""

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
{review_feedback}

## Output protocol
When you are finished, emit a fenced JSON block summarizing the work you did. This block MUST appear at the very end of your output, using the exact fence marker shown below. Do not nest code fences inside it.

```forge-output
{{"files_modified": ["path/to/file1.py", "path/to/file2.py"]}}
```"""

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

{retry_context}

## Output protocol
When you are finished, emit a fenced JSON block with your review results. This block MUST appear at the very end of your output, using the exact fence marker shown below. Do not nest code fences inside it. The gate script remains the authority on pass/fail — this block is for engine observability.

```forge-output
{{"verdict": "PASS", "review_path": "_forge/reviews/{task_id}.md", "issues": [], "follow_ups": []}}
```

- `verdict`: Use `PASS` if all criteria met, `ISSUES` otherwise.
- `issues`: Array of issue description strings. Empty array if verdict is PASS.
- `follow_ups`: Optional array of follow-up task objects for pre-existing issues. Omit if none."""

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
{review_feedback}

## Output protocol
When you are finished, emit a fenced JSON block summarizing the work you did. This block MUST appear at the very end of your output, using the exact fence marker shown below. Do not nest code fences inside it.

```forge-output
{{"files_modified": ["path/to/file1.py", "path/to/file2.py"]}}
```"""

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

{retry_context}

## Output protocol
When you are finished, emit a fenced JSON block with your review results. This block MUST appear at the very end of your output, using the exact fence marker shown below. Do not nest code fences inside it. The gate script remains the authority on pass/fail — this block is for engine observability.

```forge-output
{{"verdict": "PASS", "review_path": "_forge/reviews/{task_id}.md", "issues": [], "follow_ups": []}}
```

- `verdict`: Use `PASS` if all criteria met, `ISSUES` otherwise.
- `issues`: Array of issue description strings. Empty array if verdict is PASS.
- `follow_ups`: Optional array of follow-up task objects for pre-existing issues. Omit if none."""

EPIC_SPEC_TEMPLATE = """\
You are working on the project "{project_name}".

## Epic
{task_title}

{task_description}

## Your job
Decompose this epic into concrete, actionable child tasks. Save the result to: _forge/epic-decompositions/{task_id}.json

Before decomposing:
- Read the project's existing code and documentation to understand the current state.
- Identify logical units of work that can each be completed in a single pipeline pass.
- Keep each child task narrow and self-contained.

The output file must contain a JSON array of task objects. Each object has:
- `title` (string, required): A concise, descriptive title for the child task.
- `description` (string, optional): What the task should accomplish, with enough detail for an agent to implement it without additional context.
- `flow` (string, optional): Either `"standard"` (default — full spec/plan/implement/review pipeline) or `"quick"` (skip spec/plan, go straight to implement/review). Use `"quick"` only for simple, mechanical fixes.
- `priority` (integer, optional): Higher numbers run first. Default is 0.

Example output:
```json
[
  {{"title": "Add user authentication endpoint", "description": "Create POST /auth/login ...", "flow": "standard", "priority": 2}},
  {{"title": "Fix typo in README", "description": "Change 'recieve' to 'receive'", "flow": "quick", "priority": 0}}
]
```

Load the following skills for context:
{skill_references}

{retry_context}

## Output protocol
When you are finished, emit a fenced JSON block with the artifact paths you produced. This block MUST appear at the very end of your output, using the exact fence marker shown below. Do not nest code fences inside it.

```forge-output
{{"spec_path": "_forge/epic-decompositions/{task_id}.json"}}
```"""

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
Review the integrated result of all child tasks against the original epic intent. Save your review to: _forge/reviews/{task_id}.md

Your review must include:
- **Verdict**: Either "PASS" or "ISSUES"
- **Epic intent check**: Does the current state of the codebase satisfy what the epic set out to accomplish? Evaluate each child task's contribution.
- **Integration check**: Do the child task results work together coherently? Look for inconsistencies, missing glue code, or gaps between individually-completed pieces.
- **Issues found**: If verdict is ISSUES, list each issue with: what's wrong, where it is, and what should be done about it. Be specific — cite file paths and line numbers.

Your verdict must be one of:
- PASS: The integrated result fully satisfies the original epic intent and all child tasks work together coherently.
- ISSUES: You found gaps or problems. List every issue.

If verdict is ISSUES, also write follow-up tasks to `_forge/follow-ups/{task_id}.json` as a JSON array of objects, each with `title`, `description`, and an optional `flow` field (default `"quick"`).

Load the following skills for context:
{skill_references}

{retry_context}

## Output protocol
When you are finished, emit a fenced JSON block with your review results. This block MUST appear at the very end of your output, using the exact fence marker shown below. Do not nest code fences inside it. The gate script remains the authority on pass/fail — this block is for engine observability.

```forge-output
{{"verdict": "PASS", "review_path": "_forge/reviews/{task_id}.md", "issues": [], "follow_ups": []}}
```

- `verdict`: Use `PASS` if all criteria met, `ISSUES` otherwise.
- `issues`: Array of issue description strings. Empty array if verdict is PASS.
- `follow_ups`: Optional array of follow-up task objects for pre-existing issues. Omit if none."""

EPIC_STAGE_TEMPLATES: dict[str, str] = {
    "spec": EPIC_SPEC_TEMPLATE,
    "review": EPIC_REVIEW_TEMPLATE,
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
