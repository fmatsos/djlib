import json

from sqlalchemy import Engine, select

from djlib.config import DjlibConfig
from djlib.curation.journal import CurationJournal
from djlib.db.models import AppState, CurationEvent
from djlib.db.session import session_factory


def _add_event(session, sequence: int) -> None:
    session.add(
        CurationEvent(
            sequence=sequence,
            event_uuid=f'00000000-0000-0000-0000-{sequence:012d}',
            event_type='DUPLICATE_GROUP_CONFIRMED',
            track_public_id=None,
            file_public_id=None,
            payload_json={'group_id': f'dup_{sequence}'},
        )
    )
    session.commit()


def _events_path(config: DjlibConfig):
    return config.data_root / 'curation' / 'events.jsonl'


def test_export_pending_is_a_safe_noop_with_nothing_pending(
    config: DjlibConfig, engine: Engine
) -> None:
    session_maker = session_factory(engine)
    journal = CurationJournal(config)

    with session_maker() as session:
        count = journal.export_pending(session)

    assert count == 0
    assert not _events_path(config).exists()


def test_export_pending_catches_up_incrementally_without_duplication(
    config: DjlibConfig, engine: Engine
) -> None:
    """Two accepted decisions land in SQLite at different times (design §25).
    Exporting after the first advances the watermark to sequence 1; a second,
    independent `export_pending()` call after the second event lands must
    export sequence 2 exactly once -- not re-emit sequence 1, not skip
    sequence 2.
    """
    session_maker = session_factory(engine)
    journal = CurationJournal(config)

    with session_maker() as session:
        _add_event(session, 1)
        exported_first = journal.export_pending(session)
    assert exported_first == 1

    lines = _events_path(config).read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])['sequence'] == 1

    with session_maker() as session:
        app_state = session.execute(select(AppState)).scalar_one()
        assert app_state.last_exported_curation_sequence == 1

    with session_maker() as session:
        _add_event(session, 2)
        exported_second = journal.export_pending(session)
    assert exported_second == 1

    lines = _events_path(config).read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])['sequence'] == 1
    assert json.loads(lines[1])['sequence'] == 2

    with session_maker() as session:
        app_state = session.execute(select(AppState)).scalar_one()
        assert app_state.last_exported_curation_sequence == 2

    # Idempotent: nothing pending now -- no rewrite, no duplication.
    with session_maker() as session:
        assert journal.export_pending(session) == 0
    assert _events_path(config).read_text(encoding='utf-8').splitlines() == lines


def test_export_pending_repairs_a_gap_left_by_a_simulated_crash(
    config: DjlibConfig, engine: Engine
) -> None:
    """The literal design §25 repair scenario: sequence 1 and 2 are both
    already committed to SQLite (as if two decisions were accepted) before
    the process crashed partway through exporting them -- sequence 1 made it
    into `events.jsonl` and `last_exported_curation_sequence` was advanced to
    1, but sequence 2 did not. A later `export_pending()` call (this is
    exactly the function Task 14's `doctor --repair-journal` will call) must
    append sequence 2 exactly once and catch the watermark up to 2.
    """
    session_maker = session_factory(engine)
    journal = CurationJournal(config)

    with session_maker() as session:
        _add_event(session, 1)
        _add_event(session, 2)

    events_path = _events_path(config)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(json.dumps({'sequence': 1, 'simulated_pre_crash_export': True}) + '\n', encoding='utf-8')
    with session_maker() as session:
        session.add(AppState(last_exported_curation_sequence=1))
        session.commit()

    with session_maker() as session:
        exported = journal.export_pending(session)

    assert exported == 1
    lines = events_path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])['sequence'] == 2

    with session_maker() as session:
        app_state = session.execute(select(AppState)).scalar_one()
        assert app_state.last_exported_curation_sequence == 2

    # Calling again is a safe no-op -- no duplication.
    with session_maker() as session:
        assert journal.export_pending(session) == 0
    assert events_path.read_text(encoding='utf-8').splitlines() == lines
