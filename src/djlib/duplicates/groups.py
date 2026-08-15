from dataclasses import dataclass

from djlib.db.enums import DuplicateStatus, PairClassification
from djlib.duplicates.classifier import PairDecision

_INCONSISTENT = frozenset({PairClassification.DIFFERENT, PairClassification.CONFLICT})


@dataclass(frozen=True)
class ClassifiedPair:
    """One already-classified candidate pair, at FILE granularity."""

    left_file_id: int
    right_file_id: int
    decision: PairDecision


@dataclass(frozen=True)
class DuplicateGroupDraft:
    """A proposed duplicate group with a status derived from *every* internal edge."""

    file_ids: tuple[int, ...]
    status: DuplicateStatus
    confidence: float
    reasons: tuple[str, ...]


def connected_components(pairs: list[tuple[int, int]]) -> list[list[int]]:
    """Union-find connected components over an undirected candidate-pair graph.

    Grouping candidate files is not the same question as whether a group is
    safe to auto-confirm (see `DuplicateGroupBuilder`) -- this function only
    answers "which files were ever compared to each other at all", so it is
    reusable both for `DuplicateGroupBuilder.build()` and for
    `DuplicateService.detect()`'s initial (pre-evidence) grouping.
    """
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    for left, right in pairs:
        parent.setdefault(left, left)
        parent.setdefault(right, right)
        union(left, right)

    grouped: dict[int, list[int]] = {}
    for node in parent:
        grouped.setdefault(find(node), []).append(node)
    return [sorted(members) for members in grouped.values()]


class DuplicateGroupBuilder:
    """Graph-aware, non-transitive duplicate grouping (design §16).

    Connected components are computed purely from which files were ever
    compared at all. A component is eligible for `AUTO_CONFIRMED` only when
    *every* internal pairwise classification is `EXACT` or
    `AUDIO_EQUIVALENT`. A single `DIFFERENT`/`CONFLICT` edge anywhere inside
    an otherwise-confident component forces `REVIEW_REQUIRED` for the whole
    group -- naive transitive closure (e.g. trusting an A-B `AUDIO_EQUIVALENT`
    edge alone) never overrides a contradicting A-C edge elsewhere in the same
    component. A component whose edges are only `PROBABLE` (no outright
    contradictions, but no `EXACT`/`AUDIO_EQUIVALENT` confidence either) is
    also `REVIEW_REQUIRED` -- never silently auto-confirmed, and never
    silently dropped.
    """

    def build(self, pairs: list[ClassifiedPair]) -> list[DuplicateGroupDraft]:
        components = connected_components([(p.left_file_id, p.right_file_id) for p in pairs])

        file_to_component: dict[int, tuple[int, ...]] = {}
        for component in components:
            key = tuple(component)
            for file_id in component:
                file_to_component[file_id] = key

        edges_by_component: dict[tuple[int, ...], list[ClassifiedPair]] = {
            tuple(component): [] for component in components
        }
        for pair in pairs:
            key = file_to_component[pair.left_file_id]
            edges_by_component[key].append(pair)

        return [
            self._draft(tuple(component), edges_by_component[tuple(component)])
            for component in components
        ]

    def _draft(
        self, file_ids: tuple[int, ...], edges: list[ClassifiedPair]
    ) -> DuplicateGroupDraft:
        classifications = {edge.decision.classification for edge in edges}
        confidence = min((edge.decision.confidence for edge in edges), default=0.0)

        contradictions = classifications & _INCONSISTENT
        if contradictions:
            bad_edges = tuple(
                f'{edge.left_file_id}-{edge.right_file_id} classified '
                f'{edge.decision.classification.value}'
                for edge in edges
                if edge.decision.classification in _INCONSISTENT
            )
            return DuplicateGroupDraft(
                file_ids=file_ids,
                status=DuplicateStatus.REVIEW_REQUIRED,
                confidence=confidence,
                reasons=(
                    'conflicting or contradictory pairwise evidence within this group '
                    '(design §16: never rely on naive transitive closure)',
                )
                + bad_edges,
            )

        if PairClassification.PROBABLE in classifications:
            return DuplicateGroupDraft(
                file_ids=file_ids,
                status=DuplicateStatus.REVIEW_REQUIRED,
                confidence=confidence,
                reasons=(
                    'at least one PROBABLE pair -- plausible but not confident enough '
                    'for automatic consolidation',
                ),
            )

        return DuplicateGroupDraft(
            file_ids=file_ids,
            status=DuplicateStatus.AUTO_CONFIRMED,
            confidence=confidence,
            reasons=('every pairwise classification in this group is EXACT or AUDIO_EQUIVALENT',),
        )
