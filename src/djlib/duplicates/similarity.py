from rapidfuzz import fuzz

from djlib.duplicates.types import (
    MetadataEvidence,
    TrackIdentitySnapshot,
    VersionCompatibility,
    VersionCompatibilityState,
)
from djlib.resolve.normalizer import normalize_identity

_FIVE_MINUTES_MS = 5 * 60 * 1000
_TEN_MINUTES_MS = 10 * 60 * 1000

# Annotations that describe a fundamentally different musical rendition rather
# than a mix/edit variant of the same one. When one side has no version
# annotation at all, an unannotated file is presumed to be the plain/default
# rendition (design §19's "Studio"/"Vocal" counterpart to Live/Instrumental),
# so these three markers conflict with an empty version outright. Any other
# explicit marker (e.g. "Original Mix", "Radio Edit") against an empty version
# is only weakened, per design §19's explicit "Original Mix vs empty" example
# -- absence of an annotation is evidence, not proof, of the default rendition.
_ALTERNATE_FORM_MARKERS = frozenset({'live', 'instrumental', 'bootleg'})

# RapidFuzz fuzz.ratio() threshold (0-100 scale) for the "fuzzy-close title"
# blocking tier. 85 tolerates small punctuation/whitespace/typo drift (e.g.
# "Don't You Want Me" vs "Dont You Want Me") while still requiring the bulk of
# the string to match -- short titles that merely share a few words score well
# below this on fuzz.ratio, so it stays conservative per the anti-false-positive
# rule in .claude/rules/duplicate-detection.md.
FUZZY_TITLE_THRESHOLD = 85.0


def duration_tolerance_ms(duration_ms: int) -> int:
    """Duration-bucket tolerance for blocking (design §14).

    Boundaries: a duration of exactly 5 minutes (300_000 ms) falls in the
    "<=5 min" bucket (2000 ms); a duration of exactly 10 minutes (600_000 ms)
    falls in the "5-10 min" bucket (3000 ms). Anything past 10 minutes gets
    the widest window (5000 ms), since longer tracks naturally have more
    encoder/tagging jitter in their reported duration.
    """
    if duration_ms <= _FIVE_MINUTES_MS:
        return 2000
    if duration_ms <= _TEN_MINUTES_MS:
        return 3000
    return 5000


def _normalized_or_empty(value: str | None) -> str:
    if value is None:
        return ''
    return normalize_identity(value)


def version_compatibility(left: str | None, right: str | None) -> VersionCompatibility:
    """Classify version-annotation compatibility for the automatic-merge path.

    Accepts either raw resolved strings or already-normalized strings --
    `normalize_identity` is idempotent, so callers holding
    `Track.version_normalized` can pass it straight through.
    """
    left_norm = _normalized_or_empty(left)
    right_norm = _normalized_or_empty(right)

    if left_norm == right_norm:
        return VersionCompatibility(
            state=VersionCompatibilityState.COMPATIBLE,
            same_string=bool(left_norm),
        )

    if not left_norm or not right_norm:
        non_empty = left_norm or right_norm
        if non_empty in _ALTERNATE_FORM_MARKERS:
            return VersionCompatibility(
                state=VersionCompatibilityState.INCOMPATIBLE, same_string=False
            )
        return VersionCompatibility(
            state=VersionCompatibilityState.COMPATIBLE_WEAK, same_string=False
        )

    return VersionCompatibility(state=VersionCompatibilityState.INCOMPATIBLE, same_string=False)


def edition_compatibility(left: str | None, right: str | None) -> VersionCompatibility:
    """Classify edition-annotation compatibility.

    Editions (remaster/reissue/anniversary edition) are a *much* weaker signal
    than versions (design §10.5/§20): a remaster is the same musical track with
    different mastering, not a different rendition. So unlike
    `version_compatibility`, this never returns INCOMPATIBLE -- a mismatched
    or missing edition only ever downgrades to COMPATIBLE_WEAK, it never
    excludes a pair from the automatic-merge path by itself.
    """
    left_norm = _normalized_or_empty(left)
    right_norm = _normalized_or_empty(right)

    if left_norm == right_norm:
        return VersionCompatibility(
            state=VersionCompatibilityState.COMPATIBLE,
            same_string=bool(left_norm),
        )
    return VersionCompatibility(state=VersionCompatibilityState.COMPATIBLE_WEAK, same_string=False)


def _ratio(left: str | None, right: str | None) -> float:
    left_norm = _normalized_or_empty(left)
    right_norm = _normalized_or_empty(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    return fuzz.ratio(left_norm, right_norm) / 100.0


def _featured_artist_similarity(
    left_names: tuple[str, ...], right_names: tuple[str, ...]
) -> float | None:
    if not left_names or not right_names:
        return None
    left_set = {normalize_identity(name) for name in left_names}
    right_set = {normalize_identity(name) for name in right_names}
    union = left_set | right_set
    if not union:
        return None
    return len(left_set & right_set) / len(union)


def _duration_delta_ms(left_ms: int | None, right_ms: int | None) -> int | None:
    if left_ms is None or right_ms is None:
        return None
    return abs(left_ms - right_ms)


def _composite_similarity(
    artist_similarity: float, title_similarity: float, featured_artist_similarity: float | None
) -> float:
    """Weighted composite of the honest per-signal similarities.

    Weights: artist 0.5, title 0.4, featured-artist overlap 0.1 when it carries
    a signal at all. When featured_artist_similarity is None (no signal, see
    `MetadataEvidence`), its weight is dropped and the remaining weights are
    renormalized rather than treating the missing signal as 0.0 -- a missing
    feat on one side must not be silently punished (design §9).
    """
    weighted = [(artist_similarity, 0.5), (title_similarity, 0.4)]
    if featured_artist_similarity is not None:
        weighted.append((featured_artist_similarity, 0.1))
    total_weight = sum(weight for _, weight in weighted)
    return sum(value * weight for value, weight in weighted) / total_weight


def metadata_similarity(
    left: TrackIdentitySnapshot, right: TrackIdentitySnapshot
) -> MetadataEvidence:
    artist_similarity = _ratio(left.artist_normalized, right.artist_normalized)
    title_similarity = _ratio(left.title_normalized, right.title_normalized)
    featured_artist_sim = _featured_artist_similarity(
        left.featured_artist_normalized_names, right.featured_artist_normalized_names
    )

    return MetadataEvidence(
        artist_similarity=artist_similarity,
        title_similarity=title_similarity,
        featured_artist_similarity=featured_artist_sim,
        version_compatibility=version_compatibility(left.version_normalized, right.version_normalized),
        edition_compatibility=edition_compatibility(left.edition_normalized, right.edition_normalized),
        duration_delta_ms=_duration_delta_ms(left.duration_ms, right.duration_ms),
        metadata_similarity=_composite_similarity(
            artist_similarity, title_similarity, featured_artist_sim
        ),
    )
