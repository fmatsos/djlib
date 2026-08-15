from pathlib import Path

from blake3 import blake3

from djlib.db.enums import AnalysisStatus
from djlib.db.models import FileRecord

_CHUNK_SIZE_BYTES = 1024 * 1024


class HashingError(Exception):
    """A file could not be read to compute its BLAKE3 hash."""


class HashService:
    """Targeted BLAKE3 binary hashing for duplicate-candidate files (design §17).

    This service never runs library-wide: `ensure_current` is only ever meant
    to be called for files a caller has already identified as duplicate
    candidates (Task 10's `duplicates analyze` orchestration, or Task 8's own
    `duplicates calibrate`). A plain `djlib scan` must never call this.

    Caching contract: `FileRecord.binary_hash_status == CURRENT` is the *sole*
    signal that `FileRecord.binary_hash` matches the file's current on-disk
    content. There is no `analyzer_version` column for this field (unlike
    `FileQualityAnalysis`, see Task 9) -- the contract instead depends entirely
    on `ScanService` being the only writer of `size_bytes`/`mtime_ns`, and on
    it always flipping `binary_hash_status` to STALE in the same transaction
    it changes those fields (or forces a full re-extraction). A brand-new
    file's status defaults to PENDING at the ORM level, which this service
    treats identically to STALE and ERROR: all three mean "(re)compute now".
    Only CURRENT means "reuse the cached value".
    """

    def __init__(self, music_root: Path) -> None:
        self._music_root = music_root

    def ensure_current(self, file: FileRecord) -> str:
        if file.binary_hash_status == AnalysisStatus.CURRENT and file.binary_hash is not None:
            return file.binary_hash

        path = self._music_root / file.relative_path
        try:
            digest = _hash_file(path)
        except OSError as exc:
            file.binary_hash_status = AnalysisStatus.ERROR
            raise HashingError(f'failed to hash {file.relative_path}: {exc}') from exc

        file.binary_hash = digest
        file.binary_hash_status = AnalysisStatus.CURRENT
        return digest


def _hash_file(path: Path) -> str:
    """Stream `path` through BLAKE3 in fixed-size chunks -- never load it whole."""
    hasher = blake3()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE_BYTES), b''):
            hasher.update(chunk)
    return hasher.hexdigest()
