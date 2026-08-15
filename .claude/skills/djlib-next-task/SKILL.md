---
name: djlib-next-task
description: Drive the next unimplemented task of the djlib Milestone 1 implementation plan (docs/superpowers/plans/2026-08-15-djlib-milestone-1-implementation.md) through a TDD subagent, review the result independently, then commit and push. Use this whenever the user asks to continue, resume, or advance djlib's implementation plan task-by-task — phrases like "next task", "continue the plan", "implement the next step", "keep going with djlib", or "run task N" — instead of re-deriving the workflow from scratch each time. Replaces the plan's own reference to a "superpowers:subagent-driven-development" skill, which isn't available in this environment.
---

# djlib: drive the next implementation-plan task

The plan at `docs/superpowers/plans/2026-08-15-djlib-milestone-1-implementation.md` already
specifies every task as literal `- [ ] **Step k: ...**` checkboxes with exact commands and code
snippets, each task ending in a `git commit` step. This skill exists so that "do the next task"
doesn't require re-reading the whole plan's conventions from memory each time — it just tells you
where to look and what order to do things in. The actual engineering judgment (does this diff
correctly implement the step? do the tests really prove the behavior?) is still yours to make, not
something to delegate away.

## 1. Find the next task

Read `docs/superpowers/plans/2026-08-15-djlib-milestone-1-implementation.md` and scan its
`### Task N: ...` sections in order. The first one that still has any unchecked `- [ ] **Step
k:...**` box is the next task. If every task through Task 16 is fully checked, stop and say so —
what remains is the "Production acceptance on the real DJ archive" section, which needs the real
`/music` library and is out of scope for this skill.

If the user named a specific task number or asked to redo one, use that instead of the scan.

## 2. Read before delegating

Read that task's full spec in the plan (Files / Interfaces / every Step, including the literal code
snippets and commit message) and the parts of
`docs/superpowers/specs/2026-08-15-djlib-milestone-1-catalog-dedup-design.md` it depends on — the
plan tells you which design sections matter per task. Skim `.claude/rules/*.md` if you haven't
recently; several already encode invariants (read-only source, conservative track identity, human
override precedence, transactional persistence) that a delegated subagent must not violate, so you
don't need to restate them by hand in every prompt — just point the subagent at the plan text, the
task boundaries, and let the rules and hooks catch violations.

## 3. Delegate implementation to a subagent

Spawn one subagent (foreground — you need its result before reviewing) per task. The prompt must be
self-contained since the subagent starts with no memory of this conversation. Include:

- Which task and step range to implement, quoting the plan's own Files/Interfaces/Steps for that
  task (don't paraphrase the code snippets — the subagent should follow them, deviating only where
  it hits a real technical obstacle).
- Strict TDD in the plan's own order: write the failing test(s) first, run them and confirm they
  fail, then implement, then confirm they pass. Skipping straight to implementation is not
  acceptable even if it "obviously" would work.
- A hard boundary: implement only this task. Don't create files, modules, or config keys that belong
  to a later task in the plan (check the "Planned repository structure" section if unsure which task
  owns what).
- No commit, no push, no `git add` even — leave the diff as uncommitted working-tree changes. You
  (the orchestrating session) do the commit after reviewing, so a subagent-authored commit would
  bypass that review.
- Ask it to report every deviation from the plan's literal snippets and why (dependency version
  mismatch, a stricter type checker, an API that behaves differently than the snippet assumes,
  etc.) — these deviations are exactly what your review in the next step needs to check.

## 4. Review independently — don't take the subagent's word for it

This is the review gate the milestone plan calls for between tasks. A subagent's self-report
describes what it intended to do, not necessarily what it did.

- Read the actual diff (`git status`, `git diff`, and the changed files themselves), not just the
  subagent's summary.
- Re-run the test commands from the plan's own verification steps yourself, from a clean shell.
  Don't trust a pasted "3 passed" from the subagent's report.
- Check the task's stated invariants actually hold (e.g. for a duplicates-pipeline task: does a
  version-conflict fixture really end up `REVIEW_REQUIRED`, not silently merged?).
- If something is wrong, missing, or the subagent quietly reduced scope: send it a follow-up (reuse
  the same agent handle) or fix it directly yourself if it's small — don't commit broken or
  incomplete work just to keep the checklist moving.

## 5. Commit, check off, push

Once the diff is verified:

1. Edit the plan file to check off (`- [x]`) every step box for the task you just completed.
2. Stage the task's files plus the plan-file checkbox edit.
3. Commit using the exact message given in the task's own final commit step (or, if the plan gives
   no verbatim message for that step, one in the same terse `type: summary` style used throughout
   the plan's other commit steps).
4. Push to the current branch (`git push -u origin <branch>`, matching this repo's existing
   convention of one branch carrying the whole milestone).

## 6. When to stop instead of forcing it

Don't commit a task that fails its own verification steps, that you can't independently confirm, or
where the subagent had to make a judgment call significant enough that a human should weigh in (e.g.
changing a documented threshold, restructuring an interface another task depends on). Surface the
specific blocker instead of pushing through it — a skipped or wrong task compounds across the
remaining 15, since later tasks build on earlier ones' interfaces.
