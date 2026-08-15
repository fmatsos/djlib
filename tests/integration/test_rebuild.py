"""Task 15, Step 2: the full rebuild flow -- scan, curate, export, delete
SQLite, fresh-migrate, full scan, replay, invariants -- via the real
`RebuildService`, proving the milestone's central promise end to end (design
§25/§33): the catalogue is fully reconstructible from `/music` plus
`/data/curation/events.jsonl` alone. Never modifies `/music` anywhere in this
test -- every fixture file's hash is verified unchanged before and after.
"""

import datetime as dt
import json
import subprocess
from pathlib import Path

from blake3 import blake3
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import sessionmaker, Session

from djlib.catalog.queries import active_track_for_file
from djlib.catalog.service import CatalogService
from djlib.config import DjlibConfig
from djlib.curation.journal import CurationJournal
from djlib.curation.decisions import DecisionImporter
from djlib.curation.rebuild import RebuildService
from djlib.db.enums import DuplicateStatus, TrackStatus
from djlib.db.models import DuplicateGroup, FileRecord, Track, TrackFile
from djlib.db.session import session_factory
from djlib.duplicates.service import DuplicateService
from djlib.report.generator import ReportGenerator
from djlib.scan.service import ScanService


def _make_noise_wav(path: Path, seed: int, duration: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            'ffmpeg', '-y', '-v', 'error',
            '-f', 'lavfi', '-i', f'anoisesrc=duration={duration}:color=white:seed={seed}',
            '-ar', '44100', '-sample_fmt', 's16', str(path),
        ],
        check=True,
    )


def _reencode(src: Path, dst: Path, *codec_args: str) -> None:
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(src), *codec_args, str(dst)], check=True)


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _hash_all(music_root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(music_root)): blake3(path.read_bytes()).hexdigest()
        for path in sorted(music_root.rglob('*'))
        if path.is_file()
    }


def _active_relative_paths(session: Session, track_id: int) -> list[str]:
    return sorted(
        f.relative_path
        for f in session.execute(
            select(FileRecord)
            .join(TrackFile, TrackFile.file_id == FileRecord.id)
            .where(TrackFile.track_id == track_id, TrackFile.is_active.is_(True))
        ).scalars()
    )


def _curated_projection(
    session_maker: sessionmaker[Session],
    *,
    override_file_relative_path: str,
    override_track_public_id: str,
    merge_survivor_public_id: str,
    merge_absorbed_public_id: str,
    dup_group_public_id: str,
    dup_survivor_file_relative_path: str,
) -> dict:
    """Looks every entity up by its known-stable public ID. A track that only
    ever received an override is just as much a stable identity as a
    merged/split one (design §8/§33), so `override_track_public_id` must
    resolve against the *rebuilt* database too -- that's part of the proof,
    exactly like `merge_survivor_public_id`/`merge_absorbed_public_id`/
    `dup_group_public_id` below.
    """
    with session_maker() as session:
        override_file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == override_file_relative_path)
        ).scalar_one()
        override_track = active_track_for_file(session, override_file.id)
        assert override_track is not None
        assert override_track.public_id == override_track_public_id
        override_identity = CatalogService(session).effective_identity(override_track)

        survivor = session.execute(
            select(Track).where(Track.public_id == merge_survivor_public_id)
        ).scalar_one()
        absorbed = session.execute(
            select(Track).where(Track.public_id == merge_absorbed_public_id)
        ).scalar_one()

        group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == dup_group_public_id)
        ).scalar_one()
        dup_survivor_file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == dup_survivor_file_relative_path)
        ).scalar_one()
        dup_survivor_track = active_track_for_file(session, dup_survivor_file.id)
        assert dup_survivor_track is not None
        dup_survivor_preferred_relative_path = None
        if dup_survivor_track.preferred_file_id is not None:
            dup_survivor_preferred_relative_path = session.get(
                FileRecord, dup_survivor_track.preferred_file_id
            ).relative_path

        return {
            'override_identity': (
                override_identity.artist,
                override_identity.title,
                override_identity.version,
                override_identity.edition,
            ),
            'merge_survivor_status': survivor.status.value,
            'merge_survivor_active_relative_paths': _active_relative_paths(session, survivor.id),
            'merge_absorbed_status': absorbed.status.value,
            'merge_absorbed_merged_into_survivor': absorbed.merged_into_track_id == survivor.id,
            'dup_group_status': group.status.value,
            'dup_survivor_status': dup_survivor_track.status.value,
            'dup_survivor_preferred_relative_path': dup_survivor_preferred_relative_path,
        }


