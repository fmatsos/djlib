from djlib.db.enums import TranscodeSuspicion
from djlib.duplicates.preferred import PreferredCandidate, PreferredFileSelector
from djlib.duplicates.quality import QualityResult


def _quality(
    integrity_ok: bool = True,
    lossless: bool = False,
    transcode_suspicion: TranscodeSuspicion = TranscodeSuspicion.NONE,
    clipping_detected: bool = False,
    audio_quality_score: float = 50.0,
    metadata_completeness: float = 0.0,
) -> QualityResult:
    return QualityResult(
        integrity_ok=integrity_ok,
        lossless=lossless,
        transcode_suspicion=transcode_suspicion,
        clipping_detected=clipping_detected,
        audio_quality_score=audio_quality_score,
        metadata_completeness=metadata_completeness,
        quality_score=audio_quality_score,
        details={},
    )


def test_failed_integrity_always_loses_regardless_of_everything_else() -> None:
    corrupt_but_nominally_perfect = PreferredCandidate(
        file_id=1,
        quality=_quality(integrity_ok=False, lossless=True, audio_quality_score=100.0),
    )
    valid = PreferredCandidate(
        file_id=2, quality=_quality(integrity_ok=True, lossless=False, audio_quality_score=10.0)
    )

    choice = PreferredFileSelector().choose([corrupt_but_nominally_perfect, valid])

    assert choice.file_id == 2


def test_clean_lossless_beats_a_clean_mp3() -> None:
    flac = PreferredCandidate(file_id=1, quality=_quality(lossless=True, audio_quality_score=100.0))
    mp3 = PreferredCandidate(file_id=2, quality=_quality(lossless=False, audio_quality_score=90.0))

    choice = PreferredFileSelector().choose([mp3, flac])

    assert choice.file_id == 1
    assert choice.reasons


def test_lossless_under_high_transcode_suspicion_loses_to_a_clean_mp3() -> None:
    # Design §21's own worked example: a nominally-lossless file must not
    # win purely on the "lossless"/resolution bonus when it is under HIGH
    # transcode suspicion.
    suspicious_flac = PreferredCandidate(
        file_id=1,
        quality=_quality(
            lossless=True,
            transcode_suspicion=TranscodeSuspicion.HIGH,
            audio_quality_score=100.0,
        ),
    )
    clean_mp3 = PreferredCandidate(
        file_id=2,
        quality=_quality(
            lossless=False,
            transcode_suspicion=TranscodeSuspicion.NONE,
            audio_quality_score=90.0,
        ),
    )

    choice = PreferredFileSelector().choose([suspicious_flac, clean_mp3])

    assert choice.file_id == 2


def test_metadata_completeness_breaks_a_close_tie() -> None:
    tagged = PreferredCandidate(
        file_id=1, quality=_quality(audio_quality_score=90.0, metadata_completeness=1.0)
    )
    untagged = PreferredCandidate(
        file_id=2, quality=_quality(audio_quality_score=90.0, metadata_completeness=0.0)
    )

    choice = PreferredFileSelector().choose([untagged, tagged])

    assert choice.file_id == 1


def test_metadata_completeness_cannot_offset_a_real_audio_quality_deficit() -> None:
    fully_tagged_but_worse = PreferredCandidate(
        file_id=1, quality=_quality(audio_quality_score=40.0, metadata_completeness=1.0)
    )
    untagged_but_better = PreferredCandidate(
        file_id=2, quality=_quality(audio_quality_score=90.0, metadata_completeness=0.0)
    )

    choice = PreferredFileSelector().choose([fully_tagged_but_worse, untagged_but_better])

    assert choice.file_id == 2


def test_historical_value_never_overrides_a_clear_technical_loss() -> None:
    # Milestone 1 has no real play-history source: historical_value is
    # represented as an optional field, injected here only to prove it is
    # ignored/last-resort even when present and lopsided.
    technically_superior_rarely_played = PreferredCandidate(
        file_id=1,
        quality=_quality(lossless=True, audio_quality_score=100.0),
        historical_value=1.0,
    )
    technically_worse_heavily_played = PreferredCandidate(
        file_id=2,
        quality=_quality(lossless=False, audio_quality_score=60.0),
        historical_value=999.0,
    )

    choice = PreferredFileSelector().choose(
        [technically_worse_heavily_played, technically_superior_rarely_played]
    )

    assert choice.file_id == 1


def test_historical_value_is_consulted_only_once_every_technical_dimension_ties() -> None:
    a = PreferredCandidate(
        file_id=1, quality=_quality(audio_quality_score=90.0, metadata_completeness=0.5),
        historical_value=5.0,
    )
    b = PreferredCandidate(
        file_id=2, quality=_quality(audio_quality_score=90.0, metadata_completeness=0.5),
        historical_value=50.0,
    )

    choice = PreferredFileSelector().choose([a, b])

    assert choice.file_id == 2
    assert any('historical value' in reason for reason in choice.reasons)


def test_historical_value_none_on_either_side_is_safely_ignored() -> None:
    a = PreferredCandidate(file_id=1, quality=_quality(audio_quality_score=90.0))
    b = PreferredCandidate(file_id=2, quality=_quality(audio_quality_score=90.0))

    choice = PreferredFileSelector().choose([a, b])

    assert choice.file_id == 1
    assert 'fully tied' in choice.reasons[-1]


def test_single_candidate_is_chosen_trivially() -> None:
    only = PreferredCandidate(file_id=7, quality=_quality())

    choice = PreferredFileSelector().choose([only])

    assert choice.file_id == 7
    assert choice.reasons
