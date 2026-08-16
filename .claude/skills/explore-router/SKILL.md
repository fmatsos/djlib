---
name: explore-router
description: Use when deciding how to search the codebase for a "find X", "where is Y defined", "which files reference Z", or "how does W work" question — whether to just search directly, use the built-in Explore agent, or delegate to this project's explore-precise / explore-broad agents. Chooses the right option by how well-defined the target is and how much ground needs covering, so a one-line lookup doesn't spin up a broad search and a genuinely open question doesn't get a search too narrow to find most of the answer.
---

# Exploration routing: precise vs broad vs "just search it yourself"

This project defines two exploration agents under `.claude/agents/` — `explore-precise` (haiku) and
`explore-broad` (sonnet, effort low) — tuned to opposite ends of "how well-defined is the search
target." This skill is the decision point for which one to use, and for when neither is warranted
because the search is trivial enough to do inline.

## Decision order

1. **A single obvious lookup you can resolve in 1-2 direct tool calls** (you already know the
   filename pattern, or the exact symbol to grep) — just use `Glob`/`Grep`/`Read` yourself. Don't
   spend an agent call on something that direct. This matches the general guidance to only reach for
   the built-in `Explore` agent when a search would take more than about 3 queries.

2. **Precise target, but worth isolating from the main context or likely to take a few tries** — a
   specific symbol, string, or "where is X defined" / "which files call Y" question where you know
   almost exactly what you're looking for, just not precisely where — use **`explore-precise`**.
   It's fast and cheap (haiku); use it when the question names the target closely enough that a
   handful of grep/glob attempts will resolve it.

3. **Vague, multi-part, or requires synthesis** — "how does the duplicate pipeline work end to end",
   "map out everywhere concept X is handled", "what would need to change to support Y", or any
   scoping pass before an implementation task where the right search angle isn't obvious yet — use
   **`explore-broad`**. It searches from multiple angles and synthesizes a structured answer instead
   of pointing at one location; a narrow search here would reliably miss most of the answer.

4. **Genuinely unsure which bucket it's in** — default to `explore-broad`. A broad search that turns
   out to have a simple answer just costs a bit more; a narrow search that turns out to be the wrong
   tool means re-running the whole thing anyway.

## Relationship to the built-in `Explore` agent

The environment's generic `Explore` agent is a reasonable default when this project's own agents
aren't relevant (e.g. exploring something outside this codebase, or when tool access beyond
Glob/Grep/Read is needed for the search itself). Within this repo, prefer `explore-precise` /
`explore-broad` over the generic `Explore` agent for code-location questions — they're cost-tuned
(haiku vs sonnet-low) to the two shapes of query this codebase's exploration actually needs, rather
than one model tier for everything.

## Launching

Use the `Agent` tool with `subagent_type: "explore-precise"` or `subagent_type: "explore-broad"`.
State the search breadth implicitly by which agent you pick — you don't need to also pass a
"quick/thorough" hint the way the generic `Explore` agent expects. Write the question as a complete,
self-contained prompt (the agent has no memory of this conversation); for `explore-broad`, spell out
the sub-questions you actually want answered so it doesn't wander past what you needed.
