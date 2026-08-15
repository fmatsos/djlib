from pathlib import Path
from subprocess import CompletedProcess

import pytest

from djlib.db.enums import AnalysisStatus
from djlib.db.models import FileRecord
from djlib.duplicates.chromaprint import (
    ChromaprintError,
    ChromaprintService,
    FingerprintResult,
    fingerprint_similarity,
)

FPCALC_JSON = '{"duration":401.25,"fingerprint":"AQADtEmUaEkSRZEG..."}'


class FakeCommandRunner:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = '') -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> CompletedProcess[str]:
        self.calls.append(list(argv))
        return CompletedProcess(
            args=list(argv), returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def _file_record(**overrides: object) -> FileRecord:
    defaults: dict[str, object] = dict(
        public_id='fil_test', relative_path='track.flac', size_bytes=100, mtime_ns=1, extension='.flac'
    )
    defaults.update(overrides)
    return FileRecord(**defaults)  # type: ignore[arg-type]


def test_parses_fingerprint_and_duration_from_fpcalc_json(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout=FPCALC_JSON)
    service = ChromaprintService(tmp_path, runner)
    file = _file_record()

    result = service.ensure_current(file)

    assert result == FingerprintResult(fingerprint='AQADtEmUaEkSRZEG...', duration_ms=401250)
    assert file.chromaprint == 'AQADtEmUaEkSRZEG...'
    assert file.chromaprint_duration_ms == 401250
    assert file.chromaprint_status == AnalysisStatus.CURRENT
    assert len(runner.calls) == 1
    assert runner.calls[0][0] == 'fpcalc'
    assert '-json' in runner.calls[0]


def test_cache_hit_skips_reinvoking_fpcalc_when_status_stays_current(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout=FPCALC_JSON)
    service = ChromaprintService(tmp_path, runner)
    file = _file_record()

    first = service.ensure_current(file)
    second = service.ensure_current(file)

    assert first == second
    assert len(runner.calls) == 1


def test_stale_status_after_source_signature_change_forces_recompute(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout=FPCALC_JSON)
    service = ChromaprintService(tmp_path, runner)
    file = _file_record()
    service.ensure_current(file)

    # Simulates what ScanService does when (size_bytes, mtime_ns) changes.
    file.chromaprint_status = AnalysisStatus.STALE
    runner.stdout = '{"duration":120.5,"fingerprint":"BRANDNEWFINGERPRINT"}'

    result = service.ensure_current(file)

    assert result == FingerprintResult(fingerprint='BRANDNEWFINGERPRINT', duration_ms=120500)
    assert len(runner.calls) == 2


def test_ensure_current_marks_error_status_on_unparseable_output(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout='not json', returncode=1, stderr='ERROR: could not decode')
    service = ChromaprintService(tmp_path, runner)
    file = _file_record()

    with pytest.raises(ChromaprintError):
        service.ensure_current(file)

    assert file.chromaprint_status == AnalysisStatus.ERROR


def test_fingerprint_similarity_handles_empty_strings() -> None:
    assert fingerprint_similarity(
        FingerprintResult(fingerprint='', duration_ms=1000),
        FingerprintResult(fingerprint='', duration_ms=1000),
    ) == 0.0
    assert fingerprint_similarity(
        FingerprintResult(fingerprint='AQAD', duration_ms=1000),
        FingerprintResult(fingerprint='', duration_ms=1000),
    ) == 0.0


def test_fingerprint_similarity_returns_zero_for_undecodable_fingerprint_instead_of_raising() -> None:
    # "AQADtEmUaEkSRZEG..." is the plan's illustrative fixture text, not a real
    # Chromaprint-compressed fingerprint -- it can't be decoded, and a bad/
    # corrupt fingerprint must not crash a whole calibration/analysis batch.
    result = fingerprint_similarity(
        FingerprintResult(fingerprint='AQADtEmUaEkSRZEG...', duration_ms=401_250),
        FingerprintResult(fingerprint='AQADtEmUaEkSRZEG...', duration_ms=401_250),
    )
    assert result == 0.0


# fingerprint_similarity's real decode+compare behavior (identical real audio
# scores 1.0, distinct real audio scores markedly lower) requires genuine
# fpcalc-generated fingerprints and is covered end-to-end in
# tests/integration/test_analysis_cache.py -- fabricated strings here can't
# exercise the real Chromaprint decoder honestly.
