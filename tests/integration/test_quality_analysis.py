import random
import subprocess
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.db.enums import AnalysisStatus
from djlib.db.models import FileQualityAnalysis, FileRecord
from djlib.db.session import session_factory
from djlib.duplicates.quality import QualityAnalyzer
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


def test_plain_scan_never_invokes_quality_analysis_and_leaves_status_pending(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    _make_noise_mp3(config.music_root / 'track.mp3', seed=1)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()
        assert file.quality_status == AnalysisStatus.PENDING

        rows = session.execute(select(FileQualityAnalysis)).scalars().all()
        assert rows == []


def test_analyze_twice_on_an_unchanged_file_only_invokes_ffmpeg_once(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    fixture = config.music_root / 'track.mp3'
    _make_noise_mp3(fixture, seed=2)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    runner = SubprocessCommandRunner()
    analyzer = QualityAnalyzer(runner)

    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()

        with patch.object(runner, 'run', wraps=runner.run) as spy:
            first = analyzer.analyze(fixture, file)
            calls_after_first_analysis = spy.call_count
            second = analyzer.analyze(fixture, file)

        assert calls_after_first_analysis > 0
        # The cache hit must not invoke ffmpeg again at all.
        assert spy.call_count == calls_after_first_analysis
        assert file.quality_status == AnalysisStatus.CURRENT
        assert first.quality_score == second.quality_score
        session.commit()
        file_id = file.id

    with session_maker() as session:
        rows = session.execute(
            select(FileQualityAnalysis).where(FileQualityAnalysis.file_id == file_id)
        ).scalars().all()
        # Exactly one row was ever persisted -- the second `analyze` call
        # reused the cached result rather than inserting a second version.
        assert len(rows) == 1


def test_rescanning_a_changed_file_forces_quality_reanalysis(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    fixture = config.music_root / 'track.mp3'
    _make_noise_mp3(fixture, seed=6)

    session_maker = session_factory(engine)
    scan_service = ScanService(config, session_maker)
    scan_service.scan()

    analyzer = QualityAnalyzer(SubprocessCommandRunner())
    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()
        analyzer.analyze(fixture, file)
        assert file.quality_status == AnalysisStatus.CURRENT
        session.commit()

    import os

    _make_noise_mp3(fixture, seed=7)
    stat = fixture.stat()
    os.utime(fixture, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    scan_service.scan()

    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'track.mp3')
        ).scalar_one()
        # ScanService's existing _mark_analysis_stale must invalidate the
        # cached quality analysis too, same as binary_hash/chromaprint.
        assert file.quality_status == AnalysisStatus.STALE
        analyzer.analyze(fixture, file)
        assert file.quality_status == AnalysisStatus.CURRENT
        session.commit()
        file_id = file.id

    with session_maker() as session:
        rows = session.execute(
            select(FileQualityAnalysis)
            .where(FileQualityAnalysis.file_id == file_id)
            .order_by(FileQualityAnalysis.id)
        ).scalars().all()
        # Versioned: the stale re-analysis added a second row rather than
        # overwriting the first.
        assert len(rows) == 2


def test_corrupt_file_gets_integrity_failure_recorded_instead_of_crashing(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    seed_source = config.music_root / '_seed.mp3'
    _make_noise_mp3(seed_source, seed=8)
    data = bytearray(seed_source.read_bytes())
    n = len(data)
    rnd = random.Random(8)
    for i in range(n // 3, 2 * n // 3):
        data[i] = rnd.randint(0, 255)
    corrupt_path = config.music_root / 'corrupt.mp3'
    corrupt_path.write_bytes(bytes(data[: int(n * 0.5)]))
    seed_source.unlink()

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    analyzer = QualityAnalyzer(SubprocessCommandRunner())
    with session_maker() as session:
        file = session.execute(
            select(FileRecord).where(FileRecord.relative_path == 'corrupt.mp3')
        ).scalar_one()

        result = analyzer.analyze(corrupt_path, file)

        assert result.integrity_ok is False
        assert result.quality_score == 0.0
        # Analysis itself succeeded (we have an honest finding) -- this is
        # not the same as an analyzer error, so quality_status is CURRENT.
        assert file.quality_status == AnalysisStatus.CURRENT
        session.commit()
        file_id = file.id

    with session_maker() as session:
        rows = session.execute(
            select(FileQualityAnalysis).where(FileQualityAnalysis.file_id == file_id)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].integrity_status == 'FAILED'
