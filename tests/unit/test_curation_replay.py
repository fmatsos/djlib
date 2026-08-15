"""Task 15, Step 1: replay must restore stable track public IDs, preferred
files, overrides and merge/split outcomes after a fresh rescan -- using only
the events' stable `relative_path` references, never a carried-over
`public_id` coincidence (a fresh scan of a *second*, independent database
assigns brand-new random public IDs to every file/track; see
`scan/service.py` and `.claude/rules/curation-persistence.md`).

Self-contained (no shared `tests/integration/conftest.py` fixtures -- this
file lives under `tests/unit/` per the implementation plan's own file list,
even though it drives real `ScanService`/`CatalogService` calls against a
real, if minimal, on-disk audio fixture library).
"""

import json
import wave
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.catalog.queries import active_track_for_file
from djlib.catalog.service import CatalogService
from djlib.config import DjlibConfig
from djlib.curation.journal import CurationJournal
from djlib.curation.replay import CurationReplay, ReplayError
from djlib.db.base import Base
from djlib.db.engine import create_engine_for_config
from djlib.db.enums import TrackStatus
from djlib.db.models import FileRecord, Track, TrackFile
from djlib.db.session import session_factory
from djlib.scan.service import ScanService


def _config(tmp_path: Path, data_dir_name: str) -> DjlibConfig:
    return DjlibConfig(music_root=tmp_path / 'music', data_root=tmp_path / data_dir_name)


def _engine(config: DjlibConfig) -> Engine:
    engine = create_engine_for_config(config)
    Base.metadata.create_all(engine)
    return engine


def _write_valid_wav(path: Path, num_frames: int) -> None:
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b'\x00\x00' * num_frames)


def _track_owning(session_maker: sessionmaker[Session], relative_path: str) -> Track:
    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one()
        track = active_track_for_file(session, file.id)
        assert track is not None
        return track


def _active_file_ids(session: Session, track_id: int) -> set[int]:
    return {
        link.file_id
        for link in session.execute(
            select(TrackFile).where(TrackFile.track_id == track_id, TrackFile.is_active.is_(True))
        ).scalars()
    }


def _events_path(config: DjlibConfig) -> Path:
    return config.data_root / 'curation' / 'events.jsonl'


