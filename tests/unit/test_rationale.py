from djlib.db.enums import PairClassification
from djlib.db.models import DuplicatePairEvidence
from djlib.duplicates.rationale import group_reasons


def _pair(classification: PairClassification) -> DuplicatePairEvidence:
    return DuplicatePairEvidence(classification=classification)


def test_no_evidence_reason() -> None:
    assert group_reasons([]) == ['no pairwise evidence available (fewer than two analyzed files)']


def test_all_exact_or_audio_equivalent_reason() -> None:
    reasons = group_reasons(
        [_pair(PairClassification.EXACT), _pair(PairClassification.AUDIO_EQUIVALENT)]
    )
    assert reasons == ['every pairwise classification in this group is EXACT or AUDIO_EQUIVALENT']


def test_probable_reason() -> None:
    reasons = group_reasons([_pair(PairClassification.EXACT), _pair(PairClassification.PROBABLE)])
    assert reasons == [
        'at least one PROBABLE pair -- plausible but not confident enough '
        'for automatic consolidation'
    ]


def test_conflicting_evidence_reason() -> None:
    reasons = group_reasons([_pair(PairClassification.EXACT), _pair(PairClassification.CONFLICT)])
    assert reasons == [
        'conflicting or contradictory pairwise evidence within this group '
        '(design Sec.16: never rely on naive transitive closure)'
    ]


def test_different_classification_counts_as_inconsistent() -> None:
    reasons = group_reasons([_pair(PairClassification.DIFFERENT)])
    assert reasons == [
        'conflicting or contradictory pairwise evidence within this group '
        '(design Sec.16: never rely on naive transitive closure)'
    ]
