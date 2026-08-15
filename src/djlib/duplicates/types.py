from dataclasses import dataclass
from enum import StrEnum


class VersionCompatibilityState(StrEnum):
    COMPATIBLE = 'COMPATIBLE'
    COMPATIBLE_WEAK = 'COMPATIBLE_WEAK'
    INCOMPATIBLE = 'INCOMPATIBLE'


@dataclass(frozen=True)
class VersionCompatibility:
    """Result of comparing a version (or edition) annotation on two sides.

    `same_string` is True only when both sides carried the *same non-empty*
    normalized annotation -- it lets a caller distinguish an explicit textual
    match (e.g. "Extended Mix" == "Extended Mix") from the weaker case of both
    sides simply having no annotation at all (`state` is still COMPATIBLE, but
    `same_string` is False there, and also False for every COMPATIBLE_WEAK /
    INCOMPATIBLE result).
    """

    state: VersionCompatibilityState
    same_string: bool


@dataclass(frozen=True)
class TrackIdentitySnapshot:
    """The subset of a Track's already-resolved identity blocking/similarity need.

    Callers build this from Task 6 output (`Track.*_normalized`,
    `TrackFeaturedArtist`) and the file's own technical `duration_ms` -- nothing
    here is recomputed from raw tags.
    """

    artist_normalized: str | None
    title_normalized: str | None
    version_normalized: str | None
    edition_normalized: str | None
    duration_ms: int | None
    featured_artist_normalized_names: tuple[str, ...]


@dataclass(frozen=True)
class MetadataEvidence:
    """Pairwise metadata evidence for one candidate file pair (design §15).

    Each component is an honest, independently-inspectable signal. In
    particular `metadata_similarity` (the composite score) never overrides
    `version_compatibility`: a caller (Task 10's classifier) must check
    `version_compatibility.state` itself before treating a pair as
    automatic-merge eligible, no matter how high the composite score is.
    """

    artist_similarity: float
    title_similarity: float
    featured_artist_similarity: float | None
    version_compatibility: VersionCompatibility
    edition_compatibility: VersionCompatibility
    duration_delta_ms: int | None
    metadata_similarity: float


class BlockingTier(StrEnum):
    STRONG = 'STRONG'
    CANDIDATE = 'CANDIDATE'
    WEAK = 'WEAK'


@dataclass(frozen=True)
class CandidatePair:
    """One blocked duplicate candidate, at FILE granularity (design §15-16).

    `auto_merge_eligible` is False whenever `evidence.version_compatibility`
    is INCOMPATIBLE -- such a pair is still returned (not silently dropped)
    so Task 10 can surface it for human review as a `CONFLICT`, it is just
    marked ineligible for the automatic-merge path.
    """

    left_file_id: int
    right_file_id: int
    tier: BlockingTier
    evidence: MetadataEvidence
    auto_merge_eligible: bool
