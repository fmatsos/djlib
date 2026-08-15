from dataclasses import dataclass

from djlib.config import ChromaprintThresholds
from djlib.db.enums import PairClassification
from djlib.duplicates.types import MetadataEvidence, VersionCompatibilityState


@dataclass(frozen=True)
class PairDecision:
    """One pairwise classification outcome (design §15-19).

    `reasons` is never empty: every automatic conclusion must be traceable to
    stored evidence (design invariant #10), not a bare confidence number --
    `catalog inspect`/Task 12's report both need this trail.
    """

    classification: PairClassification
    confidence: float
    reasons: tuple[str, ...]


class PairClassifier:
    """Classifies one candidate file pair into a `PairClassification` (design §17-19).

    Thresholds are injected (`ChromaprintThresholds`, see `djlib.config`)
    rather than hardcoded, so production config and tests can vary them
    explicitly -- see `config.example.toml`'s `[duplicates.chromaprint]`
    section and `.claude/rules/duplicate-detection.md`.

    Decision order (binary hash is authoritative and checked first; every
    other branch depends on `MetadataEvidence.version_compatibility`, never
    on the composite `metadata_similarity` number alone -- design §15's own
    invariant, see `duplicates/types.py::MetadataEvidence`):

    1. identical BLAKE3 binary hash                          -> EXACT
    2. version/edition INCOMPATIBLE and similarity >= review_floor
       (audio unexpectedly similar despite conflicting metadata) -> CONFLICT
    3. version/edition compatible and similarity >= auto_equivalent -> AUDIO_EQUIVALENT
    4. similarity >= review_floor (plausible, not confident enough) -> PROBABLE
    5. otherwise                                              -> DIFFERENT
    """

    def __init__(self, thresholds: ChromaprintThresholds | None = None) -> None:
        self._thresholds = thresholds or ChromaprintThresholds()

    def classify(
        self,
        evidence: MetadataEvidence,
        binary_hash_equal: bool,
        chromaprint_similarity: float | None,
    ) -> PairDecision:
        if binary_hash_equal:
            return PairDecision(
                classification=PairClassification.EXACT,
                confidence=1.0,
                reasons=('identical BLAKE3 binary hash (design §17)',),
            )

        similarity = chromaprint_similarity if chromaprint_similarity is not None else 0.0
        version_incompatible = (
            evidence.version_compatibility.state == VersionCompatibilityState.INCOMPATIBLE
        )
        thresholds = self._thresholds

        if version_incompatible and similarity >= thresholds.review_floor:
            return PairDecision(
                classification=PairClassification.CONFLICT,
                confidence=similarity,
                reasons=(
                    'explicit version/edition metadata conflict (design §19: '
                    'strong negative evidence)',
                    f'chromaprint similarity {similarity:.4f} is unexpectedly high for '
                    'conflicting metadata -- requires human review, never a silent merge',
                ),
            )

        if not version_incompatible and similarity >= thresholds.auto_equivalent:
            return PairDecision(
                classification=PairClassification.AUDIO_EQUIVALENT,
                confidence=similarity,
                reasons=(
                    f'chromaprint similarity {similarity:.4f} >= auto_equivalent threshold '
                    f'({thresholds.auto_equivalent})',
                    'version/edition metadata compatible',
                ),
            )

        if similarity >= thresholds.review_floor:
            return PairDecision(
                classification=PairClassification.PROBABLE,
                confidence=similarity,
                reasons=(
                    f'chromaprint similarity {similarity:.4f} is between review_floor '
                    f'({thresholds.review_floor}) and auto_equivalent '
                    f'({thresholds.auto_equivalent}) -- plausible but not confident enough '
                    'for automatic merge',
                ),
            )

        reasons = [
            f'chromaprint similarity {similarity:.4f} is below review_floor '
            f'({thresholds.review_floor})'
        ]
        if chromaprint_similarity is None:
            reasons.append('no chromaprint fingerprint was available for comparison')
        if version_incompatible:
            reasons.append('version/edition metadata also conflicts')
        return PairDecision(
            classification=PairClassification.DIFFERENT,
            confidence=1.0 - similarity,
            reasons=tuple(reasons),
        )
