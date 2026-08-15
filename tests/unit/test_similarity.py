from djlib.duplicates.similarity import metadata_similarity, version_compatibility
from djlib.duplicates.types import TrackIdentitySnapshot, VersionCompatibilityState


def _snapshot(
    artist: str | None = 'Artist',
    title: str | None = 'Title',
    version: str | None = None,
    edition: str | None = None,
    duration_ms: int | None = 300_000,
    featured: tuple[str, ...] = (),
) -> TrackIdentitySnapshot:
    return TrackIdentitySnapshot(
        artist_normalized=artist,
        title_normalized=title,
        version_normalized=version,
        edition_normalized=edition,
        duration_ms=duration_ms,
        featured_artist_normalized_names=featured,
    )


# -- version_compatibility: explicitly incompatible pairs (design §19) --


def test_original_mix_vs_radio_edit_is_incompatible() -> None:
    result = version_compatibility('Original Mix', 'Radio Edit')
    assert result.state == VersionCompatibilityState.INCOMPATIBLE
    assert result.same_string is False


def test_original_mix_vs_extended_mix_is_incompatible() -> None:
    result = version_compatibility('Original Mix', 'Extended Mix')
    assert result.state == VersionCompatibilityState.INCOMPATIBLE


def test_two_different_remixers_are_incompatible() -> None:
    result = version_compatibility('Nalin & Kane Remix', 'Sasha Remix')
    assert result.state == VersionCompatibilityState.INCOMPATIBLE


def test_radio_edit_vs_extended_mix_is_incompatible() -> None:
    result = version_compatibility('Radio Edit', 'Extended Mix')
    assert result.state == VersionCompatibilityState.INCOMPATIBLE


# Task 5's parser has no literal "Studio" marker: an untagged/empty version is
# treated as the implicit plain/default rendition, so it stands in for
# "Studio" here (the counterpart to an explicit "Live" annotation).
def test_live_vs_studio_is_incompatible() -> None:
    result = version_compatibility('Live', None)
    assert result.state == VersionCompatibilityState.INCOMPATIBLE


def test_studio_vs_live_is_incompatible_regardless_of_side() -> None:
    result = version_compatibility(None, 'Live')
    assert result.state == VersionCompatibilityState.INCOMPATIBLE


# Task 5's parser has no literal "Vocal" marker either: an untagged/empty
# version stands in for the full vocal rendition, conflicting with an explicit
# "Instrumental" annotation.
def test_instrumental_vs_vocal_is_incompatible() -> None:
    result = version_compatibility('Instrumental', None)
    assert result.state == VersionCompatibilityState.INCOMPATIBLE


def test_bootleg_vs_original_mix_is_incompatible() -> None:
    result = version_compatibility('Bootleg', 'Original Mix')
    assert result.state == VersionCompatibilityState.INCOMPATIBLE


def test_bootleg_vs_empty_is_incompatible() -> None:
    result = version_compatibility('Bootleg', None)
    assert result.state == VersionCompatibilityState.INCOMPATIBLE


# -- version_compatibility: three genuinely different outcomes --


def test_original_mix_vs_empty_is_compatible_weak_not_identical_not_incompatible() -> None:
    result = version_compatibility('Original Mix', None)
    assert result.state == VersionCompatibilityState.COMPATIBLE_WEAK
    assert result.same_string is False


def test_identical_explicit_version_is_compatible_with_same_string_true() -> None:
    result = version_compatibility('Extended Mix', 'Extended Mix')
    assert result.state == VersionCompatibilityState.COMPATIBLE
    assert result.same_string is True


def test_both_empty_is_compatible_but_not_same_string() -> None:
    result = version_compatibility(None, None)
    assert result.state == VersionCompatibilityState.COMPATIBLE
    assert result.same_string is False


def test_version_compatibility_is_case_and_whitespace_insensitive() -> None:
    result = version_compatibility('Extended Mix', '  extended   mix ')
    assert result.state == VersionCompatibilityState.COMPATIBLE
    assert result.same_string is True


# -- featuring-tolerance (design §9) --


def test_missing_feat_on_one_side_does_not_exclude_the_pair() -> None:
    left = _snapshot(featured=('DJ Sneak',))
    right = _snapshot(featured=())
    evidence = metadata_similarity(left, right)
    assert evidence.featured_artist_similarity is None
    assert evidence.metadata_similarity > 0.0
    assert evidence.version_compatibility.state != VersionCompatibilityState.INCOMPATIBLE


def test_no_featured_artists_on_either_side_is_no_signal() -> None:
    evidence = metadata_similarity(_snapshot(featured=()), _snapshot(featured=()))
    assert evidence.featured_artist_similarity is None


def test_matching_featured_artists_score_higher_than_conflicting_ones() -> None:
    matching = metadata_similarity(
        _snapshot(featured=('DJ Sneak',)), _snapshot(featured=('DJ Sneak',))
    )
    conflicting = metadata_similarity(
        _snapshot(featured=('DJ Sneak',)), _snapshot(featured=('Green Velvet',))
    )
    assert matching.featured_artist_similarity == 1.0
    assert conflicting.featured_artist_similarity == 0.0
    assert matching.metadata_similarity > conflicting.metadata_similarity


def test_conflicting_featured_artists_reduce_confidence_relative_to_missing_case() -> None:
    missing_side = metadata_similarity(_snapshot(featured=('DJ Sneak',)), _snapshot(featured=()))
    conflicting = metadata_similarity(
        _snapshot(featured=('DJ Sneak',)), _snapshot(featured=('Green Velvet',))
    )
    assert conflicting.featured_artist_similarity == 0.0
    assert missing_side.featured_artist_similarity is None
    assert conflicting.metadata_similarity < missing_side.metadata_similarity


# -- metadata_similarity composite --


def test_metadata_similarity_never_hides_an_incompatible_version_as_separate_signal() -> None:
    left = _snapshot(version='Original Mix')
    right = _snapshot(version='Extended Mix')
    evidence = metadata_similarity(left, right)
    assert evidence.artist_similarity == 1.0
    assert evidence.title_similarity == 1.0
    assert evidence.metadata_similarity == 1.0
    assert evidence.version_compatibility.state == VersionCompatibilityState.INCOMPATIBLE


def test_edition_mismatch_is_never_incompatible() -> None:
    left = _snapshot(edition='Remaster')
    right = _snapshot(edition=None)
    evidence = metadata_similarity(left, right)
    assert evidence.edition_compatibility.state == VersionCompatibilityState.COMPATIBLE_WEAK


def test_duration_delta_is_absolute_difference() -> None:
    evidence = metadata_similarity(_snapshot(duration_ms=300_000), _snapshot(duration_ms=301_500))
    assert evidence.duration_delta_ms == 1500


def test_duration_delta_is_none_when_either_side_unknown() -> None:
    evidence = metadata_similarity(_snapshot(duration_ms=None), _snapshot(duration_ms=300_000))
    assert evidence.duration_delta_ms is None
