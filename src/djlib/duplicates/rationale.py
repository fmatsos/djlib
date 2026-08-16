"""Shared, read-only duplicate-group rationale helpers.

`group_reasons` (design §16's group-status rationale) and
`preferred_choice_from_persisted` (design §21's preferred-file selection,
recomputed from already-persisted `FileQualityAnalysis` rows rather than
re-running quality analysis -- see `report/generator.py`'s module docstring
for the full "recomputed, not re-run" rationale) were each copy-pasted
between `cli.py` and `report/generator.py`; this module is their single
shared home now that a third read-only caller (`duplicates/export.py`) needs
the same logic.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from djlib.db.enums import PairClassification
from djlib.db.models import DuplicatePairEvidence, FileQualityAnalysis, FileRecord
from djlib.duplicates.preferred import PreferredCandidate, PreferredChoice, PreferredFileSelector
from djlib.duplicates.quality import QualityResult

# design §16 (`DuplicateGroupBuilder._draft`): a group's own status already IS
# the classification outcome, but a human reviewer also needs the *rationale*
# for that status, which was never itself persisted as a column on
# `DuplicateGroup`.
_INCONSISTENT_CLASSIFICATIONS = frozenset(
    {PairClassification.DIFFERENT, PairClassification.CONFLICT}
)


def group_reasons(pair_rows: list[DuplicatePairEvidence]) -> list[str]:
    classifications = {row.classification for row in pair_rows}
    if classifications & _INCONSISTENT_CLASSIFICATIONS:
        return [
            'conflicting or contradictory pairwise evidence within this group '
            '(design Sec.16: never rely on naive transitive closure)'
        ]
    if PairClassification.PROBABLE in classifications:
        return [
            'at least one PROBABLE pair -- plausible but not confident enough '
            'for automatic consolidation'
        ]
    if classifications:
        return ['every pairwise classification in this group is EXACT or AUDIO_EQUIVALENT']
    return ['no pairwise evidence available (fewer than two analyzed files)']


def _quality_result_from_row(row: FileQualityAnalysis) -> QualityResult:
    """Reconstructs a `QualityResult` from an already-persisted
    `FileQualityAnalysis` row -- mirrors `duplicates/quality.py`'s private
    `_cached_result` helper, kept as its own copy rather than importing a
    private function across modules or widening `quality.py`'s public
    surface for a use case outside its own scope.
    """
    details = row.details_json or {}
    return QualityResult(
        integrity_ok=row.integrity_status == 'OK',
        lossless=row.lossless_status == 'LOSSLESS',
        transcode_suspicion=row.transcode_suspicion,
        clipping_detected=row.clipping_status == 'CLIPPED',
        audio_quality_score=details.get('audio_quality_score', 0.0),
        metadata_completeness=details.get('metadata_completeness', 0.0),
        quality_score=row.quality_score if row.quality_score is not None else 0.0,
        details=details,
    )


def preferred_choice_from_persisted(
    session: Session, files: Iterable[FileRecord]
) -> PreferredChoice | None:
    """Reconstructs preferred-file rationale from already-persisted
    `FileQualityAnalysis` rows only -- never re-runs quality analysis (no
    ffmpeg invocation, no new `FileQualityAnalysis` row), so it is safe to
    call from purely read-only reporting code.
    """
    candidates: list[PreferredCandidate] = []
    for file in files:
        row = session.execute(
            select(FileQualityAnalysis)
            .where(FileQualityAnalysis.file_id == file.id)
            .order_by(FileQualityAnalysis.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            continue
        candidates.append(PreferredCandidate(file_id=file.id, quality=_quality_result_from_row(row)))
    if not candidates:
        return None
    return PreferredFileSelector().choose(candidates)