def test_rebuild_reconstructs_the_curated_projection_exactly(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)

    # override target
    override_path = config.music_root / 'override.wav'
    _make_noise_wav(override_path, seed=1)

    # human-merge pair (no duplicate-detection involvement at all)
    merge_a_path = config.music_root / 'merge_a.wav'
    merge_b_path = config.music_root / 'merge_b.wav'
    _make_noise_wav(merge_a_path, seed=2)
    _make_noise_wav(merge_b_path, seed=3)

    # a real version-conflict-shaped duplicate pair -> REVIEW_REQUIRED group
    # with a proposed preferred file (Task 10's own established scenario) ->
    # CONFIRM via a real report + DecisionImporter import cycle
    original_mix = config.music_root / 'dup' / 'Artist - Track (Original Mix).wav'
    extended_mix = config.music_root / 'dup' / 'Artist - Track (Extended Mix).flac'
    _make_noise_wav(original_mix, seed=4, duration=3.0)
    _reencode(original_mix, extended_mix, '-codec:a', 'flac')

    hashes_before_curation = _hash_all(config.music_root)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        override_track_public_id = active_track_for_file(
            session,
            session.execute(
                select(FileRecord.id).where(FileRecord.relative_path == 'override.wav')
            ).scalar_one(),
        ).public_id
        merge_a_track_public_id = active_track_for_file(
            session,
            session.execute(
                select(FileRecord.id).where(FileRecord.relative_path == 'merge_a.wav')
            ).scalar_one(),
        ).public_id
        merge_b_track_public_id = active_track_for_file(
            session,
            session.execute(
                select(FileRecord.id).where(FileRecord.relative_path == 'merge_b.wav')
            ).scalar_one(),
        ).public_id

    # -- apply a mix of human curation --

    with session_maker() as session:
        CatalogService(session).set_override(override_track_public_id, 'title', 'Curated Title')
        session.commit()

    with session_maker() as session:
        CatalogService(session).merge_tracks(merge_b_track_public_id, merge_a_track_public_id)
        session.commit()

    with session_maker() as session:
        DuplicateService(config, session).detect()
    with session_maker() as session:
        DuplicateService(config, session).analyze()

    with session_maker() as session:
        (dup_group,) = list(session.execute(select(DuplicateGroup)).scalars())
        assert dup_group.status == DuplicateStatus.REVIEW_REQUIRED
        dup_group_public_id = dup_group.public_id

    artifact = ReportGenerator(config, session_maker()).generate()

    decisions_path = config.data_root / 'decisions.json'
    decisions_path.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'report_id': artifact.report_id,
                'catalog_revision': artifact.catalog_revision,
                'generated_at': _now_iso(),
                'decisions': [
                    {
                        'group_id': dup_group_public_id,
                        'decision': 'CONFIRM',
                        'reviewed_at': _now_iso(),
                    }
                ],
            }
        ),
        encoding='utf-8',
    )
    with session_maker() as session:
        DecisionImporter(config, session).import_file(decisions_path)

    with session_maker() as session:
        dup_survivor_track = session.execute(
            select(Track).where(Track.status == TrackStatus.ACTIVE, Track.preferred_file_id.is_not(None))
        ).scalars().first()
        # There are two ACTIVE-with-preferred-file tracks by now (the merge
        # survivor never got a preferred_file_id set, so it's unambiguous):
        # pick the one whose preferred file lives under 'dup/'.
        dup_survivor_file = session.get(FileRecord, dup_survivor_track.preferred_file_id)
        dup_survivor_file_relative_path = dup_survivor_file.relative_path

    with session_maker() as session:
        exported = CurationJournal(config).export_pending(session)
    assert exported > 0

    before_projection = _curated_projection(
        session_maker,
        override_file_relative_path='override.wav',
        override_track_public_id=override_track_public_id,
        merge_survivor_public_id=merge_a_track_public_id,
        merge_absorbed_public_id=merge_b_track_public_id,
        dup_group_public_id=dup_group_public_id,
        dup_survivor_file_relative_path=dup_survivor_file_relative_path,
    )
    assert before_projection['override_identity'][1] == 'Curated Title'
    assert before_projection['merge_absorbed_merged_into_survivor'] is True
    assert before_projection['dup_group_status'] == 'CONFIRMED'

    hashes_before_rebuild = _hash_all(config.music_root)
    assert hashes_before_rebuild == hashes_before_curation

    engine.dispose()

    # -- the actual rebuild: real RebuildService, real subprocess `alembic
    # upgrade head`, real full scan, real replay --
    summary = RebuildService(config).rebuild()

    assert summary.backup_path is not None
    assert summary.backup_path.exists()
    assert summary.scan_summary.files_new == 5
    assert summary.replay_summary.events_replayed > 0
    # `music_root_read_only` is a sandbox artifact unrelated to rebuild
    # correctness (this test's `tmp_path` is a genuinely writable directory,
    # not the real read-only bind mount -- see `test_doctor.py`'s identical
    # caveat); every other invariant must hold.
    relevant_failed_checks = [c for c in summary.failed_checks if c != 'music_root_read_only']
    assert relevant_failed_checks == []

    hashes_after_rebuild = _hash_all(config.music_root)
    assert hashes_after_rebuild == hashes_before_curation

    rebuilt_engine = create_engine(config.database_url, future=True)
    rebuilt_session_maker = session_factory(rebuilt_engine)
    try:
        after_projection = _curated_projection(
            rebuilt_session_maker,
            override_file_relative_path='override.wav',
            override_track_public_id=override_track_public_id,
            merge_survivor_public_id=merge_a_track_public_id,
            merge_absorbed_public_id=merge_b_track_public_id,
            dup_group_public_id=dup_group_public_id,
            dup_survivor_file_relative_path=dup_survivor_file_relative_path,
        )
    finally:
        rebuilt_engine.dispose()

    assert after_projection == before_projection


