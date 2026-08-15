---
paths:
  - "src/djlib/**/*.py"
  - "tests/**/*.py"
description: Core Python conventions and TDD discipline for djlib
---

# Python conventions (djlib)

## Stack
- Python 3.12+, src-layout (`src/djlib/`), packaged via `setuptools` (see `pyproject.toml`).
- Typer for the CLI, SQLAlchemy 2.x + Alembic for persistence, pytest for tests.

## Style (observed in `src/djlib/config.py`, `src/djlib/ids.py`)
- Use `@dataclass(frozen=True)` for value objects (e.g. `DjlibConfig`); prefer immutable data over mutable state classes.
- Use classmethod constructors (`.defaults()`, `.load()`) instead of overloaded `__init__`.
- Type-hint everything, including return types (`-> None`, `-> 'DjlibConfig'`); use PEP 604 unions (`Path | None`) not `Optional[...]`.
- Single quotes for string literals; f-strings for interpolation.
- No comments explaining *what* code does — identifiers and structure should be self-explanatory. Only add a comment for a genuinely non-obvious *why* (a subtle invariant, a workaround), and keep it to one line.

## TDD is mandatory (see implementation plan, "Global Constraints")
- Every new behavior starts with a failing test committed or verified failing before the implementation is written. Do not write production code first and backfill tests.
- Tests are plain functions (`def test_x() -> None:`), not test classes, matching `tests/unit/test_config.py` / `test_ids.py`. Use bare `assert`, not a custom assertion framework.
- Commit after every independently testable unit of work (one task/step = one commit), not as one giant end-of-task commit.

## Scope discipline
- This is Milestone 1 (catalogue & deduplication) only. Do not add code for explicitly deferred features: Traktor/Serato history import, DJ-history scoring/tiers, transition graphs, narrative tags, Engine DJ export, internet metadata enrichment, audio previews, or a persistent web app. If a later task in the implementation plan owns a concept, do not anticipate it in an earlier task.
- No speculative abstractions: don't introduce a base class, plugin system, or config knob for a use case the current task doesn't need.
