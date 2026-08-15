from pathlib import Path
from unittest.mock import patch

import pytest
from blake3 import blake3

from djlib.db.enums import AnalysisStatus
from djlib.db.models import FileRecord
from djlib.duplicates import hashing
from djlib.duplicates.hashing import HashingError, HashService


def _file_record(relative_path: str, size_bytes: int, mtime_ns: int) -> FileRecord:
    return FileRecord(
        public_id='fil_test',
        relative_path=relative_path,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        extension=Path(relative_path).suffix,
    )


def _deterministic_content(size: int) -> bytes:
    # Deterministic "random-ish" content -- no need for cryptographic
    # randomness, just non-repeating bytes so the digest is meaningful.
    return bytes((i * 2654435761) % 256 for i in range(size))


def test_byte_identical_files_produce_the_same_blake3_digest(tmp_path: Path) -> None:
    # Larger than the 1 MiB chunk size so the streaming reader crosses a
    # chunk boundary and still produces the correct digest.
    content = _deterministic_content(2 * 1024 * 1024 + 17)
    left_path = tmp_path / 'left.mp3'
    right_path = tmp_path / 'right.mp3'
    left_path.write_bytes(content)
    right_path.write_bytes(content)

    service = HashService(tmp_path)
    left = _file_record('left.mp3', left_path.stat().st_size, left_path.stat().st_mtime_ns)
    right = _file_record('right.mp3', right_path.stat().st_size, right_path.stat().st_mtime_ns)

    left_digest = service.ensure_current(left)
    right_digest = service.ensure_current(right)

    assert left_digest == right_digest
    assert left_digest == blake3(content).hexdigest()
    assert left.binary_hash == left_digest
    assert left.binary_hash_status == AnalysisStatus.CURRENT


def test_ensure_current_only_hashes_once_across_two_calls_when_cache_stays_current(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'track.mp3'
    path.write_bytes(b'some audio bytes')
    file = _file_record('track.mp3', path.stat().st_size, path.stat().st_mtime_ns)
    service = HashService(tmp_path)

    with patch.object(hashing, '_hash_file', wraps=hashing._hash_file) as spy:
        first = service.ensure_current(file)
        second = service.ensure_current(file)

    assert first == second
    assert spy.call_count == 1
    assert file.binary_hash_status == AnalysisStatus.CURRENT


@pytest.mark.parametrize('status', [AnalysisStatus.PENDING, AnalysisStatus.STALE, AnalysisStatus.ERROR])
def test_ensure_current_recomputes_for_every_non_current_status(
    tmp_path: Path, status: AnalysisStatus
) -> None:
    path = tmp_path / 'track.mp3'
    path.write_bytes(b'content')
    file = _file_record('track.mp3', path.stat().st_size, path.stat().st_mtime_ns)
    file.binary_hash_status = status

    service = HashService(tmp_path)
    with patch.object(hashing, '_hash_file', wraps=hashing._hash_file) as spy:
        service.ensure_current(file)

    assert spy.call_count == 1
    assert file.binary_hash_status == AnalysisStatus.CURRENT


def test_ensure_current_marks_error_status_and_raises_on_missing_file(tmp_path: Path) -> None:
    file = _file_record('missing.mp3', size_bytes=1, mtime_ns=1)
    service = HashService(tmp_path)

    with pytest.raises(HashingError):
        service.ensure_current(file)

    assert file.binary_hash_status == AnalysisStatus.ERROR
