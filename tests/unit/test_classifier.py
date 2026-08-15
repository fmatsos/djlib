from djlib.config import ChromaprintThresholds
from djlib.db.enums import PairClassification
from djlib.duplicates.classifier import PairClassifier
from djlib.duplicates.similarity import metadata_similarity
from djlib.duplicates.types import MetadataEvidence, TrackIdentitySnapshot, VersionCompatibilityState


def _thresholds() -> ChromaprintThresholds:
    # Deliberately different from the production defaults (0.985/0.93) so
    # these tests can never pass by accidentally matching hardcoded numbers
    # -- every assertion below derives its expected value from this object.
    return ChromaprintThresholds(auto_equivalent=0.9, review_floor=0.6)


def _snapshot(
    version: str | None = None, duration_ms: int = 300_000
) -> TrackIdentitySnapshot:
    return TrackIdentitySnapshot(
        artist_normalized='artist',
        title_normalized='title',
        version_normalized=version,
        edition_normalized=None,
        duration_ms=duration_ms,
        featured_artist_normalized_names=(),
    )


def _evidence(
    left_version: str | None = None, right_version: str | None = None
) -> MetadataEvidence:
    return metadata_similarity(_snapshot(left_version), _snapshot(right_version))


def test_same_binary_hash_is_exact_regardless_of_fingerprint_similarity() -> None:
    thresholds = _thresholds()
    classifier = PairClassifier(thresholds)

    decision = classifier.classify(
        evidence=_evidence(), binary_hash_equal=True, chromaprint_similarity=0.0
    )

    assert decision.classification == PairClassification.EXACT
    assert decision.confidence == 1.0
    assert decision.reasons


def test_high_fingerprint_similarity_with_compatible_metadata_is_audio_equivalent() -> None:
    thresholds = _thresholds()
    classifier = PairClassifier(thresholds)

    decision = classifier.classify(
        evidence=_evidence(),
        binary_hash_equal=False,
        chromaprint_similarity=thresholds.auto_equivalent,
    )

    assert decision.classification == PairClassification.AUDIO_EQUIVALENT
    assert decision.confidence == thresholds.auto_equivalent
    assert decision.reasons


def test_intermediate_fingerprint_similarity_is_probable() -> None:
    thresholds = _thresholds()
    classifier = PairClassifier(thresholds)
    midpoint = (thresholds.review_floor + thresholds.auto_equivalent) / 2

    decision = classifier.classify(
        evidence=_evidence(), binary_hash_equal=False, chromaprint_similarity=midpoint
    )

    assert decision.classification == PairClassification.PROBABLE
    assert decision.confidence == midpoint


def test_low_fingerprint_similarity_is_different() -> None:
    thresholds = _thresholds()
    classifier = PairClassifier(thresholds)
    below_floor = thresholds.review_floor / 2

    decision = classifier.classify(
        evidence=_evidence(), binary_hash_equal=False, chromaprint_similarity=below_floor
    )

    assert decision.classification == PairClassification.DIFFERENT


def test_high_fingerprint_similarity_with_explicit_version_conflict_is_conflict() -> None:
    thresholds = _thresholds()
    classifier = PairClassifier(thresholds)
    evidence = _evidence(left_version='Original Mix', right_version='Extended Mix')
    assert evidence.version_compatibility.state == VersionCompatibilityState.INCOMPATIBLE

    decision = classifier.classify(
        evidence=evidence,
        binary_hash_equal=False,
        chromaprint_similarity=thresholds.auto_equivalent,
    )

    assert decision.classification == PairClassification.CONFLICT
    assert decision.confidence == thresholds.auto_equivalent
    assert decision.reasons


def test_conflicting_metadata_with_low_similarity_is_different_not_conflict() -> None:
    thresholds = _thresholds()
    classifier = PairClassifier(thresholds)
    evidence = _evidence(left_version='Original Mix', right_version='Extended Mix')
    below_floor = thresholds.review_floor / 2

    decision = classifier.classify(
        evidence=evidence, binary_hash_equal=False, chromaprint_similarity=below_floor
    )

    assert decision.classification == PairClassification.DIFFERENT


def test_missing_chromaprint_similarity_defaults_conservatively_to_different() -> None:
    thresholds = _thresholds()
    classifier = PairClassifier(thresholds)

    decision = classifier.classify(
        evidence=_evidence(), binary_hash_equal=False, chromaprint_similarity=None
    )

    assert decision.classification == PairClassification.DIFFERENT


def test_thresholds_default_to_chromaprint_thresholds_defaults_when_omitted() -> None:
    classifier = PairClassifier()
    default_thresholds = ChromaprintThresholds()

    decision = classifier.classify(
        evidence=_evidence(),
        binary_hash_equal=False,
        chromaprint_similarity=default_thresholds.auto_equivalent,
    )

    assert decision.classification == PairClassification.AUDIO_EQUIVALENT
