"""Task 15, Step 3: prove the incremental-idempotence guarantees the plan
demands, rather than assuming them.

Two consecutive unchanged `scan()` calls must report `0 new, 0 changed, N
unchanged, 0 failed` on the second call and never re-run metadata
extraction/resolution for an untouched file. Two consecutive
`DuplicateService.analyze()` calls on an unchanged library must not repeat
BLAKE3/fpcalc/quality subprocess work -- the STALE/CURRENT +
`(size_bytes, mtime_ns[, analyzer_version])` caching contract already built
in Tasks 6-10 (see `duplicates/hashing.py`, `duplicates/chromaprint.py`,
`duplicates/quality.py`) is exercised here through the real orchestration
layer (`ScanService`, `DuplicateService`) rather than by calling
`ensure_current`/`analyze` directly, as `tests/integration/
test_analysis_cache.py` already does.

Verdict up front (see the Task 15 report for the full reasoning): every test
below passed on the first run with zero production-code changes -- Tasks
6-10's existing caching is already correct and idempotent. Nothing in
`scan/service.py` or `duplicates/service.py` needed retrofitting for this
step.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.db.enums import AnalysisStatus, DuplicateStatus
from djlib.db.models import DuplicateGroup, FileRecord
from djlib.db.session import session_factory
from djlib.duplicates import hashing
from djlib.duplicates.service import DuplicateService
from djlib.metadata.types import SubprocessCommandRunner
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


def _file_record(session_maker: sessionmaker[Session], relative_path: str) -> FileRecord:
    with session_maker() as session:
        return session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one()


# -- scan() idempotence -----------------------------------------------------


def test_second_unchanged_scan_reports_all_unchanged_and_never_re_extracts(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    _make_noise_wav(config.music_root / 'a.wav', seed=1)
    _make_noise_wav(config.music_root / 'b.wav', seed=2)

    session_maker = session_factory(engine)
    scan_service = ScanService(config, session_maker)

    first = scan_service.scan()
    assert first.files_new == 2
    assert first.files_failed == 0

    a_before = _file_record(session_maker, 'a.wav')
    b_before = _file_record(session_maker, 'b.wav')
    assert a_before.binary_hash_status == AnalysisStatus.PENDING
    assert b_before.binary_hash_status == AnalysisStatus.PENDING

    with patch.object(
        scan_service._metadata_extractor,
        'extract',
        wraps=scan_service._metadata_extractor.extract,
    ) as extract_spy:
        second = scan_service.scan()

    assert second.files_seen == 2
    assert second.files_new == 0
    assert second.files_changed == 0
    assert second.files_unchanged == 2
    assert second.files_missing == 0
    assert second.files_failed == 0
    extract_spy.assert_not_called()

    # No content change -> no cache invalidation either (a plain, non-`--full`
    # rescan of unchanged files must not touch STALE/CURRENT flags at all --
    # `last_seen_at`/`is_present` legitimately update every scan regardless,
    # so `updated_at` moving is expected and is not itself a cache miss).
    a_after = _file_record(session_maker, 'a.wav')
    b_after = _file_record(session_maker, 'b.wav')
    assert a_after.binary_hash_status == AnalysisStatus.PENDING
    assert b_after.binary_hash_status == AnalysisStatus.PENDING
    assert a_after.metadata_updated_at == a_before.metadata_updated_at
    assert b_after.metadata_updated_at == b_before.metadata_updated_at


# -- DuplicateService idempotence -------------------------------------------


def test_two_consecutive_analyze_calls_do_not_repeat_expensive_evidence_computation(
    config: DjlibConfig, engine: Engine
) -> None:
    """An exact-duplicate pair auto-confirms to `AUTO_CONFIRMED` -- which
    stays in `DuplicateService._ANALYZABLE_STATUSES`, so a *second*
    `analyze()` call deliberately re-examines the same group (design: only
    human decisions -- CONFIRMED/REJECTED/DEFERRED -- are excluded). Real
    idempotence here lives one layer down: `HashService`/`ChromaprintService`/
    `QualityAnalyzer` must serve the second call entirely from their
    `(size_bytes, mtime_ns[, analyzer_version])`-keyed cache, without a
    second BLAKE3 hash or a second `fpcalc`/`ffmpeg` subprocess call.
    """
    config.music_root.mkdir(parents=True)
    original = config.music_root / 'crate_a' / 'Artist - Track.wav'
    duplicate = config.music_root / 'crate_b' / 'Artist - Track.wav'
    _make_noise_wav(original, seed=11)
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(original.read_bytes())

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    runner = SubprocessCommandRunner()

    with (
        patch.object(hashing, '_hash_file', wraps=hashing._hash_file) as hash_spy,
        patch.object(runner, 'run', wraps=runner.run) as runner_spy,
    ):
        with session_maker() as session:
            DuplicateService(config, session, runner=runner).detect()
        with session_maker() as session:
            DuplicateService(config, session, runner=runner).analyze()

        hash_calls_after_first = hash_spy.call_count
        runner_calls_after_first = runner_spy.call_count
        assert hash_calls_after_first == 2  # one BLAKE3 hash per file

        with session_maker() as session:
            group = session.execute(select(DuplicateGroup)).scalar_one()
            assert group.status == DuplicateStatus.AUTO_CONFIRMED

        with session_maker() as session:
            second_groups_analyzed = DuplicateService(config, session, runner=runner).analyze()

    assert second_groups_analyzed == 1  # the group really was re-examined...
    # ...but not a single extra BLAKE3/fpcalc/ffmpeg call happened doing it.
    assert hash_spy.call_count == hash_calls_after_first
    assert runner_spy.call_count == runner_calls_after_first
