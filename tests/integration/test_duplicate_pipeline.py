import subprocess
from pathlib import Path

from blake3 import blake3
from sqlalchemy import Engine, select

from djlib.config import DjlibConfig
from djlib.db.enums import DuplicateStatus, IdentityEventType, PairClassification, TrackStatus
from djlib.db.models import DuplicateGroup, Track, TrackFile, TrackIdentityEvent
from djlib.db.session import session_factory
from djlib.duplicates.service import DuplicateService
from djlib.scan.service import ScanService


def _make_noise_wav(path: Path, seed: int, duration: float = 3.0) -> None:
    """A real, deterministic-per-seed PCM source via the system ffmpeg -- no mocks."""
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


def test_exact_duplicate_pair_auto_consolidates_into_one_active_and_one_merged_track(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    original = config.music_root / 'crate_a' / 'Artist - Track.wav'
    duplicate = config.music_root / 'crate_b' / 'Artist - Track.wav'
    _make_noise_wav(original, seed=1)
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(original.read_bytes())

    original_hash_before = blake3(original.read_bytes()).hexdigest()
    duplicate_hash_before = blake3(duplicate.read_bytes()).hexdigest()

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        tracks_before = list(session.execute(select(Track)).scalars())
        assert len(tracks_before) == 2
        assert {t.status for t in tracks_before} == {TrackStatus.PROVISIONAL}

        summary = DuplicateService(config, session).run()

    assert summary.groups_detected == 1
    assert summary.groups_analyzed == 1
    assert summary.groups_consolidated == 1

    # Never touch the source files themselves -- purely a database-identity operation.
    assert blake3(original.read_bytes()).hexdigest() == original_hash_before
    assert blake3(duplicate.read_bytes()).hexdigest() == duplicate_hash_before

    with session_maker() as session:
        tracks = list(session.execute(select(Track)).scalars())
        assert len(tracks) == 2
        active_tracks = [t for t in tracks if t.status == TrackStatus.ACTIVE]
        merged_tracks = [t for t in tracks if t.status == TrackStatus.MERGED]
        assert len(active_tracks) == 1
        assert len(merged_tracks) == 1

        survivor = active_tracks[0]
        merged = merged_tracks[0]
        assert merged.merged_into_track_id == survivor.id
        assert survivor.preferred_file_id is not None

        survivor_files = list(
            session.execute(select(TrackFile).where(TrackFile.track_id == survivor.id)).scalars()
        )
        assert len(survivor_files) == 2
        assert all(link.is_active for link in survivor_files)

        merged_files = list(
            session.execute(select(TrackFile).where(TrackFile.track_id == merged.id)).scalars()
        )
        assert merged_files == []

        groups = list(session.execute(select(DuplicateGroup)).scalars())
        assert len(groups) == 1
        assert groups[0].status == DuplicateStatus.AUTO_CONFIRMED

        events = list(
            session.execute(
                select(TrackIdentityEvent).where(
                    TrackIdentityEvent.event_type == IdentityEventType.MERGE
                )
            ).scalars()
        )
        assert len(events) == 1
        event = events[0]
        assert event.source_track_public_id == merged.public_id
        assert event.target_track_public_id == survivor.public_id
        assert event.payload_json is not None
        assert event.payload_json.get('decision_source') == 'AUTOMATIC'


def test_version_conflict_pair_is_review_required_and_never_auto_merged(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    original_mix = config.music_root / 'Artist - Track (Original Mix).wav'
    _make_noise_wav(original_mix, seed=7, duration=3.0)
    # Same underlying audio, re-encoded losslessly into a different container
    # (so the binary hash differs but Chromaprint similarity stays very
    # high) -- filename explicitly claims a conflicting version. This is
    # exactly design §19's "audio evidence unexpectedly similar while
    # version metadata conflicts" scenario.
    extended_mix = config.music_root / 'Artist - Track (Extended Mix).flac'
    _reencode(original_mix, extended_mix, '-codec:a', 'flac')

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        tracks_before = {(t.public_id, t.status) for t in session.execute(select(Track)).scalars()}
        assert {status for _, status in tracks_before} == {TrackStatus.PROVISIONAL}

        summary = DuplicateService(config, session).run()

    assert summary.groups_detected == 1
    assert summary.groups_consolidated == 0

    with session_maker() as session:
        groups = list(session.execute(select(DuplicateGroup)).scalars())
        assert len(groups) == 1
        assert groups[0].status == DuplicateStatus.REVIEW_REQUIRED

        tracks_after = {(t.public_id, t.status) for t in session.execute(select(Track)).scalars()}
        # Nothing changed: same two PROVISIONAL tracks, completely untouched.
        assert tracks_after == tracks_before

        track_files = list(session.execute(select(TrackFile)).scalars())
        assert len(track_files) == 2
        assert all(link.is_active for link in track_files)

        events = list(session.execute(select(TrackIdentityEvent)).scalars())
        assert events == []


def test_review_required_group_pair_evidence_is_conflict(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    original_mix = config.music_root / 'Artist - Track (Original Mix).wav'
    _make_noise_wav(original_mix, seed=9, duration=3.0)
    extended_mix = config.music_root / 'Artist - Track (Extended Mix).flac'
    _reencode(original_mix, extended_mix, '-codec:a', 'flac')

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        service = DuplicateService(config, session)
        service.detect()
        service.analyze()
        stats = service.stats()

    assert stats.group_status_counts[DuplicateStatus.REVIEW_REQUIRED.value] == 1
    assert stats.pair_classification_counts[PairClassification.CONFLICT.value] == 1
