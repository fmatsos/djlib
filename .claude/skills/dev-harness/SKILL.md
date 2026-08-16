---
name: dev-harness
description: Use when the user asks to implement, build, fix, or add something in this codebase and wants the implement-review-fix loop handled by dedicated agents instead of driven turn-by-turn in the main conversation — phrases like "implement X", "build this feature", "have an agent do this and review it", "use the dev harness", "run this through the agents". Explains the four dev agents (impl-scoped, impl-quick, impl-reviewer, dev-orchestrator), how to pick between them, how to launch them correctly with the Agent tool, and how to stay in control of the run instead of just firing and forgetting.
---

# Dev harness: implement -> review -> fix, via dedicated agents

This project defines four agents under `.claude/agents/` specifically for implementation work, each
tuned to a different point on the cost/judgment curve. This skill is the map for using them from the
main conversation — which one to call, how to call it, and what "managing" the run actually means.

## The four agents

| Agent | Model / effort | Role |
|---|---|---|
| `impl-quick` | haiku | Small, fully-specified, mechanical changes — nothing to decide. |
| `impl-scoped` | sonnet, effort low | Framed tasks that still need a small judgment call before coding. |
| `impl-reviewer` | sonnet, effort medium | Reviews a diff for quality/simplicity/compliance; classifies fixes and delegates them. |
| `dev-orchestrator` | sonnet, effort medium | Runs the whole scope -> implement -> review -> fix loop for you. |

They're deliberately layered by cost: haiku for what needs no thinking, sonnet-low for what needs a
little, sonnet-medium for judging others' work and for coordinating a whole run. Don't reach for a
more expensive agent than the task needs, and don't push a task needing real judgment onto `impl-quick`
just because it looks small on the surface.

## Deciding what to call

Use this decision order, not a fixed rule — the actual signal is how much is already decided versus
how much judgment the task still needs, and how many steps it spans:

1. **Trivial, one-shot, you already know exactly what to change** (a typo, a config value, mirroring
   one existing line) — it's often faster to just make the edit yourself than to spin up any agent.
   Reserve the harness for changes worth isolating and reviewing.
2. **One well-scoped change, worth a review pass** — call `impl-quick` or `impl-scoped` directly
   (see the classification rule below), then call `impl-reviewer` yourself on the result. This gives
   you a review without the overhead of the full orchestrator loop.
3. **Multi-step, spans a plan/spec, or you want the whole loop (including fix iterations) handled
   without babysitting each round** — call `dev-orchestrator` and let it scope, dispatch, review, and
   iterate. This is the default for anything beyond a single small change.

**impl-quick vs impl-scoped**: ask "is there anything left to decide here?" If the task fully
specifies the change (what file, what edit, what it looks like when done) — impl-quick. If there's a
real choice (which approach, how to structure it, an edge case the spec doesn't cover) — impl-scoped.
When genuinely unsure, prefer impl-scoped; it costs more but won't guess where it shouldn't.

## Launching them

Use the `Agent` tool with `subagent_type` set to the agent's name (`impl-quick`, `impl-scoped`,
`impl-reviewer`, `dev-orchestrator`). Each of these agents starts with zero memory of this
conversation — the prompt must be fully self-contained:

- The exact scope: files, goal, acceptance criteria, any pattern to mirror.
- An explicit boundary on what's out of scope (files/features it should not touch).
- For `impl-reviewer`: the original task instructions, the changed files or diff, and the
  implementer's report — it needs all three to review against the actual ask, not just the code.
- For `dev-orchestrator`: the full request plus pointers to any plan/spec/rules it should read itself
  before scoping sub-tasks.

Run them in the background unless your very next action depends on the result. `dev-orchestrator` in
particular is meant to run unattended through its fix loop — don't poll it; you'll get a notification
when it finishes.

## Staying in control, not just firing and forgetting

"Managing" these agents means:

- **Verify, don't just relay.** An implementer's or even the reviewer's report describes what it
  intended, not necessarily what happened. Before telling the user something is done, look at the
  actual diff yourself (`git status` / `git diff`), at least at the level of "does this match what was
  asked."
- **Relay escalations, don't resolve them yourself.** If `impl-reviewer` or `dev-orchestrator` comes
  back with `ESCALATE` — a real requirement conflict, a decision only a human should make — surface it
  to the user with `AskUserQuestion` rather than picking an interpretation and re-dispatching.
- **Respect the git boundary.** None of these four agents commit or push on their own initiative
  (their instructions say so explicitly). Committing and pushing stays your call and the user's, per
  this repo's normal git-safety rules — don't ask an agent to do it instead as a shortcut.
- **Don't let scope drift.** If a run comes back having touched more than what was asked (even if the
  extra work looks like a reasonable improvement), treat that as something to flag, not something to
  wave through because `dev-orchestrator` didn't catch it.
