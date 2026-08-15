import wave
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.db.enums import DecisionSource, RelationshipType, TrackStatus
from djlib.db.models import FileRecord, Track, TrackFile
from djlib.db.session import session_factory
from djlib.scan.service import ScanService


def _write_valid_wav(path: Path, num_frames: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b'\x00\x00' * num_frames)


def _all_tracks(session_maker: sessionmaker[Session]) -> list[Track]:
    with session_maker() as session:
        return list(session.execute(select(Track)).scalars())


def _all_track_files(session_maker: sessionmaker[Session]) -> list[TrackFile]:
    with session_maker() as session:
        return list(session.execute(select(TrackFile)).scalars())


def _file_record(session_maker: sessionmaker[Session], relative_path: str) -> FileRecord:
    with session_maker() as session:
        return session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one()


def test_first_scan_creates_one_provisional_track_per_file(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'Artist One - Track One.wav')
    _write_valid_wav(config.music_root / 'Artist Two - Track Two.wav')

    session_maker = session_factory(engine)
    service = ScanService(config, session_maker)
    summary = service.scan()

    assert summary.files_new == 2

    with session_maker() as session:
        files = list(session.execute(select(FileRecord)).scalars())
        tracks = list(session.execute(select(Track)).scalars())
        track_files = list(session.execute(select(TrackFile)).scalars())

    assert len(files) == 2
    assert len(tracks) == 2
    assert {track.status for track in tracks} == {TrackStatus.PROVISIONAL}
    assert {track.public_id for track in tracks} == {tracks[0].public_id, tracks[1].public_id}
    assert len(track_files) == 2
    for link in track_files:
        assert link.relationship == RelationshipType.PRIMARY
        assert link.is_active is True
        assert link.decision_source == DecisionSource.AUTOMATIC

    file_ids = {f.id for f in files}
    linked_file_ids = {link.file_id for link in track_files}
    assert linked_file_ids == file_ids
    linked_track_ids = {link.track_id for link in track_files}
    assert linked_track_ids == {t.id for t in tracks}


def test_identical_resolved_identity_still_yields_two_separate_tracks(
    config: DjlibConfig, engine: Engine
) -> None:
    # Two distinct physical files that happen to resolve to the exact same
    # artist/title/version (same filename, different folders) must never be
    # merged into one track by tag resemblance alone -- design §8.
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'copy-a' / 'Artist - Title.wav', num_frames=100)
    _write_valid_wav(config.music_root / 'copy-b' / 'Artist - Title.wav', num_frames=100)

    session_maker = session_factory(engine)
    service = ScanService(config, session_maker)
    service.scan()

    with session_maker() as session:
        tracks = list(session.execute(select(Track)).scalars())

    assert len(tracks) == 2
    assert tracks[0].public_id != tracks[1].public_id
    for track in tracks:
        assert track.artist == 'Artist'
        assert track.title == 'Title'
        assert track.artist_normalized == 'artist'
        assert track.title_normalized == 'title'
        assert track.status == TrackStatus.PROVISIONAL


def test_second_unchanged_scan_creates_no_new_tracks(config: DjlibConfig, engine: Engine) -> None:
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'Artist One - Track One.wav')
    _write_valid_wav(config.music_root / 'Artist Two - Track Two.wav')

    session_maker = session_factory(engine)
    service = ScanService(config, session_maker)
    service.scan()

    tracks_after_first_scan = _all_tracks(session_maker)
    track_files_after_first_scan = _all_track_files(session_maker)
    track_ids_after_first_scan = {t.id for t in tracks_after_first_scan}

    second_summary = service.scan()

    assert second_summary.files_new == 0
    assert second_summary.files_changed == 0
    assert second_summary.files_unchanged == 2

    tracks_after_second_scan = _all_tracks(session_maker)
    track_files_after_second_scan = _all_track_files(session_maker)

    assert len(tracks_after_second_scan) == len(tracks_after_first_scan)
    assert {t.id for t in tracks_after_second_scan} == track_ids_after_first_scan
    assert len(track_files_after_second_scan) == len(track_files_after_first_scan)


def test_changed_file_refreshes_existing_track_instead_of_creating_a_second_one(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    fixture = config.music_root / 'Artist One - Track One.wav'
    _write_valid_wav(fixture, num_frames=100)

    session_maker = session_factory(engine)
    service = ScanService(config, session_maker)
    service.scan()

    with session_maker() as session:
        first_track = session.execute(select(Track)).scalar_one()
        original_track_public_id = first_track.public_id
        original_duration_ms = first_track.duration_reference_ms

    _write_valid_wav(fixture, num_frames=100_000)
    changed_summary = service.scan()

    assert changed_summary.files_changed == 1
    assert changed_summary.files_new == 0

    with session_maker() as session:
        tracks = list(session.execute(select(Track)).scalars())
        track_files = list(session.execute(select(TrackFile)).scalars())

    assert len(tracks) == 1
    refreshed_track = tracks[0]
    assert refreshed_track.public_id == original_track_public_id
    assert refreshed_track.duration_reference_ms is not None
    assert refreshed_track.duration_reference_ms != original_duration_ms
    assert len(track_files) == 1
    assert track_files[0].is_active is True
    assert track_files[0].relationship == RelationshipType.PRIMARY

    changed_record = _file_record(session_maker, 'Artist One - Track One.wav')
    assert refreshed_track.duration_reference_ms == changed_record.duration_ms
