import json
from dataclasses import dataclass
from pathlib import Path

# `chromaprint` here is pyacoustid's ctypes binding module (site-packages/chromaprint.py,
# installed as a dependency of the `pyacoustid` package -- see pyproject.toml), NOT this
# file. Python 3's absolute imports mean this doesn't self-collide even though this
# module is also named `chromaprint` (djlib.duplicates.chromaprint).
import chromaprint

from djlib.db.enums import AnalysisStatus
from djlib.db.models import FileRecord
from djlib.metadata.types import CommandRunner


class ChromaprintError(Exception):
    """fpcalc failed or produced output that could not be parsed for a file."""


@dataclass(frozen=True)
class FingerprintResult:
    fingerprint: str
    duration_ms: int


class ChromaprintService:
    """Targeted Chromaprint fingerprinting for duplicate-candidate files (design §18).

    `ensure_current` only computes-if-needed: it does **not** enforce design
    §18's policy of "only fingerprint when the binary hash differs AND
    metadata/duration remain plausible". That gating is deliberately left to
    the caller (Task 10's `duplicates analyze` orchestration decides *whether*
    a given pair is worth fingerprinting at all, typically after comparing
    `HashService` results first) -- baking it in here would make this service
    reach into pairwise blocking/hash-comparison concerns it has no business
    owning. Task 10's author: this is the one place that decision belongs.

    Caching contract mirrors `HashService`: `chromaprint_status == CURRENT` is
    the sole signal that `chromaprint`/`chromaprint_duration_ms` match the
    file's current content, with the same "no analyzer_version column, invalidate
    via ScanService" reasoning documented there.
    """

    def __init__(self, music_root: Path, runner: CommandRunner) -> None:
        self._music_root = music_root
        self._runner = runner

    def ensure_current(self, file: FileRecord) -> FingerprintResult:
        if (
            file.chromaprint_status == AnalysisStatus.CURRENT
            and file.chromaprint is not None
            and file.chromaprint_duration_ms is not None
        ):
            return FingerprintResult(
                fingerprint=file.chromaprint, duration_ms=file.chromaprint_duration_ms
            )

        path = self._music_root / file.relative_path
        result = self._runner.run(['fpcalc', '-json', str(path)])

        try:
            parsed = json.loads(result.stdout)
            fingerprint = parsed['fingerprint']
            if not isinstance(fingerprint, str) or not fingerprint:
                raise ValueError('empty fingerprint')
            duration_ms = round(float(parsed['duration']) * 1000)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            file.chromaprint_status = AnalysisStatus.ERROR
            raise ChromaprintError(
                f'fpcalc produced invalid output for {file.relative_path}: {result.stderr.strip()}'
            ) from exc

        file.chromaprint = fingerprint
        file.chromaprint_duration_ms = duration_ms
        file.chromaprint_status = AnalysisStatus.CURRENT
        return FingerprintResult(fingerprint=fingerprint, duration_ms=duration_ms)


# Chromaprint's own similarity-scoring parameters (same values pyacoustid uses
# internally): how many subfingerprint-array positions to search for the best
# alignment offset, and how many mismatched bits between two 32-bit
# subfingerprints still count as "matching" at that offset.
_MAX_ALIGN_OFFSET = 120
_MAX_BIT_ERROR = 2


def _decode(fingerprint: str) -> list[int]:
    # pyacoustid's ctypes binding expects bytes, not str -- passing the str
    # fpcalc/json.loads gives us raises `ctypes.ArgumentError: wrong type`.
    raw, _algorithm = chromaprint.decode_fingerprint(fingerprint.encode('ascii'))
    return [int(value) for value in raw]


def _popcount(value: int) -> int:
    return bin(value).count('1')


def _hamming_match_score(left: list[int], right: list[int]) -> float:
    """Best-alignment bit-agreement score between two decoded subfingerprint arrays.

    This is the standard Chromaprint/AcoustID comparison algorithm: for every
    candidate alignment offset within `_MAX_ALIGN_OFFSET`, count how many
    subfingerprint pairs agree within `_MAX_BIT_ERROR` bits, then take the
    best-scoring offset. Vendored as a small, readable local copy (rather than
    depending on pyacoustid's private `_match_fingerprints`) since it's short,
    stable, and documented at
    https://essentia.upf.edu/tutorial_fingerprinting_chromaprint.html.
    """
    left_size, right_size = len(left), len(right)
    if left_size == 0 or right_size == 0:
        return 0.0
    counts = [0] * (left_size + right_size + 1)
    for i in range(left_size):
        for j in range(max(0, i - _MAX_ALIGN_OFFSET), min(right_size, i + _MAX_ALIGN_OFFSET)):
            if _popcount(left[i] ^ right[j]) <= _MAX_BIT_ERROR:
                counts[i - j + right_size] += 1
    return max(counts) / min(left_size, right_size)


def fingerprint_similarity(left: FingerprintResult, right: FingerprintResult) -> float:
    """Similarity in [0.0, 1.0] between two stored Chromaprint fingerprints.

    `fpcalc -json` (confirmed on this system, fpcalc 1.5.1) returns
    `fingerprint` as Chromaprint's *compressed* textual encoding (e.g.
    `"AQADtEmUaEkSRZEG..."`), not the raw 32-bit subfingerprint integer array.
    Decoding that compressed form correctly requires the actual Chromaprint
    algorithm, not a text-similarity proxy over the compressed bytes (an
    earlier version of this function used RapidFuzz edit-distance over the
    compressed strings as a fallback, reasoning that no decoder was available
    -- that reasoning was wrong: `libchromaprint.so.1` is present on this
    system and `pyacoustid` (its `chromaprint` submodule, imported above)
    binds to it via ctypes to decode a fingerprint back into its real raw
    subfingerprint integer array. `pyacoustid` is now a project dependency
    (see pyproject.toml) specifically for this decode step; its network/
    AcoustID-API surface (`acoustid.lookup`, `submit`, etc.) is never used or
    imported by djlib. `pyacoustid.compare_fingerprints` itself has a bug for
    this use case -- it passes the `str` fingerprint straight to the ctypes
    decode call, which requires `bytes` and raises `ArgumentError` -- so this
    function decodes directly (`.encode('ascii')` first) and scores the two
    decoded integer arrays with a small local copy of the same
    alignment-search Hamming-agreement algorithm pyacoustid uses internally
    (see `_hamming_match_score`), rather than depending on its broken
    convenience wrapper.

    A fingerprint that fails to decode (e.g. malformed/truncated data) is
    treated as "no similarity" (0.0) rather than raising -- per-pair fault
    isolation matches the project's established pattern (see Task 4's
    per-file extraction errors) rather than letting one bad fingerprint crash
    a whole calibration/analysis batch.

    Verified empirically against real ffmpeg-generated audio in
    tests/integration/test_analysis_cache.py: two encodings of the *same*
    signal score 1.0, distinct real audio scores markedly lower.
    """
    if not left.fingerprint or not right.fingerprint:
        return 0.0
    try:
        left_ints = _decode(left.fingerprint)
        right_ints = _decode(right.fingerprint)
    except Exception:
        return 0.0
    return _hamming_match_score(left_ints, right_ints)
