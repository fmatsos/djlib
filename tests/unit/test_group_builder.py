from djlib.db.enums import DuplicateStatus, PairClassification
from djlib.duplicates.classifier import PairDecision
from djlib.duplicates.groups import ClassifiedPair, DuplicateGroupBuilder, connected_components


def _pair(
    left: int, right: int, classification: PairClassification, confidence: float = 0.9
) -> ClassifiedPair:
    return ClassifiedPair(
        left_file_id=left,
        right_file_id=right,
        decision=PairDecision(
            classification=classification, confidence=confidence, reasons=('test evidence',)
        ),
    )


def test_connected_components_groups_transitively_linked_files() -> None:
    components = connected_components([(1, 2), (2, 3), (4, 5)])
    assert sorted(components) == [[1, 2, 3], [4, 5]]


def test_non_transitive_conflict_forces_review_required_never_auto_confirmed() -> None:
    # A-B AUDIO_EQUIVALENT, B-C PROBABLE, A-C DIFFERENT: naive transitive
    # closure from A-B alone would wrongly suggest confidence: design §16
    # requires REVIEW_REQUIRED for the whole component instead.
    pairs = [
        _pair(1, 2, PairClassification.AUDIO_EQUIVALENT),
        _pair(2, 3, PairClassification.PROBABLE),
        _pair(1, 3, PairClassification.DIFFERENT),
    ]

    drafts = DuplicateGroupBuilder().build(pairs)

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.file_ids == (1, 2, 3)
    assert draft.status == DuplicateStatus.REVIEW_REQUIRED
    assert draft.status != DuplicateStatus.AUTO_CONFIRMED
    assert draft.reasons


def test_all_exact_or_audio_equivalent_edges_are_auto_confirmed() -> None:
    pairs = [
        _pair(1, 2, PairClassification.EXACT),
        _pair(2, 3, PairClassification.AUDIO_EQUIVALENT),
    ]

    drafts = DuplicateGroupBuilder().build(pairs)

    assert len(drafts) == 1
    assert drafts[0].status == DuplicateStatus.AUTO_CONFIRMED
    assert drafts[0].file_ids == (1, 2, 3)


def test_all_probable_edges_are_review_required_not_dropped_not_auto_confirmed() -> None:
    pairs = [_pair(1, 2, PairClassification.PROBABLE)]

    drafts = DuplicateGroupBuilder().build(pairs)

    assert len(drafts) == 1
    assert drafts[0].status == DuplicateStatus.REVIEW_REQUIRED


def test_a_single_conflict_edge_forces_review_required_even_amid_strong_edges() -> None:
    pairs = [
        _pair(1, 2, PairClassification.EXACT),
        _pair(2, 3, PairClassification.CONFLICT),
    ]

    drafts = DuplicateGroupBuilder().build(pairs)

    assert len(drafts) == 1
    assert drafts[0].status == DuplicateStatus.REVIEW_REQUIRED


def test_disconnected_pairs_produce_separate_independent_groups() -> None:
    pairs = [
        _pair(1, 2, PairClassification.EXACT),
        _pair(3, 4, PairClassification.AUDIO_EQUIVALENT),
    ]

    drafts = DuplicateGroupBuilder().build(pairs)

    assert len(drafts) == 2
    statuses = {draft.file_ids: draft.status for draft in drafts}
    assert statuses[(1, 2)] == DuplicateStatus.AUTO_CONFIRMED
    assert statuses[(3, 4)] == DuplicateStatus.AUTO_CONFIRMED


def test_group_confidence_is_the_weakest_edge_not_the_strongest() -> None:
    pairs = [
        _pair(1, 2, PairClassification.EXACT, confidence=1.0),
        _pair(2, 3, PairClassification.AUDIO_EQUIVALENT, confidence=0.5),
    ]

    drafts = DuplicateGroupBuilder().build(pairs)

    assert drafts[0].confidence == 0.5
