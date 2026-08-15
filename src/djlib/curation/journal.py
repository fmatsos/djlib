"""Durable curation journal export (design §25).

SQLite (`curation_events`) is the immediate transactional source for
accepted human curation actions -- every accepted decision import (Task 13)
inserts its `CurationEvent` row(s) in the *same* DB transaction as the
business-state change it represents. `/data/curation/events.jsonl` is the
durable, independently-replayable record that survives even total SQLite
loss (design §33, `.claude/rules/curation-persistence.md`): it must be
sufficient, together with `/music`, to reconstruct all curated state.

`CurationJournal.export_pending()` is the one function that bridges the two:
it appends every not-yet-exported `CurationEvent` (`sequence >
AppState.last_exported_curation_sequence`) to `events.jsonl`, in sequence
order, then advances the watermark. It is called once, right after a
decision import's own DB transaction commits -- deliberately a *separate*
transaction/step, never nested inside the import's own commit, so that:

* if the process dies after the import commits but before the export runs,
  the gap is safely repairable later (`djlib doctor --repair-journal`, Task
  14) by calling this exact same function again -- nothing about the
  interrupted export corrupts anything, since the JSONL append and the
  watermark advance are the only two things this function does, and it never
  re-emits an already-exported sequence;
* calling it with nothing pending is a safe no-op: it returns 0 without
  touching the file or the watermark.
"""

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from djlib.config import DjlibConfig
from djlib.db.models import AppState, CurationEvent


class CurationJournal:
    def __init__(self, config: DjlibConfig) -> None:
        self._config = config

    @property
    def _events_path(self) -> Path:
        return self._config.data_root / 'curation' / 'events.jsonl'

    def export_pending(self, session: Session) -> int:
        app_state = self._get_or_create_app_state(session)

        pending = list(
            session.execute(
                select(CurationEvent)
                .where(CurationEvent.sequence > app_state.last_exported_curation_sequence)
                .order_by(CurationEvent.sequence)
            ).scalars()
        )
        if not pending:
            return 0

        path = self._events_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            for event in pending:
                handle.write(json.dumps(_event_line(event), sort_keys=True))
                handle.write('\n')

        app_state.last_exported_curation_sequence = pending[-1].sequence
        session.commit()
        return len(pending)

    def _get_or_create_app_state(self, session: Session) -> AppState:
        app_state = session.execute(
            select(AppState).order_by(AppState.id).limit(1)
        ).scalar_one_or_none()
        if app_state is None:
            app_state = AppState(last_exported_curation_sequence=0)
            session.add(app_state)
            session.flush()
        return app_state


def _event_line(event: CurationEvent) -> dict:
    return {
        'sequence': event.sequence,
        'event_uuid': event.event_uuid,
        'event_type': event.event_type,
        'track_public_id': event.track_public_id,
        'file_public_id': event.file_public_id,
        'payload': event.payload_json,
        'created_at': event.created_at.isoformat() if event.created_at else None,
    }
