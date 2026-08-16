import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from djlib.config import DjlibConfig
from djlib.db.session import session_factory
from djlib.duplicates import service as duplicate_service
from djlib.duplicates.service import DuplicateService
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


def _make_exact_duplicate_pair(music_root: Path, name: str, seed: int) -> None:
    filename = f'Test Artist - {name}.wav'
    original = music_root / name / 'crate_a' / filename
    duplicate = music_root / name / 'crate_b' / filename
    _make_noise_wav(original, seed=seed)
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(original.read_bytes())


def test_analyze_commits_progress_in_batches_instead_of_one_commit_for_the_whole_run(
    config: DjlibConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors the scan-batching fix: `analyze()` previously computed BLAKE3/
    Chromaprint/ffmpeg quality evidence for every DETECTED group and only
    called `session.commit()` once, at the very end -- holding every new
    `DuplicatePairEvidence`/`FileQualityAnalysis` row (and every touched
    `FileRecord`) live in memory for the whole run. On a library with many
    duplicate groups this grows memory unboundedly for the run's entire,
    possibly hours-long, duration. Committing per group (or every few groups)
    bounds it."""
    config.music_root.mkdir(parents=True)
    _make_exact_duplicate_pair(config.music_root, 'pair-a', seed=1)
    _make_exact_duplicate_pair(config.music_root, 'pair-b', seed=2)

    monkeypatch.setattr(duplicate_service, '_ANALYZE_COMMIT_BATCH_SIZE', 1)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        service = DuplicateService(config, session)
        groups_detected = service.detect()
        assert groups_detected == 2

        commit = MagicMock(wraps=session.commit)
        monkeypatch.setattr(session, 'commit', commit)

        service.analyze()

    # One commit per analyzed group (batch size 1) plus the loop's own final
    # commit -- proof that progress lands in the database incrementally
    # rather than accumulating uncommitted for the whole run.
    assert commit.call_count >= 2
