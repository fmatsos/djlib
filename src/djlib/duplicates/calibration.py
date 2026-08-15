import csv
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from djlib.db.models import FileRecord
from djlib.duplicates.blocking import CandidateBlocker
from djlib.duplicates.chromaprint import ChromaprintService, fingerprint_similarity
from djlib.duplicates.hashing import HashService
from djlib.duplicates.types import CandidatePair

CALIBRATION_FIELDS = (
    'left_public_id',
    'left_relative_path',
    'right_public_id',
    'right_relative_path',
    'blocking_tier',
    'binary_hash_equal',
    'chromaprint_similarity',
    'duration_delta_ms',
    'version_compatibility',
)


@dataclass(frozen=True)
class CalibrationRow:
    """One reviewable data point for `djlib duplicates calibrate` (design §18).

    Purely descriptive: nothing here is a threshold, and producing it never
    writes `duplicate_groups`/`duplicate_pair_evidence` (Task 10 owns that
    persistence) or rewrites any config/threshold value -- a human reviews
    this data and decides what, if anything, to recalibrate.
    """

    left_public_id: str
    left_relative_path: str
    right_public_id: str
    right_relative_path: str
    blocking_tier: str
    binary_hash_equal: bool
    chromaprint_similarity: float | None
    duration_delta_ms: int | None
    version_compatibility: str


def collect_calibration_rows(
    session: Session,
    hash_service: HashService,
    chromaprint_service: ChromaprintService,
) -> list[CalibrationRow]:
    """Blocked candidate pairs (Task 7) annotated with hash/fingerprint evidence.

    Read-only reporting: mutates only the per-file cached `binary_hash*` /
    `chromaprint*` columns that `HashService`/`ChromaprintService` already
    manage (the caller commits that cache), and never touches duplicate group
    or pairwise-evidence tables.
    """
    files = {
        record.id: record
        for record in session.execute(
            select(FileRecord).where(FileRecord.is_present.is_(True))
        ).scalars()
    }
    blocker = CandidateBlocker(session)

    seen_pairs: set[tuple[int, int]] = set()
    rows: list[CalibrationRow] = []

    for file_id in files:
        for pair in blocker.find_candidates(file_id):
            key = (min(pair.left_file_id, pair.right_file_id), max(pair.left_file_id, pair.right_file_id))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            rows.append(_evaluate_pair(files, pair, hash_service, chromaprint_service))

    return rows


def _evaluate_pair(
    files: dict[int, FileRecord],
    pair: CandidatePair,
    hash_service: HashService,
    chromaprint_service: ChromaprintService,
) -> CalibrationRow:
    left = files[pair.left_file_id]
    right = files[pair.right_file_id]

    left_hash = hash_service.ensure_current(left)
    right_hash = hash_service.ensure_current(right)
    binary_hash_equal = left_hash == right_hash

    chromaprint_sim: float | None = None
    if not binary_hash_equal:
        # design §18: only fingerprint once the binary hash has ruled out an
        # exact copy -- an identical hash already means EXACT, fingerprinting
        # it would be redundant work for a report row that already says so.
        left_fp = chromaprint_service.ensure_current(left)
        right_fp = chromaprint_service.ensure_current(right)
        chromaprint_sim = fingerprint_similarity(left_fp, right_fp)

    return CalibrationRow(
        left_public_id=left.public_id,
        left_relative_path=left.relative_path,
        right_public_id=right.public_id,
        right_relative_path=right.relative_path,
        blocking_tier=pair.tier.value,
        binary_hash_equal=binary_hash_equal,
        chromaprint_similarity=chromaprint_sim,
        duration_delta_ms=pair.evidence.duration_delta_ms,
        version_compatibility=pair.evidence.version_compatibility.state.value,
    )


def write_calibration_csv(rows: Iterable[CalibrationRow], stream: TextIO) -> None:
    writer = csv.writer(stream)
    writer.writerow(CALIBRATION_FIELDS)
    for row in rows:
        writer.writerow(
            [
                row.left_public_id,
                row.left_relative_path,
                row.right_public_id,
                row.right_relative_path,
                row.blocking_tier,
                row.binary_hash_equal,
                '' if row.chromaprint_similarity is None else f'{row.chromaprint_similarity:.4f}',
                '' if row.duration_delta_ms is None else row.duration_delta_ms,
                row.version_compatibility,
            ]
        )


def write_calibration_json(rows: Iterable[CalibrationRow], stream: TextIO) -> None:
    json.dump([asdict(row) for row in rows], stream, indent=2)
    stream.write('\n')