def test_replay_restores_override_merge_and_split_after_a_fresh_rescan(tmp_path: Path) -> None:
    config = _config(tmp_path, 'data')
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'override.wav', num_frames=100)
    _write_valid_wav(config.music_root / 'a.wav', num_frames=200)
    _write_valid_wav(config.music_root / 'b.wav', num_frames=300)

    engine = _engine(config)
    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    override_track_public_id = _track_owning(session_maker, 'override.wav').public_id
    a_track_public_id = _track_owning(session_maker, 'a.wav').public_id
    b_track_public_id = _track_owning(session_maker, 'b.wav').public_id
    with session_maker() as session:
        b_file_public_id = session.execute(
            select(FileRecord.public_id).where(FileRecord.relative_path == 'b.wav')
        ).scalar_one()

    # -- apply a human override, a merge, and a split (real CatalogService) --

    with session_maker() as session:
        CatalogService(session).set_override(
            override_track_public_id, 'artist', 'Curated Artist Name'
        )
        session.commit()

    with session_maker() as session:
        CatalogService(session).merge_tracks(b_track_public_id, a_track_public_id)
        session.commit()

    with session_maker() as session:
        new_track = CatalogService(session).split_track(
            a_track_public_id, file_public_ids=[b_file_public_id]
        )
        new_track_public_id = new_track.public_id
        session.commit()

    with session_maker() as session:
        exported = CurationJournal(config).export_pending(session)
    assert exported == 3
    events_path = _events_path(config)
    assert events_path.exists()

    # -- a second, independent database; fresh full scan of the SAME files --

    rebuilt_config = _config(tmp_path, 'data-rebuilt')
    rebuilt_engine = _engine(rebuilt_config)
    rebuilt_session_maker = session_factory(rebuilt_engine)

    scan_summary = ScanService(rebuilt_config, rebuilt_session_maker).scan(full=True)
    assert scan_summary.files_new == 3

    # Fresh scan assigns brand-new random public IDs -- prove they really
    # differ before replay restores the historical ones.
    with rebuilt_session_maker() as session:
        fresh_a_public_id = session.execute(
            select(Track.public_id).where(
                Track.id == active_track_for_file(
                    session,
                    session.execute(
                        select(FileRecord.id).where(FileRecord.relative_path == 'a.wav')
                    ).scalar_one(),
                ).id
            )
        ).scalar_one()
    assert fresh_a_public_id != a_track_public_id

    with rebuilt_session_maker() as session:
        replay_summary = CurationReplay(rebuilt_config, session).replay(events_path)

    assert replay_summary.events_replayed == 3
    assert replay_summary.overrides_applied == 1
    assert replay_summary.merges_applied == 1
    assert replay_summary.splits_applied == 1

    with rebuilt_session_maker() as session:
        a_after = session.execute(
            select(Track).where(Track.public_id == a_track_public_id)
        ).scalar_one()
        b_after = session.execute(
            select(Track).where(Track.public_id == b_track_public_id)
        ).scalar_one()
        c_after = session.execute(
            select(Track).where(Track.public_id == new_track_public_id)
        ).scalar_one()

        a_file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'a.wav')
        ).scalar_one()
        b_file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'b.wav')
        ).scalar_one()
        override_file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'override.wav')
        ).scalar_one()

        # -- merge/split outcome: same public IDs, same statuses/relationships --
        assert a_after.status == TrackStatus.ACTIVE
        assert b_after.status == TrackStatus.MERGED
        assert b_after.merged_into_track_id == a_after.id
        assert c_after.status == TrackStatus.ACTIVE

        assert _active_file_ids(session, a_after.id) == {a_file.id}
        assert _active_file_ids(session, c_after.id) == {b_file.id}

        # -- same preferred file --
        assert c_after.preferred_file_id == b_file.id

        # -- same override, AND the same track public_id restored (a track
        # that only ever received an override is just as much a stable
        # identity as a merged/split one -- design §8/§33) --
        override_track_after = active_track_for_file(session, override_file.id)
        assert override_track_after is not None
        assert override_track_after.public_id == override_track_public_id
        identity = CatalogService(session).effective_identity(override_track_after)
        assert identity.artist == 'Curated Artist Name'


def test_replay_fails_loudly_when_a_referenced_file_no_longer_exists(tmp_path: Path) -> None:
    """The literal review-gate demand: deliberately construct an event
    referencing a file that no longer exists at its recorded relative_path,
    and confirm replay fails loudly rather than silently skipping or
    guessing (`.claude/rules/curation-persistence.md`).
    """
    config = _config(tmp_path, 'data')
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'present.wav', num_frames=100)

    engine = _engine(config)
    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    events_path = _events_path(config)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            {
                'sequence': 1,
                'event_uuid': '00000000-0000-0000-0000-000000000001',
                'event_type': 'TRACK_OVERRIDE_SET',
                'track_public_id': 'trk_doesnotmatter00000000000000000',
                'file_public_id': None,
                'payload': {
                    'track_public_id': 'trk_doesnotmatter00000000000000000',
                    'field': 'artist',
                    'value': 'Ghost Artist',
                    'track_relative_paths': ['this-file-was-deleted-before-rebuild.wav'],
                },
                'created_at': None,
            }
        )
        + '\n',
        encoding='utf-8',
    )

    with session_maker() as session:
        with pytest.raises(ReplayError, match='this-file-was-deleted-before-rebuild.wav'):
            CurationReplay(config, session).replay(events_path)

    # Nothing was left half-applied.
    with session_maker() as session:
        tracks = list(session.execute(select(Track)).scalars())
        assert len(tracks) == 1
        assert tracks[0].status == TrackStatus.PROVISIONAL
