import subprocess
import time
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.catalog.service import CatalogService
from djlib.config import DjlibConfig
from djlib.db.enums import DecisionSource, DuplicateStatus, IdentityEventType, RelationshipType, TrackStatus
from djlib.db.models import DuplicateGroup, DuplicateGroupMember, FileRecord, Track, TrackFile, TrackIdentityEvent
from djlib.db.session import session_factory
from djlib.duplicates.service import DuplicateService
from djlib.ids import new_public_id
from djlib.scan.service import ScanService


def _make_tagged_m4a(path: Path, seed: int, artist: str, title: str, duration: float = 1.0) -> None:
    """A real, retaggable audio fixture -- no mocks.

    The `exiftool` build available in CI can only *write* tags into M4A
    containers (MP3/FLAC/WAV/OGG/AIFF writing is unsupported here), so the
    override-rescan test needs a container it can actually retag to
    simulate a real source metadata change.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            'ffmpeg', '-y', '-v', 'error',
            '-f', 'lavfi', '-i', f'anoisesrc=duration={duration}:color=white:seed={seed}',
            str(path),
        ],
        check=True,
    )
    subprocess.run(
        ['exiftool', '-overwrite_original', f'-Artist={artist}', f'-Title={title}', str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _retag(path: Path, **tags: str) -> None:
    time.sleep(0.01)  # guarantee a distinct mtime_ns from the previous write
    args = ['exiftool', '-overwrite_original', *(f'-{key}={value}' for key, value in tags.items()), str(path)]
    subprocess.run(args, check=True, capture_output=True, text=True)


def _make_active_track_with_file(
    session: Session, relative_path: str, artist: str, title: str
) -> Track:
    file = FileRecord(
        public_id=new_public_id('fil'),
        relative_path=relative_path,
        size_bytes=1_000,
        mtime_ns=1,
        extension='.m4a',
        resolved_artist=artist,
        resolved_title=title,
    )
    session.add(file)
    session.flush()

    track = Track(public_id=new_public_id('trk'), status=TrackStatus.PROVISIONAL)
    session.add(track)
    session.flush()

    session.add(
        TrackFile(
            track_id=track.id,
            file_id=file.id,
            relationship=RelationshipType.PRIMARY,
            decision_source=DecisionSource.AUTOMATIC,
            is_active=True,
        )
    )
    session.flush()

    CatalogService(session).activate_track(track, preferred_file_id=file.id)
    return track


# -- Step 1: override-rescan test ------------------------------------------


def test_override_survives_rescan_of_changed_source_metadata(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    fixture = config.music_root / 'Artist One - Track One.m4a'
    _make_tagged_m4a(fixture, seed=1, artist='Original Artist', title='Original Title')

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        track = session.execute(select(Track)).scalar_one()
        track_public_id = track.public_id
        assert track.artist == 'Original Artist'

        CatalogService(session).set_override(track_public_id, 'artist', 'Curated Artist Name')
        session.commit()

    with session_maker() as session:
        track = session.execute(select(Track).where(Track.public_id == track_public_id)).scalar_one()
        identity = CatalogService(session).effective_identity(track)
        assert identity.artist == 'Curated Artist Name'
        # Resolved layer is still directly inspectable and unchanged so far.
        assert track.artist == 'Original Artist'

    _retag(fixture, Artist='Changed Source Artist')
    summary = ScanService(config, session_maker).scan()
    assert summary.files_changed == 1

    with session_maker() as session:
        track = session.execute(select(Track).where(Track.public_id == track_public_id)).scalar_one()
        identity = CatalogService(session).effective_identity(track)

        # RESOLVED layer genuinely refreshed underneath from the new source tag...
        assert track.artist == 'Changed Source Artist'
        # ...but EFFECTIVE identity still returns the untouched human override.
        assert identity.artist == 'Curated Artist Name'


# -- Step 2: immutable merge test -------------------------------------------


def test_human_merge_marks_source_merged_target_active_and_records_immutable_event(
    config: DjlibConfig, engine: Engine
) -> None:
    session_maker = session_factory(engine)
    with session_maker() as session:
        source = _make_active_track_with_file(session, 'a.m4a', artist='Artist A', title='Track A')
        target = _make_active_track_with_file(session, 'b.m4a', artist='Artist B', title='Track B')
        source_public_id = source.public_id
        target_public_id = target.public_id
        source_file_id = session.execute(
            select(TrackFile.file_id).where(TrackFile.track_id == source.id)
        ).scalar_one()
        session.commit()

    with session_maker() as session:
        event = CatalogService(session).merge_tracks(source_public_id, target_public_id)
        assert event.event_type == IdentityEventType.MERGE
        session.commit()

    with session_maker() as session:
        source_after = session.execute(
            select(Track).where(Track.public_id == source_public_id)
        ).scalar_one()
        target_after = session.execute(
            select(Track).where(Track.public_id == target_public_id)
        ).scalar_one()

        assert source_after.status == TrackStatus.MERGED
        assert source_after.merged_into_track_id == target_after.id
        assert target_after.status == TrackStatus.ACTIVE
        # Both public IDs unchanged -- no ID reuse.
        assert source_after.public_id == source_public_id
        assert target_after.public_id == target_public_id

        migrated_link = session.execute(
            select(TrackFile).where(TrackFile.file_id == source_file_id)
        ).scalar_one()
        assert migrated_link.track_id == target_after.id
        assert migrated_link.is_active is True
        assert migrated_link.relationship == RelationshipType.AUDIO_EQUIVALENT
        assert migrated_link.decision_source == DecisionSource.HUMAN

        events = list(session.execute(select(TrackIdentityEvent)).scalars())
        assert len(events) == 1
        assert events[0].event_type == IdentityEventType.MERGE
        assert events[0].source_track_public_id == source_public_id
        assert events[0].target_track_public_id == target_public_id
        assert events[0].payload_json['decision_source'] == DecisionSource.HUMAN.value

        all_tracks = list(session.execute(select(Track)).scalars())
        assert len(all_tracks) == 2
        assert {t.public_id for t in all_tracks} == {source_public_id, target_public_id}


# -- Step 3: split test ------------------------------------------------------


def test_human_split_creates_new_public_id_and_retains_source_id_for_remaining_files(
    config: DjlibConfig, engine: Engine
) -> None:
    session_maker = session_factory(engine)
    with session_maker() as session:
        file1 = FileRecord(
            public_id=new_public_id('fil'), relative_path='f1.m4a', size_bytes=1, mtime_ns=1,
            extension='.m4a', resolved_artist='Shared Artist', resolved_title='Track One',
        )
        file2 = FileRecord(
            public_id=new_public_id('fil'), relative_path='f2.m4a', size_bytes=1, mtime_ns=1,
            extension='.m4a', resolved_artist='Shared Artist', resolved_title='Track One',
        )
        file3 = FileRecord(
            public_id=new_public_id('fil'), relative_path='f3.m4a', size_bytes=1, mtime_ns=1,
            extension='.m4a', resolved_artist='Distinct Remix Artist',
            resolved_title='Totally Different Track',
        )
        session.add_all([file1, file2, file3])
        session.flush()

        track = Track(
            public_id=new_public_id('trk'), status=TrackStatus.ACTIVE,
            artist='Shared Artist', title='Track One',
        )
        session.add(track)
        session.flush()

        for file, relationship in (
            (file1, RelationshipType.PRIMARY),
            (file2, RelationshipType.AUDIO_EQUIVALENT),
            (file3, RelationshipType.PROBABLE),
        ):
            session.add(
                TrackFile(
                    track_id=track.id, file_id=file.id, relationship=relationship,
                    decision_source=DecisionSource.AUTOMATIC, is_active=True,
                )
            )
        session.flush()

        source_public_id = track.public_id
        file1_id, file2_id, file3_id = file1.id, file2.id, file3.id
        file3_public_id = file3.public_id
        session.commit()

    with session_maker() as session:
        new_track = CatalogService(session).split_track(
            source_public_id, file_public_ids=[file3_public_id]
        )
        new_track_public_id = new_track.public_id
        session.commit()

    assert new_track_public_id != source_public_id

    with session_maker() as session:
        source_after = session.execute(
            select(Track).where(Track.public_id == source_public_id)
        ).scalar_one()
        new_after = session.execute(
            select(Track).where(Track.public_id == new_track_public_id)
        ).scalar_one()

        assert source_after.public_id == source_public_id
        assert new_after.public_id == new_track_public_id

        source_active_file_ids = {
            link.file_id
            for link in session.execute(
                select(TrackFile).where(
                    TrackFile.track_id == source_after.id, TrackFile.is_active.is_(True)
                )
            ).scalars()
        }
        assert source_active_file_ids == {file1_id, file2_id}

        new_links = list(
            session.execute(select(TrackFile).where(TrackFile.track_id == new_after.id)).scalars()
        )
        assert len(new_links) == 1
        assert new_links[0].file_id == file3_id
        assert new_links[0].is_active is True
        assert new_links[0].relationship == RelationshipType.PRIMARY

        # Identity copied from the split-off file's own resolved metadata,
        # never from the source track's shared identity.
        assert new_after.artist == 'Distinct Remix Artist'
        assert new_after.title == 'Totally Different Track'
        assert new_after.status == TrackStatus.ACTIVE

        events = list(session.execute(select(TrackIdentityEvent)).scalars())
        assert len(events) == 1
        assert events[0].event_type == IdentityEventType.SPLIT
        assert events[0].source_track_public_id == source_public_id
        assert events[0].target_track_public_id == new_track_public_id


# -- Regression: a human split must never be silently re-merged by a later
# automatic `duplicates run` (design §13; found during Task 11 review). --


def test_split_rejects_any_duplicate_group_spanning_both_sides_so_it_cannot_be_auto_remerged(
    config: DjlibConfig, engine: Engine
) -> None:
    session_maker = session_factory(engine)
    with session_maker() as session:
        remaining_file = FileRecord(
            public_id=new_public_id('fil'), relative_path='remaining.m4a',
            size_bytes=1, mtime_ns=1, extension='.m4a',
            resolved_artist='Shared Artist', resolved_title='Track One',
        )
        moved_file = FileRecord(
            public_id=new_public_id('fil'), relative_path='moved.m4a',
            size_bytes=1, mtime_ns=1, extension='.m4a',
            resolved_artist='Shared Artist', resolved_title='Track One',
        )
        session.add_all([remaining_file, moved_file])
        session.flush()

        track = Track(
            public_id=new_public_id('trk'), status=TrackStatus.ACTIVE,
            artist='Shared Artist', title='Track One',
        )
        session.add(track)
        session.flush()

        for file in (remaining_file, moved_file):
            session.add(
                TrackFile(
                    track_id=track.id, file_id=file.id, relationship=RelationshipType.AUDIO_EQUIVALENT,
                    decision_source=DecisionSource.AUTOMATIC, is_active=True,
                )
            )
        session.flush()

        # A pre-existing duplicate-candidate group spans both files -- this
        # is exactly what would let a later automatic run reconsider merging
        # them, since it has no memory of the split that's about to happen.
        group = DuplicateGroup(
            public_id=new_public_id('dup'), status=DuplicateStatus.AUTO_CONFIRMED,
            matcher_version='1',
        )
        session.add(group)
        session.flush()
        session.add(DuplicateGroupMember(group_id=group.id, file_id=remaining_file.id))
        session.add(DuplicateGroupMember(group_id=group.id, file_id=moved_file.id))
        session.flush()

        source_public_id = track.public_id
        moved_file_public_id = moved_file.public_id
        group_id = group.id
        session.commit()

    with session_maker() as session:
        CatalogService(session).split_track(source_public_id, file_public_ids=[moved_file_public_id])
        session.commit()

    with session_maker() as session:
        group_after = session.get(DuplicateGroup, group_id)
        assert group_after.status == DuplicateStatus.REJECTED

    # And the ultimate proof: analyze()/run() must leave the split apart.
    with session_maker() as session:
        DuplicateService(config, session).analyze()
        session.commit()

    with session_maker() as session:
        tracks_after_analyze = list(session.execute(select(Track)).scalars())
        assert len(tracks_after_analyze) == 2
        assert {t.status for t in tracks_after_analyze} == {TrackStatus.ACTIVE}
        assert all(t.merged_into_track_id is None for t in tracks_after_analyze)
