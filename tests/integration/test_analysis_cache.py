import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.db.enums import AnalysisStatus
from djlib.db.models import FileRecord
from djlib.db.session import session_factory
from djlib.duplicates import hashing
from djlib.duplicates.calibration import collect_calibration_rows
from djlib.duplicates.chromaprint import ChromaprintService, fingerprint_similarity
from djlib.duplicates.hashing import HashService
from djlib.metadata.types import SubprocessCommandRunner
from djlib.scan.service import ScanService


def _make_noise_mp3(path: Path, seed: int, duration: float = 3.0) -> None:
    """Encode a real, distinct-per-seed mp3 using the system ffmpeg -- no mocks."""
    subprocess.run(
        [
            'ffmpeg', '-y', '-v', 'error',
            '-f', 'lavfi', '-i', f'anoisesrc=duration={duration}:color=white:seed={seed}',
            '-ar', '44100', '-codec:a', 'libmp3lame', '-qscale:a', '4', str(path),
        ],
        check=True,
    )


def _file_record(session_maker: sessionmaker[Session], relative_path: str) -> FileRecord:
    with session_maker() as session:
        return session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one()


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_hash_and_fingerprint_are_computed_once_for_an_unchanged_file(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    _make_noise_mp3(config.music_root / 'track.mp3', seed=1)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    hash_service = HashService(config.music_root)
    runner = SubprocessCommandRunner()
    chromaprint_service = ChromaprintService(config.music_root, runner)

    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()

        with patch.object(hashing, '_hash_file', wraps=hashing._hash_file) as hash_spy:
            first_hash = hash_service.ensure_current(file)
            second_hash = hash_service.ensure_current(file)
        assert first_hash == second_hash
        assert hash_spy.call_count == 1

        with patch.object(runner, 'run', wraps=runner.run) as fpcalc_spy:
            first_fp = chromaprint_service.ensure_current(file)
            second_fp = chromaprint_service.ensure_current(file)
        assert first_fp == second_fp
        assert fpcalc_spy.call_count == 1

        session.commit()


def test_rescanning_a_changed_file_invalidates_and_recomputes_new_evidence(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    fixture = config.music_root / 'track.mp3'
    _make_noise_mp3(fixture, seed=1)

    session_maker = session_factory(engine)
    scan_service = ScanService(config, session_maker)
    scan_service.scan()

    hash_service = HashService(config.music_root)
    chromaprint_service = ChromaprintService(config.music_root, SubprocessCommandRunner())

    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()
        old_hash = hash_service.ensure_current(file)
        old_fp = chromaprint_service.ensure_current(file)
        assert file.binary_hash_status == AnalysisStatus.CURRENT
        assert file.chromaprint_status == AnalysisStatus.CURRENT
        session.commit()

    _make_noise_mp3(fixture, seed=2)
    _bump_mtime(fixture)
    changed_run = scan_service.scan()
    assert changed_run.files_changed == 1

    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()
        assert file.binary_hash_status == AnalysisStatus.STALE
        assert file.chromaprint_status == AnalysisStatus.STALE
        assert file.quality_status == AnalysisStatus.STALE

        new_hash = hash_service.ensure_current(file)
        new_fp = chromaprint_service.ensure_current(file)
        session.commit()

    assert new_hash != old_hash
    assert new_fp.fingerprint != old_fp.fingerprint


def test_full_rescan_marks_cached_analysis_stale_even_when_file_is_unchanged(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    fixture = config.music_root / 'track.mp3'
    _make_noise_mp3(fixture, seed=3)

    session_maker = session_factory(engine)
    scan_service = ScanService(config, session_maker)
    scan_service.scan()

    hash_service = HashService(config.music_root)
    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()
        hash_service.ensure_current(file)
        assert file.binary_hash_status == AnalysisStatus.CURRENT
        session.commit()

    unchanged_run = scan_service.scan(full=True)
    assert unchanged_run.files_unchanged == 1
    assert unchanged_run.files_changed == 0

    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()
        assert file.binary_hash_status == AnalysisStatus.STALE
        assert file.chromaprint_status == AnalysisStatus.STALE
        assert file.quality_status == AnalysisStatus.STALE


def test_fingerprint_similarity_scores_identical_audio_high_and_distinct_audio_lower(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    same_a = config.music_root / 'same_a.mp3'
    same_b = config.music_root / 'same_b.mp3'
    different = config.music_root / 'different.mp3'
    _make_noise_mp3(same_a, seed=10)
    same_b.write_bytes(same_a.read_bytes())
    _make_noise_mp3(different, seed=20)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    chromaprint_service = ChromaprintService(config.music_root, SubprocessCommandRunner())
    with session_maker() as session:
        file_a = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'same_a.mp3')
        ).scalar_one()
        file_b = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'same_b.mp3')
        ).scalar_one()
        file_c = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'different.mp3')
        ).scalar_one()

        fp_a = chromaprint_service.ensure_current(file_a)
        fp_b = chromaprint_service.ensure_current(file_b)
        fp_c = chromaprint_service.ensure_current(file_c)
        session.commit()

    identical_similarity = fingerprint_similarity(fp_a, fp_b)
    distinct_similarity = fingerprint_similarity(fp_a, fp_c)

    assert identical_similarity == 1.0
    assert distinct_similarity < 0.5
    assert distinct_similarity < identical_similarity


def test_calibrate_reports_candidate_pairs_without_touching_duplicate_tables(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    (config.music_root / 'crate_a').mkdir()
    (config.music_root / 'crate_b').mkdir()
    original = config.music_root / 'crate_a' / 'Artist - Track.mp3'
    duplicate = config.music_root / 'crate_b' / 'Artist - Track.mp3'
    _make_noise_mp3(original, seed=42)
    duplicate.write_bytes(original.read_bytes())

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    hash_service = HashService(config.music_root)
    chromaprint_service = ChromaprintService(config.music_root, SubprocessCommandRunner())

    with session_maker() as session:
        rows = collect_calibration_rows(session, hash_service, chromaprint_service)
        session.commit()

    assert len(rows) == 1
    row = rows[0]
    assert row.binary_hash_equal is True
    # An exact binary duplicate never needs fingerprinting (design §18).
    assert row.chromaprint_similarity is None

    with engine.connect() as conn:
        from sqlalchemy import text

        group_count = conn.execute(text('SELECT COUNT(*) FROM duplicate_groups')).scalar_one()
        evidence_count = conn.execute(
            text('SELECT COUNT(*) FROM duplicate_pair_evidence')
        ).scalar_one()
    assert group_count == 0
    assert evidence_count == 0
