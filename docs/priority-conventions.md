# Forge Priority Conventions

## Overview

Task priority is a positive integer. The engine activates the highest-priority
backlog task first. These conventions standardize how priorities are assigned so
that tasks execute in a predictable order without constant manual adjustment.

The scale is **guidance, not enforcement** — there is no code that rejects a
priority outside its expected tier. You can always override when the situation
calls for it.

---

## Tier Scale

| Tier       | Range | When to use                                              |
|------------|-------|----------------------------------------------------------|
| Critical   | 100   | Pipeline is broken, nothing else should run               |
| Blocking   | 80–99 | Fixes or follow-ups that block an in-progress epic        |
| Active     | 60–79 | Sub-tasks of the current focus area                       |
| Queued     | 40–59 | Next batch of work, after the current epic wraps          |
| Background | 20–39 | Polish, refactors, improvements — real but not urgent     |
| Someday    | 1–19  | Parked ideas, low-value cleanup                           |

Gaps of 20 between tier floors leave room to slot tasks in without renumbering.

---

## Ordering Within a Tier

When creating a batch of sub-tasks that should run in sequence, **count down by
twos from near the tier ceiling**.

Example — 6 sub-tasks in an active epic:

    71, 69, 67, 65, 63, 61

- They execute in order (highest first).
- Every adjacent pair has a gap for insertions.
- Room remains at 72–79 if something needs to jump ahead.

---

## Default Rules

### Epic sub-tasks (created in batch)

Start near the tier ceiling, count down by twos. The first task to run gets
the highest number.

### Review follow-ups

**Parent task priority minus 1.** A follow-up from the task at 67 gets 66,
which lands in the gap and runs next — before the remaining sub-tasks at 65,
63, etc. — without renumbering anything.

This replaces the current behavior of defaulting follow-ups to priority 0.

### Standalone tasks (created in chat, not part of an epic)

Default to **50** — middle of the Queued tier. Safe "I haven't decided where
this fits yet" default that won't jump ahead of active work but won't get
buried.

### Hotfixes

**80+**, slotted by severity within the Blocking tier.

---

## Multi-Project Considerations

Each project maintains its own independent priority space. A Forge task at
priority 65 and an Olivia task at priority 65 are unrelated — they are
compared only within their own project's backlog.

This requires per-project task selection in the engine (see the per-project
concurrency task). With global priority selection, cross-project priority
collisions would need manual management.

---

## When to Renumber

The gap-of-two pattern handles most insertions. Renumbering is only needed if
you need to insert **two or more tasks** between the same adjacent pair. Even
then, you only move one or two tasks, not the whole batch. Use the
`reprioritize_task` MCP tool or chat interface.
