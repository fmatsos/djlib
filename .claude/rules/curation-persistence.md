---
paths:
  - "src/djlib/curation/**/*.py"
  - "src/djlib/db/**/*.py"
  - "alembic/versions/**/*.py"
description: Human precedence, durable curation journal, and transactional persistence
---

# Curation precedence and persistence (djlib)

## The invariant
SQLite is a rebuildable projection. `/music` plus `/data/curation/events.jsonl` are the durable
source of truth for human curation and must be sufficient to reconstruct all curated state
(design §25, §33).

## Rules
- Human decisions always win over automatic ones. A track override, `MERGE`/`SPLIT`, or a
  `CONFIRM`/`CHANGE_PREFERRED`/`REJECT`/`DEFER` import must never be silently reversed or
  overwritten by a later `scan` or `duplicates run` — a conflicting rescan raises a notice, it never
  clobbers curation.
- `track_overrides` and identity events are append-only: supersede a prior active row
  (`superseded_at`) rather than deleting or updating it in place. Never destroy override/merge/split
  history.
- Public IDs (`public_id` on tracks, groups, runs, etc.) are immutable once assigned and are never
  reused, including after a `MERGE`/`SPLIT`/rebuild.
- Every accepted curation action commits its `CurationEvent` row inside the same DB transaction as
  the state change it represents; only after that COMMIT do you append it to
  `/data/curation/events.jsonl` and advance `last_exported_curation_sequence`. Never append to the
  JSONL before the DB transaction that produced the event has committed.
- Decision import (Task 13) is atomic: validate schema version, report ID, catalog/group revision,
  IDs, current group state, and file signatures *before* any write; on staleness, reject the whole
  file — there is no partial apply and no `--force` in Milestone 1.
- Replay (Task 15) must fail loudly on an event it cannot safely map to a file identity — never
  guess among ambiguous candidates.
- All business writes go through explicit transactions. SQLite connections must enable
  `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, and a non-zero `busy_timeout` (see
  `src/djlib/db/engine.py`'s `create_engine_for_config`).
- Don't introduce a new reference table (artist/album/genre) or other schema beyond what the current
  task's design section specifies — Milestone 1 explicitly avoids that.