def test_rebuild_restores_preferred_file_for_a_fully_automatic_consolidation(
    config: DjlibConfig, engine: Engine
) -> None:
    """Task 16 finding: `duplicates run`'s fully-automatic consolidation path
    (design §21's AUTO_CONFIRMED groups -- no human decision anywhere) must
    durably record which file became preferred, or `djlib rebuild` silently
    drops it -- violating design §32's "same preferred-file decisions"
    rebuild guarantee for the single most common duplicate scenario of all:
    byte-identical copies.
    """
    config.music_root.mkdir(parents=True)
    # Same "Artist - Title" filename in two different subdirectories: with no
    # tags at all, the filename parser (Task 5) is the only source of
    # artist/title, and blocking's exact-artist tier
    # (`blocking.py::_collect_exact_artist_tier`) requires a non-null
    # `artist_normalized`/`title_normalized` match to consider two files
    # candidates at all -- a bare, separator-less filename resolves to no
    # title whatsoever and would never be blocked together.
    original = config.music_root / 'dup' / 'a' / 'Some Artist - Song Title.wav'
    exact_copy = config.music_root / 'dup' / 'b' / 'Some Artist - Song Title.wav'
    _make_noise_wav(original, seed=42)
    exact_copy.parent.mkdir(parents=True, exist_ok=True)
    exact_copy.write_bytes(original.read_bytes())

    hashes_before_curation = _hash_all(config.music_root)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        DuplicateService(config, session).run()

    with session_maker() as session:
        (group,) = list(session.execute(select(DuplicateGroup)).scalars())
        assert group.status == DuplicateStatus.AUTO_CONFIRMED
        survivor = session.execute(
            select(Track).where(Track.status == TrackStatus.ACTIVE)
        ).scalar_one()
        assert survivor.preferred_file_id is not None
        survivor_public_id = survivor.public_id
        preferred_relative_path = session.get(FileRecord, survivor.preferred_file_id).relative_path

    with session_maker() as session:
        exported = CurationJournal(config).export_pending(session)
    assert exported > 0

    hashes_before_rebuild = _hash_all(config.music_root)
    assert hashes_before_rebuild == hashes_before_curation

    engine.dispose()
    summary = RebuildService(config).rebuild()
    relevant_failed_checks = [c for c in summary.failed_checks if c != 'music_root_read_only']
    assert relevant_failed_checks == []

    assert _hash_all(config.music_root) == hashes_before_curation

    rebuilt_engine = create_engine(config.database_url, future=True)
    try:
        rebuilt_session_maker = session_factory(rebuilt_engine)
        with rebuilt_session_maker() as session:
            survivor_after = session.execute(
                select(Track).where(Track.public_id == survivor_public_id)
            ).scalar_one()
            assert survivor_after.preferred_file_id is not None
            preferred_after = session.get(FileRecord, survivor_after.preferred_file_id)
            assert preferred_after.relative_path == preferred_relative_path
    finally:
        rebuilt_engine.dispose()
